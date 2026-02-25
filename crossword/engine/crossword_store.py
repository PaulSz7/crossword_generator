"""Persistent crossword document store.

Every generation attempt (success or failure) is saved as a JSON document
under ``local_db/collections/crosswords/``.  The documents are
frontend-ready and contain the full grid state, all slots, clues, and stats.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from ..core.constants import CellType
from ..utils.logger import get_logger

if TYPE_CHECKING:
    from ..data.dictionary import WordDictionary
    from ..data.theme import ThemeWord
    from ..engine.generator import CrosswordResult, GeneratorConfig
    from ..engine.grid import CrosswordGrid


LOGGER = get_logger(__name__)

DEFAULT_STORE_DIR = Path("local_db/collections/crosswords")


class CrosswordStore:
    """Save crossword generation results as structured JSON documents."""

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
        now = datetime.now(timezone.utc).isoformat()

        doc = {
            "id": doc_id,
            "created_at": now,
            "status": "success",
            "config": self._serialize_config(config),
            "theme_cache_ref": theme_cache_ref,
            "crossword_title": result.crossword_title,
            "theme_content": result.theme_content,
            "theme_words": [
                {"word": tw.word, "clue": tw.clue, "source": tw.source}
                for tw in result.theme_words
            ],
            "slots": self._serialize_slots(result.slots),
            "clues": self._extract_clues(result.grid),
            "validation": result.validation_messages,
            "seed": result.seed,
            "grid": result.grid.to_jsonable(),
            "stats": self._compute_stats(result.grid, result.slots, dictionary=dictionary),
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
        now = datetime.now(timezone.utc).isoformat()

        slots = list(grid.word_slots.values()) if grid is not None else []

        doc = {
            "id": doc_id,
            "created_at": now,
            "status": "failed",
            "error": error,
            "config": self._serialize_config(config),
            "theme_cache_ref": theme_cache_ref,
            "crossword_title": crossword_title,
            "theme_content": theme_content,
            "theme_words": [
                {"word": tw.word, "clue": tw.clue, "source": tw.source}
                for tw in (theme_words or [])
            ],
            "grid": grid.to_jsonable() if grid is not None else None,
            # Partial stats: grid + words sections only (no difficulty section)
            "stats": self._compute_stats(grid, slots) if grid is not None else {},
        }

        path = self.store_dir / f"{doc_id}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Crossword failure saved: %s", doc_id)
        return doc_id

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_clues(self, grid: "CrosswordGrid") -> list:
        """Collect all clues from CLUE_BOX cells."""
        clues = []
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type != CellType.CLUE_BOX:
                    continue
                for clue in cell.clues_hosted:
                    clues.append({
                        "id": clue.id,
                        "text": clue.text,
                        "solution_word_ref_id": clue.solution_word_ref_id,
                        "solution_length": clue.solution_length,
                        "direction": clue.direction.value,
                        "clue_box": [r, c],
                        "start_offset_r": clue.start_offset_r,
                        "start_offset_c": clue.start_offset_c,
                    })
        return clues

    def _compute_stats(
        self,
        grid: "CrosswordGrid",
        slots: list,
        dictionary: Optional["WordDictionary"] = None,
    ) -> dict:
        """Compute stats dict mirroring print_crossword_stats logic.

        If ``dictionary`` is None the difficulty section is omitted.
        """
        total_cells = grid.bounds.rows * grid.bounds.cols
        letter_cells = clue_cells = blocker_cells = empty_cells = 0
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                ct = grid.cell(r, c).type
                if ct == CellType.LETTER:
                    letter_cells += 1
                elif ct == CellType.CLUE_BOX:
                    clue_cells += 1
                elif ct == CellType.BLOCKER_ZONE:
                    blocker_cells += 1
                elif ct == CellType.EMPTY_PLAYABLE:
                    empty_cells += 1

        words = [s.text for s in slots if s.text and len(s.text) >= 2]
        words_3plus = [w for w in words if len(w) >= 3]
        theme_slots = [s for s in slots if s.is_theme]
        fill_slots = [s for s in slots if not s.is_theme and s.text and len(s.text) >= 3]
        lengths = [len(w) for w in words_3plus]
        length_dist = Counter(lengths)

        stats: dict = {
            "grid": {
                "rows": grid.bounds.rows,
                "cols": grid.bounds.cols,
                "total_cells": total_cells,
                "letter_cells": letter_cells,
                "clue_boxes": clue_cells,
                "blocker_cells": blocker_cells,
                "unfilled_cells": empty_cells,
            },
            "words": {
                "total_slots": len(slots),
                "words_3plus": len(words_3plus),
                "theme_words": len(theme_slots),
                "fill_words": len(fill_slots),
                "length_min": min(lengths) if lengths else 0,
                "length_max": max(lengths) if lengths else 0,
                "length_avg": round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
                "length_distribution": {str(k): v for k, v in sorted(length_dist.items())},
            },
        }

        if dictionary and words_3plus:
            fill_scores: List[float] = []
            fill_freqs: List[float] = []
            theme_scores: List[float] = []

            for s in fill_slots:
                if s.text:
                    entry = dictionary.get(s.text)
                    if entry:
                        fill_scores.append(entry.difficulty_score)
                        fill_freqs.append(entry.frequency)

            for s in theme_slots:
                if s.text:
                    entry = dictionary.get(s.text)
                    if entry:
                        theme_scores.append(entry.difficulty_score)

            if fill_scores:
                avg_ds = sum(fill_scores) / len(fill_scores)
                avg_freq = sum(fill_freqs) / len(fill_freqs)
                easy_n = sum(1 for s in fill_scores if s < 0.3)
                med_n = sum(1 for s in fill_scores if 0.3 <= s < 0.6)
                hard_n = sum(1 for s in fill_scores if s >= 0.6)
                total_scored = len(fill_scores)

                stats["difficulty"] = {
                    "avg_score": round(avg_ds, 3),
                    "avg_frequency": round(avg_freq, 3),
                    "easy_count": easy_n,
                    "easy_pct": round(easy_n / total_scored * 100, 1),
                    "medium_count": med_n,
                    "medium_pct": round(med_n / total_scored * 100, 1),
                    "hard_count": hard_n,
                    "hard_pct": round(hard_n / total_scored * 100, 1),
                    "dict_coverage": f"{total_scored}/{len(fill_slots)}",
                    "theme_avg_score": (
                        round(sum(theme_scores) / len(theme_scores), 3)
                        if theme_scores else None
                    ),
                }

        return stats

    @staticmethod
    def _serialize_slots(slots: list) -> list:
        return [
            {
                "id": slot.id,
                "start": [slot.start_row, slot.start_col],
                "direction": slot.direction.value,
                "length": slot.length,
                "text": slot.text,
                "clue_box": list(slot.clue_box),
                "is_theme": slot.is_theme,
            }
            for slot in slots
        ]

    @staticmethod
    def _serialize_config(config: "GeneratorConfig") -> dict:
        return {
            "height": config.height,
            "width": config.width,
            "theme_title": config.theme_title,
            "theme_type": config.theme_type,
            "theme_description": config.theme_description,
            "difficulty": config.difficulty,
            "language": config.language,
            "seed": config.seed,
            "completion_target": config.completion_target,
            "min_theme_coverage": config.min_theme_coverage,
            "place_blocker_zone": config.place_blocker_zone,
        }

    @staticmethod
    def _new_id() -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"{ts}_{short_uuid}"
