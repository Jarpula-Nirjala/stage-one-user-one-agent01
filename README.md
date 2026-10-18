# One User, One Agent

A per-viewer lifecycle agent for the STAGE OTT take-home. For every viewer it **understands** (facts with source and
freshness, conflicts kept visible), **chooses one measurable goal**, **decides** (home page, one message, or nothing),
**acts inside a latency and call budget**, has an **independent verifier** that can veto it, **closes the loop** from
real outcome signals and **proves impact against the randomized holdout**.

Standard library only (Python 3.9+). No API key needed: the model is a deterministic mock that also replays the pack's
injected faults. An optional hosted-model adapter exists but is never used by the tests or the sample run.

## Setup

```bash
# 1. put the data pack somewhere (it is personal data: keep it out of git and off any service that retains content)
unzip one_user_one_agent_data_pack.zip -d pack_root        # -> pack_root/one_user_one_agent_pack/...
cd submission
python -m pip install pytest                                # only needed to run the tests
```

## One command: replay everything

```bash
cd submission
PYTHONPATH=src python -m stage_agent run-all --pack /path/to/one_user_one_agent_pack --out sample_run
# Windows PowerShell:  $env:PYTHONPATH="src"; python -m stage_agent run-all --pack ..\pack --out sample_run
```

About 2-3 minutes. It replays the trigger script (with live events, control events and model faults), audits the actions
independently, runs the pack's simulator, judges every goal, measures impact vs holdout (several simulator seeds) and
writes everything under `sample_run/`. Nothing in `src/` is specific to this replay day: the replay window, IST day,
titles, goals, faults and corrections are all read from the pack, so it runs unchanged on an unseen day. Use
`--seed` for the simulator seed, `--extra-seeds ""` to skip the robustness runs, `--config file.json` to override any
`Config` field (budgets, timeouts, workers, interpretations), `--priors sample_run/learned_priors.json` to feed one run's
learning into the next.

## Other commands

| command | what it does |
|---|---|
| `python -m stage_agent viewer --run sample_run --viewer U-01991` | walk one viewer from app open to judged outcome; add `--at 2026-10-01T10:00:00Z` to see only what was known then; `--journal` for the raw timeline |
| `python -m stage_agent audit-actions --pack P --actions sample_run/actions.jsonl` | independent safety audit (holdout, consent, quiet hours, cap, dialect, availability, copy, templates); exit code 1 on violation |
| `python -m stage_agent bench --pack P --n 1000 --workers 8` | N triggers inside one minute: queueing, load shedding, latency |
| `python -m pytest` | 41 tests (the 4 required ones plus safety, failure and measurement tests; one runs the full pack if `../pack` exists) |

Open `sample_run/dashboard.html` in a browser (offline, no libraries) for the decision mix, impact forest plot, replay
timeline with the injected incidents, latency histogram and a viewer-journey explorer.

## Live website (deploy on Render)

A standard-library web app serves the run's outputs plus a **live demo that runs the real agent** on a synthetic viewer you
build in the browser (subscription, dialect conflicts, consent, offline-buffered opens, model faults, store outage, a
purchase after the decision...). It never serves the raw data pack.

```bash
PYTHONPATH=src python -m stage_agent serve --port 8000      # then open http://localhost:8000
```

Render: New + > Blueprint > pick this repo (it reads `render.yaml`), or create a Web Service with build command
`echo ok` and start command `PYTHONPATH=src python -m stage_agent.web`. No dependencies, health check `/healthz`.
Tabs: overview, live demo, viewer explorer, decision browser, dashboard, impact report, final report, architecture, limitations.

### Sign-in and sign-up

New visitors use the **Create account** tab (name, email, password; passwords are stored only as salted PBKDF2 hashes in
`$DATA_DIR/users.json`, capped at 500 accounts). On Render's free plan the disk is ephemeral, so accounts survive sleeps but
not a redeploy; the admin account below always works. Photographs on the site are from Unsplash (free licence), stored in
`src/stage_agent/web_static/img/` (about 1 MB).

The site sits behind a sign-in page (HMAC-signed session cookie, per-IP brute-force throttle, no password in the repo).
Set the account through environment variables:

| variable | meaning |
|---|---|
| `APP_USERNAME` | default `jarpula` |
| `APP_PASSWORD` | the password. On Render `render.yaml` makes Render generate one: read it in the service's **Environment** tab. If unset locally, a random one is printed in the server log |
| `SECRET_KEY` | signs session cookies (Render generates it) |
| `ALLOW_GUEST` | `1` (default) shows "Continue as guest": read-only browsing, no live demo; `0` disables guests |

## What is in `sample_run/`

| file | content |
|---|---|
| `decisions.jsonl` | every decision (including holdout, duplicates, stale, replans) in the required schema plus extras: confidence_reason, no_action_reason, fallback_used, model/tool calls, goal candidates and rejections, model claims, verifier results |
| `actions.jsonl` | published actions in the simulator's input format (cancelled ones removed); `action_ledger.jsonl` has the full lifecycle incl. cancellations and supersessions |
| `no_action.jsonl` | every do-nothing decision with its reason |
| `viewer_state/` | one JSON per viewer: facts with provenance, goals and revisions, decisions, actions, outcomes, learnings, journal |
| `outcomes.jsonl` | succeeded / failed / inconclusive per goal, with evidence and attribution notes; `reference_goal_outcomes.jsonl` is the same-goal-both-arms table behind the impact analysis |
| `latency_report.json` | p50/p95/p99, 60 s and 5 min counts, per-incident phases, surge window, budgets, fallbacks |
| `impact_report.md` | agent vs holdout (ITT), by goal, CACE, balance check, seed robustness, pilot recomputation |
| `final_report.md` | summary, data-issue table, surprise-handling table |
| `safety_audit.json`, `security_log.jsonl`, `unsupported_claims.jsonl`, `revisions.jsonl`, `resilience_log.jsonl`, `data_quality.json`, `learned_priors.json`, `sim/` | supporting evidence |

## Layout

```
src/stage_agent/  loader facts goals intervention verifier model resilience agent harness judge impact audit report dashboard cli
tests/            fixtures.py + 4 test modules (synthetic world, no personal data)
ARCHITECTURE.md  AI_COLLABORATION.md  KNOWN_LIMITATIONS.md
```

Read `ARCHITECTURE.md` first for ownership, planning, memory, verification, latency, failure recovery and trust boundaries.
