# agents.md

This project builds an advanced **Romanian cryptic barred crossword generator**.
Agents collaborating on this codebase must respect the architectural principles,
constraints, and terminology described below.

---

## 🧠 Project Overview

The crossword generator does **NOT** rely on pre-generated templates or ML-trained layouts. 
Instead, it uses a **constructive growth algorithm**:

1. Start from an empty `HxW` grid
2. Optionally place a rectangular `BLOCKER_ZONE`
3. Seed a small number of **theme words**, each licensed by an adjacent `CLUE_BOX`
4. Dynamically grow the crossword outward by:
   - creating new word slots intersecting with the existing theme words in the grid
   - if a new word slot does not have an existing adjacent `CLUE_BOX` it must be created
5. Finalize by validating the crossword and ensuring all the rules in the sections below are respected

This guarantees:
- every word has a clue box
- strong interlock
- validity-first generation

---

## 🧩 Core Cell Types (authoritative)

Agents must use **only** these cell types and semantics:

| Type | Meaning                                                                          |
|----|----------------------------------------------------------------------------------|
| `EMPTY_PLAYABLE` | Empty cell, not yet committed to any word                                        |
| `LETTER` | Cell containing a letter belonging to one or two words (if intersected)          |
| `CLUE_BOX` | Blocked cell hosting clues for one or multiple words (adjacent with word starts) |
| `BLOCKER_ZONE` | Non-playable area (decorative / image / padding)                                 |

---

## 📏 Crossword Rules (non-negotiable)

Agents must not violate these rules:

1. **Grid structure**
   - The crossword size (`HxW`) is sent as parameter and must be respected
   - The blocker zone will have a random width and height size, where `h` and `w` can be between 3 and 6 cells but no more than half of the grid size (`H/2 x W/2`)
   - The top left corner will always be a `CLUE_BOX`
   - If a blocker zone occupies the extreme top-left corner (size `h×w`), automatically plant clue boxes at `(0, w)` and `(h, 0)` (the top left corners of empty area).
   - No two clue boxes may be orthogonally adjacent (diagonal contact is fine).
   - The bottom right 2x2 corner area could not possibly contain a `CLUE_BOX`
   
2. **Word Length**
   - The word slots that require an adjacent `CLUE_BOX` should have a minimum length of two cells
   - Boundaries:
     - Words can **start** from the left or top grid margin, from a `CLUE_BOX` or a `BLOCKER_ZONE`
     - Words **stop** when they reach the left or top grid margin, a `CLUE_BOX` or `BLOCKER_ZONE`
   - It's possible to have one letter slots if that letter is part of another valid word
   - The filling algorithm doesn't have to fill the exact space between two existing boundaries; it could try with shorter matching words and place `CLUE_BOX` cells at the necessary boundaries, if all the other rules are respected

3. **Clue Licensing Rule**
   - Every word start (first cell of the word slot) must be adjacent to a `CLUE_BOX`
   - Allowed adjacency:
     - word going ACROSS: left / above / below (❌ not right)
     - word going DOWN: above / left / right (❌ not bottom)
   - Every clue box must license at least one word start (across/down) of length ≥2.
   - All across and down letter sequences of length ≥2 must have an adjacent `CLUE_BOX` to the first letter cell.

4. **Validity**
   - All rules above +
   - All letters are alphabetical A-Z
   - All across and down letter sequences of length ≥3 must be valid Romanian words from the theme list or the existing dataset
   - Two letter slots do not have to be valid words
   - One letter slots don't need validation from all directions if they're part of another valid word
   - NO duplicate words are allowed in the grid, even the two letter slots should be distinct
   - NO clue boxes in the 2x2 bottom right corner area of the grid because it will end up with word slots of less than 2 characters

---

## 🧱 Architecture Summary

### Algorithm Type

- research various approaches that would build the most accurate and efficient crossword generator
- can use backtracking, OR-TOOLS or any other performant algorithm to implement the following pseudocode:

