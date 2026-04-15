---
name: cpsat-fill
description: "CP-SAT constraint solver for crossword grid filling"
user-invocable: false
paths: ["crossword/engine/solver.py"]
---

# CP-SAT Fill Solver

## Overview

The solver fills all unfilled slots in the grid using Google OR-Tools CP-SAT. It is called after theme seeding + clue box placement (layout phase).

## Entry Point

```python
solve_crossword(
    grid, unfilled_slots, dictionary, used_words, theme_surfaces,
    timeout=30.0, max_candidates=8000, fallback_fraction=0.0,
    max_difficulty_score=None, medium_slot_limit=None,
) -> Optional[List[Tuple[SlotSignature, str]]]
```

Returns list of `(slot, word)` pairs, or `None` if infeasible.

## Model Structure

### Step 1: Cell Letter Variables
- Each cell gets an `IntVar(0, 25)` representing A-Z, or a constant if the cell already has a letter (from theme words).

### Step 2: Per-Slot Candidates + Table Constraints
- For slots with length >= 3: `dictionary.find_candidates(length, pattern, banned, limit)` retrieves candidates.
- For 2-letter slots: all 676 letter combos are generated (filtered by known letters).
- `AddAllowedAssignments` creates a table constraint per slot from candidate word letter tuples.
- When `max_difficulty_score` is set (EASY mode), candidates above the threshold are filtered out. Slots with zero easy candidates can use the full pool if `medium_slot_limit` allows it.

### Step 3: Uniqueness Constraints
- Pairwise `_add_differ_constraint` for same-length slots ensures no two slots share the same word.
- `_forbid_word` prevents unfilled slots from matching already-placed theme words.

### Step 4: Solve
- `solver.parameters.max_time_in_seconds` — timeout (default 30s per call, generator uses 180s).
- `solver.parameters.num_workers = 4` — parallel search workers.
- Accepts OPTIMAL or FEASIBLE status.

### Step 5: Extract Solution
- Reads cell variable values and reconstructs word strings.

## EASY Difficulty Flow

When `max_difficulty_score` is set:
1. Filter candidates to those below the score threshold.
2. If a slot has zero easy candidates, increment `medium_slot_count`.
3. If `medium_slot_count > medium_slot_limit`, return `None` (reject layout).
4. Otherwise, allow that slot to use the full unfiltered candidate pool.

The generator calls the solver in two phases for EASY:
- Phase 1: easy-only retries (`_EASY_PHASE1_RETRIES` attempts)
- Phase 2: allows medium fallback via `medium_slot_limit`

## Performance Notes

- Filling is the most expensive step in the pipeline. Theme/clue generation is cheap by comparison.
- Infeasibility -> `SlotFillError` -> generator retries with a new layout (up to `retry_limit`).
- The `fallback_fraction` parameter reserves a fraction of each slot's candidate pool for off-tier words, ensuring the solver always has backup candidates.
