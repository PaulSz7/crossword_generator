# Plan: Theme Type Upgrade

**Status: Implemented and refined (2026-02-23)**

## Context
The current crossword generator had a single `theme` parameter (renamed to `theme_title`) that drove
LLM word generation for a fixed "domain specific words" strategy. This plan introduced a `theme_type`
field to support multiple theme generation strategies, extensible over time.

The fill algorithm is **not touched** — only theme word generation changes.

---

## New `ThemeType` Values
- `domain_specific_words` — current behavior, default
- `words_containing_substring` — filter dictionary for words that contain `theme_title` as substring (no LLM)
- `joke_continuation` — LLM provides words that form a joke punchline, plus the full joke text
- `custom` — LLM uses `theme_description` as creative brief; returns words, a crossword title, and a content blurb

---

## File-by-file Changes

### 1. `crossword/data/theme.py`

**Added `ThemeType` enum:**
```python
class ThemeType(str, Enum):
    DOMAIN_SPECIFIC_WORDS = "domain_specific_words"
    WORDS_CONTAINING_SUBSTRING = "words_containing_substring"
    JOKE_CONTINUATION = "joke_continuation"
    CUSTOM = "custom"
```

**Added `ThemeOutput` dataclass** (replaces bare `List[ThemeWord]`):
```python
@dataclass
class ThemeOutput:
    words: List[ThemeWord] = field(default_factory=list)
    crossword_title: Optional[str] = None
    content: Optional[str] = None   # hint text for blocker zone in UI
```

**Updated `ThemeWordGenerator` protocol** — `generate()` returns `ThemeOutput` instead of `List[ThemeWord]`.

**Updated `GeminiThemeWordGenerator`:**
- Constructor gains `theme_type: str = "domain_specific_words"` and `theme_description: str = ""`
- Prompt templates per type:
  - `domain_specific_words` — existing prompt (JSON lines, word+clue)
  - `joke_continuation` — single JSON object: `{joke_text, words:[{word,clue}]}`; if `theme_description` is given, it is the joke text and LLM must fit words to it
  - `custom` — single JSON object: `{crossword_title, content, words:[{word,clue}]}`; `theme_description` is the creative brief
- Response parser: JSON lines for `domain_specific_words`, `json.loads()` object for the others
- Returns `ThemeOutput` with populated `crossword_title`/`content` where applicable
- Handles markdown code fences in LLM responses

**Added `SubstringThemeWordGenerator`:**
```python
class SubstringThemeWordGenerator:
    def __init__(self, dictionary: WordDictionary, theme_title: str) -> None: ...
    def generate(self, theme: str, limit=80, difficulty="MEDIUM", language="Romanian") -> ThemeOutput:
        # iterate dictionary._entry_by_surface, keep words where theme_title.lower() in word.lower()
        # sort by entry.score(Difficulty(difficulty)) descending (difficulty-aware, same scoring as fill solver)
        # clue: "Conține «{theme_title}»"
        # return ThemeOutput(words=filtered[:limit])
```
Imports `Difficulty` from `crossword.core.constants` for the scoring call.

**Updated `UserWordListGenerator.generate()`** → returns `ThemeOutput(words=self._theme_words)`.

**Updated `DummyThemeWordGenerator.generate()`** → returns `ThemeOutput(words=results)`.

**Updated `DummyThemeWordGenerator` error handling:**
- Removed `FALLBACK_BUCKET` constant (was a set of generic fallback words used when theme not in `DEFAULT_THEME_BUCKETS`).
- If requested theme is not in `DEFAULT_THEME_BUCKETS`, raises `ValueError` with a clear message listing known themes.
- Prevents silently polluting the crossword with unrelated filler words.

**Updated `merge_theme_generators()`:**
- Returns `ThemeOutput` (was `List[ThemeWord]`)
- Collects `crossword_title` and `content` from the first generator that provides them
- Merges `words` lists (existing deduplication logic unchanged)
- Returns `ThemeOutput(words=..., crossword_title=..., content=...)`

---

### 2. `crossword/engine/generator.py`

