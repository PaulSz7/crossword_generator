# Plan: User-Provided Word List Feature (with optional hybrid LLM extension)

## Context
Currently every crossword generation requires a `--theme` string, and theme words always come from the LLM (Gemini) or hardcoded dummy buckets. Two new modes are needed:

1. **Words-only mode**: User supplies an explicit list of seed words. No LLM is called; no dummy words are added. "Use exactly what the user inputs."
2. **Hybrid mode**: User supplies seed words *and* a theme + optional description. The LLM extends the predefined list with additional theme-based words to reach coverage targets.

Note: in the current `main.py`, `GeminiThemeWordGenerator` is never wired up — the CLI always falls back to the dummy generator. The hybrid mode will be its first real CLI integration.

---

## Three Generation Modes

| Mode | Flags | Primary generator | Fallback generators |
|------|-------|-------------------|---------------------|
| Theme-only (existing) | `--theme X` | `None` | `[DummyThemeWordGenerator]` |
| Words-only (new) | `--words ...` | `UserWordListGenerator` | `[]` (none) |
| Hybrid (new) | `--words ... --theme X --llm` | `UserWordListGenerator` | `[GeminiThemeWordGenerator, DummyThemeWordGenerator]` |

The hybrid fallback chain includes `DummyThemeWordGenerator` as a second fallback so generation succeeds even when no Gemini API key is available.

The existing `merge_theme_generators` function already handles this cascading pattern cleanly: it fills from primary first, then falls back to each fallback generator until the target count is reached.

---

## Implementation

### 1. `crossword/data/theme.py` — Add `UserWordListGenerator`

```python
class UserWordListGenerator:
    """Returns a user-supplied list of words as ThemeWord objects."""

    def __init__(self, raw_words: List[str]) -> None:
        self._theme_words: List[ThemeWord] = []
        for item in raw_words:
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                word, _, clue = item.partition(":")
                self._theme_words.append(ThemeWord(word.strip().upper(), clue.strip(), "user"))
            else:
                self._theme_words.append(ThemeWord(item.upper(), "", "user"))

    def generate(self, theme: str, limit: int = 80,
                 difficulty: str = "MEDIUM", language: str = "Romanian") -> List[ThemeWord]:
        return list(self._theme_words)  # always returns user's words, ignores theme/limit
```

### 2. `crossword/engine/generator.py` — Two small changes

**a) Make `GeneratorConfig.theme` optional:**
```python
theme: str = ""
```
(Move to after `dictionary_path`; ensure it comes before any field with a default, or add `= ""`)

**b) Add `theme_fallback_generators` param to `CrosswordGenerator.__init__`:**
```python
def __init__(
    self,
    config: GeneratorConfig,
    dictionary: Optional[WordDictionary] = None,
    theme_generator: Optional[ThemeWordGenerator] = None,
    clue_generator: Optional[ClueGenerator] = None,
    theme_fallback_generators: Optional[List[ThemeWordGenerator]] = None,  # NEW
) -> None:
    ...
    self.theme_fallback_generators = (
        theme_fallback_generators
        if theme_fallback_generators is not None
        else [DummyThemeWordGenerator(seed=config.seed)]
    )
```

### 3. `crossword/engine/generator.py` — Additional placement behaviour

**c) Skip coverage check in words-only mode:**

The `min_theme_coverage` check in `_seed_theme_words` is skipped when `theme_fallback_generators` is empty. The user takes responsibility for how many words are provided; enforcing a coverage floor would make small word lists always fail.

**d) Skip crossing validation for user words:**

`_place_word_at` accepts a `skip_crossing_validation` flag, threaded through `_attempt_place_specific_word` and `_attempt_pending_start`. The flag is set to `True` when `theme_entry.source == "user"`, regardless of mode. This ensures user words are always placed — crossing feasibility is instead enforced by `_verify_feasibility` and the CP-SAT solver downstream. Gemini and dummy words still go through the normal crossing check.

### 4. `main.py` — CLI changes

