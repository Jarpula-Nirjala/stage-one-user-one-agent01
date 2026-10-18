"""Safety rules, verifier independence, live revisions (purchase, withdrawal, opt-out) and the measurement maths."""
from datetime import timedelta

import pytest

from fixtures import SNAP, T0, ev, history_for, profile, runtime, world
from stage_agent.audit import audit_actions
from stage_agent.impact import newcombe_diff, perm_p, stratified_diff
from stage_agent.loader import Ev
from stage_agent.timeutil import IST, in_quiet_hours, iso, ist_date
from stage_agent.verifier import Verifier


def lapsed_world(push="true", wa="false", at=T0, extra_hist=(), arm="agent"):
    """A recently lapsed viewer who opens the app but never plays in-session: message-worthy by design."""
    u = "U-L"
    hist = history_for(u, plays=0, completes=0, opens=8) + [ev(u, "subscription_lapsed", T0 - timedelta(days=5))] + list(extra_hist)
    return world([profile(u, status="lapsed", push=push, wa=wa)], hist, [ev(u, "app_open", at)], arms={u: arm}), u


def test_holdout_viewer_gets_zero_actions_and_no_model_call():
    pack, u = lapsed_world(arm="holdout")
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert d["status"] == "HOLDOUT_NO_ACTION" and rt.effective_actions() == [] and d["model_calls"] == 0
    assert d["counterfactual_goal"] == "G06"          # computed for measurement only
    assert rt.snapshot(u)["action_ids"] == []


def test_message_goes_out_only_with_consent_in_hours_once_per_day():
    pack, u = lapsed_world(push="true")
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    msgs = [a for a in rt.effective_actions() if a["type"] in ("push", "whatsapp")]
    assert len(msgs) == 1 and msgs[0]["type"] == "push" and d["goal"]["id"] == "G06"
    send = Ev(dict(ev(u, "app_open", T0), received_at=msgs[0]["send_at"])).recv
    assert not in_quiet_hours(send) and len(msgs[0]["text"]["title"]) <= 40 and len(msgs[0]["text"]["body"]) <= 120
    assert msgs[0]["deeplink"] == "ott://title/%s" % msgs[0]["title_id"]
    assert rt.cat.titles[msgs[0]["title_id"]].dialect == "d1"


def test_no_consent_means_no_push_or_whatsapp_only_in_app():
    pack, u = lapsed_world(push="false", wa="false")
    rt = runtime(pack)
    rt.on_trigger(pack.triggers[0])
    assert not any(a["type"] in ("push", "whatsapp") for a in rt.effective_actions())


def test_opt_out_event_overrides_profile_consent():
    optout = ev("U-L", "notification_opt_out", T0 - timedelta(days=2), {"channel": "push"})
    pack, u = lapsed_world(push="true", extra_hist=[optout])
    rt = runtime(pack)
    rt.on_trigger(pack.triggers[0])
    assert not any(a["type"] == "push" for a in rt.effective_actions())


def test_quiet_hours_trigger_schedules_for_the_morning_instead():
    night = T0.replace(hour=20, minute=0)                 # 01:30 IST
    pack, u = lapsed_world(at=night)
    rt = runtime(pack)
    rt.on_trigger(pack.triggers[0])
    msg = next(a for a in rt.effective_actions() if a["type"] == "push")
    send = Ev(dict(ev(u, "app_open", T0), received_at=msg["send_at"])).recv
    assert not in_quiet_hours(send) and send > night


def test_frequency_cap_counts_history_and_blocks_a_second_message_that_ist_day():
    delivered = ev("U-L", "message_delivered", T0 - timedelta(hours=1), {"channel": "whatsapp", "campaign_id": "c", "title_id": "T0001"})
    pack, u = lapsed_world(extra_hist=[delivered])
    rt = runtime(pack)
    rt.on_trigger(pack.triggers[0])
    day = ist_date(T0 - timedelta(hours=1))
    for a in rt.effective_actions():
        if a["type"] in ("push", "whatsapp"):
            assert ist_date(Ev(dict(ev(u, "app_open", T0), received_at=a["send_at"])).recv) != day


