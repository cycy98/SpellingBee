"""Spelling Bee — game engine (pure logic, no IO)."""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple, NotRequired, TypedDict, cast

# Domain types

type Visibility = Literal["private", "public", "solo", "local"]


class WordEntry(TypedDict):
    word: str
    definition: str
    part_of_speech: str
    tier: str
    homophones: NotRequired[list[str]]


class Feedback(TypedDict):
    title: str
    type: str
    body: NotRequired[str]
    word: NotRequired[str]
    definition: NotRequired[str]
    wpm: NotRequired[float]
    homophone: NotRequired[str]
    alternatives: NotRequired[list[str]]


class Ranking(TypedDict):
    sid: str
    name: str
    rank: int
    account: str | None


class MatchResult(TypedDict):
    name: str
    rank: int
    sid: str
    words_correct: int
    words_attempted: int
    elo: NotRequired[float]
    elo_delta: NotRequired[float]


# Config

ROOT = Path(__file__).resolve().parent.parent
MAX_CHAT = 80
MAX_CHAT_LEN = 150
# Null-family and object-notation strings that appear as bot/test noise but never in real chat
CHAT_BLOCKLIST = frozenset(
    {
        "undefined",
        "undef",
        "null",
        "(null)",
        "nil",
        "nul",
        "\\n",
        "void",
        "nan",
        "-infinity",
        "inf",
        "-inf",
        "#nil",
        "null null",
        "null;",
        "null,null",
        "[object object]",
        "[object array]",
        "[object null]",
        "[object undefined]",
        "[object promise]",
        "{}",
        "[]",
        "[null]",
    },
)
MAX_WORD_LEN = 83
# Allowlist: ASCII letters + wordlist accented chars (é í ō) + ligatures (æ œ) + space/hyphen
WORD_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyzæœÆŒéíō -")
STALE_MINUTES = 30
NETWORK_GRACE = 2.0  # anti-cheat ceiling cushion: absorbs POST round-trip + audio buffering jitter
MAX_CHARS_PER_SEC = 15  # physics floor for typing speed — bounds client-reported typing_ms
MAX_SESSIONS_PER_IP = 10
MAX_PLAYERS = 15
MAX_LOCAL_PLAYERS = 12

RATE_LIMITS: dict[str, tuple[int, int]] = {
    "login": (5, 60),
    "register": (3, 60),
    "create_room": (5, 60),
    "chat": (10, 30),
    "guess": (60, 60),
    "draft": (60, 60),
}

# Word catalog


