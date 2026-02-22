# Difficulty Levels Feature Plan

## Goal

Add 3 difficulty levels (EASY, MEDIUM, HARD) to the crossword generator. Common, everyday words go into EASY; unusual, archaic, or domain-specific words are reserved for HARD.

## Available Signals in dexonline

### Already captured in `dex_query.sql`

| Signal | Column | Notes |
|--------|--------|-------|
| Lexeme frequency | `l.frequency` | Float 0.0-1.0. P25=0.54, P50=0.74, P90=0.96. Higher = more common |
| Compound flag | `l.compound` | Bool. Only 1,274 of 361K rows |
| Stopword flag | `l.stopWord` | Bool. Only 46 rows |

### New signals to add via SQL

#### 1. Definition source (`d.sourceId` -> `Source`)

The `Source` table identifies which dictionary a definition comes from. Source identity is a strong commonality signal:

- **Common-word sources**: DEX '16/'12/'09 (modern explanatory), DOOM 2/3 (orthographic standard), MDA2 (academic mini-dict)
- **Rare/specialized sources**: DAR (archaisms & regionalisms), DRAM (Maramures regional), Argou (slang), DTM (musical terms), DGE (gastronomy)

Query addition:
```sql
SELECT ...
    s.shortName AS source_short_name,
    s.id        AS source_id
FROM ...
JOIN Source s ON s.id = d.sourceId
```

#### 2. Tags via `ObjectTag` + `Tag`

Tags are associated with definitions (`ObjectTag.objectType = 1`) and meanings (`objectType = 3`). Key difficulty-relevant tags from dexonline.ro/etichete:

| Tag value | Difficulty signal |
|-----------|-------------------|
| `rar` | HARD |
| `inv` / `invechit` / `ieșit din uz` | HARD (archaic) |
| `regional` + sub-tags (Banat, Moldova, etc.) | HARD |
| `popular` | EASY (folk/common) |
| `familiar` | EASY (conversational) |
| `livresc` | HARD (bookish) |
| `argou` / `argotic` | HARD (slang) |
| `disciplina: *` (100+ domains) | HARD (technical) |

Query addition (aggregate tags per definition):
```sql
SELECT ...
    GROUP_CONCAT(DISTINCT t.value ORDER BY t.value SEPARATOR '|') AS tags
FROM ...
LEFT JOIN ObjectTag ot
    ON ot.objectId = d.id AND ot.objectType = 1
LEFT JOIN Tag t
    ON t.id = ot.tagId
...
GROUP BY f.id, d.id
```

#### 3. Definition count per entry

Words with more definitions across more sources tend to be more common. Count distinct definitions and distinct sources per entry:

```sql
SELECT ...
    (SELECT COUNT(DISTINCT ed2.definitionId)
     FROM EntryDefinition ed2 WHERE ed2.entryId = e.id) AS definition_count,
    (SELECT COUNT(DISTINCT d2.sourceId)
     FROM EntryDefinition ed2
     JOIN Definition d2 ON d2.id = ed2.definitionId AND d2.status = 0
     WHERE ed2.entryId = e.id) AS source_count
```

#### 4. Definition length as a proxy

Longer, more elaborate definitions often indicate rarer or more specialized words. Already captured as `definition_length`.

## Updated SQL Query

