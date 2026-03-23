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
from .prompt_log import PromptLog


LOGGER = get_logger(__name__)


def _strip_dex_markup(text: str) -> str:
    """Strip dexonline internalRep markup (@, #, $, [...]) for clean LLM context."""
    text = re.sub(r'\[.*?\]', '', text)   # remove pronunciation notes
    text = re.sub(r'[@#$]', '', text)     # strip marker characters
    return re.sub(r'  +', ' ', text).strip()


@dataclass
class ClueRequest:
    """A request to generate a clue for a single word slot."""
    slot_id: str
    word: str
    direction: str
    clue_box: tuple[int, int]
    definition: Optional[str] = None          # DEX definition for LLM context
    preset_main_clue: Optional[str] = None    # user-provided clue; LLM generates hints only
    sibling_word: Optional[str] = None        # other word sharing the same clue box (if any)


@dataclass
class ClueBundle:
    """Holds all three clue tiers returned by the LLM for one answer."""
    main_clue: str
    hint_1: str
    hint_2: str


_DIFFICULTY_CONTROL: Dict[str, str] = {
    "EASY": (
        "- main_clue: clear, accessible; simple one-to-one synonyms and direct definitions are allowed\n"
        "- hint_1: a straightforward descriptive phrase that makes the answer fairly guessable\n"
        "- hint_2: a clear, helpful explanation that most players can solve immediately\n"
        "- Overall tone: friendly and approachable, minimal misdirection"
    ),
    "MEDIUM": (
        "- main_clue: compact, indirect, but fair; prefer metaphor, wordplay, elliptical phrasing\n"
        "- hint_1: moderately descriptive, narrows possibilities without giving the answer away\n"
        "- hint_2: clearer guidance with enough context for most solvers\n"
        "- Overall tone: clever but not obscure, balanced challenge"
    ),
    "HARD": (
        "- main_clue: oblique, playful, and misleading while still solvable; avoid simple synonyms\n"
        "- hint_1: subtly narrows the field but still requires lateral thinking\n"
        "- hint_2: provides semantic guidance but through indirect or literary language\n"
        "- Overall tone: deceptive, requires deep vocabulary or lateral thinking"
    ),
}

_BEST_CANDIDATE_NOTE: Dict[str, str] = {
    "EASY": "Prefer clear, accessible candidates; simple synonyms are acceptable.",
    "MEDIUM": "Reject weak candidates such as direct definitions, simple synonyms, repetitive structures, or overly descriptive clues.",
    "HARD": "Reject weak candidates such as direct definitions, simple synonyms, or repetitive structures. Prefer oblique, lateral-thinking angles.",
}

_DEFINITION_STYLE_RULE: Dict[str, str] = {
    "EASY": "Dictionary-style definitions are acceptable.",
    "MEDIUM": "Avoid dictionary-style definitions.",
    "HARD": "Avoid dictionary-style definitions.",
}


