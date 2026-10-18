"""Tiny account store for sign-up: JSON file, salted PBKDF2-HMAC-SHA256 hashes, atomic writes, hard size cap.

Location: $DATA_DIR/users.json (default ./data). On Render's free plan the disk is ephemeral (it is wiped on redeploy), so
accounts persist across sleeps but not across new deploys; mount a persistent disk or use the admin account for permanence."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Optional, Tuple

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,}$")
ITER = 200_000
MAX_USERS = 500


def _hash(password: str, salt: bytes, iters: int = ITER) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters).hex()


class UserStore:
    def __init__(self, data_dir: Optional[str] = None):
        self.dir = data_dir or os.environ.get("DATA_DIR") or os.path.join(os.getcwd(), "data")
        self.path = os.path.join(self.dir, "users.json")
        self.lock = threading.Lock()
        self.users = {}
        try:
            self.users = json.load(open(self.path, encoding="utf-8"))
        except Exception:
            self.users = {}

    @staticmethod
    def validate(name: str, email: str, password: str) -> Optional[str]:
        if not (1 <= len(name.strip()) <= 60):
            return "please enter your name (up to 60 characters)"
        if not EMAIL_RE.match(email):
            return "please enter a valid email address"
        if len(password) < 8:
            return "password must be at least 8 characters"
        if len(password) > 128:
            return "password is too long"
        if password.lower() in (email.lower(), email.split("@")[0].lower()):
            return "password must not be your email"
        if len(set(password)) < 4:
            return "password is too simple"
        return None

    def register(self, name: str, email: str, password: str) -> Tuple[bool, str]:
        email = email.strip().lower()
        err = self.validate(name, email, password)
        if err:
            return False, err
        with self.lock:
            if email in self.users:
                return False, "an account with this email already exists: sign in instead"
            if len(self.users) >= MAX_USERS:
                return False, "registrations are closed (limit reached)"
            salt = secrets.token_bytes(16)
            self.users[email] = {"name": name.strip()[:60], "salt": salt.hex(), "iters": ITER, "hash": _hash(password, salt), "created": int(time.time())}
            self._save()
        return True, "ok"

    def verify(self, email: str, password: str) -> Optional[str]:
        """Return the display name if the credentials are right. Always does one hash so timing does not reveal unknown emails."""
        u = self.users.get(email.strip().lower())
        salt = bytes.fromhex(u["salt"]) if u else b"\x00" * 16
        h = _hash(password, salt, u["iters"] if u else ITER)
        if u and hmac.compare_digest(h, u["hash"]):
            return u["name"]
        return None

    def _save(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.users, f)
        os.replace(tmp, self.path)
