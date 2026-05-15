from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable

LEVELS: tuple[str, ...] = (
    "starter",
    "easy",
    "tricky",
    "advanced",
    "insane",
    "expert",
    "master",
    "sesquipedalian",
)

DATA_PATH = Path("wordlist.json")


class WordEntry(TypedDict):
    part_of_speech: str
    definition: str
    homophones: list[str]


type WordData = dict[str, dict[str, WordEntry]]


def _sorted(data: WordData) -> WordData:
    result: WordData = {}
    for level in LEVELS:
        words = data.get(level, {})
        result[level] = {
            word: WordEntry(
                part_of_speech=entry["part_of_speech"],
                definition=entry["definition"],
                homophones=sorted(entry["homophones"], key=str.lower),
            )
            for word, entry in sorted(words.items(), key=lambda x: x[0].lower())
        }
    for key, val in data.items():
        if key not in LEVELS:
            result[key] = val
    return result


def _coerce_entry(raw: object) -> WordEntry:
    d = raw if isinstance(raw, dict) else {}
    pos = d.get("part_of_speech", "")
    defn = d.get("definition", "")
    homs = d.get("homophones", [])
    return WordEntry(
        part_of_speech=pos if isinstance(pos, str) else "",
        definition=defn if isinstance(defn, str) else "",
        homophones=[h for h in homs if isinstance(h, str)] if isinstance(homs, list) else [],
    )


def validate_data(raw: object) -> WordData:
    if not isinstance(raw, dict):
        msg = "wordlist.json must contain a JSON object at the top level"
        raise ValueError(msg)
    data: WordData = {level: {} for level in LEVELS}
    seen: set[str] = set()
    for level in LEVELS:
        level_raw = raw.get(level, {})
        level_dict = level_raw if isinstance(level_raw, dict) else {}
        for word, entry_raw in level_dict.items():
            if not isinstance(word, str) or not word:
                continue
            if word in seen:
                continue
            seen.add(word)
            data[level][word] = _coerce_entry(entry_raw)
    for key, val in raw.items():
        if key not in LEVELS:
            data[key] = val if isinstance(val, dict) else {}
    result = _sorted(data)
    _check_invariants(result)
    return result


def _check_invariants(data: WordData) -> None:
    """Each word in exactly one level; words and homophones sorted case-insensitively."""
    seen_words: set[str] = set()
    for level in LEVELS:
        for word in data[level]:
            assert word not in seen_words, f"Word '{word}' in multiple levels"
            seen_words.add(word)

    for level in LEVELS:
        words = list(data[level].keys())
        sorted_words = sorted(words, key=str.lower)
        assert words == sorted_words, f"Words in '{level}' not sorted"

    for level in LEVELS:
        for word, entry in data[level].items():
            homs = entry["homophones"]
            sorted_homs = sorted(homs, key=str.lower)
            assert homs == sorted_homs, f"Homophones of '{word}' not sorted"


def find_word(data: WordData, word: str) -> str | None:
    for level in LEVELS:
        if word in data.get(level, {}):
            return level
    return None


def add_word(data: WordData, level: str, word: str, pos: str, definition: str) -> WordData:
    word = word.strip()
    if not word:
        msg = "Word cannot be empty"
        raise ValueError(msg)
    if level not in LEVELS:
        msg = f"Invalid level: {level!r}"
        raise ValueError(msg)
    if find_word(data, word) is not None:
        msg = f"'{word}' already exists"
        raise ValueError(msg)
    entry: WordEntry = {
        "part_of_speech": pos.strip(),
        "definition": definition.strip(),
        "homophones": [],
    }
    return _sorted(data | {level: data[level] | {word: entry}})


def add_homophone(data: WordData, word: str, homophone: str) -> WordData:
    homophone = homophone.strip()
    if not homophone:
        msg = "Homophone cannot be empty"
        raise ValueError(msg)
    level = find_word(data, word)
    if level is None:
        msg = f"'{word}' not found"
        raise ValueError(msg)
    entry = data[level][word]
    if homophone in entry["homophones"]:
        return data
    new_entry = WordEntry(
        part_of_speech=entry["part_of_speech"],
        definition=entry["definition"],
        homophones=sorted(entry["homophones"] + [homophone], key=str.lower),
    )
    return data | {level: data[level] | {word: new_entry}}


def move_word(data: WordData, word: str, delta: Literal[1, -1]) -> WordData:
    level = find_word(data, word)
    if level is None:
        msg = f"'{word}' not found"
        raise ValueError(msg)
    idx = LEVELS.index(level)
    new_idx = idx + delta
    if not 0 <= new_idx < len(LEVELS):
        msg = f"'{word}' is already at the {'highest' if delta > 0 else 'lowest'} level"
        raise ValueError(msg)
    new_level = LEVELS[new_idx]
    entry = data[level][word]
    return _sorted(
        data
        | {
            level: {w: e for w, e in data[level].items() if w != word},
            new_level: data[new_level] | {word: entry},
        },
    )


def load_data(path: Path = DATA_PATH) -> WordData:
    if not path.exists():
        return {level: {} for level in LEVELS}
    if not path.is_file():
        msg = f"{path} is not a regular file"
        raise OSError(msg)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return validate_data(raw)


def save_data(path: Path, data: WordData) -> None:
    _check_invariants(data)
    ordered: dict = {level: data[level] for level in LEVELS if level in data}
    ordered |= {k: v for k, v in data.items() if k not in LEVELS}
    text = json.dumps(ordered, ensure_ascii=False, indent=4)
    path.write_text(text, encoding="utf-8")


