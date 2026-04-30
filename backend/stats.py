"""Spelling Bee — shared stats service layer.

All functions are async and use backend.db (aiosqlite singleton).
No Discord, HTTP, HTML, or Jinja knowledge lives here.
Guild-scoped queries use _GUILD_SUBQ; single-user queries are module-level.
Discord→username resolution is the caller's responsibility.
"""
# ruff: noqa: S608  — SQL column interpolations are guarded by whitelist/Literal

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NamedTuple

from backend import db
from backend.game import compute_elo_stake

if TYPE_CHECKING:
    from backend.game import Catalog


class UserStats(NamedTuple):
    username: str
    elo: float
    games: int
    wins: int
    words: int
    correct: int
    best_wpm: int
    best_word: str
    best_streak: int
    member_since: int | None


class TodayData(NamedTuple):
    attempts: int
    correct: int
    best_wpm: float | None
    best_streak: int
    matches: int


class StudyItem(NamedTuple):
    word: str
    n: int
    definition: str


class RankData(NamedTuple):
    rank: int
    total: int
    top_pct: float
    elo: float
    elo_7d_ago: float | None


class HeadToHead(NamedTuple):
    player_wins: int
    opponent_wins: int
    total_games: int
    player_elo: float
    opponent_elo: float


class WordStats(NamedTuple):
    word: str
    tries: int
    hits: int
    best_wpm: float | None


class WordRaceEntry(NamedTuple):
    username: str
    wpm: float
    ts: int


class GuildOverview(NamedTuple):
    total_matches: int
    avg_elo: float | None
    total_correct: int
    members: int
    badges: int
    guesses_today: int
    matches_week: int
    active_today: int


_SORT_COLS = frozenset({"elo", "games", "wins", "correct", "best_wpm", "best_streak"})


# ─── Personal query functions ─────────────────────────────────────────────────


def row_to_user_stats(row: db.Row) -> UserStats:
    return UserStats(
        row["username"],
        float(row["elo"]),
        int(row["games"]),
        int(row["wins"]),
        int(row["words"]),
        int(row["correct"]),
        int(row["best_wpm"] or 0),
        row["best_word"] or "",
        int(row["best_streak"]),
        int(row["member_since"]) if row["member_since"] else None,
    )


async def get_user_stats(username: str) -> UserStats | None:
    row = await db.fetchone("SELECT * FROM user_stats WHERE username=?", (username,))
    if row is None:
        return None
    return row_to_user_stats(row)


async def _get_rank(username: str) -> RankData | None:
    row = await db.fetchone(
        "SELECT (SELECT COUNT(*)+1 FROM user_stats WHERE elo>s.elo) AS rank,"
        " (SELECT COUNT(*) FROM user_stats) AS total, s.elo"
        " FROM user_stats s WHERE s.username=?",
        (username,),
    )
    if row is None or row["elo"] is None:
        return None
    rank, total, elo = int(row["rank"]), int(row["total"]), float(row["elo"])
    elo7 = await db.fetchone(
        "SELECT elo_after FROM match_results WHERE username=? AND ts<UNIXEPOCH()-7*86400"
        " ORDER BY ts DESC LIMIT 1",
        (username,),
    )
    return RankData(
        rank,
        total,
        round((rank / total) * 100, 1) if total else 0.0,
        elo,
        float(elo7["elo_after"]) if elo7 else None,
    )


async def _get_today(username: str) -> TodayData:
    """Return today's activity counts."""
    row, mr = await asyncio.gather(
        db.fetchone(
            "SELECT COUNT(*) AS attempts, COUNT(*) FILTER(WHERE correct=1) AS correct,"
            " MAX(wpm) AS best_wpm, MAX(streak) AS best_streak"
            " FROM guess_log WHERE username=? AND ts>strftime('%s','now','start of day')",
            (username,),
        ),
        db.fetchone(
            "SELECT COUNT(*) AS matches FROM match_results"
            " WHERE username=? AND ts>strftime('%s','now','start of day')",
            (username,),
        ),
    )
    return TodayData(
        int(row["attempts"]) if row else 0,
        int(row["correct"]) if row else 0,
        float(row["best_wpm"]) if row and row["best_wpm"] is not None else None,
        int(row["best_streak"]) if row and row["best_streak"] is not None else 0,
        int(mr["matches"]) if mr else 0,
    )


