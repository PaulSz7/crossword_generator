"""Convenience entrypoint with predefined generator settings for debugging.

Usage in a Python console (Jupyter-style)::

    import debug_main
    state = debug_main.prepare_state(height=8, width=10)
    debug_main.step_seed_theme(state)
    debug_main.step_fill(state)
    debug_main.step_validate(state)
    debug_main.step_clues(state)
    result = debug_main.build_result(state)

Call :func:`run_debug` for a one-liner, or execute the functions above one by
one to inspect intermediate state.
"""

from __future__ import annotations

import logging
import time
import random
from pathlib import Path
from typing import Any, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from crossword.core.exceptions import CrosswordError, ThemeWordError
from crossword.engine.generator import CrosswordGenerator, GeneratorConfig, CrosswordResult
from crossword.engine.grid import CrosswordGrid
from crossword.data.preprocess import ensure_processed_dictionary
from crossword.io.clues import ClueRequest, attach_clues_to_grid
from crossword.utils.logger import configure_logging
from crossword.utils.pretty import pretty_print_grid, print_crossword_stats

DEFAULT_DEBUG_ARGS: Dict[str, Any] = {
    "height": 10,
    "width": 12,
    "theme": "mitologie",
    "dictionary_path": Path("local_db/dex_words.tsv"),
    "seed": None,
    "completion_target": 1,
    "max_iterations": 8000,
    "fill_timeout_seconds": 75.0,
    "difficulty": "HARD",
    "place_blocker_zone": False
}

LOGGER = logging.getLogger(__name__)


def prepare_state(**overrides: Any) -> Dict[str, Any]:
    """Return a mutable state dictionary used by the step helpers."""

    args = {**DEFAULT_DEBUG_ARGS, **overrides}
    configure_logging()
    dictionary_path = Path(args["dictionary_path"])
    processed_path = ensure_processed_dictionary(
        dictionary_path,
        dictionary_path.with_name(f"{dictionary_path.stem}_processed{dictionary_path.suffix}"),
    )
    LOGGER.info("Using processed dictionary cache at %s", processed_path)
    config_kwargs: Dict[str, Any] = {
        "height": int(args["height"]),
        "width": int(args["width"]),
        "dictionary_path": dictionary_path,
        "theme": str(args["theme"]),
        "seed": int(args["seed"]) if args.get("seed") is not None else None,
        "completion_target": float(args["completion_target"]),
    }
    optional_fields: Dict[str, Any] = {
        "max_iterations": int,
        "retry_limit": int,
        "fill_timeout_seconds": float,
        "min_theme_coverage": float,
        "max_theme_ratio": float,
        "theme_request_size": int,
        "theme_placement_attempts": int,
        "prefer_theme_candidates": bool,
        "difficulty": str,
        "language": str,
        "place_blocker_zone": bool,
        "blocker_zone_height": int,
        "blocker_zone_width": int,
        "blocker_zone_row": int,
        "blocker_zone_col": int,
    }
    for field, caster in optional_fields.items():
        if field in args:
            config_kwargs[field] = caster(args[field])
    config = GeneratorConfig(**config_kwargs)
    generator = CrosswordGenerator(config)
    grid_seed = generator.rng.randint(0, 1_000_000)
    grid = CrosswordGrid(config.to_grid_config(seed_override=grid_seed))
    return {
        "config": config,
        "generator": generator,
        "grid": grid,
        "theme_words": [],
        "validation": None,
        "slots": [],
        "clue_texts": {},
        "processed_dictionary_path": processed_path,
    }


def load_processed_dataframe(state: Dict[str, Any], *, limit: int | None = 10):
    """Return the processed dictionary as a pandas DataFrame.

    ``limit`` controls how many rows are printed (``None`` disables the preview).
    """

    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Viewing the processed dictionary requires pandas. Install it via 'poetry add pandas' or 'pip install pandas'."
        ) from exc

    path = state.get("processed_dictionary_path")
    if not path:
        raise RuntimeError("State missing 'processed_dictionary_path'. Call prepare_state() first.")

    df = pd.read_csv(path, sep="\t")
    if limit is not None:
        print(df.head(limit))
    return df


def step_seed_theme(state: Dict[str, Any], retries: int = 3):
    generator: CrosswordGenerator = state["generator"]
    for attempt in range(1, retries + 1):
        try:
            state["theme_words"] = generator._seed_theme_words(state["grid"])
            return state["theme_words"]
        except ThemeWordError as exc:
            LOGGER.warning(
                "Theme seeding attempt %s/%s failed: %s",
                attempt,
                retries,
                exc,
            )
            if attempt >= retries:
                raise
            generator._reset_state()
            new_seed = generator.rng.randint(0, 1_000_000)
            state["grid"] = CrosswordGrid(
                state["config"].to_grid_config(seed_override=new_seed)
            )
            state["theme_words"] = []
            state["slots"] = []
            state["validation"] = None
            state["clue_texts"] = {}
    return state["theme_words"]


def step_fill(state: Dict[str, Any]):
    deadline = (
        time.time() + state["config"].fill_timeout_seconds
        if  state["config"].fill_timeout_seconds
        else None
    )
    state["generator"]._fill_crossword(state["grid"], deadline)
    return state["grid"]


