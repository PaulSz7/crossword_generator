"""Persistent log of all prompts sent to Gemini and their responses.

Each LLM call is saved as a JSON document under its own sub-collection:
  local_db/collections/prompt_log/<request_type>/<id>.json

The system instruction is intentionally omitted — it is static per request
type and not useful to log repeatedly.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..utils.logger import get_logger


LOGGER = get_logger(__name__)

DEFAULT_LOG_DIR = Path("local_db/collections/prompt_log")


class PromptLog:
    """Append-only log of Gemini prompt/response pairs, partitioned by request type."""

    def __init__(self, log_dir: Path | str = DEFAULT_LOG_DIR) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def record(self, request_type: str, prompt: str, response: str) -> str:
        """Save one prompt/response entry under its request-type sub-collection.

        Returns the document ID.
        """
        doc_id = self._new_id()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = {
            "id": doc_id,
            "created_at": now,
            "request_type": request_type,
            "prompt": prompt,
            "response": response,
        }
        collection_dir = self.log_dir / request_type
        collection_dir.mkdir(parents=True, exist_ok=True)
        path = collection_dir / f"{doc_id}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.debug("Prompt logged: %s (%s)", doc_id, request_type)
        return doc_id

    @staticmethod
    def _new_id() -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"{ts}_{short_uuid}"
