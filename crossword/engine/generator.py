"""Main crossword generator orchestration.

Two-phase approach:
  1. Layout: place blocker zone, theme words, then clue boxes to define all slots.
  2. Fill: use CP-SAT solver to fill all remaining slots.
"""

from __future__ import annotations

import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
import heapq
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..io.clues import ClueBundle, ClueGenerator, ClueRequest, TemplateClueGenerator, attach_clues_to_grid
from ..io.definition_fetcher import DefinitionFetcher, is_incomplete_definition
from ..core.constants import CellType, Difficulty, Direction, ORTHOGONAL_STEPS
from ..data.dictionary import DictionaryConfig, WordDictionary, WordEntry
from ..core.exceptions import (ClueBoxError, CrosswordError, SlotPlacementError,
                               ThemeWordError, ValidationError)
from .grid import CrosswordGrid, GridConfig, MAX_CLUES_PER_BOX
from ..utils.logger import get_logger
from ..core.models import Clue, WordSlot
from ..data.theme import (
    DummyThemeWordGenerator, SubstringThemeWordGenerator,
    ThemeOutput, ThemeType, ThemeWord, ThemeWordGenerator, UserWordListGenerator,
    merge_theme_generators,
)
from .crossword_store import CrosswordStore
from ..data.theme_cache import ThemeCache
from .validator import GridValidator


LOGGER = get_logger(__name__)


@dataclass
class GeneratorConfig:
    height: int
    width: int
    dictionary_path: Path | str
    theme_title: str = ""
    theme_type: str = "domain_specific_words"
    theme_description: str = ""
    extend_with_substring: bool = False  # words_containing_substring: extend user words from dictionary
    seed: Optional[int] = None
    completion_target: float = 0.85
    max_iterations: int = 2500
    retry_limit: int = 3
    min_theme_coverage: float = 0.10
    max_theme_ratio: float = 0.4
    theme_request_size: int = 80
    theme_placement_attempts: int = 30
    prefer_theme_candidates: bool = True
    fill_timeout_seconds: float = 180.0
    difficulty: str = "MEDIUM"
    language: str = "Romanian"
    place_blocker_zone: bool = True
    blocker_zone_height: Optional[int] = None
    blocker_zone_width: Optional[int] = None
    blocker_zone_row: Optional[int] = None
    blocker_zone_col: Optional[int] = None
    allow_adult: bool = False
    allow_multi_word: bool = False
    allow_repair: bool = False
    strict_user_words: bool = True

    def to_grid_config(self, seed_override: Optional[int] = None) -> GridConfig:
        blocker_seed = self._manual_blocker_seed()
        return GridConfig(
            height=self.height,
            width=self.width,
            place_blocker_zone=self.place_blocker_zone,
            blocker_zone_height=self.blocker_zone_height,
            blocker_zone_width=self.blocker_zone_width,
            blocker_zone_row=self.blocker_zone_row,
            blocker_zone_col=self.blocker_zone_col,
            blocker_zone_seed=blocker_seed,
            rng_seed=seed_override if seed_override is not None else self.seed,
        )

    def to_dictionary_config(self) -> DictionaryConfig:
        return DictionaryConfig(
            path=self.dictionary_path,
            rng=random.Random(self.seed),
            difficulty=Difficulty(self.difficulty),
            allow_compounds=self.allow_multi_word,
        )

    def _manual_blocker_seed(self) -> Optional[int]:
        overrides = (
            self.blocker_zone_height,
            self.blocker_zone_width,
            self.blocker_zone_row,
            self.blocker_zone_col,
        )
        if not any(value is not None for value in overrides):
            return None
        # Deterministic 32-bit mixing derived from override values and optional seed
        seed_value = 0x9E3779B1
        for value in overrides:
            component = -1 if value is None else int(value)
            seed_value = (seed_value ^ (component + 0x7F4A7C15)) & 0xFFFFFFFF
            seed_value = (seed_value * 0x45D9F3B) & 0xFFFFFFFF
        if self.seed is not None:
            seed_value = (seed_value ^ (self.seed & 0xFFFFFFFF)) & 0xFFFFFFFF
            seed_value = (seed_value * 0x45D9F3B) & 0xFFFFFFFF
        return seed_value & 0xFFFFFFFF


@dataclass
class CrosswordResult:
    grid: CrosswordGrid
    slots: List[WordSlot]
    theme_words: List[ThemeWord]
    validation_messages: List[str] = field(default_factory=list)
    seed: Optional[int] = None
    crossword_title: Optional[str] = None
    theme_content: Optional[str] = None


@dataclass
class SlotSignature:
    start_row: int
    start_col: int
    direction: Direction
    cells: List[Tuple[int, int]]

    @property
    def length(self) -> int:
        return len(self.cells)


