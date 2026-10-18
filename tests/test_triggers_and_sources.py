"""Required tests 1 and 2: late/duplicate triggers and conflicting sources."""
from datetime import timedelta

from fixtures import SNAP, T0, ev, history_for, profile, runtime, world
from stage_agent.loader import Ev


def E(raw):
    return Ev(dict(raw, deliver_at=raw["received_at"]))


def basic_world(**kw):
    u = "U-1"
    hist = history_for(u)
    trig = ev(u, "app_open", T0, dialect="d1")
    return world([profile(u)], hist, [trig]), u, trig


def test_duplicate_trigger_creates_one_action_only():
    pack, u, trig = basic_world()
    rt = runtime(pack)
    t = pack.triggers[0]
    d1 = rt.on_trigger(t)
    n_actions = len(rt.effective_actions())
    d2 = rt.on_trigger(t)                       # same event_id delivered again
    assert d1["status"] == "PUBLISHED"
    assert d2["status"] == "DUPLICATE_SUPPRESSED" and d2["duplicate_of"] == d1["decision_id"]
    assert len(rt.effective_actions()) == n_actions


def test_second_trigger_in_same_session_does_not_publish_again():
    pack, u, trig = basic_world()
    rt = runtime(pack)
    rt.on_trigger(pack.triggers[0])
    second = E(ev(u, "login_success", T0 + timedelta(seconds=5), session=trig["session_id"]))
    d = rt.on_trigger(second)
    assert d["status"] == "NO_ACTION_SESSION_DUP"
    assert sum(1 for a in rt.effective_actions() if a["type"] == "home_page") == 1


def test_buffered_open_days_old_is_not_a_live_session():
    u = "U-1"
    stale = ev(u, "app_open", T0, dialect="d1", age=3 * 86400)
    pack = world([profile(u)], history_for(u), [stale])
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert d["status"] == "NO_ACTION_STALE" and "stale_trigger" in d["no_action_reason"]
    assert d["trigger"]["event_age_s"] == 3 * 86400
    assert rt.effective_actions() == []


def test_wrong_phone_clock_but_live_is_not_mistaken_for_stale():
    u = "U-1"
    skewed = ev(u, "app_open", T0, dialect="d1", skew=-6 * 3600)   # phone clock 6 h behind: received - occurred is huge, sent - occurred is 0
    pack = world([profile(u)], history_for(u), [skewed])
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert d["status"] == "PUBLISHED"
    assert d["trigger"]["event_age_s"] == 0


def test_history_duplicates_are_collapsed_keeping_first_delivery():
    u = "U-1"
    e1 = ev(u, "play_started", T0 - timedelta(days=2), {"title_id": "T0001", "episode_id": "s-d1-00-s1e1"}, eid="same")
    e2 = dict(e1, received_at=(T0 - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    pack = world([profile(u)], [e1, e2], [ev(u, "app_open", T0)])
    assert len(pack.events[u]) == 1 and pack.events[u][0].dup_count == 1


def test_three_way_dialect_conflict_resolved_by_app_setting_and_kept_visible():
    u = "U-1"
    trig = ev(u, "app_open", T0, dialect="d1", props={"deeplink": "ott://c/cmp001/d2/title/T0015"})
    pack = world([profile(u, dialect="d3")], history_for(u), [trig])
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert d["dialect"]["used"] == "d1" and d["dialect"]["source"] == "app_setting"
    assert set(d["dialect"]["conflicts"]) == {"profile:d3", "campaign:d2"}
    assert "three_way_dialect_conflict" in d["conflicts"]
    page = next(a for a in rt.effective_actions() if a["type"] == "home_page")
    assert page["dialect"] == "d1"
    assert all(rt.cat.titles[t].dialect == "d1" for r in page["rows"] for t in r["title_ids"])


def test_invalid_trigger_dialect_falls_back_to_last_valid_setting_and_never_messages():
    u = "U-1"
    hist = history_for(u, dialect="d2") + [ev(u, "app_open", T0 - timedelta(days=1), dialect="d2")]
    pack = world([profile(u, dialect="d1")], hist, [ev(u, "app_open", T0, dialect="NA")])
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert d["dialect"]["used"] == "d2" and d["dialect"]["source"] == "last_valid_app_setting"
    assert d["confidence"] < 0.7 and "invalid" in d["confidence_reason"]
    assert all(a["type"] in ("home_page",) for a in rt.effective_actions())


def test_events_beat_profile_when_profile_says_never_but_a_purchase_exists():
    u = "U-1"
    hist = history_for(u) + [ev(u, "purchase_completed", SNAP - timedelta(days=3), {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})]
    pack = world([profile(u, status="never")], hist, [ev(u, "app_open", T0)])
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert "subscription:profile:never!=events:paid_before_snapshot" in d["conflicts"]
    assert (d["goal"] or {}).get("id") != "G05"          # a payer is not eligible for "first paid"
    sub = next(f for f in d["viewer_facts"] if f["name"] == "subscription_status")
    assert sub["value"] == "active"


def test_newer_purchase_event_overrides_stale_snapshot():
    u = "U-1"
    hist = history_for(u) + [ev(u, "purchase_completed", SNAP + timedelta(hours=3), {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})]
    pack = world([profile(u, status="lapsed")], hist, [ev(u, "app_open", T0)])
    d = runtime(pack).on_trigger(pack.triggers[0])
    sub = next(f for f in d["viewer_facts"] if f["name"] == "subscription_status")
    assert sub["value"] == "active" and "event_newer" in sub["source"]
    assert (d["goal"] or {}).get("id") not in ("G06", "G05")


def test_duplicate_profile_rows_that_disagree_block_status_dependent_goals():
    u = "U-1"
    rows = [profile(u, status="never", updated="2026-09-29T00:00:00.000Z"), profile(u, status="lapsed", updated="2026-09-12T00:00:00.000Z")]
    pack = world(rows, history_for(u), [ev(u, "app_open", T0)])
    rt = runtime(pack)
    d = rt.on_trigger(pack.triggers[0])
    assert "subscription:duplicate_profile_rows_disagree" in d["conflicts"]
    assert any(c.startswith("profile_rows:subscription_status") for c in d["conflicts"])
    for g in ("G04", "G05", "G06", "G08", "G09"):
        assert g in d["goal_candidates"]["rejected"] and "ambiguous" in d["goal_candidates"]["rejected"][g][0]


def test_brand_new_viewer_without_profile_row_is_built_from_signup_event():
    u = "U-NEW"
    signup = ev(u, "signup_completed", T0 - timedelta(hours=2), {"method": "phone_otp"})
    pack = world([], [signup], [ev(u, "app_open", T0)], arms={u: "agent"})
    d = runtime(pack).on_trigger(pack.triggers[0])
    assert d["goal"]["id"] == "G01"
    assert any("no profile row" in a for a in d["assumptions"])
    assert not any(a["type"] in ("push", "whatsapp") for a in [])  # no profile => no push/WhatsApp consent
