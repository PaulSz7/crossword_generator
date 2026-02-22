"""Theme word generation interfaces."""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from typing import Iterable, List, Protocol, Sequence

from ..utils.logger import get_logger


LOGGER = get_logger(__name__)


@dataclass
class ThemeWord:
    """Represents a theme-driven seed word and its clue."""

    word: str
    clue: str
    source: str = "unknown"


class ThemeWordGenerator(Protocol):
    """Protocol implemented by all theme word providers."""

    def generate(
        self, theme: str, limit: int = 80,
        difficulty: str = "MEDIUM", language: str = "Romanian",
    ) -> List[ThemeWord]:
        ...


class GeminiThemeWordGenerator:
    """LLM-powered generator using the Gemini API."""

    def __init__(self, model_name: str = "gemini-pro", api_key_env: str = "GOOGLE_API_KEY") -> None:
        self.model_name = model_name
        self.api_key_env = api_key_env

    THEME_BASE_PROMPT = (
        "You are assisting with a {language} cryptic crossword. "
        "Generate between 50 and {limit} JSON lines describing unique theme words. "
        "Theme: '{theme}'. Each JSON line must contain fields: word, clue. "
        "The clue must be 3-5 words in {language}, cryptic-friendly. "
        "Output no more than {limit} entries."
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
    ) -> List[ThemeWord]:  # pragma: no cover - external
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
        return self._parse_response(response.text)

    @classmethod
    def _render_prompt(cls, theme: str, limit: int,
                       difficulty: str = "MEDIUM", language: str = "Romanian") -> str:
        base = cls.THEME_BASE_PROMPT.format(language=language, limit=limit, theme=theme)
        diff_key = difficulty.upper() if difficulty else "MEDIUM"
        diff_text = cls.THEME_DIFFICULTY_PROMPT.get(diff_key, cls.THEME_DIFFICULTY_PROMPT["MEDIUM"])
        return base + " " + diff_text.format(language=language)

    @staticmethod
    def _parse_response(text: str) -> List[ThemeWord]:
        entries: List[ThemeWord] = []
        if not text:
            return entries
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
        return entries


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

FALLBACK_BUCKET = {
    "EASY": [
        "ROMA", "DUNARE", "SOLAR", "VIATA", "LUMEA", "PIATA", "PORT", "CETATE",
    ],
    "MEDIUM": [
        "CARPA", "RITUAL", "LEGAT", "CLIPA", "CAMPIE", "RAZBOI", "ACORD",
    ],
    "HARD": [
        "PATRU", "POD", "CLASA", "COLINA",
    ],
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
    ) -> List[ThemeWord]:
        key = (theme or "").strip().lower()
        tier = difficulty.upper() if difficulty else "MEDIUM"
        tier_map = self.theme_buckets.get(key, FALLBACK_BUCKET)

        # Prefer on-tier words, then fall back to other tiers
        on_tier = list(tier_map.get(tier, []))
        off_tier: List[str] = []
        for t, words in tier_map.items():
            if t != tier:
                off_tier.extend(words)

        self.rng.shuffle(on_tier)
        self.rng.shuffle(off_tier)
        combined = on_tier + off_tier
        combined = [w.upper() for w in combined if w]

        if not combined:
            # Ultimate fallback: flatten FALLBACK_BUCKET
            flat = []
            for words in FALLBACK_BUCKET.values():
                flat.extend(words)
            combined = flat

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
        return results


def merge_theme_generators(
    primary: ThemeWordGenerator | None,
    fallbacks: Sequence[ThemeWordGenerator],
    theme: str,
    target: int,
    difficulty: str = "MEDIUM",
    language: str = "Romanian",
) -> List[ThemeWord]:
    """Attempt primary generator, cascaded fallbacks, and deduplicate results."""

    collected: List[ThemeWord] = []
    seen: set[str] = set()

    def extend(entries: Iterable[ThemeWord]) -> None:
        for entry in entries:
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

    return collected[:target]
