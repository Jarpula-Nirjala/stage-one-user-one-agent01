# Impact report: agent arm vs randomized holdout

## 1. Headline

**We cannot tell the agent arm apart from the holdout arm.** Intention-to-treat on every goal-bearing triggered viewer: 12.2% (92/755) vs 11.4% (85/744): **+0.8 pp** [95% CI -2.5 pp to +4.0 pp], permutation p = 0.692.

- Precision-weighted across goals: **+0.8 pp** [95% CI -1.7 pp to +3.3 pp].
- Among viewers who actually saw a personalised page (served share 72.3% of the agent arm), the Bloom/CACE estimate is **+1.1 pp** [-3.5 pp to +5.6 pp]; this is the *causal* effect on served viewers, unlike the naive comparison below.

- Smallest effect this replay could reliably detect (80% power, alpha 5%) at the holdout base rate: about **+4.6 pp**. Anything smaller is inside the noise, which is why the confidence interval is reported and not just the point estimate.

- Honest reading: the agent is a small lever in this simulator. The simulator only raises play propensity while a personalised page is live (+10% per valid row, up to +80%) and adds a little for messages; goals that need a purchase barely move.

## 2. Design

- **Randomization:** `replay/arms.csv` assigns every viewer to `agent` or `holdout` at random. Holdout viewers received **zero** actions (verified below from the simulator report and `actions.jsonl`).
- **Same goal in both arms:** the agent's *reference goal* is computed for every triggered viewer from pre-treatment facts at the first live trigger, by the same rule engine. For holdout viewers this is a computation-only counterfactual: no model call, no action, no state visible to the viewer. That makes goal-level comparison fair.
- **Success** is judged from simulator outcome events (+ scripted live events, de-duplicated by event_id) using the goal's success rule at its final version (G03 was corrected mid-replay and the corrected rule applies to every G03 goal). Events count only if their *estimated true time* lies inside the goal window. Goals whose window is not fully observable are `inconclusive` and excluded **symmetrically** (agent 1, holdout 1); viewers without any goal: agent 107, holdout 138.
- **Triggered viewers:** 1746 (agent 863, holdout 883). Viewers with no trigger on the replay day are outside the experiment.


## 3. Results by goal (intention-to-treat)

| goal | agent | holdout | diff | 95% CI | p |
|---|---|---|---|---|---|
| G01 | 33.9% (20/59) | 29.4% (15/51) | +4.5 pp | -12.8 pp to +21.1 pp | 0.684 |
| G02 | 15.7% (34/217) | 12.7% (25/197) | +3.0 pp | -3.9 pp to +9.7 pp | 0.396 |
| G03 | 0.0% (0/1) | 0.0% (0/0) | +0.0 pp | -100.0 pp to +100.0 pp | 1.000 |
| G04 | 0.0% (0/1) | 0.0% (0/1) | +0.0 pp | -79.3 pp to +79.3 pp | 1.000 |
| G05 | 4.4% (2/45) | 6.2% (3/48) | -1.8 pp | -12.9 pp to +9.4 pp | 1.000 |
| G06 | 9.6% (5/52) | 14.3% (9/63) | -4.7 pp | -16.7 pp to +8.1 pp | 0.559 |
| G07 | 3.7% (12/321) | 3.2% (10/311) | +0.5 pp | -2.5 pp to +3.6 pp | 0.833 |
| G08 | 25.6% (11/43) | 21.7% (10/46) | +3.8 pp | -13.6 pp to +21.3 pp | 0.815 |
| G09 | 27.3% (3/11) | 20.0% (2/10) | +7.3 pp | -28.3 pp to +39.9 pp | 1.000 |
| G12 | 100.0% (5/5) | 64.7% (11/17) | +35.3 pp | -11.7 pp to +58.7 pp | 0.268 |

Per-goal cells are small; treat single-goal differences as hypotheses, not findings.

### Universal outcomes (every triggered viewer, no goal required)

| outcome | agent | holdout | diff | 95% CI | p |
|---|---|---|---|---|---|
| plays_7d | 68.0% (587/863) | 64.7% (571/883) | +3.4 pp | -1.1 pp to +7.8 pp | 0.141 |
| completes_7d | 46.1% (398/863) | 43.1% (381/883) | +3.0 pp | -1.7 pp to +7.6 pp | 0.230 |
| purchase_14d | 3.7% (32/863) | 4.1% (36/883) | -0.4 pp | -2.2 pp to +1.5 pp | 0.715 |

## 4. Why "served vs not served" misleads

Comparing viewers the agent *served* with everyone else gives 13.6% (85/623) vs everyone else: 10.5% (92/876): **+3.1 pp** [95% CI -0.2 pp to +6.6 pp], permutation p = 0.076.

That number is not an effect of the agent. "Served" is a *post-treatment* condition: a page counts only if the viewer opened a session in which it was shown, and visiting is itself a precondition for playing or buying. Conditioning on it compares people who came back with people who did not, whatever the agent did. Pre-treatment activity can look similar (table) while the post-treatment gap is large:

