# Difficulty Levels — Implementation Record

**Status**: Implemented
**Date**: 2026-02-22
**Based on**: `difficulty_feature_plan.md`

## Summary

Implemented 3 difficulty levels (EASY, MEDIUM, HARD) across fill word selection, theme word generation, and clue complexity. Uses a combination of hard tier guarantees (floors in `compute_difficulty_score`, strict candidate filtering) and soft scoring preferences (tier-affinity blending in `WordEntry.score()`).

## Files Modified

| File | Changes |
|------|---------|
| `local_db/dex_query.sql` | Added `source_short_name`, `tags`, `definition_count`, `source_count` columns; added GROUP BY clause; removed `def_abbrevs` CTE (moved to Python) |
| `crossword/core/constants.py` | Added `Difficulty(str, Enum)` with EASY, MEDIUM, HARD |
| `crossword/data/preprocess.py` | Python-side abbreviation extraction (`_load_abbreviation_lookup`, `_extract_def_abbrevs`); refactored `_tag_difficulty_score` to substring matching; consolidated `_FOREIGN_LANG_TAGS` to root forms; two-layer `compute_difficulty_score` with hard floors; adult-word filtering |
| `crossword/data/dictionary.py` | `difficulty_score` on `WordEntry`; direction-bonus `score()` (base×0.15 + affinity×0.55 + direction×0.30); `fallback_fraction` on `find_candidates()`; `difficulty` on `DictionaryConfig` |
| `crossword/data/theme.py` | `difficulty`+`language` params on all generators; tiered dummy buckets (`dict[str, dict[str, List[str]]]`); `merge_theme_generators()` passes params through |
| `crossword/io/clues.py` | Split prompt into `CLUE_RULES` + `CLUE_DIFFICULTY_PROMPT` per tier; `difficulty`+`language` on all `ClueGenerator` implementations |
| `crossword/engine/generator.py` | `difficulty`+`language` on `GeneratorConfig`; two-phase EASY fill (`_EASY_PHASE1_RETRIES=3`); `allow_phase2` flag threaded through `generate()` → `_cpsat_fill()` |
| `crossword/engine/solver.py` | `max_difficulty_score` parameter: hard-filters candidates with `DS < threshold`; `medium_slot_limit` parameter: allows up to N slots with zero easy candidates to use full pool, rejects layout if exceeded |
| `crossword/utils/pretty.py` | `print_crossword_stats()`: fill-words-only difficulty breakdown (theme words excluded from easy/medium/hard counts, shown separately) |
| `main.py` | `--difficulty` (EASY/MEDIUM/HARD, default MEDIUM) and `--language` CLI args |
| `debug_main.py` | Default grid 15×12; `difficulty: "EASY"` default; calls `print_crossword_stats()` |
| `tests/test_theme.py` | Updated test buckets to tiered format; updated `EmptyGenerator.generate()` signature |

## Key Design Decisions

### Two-layer difficulty score (compute_difficulty_score)

Hard floors guarantee a minimum tier regardless of frequency, then a continuous base score differentiates within the tier:

```python
base = (
    0.40 * (1.0 - frequency)
    + 0.20 * length_score        # (min(length,12) - 3) / 9
    + 0.15 * tag_score
    + 0.10 * source_score
    + 0.05 * (1 - min(source_count, 5) / 5)
    + 0.05 * (1 - min(definition_count, 10) / 10)
)

floor = 0.0
if tag_score == 1.0:      floor = 0.60   # hard tag → guaranteed hard tier
elif tag_score == 0.5:    floor = 0.30   # medium/foreign tag → guaranteed medium tier
if source_score == 1.0:   floor = max(floor, 0.35)  # rare source → at least medium

return max(base, floor)
```

A common word with a hard tag gets bumped to DS≥0.60 regardless of frequency.

### Tag matching (\_tag\_difficulty\_score)

Splits by `|` only, then does substring search (`pat in part`) so compound tag values like `"argou; argotic"` or `"regional > Banat"` are caught without a secondary split. `_FOREIGN_LANG_TAGS` uses root forms (e.g. `"latin"` catches `latinism`, `latinesc`); `"limba "` catches `"limba engleză"` etc.

### Python-side abbreviation extraction

