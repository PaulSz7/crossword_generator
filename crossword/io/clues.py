"""Clue generation interfaces with structured LLM prompting and validation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Set, Tuple

from ..core.constants import CellType
from ..core.models import Clue, WordSlot
from ..utils.logger import get_logger
from .gemini_client import GeminiClient


LOGGER = get_logger(__name__)


@dataclass
class ClueRequest:
    """A request to generate a clue for a single word slot."""
    slot_id: str
    word: str
    direction: str
    clue_box: tuple[int, int]


@dataclass
class ClueBundle:
    """Holds all three clue tiers returned by the LLM for one answer."""
    main_clue: str
    hint_1: str
    hint_2: str


SYSTEM_INSTRUCTION = """\
You are a professional crossword constructor specialized in compact integrame-style clues.

Your task is to generate high-quality crossword clues and progressive hints for a provided list of answer words.

You must follow all rules below exactly.

GENERAL BEHAVIOR
- Use strictly and only the requested language.
- Do not include words from any other language.
- Return only JSON matching the provided response schema.
- Do not output explanations, reasoning, comments, markdown, or extra text.
- Do not reveal internal analysis.

SAFETY CHECK
Before generating clues, inspect all provided answer words.
If ANY answer word is:
- obscene
- sexually explicit
- racist
- discriminatory
- extremist
- hate-related
- defamatory
- unsafe for general audiences

then do not generate clues.
Instead, return an error response matching the schema with:
- status = "error"
- reason = "Content violates safety policy"
- invalid_words = list of offending words

CLUE GENERATION TASK
For every valid answer word, generate exactly three fields:
- main_clue
- hint_1
- hint_2

MAIN_CLUE REQUIREMENTS
- Maximum 4 words
- Extremely compact
- Suitable for a very small crossword clue cell
- Style varies by difficulty (see DIFFICULTY CONTROL)

HINT_1 REQUIREMENTS
- Exactly one full phrase (one sentence, no sentence-ending punctuation mid-text)
- Slightly more descriptive than main_clue
- Helps narrow possibilities
- Style varies by difficulty (see DIFFICULTY CONTROL)

HINT_2 REQUIREMENTS
- Maximum 2 full phrases (one or two sentences)
- Gives stronger semantic guidance
- Should help most players infer the answer
- Style varies by difficulty (see DIFFICULTY CONTROL)

STRICT CLUE RESTRICTIONS
For all clue fields:
- Do NOT include the answer, any inflected form, derivative, obvious lexical relative, or visible substring fragment of the answer
- Use synonyms, metaphors, contextual associations, cultural references, abbreviations, initials, letter riddles, indirect descriptions, or playful mixed meanings
- Avoid dictionary-style definitions (except where allowed by EASY difficulty)
- Avoid overly obvious clues
- Avoid sensitive political, religious, hateful, sexual, or defamatory framing
- Avoid repetitive wording or repeated clue structures across the list

SHORT WORD SPECIAL RULE
For answers of length 2 or 3, especially abbreviations or compact entries, main_clue may use playful crossword-riddle logic such as:
- initials
- abbreviations
- letter extraction
- compact language riddles
- playful orthographic or phrase decomposition

Examples of allowed styles:
- name initials
- institutional abbreviations
- playful letter riddles

If main_clue uses this riddle/abbreviation style, it must end with an exclamation mark.
Be creative and do not limit yourself to the examples above.

THEME INTEGRATION
- When natural, connect clues subtly to the provided theme.
- Do not force the theme if it makes the clue awkward or obvious.

DIFFICULTY CONTROL
Adjust ALL clue fields (main_clue, hint_1, hint_2) to the requested difficulty:

EASY:
- main_clue: clear, accessible; simple one-to-one synonyms and direct definitions are allowed
- hint_1: a straightforward descriptive phrase that makes the answer fairly guessable
- hint_2: a clear, helpful explanation that most players can solve immediately
- Overall tone: friendly and approachable, minimal misdirection

MEDIUM:
- main_clue: compact, indirect, but fair; prefer metaphor, wordplay, elliptical phrasing
- hint_1: moderately descriptive, narrows possibilities without giving the answer away
- hint_2: clearer guidance with enough context for most solvers
- Overall tone: clever but not obscure, balanced challenge

HARD:
- main_clue: oblique, playful, and misleading while still solvable; avoid simple synonyms
- hint_1: subtly narrows the field but still requires lateral thinking
- hint_2: provides semantic guidance but through indirect or literary language
- Overall tone: deceptive, requires deep vocabulary or lateral thinking

