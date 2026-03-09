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
- **`Clue`** model (`crossword/core/models.py`): added `hint_1: str = ""`, `hint_2: str = ""`

### Constants
- **`SYSTEM_INSTRUCTION`**: general behavior, safety check, clue field requirements (main_clue/hint_1/hint_2), strict restrictions, short word special rule, theme integration, difficulty control (per-field per-tier), best-candidate selection, style variation, self-validation
- **`MAIN_PROMPT_TEMPLATE`**: `{{LANGUAGE}}`, `{{THEME}}`, `{{DIFFICULTY}}`, `{{WORD_LIST_JSON}}`
- **`CLUE_RESPONSE_SCHEMA`**: `{status, clues: [{answer, main_clue, hint_1, hint_2}], error: {reason, invalid_words}}`

### Clue Field Constraints
| Field | Constraint |
|---|---|
| `main_clue` | max 4 words |
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
- No field contains the answer (case-insensitive substring)
- `answer` in expected word set

### Repair Loop
1. `_validate_clues()` splits response into `(valid, invalid)` lists
2. Invalid entries get a `violations` list (e.g. `"main_clue exceeds 4 words (6 words)"`)
3. `_build_repair_prompt()` sends only failed entries with their specific violations
4. Repaired response re-validated; valid entries merged into results

### `generate()` Flow
1. Build `answer → [slot_id, ...]` reverse map
2. Render prompt via `MAIN_PROMPT_TEMPLATE`
3. Call Gemini with `system_instruction` + `response_schema`
4. Parse JSON; handle `status="error"` → return empty
5. Validate → repair if needed → merge
6. Map `answer → ClueBundle` back to `slot_id → ClueBundle`

### Call Site Updates
- `generator.py`: passes `theme=self.config.theme_title`
- `debug_main.py`: passes `theme=state["generator"].config.theme_title or ""`
- `crossword_store.py`: serializes `hint_1`, `hint_2` in `_extract_clues()`
- `ClueGenerator` Protocol and `TemplateClueGenerator`: return `Dict[str, ClueBundle]`
- `attach_clues_to_grid`: accepts `Dict[str, ClueBundle]`, sets `hint_1`/`hint_2` on `Clue`

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

### Word Generation Rules (CRITICAL)
- Every word MUST be a real word in the requested language — foreign words strictly forbidden
- Every word MUST be unique — no duplicates, even with different casing or diacritics
- Uppercase ASCII A-Z only, ≥ 2 letters, diacritics normalized

### Theme Word Field Constraints (aligned with clue generator)
| Field | Constraint |
|---|---|
| `clue` | max 3 words (ultra-short for clue cell) |
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
- No field contains the answer (case-insensitive substring)

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
1. **Tests**: `.venv/bin/python -m pytest tests/ -v` — all 83 pass
2. **New test coverage**: `_count_sentences`, `_validate_theme_words`, `_parse_response` error/tuple return, `_build_theme_words`, schema structure, system instruction sections
3. **Integration**: `python main.py --theme-title "animale" --difficulty EASY`
4. **Safety path**: `status="error"` → returns empty `ThemeOutput` / empty `dict`
5. **Repair loop**: invalid entries re-sent with violation details, re-validated before merge
