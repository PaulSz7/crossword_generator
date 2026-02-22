"""CP-SAT crossword filling solver using OR-Tools."""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Dict, List, Optional, Set, Tuple

from ortools.sat.python import cp_model

from ..core.constants import Direction
from ..data.dictionary import WordDictionary
from ..utils.logger import get_logger

LOGGER = get_logger(__name__)

# SlotSignature is imported late or passed as a duck type; we rely on the
# attributes: start_row, start_col, direction, cells, length.


def solve_crossword(
    grid,
    unfilled_slots: list,
    dictionary: WordDictionary,
    used_words: Set[str],
    theme_surfaces: Set[str],
    timeout: float = 30.0,
    max_candidates: int = 8000,
    fallback_fraction: float = 0.0,
    max_difficulty_score: Optional[float] = None,
    medium_slot_limit: Optional[int] = None,
) -> Optional[List[Tuple]]:
    """Fill unfilled slots via CP-SAT.

    Args:
        grid: CrosswordGrid with fixed layout.
        unfilled_slots: List of SlotSignature objects to fill.
        dictionary: WordDictionary for candidate lookup.
        used_words: Words already placed (theme words, etc.).
        theme_surfaces: Theme word surfaces for validation.
        timeout: Solver time limit in seconds.
        max_candidates: Max candidates per slot.
        fallback_fraction: Fraction of each slot's candidate pool reserved for
            the next-lower tier (passed through to find_candidates).
        max_difficulty_score: Hard ceiling on candidate difficulty_score.
            Candidates above this threshold are excluded from the solver pool.
        medium_slot_limit: When set alongside max_difficulty_score, slots with
            zero easy candidates are allowed to use the full (unfiltered) pool
            instead of returning None immediately. If the number of such slots
            exceeds this limit the call returns None (layout rejected).

    Returns:
        List of (SlotSignature, word_surface) pairs, or None if unsolvable.
    """
    if not unfilled_slots:
        return []

    model = cp_model.CpModel()

    # ------------------------------------------------------------------
    # Step 1: Cell letter variables
    # ------------------------------------------------------------------
    cell_vars: Dict[Tuple[int, int], object] = {}  # (r,c) -> IntVar or int

    for slot in unfilled_slots:
        for r, c in slot.cells:
            if (r, c) in cell_vars:
                continue
            existing = grid.cell(r, c).letter
            if existing:
                cell_vars[(r, c)] = ord(existing) - ord('A')
            else:
                cell_vars[(r, c)] = model.new_int_var(0, 25, f'L_{r}_{c}')

    # ------------------------------------------------------------------
    # Step 2: Per-slot word candidates + table constraints
    # ------------------------------------------------------------------
    slot_candidates: Dict[str, List[str]] = {}
    medium_slot_count = 0

    for slot in unfilled_slots:
        pattern = [grid.cell(r, c).letter for r, c in slot.cells]

        if slot.length >= 3:
            candidates = dictionary.find_candidates(
                slot.length, pattern=pattern,
                banned=used_words, limit=max_candidates,
                fallback_fraction=fallback_fraction,
            )
            if max_difficulty_score is not None:
                easy = [e for e in candidates if e.difficulty_score < max_difficulty_score]
                if easy:
                    candidates = easy
                else:
                    # No easy candidates for this slot — attempt per-slot relaxation.
                    medium_slot_count += 1
                    if medium_slot_limit is None or medium_slot_count > medium_slot_limit:
                        LOGGER.debug(
                            "No easy candidates for slot (%d,%d) dir=%s len=%d "
                            "(medium slots so far: %d, limit: %s)",
                            slot.start_row, slot.start_col, slot.direction.value,
                            slot.length, medium_slot_count, medium_slot_limit,
                        )
                        return None  # reject: too many slots need medium words
                    LOGGER.debug(
                        "Slot (%d,%d) dir=%s len=%d gets medium fallback (%d/%s)",
                        slot.start_row, slot.start_col, slot.direction.value,
                        slot.length, medium_slot_count, medium_slot_limit,
                    )
                    # candidates already holds the full (unfiltered) pool
            surfaces = [e.surface for e in candidates]
        else:
            # 2-letter slots: generate all valid letter combos
            surfaces = _generate_2letter_candidates(pattern)

        if not surfaces:
            LOGGER.debug(
                "No candidates for slot at (%d,%d) dir=%s len=%d",
                slot.start_row, slot.start_col, slot.direction.value, slot.length,
            )
            return None  # infeasible

        key = f"{slot.start_row}_{slot.start_col}_{slot.direction.value}"
        slot_candidates[key] = surfaces

        cell_list = [cell_vars[(r, c)] for r, c in slot.cells]
        # Only add table constraint if there's at least one real IntVar
        has_var = any(isinstance(v, cp_model.IntVar) for v in cell_list)
        if has_var:
            tuples = []
            for word in surfaces:
                tuples.append([ord(ch) - ord('A') for ch in word])
            model.add_allowed_assignments(cell_list, tuples)

    # ------------------------------------------------------------------
    # Step 3: Uniqueness constraints
    # ------------------------------------------------------------------
    # Group by length for pairwise uniqueness
    by_length: Dict[int, List] = defaultdict(list)
    for slot in unfilled_slots:
        by_length[slot.length].append(slot)

    for length, group in by_length.items():
        for s1, s2 in combinations(group, 2):
            _add_differ_constraint(model, cell_vars, s1, s2)

    # Forbid unfilled slots from matching already-placed words
    for slot in unfilled_slots:
        for placed_word in used_words:
            if len(placed_word) != slot.length:
                continue
            _forbid_word(model, cell_vars, slot, placed_word)

    # ------------------------------------------------------------------
    # Step 4: Solve
    # ------------------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = timeout
    solver.parameters.num_workers = 4

    LOGGER.info(
        "CP-SAT: %d slots, %d cell vars, solving (timeout=%0.1fs)...",
        len(unfilled_slots),
        sum(1 for v in cell_vars.values() if isinstance(v, cp_model.IntVar)),
        timeout,
    )

    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        LOGGER.warning("CP-SAT: no solution found (status=%s)", solver.status_name(status))
        return None

    LOGGER.info("CP-SAT: solution found in %.2fs", solver.wall_time)

    # ------------------------------------------------------------------
    # Step 5: Extract solution
    # ------------------------------------------------------------------
    result = []
    for slot in unfilled_slots:
        word = ''.join(
            chr(_resolve_var(solver, cell_vars[(r, c)]) + ord('A'))
            for r, c in slot.cells
        )
        result.append((slot, word))
    return result


