# Compact Firestore-Optimized Crossword Storage

## Context
The current `CrosswordStore` saves pretty-printed JSON with ~3756 lines for a 15×12 crossword. The bloat comes from three sources: (1) a fully serialized `grid` object (all cell metadata), (2) redundant `slots` and `clues` arrays that overlap heavily, and (3) verbose `stats`/`config` objects. A frontend consuming this for rendering a puzzle only needs a fraction of this data. The goal is to redesign the serialization format to be compact, Firestore-ready (1MB doc limit), and easy for a web/mobile client to consume — while keeping local and Firestore formats aligned.

---

## Proposed Compact Schema

```json
{
  "id": "20260224T235427_3894fc32",
  "created_at": "2026-02-24T23:54:27Z",
  "status": "success",
  "title": "Animals of the Forest",
  "theme_content": "optional narrative paragraph...",
  "width": 15,
  "height": 12,
  "difficulty": "MEDIUM",
  "theme_title": "forest animals",
  "seed": 42,
  "grid": "CROSSWORD______PUZZLE______...",
  "entries": [
    {
      "id": "1A",
      "r": 0, "c": 0,
      "dir": "A",
      "len": 9,
      "clue": "Goes across the top",
      "hint_1": "optional first hint",
      "hint_2": "optional second hint",
      "answer": "CROSSWORD",
      "theme": true
    }
  ],
  "stats": {
    "word_count": 42,
    "theme_coverage": 0.18,
    "difficulty_avg": 0.35,
    "easy_pct": 0.42,
    "medium_pct": 0.44,
    "hard_pct": 0.14
  },
  "theme_cache_ref": "optional-ref-id"
}
```

### Grid string encoding
- Flat string, rows concatenated left→right, top→bottom
- `_` = black/blocker cell, uppercase letter = filled cell
- Length = `width × height` (e.g. 180 chars for 15×12)
- Frontend reconstructs the visual grid + cell numbers from `entries` positions

### Entries array (replaces both `slots` and `clues`)
Each entry: `id` (e.g. "3D"), `r`, `c` (start row/col), `dir` ("A"/"D"), `len`, `clue`, `hint_1`, `hint_2` (both optional), `answer`, `theme` (bool)
- Clue number is embedded in `id` and derivable from sorted positions
- Drops: `long_clue`, `clue_box` (derivable from r/c), `source`, `solution_word_ref_id`, `start_offset_*`
- Keeps: `hint_1`, `hint_2` as separate fields for progressive reveal

### Failed docs
- Save id, created_at, status=failed, config summary (width, height, theme_title, difficulty), and partial grid string if available

---

## What's Dropped vs Current

| Removed | Reason |
|---|---|
| `grid` object (full cell tree) | Replaced by compact flat string |
| `slots` array | Merged into `entries` |
| `clues` array | Merged into `entries` |
| `config` object (11 fields) | Key fields promoted to root; generation params (completion_target, min_theme_coverage, place_blocker_zone) dropped |
| `theme_words` list | Redundant — info lives in `entries[].theme` + clue text |
| `stats.words.distribution` | Analytics only, not needed for rendering |
| `stats.grid.*` (8 cell counts) | Not needed by frontend |
| `validation` messages | Dev/debug only |

### Estimated size reduction
- Grid string: 180 chars vs ~500–1000 lines of cell objects → ~95% reduction for grid field
- entries vs slots+clues: merged single array, fewer fields per item → ~40% reduction
- Dropping `theme_words`, `config`, `validation`, `stats` sub-objects → significant savings
- **Overall estimate: ~80–90% smaller** (3756 lines → ~150–300 lines)

---

## Files to Modify

**Primary:** `crossword/engine/crossword_store.py`
- Rewrite `save_success()` to produce the compact schema
- Add `_encode_grid_string(grid) -> str` helper: iterate cells row by row, `_` for blockers, uppercase letter otherwise
- Add `_build_entries(slots, clues) -> list` helper: join on shared id/position, output merged entry dicts
- Update `save_failure()` to compact format

**Secondary (read to confirm field names, no changes expected):**
- `crossword/engine/generator.py` — `CrosswordResult` fields passed to store
- `crossword/core/models.py` — `CrosswordGrid` cell access API for grid string encoding

---

## Verification
1. Run generation: `python main.py --theme-title "test" --width 15 --height 12`
2. Check output JSON in `local_db/collections/crosswords/` — should be dramatically smaller
3. Verify `len(doc["grid"]) == doc["width"] * doc["height"]`
4. Verify `len(doc["entries"])` matches the old `slots` count
5. Run tests: `.venv/bin/python -m pytest tests/ -v` — all 85 should pass
