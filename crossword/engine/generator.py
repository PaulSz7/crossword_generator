"""Main crossword generator orchestration.

Two-phase approach:
  1. Layout: place blocker zone, theme words, then clue boxes to define all slots.
  2. Fill: use CP-SAT solver to fill all remaining slots.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
import heapq
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..io.clues import ClueGenerator, ClueRequest, TemplateClueGenerator, attach_clues_to_grid
from ..core.constants import CellType, Direction, ORTHOGONAL_STEPS
from ..data.dictionary import DictionaryConfig, WordDictionary, WordEntry
from ..core.exceptions import (ClueBoxError, CrosswordError, SlotPlacementError,
                               ThemeWordError, ValidationError)
from .grid import CrosswordGrid, GridConfig
from ..utils.logger import get_logger
from ..core.models import Clue, WordSlot
from ..data.theme import DummyThemeWordGenerator, ThemeWord, ThemeWordGenerator, merge_theme_generators
from .validator import GridValidator


LOGGER = get_logger(__name__)


@dataclass
class GeneratorConfig:
    height: int
    width: int
    dictionary_path: Path | str
    theme: str
    seed: Optional[int] = None
    completion_target: float = 0.85
    max_iterations: int = 2500
    retry_limit: int = 3
    min_theme_coverage: float = 0.10
    max_theme_ratio: float = 0.4
    theme_request_size: int = 80
    theme_placement_attempts: int = 30
    prefer_theme_candidates: bool = True
    fill_timeout_seconds: float = 180.0

    def to_grid_config(self, seed_override: Optional[int] = None) -> GridConfig:
        return GridConfig(
            height=self.height,
            width=self.width,
            rng_seed=seed_override if seed_override is not None else self.seed,
        )

    def to_dictionary_config(self) -> DictionaryConfig:
        return DictionaryConfig(path=self.dictionary_path, rng=random.Random(self.seed))


@dataclass
class CrosswordResult:
    grid: CrosswordGrid
    slots: List[WordSlot]
    theme_words: List[ThemeWord]
    validation_messages: List[str] = field(default_factory=list)
    seed: Optional[int] = None


@dataclass
class SlotSignature:
    start_row: int
    start_col: int
    direction: Direction
    cells: List[Tuple[int, int]]

    @property
    def length(self) -> int:
        return len(self.cells)


class CrosswordGenerator:
    """High-level orchestrator: layout generation then CP-SAT filling."""

    def __init__(
        self,
        config: GeneratorConfig,
        dictionary: Optional[WordDictionary] = None,
        theme_generator: Optional[ThemeWordGenerator] = None,
        clue_generator: Optional[ClueGenerator] = None,
    ) -> None:
        self.config = config
        self.rng = random.Random(config.seed)
        self.dictionary = dictionary or WordDictionary(config.to_dictionary_config())
        self.theme_generator = theme_generator
        self.theme_fallback_generators = [
            DummyThemeWordGenerator(seed=config.seed),
        ]
        self.clue_generator = clue_generator or TemplateClueGenerator()
        self.validator = GridValidator(self.dictionary)
        self.used_words: Set[str] = set()
        self.remaining_theme_words: Set[str] = set()
        self.theme_word_surfaces: Set[str] = set()
        self._slot_counter = 0
        self._occupied_slots: Set[str] = set()
        self._slot_keys: Dict[str, str] = {}
        self._placement_history: List[str] = []
        self.pending_starts: List[Tuple[float, int, Tuple[int, int, Direction]]] = []
        self._pending_counter = 0
        self._layout_rng = random.Random(config.seed)

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------
    def generate(self) -> CrosswordResult:
        for attempt in range(1, self.config.retry_limit + 1):
            LOGGER.info("Generation attempt %s/%s", attempt, self.config.retry_limit)
            try:
                grid_seed = self.rng.randint(0, 1_000_000)
                grid = CrosswordGrid(self.config.to_grid_config(seed_override=grid_seed))
                self._anneal_layout(grid)
                theme_words = self._seed_theme_words(grid)
                self._complete_layout(grid)
                self._cpsat_fill(grid)
                theme_word_surfaces = self.theme_word_surfaces.copy()
                validation = self.validator.validate(grid, theme_word_surfaces)
                if not validation.ok:
                    raise ValidationError(
                        f"Grid validation failed: {validation.messages}"
                    )
                slots = list(grid.word_slots.values())
                clue_requests = [
                    ClueRequest(
                        slot_id=slot.id,
                        word=slot.text or "",
                        direction=slot.direction.value,
                        clue_box=slot.clue_box,
                    )
                    for slot in slots
                ]
                clue_texts = self.clue_generator.generate(clue_requests)
                attach_clues_to_grid(grid, slots, clue_texts)
                LOGGER.info("Crossword generation completed with %s words", len(slots))
                return CrosswordResult(
                    grid=grid,
                    slots=slots,
                    theme_words=theme_words,
                    validation_messages=validation.messages,
                    seed=self.config.seed,
                )
            except CrosswordError as exc:
                LOGGER.warning("Generation attempt failed: %s", exc)
                self._reset_state()
                continue
        raise CrosswordError("Unable to generate crossword after retries")

    # ------------------------------------------------------------------
    # Theme seeding
    # ------------------------------------------------------------------
    def _seed_theme_words(self, grid: CrosswordGrid) -> List[ThemeWord]:
        LOGGER.info("Preparing theme words for '%s'", self.config.theme)

        playable_cells = grid._playable_count
        min_theme_letters = max(1, int(playable_cells * self.config.min_theme_coverage))
        letter_budget = max(min_theme_letters, int(playable_cells * self.config.max_theme_ratio))

        # Auto-scale request size for larger grids
        estimated_words_needed = (min_theme_letters // 5) + 2
        target = max(self.config.theme_request_size, estimated_words_needed * 3)

        theme_words = merge_theme_generators(
            self.theme_generator, self.theme_fallback_generators, self.config.theme, target
        )
        if not theme_words:
            raise ThemeWordError("No theme words available")

        letters_used = 0
        placed: List[ThemeWord] = []
        for theme_entry in theme_words:
            cleaned = self.dictionary.sanitize(theme_entry.word)
            if len(cleaned) < 2:
                continue
            if cleaned in self.used_words:
                continue
            if letters_used >= letter_budget:
                LOGGER.info(
                    "Reached theme letter budget (%d letters, %.0f%% of playable)",
                    letters_used,
                    (letters_used / playable_cells) * 100 if playable_cells else 0,
                )
                break
            if not self._attempt_place_specific_word(
                grid, cleaned, theme_entry, is_theme=True
            ):
                continue
            placed.append(theme_entry)
            letters_used += len(cleaned)

        if letters_used < min_theme_letters:
            raise ThemeWordError(
                f"Insufficient theme coverage: {letters_used}/{min_theme_letters} letters "
                f"({letters_used / playable_cells * 100:.0f}% vs "
                f"{self.config.min_theme_coverage * 100:.0f}% target)"
            )

        self.remaining_theme_words = {
            self.dictionary.sanitize(entry.word) for entry in theme_words if entry not in placed
        }
        LOGGER.info(
            "Placed %s theme words (%d letters, %.0f%% of %d playable)",
            len(placed), letters_used,
            (letters_used / playable_cells) * 100 if playable_cells else 0,
            playable_cells,
        )
        return placed

    def _attempt_place_specific_word(
        self,
        grid: CrosswordGrid,
        word: str,
        theme_entry: Optional[ThemeWord] = None,
        is_theme: bool = False,
    ) -> bool:
        if self._attempt_pending_start(grid, word, theme_entry, is_theme):
            return True

        for _ in range(self.config.theme_placement_attempts):
            direction = self.rng.choice([Direction.ACROSS, Direction.DOWN])
            start_positions = self._candidate_starts(grid, len(word), direction)
            if not start_positions:
                continue
            start_row, start_col = self.rng.choice(start_positions)
            if self._place_word_at(
                grid, word, theme_entry, is_theme, direction, start_row, start_col
            ):
                return True
        return False

    def _attempt_pending_start(
        self,
        grid: CrosswordGrid,
        word: str,
        theme_entry: Optional[ThemeWord],
        is_theme: bool,
    ) -> bool:
        while self.pending_starts:
            _, _, (row, col, direction) = heapq.heappop(self.pending_starts)
            if self._place_word_at(grid, word, theme_entry, is_theme, direction, row, col):
                return True
        return False

    def _place_word_at(
        self,
        grid: CrosswordGrid,
        word: str,
        theme_entry: Optional[ThemeWord],
        is_theme: bool,
        direction: Direction,
        start_row: int,
        start_col: int,
    ) -> bool:
        if not self._can_place_word(grid, start_row, start_col, direction, word):
            return False
        try:
            clue_box = grid.ensure_clue_box(start_row, start_col, direction)
        except ClueBoxError as exc:
            LOGGER.debug("Unable to license start via clue: %s", exc)
            return False

        slot = WordSlot(
            id=self._next_slot_id(direction),
            start_row=start_row,
            start_col=start_col,
            direction=direction,
            length=len(word),
            clue_box=clue_box,
            is_theme=is_theme,
        )

        try:
            grid.place_word(slot, word)
            self._validate_crossings(grid, slot)
            extension = grid.ensure_terminal_boundary(slot)
        except (ClueBoxError, SlotPlacementError) as exc:
            if slot.id in grid.word_slots:
                grid.remove_word(slot.id)
            LOGGER.debug("Slot rejected during placement: %s", exc)
            return False

        self._register_slot(slot)
        self.used_words.add(word)
        if extension:
            self._queue_start(grid, *extension)
        if theme_entry:
            self._attach_theme_clue(grid, slot, theme_entry)
            self.theme_word_surfaces.add(self.dictionary.sanitize(theme_entry.word))
        return True

    def _attach_theme_clue(self, grid: CrosswordGrid, slot: WordSlot, theme_entry: ThemeWord) -> None:
        clue_cell = grid.cell(*slot.clue_box)
        clue_cell.clues_hosted.append(
            Clue(
                id=f"{slot.id}-theme",
                text=theme_entry.clue,
                solution_word_ref_id=slot.id,
                solution_length=slot.length,
                direction=slot.direction,
                start_offset_r=slot.start_row - slot.clue_box[0],
                start_offset_c=slot.start_col - slot.clue_box[1],
            )
        )

    # ------------------------------------------------------------------
    # Layout completion: place clue boxes to create fillable slot structure
    # ------------------------------------------------------------------
    def _complete_layout(self, grid: CrosswordGrid) -> None:
        """Place clue boxes to create a valid, fillable slot structure."""
        self._heal_isolated_cells(grid)

        # Partition long runs in two passes: first coarse, then fine.
        # Target slot length 4-8 for best dictionary coverage.
        for max_len in (10, 8):
            for _ in range(30):  # safety limit
                changed = self._partition_long_runs(grid, max_len)
                if changed:
                    self._heal_isolated_cells(grid)
                else:
                    break

        # Ensure every slot has clue licensing
        self._ensure_all_licensed(grid)

        # Repair orphan clue boxes
        self._repair_orphan_clues(grid)

        # Verify feasibility of all slots
        self._verify_feasibility(grid)

    def _partition_long_runs(self, grid: CrosswordGrid, max_len: int) -> bool:
        """Scan all rows/columns for runs > max_len and insert clue boxes."""
        changed = False
        for direction in (Direction.ACROSS, Direction.DOWN):
            for r in range(grid.bounds.rows):
                for c in range(grid.bounds.cols):
                    cell = grid.cell(r, c)
                    if not cell.is_playable():
                        continue
                    if not grid.is_boundary(r, c, direction):
                        continue
                    sig = self._build_signature(grid, r, c, direction)
                    if not sig or sig.length <= max_len:
                        continue
                    # Only partition if there are unfilled cells in this span
                    pattern = self._signature_pattern(grid, sig)
                    if all(pattern):
                        continue

                    mid = sig.length // 2

                    def _partition_score(x: int, _len: int = sig.length) -> float:
                        left = x
                        right = _len - x - 1
                        penalty = 0
                        if left == 3:
                            penalty += 10
                        if right == 3:
                            penalty += 10
                        return abs(x - mid) + penalty

                    offsets = sorted(range(2, sig.length - 1), key=_partition_score)
                    for offset in offsets:
                        box_r, box_c = sig.cells[offset]
                        box_cell = grid.cell(box_r, box_c)
                        if box_cell.type != CellType.EMPTY_PLAYABLE:
                            continue
                        if box_cell.part_of_word_ids:
                            continue
                        try:
                            grid._add_clue_box(box_r, box_c)
                            LOGGER.debug(
                                "Partitioned span (%d,%d) len=%d with clue at (%d,%d)",
                                r, c, sig.length, box_r, box_c,
                            )
                            changed = True
                            break
                        except ClueBoxError:
                            continue
        return changed

    def _ensure_all_licensed(self, grid: CrosswordGrid) -> None:
        """Ensure every slot boundary has clue licensing.

        This eagerly creates clue boxes for every slot start that doesn't
        already have one. If a clue box can't be created for a slot start,
        convert the start cell itself into a clue box to eliminate the
        unlicensable slot.
        """
        changed = True
        while changed:
            changed = False
            for direction in (Direction.ACROSS, Direction.DOWN):
                for r in range(grid.bounds.rows):
                    for c in range(grid.bounds.cols):
                        cell = grid.cell(r, c)
                        if not cell.is_playable():
                            continue
                        if not grid.is_boundary(r, c, direction):
                            continue
                        sig = self._build_signature(grid, r, c, direction)
                        if not sig or sig.length < 2:
                            continue
                        # Check if already licensed
                        already_licensed = False
                        for dr, dc in grid._clue_offsets(direction):
                            nr, nc = r + dr, c + dc
                            if grid.bounds.contains(nr, nc) and grid.cell(nr, nc).type == CellType.CLUE_BOX:
                                already_licensed = True
                                break
                        if already_licensed:
                            continue
                        # Try to create a clue box at a valid offset
                        try:
                            grid.ensure_clue_box(r, c, direction)
                            changed = True
                        except ClueBoxError:
                            # Can't license this slot start. Convert the start
                            # cell to a clue box to eliminate the unlicensable slot.
                            if cell.type == CellType.EMPTY_PLAYABLE and not cell.part_of_word_ids:
                                try:
                                    grid._add_clue_box(r, c)
                                    LOGGER.debug(
                                        "Eliminated unlicensable slot start at (%d,%d) dir=%s -> CLUE_BOX",
                                        r, c, direction.value,
                                    )
                                    changed = True
                                except ClueBoxError:
                                    LOGGER.debug(
                                        "Cannot resolve unlicensable slot at (%d,%d) dir=%s",
                                        r, c, direction.value,
                                    )
            if changed:
                self._heal_isolated_cells(grid)

    def _verify_feasibility(self, grid: CrosswordGrid) -> None:
        """Verify all ≥3-letter slots have dictionary candidates."""
        all_sigs = self._enumerate_all_slots(grid)
        for sig in all_sigs:
            if sig.length < 3:
                continue
            pattern = self._signature_pattern(grid, sig)
            if all(pattern):
                # Fully filled — check it's a valid word
                surface = "".join(p for p in pattern if p)
                if surface in self.theme_word_surfaces or self.dictionary.contains(surface):
                    continue
                raise CrosswordError(
                    f"Pre-filled invalid word '{surface}' at ({sig.start_row},{sig.start_col})"
                )
            if not self.dictionary.has_candidates(sig.length, pattern=pattern, banned=self.used_words):
                # Try to partition the infeasible slot
                if sig.length <= 4 and self._try_partition_infeasible(grid, sig):
                    continue
                raise CrosswordError(
                    f"Infeasible slot at ({sig.start_row},{sig.start_col}) len={sig.length}"
                )

    def _try_partition_infeasible(self, grid: CrosswordGrid, sig: SlotSignature) -> bool:
        """Try to break an infeasible slot by placing a clue box in it."""
        for offset in range(1, sig.length):
            box_r, box_c = sig.cells[offset]
            box_cell = grid.cell(box_r, box_c)
            if box_cell.type != CellType.EMPTY_PLAYABLE:
                continue
            if box_cell.part_of_word_ids:
                continue
            try:
                grid._add_clue_box(box_r, box_c)
                LOGGER.info(
                    "Partitioned infeasible slot at (%d,%d) with clue at (%d,%d)",
                    sig.start_row, sig.start_col, box_r, box_c,
                )
                return True
            except ClueBoxError:
                continue
        return False

    # ------------------------------------------------------------------
    # CP-SAT filling
    # ------------------------------------------------------------------
    def _cpsat_fill(self, grid: CrosswordGrid) -> None:
        """Fill all unfilled slots using CP-SAT solver."""
        from .solver import solve_crossword

        all_slots = self._enumerate_all_slots(grid)
        unfilled = [
            s for s in all_slots
            if not self._is_fully_filled(grid, s)
        ]
        if not unfilled:
            LOGGER.info("No unfilled slots; skipping CP-SAT")
            return

        # Pre-resolve clue boxes and filter out unlicensable slots
        slot_clue_boxes: Dict[int, Tuple[int, int]] = {}
        licensable: List = []
        for sig in unfilled:
            try:
                cb = grid.ensure_clue_box(sig.start_row, sig.start_col, sig.direction)
                slot_clue_boxes[id(sig)] = cb
                licensable.append(sig)
            except ClueBoxError:
                LOGGER.debug(
                    "Skipping unlicensable slot at (%d,%d) dir=%s len=%d",
                    sig.start_row, sig.start_col, sig.direction.value, sig.length,
                )

        if not licensable:
            LOGGER.info("No licensable unfilled slots; skipping CP-SAT")
            return

        LOGGER.info("CP-SAT: %d unfilled slots to fill (%d skipped unlicensable)",
                     len(licensable), len(unfilled) - len(licensable))

        timeout = min(30.0, self.config.fill_timeout_seconds)
        result = solve_crossword(
            grid, licensable, self.dictionary,
            used_words=self.used_words,
            theme_surfaces=self.theme_word_surfaces,
            timeout=timeout,
        )
        if result is None:
            raise CrosswordError("CP-SAT solver found no solution")

        # Place all solved words on the grid
        for sig, word in result:
            clue_box = slot_clue_boxes[id(sig)]
            slot = WordSlot(
                id=self._next_slot_id(sig.direction),
                start_row=sig.start_row,
                start_col=sig.start_col,
                direction=sig.direction,
                length=sig.length,
                clue_box=clue_box,
            )
            try:
                grid.place_word(slot, word)
            except SlotPlacementError as exc:
                raise CrosswordError(
                    f"Cannot place CP-SAT word '{word}' at ({sig.start_row},{sig.start_col}): {exc}"
                ) from exc
            self._register_slot(slot)
            self.used_words.add(word)

    def _enumerate_all_slots(self, grid: CrosswordGrid) -> List[SlotSignature]:
        """Find all slots (filled and unfilled) by scanning for ≥2-length runs."""
        seen_keys: Set[str] = set()
        slots: List[SlotSignature] = []
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type not in {CellType.EMPTY_PLAYABLE, CellType.LETTER}:
                    continue
                for direction in (Direction.ACROSS, Direction.DOWN):
                    if not grid.is_boundary(r, c, direction):
                        continue
                    sig = self._build_signature(grid, r, c, direction)
                    if not sig or sig.length < 2:
                        continue
                    key = self._signature_key(sig)
                    if key in seen_keys:
                        continue
                    # Skip already-registered slots (theme words)
                    if key in self._occupied_slots:
                        continue
                    seen_keys.add(key)
                    slots.append(sig)
        return slots

    @staticmethod
    def _is_fully_filled(grid: CrosswordGrid, sig: SlotSignature) -> bool:
        return all(grid.cell(r, c).letter for r, c in sig.cells)

    # ------------------------------------------------------------------
    # Isolated cell healing
    # ------------------------------------------------------------------
    def _heal_isolated_cells(self, grid: CrosswordGrid) -> None:
        """Convert isolated EMPTY_PLAYABLE cells to CLUE_BOX."""
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type != CellType.EMPTY_PLAYABLE:
                    continue
                has_playable = False
                for dr, dc in ORTHOGONAL_STEPS:
                    nr, nc = r + dr, c + dc
                    if grid.bounds.contains(nr, nc) and grid.cell(nr, nc).type in (
                        CellType.EMPTY_PLAYABLE, CellType.LETTER,
                    ):
                        has_playable = True
                        break
                if has_playable:
                    continue
                try:
                    grid._add_clue_box(r, c)
                    LOGGER.debug("Healed isolated cell at (%d,%d) -> CLUE_BOX", r, c)
                except ClueBoxError:
                    raise CrosswordError(f"Isolated cell at ({r},{c}) cannot be healed")

    # ------------------------------------------------------------------
    # Orphan clue repair
    # ------------------------------------------------------------------
    def _repair_orphan_clues(self, grid: CrosswordGrid) -> bool:
        repaired = False
        for clue_pos, licenses in list(grid.clue_box_licenses.items()):
            cell = grid.cell(*clue_pos)
            if cell.type != CellType.CLUE_BOX or licenses:
                continue
            if self._assign_existing_slot_to_clue(grid, clue_pos):
                repaired = True
                continue
        return repaired

    def _assign_existing_slot_to_clue(self, grid: CrosswordGrid, clue_pos: Tuple[int, int]) -> bool:
        for slot in grid.word_slots.values():
            if not self._clue_can_license_slot(grid, clue_pos, slot):
                continue
            current_clue = slot.clue_box
            if current_clue == clue_pos:
                return True
            current_licenses = grid.clue_box_licenses.get(current_clue, set())
            if len(current_licenses) <= 1:
                continue
            self._move_slot_to_clue(grid, slot, clue_pos)
            LOGGER.debug("Reassigned slot %s to clue %s", slot.id, clue_pos)
            return True
        return False

    def _move_slot_to_clue(
        self,
        grid: CrosswordGrid,
        slot: WordSlot,
        new_clue: Tuple[int, int],
    ) -> None:
        old_clue = slot.clue_box
        if old_clue == new_clue:
            return
        grid.clue_box_licenses.setdefault(new_clue, set()).add(slot.id)
        grid.clue_box_licenses.get(old_clue, set()).discard(slot.id)
        slot.clue_box = new_clue

        old_cell = grid.cell(*old_clue)
        new_cell = grid.cell(*new_clue)
        for clue in list(old_cell.clues_hosted):
            if clue.solution_word_ref_id != slot.id:
                continue
            old_cell.clues_hosted.remove(clue)
            clue.start_offset_r = slot.start_row - new_clue[0]
            clue.start_offset_c = slot.start_col - new_clue[1]
            new_cell.clues_hosted.append(clue)

    def _clue_can_license_slot(
        self,
        grid: CrosswordGrid,
        clue_pos: Tuple[int, int],
        slot: WordSlot,
    ) -> bool:
        for dr, dc in grid._clue_offsets(slot.direction):
            if (slot.start_row + dr, slot.start_col + dc) == clue_pos:
                return True
        return False

    # ------------------------------------------------------------------
    # Crossing validation (used during theme seeding)
    # ------------------------------------------------------------------
    def _validate_crossings(self, grid: CrosswordGrid, slot: WordSlot) -> None:
        for check_dir in (Direction.ACROSS, Direction.DOWN):
            for row, col in slot.cells:
                signature = self._build_signature(grid, row, col, check_dir)
                if not signature or signature.length < 2:
                    continue
                key = self._signature_key(signature)
                if key in self._occupied_slots:
                    continue
                if not self._start_has_clue_capacity(
                    grid, signature.start_row, signature.start_col, check_dir
                ):
                    raise ClueBoxError(
                        f"No available clue position for start {(signature.start_row, signature.start_col)}"
                    )
                if signature.length < 3:
                    continue
                pattern = self._signature_pattern(grid, signature)
                if all(pattern):
                    surface = "".join(pattern)
                    sanitized = self.dictionary.sanitize(surface)
                    if sanitized in self.theme_word_surfaces or self.dictionary.contains(surface):
                        continue
                if signature.length == 3:
                    count = self.dictionary.count_candidates(
                        signature.length, pattern, banned=self.used_words,
                    )
                    if count < 3:
                        raise SlotPlacementError(
                            f"Too few candidates ({count}) for 3-letter crossing at "
                            f"{(signature.start_row, signature.start_col)}"
                        )
                elif not self.dictionary.has_candidates(
                    signature.length, pattern, banned=self.used_words,
                ):
                    raise SlotPlacementError(
                        f"No viable candidates for crossing slot at "
                        f"{(signature.start_row, signature.start_col)}"
                    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_signature(
        self, grid: CrosswordGrid, seed_row: int, seed_col: int, direction: Direction
    ) -> Optional[SlotSignature]:
        dr, dc = self._step(direction)
        start_row, start_col = seed_row, seed_col
        while True:
            prev_row, prev_col = start_row - dr, start_col - dc
            if not grid.bounds.contains(prev_row, prev_col):
                break
            prev_cell = grid.cell(prev_row, prev_col)
            if prev_cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
                break
            if prev_cell.type in {CellType.LETTER, CellType.EMPTY_PLAYABLE}:
                start_row, start_col = prev_row, prev_col
                continue
            break

        cells: List[Tuple[int, int]] = []
        row, col = start_row, start_col
        while grid.bounds.contains(row, col):
            cell = grid.cell(row, col)
            if cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
                break
            cells.append((row, col))
            row += dr
            col += dc
        if len(cells) < 2:
            return None
        return SlotSignature(start_row=start_row, start_col=start_col, direction=direction, cells=cells)

    def _signature_pattern(self, grid: CrosswordGrid, signature: SlotSignature) -> Sequence[Optional[str]]:
        pattern: List[Optional[str]] = []
        for row, col in signature.cells:
            letter = grid.cell(row, col).letter
            pattern.append(letter if letter else None)
        return pattern

    def _candidate_starts(
        self, grid: CrosswordGrid, length: int, direction: Direction
    ) -> List[Tuple[int, int]]:
        starts: List[Tuple[int, int]] = []
        for row in range(grid.bounds.rows):
            for col in range(grid.bounds.cols):
                if not grid.is_boundary(row, col, direction):
                    continue
                if self._slot_overlaps_block(grid, row, col, direction, length):
                    continue
                starts.append((row, col))
        return starts

    def _queue_start(self, grid: CrosswordGrid, row: int, col: int, direction: Direction) -> None:
        signature = self._build_signature(grid, row, col, direction)
        if not signature or signature.length < 2:
            return
        pattern = self._signature_pattern(grid, signature)
        if all(pattern):
            return
        priority = self._pending_priority(signature, pattern)
        heapq.heappush(
            self.pending_starts,
            (priority, self._pending_counter, (signature.start_row, signature.start_col, direction)),
        )
        self._pending_counter += 1

    @staticmethod
    def _pending_priority(signature: SlotSignature, pattern: Sequence[Optional[str]]) -> float:
        filled = sum(1 for letter in pattern if letter)
        openness = signature.length - filled
        return -(filled * 10) + openness

    def _slot_overlaps_block(
        self, grid: CrosswordGrid, row: int, col: int, direction: Direction, length: int
    ) -> bool:
        dr, dc = self._step(direction)
        for offset in range(length):
            r = row + dr * offset
            c = col + dc * offset
            if not grid.bounds.contains(r, c):
                return True
            cell = grid.cell(r, c)
            if cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
                return True
        return False

    def _can_place_word(
        self, grid: CrosswordGrid, row: int, col: int, direction: Direction, word: str
    ) -> bool:
        dr, dc = self._step(direction)
        for index, letter in enumerate(word):
            r = row + dr * index
            c = col + dc * index
            if not grid.bounds.contains(r, c):
                return False
            cell = grid.cell(r, c)
            if cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
                return False
            if cell.letter and cell.letter != letter:
                return False
        return True

    def _signature_key(self, signature: SlotSignature) -> str:
        return self._signature_components(
            signature.start_row, signature.start_col, signature.direction, signature.length
        )

    def _signature_components(
        self, row: int, col: int, direction: Direction, length: int
    ) -> str:
        return f"{row}:{col}:{direction.value}:{length}"

    def _register_slot(self, slot: WordSlot) -> None:
        key = self._signature_components(slot.start_row, slot.start_col, slot.direction, slot.length)
        self._occupied_slots.add(key)
        self._slot_keys[slot.id] = key
        self._placement_history.append(slot.id)

    def _next_slot_id(self, direction: Direction) -> str:
        self._slot_counter += 1
        prefix = "A" if direction == Direction.ACROSS else "D"
        return f"{prefix}{self._slot_counter:04d}"

    def _step(self, direction: Direction) -> Tuple[int, int]:
        return (0, 1) if direction == Direction.ACROSS else (1, 0)

    def _reset_state(self) -> None:
        self.used_words.clear()
        self.remaining_theme_words.clear()
        self.theme_word_surfaces.clear()
        self._occupied_slots.clear()
        self._slot_keys.clear()
        self._slot_counter = 0
        self._placement_history.clear()
        self.pending_starts = []
        self._pending_counter = 0

    def _start_has_clue_capacity(
        self,
        grid: CrosswordGrid,
        start_row: int,
        start_col: int,
        direction: Direction,
    ) -> bool:
        for dr, dc in grid._clue_offsets(direction):
            clue_row = start_row + dr
            clue_col = start_col + dc
            if not grid.bounds.contains(clue_row, clue_col):
                continue
            neighbor = grid.cell(clue_row, clue_col)
            if neighbor.type == CellType.CLUE_BOX:
                return True
            if neighbor.type == CellType.EMPTY_PLAYABLE and grid._can_place_clue_box(clue_row, clue_col):
                return True
        return False

    # ------------------------------------------------------------------
    # Annealing
    # ------------------------------------------------------------------
    def _anneal_layout(self, grid: CrosswordGrid) -> None:
        best_snapshot = grid.snapshot()
        best_score = self._score_layout(grid)
        attempts = max(3, min(8, self.config.retry_limit * 2))

        for _ in range(attempts):
            trial_grid = CrosswordGrid(grid.config)
            trial_grid.place_blocker_zone()
            score = self._score_layout(trial_grid)
            if score > best_score:
                best_score = score
                best_snapshot = trial_grid.snapshot()

        grid.restore(best_snapshot)

    def _score_layout(self, grid: CrosswordGrid) -> float:
        playable = sum(
            1
            for row in grid.cells
            for cell in row
            if cell.type not in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}
        )
        if not playable:
            return 0.0
        clue_penalty = 0
        for (row, col), licenses in grid.clue_box_licenses.items():
            if not licenses:
                clue_penalty += 1
            for dr, dc in ORTHOGONAL_STEPS:
                nr, nc = row + dr, col + dc
                if grid.bounds.contains(nr, nc) and grid.cell(nr, nc).type == CellType.CLUE_BOX:
                    clue_penalty += 2
        blocker_penalty = 0
        if grid.blocker_zone:
            h, w = grid.blocker_zone[2], grid.blocker_zone[3]
            blocker_penalty = abs(h - w)
        return playable - 3 * clue_penalty - blocker_penalty

    # ------------------------------------------------------------------
    # Compatibility: _fill_crossword for debug_main step_fill
    # ------------------------------------------------------------------
    def _fill_crossword(self, grid: CrosswordGrid, deadline: Optional[float]) -> None:
        """Fill the crossword using layout completion + CP-SAT.

        This method exists for backward compatibility with debug_main.step_fill().
        """
        self._complete_layout(grid)
        self._cpsat_fill(grid)
