"""Spelling Bee — FastAPI HTTP shell."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from backend import db
from backend.auth import _session_cookie_kwargs, get_current_user, set_session_cookie
from backend.errors import HtmxError
from backend.game import (
    CHAT_BLOCKLIST,
    MAX_CHAT_LEN,
    MAX_E2EE_CHAT_LEN,
    MAX_LOCAL_PLAYERS,
    MAX_PLAYERS,
    MAX_SESSIONS_PER_IP,
    MAX_WORD_LEN,
    ROOM_CODE_LEN,
    ROOT,
    WORD_CHARS,
    Catalog,
    Room,
    Session,
    Visibility,
    clean_name,
    feedback,
    room_host_sid,
)
from backend.persistence import (
    is_name_reserved,
    load_highest_tier,
    record_guess_stats,
)
from backend.state import AppState
from discord_bot.bot import BotCore
from routes.account import router as account_router
from routes.auth import router as auth_router
from templating import client_ip, templates, tpl

try:
    from landlock import Ruleset
except ImportError:
    Ruleset = None

if TYPE_CHECKING:
    from starlette.responses import Response as StarletteResponse

_access_log = logging.getLogger("spelling.access")


class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Any) -> StarletteResponse:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# Config (HTTP-only)

DB_PATH = ROOT / "spellingbee.db"
MAX_BODY = 8 * 1024


# HTTP helpers


def get_session(state: AppState, request: Request) -> Session | None:
    sid = request.cookies.get("session_id")
    if not sid:
        return None
    return state.sessions.get(sid)


def require_session(
    state: AppState,
    request: Request,
    rate_key: str | None = None,
) -> Session:
    """Rate-check + session lookup. Raises HtmxError on failure."""
    if rate_key:
        ip = client_ip(request)
        if not state.check_rate(ip, rate_key):
            msg = "Too many attempts. Try again later."
            raise HtmxError(msg, 429)
    sess = get_session(state, request)
    if not sess:
        msg = "Invalid session."
        raise HtmxError(msg, 403)
    return sess


def require_room(
    state: AppState,
    request: Request,
    code: str,
    rate_key: str | None = None,
) -> tuple[Session, Room]:
    sess = require_session(state, request, rate_key)
    if sess.room_code != code:
        msg = "Not in this room."
        raise HtmxError(msg, 403)
    room = state.rooms.get(code)
    if not room:
        msg = "Room not found."
        raise HtmxError(msg, 404)
    return sess, room


async def resolve_session(
    state: AppState,
    request: Request,
    player_name: str,
    difficulty: str,
    ip: str,
    account: str | None = None,
    highest_tier: str = "",
) -> Session:
    """Return the caller's existing session (updating mutable fields) or create a fresh one.

    Existing session is reused when the cookie points to a live, roomless session,
    or to a session already assigned to a registered account matching `account`.
    """
    sid = request.cookies.get("session_id")
    sess = state.sessions.get(sid) if sid else None
    if sess and not sess.room_code:
        sess.player_name = player_name
        sess.difficulty = difficulty
        sess.ip = ip
        if account:
            sess.account_username = account
        if highest_tier:
            sess.highest_tier = highest_tier
        return sess
    return state.create_session(player_name, difficulty, ip, account=account, highest_tier=highest_tier)


def _session_already_in_room(
    state: AppState,
    room: Room,
    request: Request,
    account: str | None,
) -> Session | None:
    """Return an existing session that is already seated in this room, or None."""
    sid = request.cookies.get("session_id")
    if sid and sid in room.sessions:
        return state.sessions.get(sid)
    if account:
        for s in room.sessions:
            existing = state.sessions.get(s)
            if existing and existing.account_username == account:
                return existing
    return None


def check_creation_limits(state: AppState, request: Request) -> None:
    """Rate-check + stale purge + session-count guard. Raises HtmxError on failure."""
    ip = client_ip(request)
    if not state.check_rate(ip, "create_room"):
        msg = "Too many attempts. Try again later."
        raise HtmxError(msg, 429)
    state.purge_stale()
    if state.count_sessions_for_ip(ip) >= MAX_SESSIONS_PER_IP:
        msg = "Too many active sessions."
        raise HtmxError(msg, 429)


# Middleware


class BodyLimitMiddleware:
    """Pure ASGI middleware — zero overhead for non-POST / static / SSE requests.

    Enforces MAX_BODY on both Content-Length (fast path) and chunked bodies
    (streaming path) so clients cannot bypass the limit by omitting the header.
    """

    def __init__(self, app: Any, max_bytes: int = MAX_BODY) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        cl = next((v for k, v in scope.get("headers", []) if k == b"content-length"), None)
        if cl:
            try:
                if int(cl) > self.max_bytes:
                    resp = HTMLResponse("<p class='error'>Request too large.</p>", status_code=413)
                    await resp(scope, receive, send)
                    return
            except ValueError:
                resp = HTMLResponse("<p class='error'>Invalid request.</p>", status_code=400)
                await resp(scope, receive, send)
                return

        # No Content-Length (chunked): buffer full body before deciding, so the
        # inner app never starts if the limit is exceeded (avoids double-response).
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") == "http.request":
                body += msg.get("body", b"")
                if len(body) > self.max_bytes:
                    resp = HTMLResponse("<p class='error'>Request too large.</p>", status_code=413)
                    await resp(scope, receive, send)
                    return
                if not msg.get("more_body", False):
                    break

        async def buffered_receive() -> Any:
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, buffered_receive, send)


class SecurityHeadersMiddleware:
    """Add security response headers to every HTML response."""

    _HEADERS: ClassVar[list[tuple[bytes, bytes]]] = [
        (n.encode(), v.encode())
        for n, v in [
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
            (
                "Content-Security-Policy",
                (
                    "default-src 'self'; "
                    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://cdn.jsdelivr.net https://esm.sh; "  # noqa: E501
                    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                    "img-src 'self' data:; "
                    "connect-src 'self'; "
                    "media-src 'self'; "
                    "font-src 'self' https://cdn.jsdelivr.net; "
                    "frame-ancestors https://arcator.co.uk;"
                ),
            ),
        ]
    ]

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", [])) + self._HEADERS}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AccessLogMiddleware:
    """Replaces uvicorn.access: logs username (when signed in) instead of IP."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 0

        async def capture_status(message: Any) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        start = time.perf_counter()
        request = Request(scope)
        await self.app(scope, receive, capture_status)
        ms = (time.perf_counter() - start) * 1000

        state: AppState | None = getattr(request.app.state, "srv", None)
        sid = request.cookies.get("session_id")
        sess = state.sessions.get(sid) if state and sid else None
        label = (sess.account_username or client_ip(request)) if sess else client_ip(request)
        _access_log.info(
            '"%s %s" %s %.0fms %s',
            request.method,
            request.url.path,
            status_code,
            ms,
            label,
        )


