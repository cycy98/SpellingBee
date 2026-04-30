"""Spelling Bee — async DB persistence helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend import db
from backend.game import Ranking, Room, update_elo

if TYPE_CHECKING:
    from collections.abc import Callable

    from backend.state import AppState


async def is_name_reserved(player_name: str, account_username: str | None) -> bool:
    """Return True if player_name belongs to a registered account that isn't the current user."""
    if not player_name or player_name == account_username:
        return False
    return await db.fetchone("SELECT 1 FROM users WHERE username = ?", (player_name,)) is not None


async def load_highest_tier(username: str, difficulties: list[str]) -> str:
    rows = await db.fetchall(
        "SELECT DISTINCT tier FROM guess_log WHERE username=? AND correct=1",
        (username,),
    )
    tiers = [r["tier"] for r in rows if r["tier"] in difficulties]
    return max(tiers, key=difficulties.index) if tiers else ""


async def record_guess_stats(
    username: str | None,
    wpm: float,
    word_str: str,
    correct: bool,
    tier: str = "",
    streak: int = 0,
) -> None:
    if not username:
        return
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO guess_log(username, word, correct, wpm, tier, streak) VALUES(?,?,?,?,?,?)",
            (username, word_str, int(correct), wpm if correct else None, tier, streak),
        )


async def persist_match_elo(
    room: Room,
    rankings: list[Ranking],
    notify: Callable[[str], None] | None = None,
    app_state: AppState | None = None,
) -> None:
    """Run ELO update, write match_results rows, and enrich room scoreboard with ELO deltas."""
    if room.visibility == "local":
        return
    tracked: list[dict[str, Any]] = []
    async with db.transaction() as conn:
        for r in rankings:
            if r["account"]:
                exists = await conn.execute("SELECT 1 FROM users WHERE username=?", (r["account"],))
                if await exists.fetchone():
                    elo_cur = await conn.execute(
                        "SELECT elo_after FROM match_results WHERE username=? ORDER BY ts DESC LIMIT 1",
                        (r["account"],),
                    )
                    elo_row = await elo_cur.fetchone()
                    elo = float(elo_row["elo_after"]) if elo_row else 1000.0
                    tracked.append({**r, "elo": elo, "old_elo": elo})
        if len(tracked) >= 2:
            update_elo(tracked)
            match_cur = await conn.execute(
                "INSERT INTO matches(difficulty, visibility) VALUES(?,?) RETURNING id",
                (room.difficulty, room.visibility),
            )
            match_row = await match_cur.fetchone()
            assert match_row is not None
            match_id = match_row[0]
            for t in tracked:
                await conn.execute(
                    "INSERT INTO match_results(username, match_id, rank, elo_after) VALUES(?,?,?,?)",
                    (t["account"], match_id, t["rank"], t["elo"]),
                )

    # Enrich the already-set scoreboard entries with ELO data
    last_match_results = room.current_game.last_match_results if room.current_game else []
    for entry in last_match_results:
        for t in tracked:
            if t["sid"] == entry["sid"]:
                entry["elo"] = round(t["elo"], 1)
                entry["elo_delta"] = round(t["elo"] - t["old_elo"], 1)
    if notify:
        notify(room.code)

    # Push events to in-process queue for Discord bot consumption
    if app_state is not None and len(tracked) >= 2:
        from backend.state import MatchCompleteEvent  # noqa: PLC0415

        n_players = len(tracked)
        for t in tracked:
            app_state.event_queue.put_nowait(
                MatchCompleteEvent(
                    username=t["account"],
                    rank=t["rank"],
                    elo_delta=round(t["elo"] - t["old_elo"], 1),
                    room_code=room.code,
                    visibility=room.visibility,
                    n_players=n_players,
                ),
            )