def step_validate(state: Dict[str, Any]):
    theme_surfaces = state["generator"].theme_word_surfaces
    state["validation"] = state["generator"].validator.validate(state["grid"], theme_surfaces)
    return state["validation"]


def step_clues(state: Dict[str, Any]):
    state["slots"] = list(state["grid"].word_slots.values())
    requests: List[ClueRequest] = [
        ClueRequest(
            slot_id=slot.id,
            word=slot.text or "",
            direction=slot.direction.value,
            clue_box=slot.clue_box,
        )
        for slot in state["slots"]
    ]
    state["clue_texts"] = state["generator"].clue_generator.generate(requests)
    attach_clues_to_grid(state["grid"], state["slots"], state["clue_texts"])
    return state["clue_texts"]


def build_result(state: Dict[str, Any]) -> CrosswordResult:
    slots = state["slots"] or list(state["grid"].word_slots.values())
    messages = state["validation"].messages if state["validation"] else []
    return CrosswordResult(
        grid=state["grid"],
        slots=slots,
        theme_words=state["theme_words"],
        validation_messages=messages,
        seed=state["config"].seed,
    )


def run_debug(**overrides: Any) -> CrosswordResult:
    """Execute the pipeline with automatic retries for a valid grid."""

    max_runs = int(overrides.pop("max_runs", 15))
    theme_retries = int(overrides.pop("theme_retries", 5))
    parallel_runs = int(overrides.pop("parallel_runs", 1))
    requested_seed = overrides.get("seed", DEFAULT_DEBUG_ARGS.get("seed"))
    base_max_iterations = int(overrides.get("max_iterations", DEFAULT_DEBUG_ARGS["max_iterations"]))
    base_timeout = float(overrides.get(
        "fill_timeout_seconds",
        DEFAULT_DEBUG_ARGS["fill_timeout_seconds"],
    ))
    last_error: Exception | None = None
    base_overrides = dict(overrides)
    base_overrides.pop("seed", None)
    base_overrides.pop("max_iterations", None)
    base_overrides.pop("fill_timeout_seconds", None)

    attempt_index = 0
    while attempt_index < max_runs:
        batch_size = min(parallel_runs, max_runs - attempt_index)
        batch: List[Tuple[int, Dict[str, Any]]] = []
        for _ in range(batch_size):
            attempt_index += 1
            attempt_seed = (
                requested_seed
                if requested_seed is not None and attempt_index == 1
                else random.randint(0, 1_000_000)
            )
            attempt_overrides = dict(base_overrides)
            attempt_overrides["seed"] = attempt_seed
            scale = min(1.2, 1 + 0.1 * (attempt_index - 1))
            attempt_overrides["max_iterations"] = int(base_max_iterations * scale)
            attempt_overrides["fill_timeout_seconds"] = base_timeout * scale
            batch.append((attempt_index, attempt_overrides))

        if batch_size == 1:
            attempt_no, attempt_kwargs = batch[0]
            try:
                result = _run_single_attempt(attempt_no, attempt_kwargs, theme_retries)
                return result
            except (ThemeWordError, CrosswordError) as exc:
                LOGGER.warning(
                    "Attempt %s/%s failed with seed %s: %s",
                    attempt_no,
                    max_runs,
                    attempt_kwargs.get("seed"),
                    exc,
                )
                last_error = exc
                continue

        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = {
                executor.submit(_run_single_attempt, attempt_no, attempt_kwargs, theme_retries): (
                    attempt_no,
                    attempt_kwargs,
                )
                for attempt_no, attempt_kwargs in batch
            }
            success: CrosswordResult | None = None
            for future in as_completed(futures):
                attempt_no, attempt_kwargs = futures[future]
                try:
                    success = future.result()
                    LOGGER.info(
                        "Generation succeeded on attempt %s/%s (seed %s)",
                        attempt_no,
                        max_runs,
                        attempt_kwargs.get("seed"),
                    )
                    break
                except (ThemeWordError, CrosswordError) as exc:
                    LOGGER.warning(
                        "Attempt %s/%s failed with seed %s: %s",
                        attempt_no,
                        max_runs,
                        attempt_kwargs.get("seed"),
                        exc,
                    )
                    last_error = exc
                    continue
            if success:
                for future in futures:
                    future.cancel()
                return success

    raise CrosswordError("Unable to generate crossword after retries") from last_error


def _run_single_attempt(
    attempt_no: int,
    attempt_overrides: Dict[str, Any],
    theme_retries: int,
) -> CrosswordResult:
    state = prepare_state(**attempt_overrides)
    step_seed_theme(state, retries=theme_retries)
    pretty_print_grid(state['grid'])
    step_fill(state)
    validation = step_validate(state)
    if validation and not validation.ok:
        pretty_print_grid(state['grid'])
        raise CrosswordError(f"Validation failed: {validation.messages}")
    step_clues(state)
    result = build_result(state)
    print_crossword_stats(result, state["generator"].dictionary)
    return result


def main() -> None:  # pragma: no cover - manual helper
    result = run_debug()
    print(f"Seed: {result.seed}")
    print(f"Generated {len(result.slots)} slots for theme '{DEFAULT_DEBUG_ARGS['theme']}'")
    print(f"Validation: {result.validation_messages or 'ok'}")


if __name__ == "__main__":
    main()
