# Architecture

Design rule: **the core is deterministic and rule-based; the model is an advisor whose every claim is verified.** A
decision is cheap, explainable and replayable; the model reduces uncertainty only where rules cannot (second opinion on
goal choice, message copy) and can never widen what the agent is allowed to do.

```
trigger / live event / control event / send timer
        |
  harness (time-ordered merge, worker-pool queue, load shedding)
        |
  Runtime.on_trigger -- idempotency(event_id) -- stale? -- holdout? -- session served? --+
        |                                                                                |
   fact resolver (precedence + freshness + conflicts)   <-- data store (guarded) / snapshot cache
        |
   goal planner (eligibility per goal from evidence, priority ladder)  <-- model advice (claim, verified)
        |
   intervention planner (home page | one message | nothing)   -->  VERIFIER (independent, can veto)
        |                                                                |
   publish (actions.jsonl) <---------- degradation ladder -------------+
        |
   dispatch gate at send time  /  live-event revisions  /  outcome judge  /  impact analysis
```

## Ownership: one agent, one goal, one plan

`ViewerRecord` (agent.py) is the single owner of a viewer's state: facts, **at most one active goal**
(`ViewerRecord.active_goal`), the plan (pending actions), decisions, actions, outcomes, learnings and a journal. No other
component writes a goal. Everything that changes the situation (a new trigger, a purchase, a rule correction, a
withdrawal, an opt-out) goes through the same `Runtime` methods and lands on that record. Goals are kept while still
valid (no churn), closed on success, deadline or lost eligibility, and re-planned on events, not on a schedule.

## Planning

* **Eligibility is evidence, not opinion** (goals.py). Each goal has a function that reads resolved facts and returns
  `eligible`, the reasons, evidence refs, assumptions and confidence penalties. Rejection reasons for every other goal are
  stored in the decision (`goal_candidates.rejected`).
* **Measurability gate.** A goal whose success event is null (G10) or never occurs in the stream (G11 `tv_app_open`) is
  detected from the event vocabulary and never chosen.
* **Priority ladder** (`config.DEFAULT_GOAL_WEIGHTS`): money-at-risk and activation first (G04 trial->paid, G01 first
  play, G06 recently lapsed, G02 first full episode, G12 return, G07 resume series, G03, then G05, G09, G08). It is
  explainable and, crucially, **independent of the arm**, so holdout viewers get the same counterfactual goal
  (tested by swapping all arms).
* **Under-specified rules** are interpreted explicitly (`goals.INTERPRETATIONS`) and printed in each affected decision's
  `assumptions`. Entry-only conditions (G12's absence, G09's low play count) are not re-checked after commitment.
* **Rule versions.** Success rules are versioned. A `goal_rule_corrected` control event creates v+1, finds open goals
  through an index (`open_by_goal`), updates and re-judges only those, logs `open_goals_revised` vs viewers known.
* **Intervention proportionality.** Every trigger gets a home page (cheap, no policy risk). A message is added only when the
  goal warrants leaving the app, the dialect is confirmed by the app setting, the viewer usually opens without playing,
  the 7-day message budget is not used up and there is an allowed slot before the deadline. Channel order: WhatsApp
  (approved Hindi templates, variables from catalogue/pricing only) > push > in-app. Timing: the viewer's habitual open
  hour, never in quiet hours, one per IST day.

## Source handling (facts.py)

| fact | authoritative | fallback | conflict handling |
|---|---|---|---|
| dialect to show | app setting on the trigger (what the app will request) | last valid setting -> profile -> campaign link | every disagreement listed in `dialect.conflicts`; invalid values (`NA`, `d7`, null) rejected; fallbacks penalise confidence and forbid messages |
| subscription | sub events newer than the profile row | profile row; a `never` contradicted by any paid event loses to the events | `subscription:...` conflicts; duplicate rows that disagree and cannot be arbitrated make status-based goals ineligible |
| signup time | profile (server) | signup event (est. from `received_at`) for viewers with no row | mismatch recorded |
| consent | profile AND no opt-out event ever | none | duplicate rows: all must agree; no row = no consent |
| event time | `received_at` for ordering/freshness; `est = received - (sent - occurred)` for windows | | a wrong phone clock shifts `sent` and `occurred` together, so the difference is robust |

Each fact carries source, observed time, freshness in seconds and a ref (`event:<id>`, `profile:<viewer>`, `catalogue:<id>`,
`goal_rule:<goal>-v<n>`). Freshness feeds confidence: a stale snapshot, a fallback dialect, a trial status that rests on
the snapshot alone, a windowed "never" claim each multiply confidence down and the product is explained in
`confidence_reason`.

## Memory

