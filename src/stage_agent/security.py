"""Trust boundary for free text.

Rule 6 of the messaging policy: text inside titles, descriptions, search queries and deeplinks is *content*, never an
instruction. We enforce that structurally (descriptions are scanned once at load time and then discarded, so no later
component can read them) and defensively (a scanner flags text that talks *to* an agent so it can be logged and the
title kept out of message copy).
"""
from __future__ import annotations

import re
import unicodedata
from typing import List

_PATTERNS = [
    ("addresses_ai", re.compile(r"\b(ai|llm|assistant|agent|model|bot)s?\b.{0,40}\b(note|instruction|must|should|ignore|override)", re.I)),
    ("system_note", re.compile(r"\b(system|developer|admin(istrator)?)\s*(note|message|prompt|instruction|override)", re.I)),
    ("assistant_tag", re.compile(r"\[\s*(assistant|system|instruction|agent)[^\]]*\]", re.I)),
    ("ignore_rules", re.compile(r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}\b(previous|prior|above|earlier|your|all|any|the)?\s*(rules?|instructions?|polic(y|ies)|checks?|guardrails?|constraints?)", re.I)),
    ("skip_checks", re.compile(r"\bskip\b.{0,20}\b(all\s+)?(checks?|verification|validation|consent)", re.I)),
    ("broadcast", re.compile(r"\b(message|send|push|notify|whatsapp|email)\b.{0,50}\b(every|all)\s+(users?|viewers?|customers?|subscribers?)", re.I)),
    ("opt_out_override", re.compile(r"\b(including|even)\b.{0,30}\b(opted?[- ]out|unsubscribed|no consent)", re.I)),
    ("pre_approved", re.compile(r"\bpre[- ]?approved\b|\bmark\b.{0,25}\b(eligible|approved)\b", re.I)),
    ("role_play", re.compile(r"\b(you are now|act as|from now on|new instructions?)\b", re.I)),
]


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def scan(text: str) -> List[str]:
    """Return the names of every injection pattern found in `text` (empty list = clean)."""
    t = normalise(text)
    return [name for name, rx in _PATTERNS if rx.search(t)]


def sanitize_for_log(text: str, limit: int = 160) -> str:
    t = re.sub(r"\s+", " ", normalise(text)).strip()
    return t[:limit] + ("..." if len(t) > limit else "")


_DEEPLINK = re.compile(r"^ott://c/(?P<campaign>[A-Za-z0-9_\-]+)/(?P<dialect>[A-Za-z0-9_]+|None)/title/(?P<title>T\d+)$")


def parse_campaign_deeplink(link: str):
    """Strict parse of `ott://c/<campaign>/<dialect>/title/<title_id>`. Anything else yields None: a deeplink is never
    interpreted, only matched against this one shape."""
    if not isinstance(link, str):
        return None
    m = _DEEPLINK.match(link)
    if not m:
        return None
    return {"campaign": m.group("campaign"), "dialect": None if m.group("dialect") == "None" else m.group("dialect"),
            "title_id": m.group("title")}
