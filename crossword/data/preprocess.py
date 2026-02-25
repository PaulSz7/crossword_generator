"""Dictionary preprocessing helpers for cacheable DataFrames."""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .normalization import clean_word, ROMANIAN_DIACRITICS

# Characters accepted in a raw entry_word.  Anything outside this set means
# the word contains a non-Romanian letter (e.g. "ñ", "é", "ö") that would be
# silently dropped by clean_word, producing an invalid surface like "RAA".
# Such entries are skipped entirely during preprocessing.
_VALID_WORD_CHARS: frozenset = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    + "".join(ROMANIAN_DIACRITICS.keys())
)

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

# Only sources whose full definition text is available in the public DB export
# (canDistribute=1 in the Source table).  All other sources have truncated stubs
# and therefore cannot reliably confirm a word's register — they receive a medium
# floor so they never appear in EASY difficulty unless their frequency alone
# pushes the score low enough with a known-distributable source backing them.
_COMMON_SOURCES = frozenset({
    "DEX '96",
    "DEX '98",
    # # DEX editions
    # "DEX '75", "DEX '84", "DEX '96", "DEX '98", "DEX '09", "DEX '12", "DEX '16",
    # "DEX-S", "DEX-școlar",
    # # DOOM editions
    # "DOOM", "DOOM 2", "DOOM 3",
    # # MDA / MDN editions
    # "MDA", "MDA2", "MDN '00", "MDN '08",
    # # Other standard references
    # "DLRLC", "DEXI", "NODEX",
})
# All DEX editions — used to gate tag extraction.  Definitions may be truncated
# in non-distributable editions (DEX '09, '12, …) but register markers like
# (Rar) or (Regional) are usually present even in partial text, so we include
# them in tag inspection regardless.
_DEX_SOURCES = frozenset({
    "DEX '75", "DEX '84", "DEX '96", "DEX '98",
    "DEX '09", "DEX '12", "DEX '16",
    "DEX-S", "DEX-școlar",
})
# Sources whose entire purpose is documenting regional, dialectal, or archaic vocabulary.
# Every word from these dictionaries should carry a HARD floor (0.60) regardless of
# frequency, since the register markers in internalRep are not reliably available.
_HARD_SOURCES = frozenset({
    "DAR",          # Dicționar de arhaisme și regionalisme
    "DRAM",         # Dicționar de regionalisme și arhaisme din Maramureș
    "DRAM 2015",    # Same, edition 2
    "DRAM 2021",    # Same, edition 3
    "Argou",        # Dicționar de argou al limbii române
    "DLRLV",        # Dicț. limbii române literare vechi (1640–1780) – termeni regionali
})
_RARE_SOURCES = frozenset({"DTM", "DGS", "DER", "Scriban", "CADE"})
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
# Prefix roots whose word-boundary form is already in _HARD_TAGS but whose
# feminine / plural inflections (ending in alphabetic "-ă", "-e", "-i") are
# missed by the boundary matcher, plus new roots not represented above.
_HARD_PREFIXES = frozenset({
    "vech",     # vechi, veche, vechiu — archaic-form annotations
    "arha",     # arhaic, arhaică, arhaice — adjectival archaic (arhaism/arhaizant exact)
    "învechi",  # învechită, învechire — inflected "învechit" missed by boundary check
    "regional", # regională, regionale — feminine inflection of "regional"
    "dialectal",# dialectală — feminine inflection of "dialectal"
    "argoti",   # argotică, argotice — feminine inflections of "argotic"
    "neobiș",   # neobișnuită, neobișnuite — inflected "neobișnuit"
})
_MEDIUM_TAGS = frozenset({
    # Marked literary / stylistic registers
    "livresc", "poetic", "figurat", "metaforic", "alegoric",
    "eufemistic", "ironic", "glumeț", "hiperbolic", "emfatic",
    "peiorativ", "depreciativ", "vulgar",
    # Familiar / colloquial (less formal but still recognisable)
    "familiar", "popular",
})
# Prefix roots for feminine/plural inflections of _MEDIUM_TAGS entries.
_MEDIUM_PREFIXES = frozenset({
    "livresc",  # livrescă — feminine inflection of "livresc"
    "poet",     # poetică, poetice — inflected "poetic"
    "figurat",  # figurată, figurate — inflected "figurat"
    "metafor",  # metaforică — inflected "metaforic"
    "famili",   # familiară, familiare — inflected "familiar"
    "peiorat",  # peiorativă — inflected "peiorativ"
    "depreciat",# depreciativă — inflected "depreciativ"
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
_PAREN_RE = re.compile(r"\(([^)]+)\)")


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


def _extract_paren_tags(definition: str) -> set[str]:
    """Extract register/difficulty tags from parenthesised text in a complete internalRep.

    DEX '98/96 sometimes encodes register information inline as plain text,
    e.g. ``(Rar)``, ``(Regional)``, ``(Familiar)``, rather than storing it
    as an ObjectTag.  This scans every ``(…)`` group, lowercases the content,
    strips trailing punctuation, and returns only strings that are recognised
    by our existing tag-scoring vocabulary (i.e. ``_tag_difficulty_score > 0``).

    Only call this for common-source rows — non-distributable sources have
    truncated stubs where parenthesised content is unreliable.
    """
    found: set[str] = set()
    for match in _PAREN_RE.finditer(definition):
        text = match.group(1).strip().rstrip(".").lower()
        if text and _tag_difficulty_score(text) > 0.0:
            found.add(text)
    return found


def _source_rarity_score(source_name: str) -> float:
    if not source_name:
        return 0.5
    name = source_name.strip()
    if name in _COMMON_SOURCES:
        return 0.0
    if name in _HARD_SOURCES:
        return 2.0   # sentinel: guaranteed hard floor in compute_difficulty_score
    if name in _RARE_SOURCES:
        return 1.0
    return 0.5


def _best_source(all_sources_str: str) -> str:
    """Return the least-rare source from a pipe-separated list of source names.

    When the raw TSV exposes all sources an entry appears in (via the
    ``all_sources`` SQL column), we prefer the most standard/common one so
    that a word like "alb" (white) — which happens to also appear in *Argou* —
    is classified by its primary DEX source rather than the slang dictionary.

    Priority: common (0.0) < unknown (0.5) < rare (1.0) < hard (2.0).
    Falls back to the first non-empty token if the list is empty.
    """
    sources = [s.strip() for s in all_sources_str.split("|") if s.strip()]
    if not sources:
        return ""
    return min(sources, key=_source_rarity_score)


def _tag_difficulty_score(tags: str) -> float:
    if not tags:
        return 0.0
    # Split by "|" only; search each segment for scoring keywords.
    parts = [part.strip().lower() for part in tags.split("|") if part.strip()]

    def _matches_boundary(patterns: frozenset) -> bool:
        """Match patterns with word-boundary awareness.

        Single-word patterns require word boundaries on both sides so that
        short patterns like "rar" don't match inside unrelated words such as
        "glife rare" (a typographic metadata field from dexonline) or "rare".

        Multi-word patterns (containing spaces) fall back to simple substring
        matching because they are inherently specific enough (e.g. "ieșit din
        uz", "paradigmă învechită").
        """
        for part in parts:
            for pat in patterns:
                if " " in pat:
                    if pat in part:
                        return True
                else:
                    idx = part.find(pat)
                    while idx != -1:
                        before_ok = idx == 0 or not part[idx - 1].isalpha()
                        after_ok = (
                            idx + len(pat) >= len(part)
                            or not part[idx + len(pat)].isalpha()
                        )
                        if before_ok and after_ok:
                            return True
                        idx = part.find(pat, idx + 1)
        return False

    def _matches_prefix(patterns: frozenset) -> bool:
        """Simple substring match for prefix patterns like 'angl' → 'anglicism'."""
        return any(pat in part for part in parts for pat in patterns)

    if _matches_boundary(_HARD_TAGS) or _matches_prefix(_HARD_PREFIXES):
        return 1.0
    if _matches_boundary(_MEDIUM_TAGS) or _matches_prefix(_MEDIUM_PREFIXES) or _matches_prefix(_FOREIGN_LANG_TAGS):
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
        - Medium/foreign tag or rare source    → floor 0.27  (medium tier)
        - Rare source alone                    → floor 0.35  (solid medium)
        - Hard source (regional/archaic dict)  → floor 0.60  (hard tier)

    ``source_name`` is expected to be the *least-rare* source across all
    definitions for the word (set by ``_best_source`` during preprocessing),
    so the hard-source floor only fires for words that genuinely appear
    exclusively in specialised hard dictionaries.
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
        if frequency < 0.75:
            floor = 0.30
    if source_score >= 2.0:     # hard source (regional/archaic dictionary) → hard tier
        floor = max(floor, 0.60)
    elif source_score == 1.0:   # rare source → at least solid medium
        floor = max(floor, 0.35)
    elif source_score == 0.5:   # other source (truncated definitions) → medium floor
        # High-frequency words are common-usage regardless of which dictionary
        # they come from; allow them back into EASY by skipping the floor.
        if frequency < 0.75:
            floor = max(floor, 0.30)

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

    # Some DEX '98 definitions are large; raise the csv field limit so they
    # don't trigger "_csv.Error: field larger than field limit".
    import sys as _sys
    csv.field_size_limit(_sys.maxsize)

    abbrev_lookup = _load_abbreviation_lookup(source.parent / "distinct_abbreviations.csv")

    aggregated: Dict[str, ProcessedWordRecord] = {}
    # Tracks surfaces whose definition text was already set from a DEX '96/'98 row.
    # Used to prevent lower-quality (truncated) definitions from overwriting it.
    _preferred_def_surfaces: set[str] = set()

    with source.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            if _parse_bool(row.get("is_adult")):
                continue
            raw_entry = (row.get("entry_word") or "").strip()
            if not raw_entry or not all(c in _VALID_WORD_CHARS for c in raw_entry):
                continue
            surface = clean_word(raw_entry)
            if not surface:
                continue
            lemma = (row.get("lemma") or "").strip()
            definition = (row.get("definition") or "").strip()
            frequency = _parse_float(row.get("lexeme_frequency"))
            is_compound = _parse_bool(row.get("is_compound"))
            is_stopword = _parse_bool(row.get("is_stopword"))
            tags = (row.get("tags") or "").strip()
            row_source = (row.get("source_short_name") or "").strip()
            is_common_row = row_source in _COMMON_SOURCES
            is_dex_row = row_source in _DEX_SOURCES
            # Extract tags from all DEX editions: register markers like (Rar) /
            # (Regional) appear even in truncated definitions.  def_abbrevs are
            # also extracted — partial #abbrev# markers are still valid signals,
            # just possibly incomplete.  Non-DEX sources (Argou, DAR, …) are
            # excluded as before.
            def_abbrevs = _extract_def_abbrevs(definition, abbrev_lookup) if is_dex_row else ""
            paren_tags = _extract_paren_tags(definition) if is_dex_row else set()
            definition_count = _parse_int(row.get("definition_count"))
            source_count = _parse_int(row.get("source_count"))
            # all_sources: pipe-separated list of every source this entry appears
            # in (from the SQL all_sources column).  Use it to pick the least-rare
            # (most standard) source name so common words aren't misclassified as
            # hard just because their top-ranked definition came from Argou/DAR.
            all_sources_str = (row.get("all_sources") or "").strip()
            best_src = _best_source(all_sources_str) if all_sources_str else (row.get("source_short_name") or "").strip()

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
                    source_name=best_src,
                    tags=tags,
                    def_abbrevs=def_abbrevs,
                    definition_count=definition_count,
                    source_count=source_count,
                )
                aggregated[surface] = record
                if is_common_row:
                    _preferred_def_surfaces.add(surface)

            if raw_entry:
                record.raw_forms.add(raw_entry)
            record.is_compound = record.is_compound or is_compound
            record.is_stopword = record.is_stopword or is_stopword
            # Merge tags from all rows (ObjectTags + inline paren register markers)
            new_tags: set[str] = set()
            if tags:
                new_tags |= {t.strip() for t in tags.split("|") if t.strip()}
            new_tags |= paren_tags
            if new_tags:
                existing_tags = {t for t in record.tags.split("|") if t}
                record.tags = "|".join(sorted(existing_tags | new_tags))
            # Merge def_abbrevs from all rows
            if def_abbrevs:
                existing = {t for t in record.def_abbrevs.split("|") if t}
                new = {t.strip() for t in def_abbrevs.split("|") if t.strip()}
                record.def_abbrevs = "|".join(sorted(existing | new))
            # Keep max counts across rows
            record.definition_count = max(record.definition_count, definition_count)
            record.source_count = max(record.source_count, source_count)
            # Always track max frequency (drives difficulty scoring).
            if frequency > record.frequency:
                record.frequency = frequency
            # Definition text: prefer DEX '96/'98 (complete definitions).
            # Fall back to highest-frequency row when no common source seen yet.
            if is_common_row and surface not in _preferred_def_surfaces:
                record.lemma = lemma
                record.definition = definition
                _preferred_def_surfaces.add(surface)
            elif surface not in _preferred_def_surfaces and frequency >= record.frequency:
                record.lemma = lemma
                record.definition = definition
            # Source selection: keep the least-rare source seen across all rows.
            # When all_sources is available this is already settled by _best_source;
            # across multiple rows for the same surface we take the minimum again.
            if _source_rarity_score(best_src) < _source_rarity_score(record.source_name):
                record.source_name = best_src

    # Merge scraped tags first (if local_db/scraped_tags.tsv exists), then
    # compute difficulty scores so scraped tags can influence them.
    scraped_path = source.parent / "scraped_tags.tsv"
    records_list = list(aggregated.values())
    _merge_scraped_tags(records_list, scraped_path)

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


def _merge_scraped_tags(records: List[ProcessedWordRecord], scraped_path: Path) -> None:
    """Merge tags from scraped_tags.tsv into records (in-place), if the file exists.

    The scraper (local_db/scrape_dex_tags.py) writes a two-column TSV:
        surface<TAB>tags   (tags is "|"-joined, lowercased)

    Any new tags are unioned into record.tags; difficulty scores are NOT
    recomputed here — the caller does that after this merge.
    """
    if not scraped_path.exists():
        return

    by_surface: Dict[str, ProcessedWordRecord] = {r.surface: r for r in records}
    merged_count = 0

    with scraped_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            surface = (row.get("surface") or "").strip().upper()
            scraped_tags_str = (row.get("tags") or "").strip()
            if not surface or not scraped_tags_str:
                continue
            record = by_surface.get(surface)
            if record is None:
                continue
            existing = {t for t in record.tags.split("|") if t}
            new = {t.strip() for t in scraped_tags_str.split("|") if t.strip()}
            added = new - existing
            if added:
                record.tags = "|".join(sorted(existing | new))
                merged_count += 1

    if merged_count:
        import logging
        logging.getLogger(__name__).info("Merged scraped tags into %d records", merged_count)


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
