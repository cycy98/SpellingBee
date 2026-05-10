"""Spelling Bee — account & leaderboard routes."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from backend import db, stats
from backend.auth import ADMIN_USERS, discord_link_code, get_current_user, verify_password
from backend.errors import HtmxError
from backend.progression import compute_progression
from templating import PICO_THEMES, set_theme, tpl

_DISCORD_ID = os.environ.get("DISCORD_ID")

if TYPE_CHECKING:
    from starlette.requests import Request

    from backend.game import Catalog


async def leaderboard(request: Request) -> HTMLResponse:
    sort = request.query_params.get("sort", "elo")
    if sort not in {"elo", "games", "wins", "correct", "best_wpm", "best_streak"}:
        sort = "elo"
    players = await stats.get_leaderboard(sort=sort, limit=100)
    return await tpl(
        request,
        "fragments/leaderboard.html",
        {"players": players, "sort": sort},
    )


async def _fetch_user_row(username: str) -> db.Row:
    row = await db.fetchone("SELECT * FROM user_stats WHERE username = ?", (username,))
    if not row:
        msg = "Player not found."
        raise HtmxError(msg, 404)
    return row


async def _account_ctx(username: str, catalog: Catalog, row: db.Row) -> dict[str, Any]:
    profile = stats.row_to_user_stats(row)
    data, study = await asyncio.gather(
        stats.fetch_account_page(username),
        stats.get_study_list(username, catalog),
    )
    progression = compute_progression(profile)
    win_pct = round(row["wins"] / row["games"] * 100, 1) if row["games"] > 0 else 0.0
    return {
        **data,
        "practice": [{"word": s.word, "n": s.n, "definition": s.definition} for s in study],
        "badges": [(b.name, b.icon) for b in progression.earned_badges],
        "discord_id": _DISCORD_ID,
        "discord_link_code": discord_link_code(username),
        "member_since": profile.member_since,
        "win_pct": win_pct,
    }


async def export_account(request: Request) -> JSONResponse:
    user = get_current_user(request)
    if not user:
        msg = "Not logged in."
        raise HtmxError(msg, 403)
    guesses, matches, summary = await asyncio.gather(
        db.fetchall(
            "SELECT word, correct, wpm, tier, streak, ts FROM guess_log WHERE username=? ORDER BY ts",
            (user,),
        ),
        db.fetchall(
            "SELECT match_id, rank, elo_after, ts FROM match_results WHERE username=? ORDER BY ts",
            (user,),
        ),
        db.fetchone("SELECT * FROM user_stats WHERE username=?", (user,)),
    )
    payload = {
        "username": user,
        "summary": dict(summary) if summary else {},
        "guess_log": [dict(r) for r in guesses],
        "match_results": [dict(r) for r in matches],
    }
    headers = {"Content-Disposition": f'attachment; filename="{user}-data.json"'}
    return JSONResponse(content=payload, headers=headers)


async def account_view(request: Request) -> HTMLResponse:
    username = request.path_params["username"]
    row = await _fetch_user_row(username)
    viewer = get_current_user(request)
    if row["visibility"] == "private" and viewer != username and viewer not in ADMIN_USERS:
        msg = "Player not found."
        raise HtmxError(msg, 404)
    catalog: Catalog = request.app.state.srv.catalog
    owner_theme = row["theme"] if row["theme"] in PICO_THEMES else "amber"
    return await tpl(
        request,
        "fragments/account.html",
        {
            "player": row,
            "pico_theme": owner_theme,
            **(await _account_ctx(username, catalog, row)),
        },
    )


async def update_settings(request: Request) -> Response:
    form = await request.form()
    theme = str(form.get("theme", ""))
    visibility = str(form.get("visibility", "public"))
    user = get_current_user(request)
    if not user:
        msg = "Not logged in."
        raise HtmxError(msg, 403)
    if theme not in PICO_THEMES:
        msg = "Invalid theme."
        raise HtmxError(msg, 400)
    if visibility not in ("public", "private"):
        msg = "Invalid visibility."
        raise HtmxError(msg, 400)
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE users SET theme=?, visibility=? WHERE username=?",
            (theme, visibility, user),
        )
    set_theme(user, theme)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


async def admin_edit_user(request: Request) -> HTMLResponse:
    form = await request.form()
    username = request.path_params["username"]
    bio = str(form.get("bio", ""))
    visibility = str(form.get("visibility", "public"))
    suspended_until = str(form.get("suspended_until", ""))
    caller = get_current_user(request)
    if caller not in ADMIN_USERS:
        msg = "Forbidden."
        raise HtmxError(msg, 403)
    if caller == username:
        msg = "Cannot edit your own account via admin."
        raise HtmxError(msg, 400)
    if visibility not in ("public", "private"):
        msg = "Invalid visibility."
        raise HtmxError(msg, 400)
    suspend_ts: int | None = None
    if suspended_until.strip():
        try:
            suspend_ts = int(suspended_until.strip())
        except ValueError:
            msg = "suspended_until must be a unix timestamp."
            raise HtmxError(msg, 400) from None
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE users SET bio=?, visibility=?, suspended_until=? WHERE username=?",
            (bio[:500], visibility, suspend_ts, username),
        )
    catalog: Catalog = request.app.state.srv.catalog
    row = await _fetch_user_row(username)
    ctx = {"player": row, **(await _account_ctx(username, catalog, row))}
    return await tpl(request, "fragments/account.html", ctx)


async def admin_delete_user(request: Request) -> Response:
    username = request.path_params["username"]
    caller = get_current_user(request)
    if caller not in ADMIN_USERS:
        msg = "Forbidden."
        raise HtmxError(msg, 403)
    if caller == username:
        msg = "Cannot delete your own account."
        raise HtmxError(msg, 400)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM users WHERE username=?", (username,))
    root = request.scope.get("root_path", "")
    return Response(status_code=200, headers={"HX-Redirect": f"{root}/leaderboard"})


async def delete_account(request: Request) -> Response:
    form = await request.form()
    password = str(form.get("password", ""))
    user = get_current_user(request)
    if not user:
        msg = "Not logged in."
        raise HtmxError(msg, 403)
    row = await db.fetchone("SELECT pw_hash FROM users WHERE username=?", (user,))
    if not row or not await verify_password(password, row["pw_hash"]):
        msg = "Incorrect password."
        raise HtmxError(msg, 400)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM users WHERE username=?", (user,))
    root = request.scope.get("root_path", "")
    resp = Response(status_code=200, headers={"HX-Redirect": f"{root}/"})
    resp.delete_cookie("auth", path="/")
    return resp


async def privacy(request: Request) -> HTMLResponse:
    return await tpl(request, "fragments/privacy.html", {})


async def stats_view(request: Request) -> Response:
    username = request.path_params["username"]
    root = request.scope.get("root_path", "")
    return Response(
        status_code=302,
        headers={"Location": f"{root}/account/{username}"},
    )


async def own_account(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if not user:
        msg = "Not logged in."
        raise HtmxError(msg, 403)
    row = await _fetch_user_row(user)
    catalog: Catalog = request.app.state.srv.catalog
    return await tpl(
        request,
        "fragments/account.html",
        {"player": row, **(await _account_ctx(user, catalog, row))},
    )


routes = [
    Route("/leaderboard", leaderboard),
    Route("/account/export", export_account),
    Route("/account/settings", update_settings, methods=["POST"]),
    Route("/account/delete", delete_account, methods=["POST"]),
    Route("/account/{username}", account_view),
    Route("/account", own_account),
    Route("/admin/user/{username}/edit", admin_edit_user, methods=["POST"]),
    Route("/admin/user/{username}/delete", admin_delete_user, methods=["POST"]),
    Route("/privacy", privacy),
    Route("/stats/{username}", stats_view),
]
