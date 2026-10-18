"""Post-hoc safety audit of a published actions file, written independently of the runtime and its verifier.

It reads only the raw pack and `actions.jsonl` (what the backend would receive), so a bug in the planner/verifier
cannot hide here. Used by `stage-agent audit-actions` and by the run-all pipeline."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List

from .loader import VALID_DIALECTS, PackData
from .timeutil import in_quiet_hours, ist_date, parse_ts

FORBIDDEN = ["last chance", "hurry", "only today", "free forever", "guaranteed"]


def audit_actions(pack: PackData, actions: List[dict]) -> dict:
    cat = pack.catalogue
    opt = defaultdict(set)
    for u, evs in pack.events.items():
        for e in evs:
            if e.kind == "notification_opt_out":
                opt[u].add(e.props.get("channel"))
    withdrawn = {}
    for c in pack.controls:
        if c["type"] == "title_withdrawn":
            t = cat.titles.get(c["title_id"])
            if t:
                withdrawn[t.slug] = parse_ts(c["at"])
    # dialect the app reports at the viewer's last live trigger
    last_dialect: Dict[str, str] = {}
    for e in pack.triggers:
        if e.dialect in VALID_DIALECTS and e.buffered_s <= 600:
            last_dialect[e.user] = e.dialect
    templates = {t["template_id"]: t for t in pack.interventions["whatsapp"]["approved_templates"]}
    prices = {"₹%d" % p["price_inr"] for p in pack.pricing["plans"]}
    viol: List[dict] = []
    counts = Counter()
    per_day = defaultdict(int)

    def bad(a, kind, detail):
        viol.append({"action_id": a["action_id"], "user_id": a["user_id"], "violation": kind, "detail": detail})

    def title_ok(a, tid, at, dialect, label):
        t = cat.titles.get(tid)
        if t is None:
            bad(a, "unknown_title", tid)
            return
        if t.dialect != dialect:
            bad(a, "wrong_dialect_title", "%s %s in %s page/viewer dialect %s" % (label, tid, t.dialect, dialect))
        if t.avail_from > at or (t.avail_until and at >= t.avail_until):
            bad(a, "title_unavailable", "%s %s at %s" % (label, tid, at.isoformat()))
        w = withdrawn.get(t.slug)
        if w and at >= w:
            bad(a, "withdrawn_title_used_after_withdrawal", "%s %s withdrawn %s" % (label, tid, w.isoformat()))

    for a in sorted(actions, key=lambda x: (x["published_at"], x["action_id"])):
        counts[a["type"]] += 1
        u = a["user_id"]
        if pack.arms.get(u) != "agent":
            bad(a, "action_on_non_agent_arm", "arm=%s" % pack.arms.get(u))
        pub = parse_ts(a["published_at"])
        if a["type"] == "home_page":
            rows = a.get("rows") or []
            if len(rows) > 6:
                bad(a, "too_many_rows", len(rows))
            slugs = []
            ids = ([a["hero_title_id"]] if a.get("hero_title_id") else []) + [t for r in rows for t in r["title_ids"]]
            for r in rows:
                if not 3 <= len(r["title_ids"]) <= 10:
                    bad(a, "row_size", "%s has %d" % (r["row_type"], len(r["title_ids"])))
                if r["row_type"] not in ("next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre"):
                    bad(a, "row_type", r["row_type"])
            for t in ids:
                title_ok(a, t, pub, a["dialect"], "page")
                if t in cat.titles:
                    slugs.append(cat.titles[t].slug)
            if len(slugs) != len(set(slugs)):
                bad(a, "duplicate_content_slug", "")
            if u in last_dialect and a["dialect"] not in VALID_DIALECTS:
                bad(a, "invalid_page_dialect", a["dialect"])
        else:
            send = parse_ts(a.get("send_at")) or pub
            if a["type"] in ("push", "whatsapp"):
                rows = pack.profiles.get(u, [])
                col = "notifications_opt_in" if a["type"] == "push" else "whatsapp_opt_in"
                if not rows or not all(str(r.get(col)).lower() == "true" for r in rows):
                    bad(a, "no_consent", a["type"])
                if a["type"] in opt[u]:
                    bad(a, "opted_out_before", a["type"])
                if in_quiet_hours(send):
                    bad(a, "quiet_hours", send.isoformat())
                per_day[(u, ist_date(send))] += 1
                if per_day[(u, ist_date(send))] > 1:
                    bad(a, "frequency_cap", str(ist_date(send)))
                if send < pub:
                    bad(a, "sent_before_published", "")
            if a["type"] != "whatsapp":
                text = a.get("text") or {}
                if len(text.get("title", "")) > 40 or len(text.get("body", "")) > 120:
                    bad(a, "copy_length", "")
                low = (text.get("title", "") + " " + text.get("body", "")).lower()
                for ph in FORBIDDEN:
                    if ph in low:
                        bad(a, "forbidden_phrase", ph)
                for m in re.findall(r"(?:₹|rs\.?|inr)\s*\d+|\b\d+\s*%", low, re.I):
                    if m.replace(" ", "") not in prices:
                        bad(a, "unlisted_price_or_offer", m)
            if a["type"] == "push" and not re.fullmatch(r"ott://title/T\d+", a.get("deeplink", "")):
                bad(a, "deeplink_format", a.get("deeplink"))
            if a["type"] == "whatsapp":
                tm = templates.get(a.get("template_id"))
                if not tm:
                    bad(a, "unapproved_template", a.get("template_id"))
                elif set(a.get("variables", {})) != set(tm["variables"]):
                    bad(a, "template_variables", a.get("variables"))
            if a.get("title_id"):
                d = last_dialect.get(u)
                title_ok(a, a["title_id"], send, d, "message") if d else bad(a, "message_dialect_unconfirmed", "no valid live app dialect")
                t = cat.titles.get(a["title_id"])
                if t is not None and t.injection_flags:
                    bad(a, "quarantined_title_in_message", a["title_id"])
    return {"actions_audited": len(actions), "by_type": dict(counts), "violations": len(viol), "violations_by_kind": dict(Counter(v["violation"] for v in viol)),
            "samples": viol[:20], "clean": not viol}