The `def_abbrevs` CTE was removed from SQL (caused MySQL Error 2013 timeout via unindexable LIKE cross-join). Replaced by `_load_abbreviation_lookup()` reading `distinct_abbreviations.csv` and `_extract_def_abbrevs()` applying regex `#([^#]+)#` to the definition text. Stores `Dict[str, List[str]]` to handle shorts with multiple expansions.

### Word selection formula (WordEntry.score)

Direction bonus ensures correct off-tier ordering (HARD > MEDIUM > EASY for HARD difficulty; EASY > MEDIUM > HARD for EASY):

```python
def score(self, difficulty: Difficulty = Difficulty.MEDIUM) -> float:
    base = self.frequency
    if self.is_compound: base -= 0.15
    if self.is_stopword: base -= 0.3
    distance = abs(self.difficulty_score - _TIER_CENTER[difficulty])
    affinity = max(0.0, 1.0 - distance * 3.5)
    if difficulty == Difficulty.EASY:
        direction = 1.0 - self.difficulty_score
    elif difficulty == Difficulty.HARD:
        direction = self.difficulty_score
    else:
        direction = 0.5
    return max(0.0, base * 0.15 + affinity * 0.55 + direction * 0.30)
```

### EASY difficulty enforcement (two-phase fill)

The CP-SAT solver treats all candidates equally — having 90% easy candidates in the pool does not guarantee 90% easy words in the output. Enforcement uses:

1. **Hard candidate filter** (`max_difficulty_score=0.3`, strict `<`): only words with DS<0.3 enter the solver's candidate pool
2. **Two-phase fill** in `_cpsat_fill`:
   - **Phase 1** (attempts 1–`_EASY_PHASE1_RETRIES=3`): `medium_slot_limit=0` — any slot with zero easy candidates causes immediate rejection → outer retry loop picks a new layout+theme
   - **Phase 2** (attempts 4+): `medium_slot_limit = max(2, slots//10)` — slots with zero easy candidates get the full unfiltered pool; if more slots need this than the limit, the layout is rejected
3. Theme words and blocker zone cells are **excluded** from the difficulty percentage (only fill words counted)

### Stats display (pretty.py)

`print_crossword_stats()` computes easy/medium/hard counts from **fill words only** (non-theme, ≥3 letters). Theme words are shown separately as "Theme avg DS". Section header reads "Difficulty (fill words only)".

### Tiered theme buckets

`DEFAULT_THEME_BUCKETS` and `FALLBACK_BUCKET` are `dict[str, dict[str, list[str]]]` (theme → tier → words). Both `GeminiThemeWordGenerator` and `DummyThemeWordGenerator` accept `difficulty` and `language` kwargs.

### Prompts in English

All LLM prompts (theme + clue) written in English with `{language}` parameter for future multi-language support.

### Adult word filtering

`preprocess_dictionary()` skips any row where `is_adult=1` before aggregation.

### Backwards compatibility

Old processed TSVs without new columns still load via `load_processed_dictionary()` which falls back to `difficulty_score = 1.0 - frequency` when source/tag columns are missing.

## Test Results

### Dictionary distribution (current processed cache)

| Bucket | Count | Percentage |
|--------|-------|------------|
| EASY (<0.3) | — | — |
| MEDIUM (0.3-0.6) | — | — |
| HARD (>=0.6) | — | — |

*(Run the quick snippet in debug_main to get current numbers after cache regeneration)*

### Full generation (15×12, theme="mitologie", difficulty=EASY)

| Result | Value |
|--------|-------|
| Easy words | ≥90% (100% when phase 1 succeeds) |
| Medium words | 0 (phase 1) or ≤`max(2, slots//10)` (phase 2) |
| Typical attempts needed | 1–10 |

## Remaining Work

1. **Re-export `dex_words.tsv`** from dexonline with the updated SQL (`source_short_name`, `tags`, `definition_count`, `source_count`) to activate all scoring signals
2. **Delete processed cache** (`dex_words_processed.tsv`) after re-export to trigger regeneration with full difficulty scores
3. **Verify** HARD tier produces genuinely different fill words once source/tag data is available
4. **Tune `_EASY_PHASE1_RETRIES`** and `medium_slot_limit` if generation still requires too many attempts after re-export
