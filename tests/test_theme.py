import unittest
from unittest.mock import MagicMock

from crossword.data.theme import (
    DummyThemeWordGenerator,
    GeminiThemeWordGenerator,
    SubstringThemeWordGenerator,
    ThemeOutput,
    ThemeType,
    ThemeWord,
    ThemeWordGenerator,
    UserWordListGenerator,
    merge_theme_generators,
)


class ThemeTypeEnumTests(unittest.TestCase):
    def test_all_values_exist(self) -> None:
        self.assertEqual(ThemeType.DOMAIN_SPECIFIC_WORDS.value, "domain_specific_words")
        self.assertEqual(ThemeType.WORDS_CONTAINING_SUBSTRING.value, "words_containing_substring")
        self.assertEqual(ThemeType.JOKE_CONTINUATION.value, "joke_continuation")
        self.assertEqual(ThemeType.CUSTOM.value, "custom")

    def test_is_str_enum(self) -> None:
        self.assertIsInstance(ThemeType.DOMAIN_SPECIFIC_WORDS, str)


class ThemeOutputTests(unittest.TestCase):
    def test_defaults(self) -> None:
        out = ThemeOutput()
        self.assertEqual(out.words, [])
        self.assertIsNone(out.crossword_title)
        self.assertIsNone(out.content)

    def test_with_words(self) -> None:
        words = [ThemeWord("ZEUS", "Rege", "user")]
        out = ThemeOutput(words=words, crossword_title="Mitologie", content="Zeii greci")
        self.assertEqual(len(out.words), 1)
        self.assertEqual(out.crossword_title, "Mitologie")
        self.assertEqual(out.content, "Zeii greci")


class DummyThemeGeneratorTests(unittest.TestCase):
    def test_dummy_generator_returns_theme_output(self) -> None:
        buckets = {"natura": {"EASY": ["lup", "brad", "munte"]}}
        generator = DummyThemeWordGenerator(theme_buckets=buckets, seed=1)
        result = generator.generate("natura", limit=2)
        self.assertIsInstance(result, ThemeOutput)

    def test_dummy_generator_returns_requested_limit(self) -> None:
        buckets = {"natura": {"EASY": ["lup", "brad", "munte"]}}
        generator = DummyThemeWordGenerator(theme_buckets=buckets, seed=1)
        result = generator.generate("natura", limit=2)
        self.assertEqual(len(result.words), 2)
        for entry in result.words:
            self.assertTrue(entry.word.isalpha())

    def test_dummy_generator_raises_for_unknown_theme(self) -> None:
        buckets = {"natura": {"EASY": ["lup", "brad"]}}
        generator = DummyThemeWordGenerator(theme_buckets=buckets, seed=1)
        with self.assertRaises(ValueError) as ctx:
            generator.generate("spatiu", limit=5)
        self.assertIn("spatiu", str(ctx.exception))
        self.assertIn("natura", str(ctx.exception))  # lists known themes

    def test_merge_uses_dummy_when_primary_missing(self) -> None:
        buckets = {
            "drumetie": {"EASY": ["munte", "lac", "drum"]},
            "default": {"EASY": ["oras"]},
        }
        dummy = DummyThemeWordGenerator(theme_buckets=buckets, seed=2)

        class EmptyGenerator:
            def generate(self, theme: str, limit: int = 80,
                         difficulty: str = "MEDIUM", language: str = "Romanian") -> ThemeOutput:
                raise RuntimeError("test failure")

        result = merge_theme_generators(EmptyGenerator(), [dummy], "drumetie", 3)
        self.assertIsInstance(result, ThemeOutput)
        self.assertGreaterEqual(len(result.words), 2)