**New arguments:**
- `--theme` — remove `required=True`, add `default=""`
- `--words` — `nargs='+'`, accepts `WORD` or `WORD:Clue` tokens
- `--words-file` — `type=Path`, reads one `WORD` or `WORD:Clue` entry per line (blank lines and `#` comments ignored)
- `--llm` — `action='store_true'`, enables LLM extension (hybrid mode); requires `--theme`
- `--theme-description` — optional string; additional context for LLM prompt (e.g. "focus on Olympian gods")

**Validation logic (after `parse_args`):**
```
if --llm and not --theme:
    error "--llm requires --theme"
if not --theme and not (--words or --words-file):
    error "provide at least --theme or --words / --words-file"
```

**Generator wiring:**
```python
user_words = []
if args.words:
    user_words.extend(args.words)
if args.words_file:
    user_words.extend(parse_words_file(args.words_file))  # helper reads lines

theme_gen = None
fallbacks = None  # None → default [DummyThemeWordGenerator]

if user_words:
    theme_gen = UserWordListGenerator(user_words)
    if args.llm:
        # Hybrid: user words first, then LLM extends, dummy as safety net
        fallbacks = [GeminiThemeWordGenerator(), DummyThemeWordGenerator(seed=config.seed)]
    else:
        # Words-only: no fallbacks
        fallbacks = []

generator = CrosswordGenerator(
    config,
    theme_generator=theme_gen,
    theme_fallback_generators=fallbacks,
)
```

Add a small `parse_words_file(path: Path) -> List[str]` helper in `main.py` that reads lines, strips whitespace, skips blanks and `#`-prefixed comments.

---

## Critical Files

| File | Change |
|------|--------|
| `crossword/data/theme.py` | Add `UserWordListGenerator` class |
| `crossword/engine/generator.py` | Add `theme_fallback_generators` param; make `GeneratorConfig.theme` optional (`= ""`); skip coverage check in words-only mode; skip crossing validation for `source == "user"` words |
| `main.py` | Add `--words`, `--words-file`, `--llm`, `--theme-description`; make `--theme` optional; wire up generators |
| `debug_main.py` | Add `words`, `words_file`, `llm`, `theme_description` keys to `DEFAULT_DEBUG_ARGS`; wire up generators in `prepare_state` |

---

## Example Usage

```bash
# Existing behavior — unchanged
python main.py --height 10 --width 12 --theme mitologie

# Words-only (no LLM)
python main.py --height 10 --width 12 \
  --words APOLON ARES 'ATHENA:Zeita intelepciunii'

# Words from file
python main.py --height 10 --width 12 --words-file my_words.txt

# Hybrid: user words + LLM extension
python main.py --height 10 --width 12 \
  --words 'APOLON:Zeul soarelui' ARES \
  --theme mitologie \
  --theme-description "Zeii olimpieni din mitologia greaca" \
  --llm

# words-file + LLM
python main.py --height 10 --width 12 \
  --words-file seeds.txt \
  --theme mitologie --llm
```

**`my_words.txt` format:**
```
# Lines starting with # are comments
APOLON:Zeul soarelui
ARES
ATHENA:Zeita intelepciunii
```

---

## Verification

1. **Existing mode unchanged**: `--theme mitologie` → output `theme_words` have `source: "dummy"`, no regression.
2. **Words-only**: `--words APOLON ARES ATHENA` → all three in output with `source: "user"`, zero `source: "dummy"` entries.
3. **Inline clues**: `--words 'APOLON:Zeul soarelui'` → `theme_words[0].clue == "Zeul soarelui"`.
4. **Words file**: `--words-file` with a valid file produces correct theme words; `#` comments are skipped.
5. **Hybrid mode**: `--words APOLON --theme mitologie --llm` → output contains `APOLON` (user) plus additional words with `source: "gemini"`.
6. **Hybrid with description**: `--theme-description` text appears in the LLM prompt context (verify via `--log-level DEBUG`).
7. **Validation errors**: neither flag → error; `--llm` without `--theme` → error.
