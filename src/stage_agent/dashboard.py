"""Self-contained HTML dashboard (no network, no libraries): KPIs, decision mix, latency, impact forest plot, replay timeline
and a viewer journey explorer built from the run's own artefacts."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import List

from .agent import Runtime
from .timeutil import parse_ts

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Run Dashboard</title><style>
:root{--bg:#f7f8fb;--card:#fff;--ink:#1b2333;--mute:#667085;--line:#e4e7ee;--a:#5b3df5;--b:#12a594;--c:#e5484d;--d:#f5a524}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0e1320;--card:#161d2e;--ink:#e8ecf5;--mute:#98a2b8;--line:#263049;--a:#8c7bff;--b:#3dd6c3;--c:#ff7a80;--d:#ffc457}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{margin:0 0 4px;font-size:26px}h2{font-size:17px;margin:0 0 10px}
.sub{color:var(--mute);margin-bottom:20px}.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}.k{font-size:26px;font-weight:700}.l{color:var(--mute);font-size:13px}
.bar{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:13px}.bar i{display:block;height:14px;border-radius:4px;background:var(--a)}.bar span{min-width:190px;color:var(--mute)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left}svg{max-width:100%;height:auto}
input,select{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit;max-width:100%}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}.ok{color:var(--b)}.no{color:var(--c)}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:var(--bg);padding:10px;border-radius:8px;border:1px solid var(--line)}
</style></head><body><main>
<h1>One User, One Agent: run dashboard</h1><div class="sub" id="sub"></div>
<div class="grid" id="kpis"></div>
<div class="card"><h2>What the agent did with each trigger</h2><div id="mix"></div></div>
<div class="card"><h2>Impact: agent arm minus holdout (intention-to-treat, 95% CI)</h2><svg id="forest" viewBox="0 0 760 330" role="img" aria-label="forest plot"></svg><div class="l" id="forestnote"></div></div>
<div class="card"><h2>Triggers per 10 minutes (live vs buffered) and the injected incidents</h2><svg id="tl" viewBox="0 0 760 210" role="img" aria-label="timeline"></svg><div class="l" id="tlnote"></div></div>
<div class="card"><h2>Decision latency (published decisions)</h2><svg id="lat" viewBox="0 0 760 170" role="img" aria-label="latency histogram"></svg><div class="l" id="latnote"></div></div>
<div class="card"><h2>Viewer journey explorer</h2>
<select id="pick"></select> <input id="vid" placeholder="or type a viewer id (e.g. U-01991)" size="30"><div id="journey"></div></div>
</main><script>
const D=__DATA__;const $=id=>document.getElementById(id);
const pct=x=>(x*100).toFixed(1)+"%",pp=x=>(x>=0?"+":"")+(x*100).toFixed(1)+" pp";
$("sub").textContent=`Replay ${D.window[0]} to ${D.window[1]} (UTC) | ${D.n_triggers} triggers | ${D.n_viewers} viewers`;
const kp=[["Published decisions",D.status.PUBLISHED||0],["Holdout (zero action)",D.status.HOLDOUT_NO_ACTION||0],["Stale buffered opens skipped",D.status.NO_ACTION_STALE||0],
["Duplicates suppressed",D.status.DUPLICATE_SUPPRESSED||0],["p95 latency (s)",D.lat.p95],["Decisions over 60 s",D.lat.over],["Safety audit violations",D.violations],["ITT diff",pp(D.itt.diff)]];
$("kpis").innerHTML=kp.map(k=>`<div class="card"><div class="k">${k[1]}</div><div class="l">${k[0]}</div></div>`).join("");
const mx=Math.max(...Object.values(D.status));$("mix").innerHTML=Object.entries(D.status).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<div class="bar"><span>${k}</span><i style="width:${Math.max(2,v/mx*60)}%"></i>${v}</div>`).join("");
// forest
(function(){const rows=[{n:"ALL (primary)",...D.itt},...D.by_goal];const W=760,x0=170,x1=720,lo=-0.45,hi=0.45;const X=v=>x0+(Math.min(hi,Math.max(lo,v))-lo)/(hi-lo)*(x1-x0);
let s=`<line x1="${X(0)}" x2="${X(0)}" y1="10" y2="${20+rows.length*26}" stroke="currentColor" stroke-dasharray="4 3" opacity=".5"/>`;
rows.forEach((r,i)=>{const y=28+i*26;const col=r.lo>0?"var(--b)":r.hi<0?"var(--c)":"var(--a)";
s+=`<text x="4" y="${y+4}" font-size="12" fill="currentColor">${r.n} (n=${r.agent_n}/${r.holdout_n})</text><line x1="${X(r.lo)}" x2="${X(r.hi)}" y1="${y}" y2="${y}" stroke="${col}" stroke-width="2.5"/><circle cx="${X(r.diff)}" cy="${y}" r="4.5" fill="${col}"/><text x="${x1+8}" y="${y+4}" font-size="11" fill="currentColor">${pp(r.diff)}</text>`});
for(const t of [-.4,-.2,0,.2,.4])s+=`<text x="${X(t)}" y="${40+rows.length*26}" font-size="10" text-anchor="middle" fill="currentColor" opacity=".6">${(t*100)|0}pp</text>`;
$("forest").innerHTML=s;$("forestnote").textContent="Bars wider than the plot are clipped. A bar that crosses 0 means the replay cannot tell the arms apart. Small goals have very wide bars."})();
// timeline
(function(){const T=D.timeline,m=Math.max(...T.map(t=>t.live+t.stale),1),W=760,H=150,bw=W/T.length;let s="";
T.forEach((t,i)=>{const hl=t.live/m*H,hs=t.stale/m*H;s+=`<rect x="${i*bw}" y="${H-hl}" width="${bw-.5}" height="${hl}" fill="var(--a)"/><rect x="${i*bw}" y="${H-hl-hs}" width="${bw-.5}" height="${hs}" fill="var(--d)"/>`});
D.incidents.forEach(n=>{const x=n.idx*bw;s+=`<line x1="${x}" x2="${x}" y1="0" y2="${H}" stroke="var(--c)"/><text x="${Math.min(x+3,W-120)}" y="${12+(n.row||0)*12}" font-size="10" fill="var(--c)">${n.label}</text>`});
$("tl").innerHTML=s;$("tlnote").textContent="Blue = live visits, amber = offline-buffered opens (never treated as sessions). Red lines = withdrawals, rule correction, model outage, store degradation."})();
// latency hist
(function(){const h=D.lat.hist,m=Math.max(...h.map(b=>b.n),1),W=760,H=120,bw=W/h.length;let s="";h.forEach((b,i)=>{const bh=b.n/m*H;s+=`<rect x="${i*bw+2}" y="${H-bh}" width="${bw-4}" height="${bh}" fill="var(--b)"/><text x="${i*bw+bw/2}" y="${H+14}" font-size="10" text-anchor="middle" fill="currentColor" opacity=".7">${b.l}</text><text x="${i*bw+bw/2}" y="${H-bh-3}" font-size="10" text-anchor="middle" fill="currentColor">${b.n}</text>`});
$("lat").innerHTML=s;$("latnote").textContent=`p50 ${D.lat.p50}s, p95 ${D.lat.p95}s, max ${D.lat.max}s. The SLO is 60 s; hard failure 300 s.`})();
// explorer
const pick=$("pick");pick.innerHTML=D.picks.map(p=>`<option value="${p.id}">${p.label}</option>`).join("");
function show(id){const j=D.journeys[id];if(!j){$("journey").innerHTML="<p class=l>No journey embedded for that viewer. Use <code>python -m stage_agent viewer --viewer "+id+"</code> on the full run.</p>";return}
$("journey").innerHTML=`<p><b>${id}</b> <span class="pill">${j.arm}</span> reference goal ${j.goal||"none"}</p>`+j.decisions.map(d=>`<div class="card"><b>${d.id}</b> ${d.at} <span class="pill">${d.status}</span> action <b>${d.action}</b>
<div class="l">${d.goal?("goal "+d.goal+" until "+d.deadline):"no goal committed"} | dialect ${d.dialect||"?"} via ${d.dsrc||"?"} ${d.conf?("| conf "+d.conf):""} | ${d.lat}s</div>
<div>${d.reason?("<i>"+d.reason+"</i>"):""}</div><div class="l">checks: <span class="ok">${(d.passed||[]).join(", ")}</span> <span class="no">${(d.failed||[]).join(", ")}</span></div>${d.note?`<div class="l">${d.note}</div>`:""}</div>`).join("")+
(j.outcomes.length?`<div class="card"><b>Outcome</b>${j.outcomes.map(o=>`<div>${o.goal}: <b>${o.judged}</b> <span class=l>${o.reason}</span></div>`).join("")}${j.learn.map(l=>`<div class=l>learned: ${l}</div>`).join("")}</div>`:"")}
pick.onchange=()=>show(pick.value);$("vid").oninput=e=>{if(D.journeys[e.target.value])show(e.target.value)};if(D.picks.length)show(D.picks[0].id);
</script></body></html>"""


