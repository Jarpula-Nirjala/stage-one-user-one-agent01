"""Intervention planner: home page, one message (or none), timing. Pure planning - nothing here publishes anything;
the verifier decides whether a plan may go out."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .config import Config
from .facts import Features, ViewerFacts
from .goals import Eligibility
from .loader import Catalogue, Ev, Title
from .security import parse_campaign_deeplink
from .timeutil import IST, at_ist_hour, ist_date, next_send_window

ACTIVATION_GOALS = ("G01", "G02", "G03", "G12")
PAID_GOALS = ("G04", "G05", "G06")
CTYPE_LABEL = {"film": "film", "show": "show", "micro_drama": "micro-drama"}


def compute_popularity(cat: Catalogue, events_by_user: Dict[str, List[Ev]], as_of: datetime, days: int = 14) -> None:
    lo = as_of - timedelta(days=days)
    score: Counter = Counter()
    for evs in events_by_user.values():
        for e in evs:
            if e.recv < lo or e.recv > as_of:
                continue
            tid = e.props.get("title_id")
            t = cat.titles.get(tid) if tid else None
            if not t:
                continue
            if e.kind == "play_started":
                score[t.slug] += 1.0
            elif e.kind == "episode_completed":
                score[t.slug] += 2.0
            elif e.kind == "title_viewed":
                score[t.slug] += 0.2
    cat.popularity = dict(score)


# ------------------------------------------------------------------ home page
@dataclass
class PagePlan:
    dialect: str
    hero: Optional[str]
    rows: List[dict]
    evidence: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    slugs: List[str] = field(default_factory=list)

    def title_ids(self) -> List[str]:
        out = [self.hero] if self.hero else []
        for r in self.rows:
            out.extend(r["title_ids"])
        return out

    def to_action(self) -> dict:
        return {"type": "home_page", "dialect": self.dialect, "hero_title_id": self.hero, "rows": self.rows}


def build_page(vf: ViewerFacts, goal_id: Optional[str], params: dict, cat: Catalogue, cfg: Config, now: datetime,
               trigger: Optional[Ev], minimal: bool = False, exclude_slugs: Optional[set] = None) -> Optional[PagePlan]:
    d = vf.dialect
    if not d:
        return None
    feats: Optional[Features] = vf.features if not minimal else None
    lang = vf.language
    paying = vf.status in ("active", "trial_active")
    free_only = (goal_id in ACTIVATION_GOALS) and not paying
    horizon = now + timedelta(minutes=cfg.avoid_expiring_within_min)
    exclude_slugs = set(exclude_slugs or ())
    pop = cat.popularity
    notes: List[str] = []

    cands: Dict[str, Title] = {}
    for t in cat.by_dialect.get(d, []):
        if t.slug in exclude_slugs or t.slug in cands or t.injection_flags and False:
            continue
        v = cat.variant_for(t.slug, lang, now)
        if v is not None:
            cands[t.slug] = v

    def ranked(pred, key):
        pool = [t for t in cands.values() if pred(t)]
        pool.sort(key=key)
        return pool

    def outlives(t):  # titles that vanish mid-TTL rank lower
        return 1 if (t.avail_until is not None and t.avail_until < horizon) else 0

    base_key = lambda t: (outlives(t), -pop.get(t.slug, 0.0), t.id)
    affin = feats.genre_affinity(cat) if feats else Counter()
    top_genres = [g for g, _ in affin.most_common(3)]
    watched = feats.watched_slugs(cat) if feats else set()
    progress = feats.series_progress(cat) if feats else {}
    used: set = set()
    evidence: List[str] = []

    def free_ok(t: Title) -> bool:
        return not (free_only and t.premium)

    # ---- hero
    hero: Optional[Title] = None
    camp = parse_campaign_deeplink(trigger.props.get("deeplink")) if trigger is not None and trigger.props.get("deeplink") else None
    if camp and goal_id != "G07":
        ct = cat.titles.get(camp["title_id"])
        if ct and ct.dialect == d and ct.slug in cands and free_ok(cands[ct.slug]):
            hero = cands[ct.slug]
            evidence.append("event:%s(deeplink)" % trigger.id)
            notes.append("hero honours the campaign deeplink title (valid in the viewer's dialect)")
    if hero is None and goal_id == "G07" and params.get("title_id"):
        t = cat.titles.get(params["title_id"])
        if t and t.slug in cands:
            hero = cands[t.slug]
    if hero is None and not minimal:
        if goal_id in ACTIVATION_GOALS:
            pool = ranked(lambda t: free_ok(t), lambda t: (outlives(t), t.ctype != "micro_drama", -sum(g in top_genres for g in t.genres),
                                                           -pop.get(t.slug, 0.0), t.id))
        elif goal_id in PAID_GOALS:
            pool = ranked(lambda t: t.premium, base_key) or ranked(lambda t: True, base_key)
        elif goal_id == "G08":
            pool = ranked(lambda t: t.release <= now, lambda t: (outlives(t), -t.release.timestamp(), t.id))
        else:
            pool = ranked(lambda t: True, lambda t: (outlives(t), -sum(g in top_genres for g in t.genres), -pop.get(t.slug, 0.0), t.id))
        hero = pool[0] if pool else None
    if hero is not None:
        used.add(hero.slug)
        evidence.append("catalogue:%s" % hero.id)

    rows: List[dict] = []

    def add_row(rtype: str, pool: List[Title], size: int):
        take = [t for t in pool if t.slug not in used][:size]
        if len(take) >= cfg.min_row_titles and len(rows) < cfg.max_rows:
            for t in take:
                used.add(t.slug)
            rows.append({"row_type": rtype, "title_ids": [t.id for t in take]})
            evidence.extend("catalogue:%s" % t.id for t in take[:2])
            return True
        return False

    size = min(cfg.target_row_titles, cfg.max_row_titles)
    if not minimal:
        cont = []
        for slug, p in sorted(progress.items(), key=lambda kv: kv[1]["last_play"], reverse=True):
            t = cands.get(slug)
            if t and p["dialect"] == d and cat.next_episode(slug, p["watched"]) is not None:
                cont.append(t)
        if not add_row("next_episode", [t for t in cont if free_ok(t) or paying or goal_id == "G07"], size):
            notes.append("next_episode row dropped: only %d unfinished series in this dialect" % len(cont))
        # because_you_watched: continuations that did not fit a row of their own come first, then genre neighbours
        seeds = [t for t in cont if t.slug not in used]
        nb = ranked(lambda t: t.slug not in watched and any(g in top_genres for g in t.genres) and free_ok(t),
                    lambda t: (outlives(t), -sum(g in top_genres for g in t.genres), -pop.get(t.slug, 0.0), t.id))
        if top_genres:
            add_row("because_you_watched", seeds + nb, size)
    add_row("popular_in_dialect", ranked(free_ok, base_key), size)
    if not minimal:
        add_row("new_releases", ranked(lambda t: free_ok(t) and timedelta(0) <= now - t.release <= timedelta(days=cfg.new_release_days),
                                       lambda t: (outlives(t), -t.release.timestamp(), t.id)), size)
        for g in top_genres[:2]:
            add_row("genre", ranked(lambda t, g=g: g in t.genres and free_ok(t), base_key), size)
    else:
        add_row("new_releases", ranked(lambda t: timedelta(0) <= now - t.release <= timedelta(days=cfg.new_release_days),
                                       lambda t: (outlives(t), -t.release.timestamp(), t.id)), size)
    if not rows and hero is None:
        return None
    if free_only:
        notes.append("activation goal for a non-paying viewer: free titles preferred (premium would hit a paywall)")
    return PagePlan(d, hero.id if hero else None, rows, evidence, notes, sorted(used))


# ------------------------------------------------------------------ messages
@dataclass
class MessagePlan:
    channel: str            # push | whatsapp | in_app_message
    send_at: Optional[datetime]
    title_id: str
    payload: dict           # type-specific action body
    rationale: str
    evidence: List[str] = field(default_factory=list)


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


def draft_copy(goal_id: str, t: Title, params: dict) -> dict:
    label = CTYPE_LABEL.get(t.ctype, "title")
    genre = t.genres[0].lower() if t.genres else ""
    if goal_id == "G07" and params.get("next_episode"):
        n = params["next_episode"]
        title = trunc("%s: episode %d is waiting" % (t.name, n), 40)
        body = trunc("Pick up %s from episode %d. Open the app to keep watching." % (t.name, n), 120)
    elif goal_id in ACTIVATION_GOALS:
        title = trunc("Start %s" % t.name, 40)
        body = trunc("A %s %s in %s: %s. Tap to start watching." % (genre, label, t.dialect, t.name), 120)
    else:
        title = trunc("Watch %s" % t.name, 40)
        body = trunc("%s is available now in %s. Tap to watch." % (t.name, t.dialect), 120)
    return {"title": title, "body": body}


def pick_message_title(vf: ViewerFacts, goal_id: str, params: dict, page: Optional[PagePlan], cat: Catalogue,
                       send_at: datetime) -> Optional[Title]:
    cands = []
    if goal_id == "G07" and params.get("title_id"):
        cands.append(cat.titles.get(params["title_id"]))
    if page and page.hero:
        cands.append(cat.titles.get(page.hero))
    for r in (page.rows if page else []):
        cands.extend(cat.titles.get(t) for t in r["title_ids"][:1])
    for t in cands:
        if t and not t.injection_flags and t.dialect == vf.dialect and cat.is_available(t.id, send_at):
            # a title that expires before the send slot (or within 6h after) is a poor promise
            if t.avail_until is None or t.avail_until > send_at + timedelta(hours=6):
                return t
    return None


def plan_message(vf: ViewerFacts, goal_id: str, params: dict, deadline: datetime, page: Optional[PagePlan], cat: Catalogue,
                 cfg: Config, now: datetime, pricing: dict, priors: Optional[dict] = None) -> Tuple[Optional[MessagePlan], str]:
    """Returns (plan, reason). reason explains a refusal; for a plan it explains the choice."""
    f = vf.features
    if goal_id not in cfg.message_goals:
        return None, "goal %s does not warrant leaving the app (home page is proportionate)" % goal_id
    if vf.mode != "full" or f is None:
        return None, "no message while the data store is degraded (opt-outs after the snapshot unverifiable)"
    if vf.dialect_source != "app_setting":
        return None, "dialect not confirmed by the app setting on this trigger (source=%s); messages need certainty" % vf.dialect_source
    if f.messages_last_days(7) >= cfg.max_messages_per_7d:
        return None, "already received %d messages in 7 days (cap %d)" % (f.messages_last_days(7), cfg.max_messages_per_7d)
    owp = f.open_without_play_rate()
    if goal_id != "G04":
        if owp is None and goal_id != "G01":
            return None, "too few past sessions to show that the home page alone fails for this viewer; no nudge yet"
        if owp is not None and owp < cfg.min_open_without_play_for_push:
            return None, "viewer usually plays within the session (open-without-play %.0f%% < %.0f%%); a nudge adds little" % (
                owp * 100, cfg.min_open_without_play_for_push * 100)
    base = now + timedelta(minutes=cfg.message_lead_min)
    hour = f.habitual_hour() or 19.5
    local = base.astimezone(IST)
    cand = at_ist_hour(ist_date(base), hour)
    if cand < base:
        cand = at_ist_hour(ist_date(base) + timedelta(days=1), hour)
    send_at = next_send_window(cand, cfg.send_start_h, cfg.send_end_h)
    if send_at > deadline - timedelta(hours=1):
        send_at = next_send_window(base, cfg.send_start_h, cfg.send_end_h)
        if send_at > deadline - timedelta(hours=1):
            return None, "no allowed send slot before the goal deadline"
    t = pick_message_title(vf, goal_id, params, page, cat, send_at)
    if t is None:
        return None, "no clean, available, in-dialect title for the send slot"
    ev = ["catalogue:%s" % t.id]
    copy = draft_copy(goal_id, t, params)
    why_time = "habitual open hour %.1f IST" % hour if f.habitual_hour() else "default evening slot (no habit evidence)"
    # channel choice: WhatsApp (approved templates are Hindi) > push > in-app
    if vf.can_whatsapp() and vf.language == "hin":
        tmpl, variables = "wa_recommend_title_v1", {"title_name": t.name, "dialect_name": t.dialect}
        if goal_id == "G04" and vf.plan_ends_at and now <= vf.plan_ends_at <= now + timedelta(hours=48):
            plan = next((p for p in pricing["plans"] if p["plan_id"] == "P_MONTH"), None)
            if plan:
                tmpl, variables = "wa_trial_ending_v1", {"plan_price": "₹%d" % plan["price_inr"]}
        return MessagePlan("whatsapp", send_at, t.id, {"type": "whatsapp", "template_id": tmpl, "variables": variables,
                                                       "title_id": t.id}, "WhatsApp consented, Hindi template; " + why_time, ev), "ok"
    if vf.can_push():
        return MessagePlan("push", send_at, t.id, {"type": "push", "title_id": t.id, "text": copy,
                                                   "deeplink": "ott://title/%s" % t.id}, "push consented; " + why_time, ev), "ok"
    return MessagePlan("in_app_message", None, t.id, {"type": "in_app_message", "title_id": t.id, "text": copy,
                                                      "deeplink": "ott://title/%s" % t.id},
                       "no push/WhatsApp consent: in-app message shown at the next open (consent not required)", ev), "ok"
