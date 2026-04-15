---
name: generator-pipeline
description: "Crossword generator orchestration, config, CLI entrypoints"
user-invocable: false
paths: ["crossword/engine/generator.py", "main.py", "debug_main.py"]
---

# Generator Pipeline

## GeneratorConfig (`generator.py:42`)

All configuration fields with key defaults:
- Grid: `height`, `width`, `place_blocker_zone`, `blocker_zone_*` overrides
- Theme: `theme_title`, `theme_type`, `theme_description`, `extend_with_substring`
- Fill: `completion_target=0.85`, `max_iterations=2500`, `fill_timeout_seconds=180.0`
- Quality: `min_theme_coverage=0.10`, `max_theme_ratio=0.4`, `theme_request_size=80`
- Difficulty: `difficulty="MEDIUM"`, `language="Romanian"`
- Flags: `allow_adult=False`, `allow_multi_word=False`, `allow_repair=False`, `strict_user_words=True`
- Retry: `retry_limit=3`, `seed`

Helper methods:
- `to_grid_config(seed_override)` — creates GridConfig with deterministic blocker seed
- `to_dictionary_config()` — creates DictionaryConfig from relevant fields

## CrosswordResult (`generator.py:116`)

- `grid: CrosswordGrid`, `slots: List[WordSlot]`, `theme_words: List[ThemeWord]`
- `validation_messages`, `seed`, `crossword_title`, `theme_content`

## CrosswordGenerator (`generator.py:138`)

### Constructor Dependencies
- `config`, `dictionary` (auto-created if None), `theme_generator`, `clue_generator`
- `theme_fallback_generators` (defaults to `[DummyThemeWordGenerator]`)
- `store: CrosswordStore`, `theme_cache: ThemeCache`, `definition_fetcher: GeminiDefinitionFetcher`

### Pipeline Phases (in `generate()`)
1. **Setup** — `_reset_state()`, create grid from config
2. **Theme seeding** — `_seed_theme_words()` calls `merge_theme_generators()`, places theme words on grid
3. **Layout** — iterative clue box + slot placement until `completion_target` reached
4. **CP-SAT fill** — `solve_crossword()` fills remaining slots
5. **Validation** — `GridValidator.validate()` checks all rules
6. **Checkpoint** — `store.save_filled()` persists the filled state (pre-clue)
7. **Definition fetch** — `definition_fetcher.fetch_batch()` for words with missing/incomplete defs
8. **Clue routing** — splits slots into `theme_bundles` (gemini source) and `clue_requests` (everything else)
9. **Clue generation** — `clue_generator.generate(clue_requests)` via LLM
10. **Attach** — `attach_clues_to_grid()` populates clue box cells
11. **Result** — builds `CrosswordResult`, saves success document

### Retry Loop
- Up to `retry_limit` attempts with `_reset_state()` between failures
- EASY difficulty: `_EASY_PHASE1_RETRIES` attempts with easy-only fill, then allows medium fallback
- On failure: `store.save_failure()` with partial state

### Theme Generator Wiring (complex routing in `main.py`)
Depends on `theme_type` + `user_words` + `--llm` flag:
- `domain_specific_words`: user words -> UserWordListGenerator primary, optionally GeminiThemeWordGenerator fallback with `--llm`
- `words_containing_substring`: UserWordListGenerator primary (if words), SubstringThemeWordGenerator wired internally
- `joke_continuation`: GeminiThemeWordGenerator primary (or UserWordListGenerator if words provided)
- `custom`: GeminiThemeWordGenerator always primary

## debug_main.py

Step-by-step helper functions for interactive debugging:

- `prepare_state(**overrides)` — creates config, dictionary, generators from `DEFAULT_DEBUG_ARGS`
- `step_seed_theme(state)` — runs theme generation + seeding
- `step_fill(state)` — runs CP-SAT fill
- `step_validate(state)` — runs validation
- `step_clues(state)` — runs definition fetch + clue routing + clue generation + attach
- `build_result(state)` — assembles `CrosswordResult`
- `run_debug(**overrides)` — auto-retry wrapper with `parallel_runs` support
- `resume_from_filled(doc_id, **overrides)` — reconstructs grid from filled checkpoint, runs clues only

## Critical Gotcha

Three separate code paths handle clue routing:
1. `generator.generate()` — the main pipeline
2. `debug_main.step_clues()` — debug step function
3. `debug_main.resume_from_filled()` — resume from checkpoint

Changes to clue routing logic must be mirrored across all three. Each builds `ClueRequest` lists and `theme_bundles` dicts independently.

## main.py CLI

Key flags: `--height`, `--width`, `--theme-title`, `--theme-type`, `--words`, `--words-file`, `--llm`, `--clues`, `--resume DOC_ID`, `--difficulty`, `--allow-multi-word`, `--allow-repair`, `--no-blocker-zone`

`--resume` bypasses the entire layout/fill pipeline and calls `resume_from_filled()` directly.
