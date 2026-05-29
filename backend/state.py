from __future__ import annotations

import asyncio
import itertools
import random
import string
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.game import (
    RATE_LIMITS,
    ROOM_CODE_LEN,
    STALE_MINUTES,
    Catalog,
    GameEvent,
    MatchCompleteEvent,
    Ranking,
    Room,
    Session,
    Visibility,
    make_session_id,
)
from backend.persistence import persist_match_elo

if TYPE_CHECKING:
    from collections.abc import Coroutine

DISCONNECT_GRACE = 30  # seconds before a disconnected player is auto-forfeited


@dataclass
class AppState:
    catalog: Catalog
    sessions: dict[str, Session] = field(default_factory=dict)
    rooms: dict[str, Room] = field(default_factory=dict)
    rate_buckets: dict[str, dict[str, list[float]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list)),
    )
    subscribers: dict[str, set[asyncio.Queue]] = field(default_factory=lambda: defaultdict(set))
    room_timers: dict[str, asyncio.TimerHandle] = field(default_factory=dict)
    disconnect_timers: dict[str, asyncio.TimerHandle] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)
    event_queue: asyncio.Queue[GameEvent] = field(default_factory=asyncio.Queue)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def make_room(self, code: str, difficulty: str, visibility: Visibility) -> Room:
        return Room(
            code=code,
            difficulty=difficulty,
            visibility=visibility,
            sessions_map=self.sessions,
            catalog=self.catalog,
        )

    def make_room_code(self) -> str:
        chars = string.ascii_uppercase + string.digits
        available = [
            "".join(t) for t in itertools.product(chars, repeat=ROOM_CODE_LEN) if "".join(t) not in self.rooms
        ]
        if not available:
            msg = "all room codes exhausted"
            raise RuntimeError(msg)
        return random.choice(available)  # noqa: S311

    def check_rate(self, ip: str, action: str) -> bool:
        limit, window = RATE_LIMITS[action]
        now = time.time()
        bucket = self.rate_buckets[ip][action]
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def count_sessions_for_ip(self, ip: str) -> int:
        return sum(1 for s in self.sessions.values() if s.ip == ip)

    def create_session(
        self,
        player_name: str,
        difficulty: str,
        ip: str,
        account: str | None = None,
        highest_tier: str = "",
    ) -> Session:
        sid = make_session_id()
        sess = Session(
            id=sid,
            player_name=player_name,
            difficulty=difficulty,
            account_username=account,
            ip=ip,
        )
        if highest_tier:
            sess.highest_tier = highest_tier
        self.sessions[sid] = sess
        return sess

    def add_player_to_room(self, room: Room, sess: Session, spectate: bool = False) -> Session:
        sess.room_code = room.code
        if sess.id not in room.sessions:
            room.sessions.append(sess.id)
        if spectate:
            room.spectators.add(sess.id)
        room.last_activity = time.time()
        return sess

    def _destroy_room(self, code: str) -> None:
        room = self.rooms.pop(code, None)
        if not room:
            return
        for sid in room.sessions:
            handle = self.disconnect_timers.pop(sid, None)
            if handle:
                handle.cancel()
            self.sessions.pop(sid, None)
        for q in self.subscribers.pop(code, set()):
            q.put_nowait({"event": "close", "data": ""})
        handle = self.room_timers.pop(code, None)
        if handle:
            handle.cancel()

    def purge_stale(self) -> None:
        cutoff = time.time() - STALE_MINUTES * 60
        stale_rooms = [c for c, r in self.rooms.items() if r.last_activity < cutoff]
        for c in stale_rooms:
            self._destroy_room(c)
        stale_ips = [
            ip for ip, actions in self.rate_buckets.items() if all(not ts for ts in actions.values())
        ]
        for ip in stale_ips:
            del self.rate_buckets[ip]

    def room_changed(self, code: str) -> None:
        """Wake SSE subscribers and reschedule the timer for the next deadline."""
        for q in self.subscribers.get(code, set()):
            q.put_nowait({"event": "refresh", "data": ""})
        self._arm_room_timer(code)

    def chat_changed(self, code: str, html: str) -> None:
        """Push a rendered chat message to SSE subscribers without a full refresh."""
        for q in self.subscribers.get(code, set()):
            q.put_nowait({"event": "chat", "data": html})

    def draft_changed(self, code: str, text: str) -> None:
        """Push draft text to SSE subscribers without a full refresh."""
        for q in self.subscribers.get(code, set()):
            q.put_nowait({"event": "draft", "data": text})

    def _arm_room_timer(self, code: str) -> None:
        handle = self.room_timers.pop(code, None)
        if handle:
            handle.cancel()
        room = self.rooms.get(code)
        if not room:
            return
        deadline = room.next_deadline()
        if deadline is None:
            return
        loop = asyncio.get_running_loop()
        self.room_timers[code] = loop.call_later(deadline - time.time(), self._room_timer_fire, code)

    async def finalize_mutation(self, code: str, rankings: list[Ranking] | None) -> None:
        room = self.rooms.get(code)
        if not room:
            return
        if rankings:
            elo_results = await persist_match_elo(room, rankings)
            if room.current_game:
                room.current_game.apply_elo_results(elo_results)
            n = len(elo_results)
            for r in elo_results:
                self.event_queue.put_nowait(
                    MatchCompleteEvent(
                        username=r.username,
                        rank=r.rank,
                        elo_delta=r.elo_delta,
                        room_code=code,
                        visibility=room.visibility,
                        n_players=n,
                    ),
                )
        if not room.sessions:
            self._destroy_room(code)
            return
        self.room_changed(code)

    def _room_timer_fire(self, code: str) -> None:
        self.room_timers.pop(code, None)
        room = self.rooms.get(code)
        if not room:
            return
        self.spawn(self.finalize_mutation(code, room.tick()), name=f"fire-{code}")

    def schedule_disconnect_forfeit(self, code: str, sid: str) -> None:
        room = self.rooms.get(code)
        if not room or room.visibility in ("solo", "local"):
            return
        if sid not in room.sessions:
            return
        loop = asyncio.get_running_loop()
        handle = loop.call_later(DISCONNECT_GRACE, self._disconnect_forfeit, code, sid)
        self.disconnect_timers[sid] = handle

    def _disconnect_forfeit(self, code: str, sid: str) -> None:
        self.disconnect_timers.pop(sid, None)
        room = self.rooms.get(code)
        if not room:
            return
        self.spawn(self.finalize_mutation(code, room.forfeit(sid)), name=f"df-{sid}")
