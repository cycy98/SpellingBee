"""Spelling Bee — auth routes (register, login, logout)."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from starlette.routing import Route

from backend import db
from backend.auth import (
    get_current_user,
    hash_password,
    set_auth_cookie,
    verify_password,
)
from backend.errors import HtmxError
from templating import client_ip, tpl

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import HTMLResponse


async def register(request: Request) -> HTMLResponse:
    state = request.app.state.srv
    ip = client_ip(request)
    if not state.check_rate(ip, "register"):
        msg = "Too many attempts. Try again later."
        raise HtmxError(msg, 429)

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    async def _err(msg: str) -> HTMLResponse:
        return await tpl(request, "fragments/menu.html", {"auth_error": msg})

    if not re.match(r"^[A-Za-z0-9_]{3,24}$", username):
        return await _err("Username must be 3-24 characters (letters, numbers, underscore).")

    if len(password) < 8 or len(password) > 128:
        return await _err("Password must be 8-128 characters.")

    async with db.transaction() as conn:
        cursor = await conn.execute("SELECT 1 FROM users WHERE username = ?", (username,))
        if await cursor.fetchone():
            return await _err("Could not create account. Try a different username.")
        pw_hash = await hash_password(password)
        await conn.execute(
            "INSERT INTO users (username, pw_hash) VALUES (?, ?)",
            (username, pw_hash),
        )

    resp = await tpl(request, "fragments/menu.html", {"user": username, "elo": 1000.0})
    set_auth_cookie(resp, username)
    resp.headers["HX-Trigger"] = json.dumps({"auth-changed": {"user": username}})
    return resp


async def login(request: Request) -> HTMLResponse:
    state = request.app.state.srv
    ip = client_ip(request)
    if not state.check_rate(ip, "login"):
        msg = "Too many attempts. Try again later."
        raise HtmxError(msg, 429)

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    row = await db.fetchone("SELECT username, pw_hash FROM users WHERE username = ?", (username,))

    if not row or not await verify_password(password, row["pw_hash"]):
        return await tpl(
            request,
            "fragments/menu.html",
            {"auth_error": "Unknown username or password."},
        )

    stats_row = await db.fetchone("SELECT elo FROM user_stats WHERE username=?", (row["username"],))
    elo = float(stats_row["elo"]) if stats_row else 1000.0
    resp = await tpl(request, "fragments/menu.html", {"user": row["username"], "elo": elo})
    set_auth_cookie(resp, row["username"])
    resp.headers["HX-Trigger"] = json.dumps({"auth-changed": {"user": row["username"]}})
    return resp


async def set_password(request: Request) -> HTMLResponse:
    logged_in_user = get_current_user(request)
    if not logged_in_user:
        msg = "Login required."
        raise HtmxError(msg, 403)

    state = request.app.state.srv
    ip = client_ip(request)
    if not state.check_rate(ip, "login"):
        msg = "Too many attempts. Try again later."
        raise HtmxError(msg, 429)

    form = await request.form()
    old_password = str(form.get("old_password", ""))
    new_password = str(form.get("password", ""))

    async def _err(msg: str) -> HTMLResponse:
        return await tpl(request, "fragments/menu.html", {"user": logged_in_user, "pw_error": msg})

    if len(new_password) < 8 or len(new_password) > 128:
        return await _err("Password must be 8-128 characters.")

    row = await db.fetchone("SELECT pw_hash FROM users WHERE username=?", (logged_in_user,))
    if not row:
        msg = "Account not found."
        raise HtmxError(msg, 404)

    if not await verify_password(old_password, row["pw_hash"]):
        return await _err("Current password is incorrect.")

    pw_hash = await hash_password(new_password)
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE users SET pw_hash = ? WHERE username = ?",
            (pw_hash, logged_in_user),
        )

    stats_row = await db.fetchone("SELECT elo FROM user_stats WHERE username=?", (logged_in_user,))
    elo = float(stats_row["elo"]) if stats_row else 1000.0
    resp = await tpl(request, "fragments/menu.html", {"user": logged_in_user, "elo": elo})
    set_auth_cookie(resp, logged_in_user)
    resp.headers["HX-Trigger"] = json.dumps({"auth-changed": {"user": logged_in_user}})
    return resp


async def logout(request: Request) -> HTMLResponse:
    resp = await tpl(request, "fragments/menu.html", {"user": None})
    resp.delete_cookie("auth", path="/")
    resp.headers["HX-Trigger"] = json.dumps({"auth-changed": {"user": None}})
    return resp


routes = [
    Route("/register", register, methods=["POST"]),
    Route("/login", login, methods=["POST"]),
    Route("/set-password", set_password, methods=["POST"]),
    Route("/logout", logout, methods=["POST"]),
]
