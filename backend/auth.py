from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
import warnings
from typing import TYPE_CHECKING

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

_ph = PasswordHasher(memory_cost=204800, time_cost=3, parallelism=4)

_raw_secret = os.environ.get("AUTH_SECRET")
if _raw_secret:
    SECRET = bytes.fromhex(_raw_secret)
else:
    warnings.warn(
        "AUTH_SECRET not set — sessions won't survive restarts or scale across workers. "
        'Generate one with: python -c "import secrets; print(secrets.token_bytes(32).hex())"',
        stacklevel=1,
    )
    SECRET = secrets.token_bytes(32)

# Set COOKIE_SECURE=true in production (HTTPS). Leave unset for local HTTP dev.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")

# Comma-separated usernames granted admin privileges, e.g. ADMIN_USERS=alice,bob
ADMIN_USERS: frozenset[str] = frozenset(
    u.strip() for u in os.environ.get("ADMIN_USERS", "").split(",") if u.strip()
)

COOKIE_MAX_AGE = 3 * 24 * 3600


def is_legacy_hash(stored: str) -> bool:
    return not stored.startswith("$argon2")


async def hash_password(pw: str) -> str:
    return await asyncio.to_thread(_ph.hash, pw)


async def verify_password(pw: str, stored: str) -> bool:
    if is_legacy_hash(stored):
        return await asyncio.to_thread(_verify_scrypt, pw, stored)
    try:
        return await asyncio.to_thread(_ph.verify, stored, pw)
    except Argon2Error:
        return False


def _verify_scrypt(pw: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(h.hex(), hash_hex)
    except (ValueError, OSError):
        return False


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def make_token(username: str) -> str:
    expiry = str(int(time.time()) + COOKIE_MAX_AGE)
    payload = f"{username}|{expiry}"
    sig = hmac.new(SECRET, payload.encode(), "sha256").hexdigest()
    return f"{payload}|{sig}"


def verify_token(token: str) -> str | None:
    parts = token.rsplit("|", 2)
    if len(parts) != 3:
        return None
    username, expiry_str, sig = parts
    if not _USERNAME_RE.match(username):
        return None
    try:
        if int(expiry_str) < time.time():
            return None
    except ValueError:
        return None
    expected = hmac.new(SECRET, f"{username}|{expiry_str}".encode(), "sha256").hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return username


_DISCORD_LINK_BIN = 600  # 10-minute windows


def discord_link_code(username: str) -> str:
    bin_ = int(time.time()) // _DISCORD_LINK_BIN
    return hmac.new(SECRET, f"discord:{username}:{bin_}".encode(), "sha256").hexdigest()[:8]


def verify_discord_link_code(username: str, code: str) -> bool:
    bin_ = int(time.time()) // _DISCORD_LINK_BIN
    for b in (bin_, bin_ - 1):
        expected = hmac.new(SECRET, f"discord:{username}:{b}".encode(), "sha256").hexdigest()[:8]
        if hmac.compare_digest(expected, code):
            return True
    return False


def get_current_user(request: Request) -> str | None:
    token = request.cookies.get("auth")
    if not token:
        return None
    return verify_token(token)


def _session_cookie_kwargs() -> dict:
    return {"httponly": True, "samesite": "lax", "secure": COOKIE_SECURE, "path": "/"}


def set_auth_cookie(response: Response, username: str) -> None:
    # SameSite=Lax blocks cross-origin POSTs, which is sufficient CSRF protection
    # for these endpoints — no additional CSRF tokens are needed.
    response.set_cookie(
        "auth",
        make_token(username),
        max_age=COOKIE_MAX_AGE,
        **_session_cookie_kwargs(),
    )


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        "session_id",
        session_id,
        max_age=COOKIE_MAX_AGE,
        **_session_cookie_kwargs(),
    )