**`GeneratorConfig`:**
- Renamed `theme: str = ""` → `theme_title: str = ""`
- Added `theme_type: str = "domain_specific_words"`
- Added `theme_description: str = ""`
- Added `extend_with_substring: bool = False` — explicit flag that tells `_seed_theme_words` whether to add `SubstringThemeWordGenerator` as a fallback after user-supplied words (replaces a fragile `None` vs `[]` convention that was indistinguishable by callees)

**`CrosswordResult`:**
```python
crossword_title: Optional[str] = None
theme_content: Optional[str] = None
```

**`CrosswordGenerator.__init__()`:**
- Added `self._theme_crossword_title: Optional[str] = None`
- Added `self._theme_content: Optional[str] = None`

**`CrosswordGenerator._seed_theme_words()`:**
- Log line updated to `self.config.theme_title`
- For `words_containing_substring`: creates `SubstringThemeWordGenerator(self.dictionary, config.theme_title)`:
  - If `self.theme_generator is None` → SubstringGen becomes the primary (user gave no words)
  - Else if `config.extend_with_substring` is `True` → SubstringGen added as fallback (user gave words, `--llm` requested extension)
  - Else → fallbacks stay empty (user gave words, `--llm` not set, use words only)
- Calls `merge_theme_generators(...)` passing `config.theme_title`
- Stores `output.crossword_title` → `self._theme_crossword_title` and `output.content` → `self._theme_content`
- For `joke_continuation` with user words + `theme_description` and no LLM: sets `self._theme_content` from `config.theme_description`

**`CrosswordGenerator.generate()`:**
- Passes `crossword_title=self._theme_crossword_title, theme_content=self._theme_content` into `CrosswordResult`

**`CrosswordGenerator._reset_state()`:** resets `_theme_crossword_title` and `_theme_content` to `None`.

---

### 3. `main.py`

**CLI:**
- `--theme` → `--theme-title`
- Added `--theme-type` with `choices=[t.value for t in ThemeType]`, default `"domain_specific_words"`
- `--theme-description` remains, now passes directly to `GeneratorConfig`

**Validation:**
- `--llm` still requires `--theme-title` for `domain_specific_words`
- `words_containing_substring` requires `--theme-title`; `--llm` controls dictionary extension (not LLM calls):
  - no `--words` and no `--llm` → error (nothing to search with)
  - no `--words` + `--llm` → dictionary substring search only
  - `--words` + no `--llm` → user words only (no dictionary extension)
  - `--words` + `--llm` → user words extended with dictionary substring matches

**`GeneratorConfig` construction** — uses `theme_title`, `theme_type`, `theme_description`, and `extend_with_substring` directly.
Removed the old `theme_str` concatenation hack.
`extend_with_substring` is computed as `theme_type == "words_containing_substring" and bool(user_words) and args.llm`.

**Generator wiring by `theme_type`:**

| scenario | theme_gen | fallbacks |
|---|---|---|
| `domain_specific_words`, no user words | `None` (→ default `[DummyThemeWordGenerator]`) | — |
| `domain_specific_words`, user words, no `--llm` | `UserWordListGenerator` | `[]` |
| `domain_specific_words`, user words + `--llm` | `UserWordListGenerator` | `[GeminiThemeWordGenerator, Dummy]` |
| `words_containing_substring`, no user words, `--llm` | `None` (→ SubstringGen created in `_seed_theme_words`) | `[]` |
| `words_containing_substring` + user words, no `--llm` | `UserWordListGenerator` | `[]` (user words only) |
| `words_containing_substring` + user words, `--llm` | `UserWordListGenerator` | `[SubstringGen]` (extend_with_substring=True) |
| `joke_continuation`, no user words | `GeminiThemeWordGenerator(type=joke_continuation)` | `[Dummy]` |
| `joke_continuation`, user words, no `--llm` | `UserWordListGenerator` | `[]` (content from theme_description) |
| `joke_continuation`, user words + `--llm` | `UserWordListGenerator` | `[GeminiThemeWordGenerator, Dummy]` |
| `custom` | `GeminiThemeWordGenerator(type=custom)` | `[Dummy]` |