def test_verifier_rejects_what_the_planner_might_get_wrong():
    pack, u = lapsed_world()
    rt = runtime(pack)
    v: Verifier = rt.verifier
    page_bad = {"type": "home_page", "dialect": "d1", "hero_title_id": "T0016", "rows": [{"row_type": "popular_in_dialect", "title_ids": ["T0001", "T0002"]},
                                                                                         {"row_type": "continue_watching", "title_ids": ["T0003", "T0004", "T0005"]}]}
    r = v.verify_page(u, page_bad, T0, "d1")
    failed = {f["check"] for f in r.failed()}
    assert {"dialect", "page_structure", "no_continue_watching"} <= failed        # d2 hero on a d1 page, 2-title row, forbidden row type
    push = {"action_id": "A-x", "type": "push", "title_id": "T0004", "send_at": iso(T0 + timedelta(hours=1)), "deeplink": "ott://title/T0004",
            "text": {"title": "Hurry! Last chance for Title d1 03", "body": "Only today ₹99 offer on Title d1 03"}}
    r = v.verify_message(u, push, T0, "d1", "app_setting")
    failed = {f["check"] for f in r.failed()}
    assert {"forbidden_phrases", "honest_copy_no_unlisted_price_or_offer", "injection_quarantine"} <= failed
    r2 = v.verify_message(u, dict(push, text={"title": "Watch Title d1 03", "body": "Title d1 03 is available now. ₹149 per month."}), T0, "d1", "app_setting")
    assert "honest_copy_no_unlisted_price_or_offer" in r2.passed()                # the one real price is allowed


def test_injected_catalogue_text_is_flagged_discarded_and_never_promoted_in_messages():
    pack, u = lapsed_world()
    rt = runtime(pack)
    flagged = [t for t in rt.cat.titles.values() if t.injection_flags]
    assert [t.id for t in flagged] == ["T0004"]
    assert not hasattr(flagged[0], "description")                                  # the text cannot be read downstream
    assert any(e["source"] == "catalogue.description" and e["title_id"] == "T0004" for e in rt.security_log)
    rt.on_trigger(pack.triggers[0])
    assert all(a.get("title_id") != "T0004" for a in rt.effective_actions())


def test_sparse_dialect_row_below_three_titles_is_dropped_not_published():
    u = "U-S"
    pack = world([profile(u, dialect="d3")], history_for(u, dialect="d3"), [ev(u, "app_open", T0, dialect="d3")])
    rt = runtime(pack)
    rt.on_trigger(pack.triggers[0])
    for a in rt.effective_actions():
        if a["type"] == "home_page":
            assert a["rows"] == [] or all(len(r["title_ids"]) >= 3 for r in a["rows"])


def test_unavailable_titles_are_never_on_a_page():
    pack, u = lapsed_world()
    rt = runtime(pack)
    rt.on_trigger(pack.triggers[0])
    page = next(a for a in rt.effective_actions() if a["type"] == "home_page")
    ids = [t for r in page["rows"] for t in r["title_ids"]] + [page["hero_title_id"]]
    assert all(rt.cat.is_available(t, T0) for t in ids)
    slugs = [rt.cat.titles[t].slug for t in ids]
    assert len(slugs) == len(set(slugs)) and len(page["rows"]) <= 6


def test_withdrawn_title_is_pulled_from_live_pages_and_pending_messages():
    pack, u = lapsed_world()
    rt = runtime(pack)
    rt.on_trigger(pack.triggers[0])
    page = next(a for a in rt.effective_actions() if a["type"] == "home_page")
    target = page["hero_title_id"]
    rec = rt.on_control({"at": iso(T0 + timedelta(minutes=30)), "type": "title_withdrawn", "title_id": target, "reason": "licence"}, T0 + timedelta(minutes=30))
    assert rec["pages_republished"] >= 1
    live = [a for a in rt.effective_actions() if a["type"] == "home_page"][-1]
    ids = [live["hero_title_id"]] + [t for r in live["rows"] for t in r["title_ids"]]
    assert target not in ids and live["supersedes"] == page["action_id"]
    assert not rt.cat.is_available(target, T0 + timedelta(minutes=31))