@dataclass
class Catalog:
    tier_colors: dict[str, str]
    words: dict[str, dict[str, dict[str, Any]]]
    audio_durations: dict[str, float]
    difficulties: list[str] = field(init=False, repr=False)
    total_words: int = field(init=False, repr=False)
    all_words: dict[str, dict[str, Any]] = field(init=False, repr=False)
    _word_keys: dict[str, list[str]] = field(init=False, repr=False)
    _all_word_keys: list[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.difficulties = list(self.words)
        self.total_words = sum(len(v) for v in self.words.values())
        self.all_words = {
            w: {**wdata, "tier": tier}
            for tier, wdict in self.words.items()
            for w, wdata in wdict.items()
        }
        self._word_keys = {d: list(ws) for d, ws in self.words.items()}
        self._all_word_keys = list(self.all_words)

    @classmethod
    def load(cls, root: Path) -> Catalog:
        with (root / "wordlist.json").open() as f:
            raw: dict[str, Any] = json.load(f)
        return cls(
            tier_colors=raw["info"]["color"],
            words={k: v for k, v in raw.items() if k != "info"},
            audio_durations=_load_audio_durations(root),
        )

    def pick_word(self, difficulty: str) -> WordEntry:
        if difficulty == "randomizer":
            word_str = secrets.choice(self._all_word_keys)
            return cast("WordEntry", {"word": word_str, **self.all_words[word_str]})
        keys = self._word_keys.get(difficulty, self._word_keys[self.difficulties[0]])
        pool = self.words.get(difficulty, self.words[self.difficulties[0]])
        word_str = secrets.choice(keys)
        return cast("WordEntry", {"word": word_str, **pool[word_str], "tier": difficulty})

    def has_audio(self, word: str) -> bool:
        return word.lower() in self.audio_durations

    def validate_difficulty(self, raw: str) -> str:
        if raw in self.difficulties or raw == "randomizer":
            return raw
        return self.difficulties[0]

    def template_ctx(self) -> dict[str, Any]:
        return {
            "difficulties": self.difficulties,
            "words": self.words,
            "total_words": self.total_words,
        }


def _load_audio_durations(root: Path) -> dict[str, float]:
    audios_dir = root / "audios"
    if not audios_dir.is_dir():
        return {}
    durations: dict[str, float] = {}
    try:
        from mutagen.mp3 import MP3  # noqa: PLC0415

        for p in audios_dir.glob("*.mp3"):
            try:
                durations[p.stem.lower()] = MP3(p).info.length
            except Exception:  # noqa: BLE001
                durations[p.stem.lower()] = len(p.stem) * 0.12 + 0.5
    except ImportError:
        for p in audios_dir.glob("*.mp3"):
            durations[p.stem.lower()] = len(p.stem) * 0.12 + 0.5
    return durations


# In-memory state


@dataclass
class Session:
    id: str
    player_name: str
    difficulty: str
    room_code: str | None = None
    account_username: str | None = None
    highest_tier: str = ""
    ip: str = ""


@dataclass
class GuessResult:
    correct: bool
    skipped: bool
    wpm: float
    word: str
    tier: str
    definition: str = ""
    homophone: str | None = None
    alternatives: list[str] = field(default_factory=list)
    streak_at_guess: int = 0  # streak value before mutation (for stats recording)


@dataclass
class GameParticipant:
    sid: str
    player_name: str
    account: str | None
    words_attempted: int = 0
    words_correct: int = 0
    streak: int = 0
    best_streak: int = 0
    last_feedback: Feedback | None = None
    eliminated: bool = False
    elimination_order: int = -1


@dataclass
class Game:
    number: int
    difficulty: str
    visibility: Visibility
    participants: dict[str, GameParticipant]  # sid → participant, insertion-ordered
    turn_order: list[str]  # sids in turn rotation order
    turn_index: int = 0
    current_word: WordEntry | None = None
    word_served_at: float = 0.0
    word_audio_duration: float = 0.0
    turn_deadline: float = 0.0
    turn_time_limit: float = 0.0
    draft_text: str = ""
    winner: str | None = None
    winner_sid: str | None = None
    last_match_results: list[MatchResult] = field(default_factory=list)
    _elimination_counter: int = field(default=0, repr=False)
    catalog: Catalog | None = field(default=None, repr=False)

    def alive_sids(self) -> list[str]:
        return [sid for sid in self.turn_order if not self.participants[sid].eliminated]

    def active_sid(self) -> str | None:
        alive = self.alive_sids()
        if not alive:
            return None
        return alive[self.turn_index % len(alive)]

    def eliminate(self, sid: str) -> None:
        p = self.participants[sid]
        p.eliminated = True
        p.elimination_order = self._elimination_counter
        self._elimination_counter += 1

    def get_participant(self, sid: str) -> GameParticipant | None:
        return self.participants.get(sid)

    def serve_new_word(self, streak: int = 0) -> None:
        assert self.catalog is not None, "Game.catalog must be set before serving words"  # noqa: S101
        last = self.current_word["word"] if self.current_word else None
        word_data = self.catalog.pick_word(self.difficulty)
        if word_data["word"] == last:
            word_data = self.catalog.pick_word(self.difficulty)
        self.current_word = word_data
        self.word_served_at = time.time()
        self.draft_text = ""
        word_str = word_data["word"]
        is_solo = self.visibility == "solo"
        tl = compute_time_limit(word_str, streak=streak, multiplayer=not is_solo)
        self.turn_time_limit = tl
        self.word_audio_duration = self.catalog.audio_durations.get(word_str.lower(), 0.0)
        self.turn_deadline = time.time() + tl + self.word_audio_duration + NETWORK_GRACE

    def build_rankings(self) -> list[Ranking]:
        rankings: list[Ranking] = []
        eliminated = sorted(
            [p for p in self.participants.values() if p.eliminated],
            key=lambda p: p.elimination_order,
        )
        n_eliminated = len(eliminated)
        for i, p in enumerate(eliminated):
            rankings.append(
                Ranking(
                    sid=p.sid,
                    name=p.player_name,
                    rank=n_eliminated - i + 1,  # first out = worst rank
                    account=p.account,
                ),
            )
        alive = [p for p in self.participants.values() if not p.eliminated]
        if alive:
            rankings.append(
                Ranking(
                    sid=alive[0].sid,
                    name=alive[0].player_name,
                    rank=1,
                    account=alive[0].account,
                ),
            )
        return rankings

    def finish(self) -> list[Ranking]:
        rankings = self.build_rankings()
        winner_ranking = next((r for r in rankings if r["rank"] == 1), None)
        self.winner = winner_ranking["name"] if winner_ranking else "Nobody"
        self.winner_sid = winner_ranking["sid"] if winner_ranking else None
        self.turn_deadline = 0
        self.draft_text = ""

        self.last_match_results = []
        for r in rankings:
            p = self.participants.get(r["sid"])
            self.last_match_results.append(
                MatchResult(
                    name=r["name"],
                    rank=r["rank"],
                    sid=r["sid"],
                    words_correct=p.words_correct if p else 0,
                    words_attempted=p.words_attempted if p else 0,
                ),
            )
        self.last_match_results.sort(key=lambda x: x["rank"])
        return rankings

    def advance_turn(self, eliminated: bool = False) -> list[Ranking] | None:
        alive = self.alive_sids()
        if len(alive) <= 1:
            return self.finish()
        if not eliminated:
            self.turn_index = (self.turn_index + 1) % len(alive)
        else:
            self.turn_index = self.turn_index % len(alive)
        self.draft_text = ""
        self.serve_new_word()
        return None

    def check_timeout(self) -> list[Ranking] | None:
        if self.winner:
            return None
        if self.turn_deadline <= 0 or time.time() < self.turn_deadline:
            return None
        active = self.active_sid()
        if not active:
            return None
        p = self.participants.get(active)
        if p:
            fb: Feedback = {"title": "Time's up", "type": "error"}
            if self.current_word:
                fb["word"] = self.current_word["word"]
                fb["definition"] = self.current_word.get("definition", "")
            p.last_feedback = fb
        self.eliminate(active)
        return self.advance_turn(eliminated=True)

    def _apply_to_participant(
        self,
        participant: GameParticipant,
        result: GuessResult,
        is_solo: bool,
    ) -> None:
        if result.correct:
            participant.words_correct += 1
            if is_solo:
                participant.streak += 1
                participant.best_streak = max(participant.best_streak, participant.streak)
            fb: Feedback = {"title": "Correct", "type": "success", "wpm": result.wpm}
            if result.homophone:
                fb["homophone"] = result.homophone
            if not is_solo:
                fb["body"] = "You stay in."
        else:
            if is_solo:
                participant.streak = 0
            title = ("Skipped" if result.skipped else "Incorrect") if is_solo else "Eliminated"
            fb = {
                "title": title,
                "type": "error",
                "word": result.word,
                "definition": result.definition,
            }
            if result.skipped and not is_solo:
                fb["body"] = "Skipped."
            if not result.skipped and is_solo:
                fb["wpm"] = result.wpm
            if result.alternatives:
                fb["alternatives"] = result.alternatives
        participant.last_feedback = fb

    def submit_guess(
        self,
        participant: GameParticipant,
        guess_text: str,
        typing_ms: int | None = None,
    ) -> tuple[GuessResult | None, list[Ranking] | None]:
        word_data = self.current_word
        if not word_data:
            return None, None

        elapsed = typing_window_s(
            typing_ms,
            guess_text,
            self.word_served_at,
            self.word_audio_duration,
        )
        is_solo = self.visibility == "solo"

        timed_out = self.turn_deadline > 0 and time.time() > self.turn_deadline
        if timed_out:
            guess_text = ""

        participant.words_attempted += 1
        skipped = not guess_text

        if skipped:
            correct, homophone, wpm = False, None, 0.0
        else:
            correct, homophone = evaluate_guess(guess_text, word_data)
            wpm = round(compute_wpm(guess_text, elapsed), 1)

        streak_before = participant.streak

        result = GuessResult(
            correct=correct,
            skipped=skipped,
            wpm=wpm,
            word=word_data["word"],
            tier=word_data["tier"],
            definition=word_data.get("definition", ""),
            homophone=homophone,
            alternatives=word_data.get("homophones", []),
            streak_at_guess=streak_before,
        )

        self._apply_to_participant(participant, result, is_solo)
        if timed_out:
            participant.last_feedback = {
                "title": "Time's up",
                "type": "error",
                "word": result.word,
                "definition": result.definition,
            }

        rankings = None
        if is_solo:
            self.serve_new_word(streak=participant.streak)
        elif skipped or not correct:
            self.eliminate(participant.sid)
            rankings = self.advance_turn(eliminated=True)
        else:
            rankings = self.advance_turn(eliminated=False)

        return result, rankings

    def set_draft(self, text: str) -> None:
        self.draft_text = text


@dataclass
class Room:
    code: str
    difficulty: str
    visibility: Visibility
    sessions: list[str] = field(default_factory=list)  # session IDs, ordered (lobby)
    spectators: set[str] = field(default_factory=set)  # spectator sids (subset of sessions)
    current_game: Game | None = None
    chat: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=MAX_CHAT))
    intermission_until: float = 0.0
    game_number: int = 1
    locked: bool = False
    ready_votes: set[str] = field(default_factory=set)
    last_activity: float = field(default_factory=time.time)
    # Injected at construction; defaults to empty/None so Room is usable in tests.
    sessions_map: dict[str, Session] = field(default_factory=dict, repr=False)
    catalog: Catalog | None = field(default=None, repr=False)

    @property
    def winner(self) -> str | None:
        return self.current_game.winner if self.current_game else None

    def start_game(self, starting_sid: str | None = None) -> Game:
        lobby_sids = [sid for sid in self.sessions if sid not in self.spectators]
        turn_order = list(lobby_sids)
        if starting_sid and starting_sid in turn_order:
            idx = turn_order.index(starting_sid)
            turn_order = turn_order[idx:] + turn_order[:idx]
        participants: dict[str, GameParticipant] = {}
        for sid in turn_order:
            s = self.sessions_map[sid]
            participants[sid] = GameParticipant(
                sid=sid,
                player_name=s.player_name,
                account=s.account_username,
            )
        assert self.catalog is not None  # noqa: S101
        game = Game(
            number=self.game_number,
            difficulty=self.difficulty,
            visibility=self.visibility,
            participants=participants,
            turn_order=turn_order,
            catalog=self.catalog,
        )
        game.serve_new_word()
        self.current_game = game
        self.game_number += 1
        self.last_activity = time.time()
        return game

    def start_new_game(self) -> None:
        prev_game = self.current_game
        starting_sid = None
        if prev_game and prev_game.turn_order:
            eliminated = [p for p in prev_game.participants.values() if p.eliminated]
            if eliminated:
                last_eliminated = max(eliminated, key=lambda p: p.elimination_order)
                if last_eliminated.sid in self.sessions:
                    starting_sid = last_eliminated.sid
        self.intermission_until = 0
        self.ready_votes.clear()
        self.start_game(starting_sid=starting_sid)

    def tick(self) -> list[Ranking] | None:
        game = self.current_game
        if not game:
            return None
        rankings = game.check_timeout()
        if self.intermission_until and time.time() >= self.intermission_until and game.winner:
            self.start_new_game()
        return rankings

    def begin_if_ready(self) -> bool:
        if self.visibility == "private":
            return False
        non_spectator = [s for s in self.sessions if s not in self.spectators]
        if len(non_spectator) == 2 and self.current_game is None:
            self.start_game()
            return True
        return False

    def forfeit(self, sid: str) -> list[Ranking] | None:
        if sid not in self.sessions:
            return None
        rankings = None
        game = self.current_game
        if game and sid in game.participants and not game.participants[sid].eliminated:
            prev_active = game.active_sid()
            was_active = prev_active == sid
            game.eliminate(sid)
            alive = game.alive_sids()
            if len(alive) <= 1 and game.current_word and not game.winner:
                rankings = game.finish()
            elif was_active and alive:
                game.turn_index = game.turn_index % len(alive)
                game.draft_text = ""
                game.serve_new_word()
            elif alive and prev_active and prev_active in alive:
                game.turn_index = alive.index(prev_active)
        # Lobby removal — does NOT affect game.participants
        self.ready_votes.discard(sid)
        self.spectators.discard(sid)
        self.sessions.remove(sid)
        self.last_activity = time.time()
        return rankings

    def player_status(self, sid: str) -> tuple[str, str]:
        if sid in self.spectators:
            return "Spectating", "spectating"
        game = self.current_game
        if not game or sid not in game.participants:
            return "Waiting", "waiting"
        p = game.participants[sid]
        if game.winner:
            for mr in game.last_match_results:
                if mr["sid"] == sid:
                    return (
                        f"Rank {mr['rank']}",
                        "winner" if mr["rank"] == 1 else "eliminated",
                    )
        if p.eliminated:
            return "Eliminated", "eliminated"
        if sid == game.active_sid():
            return "Spelling", "spelling"
        return "Waiting", "waiting"

    def add_chat(self, entry: dict[str, str]) -> None:
        self.chat.append(entry)
        self.last_activity = time.time()

    def toggle_lock(self) -> None:
        self.locked = not self.locked