def _violations(path: str):
    import os
    try:
        return json.load(open(os.path.join(os.path.dirname(path), "safety_audit.json"), encoding="utf-8"))["violations"]
    except Exception:
        return "n/a"


def write_dashboard(path: str, rt: Runtime, lat: dict, impact: dict, judged: dict, itt_rows: list) -> None:
    D = rt.decisions
    status = Counter(d["status"] for d in D)
    t0 = parse_ts(D[0]["trigger"]["deliver_at"])
    nb = int(((parse_ts(D[-1]["trigger"]["deliver_at"]) - t0).total_seconds()) // 600) + 1
    tl = [{"live": 0, "stale": 0} for _ in range(nb)]
    for d in D:
        i = int((parse_ts(d["trigger"]["deliver_at"]) - t0).total_seconds() // 600)
        tl[min(i, nb - 1)]["stale" if d["status"] == "NO_ACTION_STALE" else "live"] += 1
    inc = []
    for k, c in enumerate(rt.pack.controls):
        i = int((parse_ts(c["at"]) - t0).total_seconds() // 600)
        label = {"title_withdrawn": "title withdrawn", "goal_rule_corrected": "G03 rule corrected", "dependency_unavailable": "model down",
                 "dependency_degraded": "store degraded"}.get(c["type"], c["type"])
        inc.append({"idx": max(0, i), "label": label, "row": k % 4})
    pub = sorted(d["latency_s"] for d in D if d["status"] == "PUBLISHED")
    edges = [(0, .2), (.2, .4), (.4, .6), (.6, 1), (1, 2), (2, 5), (5, 60), (60, 1e9)]
    hist = [{"l": ("%g-%gs" % (a, b)) if b < 1e8 else ">60s", "n": sum(1 for x in pub if a <= x < b)} for a, b in edges]
    by_goal = [{"n": g, "agent_n": c["agent_n"], "holdout_n": c["holdout_n"], "diff": c["diff"], "lo": max(-1, c["lo"]), "hi": min(1, c["hi"])}
               for g, c in impact["by_goal"].items() if c["agent_n"] >= 10 and c["holdout_n"] >= 10]
    P = impact["primary"]
    # journeys: one example of each interesting kind plus the injected-fault viewers
    by_user = defaultdict(list)
    for d in D:
        by_user[d["viewer_id"]].append(d)
    picks, seen = [], set()

    def add(u, label):
        if u not in seen and u in by_user and len(picks) < 40:
            seen.add(u)
            picks.append({"id": u, "label": "%s: %s" % (u, label)})
    for f in rt.pack.faults.values():
        add(f["user_id"], "injected model fault (%s)" % f["fault"])
    for d in D:
        if d["status"] == "REPLANNED":
            add(d["viewer_id"], "purchase after decision, replanned")
        elif d.get("dialect") and d["dialect"].get("conflicts") and len(d["dialect"]["conflicts"]) >= 2:
            add(d["viewer_id"], "three-way dialect conflict")
        elif d["status"] == "NO_ACTION_STALE":
            add(d["viewer_id"], "offline-buffered open (stale)")
        elif "WHATSAPP" in d["action"]:
            add(d["viewer_id"], "page + WhatsApp")
        elif "PUSH" in d["action"]:
            add(d["viewer_id"], "page + push")
        elif d["status"] == "HOLDOUT_NO_ACTION":
            add(d["viewer_id"], "holdout (no action)")
        elif d["status"] == "DUPLICATE_SUPPRESSED":
            add(d["viewer_id"], "duplicate trigger suppressed")
        elif d["status"] == "NO_ACTION_ABSTAIN":
            add(d["viewer_id"], "abstained (dialect unresolvable)")
    for r in rt.revisions:
        if r.get("type") == "cancel_after_opt_out" or r.get("type") == "goal_closed_by_event":
            add(r["viewer_id"], r["type"])
    journeys = {}
    for p in picks:
        u = p["id"]
        v = rt.viewers.get(u)
        journeys[u] = {"arm": v.arm if v else "?", "goal": ((v.analysis_goal or {}).get("goal_id") if v else None),
                       "decisions": [{"id": d["decision_id"], "at": d["trigger"]["deliver_at"], "status": d["status"], "action": d["action"],
                                      "goal": (d.get("goal") or {}).get("id"), "deadline": (d.get("goal") or {}).get("deadline"),
                                      "dialect": (d.get("dialect") or {}).get("used"), "dsrc": (d.get("dialect") or {}).get("source"),
                                      "conf": d.get("confidence"), "lat": d.get("latency_s"), "reason": d.get("no_action_reason"),
                                      "passed": d["checks"]["passed"], "failed": [f["check"] for f in d["checks"]["failed"]],
                                      "note": "; ".join(d.get("fallback_used", [])) or d.get("fault_not_exercised")} for d in by_user[u]],
                       "outcomes": v.outcomes if v else [], "learn": [l["lesson"] for l in (v.learnings if v else [])]}
    data = {"window": [D[0]["trigger"]["deliver_at"], D[-1]["trigger"]["deliver_at"]], "n_triggers": len(D), "n_viewers": len({d["viewer_id"] for d in D}),
            "status": dict(status), "violations": _violations(path),
            "lat": {"p50": lat["published_decisions"]["p50_s"], "p95": lat["published_decisions"]["p95_s"], "max": lat["published_decisions"]["max_s"],
                    "over": lat["over_slo_60s"], "hist": hist},
            "itt": {"diff": P["diff"], "lo": P["lo"], "hi": P["hi"], "agent_n": P["agent_n"], "holdout_n": P["holdout_n"]},
            "by_goal": by_goal, "timeline": tl, "incidents": inc, "picks": picks, "journeys": journeys}
    with open(path, "w", encoding="utf-8") as f:
        f.write(PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False, default=str)))