async def get_study_list(username: str, catalog: Catalog | None = None) -> list[StudyItem]:
    rows = await db.fetchall(
        "SELECT word, COUNT(*) AS n FROM guess_log"
        " WHERE username=? AND correct=0 GROUP BY word ORDER BY n DESC LIMIT 10",
        (username,),
    )
    all_words: dict[str, Any] = catalog.all_words if catalog else {}
    return [
        StudyItem(r["word"], int(r["n"]), all_words.get(r["word"], {}).get("definition", ""))
        for r in rows
    ]


async def _get_h2h(player: str, opponent: str) -> HeadToHead:
    elo_rows, row = await asyncio.gather(
        db.fetchall(
            "SELECT username, elo FROM user_stats WHERE username IN (?,?)",
            (player, opponent),
        ),
        db.fetchone(
            "SELECT COUNT(DISTINCT mr_a.match_id) AS total_games,"
            " SUM(CASE WHEN mr_a.rank<mr_b.rank THEN 1 ELSE 0 END) AS player_wins,"
            " SUM(CASE WHEN mr_b.rank<mr_a.rank THEN 1 ELSE 0 END) AS opponent_wins"
            " FROM match_results mr_a"
            " JOIN match_results mr_b ON mr_b.match_id=mr_a.match_id AND mr_b.username=?"
            " WHERE mr_a.username=?",
            (opponent, player),
        ),
    )
    m = {r["username"]: float(r["elo"]) for r in elo_rows}
    return HeadToHead(
        int(row["player_wins"]) if row and row["player_wins"] else 0,
        int(row["opponent_wins"]) if row and row["opponent_wins"] else 0,
        int(row["total_games"]) if row and row["total_games"] else 0,
        m.get(player, 1000.0),
        m.get(opponent, 1000.0),
    )


async def _get_word_personal(word: str, username: str) -> WordStats:
    row = await db.fetchone(
        "SELECT COUNT(*) AS tries, COUNT(*) FILTER(WHERE correct=1) AS hits, MAX(wpm) AS best_wpm"
        " FROM guess_log WHERE word=? AND username=?",
        (word, username),
    )
    if row is None:
        return WordStats(word, 0, 0, None)
    return WordStats(
        word,
        int(row["tries"]),
        int(row["hits"]),
        float(row["best_wpm"]) if row["best_wpm"] is not None else None,
    )


# ─── Guild-scoped helpers ─────────────────────────────────────────────────────


_GUILD_SUBQ = "(SELECT username FROM guild_linked_users WHERE guild_id = ?)"


async def _guild_witnessed_by(guild_id: str, word: str) -> list[db.Row]:
    return await db.fetchall(
        f"SELECT gl.username, MIN(gl.ts) AS first_ts FROM guess_log gl"
        f" WHERE gl.word=? AND gl.correct=1 AND gl.username IN {_GUILD_SUBQ}"
        f" GROUP BY gl.username ORDER BY first_ts ASC",
        (word, guild_id),
    )


# ─── Global leaderboard ───────────────────────────────────────────────────────


async def get_leaderboard(
    sort: str = "elo",
    limit: int = 100,
    guild_id: str | None = None,
) -> list[UserStats]:
    if sort not in _SORT_COLS:
        sort = "elo"
    cols = "username, elo, games, wins, words, correct, best_wpm, best_word, best_streak"
    tb = "" if sort == "elo" else ", elo DESC"
    if guild_id:
        rows = await db.fetchall(
            f"SELECT {cols} FROM user_stats WHERE username IN {_GUILD_SUBQ}"
            f" ORDER BY {sort} DESC{tb} LIMIT ?",
            (guild_id, limit),
        )
    else:
        rows = await db.fetchall(
            f"SELECT {cols} FROM user_stats ORDER BY {sort} DESC{tb} LIMIT ?",
            (limit,),
        )
    return _to_user_stats(rows)


def _to_user_stats(rows: list[db.Row]) -> list[UserStats]:
    return [
        UserStats(
            r["username"],
            float(r["elo"]),
            int(r["games"]),
            int(r["wins"]),
            int(r["words"]),
            int(r["correct"]),
            int(r["best_wpm"]),
            r["best_word"],
            int(r["best_streak"]),
            None,
        )
        for r in rows
    ]


# ─── Dashboard batch fetchers ─────────────────────────────────────────────────


