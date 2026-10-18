"""Viewer understanding: event features plus the fact resolver with explicit per-fact precedence.

Precedence table (also in ARCHITECTURE.md). Newest or largest source never wins by default; each fact names the
source that is *authoritative for that question* and what happens on disagreement.

| fact            | authoritative source                                   | fallbacks (in order)                          |
|-----------------|--------------------------------------------------------|-----------------------------------------------|
| dialect to show | app setting on the trigger (what the app will request) | last valid app setting -> profile -> campaign |
| subscription    | sub events newer than the profile row > profile row    | contradicted 'never' -> event history         |
| signup time     | profile (server-side) > signup event (est. time)       | event only for users with no profile row      |
| consent         | profile flag AND no opt-out event ever seen            | no profile row => no consent                  |
| language        | trigger app_language                                   | last event language -> profile                |
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .loader import VALID_DIALECTS, Catalogue, Ev
from .security import parse_campaign_deeplink
from .timeutil import IST, iso, parse_ts, ist_date, ist_hour

PAID_EVENTS = ("purchase_completed", "subscription_renewed")


@dataclass
class Fact:
    name: str
    value: object
    source: str
    observed_at: Optional[datetime]
    ref: str
    freshness_s: Optional[float]
    note: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "source": self.source, "observed_at": iso(self.observed_at),
                "freshness_s": None if self.freshness_s is None else round(self.freshness_s), "ref": self.ref,
                "note": self.note}


class Features:
    """Everything derivable from the event history visible at `now` (recv <= now)."""

    def __init__(self, user: str, events: List[Ev], now: datetime):
        self.user, self.now = user, now
        self.events = [e for e in events if e.recv <= now]
        self.plays: List[Ev] = []
        self.completes: List[Ev] = []
        self.opens: List[Ev] = []
        self.views: List[Ev] = []
        self.paywalls: List[Ev] = []
        self.paid_events: List[Ev] = []
        self.lapses: List[Ev] = []
        self.optouts: Dict[str, Ev] = {}
        self.delivered: List[Ev] = []
        self.settings: List[Ev] = []          # events carrying a *valid* app dialect
        self.signup_event: Optional[Ev] = None
        self.deeplinks: List[Tuple[Ev, dict]] = []
        for e in self.events:
            k = e.kind
            if e.dialect in VALID_DIALECTS:
                self.settings.append(e)
            if k == "play_started":
                self.plays.append(e)
            elif k == "episode_completed":
                self.completes.append(e)
            elif k == "app_open":
                self.opens.append(e)
                dl = parse_campaign_deeplink(e.props.get("deeplink")) if e.props.get("deeplink") else None
                if dl:
                    self.deeplinks.append((e, dl))
            elif k in ("title_viewed", "home_viewed"):
                self.views.append(e)
            elif k == "paywall_viewed":
                self.paywalls.append(e)
            elif k in PAID_EVENTS:
                self.paid_events.append(e)
            elif k == "subscription_lapsed":
                self.lapses.append(e)
            elif k == "notification_opt_out":
                self.optouts.setdefault(e.props.get("channel"), e)
            elif k == "message_delivered":
                self.delivered.append(e)
            elif k == "signup_completed" and self.signup_event is None:
                self.signup_event = e

    # ---- derived helpers (all as of `now`) ----
    def last_open(self, before: Optional[datetime] = None) -> Optional[Ev]:
        cands = [e for e in self.opens if before is None or e.recv < before]
        return max(cands, key=lambda e: e.est) if cands else None

    def play_days(self, days: int) -> set:
        lo = self.now - timedelta(days=days)
        return {ist_date(e.est) for e in self.plays if lo <= e.est <= self.now}

    def messages_on_ist_day(self, day) -> int:
        return sum(1 for e in self.delivered if e.props.get("channel") in ("push", "whatsapp") and ist_date(e.est) == day)

    def messages_last_days(self, days: int) -> int:
        lo = self.now - timedelta(days=days)
        return sum(1 for e in self.delivered if e.recv >= lo)

    def habitual_hour(self) -> Optional[float]:
        hrs = [ist_hour(e.est) for e in self.opens if 8 <= ist_hour(e.est) < 22]
        if len(hrs) < 3:
            return None
        c = Counter(int(h) for h in hrs)
        return float(c.most_common(1)[0][0]) + 0.5

    def open_without_play_rate(self) -> Optional[float]:
        sess_open = {e.session for e in self.opens}
        if len(sess_open) < 3:
            return None
        played = {e.session for e in self.plays}
        return 1.0 - len(sess_open & played) / len(sess_open)

    def genre_affinity(self, cat: Catalogue) -> Counter:
        c: Counter = Counter()
        for e in self.completes:
            t = cat.titles.get(e.props.get("title_id"))
            if t:
                for g in t.genres:
                    c[g] += 2
        for e in self.plays:
            t = cat.titles.get(e.props.get("title_id"))
            if t:
                for g in t.genres:
                    c[g] += 1
        return c

    def series_progress(self, cat: Catalogue) -> Dict[str, dict]:
        """slug -> {started, watched_episode_ids, last_play (est), title_ids, ctype}. Films excluded."""
        prog: Dict[str, dict] = {}
        for e in sorted(self.plays + self.completes, key=lambda x: x.est):
            t = cat.titles.get(e.props.get("title_id"))
            if not t or t.ctype == "film":
                continue
            d = prog.setdefault(t.slug, {"watched": set(), "last_play": e.est, "title_ids": set(), "dialect": t.dialect,
                                         "last_ev": e.id, "ctype": t.ctype})
            d["watched"].add(e.props.get("episode_id"))
            d["title_ids"].add(t.id)
            if e.kind == "play_started" and e.est >= d["last_play"]:
                d["last_play"] = e.est
                d["last_ev"] = e.id
        return prog

    def watched_slugs(self, cat: Catalogue) -> set:
        out = set()
        for e in self.plays + self.completes:
            t = cat.titles.get(e.props.get("title_id"))
            if t:
                out.add(t.slug)
        return out


@dataclass
class ViewerFacts:
    viewer_id: str
    now: datetime
    mode: str                      # "full" | "snapshot_cache" (data store degraded)
    features: Optional[Features]
    dialect: Optional[str] = None
    dialect_source: str = "unresolved"
    dialect_conflicts: List[str] = field(default_factory=list)
    dialect_note: str = ""
    language: Optional[str] = None
    status: str = "unknown"        # never | trial_active | active | lapsed | unknown
    status_source: str = ""
    status_ref: List[str] = field(default_factory=list)
    status_ambiguous: bool = False
    has_paid_ever: Optional[bool] = None
    lapse_at: Optional[datetime] = None
    lapse_dated: bool = False
    plan_ends_at: Optional[datetime] = None
    signup_at: Optional[datetime] = None
    signup_source: str = ""
    consent: Dict[str, Tuple[bool, str]] = field(default_factory=dict)
    profile_row: Optional[dict] = None
    profile_age_s: Optional[float] = None
    conflicts: List[str] = field(default_factory=list)
    facts: List[Fact] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    penalties: List[Tuple[str, float]] = field(default_factory=list)  # (reason, multiplier) for confidence

    def can_push(self) -> bool:
        return self.consent.get("push", (False, ""))[0]

    def can_whatsapp(self) -> bool:
        return self.consent.get("whatsapp", (False, ""))[0]

    def summary(self) -> dict:
        """Compact, PII-free-ish digest that is the only thing a hosted model would ever see."""
        f = self.features
        return {"dialect": self.dialect, "language": self.language, "subscription": self.status,
                "signup_days": None if not self.signup_at else round((self.now - self.signup_at).total_seconds() / 86400, 1),
                "plays": len(f.plays) if f else None, "completions": len(f.completes) if f else None,
                "mode": self.mode}


def _row_status_events(feats: Features, before: Optional[datetime] = None):
    evs = [e for e in feats.paid_events + feats.lapses if before is None or e.recv < before]
    evs.sort(key=lambda e: e.recv)
    st = None
    for e in evs:
        if e.kind == "purchase_completed":
            st = "trial_active" if e.props.get("is_trial") else "active"
        elif e.kind == "subscription_renewed":
            st = "active"
        else:
            st = "lapsed"
    return st, (evs[-1] if evs else None)


def pick_profile_row(rows: List[dict], feats: Optional[Features], snapshot_at: Optional[datetime]):
    """Pick one of possibly several snapshot rows. Rule: a row contradicted by events loses; otherwise the most recently
    updated row wins. Disagreement between rows is returned as conflict strings (and never silently dropped)."""
    if not rows:
        return None, [], False
    if len(rows) == 1:
        return rows[0], [], False
    ev_state, _ = _row_status_events(feats, snapshot_at) if feats else (None, None)
    paid_seen = bool(feats and feats.paid_events)
    best, best_score = None, None
    for r in rows:
        score = 0.0
        st = r["subscription_status"]
        if ev_state and st == ev_state:
            score += 2
        if paid_seen and st == "never":
            score -= 3
        if feats and feats.settings and r.get("preferred_dialect") == feats.settings[-1].dialect:
            score += 1
        score += (parse_ts(r["updated_at"]).timestamp() / 1e11)  # tie-break: newest
        if best is None or score > best_score:
            best, best_score = r, score
    conflicts, ambiguous = [], False
    for fld in ("subscription_status", "preferred_dialect", "notifications_opt_in", "whatsapp_opt_in"):
        vals = sorted({r.get(fld) or "" for r in rows})
        if len(vals) > 1:
            conflicts.append("profile_rows:%s:%s" % (fld, "|".join(vals)))
    if len({r["subscription_status"] for r in rows}) > 1:
        loser_ok = not paid_seen
        ambiguous = loser_ok  # events cannot arbitrate (absence of events in a 30-day window is not evidence of "never")
    return best, conflicts, ambiguous


def resolve_facts(viewer_id: str, now: datetime, trigger: Optional[Ev], rows: List[dict], events: List[Ev],
                  snapshot_at: Optional[datetime], cat: Catalogue, mode: str = "full") -> ViewerFacts:
    if trigger is not None:   # the triggering open is the thing being decided about, not part of the prior history
        events = [e for e in events if e.id != trigger.id]
    feats = Features(viewer_id, events, now) if mode == "full" else None
    vf = ViewerFacts(viewer_id, now, mode, feats)
    row, dup_conf, ambiguous = pick_profile_row(rows, feats, snapshot_at)
    vf.profile_row = row
    vf.conflicts.extend(dup_conf)
    if dup_conf:
        vf.facts.append(Fact("profile_rows", len(rows), "profile_snapshot", snapshot_at, "profile:%s" % viewer_id, None,
                             "%d rows; chosen by event consistency then updated_at" % len(rows)))
    if row and snapshot_at:
        vf.profile_age_s = (now - snapshot_at).total_seconds()

    # ---------------- dialect ----------------
    trig_d = trigger.dialect if trigger is not None else None
    prof_d = (row or {}).get("preferred_dialect") or None
    last_setting = feats.settings[-1] if feats and feats.settings else None
    conflicts: List[str] = []
    camp = None
    if feats and trigger is not None and trigger.props.get("deeplink"):
        camp = parse_campaign_deeplink(trigger.props["deeplink"])
    elif trigger is not None and trigger.props.get("deeplink"):
        camp = parse_campaign_deeplink(trigger.props["deeplink"])
    if trig_d in VALID_DIALECTS:
        vf.dialect, vf.dialect_source = trig_d, "app_setting"
        vf.facts.append(Fact("dialect", trig_d, "app_setting(trigger)", trigger.recv, "event:%s" % trigger.id, 0.0))
    elif last_setting is not None:
        age_d = (now - last_setting.recv).total_seconds() / 86400
        vf.dialect, vf.dialect_source = last_setting.dialect, "last_valid_app_setting"
        vf.dialect_note = "trigger carried invalid app_dialect %r; used last valid app setting from %s" % (trig_d, last_setting.id)
        vf.facts.append(Fact("dialect", last_setting.dialect, "last_valid_app_setting", last_setting.recv,
                             "event:%s" % last_setting.id, (now - last_setting.recv).total_seconds(), vf.dialect_note))
        vf.penalties.append(("trigger app_dialect invalid; fell back to last valid app setting", 0.75 if age_d <= 14 else 0.5))
    elif prof_d in VALID_DIALECTS:
        vf.dialect, vf.dialect_source = prof_d, "profile"
        vf.dialect_note = "no valid app setting visible; used profile preference (1+ day stale)"
        vf.facts.append(Fact("dialect", prof_d, "profile", snapshot_at, "profile:%s" % viewer_id,
                             vf.profile_age_s, vf.dialect_note))
        vf.penalties.append(("no valid app setting; profile dialect is a stale preference", 0.55))
    elif camp and camp["dialect"] in VALID_DIALECTS:
        vf.dialect, vf.dialect_source = camp["dialect"], "campaign_link"
        vf.dialect_note = "only the campaign link names a valid dialect"
        vf.penalties.append(("dialect known only from campaign link", 0.4))
    for src, val in (("profile", prof_d), ("campaign", camp["dialect"] if camp else None),
                     ("trigger_app_setting", trig_d if trig_d and trig_d != vf.dialect else None),
                     ("last_valid_setting", last_setting.dialect if last_setting else None)):
        if val and vf.dialect and val != vf.dialect:
            conflicts.append("%s:%s" % (src, val))
    vf.dialect_conflicts = conflicts
    if len({v.split(":")[1] for v in conflicts} | ({vf.dialect} if vf.dialect else set())) >= 3:
        vf.conflicts.append("three_way_dialect_conflict")
    vf.conflicts.extend("dialect:" + c for c in conflicts)

    # ---------------- language ----------------
    if trigger is not None and trigger.lang:
        vf.language = trigger.lang
    elif feats and feats.events:
        langs = [e.lang for e in feats.events if e.lang]
        vf.language = langs[-1] if langs else (row or {}).get("preferred_language")
    else:
        vf.language = (row or {}).get("preferred_language")

    # ---------------- signup ----------------
    p_signup = parse_ts(row["signup_at"]) if row and row.get("signup_at") else None
    e_signup = feats.signup_event if feats else None
    if p_signup:
        vf.signup_at, vf.signup_source = p_signup, "profile"
        vf.facts.append(Fact("signup_at", iso(p_signup), "profile(server-side)", snapshot_at, "profile:%s" % viewer_id,
                             vf.profile_age_s))
        if e_signup and abs((e_signup.est - p_signup).total_seconds()) > 3600:
            vf.conflicts.append("signup:profile!=event(%s)" % e_signup.id)
    elif e_signup:
        vf.signup_at, vf.signup_source = e_signup.est, "signup_event"
        vf.facts.append(Fact("signup_at", iso(e_signup.est), "signup_completed event (est. from received_at)", e_signup.recv,
                             "event:%s" % e_signup.id, (now - e_signup.recv).total_seconds(),
                             "no profile row yet (brand-new viewer)"))
        vf.assumptions.append("no profile row: facts built from signup_completed event")

    # ---------------- subscription ----------------
    st_prof = (row or {}).get("subscription_status") or None
    ev_state, ev_last = _row_status_events(feats) if feats else (None, None)
    plan_ends = parse_ts(row["plan_ends_at"]) if row and row.get("plan_ends_at") else None
    vf.plan_ends_at = plan_ends
    if mode != "full":
        vf.status, vf.status_source = st_prof or "unknown", "profile_snapshot(cache)"
        vf.status_ambiguous = ambiguous
    elif st_prof is None:
        if ev_state:
            vf.status, vf.status_source = ev_state, "events"
            vf.status_ref = ["event:%s" % ev_last.id]
        elif e_signup is not None:
            vf.status, vf.status_source = "never", "events(signup observed, no purchase since)"
            vf.assumptions.append("no profile row; signup observed in event stream with no purchase event since => never paid")
        else:
            vf.status, vf.status_source = "unknown", "none"
    else:
        vf.status, vf.status_source = st_prof, "profile_snapshot"
        vf.status_ref = ["profile:%s" % viewer_id]
        vf.status_ambiguous = ambiguous
        if ev_last is not None and snapshot_at is not None and ev_last.recv > max(snapshot_at, parse_ts(row["updated_at"])):
            vf.status, vf.status_source = ev_state, "event_newer_than_profile"
            vf.status_ref = ["event:%s" % ev_last.id]
            vf.conflicts.append("subscription:profile:%s!=event:%s(%s)" % (st_prof, ev_state, ev_last.kind))
        elif st_prof == "never" and feats.paid_events:
            vf.status, vf.status_source = ev_state, "event_history_contradicts_profile"
            vf.status_ref = ["event:%s" % feats.paid_events[-1].id]
            vf.conflicts.append("subscription:profile:never!=events:paid_before_snapshot")
        elif ev_state and ev_state != st_prof and ev_last is not None and ev_last.recv > parse_ts(row["updated_at"]):
            vf.status, vf.status_source = ev_state, "event_newer_than_profile_row"
            vf.status_ref = ["event:%s" % ev_last.id]
            vf.conflicts.append("subscription:profile:%s!=event:%s" % (st_prof, ev_state))
    src_ev = ev_last is not None and vf.status_source.startswith("event") and not vf.status_source.startswith("events(")
    vf.facts.append(Fact("subscription_status", vf.status, vf.status_source,
                         ev_last.recv if src_ev else snapshot_at,
                         (vf.status_ref or ["profile:%s" % viewer_id])[0],
                         (now - ev_last.recv).total_seconds() if src_ev else vf.profile_age_s))
    if vf.status_ambiguous:
        vf.conflicts.append("subscription:duplicate_profile_rows_disagree")
        vf.penalties.append(("duplicate profile rows disagree on subscription status and events cannot arbitrate", 0.6))
    if vf.status in ("never", "trial_active", "lapsed", "active") and vf.profile_age_s and vf.profile_age_s > 0:
        pass
    # paid-ever + lapse recency
    paid_evt = bool(feats and feats.paid_events)
    vf.has_paid_ever = True if (vf.status in ("active", "lapsed") or paid_evt) else (False if vf.status in ("never", "trial_active") else None)
    if vf.status == "lapsed":
        lapse_ev = [e for e in (feats.lapses if feats else [])]
        if lapse_ev:
            vf.lapse_at, vf.lapse_dated = max(lapse_ev, key=lambda e: e.est).est, True
        elif plan_ends:
            vf.lapse_at, vf.lapse_dated = plan_ends, True
    # ---------------- consent ----------------
    for ch, col in (("push", "notifications_opt_in"), ("whatsapp", "whatsapp_opt_in")):
        if not rows:
            vf.consent[ch] = (False, "no_profile_row")
            continue
        flag = all(str(r.get(col, "")).lower() == "true" for r in rows)   # conservative across duplicate rows
        src = "profile" if flag else "profile_false"
        if flag and feats is not None and ch in feats.optouts:
            flag, src = False, "opt_out_event:%s" % feats.optouts[ch].id
        vf.consent[ch] = (flag, src)
        vf.facts.append(Fact("consent_" + ch, flag, src, snapshot_at, "profile:%s" % viewer_id, vf.profile_age_s))
    if mode != "full":
        vf.penalties.append(("data store degraded: facts come from the day-old warehouse snapshot only", 0.55))
        vf.assumptions.append("event history unavailable (data store degraded); opt-out events after the snapshot are unknown")
        # unknown opt-outs after the snapshot make messaging unsafe
        vf.consent = {k: (False, "degraded_store_unverifiable_optout") for k in vf.consent}
    if vf.profile_age_s is not None and vf.profile_age_s > 36 * 3600:
        vf.penalties.append(("profile snapshot is %.0f h old" % (vf.profile_age_s / 3600), 0.92))
    return vf
