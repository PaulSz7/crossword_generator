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
- **Theme word placement**: Places 2-5 theme words before layout completion
- **Blocker zone placement**: Random corner/center placement adhering to project rules
- **TSV-based Romanian dictionary**: Preprocessing with diacritic normalization
- **Structured JSON output**: Compatible with downstream renderers
- **Configurable timeouts**: Automatic retry with fresh seeds if generation fails

## Quick Start

```bash
python main.py --height 10 --width 15 --theme "mitologie" \
  --dictionary local_db/dex_words.tsv --seed 42 --completion-target 1.0
```

The command above generates a 10×15 crossword with theme words related to "mitologie".
The CP-SAT solver ensures 100% cell coverage in under 20 seconds.

### Dictionary preprocessing cache

Processing the raw `dex_words.tsv` file once yields a compact, deduplicated
DataFrame that subsequent runs can load much faster:

```bash
python -m crossword.data.preprocess --source local_db/dex_words.tsv
```

The command writes `local_db/dex_words_processed.tsv` containing one row per normalized
entry (diacritics removed). `WordDictionary` auto-generates this cache on first
run if missing.

### Fast debug runs

```python
from debug_main import run_debug

# Single run with defaults (10x15, mitologie theme)
result = run_debug(max_runs=15)

# Custom size and theme
result = run_debug(max_runs=15, height=12, width=18, theme="natura")
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

All 9 tests should pass.

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