def _resolve_var(solver: cp_model.CpSolver, var_or_const) -> int:
    """Get the value of a variable or constant."""
    if isinstance(var_or_const, cp_model.IntVar):
        return solver.value(var_or_const)
    return var_or_const


def _add_differ_constraint(model, cell_vars, s1, s2) -> None:
    """Ensure two same-length slots cannot contain identical words."""
    diffs = []
    for pos in range(s1.length):
        r1, c1 = s1.cells[pos]
        r2, c2 = s2.cells[pos]
        v1 = cell_vars[(r1, c1)]
        v2 = cell_vars[(r2, c2)]
        # If both are constants and differ, constraint is automatically satisfied
        if not isinstance(v1, cp_model.IntVar) and not isinstance(v2, cp_model.IntVar):
            if v1 != v2:
                return  # Already guaranteed different
            continue  # Same constant at this position, no help
        b = model.new_bool_var(
            f'd_{s1.start_row}{s1.start_col}{s1.direction.value}_'
            f'{s2.start_row}{s2.start_col}{s2.direction.value}_{pos}'
        )
        if isinstance(v1, cp_model.IntVar) and isinstance(v2, cp_model.IntVar):
            model.add(v1 != v2).only_enforce_if(b)
            model.add(v1 == v2).only_enforce_if(~b)
        elif isinstance(v1, cp_model.IntVar):
            model.add(v1 != v2).only_enforce_if(b)
            model.add(v1 == v2).only_enforce_if(~b)
        else:
            model.add(v2 != v1).only_enforce_if(b)
            model.add(v2 == v1).only_enforce_if(~b)
        diffs.append(b)
    if diffs:
        model.add_bool_or(diffs)


def _forbid_word(model, cell_vars, slot, placed_word: str) -> None:
    """Forbid a slot from matching a specific placed word."""
    diffs = []
    for pos in range(slot.length):
        r, c = slot.cells[pos]
        v = cell_vars[(r, c)]
        letter_val = ord(placed_word[pos]) - ord('A')
        if not isinstance(v, cp_model.IntVar):
            if v != letter_val:
                return  # Already guaranteed different
            continue
        b = model.new_bool_var(f'ne_{slot.start_row}{slot.start_col}_{placed_word}_{pos}')
        model.add(v != letter_val).only_enforce_if(b)
        model.add(v == letter_val).only_enforce_if(~b)
        diffs.append(b)
    if diffs:
        model.add_bool_or(diffs)


def _generate_2letter_candidates(pattern: List[Optional[str]]) -> List[str]:
    """Generate all 2-letter uppercase combinations, filtering by known letters."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    results = []
    if len(pattern) != 2:
        return results
    chars0 = [pattern[0]] if pattern[0] else list(letters)
    chars1 = [pattern[1]] if pattern[1] else list(letters)
    for a in chars0:
        for b in chars1:
            results.append(a + b)
    return results