```pseudocode
grid = init_grid(H,W)
grid = place_random_blocker_zone(grid)
theme_words = generate_theme_words(theme)
grid = place_random_theme_words(grid, theme_words)
grid = fill_crossword(grid)

def fill_crossword(grid):
    for word_slot in grid.already_placed_words:
        for cell in grid.cells_around_word(word_slot):
            if cell = 'EMPTY_PLAYABLE':
                try:
                    grid.place_new_random_word_slot(cell) // places a random length word to intersect with the current word_slot in the loop and respct all the rules
                except:
                    continue
            if cell = 'BLOCKER_ZONE' or 'CLUE_BOX':
                continue
    // loop until the entire grid is filled with word slots and clue boxes
```

#### Implementation steps

**I. Theme words generation and dictionary preprocessing:**
1. Firstly, the system will generate using Gemini a list of 50-100 words related to inputed theme along with their clues.
   * The words should have various lengths, so we can find candidates when the filling happens
2. I created the local_db/dex_words.tsv file with a database of words having this header: 'entry_word', 'entry_length', 'definition', 'definition_length', 'lemma', 'lexeme_frequency', 'is_compound', 'inflection_description', 'is_stopword'. 
   * The preprocessing step should eliminate the diacritics in entry_word and keep their normal form (e.g. ăâîșț = aaist)
   * In the preprocessing stage, I want to group by cleaned entry_word to have only one row per word. for now, I don't care about the local definition because when the crossword will be generated LLM will pick up this task