async def fetch_player(username: str) -> dict | None:
    (
        streak_row,
        badges_rows,
        profile,
        rank,
        today,
        elo_rows,
        tier_rows,
        recent_rows,
    ) = await asyncio.gather(
        db.fetchone(
            "SELECT streak FROM guess_log WHERE username=? ORDER BY ts DESC LIMIT 1",
            (username,),
        ),
        db.fetchall(
            "SELECT badge, earned_at FROM user_badges WHERE username=? ORDER BY earned_at DESC",
            (username,),
        ),
        get_user_stats(username),
        _get_rank(username),
        _get_today(username),
        db.fetchall(
            "SELECT ts, elo_after FROM match_results WHERE username=? ORDER BY ts ASC",
            (username,),
        ),
        db.fetchall(
            "SELECT tier, attempts, correct, accuracy, best_wpm"
            " FROM user_tier_stats WHERE username=? ORDER BY tier",
            (username,),
        ),
        db.fetchall(
            "SELECT correct FROM guess_log WHERE username=? ORDER BY ts DESC LIMIT 20",
            (username,),
        ),
    )
    if profile is None:
        return None
    hot_streak = 0
    for r in recent_rows:
        if r["correct"]:
            hot_streak += 1
        else:
            break
    return {
        "profile": profile,
        "rank": rank,
        "today": today,
        "streak": int(streak_row["streak"]) if streak_row else 0,
        "badges": badges_rows,
        "elo_ts": [int(r["ts"]) for r in elo_rows],
        "elo_vals": [float(r["elo_after"]) for r in elo_rows],
        "tiers": [
            {
                "tier": r["tier"],
                "attempts": int(r["attempts"]),
                "accuracy": float(r["accuracy"] or 0),
                "best_wpm": r["best_wpm"],
            }
            for r in tier_rows
        ],
        "member_since": profile.member_since,
        "hot_streak": hot_streak,
    }


async def fetch_account_page(username: str) -> dict:
    (
        recent_rows,
        avg_row,
        rank,
        today,
        elo_rows,
        tier_rows,
        heatmap_rows,
        wpm_rows,
    ) = await asyncio.gather(
        db.fetchall(
            "SELECT word, correct, wpm, tier, ts FROM guess_log"
            " WHERE username=? ORDER BY ts DESC LIMIT 20",
            (username,),
        ),
        db.fetchone(
            "SELECT AVG(wpm) AS avg_wpm FROM"
            " (SELECT wpm FROM guess_log WHERE username=? AND correct=1 ORDER BY ts DESC LIMIT 50)",
            (username,),
        ),
        _get_rank(username),
        _get_today(username),
        db.fetchall(
            "SELECT ts, elo_after FROM match_results WHERE username=? ORDER BY ts ASC",
            (username,),
        ),
        db.fetchall(
            "SELECT tier, attempts, accuracy, best_wpm"
            " FROM user_tier_stats WHERE username=? ORDER BY tier",
            (username,),
        ),
        db.fetchall(
            "SELECT DATE(ts,'unixepoch') AS day, COUNT(*) AS n"
            " FROM guess_log WHERE username=? AND ts>UNIXEPOCH()-90*86400"
            " GROUP BY day ORDER BY day",
            (username,),
        ),
        db.fetchall(
            "SELECT ts, wpm FROM guess_log"
            " WHERE username=? AND correct=1 AND wpm IS NOT NULL"
            " ORDER BY ts ASC LIMIT 200",
            (username,),
        ),
    )
    hot_streak = 0
    for r in recent_rows:
        if r["correct"]:
            hot_streak += 1
        else:
            break
    avg_wpm = round(float(avg_row["avg_wpm"]), 1) if avg_row and avg_row["avg_wpm"] else 0.0
    return {
        "recent": [
            {
                "word": r["word"],
                "correct": bool(r["correct"]),
                "wpm": float(r["wpm"]) if r["wpm"] is not None else None,
                "tier": r["tier"] or "",
                "ts": int(r["ts"]),
            }
            for r in recent_rows
        ],
        "avg_wpm": avg_wpm,
        "earned_tiers": [r["tier"] for r in tier_rows],
        "hot_streak": hot_streak,
        "rank": rank,
        "today": today,
        "elo_ts": [int(r["ts"]) for r in elo_rows],
        "elo_vals": [float(r["elo_after"]) for r in elo_rows],
        "tiers": [
            {
                "tier": r["tier"],
                "attempts": int(r["attempts"]),
                "accuracy": float(r["accuracy"] or 0),
                "best_wpm": r["best_wpm"],
            }
            for r in tier_rows
        ],
        "heatmap_rows": heatmap_rows,
        "wpm_ts": [int(r["ts"]) for r in wpm_rows],
        "wpm_vals": [float(r["wpm"]) for r in wpm_rows],
    }


