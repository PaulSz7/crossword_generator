"""Theme word generation interfaces."""

from __future__ import annotations

import copy
import json
import os
import re
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from ..core.constants import Difficulty
from ..io.gemini_client import GeminiClient
from ..io.prompt_log import PromptLog
from ..utils.logger import get_logger

if TYPE_CHECKING:
    from .dictionary import WordDictionary
    from .theme_cache import ThemeCache


LOGGER = get_logger(__name__)


class ThemeType(str, Enum):
    DOMAIN_SPECIFIC_WORDS = "domain_specific_words"
    WORDS_CONTAINING_SUBSTRING = "words_containing_substring"
    JOKE_CONTINUATION = "joke_continuation"
    CUSTOM = "custom"


@dataclass
class ThemeWord:
    """Represents a theme-driven seed word and its clue bundle."""

    word: str
    clue: str  # ultra-short clue (1-3 words)
    source: str = "unknown"
    long_clue: str = ""
    hint: str = ""
    has_user_clue: bool = False


@dataclass
class ThemeOutput:
    """Wraps theme word generation results with optional metadata."""

    words: List[ThemeWord] = field(default_factory=list)
    crossword_title: Optional[str] = None
    content: Optional[str] = None  # hint text for blocker zone in UI


class ThemeWordGenerator(Protocol):
    """Protocol implemented by all theme word providers."""

    def generate(
        self, theme: str, limit: int = 80,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> ThemeOutput:
        ...


# ---------------------------------------------------------------------------
# Gemini Theme Word Generator — constants
# ---------------------------------------------------------------------------

THEME_SYSTEM_INSTRUCTION = """\
You are a professional crossword theme designer specialized in Romanian integrame-style puzzles.

Your task is to generate high-quality theme words with compact clues, full clues, and progressive hints for a crossword puzzle.

You must follow all rules below exactly.

GENERAL BEHAVIOR
- Use strictly and only the requested language for all text fields.
- Do not include words from any other language.
- Return only JSON matching the provided response schema.
- Do not output explanations, reasoning, comments, markdown, or extra text.
- Do not reveal internal analysis.

SAFETY CHECK
Before generating words, inspect the theme title and description.
If the theme is:
- obscene
- sexually explicit
- racist
- discriminatory
- extremist
- hate-related
- defamatory
- unsafe for general audiences

then do not generate words.
Instead, return a response with:
- status = "error"
- No words array

WORD GENERATION REQUIREMENTS
- CRITICAL: Every word MUST be a real word in the requested language. Do NOT generate words from other languages under any circumstances. English, French, Latin, or any other foreign words are strictly forbidden unless they have been fully adopted into the requested language's standard dictionary.
- CRITICAL: Every word MUST be unique. Do NOT repeat any word in the response, even with different casing or diacritics. Duplicates will be rejected.
- All answer words must be uppercase ASCII letters A-Z only.
- Normalize Romanian diacritics: replace ă/â with A, î with I, ș with S, ț with T.
- Every word must contain at least 2 letters.
- All words must be strongly tied to the requested theme.
- Words must be suitable for Romanian barred crosswords (integrame).
- Mix word lengths: include short (2-4 letters), medium (5-7 letters), and long (8+ letters) words.

CLUE FIELD REQUIREMENTS
For every word, generate exactly three fields: clue, long_clue, hint.

CLUE (ultra-short clue)
- Maximum 3 words; strongly prefer 1–2 words
- Extremely compact — suitable for a tiny crossword clue cell
- Style varies by difficulty (see DIFFICULTY CONTROL)

LONG_CLUE (full clue)
- Exactly one full phrase (one sentence)
- More descriptive than the clue field
- Helps narrow down the answer
- Style varies by difficulty (see DIFFICULTY CONTROL)

HINT (progressive hint)
- Maximum 2 full phrases (one or two sentences)
- Provides stronger semantic guidance
- Should help most players infer the answer
- Premium hints are sold to solvers, so make each hint enticing yet non-spoiling
- Style varies by difficulty (see DIFFICULTY CONTROL)

STRICT CLUE RESTRICTIONS
For all clue fields (clue, long_clue, hint):
- Do NOT include the answer word, any inflected form, derivative, obvious lexical relative, or visible substring fragment of the answer
- Use synonyms, metaphors, contextual associations, cultural references, or indirect descriptions
- Avoid dictionary-style definitions (except where allowed by EASY difficulty)
- Avoid overly obvious clues
- Avoid sensitive political, religious, hateful, sexual, or defamatory framing
- Avoid repetitive wording or repeated clue structures across entries
- PUNCTUATION: Use no punctuation at all unless strictly necessary. Ellipsis (...) is allowed only when intentional trailing ambiguity is needed. Exclamation mark (!) is allowed only for strong emphasis where clearly warranted. All other punctuation — periods, commas, semicolons, colons, question marks, hyphens, dashes, parentheses, quotation marks — is forbidden.

DIFFICULTY CONTROL
Adjust BOTH word selection AND clue style to the requested difficulty:

EASY:
- Words: well-known, common, everyday vocabulary; avoid rare or literary words
- clue: clear, accessible; simple one-to-one synonyms and direct definitions are allowed
- long_clue: a straightforward descriptive phrase that makes the answer fairly guessable
- hint: a clear, helpful explanation that most players can solve immediately
- Overall tone: friendly and approachable, minimal misdirection

MEDIUM:
- Words: mix of common and moderately challenging vocabulary
- clue: compact, indirect; prefer metaphor, wordplay, or elliptical phrasing
- long_clue: moderately descriptive, narrows possibilities without giving the answer away
- hint: clearer guidance with enough context for most solvers
- Overall tone: clever but not obscure, balanced challenge

HARD:
- Words: include rare, literary, or domain-specific vocabulary
- clue: oblique, playful, and misleading while still solvable; avoid simple synonyms
- long_clue: subtly narrows the field but still requires lateral thinking
- hint: provides semantic guidance but through indirect or literary language
- Overall tone: deceptive, requires deep vocabulary or lateral thinking

THEME INTEGRATION
- Every word must be strongly connected to the theme.
- Clue and hint fields should subtly reinforce the thematic connection when natural.
- Do not force thematic references if they make clues awkward.

INTERNAL BEST-CANDIDATE SELECTION
For each word:
- Consider multiple candidate words and clue angles internally
- For MEDIUM and HARD: reject weak candidates such as direct definitions or simple synonyms
- For EASY: prefer clear, accessible candidates; simple synonyms are acceptable
- Select the strongest candidates for the requested difficulty
- Do not output candidates or reasoning

STYLE VARIATION
Across the full word list, vary clue style where possible.
Distribute different styles such as:
- metaphor
- cultural reference
- wordplay
- elliptical noun phrase
- contextual association
- indirect description

SELF-VALIDATION
Before returning the final response, verify internally that:
- every word is a real word in the requested language — reject any foreign-language word
- every word is unique — no duplicates anywhere in the list, even with different diacritics
- every word is uppercase A-Z and at least 2 letters
- every word has exactly one clue, one long_clue, and one hint
- clue has at most 3 words
- long_clue is exactly one full phrase (one sentence)
- hint is at most 2 full phrases (one or two sentences)
- no clue field contains the answer word or obvious substring
- all text is strictly in the requested language
- clue style matches the requested difficulty level
- word difficulty matches the requested difficulty level
- no clue field uses punctuation unless strictly necessary (ellipsis only for intentional ambiguity, exclamation mark only for strong warranted emphasis)
- the response matches the required JSON schema

If any rule fails, revise internally until the output is valid."""

THEME_PROMPT_TEMPLATE = """\
Generate theme words for a crossword puzzle.

LANGUAGE: {{LANGUAGE}}
THEME: {{THEME}}
DIFFICULTY: {{DIFFICULTY}}
WORD COUNT: between {{MIN_WORDS}} and {{MAX_WORDS}} unique words

{{TYPE_INSTRUCTIONS}}

{{DESCRIPTION_LINE}}

Requirements:
- Return one word object for each generated word.
- Every word must be uppercase ASCII A-Z, unique, and strongly tied to the theme.
- Mix well-known anchors with more varied vocabulary to guarantee variety.
- Apply the requested difficulty to both word selection and clue style.
- Use only the requested language in all text fields.
- Return only valid JSON matching the response schema."""

THEME_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "status": {
            "type": "STRING",
            "enum": ["ok", "error"],
        },
        "crossword_title": {"type": "STRING", "nullable": True},
        "content": {"type": "STRING", "nullable": True},
        "words": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "word": {"type": "STRING"},
                    "clue": {"type": "STRING"},
                    "long_clue": {"type": "STRING"},
                    "hint": {"type": "STRING"},
                },
                "required": ["word", "clue", "long_clue", "hint"],
            },
        },
    },
    "required": ["status"],
}

