"""Runtime configuration. Every number that shapes behaviour lives here so it can be tuned without code changes."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, Optional

RULE_VERSION = "agent-rules-2026.10.1"

# Priority of each goal when a viewer is eligible for several. Higher wins. The ladder encodes lifecycle stage:
# money-at-risk and activation first, engagement last. See ARCHITECTURE.md ("Goal planner").
DEFAULT_GOAL_WEIGHTS: Dict[str, float] = {
    "G04": 100.0,  # trial -> paid: shortest clock, highest value
    "G01": 92.0,   # brand-new viewer, never played
    "G06": 86.0,   # recently lapsed subscriber
    "G02": 82.0,   # first full episode
    "G12": 78.0,   # back after 14+ days away
    "G07": 75.0,   # resume an unfinished series
    "G03": 70.0,   # second-day return (new, already played)
    "G05": 60.0,   # first paid plan
    "G09": 55.0,   # weekly habit for a rare-watching subscriber
    "G08": 50.0,   # try a new release
}

# Base rates observed in the *randomized holdout arm* of the earlier pilot (no agent action). They are used only as
# priors for confidence text and for "is there headroom" reasoning, never as eligibility evidence.
PILOT_HOLDOUT_BASE_RATES: Dict[str, float] = {"G02": 0.197, "G04": 0.227, "G05": 0.0056, "G06": 0.0287, "G09": 0.673}


@dataclass
class Config:
    # --- budgets (assignment: configurable max 8 model calls / 20 tool calls per decision) ---
    max_model_calls: int = 8
    max_tool_calls: int = 20
    latency_slo_s: float = 60.0
    latency_hard_fail_s: float = 300.0

    # --- model provider ---
    model_provider: str = "mock"          # mock | http
    model_call_policy: str = "always"     # always | ambiguous_only | never
    model_timeout_s: float = 2.5          # client-side timeout; a 45 s provider stall costs us 2.5 s
    model_retries: int = 1                # retries on malformed output (timeouts are never retried)
    model_mock_latency_ms: float = 320.0  # simulated latency of one healthy model call
    breaker_failures: int = 3             # consecutive failures that open the circuit
    breaker_cooldown_s: float = 60.0
    fault_persists_on_retry: bool = False  # injected faults hit the first attempt only unless set

    # --- data store (viewer-state dependency) ---
    store_read_ms: float = 12.0           # simulated healthy read latency
    store_timeout_ms: float = 1500.0
    degraded_store_p95_ms: float = 8000.0
    degraded_store_timeout_rate: float = 0.20

    # --- concurrency / load shedding ---
    workers: int = 8
    shed_wait_s: float = 8.0              # projected queue wait above which optional model calls are skipped
    shed_minimal_wait_s: float = 25.0     # above this only the minimal safe page is produced
    tool_ms: float = 1.5                  # simulated cost of an in-process tool call

    # --- freshness / staleness ---
    stale_trigger_s: float = 600.0        # app_open older than this is a buffered flush, not a live visit
    aging_trigger_s: float = 120.0        # live but not instant: confidence is reduced
    snapshot_fresh_h: float = 36.0        # profile snapshot older than this lowers confidence of status-based goals
    last_setting_max_age_d: float = 14.0  # a "last valid app setting" older than this is only a weak fallback

    # --- goal interpretation (documented assumptions) ---
    recent_lapse_days: int = 30           # G06 "recently lapsed": dated lapse evidence within N days
    new_user_days: int = 7                # G01
    second_day_days: int = 2              # G03
    absence_days: int = 14                # G12
    series_idle_days: int = 3             # G07
    habit_lookback_days: int = 14         # G09
    new_release_days: int = 30            # G08
    allow_windowed_never_claims: bool = True  # accept "no event in the 30-day history" as "never" (flagged, penalised)
    goal_weights: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_GOAL_WEIGHTS))

    # --- home page ---
    page_ttl_min: int = 120
    max_rows: int = 6
    min_row_titles: int = 3
    max_row_titles: int = 10
    target_row_titles: int = 8
    avoid_expiring_within_min: int = 120  # prefer titles that outlive the page's TTL

    # --- messaging proportionality ---
    max_messages_per_7d: int = 2
    message_lead_min: int = 25            # minimum delay before a scheduled message
    message_goals: tuple = ("G04", "G06", "G07", "G01", "G12")
    min_open_without_play_for_push: float = 0.5
    send_start_h: int = 8
    send_end_h: int = 22

    # --- learning ---
    use_priors: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["message_goals"] = list(self.message_goals)
        return d

    @staticmethod
    def from_file(path: Optional[str]) -> "Config":
        cfg = Config()
        if not path:
            return cfg
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        names = {f_.name for f_ in fields(Config)}
        for k, v in raw.items():
            if k not in names:
                raise KeyError("unknown config key: %s" % k)
            if k == "message_goals":
                v = tuple(v)
            setattr(cfg, k, v)
        return cfg
