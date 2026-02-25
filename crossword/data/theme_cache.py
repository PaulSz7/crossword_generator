"""Local LLM theme word cache.

Persists GeminiThemeWordGenerator outputs to JSON files under
``local_db/collections/llm_theme_cache/`` so the same theme request
is never sent to the LLM twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .theme import ThemeOutput, ThemeType, ThemeWord
from ..utils.logger import get_logger


LOGGER = get_logger(__name__)

DEFAULT_CACHE_DIR = Path("local_db/collections/llm_theme_cache")


class ThemeCache:
    """Persist and reuse LLM-generated theme word lists.

    Cache key strategy
    ------------------
    Key = ``{language}_{difficulty}_{title_slug}_{desc_hash}.json``

    Both ``theme_title`` and ``theme_description`` are normalised independently
    (lowercase, Romanian diacritics stripped, whitespace collapsed).  The
    description is then MD5-hashed (first 8 hex chars) so that two different
    descriptions for the same title produce different cache entries.

    Lookup is O(1) — we compute the expected filename directly.

    CUSTOM / JOKE_CONTINUATION entries are store-only (never looked up) and
    always written to a new timestamped file.
    """

    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE_DIR) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def lookup(
        self,
        theme_title: str,
        difficulty: str,
        language: str,
        theme_description: str = "",
        min_words: int = 1,
        theme_type: str = ThemeType.DOMAIN_SPECIFIC_WORDS.value,
    ) -> Optional[ThemeOutput]:
        """Return cached ThemeOutput for any theme type, or None on miss."""
        path = self._domain_path(theme_title, difficulty, language, theme_description, theme_type=theme_type)
        if not path.exists():
            LOGGER.debug("Theme cache miss: %s", path.name)
            return None

        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            LOGGER.warning("Theme cache read error (%s): %s", path.name, exc)
            return None

        words = [
            ThemeWord(
                word=w["word"],
                clue=w.get("clue", ""),
                source=w.get("source", "gemini"),
            )
            for w in doc.get("words", [])
        ]
        if len(words) < min_words:
            LOGGER.debug(
                "Theme cache hit but too few words (%d < %d): %s",
                len(words), min_words, path.name,
            )
            return None

        LOGGER.info("Theme cache hit: %s (%d words)", path.name, len(words))
        return ThemeOutput(
            words=words,
            crossword_title=doc.get("crossword_title"),
            content=doc.get("content"),
        )

    def save(
        self,
        theme_title: str,
        theme_type: str,
        difficulty: str,
        language: str,
        theme_output: ThemeOutput,
        theme_description: str = "",
    ) -> None:
        """Persist theme output to the cache."""
        now = datetime.now(timezone.utc).isoformat()
        normalized_type = (
            theme_type.value if isinstance(theme_type, ThemeType) else str(theme_type)
        )
        self._save_domain(
            theme_title, difficulty, language, theme_description,
            theme_output, now, theme_type=normalized_type,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save_domain(
        self,
        theme_title: str,
        difficulty: str,
        language: str,
        theme_description: str,
        theme_output: ThemeOutput,
        now: str,
        theme_type: str = ThemeType.DOMAIN_SPECIFIC_WORDS.value,
    ) -> None:
        path = self._domain_path(theme_title, difficulty, language, theme_description, theme_type=theme_type)
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                # Merge: new clue text wins on conflict (prefer fresher LLM output)
                existing: dict[str, dict] = {
                    w["word"].upper(): w for w in doc.get("words", [])
                }
                for tw in theme_output.words:
                    key = tw.word.upper()
                    existing[key] = {"word": tw.word, "clue": tw.clue, "source": tw.source}
                doc["words"] = list(existing.values())
                doc["updated_at"] = now
                if theme_output.crossword_title:
                    doc["crossword_title"] = theme_output.crossword_title
                if theme_output.content:
                    doc["content"] = theme_output.content
                path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
                LOGGER.info(
                    "Theme cache updated: %s (%d words total)",
                    path.name, len(doc["words"]),
                )
                return
            except (json.JSONDecodeError, OSError) as exc:
                LOGGER.warning("Theme cache update failed (%s): %s; recreating", path.name, exc)

        doc = self._make_document(
            theme_title, theme_type, difficulty, language,
            theme_output, created_at=now, updated_at=now,
        )
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Theme cache created: %s", path.name)

    @staticmethod
    def _make_document(
        theme_title: str,
        theme_type: str,
        difficulty: str,
        language: str,
        theme_output: ThemeOutput,
        created_at: str,
        updated_at: str,
    ) -> dict:
        return {
            "theme_title": theme_title,
            "theme_type": theme_type,
            "difficulty": difficulty,
            "language": language,
            "created_at": created_at,
            "updated_at": updated_at,
            "words": [
                {"word": tw.word, "clue": tw.clue, "source": tw.source}
                for tw in theme_output.words
            ],
            "crossword_title": theme_output.crossword_title,
            "content": theme_output.content,
        }

    def cache_id(
        self,
        theme_title: str,
        difficulty: str,
        language: str,
        theme_description: str = "",
        theme_type: str = ThemeType.DOMAIN_SPECIFIC_WORDS.value,
    ) -> str:
        """Return the cache document ID (filename stem) for the given params."""
        return self._domain_path(
            theme_title, difficulty, language, theme_description, theme_type=theme_type
        ).stem

    def _domain_path(
        self,
        theme_title: str,
        difficulty: str,
        language: str,
        theme_description: str = "",
        theme_type: str = ThemeType.DOMAIN_SPECIFIC_WORDS.value,
    ) -> Path:
        norm_title = self._normalize(theme_title)
        norm_desc = self._normalize(theme_description)
        title_slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", norm_title)).strip("_")
        desc_hash = hashlib.md5(norm_desc.encode()).hexdigest()[:8]
        type_slug = re.sub(r"[^a-z0-9]", "_", theme_type.lower()).strip("_")
        filename = f"{type_slug}_{language.lower()}_{difficulty.lower()}_{title_slug}_{desc_hash}.json"
        return self.cache_dir / filename

    @staticmethod
    def _normalize(text: str) -> str:
        """Lowercase, strip Romanian diacritics, collapse whitespace."""
        text = text.lower().strip()
        # Romanian diacritics (lowercase only; uppercase becomes lowercase above)
        for src, dst in [
            ("ă", "a"), ("â", "a"), ("î", "i"),
            ("ș", "s"), ("ş", "s"), ("ț", "t"), ("ţ", "t"),
        ]:
            text = text.replace(src, dst)
        # Collapse internal whitespace
        return re.sub(r"\s+", " ", text).strip()
