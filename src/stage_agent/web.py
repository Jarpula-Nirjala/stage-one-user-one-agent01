"""Live web app (standard library only, deploys on Render/any PaaS).

Serves the sample run's artefacts (synthetic output, never the raw pack) and a live demo that runs the real agent on a
scenario you build in the browser. `python -m stage_agent.web` (reads $PORT, default 8000)."""
from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "web_static")


def find_run_dir() -> str:
    env = os.environ.get("SAMPLE_RUN")
    cands = [env] if env else []
    cands += [os.path.join(os.getcwd(), "sample_run"), os.path.join(HERE, "..", "..", "sample_run")]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "decisions.jsonl")):
            return os.path.abspath(c)
    raise SystemExit("sample_run/ not found; set SAMPLE_RUN or run `python -m stage_agent run-all` first")


# ------------------------------------------------------------------ markdown -> html (headings, tables, lists, bold, code)
def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*([^*\s][^*]*)\*", r"<i>\1</i>", s)
    return s


def md_to_html(md: str) -> str:
    out, lines, i = [], md.split("\n"), 0
    in_list = False
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s\-|:]+\|?$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            out.append("<div class=tw><table><thead><tr>" + "".join("<th>%s</th>" % _inline(c) for c in head) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                out.append("<tr>" + "".join("<td>%s</td>" % _inline(c.strip()) for c in lines[i].strip().strip("|").split("|")) + "</tr>")
                i += 1
            out.append("</tbody></table></div>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            if in_list:
                out.append("</ul>")
                in_list = False
            n = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (n + 1, _inline(m.group(2)), n + 1))
        elif ln.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % _inline(ln[2:]))
        elif ln.strip() == "":
            if in_list:
                out.append("</ul>")
                in_list = False
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<p>%s</p>" % _inline(ln))
        i += 1
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


class State:
    def __init__(self, run: str):
        self.run = run
        self.lock = threading.Lock()
        self.decisions = [json.loads(l) for l in open(os.path.join(run, "decisions.jsonl"), encoding="utf-8")]
        self.by_id = {d["decision_id"]: d for d in self.decisions}
        self.index = json.load(open(os.path.join(run, "viewer_state", "_index.json"), encoding="utf-8"))
        self.by_viewer = {}
        for d in self.decisions:
            self.by_viewer.setdefault(d["viewer_id"], []).append(d["decision_id"])
        self.summary = self._summary()

    def _j(self, name):
        p = os.path.join(self.run, name)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}

    def _summary(self):
        lat, imp, saf = self._j("latency_report.json"), self._j("impact_summary.json"), self._j("safety_audit.json")
        from collections import Counter
        st = Counter(d["status"] for d in self.decisions)
        goals = Counter((d.get("goal") or {}).get("id") for d in self.decisions if d["status"] == "PUBLISHED")
        outs = Counter()
        p = os.path.join(self.run, "outcomes.jsonl")
        if os.path.exists(p):
            for l in open(p, encoding="utf-8"):
                outs[json.loads(l)["outcome"]] += 1
        return {"status": dict(st), "goals_published": {str(k): v for k, v in goals.items()}, "outcomes": dict(outs),
                "viewers": len(self.index), "decisions": len(self.decisions), "latency": {k: lat.get(k) for k in ("live_visits", "published_decisions", "over_slo_60s", "over_5min_failures", "budgets", "circuit_breaker_trips")},
                "impact": {"primary": imp.get("primary"), "stratified": imp.get("stratified"), "cace": imp.get("cace"), "by_goal": imp.get("by_goal"), "seeds": imp.get("seeds"),
                           "pilot": imp.get("pilot"), "naive": imp.get("naive")},
                "safety": {k: saf.get(k) for k in ("actions_audited", "by_type", "violations", "clean")}}

    def list_decisions(self, q):
        status, arm, goal, text = q.get("status", [""])[0], q.get("arm", [""])[0], q.get("goal", [""])[0], q.get("q", [""])[0].lower()
        off, lim = int(q.get("offset", ["0"])[0] or 0), min(100, int(q.get("limit", ["30"])[0] or 30))
        rows = []
        for d in self.decisions:
            if status and d["status"] != status:
                continue
            if arm and d.get("arm") != arm:
                continue
            if goal and (d.get("goal") or {}).get("id") != goal:
                continue
            if text and text not in (d["viewer_id"] + " " + d["decision_id"] + " " + str(d.get("no_action_reason") or "")).lower():
                continue
            rows.append({"id": d["decision_id"], "viewer": d["viewer_id"], "arm": d.get("arm"), "status": d["status"], "action": d["action"],
                         "goal": (d.get("goal") or {}).get("id"), "conf": d.get("confidence"), "lat": d.get("latency_s"),
                         "at": d["trigger"]["deliver_at"], "reason": (d.get("no_action_reason") or "")[:140]})
        return {"total": len(rows), "rows": rows[off: off + lim]}

    def viewer(self, vid):
        p = os.path.join(self.run, "viewer_state", vid + ".json")
        if not re.fullmatch(r"[A-Za-z0-9\-_]+", vid) or not os.path.exists(p):
            return None
        st = json.load(open(p, encoding="utf-8"))
        st["decisions"] = [self.by_id[i] for i in self.by_viewer.get(vid, [])]
        return st


