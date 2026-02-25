# Romanian Cryptic Barred Crossword Generator

This repository implements a two-phase crossword generation algorithm that produces
Romanian cryptic barred crosswords using constraint programming. The generator first
creates a valid slot layout, then uses OR-Tools CP-SAT to fill all slots with dictionary
words in 5-16 seconds with near 100% success rate.

## Features

- **Two-phase architecture**: Layout generation followed by CP-SAT word filling
- **Fast & reliable**: 5-16 seconds per crossword, 5/5 success rate on 10×15 grids
- **OR-Tools CP-SAT solver**: Constraint satisfaction with table constraints and uniqueness
- **Intelligent layout**: Partitions long runs (max 8-10 letters) to target 4-8 letter slots
- **Three theme generation modes**: theme-only (dummy/LLM), words-only (user list), hybrid (user list + LLM extension)
- **Blocker zone placement**: Random corner/center placement with optional CLI overrides
- **TSV-based Romanian dictionary**: Preprocessing with diacritic normalization
- **Structured JSON output**: Compatible with downstream renderers
- **Configurable timeouts**: Automatic retry with fresh seeds if generation fails

## Quick Start

Export dictionary from db
```bash
mysql -u root dexonline --default-character-set=utf8mb4 --batch --quick < local_db/dex_query.sql > 
local_db/dex_words.tsv
```

```bash
python main.py --height 10 --width 15 --theme-title "mitologie" \
  --dictionary local_db/dex_words.tsv --seed 42 --completion-target 1.0
```

The command above generates a 10×15 crossword with theme words related to "mitologie".
The CP-SAT solver ensures 100% cell coverage in under 20 seconds.

### Blocker zone controls

You can disable blocker zone placement entirely or force a custom rectangle via CLI flags:

```bash
# Disable the blocker zone
python main.py --height 10 --width 15 --theme-title "mitologie" --no-blocker-zone

# Force a 5×10 top strip blocker zone (rows 0-4, cols 0-9)
python main.py --height 20 --width 10 --theme-title "mitologie" \
  --blocker-zone-height 5 --blocker-zone-width 10 --blocker-zone-row 0 --blocker-zone-col 0
```

Omit the `--blocker-zone-row/col` arguments to keep randomized placement while still
overriding the rectangle dimensions.

### Theme generation modes

The generator supports three modes for supplying theme words:

**Theme-only** (original behaviour) — dummy word buckets fill the theme pool from predefined lists keyed by theme name:

```bash
python main.py --height 10 --width 12 --theme-title mitologie
```

**Words-only** — an explicit user-supplied list is used as the entire theme pool. No LLM is called and no dummy words are added. The minimum coverage check is skipped (the user is responsible for how many words are provided). User words bypass crossing-slot validation so they are always placed if they fit geometrically:

```bash
# Inline words (WORD or WORD:Clue)
python main.py --height 10 --width 12 \
  --words APOLON ARES 'ATHENA:Zeita intelepciunii'

# From a file (one entry per line, # comments and blank lines ignored)
python main.py --height 10 --width 12 --words-file my_words.txt
```

`my_words.txt` format:
```
# Lines starting with # are comments
APOLON:Zeul soarelui
ARES
ATHENA:Zeita intelepciunii
```

**Hybrid** — user-supplied words are placed first (guaranteed), then Gemini extends the pool to reach coverage targets. If no Gemini API key is available the dummy generator acts as a second fallback:

```bash
python main.py --height 10 --width 12 \
  --words 'APOLON:Zeul soarelui' ARES \
  --theme-title mitologie \
  --theme-description "Zeii olimpieni din mitologia greaca" \
  --llm
```

`--llm` requires `--theme-title`. `--theme-description` is optional additional context for the LLM prompt.

Gemini integration expects an API key and optional model override exposed via environment variables before running
the generator:

```bash
export GEMINI_API_KEY="sk-your-key"
export GEMINI_MODEL="gemini-2.5-flash"  # optional
python main.py --height 10 --width 12 --theme-title mitologie --llm
```

The fallback chain for each mode:

| Mode | Flags | Fallback chain |
|------|-------|----------------|
| Theme-only | `--theme X` | `DummyThemeWordGenerator` |
| Words-only | `--words ...` | *(none)* |
| Hybrid | `--words ... --theme X --llm` | `GeminiThemeWordGenerator → DummyThemeWordGenerator` |

### Dictionary preprocessing cache

Processing the raw `dex_words.tsv` file once yields a compact, deduplicated
DataFrame that subsequent runs can load much faster:

```bash
python -m crossword.data.preprocess --source local_db/dex_words.tsv
```

The command writes `local_db/dex_words_processed.tsv` containing one row per normalized
entry (diacritics removed). `WordDictionary` auto-generates this cache on first
run if missing.

Whenever `dex_query.sql` changes or a fresh DB export is needed, regenerate both files:

```bash
mysql -u root dexonline --default-character-set=utf8mb4 --batch < local_db/dex_query.sql \
  > local_db/dex_words.tsv
rm -f local_db/dex_words_processed.tsv
python -m crossword.data.preprocess --source local_db/dex_words.tsv
```

### Word difficulty scoring

Every preprocessed entry carries a `difficulty_score` in [0, 1] (higher = harder).
The score drives EASY/MEDIUM/HARD mode: in EASY mode the CP-SAT solver only considers
fill words with `difficulty_score < 0.30`.

**Score formula** (`compute_difficulty_score` in `crossword/data/preprocess.py`):

```
base = 0.40 × (1 − frequency)
     + 0.20 × length_score          # normalised word length 3–12
     + 0.15 × tag_score             # from linguistic register tags
     + 0.10 × source_score          # from dictionary source rarity
     + 0.05 × (1 − source_count)    # fewer sources → harder
     + 0.05 × (1 − definition_count)
score = max(base, floor)
```

**Hard floors** guarantee a minimum tier regardless of base score.
The medium floors (0.30) are **bypassed when `frequency ≥ 0.75`** — very
common words are allowed into EASY even if their source or tag would otherwise
push them to MEDIUM:

