"""Outcome judge: closes the loop. Judges every goal from attributable events only and writes what was learned back
into each viewer's durable state (and into a small cross-viewer prior file used by later runs)."""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .agent import Runtime, ViewerRecord
from .goals import GoalInstance, SuccessRule, evaluate_goal, parse_condition
from .loader import Ev
from .timeutil import iso, parse_ts


def load_outcome_events(sim_dir: str) -> List[Ev]:
    path = os.path.join(sim_dir, "outcome_events.jsonl")
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                raw = json.loads(line)
                out.append(_ev(raw))
    return out


def _ev(raw: dict) -> Ev:
    raw = dict(raw)
    raw.setdefault("occurred_at", raw["received_at"])
    raw.setdefault("sent_at", raw["received_at"])
    return Ev(raw)


class Observations:
    """Everything observed after the replay started, per viewer, de-duplicated by event_id (the simulator re-emits the
    scripted live events, so naive concatenation would double count)."""

    def __init__(self, rt: Runtime, sim_events: List[Ev]):
        self.by_user: Dict[str, List[Ev]] = defaultdict(list)
        seen = set()
        pools = [e for e in rt.pack.live] + list(sim_events)
        for e in pools:
            if e.id in seen:
                continue
            seen.add(e.id)
            self.by_user[e.user].append(e)
        for lst in self.by_user.values():
            lst.sort(key=lambda e: (e.est, e.id))
        self.horizon = max((e.recv for e in sim_events), default=None)
        self.pages_shown: Dict[str, List[Ev]] = defaultdict(list)
        self.delivered: Dict[str, List[Ev]] = defaultdict(list)
        self.failed: Dict[str, List[Ev]] = defaultdict(list)
        self.optouts: Dict[str, List[Ev]] = defaultdict(list)
        for e in sim_events:
            if e.kind == "home_page_shown" and e.props.get("personalised"):
                self.pages_shown[e.user].append(e)
            elif e.kind == "message_delivered":
                self.delivered[e.user].append(e)
            elif e.kind == "message_failed":
                self.failed[e.user].append(e)
            elif e.kind == "notification_opt_out":
                self.optouts[e.user].append(e)

    def events(self, user: str, after: datetime) -> List[Ev]:
        return [e for e in self.by_user.get(user, []) if e.recv > after - timedelta(days=1)]


def judge_goal(g: GoalInstance, obs: Observations, rt: Runtime, user: str, closed_at: Optional[datetime] = None) -> dict:
    """succeeded | failed | inconclusive with the reason. Only events whose estimated true time lies inside the goal
    window can count; late arrivals that happened before the window are surfaced, never counted."""
    evs = obs.events(user, g.start)
    until = None
    ok, evid, late = evaluate_goal(g, evs, rt.cat, until=until)
    first_expo = None
    cands = [e.recv for e in obs.pages_shown.get(user, []) if e.recv >= g.start] + [e.recv for e in obs.delivered.get(user, []) if e.recv >= g.start]
    if cands:
        first_expo = min(cands)
    j = {"status": None, "reason": "", "evidence": evid, "late_unattributable_events": late, "window": [iso(g.start), iso(g.deadline)],
         "rule_version": g.rule.version, "success_rule": g.rule.to_dict()}
    if ok:
        succ = next(e for e in evs if e.id == evid[0]) if evid else None
        j["status"], j["reason"] = "succeeded", "success event %s inside the window" % evid[:1]
        if succ is not None:
            j["success_at"] = iso(succ.est)
            j["exposed_before_success"] = bool(first_expo and first_expo <= succ.recv)
            if not j["exposed_before_success"]:
                j["attribution_note"] = "success preceded any page view/message delivery from the agent: organic, do not credit the agent"
    elif late:
        j["status"], j["reason"] = "inconclusive", "only late-delivered events matched, and they happened before the goal started (not attributable)"
    elif obs.horizon is not None and obs.horizon < g.deadline - timedelta(minutes=1):
        j["status"], j["reason"] = "inconclusive", "observation ends %s, before the deadline %s" % (iso(obs.horizon), iso(g.deadline))
    elif g.status == "superseded" and g.closed_at and g.closed_at < g.deadline and closed_at is not None:
        j["status"], j["reason"] = "inconclusive", "goal was superseded at %s before its deadline" % iso(g.closed_at)
    else:
        j["status"], j["reason"] = "failed", "no matching event in the window and the window fully observed"
    j["page_views_after_start"] = sum(1 for e in obs.pages_shown.get(user, []) if e.recv >= g.start)
    j["messages_delivered_after_start"] = sum(1 for e in obs.delivered.get(user, []) if e.recv >= g.start)
    return j


def reference_instance(rt: Runtime, ag: dict) -> GoalInstance:
    spec = rt.reg.specs[ag["goal_id"]]
    start = parse_ts(ag["start"])
    return GoalInstance(ag["goal_id"], spec.name, start, start + timedelta(hours=spec.window_hours), spec.window_hours,
                        SuccessRule(**spec.rule.__dict__), dict(ag.get("params") or {}))