class CrosswordGenerator:
    """High-level orchestrator: layout generation then CP-SAT filling."""

    def __init__(
        self,
        config: GeneratorConfig,
        dictionary: Optional[WordDictionary] = None,
        theme_generator: Optional[ThemeWordGenerator] = None,
        clue_generator: Optional[ClueGenerator] = None,
        theme_fallback_generators: Optional[List[ThemeWordGenerator]] = None,
        store: Optional[CrosswordStore] = None,
        theme_cache: Optional[ThemeCache] = None,
        definition_fetcher: Optional[DefinitionFetcher] = None,
    ) -> None:
        self.config = config
        self.rng = random.Random(config.seed)
        self.dictionary = dictionary or WordDictionary(config.to_dictionary_config())
        self.theme_generator = theme_generator
        self.theme_fallback_generators = (
            theme_fallback_generators
            if theme_fallback_generators is not None
            else [DummyThemeWordGenerator(seed=config.seed)]
        )
        self.clue_generator = clue_generator or TemplateClueGenerator()
        self.validator = GridValidator(self.dictionary)
        self.store = store
        self.theme_cache = theme_cache
        self.definition_fetcher = definition_fetcher
        self.used_words: Set[str] = set()
        self.remaining_theme_words: Set[str] = set()
        self.theme_word_surfaces: Set[str] = set()
        self._theme_crossword_title: Optional[str] = None
        self._theme_content: Optional[str] = None
        self._slot_counter = 0
        self._occupied_slots: Set[str] = set()
        self._slot_keys: Dict[str, str] = {}
        self._placement_history: List[str] = []
        self.pending_starts: List[Tuple[float, int, Tuple[int, int, Direction]]] = []
        self._pending_counter = 0
        self._layout_rng = random.Random(config.seed)
        # Persist last-known state across retries (not cleared by _reset_state)
        self._last_known_theme_words: Optional[List[ThemeWord]] = None
        self._last_known_grid: Optional[CrosswordGrid] = None
        self._theme_cache_ref: Optional[str] = None

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------
    # For EASY difficulty: number of full layout+theme retries using phase-1-only
    # (easy words exclusively) before allowing medium fallback in phase 2.
    # Must be < retry_limit for phase 2 to ever activate in generate().
    _EASY_PHASE1_RETRIES = 1

    def generate(self) -> CrosswordResult:
        try:
            for attempt in range(1, self.config.retry_limit + 1):
                LOGGER.info("Generation attempt %s/%s", attempt, self.config.retry_limit)
                allow_phase2 = (
                    self.config.difficulty != "EASY"
                    or attempt > self._EASY_PHASE1_RETRIES
                )
                try:
                    grid_seed = self.rng.randint(0, 1_000_000)
                    grid = CrosswordGrid(self.config.to_grid_config(seed_override=grid_seed))
                    self._last_known_grid = grid
                    self._anneal_layout(grid)
                    theme_words = self._seed_theme_words(grid)
                    self._complete_layout(grid)
                    self._cpsat_fill(grid, allow_phase2=allow_phase2)
                    theme_word_surfaces = self.theme_word_surfaces.copy()
                    validation = self.validator.validate(grid, theme_word_surfaces)
                    if not validation.ok:
                        raise ValidationError(
                            f"Grid validation failed: {validation.messages}"
                        )
                    slots = list(grid.word_slots.values())

                    # Save a filled checkpoint before clue generation — if
                    # definition fetching or clue generation fails, the
                    # expensive fill work can be resumed later.
                    filled_doc_id: Optional[str] = None
                    if self.store:
                        filled_doc_id = self.store.save_filled(
                            grid, self.config, slots,
                            theme_words=theme_words,
                            crossword_title=self._theme_crossword_title,
                            theme_content=self._theme_content,
                            dictionary=self.dictionary,
                            theme_cache_ref=self._theme_cache_ref,
                        )

                    # Build theme word lookup: sanitized surface → ThemeWord
                    theme_word_map = {
                        self.dictionary.sanitize(tw.word): tw for tw in theme_words
                    }

                    # Collect words missing or with truncated local definitions for batch lookup.
                    # Skip 2-letter words — they use free-form letter combos and have no DEX entry.
                    words_needing_def = [
                        slot.text for slot in slots
                        if slot.text and len(slot.text) >= 3 and (
                            (e := self.dictionary.get(slot.text)) is None
                            or is_incomplete_definition(e.definition or "")
                        )
                    ]
                    fetched_defs: Dict[str, str] = {}
                    if words_needing_def and self.definition_fetcher:
                        LOGGER.info(
                            "Fetching definitions for %d word(s) via DEX: %s",
                            len(words_needing_def),
                            ", ".join(words_needing_def),
                        )
                        fetched_defs = self.definition_fetcher.fetch_batch(words_needing_def)

                    # Categorize slots: fill words go to LLM, theme words use existing clues
                    clue_requests: List[ClueRequest] = []
                    theme_bundles: Dict[str, ClueBundle] = {}

                    for slot in slots:
                        word = slot.text or ""
                        if not slot.is_theme:
                            entry = self.dictionary.get(word)
                            clue_requests.append(ClueRequest(
                                slot_id=slot.id,
                                word=word,
                                direction=slot.direction.value,
                                clue_box=slot.clue_box,
                                definition=fetched_defs.get(word) or (entry.definition if entry else None),
                            ))
                        else:
                            tw = theme_word_map.get(word)
                            if tw is None:
                                # Fallback: send to LLM for full generation
                                clue_requests.append(ClueRequest(
                                    slot_id=slot.id,
                                    word=word,
                                    direction=slot.direction.value,
                                    clue_box=slot.clue_box,
                                ))
                            elif tw.source == "gemini":
                                # Gemini generated all three clue fields — use directly
                                theme_bundles[slot.id] = ClueBundle(
                                    main_clue=tw.clue,
                                    hint_1=tw.long_clue or "",
                                    hint_2=tw.hint or "",
                                )
                            elif tw.has_user_clue:
                                # User provided explicit main clue — LLM generates hints only
                                entry = self.dictionary.get(word)
                                clue_requests.append(ClueRequest(
                                    slot_id=slot.id,
                                    word=word,
                                    direction=slot.direction.value,
                                    clue_box=slot.clue_box,
                                    definition=fetched_defs.get(word) or (entry.definition if entry else None),
                                    preset_main_clue=tw.clue,
                                ))
                            else:
                                # User word without clue, substring, or dummy — LLM generates all
                                entry = self.dictionary.get(word)
                                clue_requests.append(ClueRequest(
                                    slot_id=slot.id,
                                    word=word,
                                    direction=slot.direction.value,
                                    clue_box=slot.clue_box,
                                    definition=fetched_defs.get(word) or (entry.definition if entry else None),
                                ))

                    # Detect sibling entry pairs: slots sharing the same (row, col) start
                    start_pos_map: Dict[Tuple[int, int], List[WordSlot]] = {}
                    for slot in slots:
                        start_pos_map.setdefault((slot.start_row, slot.start_col), []).append(slot)

                    slot_id_to_slot: Dict[str, WordSlot] = {slot.id: slot for slot in slots}
                    for req in clue_requests:
                        slot = slot_id_to_slot.get(req.slot_id)
                        if slot is None:
                            continue
                        siblings = start_pos_map.get((slot.start_row, slot.start_col), [])
                        sibling = next((s for s in siblings if s.id != slot.id and s.text), None)
                        if sibling:
                            req.sibling_word = sibling.text

                    clue_texts = self.clue_generator.generate(
                        clue_requests, difficulty=self.config.difficulty,
                        language=self.config.language,
                        theme=self.config.theme_title or "",
                        allow_adult=self.config.allow_adult,
                    )
                    attach_clues_to_grid(grid, slots, {**clue_texts, **theme_bundles})
                    LOGGER.info("Crossword generation completed with %s words", len(slots))
                    result = CrosswordResult(
                        grid=grid,
                        slots=slots,
                        theme_words=theme_words,
                        validation_messages=validation.messages,
                        seed=grid_seed,
                        crossword_title=self._theme_crossword_title,
                        theme_content=self._theme_content,
                    )
                    if self.store:
                        self.store.save_success(
                            result, self.config,
                            dictionary=self.dictionary,
                            theme_cache_ref=self._theme_cache_ref,
                        )
                    return result
                except CrosswordError as exc:
                    LOGGER.warning("Generation attempt failed: %s", exc)
                    self._reset_state()
                    continue
            raise CrosswordError("Unable to generate crossword after retries")
        except CrosswordError as final_exc:
            if self.store:
                self.store.save_failure(
                    self.config, str(final_exc),
                    grid=self._last_known_grid,
                    theme_words=self._last_known_theme_words,
                    crossword_title=self._theme_crossword_title,
                    theme_content=self._theme_content,
                    dictionary=self.dictionary,
                    theme_cache_ref=self._theme_cache_ref,
                )
            raise

    # ------------------------------------------------------------------
    # Theme seeding
    # ------------------------------------------------------------------
    def _seed_theme_words(self, grid: CrosswordGrid) -> List[ThemeWord]:
        LOGGER.info("Preparing theme words for '%s'", self.config.theme_title)

        playable_cells = grid._playable_count
        min_theme_letters = max(1, int(playable_cells * self.config.min_theme_coverage))
        letter_budget = max(min_theme_letters, int(playable_cells * self.config.max_theme_ratio))

        # Auto-scale request size for larger grids
        estimated_words_needed = (min_theme_letters // 5) + 2
        target = max(self.config.theme_request_size, estimated_words_needed * 3)

        primary = self.theme_generator
        fallbacks = list(self.theme_fallback_generators)
        if self.config.theme_type in (ThemeType.WORDS_CONTAINING_SUBSTRING, "words_containing_substring"):
            substring_gen = SubstringThemeWordGenerator(self.dictionary, self.config.theme_title)
            if primary is None:
                # llm=True, no user words: dictionary is the sole source
                primary = substring_gen
                fallbacks = []
            elif self.config.extend_with_substring:
                # llm=True, user words provided: user words first, dictionary extends
                fallbacks = [substring_gen]
            else:
                # llm=False, user words provided: user words only
                fallbacks = []

        theme_output: ThemeOutput = merge_theme_generators(
            primary, fallbacks, self.config.theme_title, target,
            difficulty=self.config.difficulty, language=self.config.language,
        )
        # Capture metadata from LLM for downstream use
        self._theme_crossword_title = theme_output.crossword_title
        self._theme_content = theme_output.content
        if self.theme_cache is not None:
            self._theme_cache_ref = self.theme_cache.cache_id(
                theme_title=self.config.theme_title,
                difficulty=self.config.difficulty,
                language=self.config.language,
                theme_description=self.config.theme_description,
                theme_type=self.config.theme_type,
            )

        # For joke_continuation with user words + description but no LLM: use description as content
        if (
            self.config.theme_type in (ThemeType.JOKE_CONTINUATION, "joke_continuation")
            and self._theme_content is None
            and self.config.theme_description
        ):
            self._theme_content = self.config.theme_description

        theme_words = theme_output.words
        if not theme_words:
            raise ThemeWordError("No theme words available")

        letters_used = 0
        placed: List[ThemeWord] = []
        for theme_entry in theme_words:
            cleaned = self.dictionary.sanitize(theme_entry.word)
            if len(cleaned) < 2:
                continue
            if cleaned in self.used_words:
                continue
            if letters_used >= letter_budget:
                LOGGER.info(
                    "Reached theme letter budget (%d letters, %.0f%% of playable)",
                    letters_used,
                    (letters_used / playable_cells) * 100 if playable_cells else 0,
                )
                break
            # User words use soft crossing thresholds (>=1 candidate) by default
            # to avoid rejecting placements too aggressively. When strict_user_words
            # is False, user words get the same graduated thresholds as LLM words.
            if theme_entry.source == "user" and self.config.strict_user_words:
                override = 1
            else:
                override = None
            if not self._attempt_place_specific_word(
                grid, cleaned, theme_entry, is_theme=True,
                min_candidates_override=override,
            ):
                continue
            placed.append(theme_entry)
            letters_used += len(cleaned)

        # Skip coverage enforcement when the user explicitly provided words
        # (LLM / Dummy fallbacks are supplemental — if they fail, proceed with user words)
        has_user_primary = isinstance(self.theme_generator, UserWordListGenerator)
        if letters_used < min_theme_letters and self.theme_fallback_generators and not has_user_primary:
            raise ThemeWordError(
                f"Insufficient theme coverage: {letters_used}/{min_theme_letters} letters "
                f"({letters_used / playable_cells * 100:.0f}% vs "
                f"{self.config.min_theme_coverage * 100:.0f}% target)"
            )
        if letters_used < min_theme_letters:
            LOGGER.info(
                "Words-only mode: skipping coverage check (%d/%d letters placed)",
                letters_used, min_theme_letters,
            )

        self._last_known_theme_words = placed

        self.remaining_theme_words = {
            self.dictionary.sanitize(entry.word) for entry in theme_words if entry not in placed
        }
        LOGGER.info(
            "Placed %s theme words (%d letters, %.0f%% of %d playable)",
            len(placed), letters_used,
            (letters_used / playable_cells) * 100 if playable_cells else 0,
            playable_cells,
        )
        return placed

    def _attempt_place_specific_word(
        self,
        grid: CrosswordGrid,
        word: str,
        theme_entry: Optional[ThemeWord] = None,
        is_theme: bool = False,
        min_candidates_override: Optional[int] = None,
    ) -> bool:
        """Try to place *word* on the grid, preferring high-scoring positions.

        For theme words, all valid positions are scored by crossing viability
        and the top candidates are tried in order. For non-theme words, pending
        starts are attempted first.

        Args:
            min_candidates_override: When set, crossing validation uses this
                as the minimum candidate count for all crossing lengths instead
                of the graduated defaults.
        """
        # Pending starts chain words together — useful for fill but counter-productive
        # for theme seeding where words should be spread across the grid.
        if not is_theme:
            if self._attempt_pending_start(grid, word, theme_entry, is_theme,
                                           min_candidates_override=min_candidates_override):
                return True

        if is_theme:
            return self._scored_theme_placement(
                grid, word, theme_entry, min_candidates_override,
            )

        for _ in range(self.config.theme_placement_attempts):
            direction = self.rng.choice([Direction.ACROSS, Direction.DOWN])
            start_positions = self._candidate_starts(grid, len(word), direction)
            if not start_positions:
                continue
            start_row, start_col = self.rng.choice(start_positions)
            if self._place_word_at(
                grid, word, theme_entry, is_theme, direction, start_row, start_col,
                min_candidates_override=min_candidates_override,
            ):
                return True
        return False

    def _scored_theme_placement(
        self,
        grid: CrosswordGrid,
        word: str,
        theme_entry: Optional[ThemeWord],
        min_candidates_override: Optional[int],
    ) -> bool:
        """Score all valid placements and try the best ones first.

        Collects (direction, start_row, start_col) candidates for both
        directions, scores each by perpendicular crossing viability, sorts
        descending, and attempts the top positions.
        """
        scored_positions: List[Tuple[float, int, int, int, Direction]] = []
        counter = 0
        for direction in (Direction.ACROSS, Direction.DOWN):
            start_positions = self._candidate_starts(grid, len(word), direction)
            for start_row, start_col in start_positions:
                if not self._can_place_word(grid, start_row, start_col, direction, word):
                    continue
                score = self._score_placement(grid, word, direction, start_row, start_col)
                if score == float("-inf"):
                    continue
                scored_positions.append((score, counter, start_row, start_col, direction))
                counter += 1

        if not scored_positions:
            return False

        # Sort descending by score; tie-break by counter for determinism
        scored_positions.sort(key=lambda t: (-t[0], t[1]))

        top_n = min(10, len(scored_positions))
        for score, _, start_row, start_col, direction in scored_positions[:top_n]:
            LOGGER.debug(
                "Trying theme placement for %s at (%d,%d) %s — score=%.2f",
                word, start_row, start_col, direction.value, score,
            )
            if self._place_word_at(
                grid, word, theme_entry, True, direction, start_row, start_col,
                min_candidates_override=min_candidates_override,
            ):
                return True
        return False

    def _score_placement(
        self,
        grid: CrosswordGrid,
        word: str,
        direction: Direction,
        start_row: int,
        start_col: int,
    ) -> float:
        """Score a candidate placement by perpendicular crossing viability.

        For each cell of the word, builds the perpendicular crossing signature
        and counts dictionary candidates. Returns sum(log(count + 1)) across
        crossings, or -inf if any crossing has zero candidates.
        """
        dr, dc = self._step(direction)
        cross_dir = Direction.DOWN if direction == Direction.ACROSS else Direction.ACROSS
        total_score = 0.0

        for i, letter in enumerate(word):
            row = start_row + dr * i
            col = start_col + dc * i

            existing = grid.cell(row, col).letter
            if existing == letter:
                # Cell already has the right letter — no new crossing constraint
                continue

            sig = self._build_signature(grid, row, col, cross_dir)
            if sig is None or sig.length < 2:
                continue

            # Build the pattern for this crossing with the proposed letter placed
            pattern: List[Optional[str]] = []
            for cr, cc in sig.cells:
                if (cr, cc) == (row, col):
                    pattern.append(letter)
                else:
                    cell_letter = grid.cell(cr, cc).letter
                    pattern.append(cell_letter if cell_letter else None)

            count = self.dictionary.count_candidates(
                sig.length, pattern, banned=self.used_words,
            )
            if count == 0:
                return float("-inf")
            total_score += math.log(count + 1)

        return total_score

    def _attempt_pending_start(
        self,
        grid: CrosswordGrid,
        word: str,
        theme_entry: Optional[ThemeWord],
        is_theme: bool,
        min_candidates_override: Optional[int] = None,
    ) -> bool:
        while self.pending_starts:
            _, _, (row, col, direction) = heapq.heappop(self.pending_starts)
            if self._place_word_at(grid, word, theme_entry, is_theme, direction, row, col,
                                   min_candidates_override=min_candidates_override):
                return True
        return False

    def _place_word_at(
        self,
        grid: CrosswordGrid,
        word: str,
        theme_entry: Optional[ThemeWord],
        is_theme: bool,
        direction: Direction,
        start_row: int,
        start_col: int,
        min_candidates_override: Optional[int] = None,
    ) -> bool:
        if not self._can_place_word(grid, start_row, start_col, direction, word):
            return False
        try:
            clue_box = grid.ensure_clue_box(start_row, start_col, direction)
        except ClueBoxError as exc:
            LOGGER.debug("Unable to license start via clue: %s", exc)
            return False

        slot = WordSlot(
            id=self._next_slot_id(direction),
            start_row=start_row,
            start_col=start_col,
            direction=direction,
            length=len(word),
            clue_box=clue_box,
            is_theme=is_theme,
            word_breaks=theme_entry.word_breaks if theme_entry else (),
        )

        try:
            grid.place_word(slot, word)
            self._validate_crossings(grid, slot, min_candidates_override=min_candidates_override)
            extension = grid.ensure_terminal_boundary(slot)
        except (ClueBoxError, SlotPlacementError) as exc:
            if slot.id in grid.word_slots:
                grid.remove_word(slot.id)
            LOGGER.debug("Slot rejected during placement: %s", exc)
            return False

        self._register_slot(slot)
        self.used_words.add(word)
        if extension:
            self._queue_start(grid, *extension)
        if theme_entry:
            self._attach_theme_clue(grid, slot, theme_entry)
            self.theme_word_surfaces.add(self.dictionary.sanitize(theme_entry.word))
        return True

    def _attach_theme_clue(self, grid: CrosswordGrid, slot: WordSlot, theme_entry: ThemeWord) -> None:
        clue_cell = grid.cell(*slot.clue_box)
        clue_cell.clues_hosted.append(
            Clue(
                id=f"{slot.id}-theme",
                text=theme_entry.clue,
                solution_word_ref_id=slot.id,
                solution_length=slot.length,
                direction=slot.direction,
                start_offset_r=slot.start_row - slot.clue_box[0],
                start_offset_c=slot.start_col - slot.clue_box[1],
            )
        )

    # ------------------------------------------------------------------
    # Layout completion: place clue boxes to create fillable slot structure
    # ------------------------------------------------------------------
    def _complete_layout(self, grid: CrosswordGrid) -> None:
        """Place clue boxes to create a valid, fillable slot structure."""
        self._heal_isolated_cells(grid)

        # Partition long runs in two passes: first coarse, then fine.
        # Target slot length 4-8 for best dictionary coverage.
        for max_len in (10, 8):
            for _ in range(30):  # safety limit
                changed = self._partition_long_runs(grid, max_len)
                if changed:
                    self._heal_isolated_cells(grid)
                else:
                    break

        # Ensure every slot has clue licensing
        self._ensure_all_licensed(grid)

        # Repair orphan clue boxes
        self._repair_orphan_clues(grid)

        # Verify feasibility of all slots
        self._verify_feasibility(grid)

    def _partition_long_runs(self, grid: CrosswordGrid, max_len: int) -> bool:
        """Scan all rows/columns for runs > max_len and insert clue boxes."""
        changed = False
        for direction in (Direction.ACROSS, Direction.DOWN):
            for r in range(grid.bounds.rows):
                for c in range(grid.bounds.cols):
                    cell = grid.cell(r, c)
                    if not cell.is_playable():
                        continue
                    if not grid.is_boundary(r, c, direction):
                        continue
                    sig = self._build_signature(grid, r, c, direction)
                    if not sig or sig.length <= max_len:
                        continue
                    # Only partition if there are unfilled cells in this span
                    pattern = self._signature_pattern(grid, sig)
                    if all(pattern):
                        continue

                    mid = sig.length // 2

                    def _partition_score(x: int, _len: int = sig.length) -> float:
                        left = x
                        right = _len - x - 1
                        penalty = 0
                        if left == 3:
                            penalty += 10
                        if right == 3:
                            penalty += 10
                        return abs(x - mid) + penalty

                    offsets = sorted(range(2, sig.length - 1), key=_partition_score)
                    for offset in offsets:
                        box_r, box_c = sig.cells[offset]
                        box_cell = grid.cell(box_r, box_c)
                        if box_cell.type != CellType.EMPTY_PLAYABLE:
                            continue
                        if box_cell.part_of_word_ids:
                            continue
                        try:
                            grid._add_clue_box(box_r, box_c)
                            LOGGER.debug(
                                "Partitioned span (%d,%d) len=%d with clue at (%d,%d)",
                                r, c, sig.length, box_r, box_c,
                            )
                            changed = True
                            break
                        except ClueBoxError:
                            continue
        return changed

    def _ensure_all_licensed(self, grid: CrosswordGrid) -> None:
        """Ensure every slot boundary has clue licensing, respecting MAX_CLUES_PER_BOX.

        Maintains structural license counts separately from placed-word licenses:
        during layout no words are placed yet, so clue_box_licenses would read as
        empty for all newly created boxes. The structural dict tracks how many slot
        starts have been assigned to each box within this call.
        """
        # (row, col, direction.value) → assigned clue box position
        assigned: Dict[Tuple[int, int, str], Tuple[int, int]] = {}
        # box position → number of slot starts assigned to it (layout-phase only)
        structural: Dict[Tuple[int, int], int] = defaultdict(int)

        changed = True
        while changed:
            changed = False
            for direction in (Direction.ACROSS, Direction.DOWN):
                for r in range(grid.bounds.rows):
                    for c in range(grid.bounds.cols):
                        slot_key = (r, c, direction.value)
                        if slot_key in assigned:
                            continue
                        cell = grid.cell(r, c)
                        if not cell.is_playable():
                            continue
                        if not grid.is_boundary(r, c, direction):
                            continue
                        sig = self._build_signature(grid, r, c, direction)
                        if not sig or sig.length < 2:
                            continue

                        # Find adjacent clue box with remaining capacity
                        licensed_by: Optional[Tuple[int, int]] = None
                        for dr, dc in grid._clue_offsets(direction):
                            nr, nc = r + dr, c + dc
                            if not grid.bounds.contains(nr, nc):
                                continue
                            if grid.cell(nr, nc).type == CellType.CLUE_BOX:
                                placed = len(grid.clue_box_licenses.get((nr, nc), set()))
                                if placed + structural[(nr, nc)] < MAX_CLUES_PER_BOX:
                                    licensed_by = (nr, nc)
                                    break

                        if licensed_by is not None:
                            assigned[slot_key] = licensed_by
                            structural[licensed_by] += 1
                            continue

                        # No adjacent under-capacity box — find or create one
                        try:
                            cb = grid.ensure_clue_box(r, c, direction)
                            assigned[slot_key] = cb
                            structural[cb] += 1
                            changed = True
                        except ClueBoxError:
                            # Convert start cell to clue box to eliminate the unlicensable slot
                            if cell.type == CellType.EMPTY_PLAYABLE and not cell.part_of_word_ids:
                                try:
                                    grid._add_clue_box(r, c)
                                    LOGGER.debug(
                                        "Eliminated unlicensable slot start at (%d,%d) dir=%s -> CLUE_BOX",
                                        r, c, direction.value,
                                    )
                                    changed = True
                                except ClueBoxError:
                                    LOGGER.debug(
                                        "Cannot resolve unlicensable slot at (%d,%d) dir=%s",
                                        r, c, direction.value,
                                    )
            if changed:
                self._heal_isolated_cells(grid)

    def _verify_feasibility(self, grid: CrosswordGrid) -> None:
        """Verify all ≥3-letter slots have dictionary candidates."""
        all_sigs = self._enumerate_all_slots(grid)
        for sig in all_sigs:
            if sig.length < 3:
                continue
            pattern = self._signature_pattern(grid, sig)
            if all(pattern):
                # Fully filled — check it's a valid word
                surface = "".join(p for p in pattern if p)
                if surface in self.theme_word_surfaces or self.dictionary.contains(surface):
                    continue
                raise CrosswordError(
                    f"Pre-filled invalid word '{surface}' at ({sig.start_row},{sig.start_col})"
                )
            if not self.dictionary.has_candidates(sig.length, pattern=pattern, banned=self.used_words):
                # Try to partition the infeasible slot
                if sig.length <= 4 and self._try_partition_infeasible(grid, sig):
                    continue
                raise CrosswordError(
                    f"Infeasible slot at ({sig.start_row},{sig.start_col}) len={sig.length}"
                )

    def _try_partition_infeasible(self, grid: CrosswordGrid, sig: SlotSignature) -> bool:
        """Try to break an infeasible slot by placing a clue box in it."""
        for offset in range(1, sig.length):
            box_r, box_c = sig.cells[offset]
            box_cell = grid.cell(box_r, box_c)
            if box_cell.type != CellType.EMPTY_PLAYABLE:
                continue
            if box_cell.part_of_word_ids:
                continue
            try:
                grid._add_clue_box(box_r, box_c)
                LOGGER.info(
                    "Partitioned infeasible slot at (%d,%d) with clue at (%d,%d)",
                    sig.start_row, sig.start_col, box_r, box_c,
                )
                return True
            except ClueBoxError:
                continue
        return False

    # ------------------------------------------------------------------
    # CP-SAT filling
    # ------------------------------------------------------------------
    def _cpsat_fill(self, grid: CrosswordGrid, allow_phase2: bool = True) -> None:
        """Fill all unfilled slots using CP-SAT solver."""
        from .solver import solve_crossword

        all_slots = self._enumerate_all_slots(grid)
        unfilled = [
            s for s in all_slots
            if not self._is_fully_filled(grid, s)
        ]
        if not unfilled:
            LOGGER.info("No unfilled slots; skipping CP-SAT")
            return

        # Pre-resolve clue boxes and filter out unlicensable slots.
        # Track pending assignments per box so we don't exceed MAX_CLUES_PER_BOX
        # within the same batch (ensure_clue_box only sees already-placed licenses).
        slot_clue_boxes: Dict[int, Tuple[int, int]] = {}
        licensable: List = []
        pending_licenses: Dict[Tuple[int, int], int] = {}
        for sig in unfilled:
            try:
                cb = grid.ensure_clue_box(sig.start_row, sig.start_col, sig.direction)
                existing = len(grid.clue_box_licenses.get(cb, set()))
                if existing + pending_licenses.get(cb, 0) >= MAX_CLUES_PER_BOX:
                    LOGGER.debug(
                        "Skipping slot (%d,%d) dir=%s — clue box %s full after pending",
                        sig.start_row, sig.start_col, sig.direction.value, cb,
                    )
                    continue
                slot_clue_boxes[id(sig)] = cb
                pending_licenses[cb] = pending_licenses.get(cb, 0) + 1
                licensable.append(sig)
            except ClueBoxError:
                LOGGER.debug(
                    "Skipping unlicensable slot at (%d,%d) dir=%s len=%d",
                    sig.start_row, sig.start_col, sig.direction.value, sig.length,
                )

        if not licensable:
            LOGGER.info("No licensable unfilled slots; skipping CP-SAT")
            return

        LOGGER.info("CP-SAT: %d unfilled slots to fill (%d skipped unlicensable)",
                     len(licensable), len(unfilled) - len(licensable))

        # Candidate cap controls tier purity for the CP-SAT solver.
        # Candidates are sorted by WordEntry.score(), so earlier entries are
        # always higher-priority for the active difficulty tier.
        # EASY:   large pool — scoring gap (~0.86 vs ~0.24) ensures ≥95% easy
        #         words in the primary pool; medium fallback only used if primary fails
        # MEDIUM: large pool → no strict tier constraint, natural variety
        # HARD:   medium cap → ~60% hard words; remainder are medium then easy
        #         (direction bonus in score() ensures HARD > MEDIUM > EASY ordering)
        _SOLVER_CANDIDATES = {"EASY": 2000, "MEDIUM": 8000, "HARD": 1500}

        max_cands = _SOLVER_CANDIDATES.get(self.config.difficulty, 8000)
        timeout = self.config.fill_timeout_seconds

        # EASY: two-phase fill.
        # Phase 1 (easy-only): hard DS ceiling guarantees no medium words slip in.
        # Phase 2 (limited medium): only reached after _EASY_PHASE1_RETRIES full
        # layout+theme attempts have all failed phase 1. Medium words are allowed
        # only for slots that have zero easy candidates, capped at ~25% of slots.
        if self.config.difficulty == "EASY":
            phase1_timeout = timeout if not allow_phase2 else timeout * 0.6
            result = solve_crossword(
                grid, licensable, self.dictionary,
                used_words=self.used_words,
                theme_surfaces=self.theme_word_surfaces,
                timeout=phase1_timeout,
                max_candidates=max_cands,
                fallback_fraction=0.0,
                max_difficulty_score=0.3,
                medium_slot_limit=0,
            )
            if result is None and allow_phase2:
                # Each relaxed slot contributes at most 1 medium word.
                # Dynamic cap: ~25% of fill slots, floor of 5.
                medium_limit = max(5, len(licensable) // 4)
                LOGGER.info(
                    "EASY phase 1 failed; retrying with medium fallback for up to %d slot(s)",
                    medium_limit,
                )
                result = solve_crossword(
                    grid, licensable, self.dictionary,
                    used_words=self.used_words,
                    theme_surfaces=self.theme_word_surfaces,
                    timeout=timeout * 0.4,
                    max_candidates=max_cands,
                    fallback_fraction=0.0,
                    max_difficulty_score=0.3,
                    medium_slot_limit=medium_limit,
                )
            elif result is None:
                raise CrosswordError("CP-SAT phase 1 (easy-only) found no solution")
        else:
            result = solve_crossword(
                grid, licensable, self.dictionary,
                used_words=self.used_words,
                theme_surfaces=self.theme_word_surfaces,
                timeout=timeout,
                max_candidates=max_cands,
                fallback_fraction=0.0,
            )
        if result is None:
            raise CrosswordError("CP-SAT solver found no solution")

        # Place all solved words on the grid
        for sig, word in result:
            clue_box = slot_clue_boxes[id(sig)]
            slot = WordSlot(
                id=self._next_slot_id(sig.direction),
                start_row=sig.start_row,
                start_col=sig.start_col,
                direction=sig.direction,
                length=sig.length,
                clue_box=clue_box,
            )
            try:
                grid.place_word(slot, word)
            except SlotPlacementError as exc:
                raise CrosswordError(
                    f"Cannot place CP-SAT word '{word}' at ({sig.start_row},{sig.start_col}): {exc}"
                ) from exc
            self._register_slot(slot)
            self.used_words.add(word)

    def _enumerate_all_slots(self, grid: CrosswordGrid) -> List[SlotSignature]:
        """Find all slots (filled and unfilled) by scanning for ≥2-length runs."""
        seen_keys: Set[str] = set()
        slots: List[SlotSignature] = []
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type not in {CellType.EMPTY_PLAYABLE, CellType.LETTER}:
                    continue
                for direction in (Direction.ACROSS, Direction.DOWN):
                    if not grid.is_boundary(r, c, direction):
                        continue
                    sig = self._build_signature(grid, r, c, direction)
                    if not sig or sig.length < 2:
                        continue
                    key = self._signature_key(sig)
                    if key in seen_keys:
                        continue
                    # Skip already-registered slots (theme words)
                    if key in self._occupied_slots:
                        continue
                    seen_keys.add(key)
                    slots.append(sig)
        return slots

    @staticmethod
    def _is_fully_filled(grid: CrosswordGrid, sig: SlotSignature) -> bool:
        return all(grid.cell(r, c).letter for r, c in sig.cells)

    # ------------------------------------------------------------------
    # Isolated cell healing
    # ------------------------------------------------------------------
    def _heal_isolated_cells(self, grid: CrosswordGrid) -> None:
        """Convert isolated EMPTY_PLAYABLE cells to CLUE_BOX."""
        for r in range(grid.bounds.rows):
            for c in range(grid.bounds.cols):
                cell = grid.cell(r, c)
                if cell.type != CellType.EMPTY_PLAYABLE:
                    continue
                has_playable = False
                for dr, dc in ORTHOGONAL_STEPS:
                    nr, nc = r + dr, c + dc
                    if grid.bounds.contains(nr, nc) and grid.cell(nr, nc).type in (
                        CellType.EMPTY_PLAYABLE, CellType.LETTER,
                    ):
                        has_playable = True
                        break
                if has_playable:
                    continue
                try:
                    grid._add_clue_box(r, c)
                    LOGGER.debug("Healed isolated cell at (%d,%d) -> CLUE_BOX", r, c)
                except ClueBoxError:
                    raise CrosswordError(f"Isolated cell at ({r},{c}) cannot be healed")

    # ------------------------------------------------------------------
    # Orphan clue repair
    # ------------------------------------------------------------------
    def _repair_orphan_clues(self, grid: CrosswordGrid) -> bool:
        repaired = False
        for clue_pos, licenses in list(grid.clue_box_licenses.items()):
            cell = grid.cell(*clue_pos)
            if cell.type != CellType.CLUE_BOX or licenses:
                continue
            if self._assign_existing_slot_to_clue(grid, clue_pos):
                repaired = True
                continue
        return repaired

    def _assign_existing_slot_to_clue(self, grid: CrosswordGrid, clue_pos: Tuple[int, int]) -> bool:
        target_licenses = grid.clue_box_licenses.get(clue_pos, set())
        if len(target_licenses) >= MAX_CLUES_PER_BOX:
            return False
        for slot in grid.word_slots.values():
            if not self._clue_can_license_slot(grid, clue_pos, slot):
                continue
            current_clue = slot.clue_box
            if current_clue == clue_pos:
                return True
            current_licenses = grid.clue_box_licenses.get(current_clue, set())
            if len(current_licenses) <= 1:
                continue
            self._move_slot_to_clue(grid, slot, clue_pos)
            LOGGER.debug("Reassigned slot %s to clue %s", slot.id, clue_pos)
            return True
        return False

    def _move_slot_to_clue(
        self,
        grid: CrosswordGrid,
        slot: WordSlot,
        new_clue: Tuple[int, int],
    ) -> None:
        old_clue = slot.clue_box
        if old_clue == new_clue:
            return
        grid.clue_box_licenses.setdefault(new_clue, set()).add(slot.id)
        grid.clue_box_licenses.get(old_clue, set()).discard(slot.id)
        slot.clue_box = new_clue

        old_cell = grid.cell(*old_clue)
        new_cell = grid.cell(*new_clue)
        for clue in list(old_cell.clues_hosted):
            if clue.solution_word_ref_id != slot.id:
                continue
            old_cell.clues_hosted.remove(clue)
            clue.start_offset_r = slot.start_row - new_clue[0]
            clue.start_offset_c = slot.start_col - new_clue[1]
            new_cell.clues_hosted.append(clue)

    def _clue_can_license_slot(
        self,
        grid: CrosswordGrid,
        clue_pos: Tuple[int, int],
        slot: WordSlot,
    ) -> bool:
        for dr, dc in grid._clue_offsets(slot.direction):
            if (slot.start_row + dr, slot.start_col + dc) == clue_pos:
                return True
        return False

    # ------------------------------------------------------------------
    # Crossing validation (used during theme seeding)
    # ------------------------------------------------------------------
    def _validate_crossings(
        self,
        grid: CrosswordGrid,
        slot: WordSlot,
        min_candidates_override: Optional[int] = None,
    ) -> None:
        """Validate that all crossings created by *slot* have enough dictionary candidates.

        Args:
            min_candidates_override: When set, use this value as the minimum
                candidate count for all crossing lengths (soft mode for user words).
                When None, graduated thresholds apply: length 3-4 need >=3,
                length 5 needs >=2, length 6+ needs >=1.
        """
        for check_dir in (Direction.ACROSS, Direction.DOWN):
            for row, col in slot.cells:
                signature = self._build_signature(grid, row, col, check_dir)
                if not signature or signature.length < 2:
                    continue
                key = self._signature_key(signature)
                if key in self._occupied_slots:
                    continue
                if not self._start_has_clue_capacity(
                    grid, signature.start_row, signature.start_col, check_dir
                ):
                    raise ClueBoxError(
                        f"No available clue position for start {(signature.start_row, signature.start_col)}"
                    )
                if signature.length < 3:
                    continue
                pattern = self._signature_pattern(grid, signature)
                if all(pattern):
                    surface = "".join(pattern)
                    sanitized = self.dictionary.sanitize(surface)
                    if sanitized in self.theme_word_surfaces or self.dictionary.contains(surface):
                        continue

                # Determine minimum candidate threshold
                if min_candidates_override is not None:
                    min_required = min_candidates_override
                elif signature.length <= 4:
                    min_required = 3
                elif signature.length == 5:
                    min_required = 2
                else:
                    min_required = 1

                count = self.dictionary.count_candidates(
                    signature.length, pattern, banned=self.used_words,
                )
                if count < min_required:
                    raise SlotPlacementError(
                        f"Too few candidates ({count}/{min_required}) for "
                        f"{signature.length}-letter crossing at "
                        f"{(signature.start_row, signature.start_col)}"
                    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_signature(
        self, grid: CrosswordGrid, seed_row: int, seed_col: int, direction: Direction
    ) -> Optional[SlotSignature]:
        dr, dc = self._step(direction)
        start_row, start_col = seed_row, seed_col
        while True:
            prev_row, prev_col = start_row - dr, start_col - dc
            if not grid.bounds.contains(prev_row, prev_col):
                break
            prev_cell = grid.cell(prev_row, prev_col)
            if prev_cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
                break
            if prev_cell.type in {CellType.LETTER, CellType.EMPTY_PLAYABLE}:
                start_row, start_col = prev_row, prev_col
                continue
            break

        cells: List[Tuple[int, int]] = []
        row, col = start_row, start_col
        while grid.bounds.contains(row, col):
            cell = grid.cell(row, col)
            if cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
                break
            cells.append((row, col))
            row += dr
            col += dc
        if len(cells) < 2:
            return None
        return SlotSignature(start_row=start_row, start_col=start_col, direction=direction, cells=cells)

    def _signature_pattern(self, grid: CrosswordGrid, signature: SlotSignature) -> Sequence[Optional[str]]:
        pattern: List[Optional[str]] = []
        for row, col in signature.cells:
            letter = grid.cell(row, col).letter
            pattern.append(letter if letter else None)
        return pattern

    def _candidate_starts(
        self, grid: CrosswordGrid, length: int, direction: Direction
    ) -> List[Tuple[int, int]]:
        starts: List[Tuple[int, int]] = []
        for row in range(grid.bounds.rows):
            for col in range(grid.bounds.cols):
                if not grid.is_boundary(row, col, direction):
                    continue
                if self._slot_overlaps_block(grid, row, col, direction, length):
                    continue
                starts.append((row, col))
        return starts

    def _queue_start(self, grid: CrosswordGrid, row: int, col: int, direction: Direction) -> None:
        signature = self._build_signature(grid, row, col, direction)
        if not signature or signature.length < 2:
            return
        pattern = self._signature_pattern(grid, signature)
        if all(pattern):
            return
        priority = self._pending_priority(signature, pattern)
        heapq.heappush(
            self.pending_starts,
            (priority, self._pending_counter, (signature.start_row, signature.start_col, direction)),
        )
        self._pending_counter += 1

    @staticmethod
    def _pending_priority(signature: SlotSignature, pattern: Sequence[Optional[str]]) -> float:
        filled = sum(1 for letter in pattern if letter)
        openness = signature.length - filled
        return -(filled * 10) + openness

    def _slot_overlaps_block(
        self, grid: CrosswordGrid, row: int, col: int, direction: Direction, length: int
    ) -> bool:
        dr, dc = self._step(direction)
        for offset in range(length):
            r = row + dr * offset
            c = col + dc * offset
            if not grid.bounds.contains(r, c):
                return True
            cell = grid.cell(r, c)
            if cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
                return True
        return False

    def _can_place_word(
        self, grid: CrosswordGrid, row: int, col: int, direction: Direction, word: str
    ) -> bool:
        dr, dc = self._step(direction)
        for index, letter in enumerate(word):
            r = row + dr * index
            c = col + dc * index
            if not grid.bounds.contains(r, c):
                return False
            cell = grid.cell(r, c)
            if cell.type in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}:
                return False
            if cell.letter and cell.letter != letter:
                return False
        return True

    def _signature_key(self, signature: SlotSignature) -> str:
        return self._signature_components(
            signature.start_row, signature.start_col, signature.direction, signature.length
        )

    def _signature_components(
        self, row: int, col: int, direction: Direction, length: int
    ) -> str:
        return f"{row}:{col}:{direction.value}:{length}"

    def _register_slot(self, slot: WordSlot) -> None:
        key = self._signature_components(slot.start_row, slot.start_col, slot.direction, slot.length)
        self._occupied_slots.add(key)
        self._slot_keys[slot.id] = key
        self._placement_history.append(slot.id)

    def _next_slot_id(self, direction: Direction) -> str:
        self._slot_counter += 1
        prefix = "A" if direction == Direction.ACROSS else "D"
        return f"{prefix}{self._slot_counter:04d}"

    def _step(self, direction: Direction) -> Tuple[int, int]:
        return (0, 1) if direction == Direction.ACROSS else (1, 0)

    def _reset_state(self) -> None:
        self.used_words.clear()
        self.remaining_theme_words.clear()
        self.theme_word_surfaces.clear()
        self._theme_crossword_title = None
        self._theme_content = None
        self._theme_cache_ref = None
        self._occupied_slots.clear()
        self._slot_keys.clear()
        self._slot_counter = 0
        self._placement_history.clear()
        self.pending_starts = []
        self._pending_counter = 0

    def _start_has_clue_capacity(
        self,
        grid: CrosswordGrid,
        start_row: int,
        start_col: int,
        direction: Direction,
    ) -> bool:
        for dr, dc in grid._clue_offsets(direction):
            clue_row = start_row + dr
            clue_col = start_col + dc
            if not grid.bounds.contains(clue_row, clue_col):
                continue
            neighbor = grid.cell(clue_row, clue_col)
            if neighbor.type == CellType.CLUE_BOX:
                return True
            if neighbor.type == CellType.EMPTY_PLAYABLE and grid._can_place_clue_box(clue_row, clue_col):
                return True
        return False

    # ------------------------------------------------------------------
    # Annealing
    # ------------------------------------------------------------------
    def _anneal_layout(self, grid: CrosswordGrid) -> None:
        best_snapshot = grid.snapshot()
        best_score = self._score_layout(grid)
        attempts = max(3, min(8, self.config.retry_limit * 2))

        for _ in range(attempts):
            trial_grid = CrosswordGrid(grid.config)
            trial_grid.place_blocker_zone()
            score = self._score_layout(trial_grid)
            if score > best_score:
                best_score = score
                best_snapshot = trial_grid.snapshot()

        grid.restore(best_snapshot)

    def _score_layout(self, grid: CrosswordGrid) -> float:
        playable = sum(
            1
            for row in grid.cells
            for cell in row
            if cell.type not in {CellType.CLUE_BOX, CellType.BLOCKER_ZONE}
        )
        if not playable:
            return 0.0
        clue_penalty = 0
        for (row, col), licenses in grid.clue_box_licenses.items():
            if not licenses:
                clue_penalty += 1
            for dr, dc in ORTHOGONAL_STEPS:
                nr, nc = row + dr, col + dc
                if grid.bounds.contains(nr, nc) and grid.cell(nr, nc).type == CellType.CLUE_BOX:
                    clue_penalty += 2
        blocker_penalty = 0
        if grid.blocker_zone:
            h, w = grid.blocker_zone[2], grid.blocker_zone[3]
            blocker_penalty = abs(h - w)
        return playable - 3 * clue_penalty - blocker_penalty

    # ------------------------------------------------------------------
    # Compatibility: _fill_crossword for debug_main step_fill
    # ------------------------------------------------------------------
    def _fill_crossword(self, grid: CrosswordGrid, deadline: Optional[float]) -> None:
        """Fill the crossword using layout completion + CP-SAT.

        This method exists for backward compatibility with debug_main.step_fill().
        """
        self._complete_layout(grid)
        self._cpsat_fill(grid, allow_phase2=True)
