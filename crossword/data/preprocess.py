"""Dictionary preprocessing helpers for cacheable DataFrames."""

from __future__ import annotations

import argparse
import csv
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

    aggregated: Dict[str, ProcessedWordRecord] = {}

    with source.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            surface = clean_word(row.get("entry_word", ""))
            if not surface:
                continue

            raw_entry = (row.get("entry_word") or "").strip()
            lemma = (row.get("lemma") or "").strip()
            definition = (row.get("definition") or "").strip()
            frequency = _parse_float(row.get("lexeme_frequency"))
            is_compound = _parse_bool(row.get("is_compound"))
            is_stopword = _parse_bool(row.get("is_stopword"))

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
                )
                aggregated[surface] = record

            if raw_entry:
                record.raw_forms.add(raw_entry)
            record.is_compound = record.is_compound or is_compound
            record.is_stopword = record.is_stopword or is_stopword
            if frequency > record.frequency:
                record.frequency = frequency
                record.lemma = lemma
                record.definition = definition

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
            records.append(
                ProcessedWordRecord(
                    surface=(row.get("surface") or "").strip().upper(),
                    length=int(row.get("length") or 0),
                    lemma=(row.get("lemma") or "").strip(),
                    definition=(row.get("definition") or "").strip(),
                    frequency=_parse_float(row.get("frequency")),
                    is_compound=_parse_bool(row.get("is_compound")),
                    is_stopword=_parse_bool(row.get("is_stopword")),
                    raw_forms=set(raw_forms),
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
