"""Sign-in: signed cookies cannot be forged, credentials come from the environment, brute force is throttled."""
import threading
import time
import urllib.error
import urllib.request
import json
from http.server import ThreadingHTTPServer

from stage_agent.auth import Auth


def mk(**env):
    base = {"APP_USERNAME": "jarpula", "APP_PASSWORD": "s3cret-pass", "SECRET_KEY": "k" * 32}
    base.update(env)
    return Auth(base)


def test_good_and_bad_credentials():
    a = mk()
    assert a.check("jarpula", "s3cret-pass") and not a.check("jarpula", "nope") and not a.check("x", "s3cret-pass")
    assert not a.generated


def test_unset_password_generates_a_random_one_not_a_default():
    a, b = Auth({}), Auth({})
    assert a.generated and a.password != b.password and len(a.password) >= 8


def test_cookie_roundtrip_expiry_and_tamper():
    a = mk()
    c = a.make_cookie("user", "jarpula")
    assert a.read_cookie(c) == ("user", "jarpula")
    assert a.read_cookie(c.replace("user", "guest", 1)) is None            # role swap breaks the MAC
    assert a.read_cookie(c[:-1] + ("0" if c[-1] != "0" else "1")) is None   # flipped MAC
    assert a.read_cookie(c, now=time.time() + a.ttl + 5) is None            # expired
    assert a.read_cookie("garbage") is None and a.read_cookie(None) is None
    assert mk(SECRET_KEY="z" * 32).read_cookie(c) is None                   # a different server secret rejects it


def test_five_failures_block_an_ip_then_success_clears_it():
    a = mk()
    for _ in range(5):
        assert not a.blocked("1.2.3.4")
        a.fail("1.2.3.4")
    assert a.blocked("1.2.3.4") and not a.blocked("5.6.7.8")
    assert not a.blocked("1.2.3.4", now=time.time() + 301)
    a.ok("1.2.3.4")
    assert not a.blocked("1.2.3.4")


def test_http_flow_gate_login_guest_and_demo_permission(tmp_path):
    import stage_agent.web as web
    from stage_agent.auth import Auth as A
    run = tmp_path
    (run / "viewer_state").mkdir()
    (run / "viewer_state" / "_index.json").write_text("{}")
    (run / "decisions.jsonl").write_text("")
    web.STATE, web.AUTH = web.State(str(run)), A({"APP_USERNAME": "jarpula", "APP_PASSWORD": "pw12345678", "SECRET_KEY": "s" * 32})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect)

    def call(path, data=None, cookie=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
        try:
            r = op.open(req)
            return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers
    try:
        assert call("/")[0] == 302                                             # gated
        assert call("/api/summary")[0] == 401
        assert call("/signin")[0] == 200 and call("/healthz")[0] == 200        # public
        assert call("/api/login", {"username": "jarpula", "password": "wrong"})[0] == 401
        code, _, h = call("/api/login", {"username": "jarpula", "password": "pw12345678"})
        assert code == 200 and "HttpOnly" in h["Set-Cookie"]
        user = h["Set-Cookie"].split(";")[0]
        assert call("/", cookie=user)[0] == 200
        code, _, h = call("/api/login", {"guest": True})
        guest = h["Set-Cookie"].split(";")[0]
        assert code == 200 and call("/", cookie=guest)[0] == 200
        status, body, _ = call("/api/demo", {"status": "lapsed"}, cookie=guest)
        assert status == 403 and "signed-in" in body                           # guests are read-only
        status, body, _ = call("/api/demo", {"status": "lapsed"}, cookie=user)
        assert status == 200 and "decision" in json.loads(body)
        assert call("/api/demo", {"status": "never"}, cookie="sid=forged~user~9999999999~abc")[0] == 401
    finally:
        srv.shutdown()
