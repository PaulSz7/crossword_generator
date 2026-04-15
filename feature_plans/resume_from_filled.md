# Save Filled Crosswords & Resume Clue Generation

## Context

When a crossword fills successfully but then fails at definition fetching or clue generation (e.g., Gemini 503), the filled grid is lost. Filling is the expensive step — clue generation just needs the filled grid + config. We save a checkpoint after fill and allow resuming with `--resume <doc_id>`.

The saved document uses the **same compact format** as existing success/failure docs (no new fields beyond what's already there), just with `status: "filled"` and entries without clues.

---

## Implementation

### 1. `CrosswordStore.save_filled()` — save checkpoint after fill
**File:** `crossword/engine/crossword_store.py`

New method, same structure as `save_success` but:
- `status: "filled"` instead of `"success"`
- `entries` have slot info but `clue: null` (no clues yet)
- Includes `theme_words` array with metadata needed for clue routing:
  ```json
  [{"word": "MINGE", "clue": "Sferă", "source": "gemini",
    "long_clue": "...", "hint": "...", "has_user_clue": false, "word_breaks": []}]
  ```
- Also saves `language`, `allow_adult`, `dictionary_path` (needed for resume)

### 2. `CrosswordStore.load(doc_id)` — load a document
**File:** `crossword/engine/crossword_store.py`

Reads `{doc_id}.json`, parses JSON, returns dict. Raises `FileNotFoundError` if missing.

### 3. `CrosswordGrid.from_entries()` — reconstruct grid from compact format
**File:** `crossword/engine/grid.py`

Classmethod that reconstructs the grid from the entries array + blocker_zone:

1. All cells start as `EMPTY_PLAYABLE`
2. Mark `blocker_zone` cells as `BLOCKER_ZONE`
3. Mark all unique `entries[].cb` positions as `CLUE_BOX`
4. For each entry, walk its cells and set `LETTER` + letter from `answer`
5. Rebuild `word_slots` dict from entries
6. Rebuild `clue_box_licenses` from entries (slot → cb mapping)
7. Recount `_playable_count` and `_filled_count`

Signature:
```python
@classmethod
def from_entries(cls, width: int, height: int, entries: list,
                 blocker_zone: list | None = None) -> CrosswordGrid:
```

### 4. Save checkpoint in `generator.generate()`
**File:** `crossword/engine/generator.py`

After validation passes, before clue generation:
```python
if self.store:
    filled_id = self.store.save_filled(grid, self.config, slots, ...)
```

### 5. Save checkpoint in `debug_main._run_single_attempt()`
**File:** `debug_main.py`

After `step_validate()` passes, before `step_clues()`:
- Save filled checkpoint via store
- If `step_clues()` raises, log the filled doc_id so user knows what to resume

### 6. Resume in `debug_main.py`
**File:** `debug_main.py`

New function `resume_from_filled(doc_id, **overrides)`:
- Load document via `CrosswordStore.load(doc_id)`
- Validate `status == "filled"`
- Reconstruct grid via `CrosswordGrid.from_entries()`
- Reconstruct `ThemeWord` list from `theme_words` array
- Init dictionary + definition fetcher + clue generator from saved config
- Run full clue routing logic (same as generator.generate): theme bundles for gemini words, preset clues for user words, LLM generation for fill words
- Detect sibling entry pairs for clue context
- Build result, save as success

`run_debug()` accepts `resume_from=<doc_id>` kwarg. When set, skips theme/fill/validation and calls `resume_from_filled` instead.

### 7. Resume in `main.py`
**File:** `main.py`

`--resume <DOC_ID>` CLI flag. When set:
- Load filled document
- `--height`/`--width` no longer required (loaded from document)
- Optional overrides: `--difficulty`, `--language`, `--dictionary`
- Delegates to `resume_from_filled()`
- Outputs JSON in same format as normal generation

---

## Files Modified

| File | Changes |
|------|---------|
| `crossword/engine/crossword_store.py` | `save_filled()`, `load()` |
| `crossword/engine/grid.py` | `CrosswordGrid.from_entries()` classmethod |
| `crossword/engine/generator.py` | Save filled checkpoint after validation |
| `debug_main.py` | `resume_from_filled()`, checkpoint save before clues, `resume_from` wiring |
| `main.py` | `--resume` flag and resume code path |
| `tests/test_grid.py` | 5 round-trip tests for `from_entries()` |

## Verification

1. Full test suite: 107 passed (102 original + 5 new round-trip tests)
2. Round-trip tests cover: cell types/letters, word slots, clue box licenses, blocker zone, playable/filled counters
3. Manual test: trigger a clue failure, verify "filled" doc appears and doc_id is logged
4. Manual test: `--resume <doc_id>` generates clues and saves success doc
