---
name: grid-layout
description: "Grid structure, cells, clue boxes, blocker zones, validation"
user-invocable: false
paths: ["crossword/engine/grid.py", "crossword/engine/validator.py"]
---

# Grid Layout System

## CrosswordGrid (`grid.py:51`)

The grid is the central mutable data structure holding the crossword state.

### Key Fields
- `cells: List[List[Cell]]` — 2D matrix of `Cell` objects
- `word_slots: Dict[str, WordSlot]` — all placed word slots keyed by ID
- `clue_box_licenses: Dict[Tuple[int,int], Set[str]]` — maps clue box position to set of slot IDs it licenses
- `blocker_zone: Optional[Tuple[int,int,int,int]]` — `(row, col, height, width)` of the blocker rectangle
- `_playable_count` / `_filled_count` — counters for fill ratio tracking

### CellType enum (defined in `core/constants.py`)
- `EMPTY_PLAYABLE` — initial state, can receive a letter
- `LETTER` — contains a placed letter (part of one or more word slots)
- `CLUE_BOX` — non-playable, hosts clue text for adjacent slots
- `BLOCKER_ZONE` — non-playable, reserved for UI content (joke text, theme blurb)

### GridConfig (`grid.py:22`)
- `height`, `width` — grid dimensions
- `min_blocker_size` / `max_blocker_size` — random blocker zone size bounds (default 3-6)
- `blocker_zone_*` — explicit overrides for position/size
- `blocker_zone_seed` — separate RNG seed for blocker placement (stability across retries)
- `rng_seed` — general grid RNG seed

## Clue Box Rules

- No adjacent clue boxes (orthogonal check in `_can_place_clue_box`)
- No clue boxes in the bottom-right 2x2 corner
- `MAX_CLUES_PER_BOX = 3` — hard limit on slots per clue box
- `ensure_clue_box()` finds existing box or creates new one for a slot start cell
- `ensure_terminal_boundary()` auto-places a clue box after a word ends (if space allows)

## Word Placement

- `place_word(slot, text)` — validates then mutates cells (EMPTY_PLAYABLE -> LETTER)
- `place_word_undoable(slot, text)` — same but returns an `undo()` callable for backtracking
- `remove_word(slot_id)` — reverts cells that have no other word references

## Grid Reconstruction

- `from_entries(width, height, entries, blocker_zone)` — reconstructs a filled grid from compact JSON entries (the inverse of `CrosswordStore._build_entries`). Used by the resume-from-filled path.
- Bypasses `__init__` auto-placement via `object.__new__()` to avoid re-placing blocker/initial clue box

## enumerate_slots (`grid.py:634`)

Derives all across/down slots from current cell state by scanning for LETTER cells at word boundaries. Used post-fill for validation and clue attachment.

## GridValidator (`validator.py:24`)

Deterministic rule checks run after fill:
- `_check_clue_boxes` — every box licenses 1-3 words, no adjacent boxes
- `_check_no_clue_box_in_bottom_right` — 2x2 corner exclusion
- `_check_no_isolated_cells` — every EMPTY_PLAYABLE has a playable neighbor
- `_check_no_duplicate_words` — no word appears twice in the grid
- `_check_letters_valid` — all LETTER cells contain uppercase A-Z
- `_check_sequences` — every 3+ letter slot is in the dictionary or theme set, every slot has a valid clue box license

## Gotchas

- `_playable_count` starts at `rows * cols` and decrements for blocker/clue box cells. `filled_ratio = _filled_count / _playable_count`.
- `snapshot()` / `restore()` use `copy.deepcopy` — expensive but necessary for backtracking.
- The mandatory top-left clue box is placed in `__init__` via `_place_initial_clue()`.
- When blocker zone covers (0,0), additional clue boxes are auto-planted at the edges of remaining playable area.
