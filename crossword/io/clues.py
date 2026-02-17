"""Clue generation interfaces."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Protocol

from ..core.constants import CellType
from ..core.models import Clue, WordSlot
from ..utils.logger import get_logger


LOGGER = get_logger(__name__)


@dataclass
class ClueRequest:
    slot_id: str
    word: str
    direction: str
    clue_box: tuple[int, int]


class ClueGenerator(Protocol):
    def generate(self, requests: List[ClueRequest]) -> Dict[str, str]:
        """Return mapping from slot_id to clue text."""


class GeminiClueGenerator:
    """LLM clue generator using Gemini."""

    def __init__(self, model_name: str = "gemini-pro", api_key_env: str = "GOOGLE_API_KEY") -> None:
        self.model_name = model_name
        self.api_key_env = api_key_env

    def generate(self, requests: List[ClueRequest]) -> Dict[str, str]:  # pragma: no cover - external
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Missing Gemini API key in environment variable {self.api_key_env}"
            )
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as exc:
            raise RuntimeError("google-generativeai package required for Gemini integration") from exc

        genai.configure(api_key=api_key)
        prompt = self._render_prompt(requests)
        response = genai.GenerativeModel(self.model_name).generate_content(prompt)
        return self._parse_response(response.text)

    @staticmethod
    def _render_prompt(requests: List[ClueRequest]) -> str:
        payload = [request.__dict__ for request in requests]
        return (
            "Genereaza indicii criptice si directe in romana pentru fiecare intrare. "
            "Raspunde ca lista JSON {slot_id, clue}. "
            f"Solicitari: {json.dumps(payload, ensure_ascii=False)}"
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

    def generate(self, requests: List[ClueRequest]) -> Dict[str, str]:
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