_SYSTEM_INSTRUCTION_TEMPLATE = """\
You are a professional crossword constructor specialized in compact integrame-style clues.

Your task is to generate high-quality crossword clues and progressive hints for a provided list of answer words.

You must follow all rules below exactly.

ACTIVE DIFFICULTY: {difficulty}
All difficulty-dependent rules and style choices in this instruction apply at the {difficulty} level.

GENERAL BEHAVIOR
- Use strictly and only the requested language.
- Do not include words from any other language.
- Return only JSON matching the provided response schema.
- Do not output explanations, reasoning, comments, markdown, or extra text.
- Do not reveal internal analysis.

{language_upper} WORDS RULE
All answer words are {language} words from the {language} dictionary.
Even if an answer looks identical to a word in another language, treat it EXCLUSIVELY as a {language} word.
Never interpret an answer by its meaning in another language.
Generate clues that reflect the word's meaning and usage in {language} only.

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
- Maximum 4 words; strongly prefer 1–2 words
- Extremely compact
- Suitable for a very small crossword clue cell

HINT_1 REQUIREMENTS
- Exactly one full phrase (one sentence, no sentence-ending punctuation mid-text)
- Slightly more descriptive than main_clue
- Helps narrow possibilities

HINT_2 REQUIREMENTS
- Maximum 2 full phrases (one or two sentences)
- Gives stronger semantic guidance
- Should help most players infer the answer

STRICT CLUE RESTRICTIONS
For all clue fields:
- Do NOT include the answer, any inflected form, derivative, obvious lexical relative, or visible substring fragment of the answer
- Example violation: answer POLIPIER — "polipi" appears in the clue "Colonie de polipi." because "polip" is a root of "POLIPIER". This is forbidden even for inflected forms of the root.
- Use synonyms, metaphors, contextual associations, cultural references, abbreviations, initials, letter riddles, indirect descriptions, or playful mixed meanings
- {definition_style_rule}
- Avoid overly obvious clues
- Avoid sensitive political, religious, hateful, sexual, or defamatory framing
- Avoid repetitive wording or repeated clue structures across the list
- PUNCTUATION: Use no punctuation at all unless strictly necessary. Ellipsis (...) is allowed only when intentional trailing ambiguity is needed. Exclamation mark (!) is allowed ONLY when the clue is a riddle, initials expansion, letter extraction, or abbreviation-logic clue. All other punctuation — periods, commas, semicolons, colons, question marks, hyphens, dashes, parentheses, quotation marks — is forbidden.

RIDDLE / ABBREVIATION STYLE RULE
main_clue may use playful crossword-riddle logic for short words. Use this style only when it fits naturally — it is a stylistic choice, never a requirement.

WHEN TO USE RIDDLE STYLE:
- Only for words of {riddle_max_letters} letters or fewer (active difficulty: {difficulty})
- Do NOT use riddle style for longer words
- Only use it when the riddle genuinely works and is solvable — skip it if no clean riddle angle exists and provide normal difficulty-controlled clue

RIDDLE TECHNIQUES (pick one per clue):
- Initials expansion: each letter of the answer is the first letter of a word in a name (person, celebrity, company, etc.)
- Letter extraction (start): the answer letters open another word
- Letter extraction (end): the answer letters close another word
- Letter extraction (hidden): the answer letters are embedded inside another word
- Phrase decomposition: a familiar word or phrase, read differently, yields the answer letters

ROMANIAN RIDDLE EXAMPLES:
- Answer MP → "Mihai Popescu" — M and P are the initials of this name
- Answer EPU → "Început!" — EPU is hidden inside "ceput" (în c·e·p·u·t)
- Answer RA → "Final de aurora!" — RA closes the word "aurora" (auro·r·a)
- Answer CAP → "Debut de capital!" — CAP opens the word "capital"

If main_clue uses this riddle/abbreviation style, it MUST end with an exclamation mark.
If main_clue is descriptive, synonymic, or definition-style, it MUST NOT end with an exclamation mark — regardless of word length.
Example violation: "Fruct mic!" is forbidden because "fruct mic" is a descriptive phrase, not a riddle.
Be creative beyond the examples above. Every riddle clue must be genuinely solvable and make logical sense.

FOREIGN ABBREVIATIONS
If the answer is an abbreviation that originates from a foreign language (e.g. BC = Before Christ in English), the clue MUST acknowledge the foreign origin.
Use a formulation in {language} that names the source language (e.g. "abbreviation from English", translated appropriately).
Do NOT expand a foreign abbreviation as if it were a native {language} abbreviation.

THEME INTEGRATION
- When natural, connect clues subtly to the provided theme.
- Do not force the theme if it makes the clue awkward or obvious.

DIFFICULTY CONTROL
Adjust ALL clue fields (main_clue, hint_1, hint_2) to the {difficulty} level:
{difficulty_control}

INTERNAL BEST-CANDIDATE SELECTION
For each answer:
- Generate multiple possible main clue angles internally
- Compare them for originality, brevity, cleverness, fairness, and non-obviousness
- {best_candidate_note}
- Select the strongest candidate
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

SIBLING ENTRIES (same start position)
Some pairs of entries share the same starting cell and therefore the same clue box.
For each such pair the prompt lists: PRIMARY: X   SIBLING: Y
The PRIMARY entry's main_clue must work as a useful hint for BOTH words simultaneously.
Ideal shared clues name a common category, concept, or wordplay angle that applies to both answers (e.g. "Simbol feminin" works for both EA and EVA).
The SIBLING entry receives its own independent clue as normal — only the PRIMARY's main_clue must serve double duty.

SELF-VALIDATION
Before returning the final response, verify internally that:
- every answer has exactly one main_clue, one hint_1, and one hint_2
- main_clue has at most 4 words
- hint_1 is exactly one full phrase (one sentence)
- hint_2 is at most 2 full phrases (one or two sentences)
- no clue contains the answer
- no clue contains any inflected form, derivative, obvious lexical relative, or visible substring fragment of the answer
- all clue text is strictly in the requested language
- every clue matches the grammatical form of its answer (number, gender, verb form)
- clue style strictly matches {difficulty} guidelines
- clue wording is not duplicated excessively across entries
- exclamation mark (!) is ONLY used when the clue is a genuine riddle, initials expansion, letter extraction, or abbreviation-logic clue — never for descriptive or synonymic clues
- riddle style is only used for words of ≤ {riddle_max_letters} letters (active difficulty: {difficulty}), and only when the riddle genuinely works
- no other punctuation is used unless strictly necessary (ellipsis only for intentional ambiguity)
- the response matches the required JSON schema

If any rule fails, revise internally until the output is valid.

GRAMMATICAL AGREEMENT (applies to every word, with or without a definition)
Every clue must match the exact grammatical form of the answer word as it appears in the crossword:
- If the answer is plural, the clue must be plural.
- If the answer is singular, the clue must be singular.
- Respect gender (masculine / feminine / neuter) in the clue phrasing.
- Respect verb form if the answer is a verb (infinitive, conjugated form, etc.).
Use your knowledge of {language} to determine the correct grammatical form of every answer word.

DEFINITIONS (provided for some words)
When a definition is available, use it as a semantic anchor to confirm the exact meaning and grammatical form of the word — but do NOT reproduce it verbatim.
Definitions may contain part-of-speech markers — use them to identify the word's grammatical category:
- s. m. / s. f. / s. n. = noun (masculine / feminine / neuter)
- vb. / v. = verb
- adj. = adjective
- adv. = adverb
- interj. = interjection
- prep. = preposition
These markers help confirm the form inferred from the word itself. When a definition is not provided, rely solely on your knowledge of {language}.

PRESET MAIN CLUES
For words marked with a preset_main_clue, echo that value exactly in the `main_clue` field. Generate only `hint_1` and `hint_2` for these words."""


