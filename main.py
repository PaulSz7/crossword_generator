"""CLI entrypoint for the Romanian cryptic crossword generator."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

from crossword.engine.generator import CrosswordGenerator, GeneratorConfig
from crossword.utils.logger import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Romanian cryptic barred crosswords",
    )
    parser.add_argument("--height", type=int, required=True, help="Grid height in cells")
    parser.add_argument("--width", type=int, required=True, help="Grid width in cells")
    parser.add_argument("--theme", type=str, required=True, help="Theme description")
    parser.add_argument(
        "--dictionary",
        type=Path,
        default=Path("local_db/dex_words.tsv"),
        help="Path to dex_words.tsv",
    )
    parser.add_argument(
        "--completion-target",
        type=float,
        default=0.85,
        help="Target fill ratio between 0 and 1",
    )
    parser.add_argument(
        "--min-theme-coverage",
        type=float,
        default=0.10,
        help="Minimum fraction of playable cells covered by theme letters (default 0.10)",
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument(
        "--difficulty",
        type=str,
        choices=["EASY", "MEDIUM", "HARD"],
        default="MEDIUM",
        help="Difficulty level (EASY, MEDIUM, HARD)",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="Romanian",
        help="Target language for clues and theme words",
    )
    parser.add_argument("--output", type=Path, help="Optional path to JSON output")
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--no-blocker-zone",
        action="store_true",
        help="Disable BLOCKER_ZONE placement",
    )
    parser.add_argument(
        "--blocker-zone-height",
        type=int,
        help="Override blocker zone height (rows)",
    )
    parser.add_argument(
        "--blocker-zone-width",
        type=int,
        help="Override blocker zone width (columns)",
    )
    parser.add_argument(
        "--blocker-zone-row",
        type=int,
        help="Override blocker zone start row",
    )
    parser.add_argument(
        "--blocker-zone-col",
        type=int,
        help="Override blocker zone start column",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = getattr(logging, args.log_level.upper(), logging.INFO)
    configure_logging(level)

    override_fields = [
        args.blocker_zone_height,
        args.blocker_zone_width,
        args.blocker_zone_row,
        args.blocker_zone_col,
    ]
    if args.no_blocker_zone and any(value is not None for value in override_fields):
        parser.error("--no-blocker-zone cannot be combined with blocker zone overrides")

    config = GeneratorConfig(
        height=args.height,
        width=args.width,
        dictionary_path=args.dictionary,
        theme=args.theme,
        seed=args.seed,
        completion_target=args.completion_target,
        min_theme_coverage=args.min_theme_coverage,
        difficulty=args.difficulty,
        language=args.language,
        place_blocker_zone=not args.no_blocker_zone,
        blocker_zone_height=args.blocker_zone_height,
        blocker_zone_width=args.blocker_zone_width,
        blocker_zone_row=args.blocker_zone_row,
        blocker_zone_col=args.blocker_zone_col,
    )
    generator = CrosswordGenerator(config)
    result = generator.generate()

    payload: Dict[str, Any] = {
        "grid": result.grid.to_jsonable(),
        "theme_words": [tw.__dict__ for tw in result.theme_words],
        "slots": [
            {
                "id": slot.id,
                "start": [slot.start_row, slot.start_col],
                "direction": slot.direction.value,
                "length": slot.length,
                "text": slot.text,
                "clue_box": list(slot.clue_box),
                "is_theme": slot.is_theme,
            }
            for slot in result.slots
        ],
        "validation": result.validation_messages,
    }

    output_text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output_text, encoding="utf-8")
    else:
        print(output_text)


if __name__ == "__main__":  # pragma: no cover
    main()