```sql
SELECT
    f.formNoAccent              AS entry_word,
    CHAR_LENGTH(f.formNoAccent) AS entry_length,

    d.internalRep               AS definition,
    CHAR_LENGTH(d.internalRep)  AS definition_length,

    l.formNoAccent              AS lemma,
    l.frequency                 AS lexeme_frequency,
    l.compound                  AS is_compound,

    e.adult                     AS is_adult,
    inf.description             AS inflection_description,
    l.stopWord                  AS is_stopword,

    -- NEW: source info
    s.shortName                 AS source_short_name,

    -- NEW: tag aggregation
    GROUP_CONCAT(DISTINCT t.value ORDER BY t.value SEPARATOR '|') AS tags,

    -- NEW: entry breadth (more defs/sources = more common)
    (SELECT COUNT(DISTINCT ed2.definitionId)
     FROM EntryDefinition ed2 WHERE ed2.entryId = e.id)   AS definition_count,
    (SELECT COUNT(DISTINCT d2.sourceId)
     FROM EntryDefinition ed2
     JOIN Definition d2 ON d2.id = ed2.definitionId AND d2.status = 0
     WHERE ed2.entryId = e.id)                             AS source_count

FROM Entry e
JOIN EntryLexeme el
    ON el.entryId = e.id
   AND el.main = 1
JOIN Lexeme l
    ON l.id = el.lexemeId
JOIN InflectedForm f
    ON f.lexemeId = l.id
   AND f.recommended = 1
JOIN EntryDefinition ed
    ON ed.entryId = e.id
JOIN Definition d
    ON d.id = ed.definitionId
   AND d.status = 0
JOIN Source s
    ON s.id = d.sourceId
LEFT JOIN Inflection inf
    ON inf.id = f.inflectionId
LEFT JOIN ObjectTag ot
    ON ot.objectId = d.id AND ot.objectType = 1
LEFT JOIN Tag t
    ON t.id = ot.tagId
WHERE
    ed.definitionRank = 1
    AND (
      NOT (LOWER(inf.description) LIKE '%articulat%'
           AND LOWER(inf.description) NOT LIKE '%nearticulat%')
      AND (
             LOWER(inf.description) LIKE '%nearticulat%'
          OR LOWER(inf.description) LIKE '%infinitiv%'
          OR LOWER(inf.description) LIKE '%indicativ, prezent%'
      )
      AND (LOWER(inf.description) NOT LIKE '%genitiv%'
           OR LOWER(inf.description) NOT LIKE '%dativ%')
    )
    AND CHAR_LENGTH(f.formNoAccent) > 2
    AND f.apheresis = 0
GROUP BY f.id, d.id
```

## Difficulty Scoring Algorithm

### Phase 1: Compute a `difficulty_score` (0.0 = easiest, 1.0 = hardest) per word

```
difficulty_score = weighted_sum(
    0.35 * (1.0 - frequency),           # low frequency = harder
    0.25 * source_rarity_score,          # rare source = harder
    0.20 * tag_difficulty_score,         # archaic/regional/technical tags = harder
    0.10 * (1.0 - normalized_source_count),  # fewer sources = harder
    0.10 * (1.0 - normalized_def_count),     # fewer definitions = harder
)
```

Where:
- `source_rarity_score`: 0.0 for DEX/DOOM/MDA, 0.5 for older academic, 1.0 for DAR/DRAM/Argou/specialized
- `tag_difficulty_score`: 0.0 if no difficulty tags, 0.5 for `livresc`, 1.0 for `rar`/`invechit`/`regional`
- `normalized_source_count`: min(source_count, 5) / 5
- `normalized_def_count`: min(definition_count, 10) / 10

### Phase 2: Bucket into difficulty tiers

| Level | Score range | Description |
|-------|------------|-------------|
| EASY | 0.00 - 0.30 | High-frequency words in major dictionaries, no special tags |
| MEDIUM | 0.30 - 0.60 | Moderate frequency, may have some specialized usage |
| HARD | 0.60 - 1.00 | Low frequency, archaic, regional, technical, or rare tags |

### Phase 3: Soft preference in candidate scoring (no hard filtering)

**Important**: Difficulty levels do NOT restrict the word pool. All words remain available at every difficulty level. Instead, difficulty controls a **scoring bonus/penalty** in `WordEntry.score()` so that the CP-SAT solver and candidate selection *prefer* words matching the target difficulty, while still allowing out-of-tier words when needed to complete the grid.

This preserves generation success rate (especially for constrained slots like 3-letter words where candidates are scarce) while still producing noticeably different puzzles per difficulty level.

#### Scoring adjustment

Given a target difficulty tier and a word's `difficulty_score`:

```
tier_center = { EASY: 0.15, MEDIUM: 0.45, HARD: 0.80 }
distance = abs(difficulty_score - tier_center[target])
affinity_bonus = max(0, 1.0 - distance * 2.5)   # 1.0 at center, 0.0 far away
```

The `affinity_bonus` (range 0.0-1.0) is folded into `WordEntry.score()`:

```python
def score(self, difficulty: str = "MEDIUM") -> float:
    base = self.frequency
    if self.is_compound:
        base -= 0.15
    if self.is_stopword:
        base -= 0.3

    tier_center = {"EASY": 0.15, "MEDIUM": 0.45, "HARD": 0.80}
    distance = abs(self.difficulty_score - tier_center[difficulty])
    affinity = max(0.0, 1.0 - distance * 2.5)

    # Blend: 60% base quality, 40% difficulty affinity
    return max(0.0, base * 0.6 + affinity * 0.4)
```

Effect:
- EASY mode: common words score highest, rare words still usable but ranked last
- HARD mode: rare/archaic words get a scoring boost, common words are deprioritized but available
- MEDIUM mode: balanced, slight preference for mid-range words

#### Where scoring affects behavior

1. **`find_candidates()`**: candidates are sorted by `score()` — top-ranked words are tried first by the CP-SAT solver's table constraints and by theme placement
2. **`max_entries_per_length`**: if set, keeps the top-N by score — difficulty shifts which words survive this cut
3. **CP-SAT solver**: when multiple valid solutions exist, the solver picks from the candidate pool which is already ordered by difficulty-aware scoring

No words are ever excluded from the dictionary. The solver can always fall back to any valid word to complete the grid.

## Difficulty-Aware Theme Words

Difficulty affects **both** fill words (via dictionary scoring) **and** theme words (via LLM prompt + dummy buckets).

### Gemini prompt changes (`GeminiThemeWordGenerator`)

The current prompt in `crossword/data/theme.py` (lines 57-64) is Romanian-specific. Replace with a language-parameterized English prompt:

```python
THEME_BASE_PROMPT = (
    "You are assisting with a {language} cryptic crossword. "
    "Generate between 50 and {limit} JSON lines describing unique theme words. "
    "Theme: '{theme}'. Each JSON line must contain fields: word, clue. "
    "The clue must be 3-5 words in {language}, cryptic-friendly. "
    "Output no more than {limit} entries."
)
```

Updated prompt — inject difficulty-specific and language-parameterized instructions:

```python
THEME_DIFFICULTY_PROMPT = {
    "EASY": (
        "Target audience: beginners and casual players. "
        "Use only well-known, common {language} words that most people would recognize. "
        "Clues should be straightforward definitions or simple wordplay, 3-5 words each. "
        "Avoid obscure, archaic, or highly technical words."
    ),
    "MEDIUM": (
        "Target audience: regular crossword solvers. "
        "Use a mix of common and moderately challenging {language} words. "
        "Clues should use cryptic conventions (anagrams, double meanings, hidden words), 3-5 words each."
    ),
    "HARD": (
        "Target audience: expert crossword solvers. "
        "Prefer rare, literary, archaic, or domain-specific {language} words that would challenge advanced players. "
        "Clues should use advanced cryptic techniques (complex anagrams, misdirection, obscure references), 3-6 words each."
    ),
}
```

The `generate()` method receives `difficulty` and `language` (default `"Romanian"`) and includes the matching prompt segment with `{language}` substituted.

### Gemini clue generation (`GeminiClueGenerator` in `clues.py`)

The current prompt (`_render_prompt`) is minimal and has no rules enforcement. The updated prompt must:

1. **Always enforce cryptic crossword rules** — these are invariant across all difficulty levels
2. **Modulate complexity** — which techniques and vocabulary the clue uses

#### Base rules prompt (constant, all difficulties)

All prompts are written in English for multi-language support. The target language is a parameter (`language`, default `"Romanian"`).