Per-viewer durable state with provenance (`viewer_state/<viewer>.json`) plus an append-only journal. The runtime keeps an
event list per viewer containing only what has been *observed so far*: live events are applied in delivery order, so a
decision can never see the future. `viewer --at <time>` reconstructs what was known at a moment. Learning is written back
by the judge (per-viewer `learnings` with a `next_step_hint`, and cross-viewer `learned_priors.json` from holdout base
rates) and can be fed to the next run.

## Verification (verifier.py)

Separate module, separate code paths, receives ground truth (catalogue, arms, consent ledger, message ledger, policy
config) and never the planner's own flags. Checks: holdout, dialect, availability at publish **and** send time (incl.
withdrawals), page structure (rows, sizes, types, unique content_slug, no Continue Watching), consent (profile + opt-out
events), quiet hours, frequency cap (counts history deliveries too), template approval and variables, copy length,
forbidden phrases, honest-price check, deeplink format, injected-text quarantine, goal still open at send time. Model
claims are accepted only if the rule engine independently found the goal eligible and its evidence refs resolve; the
claim's *reason text is ignored* ("confirmed in billing" proves nothing). A second, fully independent post-hoc
auditor (`audit.py`) re-checks the final `actions.jsonl` against the raw pack.

## Latency and budgets

`decided_in_ms` = measured compute + modelled I/O (mock model call 320 ms, store read 12 ms; timeouts accounted virtually,
not slept). `published_at = deliver_at + queue_wait + decided_in_ms`. Budgets are configurable (`max_model_calls=8`,
`max_tool_calls=20`); planning tools stop two calls short of the cap so the mandatory verification calls always fit; an
exhausted budget means a smaller action, never a crash. The model has a client timeout (2.5 s), so a 45 s provider stall
costs 2.5 s. The harness simulates a worker pool with per-viewer serialisation: wait > 8 s skips the model, > 25 s
publishes only the minimal safe page, > 30 s drops the decision (recorded) rather than publish late; offline-buffered
flushes never occupy a worker. For real concurrency the same design maps to an actor per viewer on a partitioned queue.

## Failure recovery

| failure | response |
|---|---|
| model 503 / timeout / broken JSON | safe parse, bounded retry for malformed output only, never retry a stall; circuit breaker (3 failures, 60 s cooldown); rules-only decision |
| model claims eligibility with no evidence | verifier rejects, logged to `unsupported_claims.jsonl`, rule-engine goal used |
| data store slow/down | read capped at 1.5 s, fall back to the warehouse snapshot (day-old) + trigger facts: minimal in-dialect page, no goal committed, no message (post-snapshot opt-outs are unverifiable) |
| page fails verification | repair once with a minimal page, else no page |
| message fails verification | message dropped, page kept |
| title withdrawn | catalogue updated at that instant; live pages containing it are republished without it; pending messages cancelled; later plans exclude it |
| purchase after the decision | goal judged, pending messages for it cancelled, viewer re-planned |
| opt-out after scheduling | pending message of that channel cancelled; dispatch gate re-verifies at send time anyway |

## Trust boundaries

* The pack is personal data: processed locally, nothing uploaded. The optional hosted-model adapter sends only a compact
  digest (`ViewerFacts.summary()`), never raw events, and is off by default.
* Free text is content, never instruction: catalogue descriptions are scanned once and **discarded** (the `Title` type has no
  description field), flagged titles are kept out of message copy and logged; search queries are never interpreted;
  deeplinks are matched against one strict pattern; model output is parsed, schema-validated and then treated as a claim.
* Holdout viewers: zero actions, zero model calls. Only a computation-only counterfactual goal is stored for measurement.
* The verifier and the post-hoc auditor never import the planner's decisions as truth.

## Measurement (judge.py, impact.py)

Success counts only if the event's *estimated true time* is inside the goal window (late arrivals that happened earlier
are surfaced as unattributable). `inconclusive` = window not fully observable or only unattributable evidence. Impact is
intention-to-treat on the randomized arms using the same reference goal for both, with Newcombe CIs, permutation p,
precision-weighted pooling across goals, a Bloom/CACE served-viewer estimate, a covariate balance check, several simulator
seeds, the biased served-vs-rest number shown only for contrast, and the pilot report recomputed.

## Scaling to 10 million viewers and 40 dialects

Partition by viewer id (one actor/mailbox per viewer, so state needs no cross-viewer locking); the catalogue and
popularity indexes are read-only per dialect and replicate cheaply; dialects are data, not code (a new dialect needs
catalogue rows). Facts are materialised incrementally from the event stream instead of recomputed from history; the model
is called only for ambiguous viewers (`model_call_policy=ambiguous_only`); messaging goes through one global
frequency-cap service keyed by (viewer, IST day).
