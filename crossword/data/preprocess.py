"""Dictionary preprocessing helpers for cacheable DataFrames."""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .normalization import clean_word

FIELDNAMES = (
    "surface",
    "length",
    "lemma",
    "definition",
    "frequency",
    "is_compound",
    "is_stopword",
    "raw_forms",
    "source_name",
    "tags",
    "def_abbrevs",
    "definition_count",
    "source_count",
    "difficulty_score",
)


def _parse_float(value: Optional[str]) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_bool(value: Optional[str]) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes"}


def _parse_int(value: Optional[str]) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# --- Difficulty scoring helpers ---

_COMMON_SOURCES = frozenset({
    # DEX editions
    "DEX '75", "DEX '84", "DEX '96", "DEX '98", "DEX '09", "DEX '12", "DEX '16",
    "DEX-S", "DEX-școlar",
    # DOOM editions
    "DOOM", "DOOM 2", "DOOM 3",
    # MDA / MDN editions
    "MDA", "MDA2", "MDN '00", "MDN '08",
    # Other standard references
    "DLRLC", "DEXI", "NODEX",
})
_RARE_SOURCES = frozenset({"DAR", "DRAM", "DRAM 2015", "Argou", "DTM", "DGS", "DER", "Scriban", "CADE"})
_HARD_TAGS = frozenset({
    # Explicitly rare or obsolete
    "rar", "neobișnuit", "învechit", "arhaizant", "arhaism", "ieșit din uz", "odinioară",
    "paradigmă învechită", "cu grafie învechită",
    # Regional / dialectal (not nationally known)
    "regional", "dialectal",
    "banat", "basarabia", "bucovina", "maramureș",
    "moldova", "muntenia", "oltenia", "transilvania",
    "țara românească", "țările române",
    # Specialised registers unlikely in standard crosswords
    "argou", "argotic", "jargon",
})
_MEDIUM_TAGS = frozenset({
    # Marked literary / stylistic registers
    "livresc", "poetic", "figurat", "metaforic", "alegoric",
    "eufemistic", "ironic", "glumeț", "hiperbolic", "emfatic",
    "peiorativ", "depreciativ", "vulgar",
    # Familiar / colloquial (less formal but still recognisable)
    "familiar", "popular",
})
# Foreign-language loan-word markers (appear in both tags and def_abbrevs).
# "limba " (with trailing space) catches "limba engleză" etc. via substring match.
_FOREIGN_LANG_TAGS = frozenset({
    # Roots catch both "-ism" loanword markers and "-esc"/"-ă" adjectival forms
    "angl",       # anglicism, angloamericanism
    "american",   # americanism
    "franțuz",    # franțuzism, franțuzesc
    "germ",       # germanism, german
    "grec",       # grecism, grecesc
    "italian",    # italienism, italienesc
    "latin",      # latinism, latinesc
    "rusism",     # kept explicit — "rus" would also match "rustic"
    "slavon",     # slavonism, slavonesc
    "sârb",       # sârbism, sârbesc
    "maghiar",    # maghiarism, maghiară
    "bulgăr",     # bulgărism, bulgăresc
    "hispan",     # hispanism
    "turc",       # turcism, turcesc
    "englez",     # englezism, englezesc
    "neologism",
    "limba ",     # "limba X" — catches "limba engleză" etc. via substring
})


_ABBREV_MARKER_RE = re.compile(r"#([^#]+)#")


def _load_abbreviation_lookup(csv_path: Path) -> Dict[str, List[str]]:
    """Load short→[internalRep, ...] from distinct_abbreviations.csv, filtering junk.

    A single short can expand to multiple internalRep values across different
    source dictionaries, so each key maps to a list of all known expansions.

    Junk filtered out:
    - internalRep starting with '*'  (unresolved references)
    - internalRep containing '$'     (historical book/manuscript citations)
    """
    lookup: Dict[str, List[str]] = {}
    if not csv_path.exists():
        return lookup
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            rep = row.get("internalRep", "")
            if rep.startswith("*") or "$" in rep:
                continue
            short = row.get("short", "")
            if short:
                lookup.setdefault(short, []).append(rep)
    return lookup


def _extract_def_abbrevs(definition: str, lookup: Dict[str, List[str]]) -> str:
    """Return pipe-joined set of abbreviation expansions found in a definition string.

    Dexonline embeds abbreviations as #short# markers in internalRep text.
    Each marker is looked up in *lookup* (short→[internalRep, ...]); all
    known expansions for each matched short are collected. Unrecognised
    markers are silently ignored.
    """
    found: set[str] = set()
    for short in _ABBREV_MARKER_RE.findall(definition):
        for rep in lookup.get(short, []):
            found.add(rep)
    return "|".join(sorted(found))


def _source_rarity_score(source_name: str) -> float:
    if not source_name:
        return 0.5
    name = source_name.strip()
    if name in _COMMON_SOURCES:
        return 0.0
    if name in _RARE_SOURCES:
        return 1.0
    return 0.5