# Pure helpers


def feedback(title: str, body: str = "", kind: str = "error") -> Feedback:
    f: Feedback = {"title": title, "type": kind}
    if body:
        f["body"] = body
    return f


def make_session_id() -> str:
    return secrets.token_urlsafe(16)


_NAME_RE = re.compile(r"[^A-Za-z0-9 '_-]")


def clean_name(raw: str, fallback: str = "Player") -> str:
    return _NAME_RE.sub("", raw).strip()[:24] or fallback


def room_host_sid(room: Room) -> str | None:
    for sid in room.sessions:
        if sid not in room.spectators:
            return sid
    return None


def active_session_id(room: Room) -> str | None:
    if not room.current_game:
        return None
    return room.current_game.active_sid()


def compute_time_limit(word: str, streak: int = 0, multiplayer: bool = False) -> float:
    chars = max(len(word) / 5, 0.2)
    wpm_required = 10.0 if multiplayer else 5 * streak**0.8 + 10
    return max(3.0, (chars / wpm_required) * 60)


def typing_window_s(
    typing_ms: int | None,
    guess: str,
    served_at: float,
    audio_duration: float,
) -> float:
    """Typing window in seconds. Trusts client-reported ms above a physics floor;
    falls back to server elapsed (minus audio) when the client doesn't report."""
    floor = max(len(guess), 1) / MAX_CHARS_PER_SEC
    if typing_ms is not None:
        return max(typing_ms / 1000, floor)
    return max(time.time() - served_at - audio_duration, floor)


