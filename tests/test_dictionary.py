import tempfile
import unittest
from pathlib import Path

from crossword.data.dictionary import DictionaryConfig, WordDictionary
from crossword.data.normalization import clean_word


class DictionaryTests(unittest.TestCase):
    def test_clean_word_removes_diacritics(self) -> None:
        self.assertEqual(clean_word("ăâîșț"), "AAIST")

    def test_dictionary_filters_and_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sample = Path(tmpdir) / "sample.tsv"
            sample.write_text(
                "entry_word\tentry_length\tdefinition\tdefinition_length\tlemma\tlexeme_frequency\tis_compound\tis_stopword\n"
                "șarpe\t5\tReptila\t7\tșarpe\t0.9\t0\t0\n"
                "sarpe\t5\tReptila\t7\tșarpe\t0.5\t0\t0\n",
                encoding="utf-8",
            )

            dictionary = WordDictionary(DictionaryConfig(path=sample, min_length=2))
            entry = dictionary.get("sarpe")
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.surface, "SARPE")
            self.assertAlmostEqual(entry.frequency, 0.9)
            candidates = dictionary.find_candidates(5, pattern=["S", None, None, None, "E"])
            self.assertEqual([c.surface for c in candidates], ["SARPE"])

    def test_processed_cache_is_persisted_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sample = Path(tmpdir) / "sample.tsv"
            sample.write_text(
                "entry_word\tentry_length\tdefinition\tdefinition_length\tlemma\tlexeme_frequency\tis_compound\tis_stopword\n"
                "șarpe\t5\tReptila\t7\tșarpe\t0.9\t0\t0\n",
                encoding="utf-8",
            )

            processed = Path(tmpdir) / "custom_cache.tsv"
            dictionary = WordDictionary(
                DictionaryConfig(path=sample, processed_cache=processed)
            )
            self.assertTrue(processed.exists())
            entry = dictionary.get("sarpe")
            assert entry is not None
            self.assertAlmostEqual(entry.frequency, 0.9)

            # Modify source to verify we now rely on the cached DataFrame
            sample.write_text(
                "entry_word\tentry_length\tdefinition\tdefinition_length\tlemma\tlexeme_frequency\tis_compound\tis_stopword\n"
                "șarpe\t5\tReptila\t7\tșarpe\t0.1\t0\t0\n",
                encoding="utf-8",
            )

            cached_dictionary = WordDictionary(
                DictionaryConfig(path=sample, processed_cache=processed)
            )
            cached_entry = cached_dictionary.get("sarpe")
            assert cached_entry is not None
            self.assertAlmostEqual(cached_entry.frequency, 0.9)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