class App:
    def __init__(self, root: tk.Tk, path: Path = DATA_PATH) -> None:
        self.root = root
        self.path = path
        try:
            self.data = load_data(path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            messagebox.showerror("Load Error", f"Failed to load {path}:\n{e}")
            self.data = {level: {} for level in LEVELS}
        root.title("Spelling Bee Manager")
        self._build_layout()
        self.refresh_word_list()

    def _build_layout(self) -> None:
        left = ttk.Frame(self.root)
        left.grid(row=0, column=0, padx=10, pady=10)
        right = ttk.Frame(self.root)
        right.grid(row=0, column=1, padx=10, pady=10, sticky="n")

        ttk.Label(left, text="Level").pack()
        self.level_var = tk.StringVar(value=LEVELS[0])
        self.level_menu = ttk.Combobox(left, textvariable=self.level_var, values=LEVELS)
        self.level_menu.pack()
        self.level_menu.bind("<<ComboboxSelected>>", lambda _: self.refresh_word_list())

        ttk.Label(left, text="Search").pack(pady=(10, 0))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_word_list())
        ttk.Entry(left, textvariable=self.search_var).pack()

        self.word_list = tk.Listbox(left, width=30, height=20)
        self.word_list.pack(pady=5)
        self.word_list.bind("<<ListboxSelect>>", lambda _: self._show_details())

        ttk.Button(left, text="Promote", command=lambda: self._move(1)).pack(fill="x")
        ttk.Button(left, text="Demote", command=lambda: self._move(-1)).pack(fill="x")
        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=10)
        ttk.Button(left, text="Copy Current Level", command=self._copy_level).pack(fill="x")
        ttk.Button(left, text="Copy All (A-Z)", command=self._copy_all).pack(fill="x")

        ttk.Label(right, text="Word Details", font=("Arial", 12, "bold")).pack()
        self.details_text = tk.Text(right, width=50, height=15, wrap="word")
        self.details_text.pack()

        ttk.Label(right, text="Add New Word").pack(pady=(10, 0))
        self.new_word = ttk.Entry(right)
        self.new_word.pack()
        self.new_pos = ttk.Entry(right)
        self.new_pos.pack()
        self.new_def = ttk.Entry(right)
        self.new_def.pack()
        ttk.Button(right, text="Add Word", command=self._add_word).pack(pady=5)

        ttk.Label(right, text="Add Homophone").pack()
        self.new_hom = ttk.Entry(right)
        self.new_hom.pack()
        ttk.Button(right, text="Add Homophone", command=self._add_homophone).pack()

    def _commit(self, new_data: WordData) -> None:
        save_data(self.path, new_data)
        self.data = new_data

    def _mutate(self, fn: Callable[[WordData], WordData]) -> bool:
        try:
            self._commit(fn(self.data))
            self.refresh_word_list()
            return True
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return False
        except OSError as e:
            messagebox.showerror("Save Error", str(e))
            return False

    def _selected_word(self) -> str | None:
        sel = self.word_list.curselection()
        return self.word_list.get(sel[0]) if sel else None

    def _select_word(self, word: str) -> None:
        for i in range(self.word_list.size()):
            if self.word_list.get(i) == word:
                self.word_list.selection_set(i)
                self.word_list.see(i)
                break

    def refresh_word_list(self) -> None:
        self.word_list.delete(0, tk.END)
        level = self.level_var.get()
        search = self.search_var.get().lower()
        for word in self.data.get(level, {}):
            if search in word.lower():
                self.word_list.insert(tk.END, word)

    def _show_details(self) -> None:
        word = self._selected_word()
        if not word:
            return
        level = find_word(self.data, word)
        self.details_text.delete("1.0", tk.END)
        if not level:
            self.details_text.insert(tk.END, f"Word: {word}\nLevel: (unknown)")
            return
        entry = self.data[level][word]
        self.details_text.insert(
            tk.END,
            f"Word: {word}\nLevel: {level}\nPart of Speech: {entry['part_of_speech']}\n\n"
            f"Definition:\n{entry['definition']}\n\nHomophones:\n",
        )
        for h in entry["homophones"]:
            self.details_text.insert(tk.END, f"  - {h}\n")

    def _add_word(self) -> None:
        level = self.level_var.get()
        word, pos, defn = self.new_word.get(), self.new_pos.get(), self.new_def.get()
        if self._mutate(lambda d: add_word(d, level, word, pos, defn)):
            self.new_word.delete(0, tk.END)
            self.new_pos.delete(0, tk.END)
            self.new_def.delete(0, tk.END)

    def _add_homophone(self) -> None:
        word = self._selected_word()
        if not word:
            return
        hom = self.new_hom.get()
        if self._mutate(lambda d: add_homophone(d, word, hom)):
            self._select_word(word)
            self._show_details()
            self.new_hom.delete(0, tk.END)

    def _move(self, delta: Literal[1, -1]) -> None:
        word = self._selected_word()
        if not word:
            return
        level = find_word(self.data, word)
        if level is None:
            return
        idx = LEVELS.index(level)
        if self._mutate(lambda d: move_word(d, word, delta)):
            self.level_var.set(LEVELS[idx + delta])
            self.refresh_word_list()

    def _copy_words(self, words: list[str], context: str) -> None:
        if not words:
            messagebox.showinfo("Empty", f"No words in {context}.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(words))
        messagebox.showinfo("Copied", f"Copied {len(words)} words from '{context}' to clipboard.")

    def _copy_level(self) -> None:
        level = self.level_var.get()
        self._copy_words(sorted(self.data.get(level, {}).keys(), key=str.lower), level)

    def _copy_all(self) -> None:
        all_words = sorted(
            (word for level in LEVELS for word in self.data.get(level, {})),
            key=str.lower,
        )
        self._copy_words(all_words, "all levels")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()