def _build_system_instruction(language: str, difficulty: str = "MEDIUM") -> str:
    """Return the system instruction with language- and difficulty-specific sections filled in."""
    diff_upper = (difficulty or "MEDIUM").upper()
    riddle_max_letters = 4 if diff_upper == "HARD" else 3
    extra_riddle_example = (
        "\n- Answer LUNA → \"Deschide 'lunatic'!\" — LUNA opens the word \"lunatic\""
        if riddle_max_letters >= 4 else ""
    )
    return _SYSTEM_INSTRUCTION_TEMPLATE.format(
        language=language,
        language_upper=language.upper(),
        difficulty=diff_upper,
        riddle_max_letters=riddle_max_letters,
        extra_riddle_example=extra_riddle_example,
        difficulty_control=_DIFFICULTY_CONTROL[diff_upper],
        best_candidate_note=_BEST_CANDIDATE_NOTE[diff_upper],
        definition_style_rule=_DEFINITION_STYLE_RULE[diff_upper],
    )


MAIN_PROMPT_TEMPLATE = """\
Generate crossword clues for all provided words.

LANGUAGE: {{LANGUAGE}}
THEME: {{THEME}}
DIFFICULTY: {{DIFFICULTY}}

WORD LIST (JSON array):
{{WORD_LIST_JSON}}

{{DEFINITIONS_SECTION}}{{PRESET_CLUES_SECTION}}{{SIBLING_ENTRIES_SECTION}}Requirements:
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
        prompt_log: PromptLog | None = None,
    ) -> None:
        """Initialize with optional pre-built client."""
        resolved_model = os.environ.get(model_env, model_name)
        self.model_name = resolved_model
        self.api_key_env = api_key_env
        self.model_env = model_env
        self._client = gemini_client
        self._prompt_log = prompt_log

    def _get_client(self) -> GeminiClient:
        """Return the cached client or create a new one."""
        if self._client is None:
            self._client = GeminiClient(
                model_name=self.model_name,
                api_key_env=self.api_key_env,
                model_env=self.model_env,
                prompt_log=self._prompt_log,
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
        prompt = self._render_prompt(word_list, difficulty, language, theme, requests)
        system_instruction = _build_system_instruction(language, difficulty)

        client = self._get_client()
        response_text = client.generate_text(
            prompt,
            system_instruction=system_instruction,
            response_schema=CLUE_RESPONSE_SCHEMA,
            request_type="clue_generation",
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
                system_instruction=system_instruction,
                response_schema=CLUE_RESPONSE_SCHEMA,
                request_type="clue_repair",
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

        # Build preset_main_clue lookup: word_upper → preset clue
        preset_clues: Dict[str, str] = {}
        for req in requests:
            if req.preset_main_clue:
                preset_clues[req.word.upper()] = req.preset_main_clue

        # Map answer → ClueBundle → slot_id → ClueBundle
        result: Dict[str, ClueBundle] = {}
        for entry in valid:
            answer = entry["answer"].upper()
            preset = preset_clues.get(answer)
            bundle = ClueBundle(
                main_clue=preset if preset else entry["main_clue"],
                hint_1=entry["hint_1"],
                hint_2=entry["hint_2"],
            )
            for slot_id in answer_to_slots.get(answer, []):
                result[slot_id] = bundle

        return result

    @staticmethod
    def _render_prompt(word_list: List[str], difficulty: str,
                       language: str, theme: str,
                       requests: Optional[List[ClueRequest]] = None) -> str:
        """Fill the main prompt template with concrete values."""
        word_list_json = json.dumps(word_list, ensure_ascii=False)

        definitions_section = ""
        preset_clues_section = ""

        if requests:
            defs = [(req.word.upper(), _strip_dex_markup(req.definition))
                    for req in requests if req.definition]
            if defs:
                lines = ["WORD DEFINITIONS (reference context only — do not copy verbatim):"]
                for word, defn in defs:
                    lines.append(f"{word}: {defn}")
                definitions_section = "\n".join(lines) + "\n\n"

            presets = [(req.word.upper(), req.preset_main_clue) for req in requests if req.preset_main_clue]
            if presets:
                lines = ["PRESET MAIN CLUES (echo exactly in main_clue field, generate only hint_1 and hint_2):"]
                for word, preset in presets:
                    lines.append(f"{word}: {preset}")
                preset_clues_section = "\n".join(lines) + "\n\n"

        sibling_section = ""
        if requests:
            # Build direction lookup for primary/sibling designation
            word_to_direction = {req.word.upper(): req.direction for req in requests}

            seen_pairs: set = set()
            sibling_lines: list = []
            for req in requests:
                if not req.sibling_word:
                    continue
                word, sib = req.word.upper(), req.sibling_word.upper()
                pair_key = frozenset((word, sib))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                # Primary = ACROSS entry (displayed first in the shared box);
                # fall back to alphabetical order when both have the same direction.
                w_dir = word_to_direction.get(word, "")
                s_dir = word_to_direction.get(sib, "")
                if w_dir == "ACROSS" and s_dir != "ACROSS":
                    primary, secondary = word, sib
                elif s_dir == "ACROSS" and w_dir != "ACROSS":
                    primary, secondary = sib, word
                else:
                    primary, secondary = sorted([word, sib])

                sibling_lines.append(
                    f"  PRIMARY: {primary}   SIBLING: {secondary}"
                    f"  (write {primary}'s main_clue so it works as a hint for both words)"
                )

            if sibling_lines:
                sibling_section = (
                    "SIBLING ENTRIES (same start position — shared clue box):\n"
                    + "\n".join(sibling_lines)
                    + "\n\n"
                )

        return (
            MAIN_PROMPT_TEMPLATE
            .replace("{{LANGUAGE}}", language)
            .replace("{{THEME}}", theme or "(no theme)")
            .replace("{{DIFFICULTY}}", difficulty.upper() if difficulty else "MEDIUM")
            .replace("{{WORD_LIST_JSON}}", word_list_json)
            .replace("{{DEFINITIONS_SECTION}}", definitions_section)
            .replace("{{PRESET_CLUES_SECTION}}", preset_clues_section)
            .replace("{{SIBLING_ENTRIES_SECTION}}", sibling_section)
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
    severe_keywords = ("missing required", "unexpected answer", "contains the answer", "contains answer fragment")
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
        # Riddle-style main_clue (ends with !) intentionally contains answer letters — skip checks for it.
        is_riddle = main_clue.rstrip().endswith("!")
        clue_fields = [("main_clue", main_clue), ("hint_1", hint_1), ("hint_2", hint_2)]

        if answer_lower and len(answer_lower) >= 2:
            for field_name, field_val in clue_fields:
                # Use word-boundary matching: ignore the answer appearing as part of an unrelated word.
                # Riddle clues (main_clue ending with !) are NOT exempt here — the clue must not
                # literally spell out the answer as a standalone word even in riddle style.
                if re.search(r'\b' + re.escape(answer_lower) + r'\b', field_val.lower()):
                    all_violations.append(f"{field_name} contains the answer '{answer}'")

        # Check for long substrings of the answer appearing as standalone words in any clue field
        # (catches derivative forms like "polipi" for POLIPIER, but not coincidental embeddings)
        if answer_lower and len(answer_lower) >= 6:
            for sub_len in range(5, len(answer_lower)):
                for start in range(len(answer_lower) - sub_len + 1):
                    substr = answer_lower[start:start + sub_len]
                    for field_name, field_val in clue_fields:
                        if field_name == "main_clue" and is_riddle:
                            continue
                        if re.search(r'\b' + re.escape(substr) + r'\b', field_val.lower()):
                            all_violations.append(
                                f"{field_name} contains answer fragment '{substr}' (from '{answer}')"
                            )

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