STATE = None


class H(BaseHTTPRequestHandler):
    server_version = "stage-agent"

    def log_message(self, fmt, *a):
        if os.environ.get("QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str)
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store" if ctype.startswith("application/json") else "public, max-age=300")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self._send(200, open(os.path.join(STATIC, "index.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
            if path == "/healthz":
                return self._send(200, {"ok": True})
            if path == "/dashboard":
                return self._send(200, open(os.path.join(STATE.run, "dashboard.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
            if path == "/api/summary":
                return self._send(200, STATE.summary)
            if path == "/api/decisions":
                return self._send(200, STATE.list_decisions(q))
            m = re.fullmatch(r"/api/decision/([A-Za-z0-9\-]+)", path)
            if m:
                d = STATE.by_id.get(m.group(1))
                return self._send(200 if d else 404, d or {"error": "not found"})
            if path == "/api/viewers":
                s = q.get("q", [""])[0].upper()
                ids = [k for k in STATE.index if s in k][:30]
                return self._send(200, [{"id": k, **STATE.index[k]} for k in ids])
            m = re.fullmatch(r"/api/viewer/([A-Za-z0-9\-_]+)", path)
            if m:
                v = STATE.viewer(m.group(1))
                return self._send(200 if v else 404, v or {"error": "not found"})
            m = re.fullmatch(r"/api/report/(impact|final)", path)
            if m:
                name = {"impact": "impact_report.md", "final": "final_report.md"}[m.group(1)]
                return self._send(200, {"html": md_to_html(open(os.path.join(STATE.run, name), encoding="utf-8").read())})
            m = re.fullmatch(r"/api/doc/(README|ARCHITECTURE|KNOWN_LIMITATIONS)", path)
            if m:
                root = os.path.abspath(os.path.join(HERE, "..", ".."))
                p = os.path.join(root, m.group(1) + ".md")
                if os.path.exists(p):
                    return self._send(200, {"html": md_to_html(open(p, encoding="utf-8").read())})
            return self._send(404, {"error": "not found"})
        except Exception as e:  # never leak a traceback to the client
            sys.stderr.write("error: %r\n" % (e,))
            return self._send(500, {"error": "internal error"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/demo":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 20000:
                return self._send(413, {"error": "payload too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            from .demo_world import run_scenario
            with STATE.lock:   # the demo mutates module counters; serialise it
                return self._send(200, run_scenario(body if isinstance(body, dict) else {}))
        except Exception as e:
            sys.stderr.write("demo error: %r\n" % (e,))
            return self._send(400, {"error": "could not run that scenario"})


def serve(host: str = "0.0.0.0", port: int = None, run_dir: str = None):
    global STATE
    STATE = State(run_dir or find_run_dir())
    port = port or int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer((host, port), H)
    print("serving %d decisions / %d viewers from %s on http://%s:%d" % (len(STATE.decisions), len(STATE.index), STATE.run, host, port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve()