def lesson_for(g: GoalInstance, j: dict, v: ViewerRecord, obs: Observations) -> dict:
    uid = v.viewer_id
    expo, msgs = j["page_views_after_start"], j["messages_delivered_after_start"]
    failed_msgs = [e.props.get("reason") for e in obs.failed.get(uid, [])]
    optouts = [e.props.get("channel") for e in obs.optouts.get(uid, [])]
    if j["status"] == "succeeded" and j.get("exposed_before_success"):
        lesson, hint = "goal reached after a personalised exposure; the content approach is worth keeping", "keep_approach"
    elif j["status"] == "succeeded":
        lesson, hint = "goal reached organically before any exposure; no credit to the agent", "viewer_self_motivated"
    elif j["status"] == "failed" and expo == 0:
        lesson, hint = "no personalised page was ever shown (viewer did not return in-window or the dialect did not match); nothing to learn about content", "check_reach"
    elif j["status"] == "failed":
        lesson, hint = "viewer saw %d personalised page(s) and %d message(s) without reaching the goal; try a different goal or channel next" % (expo, msgs), "escalate_or_change_goal"
    else:
        lesson, hint = "inconclusive: " + j["reason"], "re-judge_when_window_closes"
    if "token_invalid" in failed_msgs:
        lesson += "; push token invalid for this viewer (prefer WhatsApp/in-app)"
        hint = "avoid_push"
    if optouts:
        lesson += "; viewer opted out of %s after a message: stop that channel" % sorted(set(optouts))
        hint = "respect_opt_out"
    return {"goal_id": g.goal_id, "status": j["status"], "lesson": lesson, "next_step_hint": hint, "page_views": expo, "messages": msgs}


def ref_outcomes(rt: Runtime, obs: "Observations") -> List[dict]:
    """Judge the reference (analysis) goal of every triggered viewer, in both arms, against one set of observations."""
    rows = []
    for u, v in sorted(rt.viewers.items()):
        ag = v.analysis_goal
        if ag and ag.get("goal_id"):
            gi = reference_instance(rt, ag)
            j = judge_goal(gi, obs, rt, u)
            rows.append({"viewer_id": u, "arm": v.arm, "goal_id": gi.goal_id, "start": ag["start"], "deadline": iso(gi.deadline),
                         "outcome": j["status"], "evidence": j["evidence"], "rule_version": gi.rule.version,
                         "page_views_after_start": j["page_views_after_start"],
                         "messages_delivered_after_start": j["messages_delivered_after_start"]})
    return rows


def judge_all(rt: Runtime, sim_dir: str, out_dir: str):
    sim_events = load_outcome_events(sim_dir)
    obs = Observations(rt, sim_events)
    outcomes = []
    status_counts, by_goal = Counter(), defaultdict(Counter)
    for u, v in sorted(rt.viewers.items()):
        for g in v.goals:
            j = judge_goal(g, obs, rt, u, closed_at=g.closed_at)
            g.judgment = j
            if g.status in ("active", "expired"):
                g.status = j["status"]
            outcomes.append({"viewer_id": u, "arm": v.arm, "goal_id": g.goal_id, "decision_id": g.decision_id, "goal_start": iso(g.start),
                             "deadline": iso(g.deadline), "outcome": j["status"], "reason": j["reason"], "evidence": j["evidence"],
                             "success_rule_version": g.rule.version, "exposed_before_success": j.get("exposed_before_success"),
                             "attribution_note": j.get("attribution_note"), "late_unattributable_events": j["late_unattributable_events"],
                             "page_views_after_start": j["page_views_after_start"],
                             "messages_delivered_after_start": j["messages_delivered_after_start"]})
            status_counts[j["status"]] += 1
            by_goal[g.goal_id][j["status"]] += 1
            les = lesson_for(g, j, v, obs)
            v.outcomes.append({"goal_id": g.goal_id, "judged": j["status"], "reason": j["reason"], "evidence": j["evidence"],
                               "window": j["window"], "rule_version": g.rule.version})
            v.learnings.append(les)
            v.log(g.deadline, "goal_judged", goal=g.goal_id, outcome=j["status"], lesson=les["next_step_hint"])
    ref_rows = ref_outcomes(rt, obs)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "outcomes.jsonl"), "w", encoding="utf-8") as f:
        for r in outcomes:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "reference_goal_outcomes.jsonl"), "w", encoding="utf-8") as f:
        for r in ref_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # cross-viewer learning: holdout-arm success rates per goal (untouched viewers) become the next run's baseline priors
    priors = {}
    cnt = defaultdict(lambda: [0, 0])
    for r in ref_rows:
        if r["arm"] == "holdout" and r["outcome"] in ("succeeded", "failed"):
            cnt[r["goal_id"]][0] += 1
            cnt[r["goal_id"]][1] += r["outcome"] == "succeeded"
    for gid, (n, s) in cnt.items():
        priors[gid] = {"rate": round((s + 1) / (n + 2), 4), "n": n, "source": "holdout arm of this replay, judged with the corrected rules"}
    with open(os.path.join(out_dir, "learned_priors.json"), "w", encoding="utf-8") as f:
        json.dump(priors, f, indent=2)
    return {"agent_goals_judged": len(outcomes), "status_counts": dict(status_counts), "by_goal": {k: dict(v) for k, v in by_goal.items()},
            "reference_goals": len(ref_rows), "observation_horizon": iso(obs.horizon), "priors": priors}, obs, ref_rows