# App


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await db.init(DB_PATH)
    catalog = Catalog.load(ROOT)
    templates.env.globals["tier_colors"] = catalog.tier_colors
    state = AppState(catalog=catalog)
    _app.state.srv = state

    async def _purge_loop() -> None:
        while True:
            await asyncio.sleep(300)
            state.purge_stale()

    state.spawn(_purge_loop(), name="purge-loop")

    _bot: BotCore | None = None
    if _token := os.environ.get("DISCORD_TOKEN"):
        _bot = BotCore(app_state=state)
        state.spawn(_bot.start(_token), name="discord-bot")

    yield

    if _bot is not None:
        await _bot.close()
    for t in list(state.tasks):
        t.cancel()
    await asyncio.gather(*state.tasks, return_exceptions=True)
    await db.close()


app = FastAPI(lifespan=_lifespan)


def toast_error(message: str, status_code: int = 200, kind: str = "error") -> Response:
    return Response(
        status_code=status_code,
        headers={
            "HX-Reswap": "none",
            "HX-Trigger": json.dumps({"showToast": {"message": message, "type": kind}}),
        },
    )


@app.exception_handler(HtmxError)
async def htmx_error_handler(request: Request, exc: HtmxError) -> Response:  # noqa: ARG001
    return toast_error(exc.message, exc.status_code)


