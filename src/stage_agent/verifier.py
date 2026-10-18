"""Independent verifier.

It receives a *proposed action* plus raw ground truth (catalogue, arms, consent ledger, message ledger, policy config)
and re-derives every check itself. It never reads the planner's own flags, never trusts model text and can reject any
action. Checks return (name, passed, detail); a failed check blocks that action.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import security
from .config import Config
from .loader import VALID_DIALECTS, Catalogue
from .timeutil import in_quiet_hours, ist_date

ALLOWED_ROW_TYPES = {"next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre"}
DEEPLINK_RE = re.compile(r"^ott://title/T\d+$")
PRICE_RE = re.compile(r"(₹|rs\.?|inr|rupees?)\s*\d+|\d+\s*(₹|rs\b|rupees?)|\b\d+\s*%|\b(discount|offer|cashback|coupon|deal)\b", re.I)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Verdict:
    results: List[CheckResult] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = ""):
        self.results.append(CheckResult(name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)

    def passed(self) -> List[str]:
        return [r.name for r in self.results if r.passed]

    def failed(self) -> List[dict]:
        return [{"check": r.name, "detail": r.detail} for r in self.results if not r.passed]


class ConsentLedger:
    """Ground truth for consent, independent of the planner's view: profile rows AND opt-out events, updated as live
    opt-out events stream in."""

    def __init__(self, profiles: Dict[str, List[dict]]):
        self.profiles = profiles
        self.optout: Dict[Tuple[str, str], str] = {}

    def record_optout(self, user: str, channel: str, ev_id: str):
        self.optout.setdefault((user, channel), ev_id)

    def allowed(self, user: str, channel: str) -> Tuple[bool, str]:
        if channel == "in_app_message":
            return True, "consent not required"
        rows = self.profiles.get(user)
        if not rows:
            return False, "no profile row => no consent"
        col = "notifications_opt_in" if channel == "push" else "whatsapp_opt_in"
        if not all(str(r.get(col, "")).lower() == "true" for r in rows):
            return False, "profile %s is not true (%d row(s))" % (col, len(rows))
        if (user, channel) in self.optout:
            return False, "opt_out event %s" % self.optout[(user, channel)]
        return True, "profile true and no opt-out event"


class MessageLedger:
    """Counts push+WhatsApp (and in-app) per viewer per IST day, including deliveries already in the event history."""

    def __init__(self):
        self.sched: Dict[str, List[Tuple[str, datetime, str]]] = {}   # user -> [(action_id, when, channel)]
        self.history: Dict[str, List[Tuple[datetime, str]]] = {}

    def seed_history(self, user: str, when: datetime, channel: str):
        self.history.setdefault(user, []).append((when, channel))

    def add(self, user: str, action_id: str, when: datetime, channel: str):
        self.sched.setdefault(user, []).append((action_id, when, channel))

    def remove(self, user: str, action_id: str):
        self.sched[user] = [x for x in self.sched.get(user, []) if x[0] != action_id]

    def count_day(self, user: str, day, channels=("push", "whatsapp"), exclude: Optional[str] = None) -> int:
        n = sum(1 for aid, w, ch in self.sched.get(user, []) if ch in channels and ist_date(w) == day and aid != exclude)
        n += sum(1 for w, ch in self.history.get(user, []) if ch in channels and ist_date(w) == day)
        return n


class Verifier:
    def __init__(self, cat: Catalogue, arms: Dict[str, str], consent: ConsentLedger, msgs: MessageLedger, interventions: dict,
                 pricing: dict, cfg: Config):
        self.cat, self.arms, self.consent, self.msgs, self.cfg = cat, arms, consent, msgs, cfg
        self.iv = interventions
        self.templates = {t["template_id"]: t for t in interventions["whatsapp"]["approved_templates"]}
        self.forbidden = [p.lower() for p in interventions["push"]["copy"]["forbidden_phrases"]]
        self.prices = {"₹%d" % p["price_inr"] for p in pricing["plans"]}
        self.price_values = {str(p["price_inr"]) for p in pricing["plans"]}

    # ------------------------------------------------------------ common
    def _holdout(self, v: Verdict, user: str):
        v.add("holdout", self.arms.get(user) != "holdout", "arm=%s" % self.arms.get(user, "unknown"))

    def _dialect_titles(self, v: Verdict, dialect: str, ids: List[str], expected: Optional[str]):
        bad = [t for t in ids if self.cat.titles.get(t) is None or self.cat.titles[t].dialect != dialect]
        v.add("dialect", dialect in VALID_DIALECTS and (expected is None or dialect == expected) and not bad,
              "page dialect %s vs resolved %s; wrong-dialect titles %s" % (dialect, expected, bad[:5]))

    def _available(self, v: Verdict, ids: List[str], at: datetime, label="availability"):
        bad = [t for t in ids if not self.cat.is_available(t, at)]
        v.add(label, not bad, "unavailable at %s: %s" % (at.isoformat(), bad[:5]))

    def _injection(self, v: Verdict, ids: List[str], strict: bool):
        flagged = [t for t in ids if t in self.cat.titles and self.cat.titles[t].injection_flags]
        v.add("injection_quarantine", not (strict and flagged), "titles with injected text in catalogue: %s" % flagged[:5])

    # ------------------------------------------------------------ goal / claims
    def verify_goal(self, goal_id: str, evidence: List[str], vf_status: str, store_has_event, rule_known: bool, measurable: bool) -> Verdict:
        v = Verdict()
        v.add("goal_measurable", measurable, "goal %s has a measurable success event" % goal_id)
        v.add("goal_rule_known", rule_known, "success rule version registered")
        missing = [e for e in evidence if e.startswith("event:") and not store_has_event(e[6:].split("(")[0])]
        v.add("evidence_resolvable", not missing, "evidence events not found in the viewer's store: %s" % missing[:3])
        return v

    def verify_model_claim(self, claim: dict, eligible_ids: set, supporting: Dict[str, List[str]], store_has_event) -> Tuple[bool, str]:
        """A model's 'user is eligible for G' is accepted only if the rule engine independently found the goal eligible AND
        its evidence references resolve. Reason text is ignored entirely ('confirmed in billing' proves nothing)."""
        gid = claim.get("goal_id")
        if gid is None:
            return True, "model declined to propose a goal"
        if not claim.get("eligible"):
            return True, "model says not eligible"
        if gid not in eligible_ids:
            return False, "unsupported_claim: no event or profile fact makes %s eligible" % gid
        ev = [e for e in supporting.get(gid, []) if e.startswith("event:")]
        if any(not store_has_event(e[6:].split("(")[0]) for e in ev):
            return False, "unsupported_claim: evidence references do not resolve"
        return True, "claim matches independently derived eligibility"

    # ------------------------------------------------------------ home page
    def verify_page(self, user: str, act: dict, now: datetime, resolved_dialect: Optional[str]) -> Verdict:
        v = Verdict()
        self._holdout(v, user)
        ids = ([act["hero_title_id"]] if act.get("hero_title_id") else []) + [t for r in act["rows"] for t in r["title_ids"]]
        v.add("page_has_content", bool(ids), "page lists %d titles" % len(ids))
        self._dialect_titles(v, act["dialect"], ids, resolved_dialect)
        self._available(v, ids, now)
        rows = act["rows"]
        v.add("page_structure", len(rows) <= self.cfg.max_rows and all(self.cfg.min_row_titles <= len(r["title_ids"]) <= self.cfg.max_row_titles for r in rows)
              and all(r["row_type"] in ALLOWED_ROW_TYPES for r in rows),
              "rows=%d (max %d), sizes %s, types %s" % (len(rows), self.cfg.max_rows, [len(r["title_ids"]) for r in rows], [r["row_type"] for r in rows]))
        slugs = [self.cat.titles[t].slug for t in ids if t in self.cat.titles]
        v.add("unique_content_slug", len(slugs) == len(set(slugs)), "duplicate slugs: %s" % sorted({s for s in slugs if slugs.count(s) > 1})[:3])
        v.add("no_continue_watching", all("continue" not in r["row_type"] for r in rows), "platform owns Continue Watching")
        return v

    # ------------------------------------------------------------ messages
    def verify_message(self, user: str, act: dict, now: datetime, resolved_dialect: Optional[str], dialect_source: str,
                       goal_active: bool = True, check_time: Optional[datetime] = None) -> Verdict:
        """Used both at planning time and again at the dispatch gate (`check_time` = send time)."""
        v = Verdict()
        ch = act["type"]
        when = check_time or (datetime.fromisoformat(act["send_at"].replace("Z", "+00:00")) if act.get("send_at") else now)
        self._holdout(v, user)
        ok, why = self.consent.allowed(user, ch)
        v.add("consent", ok, why)
        if ch in ("push", "whatsapp"):
            v.add("quiet_hours", not in_quiet_hours(when, self.cfg.send_end_h, self.cfg.send_start_h),
                  "send time %s IST hour must be in [08,22)" % when.astimezone().isoformat())
            day = ist_date(when)
            n = self.msgs.count_day(user, day, exclude=act.get("action_id"))
            v.add("frequency_cap", n == 0, "%d push/WhatsApp already counted for IST day %s" % (n, day))
        else:
            n = self.msgs.count_day(user, ist_date(when), ("in_app_message",), exclude=act.get("action_id"))
            v.add("frequency_cap", n < self.iv["in_app_message"]["max_per_day"], "%d in-app already on %s" % (n, ist_date(when)))
        v.add("dialect_confirmed", dialect_source == "app_setting" and resolved_dialect in VALID_DIALECTS,
              "dialect source %s" % dialect_source)
        tid = act.get("title_id")
        t = self.cat.titles.get(tid)
        v.add("dialect", t is not None and t.dialect == resolved_dialect, "title %s dialect %s vs %s" % (tid, t.dialect if t else None, resolved_dialect))
        self._available(v, [tid] if tid else [], when)
        self._injection(v, [tid] if tid else [], strict=True)
        v.add("goal_still_open", goal_active, "goal must still be active at send time")
        if ch == "whatsapp":
            tm = self.templates.get(act.get("template_id"))
            v.add("template_approved", tm is not None, "template %s" % act.get("template_id"))
            if tm is not None:
                vars_ = act.get("variables") or {}
                okv = set(vars_) == set(tm["variables"])
                if "title_name" in vars_:
                    okv = okv and t is not None and vars_["title_name"] == t.name
                if "dialect_name" in vars_:
                    okv = okv and t is not None and vars_["dialect_name"] == t.dialect
                if "plan_price" in vars_:
                    okv = okv and vars_["plan_price"] in self.prices
                v.add("template_variables", okv, "variables must come from catalogue/pricing: %s" % vars_)
        else:
            text = act.get("text") or {}
            title, body = text.get("title", ""), text.get("body", "")
            c = self.iv["push"]["copy"]
            v.add("copy_length", len(title) <= c["title_max_chars"] and len(body) <= c["body_max_chars"],
                  "title %d/%d body %d/%d" % (len(title), c["title_max_chars"], len(body), c["body_max_chars"]))
            low = (title + " " + body).lower()
            hit = [p for p in self.forbidden if p in low]
            v.add("forbidden_phrases", not hit, "found %s" % hit)
            money = [m.group(0) for m in PRICE_RE.finditer(title + " " + body)]
            bad_price = [m for m in money if m.replace(" ", "") not in self.prices]
            v.add("honest_copy_no_unlisted_price_or_offer", not bad_price, "price/offer claims: %s" % bad_price)
            v.add("copy_names_real_title", t is not None and t.name in (title + " " + body), "copy must name the title %s" % (t.name if t else None))
            v.add("copy_injection_scan", not security.scan(title + " " + body), "copy scanned for agent-addressed text")
            if ch == "push":
                v.add("deeplink_format", bool(DEEPLINK_RE.match(act.get("deeplink", ""))) and act.get("deeplink", "").endswith(str(tid)),
                      "deeplink %s" % act.get("deeplink"))
        return v
