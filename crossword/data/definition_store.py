"""Persistent store for word definitions fetched from dexonline.ro."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_STORE_DIR = Path("local_db/collections/word_definitions")


class DefinitionStore:
    """Caches word definitions fetched from external sources (one JSON file per word)."""

    def __init__(self, store_dir: Path = DEFAULT_STORE_DIR) -> None:
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def get(self, word: str) -> Optional[str]:
        """Return cached definition string or None if not stored."""
        path = self.store_dir / f"{word.upper()}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")).get("definition")
        return None

    def save(self, word: str, definition: str, source: str = "dexonline_fetched") -> None:
        """Persist a definition record to disk."""
        doc = {
            "word": word.upper(),
            "definition": definition,
            "source": source,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        path = self.store_dir / f"{word.upper()}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