```python
CLUE_RULES = (
    "You are an expert cryptic crossword clue writer. "
    "Write all clues in {language}. "
    "Mandatory rules for EVERY clue:\n"
    "1. Each clue must contain exactly one DEFINITION (synonym or periphrasis of the answer) "
    "and one CRYPTIC MECHANISM (anagram, hidden word, container, reversal, etc.).\n"
    "2. The definition must be at the beginning or end of the clue, never in the middle.\n"
    "3. The cryptic mechanism must produce exactly the letters of the solution word.\n"
    "4. The clue must read naturally as a phrase or sentence in {language}.\n"
    "5. Do NOT include the solution word (or obvious fragments of it) in the clue.\n"
    "6. The clue must be between 3 and 8 words.\n"
    "Respond as a JSON list [{slot_id, clue}]."
)
```

#### Difficulty-specific instructions (appended to base rules)

```python
CLUE_DIFFICULTY_PROMPT = {
    "EASY": (
        "Difficulty: EASY. "
        "Use only simple mechanisms: double definitions, hidden words, or obvious anagrams. "
        "Cryptic indicators must be transparent (e.g., 'mixed', 'scrambled' for anagrams). "
        "Use everyday vocabulary in the clue. "
        "The definition must be a clear, direct synonym of the solution."
    ),
    "MEDIUM": (
        "Difficulty: MEDIUM. "
        "Use varied mechanisms: anagrams, containers, reversals, hidden words. "
        "Cryptic indicators can be subtler but still recognizable. "
        "The definition may be a periphrasis, not just a direct synonym."
    ),
    "HARD": (
        "Difficulty: HARD. "
        "Use advanced mechanisms: complex anagrams, multiple containers, "
        "&lit. clues, homophones, combined reversals. "
        "Cryptic indicators should be subtle and double-meaning. "
        "The definition may be a misleading periphrasis or indirect reference. "
        "The clue should mislead on first reading but be fair upon analysis."
    ),
}
```

#### Updated `_render_prompt()`

```python
@staticmethod
def _render_prompt(
    requests: List[ClueRequest],
    difficulty: str = "MEDIUM",
    language: str = "Romanian",
) -> str:
    payload = [request.__dict__ for request in requests]
    rules = CLUE_RULES.format(language=language)
    diff_prompt = CLUE_DIFFICULTY_PROMPT.get(difficulty, CLUE_DIFFICULTY_PROMPT["MEDIUM"])
    return (
        f"{rules}\n"
        f"{diff_prompt}\n"
        f"Requests: {json.dumps(payload, ensure_ascii=False)}"
    )
```

#### How difficulty scales within the rules

| Rule | EASY | MEDIUM | HARD |
|------|------|--------|------|
| Definition present | Always (rule 1) | Always (rule 1) | Always (rule 1) |
| Definition clarity | Direct synonym | Periphrasis OK | Misleading periphrasis |
| Cryptic mechanism | 1 simple type | 1-2 varied types | 2+ combined types |
| Cryptic indicator | Obvious keyword | Subtle but clear | Double-meaning words |
| Surface reading | Transparent | Smooth misdirection | Deceptive at first glance |
| Vocabulary in clue | Everyday words | General vocabulary | Literary/rare vocabulary |
| Allowed techniques | Hidden word, double def, simple anagram | + container, reversal, deletion | + &lit, homophone, compound |

### DummyThemeWordGenerator changes

The dummy buckets (`THEME_BUCKETS`) should be split by difficulty or tagged:

```python
THEME_BUCKETS = {
    "mitologie": {
        "EASY":   [("ZEUS", "Zeul suprem"), ("ARES", "Zeul războiului"), ...],
        "MEDIUM": [("HERMES", "Mesagerul zeilor"), ("PROMETEU", "Titanul focului"), ...],
        "HARD":   [("ERINII", "Zeițele răzbunării"), ("MNEMOSYNE", "Titanida memoriei"), ...],
    },
    ...
}
```

The `generate()` method receives `difficulty` and picks from the matching tier first, then falls back to other tiers if needed (same soft-preference approach — never hard-exclude).

### Interface changes