| Trigger | Floor | Tier | Freq bypass |
|---------|-------|------|-------------|
| Hard tag (`rar`, `regional`, `argou`, …) | 0.60 | HARD | no |
| Hard-source dictionary (DAR, DRAM, Argou, DLRLV) | 0.60 | HARD | no |
| Medium/foreign tag (`livresc`, `popular`, loanword markers) | 0.30 | MEDIUM | yes (≥ 0.75) |
| Rare-source dictionary (Scriban, CADE, DTM, …) | 0.35 | MEDIUM | no |
| Other source (truncated definitions — DEX '09, MDA2, DOOM, …) | 0.30 | MEDIUM | yes (≥ 0.75) |

**Source classification** (`_source_rarity_score`):

Only DEX '96 and DEX '98 have `canDistribute=1` in the database, meaning their
`Definition.internalRep` is complete in the public SQL dump. All other sources
have truncated stubs and cannot reliably confirm a word's register, so they
receive a medium floor at minimum (unless bypassed by high frequency).

| Score | Floor | Category | Examples |
|-------|-------|----------|---------|
| 0.0 | none | Known-complete | DEX '96, DEX '98 |
| 0.5 | 0.30 | Other (truncated defs) | DEX '09, MDA2, DOOM 3, DLRLC, NODEX, … |
| 1.0 | 0.35 | Rare | Scriban, CADE, DTM, DGS, DER |
| 2.0 | 0.60 | Hard | DAR, DRAM, DRAM 2015, DRAM 2021, Argou, DLRLV |

**`_best_source` and the `all_sources` SQL column**

A word can appear in multiple dictionaries (e.g. "alb" appears in both
`DEX '98` and `Argou`). The SQL query's `entry_counts` CTE uses
`GROUP_CONCAT(DISTINCT s.shortName … SEPARATOR '|')` to expose *all* source
names for each entry, mirroring how `entry_tags` aggregates tags.

`_best_source(all_sources_str)` then picks the **least-rare** source
(minimum `_source_rarity_score`) so a common word like "alb" is classified
by `DEX '98` (score 0.0) rather than `Argou` (score 2.0), while a word that
appears exclusively in `Argou` correctly receives the hard floor.

**Tag signals** come from three sources, all merged into the `tags` field:

1. **ObjectTag entries** (`entry_tags` CTE) — curated editor tags at entry,
   lexeme, and definition level; captured across all definitions regardless of rank.
2. **`#abbrev#` markers** in `internalRep` — expanded via `distinct_abbreviations.csv`
   into full register phrases (e.g. `#sl.#` → `"limba slavonă; slavon"`). Only
   extracted from DEX '96/'98 rows (other sources have truncated stubs).
3. **Inline parenthetical markers** — plain-text register labels such as `(Rar)`,
   `(Regional)`, `(Familiar)` embedded in the definition text. Extracted by
   `_extract_paren_tags` and kept only when they match the known tag vocabulary.
   Only applied to DEX '96/'98 rows.

**Tag matching** uses word-boundary checking for single-word patterns (so
`"rar"` does not trigger on `"glife rare"`), and simple substring matching for
prefix patterns in `_FOREIGN_LANG_TAGS` (e.g. `"angl"` catches `"anglicism"`).

**Current distribution** across 282,895 words:

| Tier | Range | Count | % | Key length counts |
|------|-------|-------|---|-------------------|
| EASY | < 0.30 | ~90K | 31.7% | 4-letter: 2,090 / 5-letter: 5,708 / 8-letter: 18,590 |
| MEDIUM | 0.30–0.60 | ~137K | 48.4% | — |
| HARD | ≥ 0.60 | ~56K | 19.9% | — |

### Fast debug runs

```python
from debug_main import run_debug

# Theme-only (default DEFAULT_DEBUG_ARGS)
result = run_debug(max_runs=15)

# Custom size and theme
result = run_debug(max_runs=15, height=12, width=18, theme="natura")

# Words-only: place exactly these two words, no fallback, no coverage check
result = run_debug(words=["ZEUS", "THOR"], theme="", max_runs=5)

# Hybrid: user words guaranteed, Gemini extends (dummy fallback if no API key)
result = run_debug(words=["ZEUS:Regele zeilor", "THOR"], theme="mitologie", llm=True)
```

The `run_debug()` helper retries different RNG seeds automatically until it finds
a successful layout. Each attempt takes 5-16 seconds, so 15 attempts complete in
under 4 minutes total.

## Architecture

### Phase 1: Layout Generation (`_complete_layout`)

1. **Heal isolated cells**: Convert unreachable EMPTY_PLAYABLE cells to CLUE_BOX
2. **Partition long runs**: Two-pass partitioning at max_len=10, then max_len=8
   - Avoids 3-letter slots (only 779 dictionary words)
   - Targets 4-8 letter slots (3K-48K dictionary candidates)
   - Penalty scoring avoids creating 3-letter remainders
3. **Ensure licensing**: Every slot boundary gets a licensing clue box
4. **Verify feasibility**: All ≥3-letter slots have dictionary candidates

### Phase 2: CP-SAT Filling (`_cpsat_fill`)

1. **Cell variables**: IntVar(0-25) for unfilled cells, constants for theme words
2. **Table constraints**: `add_allowed_assignments` for each slot's valid words
3. **Uniqueness**: Pairwise differ constraints for same-length slots
4. **Solve**: OR-Tools CP-SAT with 30s timeout, 4 workers (typically 1-5s)
5. **Place words**: Apply solution to grid, register slots

### Performance

| Metric | v1 (Growth+CSP) | v2 (Layout+CP-SAT) |
|--------|-----------------|---------------------|
| Time per attempt | ~75s | **5-16s** |
| Success rate | ~60% | **100%** (5/5) |
| Code size | ~1750 lines | **~570 lines** |
| Dictionary size | Small (779 3-letter) | Small (779 3-letter) |

## Project Layout

- `crossword/core/` – Cell constants, dataclasses, and exceptions
- `crossword/data/` – Dictionary ingestion and theme-word providers
- `crossword/engine/` – Grid manager, **generator.py** (layout), **solver.py** (CP-SAT)
- `crossword/io/` – Clue formatting helpers
- `crossword/utils/` – Logging and utilities
- `main.py` – Top-level CLI entrypoint
- `debug_main.py` – Fast debug helper with auto-retry
- `local_db/dex_words.tsv` – Romanian lexicon used during filling

## Development

Install dependencies via Poetry:

```bash
poetry install
```

This installs OR-Tools (≥9.8) and other dependencies.

Run tests:

```bash
poetry run pytest
```

All 61 tests should pass.

## How It Works

### Dictionary Coverage by Length

```
Length 2:     2 words (unconstrained - validator ignores)
Length 3:   779 words (bottleneck - avoid many 3-letter slots)
Length 4: 3,257 words (sweet spot begins)
Length 5: 10,406 words
Length 6: 22,042 words
Length 7: 36,812 words
Length 8: 48,600 words (sweet spot peak)
```

The layout phase partitions long spans to create mostly 4-8 letter slots, which
have 3K-48K candidates each. This ensures the CP-SAT solver has enough flexibility
to find a valid assignment in seconds.

### Key Constraints

- **Table constraints**: Each slot's cell variables must spell a word from its
  candidate list (dictionary lookup filtered by pattern and banned words)
- **Uniqueness**: No two slots can contain the same word (pairwise differ)
- **Cross-compatibility**: Slots sharing cells implicitly constrain each other
  through shared IntVar assignments
