import unittest

from crossword.data.theme import DummyThemeWordGenerator, ThemeWord, ThemeWordGenerator, UserWordListGenerator, merge_theme_generators


class DummyThemeGeneratorTests(unittest.TestCase):
    def test_dummy_generator_returns_requested_limit(self) -> None:
        buckets = {"natura": {"EASY": ["lup", "brad", "munte"]}}
        generator = DummyThemeWordGenerator(theme_buckets=buckets, seed=1)
        words = generator.generate("natura", limit=2)
        self.assertEqual(len(words), 2)
        for entry in words:
            self.assertTrue(entry.word.isalpha())

    def test_merge_uses_dummy_when_primary_missing(self) -> None:
        buckets = {
            "drumetie": {"EASY": ["munte", "lac", "drum"]},
            "default": {"EASY": ["oras"]},
        }
        dummy = DummyThemeWordGenerator(theme_buckets=buckets, seed=2)

        class EmptyGenerator:
            def generate(self, theme: str, limit: int = 80,
                         difficulty: str = "MEDIUM", language: str = "Romanian") -> list[ThemeWord]:
                raise RuntimeError("test failure")

        results = merge_theme_generators(EmptyGenerator(), [dummy], "drumetie", 3)
        self.assertGreaterEqual(len(results), 2)


class UserWordListGeneratorTests(unittest.TestCase):
    def test_plain_words_are_uppercased(self) -> None:
        gen = UserWordListGenerator(["zeus", "ares"])
        words = gen.generate("any_theme")
        self.assertEqual([w.word for w in words], ["ZEUS", "ARES"])

    def test_clue_format_splits_word_and_clue(self) -> None:
        gen = UserWordListGenerator(["APOLON:Zeul soarelui"])
        words = gen.generate("any_theme")
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0].word, "APOLON")
        self.assertEqual(words[0].clue, "Zeul soarelui")

    def test_plain_word_has_empty_clue(self) -> None:
        gen = UserWordListGenerator(["ARES"])
        self.assertEqual(gen.generate("")[0].clue, "")

    def test_source_is_always_user(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "ARES:Zeul razboiului"])
        for entry in gen.generate("any_theme"):
            self.assertEqual(entry.source, "user")

    def test_blank_entries_are_skipped(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "", "   ", "ARES"])
        self.assertEqual(len(gen.generate("")), 2)

    def test_generate_ignores_theme_and_limit(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "ARES", "ATHENA"])
        # limit=1 should be ignored — all 3 words returned
        words = gen.generate("mitologie", limit=1)
        self.assertEqual(len(words), 3)

    def test_mixed_plain_and_clue_format(self) -> None:
        gen = UserWordListGenerator(["ZEUS:Rege", "ARES", "ATHENA:Intelepciune"])
        words = gen.generate("")
        self.assertEqual(words[0].clue, "Rege")
        self.assertEqual(words[1].clue, "")
        self.assertEqual(words[2].clue, "Intelepciune")

    def test_whitespace_trimmed_from_word_and_clue(self) -> None:
        gen = UserWordListGenerator([" APOLON : Zeul soarelui "])
        w = gen.generate("")[0]
        self.assertEqual(w.word, "APOLON")
        self.assertEqual(w.clue, "Zeul soarelui")


class MergeWithUserWordsTests(unittest.TestCase):
    _DUMMY_BUCKETS = {
        "mitologie": {"EASY": ["HERMES", "HERA", "DIANA", "POSEIDON", "APOLLO"]},
    }

    def _dummy(self) -> DummyThemeWordGenerator:
        return DummyThemeWordGenerator(theme_buckets=self._DUMMY_BUCKETS, seed=0)

    def test_words_only_returns_only_user_words(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "ARES"])
        result = merge_theme_generators(gen, [], "mitologie", target=10)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(w.source == "user" for w in result))

    def test_words_only_no_dummy_words_added(self) -> None:
        gen = UserWordListGenerator(["ZEUS"])
        result = merge_theme_generators(gen, [], "mitologie", target=10)
        self.assertFalse(any(w.source == "dummy" for w in result))

    def test_hybrid_user_words_appear_first(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "ARES"])
        result = merge_theme_generators(gen, [self._dummy()], "mitologie", target=10)
        self.assertEqual(result[0].source, "user")
        self.assertEqual(result[1].source, "user")

    def test_hybrid_extends_with_dummy_when_user_words_insufficient(self) -> None:
        gen = UserWordListGenerator(["ZEUS"])
        result = merge_theme_generators(gen, [self._dummy()], "mitologie", target=4)
        user_words = [w for w in result if w.source == "user"]
        dummy_words = [w for w in result if w.source == "dummy"]
        self.assertEqual(len(user_words), 1)
        self.assertGreater(len(dummy_words), 0)
        self.assertEqual(len(result), 4)

    def test_hybrid_deduplicates_across_sources(self) -> None:
        # HERMES appears in both user list and dummy bucket
        gen = UserWordListGenerator(["HERMES"])
        result = merge_theme_generators(gen, [self._dummy()], "mitologie", target=10)
        surfaces = [w.word for w in result]
        self.assertEqual(len(surfaces), len(set(surfaces)))
        # The user version should win (appears first)
        hermes = next(w for w in result if w.word == "HERMES")
        self.assertEqual(hermes.source, "user")

    def test_hybrid_dummy_fallback_when_primary_raises(self) -> None:
        class FailingGenerator:
            def generate(self, theme: str, limit: int = 80,
                         difficulty: str = "MEDIUM", language: str = "Romanian") -> list[ThemeWord]:
                raise RuntimeError("API unavailable")

        result = merge_theme_generators(FailingGenerator(), [self._dummy()], "mitologie", target=3)
        self.assertGreater(len(result), 0)
        self.assertTrue(all(w.source == "dummy" for w in result))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
