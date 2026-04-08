"""Shared helpers for Romanian word normalization."""

from __future__ import annotations

import re

ROMANIAN_DIACRITICS = {
    "ă": "a",
    "â": "a",
    "î": "i",
    "ș": "s",
    "ş": "s",
    "ț": "t",
    "ţ": "t",
    "Ă": "a",
    "Â": "a",
    "Î": "i",
    "Ș": "s",
    "Ţ": "t",
    "Ț": "t",
}

WORD_RE = re.compile(r"[^A-Za-z]")


def clean_word(text: str) -> str:
    """Return a normalized uppercase ASCII representation of ``text``."""

    if not text:
        return ""
    transformed = []
    for char in text:
        if char in ROMANIAN_DIACRITICS:
            transformed.append(ROMANIAN_DIACRITICS[char])
        elif char.isalpha():
            transformed.append(char)
    ascii_word = WORD_RE.sub("", "".join(transformed))
    return ascii_word.upper()


def extract_word_breaks(text: str) -> tuple[int, ...]:
    """Return character positions where spaces occur in *text* after diacritic replacement.

    The indices refer to the character stream that ``clean_word`` would see
    *before* stripping non-alpha characters, so they can be used to
    reconstruct the display form from the flat surface.
    """
    if not text:
        return ()
    breaks: list[int] = []
    idx = 0
    for char in text:
        mapped = ROMANIAN_DIACRITICS.get(char, char)
        if mapped == " ":
            breaks.append(idx)
        elif mapped.isalpha():
            idx += 1
    return tuple(breaks)


def display_form(surface: str, word_breaks: tuple[int, ...]) -> str:
    """Reconstruct a display string by inserting spaces at *word_breaks* positions.

    Args:
        surface: Flat uppercase surface (e.g. ``"DEFACTO"``).
        word_breaks: Tuple of character indices where spaces should be inserted,
            as returned by :func:`extract_word_breaks`.

    Returns:
        The surface with spaces re-inserted (e.g. ``"DE FACTO"``).
    """
    if not word_breaks:
        return surface
    result: list[str] = []
    break_set = set(word_breaks)
    for i, ch in enumerate(surface):
        if i in break_set:
            result.append(" ")
        result.append(ch)
    return "".join(result)


__all__ = ["clean_word", "extract_word_breaks", "display_form", "ROMANIAN_DIACRITICS"]

