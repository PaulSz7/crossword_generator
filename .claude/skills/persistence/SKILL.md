---
name: persistence
description: "Storage layers: crossword store, definition store, theme cache, prompt log"
user-invocable: false
paths: ["crossword/engine/crossword_store.py", "crossword/data/definition_store.py", "crossword/data/theme_cache.py", "crossword/io/prompt_log.py"]
---

# Persistence System

All stores live under `local_db/collections/<collection>/` as individual JSON files.

## CrosswordStore (`crossword_store.py`)

Directory: `local_db/collections/crosswords/`

### Save Methods
- `save_success(result, config, dictionary)` — full crossword with clues + stats
- `save_filled(grid, config, slots, theme_words, ...)` — checkpoint after fill + validation, before clue generation
- `save_failure(config, error, grid, theme_words, ...)` — failed attempt with partial state
- `load(doc_id)` — retrieves document by ID

### Document Format
```json
{
  "id": "YYYYMMDDTHHMMSS_xxxxxxxx",
  "status": "success" | "filled" | "failed",
  "grid": "ABCD_EF__GH...",  // flat string, _ for non-letter cells
  "entries": [
    {"id", "r", "c", "cb", "dir", "len", "answer", "theme", "clue", "hint_1", "hint_2", "word_breaks"}
  ],
  "blocker_zone": [row, col, h, w],
  "stats": {"word_count", "theme_coverage", "difficulty_avg", "easy_pct", "medium_pct", "hard_pct"},
  "theme_words": [{"word", "clue", "source", "long_clue", "hint", "has_user_clue", "word_breaks"}]
}
```

- `entries[].dir` uses compact `"A"` / `"D"` (not `"ACROSS"` / `"DOWN"`)
- `entries[].cb` is `[row, col]` array
- Filled checkpoints strip clue/hint fields from entries and include `theme_words` array for resume
- `theme_words` serialization must mirror `ThemeWord` dataclass fields

## ThemeCache (`theme_cache.py`)

Directory: `local_db/collections/llm_theme_cache/`

- Key: `{type_slug}_{language}_{difficulty}_{title_slug}_{desc_hash_8}.json`
- `lookup()` — O(1) filename computation, returns `ThemeOutput` or None
- `save()` — merge-on-write: existing words preserved, new clue text wins on conflict
- `_normalize()` — lowercase, strip Romanian diacritics, collapse whitespace
- `cache_id()` — returns filename stem for cross-referencing in CrosswordStore

## DefinitionStore (`definition_store.py`)

Directory: `local_db/collections/word_definitions/`

- One JSON per word: `{word.upper()}.json`
- Simple `get(word) -> Optional[str]` / `save(word, definition)` interface
- Used by `GeminiDefinitionFetcher` as the disk cache layer

## PromptLog (`prompt_log.py`)

Directory: `local_db/collections/prompt_log/`

- Append-only, partitioned by `request_type` subdirectory (e.g. `theme_generation/`, `clue_generation/`, `definition_fetch/`)
- Each entry: `{id, created_at, request_type, prompt, response}`
- System instruction intentionally omitted (static per request type)

## Document ID Format

All stores use the same ID scheme: `YYYYMMDDTHHMMSS_{8-char-uuid-hex}`