**JSON output:**
```json
{
  "crossword_title": "...",
  "theme_content": "...",
  "grid": ...,
  "theme_words": [...],
  "slots": [...],
  "validation": [...]
}
```

---

### 4. `debug_main.py`

- `DEFAULT_DEBUG_ARGS`: `"theme"` → `"theme_title"`, added `"theme_type"` and `"theme_description"`
- `prepare_state()`: uses `theme_title`, `theme_type`, `theme_description`; passes them to `GeneratorConfig`; removed `theme_str` concatenation
- `prepare_state()` normalises `theme_type` with:
  ```python
  _theme_type_raw = args.get("theme_type") or "domain_specific_words"
  theme_type = _theme_type_raw.value if hasattr(_theme_type_raw, "value") else str(_theme_type_raw)
  ```
  This handles both `ThemeType.CUSTOM` enum instances (which `str()` renders as `"ThemeType.CUSTOM"`, not `"custom"`) and plain strings.
- Computes `extend_with_substring` and passes it to `GeneratorConfig`
- `build_result()`: passes `crossword_title` and `theme_content` from `generator._theme_crossword_title/_theme_content`

---

### 5. `tests/test_theme.py`

- All existing tests updated to use `result.words` instead of bare `List[ThemeWord]`
- New test classes:
  - `ThemeTypeEnumTests` — enum existence and str subclass
  - `ThemeOutputTests` — dataclass defaults and field assignment
  - `SubstringThemeWordGeneratorTests` — filtering, case-insensitivity, clue format, difficulty-aware sorting (via `entry.score` mock), limit, empty result
  - `SubstringWithUserWordsTests` — all four `(words, llm)` combinations for `words_containing_substring` mode
  - `GeminiPromptTemplateTests` — prompt rendering per type, `_parse_response` for all types including markdown fences
  - `test_dummy_generator_raises_for_unknown_theme` — validates `ValueError` raised for themes not in `DEFAULT_THEME_BUCKETS`

### 6. `tests/test_grid.py`

- Updated three `GeneratorConfig(theme=...)` calls → `GeneratorConfig(theme_title=...)`

---

## LLM Prompt Templates

### `joke_continuation`
```
You are a creative Romanian crossword designer.
Generate words that form the punchline of a short joke related to '{theme}'.
[If theme_description provided: The joke to use is: '{theme_description}'. Extract words from its punchline.]
Return a SINGLE JSON object (not JSON lines) with:
  "joke_text": the complete short joke (setup + punchline, 1-3 sentences),
  "words": [ {"word": "UPPERCASE_WORD", "clue": "3-5 word {language} cryptic clue"}, ...]
Generate between 20 and {limit} unique {language} words.
[difficulty prompt appended]
```

### `custom`
```
You are a creative Romanian crossword designer.
Theme title: '{theme}'.
Creative brief: '{theme_description}'.
Return a SINGLE JSON object with:
  "crossword_title": engaging crossword title (5-10 words in {language}),
  "content": thematic description for display (1-3 sentences in {language}),
  "words": [ {"word": "UPPERCASE_WORD", "clue": "3-5 word {language} cryptic clue"}, ...]
Generate between 20 and {limit} unique {language} words.
[difficulty prompt appended]
```

---

## Verification

1. **Unit tests**: `pytest tests/test_theme.py` — 44 tests pass; full suite 55 tests pass
2. **Substring type**: `python main.py --height 10 --width 10 --theme-title BERE --theme-type words_containing_substring --dictionary local_db/dex_words.tsv` — theme words all contain "BERE"
3. **Domain specific (backward compat)**: `--theme-title mitologie` works same as before (only flag renamed)
4. **Joke type (no LLM)**: `--theme-type joke_continuation --words WORD1 WORD2 --theme-description "Setup...Punchline"` — uses user words, outputs content from description
5. **Custom type**: `--theme-type custom --theme-title "Stiinte" --theme-description "Crossword about science disciplines"` — LLM returns title + content + words in output JSON
6. **debug_main**: `run_debug(theme_title="mitologie", theme_type="domain_specific_words")` works
