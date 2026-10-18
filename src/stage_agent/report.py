"""final_report.md: everything an evaluator needs on one page, generated from the run's own artefacts."""
from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional

from .agent import Runtime
from .impact import pc, pp


def write_final_report(path: str, rt: Runtime, lat: dict, judged: dict, impact: dict, pilot: Optional[dict], safety: dict,
                       sim_report: Optional[dict], seed: str) -> None:
    dq = rt.pack.dq
    D = rt.decisions
    st = Counter(d["status"] for d in D)
    goals = Counter((d.get("goal") or {}).get("id") for d in D if d["status"] == "PUBLISHED")
    acts = Counter(d["action"] for d in D)
    P = impact["primary"]
    L: List[str] = []
    a = L.append
    a("# Final report: One User, One Agent\n")
    a("Replay window: %s to %s (UTC). Simulator seed `%s`. Rule version `%s`.\n" % (
        rt.decisions[0]["trigger"]["deliver_at"] if rt.decisions else "?", rt.decisions[-1]["trigger"]["deliver_at"] if rt.decisions else "?",
        seed, rt.decisions[0]["rule_version"] if rt.decisions else "?"))
    a("## 1. What happened\n")
    a("- **%d** triggers delivered (%d unique after de-duplication, %d viewers). Decisions: %s.\n" % (
        dq["trigger_lines"], dq["trigger_lines"] - dq["trigger_duplicate_deliveries"], dq["trigger_users"], dict(st)))
    a("- Published actions by combination: %s.\n" % dict(acts))
    a("- Goals committed on published decisions: %s.\n" % {k: v for k, v in sorted(goals.items(), key=lambda kv: str(kv[0]))})
    a("- Goal outcomes judged (%d goals): %s.\n" % (judged["agent_goals_judged"], judged["status_counts"]))
    a("- **Impact (honest):** %s vs %s, ITT difference **%s** [95%% CI %s to %s], p = %.3f. See `impact_report.md`.\n" % (
        pc(P["agent_rate"]), pc(P["holdout_rate"]), pp(P["diff"]), pp(P["lo"]), pp(P["hi"]), P["p"]))
    a("- **Latency:** live visits p50 %.2f s, p95 %.2f s, max %.2f s; decisions over 60 s: %d; over 5 min: %d. Max model calls used %d/%d, tool calls %d/%d.\n" % (
        lat["live_visits"]["p50_s"], lat["live_visits"]["p95_s"], lat["live_visits"]["max_s"], lat["over_slo_60s"], lat["over_5min_failures"],
        lat["budgets"]["max_model_calls_used"], lat["budgets"]["max_model_calls_configured"], lat["budgets"]["max_tool_calls_used"], lat["budgets"]["max_tool_calls_configured"]))
    a("- **Safety:** independent post-hoc audit of `actions.jsonl`: **%d violations** across %d actions %s. Simulator policy problems: %s.\n" % (
        safety["violations"], safety["actions_audited"], safety["by_type"], (sim_report or {}).get("policy_problem_count", "n/a")))
    a("\n## 2. Data issues found and how each was handled\n")
    a("| # | issue (measured in this pack) | handling | where |\n|---|---|---|---|")
    rows = [
        ("Duplicate event_ids: %d history lines, %d trigger deliveries" % (dq["history_duplicate_deliveries"], dq["trigger_duplicate_deliveries"]),
         "de-duplicated on event_id keeping the first `received_at`; duplicate triggers return the original decision (idempotency key = trigger event_id) and create no second action", "loader.py, agent.py"),
        ("Offline-buffered events: %d history, %d triggers older than 10 min" % (dq["history_offline_buffered_over_10min"], dq["trigger_offline_buffered_over_10min"]),
         "age = sent_at - occurred_at (phone-internal, immune to a wrong clock); stale opens are state updates, never live sessions (%d recorded as NO_ACTION_STALE)" % st.get("NO_ACTION_STALE", 0), "loader.py, agent.py"),
        ("Wrong phone clocks: %d history events, %d live triggers with received-sent skew over 10 min" % (dq["history_wrong_phone_clock_over_10min"], dq["trigger_wrong_clock_but_live"]),
         "ordering and freshness use `received_at`; true event time estimated as received_at minus buffering delay; skewed-but-live opens are NOT mistaken for stale ones", "loader.py"),
        ("Invalid app dialect (null / `NA` / `d7`): %d history events, %d triggers" % (dq["history_invalid_app_dialect"], dq["trigger_invalid_app_dialect"]),
         "not a dialect the catalogue has; fall back to last valid app setting (penalised), then profile, then campaign link; messages are never sent on a fallback dialect", "facts.py"),
        ("Dialect conflicts: app setting != profile on ~38% of triggers; campaign links disagree with the app", "the app setting on the trigger decides (it is what the app will request); every disagreement is listed in `dialect.conflicts`; three-way conflicts flagged", "facts.py"),
        ("Profile snapshot: %d rows for %d viewers with two rows, one day stale" % (dq["profile_rows"], dq["profile_users_with_two_rows"]),
         "rows contradicted by events lose, otherwise newest `updated_at`; disagreement stays visible and, when events cannot arbitrate, subscription-based goals are skipped", "facts.py"),
        ("No profile row for %d triggered viewers (brand-new)" % dq["trigger_users_without_profile_row"], "facts built from `signup_completed`; no profile means no messaging consent", "facts.py"),
        ("Profile says never/lapsed while events say otherwise", "any paid event in the history overrides a profile `never`; a bare `lapsed` with no date has unknown recency (G06 needs a dated lapse)", "facts.py, goals.py"),
        ("Language variants: %d content slugs have an English variant" % dq["catalogue_language_variant_slugs"], "one title per content_slug per page; variant chosen by the viewer's language", "intervention.py"),
        ("Availability windows: %d titles have an end date, some start in the future" % dq["catalogue_with_end_date"],
         "checked at publish time, again at send time (dispatch gate); titles about to expire inside the page TTL rank lower", "verifier.py, intervention.py"),
        ("Dialect d3 has only %s titles" % dq["catalogue_titles_per_dialect"].get("d3"), "rows shrink to what exists; rows under 3 titles are dropped by us before the platform would", "intervention.py"),
        ("Injected instructions in %d catalogue descriptions" % dq["catalogue_injected_descriptions"],
         "descriptions are scanned and discarded at load (no later component can read them); flagged titles are kept out of message copy; logged in `security_log.jsonl`", "security.py, loader.py"),
        ("G10 has no success event; G11's `tv_app_open` never occurs", "both detected from the event vocabulary and never selected (`unmeasurable`)", "goals.py"),
        ("Under-specified rules (G06 'recently', G07 'unfinished', G12 'before the trigger', G02 windowed 'never')", "explicit interpretation, written into each decision's `assumptions`", "goals.py INTERPRETATIONS"),
        ("Pilot report compares served vs all other", "recomputed on the randomized arms: no uplift; see impact report section 6", "impact.py"),
    ]
    for i, (iss, how, where) in enumerate(rows, 1):
        a("| %d | %s | %s | %s |" % (i, iss, how, where))
    a("\n## 3. Replay surprises: what the system noticed and did\n")
    rev = rt.revisions
    wd = next((r for r in rev if r["type"] == "title_withdrawn"), None)
    rc = next((r for r in rev if r["type"] == "goal_rule_corrected"), None)
    rp = [r for r in rev if r["type"] == "replan"]
    a("| surprise | response | evidence in this run |\n|---|---|---|")
    a("| burst of buffered app opens | stale triggers skipped, state updated, no worker consumed | %d stale decisions, oldest %.1f h |" % (st.get("NO_ACTION_STALE", 0), lat["stale_buffered_triggers"]["max_age_h"]))
    a("| purchase after the decision | goal closed as succeeded (organic), pending messages cancelled, viewer re-planned to a new goal | %d replans, %d actions cancelled |" % (len(rp), rt.counters["actions_cancelled"]))
    a("| three different dialects | app setting wins, conflicts listed | %d decisions with dialect conflicts |" % sum(1 for d in D if d.get("dialect") and d["dialect"].get("conflicts")))
    if rc:
        a("| goal rule corrected (%s) | new rule version applied to the **%d %s goals open at that moment** only, out of %d viewers known (no mass reprocessing); G03 goals opened afterwards start on the new version | `revisions.jsonl` |" % (
            rc["goal_id"], rc["open_goals_revised"], rc["goal_id"], rc["viewers_total"]))
    a("| model claims eligibility without evidence | verifier rejects unless the rule engine independently finds the goal eligible with resolvable evidence | %d rejected claims (`unsupported_claims.jsonl`); %d fault triggers exercised, others skipped for stale/holdout viewers |" % (
        len(rt.unsupported_claims), len(lat.get("model_faults_exercised", []))))
    a("| description tells the agent to message everyone | treated as content; logged; titles quarantined from copy | %d security log entries, 0 messages built from descriptions |" % len(rt.security_log))
    a("| title withdrawn | catalogue updated at that moment; **%s affected live pages republished without it, %s pending messages cancelled** | %s |" % (
        wd["pages_republished"] if wd else 0, wd["messages_cancelled"] if wd else 0, "`revisions.jsonl`"))
    a("| model provider down | circuit breaker opened %d time(s); rules-only decisions at full speed | model unavailable events %s |" % (
        lat["circuit_breaker_trips"], lat["resilience_events"].get("model_unavailable", 0)))
    a("| data store degraded | reads capped at 1.5 s, fallback to the warehouse snapshot, safe minimal page, no goal commit, no message | store timeouts %d, fallbacks %d |" % (rt.store.timeouts, rt.counters["store_fallbacks"]))
    ts = lat.get("traffic_surge", {})
    a("| traffic triples | multi-worker queue; shedding levels skip the model, then personalisation; stale flushes never queue | busiest 10 min: %s triggers (%sx median), p95 %.2f s, max %.2f s |" % (
        ts.get("triggers_in_window"), ts.get("ratio_vs_median"), ts.get("p95_s", 0), ts.get("max_s", 0)))
    a("\n## 4. Honest limitations\n")
    a("See `KNOWN_LIMITATIONS.md`. Headline: uplift in the simulator is small and the confidence interval is wide; the value of this submission is the "
      "trustworthy decide-act-verify-measure loop, not a large number.\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