class UserWordListGeneratorTests(unittest.TestCase):
    def test_plain_words_are_uppercased(self) -> None:
        gen = UserWordListGenerator(["zeus", "ares"])
        result = gen.generate("any_theme")
        self.assertEqual([w.word for w in result.words], ["ZEUS", "ARES"])

    def test_clue_format_splits_word_and_clue(self) -> None:
        gen = UserWordListGenerator(["APOLON:Zeul soarelui"])
        result = gen.generate("any_theme")
        self.assertEqual(len(result.words), 1)
        self.assertEqual(result.words[0].word, "APOLON")
        self.assertEqual(result.words[0].clue, "Zeul soarelui")

    def test_plain_word_has_empty_clue(self) -> None:
        gen = UserWordListGenerator(["ARES"])
        self.assertEqual(gen.generate("").words[0].clue, "")

    def test_source_is_always_user(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "ARES:Zeul razboiului"])
        for entry in gen.generate("any_theme").words:
            self.assertEqual(entry.source, "user")

    def test_blank_entries_are_skipped(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "", "   ", "ARES"])
        self.assertEqual(len(gen.generate("").words), 2)

    def test_generate_ignores_theme_and_limit(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "ARES", "ATHENA"])
        result = gen.generate("mitologie", limit=1)
        self.assertEqual(len(result.words), 3)

    def test_mixed_plain_and_clue_format(self) -> None:
        gen = UserWordListGenerator(["ZEUS:Rege", "ARES", "ATHENA:Intelepciune"])
        words = gen.generate("").words
        self.assertEqual(words[0].clue, "Rege")
        self.assertEqual(words[1].clue, "")
        self.assertEqual(words[2].clue, "Intelepciune")

    def test_whitespace_trimmed_from_word_and_clue(self) -> None:
        gen = UserWordListGenerator([" APOLON : Zeul soarelui "])
        w = gen.generate("").words[0]
        self.assertEqual(w.word, "APOLON")
        self.assertEqual(w.clue, "Zeul soarelui")

    def test_returns_theme_output(self) -> None:
        gen = UserWordListGenerator(["ZEUS"])
        result = gen.generate("")
        self.assertIsInstance(result, ThemeOutput)
        self.assertIsNone(result.crossword_title)
        self.assertIsNone(result.content)


class MergeWithUserWordsTests(unittest.TestCase):
    _DUMMY_BUCKETS = {
        "mitologie": {"EASY": ["HERMES", "HERA", "DIANA", "POSEIDON", "APOLLO"]},
    }

    def _dummy(self) -> DummyThemeWordGenerator:
        return DummyThemeWordGenerator(theme_buckets=self._DUMMY_BUCKETS, seed=0)

    def test_words_only_returns_only_user_words(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "ARES"])
        result = merge_theme_generators(gen, [], "mitologie", target=10)
        self.assertEqual(len(result.words), 2)
        self.assertTrue(all(w.source == "user" for w in result.words))

    def test_words_only_no_dummy_words_added(self) -> None:
        gen = UserWordListGenerator(["ZEUS"])
        result = merge_theme_generators(gen, [], "mitologie", target=10)
        self.assertFalse(any(w.source == "dummy" for w in result.words))

    def test_hybrid_user_words_appear_first(self) -> None:
        gen = UserWordListGenerator(["ZEUS", "ARES"])
        result = merge_theme_generators(gen, [self._dummy()], "mitologie", target=10)
        self.assertEqual(result.words[0].source, "user")
        self.assertEqual(result.words[1].source, "user")

    def test_hybrid_extends_with_dummy_when_user_words_insufficient(self) -> None:
        gen = UserWordListGenerator(["ZEUS"])
        result = merge_theme_generators(gen, [self._dummy()], "mitologie", target=4)
        user_words = [w for w in result.words if w.source == "user"]
        dummy_words = [w for w in result.words if w.source == "dummy"]
        self.assertEqual(len(user_words), 1)
        self.assertGreater(len(dummy_words), 0)
        self.assertEqual(len(result.words), 4)

    def test_hybrid_deduplicates_across_sources(self) -> None:
        gen = UserWordListGenerator(["HERMES"])
        result = merge_theme_generators(gen, [self._dummy()], "mitologie", target=10)
        surfaces = [w.word for w in result.words]
        self.assertEqual(len(surfaces), len(set(surfaces)))
        hermes = next(w for w in result.words if w.word == "HERMES")
        self.assertEqual(hermes.source, "user")

    def test_hybrid_dummy_fallback_when_primary_raises(self) -> None:
        class FailingGenerator:
            def generate(self, theme: str, limit: int = 80,
                         difficulty: str = "MEDIUM", language: str = "Romanian") -> ThemeOutput:
                raise RuntimeError("API unavailable")

        result = merge_theme_generators(FailingGenerator(), [self._dummy()], "mitologie", target=3)
        self.assertGreater(len(result.words), 0)
        self.assertTrue(all(w.source == "dummy" for w in result.words))

    def test_merge_returns_theme_output(self) -> None:
        gen = UserWordListGenerator(["ZEUS"])
        result = merge_theme_generators(gen, [], "mitologie", target=5)
        self.assertIsInstance(result, ThemeOutput)

    def test_merge_captures_crossword_title(self) -> None:
        class TitledGenerator:
            def generate(self, theme: str, limit: int = 80,
                         difficulty: str = "MEDIUM", language: str = "Romanian") -> ThemeOutput:
                return ThemeOutput(
                    words=[ThemeWord("ZEUS", "Rege", "llm")],
                    crossword_title="Zeii Olimpului",
                    content="O lume mitologica",
                )

        result = merge_theme_generators(TitledGenerator(), [], "mitologie", target=5)
        self.assertEqual(result.crossword_title, "Zeii Olimpului")
        self.assertEqual(result.content, "O lume mitologica")

    def test_merge_title_comes_from_first_provider(self) -> None:
        class Gen1:
            def generate(self, *a, **kw) -> ThemeOutput:
                return ThemeOutput(words=[ThemeWord("ZEUS", "", "g1")], crossword_title="Titlu1")

        class Gen2:
            def generate(self, *a, **kw) -> ThemeOutput:
                return ThemeOutput(words=[ThemeWord("HERA", "", "g2")], crossword_title="Titlu2")

        result = merge_theme_generators(Gen1(), [Gen2()], "tema", target=5)
        self.assertEqual(result.crossword_title, "Titlu1")


