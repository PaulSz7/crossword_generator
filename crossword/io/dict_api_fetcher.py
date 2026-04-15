"""Direct HTTP fetcher for word definitions from a REST dictionary API.

Language-agnostic: the fetcher behaviour is driven entirely by DictionaryApiConfig.
Add a new config constant to support additional languages or dictionaries — no
code changes needed in the fetcher itself.

Current configs:
  DEXONLINE_CONFIG — Romanian, dexonline.ro JSON API
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..data.definition_store import DefinitionStore
from ..utils.logger import get_logger
from .definition_fetcher import DefinitionFetcher

LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class DictionaryApiConfig:
    """Configuration for one REST dictionary API.

    Args:
        url_template: URL with ``{word}`` placeholder, e.g.
            ``"https://example.com/api/{word}.json"``.
        definitions_key: Top-level JSON key that holds the list of definition
            objects (default ``"definitions"``).
        source_key: Key inside each definition object that identifies the
            source/dictionary name (default ``"sourceName"``).
        text_key: Key inside each definition object that holds the raw
            definition text (default ``"internalRep"``).
        skip_sources: Source names to ignore (synonym lists, orthographic
            entries, etc.).
        source_preference: Ordered tuple of preferred source names. The first
            matching source in this list wins. Falls back to the first
            non-skipped definition when none match.
        markup_strip_pattern: Regex pattern for markup characters to remove
            from definition text (default covers dexonline ``@$#`` style).
        metadata_block_pattern: Regex pattern for metadata blocks to remove
            before stripping markup (default covers dexonline ``[...]`` blocks).
        timeout_seconds: Per-request HTTP timeout.
        request_delay_seconds: Pause between consecutive HTTP requests (be a
            good citizen toward the API host).
    """

    url_template: str
    definitions_key: str = "definitions"
    source_key: str = "sourceName"
    text_key: str = "internalRep"
    skip_sources: frozenset = field(default_factory=frozenset)
    source_preference: Tuple[str, ...] = field(default_factory=tuple)
    markup_strip_pattern: str = r'[@$#]'
    metadata_block_pattern: str = r'\[.*?\]'
    timeout_seconds: float = 10.0
    request_delay_seconds: float = 0.3


# ---------------------------------------------------------------------------
# Built-in language configs
# ---------------------------------------------------------------------------

#: Romanian — dexonline.ro JSON API
#: Endpoint: GET https://dexonline.ro/definitie/{word}/json
#: Returns 404 for unknown words, 200 with definitions list otherwise.
DEXONLINE_CONFIG = DictionaryApiConfig(
    url_template="https://dexonline.ro/definitie/{word}/json",
    skip_sources=frozenset({
        "Sinonime", "Sinonime82",   # synonym lists, no definitions
        "Ortografic",               # orthographic only, no meaning
        "DOOM 2", "DOOM 3",         # spelling norm only
    }),
    source_preference=(
        "DEX '98", "DEX '96",       # most complete internalRep in local DB
        "DEX '09",                  # updated standard dictionary
        "MDA2",                     # modern comprehensive
        "DLRLC", "NODEX", "MDN '00", "DER", "DN",
    ),
    markup_strip_pattern=r'[@$#]',
    metadata_block_pattern=r'\[.*?\]',
    timeout_seconds=10.0,
    request_delay_seconds=0.3,
)


# ---------------------------------------------------------------------------
# Markup stripping
# ---------------------------------------------------------------------------

_HTML_ENTITIES = {
    "&#039;": "'", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
}


def _strip_markup(text: str, config: DictionaryApiConfig) -> str:
    """Strip dictionary markup from a raw definition string.

    Removes metadata blocks (e.g. ``[#At:# source / #Pl:# ~i]``), decodes
    common HTML entities, then strips remaining inline markup chars.
    """
    # Remove metadata blocks first (e.g. [#At:# CIAUȘANU, V. / #Pl:# $~i$])
    if config.metadata_block_pattern:
        text = re.sub(config.metadata_block_pattern, '', text, flags=re.DOTALL)
    # Decode HTML entities
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    # Strip inline markup chars
    if config.markup_strip_pattern:
        text = re.sub(config.markup_strip_pattern, '', text)
    # Normalize whitespace
    return re.sub(r'\s+', ' ', text).strip()


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class DictApiDefinitionFetcher(DefinitionFetcher):
    """Fetches word definitions directly from a REST dictionary JSON API.

    Each word triggers one HTTP GET request. Results are persisted in
    DefinitionStore and an in-memory session cache, so repeated calls within
    the same session and across sessions never re-fetch the same word.

    Usage::

        fetcher = DictApiDefinitionFetcher(store=DefinitionStore())
        defs = fetcher.fetch_batch(["TREN", "LOCOMOTIVA"])

    To use a different language, pass a custom config::

        fetcher = DictApiDefinitionFetcher(store=store, config=MY_LANG_CONFIG)
    """

    def __init__(
        self,
        store: DefinitionStore,
        config: DictionaryApiConfig = DEXONLINE_CONFIG,
    ) -> None:
        self._store = store
        self._config = config
        self._session_cache: Dict[str, Optional[str]] = {}

    def fetch_batch(self, words: List[str]) -> Dict[str, str]:
        """Return definitions for all resolvable words.

        Checks session cache then persistent store before making HTTP requests.
        Words of length ≤ 2 are always skipped.
        """
        uncached: List[str] = []
        for w in words:
            if len(w) <= 2:
                continue
            key = w.upper()
            if key in self._session_cache:
                continue
            stored = self._store.get(w)
            if stored is not None:
                self._session_cache[key] = stored
            else:
                uncached.append(w)

        for i, w in enumerate(uncached):
            if i > 0:
                time.sleep(self._config.request_delay_seconds)
            defn = self._fetch_one(w)
            self._session_cache[w.upper()] = defn
            if defn:
                self._store.save(w, defn, source="dexonline_api")
                LOGGER.info("Fetched and stored definition for %s", w.upper())
            else:
                LOGGER.debug("No definition found for %s", w.upper())

        return {
            w: self._session_cache[w.upper()]
            for w in words
            if self._session_cache.get(w.upper())
        }

    def _fetch_one(self, word: str) -> Optional[str]:
        """Fetch and return the best definition for a single word, or None."""
        url = self._config.url_template.format(word=word.lower())
        try:
            response = requests.get(url, timeout=self._config.timeout_seconds)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data: Dict[str, Any] = response.json()
        except requests.RequestException as exc:
            LOGGER.warning("Definition fetch failed for %s: %s", word.upper(), exc)
            return None
        except ValueError as exc:
            LOGGER.warning("Failed to parse definition response for %s: %s", word.upper(), exc)
            return None

        definitions: List[Dict[str, Any]] = data.get(self._config.definitions_key) or []
        return self._select_best(definitions)

    def _select_best(self, definitions: List[Dict[str, Any]]) -> Optional[str]:
        """Pick the most authoritative definition from the API response list.

        Filters out skipped sources, then tries preferred sources in order.
        Falls back to the first non-skipped candidate when none match.
        """
        candidates = [
            d for d in definitions
            if d.get(self._config.source_key) not in self._config.skip_sources
            and d.get(self._config.text_key)
        ]
        if not candidates:
            return None

        # Index by source for O(1) preferred-source lookup
        by_source: Dict[str, Dict[str, Any]] = {
            d[self._config.source_key]: d for d in candidates
        }
        for source in self._config.source_preference:
            if source in by_source:
                return _strip_markup(by_source[source][self._config.text_key], self._config)

        # No preferred source found — use first non-skipped candidate
        return _strip_markup(candidates[0][self._config.text_key], self._config)
