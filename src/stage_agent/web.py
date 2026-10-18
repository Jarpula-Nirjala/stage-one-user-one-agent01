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
import time
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
    """Headings, tables, bullet and numbered lists (wrapped lines joined), paragraphs, bold/italic/code. Everything escaped."""
    lines = md.split("\n")
    out, i, n = [], 0, len(lines)
    structural = re.compile(r"^(#{1,4}\s|\||```)")
    item = re.compile(r"^(\s*)(?:[-*]|\d+\.)\s+(.*)")
    while i < n:
        ln = lines[i]
        if ln.startswith("|") and i + 1 < n and re.match(r"^\|[\s\-|:]+\|?$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            out.append("<div class=tw><table><thead><tr>" + "".join("<th>%s</th>" % _inline(c) for c in head) + "</tr></thead><tbody>")
            i += 2
            while i < n and lines[i].startswith("|"):
                out.append("<tr>" + "".join("<td>%s</td>" % _inline(c.strip()) for c in lines[i].strip().strip("|").split("|")) + "</tr>")
                i += 1
            out.append("</tbody></table></div>")
            continue
        if ln.strip().startswith("```"):   # fenced code block: keep line breaks, escape everything
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre>%s</pre>" % html.escape("\n".join(code)))
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            k = len(m.group(1)) + 1
            out.append("<h%d>%s</h%d>" % (k, _inline(m.group(2)), k))
            i += 1
            continue
        m = item.match(ln)
        if m:
            ordered = bool(re.match(r"^\s*\d+\.", ln))
            out.append("<ol>" if ordered else "<ul>")
            while i < n and item.match(lines[i]):
                text = [item.match(lines[i]).group(2)]
                i += 1
                while i < n and lines[i].strip() and not item.match(lines[i]) and not structural.match(lines[i]):
                    text.append(lines[i].strip())
                    i += 1
                out.append("<li>%s</li>" % _inline(" ".join(text)))
                while i < n and not lines[i].strip() and i + 1 < n and item.match(lines[i + 1]):
                    i += 1
            out.append("</ol>" if ordered else "</ul>")
            continue
        if not ln.strip():
            i += 1
            continue
        para = [ln.strip()]
        i += 1
        while i < n and lines[i].strip() and not structural.match(lines[i]) and not item.match(lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % _inline(" ".join(para)))
    return "\n".join(out)


def parse_limits(md: str):
    """KNOWN_LIMITATIONS.md -> [{n, title, body}] for the timeline view."""
    items, cur = [], None
    for ln in md.split("\n"):
        m = re.match(r"^(\d+)\.\s+\*\*(.+?)\*\*\s*(.*)", ln)
        if m:
            cur = {"n": int(m.group(1)), "title": m.group(2).strip().rstrip("."), "body": m.group(3).strip()}
            items.append(cur)
        elif cur is not None and ln.strip() and not ln.startswith("#"):
            cur["body"] = (cur["body"] + " " + ln.strip()).strip()
    return items


class State:
    """Low-memory state: only a light row per decision is kept in RAM; full decisions are read from disk by byte offset."""

    def __init__(self, run: str):
        from collections import Counter
        self.run = run
        self.lock = threading.Lock()
        self.path = os.path.join(run, "decisions.jsonl")
        self.rows, self.off, self.by_viewer = [], {}, {}
        st, goals = Counter(), Counter()
        with open(self.path, "rb") as f:
            pos = 0
            for raw in f:
                n = len(raw)
                if raw.strip():
                    d = json.loads(raw)
                    did = d["decision_id"]
                    g = (d.get("goal") or {}).get("id")
                    self.off[did] = (pos, n)
                    self.by_viewer.setdefault(d["viewer_id"], []).append(did)
                    self.rows.append({"id": did, "viewer": d["viewer_id"], "arm": d.get("arm"), "status": d["status"], "action": d["action"], "goal": g,
                                      "conf": d.get("confidence"), "lat": d.get("latency_s"), "at": d["trigger"]["deliver_at"],
                                      "reason": (d.get("no_action_reason") or "")[:160]})
                    st[d["status"]] += 1
                    if d["status"] == "PUBLISHED":
                        goals[str(g)] += 1
                pos += n
        self._st, self._goals = st, goals
        self.index = json.load(open(os.path.join(run, "viewer_state", "_index.json"), encoding="utf-8"))
        self.summary = self._summary()

    def decision(self, did):
        o = self.off.get(did)
        if not o:
            return None
        with open(self.path, "rb") as f:
            f.seek(o[0])
            return json.loads(f.read(o[1]))

    def _j(self, name):
        p = os.path.join(self.run, name)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}

    def _summary(self):
        from collections import Counter
        lat, imp, saf = self._j("latency_report.json"), self._j("impact_summary.json"), self._j("safety_audit.json")
        outs = Counter()
        p = os.path.join(self.run, "outcomes.jsonl")
        if os.path.exists(p):
            for l in open(p, encoding="utf-8"):
                outs[json.loads(l)["outcome"]] += 1
        return {"status": dict(self._st), "goals_published": dict(self._goals), "outcomes": dict(outs),
                "viewers": len(self.index), "decisions": len(self.rows), "latency": {k: lat.get(k) for k in ("live_visits", "published_decisions", "over_slo_60s", "over_5min_failures", "budgets", "circuit_breaker_trips")},
                "impact": {"primary": imp.get("primary"), "stratified": imp.get("stratified"), "cace": imp.get("cace"), "by_goal": imp.get("by_goal"), "seeds": imp.get("seeds"),
                           "pilot": imp.get("pilot"), "naive": imp.get("naive")},
                "safety": {k: saf.get(k) for k in ("actions_audited", "by_type", "violations", "clean")}}

    def list_decisions(self, q):
        status, arm, goal, text = q.get("status", [""])[0], q.get("arm", [""])[0], q.get("goal", [""])[0], q.get("q", [""])[0].lower()
        try:
            off, lim = max(0, int(q.get("offset", ["0"])[0] or 0)), max(1, min(100, int(q.get("limit", ["30"])[0] or 30)))
        except ValueError:
            off, lim = 0, 30
        rows = [r for r in self.rows if (not status or r["status"] == status) and (not arm or r["arm"] == arm) and (not goal or r["goal"] == goal)
                and (not text or text in (r["viewer"] + " " + r["id"] + " " + r["reason"]).lower())]
        return {"total": len(rows), "rows": rows[off: off + lim]}

    def viewer(self, vid):
        p = os.path.join(self.run, "viewer_state", vid + ".json")
        if not re.fullmatch(r"[A-Za-z0-9\-_]+", vid) or not os.path.exists(p):
            return None
        st = json.load(open(p, encoding="utf-8"))
        st["decisions"] = [self.decision(i) for i in self.by_viewer.get(vid, [])]
        return st