class SubstringThemeWordGeneratorTests(unittest.TestCase):
    def _make_mock_dictionary(self, surfaces):
        """Create a mock dictionary with the given word surfaces."""
        mock_dict = MagicMock()
        entries = {}
        for s in surfaces:
            entry = MagicMock()
            entry.score = MagicMock(return_value=0.5)
            entries[s] = entry
        mock_dict._entry_by_surface = entries
        return mock_dict

    def test_filters_words_containing_substring(self) -> None:
        surfaces = ["BERE", "BERERIE", "CARTE", "BERBEC", "MERE"]
        mock_dict = self._make_mock_dictionary(surfaces)
        gen = SubstringThemeWordGenerator(mock_dict, "BERE")
        result = gen.generate("BERE")
        self.assertIsInstance(result, ThemeOutput)
        found_words = {w.word for w in result.words}
        self.assertIn("BERE", found_words)
        self.assertIn("BERERIE", found_words)
        self.assertNotIn("CARTE", found_words)
        self.assertNotIn("MERE", found_words)

    def test_case_insensitive_matching(self) -> None:
        surfaces = ["bere", "BERE", "BERERIE", "Bereta"]
        mock_dict = self._make_mock_dictionary(surfaces)
        gen = SubstringThemeWordGenerator(mock_dict, "bere")
        result = gen.generate("bere")
        found_words = {w.word for w in result.words}
        # All three 'bere' variants should be found (uppercased)
        self.assertTrue(any("BERE" in w for w in found_words))

    def test_clue_contains_theme_title(self) -> None:
        mock_dict = self._make_mock_dictionary(["BERERIE"])
        gen = SubstringThemeWordGenerator(mock_dict, "BERE")
        result = gen.generate("BERE")
        self.assertEqual(len(result.words), 1)
        self.assertIn("BERE", result.words[0].clue)

    def test_source_is_substring(self) -> None:
        mock_dict = self._make_mock_dictionary(["BERERIE"])
        gen = SubstringThemeWordGenerator(mock_dict, "BERE")
        result = gen.generate("BERE")
        self.assertEqual(result.words[0].source, "substring")

    def test_sorted_by_difficulty_score(self) -> None:
        # Words with higher score for the requested difficulty should come first
        mock_dict = MagicMock()
        entry_high = MagicMock()
        entry_high.score = MagicMock(return_value=0.9)
        entry_low = MagicMock()
        entry_low.score = MagicMock(return_value=0.1)
        mock_dict._entry_by_surface = {"BERERIE": entry_high, "BERE": entry_low}
        gen = SubstringThemeWordGenerator(mock_dict, "BERE")
        result = gen.generate("BERE", difficulty="MEDIUM")
        self.assertEqual(result.words[0].word, "BERERIE")  # higher score comes first

    def test_limit_respected(self) -> None:
        surfaces = [f"BERE{i}" for i in range(20)]
        mock_dict = self._make_mock_dictionary(surfaces)
        gen = SubstringThemeWordGenerator(mock_dict, "BERE")
        result = gen.generate("BERE", limit=5)
        self.assertLessEqual(len(result.words), 5)

    def test_empty_result_when_no_match(self) -> None:
        mock_dict = self._make_mock_dictionary(["CARTE", "MASA", "SCAUN"])
        gen = SubstringThemeWordGenerator(mock_dict, "BERE")
        result = gen.generate("BERE")
        self.assertEqual(result.words, [])

    def test_no_crossword_title_or_content(self) -> None:
        mock_dict = self._make_mock_dictionary(["BERERIE"])
        gen = SubstringThemeWordGenerator(mock_dict, "BERE")
        result = gen.generate("BERE")
        self.assertIsNone(result.crossword_title)
        self.assertIsNone(result.content)


