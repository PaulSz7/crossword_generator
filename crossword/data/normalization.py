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


__all__ = ["clean_word", "ROMANIAN_DIACRITICS"]