_TYPE_INSTRUCTIONS = {
    ThemeType.DOMAIN_SPECIFIC_WORDS.value: (
        "Generate domain-specific words strongly associated with the theme.\n"
        "Set crossword_title to null.\n"
        "Provide a 1-2 sentence summary of the theme inside the content field."
    ),
    ThemeType.JOKE_CONTINUATION.value: (
        "Write a short joke (setup + punchline, max 3 sentences) connected to the theme "
        "and place the full text inside the content field.\n"
        "Every generated word must appear in, or be critical to understanding, the punchline.\n"
        "Set crossword_title to null."
    ),
    ThemeType.JOKE_CONTINUATION.value + "_with_desc": (
        "A joke has been provided in the description. Use it verbatim when possible; "
        "only adjust wording for clarity.\n"
        "Place the full joke text inside the content field.\n"
        "Every generated word must appear in, or be critical to understanding, the punchline.\n"
        "Set crossword_title to null."
    ),
    ThemeType.CUSTOM.value: (
        "Use the creative brief to craft an evocative crossword title (5-10 words) "
        "and place it in the crossword_title field.\n"
        "Fill the content field with a 1-3 sentence blurb that sells the theme to solvers."
    ),
}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _count_sentences(text: str) -> int:
    """Count sentences by splitting on sentence-ending punctuation (.!?)."""
    if not text.strip():
        return 0
    parts = re.split(r'[.!?]+(?:\s|$)', text.strip())
    return len([p for p in parts if p.strip()])


