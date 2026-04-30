"""Spelling Bee — unified badge and milestone progression system.

No HTTP, Discord, or Jinja knowledge here. Operates on PlayerProfile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.stats import UserStats


@dataclass(slots=True, frozen=True)
class BadgeDef:
    name: str
    icon: str
    column: str | None  # None for computed badges like Sharpshooter
    threshold: float
    description: str
    computed: bool = False


@dataclass(slots=True, frozen=True)
class MilestoneDef:
    category: str
    thresholds: list[int]


@dataclass(slots=True, frozen=True)
class EarnedBadge:
    name: str
    icon: str
    description: str


@dataclass(slots=True, frozen=True)
class MilestoneProgress:
    category: str
    current: float
    next_threshold: int | None
    pct: float  # 0.0-1.0


@dataclass(slots=True)
class Progression:
    earned_badges: list[EarnedBadge]
    milestones: list[MilestoneProgress]


BADGES: list[BadgeDef] = [
    BadgeDef("Century Club", "🏆", "wins", 100, "Win 100 matches"),
    BadgeDef("Word Master", "📖", "correct", 1000, "Spell 1000 words correctly"),
    BadgeDef("Veteran", "⭐", "games", 500, "Play 500 games"),
    BadgeDef("Speed Demon", "⚡", "best_wpm", 60, "Reach 60 WPM"),
    BadgeDef("Sharpshooter", "🎯", None, 0.9, "90%+ accuracy with 50+ words", computed=True),
]

MILESTONES: list[MilestoneDef] = [
    MilestoneDef("games", [50, 100, 250, 500]),
    MilestoneDef("correct", [50, 100, 250, 500]),
    MilestoneDef("wins", [10, 25, 50, 100]),
    MilestoneDef("elo", [1100, 1200, 1500]),
]


def compute_progression(profile: UserStats) -> Progression:
    earned: list[EarnedBadge] = []
    for badge in BADGES:
        if badge.computed:
            # Sharpshooter: correct >= 50 AND accuracy >= 90%
            if (
                profile.words >= 50
                and profile.correct >= 50
                and profile.correct / profile.words >= badge.threshold
            ):
                earned.append(
                    EarnedBadge(name=badge.name, icon=badge.icon, description=badge.description),
                )
        else:
            assert badge.column is not None  # noqa: S101
            value = getattr(profile, badge.column, 0)
            if value >= badge.threshold:
                earned.append(
                    EarnedBadge(name=badge.name, icon=badge.icon, description=badge.description),
                )

    milestones: list[MilestoneProgress] = []
    for mdef in MILESTONES:
        current = float(getattr(profile, mdef.category, 0.0))
        # Find the next threshold above current value
        next_t: int | None = next(
            (t for t in mdef.thresholds if t > current),
            None,
        )
        prev_t = max((t for t in mdef.thresholds if t <= current), default=0)
        if next_t is None:
            pct = 1.0
        elif next_t == prev_t:
            pct = 0.0
        else:
            pct = min(1.0, (current - prev_t) / (next_t - prev_t))
        milestones.append(
            MilestoneProgress(
                category=mdef.category,
                current=current,
                next_threshold=next_t,
                pct=round(pct, 3),
            ),
        )

    return Progression(earned_badges=earned, milestones=milestones)
