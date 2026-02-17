"""Grid representation and helper utilities."""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..core.constants import CellType, Direction, ORTHOGONAL_STEPS, Bounds
from ..core.exceptions import ClueBoxError, SlotPlacementError
from ..utils.logger import get_logger
from ..core.models import Cell, WordSlot


LOGGER = get_logger(__name__)


@dataclass
class GridConfig:
    """Configuration values driving the grid layout."""

    height: int
    width: int
    min_blocker_size: int = 3
    max_blocker_size: int = 6
    place_blocker_zone: bool = True
    rng_seed: Optional[int] = None

    def bounds(self) -> Bounds:
        return Bounds(rows=self.height, cols=self.width)


@dataclass
class GridSnapshot:
    cells: List[List[Cell]]
    word_slots: Dict[str, WordSlot]
    clue_box_licenses: Dict[Tuple[int, int], Set[str]]
    blocker_zone: Optional[Tuple[int, int, int, int]]
    playable_count: int = 0
    filled_count: int = 0


class CrosswordGrid:
    """Encapsulates the crossword grid with placement helpers."""

    def __init__(self, config: GridConfig) -> None:
        self.config = config
        self.bounds = config.bounds()
        self.rng = random.Random(config.rng_seed)
        self.cells: List[List[Cell]] = [
            [Cell() for _ in range(self.bounds.cols)] for _ in range(self.bounds.rows)
        ]
        self.word_slots: Dict[str, WordSlot] = {}
        self.clue_box_licenses: Dict[Tuple[int, int], Set[str]] = {}
        self.blocker_zone: Optional[Tuple[int, int, int, int]] = None
        self._playable_count: int = self.bounds.rows * self.bounds.cols
        self._filled_count: int = 0
        self._place_initial_clue()
        if config.place_blocker_zone:
            self.place_blocker_zone()

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------
    def _place_initial_clue(self) -> None:
        LOGGER.debug("Planting mandatory top-left clue box")
        self._add_clue_box(0, 0)

    def place_blocker_zone(self) -> None:
        """Place a blocker zone in a random corner or near the center."""

        if self.blocker_zone is not None:
            return

        max_h = min(self.config.max_blocker_size, max(3, self.bounds.rows // 2))
        max_w = min(self.config.max_blocker_size, max(3, self.bounds.cols // 2))
        height = self.rng.randint(self.config.min_blocker_size, max_h)
        width = self.rng.randint(self.config.min_blocker_size, max_w)

        anchors = [
            (0, 0),  # top-left
            (0, self.bounds.cols - width),  # top-right
            (self.bounds.rows - height, 0),  # bottom-left
            (self.bounds.rows - height, self.bounds.cols - width),  # bottom-right
            ((self.bounds.rows - height) // 2, (self.bounds.cols - width) // 2),  # center
        ]
        start_row, start_col = self.rng.choice(anchors)
        LOGGER.info(
            "Placing blocker zone at (%s,%s) size %sx%s",
            start_row,
            start_col,
            height,
            width,
        )

        for r in range(start_row, min(start_row + height, self.bounds.rows)):
            for c in range(start_col, min(start_col + width, self.bounds.cols)):
                self._set_blocker_cell(r, c)

        self.blocker_zone = (start_row, start_col, height, width)
        if start_row == 0 and start_col == 0:
            # Plant clue boxes at the top-left of remaining playable areas.
            h, w = height, width
            if w < self.bounds.cols:
                self._safe_add_clue_box(0, w)
            if h < self.bounds.rows:
                self._safe_add_clue_box(h, 0)

    # ------------------------------------------------------------------
    # Cell manipulation
    # ------------------------------------------------------------------
    def _set_blocker_cell(self, row: int, col: int) -> None:
        cell = self.cells[row][col]
        was_playable = cell.type in {CellType.EMPTY_PLAYABLE, CellType.LETTER}
        was_filled = cell.type == CellType.LETTER
        cell.type = CellType.BLOCKER_ZONE
        cell.letter = None
        cell.part_of_word_ids.clear()
        cell.clues_hosted.clear()
        self.clue_box_licenses.pop((row, col), None)
        if was_playable:
            self._playable_count -= 1
        if was_filled:
            self._filled_count -= 1

    def _add_clue_box(self, row: int, col: int) -> None:
        if not self.bounds.contains(row, col):
            raise ClueBoxError(f"Clue box outside bounds: {(row, col)}")
        cell = self.cells[row][col]
        if cell.type == CellType.BLOCKER_ZONE:
            raise ClueBoxError("Cannot convert blocker zone into clue box")
        if not self._can_place_clue_box(row, col):
            raise ClueBoxError(f"Clue adjacency violation at {(row, col)}")
        was_playable = cell.type in {CellType.EMPTY_PLAYABLE, CellType.LETTER}
        was_filled = cell.type == CellType.LETTER
        cell.type = CellType.CLUE_BOX
        cell.letter = None
        cell.part_of_word_ids.clear()
        cell.clues_hosted.clear()
        self.clue_box_licenses[(row, col)] = set()
        if was_playable:
            self._playable_count -= 1
        if was_filled:
            self._filled_count -= 1

    def _safe_add_clue_box(self, row: int, col: int) -> None:
        try:
            self._add_clue_box(row, col)
        except ClueBoxError as exc:
            LOGGER.warning("Failed to add auto clue box at (%s,%s): %s", row, col, exc)

    def _can_place_clue_box(self, row: int, col: int) -> bool:
        # No clue boxes in the bottom-right 2x2 corner
        if row >= self.bounds.rows - 2 and col >= self.bounds.cols - 2:
            return False

        for dr, dc in ORTHOGONAL_STEPS:
            nr, nc = row + dr, col + dc
            if not self.bounds.contains(nr, nc):
                continue
            neighbor = self.cells[nr][nc]
            if neighbor.type == CellType.CLUE_BOX:
                return False

        # Would this isolate any neighboring playable cell?
        for dr, dc in ORTHOGONAL_STEPS:
            nr, nc = row + dr, col + dc
            if not self.bounds.contains(nr, nc):
                continue
            if self.cells[nr][nc].type != CellType.EMPTY_PLAYABLE:
                continue
            # Check if (nr, nc) keeps at least one playable neighbor
            has_playable = False
            for dr2, dc2 in ORTHOGONAL_STEPS:
                nnr, nnc = nr + dr2, nc + dc2
                if (nnr, nnc) == (row, col):
                    continue  # this cell is becoming CLUE_BOX
                if not self.bounds.contains(nnr, nnc):
                    continue
                if self.cells[nnr][nnc].type in (CellType.EMPTY_PLAYABLE, CellType.LETTER):
                    has_playable = True
                    break
            if not has_playable:
                return False
        return True

    def snapshot(self) -> GridSnapshot:
        return GridSnapshot(
            cells=copy.deepcopy(self.cells),
            word_slots=copy.deepcopy(self.word_slots),
            clue_box_licenses={k: set(v) for k, v in self.clue_box_licenses.items()},
            blocker_zone=self.blocker_zone,
            playable_count=self._playable_count,
            filled_count=self._filled_count,
        )

    def restore(self, snapshot: GridSnapshot) -> None:
        self.cells = copy.deepcopy(snapshot.cells)
        self.word_slots = copy.deepcopy(snapshot.word_slots)
        self.clue_box_licenses = {k: set(v) for k, v in snapshot.clue_box_licenses.items()}
        self.blocker_zone = snapshot.blocker_zone
        self._playable_count = snapshot.playable_count
        self._filled_count = snapshot.filled_count

    @property
    def filled_ratio(self) -> float:
        return (self._filled_count / self._playable_count) if self._playable_count else 0.0

    # ------------------------------------------------------------------
    # Word placement
    # ------------------------------------------------------------------
    def place_word(self, slot: WordSlot, text: str) -> None:
        text = text.upper()
        if len(text) != slot.length:
            raise SlotPlacementError("Word length mismatch")

        for index, (row, col) in enumerate(slot.cells):
            if not self.bounds.contains(row, col):
                raise SlotPlacementError("Word extends outside grid")
            cell = self.cells[row][col]
            if cell.type == CellType.CLUE_BOX or cell.type == CellType.BLOCKER_ZONE:
                raise SlotPlacementError("Word overlaps blocked cell")
            existing = cell.letter
            letter = text[index]
            if existing and existing != letter:
                raise SlotPlacementError("Letter conflict")

        # All checks passed, mutate grid
        for index, (row, col) in enumerate(slot.cells):
            cell = self.cells[row][col]
            if cell.type == CellType.EMPTY_PLAYABLE:
                self._filled_count += 1
            cell.type = CellType.LETTER
            cell.letter = text[index]
            cell.part_of_word_ids.add(slot.id)

        slot.text = text
        self.word_slots[slot.id] = slot
        self.clue_box_licenses.setdefault(slot.clue_box, set()).add(slot.id)

    def remove_word(self, slot_id: str) -> None:
        slot = self.word_slots.get(slot_id)
        if not slot:
            return
        for row, col in slot.cells:
            cell = self.cells[row][col]
            cell.part_of_word_ids.discard(slot_id)
            if not cell.part_of_word_ids:
                cell.type = CellType.EMPTY_PLAYABLE
                cell.letter = None
                self._filled_count -= 1
        self.clue_box_licenses.get(slot.clue_box, set()).discard(slot_id)
        del self.word_slots[slot_id]

    def place_word_undoable(self, slot: WordSlot, text: str):
        """Place a word and return an undo callable for efficient backtracking."""
        text = text.upper()
        if len(text) != slot.length:
            raise SlotPlacementError("Word length mismatch")

        # Record state before mutation for undo
        old_states = []
        for index, (row, col) in enumerate(slot.cells):
            if not self.bounds.contains(row, col):
                raise SlotPlacementError("Word extends outside grid")
            cell = self.cells[row][col]
            if cell.type == CellType.CLUE_BOX or cell.type == CellType.BLOCKER_ZONE:
                raise SlotPlacementError("Word overlaps blocked cell")
            existing = cell.letter
            letter = text[index]
            if existing and existing != letter:
                raise SlotPlacementError("Letter conflict")
            old_states.append((row, col, cell.type, cell.letter))

        # Mutate
        newly_filled = 0
        for index, (row, col) in enumerate(slot.cells):
            cell = self.cells[row][col]
            if cell.type == CellType.EMPTY_PLAYABLE:
                newly_filled += 1
            cell.type = CellType.LETTER
            cell.letter = text[index]
            cell.part_of_word_ids.add(slot.id)
        self._filled_count += newly_filled
        slot.text = text
        self.word_slots[slot.id] = slot
        self.clue_box_licenses.setdefault(slot.clue_box, set()).add(slot.id)

        def undo():
            for row, col, old_type, old_letter in old_states:
                cell = self.cells[row][col]
                cell.part_of_word_ids.discard(slot.id)
                if not cell.part_of_word_ids:
                    cell.type = old_type
                    cell.letter = old_letter
            self._filled_count -= newly_filled
            self.clue_box_licenses.get(slot.clue_box, set()).discard(slot.id)
            if slot.id in self.word_slots:
                del self.word_slots[slot.id]
            slot.text = None

        return undo

    def ensure_terminal_boundary(self, slot: WordSlot) -> Optional[Tuple[int, int, Direction]]:
        """Ensure the cell after the word is a valid boundary, optionally returning a new start."""

        dr, dc = (0, 1) if slot.direction == Direction.ACROSS else (1, 0)
        end_row = slot.start_row + dr * (slot.length - 1)
        end_col = slot.start_col + dc * (slot.length - 1)
        next_row = end_row + dr
        next_col = end_col + dc
        if not self.bounds.contains(next_row, next_col):
            return None
        next_cell = self.cells[next_row][next_col]
        if next_cell.type == CellType.BLOCKER_ZONE:
            return None
        if next_cell.type == CellType.LETTER:
            raise SlotPlacementError("Word collides with another slot at terminal cell")

        if slot.direction == Direction.ACROSS:
            start_row = next_row
            start_col = next_col + 1
        else:
            start_row = next_row + 1
            start_col = next_col
        has_capacity = self._has_capacity_for_start(start_row, start_col, slot.direction)
        if not has_capacity:
            if next_cell.type != CellType.CLUE_BOX:
                raise SlotPlacementError("Terminal clue would strand unusable cells")
            return None

        if next_cell.type != CellType.CLUE_BOX:
            self._add_clue_box(next_row, next_col)

        if self.bounds.contains(start_row, start_col):
            start_cell = self.cells[start_row][start_col]
            if start_cell.type == CellType.EMPTY_PLAYABLE:
                return (start_row, start_col, slot.direction)
        return None

    # ------------------------------------------------------------------
    # Clue handling
    # ------------------------------------------------------------------
    def ensure_clue_box(self, row: int, col: int, direction: Direction) -> Tuple[int, int]:
        """Find or create a clue box licensing this start cell."""

        allowed_offsets = self._clue_offsets(direction)
        candidates: List[Tuple[int, int, int]] = []
        for dr, dc in allowed_offsets:
            nr, nc = row + dr, col + dc
            if not self.bounds.contains(nr, nc):
                continue
            neighbor = self.cells[nr][nc]
            if neighbor.type == CellType.CLUE_BOX:
                licenses = len(self.clue_box_licenses.get((nr, nc), set()))
                candidates.append((licenses, nr, nc))

        if candidates:
            candidates.sort(key=lambda item: item[0])
            _, nr, nc = candidates[0]
            return nr, nc

        # Need to create a new clue box. Prefer offsets with space.
        for dr, dc in allowed_offsets:
            nr, nc = row + dr, col + dc
            if not self.bounds.contains(nr, nc):
                continue
            if self.cells[nr][nc].type in {CellType.LETTER, CellType.BLOCKER_ZONE}:
                continue
            try:
                self._add_clue_box(nr, nc)
                return nr, nc
            except ClueBoxError:
                continue

        raise ClueBoxError("Unable to license word start with valid clue box")

    @staticmethod
    def _clue_offsets(direction: Direction) -> Sequence[Tuple[int, int]]:
        if direction == Direction.ACROSS:
            return ((0, -1), (-1, 0), (1, 0))
        return ((-1, 0), (0, -1), (0, 1))

    def _has_capacity_for_start(self, row: int, col: int, direction: Direction) -> bool:
        if not self.bounds.contains(row, col):
            return False
        cell = self.cells[row][col]
        if cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
            return False
        dr, dc = (0, 1) if direction == Direction.ACROSS else (1, 0)
        length = 0
        r, c = row, col
        while self.bounds.contains(r, c):
            current = self.cells[r][c]
            if current.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
                break
            length += 1
            if length >= 2:
                return True
            r += dr
            c += dc
        return False

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def cell(self, row: int, col: int) -> Cell:
        return self.cells[row][col]

    def neighbors(self, row: int, col: int) -> Iterable[Tuple[int, int]]:
        for dr, dc in ORTHOGONAL_STEPS:
            nr, nc = row + dr, col + dc
            if self.bounds.contains(nr, nc):
                yield nr, nc

    def is_boundary(self, row: int, col: int, direction: Direction) -> bool:
        if direction == Direction.ACROSS:
            left_col = col - 1
            if left_col < 0:
                return True
            left_cell = self.cells[row][left_col]
            return left_cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}
        up_row = row - 1
        if up_row < 0:
            return True
        up_cell = self.cells[up_row][col]
        return up_cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}

    def enumerate_slots(self) -> List[WordSlot]:
        """Derive current across and down slots (filled or not)."""

        slots: List[WordSlot] = []
        # Across
        for r in range(self.bounds.rows):
            c = 0
            while c < self.bounds.cols:
                cell = self.cells[r][c]
                if cell.type == CellType.LETTER and self.is_boundary(r, c, Direction.ACROSS):
                    length, coords = self._collect_slot(r, c, Direction.ACROSS)
                    if length >= 2:
                        slots.append(
                            WordSlot(
                                id=f"AC_{r}_{c}",
                                start_row=r,
                                start_col=c,
                                direction=Direction.ACROSS,
                                length=length,
                                clue_box=self._find_clue_for_start(r, c, Direction.ACROSS),
                                text="".join(self.cells[row][col].letter or "" for row, col in coords),
                            )
                        )
                    c += length
                    continue
                c += 1

        # Down
        for c in range(self.bounds.cols):
            r = 0
            while r < self.bounds.rows:
                cell = self.cells[r][c]
                if cell.type == CellType.LETTER and self.is_boundary(r, c, Direction.DOWN):
                    length, coords = self._collect_slot(r, c, Direction.DOWN)
                    if length >= 2:
                        slots.append(
                            WordSlot(
                                id=f"DN_{r}_{c}",
                                start_row=r,
                                start_col=c,
                                direction=Direction.DOWN,
                                length=length,
                                clue_box=self._find_clue_for_start(r, c, Direction.DOWN),
                                text="".join(self.cells[row][col].letter or "" for row, col in coords),
                            )
                        )
                    r += length
                    continue
                r += 1
        return slots

    def _collect_slot(self, row: int, col: int, direction: Direction) -> Tuple[int, List[Tuple[int, int]]]:
        coords: List[Tuple[int, int]] = []
        if direction == Direction.ACROSS:
            c = col
            while c < self.bounds.cols and self.cells[row][c].type == CellType.LETTER:
                coords.append((row, c))
                c += 1
        else:
            r = row
            while r < self.bounds.rows and self.cells[r][col].type == CellType.LETTER:
                coords.append((r, col))
                r += 1
        return len(coords), coords

    def _find_clue_for_start(self, row: int, col: int, direction: Direction) -> Tuple[int, int]:
        offsets = self._clue_offsets(direction)
        for dr, dc in offsets:
            nr, nc = row + dr, col + dc
            if self.bounds.contains(nr, nc) and self.cells[nr][nc].type == CellType.CLUE_BOX:
                return nr, nc
        raise ClueBoxError("Existing slot missing licensing clue box")

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------
    def to_jsonable(self) -> List[List[dict]]:
        serialized: List[List[dict]] = []
        for row in self.cells:
            serialized_row: List[dict] = []
            for cell in row:
                serialized_row.append(
                    {
                        "type": cell.type.value,
                        "letter": cell.letter,
                        "clues_hosted": [
                            {
                                "id": clue.id,
                                "text": clue.text,
                                "solution_word_ref_id": clue.solution_word_ref_id,
                                "solution_length": clue.solution_length,
                                "direction": clue.direction.value,
                                "start_offset_r": clue.start_offset_r,
                                "start_offset_c": clue.start_offset_c,
                            }
                            for clue in cell.clues_hosted
                        ],
                        "part_of_word_ids": sorted(cell.part_of_word_ids),
                    }
                )
            serialized.append(serialized_row)
        return serialized
