"""Data models supporting the crossword generator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from .constants import CellType, Direction


@dataclass
class Clue:
    """Represents an entry in a clue box."""

    id: str
    text: str
    solution_word_ref_id: str
    solution_length: int
    direction: Direction
    start_offset_r: int
    start_offset_c: int


@dataclass
class Cell:
    """Represents a grid cell with metadata."""

    type: CellType = CellType.EMPTY_PLAYABLE
    letter: Optional[str] = None
    clues_hosted: List[Clue] = field(default_factory=list)
    part_of_word_ids: Set[str] = field(default_factory=set)

    def is_playable(self) -> bool:
        return self.type in {CellType.EMPTY_PLAYABLE, CellType.LETTER}

    def is_empty(self) -> bool:
        return self.type == CellType.EMPTY_PLAYABLE and self.letter is None


@dataclass
class WordSlot:
    """A potential or placed word in the grid."""

    id: str
    start_row: int
    start_col: int
    direction: Direction
    length: int
    clue_box: Tuple[int, int]
    text: Optional[str] = None
    is_theme: bool = False
    _cells: Optional[List[Tuple[int, int]]] = field(default=None, repr=False, compare=False)

    @property
    def cells(self) -> List[Tuple[int, int]]:
        if self._cells is None:
            if self.direction == Direction.ACROSS:
                self._cells = [(self.start_row, self.start_col + i) for i in range(self.length)]
            else:
                self._cells = [(self.start_row + i, self.start_col) for i in range(self.length)]
        return self._cells
