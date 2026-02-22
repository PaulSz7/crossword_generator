"""Pretty-print helpers for crossword grids."""

from __future__ import annotations

import sys
from collections import Counter
from typing import TYPE_CHECKING, List, Optional

from ..core.constants import CellType

if TYPE_CHECKING:
    from ..data.dictionary import WordDictionary
    from ..engine.generator import CrosswordResult
    from ..engine.grid import CrosswordGrid


SYMBOLS = {
    CellType.CLUE_BOX: "#",
    CellType.BLOCKER_ZONE: "X",
    CellType.EMPTY_PLAYABLE: ".",
}


def cell_symbol(cell) -> str:
    if cell.type == CellType.LETTER:
        return cell.letter or "?"
    return SYMBOLS.get(cell.type, ".")


def format_grid(grid: CrosswordGrid) -> str:
    width = grid.bounds.cols
    header_cells = [f"{c:>2}" for c in range(width)]
    lines = ["    " + " ".join(header_cells)]
    lines.append("    " + "-" * (3 * width - 1))
    for r in range(grid.bounds.rows):
        row_cells = [cell_symbol(grid.cell(r, c)) for c in range(width)]
        row_render = " ".join(f"{symbol:>2}" for symbol in row_cells)
        lines.append(f"{r:>2} | {row_render}")
    return "\n".join(lines)


def pretty_print_grid(grid: CrosswordGrid, *, label: str | None = None, stream=None) -> None:
    """Print the crossword grid in a human-friendly format."""

    stream = stream or sys.stdout
    if label:
        print(label, file=stream)
    print(format_grid(grid), file=stream)


def print_crossword_stats(
    result: CrosswordResult,
    dictionary: Optional[WordDictionary] = None,
    *,
    stream=None,
) -> None:
    """Print grid + comprehensive stats for a completed crossword."""

    stream = stream or sys.stdout
    print(format_grid(result.grid), file=stream)

    # --- Grid geometry ---
    grid = result.grid
    total_cells = grid.bounds.rows * grid.bounds.cols
    letter_cells = 0
    clue_cells = 0
    blocker_cells = 0
    empty_cells = 0
    for r in range(grid.bounds.rows):
        for c in range(grid.bounds.cols):
            ct = grid.cell(r, c).type
            if ct == CellType.LETTER:
                letter_cells += 1
            elif ct == CellType.CLUE_BOX:
                clue_cells += 1
            elif ct == CellType.BLOCKER_ZONE:
                blocker_cells += 1
            elif ct == CellType.EMPTY_PLAYABLE:
                empty_cells += 1

    print(file=stream)
    print("--- Grid ---", file=stream)
    print(f"  Size:          {grid.bounds.rows} x {grid.bounds.cols} ({total_cells} cells)", file=stream)
    print(f"  Letters:       {letter_cells} ({letter_cells / total_cells * 100:.0f}%)", file=stream)
    print(f"  Clue boxes:    {clue_cells}", file=stream)
    print(f"  Blocker zone:  {blocker_cells}", file=stream)
    if empty_cells:
        print(f"  Unfilled:      {empty_cells}", file=stream)

    # --- Slot stats ---
    slots = result.slots
    words = [s.text for s in slots if s.text and len(s.text) >= 2]
    words_3plus = [w for w in words if len(w) >= 3]
    theme_slots = [s for s in slots if s.is_theme]
    fill_slots = [s for s in slots if not s.is_theme and s.text and len(s.text) >= 3]
    lengths = [len(w) for w in words_3plus]
    length_dist = Counter(lengths)

    print(file=stream)
    print("--- Words ---", file=stream)
    print(f"  Total slots:   {len(slots)} ({len(words_3plus)} words >= 3 letters, {len(words) - len(words_3plus)} short)", file=stream)
    print(f"  Theme words:   {len(theme_slots)}", file=stream)
    print(f"  Fill words:    {len(fill_slots)}", file=stream)
    if lengths:
        print(f"  Length range:  {min(lengths)}-{max(lengths)} (avg {sum(lengths) / len(lengths):.1f})", file=stream)
        dist_parts = [f"{l}:{c}" for l, c in sorted(length_dist.items())]
        print(f"  Distribution:  {' '.join(dist_parts)}", file=stream)

    # --- Difficulty stats (requires dictionary) ---
    # Main breakdown counts fill words only; theme words are shown separately.
    if dictionary and words_3plus:
        fill_scores: List[float] = []
        fill_freqs: List[float] = []
        theme_scores: List[float] = []
        not_found: List[str] = []

        for s in fill_slots:
            if s.text:
                entry = dictionary.get(s.text)
                if entry:
                    fill_scores.append(entry.difficulty_score)
                    fill_freqs.append(entry.frequency)
                else:
                    not_found.append(s.text)

        for s in theme_slots:
            if s.text:
                entry = dictionary.get(s.text)
                if entry:
                    theme_scores.append(entry.difficulty_score)

        if fill_scores:
            avg_ds = sum(fill_scores) / len(fill_scores)
            avg_freq = sum(fill_freqs) / len(fill_freqs)
            easy_n = sum(1 for s in fill_scores if s < 0.3)
            med_n = sum(1 for s in fill_scores if 0.3 <= s < 0.6)
            hard_n = sum(1 for s in fill_scores if s >= 0.6)
            total_scored = len(fill_scores)

            print(file=stream)
            print("--- Difficulty (fill words only) ---", file=stream)
            print(f"  Avg difficulty score:  {avg_ds:.3f}", file=stream)
            print(f"  Avg frequency:         {avg_freq:.3f}", file=stream)
            print(f"  Easy  words (<0.3):    {easy_n:>3} ({easy_n / total_scored * 100:5.1f}%)", file=stream)
            print(f"  Medium words (0.3-0.6):{med_n:>3} ({med_n / total_scored * 100:5.1f}%)", file=stream)
            print(f"  Hard  words (>=0.6):   {hard_n:>3} ({hard_n / total_scored * 100:5.1f}%)", file=stream)
            print(f"  Dict coverage:         {total_scored}/{len(fill_slots)}", file=stream)

        if theme_scores:
            avg_theme_ds = sum(theme_scores) / len(theme_scores)
            print(f"  Theme avg DS:          {avg_theme_ds:.3f} ({len(theme_scores)} words)", file=stream)

    # --- Validation ---
    if result.validation_messages:
        print(file=stream)
        print("--- Validation ---", file=stream)
        for msg in result.validation_messages:
            print(f"  {msg}", file=stream)

    if result.seed is not None:
        print(file=stream)
        print(f"Seed: {result.seed}", file=stream)
