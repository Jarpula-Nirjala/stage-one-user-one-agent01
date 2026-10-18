"""Required test 3: tool/model/data-store failures degrade to a smaller safe action or to no action."""
from datetime import timedelta

from fixtures import T0, ev, history_for, profile, runtime, world
from stage_agent.timeutil import iso


def ctrl(kind, dep, start, until):
    return {"at": iso(start), "type": kind, "dependency": dep, "until": iso(until), "detail": "test"}


def one_user_world(controls=(), faults=None, status="never"):
    u = "U-1"
    trig = ev(u, "app_open", T0, dialect="d1")
    pack = world([profile(u, status=status)], history_for(u), [trig], controls=controls, faults=faults)
    return pack, u, trig


def test_model_outage_falls_back_to_rules_without_waiting():
    c = ctrl("dependency_unavailable", "model_provider", T0 - timedelta(minutes=1), T0 + timedelta(minutes=5))
    pack, u, trig = one_user_world([c])
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert d["status"] == "PUBLISHED"
    assert "rules_only(model_unavailable)" in d["fallback_used"]
    assert d["latency_s"] < 2.0 and d["goal"] is not None


def test_circuit_breaker_stops_calling_a_dead_model():
    c = ctrl("dependency_unavailable", "model_provider", T0 - timedelta(minutes=1), T0 + timedelta(minutes=30))
    users = ["U-%d" % i for i in range(6)]
    hist = [e for u in users for e in history_for(u)]
    trig = [ev(u, "app_open", T0 + timedelta(seconds=i)) for i, u in enumerate(users)]
    pack = world([profile(u) for u in users], hist, trig, controls=[c])
    rt = runtime(pack)
    ds = [rt.on_trigger(t) for t in pack.triggers]
    assert rt.gateway.breaker.trips >= 1
    assert rt.gateway.stats["unavailable"] == 3          # exactly the failures that opened the circuit
    assert any("rules_only(model_breaker_open)" in d["fallback_used"] for d in ds)
    assert all(d["status"] == "PUBLISHED" for d in ds)


def test_malformed_json_is_parsed_safely_retried_then_accepted():
    f = {}
    pack, u, trig = one_user_world()
    pack.faults = {trig["event_id"]: {"trigger_event_id": trig["event_id"], "user_id": u, "fault": "malformed_output",
                                      "model_output": "{\"goal_id\": \"G02\", \"eligible\": tr"}}
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert "model_malformed_output" in d["resilience_events"] and "model_retry_after_malformed_output" in d["fallback_used"]
    assert d["model_calls"] == 2 and d["status"] == "PUBLISHED"


def test_persistent_malformed_output_falls_back_to_rules_within_call_budget():
    pack, u, trig = one_user_world()
    pack.faults = {trig["event_id"]: {"trigger_event_id": trig["event_id"], "user_id": u, "fault": "malformed_output", "model_output": "not json {"}}
    rt = runtime(pack, fault_persists_on_retry=True, max_model_calls=8)
    d = rt.on_trigger(pack.triggers[0])
    assert any(x.startswith("rules_only") for x in d["fallback_used"])
    assert d["model_calls"] == 2 <= 8 and d["status"] == "PUBLISHED"


def test_model_timeout_costs_only_the_client_timeout_not_the_provider_stall():
    pack, u, trig = one_user_world()
    pack.faults = {trig["event_id"]: {"trigger_event_id": trig["event_id"], "user_id": u, "fault": "timeout", "seconds": 45}}
    rt = runtime(pack, model_timeout_s=2.5)
    d = rt.on_trigger(pack.triggers[0])
    assert d["status"] == "PUBLISHED" and "rules_only(model_timeout)" in d["fallback_used"]
    assert 2.4 < d["latency_s"] < 6 and d["latency_s"] < 60


def test_unsupported_model_claim_is_rejected_by_the_verifier():
    pack, u, trig = one_user_world()
    claim = {"goal_id": "G04", "eligible": True, "confidence": 0.97, "reason": "The user is on an active trial that ends tomorrow (confirmed in billing)."}
    pack.faults = {trig["event_id"]: {"trigger_event_id": trig["event_id"], "user_id": u, "fault": "unsupported_claim", "model_output": claim}}
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert d["model_claims"][0]["accepted"] is False and "unsupported_claim" in d["model_claims"][0]["verifier"]
    assert d["goal"]["id"] != "G04" and "model_claim_rejected->rule_engine_goal" in d["fallback_used"]
    assert len(rt.unsupported_claims) == 1


def test_data_store_outage_gives_safe_minimal_page_no_goal_no_message():
    c = ctrl("dependency_unavailable", "user_data_store", T0 - timedelta(minutes=1), T0 + timedelta(minutes=5))
    u = "U-1"
    hist = history_for(u, plays=0, completes=0, opens=8)   # would normally qualify for a message
    pack = world([profile(u, status="lapsed", wa="true")], hist, [ev(u, "app_open", T0, dialect="d1")], controls=[c])
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert any(x.startswith("snapshot_cache") for x in d["fallback_used"])
    assert d["goal"] is None and d["action"] == "HOME_PAGE" and d["goal_candidates"] == {"deferred": True}
    assert [a["type"] for a in rt.effective_actions()] == ["home_page"]
    assert d["confidence"] < 0.6 and d["latency_s"] < 5


def test_tool_call_budget_is_enforced_and_decision_still_completes():
    pack, u, trig = one_user_world()
    rt = runtime(pack, max_tool_calls=4)
    d = rt.on_trigger(pack.triggers[0])
    assert d["tool_calls"] <= 4
    assert d["status"] in ("PUBLISHED", "NO_ACTION", "NO_ACTION_ABSTAIN")


def test_overload_drops_instead_of_publishing_late():
    pack, u, trig = one_user_world()
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0], queue_wait_s=40.0, shed=3)
    assert d["status"] == "NO_ACTION_OVERLOAD" and rt.effective_actions() == []
