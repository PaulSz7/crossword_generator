"""Dictionary preprocessing and candidate retrieval."""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..core.constants import Difficulty
from ..core.exceptions import DictionaryLoadError
from .normalization import clean_word
from .preprocess import (
    ProcessedWordRecord,
    load_processed_dictionary,
    preprocess_dictionary,
)


@dataclass
class DictionaryConfig:
    """Configuration for dictionary loading and filtering."""

    path: Path | str
    min_length: int = 2
    max_length: int = 24
    min_frequency: float = 0.0
    exclude_stopwords: bool = True
    cache_theme_words: bool = True
    allow_compounds: bool = False
    max_entries_per_length: Optional[int] = None
    processed_cache: Path | str | None = None
    persist_processed_cache: bool = True
    rng: Optional[random.Random] = None
    difficulty: Difficulty = Difficulty.MEDIUM


_TIER_CENTER = {Difficulty.EASY: 0.15, Difficulty.MEDIUM: 0.45, Difficulty.HARD: 0.80}


@dataclass
class WordEntry:
    """Represents a sanitized dictionary entry."""

    surface: str
    raw_forms: Set[str]
    length: int
    definition: str
    lemma: str
    frequency: float
    is_compound: bool
    is_stopword: bool
    difficulty_score: float = 0.0

    def score(self, difficulty: Difficulty = Difficulty.MEDIUM) -> float:
        base = self.frequency
        if self.is_compound:
            base -= 0.15
        if self.is_stopword:
            base -= 0.3

        distance = abs(self.difficulty_score - _TIER_CENTER[difficulty])
        affinity = max(0.0, 1.0 - distance * 3.5)

        # Direction bonus: ensures off-tier words follow the right priority order.
        # Without this, high-frequency easy words outscore medium words for HARD
        # difficulty (and vice-versa), breaking HARD > MEDIUM > EASY ordering.
        if difficulty == Difficulty.EASY:
            direction = 1.0 - self.difficulty_score  # prefer lower DS
        elif difficulty == Difficulty.HARD:
            direction = self.difficulty_score         # prefer higher DS
        else:
            direction = 0.5                           # neutral for MEDIUM

        return max(0.0, base * 0.15 + affinity * 0.55 + direction * 0.30)


