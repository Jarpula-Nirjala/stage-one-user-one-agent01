"""Sign-up accounts, the markdown renderer and the limitations parser behind the timeline."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from stage_agent.auth import Auth
from stage_agent.users import UserStore
from stage_agent.web import md_to_html, parse_limits


def test_register_verify_duplicate_and_hashing(tmp_path):
    s = UserStore(str(tmp_path))
    ok, _ = s.register("Asha Rao", "Asha@Example.com", "goodpass123")
    assert ok
    raw = (tmp_path / "users.json").read_text()
    assert "goodpass123" not in raw and "asha@example.com" in raw                  # only a salted hash is stored, email lower-cased
    assert s.verify("ASHA@example.com", "goodpass123") == "Asha Rao"
    assert s.verify("asha@example.com", "wrong") is None and s.verify("nobody@x.com", "goodpass123") is None
    assert s.register("Other", "asha@example.com", "anotherpass1")[0] is False     # duplicate email
    assert UserStore(str(tmp_path)).verify("asha@example.com", "goodpass123") == "Asha Rao"   # survives a restart


def test_registration_validation(tmp_path):
    s = UserStore(str(tmp_path))
    for name, email, pw in [("", "a@b.co", "goodpass123"), ("A", "not-an-email", "goodpass123"), ("A", "a@b.co", "short"),
                            ("A", "a@b.co", "aaaaaaaaaa"), ("A", "a@b.co", "a@b.co")]:
        assert s.register(name, email, pw)[0] is False
    assert s.users == {}


def test_cookie_with_hostile_display_name_is_safe():
    a = Auth({"APP_PASSWORD": "x" * 10, "SECRET_KEY": "k" * 32})
    c = a.make_cookie("user", "Ann; Path=/~evil\r\nSet-Cookie: x=1")
    assert ";" not in c and "\n" not in c and "\r" not in c and " " not in c
    assert a.read_cookie(c) == ("user", "Ann; Path=/~evil\r\nSet-Cookie: x=1")


def test_markdown_joins_wrapped_lines_and_handles_numbered_lists():
    md = "1. **Mock model.** Goal advice comes from a\n   deterministic mock so evaluators need\n   no key.\n2. **Second.** Text.\n\nA paragraph that\nwraps here.\n"
    h = md_to_html(md)
    assert h.count("<li>") == 2 and "<ol>" in h and "no key." in h and "<b>Mock model.</b>" in h
    assert "<p>A paragraph that wraps here.</p>" in h
    assert "**" not in h


def test_limitations_parser_extracts_every_item():
    import os
    root = os.path.join(os.path.dirname(__file__), "..", "KNOWN_LIMITATIONS.md")
    items = parse_limits(open(root, encoding="utf-8").read())
    assert [i["n"] for i in items] == list(range(1, len(items) + 1)) and len(items) >= 12
    assert all(i["title"] and len(i["body"]) > 20 for i in items)


def test_signup_flow_over_http(tmp_path):
    import stage_agent.web as web
    run = tmp_path / "run"
    (run / "viewer_state").mkdir(parents=True)
    (run / "viewer_state" / "_index.json").write_text("{}")
    (run / "decisions.jsonl").write_text("")
    web.STATE = web.State(str(run))
    web.AUTH = Auth({"APP_PASSWORD": "adminpass123", "SECRET_KEY": "s" * 32})
    web.AUTH.users = UserStore(str(tmp_path / "data"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    def call(path, data=None, cookie=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
        try:
            r = urllib.request.urlopen(req)
            return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers
    try:
        assert call("/static/img/audience.jpg")[0] == 200                         # photos are public (the sign-in page uses one)
        for evil in ("/static/img/../../auth.py", "/static/img/..%2f..%2fauth.py", "/static/img/%2e%2e/%2e%2e/auth.py"):
            assert b"def _mac" not in call(evil)[1]                                   # no path traversal: source is never served
        code, body, _ = call("/api/register", {"name": "Asha", "email": "bad", "password": "goodpass123"})
        assert code == 400 and b"valid email" in body
        code, body, h = call("/api/register", {"name": "Asha Rao", "email": "asha@example.com", "password": "goodpass123"})
        cookie = h["Set-Cookie"].split(";")[0]
        assert code == 200 and json.loads(call("/api/me", cookie=cookie)[1])["name"] == "Asha Rao"
        assert call("/api/demo", {"status": "lapsed"}, cookie=cookie)[0] == 200   # a registered user may run the demo
        code, body, _ = call("/api/login", {"username": "nobody@example.com", "password": "x"})
        assert code == 401 and b"create one" in body                              # first-time visitors are pointed to sign-up
        assert call("/api/login", {"username": "asha@example.com", "password": "goodpass123"})[0] == 200
        code, body, _ = call("/api/login", {"username": "asha@example.com", "password": "nope"})
        assert code == 401 and b"incorrect password" in body
        assert call("/api/limits", cookie=cookie)[0] == 200 and len(json.loads(call("/api/limits", cookie=cookie)[1])) >= 12
    finally:
        srv.shutdown()


def test_fenced_code_block_is_preserved_and_escaped():
    h = md_to_html("Intro text\n\n```\na <b> | b\n   indented\n```\n\nAfter.\n")
    assert "<pre>a &lt;b&gt; | b\n   indented</pre>" in h and "<p>After.</p>" in h and "<code>" not in h