async def fetch_versus(player: str, opponent: str) -> dict:
    clutch_row_p, clutch_row_o, h2h = await asyncio.gather(
        db.fetchone(
            "SELECT COUNT(*) FILTER(WHERE rank=1)*1.0/NULLIF(COUNT(*),0) AS rate, COUNT(*) AS total"
            " FROM match_results mr WHERE username=?"
            " AND (SELECT COUNT(*) FROM match_results WHERE match_id=mr.match_id)>=3",
            (player,),
        ),
        db.fetchone(
            "SELECT COUNT(*) FILTER(WHERE rank=1)*1.0/NULLIF(COUNT(*),0) AS rate, COUNT(*) AS total"
            " FROM match_results mr WHERE username=?"
            " AND (SELECT COUNT(*) FROM match_results WHERE match_id=mr.match_id)>=3",
            (opponent,),
        ),
        _get_h2h(player, opponent),
    )
    stake = compute_elo_stake(h2h.player_elo, [h2h.player_elo, h2h.opponent_elo])
    return {
        "h2h": h2h,
        "stake": stake,
        "clutch_player": {
            "rate": float(clutch_row_p["rate"])
            if clutch_row_p and clutch_row_p["rate"] is not None
            else 0.0,
            "total": int(clutch_row_p["total"]) if clutch_row_p else 0,
        },
        "clutch_opponent": {
            "rate": float(clutch_row_o["rate"])
            if clutch_row_o and clutch_row_o["rate"] is not None
            else 0.0,
            "total": int(clutch_row_o["total"]) if clutch_row_o else 0,
        },
    }


async def fetch_word_card(
    word: str,
    username: str | None = None,
    guild_id: str | None = None,
) -> dict | None:
    # global stats
    global_row = await db.fetchone(
        "SELECT word, COUNT(*) AS attempts, COUNT(*) FILTER(WHERE correct=1) AS hits,"
        " ROUND(100.0*COUNT(*) FILTER(WHERE correct=1)/NULLIF(COUNT(*),0),1) AS accuracy"
        " FROM guess_log WHERE word=?",
        (word,),
    )
    if global_row is None or global_row["attempts"] == 0:
        return None
    global_stats = {
        "word": global_row["word"],
        "attempts": int(global_row["attempts"]),
        "hits": int(global_row["hits"]),
        "accuracy": float(global_row["accuracy"]) if global_row["accuracy"] is not None else 0.0,
    }
    # race leaderboard
    race_rows = await db.fetchall(
        "SELECT username, wpm, ts FROM guess_log"
        " WHERE word=? AND correct=1 ORDER BY wpm DESC LIMIT ?",
        (word, 5),
    )
    race = [WordRaceEntry(r["username"], float(r["wpm"]), int(r["ts"])) for r in race_rows]

    data: dict[str, Any] = {"global_stats": global_stats, "race": race}

    coros: dict[str, Any] = {}
    if username:
        coros["personal"] = _get_word_personal(word, username)
    if guild_id:
        coros["witnesses"] = _guild_witnessed_by(guild_id, word)
    if coros:
        keys = list(coros.keys())
        results = await asyncio.gather(*coros.values())
        data.update(zip(keys, results, strict=False))
    return data