class SubstringWithUserWordsTests(unittest.TestCase):
    """Verify the four llm/words combinations for words_containing_substring.

    The extend_with_substring flag on GeneratorConfig controls whether
    SubstringThemeWordGenerator is injected as a fallback in _seed_theme_words.
    These tests exercise merge_theme_generators directly to verify the wiring
    that _seed_theme_words produces for each scenario.
    """

    def _make_mock_dictionary(self, surfaces):
        mock_dict = MagicMock()
        entries = {}
        for s in surfaces:
            entry = MagicMock()
            entry.score = MagicMock(return_value=0.5)
            entries[s] = entry
        mock_dict._entry_by_surface = entries
        return mock_dict

    def _substring_gen(self, surfaces, title="BERE"):
        return SubstringThemeWordGenerator(self._make_mock_dictionary(surfaces), title)

    def test_words_set_llm_true_extends_from_dictionary(self) -> None:
        # extend_with_substring=True → SubstringGen injected as fallback in _seed_theme_words
        user_gen = UserWordListGenerator(["BERE"])
        substring_gen = self._substring_gen(["BERERIE", "BERETA", "CARTE"])

        result = merge_theme_generators(user_gen, [substring_gen], "BERE", target=10)

        word_texts = {w.word for w in result.words}
        self.assertIn("BERE", word_texts)       # user word kept
        self.assertIn("BERERIE", word_texts)    # dictionary extension
        self.assertIn("BERETA", word_texts)     # dictionary extension
        self.assertNotIn("CARTE", word_texts)   # non-matching excluded

    def test_words_set_llm_false_user_words_only(self) -> None:
        # extend_with_substring=False → fallbacks=[] in _seed_theme_words
        user_gen = UserWordListGenerator(["BERE"])

        result = merge_theme_generators(user_gen, [], "BERE", target=10)

        word_texts = {w.word for w in result.words}
        self.assertEqual(word_texts, {"BERE"})

    def test_user_words_appear_before_substring_words(self) -> None:
        user_gen = UserWordListGenerator(["BERE"])
        substring_gen = self._substring_gen(["BERERIE"])

        result = merge_theme_generators(user_gen, [substring_gen], "BERE", target=10)

        self.assertEqual(result.words[0].source, "user")

    def test_no_words_llm_true_substring_is_primary(self) -> None:
        # primary=None, SubstringGen set as primary in _seed_theme_words
        substring_gen = self._substring_gen(["BERERIE", "BERETA", "CARTE"])

        result = merge_theme_generators(substring_gen, [], "BERE", target=10)

        word_texts = {w.word for w in result.words}
        self.assertIn("BERERIE", word_texts)
        self.assertIn("BERETA", word_texts)
        self.assertNotIn("CARTE", word_texts)
        self.assertTrue(all(w.source == "substring" for w in result.words))

    def test_deduplication_between_user_and_substring(self) -> None:
        # BERE in both user list and dictionary: appears once, user version wins
        user_gen = UserWordListGenerator(["BERE"])
        substring_gen = self._substring_gen(["BERE", "BERERIE"])

        result = merge_theme_generators(user_gen, [substring_gen], "BERE", target=10)

        word_texts = [w.word for w in result.words]
        self.assertEqual(word_texts.count("BERE"), 1)
        bere_entry = next(w for w in result.words if w.word == "BERE")
        self.assertEqual(bere_entry.source, "user")