def _tag_difficulty_score(tags: str) -> float:
    if not tags:
        return 0.0
    # Split by "|" only; search each segment for scoring keywords as substrings
    # so compound values like "argou; argotic" or "regional > Banat" are caught
    # without needing a secondary split on ";" or ">".
    parts = [part.strip().lower() for part in tags.split("|") if part.strip()]

    def _matches(patterns: frozenset) -> bool:
        return any(pat in part for part in parts for pat in patterns)

    if _matches(_HARD_TAGS):
        return 1.0
    if _matches(_MEDIUM_TAGS) or _matches(_FOREIGN_LANG_TAGS):
        return 0.5
    return 0.0


def compute_difficulty_score(
    frequency: float,
    length: int,
    source_name: str,
    tags: str,
    def_abbrevs: str,
    source_count: int,
    definition_count: int,
) -> float:
    """Compute a 0.0-1.0 difficulty score (higher = harder).

    Two-layer design:
    - **Base score**: continuous signals (frequency, length, counts) rank words
      within a tier. Tags and source also contribute here at lower weight since
      the floor layer already handles tier placement.
    - **Hard floors**: discrete markers guarantee a minimum tier regardless of
      how common or short the word is:
        - Hard tag (rar, regional, argou, …)  → floor 0.60  (hard tier)
        - Medium/foreign tag or rare source    → floor 0.30  (medium tier)
        - Rare source alone                    → floor 0.35  (solid medium)
    """
    tag_score = max(_tag_difficulty_score(tags), _tag_difficulty_score(def_abbrevs))
    source_score = _source_rarity_score(source_name)
    length_score = (min(length, 12) - 3) / 9

    base = (
        0.40 * (1.0 - frequency)
        + 0.20 * length_score
        + 0.15 * tag_score
        + 0.10 * source_score
        + 0.05 * (1.0 - min(source_count, 5) / 5)
        + 0.05 * (1.0 - min(definition_count, 10) / 10)
    )

    floor = 0.0
    if tag_score == 1.0:        # hard tag → guaranteed hard tier
        floor = 0.60
    elif tag_score == 0.5:      # medium / foreign tag → guaranteed medium tier
        floor = 0.27
    if source_score == 1.0:     # rare source → at least solid medium
        floor = max(floor, 0.35)

    return max(base, floor)


@dataclass
class ProcessedWordRecord:
    """Aggregated representation of a sanitized dictionary word."""

    surface: str
    length: int
    lemma: str
    definition: str
    frequency: float
    is_compound: bool
    is_stopword: bool
    raw_forms: set[str] = field(default_factory=set)
    source_name: str = ""
    tags: str = ""
    def_abbrevs: str = ""
    definition_count: int = 0
    source_count: int = 0
    difficulty_score: float = 0.0


def preprocess_dictionary(
    source_path: Path | str,
    destination_path: Path | str | None = None,
) -> List[ProcessedWordRecord]:
    """Return grouped entries and optionally persist them to ``destination_path``.

    The preprocessing logic removes Romanian diacritics, collapses all inflected
    forms into a single record, and keeps the highest-frequency metadata per
    unique cleaned surface.
    """

    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"Missing dictionary TSV: {source}")

    abbrev_lookup = _load_abbreviation_lookup(source.parent / "distinct_abbreviations.csv")

    aggregated: Dict[str, ProcessedWordRecord] = {}

    with source.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            if _parse_bool(row.get("is_adult")):
                continue
            surface = clean_word(row.get("entry_word", ""))
            if not surface:
                continue

            raw_entry = (row.get("entry_word") or "").strip()
            lemma = (row.get("lemma") or "").strip()
            definition = (row.get("definition") or "").strip()
            frequency = _parse_float(row.get("lexeme_frequency"))
            is_compound = _parse_bool(row.get("is_compound"))
            is_stopword = _parse_bool(row.get("is_stopword"))
            source_name = (row.get("source_short_name") or "").strip()
            tags = (row.get("tags") or "").strip()
            def_abbrevs = _extract_def_abbrevs(definition, abbrev_lookup)
            definition_count = _parse_int(row.get("definition_count"))
            source_count = _parse_int(row.get("source_count"))

            record = aggregated.get(surface)
            if record is None:
                record = ProcessedWordRecord(
                    surface=surface,
                    length=len(surface),
                    lemma=lemma,
                    definition=definition,
                    frequency=frequency,
                    is_compound=is_compound,
                    is_stopword=is_stopword,
                    raw_forms=set(),
                    source_name=source_name,
                    tags=tags,
                    def_abbrevs=def_abbrevs,
                    definition_count=definition_count,
                    source_count=source_count,
                )
                aggregated[surface] = record

            if raw_entry:
                record.raw_forms.add(raw_entry)
            record.is_compound = record.is_compound or is_compound
            record.is_stopword = record.is_stopword or is_stopword
            # Merge tags from all rows
            if tags:
                existing_tags = {t for t in record.tags.split("|") if t}
                new_tags = {t.strip() for t in tags.split("|") if t.strip()}
                merged = existing_tags | new_tags
                record.tags = "|".join(sorted(merged))
            # Merge def_abbrevs from all rows
            if def_abbrevs:
                existing = {t for t in record.def_abbrevs.split("|") if t}
                new = {t.strip() for t in def_abbrevs.split("|") if t.strip()}
                record.def_abbrevs = "|".join(sorted(existing | new))
            # Keep max counts across rows
            record.definition_count = max(record.definition_count, definition_count)
            record.source_count = max(record.source_count, source_count)
            if frequency > record.frequency:
                record.frequency = frequency
                record.lemma = lemma
                record.definition = definition
                record.source_name = source_name

    for record in aggregated.values():
        record.difficulty_score = compute_difficulty_score(
            frequency=record.frequency,
            length=record.length,
            source_name=record.source_name,
            tags=record.tags,
            def_abbrevs=record.def_abbrevs,
            source_count=record.source_count,
            definition_count=record.definition_count,
        )

    records = sorted(aggregated.values(), key=lambda item: item.surface)

    if destination_path:
        write_processed_dictionary(records, destination_path)

    return records


