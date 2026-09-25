"""Optional password protection (single shared password, signed session cookie)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

COOKIE_NAME = "wwp_session"
SESSION_TTL = 30 * 86400
PUBLIC_PATHS = {"/login", "/healthz", "/static/styles.css", "/favicon.svg"}


def load_secret(configured: str, data_dir: Path) -> bytes:
    if configured:
        return configured.encode("utf-8")
    path = data_dir / "session_secret"
    if path.is_file():
        return path.read_bytes().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32).encode("ascii")
    path.write_bytes(value)
    return value


def make_token(secret: bytes, password: str) -> str:
    # Binding the token to the password invalidates sessions when the password changes.
    exp = int(time.time()) + SESSION_TTL
    msg = f"{exp}".encode()
    key = secret + hashlib.sha256(password.encode("utf-8")).digest()
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_token(secret: bytes, password: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, sig = token.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    key = secret + hashlib.sha256(password.encode("utf-8")).digest()
    expected = hmac.new(key, exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def check_password(configured: str, supplied: str) -> bool:
    return hmac.compare_digest(configured.encode("utf-8"), (supplied or "").encode("utf-8"))