INTERNAL BEST-CANDIDATE SELECTION
For each answer:
- Generate multiple possible main clue angles internally
- Compare them for originality, brevity, cleverness, fairness, and non-obviousness
- For MEDIUM and HARD: reject weak candidates such as direct definitions, simple synonyms, repetitive structures, or overly descriptive clues
- For EASY: prefer clear, accessible candidates; simple synonyms are acceptable
- Select the strongest candidate for the requested difficulty
- Do not output candidates or reasoning

STYLE VARIATION
Across the full word list, vary clue style where possible.
Distribute different styles such as:
- metaphor
- cultural reference
- wordplay
- elliptical noun phrase
- ironic hint
- contextual association
- abbreviation logic
- letter riddle

SELF-VALIDATION
Before returning the final response, verify internally that:
- every answer has exactly one main_clue, one hint_1, and one hint_2
- main_clue has at most 4 words
- hint_1 is exactly one full phrase (one sentence)
- hint_2 is at most 2 full phrases (one or two sentences)
- no clue contains the answer
- no clue contains any inflected form, derivative, obvious lexical relative, or visible substring fragment of the answer
- all clue text is strictly in the requested language
- clue style matches the requested difficulty level
- clue wording is not duplicated excessively across entries
- the response matches the required JSON schema

If any rule fails, revise internally until the output is valid."""

MAIN_PROMPT_TEMPLATE = """\
Generate crossword clues for all provided words.

LANGUAGE: {{LANGUAGE}}
THEME: {{THEME}}
DIFFICULTY: {{DIFFICULTY}}

WORD LIST (JSON array):
{{WORD_LIST_JSON}}

