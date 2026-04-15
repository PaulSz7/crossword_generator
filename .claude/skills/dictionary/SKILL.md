---
name: dictionary
description: "Dictionary loading, preprocessing, candidate retrieval, normalization"
user-invocable: false
paths: ["crossword/data/dictionary.py", "crossword/data/preprocess.py", "crossword/data/normalization.py"]
---

# Dictionary System

## WordDictionary (`dictionary.py:79`)

The central word lookup used by the solver, validator, and theme generators.

### Internal Indexes
- `_entry_by_surface: Dict[str, WordEntry]` — primary lookup by uppercase ASCII surface
- `_entries_by_length: Dict[int, List[WordEntry]]` — entries grouped by length
- `_position_index: Dict[int, Dict[Tuple[int,str], Set[str]]]` — `length -> (position, letter) -> surfaces` for fast pattern matching
- `_surfaces_by_length: Dict[int, Set[str]]` — flat surface sets by length

### WordEntry dataclass (`dictionary.py:43`)
- `surface`, `raw_forms`, `length`, `definition`, `lemma`
- `frequency`, `is_compound`, `is_stopword`
- `difficulty_score: float` — computed by preprocessing
- `score(difficulty)` — returns a composite score blending frequency, difficulty affinity, and direction bonus

### DictionaryConfig (`dictionary.py:23`)
- `path` — source TSV path
- `difficulty: Difficulty` — EASY/MEDIUM/HARD (affects scoring tier center)
- `allow_compounds: bool` — enables multi-word entries (linked to `allow_multi_word`)
- `exclude_stopwords: bool = True`
- `max_entries_per_length: Optional[int]` — cap per length bucket

## Solver Interface

```python
find_candidates(length, pattern, banned, preferred, limit, fallback_fraction) -> List[WordEntry]
```

- `pattern`: sequence of known letters or `None` per position
- Uses `_index_lookup()` with set intersection (smallest-first) for fast filtering
- `fallback_fraction` reserves part of the limit for MEDIUM-scored off-tier candidates
- `has_candidates()` and `count_candidates()` for quick feasibility checks without materializing entries

## Normalization (`normalization.py`)

- `clean_word(text)` — strips Romanian diacritics (ă->a, â->a, î->i, ș->s, ț->t), removes non-alpha, uppercases
- `extract_word_breaks(text)` — records space positions before stripping (for multi-word display)
- `display_form(surface, word_breaks)` — reconstructs spaced display from flat surface
- `ROMANIAN_DIACRITICS` — mapping dict used by both functions

## Preprocessing (`preprocess.py`)

Pipeline: `dex_words.tsv` -> `dex_words_processed.tsv` (auto-generated on first run or when missing).

### Difficulty Scoring
- `_source_rarity_score`: 0.0=common (DEX '96/DEX '98), 0.5=unknown, 1.0=rare (Scriban/CADE), 2.0=hard (DAR/DRAM/Argou/DLRLV)
- `_tag_difficulty_score`: word-boundary matching for `_HARD_TAGS`/`_MEDIUM_TAGS`; substring for foreign language tag prefixes
- `compute_difficulty_score`: base = source + tag scores, with floors (tag hard -> 0.60, tag medium -> 0.27, hard source -> 0.60, rare source -> 0.35)
- "glife rare" is a typographic metadata tag — word-boundary matching prevents false positive from "rar" substring

### Adult Content Flag
- `is_adult: bool` in `ProcessedWordRecord` — OR-aggregated across duplicate surfaces
- ~145 flagged words covering explicitly sexual/reproductive terms
- Anatomical terms like ANUS caught by strict LLM safety check in clue generation instead

## Regeneration Commands

```bash
# Regenerate from MySQL
mysql -u root dexonline --batch < local_db/dex_query.sql > local_db/dex_words.tsv

# Regenerate processed cache
rm local_db/dex_words_processed.tsv && .venv/bin/python -m crossword.data.preprocess --source local_db/dex_words.tsv
```
