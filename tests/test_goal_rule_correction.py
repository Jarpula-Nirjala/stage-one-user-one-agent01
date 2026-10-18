"""Required test 4: a corrected goal rule re-judges only the affected open goals, versioned, without reprocessing everyone."""
from datetime import timedelta

from fixtures import T0, ev, history_for, profile, runtime, world
from stage_agent.goals import parse_condition
from stage_agent.timeutil import IST, iso

CORR = {"at": iso(T0 + timedelta(hours=1)), "type": "goal_rule_corrected", "goal_id": "G03",
        "old_success": {"event": "app_open", "condition": "on a later IST calendar day within the window"},
        "new_success": {"event": "play_started", "condition": "on a later IST calendar day within the window"},
        "applies_to": "every G03 goal that is still open, including goals opened before this correction", "note": "test"}


def build():
    new_users = ["N-%d" % i for i in range(3)]       # signed up yesterday, already played and completed => G03
    others = ["O-%d" % i for i in range(5)]          # long-time viewers => other goals
    profs, hist, trig = [], [], []
    for i, u in enumerate(new_users):
        profs.append(profile(u, signup=iso(T0 - timedelta(days=1))))
        hist += history_for(u, plays=2, completes=1, opens=2, days_ago=1)
        trig.append(ev(u, "app_open", T0 + timedelta(minutes=i)))
    for i, u in enumerate(others):
        profs.append(profile(u))
        hist += history_for(u)
        trig.append(ev(u, "app_open", T0 + timedelta(minutes=10 + i)))
    return world(profs, hist, trig), new_users, others


def test_correction_touches_only_open_g03_goals():
    pack, new_users, others = build()
    rt = runtime(pack)
    for t in pack.triggers:
        rt.on_trigger(t)
    g03 = {u for u in rt.viewers if rt.viewers[u].active_goal and rt.viewers[u].active_goal.goal_id == "G03"}
    assert g03 == set(new_users)
    before = {u: rt.viewers[u].active_goal.rule.version for u in g03}
    others_before = {u: [g.rule.version for g in rt.viewers[u].goals] for u in others if rt.viewers[u].goals}
    rec = rt.on_control(CORR, T0 + timedelta(hours=1))
    assert rec["open_goals_revised"] == 3 and rec["reprocessed_all_viewers"] is False
    assert rec["viewers_total"] >= 8 and rec["open_goals_revised"] < rec["viewers_total"]
    for u in g03:
        g = rt.viewers[u].active_goal
        assert g.rule.event == "play_started" and g.rule.version == before[u] + 1
        assert g.revisions[-1]["event"] == "rule_corrected"
    assert {u: [g.rule.version for g in rt.viewers[u].goals] for u in others if rt.viewers[u].goals} == others_before
    assert rt.reg.rule_log[-1]["retroactive_to_open_goals"] is True


def test_open_after_correction_no_longer_counts_but_a_play_on_a_later_day_does():
    pack, new_users, _ = build()
    rt = runtime(pack)
    for t in pack.triggers:
        rt.on_trigger(t)
    rt.on_control(CORR, T0 + timedelta(hours=1))
    u = new_users[0]
    g = rt.viewers[u].active_goal
    from stage_agent.goals import evaluate_goal
    next_day = T0 + timedelta(days=1, hours=2)
    only_open = [__import__("stage_agent.loader", fromlist=["Ev"]).Ev(ev(u, "app_open", next_day))]
    assert evaluate_goal(g, only_open, rt.cat)[0] is False                     # old rule would have said yes
    play = [__import__("stage_agent.loader", fromlist=["Ev"]).Ev(ev(u, "play_started", next_day, {"title_id": "T0001", "episode_id": "x"}))]
    assert evaluate_goal(g, play, rt.cat)[0] is True
    same_day = [__import__("stage_agent.loader", fromlist=["Ev"]).Ev(ev(u, "play_started", T0 + timedelta(hours=2), {"title_id": "T0001", "episode_id": "x"}))]
    assert evaluate_goal(g, same_day, rt.cat)[0] is False                      # same IST day as the goal start


def test_condition_parser_and_unmeasurable_goals():
    assert parse_condition("play_started", "on a later IST calendar day than the goal start").kind == "later_day"
    assert parse_condition("purchase_completed", "is_trial = false").kind == "paid_purchase"
    assert parse_condition("play_started", "on 3 distinct IST days").n == 3
    assert parse_condition(None, "engages meaningfully").kind == "unmeasurable"
    pack, _, _ = build()
    rt = runtime(pack)
    assert rt.reg.measurable("G10")[0] is False and "null" in rt.reg.measurable("G10")[1]
    pack.event_vocab.pop("tv_app_open", None)
    assert runtime(pack).reg.measurable("G11")[0] is False