Requirements:
- Return one clue object for each input word.
- Preserve the original answer exactly in the "answer" field.
- Keep the same order as the input list.
- Apply the requested difficulty.
- Integrate the theme subtly when natural.
- Use only the requested language in all clue fields.
- Return only valid JSON matching the response schema."""

CLUE_RESPONSE_SCHEMA: Dict = {
    "type": "OBJECT",
    "properties": {
        "status": {
            "type": "STRING",
            "enum": ["ok", "error"],
        },
        "clues": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "answer": {"type": "STRING"},
                    "main_clue": {"type": "STRING"},
                    "hint_1": {"type": "STRING"},
                    "hint_2": {"type": "STRING"},
                },
                "required": ["answer", "main_clue", "hint_1", "hint_2"],
            },
        },
        "error": {
            "type": "OBJECT",
            "properties": {
                "reason": {"type": "STRING"},
                "invalid_words": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                },
            },
        },
    },
    "required": ["status"],
}


class ClueGenerator(Protocol):
    """Protocol for clue generation backends."""
    def generate(self, requests: List[ClueRequest],
                 difficulty: str = "MEDIUM", language: str = "Romanian",
                 theme: str = "") -> Dict[str, ClueBundle]:
        """Return mapping from slot_id to ClueBundle."""


class GeminiClueGenerator:
    """LLM clue generator using Gemini with structured output and validation."""

    def __init__(
        self,
        model_name: str = "gemini-2.5-flash",
        api_key_env: str = "GEMINI_API_KEY",
        model_env: str = "GEMINI_MODEL",
        gemini_client: GeminiClient | None = None,
    ) -> None:
        """Initialize with optional pre-built client."""
        resolved_model = os.environ.get(model_env, model_name)
        self.model_name = resolved_model
        self.api_key_env = api_key_env
        self.model_env = model_env
        self._client = gemini_client

    def _get_client(self) -> GeminiClient:
        """Return the cached client or create a new one."""
        if self._client is None:
            self._client = GeminiClient(
                model_name=self.model_name,
                api_key_env=self.api_key_env,
                model_env=self.model_env,
            )
        return self._client

    def generate(self, requests: List[ClueRequest],
                 difficulty: str = "MEDIUM", language: str = "Romanian",
                 theme: str = "") -> Dict[str, ClueBundle]:  # pragma: no cover - external
        """Generate clue bundles for all requests via Gemini."""
        if not requests:
            return {}

        # Build answer → [slot_id, ...] reverse map
        answer_to_slots: Dict[str, List[str]] = {}
        for req in requests:
            word_upper = req.word.upper()
            answer_to_slots.setdefault(word_upper, []).append(req.slot_id)

        word_list = list(answer_to_slots.keys())
        prompt = self._render_prompt(word_list, difficulty, language, theme)

        client = self._get_client()
        response_text = client.generate_text(
            prompt,
            system_instruction=SYSTEM_INSTRUCTION,
            response_schema=CLUE_RESPONSE_SCHEMA,
        )

        parsed = self._parse_response(response_text)
        if parsed is None:
            return {}

        # Validate and repair only for severe violations
        valid, needs_repair = _validate_clues(parsed, set(word_list))
        LOGGER.info(
            "Clue validation: %d/%d entries valid, %d need repair",
            len(valid), len(parsed), len(needs_repair),
        )
        if needs_repair:
            LOGGER.warning(
                "Sending repair request for %d clue entries: %s",
                len(needs_repair),
                ", ".join(e.get("answer", "?") for e in needs_repair),
            )
            repair_prompt = _build_repair_prompt(needs_repair, language, theme, difficulty)
            repair_text = client.generate_text(
                repair_prompt,
                system_instruction=SYSTEM_INSTRUCTION,
                response_schema=CLUE_RESPONSE_SCHEMA,
            )
            repaired = self._parse_response(repair_text)
            if repaired is not None:
                repaired_valid, still_invalid = _validate_clues(repaired, set(word_list))
                valid.extend(repaired_valid)
                if still_invalid:
                    LOGGER.warning(
                        "After repair, %d clue entries still invalid (dropping): %s",
                        len(still_invalid),
                        ", ".join(e.get("answer", "?") for e in still_invalid),
                    )

        # Map answer → ClueBundle → slot_id → ClueBundle
        result: Dict[str, ClueBundle] = {}
        for entry in valid:
            answer = entry["answer"].upper()
            bundle = ClueBundle(
                main_clue=entry["main_clue"],
                hint_1=entry["hint_1"],
                hint_2=entry["hint_2"],
            )
            for slot_id in answer_to_slots.get(answer, []):
                result[slot_id] = bundle

        return result

    @staticmethod
    def _render_prompt(word_list: List[str], difficulty: str,
                       language: str, theme: str) -> str:
        """Fill the main prompt template with concrete values."""
        word_list_json = json.dumps(word_list, ensure_ascii=False)
        return (
            MAIN_PROMPT_TEMPLATE
            .replace("{{LANGUAGE}}", language)
            .replace("{{THEME}}", theme or "(no theme)")
            .replace("{{DIFFICULTY}}", difficulty.upper() if difficulty else "MEDIUM")
            .replace("{{WORD_LIST_JSON}}", word_list_json)
        )

    @staticmethod
    def _parse_response(text: str) -> Optional[List[Dict]]:
        """Parse a Gemini JSON response, returning the clue list or None."""
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            LOGGER.warning("Gemini clue response not valid JSON; falling back to empty")
            return None

        status = data.get("status", "ok")
        if status == "error":
            error_info = data.get("error", {})
            reason = error_info.get("reason", "unknown")
            invalid_words = error_info.get("invalid_words", [])
            LOGGER.warning("Gemini safety error: %s (words: %s)", reason, invalid_words)
            return None

        return data.get("clues") or []


def _count_sentences(text: str) -> int:
    """Count sentences by splitting on sentence-ending punctuation (.!?)."""
    if not text.strip():
        return 0
    # Split on sentence terminators followed by whitespace or end-of-string
    parts = re.split(r'[.!?]+(?:\s|$)', text.strip())
    # Filter out empty trailing parts from the split
    return len([p for p in parts if p.strip()])


def _is_severe_clue_violation(violation: str) -> bool:
    """Return True for violations that require a repair call (vs cosmetic issues that are accepted)."""
    severe_keywords = ("missing required", "unexpected answer", "contains the answer")
    return any(kw in violation for kw in severe_keywords)


def _validate_clues(
    clues: List[Dict], expected_words: Set[str],
) -> Tuple[List[Dict], List[Dict]]:
    """Split clue entries into valid, cosmetic-only, and severe-invalid lists.

    Entries with only cosmetic violations (word/sentence counts slightly exceeded)
    are accepted as valid. Only entries with severe violations (missing fields,
    answer leaking, unexpected answer) are flagged for repair.

    Returns (valid, needs_repair) where each needs_repair entry has a ``violations`` key.
    """
    valid: List[Dict] = []
    needs_repair: List[Dict] = []

    for entry in clues:
        all_violations: List[str] = []
        answer = entry.get("answer", "")
        main_clue = entry.get("main_clue", "")
        hint_1 = entry.get("hint_1", "")
        hint_2 = entry.get("hint_2", "")

        if not answer or not main_clue or not hint_1 or not hint_2:
            all_violations.append("missing required field(s)")

        if answer.upper() not in expected_words:
            all_violations.append(f"unexpected answer '{answer}'")

        if len(main_clue.split()) > 4:
            all_violations.append(f"main_clue exceeds 4 words ({len(main_clue.split())} words)")

        hint_1_sentences = _count_sentences(hint_1)
        if hint_1_sentences > 1:
            all_violations.append(f"hint_1 must be one phrase but has {hint_1_sentences} sentences")

        hint_2_sentences = _count_sentences(hint_2)
        if hint_2_sentences > 2:
            all_violations.append(f"hint_2 exceeds 2 phrases ({hint_2_sentences} sentences)")

        answer_lower = answer.lower()
        if answer_lower and len(answer_lower) >= 2:
            for field_name, field_val in [("main_clue", main_clue), ("hint_1", hint_1), ("hint_2", hint_2)]:
                if answer_lower in field_val.lower():
                    all_violations.append(f"{field_name} contains the answer '{answer}'")

        if not all_violations:
            valid.append(entry)
        elif any(_is_severe_clue_violation(v) for v in all_violations):
            entry_copy = dict(entry)
            entry_copy["violations"] = all_violations
            needs_repair.append(entry_copy)
            LOGGER.warning(
                "Clue entry requires repair — answer: %r | violations: %s",
                answer, "; ".join(all_violations),
            )
        else:
            # Cosmetic violations only (word/sentence count) — accept and log
            LOGGER.warning(
                "Clue entry has minor violations (accepted) — answer: %r | violations: %s",
                answer, "; ".join(all_violations),
            )
            valid.append(entry)

    return valid, needs_repair


def _build_repair_prompt(
    invalid_entries: List[Dict], language: str, theme: str, difficulty: str,
) -> str:
    """Build a repair prompt listing only the invalid entries and their violations."""
    lines = ["The following clue entries have validation errors. Please fix them.\n"]
    lines.append(f"LANGUAGE: {language}")
    lines.append(f"THEME: {theme or '(no theme)'}")
    lines.append(f"DIFFICULTY: {difficulty.upper() if difficulty else 'MEDIUM'}\n")

    for entry in invalid_entries:
        answer = entry.get("answer", "???")
        violations = entry.get("violations", [])
        lines.append(f"ANSWER: {answer}")
        for v in violations:
            lines.append(f"  - VIOLATION: {v}")
        lines.append("")

    lines.append("Requirements:")
    lines.append("- Fix all listed violations.")
    lines.append("- Return corrected clue objects for these words only.")
    lines.append("- Follow all original rules from the system instruction.")
    lines.append("- Return only valid JSON matching the response schema.")
    return "\n".join(lines)


class TemplateClueGenerator:
    """Simple fallback clue writer that produces template-based clues."""

    def generate(self, requests: List[ClueRequest],
                 difficulty: str = "MEDIUM", language: str = "Romanian",
                 theme: str = "") -> Dict[str, ClueBundle]:
        """Return template-based ClueBundle for each request."""
        results: Dict[str, ClueBundle] = {}
        for req in requests:
            base = req.word.capitalize()
            if req.direction == "ACROSS":
                text = f"{base} (oriz.)"
            else:
                text = f"{base} (vert.)"
            results[req.slot_id] = ClueBundle(main_clue=text, hint_1="", hint_2="")
        return results


def attach_clues_to_grid(grid, slots: List[WordSlot], clue_bundles: Dict[str, ClueBundle]) -> None:
    """Populate clue boxes in the grid with the generated clue bundles."""

    for row in range(grid.bounds.rows):
        for col in range(grid.bounds.cols):
            cell = grid.cell(row, col)
            if cell.type == CellType.CLUE_BOX:
                cell.clues_hosted.clear()

    for slot in slots:
        clue_box_cell = grid.cell(*slot.clue_box)
        bundle = clue_bundles.get(slot.id)
        clue = Clue(
            id=f"{slot.id}-clue",
            text=bundle.main_clue if bundle else slot.text or "",
            solution_word_ref_id=slot.id,
            solution_length=slot.length,
            direction=slot.direction,
            start_offset_r=slot.start_row - slot.clue_box[0],
            start_offset_c=slot.start_col - slot.clue_box[1],
            hint_1=bundle.hint_1 if bundle else "",
            hint_2=bundle.hint_2 if bundle else "",
        )
        clue_box_cell.clues_hosted.append(clue)
