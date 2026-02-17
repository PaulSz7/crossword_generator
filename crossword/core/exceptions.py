"""Custom exception hierarchy for crossword generation."""


class CrosswordError(Exception):
    """Base exception for generator failures."""


class DictionaryLoadError(CrosswordError):
    """Raised when the dictionary TSV cannot be parsed."""


class SlotPlacementError(CrosswordError):
    """Raised when a word slot cannot be placed without breaking rules."""


class ClueBoxError(CrosswordError):
    """Raised when a clue box violates adjacency or licensing rules."""


class ThemeWordError(CrosswordError):
    """Raised when the required theme word set cannot be generated."""


class ValidationError(CrosswordError):
    """Raised when the crossword integrity checks fail."""
