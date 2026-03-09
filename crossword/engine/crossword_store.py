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

        doc = {
            "id": doc_id,
            "created_at": now,
            "status": "success",
            "title": result.crossword_title,
            "theme_content": result.theme_content,
            "width": config.width,
            "height": config.height,
            "difficulty": config.difficulty,
            "theme_title": config.theme_title,
            "seed": result.seed,
            "grid": self._encode_grid_string(result.grid),
            "entries": self._build_entries(result.grid, result.slots),
            "stats": self._compute_compact_stats(result.slots, dictionary),
            "theme_cache_ref": theme_cache_ref,
        }

        path = self.store_dir / f"{doc_id}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Crossword saved: %s", doc_id)
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

    @staticmethod
    def _new_id() -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"{ts}_{short_uuid}"
