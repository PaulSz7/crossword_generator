"""Theme word generation interfaces."""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Iterable, List, Optional, Protocol, Sequence

from ..core.constants import Difficulty
from ..utils.logger import get_logger

if TYPE_CHECKING:
    from .dictionary import WordDictionary


LOGGER = get_logger(__name__)


class ThemeType(str, Enum):
    DOMAIN_SPECIFIC_WORDS = "domain_specific_words"
    WORDS_CONTAINING_SUBSTRING = "words_containing_substring"
    JOKE_CONTINUATION = "joke_continuation"
    CUSTOM = "custom"


@dataclass
class ThemeWord:
    """Represents a theme-driven seed word and its clue."""

    word: str
    clue: str
    source: str = "unknown"


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


class GeminiThemeWordGenerator:
    """LLM-powered generator using the Gemini API."""

    def __init__(
        self,
        model_name: str = "gemini-pro",
        api_key_env: str = "GOOGLE_API_KEY",
        theme_type: str = "domain_specific_words",
        theme_description: str = "",
    ) -> None:
        self.model_name = model_name
        self.api_key_env = api_key_env
        self.theme_type = theme_type
        self.theme_description = theme_description

    THEME_BASE_PROMPT = (
        "You are assisting with a {language} cryptic crossword. "
        "Generate between 50 and {limit} JSON lines describing unique theme words. "
        "Theme: '{theme}'. Each JSON line must contain fields: word, clue. "
        "The clue must be 3-5 words in {language}, cryptic-friendly. "
        "Output no more than {limit} entries."
    )

    JOKE_PROMPT = (
        "You are a creative Romanian crossword designer.\n"
        "Generate words that form the punchline of a short joke related to '{theme}'.\n"
        "{description_line}"
        "Return a SINGLE JSON object (not JSON lines) with:\n"
        '  "joke_text": the complete short joke (setup + punchline, 1-3 sentences),\n'
        '  "words": [ {{"word": "UPPERCASE_WORD", "clue": "3-5 word {language} cryptic clue"}}, ...]\n'
        "Generate between 20 and {limit} unique {language} words.\n"
    )

    CUSTOM_PROMPT = (
        "You are a creative Romanian crossword designer.\n"
        "Theme title: '{theme}'.\n"
        "Creative brief: '{description}'.\n"
        "{user_words_line}"
        "Return a SINGLE JSON object with:\n"
        '  "crossword_title": engaging crossword title (5-10 words in {language}),\n'
        '  "content": thematic description for display (1-3 sentences in {language}),\n'
        '  "words": [ {{"word": "UPPERCASE_WORD", "clue": "3-5 word {language} cryptic clue"}}, ...]\n'
        "Generate between 20 and {limit} unique {language} words.\n"
    )

    THEME_DIFFICULTY_PROMPT = {
        "EASY": (
            "Target audience: beginners. Use only well-known, common {language} words. "
            "Clues: straightforward definitions or simple wordplay. Avoid obscure words."
        ),
        "MEDIUM": (
            "Target audience: regular solvers. Mix common and moderately challenging {language} words. "
            "Clues: cryptic conventions (anagrams, double meanings, hidden words)."
        ),
        "HARD": (
            "Target audience: experts. Prefer rare, literary, or domain-specific {language} words. "
            "Clues: advanced cryptic techniques (complex anagrams, misdirection, obscure references)."
        ),
    }

    def generate(
        self, theme: str, limit: int = 80,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> ThemeOutput:  # pragma: no cover - external
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Missing Gemini API key in environment variable {self.api_key_env}"
            )
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("google-generativeai package required for Gemini integration") from exc

        genai.configure(api_key=api_key)
        prompt = self._render_prompt(theme, limit, difficulty, language)
        response = genai.GenerativeModel(self.model_name).generate_content(prompt)
        return self._parse_response(response.text, self.theme_type)

    def _render_prompt(self, theme: str, limit: int,
                       difficulty: str = "MEDIUM", language: str = "Romanian") -> str:
        diff_key = difficulty.upper() if difficulty else "MEDIUM"
        diff_text = self.THEME_DIFFICULTY_PROMPT.get(diff_key, self.THEME_DIFFICULTY_PROMPT["MEDIUM"])
        diff_suffix = " " + diff_text.format(language=language)

        if self.theme_type == ThemeType.JOKE_CONTINUATION or self.theme_type == "joke_continuation":
            if self.theme_description:
                description_line = (
                    f"The joke to use is: '{self.theme_description}'. "
                    "Extract words from its punchline.\n"
                )
            else:
                description_line = ""
            return (
                self.JOKE_PROMPT.format(
                    theme=theme,
                    description_line=description_line,
                    language=language,
                    limit=limit,
                )
                + diff_suffix
            )

        if self.theme_type == ThemeType.CUSTOM or self.theme_type == "custom":
            user_words_line = ""
            return (
                self.CUSTOM_PROMPT.format(
                    theme=theme,
                    description=self.theme_description or theme,
                    user_words_line=user_words_line,
                    language=language,
                    limit=limit,
                )
                + diff_suffix
            )

        # domain_specific_words (default)
        base = self.THEME_BASE_PROMPT.format(language=language, limit=limit, theme=theme)
        return base + diff_suffix

    @staticmethod
    def _parse_response(text: str, theme_type: str = "domain_specific_words") -> ThemeOutput:
        if not text:
            return ThemeOutput()

        if theme_type in (ThemeType.JOKE_CONTINUATION, "joke_continuation"):
            return GeminiThemeWordGenerator._parse_json_object_response(
                text, content_key="joke_text"
            )

        if theme_type in (ThemeType.CUSTOM, "custom"):
            return GeminiThemeWordGenerator._parse_json_object_response(
                text, content_key="content", title_key="crossword_title"
            )

        # domain_specific_words: JSON lines
        entries: List[ThemeWord] = []
        for line in text.splitlines():
            line = line.strip().strip(",")
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            word = data.get("word")
            clue = data.get("clue")
            if isinstance(word, str) and isinstance(clue, str):
                entries.append(ThemeWord(word=word, clue=clue, source="gemini"))
        return ThemeOutput(words=entries)

    @staticmethod
    def _parse_json_object_response(
        text: str,
        content_key: str = "content",
        title_key: Optional[str] = None,
    ) -> ThemeOutput:
        """Parse a response that is a single JSON object with a 'words' array."""
        # Strip markdown code fences if present
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # Remove first and last fence lines
            inner = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:])
            stripped = inner.strip()
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            LOGGER.warning("Failed to parse JSON object response from LLM")
            return ThemeOutput()

        words_data = data.get("words", [])
        entries: List[ThemeWord] = []
        for item in words_data:
            word = item.get("word")
            clue = item.get("clue", "")
            if isinstance(word, str):
                entries.append(ThemeWord(word=word, clue=clue, source="gemini"))

        content = data.get(content_key) if content_key else None
        crossword_title = data.get(title_key) if title_key else None

        return ThemeOutput(words=entries, crossword_title=crossword_title, content=content)


class SubstringThemeWordGenerator:
    """Filters dictionary words that contain theme_title as a substring."""

    def __init__(self, dictionary: "WordDictionary", theme_title: str) -> None:
        self._dictionary = dictionary
        self._theme_title = theme_title

    def generate(
        self, theme: str, limit: int = 80,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> ThemeOutput:
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
                clue=f"Conține «{self._theme_title}»",
                source="substring",
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
                self._theme_words.append(ThemeWord(word.strip().upper(), clue.strip(), "user"))
            else:
                self._theme_words.append(ThemeWord(item.upper(), "", "user"))

    def generate(
        self, theme: str, limit: int = 80,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> ThemeOutput:
        return ThemeOutput(words=list(self._theme_words))


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
                clue=f"Rezerva {theme or 'tema'}: {word.lower()}",
                source="dummy",
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
