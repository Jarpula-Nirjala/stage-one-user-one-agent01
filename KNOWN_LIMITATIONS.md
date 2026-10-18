# Known limitations (intentional, with reasons)

1. **Mock model, not a real LLM.** Goal advice and message copy come from a deterministic mock so evaluators need no key.
   The `HttpModel` adapter (Anthropic Messages API over urllib) is written but **has not been exercised against the live
   API**. With a real model, copy would still pass through the same verifier; goal choice stays rule-based.
2. **Goal choice is a fixed priority ladder, not learned.** It is explainable and arm-independent, which the holdout
   comparison needs. It does not estimate per-viewer uplift; a uplift model would need far more data than 1,746 viewers.
3. **Effect size is simulator-bound and small.** The simulator mainly rewards pages (play propensity) and barely rewards
   messages, and purchase goals almost never move. The impact report states the confidence interval and the minimum
   detectable effect; per-goal cells are small (some have fewer than 10 viewers) and should not be read as findings.
4. **Interpretations of under-specified goals are mine** (G06 needs a *dated* lapse within 30 days, so the 570 or so
   viewers whose snapshot only says `lapsed` get no G06, and no lapsed snapshot row in this pack carries a date; G07 requires the series to be in the dialect the viewer will see;
   G12 is evaluated before the triggering open; G02 "never completed" is a 30-day-window claim). All are documented per
   decision and configurable in `Config`.
5. **"Never completed / never played" is windowed** to the 30-day history for older viewers (flagged, penalised).
6. **Messages are scheduled, then re-verified at send time, but the replay cannot show events after its own end.** A message
   scheduled for the next morning is gated only against what was known when the replay stopped.
7. **Trial status rests on the snapshot.** Trial purchases do not exist in the event stream, and `plan_ends_at` is empty for
   trial rows, so a "trial ending" WhatsApp template is only used when an end date is actually known (it never is in this
   pack, so G04 viewers get a title recommendation instead).
8. **A withdrawn title whose page cannot be rebuilt** (nothing valid left) cannot be "unpublished": the simulator has no
   cancel action, so the old page would stay live. The run reports such cases in `revisions.jsonl` (`unreplaceable_pages`).
9. **Latency is partly modelled.** Compute time is measured; model and store latencies are modelled constants (configurable)
   and timeouts are accounted virtually. The queue is a deterministic worker-pool simulation, not real threads.
10. **One decision per visit.** A second trigger in the same session, or a live page that is still valid for the same
    goal and dialect, produces `NO_ACTION` with a reason instead of a new page. This avoids churn but means the page is
    not refreshed mid-session when new events arrive.
11. **`inconclusive` is conservative**: goals whose window is not fully inside the 14-day simulated horizon, or whose only
    matching events are late arrivals from before the goal, are excluded from the impact comparison (symmetrically).
12. **Fault injection for holdout and stale triggers is not exercised by design** (no model call is made for them); the
    run records `fault_not_exercised` on those decisions instead of pretending the fault was handled.
13. **The pack's simulator date is fixed** (`REPLAY_START` in `simulate_outcomes.py`); everything in this repository derives the
    replay day from the triggers, but the shipped simulator would need the evaluators' version for an unseen day.
14. **No UI beyond the static dashboard**, by the brief's own priorities.
