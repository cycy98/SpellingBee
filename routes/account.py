"""Spelling Bee — account & leaderboard routes."""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta
from typing import TYPE_CHECKING, Annotated, Any

_DISCORD_ID = os.environ.get("DISCORD_ID")

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from backend import db, stats
from backend.auth import ADMIN_USERS, discord_link_code, get_current_user
from backend.errors import HtmxError
from backend.progression import compute_progression
from templating import PICO_THEMES, set_theme, tpl

if TYPE_CHECKING:
    from backend.game import Catalog

router = APIRouter()


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request, sort: str = "elo") -> HTMLResponse:
    if sort not in {"elo", "games", "wins", "correct", "best_wpm", "best_streak"}:
        sort = "elo"
    players = await stats.get_leaderboard(sort=sort, limit=100)
    return await tpl(
        request,
        "fragments/leaderboard.html",
        {"players": players, "sort": sort},
    )


async def _account_ctx(username: str, catalog: Catalog, row: db.Row) -> dict[str, Any]:
    profile = stats.row_to_user_stats(row)
    data, study = await asyncio.gather(
        stats.fetch_account_page(username),
        stats.get_study_list(username, catalog),
    )
    progression = compute_progression(profile)
    today_date = date.today()  # noqa: DTZ011
    today_str = today_date.isoformat()
    hm_rows = data.pop("heatmap_rows")
    days_map = {r["day"]: int(r["n"]) for r in hm_rows}
    hm_max = max(days_map.values(), default=1) or 1
    heatmap_days = []
    for i in range(89, -1, -1):
        ds = (today_date - timedelta(days=i)).isoformat()
        n = days_map.get(ds, 0)
        pct = n / hm_max
        heatmap_days.append(
            {
                "date": ds,
                "n": n,
                "level": 0 if n == 0 else (3 if pct > 0.6 else (2 if pct > 0.25 else 1)),
                "today": ds == today_str,
            },
        )
    return {
        **data,
        "practice": [{"word": s.word, "n": s.n, "definition": s.definition} for s in study],
        "badges": [(b.name, b.icon) for b in progression.earned_badges],
        "discord_id": _DISCORD_ID,
        "discord_link_code": discord_link_code(username),
        "heatmap_days": heatmap_days,
        "member_since": profile.member_since,
    }


@router.get("/account/{username}", response_class=HTMLResponse)
async def account_view(request: Request, username: str) -> HTMLResponse:
    row = await db.fetchone("SELECT * FROM user_stats WHERE username = ?", (username,))
    if not row:
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


@router.post("/account/settings", response_class=HTMLResponse)
async def update_settings(request: Request, theme: Annotated[str, Form()]) -> Response:
    user = get_current_user(request)
    if not user:
        msg = "Not logged in."
        raise HtmxError(msg, 403)
    if theme not in PICO_THEMES:
        msg = "Invalid theme."
        raise HtmxError(msg, 400)
    async with db.transaction() as conn:
        await conn.execute("UPDATE users SET theme=? WHERE username=?", (theme, user))
    set_theme(user, theme)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/admin/user/{username}/edit", response_class=HTMLResponse)
async def admin_edit_user(
    request: Request,
    username: str,
    bio: Annotated[str, Form()] = "",
    visibility: Annotated[str, Form()] = "public",
    suspended_until: Annotated[str, Form()] = "",
) -> HTMLResponse:
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
    row = await db.fetchone("SELECT * FROM user_stats WHERE username=?", (username,))
    if not row:
        msg = "Player not found."
        raise HtmxError(msg, 404)
    ctx = {"player": row, **(await _account_ctx(username, catalog, row))}
    return await tpl(request, "fragments/account.html", ctx)


@router.post("/admin/user/{username}/delete")
async def admin_delete_user(request: Request, username: str) -> Response:
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


@router.get("/stats/{username}", response_class=HTMLResponse)
async def stats_view(request: Request, username: str) -> Response:
    root = request.scope.get("root_path", "")
    return Response(
        status_code=302,
        headers={"Location": f"{root}/account/{username}"},
    )


@router.get("/account", response_class=HTMLResponse)
async def own_account(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if not user:
        msg = "Not logged in."
        raise HtmxError(msg, 403)
    row = await db.fetchone("SELECT * FROM user_stats WHERE username = ?", (user,))
    if not row:
        msg = "Player not found."
        raise HtmxError(msg, 404)
    catalog: Catalog = request.app.state.srv.catalog
    return await tpl(
        request,
        "fragments/account.html",
        {"player": row, **(await _account_ctx(user, catalog, row))},
    )
