"""Small, dependency-free sign-in: HMAC-signed session cookie, env-configured credentials, per-IP brute-force throttle.

Environment:
  APP_USERNAME   default "jarpula"
  APP_PASSWORD   required in production. If unset a random one is generated and printed once to the server log.
  SECRET_KEY     signs session cookies; random per process if unset (sessions then end on restart)
  ALLOW_GUEST    "1" (default) shows a "Continue as guest" button: read-only access, no live demo
  SESSION_HOURS  default 12
No password is stored in the repository."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Optional, Tuple


class Auth:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.username = env.get("APP_USERNAME", "jarpula")
        pw = env.get("APP_PASSWORD")
        self.generated = pw is None or pw == ""
        self.password = secrets.token_urlsafe(9) if self.generated else pw
        self.secret = (env.get("SECRET_KEY") or secrets.token_hex(32)).encode()
        self.allow_guest = env.get("ALLOW_GUEST", "1") == "1"
        self.ttl = int(float(env.get("SESSION_HOURS", "12")) * 3600)
        self._fails = {}
        self._lock = threading.Lock()

    # ---- cookies
    def _mac(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()[:40]

    def make_cookie(self, role: str, name: str, now: Optional[float] = None) -> str:
        exp = int((now or time.time()) + self.ttl)
        payload = "%s|%s|%d" % (role, name, exp)
        return "%s~%s~%d~%s" % (role, name.replace("~", ""), exp, self._mac(payload))

    def read_cookie(self, value: Optional[str], now: Optional[float] = None) -> Optional[Tuple[str, str]]:
        if not value:
            return None
        parts = value.split("~")
        if len(parts) != 4:
            return None
        role, name, exp, mac = parts
        payload = "%s|%s|%s" % (role, name, exp)
        if not hmac.compare_digest(mac, self._mac(payload)):
            return None
        try:
            if int(exp) < (now or time.time()):
                return None
        except ValueError:
            return None
        return (role, name) if role in ("user", "guest") else None

    # ---- credentials and throttling
    def check(self, username: str, password: str) -> bool:
        u_ok = hmac.compare_digest(username.encode(), self.username.encode())
        p_ok = hmac.compare_digest(password.encode(), self.password.encode())
        return u_ok and p_ok

    def blocked(self, ip: str, now: Optional[float] = None) -> bool:
        now = now or time.time()
        with self._lock:
            hits = [t for t in self._fails.get(ip, []) if now - t < 300]
            self._fails[ip] = hits
            return len(hits) >= 5

    def fail(self, ip: str, now: Optional[float] = None):
        with self._lock:
            self._fails.setdefault(ip, []).append(now or time.time())
            if len(self._fails) > 5000:      # bound memory
                self._fails.clear()

    def ok(self, ip: str):
        with self._lock:
            self._fails.pop(ip, None)
