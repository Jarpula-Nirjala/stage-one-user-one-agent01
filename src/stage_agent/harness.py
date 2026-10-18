"""Replay harness: feeds triggers, live events, control events and send timers to the runtime in time order, simulates the
worker pool (queueing, load shedding) and writes the audit artefacts."""
from __future__ import annotations

import heapq
import json
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .agent import Runtime
from .loader import Ev, PackData
from .timeutil import iso, parse_ts


@dataclass
class ReplayResult:
    runtime: Runtime
    queue: Dict[str, float] = field(default_factory=dict)
    wall_s: float = 0.0
    replay_start: Optional[datetime] = None
    replay_end: Optional[datetime] = None


def run_replay(rt: Runtime, progress: bool = False, surge_factor: int = 1) -> ReplayResult:
    pack, cfg = rt.pack, rt.cfg
    items = []
    seq = 0
    for c in pack.controls:
        at = parse_ts(c["at"])
        items.append((at, 0, seq, "control", c))
        seq += 1
    for e in pack.live:
        items.append((e.deliver, 1, seq, "live", e))
        seq += 1
    for e in pack.triggers:
        items.append((e.deliver, 3, seq, "trigger", e))
        seq += 1
    heapq.heapify(items)
    workers = [0.0] * cfg.workers
    heapq.heapify(workers)
    busy: Dict[str, float] = {}
    qwaits: List[float] = []
    shed_counts: Counter = Counter()
    last_breaker_trips = 0
    import time as _t
    t0 = _t.perf_counter()
    first = items[0][0] if items else None
    last = None
    n = 0
    while items:
        at, prio, _, kind, payload = heapq.heappop(items)
        last = at
        if kind == "control":
            rec = rt.on_control(payload, at)
            if payload["type"].startswith("dependency"):
                rt.resilience_log.append({"at": payload["at"], "event": payload["type"], "dependency": payload.get("dependency"),
                                          "until": payload.get("until"), "detail": payload.get("detail")})
        elif kind == "live":
            rt.on_live_event(payload)
        elif kind == "trigger":
            n += 1
            ep = at.timestamp()
            wait, shed = 0.0, 0
            if payload.buffered_s <= cfg.stale_trigger_s:   # stale flushes are bookkeeping only; they never occupy a worker
                free = heapq.heappop(workers)
                start = max(ep, free, busy.get(payload.user, 0.0))
                wait = start - ep
                shed = 3 if wait > cfg.latency_slo_s * 0.5 else 2 if wait > cfg.shed_minimal_wait_s else 1 if wait > cfg.shed_wait_s else 0
                if shed:
                    shed_counts[shed] += 1
            dec = rt.on_trigger(payload, wait, shed)
            if payload.buffered_s <= cfg.stale_trigger_s:
                service = dec["decided_in_ms"] / 1000.0
                heapq.heappush(workers, start + service)
                busy[payload.user] = start + service
                qwaits.append(wait)
            if rt.breaker_trips_changed(last_breaker_trips):
                last_breaker_trips = rt.gateway.breaker.trips
                rt.resilience_log.append({"at": iso(at), "event": "model_circuit_breaker_opened", "trips": last_breaker_trips})
            for aid in dec.get("actions", []):
                a = rt.ledger[aid]
                if a["status"] == "SCHEDULED" and a["type"] in ("push", "whatsapp"):
                    heapq.heappush(items, (a["send_dt"], 2, seq, "dispatch", aid))
                    seq += 1
        elif kind == "dispatch":
            res = rt.dispatch(payload, at)
            rt.counters["dispatch_" + res] += 1
        if progress and n and n % 500 == 0 and kind == "trigger":
            print("  ... %d triggers" % n, flush=True)
    res = ReplayResult(rt, {"p50_wait_s": statistics.median(qwaits) if qwaits else 0.0, "max_wait_s": max(qwaits) if qwaits else 0.0,
                            "shed_level_1": shed_counts[1], "shed_level_2": shed_counts[2], "dropped_overload": shed_counts[3]},
                       _t.perf_counter() - t0, first, last)
    return res


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def pct(vals: List[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def latency_report(rt: Runtime, res: ReplayResult) -> dict:
    cfg = rt.cfg
    decs = [d for d in rt.decisions if d.get("latency_s") is not None]
    lat = [d["latency_s"] for d in decs]
    live = [d for d in decs if d["status"] not in ("NO_ACTION_STALE", "DUPLICATE_SUPPRESSED")]
    live_lat = [d["latency_s"] for d in live]
    pub = [d for d in decs if d["status"] == "PUBLISHED"]
    pub_lat = [d["latency_s"] for d in pub]

    def window(lo: str, hi: str):
        a, b = parse_ts(lo), parse_ts(hi)
        sel = [d for d in decs if a <= parse_ts(d["trigger"]["deliver_at"]) < b]
        return sel

    def summarise(sel):
        l = [d["latency_s"] for d in sel]
        return {"decisions": len(sel), "p50_s": round(pct(l, .5), 3), "p95_s": round(pct(l, .95), 3), "max_s": round(max(l), 3) if l else 0,
                "fallbacks": sum(1 for d in sel if d.get("fallback_used")),
                "published": sum(1 for d in sel if d["status"] == "PUBLISHED")}

    phases = {}
    for w in rt.windows.windows:
        phases["%s_%s_%s" % (w["dependency"], w["kind"], iso(w["start"]))] = {
            "window": [iso(w["start"]), iso(w["until"])], "detail": w.get("detail"), **summarise(window(iso(w["start"]), iso(w["until"])))}
    # traffic surge: busiest 10-minute window of live (non-stale) triggers vs the median 10-minute window
    live_t = sorted(parse_ts(d["trigger"]["deliver_at"]) for d in live)
    surge = {}
    if live_t:
        best, best_i, j = 0, 0, 0
        for i in range(len(live_t)):
            while live_t[i] - live_t[j] > timedelta(minutes=10):
                j += 1
            if i - j + 1 > best:
                best, best_i = i - j + 1, j
        buckets = Counter(int((t - live_t[0]).total_seconds() // 600) for t in live_t)
        med = statistics.median(buckets.values()) if buckets else 0
        a = live_t[best_i]
        surge = {"busiest_10min_window_start": iso(a), "triggers_in_window": best, "median_10min_triggers": med,
                 "ratio_vs_median": round(best / med, 2) if med else None, **summarise(window(iso(a), iso(a + timedelta(minutes=10))))}
    stale = [d for d in decs if d["status"] == "NO_ACTION_STALE"]
    rep = {
        "slo_seconds": cfg.latency_slo_s, "hard_fail_seconds": cfg.latency_hard_fail_s,
        "decisions_total": len(decs), "triggers_unique": rt.counters["triggers"] - rt.counters["duplicate_triggers"],
        "all": {"p50_s": round(pct(lat, .5), 3), "p95_s": round(pct(lat, .95), 3), "p99_s": round(pct(lat, .99), 3), "max_s": round(max(lat), 3) if lat else 0},
        "live_visits": {"count": len(live), "p50_s": round(pct(live_lat, .5), 3), "p95_s": round(pct(live_lat, .95), 3),
                        "p99_s": round(pct(live_lat, .99), 3), "max_s": round(max(live_lat), 3) if live_lat else 0},
        "published_decisions": {"count": len(pub), "p50_s": round(pct(pub_lat, .5), 3), "p95_s": round(pct(pub_lat, .95), 3), "max_s": round(max(pub_lat), 3) if pub_lat else 0},
        "over_slo_60s": sum(1 for x in lat if x > cfg.latency_slo_s), "over_5min_failures": sum(1 for x in lat if x > cfg.latency_hard_fail_s),
        "published_before_home_view_window": {"note": "the simulator shows a page if it is published within 3-40 s of the app_open",
                                              "published_within_3s": sum(1 for x in pub_lat if x <= 3), "published_within_40s": sum(1 for x in pub_lat if x <= 40),
                                              "total_published": len(pub_lat)},
        "budgets": {"max_model_calls_configured": cfg.max_model_calls, "max_tool_calls_configured": cfg.max_tool_calls,
                    "max_model_calls_used": max((d["model_calls"] for d in decs), default=0),
                    "max_tool_calls_used": max((d["tool_calls"] for d in decs), default=0),
                    "decisions_over_budget": sum(1 for d in decs if d["model_calls"] > cfg.max_model_calls or d["tool_calls"] > cfg.max_tool_calls)},
        "queue": res.queue, "workers": cfg.workers,
        "stale_buffered_triggers": {"count": len(stale), "max_age_h": round(max((d["trigger"]["event_age_s"] for d in stale), default=0) / 3600, 1)},
        "fallbacks": dict(Counter(f.split("(")[0].split("->")[0] for d in decs for f in d.get("fallback_used", []))),
        "resilience_events": dict(Counter(e for d in decs for e in d.get("resilience_events", []))),
        "model_gateway": rt.gateway.stats, "circuit_breaker_trips": rt.gateway.breaker.trips,
        "data_store": {"timeouts": rt.store.timeouts, "slow_reads": rt.store.slow_reads},
        "phases": phases, "traffic_surge": surge,
        "model_faults_exercised": rt.provider.exercised if hasattr(rt.provider, "exercised") else [],
        "latency_model_note": "decided_in_ms = measured real compute + modelled I/O (mock model call %d ms, store read %d ms, timeouts accounted "
                              "virtually); published_at = deliver_at + queue wait + decided_in_ms" % (cfg.model_mock_latency_ms, cfg.store_read_ms),
        "replay_wall_clock_s": round(res.wall_s, 1),
    }
    return rep


def write_outputs(rt: Runtime, res: ReplayResult, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    w = lambda name, rows: _write_jsonl(os.path.join(out_dir, name), rows)
    decisions = [_clean(d) for d in rt.decisions]
    w("decisions.jsonl", decisions)
    w("actions.jsonl", rt.effective_actions())
    ledger = []
    for a in sorted(rt.ledger.values(), key=lambda a: (a["published_at"], a["action_id"])):
        ledger.append({k: v for k, v in a.items() if not k.startswith("_") and k != "send_dt"})
    w("action_ledger.jsonl", ledger)
    w("no_action.jsonl", [{"decision_id": d["decision_id"], "viewer_id": d["viewer_id"], "arm": d.get("arm"), "status": d["status"],
                           "trigger_event_id": d["trigger"]["event_id"], "reason": d.get("no_action_reason"),
                           "goal": (d.get("goal") or {}).get("id"), "evidence": d.get("evidence")}
                          for d in decisions if d["action"] == "NO_ACTION"])
    w("revisions.jsonl", rt.revisions)
    w("security_log.jsonl", rt.security_log)
    w("unsupported_claims.jsonl", rt.unsupported_claims)
    w("resilience_log.jsonl", rt.resilience_log)
    vs = os.path.join(out_dir, "viewer_state")
    os.makedirs(vs, exist_ok=True)
    index = {}
    for u, v in sorted(rt.viewers.items()):
        snap = rt.snapshot(u)
        with open(os.path.join(vs, u + ".json"), "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
        index[u] = {"arm": v.arm, "analysis_goal": (v.analysis_goal or {}).get("goal_id"), "decisions": len(v.decision_ids), "actions": len(v.action_ids)}
    with open(os.path.join(vs, "_index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1)
    with open(os.path.join(out_dir, "latency_report.json"), "w", encoding="utf-8") as f:
        json.dump(latency_report(rt, res), f, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, "data_quality.json"), "w", encoding="utf-8") as f:
        json.dump(rt.pack.dq, f, indent=2)
    with open(os.path.join(out_dir, "run_config.json"), "w", encoding="utf-8") as f:
        json.dump({"config": rt.cfg.to_dict(), "goal_rule_log": rt.reg.rule_log, "counters": dict(rt.counters),
                   "replay_window": [iso(res.replay_start), iso(res.replay_end)]}, f, indent=2)


def _write_jsonl(path: str, rows) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