app.add_middleware(AccessLogMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
app.mount("/audios", ImmutableStaticFiles(directory=str(ROOT / "audios")), name="audios")
app.include_router(auth_router)
app.include_router(account_router)

# PWA


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(
        ROOT / "static" / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/manifest.json")
async def manifest() -> Response:
    return Response(
        json.dumps(
            {
                "name": "Spelling Bee",
                "short_name": "Spelling Bee",
                "start_url": ".",
                "scope": ".",
                "display": "standalone",
                "theme_color": "#0f1729",
                "background_color": "#0f1729",
                "icons": [
                    {
                        "src": "static/icon-192.png",
                        "sizes": "192x192",
                        "type": "image/png",
                        "purpose": "any",
                    },
                    {
                        "src": "static/icon-192.png",
                        "sizes": "192x192",
                        "type": "image/png",
                        "purpose": "maskable",
                    },
                    {
                        "src": "static/icon-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                        "purpose": "any",
                    },
                ],
            },
        ),
        media_type="application/manifest+json",
    )


# Routes


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    state: AppState = request.app.state.srv
    user = get_current_user(request)
    elo = None
    if user:
        row = await db.fetchone("SELECT elo FROM user_stats WHERE username = ?", (user,))
        if row:
            elo = row["elo"]
    # Reconnection: detect if session is still in an active room
    reconnect_code = None
    reconnect_mode = None
    sess = get_session(state, request)
    if sess and sess.room_code:
        rc_room = state.rooms.get(sess.room_code)
        if rc_room:
            reconnect_code = sess.room_code
            vis = rc_room.visibility
            reconnect_mode = vis
    # Active games indicator
    active_games: list[dict[str, Any]] = []
    total_active_players = 0
    waiting_counts: dict[str, int] = {}
    for r in state.rooms.values():
        if r.visibility != "public":
            continue
        game = r.current_game
        if game and game.current_word and not game.winner:
            n = len(game.alive_sids())
            if n > 0:
                active_games.append({"difficulty": r.difficulty, "players": n, "code": r.code})
                total_active_players += n
        elif not r.winner and len(r.sessions) < 2:
            waiting_counts[r.difficulty] = waiting_counts.get(r.difficulty, 0) + len(r.sessions)
    template = "fragments/menu_page.html" if request.headers.get("HX-Request") else "index.html"
    return await tpl(
        request,
        template,
        {
            "elo": elo,
            "reconnect_code": reconnect_code,
            "reconnect_mode": reconnect_mode,
            "active_games": active_games,
            "total_active_players": total_active_players,
            "waiting_counts": waiting_counts,
            **state.catalog.template_ctx(),
        },
    )


@app.post("/guess", response_class=HTMLResponse)
async def guess(request: Request) -> HTMLResponse:  # noqa: PLR0915
    """Handle guesses for all room modes."""
    state: AppState = request.app.state.srv
    ip = client_ip(request)
    if not state.check_rate(ip, "guess"):
        msg = "Too many attempts."
        raise HtmxError(msg, 429)

    sess = get_session(state, request)
    room: Room | None = None

    if sess and sess.room_code:
        room = state.rooms.get(sess.room_code)
        if room and room.visibility == "local":
            local_sids = request.cookies.get("local_sessions", "").split(",")
            game = room.current_game
            active_sid = game.active_sid() if game else None
            if active_sid not in local_sids:
                msg = "Invalid session."
                raise HtmxError(msg, 403)
            sess = state.sessions.get(active_sid) if active_sid else None

    if not sess or not sess.room_code:
        msg = "Invalid session."
        raise HtmxError(msg, 403)
    if not room:
        room = state.rooms.get(sess.room_code)
    if not room:
        return Response(status_code=204)

    game = room.current_game
    if not game or game.active_sid() != sess.id or not game.current_word:
        return Response(status_code=204)

    participant = game.get_participant(sess.id)
    if not participant:
        return Response(status_code=204)

    form = await request.form()
    raw_guess = str(form.get("guess", ""))
    guess_text = "".join(c for c in raw_guess if c in WORD_CHARS).strip("- ")[:MAX_WORD_LEN]
    typing_ms: int | None = None
    raw_tm = form.get("typing_ms")
    if raw_tm is not None:
        try:
            parsed = int(str(raw_tm))
            if 0 <= parsed <= 10 * 60 * 1000:
                typing_ms = parsed
        except (TypeError, ValueError):
            pass
    result, rankings = game.submit_guess(participant, guess_text, typing_ms)
    if result:
        # Update highest_tier on Session (persisted identity; GameParticipant is ephemeral)
        if result.correct and sess.account_username and state.catalog:
            t = result.tier
            diffs = state.catalog.difficulties
            if t in diffs and (not sess.highest_tier or diffs.index(t) > diffs.index(sess.highest_tier)):
                sess.highest_tier = t
        await record_guess_stats(
            sess.account_username,
            result.wpm,
            result.word,
            result.correct,
            tier=result.tier,
            streak=result.streak_at_guess,
        )
    await state.finalize_mutation(room.code, rankings)

    if room.visibility == "local":
        active_game = room.current_game
        active_sid_val = active_game.active_sid() if active_game else None
        viewer = (state.sessions.get(active_sid_val) if active_sid_val else None) or sess
    else:
        viewer = sess
    return await tpl(request, "fragments/room.html", build_room_ctx(state, room, viewer))


# Room creation / joining


@app.post("/room/create", response_class=HTMLResponse)
async def room_create(request: Request) -> Response:
    state: AppState = request.app.state.srv
    check_creation_limits(state, request)

    form = await request.form()
    difficulty = state.catalog.validate_difficulty(
        str(form.get("difficulty", state.catalog.difficulties[0])),
    )
    visibility: Visibility = "private"
    raw_vis = str(form.get("visibility", "private"))
    if raw_vis in ("private", "solo", "local"):
        visibility = raw_vis  # type: ignore[assignment]

    ip = client_ip(request)
    user = get_current_user(request)
    try:
        code = state.make_room_code()
    except RuntimeError:
        return toast_error("Server is at capacity. Try again soon.", 503)

    if visibility == "local":
        try:
            raw = json.loads(str(form.get("players", "[]")))
        except (json.JSONDecodeError, ValueError):
            msg = "Invalid player list."
            raise HtmxError(msg, 400) from None
        if not isinstance(raw, list) or not raw or not all(isinstance(n, str) for n in raw):
            msg = "Invalid player list."
            raise HtmxError(msg, 400)
        names: list[str] = raw
        room = state.make_room(code, difficulty, "local")
        all_sids: list[str] = []
        first_sess: Session | None = None
        for i, name in enumerate(names[:MAX_LOCAL_PLAYERS]):
            sess = state.create_session(clean_name(name, f"Player {i + 1}"), difficulty, ip)
            state.add_player_to_room(room, sess)
            all_sids.append(sess.id)
            if first_sess is None:
                first_sess = sess
        state.rooms[code] = room
        room.start_game()
        active_game = room.current_game
        active_sid = active_game.active_sid() if active_game else None
        viewer = (state.sessions.get(active_sid) if active_sid else None) or first_sess
        assert viewer is not None
        resp = await tpl(request, "fragments/room.html", build_room_ctx(state, room, viewer))
        assert first_sess is not None
        set_session_cookie(resp, first_sess.id)
        resp.set_cookie("local_sessions", ",".join(all_sids), **_session_cookie_kwargs())
        return resp

    # Solo or private lobby
    player_name = clean_name(str(form.get("player_name", "")))
    if await is_name_reserved(player_name, user):
        return toast_error("That name belongs to a registered account.")
    room = state.make_room(code, difficulty, visibility)
    state.rooms[code] = room
    highest_tier = await load_highest_tier(user, state.catalog.difficulties) if user else ""
    sess = await resolve_session(
        state,
        request,
        player_name,
        difficulty,
        ip,
        account=user,
        highest_tier=highest_tier,
    )
    state.add_player_to_room(room, sess)

    if visibility == "solo":
        room.start_game()

    resp = await tpl(request, "fragments/room.html", build_room_ctx(state, room, sess))
    set_session_cookie(resp, sess.id)
    return resp


@app.post("/room/join", response_class=HTMLResponse)
async def room_join(request: Request) -> Response:  # noqa: PLR0911
    state: AppState = request.app.state.srv
    check_creation_limits(state, request)

    ip = client_ip(request)
    if not state.check_rate(ip, "join_room"):
        return toast_error("Too many join attempts. Try again shortly.", 429)

    form = await request.form()
    code = re.sub(r"[^A-Z0-9]", "", str(form.get("room_code", "")).upper())[:ROOM_CODE_LEN]
    player_name = clean_name(str(form.get("player_name", "")))
    spectate = str(form.get("spectate", "")) == "1"

    room = state.rooms.get(code)
    if not room:
        return toast_error("Room not found.")
    if not spectate:
        if room.locked:
            return toast_error("Room is locked.")
        if len(room.sessions) >= MAX_PLAYERS:
            return toast_error("Room is full.")

    user = get_current_user(request)
    if not spectate and await is_name_reserved(player_name, user):
        return toast_error("That name belongs to a registered account.")

    highest_tier = await load_highest_tier(user, state.catalog.difficulties) if user else ""
    sess = _session_already_in_room(state, room, request, user) or await resolve_session(
        state,
        request,
        player_name,
        room.difficulty,
        ip,
        account=user,
        highest_tier=highest_tier,
    )
    if sess.room_code and sess.room_code != code:
        return toast_error("You're already in another room.")
    state.add_player_to_room(room, sess, spectate=spectate)
    if not spectate:
        room.begin_if_ready()
    state.room_changed(code)

    resp = await tpl(request, "fragments/room.html", build_room_ctx(state, room, sess))
    set_session_cookie(resp, sess.id)
    return resp


@app.post("/public/join", response_class=HTMLResponse)
async def public_join(request: Request) -> HTMLResponse:
    state: AppState = request.app.state.srv
    ip = client_ip(request)
    if not state.check_rate(ip, "create_room"):
        msg = "Too many attempts. Try again later."
        raise HtmxError(msg, 429)

    user = get_current_user(request)
    if not user:
        msg = "Login required for Public Arena."
        raise HtmxError(msg, 403)

    if state.count_sessions_for_ip(ip) >= MAX_SESSIONS_PER_IP:
        msg = "Too many active sessions."
        raise HtmxError(msg, 429)

    form = await request.form()
    difficulty = state.catalog.validate_difficulty(
        str(form.get("difficulty", state.catalog.difficulties[0])),
    )

    state.purge_stale()

    # Find existing public room for this difficulty
    target_room: Room | None = None
    for r in list(state.rooms.values()):
        if r.visibility == "public" and r.difficulty == difficulty and len(r.sessions) < MAX_PLAYERS:
            await state.finalize_mutation(r.code, r.tick())
            if r.code in state.rooms and not r.winner:
                target_room = r
                break

    spectate = str(form.get("spectate", "")) == "1"

    if spectate:
        if target_room is None:
            return toast_error("No active game to watch.")
    elif target_room is None:
        try:
            code = state.make_room_code()
        except RuntimeError:
            return toast_error("Server is at capacity. Try again soon.", 503)
        target_room = state.make_room(code, difficulty, "public")
        state.rooms[code] = target_room

    highest_tier = await load_highest_tier(user, state.catalog.difficulties)
    sess = _session_already_in_room(state, target_room, request, user) or await resolve_session(
        state,
        request,
        user,
        difficulty,
        ip,
        account=user,
        highest_tier=highest_tier,
    )
    if sess.room_code and sess.room_code != target_room.code:
        return toast_error("You're already in another room.")
    state.add_player_to_room(target_room, sess, spectate=spectate)
    if not spectate:
        target_room.begin_if_ready()
    state.room_changed(target_room.code)

    resp = await tpl(request, "fragments/room.html", build_room_ctx(state, target_room, sess))
    set_session_cookie(resp, sess.id)
    return resp


def build_room_ctx(state: AppState, room: Room, viewer: Session) -> dict[str, Any]:  # noqa: PLR0915
    game = room.current_game
    active_sid = game.active_sid() if game else None
    is_active = viewer.id == active_sid
    active_sess = state.sessions.get(active_sid) if active_sid else None
    host_sid = room_host_sid(room)
    viewer_participant = game.get_participant(viewer.id) if game else None
    is_spectator = viewer.id in room.spectators

    non_spectator = [s for s in room.sessions if s not in room.spectators]

    players: list[dict[str, Any]] = []
    for sid in room.sessions:
        s = state.sessions.get(sid)
        if not s:
            continue
        status, status_class = room.player_status(sid)
        p = game.get_participant(sid) if game else None
        players.append(
            {
                "sid": sid,
                "name": s.player_name,
                "status": status,
                "status_class": status_class,
                "is_viewer": sid == viewer.id,
                "is_host": sid == host_sid,
                "eliminated": p.eliminated if p else False,
                "account": s.account_username,
                "highest_tier": s.highest_tier,
                "words_correct": p.words_correct if p else 0,
                "words_attempted": p.words_attempted if p else 0,
            },
        )

    ctx: dict[str, Any] = {
        "room": room,
        "viewer": viewer,
        "players": players,
        "is_active": is_active and not is_spectator,
        "active_player_name": active_sess.player_name if active_sess else "",
        "mode": room.visibility,
        "chat": list(room.chat),
        "waiting_for_players": game is None and len(non_spectator) < 2,
        "is_host": viewer.id == host_sid,
        "room_locked": room.locked,
        "is_spectator": is_spectator,
        "ready_count": len(room.ready_votes),
        "ready_total": len(non_spectator) if non_spectator else len(room.sessions),
        "viewer_voted_ready": viewer.id in room.ready_votes,
    }

    if game and game.winner:
        ctx["feedback"] = feedback(f"{game.winner} wins", kind="success")
        ctx["match_results"] = game.last_match_results
        # Per-viewer intermission feedback
        for mr in game.last_match_results:
            if mr["sid"] == viewer.id:
                parts = [f"Rank: {mr['rank']}."]
                if mr.get("words_attempted"):
                    pct = round(mr["words_correct"] / mr["words_attempted"] * 100)
                    parts.append(f"{mr['words_correct']}/{mr['words_attempted']} correct ({pct}%).")
                if "elo" in mr:
                    sign = "+" if mr["elo_delta"] >= 0 else ""
                    parts.append(f"ELO: {mr['elo']} ({sign}{mr['elo_delta']}).")
                ctx["feedback"]["body"] = " ".join(parts)
                break
        if room.intermission_until > time.time():
            ctx["intermission_remaining"] = max(0, room.intermission_until - time.time())
    elif game and game.current_word and not ctx["waiting_for_players"]:
        word_data = game.current_word
        ctx["word_length"] = sum(1 for c in word_data["word"] if c.isalpha())
        ctx["definition"] = word_data["definition"]
        ctx["part_of_speech"] = word_data["part_of_speech"]

        ctx["audio_url"] = (
            f"audios/{word_data['word'].lower()}.mp3" if state.catalog.has_audio(word_data["word"]) else None
        )
        ctx["audio_duration"] = game.word_audio_duration
        ctx["word_served_at"] = game.word_served_at

        if is_active:
            last_fb = viewer_participant.last_feedback if viewer_participant else None
            ctx["feedback"] = last_fb or feedback("Your turn", kind="info")
        else:
            last_fb = viewer_participant.last_feedback if viewer_participant else None
            eliminated = viewer_participant.eliminated if viewer_participant else False
            ctx["feedback"] = (
                last_fb
                if eliminated
                else feedback(
                    f"{active_sess.player_name}'s turn" if active_sess else "Waiting",
                    kind="info",
                )
            )

        if game.turn_deadline > 0:
            ctx["time_remaining"] = min(
                game.turn_time_limit,
                max(0, game.turn_deadline - time.time()),
            )
            ctx["time_limit"] = game.turn_time_limit

        ctx["draft_text"] = game.draft_text

    if room.visibility == "solo" and viewer_participant:
        ctx["streak"] = viewer_participant.streak
        ctx["best_streak"] = viewer_participant.best_streak
        ctx["words_correct"] = viewer_participant.words_correct
        ctx["words_attempted"] = viewer_participant.words_attempted

    return ctx


async def _render_room_sse(
    state: AppState,
    sess: Session,
    code: str,
    request: Request,
    user: str | None,
) -> str | None:
    room = state.rooms.get(code)
    if not room:
        return None
    ctx = build_room_ctx(state, room, sess)
    ctx["request"] = request
    ctx["user"] = user
    template = templates.env.get_template("fragments/room_state.html")
    return await asyncio.to_thread(template.render, **ctx)


@app.get("/room/{code}", response_class=HTMLResponse)
async def room_poll(request: Request, code: str) -> HTMLResponse:
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code)
    return await tpl(request, "fragments/room.html", build_room_ctx(state, room, sess))


@app.get("/room/{code}/stream")
async def room_stream(request: Request, code: str):
    state: AppState = request.app.state.srv
    room = state.rooms.get(code)
    sess = get_session(state, request)
    if not room or not sess or sess.room_code != code:
        return Response(status_code=403)

    sid = sess.id
    user = get_current_user(request)

    # Cancel any pending disconnect timer (player is reconnecting)
    handle = state.disconnect_timers.pop(sid, None)
    if handle:
        handle.cancel()

    q: asyncio.Queue[dict] = asyncio.Queue()
    state.subscribers[code].add(q)

    async def gen():
        try:
            html = await _render_room_sse(state, sess, code, request, user)
            if html:
                yield {"event": "refresh", "data": html}
            while code in state.rooms:
                msg = await q.get()
                # Drain queue: deduplicate stateful events (refresh, draft), accumulate ordered events (chat)
                batch: list[dict] = [msg]
                while not q.empty():
                    batch.append(q.get_nowait())
                need_refresh = any(m["event"] == "refresh" for m in batch)
                if need_refresh:
                    # Full re-render includes all chat; skip individual chat events
                    html = await _render_room_sse(state, sess, code, request, user)
                    if html:
                        yield {"event": "refresh", "data": html}
                else:
                    latest_draft = next((m for m in reversed(batch) if m["event"] == "draft"), None)
                    if latest_draft:
                        yield latest_draft
                    for m in batch:
                        if m["event"] == "chat":
                            yield m
        finally:
            state.subscribers[code].discard(q)
            if code in state.subscribers and not state.subscribers[code]:
                del state.subscribers[code]
            state.schedule_disconnect_forfeit(code, sid)

    return EventSourceResponse(gen(), ping=15)


@app.post("/room/{code}/draft")
async def room_draft(request: Request, code: str) -> Response:
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code, "draft")
    game = room.current_game
    if not game or game.active_sid() != sess.id:
        return Response(status_code=403)

    form = await request.form()
    raw_draft = str(form.get("draft", ""))
    draft = "".join(c for c in raw_draft if c in WORD_CHARS).strip("- ")[:MAX_WORD_LEN]
    game.set_draft(draft)
    room.last_activity = time.time()
    state.draft_changed(code, draft)
    return Response(status_code=204)


