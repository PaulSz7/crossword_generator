"""Deterministic rule validation for generated crosswords."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Set

from ..core.constants import CellType, Direction, ORTHOGONAL_STEPS
from ..data.dictionary import WordDictionary
from ..core.exceptions import ValidationError
from .grid import CrosswordGrid
from ..utils.logger import get_logger


LOGGER = get_logger(__name__)


@dataclass
class ValidationResult:
    ok: bool
    messages: List[str]


class GridValidator:
    """Runs deterministic validation over the final grid."""

    def __init__(self, dictionary: WordDictionary) -> None:
        self.dictionary = dictionary

    def validate(self, grid: CrosswordGrid, theme_words: Optional[Set[str]] = None) -> ValidationResult:
        messages: List[str] = []
        try:
            self._check_clue_boxes(grid)
            self._check_no_clue_box_in_bottom_right(grid)
            self._check_no_isolated_cells(grid)
            self._check_no_duplicate_words(grid)
            self._check_letters_valid(grid)
            self._check_sequences(grid, theme_words or set())
        except ValidationError as exc:
            messages.append(str(exc))
            LOGGER.error("Validation failed: %s", exc)
            return ValidationResult(ok=False, messages=messages)
        return ValidationResult(ok=True, messages=[])

    def _check_clue_boxes(self, grid: CrosswordGrid) -> None:
        for (row, col), licenses in grid.clue_box_licenses.items():
            cell = grid.cell(row, col)
            if cell.type != CellType.CLUE_BOX:
                continue
            if not licenses:
                raise ValidationError(f"Clue box at {(row, col)} does not license any word")
            for dr, dc in ORTHOGONAL_STEPS:
                nr, nc = row + dr, col + dc
                if not grid.bounds.contains(nr, nc):
                    continue
                neighbor = grid.cell(nr, nc)
                if neighbor.type == CellType.CLUE_BOX:
                    raise ValidationError(
                        f"Clue box adjacency violation between {(row, col)} and {(nr, nc)}"
                    )

    def _check_no_clue_box_in_bottom_right(self, grid: CrosswordGrid) -> None:
        for r in range(grid.bounds.rows - 2, grid.bounds.rows):
            for c in range(grid.bounds.cols - 2, grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type == CellType.CLUE_BOX:
                    raise ValidationError(
                        f"Clue box at ({r},{c}) is in the bottom-right 2x2 corner"
                    )

    def _check_letters_valid(self, grid: CrosswordGrid) -> None:
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type == CellType.LETTER:
                    if not cell.letter or not cell.letter.isalpha() or not cell.letter.isupper():
                        raise ValidationError(
                            f"Invalid letter '{cell.letter}' at ({r},{c})"
                        )

    def _check_no_isolated_cells(self, grid: CrosswordGrid) -> None:
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type != CellType.EMPTY_PLAYABLE:
                    continue
                has_playable = any(
                    grid.bounds.contains(r + dr, c + dc)
                    and grid.cell(r + dr, c + dc).type in (CellType.EMPTY_PLAYABLE, CellType.LETTER)
                    for dr, dc in ORTHOGONAL_STEPS
                )
                if not has_playable:
                    raise ValidationError(f"Isolated unreachable cell at ({r},{c})")

    def _check_no_duplicate_words(self, grid: CrosswordGrid) -> None:
        slots = grid.enumerate_slots()
        seen: Set[str] = set()
        for slot in slots:
            text = (slot.text or "").upper()
            if not text:
                continue
            if text in seen:
                raise ValidationError(
                    f"Duplicate word '{text}' at ({slot.start_row},{slot.start_col})"
                )
            seen.add(text)

    def _check_sequences(self, grid: CrosswordGrid, theme_words: Set[str]) -> None:
        slots = grid.enumerate_slots()
        for slot in slots:
            if slot.length >= 3:
                text = (slot.text or "").upper()
                if text not in theme_words and not self.dictionary.contains(text):
                    raise ValidationError(
                        f"Invalid word '{slot.text}' at {(slot.start_row, slot.start_col)}"
                    )
            if not self._has_valid_license(grid, slot):
                raise ValidationError(
                    f"Slot {slot.id} missing valid clue adjacency"
                )

    @staticmethod
    def _has_valid_license(grid: CrosswordGrid, slot) -> bool:
        if slot.direction == Direction.ACROSS:
            offsets = ((0, -1), (-1, 0), (1, 0))
        else:
            offsets = ((-1, 0), (0, -1), (0, 1))
        for dr, dc in offsets:
            nr, nc = slot.start_row + dr, slot.start_col + dc
            if grid.bounds.contains(nr, nc) and grid.cell(nr, nc).type == CellType.CLUE_BOX:
                return True
        return False
