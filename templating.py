"""Spelling Bee — Jinja2 template helpers shared across route modules."""

from __future__ import annotations

import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi.templating import Jinja2Templates

from backend import db
from backend.auth import ADMIN_USERS, get_current_user

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import HTMLResponse

PICO_THEMES = frozenset(
    [
        "amber",
        "blue",
        "cyan",
        "fuchsia",
        "green",
        "indigo",
        "jade",
        "lime",
        "orange",
        "pink",
        "pumpkin",
        "purple",
        "red",
        "rose",
        "sand",
        "slate",
        "violet",
        "yellow",
        "zinc",
    ],
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.autoescape = True  # type: ignore[assignment]


def _name_color(name: str) -> str:
    h = 5381
    for c in name:
        h = ((h << 5) + h) ^ ord(c)
        h &= 0xFFFFFFFF  # mirror JS 32-bit coercion
    return f"hsl({h % 360}, 65%, 55%)"


templates.env.filters["name_color"] = _name_color
templates.env.globals["pico_themes"] = sorted(PICO_THEMES)


def _relative_time(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


templates.env.filters["relative_time"] = _relative_time


def client_ip(request: Request) -> str:
    # We always run behind nginx
    forwarded = request.headers.get("x-real-ip")
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host
    return "unknown"


_THEME_CACHE_MAX = 256
_theme_cache: OrderedDict[str, str] = OrderedDict()


async def _user_theme(username: str | None) -> str:
    if not username:
        return "amber"
    if username in _theme_cache:
        _theme_cache.move_to_end(username)
        return _theme_cache[username]
    row = await db.fetchone("SELECT theme FROM users WHERE username=?", (username,))
    theme = row["theme"] if row and row["theme"] in PICO_THEMES else "amber"
    _theme_cache[username] = theme
    if len(_theme_cache) > _THEME_CACHE_MAX:
        _theme_cache.popitem(last=False)
    return theme


def set_theme(username: str, theme: str) -> None:
    _theme_cache[username] = theme
    _theme_cache.move_to_end(username)


_SENTINEL = object()


async def tpl(
    request: Request,
    name: str,
    ctx: dict[str, Any] | None = None,
    *,
    user: str | None = _SENTINEL,  # type: ignore[assignment]
) -> HTMLResponse:
    if user is _SENTINEL:
        user = get_current_user(request)
    c: dict[str, Any] = {
        "request": request,
        "user": user,
        "pico_theme": await _user_theme(user),
        "is_admin": user in ADMIN_USERS,
    }
    if ctx:
        c.update(ctx)
    return templates.TemplateResponse(request, name, c)
