"""Pretty-print helpers for crossword grids."""

from __future__ import annotations

import sys
from typing import Iterable

from ..core.constants import CellType
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