STATE = None
AUTH = None
PUBLIC_PATHS = ("/signin", "/healthz", "/api/login", "/api/me")


class H(BaseHTTPRequestHandler):
    server_version = "stage-agent"

    def log_message(self, fmt, *a):
        if os.environ.get("QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None, cache=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str)
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", cache or "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid":
                return v
        return None

    def _session(self):
        return AUTH.read_cookie(self._cookie())

    def _ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def _set_cookie(self, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return {"Set-Cookie": "sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s" % (value, max_age, secure)}

    def _redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        try:
            if path == "/healthz":
                return self._send(200, {"ok": True})
            m = re.fullmatch(r"/static/img/([a-z\-]+\.jpg)", path)
            if m:   # public: the sign-in page needs its photo before anyone is signed in
                fp = os.path.join(STATIC, "img", m.group(1))
                if os.path.exists(fp):
                    return self._send(200, open(fp, "rb").read(), "image/jpeg", cache="public, max-age=86400")
                return self._send(404, {"error": "not found"})
            sess = self._session()
            if path == "/signin":
                if sess:
                    return self._redirect("/")
                return self._send(200, open(os.path.join(STATIC, "signin.html"), encoding="utf-8").read().replace("__GUEST__", "1" if AUTH.allow_guest else "0"), "text/html; charset=utf-8")
            if path == "/api/me":
                return self._send(200, {"signed_in": bool(sess), "role": sess[0] if sess else None, "name": sess[1] if sess else None})
            if not sess:   # everything else needs a session (user or guest)
                if path.startswith("/api/"):
                    return self._send(401, {"error": "sign in required"})
                return self._redirect("/signin")
            if path in ("/", "/index.html"):
                return self._send(200, open(os.path.join(STATIC, "index.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
            if path == "/dashboard":
                return self._send(200, open(os.path.join(STATE.run, "dashboard.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
            if path == "/api/summary":
                return self._send(200, STATE.summary)
            if path == "/api/limits":
                root = os.path.abspath(os.path.join(HERE, "..", ".."))
                return self._send(200, parse_limits(open(os.path.join(root, "KNOWN_LIMITATIONS.md"), encoding="utf-8").read()))
            if path == "/api/decisions":
                return self._send(200, STATE.list_decisions(q))
            m = re.fullmatch(r"/api/decision/([A-Za-z0-9\-]+)", path)
            if m:
                d = STATE.decision(m.group(1))
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

    def _body(self, limit=20000):
        n = int(self.headers.get("Content-Length", "0"))
        if n > limit:
            raise ValueError("payload too large")
        v = json.loads(self.rfile.read(n) or b"{}")
        return v if isinstance(v, dict) else {}

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                ip = self._ip()
                if AUTH.blocked(ip):
                    return self._send(429, {"error": "too many attempts; try again in a few minutes"})
                b = self._body(2000)
                if b.get("guest"):
                    if not AUTH.allow_guest:
                        return self._send(403, {"error": "guest access is disabled"})
                    return self._send(200, {"ok": True, "role": "guest"}, extra=self._set_cookie(AUTH.make_cookie("guest", "Guest"), AUTH.ttl))
                ident, pw = str(b.get("username", ""))[:200], str(b.get("password", ""))[:200]
                name = AUTH.username if AUTH.check(ident, pw) else (AUTH.users.verify(ident, pw) if AUTH.users else None)
                if name:
                    AUTH.ok(ip)
                    return self._send(200, {"ok": True, "role": "user", "name": name}, extra=self._set_cookie(AUTH.make_cookie("user", name), AUTH.ttl))
                AUTH.fail(ip)
                time.sleep(0.4)   # slow down guessing
                known = AUTH.users and ident.strip().lower() in AUTH.users.users
                return self._send(401, {"error": "incorrect password for this account" if known else "no account matches that username or email: create one below"})
            if path == "/api/register":
                ip = self._ip()
                if AUTH.reg_blocked(ip):
                    return self._send(429, {"error": "too many sign-up attempts from this network; try again later"})
                if not AUTH.users:
                    return self._send(503, {"error": "sign-up is unavailable"})
                b = self._body(2000)
                AUTH.reg_hit(ip)
                ok, msg = AUTH.users.register(str(b.get("name", "")), str(b.get("email", "")), str(b.get("password", "")))
                if not ok:
                    return self._send(400, {"error": msg})
                nm = str(b.get("name", "")).strip()[:60]
                return self._send(200, {"ok": True, "role": "user", "name": nm}, extra=self._set_cookie(AUTH.make_cookie("user", nm), AUTH.ttl))
            if path == "/api/logout":
                return self._send(200, {"ok": True}, extra=self._set_cookie("", 0))
            sess = self._session()
            if not sess:
                return self._send(401, {"error": "sign in required"})
            if path == "/api/demo":
                if sess[0] != "user":
                    return self._send(403, {"error": "the live demo needs a signed-in account; guests are read-only"})
                from .demo_world import run_scenario
                body = self._body()
                with STATE.lock:   # serialise: keeps CPU and memory bounded
                    return self._send(200, run_scenario(body))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            sys.stderr.write("post error: %r\n" % (e,))
            return self._send(400, {"error": "bad request"})


def serve(host: str = "0.0.0.0", port: int = None, run_dir: str = None):
    global STATE, AUTH
    from .auth import Auth
    from .users import UserStore
    STATE, AUTH = State(run_dir or find_run_dir()), Auth()
    AUTH.users = UserStore()
    port = port or int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer((host, port), H)
    print("serving %d decisions / %d viewers from %s on http://%s:%d" % (len(STATE.rows), len(STATE.index), STATE.run, host, port), flush=True)
    if AUTH.generated:
        print("APP_PASSWORD not set: temporary credentials for this run -> username: %s  password: %s" % (AUTH.username, AUTH.password), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve()