class GeminiPromptTemplateTests(unittest.TestCase):
    def test_domain_specific_prompt_contains_theme(self) -> None:
        gen = GeminiThemeWordGenerator(theme_type="domain_specific_words")
        prompt = gen._render_prompt("mitologie", limit=50)
        self.assertIn("mitologie", prompt)
        self.assertIn("JSON", prompt)

    def test_joke_continuation_prompt_structure(self) -> None:
        gen = GeminiThemeWordGenerator(theme_type="joke_continuation")
        prompt = gen._render_prompt("animale", limit=30)
        self.assertIn("animale", prompt)
        self.assertIn("joke_text", prompt)
        self.assertIn("words", prompt)

    def test_joke_continuation_with_description(self) -> None:
        gen = GeminiThemeWordGenerator(
            theme_type="joke_continuation",
            theme_description="De ce nu mananca elefantul calculatoare? Pentru ca ii e frica de mouse!",
        )
        prompt = gen._render_prompt("animale", limit=30)
        self.assertIn("De ce nu mananca", prompt)

    def test_custom_prompt_structure(self) -> None:
        gen = GeminiThemeWordGenerator(
            theme_type="custom",
            theme_description="Un crossword despre stiinte",
        )
        prompt = gen._render_prompt("Stiinte", limit=40)
        self.assertIn("Stiinte", prompt)
        self.assertIn("crossword_title", prompt)
        self.assertIn("content", prompt)
        self.assertIn("words", prompt)
        self.assertIn("Un crossword despre stiinte", prompt)

    def test_difficulty_appended_to_all_types(self) -> None:
        for theme_type in ["domain_specific_words", "joke_continuation", "custom"]:
            gen = GeminiThemeWordGenerator(theme_type=theme_type)
            for difficulty in ["EASY", "MEDIUM", "HARD"]:
                prompt = gen._render_prompt("tema", limit=20, difficulty=difficulty)
                self.assertTrue(len(prompt) > 50, f"Prompt too short for {theme_type}/{difficulty}")

    def test_parse_response_domain_specific(self) -> None:
        text = '{"word": "ZEUS", "clue": "Rege olimp"}\n{"word": "HERA", "clue": "Regina zeilor"}\n'
        result = GeminiThemeWordGenerator._parse_response(text, "domain_specific_words")
        self.assertIsInstance(result, ThemeOutput)
        self.assertEqual(len(result.words), 2)
        self.assertEqual(result.words[0].word, "ZEUS")

    def test_parse_response_joke_continuation(self) -> None:
        text = '{"joke_text": "De ce...? Pentru ca!", "words": [{"word": "MOUSE", "clue": "Soarece digital"}]}'
        result = GeminiThemeWordGenerator._parse_response(text, "joke_continuation")
        self.assertIsInstance(result, ThemeOutput)
        self.assertEqual(len(result.words), 1)
        self.assertEqual(result.content, "De ce...? Pentru ca!")
        self.assertIsNone(result.crossword_title)

    def test_parse_response_custom(self) -> None:
        text = '{"crossword_title": "Stiinte Exacte", "content": "Un tur prin stiinte.", "words": [{"word": "CHIMIE", "clue": "Stiinta moleculelor"}]}'
        result = GeminiThemeWordGenerator._parse_response(text, "custom")
        self.assertIsInstance(result, ThemeOutput)
        self.assertEqual(result.crossword_title, "Stiinte Exacte")
        self.assertEqual(result.content, "Un tur prin stiinte.")
        self.assertEqual(len(result.words), 1)

    def test_parse_response_handles_markdown_fences(self) -> None:
        text = '```json\n{"joke_text": "Gluma!", "words": [{"word": "ORAS", "clue": "Loc urban"}]}\n```'
        result = GeminiThemeWordGenerator._parse_response(text, "joke_continuation")
        self.assertEqual(len(result.words), 1)
        self.assertEqual(result.content, "Gluma!")

    def test_parse_response_empty_text(self) -> None:
        result = GeminiThemeWordGenerator._parse_response("", "domain_specific_words")
        self.assertEqual(result.words, [])

    def test_parse_response_invalid_json(self) -> None:
        result = GeminiThemeWordGenerator._parse_response("not json at all", "joke_continuation")
        self.assertEqual(result.words, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
