"""Crossword generator package for Romanian cryptic barred crosswords.

This package exposes the public API surface via:

- ``crossword.engine.generator.CrosswordGenerator``: orchestrates grid generation.
- ``crossword.data.dictionary.WordDictionary``: loads and filters candidate words.
- ``crossword.data.theme`` helpers: theme word generation integrations.

All functionality respects the architectural principles laid out
in the project-level ``AGENTS.md`` file.
"""

from .engine.generator import CrosswordGenerator, GeneratorConfig
from .data.dictionary import WordDictionary, DictionaryConfig

__all__ = [
    "CrosswordGenerator",
    "GeneratorConfig",
    "WordDictionary",
    "DictionaryConfig",
]

__version__ = "0.1.0"
