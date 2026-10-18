"""The per-viewer agent runtime.

`Runtime` owns every `ViewerRecord` (goal, plan, history, decisions, actions, outcomes, learnings) - one record, one active
goal per viewer. The decision pipeline is: observe -> resolve facts -> goal -> intervene -> verify -> publish, with a
degradation ladder at every step. Holdout viewers only get a computation-only counterfactual goal (no action, no model).
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from .config import RULE_VERSION, Config
from .facts import ViewerFacts, resolve_facts
from .goals import (GoalChoice, GoalInstance, GoalRegistry, choose_goal, eligibility, evaluate_goal, open_goal,
                    prior_rate, INTERPRETATIONS)
from .intervention import PagePlan, build_page, compute_popularity, plan_message
from .loader import Catalogue, Ev, PackData
from .model import ModelGateway, make_provider, validate_advice
from .resilience import BudgetExceeded, DataStoreClient, DecisionBudget, DependencyWindows, StoreUnavailable
from .timeutil import iso, ist_date
from .verifier import ConsentLedger, MessageLedger, Verdict, Verifier

WALLCLOCK = time.perf_counter


@dataclass
class ViewerRecord:
    viewer_id: str
    arm: str
    goals: List[GoalInstance] = field(default_factory=list)
    analysis_goal: Optional[dict] = None
    decision_ids: List[str] = field(default_factory=list)
    action_ids: List[str] = field(default_factory=list)
    outcomes: List[dict] = field(default_factory=list)
    learnings: List[dict] = field(default_factory=list)
    journal: List[dict] = field(default_factory=list)
    last_facts: Optional[dict] = None
    last_page: Optional[dict] = None
    served_sessions: Dict[str, str] = field(default_factory=dict)   # session_id -> decision_id
    planned_message_goal: Optional[str] = None
    conflicts_seen: Set[str] = field(default_factory=set)

    @property
    def active_goal(self) -> Optional[GoalInstance]:
        for g in reversed(self.goals):
            if g.status == "active":
                return g
        return None

    def log(self, at: datetime, kind: str, **data):
        self.journal.append({"at": iso(at), "kind": kind, **data})

    def to_dict(self) -> dict:
        return {"viewer_id": self.viewer_id, "arm": self.arm, "analysis_goal": self.analysis_goal,
                "active_goal": self.active_goal.goal_id if self.active_goal else None,
                "goals": [g.to_dict() for g in self.goals], "decision_ids": self.decision_ids, "action_ids": self.action_ids,
                "outcomes": self.outcomes, "learnings": self.learnings, "last_facts": self.last_facts,
                "last_page": {k: x for k, x in (self.last_page or {}).items() if k != "published_dt"} or None, "conflicts_seen": sorted(self.conflicts_seen), "journal": self.journal}


class Runtime:
    def __init__(self, pack: PackData, cfg: Config, priors: Optional[dict] = None):
        self.pack, self.cfg, self.priors = pack, cfg, priors or {}
        self.cat: Catalogue = pack.catalogue
        self.reg = GoalRegistry(pack.goals, pack.event_vocab)
        self.windows = DependencyWindows(pack.controls)
        self.store = DataStoreClient(cfg, self.windows)
        self.provider = make_provider(cfg, pack.faults, self.windows)
        self.gateway = ModelGateway(self.provider, cfg)
        self.consent = ConsentLedger(pack.profiles)
        self.msgs = MessageLedger()
        self.verifier = Verifier(self.cat, pack.arms, self.consent, self.msgs, pack.interventions, pack.pricing, cfg)
        # mutable per-user event lists (history + everything observed during the replay)
        self.hist: Dict[str, List[Ev]] = {u: list(v) for u, v in pack.events.items()}
        self.event_ids: Set[str] = {e.id for v in self.hist.values() for e in v}
        self.viewers: Dict[str, ViewerRecord] = {}
        self.decisions: List[dict] = []
        self.ledger: Dict[str, dict] = {}            # action_id -> action (with lifecycle fields)
        self.idem: Dict[str, str] = {}
        self.slug_index: Dict[str, Set[str]] = defaultdict(set)    # slug -> action ids that promote it
        self.open_by_goal: Dict[str, Set[str]] = defaultdict(set)  # goal_id -> viewers with an active instance
        self.security_log: List[dict] = list(pack.injection_log)
        self.unsupported_claims: List[dict] = []
        self.revisions: List[dict] = []              # withdrawals / rule corrections / replans, with affected sets
        self.resilience_log: List[dict] = []
        self.counters: Dict[str, int] = defaultdict(int)
        self._d = 0
        self._a = 0
        as_of = pack.snapshot_at or (min((e.deliver for e in pack.triggers), default=None))
        if as_of:
            compute_popularity(self.cat, self.hist, as_of)
        for u, evs in self.hist.items():
            for e in evs:
                if e.kind == "notification_opt_out":
                    self.consent.record_optout(u, e.props.get("channel"), e.id)
                elif e.kind == "message_delivered":
                    self.msgs.seed_history(u, e.est, e.props.get("channel"))

    def breaker_trips_changed(self, last: int) -> bool:
        return self.gateway.breaker.trips != last

    # ------------------------------------------------------------------ ids / state
    def viewer(self, user: str) -> ViewerRecord:
        v = self.viewers.get(user)
        if v is None:
            v = self.viewers[user] = ViewerRecord(user, self.pack.arms.get(user, "unknown"))
        return v

    def _did(self) -> str:
        self._d += 1
        return "D-%06d" % self._d

    def _aid(self) -> str:
        self._a += 1
        return "A-%06d" % self._a

    def has_event(self, user: str):
        return lambda eid: eid in self.event_ids

    def observe(self, ev: Ev):
        if ev.id in self.event_ids:
            return False
        self.event_ids.add(ev.id)
        lst = self.hist.setdefault(ev.user, [])
        if lst and ev.recv < lst[-1].recv:
            lst.append(ev)
            lst.sort(key=lambda e: (e.recv, e.id))
        else:
            lst.append(ev)
        if ev.kind == "notification_opt_out":
            self.consent.record_optout(ev.user, ev.props.get("channel"), ev.id)
        return True

    # ------------------------------------------------------------------ facts
    def facts_for(self, user: str, now: datetime, trigger: Optional[Ev], mode: str = "full") -> ViewerFacts:
        return resolve_facts(user, now, trigger, self.pack.profiles.get(user, []), self.hist.get(user, []),
                             self.pack.snapshot_at, self.cat, mode)

    # ------------------------------------------------------------------ goal lifecycle
    def refresh_goal(self, v: ViewerRecord, vf: ViewerFacts, now: datetime) -> Optional[GoalInstance]:
        """Close or keep the viewer's active goal. Returns the goal if it is still active."""
        g = v.active_goal
        if g is None:
            return None
        ok, evid, late = evaluate_goal(g, self.hist.get(v.viewer_id, []), self.cat, until=now)
        if ok:
            self._close_goal(v, g, "succeeded", now, {"evidence": evid, "note": "success event observed during the replay"})
            return None
        if now > g.deadline:
            self._close_goal(v, g, "expired", now, {"note": "deadline passed; final judgment happens in the outcome judge"})
            return None
        # Only status-based goals are re-checked: for the others eligibility is an *entry* condition (e.g. G12's absence, G09's
        # low play count) that the goal itself is meant to change, so losing it is not a reason to drop the goal.
        el = eligibility(g.goal_id, vf, self.cfg, self.cat, self.reg) if g.goal_id in ("G04", "G05", "G06", "G08") else None
        if el is not None and not el.eligible:
            self._close_goal(v, g, "superseded", now, {"note": "no longer eligible: " + "; ".join(el.reasons)})
            return None
        return g

    def _close_goal(self, v: ViewerRecord, g: GoalInstance, status: str, now: datetime, info: dict):
        g.status = "succeeded" if status == "succeeded" else ("superseded" if status in ("superseded", "expired") else status)
        g.closed_at = now
        g.revisions.append({"at": iso(now), "event": status, **info})
        self.open_by_goal[g.goal_id].discard(v.viewer_id)
        v.log(now, "goal_" + status, goal=g.goal_id, **{k: x for k, x in info.items() if k != "note"}, note=info.get("note"))
        # pending messages that belonged to this goal no longer make sense
        for aid in list(v.action_ids):
            a = self.ledger.get(aid)
            if a and a.get("goal_id") == g.goal_id and a["type"] in ("push", "whatsapp", "in_app_message") \
                    and a["status"] == "SCHEDULED" and a["send_dt"] > now:
                self.cancel(aid, now, "goal_%s_%s" % (g.goal_id, status))

    # ------------------------------------------------------------------ actions
    def _register(self, v: ViewerRecord, act: dict, now: datetime):
        act["status"] = "SCHEDULED" if act["type"] in ("push", "whatsapp", "in_app_message") else "PUBLISHED"
        if act.get("send_at"):
            act["send_dt"] = datetime.fromisoformat(act["send_at"].replace("Z", "+00:00"))
        elif act["type"] == "in_app_message":
            act["send_dt"] = datetime.fromisoformat(act["published_at"].replace("Z", "+00:00"))
        act["_published_dt"] = datetime.fromisoformat(act["published_at"].replace("Z", "+00:00"))
        self.ledger[act["action_id"]] = act
        v.action_ids.append(act["action_id"])
        if act["type"] == "home_page":
            ids = ([act["hero_title_id"]] if act.get("hero_title_id") else []) + [t for r in act["rows"] for t in r["title_ids"]]
            for t in ids:
                self.slug_index[self.cat.titles[t].slug].add(act["action_id"])
        elif act.get("title_id"):
            self.slug_index[self.cat.titles[act["title_id"]].slug].add(act["action_id"])
        if act["type"] in ("push", "whatsapp", "in_app_message"):
            self.msgs.add(v.viewer_id, act["action_id"], act["send_dt"], act["type"])

    def cancel(self, action_id: str, now: datetime, reason: str):
        a = self.ledger.get(action_id)
        if not a or a["status"] in ("CANCELLED",):
            return False
        a["status"] = "CANCELLED"
        a["cancelled_at"] = iso(now)
        a["cancel_reason"] = reason
        self.msgs.remove(a["user_id"], action_id)
        self.counters["actions_cancelled"] += 1
        self.viewer(a["user_id"]).log(now, "action_cancelled", action_id=action_id, reason=reason)
        return True

    def effective_actions(self) -> List[dict]:
        out = []
        for a in self.ledger.values():
            if a["status"] == "CANCELLED":
                continue
            out.append({k: x for k, x in a.items() if not k.startswith("_") and k not in ("send_dt",)})
        out.sort(key=lambda a: (a["published_at"], a["action_id"]))
        return out

    # ------------------------------------------------------------------ decision core
    def on_trigger(self, trig: Ev, queue_wait_s: float = 0.0, shed: int = 0) -> dict:
        t0 = WALLCLOCK()
        user, now = trig.user, trig.deliver
        v = self.viewer(user)
        did = self._did()
        budget = DecisionBudget(self.cfg, trig.id)
        age = trig.buffered_s
        dec: dict = {
            "decision_id": did, "viewer_id": user, "arm": v.arm, "rule_version": RULE_VERSION,
            "trigger": {"event": trig.kind, "event_id": trig.id, "session_id": trig.session, "received_at": iso(trig.recv),
                        "deliver_at": iso(now), "occurred_at_phone": iso(trig.occurred), "event_age_s": round(age, 1),
                        "clock_skew_s": round(trig.skew_s) if abs(trig.skew_s) > 120 and trig.buffered_s <= 600 else 0},
            "goal": None, "action": "NO_ACTION", "dialect": None, "content": [], "confidence": None, "confidence_reason": "",
            "evidence": [], "assumptions": [], "conflicts": [], "checks": {"passed": [], "failed": []},
            "model_calls": 0, "tool_calls": 0, "fallback_used": [], "no_action_reason": None, "status": "NO_ACTION",
            "actions": [], "shed_level": shed, "queue_wait_ms": round(queue_wait_s * 1000, 1),
        }
        self.counters["triggers"] += 1

        def finish(status: Optional[str] = None):
            real_ms = (WALLCLOCK() - t0) * 1000
            dec["decided_in_ms"] = round(real_ms + budget.virtual_ms, 1)
            dec["real_compute_ms"] = round(real_ms, 2)
            dec["model_calls"], dec["tool_calls"] = budget.model_calls, budget.tool_calls
            dec["resilience_events"] = budget.events
            pub = dec.pop("_pub", None)
            if pub is not None:   # actions were stamped at this instant; keep decision and actions consistent
                dec["decided_in_ms"] = round((pub - now).total_seconds() * 1000 - queue_wait_s * 1000, 1)
            else:
                pub = now + timedelta(seconds=queue_wait_s + dec["decided_in_ms"] / 1000.0)
            dec["published_at"] = iso(pub)
            dec["latency_s"] = round((pub - now).total_seconds(), 3)
            if status:
                dec["status"] = status
            v.decision_ids.append(did)
            self.decisions.append(dec)
            return dec

        # ---- idempotency: the trigger event_id is the key
        if trig.id in self.idem:
            dec.update(action="NO_ACTION", status="DUPLICATE_SUPPRESSED", no_action_reason="duplicate_trigger_event_id",
                       duplicate_of=self.idem[trig.id])
            dec["evidence"] = ["event:%s" % trig.id]
            self.counters["duplicate_triggers"] += 1
            return finish()
        self.idem[trig.id] = did
        self.observe(trig)

        # ---- stale / buffered trigger: a state update, not a visit
        if age > self.cfg.stale_trigger_s:
            dec.update(no_action_reason="stale_trigger: app_open was buffered offline for %.0f h (>%.0f s); not a live session"
                       % (age / 3600, self.cfg.stale_trigger_s), status="NO_ACTION_STALE")
            dec["evidence"] = ["event:%s" % trig.id]
            dec["confidence"], dec["confidence_reason"] = 0.95, "age derived from sent_at-occurred_at (phone-internal, robust to a wrong clock)"
            v.log(now, "stale_trigger_state_update", event=trig.id, age_s=round(age))
            if trig.id in self.pack.faults:
                dec["fault_not_exercised"] = "injected %s fault skipped: stale trigger, the model is not consulted" % self.pack.faults[trig.id]["fault"]
            self.counters["stale_triggers"] += 1
            return finish()

        # ---- overload: projected queue wait would blow the latency budget; drop (recorded) instead of publishing late
        if shed >= 3:
            dec.update(no_action_reason="overload_shed: projected queue wait %.0f s exceeds the budget; decision dropped, not published late"
                       % queue_wait_s, status="NO_ACTION_OVERLOAD")
            dec["evidence"] = ["event:%s" % trig.id]
            self.counters["overload_dropped"] += 1
            return finish()

        # ---- reference goal (identical for both arms) used for intention-to-treat analysis
        vf_full = self.facts_for(user, now, trig, "full")
        if v.analysis_goal is None:
            ch = choose_goal(vf_full, self.cfg, self.cat, self.reg)
            if ch.chosen:
                spec = self.reg.specs[ch.chosen.goal_id]
                v.analysis_goal = {"goal_id": ch.chosen.goal_id, "start": iso(now), "deadline": iso(now + timedelta(hours=spec.window_hours)),
                                   "rule_version": spec.rule.version, "params": ch.chosen.params, "trigger_event_id": trig.id,
                                   "dialect": vf_full.dialect, "status": vf_full.status}
            else:
                v.analysis_goal = {"goal_id": None, "start": iso(now), "reason": "no eligible measurable goal",
                                   "rejected": ch.rejected}
        v.last_facts = {"as_of": iso(now), "dialect": vf_full.dialect, "dialect_source": vf_full.dialect_source,
                        "subscription": vf_full.status, "subscription_source": vf_full.status_source,
                        "conflicts": vf_full.conflicts, "facts": [f.to_dict() for f in vf_full.facts]}
        for c in vf_full.conflicts:
            v.conflicts_seen.add(c.split(":")[0] + ":" + c.split(":")[1] if ":" in c else c)

        # ---- holdout: zero actions of any kind. Only a computation-only counterfactual is stored.
        if v.arm != "agent":
            reason = "holdout_arm" if v.arm == "holdout" else "arm_unknown_treated_as_holdout"
            dec.update(no_action_reason=reason, status="HOLDOUT_NO_ACTION")
            dec["counterfactual_goal"] = (v.analysis_goal or {}).get("goal_id")
            if trig.id in self.pack.faults:
                dec["fault_not_exercised"] = "injected %s fault skipped: holdout viewers get no model call and no action" % self.pack.faults[trig.id]["fault"]
            dec["evidence"] = ["arms:%s=%s" % (user, v.arm)]
            dec["checks"] = {"passed": ["holdout"], "failed": []}
            self.counters["holdout_decisions"] += 1
            return finish()

        # ---- agent arm
        if trig.session in v.served_sessions and trig.session:
            dec.update(no_action_reason="session_already_served (decision %s): one decision per visit" % v.served_sessions[trig.session],
                       status="NO_ACTION_SESSION_DUP")
            dec["evidence"] = ["event:%s" % trig.id]
            self.counters["session_dup"] += 1
            return finish()
        v.served_sessions[trig.session] = did

        # data store read (guarded) -> fall back to the day-old snapshot cache
        mode = "full"
        try:
            self.store.read(budget, now, user)
            budget.tool("store.read_profile")
            budget.tool("store.read_events")
        except StoreUnavailable:
            mode = "snapshot_cache"
            dec["fallback_used"].append("snapshot_cache(data_store_degraded)")
            self.counters["store_fallbacks"] += 1
        except BudgetExceeded:
            mode = "snapshot_cache"
        vf = vf_full if mode == "full" else self.facts_for(user, now, trig, "snapshot_cache")
        if mode != "full":
            vf.penalties.append(("trigger carried the only fresh facts (dialect); history unavailable", 0.9))

        dec["dialect"] = {"used": vf.dialect, "source": vf.dialect_source, "conflicts": vf.dialect_conflicts,
                          "note": vf.dialect_note or None}
        dec["conflicts"] = list(vf.conflicts)
        dec["viewer_facts"] = [f.to_dict() for f in vf.facts]
        dec["assumptions"] = list(vf.assumptions)

        if not vf.dialect:
            dec.update(no_action_reason="dialect_unresolvable: no valid app setting, profile or campaign dialect", status="NO_ACTION_ABSTAIN")
            dec["confidence"], dec["confidence_reason"] = 0.9, "no safe dialect => any content could be wrong"
            return finish()

        # ---- goal
        ev_refs: List[str] = ["event:%s" % trig.id]
        goal: Optional[GoalInstance] = None
        el = None
        choice: Optional[GoalChoice] = None
        degraded_plan = shed >= 2 or mode != "full"
        if mode == "full":
            goal = self.refresh_goal(v, vf, now)
            if goal is None:
                choice = choose_goal(vf, self.cfg, self.cat, self.reg)
                if choice.chosen is not None:
                    el = choice.chosen
            else:
                el = eligibility(goal.goal_id, vf, self.cfg, self.cat, self.reg)
            dec["goal_candidates"] = choice.to_dict() if choice else {"kept_active_goal": goal.goal_id if goal else None}
            # ---- model advice (a claim to be verified, never evidence)
            if el is not None and shed == 0 and self.cfg.model_call_policy != "never":
                ranked = choice.ranked if choice else [(self.cfg.goal_weights.get(el.goal_id, 0.0), el)]
                payload = {"eligible": [{"goal_id": e.goal_id, "score": s} for s, e in ranked], "summary": vf.summary()}
                res = self.gateway.call("advise_goal", payload, budget, now, trig.id, lambda x: validate_advice(x, set(self.reg.specs)))
                if res.ok:
                    if res.attempts > 1:
                        dec["fallback_used"].append("model_retry_after_malformed_output")
                    support = {e.goal_id: e.evidence for _, e in ranked}
                    accepted, why = self.verifier.verify_model_claim(res.data, {e.goal_id for _, e in ranked}, support, self.has_event(user))
                    claim = {"claim": res.data, "accepted": accepted, "verifier": why}
                    dec.setdefault("model_claims", []).append(claim)
                    if not accepted:
                        self.unsupported_claims.append({"decision_id": did, "viewer_id": user, "trigger_event_id": trig.id, **claim})
                        dec["fallback_used"].append("model_claim_rejected->rule_engine_goal")
                        vf.penalties.append(("model asserted an unsupported eligibility claim; rejected, rules decide", 0.9))
                        self.counters["unsupported_claims_rejected"] += 1
                    elif res.data["goal_id"] == el.goal_id:
                        dec["assumptions"].append("model concurs with rule-engine goal %s" % el.goal_id)
                    else:
                        dec["assumptions"].append("model preferred %s; rule engine keeps %s (priority ladder)" % (res.data["goal_id"], el.goal_id))
                else:
                    dec["fallback_used"].append("rules_only(model_%s)" % res.error)
                    self.counters["model_fallbacks"] += 1
        else:
            dec["goal_candidates"] = {"deferred": True}

        goal_id, params = (el.goal_id, el.params) if el else (None, {})
        if goal is None and el is not None:
            pass  # opened below, once published_at is known
        if el is not None:
            ev_refs.extend(el.evidence)
            dec["assumptions"].extend(a for a in el.assumptions if a not in dec["assumptions"])
        for iv_id, text in INTERPRETATIONS.items():
            if iv_id == goal_id:
                dec["assumptions"].append("interpretation of %s: %s" % (iv_id, text))

        # ---- page
        verdicts: List[Verdict] = []
        page: Optional[PagePlan] = None
        dec["page_decision"] = None
        lp = v.last_page
        page_live = (lp is not None and lp["dialect"] == vf.dialect and now < lp["published_dt"] + timedelta(minutes=self.cfg.page_ttl_min)
                     and lp.get("goal_id") == goal_id and not lp.get("invalidated"))
        if page_live and not degraded_plan:
            dec["page_decision"] = "live page %s still valid (published %s, same dialect and goal); not churning" % (lp["action_id"], lp["published_at"])
        else:
            budget_ok = True
            try:
                budget.tool("catalogue.pool_query", reserve=2)
                budget.tool("catalogue.rank", reserve=2)
                budget.tool("catalogue.episodes", reserve=2)
            except BudgetExceeded:
                budget_ok = False
                dec["fallback_used"].append("tool_budget_exhausted->minimal_page")
            page = build_page(vf, goal_id, params, self.cat, self.cfg, now, trig, minimal=degraded_plan or not budget_ok)
            if page is None:
                dec["page_decision"] = "no valid in-dialect content available"
            else:
                budget.tool("verifier.verify_page")
                vd = self.verifier.verify_page(user, page.to_action(), now, vf.dialect)
                if not vd.ok:   # degradation ladder: repair once with a minimal page, else give up on the page
                    dec["fallback_used"].append("page_failed_verification->minimal_page")
                    dec.setdefault("rejected_plans", []).append({"plan": "page", "failed": vd.failed()})
                    page = build_page(vf, goal_id, params, self.cat, self.cfg, now, trig, minimal=True)
                    vd = self.verifier.verify_page(user, page.to_action(), now, vf.dialect) if page else Verdict()
                    if page is None or not vd.ok:
                        page = None
                        dec["page_decision"] = "page abandoned: failed independent verification"
                verdicts.append(vd)

        # ---- message (one, proportionate, or none)
        msg = None
        dec["message_decision"] = None
        goal_start = now + timedelta(seconds=queue_wait_s)  # refined below
        if el is not None and not degraded_plan:
            if v.planned_message_goal == goal_id and goal is not None:
                dec["message_decision"] = "message already planned for this goal"
            else:
                spec = self.reg.specs[goal_id]
                deadline = (goal.deadline if goal is not None else now + timedelta(hours=spec.window_hours))
                try:
                    budget.tool("policy.message_plan", reserve=2)
                    msg, why = plan_message(vf, goal_id, params, deadline, page, self.cat, self.cfg, now, self.pack.pricing, self.priors)
                except BudgetExceeded:
                    msg, why = None, "tool-call budget exhausted: no message (home page only)"
                dec["message_decision"] = why if msg is None else "plan: %s via %s (%s)" % (msg.title_id, msg.channel, msg.rationale)
        elif el is not None:
            dec["message_decision"] = "skipped: degraded/shed mode (%s)" % ("load" if shed >= 2 else "data store")

        # ---- assemble, verify messages, publish
        real_ms = (WALLCLOCK() - t0) * 1000
        pub_dt = now + timedelta(seconds=queue_wait_s + (real_ms + budget.virtual_ms) / 1000.0 + 0.002)
        pub_iso = iso(pub_dt)
        actions: List[dict] = []
        if page is not None:
            a = {"action_id": self._aid(), "decision_id": did, "user_id": user, "trigger_event_id": trig.id, "published_at": pub_iso,
                 "goal_id": goal_id, "expires_at": iso(pub_dt + timedelta(minutes=self.cfg.page_ttl_min)), **page.to_action()}
            actions.append(a)
        msg_action = None
        if msg is not None:
            ma = {"action_id": self._aid(), "decision_id": did, "user_id": user, "trigger_event_id": trig.id, "published_at": pub_iso,
                  "goal_id": goal_id, **msg.payload}
            if msg.send_at is not None:
                ma["send_at"] = iso(msg.send_at)
            try:
                budget.tool("verifier.verify_message")
                mv = self.verifier.verify_message(user, ma, now, vf.dialect, vf.dialect_source, True, check_time=msg.send_at or pub_dt)
            except BudgetExceeded:
                mv = Verdict()   # an unverifiable message is never sent
                mv.add("verification_budget", False, "tool budget exhausted before the message could be verified")
            verdicts.append(mv)
            if mv.ok:
                msg_action = ma
            else:
                dec.setdefault("rejected_plans", []).append({"plan": msg.channel, "failed": mv.failed()})
                dec["message_decision"] = "message plan rejected by verifier: %s" % [f["check"] for f in mv.failed()]
                self.counters["messages_rejected_by_verifier"] += 1
        # `failed` lists every check that rejected a proposal (those proposals were dropped or repaired, see rejected_plans)
        dec["checks"] = {"passed": sorted({p for vd in verdicts for p in vd.passed()}),
                         "failed": [f for vd in verdicts for f in vd.failed()]}
        if actions or msg_action:
            dec["_pub"] = pub_dt

        # goal commitment (window starts when the agent commits = publication time)
        if el is not None and (actions or msg_action) or (el is not None and goal is None):
            if goal is None and el is not None:
                goal = open_goal(el, self.reg, pub_dt, did)
                goal.params = dict(params)
                v.goals.append(goal)
                self.open_by_goal[goal.goal_id].add(user)
                v.log(pub_dt, "goal_committed", goal=goal.goal_id, deadline=iso(goal.deadline), decision=did)
        if goal is not None:
            spec = self.reg.specs[goal.goal_id]
            dec["goal"] = {"id": goal.goal_id, "name": goal.name, "start": iso(goal.start), "deadline": iso(goal.deadline),
                           "success": {"event": goal.rule.event, "condition": goal.rule.text, "kind": goal.rule.kind,
                                       "window_hours": goal.window_hours, "rule_version": goal.rule.version},
                           "baseline_success_rate_no_action": prior_rate(goal.goal_id, self.priors) if self.cfg.use_priors else None,
                           "params": goal.params}
        else:
            dec["goal"] = None

        for a in actions:
            self._register(v, a, pub_dt)
            v.last_page = {"action_id": a["action_id"], "dialect": a["dialect"], "published_at": a["published_at"], "published_dt": pub_dt,
                           "goal_id": goal_id}
            dec["actions"].append(a["action_id"])
        if msg_action is not None:
            self._register(v, msg_action, pub_dt)
            v.planned_message_goal = goal_id
            dec["actions"].append(msg_action["action_id"])
        if page is not None:
            dec["content"] = ["title:%s" % t for t in page.title_ids()]
            ev_refs.extend(page.evidence)
        if msg_action is not None and msg_action.get("title_id"):
            dec["content"].append("title:%s(%s)" % (msg_action["title_id"], msg_action["type"]))
        dec["evidence"] = list(dict.fromkeys(ev_refs))
        dec["evidence_freshness_s"] = {"%s:%s" % (f["name"], f["ref"]): f["freshness_s"] for f in dec.get("viewer_facts", []) if f.get("freshness_s") is not None}

        # ---- action label / status / reasons
        types = [x["type"] for x in actions] + ([msg_action["type"]] if msg_action else [])
        if types:
            dec["action"] = "+".join(t.upper() for t in types)
            dec["status"] = "PUBLISHED"
        else:
            dec["action"] = "NO_ACTION"
            dec["status"] = "NO_ACTION_ABSTAIN" if goal is None else "NO_ACTION"
            parts = [dec.get("page_decision"), dec.get("message_decision")]
            dec["no_action_reason"] = "; ".join(p for p in parts if p) or "nothing to change"
        if goal is None and not types:
            dec["no_action_reason"] = (dec["no_action_reason"] or "") + "; no goal committed: " + (
                "goal deferred (degraded/shed mode)" if degraded_plan else "no eligible measurable goal for this evidence")
        # ---- confidence with reason
        conf, reasons = 0.88, ["base 0.88 (rule engine + verified evidence)"]
        pens = list(vf.penalties) + (list(el.penalties) if el else [])
        if age > self.cfg.aging_trigger_s:
            pens.append(("trigger is %.0f s old (aging, still live)" % age, 0.85))
        for why, m in pens:
            conf *= m
            reasons.append("%s x%.2f" % (why, m))
        if not goal_id:
            conf = min(conf, 0.6)
            reasons.append("no goal: abstention rather than a guess")
        dec["confidence"] = round(conf, 2)
        dec["confidence_reason"] = "; ".join(reasons)
        return finish()

    # ------------------------------------------------------------------ live events
    def on_live_event(self, ev: Ev) -> List[dict]:
        """Apply an event that arrives during the replay. Revises goals / cancels messages / replans only for the viewer
        concerned. Returns revision records (also appended to self.revisions)."""
        out: List[dict] = []
        new = self.observe(ev)
        v = self.viewers.get(ev.user)
        now = ev.deliver
        if not new:
            self.counters["duplicate_live_events"] += 1
            return out
        if v is None:
            v = self.viewer(ev.user)
        v.log(now, "event_observed", event_kind=ev.kind, event=ev.id)
        if ev.kind == "notification_opt_out":
            ch = ev.props.get("channel")
            for aid in list(v.action_ids):
                a = self.ledger.get(aid)
                if a and a["type"] == ch and a["status"] == "SCHEDULED" and a["send_dt"] > now:
                    if self.cancel(aid, now, "opt_out_event:%s" % ev.id):
                        out.append({"type": "cancel_after_opt_out", "viewer_id": ev.user, "action_id": aid, "event": ev.id})
            return self._rec(out)
        g = v.active_goal
        if g is None:
            return out
        paid_event = ev.kind in ("purchase_completed", "subscription_renewed")
        if not paid_event and ev.kind not in ("play_started", "episode_completed", "app_open"):
            return out
        vf = self.facts_for(ev.user, now, None, "full")
        before = g.goal_id
        still = self.refresh_goal(v, vf, now)
        if still is None:
            rec = {"type": "goal_closed_by_event", "viewer_id": ev.user, "goal_id": before, "event": ev.id, "event_kind": ev.kind,
                   "new_goal_status": v.goals[-1].status if v.goals else None,
                   "reason": ev.kind + (" (purchase after the decision: first-paid/trial/lapsed goals no longer make sense)" if paid_event else "")}
            out.append(rec)
            if paid_event:
                ch = choose_goal(vf, self.cfg, self.cat, self.reg)
                if ch.chosen is not None and v.arm == "agent":
                    ng = open_goal(ch.chosen, self.reg, now + timedelta(seconds=1), "REPLAN:%s" % ev.id)
                    ng.params = dict(ch.chosen.params)
                    v.goals.append(ng)
                    self.open_by_goal[ng.goal_id].add(ev.user)
                    v.planned_message_goal = None
                    v.last_page = None if not v.last_page else {**v.last_page, "invalidated": True}
                    v.log(now, "replanned", from_goal=before, to_goal=ng.goal_id, reason="purchase " + ev.id)
                    out.append({"type": "replan", "viewer_id": ev.user, "from_goal": before, "to_goal": ng.goal_id, "event": ev.id,
                                "status_now": vf.status})
                    did = self._did()
                    self.decisions.append({"decision_id": did, "viewer_id": ev.user, "arm": v.arm, "rule_version": RULE_VERSION,
                                           "trigger": {"event": ev.kind, "event_id": ev.id, "received_at": iso(ev.recv), "deliver_at": iso(now),
                                                       "event_age_s": round(ev.buffered_s, 1)},
                                           "goal": {"id": ng.goal_id, "deadline": iso(ng.deadline), "name": ng.name,
                                                    "success": {"event": ng.rule.event, "condition": ng.rule.text, "rule_version": ng.rule.version}},
                                           "action": "NO_ACTION", "status": "REPLANNED",
                                           "no_action_reason": "viewer is not in a session; new goal takes effect at the next app open. "
                                                               "Pending messages for %s cancelled." % before,
                                           "evidence": ["event:%s" % ev.id] + ch.chosen.evidence, "assumptions": list(ch.chosen.assumptions),
                                           "conflicts": vf.conflicts, "confidence": 0.8, "confidence_reason": "purchase is first-hand evidence",
                                           "checks": {"passed": ["holdout", "eligibility"], "failed": []}, "decided_in_ms": 2.0,
                                           "published_at": iso(now), "latency_s": 0.0, "model_calls": 0, "tool_calls": 2,
                                           "fallback_used": [], "actions": []})
                    v.decision_ids.append(did)
                    self.counters["replans"] += 1
        return self._rec(out)

    def _rec(self, out: List[dict]) -> List[dict]:
        self.revisions.extend(out)
        return out

    # ------------------------------------------------------------------ control events
    def on_control(self, c: dict, now: datetime) -> dict:
        kind = c["type"]
        rec = {"type": kind, "at": c["at"]}
        if kind == "title_withdrawn":
            ids = self.cat.withdraw(c["title_id"], now)
            slug = self.cat.titles[c["title_id"]].slug if c["title_id"] in self.cat.titles else None
            affected_actions = sorted(self.slug_index.get(slug, ())) if slug else []
            pages_republished, msgs_cancelled, unreplaceable = [], [], []
            for aid in affected_actions:
                a = self.ledger[aid]
                if a["status"] == "CANCELLED":
                    continue
                v = self.viewer(a["user_id"])
                if a["type"] == "home_page":
                    if now >= a["_published_dt"] + timedelta(minutes=self.cfg.page_ttl_min):
                        continue   # already expired, nothing is live
                    vf = self.facts_for(a["user_id"], now, None, "full")
                    vf.dialect, vf.dialect_source = a["dialect"], "app_setting"
                    page = build_page(vf, a.get("goal_id"), {}, self.cat, self.cfg, now, None, exclude_slugs={slug})
                    if page is not None and self.verifier.verify_page(a["user_id"], page.to_action(), now, a["dialect"]).ok:
                        na = {"action_id": self._aid(), "decision_id": a["decision_id"], "user_id": a["user_id"],
                              "trigger_event_id": a["trigger_event_id"], "published_at": iso(now + timedelta(milliseconds=40)),
                              "goal_id": a.get("goal_id"), "supersedes": aid, "revision_reason": "title_withdrawn:%s" % c["title_id"],
                              "expires_at": a["expires_at"], **page.to_action()}
                        self._register(v, na, now)
                        a["status"] = "SUPERSEDED"
                        v.last_page = {"action_id": na["action_id"], "dialect": na["dialect"], "published_at": na["published_at"],
                                       "published_dt": now, "goal_id": a.get("goal_id")}
                        v.log(now, "page_republished_without_withdrawn_title", old=aid, new=na["action_id"])
                        pages_republished.append(na["action_id"])
                    else:
                        unreplaceable.append(aid)
                elif a["status"] == "SCHEDULED" and a["send_dt"] > now:
                    if self.cancel(aid, now, "title_withdrawn:%s" % c["title_id"]):
                        msgs_cancelled.append(aid)
            rec.update(title_id=c["title_id"], slug=slug, variants_withdrawn=ids, affected_actions=len(affected_actions),
                       pages_republished=len(pages_republished), messages_cancelled=len(msgs_cancelled),
                       unreplaceable_pages=unreplaceable)
            self.counters["withdrawal_pages_republished"] += len(pages_republished)
        elif kind == "goal_rule_corrected":
            gid, rule, retro = self.reg.apply_correction(c)
            affected = sorted(self.open_by_goal.get(gid, ()))
            revised = []
            for u in affected if retro else []:
                v = self.viewer(u)
                g = v.active_goal
                if g is None or g.goal_id != gid:
                    continue
                old = g.rule.to_dict()
                g.rule = type(rule)(**{**rule.__dict__})
                g.revisions.append({"at": iso(now), "event": "rule_corrected", "from_version": old["version"], "to_version": rule.version,
                                    "old_success": old, "new_success": rule.to_dict()})
                v.log(now, "goal_rule_corrected", goal=gid, version=rule.version, note=c.get("note"))
                ok, evid, late = evaluate_goal(g, self.hist.get(u, []), self.cat, until=now)
                if ok:
                    self._close_goal(v, g, "succeeded", now, {"evidence": evid, "note": "satisfied under corrected rule"})
                revised.append(u)
            for g_ in self.analysis_goals(gid):
                g_["rule_version"] = rule.version
            rec.update(goal_id=gid, new_rule_version=rule.version, retroactive=retro, open_goals_revised=len(revised),
                       viewers_total=len(self.viewers), reprocessed_all_viewers=False, new_success=rule.to_dict())
            self.counters["rule_corrections"] += 1
        else:
            rec.update({k: x for k, x in c.items() if k not in ("type", "at")})
        self.revisions.append(rec)
        return rec

    def analysis_goals(self, gid: str):
        return [v.analysis_goal for v in self.viewers.values() if v.analysis_goal and v.analysis_goal.get("goal_id") == gid]

    # ------------------------------------------------------------------ dispatch gate (runs again at send time)
    def dispatch(self, action_id: str, now: datetime) -> str:
        a = self.ledger.get(action_id)
        if not a or a["status"] != "SCHEDULED":
            return "skipped"
        v = self.viewer(a["user_id"])
        g = next((x for x in v.goals if x.goal_id == a.get("goal_id")), None)
        vf = self.facts_for(a["user_id"], now, None, "full")
        vf_d = v.last_page["dialect"] if v.last_page else vf.dialect
        # the viewer's app dialect, as last observed, must still match the message's dialect
        t = self.cat.titles.get(a.get("title_id"))
        dialect = t.dialect if t else None
        last_setting = vf.features.settings[-1].dialect if vf.features and vf.features.settings else None
        src = "app_setting" if last_setting == dialect else "mismatch"
        mv = self.verifier.verify_message(a["user_id"], a, now, last_setting, src, g is not None and g.status == "active", check_time=a["send_dt"])
        if not mv.ok:
            self.cancel(action_id, now, "dispatch_gate_failed:" + ",".join(f["check"] for f in mv.failed()))
            return "cancelled"
        a["status"] = "SENT_REQUESTED"
        return "sent"

    def snapshot(self, user: str) -> dict:
        v = self.viewer(user)
        d = v.to_dict()
        d["actions"] = [{k: x for k, x in self.ledger[a].items() if not k.startswith("_") and k != "send_dt"} for a in v.action_ids if a in self.ledger]
        return d
