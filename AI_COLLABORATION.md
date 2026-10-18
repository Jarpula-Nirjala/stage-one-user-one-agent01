# AI collaboration log

**Tools:** Claude (Claude Code in VS Code) wrote most of the first drafts of the code, tests and docs under my direction.
I set the architecture constraints (deterministic core, independent verifier, shadow counterfactual for holdout, honest
measurement), read the pack and brief, and decided what to accept. Nothing here was submitted without being run:
`pytest` (41 tests), the full replay, the independent audit and the real simulator all ran on the real pack.

## How output was checked

* Every claim about the data was first measured with a script before any rule was written (duplicate ids, buffering vs
  clock skew, dialect conflicts, trial/lapsed rows, withdrawals, availability windows). The numbers in `final_report.md`'s
  issue table come from the loader's own counters, not from memory.
* Behaviour was pinned by tests that fail if the property is broken (holdout gets nothing, no push in quiet hours, no
  second message in an IST day, withdrawn title leaves live pages, rule correction touches only open G03 goals).
* A property test swaps every viewer's arm and checks the reference goal is identical (no arm-dependent planning).
* The planner and verifier share no flags; a third, independent auditor re-reads `actions.jsonl` against the raw pack.
* The simulator's own `policy_problems` list is the external referee for quiet hours, consent, cap and holdout.

## Things the assistant suggested or did that I corrected or rejected

1. **Event age from `received_at - occurred_at`.** The obvious reading of "late event", and what an assistant proposes first.
   Measuring the pack showed 61 live triggers with wrong phone clocks (hours of skew, sent_at == occurred_at); they would all have been
   discarded as stale. Rejected: age is `sent_at - occurred_at`, which cancels the clock error. A test pins it.
2. **The trigger counted as its own history.** `G12 returns after 14 days away` saw the very app_open being decided as
   a recent open and became unreachable. Caught in review of the pipeline before the first full run; fixed by excluding the
   trigger from the facts it is decided on.
3. **Re-checking eligibility on every trigger** (the first draft did) would have dropped a G09 or G12 goal the moment it started working (the entry
   condition is what the goal changes). Restricted the re-check to status-based goals.
4. **Over-messaging.** The first end-to-end run gave 44% of published decisions a push/WhatsApp/in-app message. That is the
   "message everyone because you can" failure. Rejected; messages now need a goal that warrants leaving the app, a
   confirmed dialect, evidence that the home page alone fails for this viewer, a 7-day budget and a send slot before the
   deadline.
5. **Padding rows to please the simulator.** The simulator pays +0.15 for a `next_episode` row containing a started series.
   An easy way to farm it is to pad that row with unrelated titles to reach 3. Rejected: the row only lists series the viewer
   actually has a next episode for; otherwise it is dropped.
6. **Letting the model's goal through when confident.** The injected `unsupported_claim` says confidence 0.97 and "confirmed in
   billing". Rejected on principle: a claim is accepted only if the rule engine independently finds the goal eligible with
   resolvable evidence, and the reason text is ignored. Tested with exactly that payload.
7. **A crash under a small tool budget.** A test with `max_tool_calls=4` found that a mandatory verification call could hit
   the cap and raise. Fixed by reserving two calls for verification and by making an unverifiable message be dropped.
8. **Wrong replan expectation in my own test** (I assumed a new payer becomes G08/G09; the ladder correctly gave G02 to a
   viewer who had never completed an episode). I changed the test to assert the property that matters (no money goal for
   a payer), not the assistant's guess of the specific goal.
9. **Served-vs-rest as "the result".** Considered and rejected as a headline: it conditions on a post-treatment event. The report
   leads with ITT on randomized arms and shows the biased number only for contrast; the pilot is recomputed (+22 points
   becomes about -1 point).
10. **Tooling slips.** Two shell heredoc edits corrupted escape sequences in source files; syntax checks caught both and I
    repaired them with the edit tool instead.

## What the assistant was good for

Boilerplate (loaders, CLI, report templates), the statistics helpers (Newcombe CI, permutation test, Bloom estimator), the
first pass of the offline dashboard, and generating adversarial test cases (3-way dialect conflict, duplicate profile rows
that disagree, a catalogue description that addresses the agent). All of it was then reviewed line by line.
