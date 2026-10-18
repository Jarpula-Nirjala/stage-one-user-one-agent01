"""Pack loader: parsing, de-duplication, clock normalisation and a data-quality ledger of every trap found."""
from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from . import security
from .timeutil import IST, UTC, parse_ts

VALID_DIALECTS = ("d1", "d2", "d3", "d4", "d5", "d6")


class Ev:
    """One normalised event. `recv` is trustworthy; `est` is our best estimate of when it truly happened."""
    __slots__ = ("id", "user", "kind", "occurred", "sent", "recv", "est", "buffered_s", "skew_s", "session",
                 "dialect", "lang", "platform", "props", "deliver", "dup_count")

    def __init__(self, raw: dict):
        self.id = raw["event_id"]
        self.user = raw["user_id"]
        self.kind = raw["event"]
        self.occurred = parse_ts(raw.get("occurred_at"))
        self.sent = parse_ts(raw.get("sent_at"))
        self.recv = parse_ts(raw["received_at"])
        self.session = raw.get("session_id")
        self.dialect = raw.get("app_dialect")
        self.lang = raw.get("app_language")
        self.platform = raw.get("platform")
        self.props = raw.get("properties") or {}
        self.deliver = parse_ts(raw["deliver_at"]) if raw.get("deliver_at") else self.recv
        self.dup_count = 0
        # Phone-internal buffering delay: occurred_at and sent_at come from the same (possibly wrong) clock, so their
        # difference is robust to a wrong clock. received_at - sent_at is NOT (it carries the clock error).
        if self.occurred and self.sent:
            self.buffered_s = max(0.0, (self.sent - self.occurred).total_seconds())
            self.skew_s = (self.recv - self.sent).total_seconds()
        else:
            self.buffered_s, self.skew_s = 0.0, 0.0
        self.est = self.recv - timedelta(seconds=self.buffered_s)

    @property
    def age_s(self) -> float:
        """Age of the happening at the moment the server received it."""
        return self.buffered_s

    def to_raw(self) -> dict:
        return {"event_id": self.id, "user_id": self.user, "event": self.kind, "app_dialect": self.dialect,
                "app_language": self.lang, "session_id": self.session, "properties": self.props}


@dataclass
class Title:
    id: str
    slug: str
    name: str
    dialect: str
    lang: str
    ctype: str
    genres: tuple
    release: datetime
    avail_from: datetime
    avail_until: Optional[datetime]
    premium: bool
    injection_flags: tuple = ()
    desc_len: int = 0  # the description itself is deliberately NOT retained


class Catalogue:
    def __init__(self, titles: Iterable[Title], episodes: Dict[str, List[dict]]):
        self.titles: Dict[str, Title] = {t.id: t for t in titles}
        self.by_slug: Dict[str, List[Title]] = defaultdict(list)
        self.by_dialect: Dict[str, List[Title]] = defaultdict(list)
        for t in self.titles.values():
            self.by_slug[t.slug].append(t)
            self.by_dialect[t.dialect].append(t)
        self.episodes = episodes  # slug -> sorted [{episode_id, season, episode, minutes}]
        self.withdrawn: Dict[str, datetime] = {}   # slug -> time withdrawn (dynamic, from control events)
        self.popularity: Dict[str, float] = {}
        self.dialects = sorted(self.by_dialect)

    def withdraw(self, title_id: str, at: datetime) -> List[str]:
        t = self.titles.get(title_id)
        if not t:
            return []
        self.withdrawn[t.slug] = at   # licence is per content, so every language variant goes with it
        return [x.id for x in self.by_slug[t.slug]]

    def is_available(self, title_id: str, at: datetime) -> bool:
        t = self.titles.get(title_id)
        if t is None or t.avail_from > at:
            return False
        if t.avail_until is not None and at >= t.avail_until:
            return False
        w = self.withdrawn.get(t.slug)
        return not (w is not None and at >= w)

    def episode_count(self, slug: str) -> int:
        return len(self.episodes.get(slug, []))

    def next_episode(self, slug: str, watched_ids: set):
        for e in self.episodes.get(slug, []):
            if e["episode_id"] not in watched_ids:
                return e
        return None

    def minutes(self, slug: str) -> float:
        eps = self.episodes.get(slug)
        return eps[0]["minutes"] if eps else 0.0

    def variant_for(self, slug: str, lang: Optional[str], at: datetime) -> Optional[Title]:
        """Language variants share a content_slug: pick the available one matching the viewer's language."""
        vs = [t for t in self.by_slug.get(slug, []) if self.is_available(t.id, at)]
        if not vs:
            return None
        vs.sort(key=lambda t: (t.lang != lang, t.id))
        return vs[0]


