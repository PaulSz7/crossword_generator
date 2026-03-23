"""Batch-fetch word definitions from an online dictionary via grounded Gemini.

Handles two cases:
- Word absent from the local DB entirely.
- Word present but definition is truncated (ends with ellipsis due to copyright).
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..data.definition_store import DefinitionStore
from ..utils.logger import get_logger
from .gemini_client import GeminiClient

LOGGER = get_logger(__name__)


# The local DB stores at most this many characters per definition.
# Entries that hit this limit are truncated mid-text regardless of whether
# they end with an ellipsis (value_counts on dex_words.tsv shows 2.3 M entries
# at exactly this length, vs. ~3 K each for 22, 21, 20, … — a clear hard cap).
_DEF_MAX_LENGTH = 23


def is_incomplete_definition(definition: str) -> bool:
    """Return True if the definition is likely truncated.

    Two signals:
    - Ends with an ellipsis (explicit copyright truncation marker).
    - Length is at or below the DB column cap (23 chars), which silently cuts
      off longer definitions stored by non-DEX-96/98 sources.
    """
    stripped = definition.rstrip()
    return (
        stripped.endswith("...") or stripped.endswith("\u2026")
        or len(stripped) <= _DEF_MAX_LENGTH
    )


_SYSTEM_TEMPLATE = (
    "You are a linguistics assistant. "
    "Look up the definition of each word on {dictionary_url} "
    'and return a JSON object {{"WORD": "definition"}} with no other text.'
)

_PROMPT_TEMPLATE = (
    "Look up the exact {language} definitions (with grammatical indicators) "
    "for each word below on {dictionary_url}. "
    'Return ONLY a JSON object {{"WORD": "definition"}}.\n\n'
    "{words}"
)


class GeminiDefinitionFetcher:
    """Batch-fetches word definitions from an online dictionary via one grounded Gemini call."""

    def __init__(
        self,
        client: GeminiClient,
        store: DefinitionStore,
        language: str = "Romanian",
        dictionary_url: str = "dexonline.ro",
    ) -> None:
        self._client = client
        self._store = store
        self._language = language
        self._dictionary_url = dictionary_url
        self._session_cache: Dict[str, Optional[str]] = {}

    def fetch_batch(self, words: List[str]) -> Dict[str, str]:
        """Return definitions for all words, using store/session cache where available.

        Words not yet cached are fetched in a single grounded Gemini call and persisted.
        """
        uncached: List[str] = []
        for w in words:
            key = w.upper()
            if key in self._session_cache:
                continue
            stored = self._store.get(w)
            if stored is not None:
                self._session_cache[key] = stored
            else:
                uncached.append(w)

        if uncached:
            system = _SYSTEM_TEMPLATE.format(dictionary_url=self._dictionary_url)
            prompt = _PROMPT_TEMPLATE.format(
                language=self._language,
                dictionary_url=self._dictionary_url,
                words="\n".join(w.upper() for w in uncached),
            )
            try:
                raw = self._client.generate_text_grounded(prompt, system_instruction=system, request_type="definition_fetch")
                # Strip markdown code fences if present
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                parsed: Dict = json.loads(raw)
                for w in uncached:
                    defn: Optional[str] = parsed.get(w.upper()) or None
                    self._session_cache[w.upper()] = defn
                    if defn:
                        self._store.save(w, defn)
                        LOGGER.info("Fetched and stored definition for %s", w.upper())
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Grounded definition lookup failed: %s", exc)
                for w in uncached:
                    self._session_cache[w.upper()] = None

        return {
            w: self._session_cache[w.upper()]
            for w in words
            if self._session_cache.get(w.upper())
        }