def compute_wpm(guess: str, elapsed: float) -> float:
    elapsed = max(elapsed, 2.0)
    chars = max(len(guess) / 5, 0.2)
    return min(300.0, chars / (elapsed / 60))


_LIGATURES = str.maketrans({"æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE"})


def _normalize(s: str) -> str:
    return s.translate(_LIGATURES)


def evaluate_guess(guess: str, word_entry: WordEntry) -> tuple[bool, str | None]:
    """Returns (correct, matched_homophone_or_None)."""
    target = word_entry["word"].lower()
    g = guess.strip().lower()
    if g == target:
        return True, None
    if any(c in target for c in "æœ") and g == _normalize(target):
        return True, None
    for h in word_entry.get("homophones", []):
        hl = h.lower()
        if g == hl:
            return True, h
        if any(c in hl for c in "æœ") and g == _normalize(hl):
            return True, h
    return False, None


class EloStake(NamedTuple):
    win: float
    loss: float


def compute_elo_stake(
    player_elo: float,
    all_elos: list[float],
) -> EloStake:
    """Predict ELO delta for player using the real multi-player formula."""
    k = 32.0
    n = max(len(all_elos), 2)
    norm = n * (n - 1) / 2

    def _exp(elo: float) -> float:
        return math.exp(0.01 * min(elo, 5000))

    denom = sum(_exp(e) for e in all_elos)
    exp_a = _exp(player_elo)
    return EloStake(
        win=round(k * ((n - 1) / norm - exp_a / denom), 1),
        loss=round(k * (0 / norm - exp_a / denom), 1),
    )


def update_elo(players: list[dict[str, Any]], k: float = 32.0) -> None:
    n = len(players)
    if n < 2:
        return
    norm = n * (n - 1) / 2

    def _exp(elo: float) -> float:
        return math.exp(0.01 * min(elo, 5000))

    denom = sum(_exp(p["elo"]) for p in players)
    for p in players:
        p["elo"] += k * ((n - p["rank"]) / norm - _exp(p["elo"]) / denom)