@dataclass
class PackData:
    root: str = ""
    events: Dict[str, List[Ev]] = field(default_factory=dict)       # user -> events sorted by recv (deduped)
    profiles: Dict[str, List[dict]] = field(default_factory=dict)   # user -> raw rows (can be >1)
    catalogue: Optional[Catalogue] = None
    goals: dict = field(default_factory=dict)
    interventions: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)
    arms: Dict[str, str] = field(default_factory=dict)
    triggers: List[Ev] = field(default_factory=list)
    live: List[Ev] = field(default_factory=list)
    controls: List[dict] = field(default_factory=list)
    faults: Dict[str, dict] = field(default_factory=dict)           # trigger_event_id -> fault
    snapshot_at: Optional[datetime] = None
    event_vocab: Counter = field(default_factory=Counter)
    dq: Dict[str, Any] = field(default_factory=dict)                # data-quality ledger
    injection_log: List[dict] = field(default_factory=list)


def _jsonl(path: str):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _csv(path: str):
    with open(path, encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def build_catalogue(rows: Iterable[dict], episodes_rows: Iterable[dict], dq: Dict[str, Any],
                    injection_log: List[dict]) -> Catalogue:
    titles = []
    for r in rows:
        flags = tuple(security.scan(r.get("description", "")) + security.scan(r.get("title", "")))
        if flags:
            injection_log.append({"source": "catalogue.description", "title_id": r["title_id"], "patterns": sorted(set(flags)),
                                  "excerpt": security.sanitize_for_log(r.get("description", "")), "handling":
                                  "description discarded; title excluded from message copy; never executed"})
        rel = parse_ts(r["release_date"] + "T00:00:00+05:30") if r.get("release_date") else parse_ts(r["available_from"])
        titles.append(Title(r["title_id"], r["content_slug"], r["title"], r["dialect"], r["language"], r["content_type"],
                            tuple(g for g in r["genres"].split("|") if g), rel, parse_ts(r["available_from"]),
                            parse_ts(r["available_until"]) if r.get("available_until") else None,
                            str(r["is_premium"]).lower() == "true", tuple(sorted(set(flags))), len(r.get("description", ""))))
    eps: Dict[str, List[dict]] = defaultdict(list)
    for r in episodes_rows:
        eps[r["content_slug"]].append({"episode_id": r["episode_id"], "season": int(r["season"]),
                                       "episode": int(r["episode"]), "minutes": float(r["minutes"])})
    for v in eps.values():
        v.sort(key=lambda e: (e["season"], e["episode"]))
    cat = Catalogue(titles, eps)
    dq["catalogue_titles"] = len(cat.titles)
    dq["catalogue_language_variant_slugs"] = sum(1 for v in cat.by_slug.values() if len(v) > 1)
    dq["catalogue_injected_descriptions"] = sum(1 for t in cat.titles.values() if t.injection_flags)
    dq["catalogue_titles_per_dialect"] = {d: len(v) for d, v in sorted(cat.by_dialect.items())}
    dq["catalogue_with_end_date"] = sum(1 for t in cat.titles.values() if t.avail_until)
    return cat


def normalise_events(raw_iter: Iterable[dict], dq: Dict[str, Any], prefix: str) -> Dict[str, List[Ev]]:
    seen: Dict[str, Ev] = {}
    total = dup = 0
    for raw in raw_iter:
        total += 1
        e = Ev(raw)
        old = seen.get(e.id)
        if old is not None:
            dup += 1
            old.dup_count += 1
            if e.recv < old.recv:  # keep the first delivery
                e.dup_count = old.dup_count
                seen[e.id] = e
            continue
        seen[e.id] = e
    by_user: Dict[str, List[Ev]] = defaultdict(list)
    buffered = skew = bad_dialect = 0
    for e in seen.values():
        by_user[e.user].append(e)
        buffered += e.buffered_s > 600
        skew += abs(e.skew_s) > 600 and e.buffered_s <= 600
        bad_dialect += e.dialect not in VALID_DIALECTS
    for v in by_user.values():
        v.sort(key=lambda e: (e.recv, e.id))
    dq[prefix + "_lines"] = total
    dq[prefix + "_duplicate_deliveries"] = dup
    dq[prefix + "_unique_events"] = len(seen)
    dq[prefix + "_offline_buffered_over_10min"] = buffered
    dq[prefix + "_wrong_phone_clock_over_10min"] = skew
    dq[prefix + "_invalid_app_dialect"] = bad_dialect
    return by_user


def load_pack(root: str, quiet: bool = False) -> PackData:
    p = PackData(root=root)
    j = lambda *a: os.path.join(root, *a)

    p.goals = json.load(open(j("config", "goals.json"), encoding="utf-8"))
    p.interventions = json.load(open(j("config", "interventions.json"), encoding="utf-8"))
    p.pricing = json.load(open(j("config", "pricing.json"), encoding="utf-8"))
    p.catalogue = build_catalogue(_csv(j("data", "catalogue.csv")), _csv(j("data", "episodes.csv")), p.dq, p.injection_log)

    prof: Dict[str, List[dict]] = defaultdict(list)
    for r in _csv(j("data", "profiles_snapshot.csv")):
        prof[r["user_id"]].append(r)
    p.profiles = dict(prof)
    p.dq["profile_rows"] = sum(len(v) for v in prof.values())
    p.dq["profile_users_with_two_rows"] = sum(1 for v in prof.values() if len(v) > 1)
    snaps = [parse_ts(r["snapshot_at"]) for v in prof.values() for r in v if r.get("snapshot_at")]
    p.snapshot_at = max(snaps) if snaps else None

    p.events = normalise_events(_jsonl(j("data", "events.jsonl")), p.dq, "history")
    for evs in p.events.values():
        for e in evs:
            p.event_vocab[e.kind] += 1

    trig_raw = list(_jsonl(j("replay", "triggers.jsonl")))
    p.triggers = [Ev(r) for r in trig_raw]
    p.triggers.sort(key=lambda e: (e.deliver, e.id))
    c = Counter(e.id for e in p.triggers)
    p.dq["trigger_lines"] = len(p.triggers)
    p.dq["trigger_duplicate_deliveries"] = sum(v - 1 for v in c.values() if v > 1)
    p.dq["trigger_offline_buffered_over_10min"] = sum(1 for e in p.triggers if e.buffered_s > 600)
    p.dq["trigger_wrong_clock_but_live"] = sum(1 for e in p.triggers if abs(e.skew_s) > 600 and e.buffered_s <= 600)
    p.dq["trigger_invalid_app_dialect"] = sum(1 for e in p.triggers if e.dialect not in VALID_DIALECTS)
    p.dq["trigger_users"] = len({e.user for e in p.triggers})
    p.dq["trigger_users_without_profile_row"] = len({e.user for e in p.triggers if e.user not in p.profiles})

    lv = j("replay", "live_events.jsonl")
    p.live = sorted((Ev(r) for r in _jsonl(lv)), key=lambda e: (e.deliver, e.id)) if os.path.exists(lv) else []
    for e in p.live:
        p.event_vocab[e.kind] += 1
    cf = j("replay", "control_events.jsonl")
    p.controls = sorted(_jsonl(cf), key=lambda c: c["at"]) if os.path.exists(cf) else []
    ff = j("replay", "model_faults.jsonl")
    p.faults = {r["trigger_event_id"]: r for r in _jsonl(ff)} if os.path.exists(ff) else {}
    p.arms = {r["user_id"]: r["arm"] for r in _csv(j("replay", "arms.csv"))}
    p.dq["arms"] = dict(Counter(p.arms.values()))
    return p
