"""Impact measurement: agent arm vs randomized holdout arm (intention-to-treat), with uncertainty, a stratified estimate,
a served-viewer (CACE) estimate, a covariate balance check, the biased served-vs-rest comparison for contrast, the pilot
re-analysis and a multi-seed robustness check of the simulator."""
from __future__ import annotations

import csv
import json
import math
import os
import random
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .agent import Runtime
from .judge import Observations, load_outcome_events
from .loader import PackData
from .timeutil import UTC, iso, parse_ts

Z95 = 1.959964


# ------------------------------------------------------------------ statistics
def wilson(s: int, n: int, z: float = Z95) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = s / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def newcombe_diff(s1: int, n1: int, s0: int, n0: int) -> Tuple[float, float, float]:
    """Difference p1-p0 with Newcombe's hybrid-score 95% CI."""
    if n1 == 0 or n0 == 0:
        return (0.0, -1.0, 1.0)
    p1, p0 = s1 / n1, s0 / n0
    l1, u1 = wilson(s1, n1)
    l0, u0 = wilson(s0, n0)
    d = p1 - p0
    return d, d - math.sqrt((p1 - l1) ** 2 + (u0 - p0) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p0 - l0) ** 2)


def perm_p(a: List[int], b: List[int], draws: int = 5000, seed: int = 7) -> float:
    if not a or not b:
        return 1.0
    obs = abs(sum(a) / len(a) - sum(b) / len(b))
    pool = a + b
    rng = random.Random(seed)
    n = len(a)
    hits = 0
    for _ in range(draws):
        rng.shuffle(pool)
        x = sum(pool[:n]) / n
        y = sum(pool[n:]) / (len(pool) - n)
        hits += abs(x - y) >= obs - 1e-12
    return (hits + 1) / (draws + 1)


def stratified_diff(strata: Dict[str, Tuple[int, int, int, int]]) -> Tuple[float, float, float]:
    """Precision-weighted (inverse-variance) pooled difference across strata (goals). Returns (diff, lo, hi)."""
    num = den = 0.0
    for s1, n1, s0, n0 in strata.values():
        if n1 < 2 or n0 < 2:
            continue
        p1, p0 = (s1 + .5) / (n1 + 1), (s0 + .5) / (n0 + 1)
        var = p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0
        w = 1 / var
        num += w * (p1 - p0)
        den += w
    if den == 0:
        return (0.0, 0.0, 0.0)
    d, se = num / den, math.sqrt(1 / den)
    return d, d - Z95 * se, d + Z95 * se


def mde(n1: int, n0: int, p: float) -> float:
    if n1 == 0 or n0 == 0:
        return float("nan")
    return (Z95 + 0.8416) * math.sqrt(p * (1 - p) * (1 / n1 + 1 / n0))


def pp(x: float) -> str:
    return "%+.1f pp" % (x * 100)


def pc(x: float) -> str:
    return "%.1f%%" % (x * 100)