**II. Empty grid generation:**
1. **Grid Cell Representation:** How to structure a JSON object for each cell on the canvas, storing:
   * type: ('LETTER', 'CLUE_BOX', ‘BLOCKER_ZONE’, ’EMPTY_PLAYABLE' (initial)).
   * letter: (char, if type = LETTER).
   * clues_hosted: (list of clue objects, if type = CLUE_BOX. Clue object: {'id', 'text', 'solution_word_ref_id', 'solution_length', 'direction', 'start_offset_r', 'start_offset_c'}). start_offset_r/c indicates solution start relative to the clue box.
   * part_of_word_ids: (set of unique IDs of words this letter cell belongs to)
2. **Blocker zone:** 
   * rectangular 3–6 cell “BLOCKER_ZONE” placed randomly in a corner or center, blocking both words and clues.
   * if a blocker zone occupies the extreme top-left corner (size `h×w`), automatically plant clue boxes at `(0, w)` and `(h, 0)` (the top left corners of empty area).

**II. Main Algorithm:**
1. The grid is initially filled with theme words and their adjacent clue cells at random places and orientations
   * We’ll limit the theme words in the crossword at minimum 2 and maximum 40% of the total number of words in the grid
   * If there is no fit, we’ll go back and request a new set of theme words.
2. Starting from the placed theme words we’ll have to fill in word slots and clue cells randomly so it will be fully interlocked and valid
   * The entire crossword will be filled with words existing in the local word database preprocessed.
   * We’ll add a completion parameter for debugging purposes. We can stop the algorithm at 80 percent completion, for example.
   * If the generation can't be completed, we'll try with another random word fillings from the beginning of this step.
3. After every step and every placed word, the Crossword should be validated so all the rules are respected
   * If, for example, after the theme words seeding step, there are letter combinations that are impossible for a valid word, the algorith should go one step back and try another variation. 
   * Also, the validation should check the correct existence of each `CLUE_BOX`
   * All these checks should be made at every step by querying the dex_words data to check for existing words with the verified pattern.

**III. LLM Clue generation:**
1. After CP-SAT fill, words are routed to one of two paths based on origin:
   - **Gemini theme words**: clue/long_clue/hint from the theme generation phase are used directly — no LLM call.
   - **All other words** (fill words, user words without a clue, substring/dummy theme words): sent to `GeminiClueGenerator` for full generation of `main_clue`, `hint_1`, and `hint_2`.
   - **User words with an explicit clue** (`WORD:clue` syntax): the user-supplied text is used as `main_clue` unchanged; the LLM generates only `hint_1` and `hint_2`.
2. Fill word requests include the word’s DEX definition as reference context so the LLM can produce informed clues for uncommon words.
3. All clue text must use no punctuation except ellipsis (`...`) for intentional ambiguity or exclamation mark (`!`) for the short-word riddle style.
4. Generated clues are applied to the clue cell objects via `attach_clues_to_grid`.

**IV. General Advice:**
1. Provide meaningful logging and debugging messages at each step for better initial development.
2. Respect the most important coding principles.
3. Build the most efficient solutions for the entire system and all its components.

---

## 🤖 Agent Roles

### 🧩 Grid / Generator Agent
Responsibilities:
- Improve slot selection heuristics
- Reduce clue box clustering
- Improve aesthetics (edge balance, slot length distribution)
- Ensure no illegal starts or dead zones

Must NOT:
- Bypass clue licensing rules

Pretty-print of a sample valid grid with empty letter cells:
     0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15
    --------------------------------------------
 0 | X X X X X # . # . # . . . # . #
 1 | X X X X X . . . . . . # . . . .
 2 | X X X X X # . . # . . . . . . .
 3 | X X X X X . . . . # . . . . . .
 4 | X X X X X # . . . . # . . # . .
 5 | # . # . # . . # . . . . . . #
 6 | . . . . . . . . # . . # . . # .
 7 | # . . . . . # . . . . . # . . .
 8 | . . . # . . . . . # . . . . . .
 9 | # . . . . . . . . . # . . . . .
Legend:  #=CLUE_BOX  X=BLOCKER_ZONE  .=EMPTY_PLAYABLE  
A-Z=LETTER (not filled in this sample)

---

### 📚 Dictionary / Word Agent
Responsibilities:
- Improve candidate filtering
- Add frequency-based scoring
- Support abbreviations / initials / special cryptic answers

Must NOT:
- Introduce non-normalized (diacritics) forms
- Bypass minimum length rules

---

### ✍️ Clue / LLM Agent
Responsibilities:
- Generate Romanian clues (cryptic & straight) for fill words and non-Gemini theme words
- Preserve user-provided `main_clue` values exactly; generate only hints for these words
- Pass DEX definitions as reference context for uncommon fill words
- Ensure multiple clues per clue box are supported
- Validate clue–answer consistency

Constraints:
- No grid modification
- No letter changes
- Must not overwrite Gemini-generated theme clues — those bypass the LLM entirely
- No punctuation in clue text except `...` (intentional ambiguity) or `!` (riddle style)

---

### 🔍 Validation / QA Agent
Responsibilities:
- Check final crossword integrity
- Ensure all rules are satisfied
- Detect orphan clue boxes or unused slots

Should:
- Fail fast
- Be deterministic
- Never “fix” by guessing

---

## 🧪 Debugging & Logging Guidelines

Agents should:
- Log **why** a placement fails (not just that it failed)
- Prefer reversible operations (snapshot → try → rollback)
- Avoid global mutation without rollback logic

Recommended logging levels:
- `INFO`: generation milestones
- `DEBUG`: slot rejection reasons
- `WARNING`: near-failure recoveries
- `ERROR`: rule violations

---

## 🚫 Anti-Patterns (do not introduce)

- Generating full templates before filling
- Randomly placing clue boxes without purpose
- Allowing words to exist without explicit slots
- Treating contiguous letters as words implicitly
- Relying on ML models without deterministic validation

---

## 🧠 Design Philosophy

> **Validity first. Structure second. Aesthetics last.**

The crossword must always be:
1. Solvable
2. Correct
3. Interlocked
4. Only then: beautiful

Agents should optimize within that order.

---

## 📌 Final Note for Agents

If a change makes generation faster **but** risks violating crossword rules, it is not acceptable.

When unsure:
- prefer rejection over guessing