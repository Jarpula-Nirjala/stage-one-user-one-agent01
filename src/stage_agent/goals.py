"""Goal catalogue: versioned success rules, evidence-based eligibility, ranking and judging predicates.

Interpretations of under-specified rules are explicit (see `INTERPRETATIONS`) and surface as `assumptions` on every
decision that relies on them.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .config import Config, PILOT_HOLDOUT_BASE_RATES
from .facts import ViewerFacts
from .loader import Catalogue, Ev
from .timeutil import ist_date, iso

INTERPRETATIONS = {
    "G06": "'recently lapsed' = a *dated* lapse (subscription_lapsed event, or plan_ends_at) within recent_lapse_days (30). "
           "A snapshot row that says 'lapsed' with no date has unknown recency and is NOT treated as eligible.",
    "G07": "'left unfinished' = the viewer started >=1 episode of a show/micro-drama in the dialect they will see, episodes "
           "remain unwatched, and the last play is >=3 days ago. Series in another dialect cannot be promoted, so they do not qualify.",
    "G12": "'No app_open in the 14 days before the trigger' is evaluated *before* the triggering open (the trigger is the "
           "return, not the absence). Viewers signed up <14 days ago cannot be 'absent'.",
    "G02": "'never completed an episode' is judged on the 30-day event history; for older viewers this is a windowed claim "
           "(flagged as an assumption and penalised in confidence).",
    "G09": "'plays on at most 1 day in the last 14' counts distinct IST days of play_started in the visible history.",
    "G03": "'signed up in the last 2 days' uses the profile signup (server clock) or, for brand-new viewers, the signup event's "
           "received_at-based estimate. Success rule was corrected mid-replay (see goal_rule_corrected).",
    "G10": "UNMEASURABLE: success.event is null, so no event can ever prove success. Never selected.",
    "G11": "UNMEASURABLE here: success event 'tv_app_open' never occurs in the pack (platforms are android/ios only). Never selected.",
}


@dataclass
class SuccessRule:
    event: Optional[str]
    kind: str            # any | paid_purchase | later_day | same_slug | released_recent | distinct_days | unmeasurable
    n: int = 1
    version: int = 1
    text: str = ""
    effective_from: Optional[str] = None

    def to_dict(self) -> dict:
        return {"event": self.event, "kind": self.kind, "n": self.n, "version": self.version, "text": self.text,
                "effective_from": self.effective_from}


def parse_condition(event: Optional[str], condition: Optional[str], version: int = 1, effective_from: Optional[str] = None) -> SuccessRule:
    c = (condition or "").lower()
    if not event:
        return SuccessRule(None, "unmeasurable", 1, version, condition or "", effective_from)
    if re.search(r"is_trial\s*=\s*false|any paid plan", c):
        k, n = "paid_purchase", 1
    elif "later ist calendar day" in c:
        k, n = "later_day", 1
    elif "same content_slug" in c:
        k, n = "same_slug", 1
    elif "released in the" in c:
        k, n = "released_recent", 30
    elif "distinct ist days" in c:
        m = re.search(r"(\d+)\s+distinct", c)
        k, n = "distinct_days", int(m.group(1)) if m else 3
    elif c in ("any", "any title", "any episode", ""):
        k, n = "any", 1
    else:
        return SuccessRule(event, "unmeasurable", 1, version, condition or "", effective_from)
    return SuccessRule(event, k, n, version, condition or "", effective_from)


@dataclass
class GoalSpec:
    goal_id: str
    name: str
    window_hours: int
    rule: SuccessRule
    description: str = ""
    eligibility_text: str = ""


class GoalRegistry:
    def __init__(self, goals_json: dict, event_vocab):
        self.version = goals_json.get("version", "?")
        self.specs: Dict[str, GoalSpec] = {}
        self.measurability: Dict[str, Tuple[bool, str]] = {}
        for g in goals_json["goals"]:
            rule = parse_condition(g["success"].get("event"), g["success"].get("condition"))
            spec = GoalSpec(g["goal_id"], g["name"], int(g["success"]["window_hours"]), rule, g.get("description", ""),
                            g.get("eligibility", {}).get("text", ""))
            self.specs[spec.goal_id] = spec
            if rule.kind == "unmeasurable":
                why = "success event is null" if rule.event is None else "success condition cannot be turned into a check"
                self.measurability[spec.goal_id] = (False, why)
            elif event_vocab and event_vocab.get(rule.event, 0) == 0:
                self.measurability[spec.goal_id] = (False, "success event '%s' never occurs in the event stream" % rule.event)
            else:
                self.measurability[spec.goal_id] = (True, "")
        self.rule_log: List[dict] = []

    def measurable(self, gid: str) -> Tuple[bool, str]:
        return self.measurability.get(gid, (False, "unknown goal"))

    def rule_ref(self, gid: str) -> str:
        return "goal_rule:%s-v%d" % (gid, self.specs[gid].rule.version)

    def apply_correction(self, ctrl: dict) -> Tuple[str, SuccessRule, bool]:
        gid = ctrl["goal_id"]
        spec = self.specs[gid]
        new = ctrl["new_success"]
        rule = parse_condition(new.get("event"), new.get("condition"), spec.rule.version + 1, ctrl["at"])
        old = spec.rule
        spec.rule = rule
        retro = "still open" in (ctrl.get("applies_to") or "").lower() or "already open" in (ctrl.get("applies_to") or "").lower()
        self.measurability[gid] = (rule.kind != "unmeasurable", "" if rule.kind != "unmeasurable" else "corrected rule unparseable")
        self.rule_log.append({"goal_id": gid, "at": ctrl["at"], "old": old.to_dict(), "new": rule.to_dict(),
                              "retroactive_to_open_goals": retro, "note": ctrl.get("note")})
        return gid, rule, retro


@dataclass
class GoalInstance:
    goal_id: str
    name: str
    start: datetime
    deadline: datetime
    window_hours: int
    rule: SuccessRule
    params: dict = field(default_factory=dict)
    status: str = "active"            # active | succeeded | failed | inconclusive | superseded
    decision_id: Optional[str] = None
    assumptions: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    revisions: List[dict] = field(default_factory=list)
    closed_at: Optional[datetime] = None
    judgment: Optional[dict] = None

    def to_dict(self) -> dict:
        return {"goal_id": self.goal_id, "name": self.name, "start": iso(self.start), "deadline": iso(self.deadline),
                "window_hours": self.window_hours, "success_rule": self.rule.to_dict(), "params": self.params,
                "status": self.status, "decision_id": self.decision_id, "assumptions": self.assumptions,
                "evidence": self.evidence, "revisions": self.revisions, "closed_at": iso(self.closed_at),
                "judgment": self.judgment}


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    reasons: List[str]
    evidence: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    penalties: List[Tuple[str, float]] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"goal_id": self.goal_id, "eligible": self.eligible, "reasons": self.reasons, "evidence": self.evidence,
                "assumptions": self.assumptions, "params": self.params}


def _no(gid, *why):
    return Eligibility(gid, False, list(why))


def _sref(vf: ViewerFacts):
    return vf.status_ref[0] if vf.status_ref else "profile:%s" % vf.viewer_id


def eligibility(gid: str, vf: ViewerFacts, cfg: Config, cat: Catalogue, reg: GoalRegistry) -> Eligibility:
    ok, why = reg.measurable(gid)
    if not ok:
        return _no(gid, "unmeasurable: " + why)
    f = vf.features
    now = vf.now
    rule = reg.rule_ref(gid)
    sig_ref = "profile:%s" % vf.viewer_id if vf.signup_source == "profile" else None
    sign_age = (now - vf.signup_at) if vf.signup_at else None

    def sub_gate(allowed):
        if vf.status == "unknown":
            return "subscription status unknown"
        if vf.status_ambiguous:
            return "subscription status ambiguous (duplicate profile rows disagree)"
        if vf.status not in allowed:
            return "subscription is %s" % vf.status
        return None

    if gid == "G01":
        if f is None:
            return _no(gid, "event history unavailable")
        if sign_age is None:
            return _no(gid, "signup time unknown")
        if sign_age > timedelta(days=cfg.new_user_days) or sign_age < timedelta(0):
            return _no(gid, "signup %.1f days ago (>%d)" % (sign_age.total_seconds() / 86400, cfg.new_user_days))
        if f.plays:
            return _no(gid, "already started a play (event:%s)" % f.plays[0].id)
        return Eligibility(gid, True, ["signed up %.1f days ago" % (sign_age.total_seconds() / 86400), "no play_started in history"],
                           [rule] + ([sig_ref] if sig_ref else []) + (["event:%s" % f.signup_event.id] if f.signup_event else []))
    if gid == "G02":
        if f is None:
            return _no(gid, "event history unavailable")
        if f.completes:
            return _no(gid, "already completed an episode (event:%s)" % f.completes[0].id)
        el = Eligibility(gid, True, ["no episode_completed in visible history"], [rule])
        if sign_age is None or sign_age > timedelta(days=30):
            if not cfg.allow_windowed_never_claims:
                return _no(gid, "'never completed' not provable beyond the 30-day history window")
            el.assumptions.append("'never completed an episode' is based on the 30-day history window only")
            el.penalties.append(("'never completed' is a windowed claim (30-day history)", 0.8))
        return el
    if gid == "G03":
        if sign_age is None:
            return _no(gid, "signup time unknown")
        if not (timedelta(0) <= sign_age <= timedelta(days=cfg.second_day_days)):
            return _no(gid, "signup %.1f days ago (>%d)" % (sign_age.total_seconds() / 86400, cfg.second_day_days))
        return Eligibility(gid, True, ["signed up %.1f days ago" % (sign_age.total_seconds() / 86400)],
                           [rule] + ([sig_ref] if sig_ref else []) + (["event:%s" % f.signup_event.id] if f and f.signup_event else []))
    if gid == "G04":
        g = sub_gate(("trial_active",))
        if g:
            return _no(gid, g)
        el = Eligibility(gid, True, ["subscription_status=trial_active"], [rule, _sref(vf)])
        el.assumptions.append("trial status comes from the day-old snapshot; no event confirms it (trial purchases are not in the stream)")
        el.penalties.append(("trial_active rests on the snapshot only", 0.85))
        return el
    if gid == "G05":
        g = sub_gate(("never",))
        if g:
            return _no(gid, g)
        if vf.has_paid_ever:
            return _no(gid, "has paid before")
        return Eligibility(gid, True, ["subscription_status=never; no paid event seen"], [rule, _sref(vf)])
    if gid == "G06":
        g = sub_gate(("lapsed",))
        if g:
            return _no(gid, g)
        if not vf.lapse_dated or vf.lapse_at is None:
            return _no(gid, "lapsed but lapse date unknown, 'recently' cannot be established")
        days = (now - vf.lapse_at).total_seconds() / 86400
        if days > cfg.recent_lapse_days:
            return _no(gid, "lapsed %.0f days ago (>%d)" % (days, cfg.recent_lapse_days))
        ev = ["event:%s" % e.id for e in (f.lapses if f else [])][-1:]
        return Eligibility(gid, True, ["lapsed %.1f days ago" % days], [rule] + ev,
                           ["'recently lapsed' interpreted as a dated lapse within %d days" % cfg.recent_lapse_days])
    if gid == "G07":
        if f is None:
            return _no(gid, "event history unavailable")
        best = None
        for slug, d in f.series_progress(cat).items():
            if d["dialect"] != vf.dialect:
                continue
            total = cat.episode_count(slug)
            nxt = cat.next_episode(slug, d["watched"])
            idle = (now - d["last_play"]).total_seconds() / 86400
            if nxt is None or total == 0 or idle < cfg.series_idle_days:
                continue
            if best is None or d["last_play"] > best[1]["last_play"]:
                best = (slug, d, nxt, idle)
        if not best:
            return _no(gid, "no unfinished series in the dialect the viewer will see idle >= %d days" % cfg.series_idle_days)
        slug, d, nxt, idle = best
        v = cat.variant_for(slug, vf.language, now)
        if v is None:
            return _no(gid, "unfinished series %s is not available now" % slug)
        return Eligibility(gid, True, ["series %s idle %.1f days, next episode s%de%d" % (slug, idle, nxt["season"], nxt["episode"])],
                           [rule, "event:%s" % d["last_ev"], "catalogue:%s" % v.id],
                           ["viewer has not played the next episode %s elsewhere (not visible in the pack)" % nxt["episode_id"]],
                           params={"slug": slug, "title_id": v.id, "next_episode_id": nxt["episode_id"],
                                   "next_episode": nxt["episode"], "season": nxt["season"]})
    if gid == "G08":
        g = sub_gate(("active",))
        if g:
            return _no(gid, g)
        return Eligibility(gid, True, ["active subscriber"], [rule, _sref(vf)])
    if gid == "G09":
        g = sub_gate(("active",))
        if g:
            return _no(gid, g)
        if f is None:
            return _no(gid, "event history unavailable")
        days = f.play_days(cfg.habit_lookback_days)
        if len(days) > 1:
            return _no(gid, "plays on %d distinct days in the last %d" % (len(days), cfg.habit_lookback_days))
        return Eligibility(gid, True, ["active subscriber with plays on %d day(s) in %d days" % (len(days), cfg.habit_lookback_days)],
                           [rule, _sref(vf)])
    if gid == "G12":
        if f is None:
            return _no(gid, "event history unavailable")
        lo = now - timedelta(days=cfg.absence_days)
        recent = [e for e in f.opens if e.est >= lo]
        if recent:
            return _no(gid, "opened the app %d time(s) in the last %d days" % (len(recent), cfg.absence_days))
        if not f.opens and (sign_age is None or sign_age < timedelta(days=cfg.absence_days)):
            return _no(gid, "no opens, but too new to count as absent")
        el = Eligibility(gid, True, ["no app_open in the %d days before this one" % cfg.absence_days], [rule])
        if not f.opens:
            el.assumptions.append("no app_open anywhere in the 30-day history; absence is at least 30 days")
        else:
            el.evidence.append("event:%s" % f.last_open().id)
        return el
    return _no(gid, "no eligibility function")


@dataclass
class GoalChoice:
    chosen: Optional[Eligibility]
    ranked: List[Tuple[float, Eligibility]]
    rejected: Dict[str, List[str]]

    def to_dict(self) -> dict:
        return {"chosen": self.chosen.goal_id if self.chosen else None,
                "ranked": [{"goal_id": e.goal_id, "score": s} for s, e in self.ranked],
                "rejected": self.rejected}


def choose_goal(vf: ViewerFacts, cfg: Config, cat: Catalogue, reg: GoalRegistry, exclude: Tuple[str, ...] = ()) -> GoalChoice:
    ranked, rejected = [], {}
    for gid in reg.specs:
        if gid in exclude:
            rejected[gid] = ["excluded by planner"]
            continue
        el = eligibility(gid, vf, cfg, cat, reg)
        if el.eligible:
            ranked.append((cfg.goal_weights.get(gid, 0.0), el))
        else:
            rejected[gid] = el.reasons
    ranked.sort(key=lambda x: (-x[0], x[1].goal_id))
    return GoalChoice(ranked[0][1] if ranked else None, ranked, rejected)


def open_goal(el: Eligibility, reg: GoalRegistry, start: datetime, decision_id: str) -> GoalInstance:
    spec = reg.specs[el.goal_id]
    return GoalInstance(el.goal_id, spec.name, start, start + timedelta(hours=spec.window_hours), spec.window_hours,
                        copy.copy(spec.rule), dict(el.params), "active", decision_id, list(el.assumptions), list(el.evidence))


# ---------------------------------------------------------------- judging
def title_of(ev: Ev, cat: Catalogue):
    return cat.titles.get(ev.props.get("title_id"))


def evaluate_goal(goal: GoalInstance, events: List[Ev], cat: Catalogue, until: Optional[datetime] = None):
    """Return (succeeded, evidence_event_ids, late_unattributable_ids). Only events whose *estimated true time* falls in
    (start, deadline] count; events delivered inside the window but that happened before it are late arrivals and are
    reported separately (they must never be attributed to this goal)."""
    r = goal.rule
    lo, hi = goal.start, goal.deadline if until is None else min(goal.deadline, until)
    hits, late, days = [], [], {}
    for e in events:
        if e.kind != r.event:
            continue
        if r.kind == "paid_purchase" and e.kind == "purchase_completed" and e.props.get("is_trial"):
            continue
        t = title_of(e, cat) if r.kind in ("same_slug", "released_recent") else None
        if r.kind == "same_slug" and (t is None or t.slug != goal.params.get("slug")):
            continue
        if r.kind == "released_recent":
            if t is None or not (goal.start - timedelta(days=r.n) <= t.release <= goal.start):
                continue
        if r.kind == "later_day" and ist_date(e.est) <= ist_date(goal.start):
            continue
        if e.est > lo and e.est <= hi:
            hits.append(e)
            days.setdefault(ist_date(e.est), e)
        elif e.est <= lo and lo < e.recv <= hi:
            late.append(e.id)
    if r.kind == "distinct_days":
        ok = len(days) >= r.n
        return ok, [e.id for e in sorted(days.values(), key=lambda x: x.est)][:r.n] if ok else [], late
    return bool(hits), [hits[0].id] if hits else [], late


def prior_rate(gid: str, priors: Optional[dict] = None) -> Optional[float]:
    if priors and gid in priors:
        return priors[gid].get("rate")
    return PILOT_HOLDOUT_BASE_RATES.get(gid)