`ThemeWordGenerator` protocol and `merge_theme_generators()` gain a `difficulty` parameter:

```python
class ThemeWordGenerator(Protocol):
    def generate(self, theme: str, limit: int = 80, difficulty: str = "MEDIUM") -> List[ThemeWord]:
        ...

def merge_theme_generators(
    primary, fallbacks, theme, target, difficulty="MEDIUM"
) -> List[ThemeWord]:
    ...
```

Called from `_seed_theme_words()` which reads `self.config.difficulty`.

## Code Changes

### 1. `local_db/dex_query.sql`

Update query as shown above (add `source_short_name`, `tags`, `definition_count`, `source_count`).

### 2. `crossword/core/constants.py`

- Add `Difficulty` enum: `EASY`, `MEDIUM`, `HARD`

### 3. `crossword/data/preprocess.py`

- Add fields to `ProcessedWordRecord`: `source_name`, `tags`, `definition_count`, `source_count`, `difficulty_score`
- Add `FIELDNAMES` entries
- Compute `difficulty_score` during preprocessing (in `preprocess_dictionary()`)
- Update `write_processed_dictionary()` and `load_processed_dictionary()` accordingly

### 4. `crossword/data/dictionary.py`

- Add `difficulty_score` to `WordEntry`
- Add `difficulty: Difficulty = Difficulty.MEDIUM` to `DictionaryConfig`
- Update `WordEntry.score()` to accept difficulty and apply affinity bonus (as above)
- Pass difficulty through to all `score()` call sites: `find_candidates()`, `theme_candidates()`, `_hydrate_entries()` sorting

### 5. `crossword/data/theme.py`

- Add `DIFFICULTY_PROMPT` dict with per-tier Gemini instructions
- Update `GeminiThemeWordGenerator.generate()` to accept and use `difficulty`
- Restructure `DummyThemeWordGenerator` buckets by difficulty tier with soft fallback
- Update `ThemeWordGenerator` protocol signature
- Update `merge_theme_generators()` to pass `difficulty` through

### 6. `crossword/io/clues.py`

- Add `CLUE_DIFFICULTY_PROMPT` dict
- Update `GeminiClueGenerator.generate()` to accept and use `difficulty`
- Update `ClueGenerator` protocol if needed

### 7. `crossword/engine/generator.py` — `GeneratorConfig`

- Add `difficulty: str = "MEDIUM"` field
- Add `language: str = "Romanian"` field
- Pass both through to `DictionaryConfig`, theme generators, and clue generator

### 8. `main.py`

- Add `--difficulty` CLI argument with choices `[EASY, MEDIUM, HARD]`
- Add `--language` CLI argument (default `"Romanian"`)

### 9. `debug_main.py`

- Add `difficulty` and `language` to `optional_fields`

## Migration / Backwards Compatibility

- Re-export of `dex_words.tsv` required (re-run the SQL query against dexonline DB)
- The processed cache (`dex_words_processed.tsv`) will be regenerated automatically since it adds new columns
- Old TSV files without the new columns will still load (missing fields default to 0/empty), but difficulty scoring will be degraded (falls back to frequency-only)

## Verification Plan

1. Run updated SQL query, inspect distribution of `source_short_name`, `tags`, `definition_count`, `source_count`
2. Compute `difficulty_score` histogram — verify reasonable spread across EASY/MEDIUM/HARD buckets
3. Spot-check: common words (casa, apa, mare, om) should score EASY; rare words (abjudeca, etc.) should score HARD
4. Generate crosswords at each difficulty level and verify word familiarity
5. Ensure EASY mode still has enough candidates per word length (especially 3-letter words, the bottleneck)
6. Test Gemini theme word generation at each difficulty — verify EASY produces familiar words, HARD produces obscure ones
7. Test Gemini clue generation — verify clue complexity scales with difficulty

## Open Questions

- Should EASY mode also prefer shorter clue definitions (simpler language)?
- Tag hierarchy: some tags are nested (e.g., `regional > Banat`). Use parent tag only, or track both?
