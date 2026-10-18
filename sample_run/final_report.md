# Final report: One User, One Agent

Replay window: 2026-09-30T18:32:59.458Z to 2026-10-01T18:31:25.944Z (UTC). Simulator seed `oua-practice`. Rule version `agent-rules-2026.10.1`.

## 1. What happened

- **2769** triggers delivered (2679 unique after de-duplication, 1746 viewers). Decisions: {'PUBLISHED': 927, 'HOLDOUT_NO_ACTION': 1092, 'NO_ACTION_ABSTAIN': 39, 'NO_ACTION_STALE': 424, 'DUPLICATE_SUPPRESSED': 90, 'NO_ACTION_SESSION_DUP': 103, 'NO_ACTION': 94, 'REPLANNED': 3}.

- Published actions by combination: {'HOME_PAGE+PUSH': 147, 'HOME_PAGE+WHATSAPP': 72, 'NO_ACTION': 1845, 'HOME_PAGE': 641, 'HOME_PAGE+IN_APP_MESSAGE': 66, 'PUSH': 1}.

- Goals committed on published decisions: {'G01': 59, 'G02': 254, 'G03': 1, 'G04': 1, 'G05': 49, 'G06': 64, 'G07': 424, 'G08': 51, 'G09': 15, 'G12': 6, None: 3}.

- Goal outcomes judged (730 goals): {'failed': 636, 'succeeded': 93, 'inconclusive': 1}.

- **Impact (honest):** 12.2% vs 11.4%, ITT difference **+0.8 pp** [95% CI -2.5 pp to +4.0 pp], p = 0.692. See `impact_report.md`.

- **Latency:** live visits p50 0.00 s, p95 0.35 s, max 1.80 s; decisions over 60 s: 0; over 5 min: 0. Max model calls used 2/8, tool calls 9/20.

- **Safety:** independent post-hoc audit of `actions.jsonl`: **0 violations** across 1227 actions {'home_page': 944, 'push': 146, 'whatsapp': 71, 'in_app_message': 66}. Simulator policy problems: 0.


## 2. Data issues found and how each was handled

| # | issue (measured in this pack) | handling | where |
|---|---|---|---|
| 1 | Duplicate event_ids: 2176 history lines, 90 trigger deliveries | de-duplicated on event_id keeping the first `received_at`; duplicate triggers return the original decision (idempotency key = trigger event_id) and create no second action | loader.py, agent.py |
| 2 | Offline-buffered events: 15402 history, 432 triggers older than 10 min | age = sent_at - occurred_at (phone-internal, immune to a wrong clock); stale opens are state updates, never live sessions (424 recorded as NO_ACTION_STALE) | loader.py, agent.py |
| 3 | Wrong phone clocks: 3802 history events, 61 live triggers with received-sent skew over 10 min | ordering and freshness use `received_at`; true event time estimated as received_at minus buffering delay; skewed-but-live opens are NOT mistaken for stale ones | loader.py |
| 4 | Invalid app dialect (null / `NA` / `d7`): 7196 history events, 185 triggers | not a dialect the catalogue has; fall back to last valid app setting (penalised), then profile, then campaign link; messages are never sent on a fallback dialect | facts.py |
| 5 | Dialect conflicts: app setting != profile on ~38% of triggers; campaign links disagree with the app | the app setting on the trigger decides (it is what the app will request); every disagreement is listed in `dialect.conflicts`; three-way conflicts flagged | facts.py |
| 6 | Profile snapshot: 4888 rows for 30 viewers with two rows, one day stale | rows contradicted by events lose, otherwise newest `updated_at`; disagreement stays visible and, when events cannot arbitrate, subscription-based goals are skipped | facts.py |
| 7 | No profile row for 103 triggered viewers (brand-new) | facts built from `signup_completed`; no profile means no messaging consent | facts.py |
| 8 | Profile says never/lapsed while events say otherwise | any paid event in the history overrides a profile `never`; a bare `lapsed` with no date has unknown recency (G06 needs a dated lapse) | facts.py, goals.py |
| 9 | Language variants: 514 content slugs have an English variant | one title per content_slug per page; variant chosen by the viewer's language | intervention.py |
| 10 | Availability windows: 103 titles have an end date, some start in the future | checked at publish time, again at send time (dispatch gate); titles about to expire inside the page TTL rank lower | verifier.py, intervention.py |
| 11 | Dialect d3 has only 16 titles | rows shrink to what exists; rows under 3 titles are dropped by us before the platform would | intervention.py |
| 12 | Injected instructions in 2 catalogue descriptions | descriptions are scanned and discarded at load (no later component can read them); flagged titles are kept out of message copy; logged in `security_log.jsonl` | security.py, loader.py |
| 13 | G10 has no success event; G11's `tv_app_open` never occurs | both detected from the event vocabulary and never selected (`unmeasurable`) | goals.py |
| 14 | Under-specified rules (G06 'recently', G07 'unfinished', G12 'before the trigger', G02 windowed 'never') | explicit interpretation, written into each decision's `assumptions` | goals.py INTERPRETATIONS |
| 15 | Pilot report compares served vs all other | recomputed on the randomized arms: no uplift; see impact report section 6 | impact.py |

## 3. Replay surprises: what the system noticed and did

| surprise | response | evidence in this run |
|---|---|---|
| burst of buffered app opens | stale triggers skipped, state updated, no worker consumed | 424 stale decisions, oldest 212.2 h |
| purchase after the decision | goal closed as succeeded (organic), pending messages cancelled, viewer re-planned to a new goal | 3 replans, 3 actions cancelled |
| three different dialects | app setting wins, conflicts listed | 380 decisions with dialect conflicts |
| goal rule corrected (G03) | new rule version applied to the **0 G03 goals open at that moment** (one G03 goal was committed later, on the corrected version), out of 898 viewers known (no mass reprocessing) | `revisions.jsonl` |
| model claims eligibility without evidence | verifier rejects unless the rule engine independently finds the goal eligible with resolvable evidence | 1 rejected claims (`unsupported_claims.jsonl`); 2 fault triggers exercised, others skipped for stale/holdout viewers |
| description tells the agent to message everyone | treated as content; logged; titles quarantined from copy | 2 security log entries, 0 messages built from descriptions |
| title withdrawn | catalogue updated at that moment; **18 affected live pages republished without it, 1 pending messages cancelled** | `revisions.jsonl` |
| model provider down | circuit breaker opened 2 time(s); rules-only decisions at full speed | model unavailable events 6 |
| data store degraded | reads capped at 1.5 s, fallback to the warehouse snapshot, safe minimal page, no goal commit, no message | store timeouts 2, fallbacks 2 |
| traffic triples | multi-worker queue; shedding levels skip the model, then personalisation; stale flushes never queue | busiest 10 min: 161 triggers (10.73x median), p95 0.35 s, max 0.35 s |

## 4. Honest limitations

See `KNOWN_LIMITATIONS.md`. Headline: uplift in the simulator is small and the confidence interval is wide; the value of this submission is the trustworthy decide-act-verify-measure loop, not a large number.

