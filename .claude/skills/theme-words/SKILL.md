---
name: theme-words
description: "Theme word generation: ThemeType, generators, cache, validation"
user-invocable: false
paths: ["crossword/data/theme.py", "crossword/data/theme_cache.py"]
---

# Theme Word Generation System

## Core Data Structures

### ThemeType enum (`theme.py:28`)
- `DOMAIN_SPECIFIC_WORDS` — LLM generates words tied to a topic (default)
- `WORDS_CONTAINING_SUBSTRING` — dictionary filter for substring matches
- `JOKE_CONTINUATION` — LLM writes a joke, words come from the punchline
- `CUSTOM` — LLM generates title + blurb + words from a creative brief

### ThemeWord dataclass (`theme.py:36`)
- `word: str` — uppercase ASCII surface (diacritics stripped via `clean_word`)
- `clue: str` — ultra-short clue (1-3 words)
- `source: str` — one of `"gemini"`, `"user"`, `"substring"`, `"dummy"`
- `long_clue: str` — one-sentence descriptive clue
- `hint: str` — 1-2 sentence progressive hint
- `has_user_clue: bool` — True when user provided `"WORD:clue"` format
- `word_breaks: Tuple[int, ...]` — space positions for multi-word entries (e.g. `"DE FACTO"`)

### ThemeOutput dataclass (`theme.py:49`)
- `words: List[ThemeWord]`
- `crossword_title: Optional[str]` — set by CUSTOM type
- `content: Optional[str]` — blocker zone UI text (joke text, theme blurb)

## Generator Protocol and Implementations

All generators implement `generate(theme, limit, difficulty, language) -> ThemeOutput`.

| Generator | Source | Notes |
|-----------|--------|-------|
| `GeminiThemeWordGenerator` | `"gemini"` | LLM call with structured output + validation + optional repair |
| `UserWordListGenerator` | `"user"` | Parses `"WORD"` or `"WORD:clue"` strings; sets `has_user_clue` |
| `SubstringThemeWordGenerator` | `"substring"` | Filters `dictionary._entry_by_surface` for substring matches |
| `DummyThemeWordGenerator` | `"dummy"` | Predefined buckets (mitologie, istorie, natura) |

## merge_theme_generators (`theme.py:935`)

Cascading strategy: tries primary generator, then fallbacks in order, deduplicating by `word.upper()`. First non-None `crossword_title` and `content` win.

## Validation (`_validate_theme_words`)

Two-tier: severe violations (missing fields, bad format, duplicates, answer leaking) trigger repair or drop; cosmetic violations (word/sentence count slightly off) are accepted with a warning.

`allow_repair=True` sends a repair LLM call for severe entries. Default is to silently drop them.

## ThemeCache (`theme_cache.py`)

- Directory: `local_db/collections/llm_theme_cache/`
- Key format: `{type_slug}_{language}_{difficulty}_{title_slug}_{desc_hash_8}.json`
- `lookup()` returns cached `ThemeOutput` or None (O(1) filename computation)
- `save()` does merge-on-write: existing words preserved, new clue text wins on conflict
- All theme types use the same cache key scheme

## Downstream Impact

- `source` field drives clue routing in `generator.py`: `"gemini"` words bypass LLM clue generation (their clue bundle is used directly), other sources go through `ClueRequest`
- `has_user_clue` sends the user's clue as `preset_main_clue` — LLM only generates hints
- Changes to `ThemeWord` fields must be mirrored in `CrosswordStore.save_filled()` serialization (`crossword_store.py:141`)
- Multi-word support: `word_breaks` flows through to `WordSlot.word_breaks` and into the compact JSON entry format
