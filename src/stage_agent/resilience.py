"""Latency / failure plumbing: per-decision budgets, virtual time, dependency outage windows, circuit breaker, data store.

Replays run faster than real time, so waits (a 2.5 s model timeout, an 8 s degraded read) are *accounted* in virtual
milliseconds instead of slept. A decision's `decided_in_ms` = real compute time + virtual wait time.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .config import Config
from .timeutil import parse_ts


def h01(*parts) -> float:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


class BudgetExceeded(Exception):
    pass


class StoreUnavailable(Exception):
    pass


class DependencyWindows:
    """Outage / slowness windows declared by control events (`dependency_unavailable`, `dependency_degraded`)."""

    def __init__(self, controls: List[dict]):
        self.windows: List[dict] = []
        for c in controls:
            if c.get("type") in ("dependency_unavailable", "dependency_degraded"):
                self.windows.append({"dependency": c["dependency"], "kind": "unavailable" if c["type"].endswith("unavailable") else "degraded",
                                     "start": parse_ts(c["at"]), "until": parse_ts(c["until"]), "detail": c.get("detail")})

    def state(self, dep: str, at: datetime) -> Optional[str]:
        for w in self.windows:
            if w["dependency"] == dep and w["start"] <= at < w["until"]:
                return w["kind"]
        return None

    def add(self, dep: str, kind: str, start: datetime, until: datetime):
        self.windows.append({"dependency": dep, "kind": kind, "start": start, "until": until, "detail": "injected"})


class DecisionBudget:
    """Counts model/tool calls and virtual latency for ONE decision; raises when a configurable cap is hit."""

    def __init__(self, cfg: Config, trigger_id: str):
        self.cfg, self.trigger_id = cfg, trigger_id
        self.model_calls = 0
        self.tool_calls = 0
        self.virtual_ms = 0.0
        self.tool_log: List[str] = []
        self.events: List[str] = []   # notable resilience events: breaker_open, store_timeout, ...

    def tool(self, name: str, ms: Optional[float] = None, reserve: int = 0):
        """`reserve` keeps room for the mandatory verification calls: planning tools stop `reserve` calls before the cap."""
        if self.tool_calls >= self.cfg.max_tool_calls - reserve:
            raise BudgetExceeded("tool-call budget (%d) exhausted at %s" % (self.cfg.max_tool_calls, name))
        self.tool_calls += 1
        self.tool_log.append(name)
        self.virtual_ms += self.cfg.tool_ms if ms is None else ms

    def model(self):
        if self.model_calls >= self.cfg.max_model_calls:
            raise BudgetExceeded("model-call budget (%d) exhausted" % self.cfg.max_model_calls)
        self.model_calls += 1


class CircuitBreaker:
    def __init__(self, failures: int, cooldown_s: float):
        self.threshold, self.cooldown = failures, cooldown_s
        self.consecutive = 0
        self.open_until: Optional[datetime] = None
        self.trips = 0

    def allow(self, now: datetime) -> bool:
        if self.open_until is None:
            return True
        if now >= self.open_until:
            return True  # half-open: let one probe through
        return False

    def record(self, ok: bool, now: datetime):
        if ok:
            self.consecutive = 0
            self.open_until = None
        else:
            self.consecutive += 1
            if self.consecutive >= self.threshold:
                self.open_until = now + timedelta(seconds=self.cooldown)
                self.trips += 1
                self.consecutive = 0


class DataStoreClient:
    """Front for the viewer data store (durable per-viewer state + event index). Applies the injected degradation: during a
    `dependency_degraded` window reads are slow (p95 ~8 s) and ~20% time out; we cap each read at `store_timeout_ms` and
    raise StoreUnavailable so the caller can fall back to the snapshot cache."""

    def __init__(self, cfg: Config, windows: DependencyWindows):
        self.cfg, self.windows = cfg, windows
        self.timeouts = 0
        self.slow_reads = 0

    def read(self, budget: DecisionBudget, now: datetime, key: str):
        state = self.windows.state("user_data_store", now)
        if state == "unavailable":
            budget.tool("store.read(unavailable)", self.cfg.store_timeout_ms)
            budget.events.append("store_unavailable")
            self.timeouts += 1
            raise StoreUnavailable("store unavailable")
        if state == "degraded":
            u, u2 = h01("store", budget.trigger_id, key, budget.tool_calls), h01("store2", budget.trigger_id, key, budget.tool_calls)
            lat = 150 + self.cfg.degraded_store_p95_ms * (u2 ** 3)
            if u < self.cfg.degraded_store_timeout_rate or lat > self.cfg.store_timeout_ms:
                budget.tool("store.read(timeout)", self.cfg.store_timeout_ms)
                budget.events.append("store_timeout")
                self.timeouts += 1
                raise StoreUnavailable("store read exceeded %d ms" % self.cfg.store_timeout_ms)
            budget.tool("store.read(slow)", lat)
            budget.events.append("store_slow")
            self.slow_reads += 1
            return
        budget.tool("store.read", self.cfg.store_read_ms)
