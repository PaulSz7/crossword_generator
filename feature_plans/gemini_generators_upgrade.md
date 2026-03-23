# Upgrade Gemini Generators — Structured Prompts, Validation & Repair

## Overview
Both `GeminiClueGenerator` and `GeminiThemeWordGenerator` were upgraded to a unified architecture:
- Dedicated module-level `SYSTEM_INSTRUCTION` constants (structured rule blocks) sent via Gemini's `system_instruction` parameter
- Structured `PROMPT_TEMPLATE` constants with `{{PLACEHOLDER}}` substitution via `.replace()`
- Formal `RESPONSE_SCHEMA` dicts in Gemini format (`OBJECT`/`STRING`/`ARRAY`) with `status`/`error` handling
- Difficulty-aware generation across all fields (EASY/MEDIUM/HARD)
- App-side validation with a one-shot repair loop (re-send only invalid entries)
- Safety check handling (`status="error"` → graceful fallback)
- Sentence-count-based validation for hint/phrase fields (`_count_sentences`)

---

## Part 1: Clue Generator (`crossword/io/clues.py`)

### New/Modified Types
- **`ClueBundle`** dataclass: `main_clue`, `hint_1`, `hint_2`
- **`ClueRequest`** dataclass: `slot_id`, `word`, `direction`, `clue_box`, `definition: Optional[str]` (DEX context), `preset_main_clue: Optional[str]` (user-supplied clue echoed exactly)
- **`Clue`** model (`crossword/core/models.py`): added `hint_1: str = ""`, `hint_2: str = ""`

### Constants
- **`SYSTEM_INSTRUCTION`**: general behavior, safety check, clue field requirements (main_clue/hint_1/hint_2), strict restrictions (including punctuation rule), short word special rule, theme integration, difficulty control (per-field per-tier), best-candidate selection, style variation, definitions section, preset main clues section, self-validation
- **`MAIN_PROMPT_TEMPLATE`**: `{{LANGUAGE}}`, `{{THEME}}`, `{{DIFFICULTY}}`, `{{WORD_LIST_JSON}}`, `{{DEFINITIONS_SECTION}}`, `{{PRESET_CLUES_SECTION}}`
- **`CLUE_RESPONSE_SCHEMA`**: `{status, clues: [{answer, main_clue, hint_1, hint_2}], error: {reason, invalid_words}}`

### Clue Field Constraints
| Field | Constraint |
|---|---|
| `main_clue` | max 4 words; strongly prefer 1–2 words |
| `hint_1` | exactly 1 sentence |
| `hint_2` | max 2 sentences |

### Difficulty Control (all three fields)
| Difficulty | main_clue | hint_1 | hint_2 |
|---|---|---|---|
| EASY | synonyms & definitions OK | straightforward, guessable | clear, immediately solvable |
| MEDIUM | metaphor, wordplay, elliptical | moderately descriptive | enough context for most |
| HARD | oblique, no simple synonyms | requires lateral thinking | indirect/literary guidance |

### Validation (`_validate_clues`)
- Required fields present
- `main_clue` ≤ 4 words
- `hint_1` = 1 sentence
- `hint_2` ≤ 2 sentences
- **Exact-answer check** (all fields including riddle clues): no field may contain the answer as a standalone word — uses `re.search(r'\b<answer>\b', ...)` (word-boundary matching) to avoid false positives from the answer appearing as a substring of an unrelated word.
- **Fragment/substring check** (non-riddle clues only): for answers ≥ 6 chars, no field may contain a long substrings of the answer as a standalone word. Riddle `main_clue` (ending with `!`) is **exempt** from this check because riddle style intentionally embeds answer letters inside other words (e.g. `"Început!"` = EPU where `ceput` contains `epu`). The exact-answer word-boundary check still applies to riddle clues.
- `answer` in expected word set

### Repair Loop
1. `_validate_clues()` splits response into `(valid, invalid)` lists
2. Invalid entries get a `violations` list (e.g. `"main_clue exceeds 4 words (6 words)"`)
3. `_build_repair_prompt()` sends only failed entries with their specific violations
4. Repaired response re-validated; valid entries merged into results

### `generate()` Flow
1. Build `answer → [slot_id, ...]` reverse map
2. Render prompt via `_render_prompt(word_list, difficulty, language, theme, requests)` — injects `DEFINITIONS_SECTION` and `PRESET_CLUES_SECTION` only when any request carries a definition or preset clue
3. Call Gemini with `system_instruction` + `response_schema`
4. Parse JSON; handle `status="error"` → return empty
5. Validate → repair if needed → merge
6. Post-process: for any request with `preset_main_clue`, override `bundle.main_clue` with the preset value regardless of LLM output
7. Map `answer → ClueBundle` back to `slot_id → ClueBundle`

