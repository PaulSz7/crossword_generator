import unittest

from crossword.data.theme import DummyThemeWordGenerator, ThemeWord, ThemeWordGenerator, merge_theme_generators


class DummyThemeGeneratorTests(unittest.TestCase):
    def test_dummy_generator_returns_requested_limit(self) -> None:
        buckets = {"natura": ["lup", "brad", "munte"]}
        generator = DummyThemeWordGenerator(theme_buckets=buckets, seed=1)
        words = generator.generate("natura", limit=2)
        self.assertEqual(len(words), 2)
        for entry in words:
            self.assertTrue(entry.word.isalpha())

    def test_merge_uses_dummy_when_primary_missing(self) -> None:
        buckets = {"drumetie": ["munte", "lac", "drum"], "default": ["oras"]}
        dummy = DummyThemeWordGenerator(theme_buckets=buckets, seed=2)

        class EmptyGenerator:
            def generate(self, theme: str, limit: int = 80) -> list[ThemeWord]:
                raise RuntimeError("test failure")

        results = merge_theme_generators(EmptyGenerator(), [dummy], "drumetie", 3)
        self.assertGreaterEqual(len(results), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