class WordDictionary:
    """Loads and filters Romanian lexemes from ``dex_words.tsv``."""

    def __init__(self, config: DictionaryConfig) -> None:
        self.config = config
        self._entries_by_length: Dict[int, List[WordEntry]] = defaultdict(list)
        self._entry_by_surface: Dict[str, WordEntry] = {}
        self._theme_cache: Dict[str, List[WordEntry]] = {}
        self._rng = config.rng or random.Random()
        self._letter_histogram: Dict[str, int] = defaultdict(int)
        self._total_letters = 0
        self._letter_frequency: Dict[str, float] = {}
        # Positional index: length -> (position, letter) -> set of surfaces
        self._position_index: Dict[int, Dict[Tuple[int, str], Set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self._surfaces_by_length: Dict[int, Set[str]] = defaultdict(set)
        self._load()

    # ------------------------------------------------------------------
    # Loading & preprocessing
    # ------------------------------------------------------------------
    def _load(self) -> None:
        source = Path(self.config.path)
        if not source.exists():
            raise DictionaryLoadError(f"Missing dictionary TSV: {source}")

        processed_path = self._resolve_processed_path(source)

        try:
            if processed_path and processed_path.exists():
                records = load_processed_dictionary(processed_path)
            else:
                destination = (
                    processed_path if processed_path and self.config.persist_processed_cache else None
                )
                records = preprocess_dictionary(source, destination)
        except Exception as exc:  # pragma: no cover - preprocessor errors are rare
            raise DictionaryLoadError(str(exc)) from exc

        self._hydrate_entries(records)

        if self.config.max_entries_per_length:
            difficulty = self.config.difficulty
            for length, entries in list(self._entries_by_length.items()):
                entries.sort(key=lambda e: e.score(difficulty), reverse=True)
                self._entries_by_length[length] = entries[: self.config.max_entries_per_length]
        self._finalize_letter_stats()

    def _hydrate_entries(self, records: Iterable[ProcessedWordRecord]) -> None:
        for record in records:
            if record.length < self.config.min_length or record.length > self.config.max_length:
                continue
            if record.frequency < self.config.min_frequency:
                continue
            if self.config.exclude_stopwords and record.is_stopword:
                continue
            if record.is_compound and not self.config.allow_compounds:
                continue

            entry = WordEntry(
                surface=record.surface,
                raw_forms=set(record.raw_forms),
                length=record.length,
                definition=record.definition,
                lemma=record.lemma,
                frequency=record.frequency,
                is_compound=record.is_compound,
                is_stopword=record.is_stopword,
                difficulty_score=record.difficulty_score,
            )
            self._entry_by_surface[entry.surface] = entry
            self._entries_by_length[entry.length].append(entry)
            self._surfaces_by_length[entry.length].add(entry.surface)
            # Build positional index
            length_index = self._position_index[entry.length]
            for pos, char in enumerate(entry.surface):
                length_index[(pos, char)].add(entry.surface)
            self._update_letter_stats(entry.surface)

    def _resolve_processed_path(self, source: Path) -> Optional[Path]:
        if self.config.processed_cache is None:
            return source.with_name(f"{source.stem}_processed{source.suffix}")
        cache = str(self.config.processed_cache).strip()
        if not cache:
            return None
        return Path(cache)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def sanitize(self, text: str) -> str:
        return clean_word(text)

    def contains(self, word: str) -> bool:
        return self.sanitize(word) in self._entry_by_surface

    def get(self, word: str) -> Optional[WordEntry]:
        return self._entry_by_surface.get(self.sanitize(word))

    def iter_length(self, length: int) -> Iterable[WordEntry]:
        return self._entries_by_length.get(length, [])

    def iter_all(self) -> Iterable[WordEntry]:
        return self._entry_by_surface.values()

    def find_candidates(
        self,
        length: int,
        pattern: Optional[Sequence[Optional[str]]] = None,
        banned: Optional[Set[str]] = None,
        preferred: Optional[Set[str]] = None,
        limit: int = 50,
        fallback_fraction: float = 0.0,
    ) -> List[WordEntry]:
        """Return candidates matching the supplied constraints.

        ``pattern`` is a sequence describing each cell (letter or ``None``).
        ``preferred`` boosts scoring for theme or high-priority entries.
        ``fallback_fraction`` reserves that fraction of ``limit`` for words
        scored by the next-lower difficulty tier (MEDIUM for EASY, MEDIUM for
        HARD), guaranteeing the solver always has off-tier backup candidates.
        Has no effect when difficulty is MEDIUM.
        """

        banned = banned or set()
        preferred = preferred or set()

        # Use positional index for fast candidate lookup
        matching = self._index_lookup(length, pattern)
        if matching is None:
            return []

        if banned:
            matching = matching - banned

        entries = [self._entry_by_surface[s] for s in matching if s in self._entry_by_surface]

        difficulty = self.config.difficulty

        def boosted_score(item: WordEntry) -> float:
            score = item.score(difficulty)
            if item.surface in preferred:
                score *= 1.4
            return score

        entries.sort(key=boosted_score, reverse=True)

        if fallback_fraction <= 0.0 or difficulty == Difficulty.MEDIUM:
            return entries[:limit]

        # Split the limit: primary tier gets (1 - fallback_fraction), secondary
        # gets fallback_fraction.  Secondary candidates are scored by MEDIUM so
        # the best medium-tier words are chosen regardless of whether difficulty
        # is EASY (medium > easy) or HARD (medium < hard).
        fallback_n = max(1, round(limit * fallback_fraction))
        primary_n = limit - fallback_n

        primary = entries[:primary_n]
        primary_surfaces = {e.surface for e in primary}

        # Score remaining matches by MEDIUM and pick the top fallback_n
        secondary_pool = [e for e in entries[primary_n:] if e.surface not in primary_surfaces]
        secondary_pool.sort(key=lambda e: e.score(Difficulty.MEDIUM), reverse=True)
        secondary = secondary_pool[:fallback_n]

        return primary + secondary

    def _index_lookup(
        self,
        length: int,
        pattern: Optional[Sequence[Optional[str]]],
    ) -> Optional[Set[str]]:
        """Use positional index to find matching surfaces via set intersection."""
        length_index = self._position_index.get(length)
        if not length_index:
            return None

        constraints: List[Set[str]] = []
        if pattern:
            for pos, letter in enumerate(pattern):
                if letter is not None:
                    key = (pos, letter)
                    match_set = length_index.get(key)
                    if match_set is None:
                        return set()
                    constraints.append(match_set)

        if not constraints:
            return set(self._surfaces_by_length.get(length, set()))

        # Intersect smallest sets first for speed
        constraints.sort(key=len)
        result = set(constraints[0])
        for s in constraints[1:]:
            result &= s
            if not result:
                return set()
        return result

    def letter_score(self, word: str) -> float:
        if not self._letter_frequency:
            return 0.0
        score = 0.0
        for char in word.upper():
            score += self._letter_frequency.get(char, 0.0)
        return score

    def has_candidates(
        self,
        length: int,
        pattern: Optional[Sequence[Optional[str]]] = None,
        banned: Optional[Set[str]] = None,
    ) -> bool:
        matching = self._index_lookup(length, pattern)
        if not matching:
            return False
        if banned:
            matching = matching - banned
        return bool(matching)

    def count_candidates(
        self,
        length: int,
        pattern: Optional[Sequence[Optional[str]]] = None,
        banned: Optional[Set[str]] = None,
    ) -> int:
        """Return the number of candidates matching the constraints (without materializing entries)."""
        matching = self._index_lookup(length, pattern)
        if not matching:
            return 0
        if banned:
            matching = matching - banned
        return len(matching)

    def theme_candidates(self, theme: str, limit: int = 80) -> List[WordEntry]:
        """Return entries whose definition or lemma reference ``theme``."""

        key = theme.lower().strip()
        if not key:
            return []
        if key in self._theme_cache:
            return self._theme_cache[key][:limit]

        difficulty = self.config.difficulty
        matches: List[Tuple[float, WordEntry]] = []
        for entry in self._entry_by_surface.values():
            haystack = f"{entry.definition} {entry.lemma}".lower()
            if key in haystack:
                matches.append((entry.score(difficulty), entry))

        matches.sort(key=lambda item: item[0], reverse=True)
        selected = [entry for _, entry in matches[:limit]]
        if self.config.cache_theme_words:
            self._theme_cache[key] = selected
        return selected

    def _update_letter_stats(self, surface: str) -> None:
        for char in surface:
            self._letter_histogram[char] += 1
            self._total_letters += 1

    def _finalize_letter_stats(self) -> None:
        if not self._total_letters:
            self._letter_frequency = {}
            return
        self._letter_frequency = {
            char: count / self._total_letters for char, count in self._letter_histogram.items()
        }