### Call Site Updates
- `generator.py`: categorizes slots before calling `generate()` — Gemini theme words go directly to `theme_bundles`, bypassing the LLM entirely; see clue routing table below
- `debug_main.py`: passes `theme=state["generator"].config.theme_title or ""`
- `crossword_store.py`: serializes `hint_1`, `hint_2` in `_extract_clues()`
- `ClueGenerator` Protocol and `TemplateClueGenerator`: return `Dict[str, ClueBundle]`
- `attach_clues_to_grid`: accepts merged `{**clue_texts, **theme_bundles}`, sets `hint_1`/`hint_2` on `Clue`

### Clue Routing (generator.py)

| Theme word source | `has_user_clue` | Routing |
|---|---|---|
| `"gemini"` | any | → `theme_bundles` directly (no LLM call) |
| `"user"` | True | → `clue_requests` with `preset_main_clue`; LLM generates hints only |
| `"user"` | False | → `clue_requests` for full LLM generation |
| `"substring"` / `"dummy"` | — | → `clue_requests` for full LLM generation |
| fill word | — | → `clue_requests` with `definition` from `dictionary.get(word)` |

---

## Part 2: Theme Word Generator (`crossword/data/theme.py`)

### Constants
- **`THEME_SYSTEM_INSTRUCTION`**: module-level constant replacing old `SYSTEM_INSTRUCTION` class attribute. Sections: general behavior, safety check, word generation requirements, clue field requirements, strict restrictions, difficulty control, theme integration, best-candidate selection, style variation, self-validation
- **`THEME_PROMPT_TEMPLATE`**: `{{LANGUAGE}}`, `{{THEME}}`, `{{DIFFICULTY}}`, `{{MIN_WORDS}}`, `{{MAX_WORDS}}`, `{{TYPE_INSTRUCTIONS}}`, `{{DESCRIPTION_LINE}}`
- **`THEME_RESPONSE_SCHEMA`**: `{status, crossword_title, content, words: [{word, clue, long_clue, hint}]}`
- **`_TYPE_INSTRUCTIONS`**: dict mapping theme type → type-specific prompt paragraph (domain_specific, joke_continuation, joke_continuation_with_desc, custom)

### Removed
- Old class attributes: `SYSTEM_INSTRUCTION`, `RESPONSE_SCHEMA_TEMPLATE`, `THEME_DIFFICULTY_PROMPT`
- Old `_build_system_instruction()` method (system instruction is now a static constant)

### Punctuation Rule (both generators)
All clue fields must contain no punctuation other than ellipsis (`...`, for intentional trailing ambiguity only) or exclamation mark (`!`, for the short-word riddle style or strong warranted emphasis only). This rule appears in both `SYSTEM_INSTRUCTION` and `THEME_SYSTEM_INSTRUCTION` under STRICT CLUE RESTRICTIONS and SELF-VALIDATION.

### Word Generation Rules (CRITICAL)
- Every word MUST be a real word in the requested language — foreign words strictly forbidden
- Every word MUST be unique — no duplicates, even with different casing or diacritics
- Uppercase ASCII A-Z only, ≥ 2 letters, diacritics normalized

### Theme Word Field Constraints (aligned with clue generator)
| Field | Constraint |
|---|---|
| `clue` | max 3 words; strongly prefer 1–2 words (ultra-short for clue cell) |
| `long_clue` | exactly 1 sentence |
| `hint` | max 2 sentences |

### Difficulty Control (words AND clue fields)
| Difficulty | Word Selection | Clue Style |
|---|---|---|
| EASY | common, everyday vocabulary | synonyms OK, direct definitions, friendly |
| MEDIUM | mixed common + challenging | indirect, metaphor, balanced |
| HARD | rare, literary, domain-specific | oblique, no synonyms, lateral thinking |

### Validation (`_validate_theme_words`)
- Required fields present
- Word format: uppercase A-Z, ≥ 2 letters (regex `^[A-Z]{2,}$`)
- Duplicate word detection (tracks `seen_words` set)
- `clue` ≤ 3 words
- `long_clue` = 1 sentence
- `hint` ≤ 2 sentences
- No field contains the answer as a standalone word (word-boundary matching, same rules as clue validator)

### Repair Loop
Same pattern as clue generator:
1. `_validate_theme_words()` → `(valid, invalid)`
2. `_build_theme_repair_prompt()` lists violations per entry
3. One repair call with `THEME_SYSTEM_INSTRUCTION` + `THEME_RESPONSE_SCHEMA`
4. Re-validate repaired entries before merging