def _is_severe_theme_violation(violation: str) -> bool:
    """Return True for violations that require a repair call (vs cosmetic issues that are accepted)."""
    severe_keywords = ("missing required", "not valid uppercase", "duplicate word", "contains the answer")
    return any(kw in violation for kw in severe_keywords)


def _validate_theme_words(
    words: List[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """Split theme word entries into valid and needs-repair lists.

    Entries with only cosmetic violations (word/sentence counts slightly exceeded)
    are accepted as valid. Only entries with severe violations (missing fields,
    invalid word format, duplicates, answer leaking) are flagged for repair.

    Returns (valid, needs_repair) where each needs_repair entry has a ``violations`` key.
    """
    valid: List[Dict] = []
    needs_repair: List[Dict] = []
    seen_words: Set[str] = set()

    for entry in words:
        all_violations: List[str] = []
        word = entry.get("word", "")
        clue = entry.get("clue", "")
        long_clue = entry.get("long_clue", "")
        hint = entry.get("hint", "")

        # Required fields
        if not word or not clue or not long_clue or not hint:
            all_violations.append("missing required field(s)")

        # Word format
        word_upper = word.upper() if word else ""
        if word_upper and not re.match(r'^[A-Z]{2,}$', word_upper):
            all_violations.append(f"word '{word}' is not valid uppercase A-Z with 2+ letters")

        # Duplicate check
        if word_upper in seen_words:
            all_violations.append(f"duplicate word '{word}'")

        # Clue word count
        if len(clue.split()) > 3:
            all_violations.append(f"clue exceeds 3 words ({len(clue.split())} words)")

        # Long clue sentence count
        long_clue_sentences = _count_sentences(long_clue)
        if long_clue_sentences > 1:
            all_violations.append(f"long_clue must be one phrase but has {long_clue_sentences} sentences")

        # Hint sentence count
        hint_sentences = _count_sentences(hint)
        if hint_sentences > 2:
            all_violations.append(f"hint exceeds 2 phrases ({hint_sentences} sentences)")

        # Answer leaking check
        word_lower = word.lower() if word else ""
        if word_lower and len(word_lower) >= 2:
            for field_name, field_val in [("clue", clue), ("long_clue", long_clue), ("hint", hint)]:
                if word_lower in field_val.lower():
                    all_violations.append(f"{field_name} contains the answer '{word}'")

        if not all_violations:
            valid.append(entry)
            seen_words.add(word_upper)
        elif any(_is_severe_theme_violation(v) for v in all_violations):
            entry_copy = dict(entry)
            entry_copy["violations"] = all_violations
            needs_repair.append(entry_copy)
            LOGGER.warning(
                "Theme word requires repair — word: %r | violations: %s",
                word, "; ".join(all_violations),
            )
        else:
            # Cosmetic violations only (word/sentence count) — accept and log
            LOGGER.warning(
                "Theme word has minor violations (accepted) — word: %r | violations: %s",
                word, "; ".join(all_violations),
            )
            valid.append(entry)
            seen_words.add(word_upper)

    return valid, needs_repair


def _build_theme_repair_prompt(
    invalid_entries: List[Dict], language: str, theme: str, difficulty: str,
) -> str:
    """Build a repair prompt listing only the invalid theme word entries."""
    lines = ["The following theme word entries have validation errors. Please fix them.\n"]
    lines.append(f"LANGUAGE: {language}")
    lines.append(f"THEME: {theme}")
    lines.append(f"DIFFICULTY: {difficulty.upper() if difficulty else 'MEDIUM'}\n")

    for entry in invalid_entries:
        word = entry.get("word", "???")
        violations = entry.get("violations", [])
        lines.append(f"WORD: {word}")
        for v in violations:
            lines.append(f"  - VIOLATION: {v}")
        lines.append("")

    lines.append("Requirements:")
    lines.append("- Fix all listed violations.")
    lines.append("- Return corrected word objects for these words only.")
    lines.append("- Follow all original rules from the system instruction.")
    lines.append("- Return only valid JSON matching the response schema.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

class GeminiThemeWordGenerator:
    """LLM-powered generator using the Gemini API with structured output and validation."""

    def __init__(
        self,
        model_name: str = "gemini-2.5-flash",
        api_key_env: str = "GEMINI_API_KEY",
        model_env: str = "GEMINI_MODEL",
        theme_type: str = "domain_specific_words",
        theme_description: str = "",
        cache: Optional["ThemeCache"] = None,
        gemini_client: Optional[GeminiClient] = None,
        prompt_log: Optional[PromptLog] = None,
    ) -> None:
        """Initialize with optional pre-built client and cache."""
        resolved_model = os.environ.get(model_env, model_name)
        self.model_name = resolved_model
        self.api_key_env = api_key_env
        self.model_env = model_env
        self.theme_type = theme_type
        self.theme_description = theme_description
        self.cache = cache
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

    def generate(
        self, theme: str, limit: int = 80,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> ThemeOutput:  # pragma: no cover - external
        """Generate theme words via Gemini with validation and repair."""
        normalized_type = (
            self.theme_type.value
            if isinstance(self.theme_type, ThemeType) else str(self.theme_type)
        )
        if self.cache is not None:
            cached = self.cache.lookup(
                theme, difficulty, language,
                theme_description=self.theme_description,
                theme_type=normalized_type,
            )
            if cached is not None:
                return cached

        prompt = self._render_prompt(theme, limit, difficulty, language)
        response_schema = self._build_response_schema(limit)

        client = self._get_client()
        response_text = client.generate_text(
            prompt,
            system_instruction=THEME_SYSTEM_INSTRUCTION,
            response_schema=response_schema,
            request_type="theme_generation",
        )

        parsed = self._parse_response(response_text, self.theme_type)
        if parsed is None:
            return ThemeOutput()

        raw_words, crossword_title, content = parsed

        # Validate and repair only for severe violations
        valid, needs_repair = _validate_theme_words(raw_words)
        LOGGER.info(
            "Theme word validation: %d/%d entries valid, %d need repair",
            len(valid), len(raw_words), len(needs_repair),
        )
        if needs_repair:
            LOGGER.warning(
                "Sending repair request for %d theme words: %s",
                len(needs_repair),
                ", ".join(e.get("word", "?") for e in needs_repair),
            )
            repair_prompt = _build_theme_repair_prompt(needs_repair, language, theme, difficulty)
            repair_schema = self._build_response_schema(len(needs_repair))
            repair_text = client.generate_text(
                repair_prompt,
                system_instruction=THEME_SYSTEM_INSTRUCTION,
                response_schema=repair_schema,
                request_type="theme_repair",
            )
            repaired = self._parse_response(repair_text, self.theme_type)
            if repaired is not None:
                repaired_words, _, _ = repaired
                repaired_valid, still_invalid = _validate_theme_words(repaired_words)
                valid.extend(repaired_valid)
                if still_invalid:
                    LOGGER.warning(
                        "After repair, %d theme words still invalid (dropping): %s",
                        len(still_invalid),
                        ", ".join(e.get("word", "?") for e in still_invalid),
                    )

        # Build ThemeOutput
        entries = self._build_theme_words(valid)
        theme_output = ThemeOutput(
            words=entries[:limit],
            crossword_title=crossword_title,
            content=content,
        )

        # Persist result
        if self.cache is not None:
            self.cache.save(
                theme, self.theme_type, difficulty, language,
                theme_output, theme_description=self.theme_description,
            )

        return theme_output

    def _render_prompt(
        self, theme: str, limit: int,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> str:
        """Fill the prompt template with concrete values."""
        normalized_type = (
            self.theme_type.value
            if isinstance(self.theme_type, ThemeType) else str(self.theme_type)
        )
        min_words = self._min_word_target(limit)
        max_words = max(1, limit)
        diff_key = difficulty.upper() if difficulty else "MEDIUM"

        # Pick type-specific instructions
        if normalized_type == ThemeType.JOKE_CONTINUATION.value and self.theme_description:
            type_key = ThemeType.JOKE_CONTINUATION.value + "_with_desc"
        else:
            type_key = normalized_type
        type_instructions = _TYPE_INSTRUCTIONS.get(
            type_key, _TYPE_INSTRUCTIONS[ThemeType.DOMAIN_SPECIFIC_WORDS.value]
        )

        description_line = ""
        if self.theme_description:
            description_line = f"Additional context: {self.theme_description}"

        return (
            THEME_PROMPT_TEMPLATE
            .replace("{{LANGUAGE}}", language)
            .replace("{{THEME}}", theme)
            .replace("{{DIFFICULTY}}", diff_key)
            .replace("{{MIN_WORDS}}", str(min_words))
            .replace("{{MAX_WORDS}}", str(max_words))
            .replace("{{TYPE_INSTRUCTIONS}}", type_instructions)
            .replace("{{DESCRIPTION_LINE}}", description_line)
        )

    def _build_response_schema(self, limit: int) -> Dict[str, Any]:
        """Build the response schema with adjusted word count limits."""
        schema = copy.deepcopy(THEME_RESPONSE_SCHEMA)
        return schema

    @staticmethod
    def _min_word_target(limit: int) -> int:
        """Compute minimum word count for the given limit."""
        if limit <= 0:
            return 1
        constrained = min(50, max(1, limit))
        return min(limit, max(10, constrained))

    @staticmethod
    def _parse_response(
        text: str, theme_type: str = "domain_specific_words",
    ) -> Optional[Tuple[List[Dict], Optional[str], Optional[str]]]:
        """Parse a Gemini JSON response.

        Returns (raw_word_dicts, crossword_title, content) or None on error.
        """
        if not text:
            return None
        stripped = GeminiThemeWordGenerator._strip_code_fences(text)
        parsed = GeminiThemeWordGenerator._maybe_parse_json_object(stripped)
        if not isinstance(parsed, dict):
            LOGGER.warning("Gemini theme payload not a JSON object; returning empty")
            return None

        # Handle safety errors
        status = parsed.get("status", "ok")
        if status == "error":
            LOGGER.warning("Gemini theme safety error")
            return None

        words_data = parsed.get("words")
        if words_data is None:
            LOGGER.warning("Gemini theme payload missing words array; returning empty")
            return None

        # Extract metadata
        crossword_title = parsed.get("crossword_title")
        if isinstance(crossword_title, str):
            crossword_title = crossword_title.strip() or None
        elif crossword_title is not None:
            crossword_title = None

        content = parsed.get("content")
        if isinstance(content, str):
            content = content.strip() or None
        elif content is not None:
            content = None
        if content is None:
            for key in ("joke_text", "jokeText", "summary"):
                candidate = parsed.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    content = candidate.strip()
                    break

        return words_data, crossword_title, content

    @staticmethod
    def _build_theme_words(word_dicts: List[Dict]) -> List[ThemeWord]:
        """Convert validated word dicts into ThemeWord objects."""
        entries: List[ThemeWord] = []
        for item in word_dicts:
            if not isinstance(item, dict):
                continue
            word = item.get("word")
            clue = item.get("clue") or item.get("short_clue") or ""
            long_clue = item.get("long_clue") or item.get("longClue") or ""
            hint = item.get("hint") or item.get("smart_hint") or ""
            if not isinstance(word, str) or not word.strip():
                continue
            entries.append(
                ThemeWord(
                    word=word.strip().upper(),
                    clue=clue.strip() if isinstance(clue, str) else "",
                    source="gemini",
                    long_clue=long_clue.strip() if isinstance(long_clue, str) else "",
                    hint=hint.strip() if isinstance(hint, str) else "",
                )
            )
        return entries

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Remove markdown code fences from response text."""
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        while lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @staticmethod
    def _maybe_parse_json_object(text: str) -> Optional[dict]:
        """Attempt to parse text as a JSON object."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


class SubstringThemeWordGenerator:
    """Filters dictionary words that contain theme_title as a substring."""

    def __init__(self, dictionary: "WordDictionary", theme_title: str) -> None:
        self._dictionary = dictionary
        self._theme_title = theme_title

    def generate(
        self, theme: str, limit: int = 80,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> ThemeOutput:
        """Generate theme words by filtering dictionary for substring matches."""
        substring = self._theme_title.lower()
        diff = Difficulty(difficulty.upper()) if difficulty else Difficulty.MEDIUM

        scored: List[tuple] = []
        seen: set = set()

        for surface, entry in self._dictionary._entry_by_surface.items():
            if substring in surface.lower() and surface not in seen:
                seen.add(surface)
                scored.append((entry.score(diff), surface, entry))

        # Sort by difficulty-adjusted score descending so the most appropriate
        # words for the chosen difficulty level come first
        scored.sort(key=lambda t: t[0], reverse=True)

        matching = [
            ThemeWord(
                word=surface.upper(),
                clue=f"Contine {self._theme_title.upper()}",
                source="substring",
                long_clue=(
                    f"Cuvant ce include secventa '{self._theme_title.upper()}' pentru a ancora tema."
                ),
                hint=(
                    f"Cauta {self._theme_title.upper()} in interiorul raspunsului."
                ),
            )
            for _, surface, _ in scored[:limit]
        ]
        LOGGER.info(
            "SubstringThemeWordGenerator found %d words containing '%s' (difficulty=%s)",
            len(matching), self._theme_title, difficulty,
        )
        return ThemeOutput(words=matching)


class UserWordListGenerator:
    """Returns a user-supplied list of words as ThemeWord objects."""

    def __init__(self, raw_words: List[str]) -> None:
        self._theme_words: List[ThemeWord] = []
        for item in raw_words:
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                word, _, clue = item.partition(":")
                word_clean = word.strip().upper()
                clue_text = clue.strip()
                has_user_clue = bool(clue_text)
                self._theme_words.append(
                    ThemeWord(
                        word_clean,
                        self._derive_short_clue(clue_text, word_clean),
                        "user",
                        long_clue=self._derive_long_clue(clue_text, word_clean),
                        hint=self._derive_hint(clue_text, word_clean),
                        has_user_clue=has_user_clue,
                    )
                )
            else:
                word_clean = item.upper()
                self._theme_words.append(
                    ThemeWord(
                        word_clean,
                        self._derive_short_clue("", word_clean),
                        "user",
                        long_clue=self._derive_long_clue("", word_clean),
                        hint=self._derive_hint("", word_clean),
                    )
                )

    def generate(
        self, theme: str, limit: int = 80,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> ThemeOutput:
        """Return the user-provided word list as ThemeOutput."""
        return ThemeOutput(words=list(self._theme_words))

    @staticmethod
    def _derive_short_clue(clue_text: str, word: str) -> str:
        """Derive a compact clue from user-provided text."""
        tokens = clue_text.split()
        if tokens:
            return " ".join(tokens[:3])
        return f"Tematic {word.title()}".strip()

    @staticmethod
    def _derive_long_clue(clue_text: str, word: str) -> str:
        """Derive a full clue from user-provided text."""
        cleaned = clue_text.strip()
        if cleaned:
            return cleaned
        return f"Cuvant tematic legat de {word.title()}."

    @staticmethod
    def _derive_hint(clue_text: str, word: str) -> str:
        """Derive a hint from user-provided text."""
        cleaned = clue_text.strip()
        if cleaned:
            return f"Context: {cleaned}"
        return f"Pista: gandeste-te la {word.title()} in tema descrisa."


DEFAULT_THEME_BUCKETS = {
    "mitologie": {
        "EASY": [
            "APOLON", "ARES", "ATHENA", "HERA", "IRIS", "HERMES", "ODIN",
            "THOR", "DIANA", "EROS", "AURORA", "TITAN", "ATLAS", "PAN",
            "ZEUS", "POSEIDON", "ISIS", "RA",
        ],
        "MEDIUM": [
            "ANUBIS", "FREIA", "MINERVA", "CERES", "NEMESIS", "HELIOS",
            "SIRENA", "FAUN", "OSIRIS", "DEMETER", "JANUS", "BALDER", "TETHYS",
        ],
        "HARD": [
            "HESTIA", "SATIR", "EOL", "MORPHEU", "ORACOL", "NEREIDA", "LIBER",
            "CHARON", "ERINIE", "HYPERION", "PROTEU",
        ],
    },
    "istorie": {
        "EASY": [
            "REGAT", "ARMATA", "REGE", "PATRIA", "SENAT", "FORT", "OPERA",
            "PACT", "COLONIE", "CRONICA", "STEAG", "SCUT", "HARTA", "CRUCE",
        ],
        "MEDIUM": [
            "LEGIE", "TRON", "VOIEVOD", "ARHIVA", "ARMURA", "CANON",
            "DOMNIE", "TRIBUT", "LEGAT", "TABELA", "DINASTIE", "HERALD",
            "ARMISTITIU", "CRONOGRAF",
        ],
        "HARD": [
            "CRONIC", "CASTRA", "ARCA", "DICTUM", "RELICVA", "PORTIC",
            "CRONICAR", "EDICT", "SIGILIU", "PAPIRUS", "PALIMPSEST", "TRIREMA",
        ],
    },
    "natura": {
        "EASY": [
            "MUNTE", "BRAD", "LUP", "CERB", "PLOAIE", "CAMP", "IARBA",
            "PAMANT", "OCEAN", "DELTA", "FRUNZA", "LAC", "NISIP", "VANT", "RAPITA",
        ],
        "MEDIUM": [
            "CODRU", "IZVOR", "STANCA", "LUNCA", "PODIS", "OGOR", "APUS",
            "CASCADA", "FAG", "AURORA", "DESERT", "GROTA", "PENINSULA", "ECOSISTEM",
        ],
        "HARD": [
            "RAPID", "VALURI", "ALBIA", "MOLID", "RACHIT", "SIRET",
            "TRESTIE", "PRAFUL", "ARIN", "GORUN", "ESTUAR", "ZADA", "LIMAN",
        ],
    },
}


class DummyThemeWordGenerator:
    """Produces placeholder theme words from predefined buckets."""

    def __init__(self, theme_buckets: dict[str, dict[str, List[str]]] | None = None, seed: int | None = None) -> None:
        buckets = theme_buckets or DEFAULT_THEME_BUCKETS
        self.theme_buckets: dict[str, dict[str, List[str]]] = {}
        for key, tier_map in buckets.items():
            self.theme_buckets[key] = {
                tier: [w.upper() for w in words if w]
                for tier, words in tier_map.items()
            }
        self.rng = random.Random(seed)

    def generate(
        self, theme: str, limit: int = 30,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> ThemeOutput:
        """Generate placeholder theme words from predefined buckets."""
        key = (theme or "").strip().lower()
        tier = difficulty.upper() if difficulty else "MEDIUM"
        tier_map = self.theme_buckets.get(key)
        if tier_map is None:
            raise ValueError(
                f"Theme '{theme}' is not in DummyThemeWordGenerator buckets "
                f"(known: {list(self.theme_buckets)}). "
                "Add it to DEFAULT_THEME_BUCKETS or use --llm / --words to provide real theme words."
            )

        # Prefer on-tier words, then fill from other tiers
        on_tier = list(tier_map.get(tier, []))
        off_tier: List[str] = []
        for t, words in tier_map.items():
            if t != tier:
                off_tier.extend(words)

        self.rng.shuffle(on_tier)
        self.rng.shuffle(off_tier)
        combined = [w.upper() for w in on_tier + off_tier if w]

        selections = combined[: max(limit, 5)]
        results = [
            ThemeWord(
                word=word,
                clue=f"Rezerva {theme.split()[0] if theme else 'tema'}",
                source="dummy",
                long_clue=(
                    f"Cuvant rezervat pentru tema '{theme or 'mister'}', util pentru interconectare: {word.lower()}."
                ),
                hint=(
                    f"Pista bonus: gandeste-te la {word.lower()} in contextul '{theme or 'mister'}'."
                ),
            )
            for word in selections[:limit]
        ]
        LOGGER.info("Dummy generator produced %s placeholders (tier=%s)", len(results), tier)
        return ThemeOutput(words=results)


def merge_theme_generators(
    primary: ThemeWordGenerator | None,
    fallbacks: Sequence[ThemeWordGenerator],
    theme: str,
    target: int,
    difficulty: str = "MEDIUM",
    language: str = "Romanian",
) -> ThemeOutput:
    """Attempt primary generator, cascaded fallbacks, and deduplicate results."""

    collected: List[ThemeWord] = []
    seen: set[str] = set()
    crossword_title: Optional[str] = None
    content: Optional[str] = None

    def extend(output: ThemeOutput) -> None:
        nonlocal crossword_title, content
        if crossword_title is None and output.crossword_title:
            crossword_title = output.crossword_title
        if content is None and output.content:
            content = output.content
        for entry in output.words:
            word_key = entry.word.upper()
            if not word_key or word_key in seen:
                continue
            collected.append(entry)
            seen.add(word_key)
            if len(collected) >= target:
                break

    if primary:
        try:
            extend(primary.generate(theme, limit=target, difficulty=difficulty, language=language))
        except Exception as exc:  # pragma: no cover - integration only
            LOGGER.warning("Primary theme generator failed: %s", exc)

    for generator in fallbacks:
        if len(collected) >= target:
            break
        try:
            extend(generator.generate(theme, limit=target, difficulty=difficulty, language=language))
        except Exception as exc:
            LOGGER.warning("Fallback theme generator %s failed: %s", generator, exc)

    return ThemeOutput(
        words=collected[:target],
        crossword_title=crossword_title,
        content=content,
    )
