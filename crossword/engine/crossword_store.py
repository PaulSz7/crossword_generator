"""Persistent crossword document store.

Every generation attempt (success or failure) is saved as a compact JSON
document under ``local_db/collections/crosswords/``.  The format is
Firestore-ready (well under 1 MB) and frontend-consumable.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from ..core.constants import CellType
from ..utils.logger import get_logger

if TYPE_CHECKING:
    from ..data.dictionary import WordDictionary
    from ..data.theme import ThemeWord
    from ..engine.generator import CrosswordResult, GeneratorConfig
    from ..engine.grid import CrosswordGrid
    from ..core.models import Clue, WordSlot


LOGGER = get_logger(__name__)

DEFAULT_STORE_DIR = Path("local_db/collections/crosswords")


class CrosswordStore:
    """Save crossword generation results as compact structured JSON documents."""

    def __init__(self, store_dir: Path | str = DEFAULT_STORE_DIR) -> None:
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def save_success(
        self,
        result: "CrosswordResult",
        config: "GeneratorConfig",
        dictionary: Optional["WordDictionary"] = None,
        theme_cache_ref: Optional[str] = None,
    ) -> str:
        """Persist a successful crossword result and return its document ID."""
        doc_id = self._new_id()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = self._build_success_doc(doc_id, now, result, config, dictionary, theme_cache_ref)
        path = self.store_dir / f"{doc_id}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Crossword saved: %s", doc_id)
        return doc_id

    def update_to_success(
        self,
        doc_id: str,
        result: "CrosswordResult",
        config: "GeneratorConfig",
        dictionary: Optional["WordDictionary"] = None,
        theme_cache_ref: Optional[str] = None,
    ) -> str:
        """Overwrite an existing document (e.g. a filled checkpoint) with a success result.

        Preserves the original ``id`` and ``created_at`` so the document
        identity stays stable across resume attempts.
        """
        path = self.store_dir / f"{doc_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"No crossword document found: {doc_id}")
        original = json.loads(path.read_text(encoding="utf-8"))
        created_at = original.get("created_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        doc = self._build_success_doc(doc_id, created_at, result, config, dictionary, theme_cache_ref)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Crossword updated to success: %s", doc_id)
        return doc_id

    def save_failure(
        self,
        config: "GeneratorConfig",
        error: str,
        grid: Optional["CrosswordGrid"] = None,
        theme_words: Optional[List["ThemeWord"]] = None,
        crossword_title: Optional[str] = None,
        theme_content: Optional[str] = None,
        dictionary: Optional["WordDictionary"] = None,
        theme_cache_ref: Optional[str] = None,
    ) -> str:
        """Persist a failed generation attempt and return its document ID."""
        doc_id = self._new_id()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        doc = {
            "id": doc_id,
            "created_at": now,
            "status": "failed",
            "error": error,
            "title": crossword_title,
            "theme_content": theme_content,
            "width": config.width,
            "height": config.height,
            "difficulty": config.difficulty,
            "theme_title": config.theme_title,
            "seed": config.seed,
            "grid": self._encode_grid_string(grid) if grid is not None else None,
            "theme_cache_ref": theme_cache_ref,
        }

        path = self.store_dir / f"{doc_id}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Crossword failure saved: %s", doc_id)
        return doc_id

    def save_filled(
        self,
        grid: "CrosswordGrid",
        config: "GeneratorConfig",
        slots: List["WordSlot"],
        theme_words: Optional[List["ThemeWord"]] = None,
        crossword_title: Optional[str] = None,
        theme_content: Optional[str] = None,
        dictionary: Optional["WordDictionary"] = None,
        theme_cache_ref: Optional[str] = None,
    ) -> str:
        """Persist a filled grid checkpoint (pre-clue) and return its document ID.

        Saved after a successful fill + validation but before clue generation,
        so the expensive fill work can be resumed if clue generation fails.
        """
        doc_id = self._new_id()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        entries = self._build_entries(grid, slots)
        # Strip clue fields — grid is filled but clues haven't been generated yet
        for entry in entries:
            entry["clue"] = None
            entry.pop("hint_1", None)
            entry.pop("hint_2", None)

        tw_array = None
        if theme_words:
            tw_array = [
                {
                    "word": tw.word,
                    "clue": tw.clue,
                    "source": tw.source,
                    "long_clue": tw.long_clue,
                    "hint": tw.hint,
                    "has_user_clue": tw.has_user_clue,
                    "word_breaks": list(tw.word_breaks) if tw.word_breaks else [],
                }
                for tw in theme_words
            ]

        doc = {
            "id": doc_id,
            "created_at": now,
            "status": "filled",
            "title": crossword_title,
            "theme_content": theme_content,
            "width": config.width,
            "height": config.height,
            "difficulty": config.difficulty,
            "theme_title": config.theme_title,
            "seed": None,  # grid seed not tracked at config level
            "language": config.language,
            "allow_adult": config.allow_adult,
            "dictionary_path": str(config.dictionary_path),
            "grid": self._encode_grid_string(grid),
            "entries": entries,
            "blocker_zone": list(grid.blocker_zone) if grid.blocker_zone else None,
            "stats": self._compute_compact_stats(slots, dictionary),
            "theme_words": tw_array,
            "theme_cache_ref": theme_cache_ref,
        }

        path = self.store_dir / f"{doc_id}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Filled checkpoint saved: %s", doc_id)
        return doc_id

    def load(self, doc_id: str) -> dict:
        """Load a stored document by ID. Raises FileNotFoundError if missing."""
        path = self.store_dir / f"{doc_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"No crossword document found: {doc_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_grid_string(grid: "CrosswordGrid") -> str:
        """Encode the grid as a flat string: letter or '_' for non-letter cells."""
        chars: List[str] = []
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type == CellType.LETTER and cell.letter:
                    chars.append(cell.letter.upper())
                else:
                    chars.append("_")
        return "".join(chars)

    @staticmethod
    def _collect_clues(grid: "CrosswordGrid") -> Dict[str, "Clue"]:
        """Build a mapping from slot ID → Clue by scanning all CLUE_BOX cells."""
        clue_map: Dict[str, "Clue"] = {}
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type != CellType.CLUE_BOX:
                    continue
                for clue in cell.clues_hosted:
                    clue_map[clue.solution_word_ref_id] = clue
        return clue_map

    @classmethod
    def _build_entries(
        cls, grid: "CrosswordGrid", slots: List["WordSlot"]
    ) -> List[dict]:
        """Merge slots and clues into a single entries array."""
        clue_map = cls._collect_clues(grid)
        entries = []
        for slot in slots:
            clue = clue_map.get(slot.id)
            dir_char = "A" if slot.direction.value == "ACROSS" else "D"
            entry: dict = {
                "id": slot.id,
                "r": slot.start_row,
                "c": slot.start_col,
                "cb": list(slot.clue_box),
                "dir": dir_char,
                "len": slot.length,
                "answer": slot.text,
                "theme": slot.is_theme,
                "clue": clue.text if clue else None,
            }
            if clue and clue.hint_1:
                entry["hint_1"] = clue.hint_1
            if clue and clue.hint_2:
                entry["hint_2"] = clue.hint_2
            if slot.word_breaks:
                entry["word_breaks"] = list(slot.word_breaks)
            entries.append(entry)
        return entries

    @staticmethod
    def _compute_compact_stats(
        slots: list,
        dictionary: Optional["WordDictionary"] = None,
    ) -> dict:
        """Compute a compact stats dict suitable for frontend display."""
        words_3plus = [s for s in slots if s.text and len(s.text) >= 3]
        theme_slots = [s for s in slots if s.is_theme and s.text]
        fill_slots = [s for s in slots if not s.is_theme and s.text and len(s.text) >= 3]

        theme_coverage = (
            round(len(theme_slots) / len(words_3plus), 3) if words_3plus else 0.0
        )

        stats: dict = {
            "word_count": len(words_3plus),
            "theme_coverage": theme_coverage,
        }

        if dictionary and fill_slots:
            fill_scores: List[float] = []
            for s in fill_slots:
                entry = dictionary.get(s.text)
                if entry:
                    fill_scores.append(entry.difficulty_score)

            if fill_scores:
                total = len(fill_scores)
                avg_ds = sum(fill_scores) / total
                easy_n = sum(1 for s in fill_scores if s < 0.3)
                med_n = sum(1 for s in fill_scores if 0.3 <= s < 0.6)
                hard_n = sum(1 for s in fill_scores if s >= 0.6)
                stats["difficulty_avg"] = round(avg_ds, 3)
                stats["easy_pct"] = round(easy_n / total, 3)
                stats["medium_pct"] = round(med_n / total, 3)
                stats["hard_pct"] = round(hard_n / total, 3)

        return stats

    def _build_success_doc(
        self,
        doc_id: str,
        created_at: str,
        result: "CrosswordResult",
        config: "GeneratorConfig",
        dictionary: Optional["WordDictionary"],
        theme_cache_ref: Optional[str],
    ) -> dict:
        """Build the JSON-serialisable dict for a success document."""
        tw_array = None
        if result.theme_words:
            tw_array = [
                {
                    "word": tw.word,
                    "clue": tw.clue,
                    "source": tw.source,
                    "long_clue": tw.long_clue,
                    "hint": tw.hint,
                    "has_user_clue": tw.has_user_clue,
                    "word_breaks": list(tw.word_breaks) if tw.word_breaks else [],
                }
                for tw in result.theme_words
            ]
        return {
            "id": doc_id,
            "created_at": created_at,
            "status": "success",
            "title": result.crossword_title,
            "theme_content": result.theme_content,
            "width": config.width,
            "height": config.height,
            "difficulty": config.difficulty,
            "theme_title": config.theme_title,
            "seed": result.seed,
            "language": config.language,
            "allow_adult": config.allow_adult,
            "dictionary_path": str(config.dictionary_path),
            "grid": self._encode_grid_string(result.grid),
            "entries": self._build_entries(result.grid, result.slots),
            "blocker_zone": list(result.grid.blocker_zone) if result.grid.blocker_zone else None,
            "stats": self._compute_compact_stats(result.slots, dictionary),
            "theme_words": tw_array,
            "theme_cache_ref": theme_cache_ref,
        }

    @staticmethod
    def _new_id() -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"{ts}_{short_uuid}"