### `_parse_response` Return Change
- Returns `Optional[Tuple[List[Dict], Optional[str], Optional[str]]]` — `(raw_word_dicts, crossword_title, content)` or `None` on error
- `_build_theme_words()` converts validated dicts to `ThemeWord` objects
- Handles `status="error"` from safety check
- Fallback keys for content: `joke_text`, `jokeText`, `summary`

### `generate()` Flow
1. Check cache → return cached `ThemeOutput` if hit
2. Render prompt via `THEME_PROMPT_TEMPLATE` with type-specific instructions
3. Call Gemini with `THEME_SYSTEM_INSTRUCTION` + schema
4. Parse JSON; handle `status="error"` → return empty `ThemeOutput`
5. Validate → repair if needed → merge
6. Build `ThemeWord` objects from validated dicts
7. Save to cache

---

## Shared Helpers
- **`_count_sentences(text)`**: splits on `.!?` followed by whitespace or end-of-string, counts non-empty segments. Used by both `_validate_clues` and `_validate_theme_words`. Defined in both `clues.py` and `theme.py`.

---

---

## Additions (post-initial implementation)

### PromptLog (`crossword/io/prompt_log.py`)
All LLM calls are persisted for debugging. `GeminiClient` accepts an optional `prompt_log: PromptLog` and calls `prompt_log.record(request_type, prompt, response)` after every successful generation. System instructions are **not** stored (stable per type). Subcollection folders under `local_db/collections/prompt_log/`:

| Subcollection | Source |
|---|---|
| `clue_generation/` | `GeminiClueGenerator.generate()` |
| `clue_repair/` | `GeminiClueGenerator` repair loop |
| `theme_generation/` | `GeminiThemeWordGenerator.generate()` |
| `theme_repair/` | `GeminiThemeWordGenerator` repair loop |
| `definition_fetch/` | `GeminiDefinitionFetcher` |

Both `GeminiClueGenerator` and `GeminiThemeWordGenerator` accept `prompt_log: Optional[PromptLog] = None` and forward it to their lazy `GeminiClient` instances.

### GeminiDefinitionFetcher + DefinitionStore
Before building the clue prompt, `generator.py` identifies fill words without a cached definition and calls `GeminiDefinitionFetcher.fetch_batch()` to retrieve DEX definitions via grounded Gemini search. Results are cached in `local_db/collections/definitions/` via `DefinitionStore`. An INFO log line lists all words sent for lookup. The `generator` logs: `"Fetching definitions for N word(s) via DEX: WORD1, WORD2, ..."`.

### Grammatical Agreement (universal rule)
Grammatical agreement is now a **universal clue requirement** — no longer conditional on having a DEX definition. Every generated clue must match the answer's grammatical form (number, gender, case) in Romanian. The DEFINITIONS section in the system instruction notes that part-of-speech markers within definitions are useful signals; the rule applies equally to words without a definition.

## Files Changed
| File | Changes |
|---|---|
| `crossword/io/clues.py` | Full rewrite: `ClueBundle`, `SYSTEM_INSTRUCTION`, `MAIN_PROMPT_TEMPLATE`, `CLUE_RESPONSE_SCHEMA`, validation + repair, updated Protocol/Template/attach |
| `crossword/data/theme.py` | Full rewrite of `GeminiThemeWordGenerator`: `THEME_SYSTEM_INSTRUCTION`, `THEME_PROMPT_TEMPLATE`, `THEME_RESPONSE_SCHEMA`, `_TYPE_INSTRUCTIONS`, validation + repair, `_parse_response` return type, strict language/uniqueness rules |
| `crossword/core/models.py` | `hint_1`, `hint_2` fields on `Clue` |
| `crossword/engine/generator.py` | Pass `theme=` to clue generator |
| `crossword/engine/crossword_store.py` | Serialize `hint_1`, `hint_2` |
| `debug_main.py` | Pass `theme=` to clue generator |
| `tests/test_theme.py` | New: `CountSentencesTests` (7), `ValidateThemeWordsTests` (11). Updated: `GeminiPromptTemplateTests` |

## Verification
1. **Tests**: `.venv/bin/python -m pytest tests/ -v` — all 85 pass
2. **New test coverage**: `_count_sentences`, `_validate_theme_words`, `_parse_response` error/tuple return, `_build_theme_words`, schema structure, system instruction sections
3. **Integration**: `python main.py --theme-title "animale" --difficulty EASY`
4. **Safety path**: `status="error"` → returns empty `ThemeOutput` / empty `dict`
5. **Repair loop**: invalid entries re-sent with violation details, re-validated before merge