def test_purchase_after_decision_replans_and_cancels_pending_messages():
    u = "U-P"
    hist = history_for(u, plays=0, completes=0, opens=8)
    pack = world([profile(u, status="lapsed")], hist + [ev(u, "subscription_lapsed", T0 - timedelta(days=4))], [ev(u, "app_open", T0)],
                 live=[ev(u, "purchase_completed", T0 + timedelta(minutes=2), {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})])
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    pending = [a for a in rt.effective_actions() if a["type"] in ("push", "whatsapp", "in_app_message")]
    assert d["goal"]["id"] == "G06" and pending
    out = rt.on_live_event(pack.live[0])
    assert any(r["type"] == "goal_closed_by_event" for r in out) and any(r["type"] == "replan" for r in out)
    assert rt.viewers[u].goals[0].status == "succeeded"
    ng = rt.viewers[u].active_goal
    assert ng is not None and ng.goal_id not in ("G04", "G05", "G06")                # a payer no longer gets money goals
    assert not [a for a in rt.effective_actions() if a["type"] in ("push", "whatsapp") and a["goal_id"] == "G06"]
    assert any(x["status"] == "REPLANNED" for x in rt.decisions)


def test_opt_out_arriving_before_send_time_cancels_the_pending_push():
    pack, u = lapsed_world()
    rt = runtime(pack)
    rt.on_trigger(pack.triggers[0])
    assert any(a["type"] == "push" for a in rt.effective_actions())
    rt.on_live_event(Ev(dict(ev(u, "notification_opt_out", T0 + timedelta(minutes=1), {"channel": "push"}), deliver_at=iso(T0 + timedelta(minutes=1)))))
    assert not any(a["type"] == "push" for a in rt.effective_actions())


def test_audit_catches_violations_independently_of_the_runtime():
    pack, u = lapsed_world(push="false")
    bad = [{"action_id": "A-1", "user_id": u, "type": "push", "published_at": iso(T0), "send_at": iso(T0.replace(hour=20)), "title_id": "T0001",
            "text": {"title": "Hurry", "body": "20% off"}, "deeplink": "ott://title/T0001"}]
    kinds = audit_actions(pack, bad)["violations_by_kind"]
    assert {"no_consent", "quiet_hours", "forbidden_phrase", "unlisted_price_or_offer"} <= set(kinds)


def test_reference_goal_does_not_depend_on_arm():
    a, u = lapsed_world(arm="agent")
    b, _ = lapsed_world(arm="holdout")
    ra, rb = runtime(a), runtime(b)
    ra.on_trigger(a.triggers[0])
    rb.on_trigger(b.triggers[0])
    assert ra.viewers[u].analysis_goal["goal_id"] == rb.viewers[u].analysis_goal["goal_id"]


def test_stats_helpers_behave():
    d, lo, hi = newcombe_diff(40, 100, 20, 100)
    assert 0.19 < d < 0.21 and lo > 0 and hi > d
    d, lo, hi = newcombe_diff(10, 100, 12, 100)
    assert lo < 0 < hi
    assert perm_p([1] * 40 + [0] * 60, [1] * 20 + [0] * 80, draws=2000) < 0.01
    assert perm_p([1] * 10 + [0] * 90, [1] * 11 + [0] * 89, draws=2000) > 0.5
    pooled, plo, phi = stratified_diff({"a": (30, 100, 20, 100), "b": (3, 50, 4, 50)})
    assert plo < pooled < phi


def test_naive_served_vs_rest_is_biased_while_itt_is_not():
    """Synthetic selection: active people are served AND convert more; the agent has zero true effect."""
    import random
    rng = random.Random(3)
    agent, hold = [], []
    for _ in range(4000):
        active = rng.random() < 0.3
        p = 0.5 if active else 0.1
        arm = rng.random() < 0.5
        served = arm and active                      # only active viewers ever see the page
        (agent if arm else hold).append((served, rng.random() < p))
    served = [y for s, y in agent if s]
    rest = [y for s, y in agent if not s] + [y for _, y in hold]
    naive = sum(served) / len(served) - sum(rest) / len(rest)
    itt = sum(y for _, y in agent) / len(agent) - sum(y for _, y in hold) / len(hold)
    assert naive > 0.2 and abs(itt) < 0.04


@pytest.mark.skipif(not __import__("os").path.exists(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "..", "pack", "replay")),
                    reason="data pack not present")
def test_full_pack_replay_is_clean_and_fast():
    import os
    from stage_agent.agent import Runtime
    from stage_agent.config import Config
    from stage_agent.harness import run_replay
    from stage_agent.loader import load_pack
    pack = load_pack(os.path.join(os.path.dirname(__file__), "..", "..", "pack"))
    rt = Runtime(pack, Config())
    run_replay(rt)
    assert audit_actions(pack, rt.effective_actions())["clean"]
    assert max(d["latency_s"] for d in rt.decisions) < 60
    assert not any(pack.arms[a["user_id"]] == "holdout" for a in rt.effective_actions())
