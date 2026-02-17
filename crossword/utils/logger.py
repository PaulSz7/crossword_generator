"""Logging utilities tailored for crossword generation."""

from __future__ import annotations

import logging
from typing import Optional


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging with a sensible formatter.

    The generator performs many reversible attempts, so logging needs to be
    structured while remaining lightweight. The default configuration can be
    customized by callers before invoking :class:`CrosswordGenerator`.
    """

    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a namespaced logger, configuring defaults if needed."""

    if not logging.getLogger().handlers:
        configure_logging()
    return logging.getLogger(name or "crossword")