def write_processed_dictionary(records: Iterable[ProcessedWordRecord], destination: Path | str) -> None:
    """Persist processed records as a TSV-backed DataFrame."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=FIELDNAMES)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "surface": record.surface,
                    "length": record.length,
                    "lemma": record.lemma,
                    "definition": record.definition,
                    "frequency": f"{record.frequency:.6f}",
                    "is_compound": "1" if record.is_compound else "0",
                    "is_stopword": "1" if record.is_stopword else "0",
                    "raw_forms": "|".join(sorted(record.raw_forms)),
                    "source_name": record.source_name,
                    "tags": record.tags,
                    "def_abbrevs": record.def_abbrevs,
                    "definition_count": record.definition_count,
                    "source_count": record.source_count,
                    "difficulty_score": f"{record.difficulty_score:.6f}",
                }
            )


def load_processed_dictionary(path: Path | str) -> List[ProcessedWordRecord]:
    """Load processed rows from disk."""

    location = Path(path)
    if not location.exists():
        raise FileNotFoundError(f"Missing processed dictionary: {location}")

    records: List[ProcessedWordRecord] = []
    with location.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            raw_forms = [
                form
                for form in (row.get("raw_forms") or "").split("|")
                if form
            ]
            frequency = _parse_float(row.get("frequency"))
            source_name = (row.get("source_name") or "").strip()
            tags = (row.get("tags") or "").strip()
            def_abbrevs = (row.get("def_abbrevs") or "").strip()
            definition_count = _parse_int(row.get("definition_count"))
            source_count = _parse_int(row.get("source_count"))
            difficulty_score = _parse_float(row.get("difficulty_score"))
            # Fallback: if no stored difficulty_score, compute from frequency only
            if not difficulty_score and not source_name and not tags:
                difficulty_score = 1.0 - frequency
            records.append(
                ProcessedWordRecord(
                    surface=(row.get("surface") or "").strip().upper(),
                    length=int(row.get("length") or 0),
                    lemma=(row.get("lemma") or "").strip(),
                    definition=(row.get("definition") or "").strip(),
                    frequency=frequency,
                    is_compound=_parse_bool(row.get("is_compound")),
                    is_stopword=_parse_bool(row.get("is_stopword")),
                    raw_forms=set(raw_forms),
                    source_name=source_name,
                    tags=tags,
                    def_abbrevs=def_abbrevs,
                    definition_count=definition_count,
                    source_count=source_count,
                    difficulty_score=difficulty_score,
                )
            )
    return records


def ensure_processed_dictionary(
    source_path: Path | str,
    destination_path: Path | str,
) -> Path:
    """Make sure the processed cache exists, generating it if necessary."""

    destination = Path(destination_path)
    if destination.exists():
        return destination
    preprocess_dictionary(source_path, destination)
    return destination


__all__ = [
    "ProcessedWordRecord",
    "ensure_processed_dictionary",
    "load_processed_dictionary",
    "preprocess_dictionary",
    "write_processed_dictionary",
]


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Preprocess dex_words.tsv into a cached DataFrame")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("local_db/dex_words.tsv"),
        help="Input TSV produced from dex_words",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path for the processed cache (defaults to <source>_processed.tsv)",
    )
    args = parser.parse_args()

    output = args.output
    if output is None:
        output = args.source.with_name(f"{args.source.stem}_processed{args.source.suffix}")

    records = preprocess_dictionary(args.source, output)
    print(f"Processed {len(records):,} unique words -> {output}")


if __name__ == "__main__":  # pragma: no cover - convenience CLI
    _cli()
