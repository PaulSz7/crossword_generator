"""CLI entrypoint for the Romanian cryptic crossword generator."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from crossword.data.theme import ThemeType
from crossword.data.theme_cache import ThemeCache
from crossword.engine.crossword_store import CrosswordStore
from crossword.engine.generator import CrosswordGenerator, GeneratorConfig
from crossword.utils.logger import configure_logging


def parse_words_file(path: Path) -> List[str]:
    """Read words from a file, one entry per line. Blank lines and # comments are skipped."""
    entries: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Romanian cryptic barred crosswords",
    )
    parser.add_argument("--height", type=int, required=True, help="Grid height in cells")
    parser.add_argument("--width", type=int, required=True, help="Grid width in cells")
    parser.add_argument("--theme-title", type=str, default="", help="Theme title / keyword")
    parser.add_argument(
        "--theme-type",
        type=str,
        choices=[t.value for t in ThemeType],
        default="domain_specific_words",
        help="Theme generation strategy",
    )
    parser.add_argument(
        "--words",
        nargs="+",
        metavar="WORD",
        help="Explicit seed words (format: WORD or WORD:Clue)",
    )
    parser.add_argument(
        "--words-file",
        type=Path,
        metavar="FILE",
        help="File with one WORD or WORD:Clue entry per line (# comments and blank lines ignored)",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Extend user-provided words with LLM-generated theme words (requires --theme-title for domain_specific_words)",
    )
    parser.add_argument(
        "--theme-description",
        type=str,
        default="",
        help="Additional context for the LLM prompt (creative brief for 'custom', joke text for 'joke_continuation')",
    )
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

    theme_type = args.theme_type
    has_user_words = bool(args.words or args.words_file)

    # Validate mode flags
    if args.llm and not args.theme_title and theme_type == "domain_specific_words":
        parser.error("--llm requires --theme-title for domain_specific_words")
    if theme_type == "domain_specific_words" and not args.theme_title and not has_user_words:
        parser.error("provide at least --theme-title or --words / --words-file")
    if theme_type == "words_containing_substring" and not args.theme_title:
        parser.error("--theme-type words_containing_substring requires --theme-title")
    if theme_type == "words_containing_substring" and not has_user_words and not args.llm:
        parser.error(
            "--theme-type words_containing_substring without --words requires --llm "
            "to enable the dictionary substring search"
        )

    # Collect user-provided words
    user_words: List[str] = []
    if args.words:
        user_words.extend(args.words)
    if args.words_file:
        user_words.extend(parse_words_file(args.words_file))

    extend_with_substring = (
        theme_type == "words_containing_substring" and bool(user_words) and args.llm
    )

    config = GeneratorConfig(
        height=args.height,
        width=args.width,
        dictionary_path=args.dictionary,
        theme_title=args.theme_title,
        theme_type=theme_type,
        theme_description=args.theme_description,
        extend_with_substring=extend_with_substring,
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

    # Initialise persistent stores
    theme_cache = ThemeCache()
    crossword_store = CrosswordStore()

    # Wire up generators based on mode
    theme_gen = None
    fallbacks = None  # None → CrosswordGenerator will use default [DummyThemeWordGenerator]

    from crossword.data.theme import DummyThemeWordGenerator, GeminiThemeWordGenerator, UserWordListGenerator

    if theme_type == "words_containing_substring":
        # SubstringThemeWordGenerator is always wired inside _seed_theme_words.
        # extend_with_substring (set above) tells it whether to extend user words from the dictionary.
        if user_words:
            theme_gen = UserWordListGenerator(user_words)
        # theme_gen=None when no user words → SubstringGen becomes primary in _seed_theme_words
        fallbacks = []  # no LLM/dummy fallbacks for this type

    elif theme_type == "joke_continuation":
        if user_words and not args.llm:
            theme_gen = UserWordListGenerator(user_words)
            fallbacks = []
        elif user_words and args.llm:
            theme_gen = UserWordListGenerator(user_words)
            fallbacks = [
                GeminiThemeWordGenerator(
                    theme_type=theme_type,
                    theme_description=args.theme_description,
                    cache=theme_cache,
                ),
                DummyThemeWordGenerator(seed=config.seed),
            ]
        else:
            theme_gen = GeminiThemeWordGenerator(
                theme_type=theme_type,
                theme_description=args.theme_description,
                cache=theme_cache,
            )
            fallbacks = [DummyThemeWordGenerator(seed=config.seed)]

    elif theme_type == "custom":
        theme_gen = GeminiThemeWordGenerator(
            theme_type=theme_type,
            theme_description=args.theme_description,
            cache=theme_cache,
        )
        fallbacks = [DummyThemeWordGenerator(seed=config.seed)]

    else:
        # domain_specific_words (default)
        if user_words:
            theme_gen = UserWordListGenerator(user_words)
            if args.llm:
                fallbacks = [
                    GeminiThemeWordGenerator(
                        theme_type=theme_type,
                        theme_description=args.theme_description,
                        cache=theme_cache,
                    ),
                    DummyThemeWordGenerator(seed=config.seed),
                ]
            else:
                fallbacks = []
        # else: theme_gen stays None → CrosswordGenerator uses default [DummyThemeWordGenerator]

    generator = CrosswordGenerator(
        config,
        theme_generator=theme_gen,
        theme_fallback_generators=fallbacks,
        store=crossword_store,
        theme_cache=theme_cache,
    )
    result = generator.generate()

    payload: Dict[str, Any] = {
        "crossword_title": result.crossword_title,
        "theme_content": result.theme_content,
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
