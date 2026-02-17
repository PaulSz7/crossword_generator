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

    def generate(self, theme: str, limit: int = 80) -> List[ThemeWord]:
        ...


class GeminiThemeWordGenerator:
    """LLM-powered generator using the Gemini API."""

    def __init__(self, model_name: str = "gemini-pro", api_key_env: str = "GOOGLE_API_KEY") -> None:
        self.model_name = model_name
        self.api_key_env = api_key_env

    def generate(self, theme: str, limit: int = 80) -> List[ThemeWord]:  # pragma: no cover - external
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
        prompt = self._render_prompt(theme, limit)
        response = genai.GenerativeModel(self.model_name).generate_content(prompt)
        return self._parse_response(response.text)

    @staticmethod
    def _render_prompt(theme: str, limit: int) -> str:
        return (
            "You are assisting with a Romanian cryptic crossword. "
            "Generate between 50 and 100 JSON lines describing unique theme words. "
            f"Theme: '{theme}'. Each JSON line should contain fields word, clue. "
            "The clue must be 3-4 Romanian words, cryptic-friendly."
            f" Output no more than {limit} entries."
        )

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
    "mitologie": [
        "APOLON",
        "ARES",
        "ATHENA",
        "HERA",
        "IRIS",
        "HERMES",
        "ANUBIS",
        "ODIN",
        "FREIA",
        "THOR",
        "HESTIA",
        "PAN",
        "SATIR",
        "MINERVA",
        "CERES",
        "DIANA",
        "EOL",
        "NEMESIS",
        "EROS",
        "AURORA",
        "HELIOS",
        "MORPHEU",
        "ORACOL",
        "SIRENA",
        "TITAN",
        "ATLAS",
        "NEREIDA",
        "FAUN",
        "OSIRIS",
        "LIBER",
    ],
    "istorie": [
        "REGAT",
        "LEGIE",
        "TRON",
        "CRONIC",
        "VOIEVOD",
        "ARHIVA",
        "ARMURA",
        "CASTRA",
        "ARCA",
        "DICTUM",
        "PACT",
        "RELICVA",
        "SENAT",
        "PATRIA",
        "ARMATA",
        "COLONIE",
        "CANON",
        "DOMNIE",
        "TRIBUT",
        "REGE",
        "LEGAT",
        "PORTIC",
        "CRONICAR",
        "TABELA",
        "FORT",
        "OPERA",
        "EDICT",
        "SIGILIU",
        "CRONICA",
    ],
    "natura": [
        "MUNTE",
        "BRAD",
        "LUP",
        "CERB",
        "CODRU",
        "RAPID",
        "IZVOR",
        "VALURI",
        "STANCA",
        "ALBIA",
        "LUNCA",
        "PODIS",
        "DELTA",
        "PLOAIE",
        "CAMP",
        "OGOR",
        "IARBA",
        "MOLID",
        "RACHIT",
        "SIRET",
        "APUS",
        "FRUNZA",
        "TRESTIE",
        "PRAFUL",
        "PAMANT",
        "AURORA",
        "OCEAN",
        "CASCADA",
        "FAG",
        "ARIN",
    ],
}

FALLBACK_BUCKET = [
    "ROMA",
    "DUNARE",
    "CARPA",
    "SOLAR",
    "RITUAL",
    "LEGAT",
    "PATRU",
    "CLIPA",
    "VIATA",
    "LUMEA",
    "CAMPIE",
    "POD",
    "RAZBOI",
    "CLASA",
    "ACORD",
    "PIATA",
    "COLINA",
    "PORT",
    "CETATE",
]


class DummyThemeWordGenerator:
    """Produces placeholder theme words from predefined buckets."""

    def __init__(self, theme_buckets: dict[str, List[str]] | None = None, seed: int | None = None) -> None:
        buckets = theme_buckets or DEFAULT_THEME_BUCKETS
        self.theme_buckets = {key: [w.upper() for w in words if w] for key, words in buckets.items()}
        self.rng = random.Random(seed)

    def generate(self, theme: str, limit: int = 30) -> List[ThemeWord]:
        key = (theme or "").strip().lower()
        words = list(self.theme_buckets.get(key, FALLBACK_BUCKET))
        words = [w.upper() for w in words if w]
        if not words:
            words = FALLBACK_BUCKET
        self.rng.shuffle(words)
        selections = words[: max(limit, 5)]
        results = [
            ThemeWord(
                word=word,
                clue=f"Rezerva {theme or 'tema'}: {word.lower()}",
                source="dummy",
            )
            for word in selections[:limit]
        ]
        LOGGER.info("Dummy generator produced %s placeholders", len(results))
        return results


def merge_theme_generators(
    primary: ThemeWordGenerator | None,
    fallbacks: Sequence[ThemeWordGenerator],
    theme: str,
    target: int,
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
            extend(primary.generate(theme, limit=target))
        except Exception as exc:  # pragma: no cover - integration only
            LOGGER.warning("Primary theme generator failed: %s", exc)

    for generator in fallbacks:
        if len(collected) >= target:
            break
        try:
            extend(generator.generate(theme, limit=target))
        except Exception as exc:
            LOGGER.warning("Fallback theme generator %s failed: %s", generator, exc)

    return collected[:target]