# ------------------------------------------------------------------ simulator
def run_simulator(pack_root: str, actions_path: str, out_dir: str, seed: str) -> None:
    sim = os.path.join(pack_root, "simulator", "simulate_outcomes.py")
    os.makedirs(out_dir, exist_ok=True)
    subprocess.run([sys.executable, "-I", sim, "--pack", pack_root, "--actions", actions_path, "--out", out_dir, "--seed", seed],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------ ITT table
def build_itt(rt: Runtime, obs: Observations, ref_rows: List[dict]) -> List[dict]:
    pack = rt.pack
    ref = {r["viewer_id"]: r for r in ref_rows}
    first_trig: Dict[str, datetime] = {}
    for e in pack.triggers:
        first_trig.setdefault(e.user, e.deliver)
    t_replay = min(first_trig.values()) if first_trig else None
    rows = []
    for u, t0 in first_trig.items():
        arm = pack.arms.get(u, "unknown")
        evs = obs.by_user.get(u, [])
        hist = rt.hist.get(u, [])
        pre_lo = t0 - timedelta(days=14)
        pre = [e for e in hist if pre_lo <= e.recv < t0]
        def within(kind, days, pred=lambda e: True):
            return any(e.kind == kind and t0 < e.est <= t0 + timedelta(days=days) and pred(e) for e in evs)
        r = ref.get(u)
        rows.append({
            "viewer_id": u, "arm": arm, "goal_id": r["goal_id"] if r else None,
            "goal_outcome": r["outcome"] if r else None,
            "exposed": len(obs.pages_shown.get(u, [])) > 0, "page_views": len(obs.pages_shown.get(u, [])),
            "plays_7d": within("play_started", 7), "completes_7d": within("episode_completed", 7),
            "purchase_14d": within("purchase_completed", 14, lambda e: not e.props.get("is_trial")),
            "pre_opens_14d": sum(1 for e in pre if e.kind == "app_open"), "pre_plays_14d": sum(1 for e in pre if e.kind == "play_started"),
            "pre_paid": bool(rt.facts_for(u, t0, None, "full").has_paid_ever),
        })
    return rows


def rate(rows, key):
    n = len(rows)
    s = sum(1 for r in rows if r[key])
    return s, n


def compare(rows_a, rows_h, key, draws=5000):
    s1, n1 = rate(rows_a, key)
    s0, n0 = rate(rows_h, key)
    d, lo, hi = newcombe_diff(s1, n1, s0, n0)
    p = perm_p([int(bool(r[key])) for r in rows_a], [int(bool(r[key])) for r in rows_h], draws)
    return {"agent_s": s1, "agent_n": n1, "agent_rate": s1 / n1 if n1 else 0, "holdout_s": s0, "holdout_n": n0,
            "holdout_rate": s0 / n0 if n0 else 0, "diff": d, "lo": lo, "hi": hi, "p": p}


def analyse(rt: Runtime, obs: Observations, ref_rows: List[dict], draws: int = 5000) -> dict:
    rows = build_itt(rt, obs, ref_rows)
    agent = [r for r in rows if r["arm"] == "agent"]
    hold = [r for r in rows if r["arm"] == "holdout"]
    res: dict = {"n_users": len(rows), "agent_n": len(agent), "holdout_n": len(hold)}
    # primary: goal success (reference goal, same for both arms), inconclusive excluded symmetrically
    g_rows = [dict(r, ok=r["goal_outcome"] == "succeeded") for r in rows if r["goal_outcome"] in ("succeeded", "failed")]
    ga = [r for r in g_rows if r["arm"] == "agent"]
    gh = [r for r in g_rows if r["arm"] == "holdout"]
    res["primary"] = compare(ga, gh, "ok", draws)
    res["inconclusive"] = Counter(r["arm"] for r in rows if r["goal_outcome"] == "inconclusive")
    res["no_goal"] = Counter(r["arm"] for r in rows if r["goal_outcome"] is None)
    by_goal, strata = {}, {}
    for gid in sorted({r["goal_id"] for r in g_rows}):
        a = [r for r in ga if r["goal_id"] == gid]
        h = [r for r in gh if r["goal_id"] == gid]
        by_goal[gid] = compare(a, h, "ok", 2000)
        strata[gid] = (by_goal[gid]["agent_s"], by_goal[gid]["agent_n"], by_goal[gid]["holdout_s"], by_goal[gid]["holdout_n"])
    res["by_goal"] = by_goal
    res["stratified"] = stratified_diff(strata)
    # secondary: universal metrics over every triggered viewer
    res["secondary"] = {k: compare(agent, hold, k, 2000) for k in ("plays_7d", "completes_7d", "purchase_14d")}
    # served-viewer effect (Bloom / Wald): ITT divided by the share of the agent arm that actually saw a personalised page
    served_share = sum(1 for r in agent if r["exposed"]) / len(agent) if agent else 0
    holdout_exposed = sum(1 for r in hold if r["exposed"])
    res["served_share_agent_arm"] = served_share
    res["holdout_exposed"] = holdout_exposed
    itt = res["primary"]["diff"]
    res["cace"] = {"estimate": itt / served_share if served_share else None,
                   "lo": res["primary"]["lo"] / served_share if served_share else None,
                   "hi": res["primary"]["hi"] / served_share if served_share else None}
    # naive (biased) comparison: served vs everyone else
    served = [r for r in g_rows if r["exposed"] and r["arm"] == "agent"]
    rest = [r for r in g_rows if not (r["exposed"] and r["arm"] == "agent")]
    nv = compare(served, rest, "ok", 2000)
    res["naive"] = nv
    res["selection"] = {
        "served": {"n": len(served), "pre_opens_14d": avg([r["pre_opens_14d"] for r in served]), "pre_plays_14d": avg([r["pre_plays_14d"] for r in served]),
                   "ever_paid": avg([r["pre_paid"] for r in served])},
        "rest": {"n": len(rest), "pre_opens_14d": avg([r["pre_opens_14d"] for r in rest]), "pre_plays_14d": avg([r["pre_plays_14d"] for r in rest]),
                 "ever_paid": avg([r["pre_paid"] for r in rest])},
        "agent_arm_unserved": {"n": len([r for r in ga if not r["exposed"]]), "success": avg([r["ok"] for r in ga if not r["exposed"]])},
        "agent_arm_served": {"n": len([r for r in ga if r["exposed"]]), "success": avg([r["ok"] for r in ga if r["exposed"]])},
    }
    # randomization check on pre-treatment covariates
    bal = {}
    for k in ("pre_opens_14d", "pre_plays_14d", "pre_paid"):
        xa, xh = [float(r[k]) for r in agent], [float(r[k]) for r in hold]
        sd = math.sqrt((var(xa) + var(xh)) / 2) or 1.0
        bal[k] = {"agent": avg(xa), "holdout": avg(xh), "std_diff": (avg(xa) - avg(xh)) / sd}
    res["balance"] = bal
    res["mde_at_holdout_rate"] = mde(len(ga), len(gh), res["primary"]["holdout_rate"] or 0.3)
    res["itt_rows"] = rows
    return res


def avg(xs):
    xs = list(xs)
    return sum(float(x) for x in xs) / len(xs) if xs else 0.0


def var(xs):
    if len(xs) < 2:
        return 0.0
    m = avg(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


# ------------------------------------------------------------------ pilot
def reanalyse_pilot(pack: PackData) -> Optional[dict]:
    root = pack.root
    pa, po = os.path.join(root, "pilot", "pilot_assignment.csv"), os.path.join(root, "pilot", "pilot_outcomes.csv")
    if not (os.path.exists(pa) and os.path.exists(po)):
        return None
    rows = list(csv.DictReader(open(po, encoding="utf-8")))
    for r in rows:
        r["ok"] = r["succeeded"] == "true"
        r["served_b"] = r["served"] == "true"
    T = [r for r in rows if r["arm"] == "treatment"]
    H = [r for r in rows if r["arm"] == "holdout"]
    served = [r for r in rows if r["served_b"]]
    other = [r for r in rows if not r["served_b"]]
    out = {"naive": compare(served, other, "ok", 2000), "itt": compare(T, H, "ok", 5000)}
    out["treated_unserved"] = {"n": sum(1 for r in T if not r["served_b"]), "rate": avg([r["ok"] for r in T if not r["served_b"]])}
    byg, strata = {}, {}
    for gid in sorted({r["goal_id"] for r in rows}):
        a = [r for r in T if r["goal_id"] == gid]
        h = [r for r in H if r["goal_id"] == gid]
        byg[gid] = compare(a, h, "ok", 2000)
        strata[gid] = (byg[gid]["agent_s"], byg[gid]["agent_n"], byg[gid]["holdout_s"], byg[gid]["holdout_n"])
    out["by_goal"] = byg
    out["stratified"] = stratified_diff(strata)
    # selection evidence: who gets 'served'? people who opened the app in the pilot window
    lo, hi = parse_ts("2026-09-01T00:00:00+05:30"), parse_ts("2026-09-15T00:00:00+05:30")
    def opens(u):
        return sum(1 for e in pack.events.get(u, []) if e.kind == "app_open" and lo <= e.recv < hi)
    out["opens_pilot_window"] = {"served": avg([opens(r["user_id"]) for r in served]), "treated_unserved": avg([opens(r["user_id"]) for r in T if not r["served_b"]]),
                                 "holdout": avg([opens(r["user_id"]) for r in H])}
    out["goal_mix"] = {"served": dict(Counter(r["goal_id"] for r in served)), "other": dict(Counter(r["goal_id"] for r in other))}
    return out


# ------------------------------------------------------------------ report
def fmt_cmp(c: dict) -> str:
    return "%s (%d/%d) vs %s (%d/%d): **%s** [95%% CI %s to %s], permutation p = %.3f" % (
        pc(c["agent_rate"]), c["agent_s"], c["agent_n"], pc(c["holdout_rate"]), c["holdout_s"], c["holdout_n"], pp(c["diff"]),
        pp(c["lo"]), pp(c["hi"]), c["p"])


def seeds_table(results: List[dict]) -> str:
    lines = ["| seed | agent-arm success | holdout success | ITT diff | 95% CI | p |", "|---|---|---|---|---|---|"]
    for r in results:
        c = r["primary"]
        lines.append("| %s | %s | %s | %s | %s to %s | %.3f |" % (r["seed"], pc(c["agent_rate"]), pc(c["holdout_rate"]), pp(c["diff"]), pp(c["lo"]), pp(c["hi"]), c["p"]))
    return "\n".join(lines)


def write_impact_report(path: str, res: dict, pilot: Optional[dict], seed_results: List[dict], sim_report: Optional[dict], judged: dict) -> None:
    P = res["primary"]
    L: List[str] = []
    a = L.append
    verdict = ("The agent arm did better than the holdout arm" if P["lo"] > 0 else
               "We cannot tell the agent arm apart from the holdout arm" if P["hi"] >= 0 else "The agent arm did worse than the holdout arm")
    a("# Impact report: agent arm vs randomized holdout\n")
    a("## 1. Headline\n")
    a("**%s.** Intention-to-treat on every goal-bearing triggered viewer: %s.\n" % (verdict, fmt_cmp(P)))
    a("- Precision-weighted across goals: **%s** [95%% CI %s to %s].\n" % (pp(res["stratified"][0]), pp(res["stratified"][1]), pp(res["stratified"][2])) +
      "- Among viewers who actually saw a personalised page (served share %s of the agent arm), the Bloom/CACE estimate is **%s** [%s to %s]; "
      "this is the *causal* effect on served viewers, unlike the naive comparison below.\n" % (
          pc(res["served_share_agent_arm"]), pp(res["cace"]["estimate"] or 0), pp(res["cace"]["lo"] or 0), pp(res["cace"]["hi"] or 0)))
    a("- Smallest effect this replay could reliably detect (80%% power, alpha 5%%) at the holdout base rate: about **%s**. Anything smaller is "
      "inside the noise, which is why the confidence interval is reported and not just the point estimate.\n" % pp(res["mde_at_holdout_rate"]))
    a("- Honest reading: the agent is a small lever in this simulator. The simulator only raises play propensity while a personalised page is "
      "live (+10% per valid row, up to +80%) and adds a little for messages; goals that need a purchase barely move.\n")
    a("## 2. Design\n")
    a("- **Randomization:** `replay/arms.csv` assigns every viewer to `agent` or `holdout` at random. Holdout viewers received **zero** actions "
      "(verified below from the simulator report and `actions.jsonl`).\n"
      "- **Same goal in both arms:** the agent's *reference goal* is computed for every triggered viewer from pre-treatment facts at the first live "
      "trigger, by the same rule engine. For holdout viewers this is a computation-only counterfactual: no model call, no action, no state visible "
      "to the viewer. That makes goal-level comparison fair.\n"
      "- **Success** is judged from simulator outcome events (+ scripted live events, de-duplicated by event_id) using the goal's success rule at "
      "its final version (G03 was corrected mid-replay and the corrected rule applies to every G03 goal). Events count only if their *estimated "
      "true time* lies inside the goal window. Goals whose window is not fully observable are `inconclusive` and excluded **symmetrically** "
      "(agent %d, holdout %d); viewers without any goal: agent %d, holdout %d.\n" % (
          res["inconclusive"].get("agent", 0), res["inconclusive"].get("holdout", 0), res["no_goal"].get("agent", 0), res["no_goal"].get("holdout", 0)) +
      "- **Triggered viewers:** %d (agent %d, holdout %d). Viewers with no trigger on the replay day are outside the experiment.\n" % (res["n_users"], res["agent_n"], res["holdout_n"]))
    a("\n## 3. Results by goal (intention-to-treat)\n")
    a("| goal | agent | holdout | diff | 95% CI | p |\n|---|---|---|---|---|---|")
    for gid, c in res["by_goal"].items():
        a("| %s | %s (%d/%d) | %s (%d/%d) | %s | %s to %s | %.3f |" % (gid, pc(c["agent_rate"]), c["agent_s"], c["agent_n"], pc(c["holdout_rate"]),
                                                                 c["holdout_s"], c["holdout_n"], pp(c["diff"]), pp(c["lo"]), pp(c["hi"]), c["p"]))
    a("\nPer-goal cells are small; treat single-goal differences as hypotheses, not findings.\n")
    a("### Universal outcomes (every triggered viewer, no goal required)\n")
    a("| outcome | agent | holdout | diff | 95% CI | p |\n|---|---|---|---|---|---|")
    for k, c in res["secondary"].items():
        a("| %s | %s (%d/%d) | %s (%d/%d) | %s | %s to %s | %.3f |" % (k, pc(c["agent_rate"]), c["agent_s"], c["agent_n"], pc(c["holdout_rate"]), c["holdout_s"],
                                                                     c["holdout_n"], pp(c["diff"]), pp(c["lo"]), pp(c["hi"]), c["p"]))
    a("\n## 4. Why \"served vs not served\" misleads\n")
    nv, sel = res["naive"], res["selection"]
    a("Comparing viewers the agent *served* with everyone else gives %s.\n" % fmt_cmp({**nv}).replace("vs", "vs everyone else:", 1))
    a("That number is not an effect of the agent. \"Served\" is a *post-treatment* condition: a page counts only if the viewer opened a session in which it "
      "was shown, and visiting is itself a precondition for playing or buying. Conditioning on it compares people who came back with people who did not, "
      "whatever the agent did. Pre-treatment activity can look similar (table) while the post-treatment gap is large:\n")
    a("| group | n | opens in the 14 days before the replay | plays in the 14 days before | ever paid |\n|---|---|---|---|---|")
    a("| served | %d | %.2f | %.2f | %s |" % (sel["served"]["n"], sel["served"]["pre_opens_14d"], sel["served"]["pre_plays_14d"], pc(sel["served"]["ever_paid"])))
    a("| everyone else | %d | %.2f | %.2f | %s |" % (sel["rest"]["n"], sel["rest"]["pre_opens_14d"], sel["rest"]["pre_plays_14d"], pc(sel["rest"]["ever_paid"])))
    a("\nInside the agent arm alone, viewers who saw a page succeeded at %s (n=%d) versus %s (n=%d) for those who did not: a gap that mixes the page's "
      "effect with who comes back at all. Only the randomized arms separate the two; in the pilot (section 6) the same flaw produced a fake +22 points "
      "and there the selection is visible even before treatment.\n" % (
          pc(sel["agent_arm_served"]["success"]), sel["agent_arm_served"]["n"], pc(sel["agent_arm_unserved"]["success"]), sel["agent_arm_unserved"]["n"]))
    a("### Randomization check (pre-treatment covariates)\n")
    a("| covariate | agent | holdout | standardised diff |\n|---|---|---|---|")
    for k, b in res["balance"].items():
        a("| %s | %.3f | %.3f | %+.3f |" % (k, b["agent"], b["holdout"], b["std_diff"]))
    a("\n|std diff| below ~0.1 means the arms are balanced, as randomization should give.\n")
    if sim_report:
        c = sim_report.get("counts", {})
        a("### Safety facts from the simulator report\n")
        a("- policy problems detected by the simulator: **%s** (quiet hours / consent / frequency cap / holdout)\n" % sim_report.get("policy_problem_count"))
        a("- holdout actions ignored by the backend: **%s**; pushes blocked for missing consent: **%s**; WhatsApp unapproved template: **%s**\n" % (
            c.get("holdout_actions_ignored", 0), c.get("push_blocked_no_consent", 0) + c.get("whatsapp_blocked_no_consent", 0), c.get("whatsapp_rejected_unapproved_template", 0)))
        a("- home pages accepted: %s, shown personalised: %s, published but never shown (dialect mismatch/too late): %s; rows dropped below minimum: %s\n" % (
            c.get("home_page_accepted", 0), c.get("home_page_shown_personalised", 0), c.get("home_page_published_but_not_shown", 0), c.get("home_page_row_dropped_below_minimum", 0)))
        a("- messages: push delivered %s (undeliverable token %s), WhatsApp delivered %s, clicks push/WA %s/%s, opt-outs caused %s\n" % (
            c.get("push_delivered", 0), c.get("push_undeliverable_token", 0), c.get("whatsapp_delivered", 0), c.get("push_clicked", 0), c.get("whatsapp_clicked", 0),
            c.get("push_caused_opt_out", 0) + c.get("whatsapp_caused_opt_out", 0)))
    if seed_results:
        a("\n## 5. Robustness: same actions, different simulator seeds\n")
        a(seeds_table(seed_results))
        diffs = [r["primary"]["diff"] for r in seed_results]
        a("\nThe simulator is stochastic given a seed. Across %d seeds the ITT difference ranges from %s to %s (mean %s). The agent's actions are "
          "identical in every row; only the simulated user behaviour changes.\n" % (len(diffs), pp(min(diffs)), pp(max(diffs)), pp(avg(diffs))))
    if pilot:
        pn, pi = pilot["naive"], pilot["itt"]
        a("\n## 6. The pilot report, recomputed\n")
        a("The internal pilot summary claims a **2.3x uplift (+22.4 points)** by comparing 217 *served* users (40.1%) with 3,674 *all other* users (17.7%). "
          "Recomputed from `pilot_outcomes.csv`:\n")
        a("| comparison | agent/served | other/holdout | difference | 95% CI |\n|---|---|---|---|---|")
        a("| pilot report (served vs all other) | %s (%d/%d) | %s (%d/%d) | %s | %s to %s |" % (pc(pn["agent_rate"]), pn["agent_s"], pn["agent_n"], pc(pn["holdout_rate"]),
                                                                                           pn["holdout_s"], pn["holdout_n"], pp(pn["diff"]), pp(pn["lo"]), pp(pn["hi"])))
        a("| **randomized: all treatment vs all holdout** | %s (%d/%d) | %s (%d/%d) | **%s** | %s to %s |" % (pc(pi["agent_rate"]), pi["agent_s"], pi["agent_n"], pc(pi["holdout_rate"]),
                                                                                                          pi["holdout_s"], pi["holdout_n"], pp(pi["diff"]), pp(pi["lo"]), pp(pi["hi"])))
        a("\n- Treatment-arm viewers the agent did *not* serve succeeded at %s (n=%d), the served viewers at %s: the agent served the people who were already "
          "going to succeed. On the randomized arms the pilot shows **no uplift** (permutation p = %.2f); the point estimate is slightly negative.\n" % (
              pc(pilot["treated_unserved"]["rate"]), pilot["treated_unserved"]["n"], pc(pn["agent_rate"]), pi["p"]))
        o = pilot["opens_pilot_window"]
        a("- Selection is visible in behaviour: average app opens during the pilot window were %.1f for served users, %.1f for treatment users not served and %.1f for holdout.\n" % (
            o["served"], o["treated_unserved"], o["holdout"]))
        a("- Goal mix also differs: served users %s; everyone else %s.\n" % (pilot["goal_mix"]["served"], pilot["goal_mix"]["other"]))
        a("\n| goal | treatment | holdout | diff | 95% CI |\n|---|---|---|---|---|")
        for gid, c in pilot["by_goal"].items():
            a("| %s | %s (%d/%d) | %s (%d/%d) | %s | %s to %s |" % (gid, pc(c["agent_rate"]), c["agent_s"], c["agent_n"], pc(c["holdout_rate"]), c["holdout_s"], c["holdout_n"],
                                                                 pp(c["diff"]), pp(c["lo"]), pp(c["hi"])))
        a("\nPrecision-weighted across goals: %s [%s to %s]. **Recommendation: do not roll out on the strength of the pilot report.**\n" % (
            pp(pilot["stratified"][0]), pp(pilot["stratified"][1]), pp(pilot["stratified"][2])))
    a("\n## 7. Limits of this analysis\n")
    a("- The effect sizes come from a simulator whose rules are visible in `simulator/simulate_outcomes.py`; real uplift would need a live experiment.\n"
      "- Goal outcomes cover a 14-day simulated horizon. Goals whose window outlasts it are `inconclusive` and excluded in both arms.\n"
      "- Only viewers with a trigger on the replay day are in the experiment; the effect on everyone else is unmeasured.\n"
      "- Multiple comparisons: the by-goal table has up to 10 rows; with 5% error per row one or two will look \"significant\" by chance.\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
