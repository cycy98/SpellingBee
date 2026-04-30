"""Validate audio coverage: every word has an .mp3, every .mp3 has a word, and no file is corrupt."""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from mutagen.mp3 import MP3, HeaderNotFoundError

    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

ROOT = Path(__file__).parent.parent
AUDIOS = ROOT / "audios"
WORDLIST = ROOT / "wordlist.json"

data: dict = json.loads(WORDLIST.read_text())
words = {w.lower() for k, v in data.items() if k != "info" and isinstance(v, dict) for w in v}

audio_stems = {p.stem: p for p in AUDIOS.glob("*.mp3")}
missing = sorted(words - audio_stems.keys())
orphans = sorted(audio_stems.keys() - words)

corrupt: list[str] = []
if HAS_MUTAGEN:
    for stem, path in audio_stems.items():
        if stem in words:
            try:
                MP3(path)
            except (HeaderNotFoundError, Exception):
                corrupt.append(stem)
    corrupt.sort()

total = len(words)
print(
    f"Words: {total}  |  Audio found: {total - len(missing)}  |  Missing: {len(missing)}  |  Orphans: {len(orphans)}",
    end="",
)
print(f"  |  Corrupt: {len(corrupt)}" if HAS_MUTAGEN else "  |  Corrupt: (mutagen not installed)")

if missing:
    print("\nMissing audio files:")
    for w in missing:
        print(f"  {w}")

if orphans:
    print("\nOrphan audio files (no matching word):")
    for w in orphans:
        print(f"  {w}")

if corrupt:
    print("\nCorrupt audio files:")
    for w in corrupt:
        print(f"  {w}")

if missing or orphans or corrupt:
    sys.exit(1)

print("\nAll words have valid audio.")