| group | n | opens in the 14 days before the replay | plays in the 14 days before | ever paid |
|---|---|---|---|---|
| served | 623 | 5.19 | 2.26 | 51.0% |
| everyone else | 876 | 4.99 | 2.18 | 51.4% |

Inside the agent arm alone, viewers who saw a page succeeded at 13.6% (n=623) versus 5.3% (n=132) for those who did not: a gap that mixes the page's effect with who comes back at all. Only the randomized arms separate the two; in the pilot (section 6) the same flaw produced a fake +22 points and there the selection is visible even before treatment.

### Randomization check (pre-treatment covariates)

| covariate | agent | holdout | standardised diff |
|---|---|---|---|
| pre_opens_14d | 4.832 | 4.972 | -0.034 |
| pre_plays_14d | 2.061 | 2.176 | -0.044 |
| pre_paid | 0.492 | 0.519 | -0.052 |

|std diff| below ~0.1 means the arms are balanced, as randomization should give.

### Safety facts from the simulator report

- policy problems detected by the simulator: **0** (quiet hours / consent / frequency cap / holdout)

- holdout actions ignored by the backend: **0**; pushes blocked for missing consent: **0**; WhatsApp unapproved template: **0**

- home pages accepted: 944, shown personalised: 832, published but never shown (dialect mismatch/too late): 37; rows dropped below minimum: 0

- messages: push delivered 122 (undeliverable token 24), WhatsApp delivered 71, clicks push/WA 1/1, opt-outs caused 0


## 5. Robustness: same actions, different simulator seeds

| seed | agent-arm success | holdout success | ITT diff | 95% CI | p |
|---|---|---|---|---|---|
| oua-practice | 12.2% | 11.4% | +0.8 pp | -2.5 pp to +4.0 pp | 0.692 |
| s2 | 12.1% | 10.1% | +2.0 pp | -1.2 pp to +5.2 pp | 0.263 |
| s3 | 12.3% | 8.9% | +3.4 pp | +0.3 pp to +6.6 pp | 0.031 |
| s4 | 10.4% | 10.5% | -0.0 pp | -3.2 pp to +3.1 pp | 1.000 |

The simulator is stochastic given a seed. Across 4 seeds the ITT difference ranges from -0.0 pp to +3.4 pp (mean +1.5 pp). The agent's actions are identical in every row; only the simulated user behaviour changes.


## 6. The pilot report, recomputed

The internal pilot summary claims a **2.3x uplift (+22.4 points)** by comparing 217 *served* users (40.1%) with 3,674 *all other* users (17.7%). Recomputed from `pilot_outcomes.csv`:

| comparison | agent/served | other/holdout | difference | 95% CI |
|---|---|---|---|---|
| pilot report (served vs all other) | 40.1% (87/217) | 17.7% (649/3674) | +22.4 pp | +16.0 pp to +29.2 pp |
| **randomized: all treatment vs all holdout** | 18.3% (349/1912) | 19.6% (387/1979) | **-1.3 pp** | -3.8 pp to +1.2 pp |

- Treatment-arm viewers the agent did *not* serve succeeded at 15.5% (n=1695), the served viewers at 40.1%: the agent served the people who were already going to succeed. On the randomized arms the pilot shows **no uplift** (permutation p = 0.32); the point estimate is slightly negative.

- Selection is visible in behaviour: average app opens during the pilot window were 6.2 for served users, 4.0 for treatment users not served and 4.3 for holdout.

- Goal mix also differs: served users {'G09': 105, 'G05': 84, 'G02': 15, 'G06': 12, 'G04': 1}; everyone else {'G05': 2034, 'G06': 319, 'G09': 848, 'G02': 345, 'G04': 128}.


| goal | treatment | holdout | diff | 95% CI |
|---|---|---|---|---|
| G02 | 19.2% (34/177) | 19.7% (36/183) | -0.5 pp | -8.6 pp to +7.8 pp |
| G04 | 23.8% (15/63) | 22.7% (15/66) | +1.1 pp | -13.4 pp to +15.6 pp |
| G05 | 1.2% (13/1045) | 0.6% (6/1073) | +0.7 pp | -0.1 pp to +1.6 pp |
| G06 | 0.6% (1/157) | 2.9% (5/174) | -2.2 pp | -5.9 pp to +1.1 pp |
| G09 | 60.9% (286/470) | 67.3% (325/483) | -6.4 pp | -12.5 pp to -0.3 pp |

Precision-weighted across goals: +0.4 pp [-0.4 pp to +1.1 pp]. **Recommendation: do not roll out on the strength of the pilot report.**


## 7. Limits of this analysis

- The effect sizes come from a simulator whose rules are visible in `simulator/simulate_outcomes.py`; real uplift would need a live experiment.
- Goal outcomes cover a 14-day simulated horizon. Goals whose window outlasts it are `inconclusive` and excluded in both arms.
- Only viewers with a trigger on the replay day are in the experiment; the effect on everyone else is unmeasured.
- Multiple comparisons: the by-goal table has up to 10 rows; with 5% error per row one or two will look "significant" by chance.

