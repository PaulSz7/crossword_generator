---
name: clue-generation
description: "Clue routing, LLM clue generation, definition fetching"
user-invocable: false
paths: ["crossword/io/clues.py", "crossword/io/definition_fetcher.py"]
---

# Clue Generation System

## Core Data Structures

### ClueRequest (`clues.py:29`)
- `slot_id: str` — unique slot identifier
- `word: str` — the answer word
- `direction: str` — "ACROSS" or "DOWN"
- `clue_box: tuple[int,int]` — position of the licensing clue box
- `definition: Optional[str]` — DEX definition for LLM context
- `preset_main_clue: Optional[str]` — user-provided clue; LLM generates hints only
- `sibling_word: Optional[str]` — other word sharing the same clue box

### ClueBundle (`clues.py:41`)
- `main_clue: str` — ultra-short clue (max 4 words, displayed in clue box)
- `hint_1: str` — one-sentence progressive hint
- `hint_2: str` — 1-2 sentence stronger hint

## Clue Routing Table

After CP-SAT fill, the generator routes each slot through one of two paths:

| Theme word source | `has_user_clue` | Routing |
|---|---|---|
| `"gemini"` | any | -> `theme_bundles` dict (clue/long_clue/hint used directly, bypasses LLM) |
| `"user"` | True | -> `clue_requests` with `preset_main_clue`; LLM generates hints only |
| `"user"` | False | -> `clue_requests` for full LLM generation |
| `"substring"` / `"dummy"` | -- | -> `clue_requests` for full LLM generation |
| fill word (non-theme) | -- | -> `clue_requests` with definition from fetched_defs or dictionary |

`attach_clues_to_grid` receives merged `{**clue_texts, **theme_bundles}`.

## GeminiClueGenerator (`clues.py:345`)

- Batches all ClueRequests into a single API call with structured JSON output.
- System instruction is parameterized by language and difficulty via `_build_system_instruction()`.
- Difficulty control adjusts clue style: EASY allows direct definitions, MEDIUM prefers metaphor/wordplay, HARD requires oblique/misleading clues.
- Riddle/abbreviation style: allowed for words <= 3 letters (MEDIUM/EASY) or <= 4 letters (HARD). Must end with `!`.
- Punctuation rule: no punctuation except `...` (intentional ambiguity) or `!` (riddle style only).
- Sibling entries: when two slots share a clue box, PRIMARY's main_clue must work as a hint for both words.
- Safety: `allow_adult=False` adds strict content policy rejecting anatomy/bodily function words.

## Validation (`_validate_clues`)

Two-tier like theme validation:
- Severe: missing fields, unexpected answer, answer/fragment leaking in clue text -> repair or drop
- Cosmetic: word/sentence count exceeded -> accepted with warning
- Fragment check: for words >= 6 chars, checks 5+ char substrings as standalone words in clue fields

## GeminiDefinitionFetcher (`definition_fetcher.py:59`)

- Fetches definitions for words with missing/incomplete local definitions.
- Uses grounded Gemini calls against dexonline.ro.
- `is_incomplete_definition()`: flags definitions <= 23 chars or ending with `...`/`...` (DB column cap truncation).
- Three-tier caching: session cache -> DefinitionStore (disk) -> grounded Gemini API.
- Batch fetch: all uncached words in a single API call.

## Critical Gotcha

`debug_main.step_clues()` is a SEPARATE code path from `generator.generate()`, and `resume_from_filled()` is yet another. Changes to clue routing must be mirrored across all three paths.