@app.post("/room/{code}/chat", response_class=HTMLResponse)
async def room_chat(request: Request, code: str) -> HTMLResponse:
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code, "chat")

    form = await request.form()
    raw = str(form.get("message", ""))
    if raw.startswith("E1|"):
        msg = raw[:MAX_E2EE_CHAT_LEN]
    else:
        msg = "".join(c for c in raw if c.isprintable()).strip()[:MAX_CHAT_LEN]
    if msg and msg.lower() not in CHAT_BLOCKLIST:
        msg_data = {"player": sess.player_name, "message": msg, "sid": sess.id, "ts": time.time()}
        room.add_chat(msg_data)
        template = templates.env.get_template("fragments/chat_message.html")
        html = await asyncio.to_thread(template.render, msg=msg_data)
        state.chat_changed(code, html)

    return HTMLResponse("")


@app.post("/room/{code}/lock")
async def room_lock_toggle(request: Request, code: str) -> Response:
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code)
    if room.visibility != "private":
        return toast_error("Invalid room.", status_code=403)
    if sess.id != room_host_sid(room):
        msg = "Only the host can lock."
        raise HtmxError(msg, 403)
    room.toggle_lock()
    state.room_changed(code)
    return Response(status_code=204)


@app.post("/room/{code}/ready")
async def room_ready(request: Request, code: str) -> Response:
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code)
    if room.visibility != "private":
        return toast_error("Invalid room.", status_code=403)

    is_host = sess.id == room_host_sid(room)
    game = room.current_game

    if not game:
        if not is_host:
            return toast_error("Only the host can start.", status_code=403)
        non_spectator = [s for s in room.sessions if s not in room.spectators]
        if len(non_spectator) < 2:
            return toast_error("Need at least 2 players.")
        room.start_game()
        state.room_changed(code)
        return Response(status_code=204)

    if game.winner:
        room.ready_votes.add(sess.id)
        non_spectator = [s for s in room.sessions if s not in room.spectators]
        all_voted = all(sid in room.ready_votes for sid in non_spectator)
        if is_host or all_voted:
            room.start_new_game()
        state.room_changed(code)
        return Response(status_code=204)

    return Response(status_code=204)


