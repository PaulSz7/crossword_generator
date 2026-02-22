"""Shared constants and enumerations for the crossword generator."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class Difficulty(str, Enum):
    """Crossword difficulty levels."""

    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class CellType(str, Enum):
    """All supported cell types in the grid."""

    EMPTY_PLAYABLE = "EMPTY_PLAYABLE"
    LETTER = "LETTER"
    CLUE_BOX = "CLUE_BOX"
    BLOCKER_ZONE = "BLOCKER_ZONE"


class Direction(str, Enum):
    """Word directions supported by the grid."""

    ACROSS = "ACROSS"
    DOWN = "DOWN"


ORTHOGONAL_STEPS: Tuple[Tuple[int, int], ...] = ((0, 1), (1, 0), (0, -1), (-1, 0))
DIAGONAL_STEPS: Tuple[Tuple[int, int], ...] = ((1, 1), (1, -1), (-1, 1), (-1, -1))


@dataclass(frozen=True)
class Bounds:
    """Simple rectangle bounds helper."""

    rows: int
    cols: int

    def contains(self, row: int, col: int) -> bool:
        return 0 <= row < self.rows and 0 <= col < self.cols
