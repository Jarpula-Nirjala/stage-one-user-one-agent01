"""Command line: `python -m stage_agent <command>`.

  run-all        replay -> simulate -> judge -> impact -> reports (the one command evaluators need)
  replay         decisions/actions/state only
  audit-actions  independent safety audit of an actions.jsonl
  viewer         what the agent knew / decided / observed for one viewer (optionally as of a time)
  bench          N triggers in one minute: queueing and load shedding
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import os
import random
import shutil
import sys
import time
from datetime import timedelta

from .agent import Runtime
from .audit import audit_actions
from .config import Config
from .harness import latency_report, run_replay, write_outputs
from .impact import analyse, reanalyse_pilot, run_simulator, write_impact_report
from .judge import Observations, judge_all, load_outcome_events, ref_outcomes
from .loader import Ev, load_pack
from .report import write_final_report
from .timeutil import iso, parse_ts


def _pack_arg(p):
    p.add_argument("--pack", default=os.environ.get("STAGE_PACK", "../pack"), help="path to the extracted data pack (or $STAGE_PACK)")
    p.add_argument("--config", default=None, help="JSON file overriding Config fields")


def _load_priors(path):
    if path and os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return None


def cmd_replay(a):
    cfg = Config.from_file(a.config)
    pack = load_pack(a.pack)
    rt = Runtime(pack, cfg, _load_priors(a.priors))
    res = run_replay(rt, progress=True)
    write_outputs(rt, res, a.out)
    print("decisions: %d, actions: %d -> %s" % (len(rt.decisions), len(rt.effective_actions()), a.out))


def cmd_run_all(a):
    t0 = time.time()
    cfg = Config.from_file(a.config)
    print("[1/6] loading pack ...", flush=True)
    pack = load_pack(a.pack)
    rt = Runtime(pack, cfg, _load_priors(a.priors))
    print("[2/6] replaying %d triggers, %d live events, %d control events ..." % (len(pack.triggers), len(pack.live), len(pack.controls)), flush=True)
    res = run_replay(rt, progress=True)
    write_outputs(rt, res, a.out)
    actions_path = os.path.join(a.out, "actions.jsonl")
    print("[3/6] independent safety audit of %s ..." % actions_path, flush=True)
    actions = [json.loads(l) for l in open(actions_path, encoding="utf-8")]
    safety = audit_actions(pack, actions)
    json.dump(safety, open(os.path.join(a.out, "safety_audit.json"), "w", encoding="utf-8"), indent=2)
    print("      violations: %d" % safety["violations"], flush=True)
    sim_dir = os.path.join(a.out, "sim")
    print("[4/6] running the simulator (seed %s) ..." % a.seed, flush=True)
    run_simulator(a.pack, actions_path, sim_dir, a.seed)
    sim_report = json.load(open(os.path.join(sim_dir, "simulator_report.json"), encoding="utf-8"))
    print("[5/6] judging outcomes and measuring impact ...", flush=True)
    judged, obs, ref_rows = judge_all(rt, sim_dir, a.out)
    # viewer state now also carries outcomes + learnings
    vs = os.path.join(a.out, "viewer_state")
    for u in rt.viewers:
        with open(os.path.join(vs, u + ".json"), "w", encoding="utf-8") as f:
            json.dump(rt.snapshot(u), f, ensure_ascii=False, separators=(",", ":"))
    main = analyse(rt, obs, ref_rows, draws=a.draws)
    main["seed"] = a.seed
    seed_results = [main]
    for s in [x for x in a.extra_seeds.split(",") if x]:
        d2 = os.path.join(a.out, "_sim_" + s)
        run_simulator(a.pack, actions_path, d2, s)
        o2 = Observations(rt, load_outcome_events(d2))
        r2 = analyse(rt, o2, ref_outcomes(rt, o2), draws=1000)
        r2["seed"] = s
        seed_results.append(r2)
        shutil.rmtree(d2, ignore_errors=True)
    pilot = reanalyse_pilot(pack)
    lat = latency_report(rt, res)
    with open(os.path.join(a.out, "latency_report.json"), "w", encoding="utf-8") as f:
        json.dump(lat, f, indent=2, ensure_ascii=False)
    write_impact_report(os.path.join(a.out, "impact_report.md"), main, pilot, seed_results, sim_report, judged)
    itt = main.pop("itt_rows")
    with open(os.path.join(a.out, "impact_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"primary": main["primary"], "stratified": main["stratified"], "cace": main["cace"], "by_goal": main["by_goal"],
                   "naive": main["naive"], "pilot": pilot and {"naive": pilot["naive"], "itt": pilot["itt"]},
                   "seeds": [{"seed": r["seed"], "primary": r["primary"]} for r in seed_results]}, f, indent=2, default=str)
    print("[6/6] writing final report ...", flush=True)
    write_final_report(os.path.join(a.out, "final_report.md"), rt, lat, judged, main, pilot, safety, sim_report, a.seed)
    try:
        from .dashboard import write_dashboard
        write_dashboard(os.path.join(a.out, "dashboard.html"), rt, lat, main, judged, itt)
    except Exception as e:  # the dashboard is a convenience; never fail the run for it
        print("dashboard skipped: %s" % e)
    print("done in %.0f s -> %s" % (time.time() - t0, a.out))


def cmd_audit_actions(a):
    pack = load_pack(a.pack)
    actions = [json.loads(l) for l in open(a.actions, encoding="utf-8")]
    rep = audit_actions(pack, actions)
    print(json.dumps(rep, indent=2))
    sys.exit(0 if rep["clean"] else 1)


def cmd_viewer(a):
    p = os.path.join(a.run, "viewer_state", a.viewer + ".json")
    if not os.path.exists(p):
        sys.exit("no state for %s in %s" % (a.viewer, a.run))
    st = json.load(open(p, encoding="utf-8"))
    at = parse_ts(a.at) if a.at else None
    decs = [json.loads(l) for l in open(os.path.join(a.run, "decisions.jsonl"), encoding="utf-8")]
    decs = [d for d in decs if d["viewer_id"] == a.viewer and (at is None or parse_ts(d["trigger"]["deliver_at"]) <= at)]
    print("VIEWER %s  arm=%s  reference goal=%s" % (st["viewer_id"], st["arm"], (st["analysis_goal"] or {}).get("goal_id")))
    if at:
        print("as of %s: %d decision(s) known" % (iso(at), len(decs)))
    for d in decs:
        t = d["trigger"]
        print("\n%s  %s @ %s (age %ss)  -> %s [%s]" % (d["decision_id"], t["event"], t["deliver_at"], t["event_age_s"], d["action"], d["status"]))
        if d.get("goal"):
            g = d["goal"]
            print("   goal %s (deadline %s), success: %s %s" % (g["id"], g["deadline"], g["success"]["event"], g["success"]["condition"]))
        if d.get("dialect"):
            print("   dialect %s via %s, conflicts %s" % (d["dialect"]["used"], d["dialect"]["source"], d["dialect"]["conflicts"]))
        print("   confidence %s: %s" % (d.get("confidence"), d.get("confidence_reason")))
        if d.get("no_action_reason"):
            print("   no action because: %s" % d["no_action_reason"])
        for k in ("page_decision", "message_decision"):
            if d.get(k):
                print("   %s: %s" % (k, d[k]))
        print("   checks passed: %s ; failed: %s" % (d["checks"]["passed"], [f["check"] for f in d["checks"]["failed"]]))
        print("   latency %.3f s, model calls %s, tool calls %s, fallbacks %s" % (d.get("latency_s", 0), d.get("model_calls"), d.get("tool_calls"), d.get("fallback_used")))
    if not a.at:
        for o in st["outcomes"]:
            print("\nOUTCOME %s: %s (%s)" % (o["goal_id"], o["judged"], o["reason"]))
        for l in st["learnings"]:
            print("LEARNED: %s -> %s" % (l["lesson"], l["next_step_hint"]))
    if a.journal:
        for j in st["journal"]:
            if at is None or parse_ts(j["at"]) <= at:
                print("  journal", j)


def cmd_bench(a):
    cfg = Config.from_file(a.config)
    cfg.workers = a.workers
    pack = load_pack(a.pack)
    rng = random.Random(11)
    users = sorted({e.user for e in pack.triggers if pack.arms.get(e.user) == "agent"})
    rng.shuffle(users)
    users = users[: a.n]
    base = min(e.deliver for e in pack.triggers) + timedelta(hours=a.offset_h)
    trig = []
    proto = {e.user: e for e in pack.triggers}
    for i, u in enumerate(users):
        e = copy.copy(proto[u])
        e.id = "bench-%05d" % i
        e.session = "bench-s-%05d" % i
        e.deliver = e.recv = e.est = base + timedelta(seconds=rng.random() * 60)
        e.buffered_s = 0.0
        e.skew_s = 0.0
        trig.append(e)
    bench_pack = dataclasses.replace(pack, triggers=sorted(trig, key=lambda e: e.deliver), live=[], controls=[], faults={})
    rt = Runtime(bench_pack, cfg)
    res = run_replay(rt)
    lat = latency_report(rt, res)
    out = {"triggers_in_one_minute": len(trig), "workers": cfg.workers, "queue": lat["queue"], "live_visits": lat["live_visits"],
           "over_slo_60s": lat["over_slo_60s"], "decisions_by_status": {k: sum(1 for d in rt.decisions if d["status"] == k) for k in sorted({d["status"] for d in rt.decisions})},
           "shed_levels": {k: v for k, v in lat["queue"].items() if k.startswith("shed") or k.startswith("dropped")}}
    print(json.dumps(out, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="stage_agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run-all", help="full pipeline")
    _pack_arg(p)
    p.add_argument("--out", default="sample_run")
    p.add_argument("--seed", default="oua-practice")
    p.add_argument("--extra-seeds", default="s2,s3,s4", help="comma list of extra simulator seeds for the robustness table ('' to skip)")
    p.add_argument("--priors", default=None, help="learned_priors.json from an earlier run")
    p.add_argument("--draws", type=int, default=5000, help="permutation draws for the primary test")
    p.set_defaults(fn=cmd_run_all)
    p = sub.add_parser("replay", help="replay only")
    _pack_arg(p)
    p.add_argument("--out", default="sample_run")
    p.add_argument("--priors", default=None)
    p.set_defaults(fn=cmd_replay)
    p = sub.add_parser("audit-actions", help="independent audit of an actions file")
    _pack_arg(p)
    p.add_argument("--actions", required=True)
    p.set_defaults(fn=cmd_audit_actions)
    p = sub.add_parser("viewer", help="inspect one viewer")
    p.add_argument("--run", default="sample_run")
    p.add_argument("--viewer", required=True)
    p.add_argument("--at", default=None, help="show only what was known at this UTC time")
    p.add_argument("--journal", action="store_true")
    p.set_defaults(fn=cmd_viewer)
    p = sub.add_parser("bench", help="N triggers inside one minute")
    _pack_arg(p)
    p.add_argument("--n", type=int, default=1000)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--offset-h", type=float, default=3.0)
    p.set_defaults(fn=cmd_bench)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