@app.post("/forfeit", response_class=HTMLResponse)
async def forfeit(request: Request) -> HTMLResponse:
    state: AppState = request.app.state.srv
    sess = get_session(state, request)
    if not sess:
        return HTMLResponse("")

    if not sess.room_code:
        return HTMLResponse("")

    room = state.rooms.get(sess.room_code)
    if not room:
        state.sessions.pop(sess.id, None)
        return HTMLResponse("")

    form = await request.form()
    target_sid = str(form.get("target", "")).strip() or None

    if target_sid:
        if room.visibility != "private" or sess.id != room_host_sid(room):
            msg = "Only the host can kick."
            raise HtmxError(msg, 403)
        if target_sid not in room.sessions or target_sid == sess.id:
            msg = "Invalid target."
            raise HtmxError(msg, 400)
        target_sess = state.sessions.get(target_sid)
        game = room.current_game
        if game and game.current_word and not game.winner:
            await state.finalize_mutation(room.code, room.forfeit(target_sid))
        else:
            room.sessions.remove(target_sid)
            room.spectators.discard(target_sid)
            await state.finalize_mutation(room.code, None)
        if target_sess:
            state.sessions.pop(target_sess.id, None)
        return HTMLResponse("")

    if sess.id in room.sessions:
        await state.finalize_mutation(room.code, room.forfeit(sess.id))

    state.sessions.pop(sess.id, None)
    return HTMLResponse("")


@app.post("/room/{code}/restart", response_class=HTMLResponse)
async def room_restart(request: Request, code: str) -> Response:
    """Restart a solo/local game."""
    state: AppState = request.app.state.srv
    _sess, room = require_room(state, request, code)
    if room.visibility not in ("solo", "local"):
        return toast_error("Invalid room.", status_code=403)

    room.start_new_game()
    state.room_changed(code)

    active_game = room.current_game
    active_sid = active_game.active_sid() if active_game else None
    viewer = (state.sessions.get(active_sid) if active_sid else None) or _sess
    return await tpl(request, "fragments/room.html", build_room_ctx(state, room, viewer))


if __name__ == "__main__":
    import uvicorn

    if Ruleset:
        # the ruleset by default disallows all filesystem access
        rs = Ruleset()
        # explicitly allow access to the local directory hierarchy
        rs.allow(".")
        # turn on protections
        rs.apply()
        logging.info("Succeeded sandboxing.")
    else:
        logging.warning("Skipping sandboxing.")

    uvicorn.run(
        app,
        host="127.0.0.1",
        log_config=str(ROOT / "log_config.json"),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        server_header=False,
        limit_concurrency=100,
        timeout_keep_alive=5,
    )
