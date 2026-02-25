"""Clue generation interfaces."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Protocol

from ..core.constants import CellType
from ..core.models import Clue, WordSlot
from ..utils.logger import get_logger
from .gemini_client import GeminiClient


LOGGER = get_logger(__name__)


@dataclass
class ClueRequest:
    slot_id: str
    word: str
    direction: str
    clue_box: tuple[int, int]


class ClueGenerator(Protocol):
    def generate(self, requests: List[ClueRequest],
                 difficulty: str = "MEDIUM", language: str = "Romanian") -> Dict[str, str]:
        """Return mapping from slot_id to clue text."""


class GeminiClueGenerator:
    """LLM clue generator using Gemini."""

    CLUE_RULES = (
        "You are an expert cryptic crossword clue writer. "
        "Write all clues in {language}. "
        "Mandatory rules for EVERY clue:\n"
        "1. Each clue must contain exactly one DEFINITION and one CRYPTIC MECHANISM.\n"
        "2. The definition must be at the beginning or end of the clue, never in the middle.\n"
        "3. The cryptic mechanism must produce exactly the letters of the solution word.\n"
        "4. The clue must read naturally as a phrase or sentence in {language}.\n"
        "5. Do NOT include the solution word (or obvious fragments) in the clue.\n"
        "6. The clue must be between 3 and 8 words.\n"
        "Respond as a JSON list [{{slot_id, clue}}]."
    )

    CLUE_DIFFICULTY_PROMPT = {
        "EASY": (
            "\nDifficulty: EASY. "
            "Mechanisms: hidden word, double definition, simple anagram only. "
            "Indicators: use transparent keywords. "
            "Definition: direct synonym. "
            "Surface reading: transparent. "
            "Vocabulary: everyday words only."
        ),
        "MEDIUM": (
            "\nDifficulty: MEDIUM. "
            "Mechanisms: anagram, hidden word, double definition, container, reversal, deletion. "
            "Indicators: subtle but recognizable. "
            "Definition: periphrasis acceptable. "
            "Surface reading: smooth misdirection. "
            "Vocabulary: general vocabulary."
        ),
        "HARD": (
            "\nDifficulty: HARD. "
            "Mechanisms: all types including &lit, homophone, compound cryptic. "
            "Indicators: double-meaning words as indicators. "
            "Definition: misleading periphrasis. "
            "Surface reading: deceptive at first glance. "
            "Vocabulary: literary or rare vocabulary."
        ),
    }

    def __init__(
        self,
        model_name: str = "gemini-2.5-flash",
        api_key_env: str = "GEMINI_API_KEY",
        model_env: str = "GEMINI_MODEL",
        gemini_client: GeminiClient | None = None,
    ) -> None:
        resolved_model = os.environ.get(model_env, model_name)
        self.model_name = resolved_model
        self.api_key_env = api_key_env
        self.model_env = model_env
        self._client = gemini_client

    def generate(self, requests: List[ClueRequest],
                 difficulty: str = "MEDIUM", language: str = "Romanian") -> Dict[str, str]:  # pragma: no cover - external
        prompt = self._render_prompt(requests, difficulty, language)
        client = self._client or GeminiClient(
            model_name=self.model_name,
            api_key_env=self.api_key_env,
            model_env=self.model_env,
        )
        self._client = client
        response_text = client.generate_text(prompt)
        return self._parse_response(response_text)

    @classmethod
    def _render_prompt(cls, requests: List[ClueRequest],
                       difficulty: str = "MEDIUM", language: str = "Romanian") -> str:
        payload = [request.__dict__ for request in requests]
        rules = cls.CLUE_RULES.format(language=language)
        diff_key = difficulty.upper() if difficulty else "MEDIUM"
        diff_text = cls.CLUE_DIFFICULTY_PROMPT.get(diff_key, cls.CLUE_DIFFICULTY_PROMPT["MEDIUM"])
        return (
            f"{rules}{diff_text}\n"
            f"Requests: {json.dumps(payload, ensure_ascii=False)}"
        )

    @staticmethod
    def _parse_response(text: str) -> Dict[str, str]:
        if not text:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            LOGGER.warning("Gemini clue payload not JSON; falling back to empty")
            return {}
        result: Dict[str, str] = {}
        for entry in data:
            slot_id = entry.get("slot_id")
            clue = entry.get("clue")
            if slot_id and clue:
                result[slot_id] = clue
        return result


class TemplateClueGenerator:
    """Simple fallback clue writer."""

    def generate(self, requests: List[ClueRequest],
                 difficulty: str = "MEDIUM", language: str = "Romanian") -> Dict[str, str]:
        results = {}
        for req in requests:
            base = req.word.capitalize()
            if req.direction == "ACROSS":
                pattern = f"{base} (oriz.)"
            else:
                pattern = f"{base} (vert.)"
            results[req.slot_id] = pattern
        return results


def attach_clues_to_grid(grid, slots: List[WordSlot], clue_texts: Dict[str, str]) -> None:
    """Populate clue boxes in the grid with the generated clue texts."""

    for row in range(grid.bounds.rows):
        for col in range(grid.bounds.cols):
            cell = grid.cell(row, col)
            if cell.type == CellType.CLUE_BOX:
                cell.clues_hosted.clear()

    for slot in slots:
        clue_box_cell = grid.cell(*slot.clue_box)
        clue_text = clue_texts.get(slot.id, slot.text or "")
        clue = Clue(
            id=f"{slot.id}-clue",
            text=clue_text,
            solution_word_ref_id=slot.id,
            solution_length=slot.length,
            direction=slot.direction,
            start_offset_r=slot.start_row - slot.clue_box[0],
            start_offset_c=slot.start_col - slot.clue_box[1],
        )
        clue_box_cell.clues_hosted.append(clue)