async def fetch_server_dashboard(guild_id: str) -> dict:
    gid = guild_id
    ov, (peak_row, streak_row, wpm_row, spot_row, nem_row), lb = await asyncio.gather(
        db.fetchone(
            "WITH gm(username) AS (SELECT username FROM guild_linked_users WHERE guild_id=?)"
            " SELECT"
            " (SELECT COUNT(DISTINCT match_id) FROM match_results"
            "  WHERE username IN (SELECT username FROM gm)) AS total_matches,"
            " (SELECT ROUND(AVG(elo),1) FROM user_stats"
            "  WHERE username IN (SELECT username FROM gm)) AS avg_elo,"
            " (SELECT SUM(correct) FROM user_stats"
            "  WHERE username IN (SELECT username FROM gm)) AS total_correct,"
            " (SELECT COUNT(*) FROM user_stats"
            "  WHERE username IN (SELECT username FROM gm)) AS members,"
            " (SELECT COUNT(DISTINCT badge) FROM user_badges"
            "  WHERE username IN (SELECT username FROM gm)) AS badges,"
            " (SELECT COUNT(*) FROM guess_log"
            "  WHERE ts>strftime('%s','now','start of day')"
            "  AND username IN (SELECT username FROM gm)) AS guesses_today,"
            " (SELECT COUNT(DISTINCT match_id) FROM match_results"
            "  WHERE ts>UNIXEPOCH()-7*86400"
            "  AND username IN (SELECT username FROM gm)) AS matches_week,"
            " (SELECT COUNT(DISTINCT username) FROM guess_log"
            "  WHERE ts>strftime('%s','now','start of day')"
            "  AND username IN (SELECT username FROM gm)) AS active_today",
            (gid,),
        ),
        asyncio.gather(
            db.fetchone(
                f"SELECT username, MAX(elo_after) AS peak_elo, ts AS peak_ts"
                f" FROM match_results WHERE username IN {_GUILD_SUBQ}"
                f" GROUP BY username ORDER BY peak_elo DESC LIMIT 1",
                (gid,),
            ),
            db.fetchone(
                f"SELECT username, MAX(streak) AS best_streak, ts AS streak_ts"
                f" FROM guess_log WHERE username IN {_GUILD_SUBQ}"
                f" GROUP BY username ORDER BY best_streak DESC LIMIT 1",
                (gid,),
            ),
            db.fetchone(
                f"SELECT username, MAX(wpm) AS best_wpm, word AS best_word, ts AS wpm_ts"
                f" FROM guess_log WHERE correct=1 AND username IN {_GUILD_SUBQ}"
                f" GROUP BY username ORDER BY best_wpm DESC LIMIT 1",
                (gid,),
            ),
            db.fetchone(
                f"SELECT mr.username, MAX(mr.elo_after)-MIN(mr.elo_after) AS gain,"
                f" COUNT(*) AS games"
                f" FROM match_results mr WHERE mr.username IN {_GUILD_SUBQ}"
                f" AND mr.ts>UNIXEPOCH()-7*86400"
                f" GROUP BY mr.username ORDER BY gain DESC LIMIT 1",
                (gid,),
            ),
            db.fetchone(
                f"SELECT word, tier, COUNT(*) AS attempts,"
                f" ROUND(100.0*COUNT(*) FILTER(WHERE correct=1)/NULLIF(COUNT(*),0),1) AS accuracy"
                f" FROM guess_log WHERE username IN {_GUILD_SUBQ} AND tier!=''"
                f" GROUP BY word HAVING COUNT(*)>=3 ORDER BY accuracy ASC LIMIT 1",
                (gid,),
            ),
        ),
        get_leaderboard(limit=5, guild_id=guild_id),
    )
    overview = GuildOverview(
        ov["total_matches"] if ov else 0,
        ov["avg_elo"] if ov else None,
        ov["total_correct"] if ov else 0,
        ov["members"] if ov else 0,
        ov["badges"] if ov else 0,
        ov["guesses_today"] if ov else 0,
        ov["matches_week"] if ov else 0,
        ov["active_today"] if ov else 0,
    )
    hof: dict[str, db.Row | None] = {
        "peak_elo": peak_row,
        "best_streak": streak_row,
        "best_wpm": wpm_row,
    }
    spotlight = (
        None
        if spot_row is None
        else {
            "username": spot_row["username"],
            "gain": float(spot_row["gain"]),
            "games": int(spot_row["games"]),
        }
    )
    nemesis = (
        None
        if nem_row is None
        else {
            "word": nem_row["word"],
            "tier": nem_row["tier"],
            "attempts": int(nem_row["attempts"]),
            "accuracy": float(nem_row["accuracy"]) if nem_row["accuracy"] is not None else 0.0,
        }
    )
    return {"stats": overview, "lb": lb, "hof": hof, "spotlight": spotlight, "nemesis": nemesis}


async def fetch_study_plan(username: str, catalog: Any = None) -> dict:
    tier_rows, unbeaten_rows, study = await asyncio.gather(
        db.fetchall(
            "SELECT * FROM user_tier_stats WHERE username=? ORDER BY tier",
            (username,),
        ),
        db.fetchall(
            "SELECT DISTINCT word FROM guess_log WHERE username=? AND word NOT IN"
            " (SELECT word FROM guess_log WHERE username=? AND correct=1) ORDER BY word LIMIT ?",
            (username, username, 10),
        ),
        get_study_list(username, catalog),
    )
    weakest = min(tier_rows, key=lambda r: float(r["accuracy"] or 100)) if tier_rows else None
    return {"study": study, "weakest_tier": weakest, "unbeaten": unbeaten_rows}
