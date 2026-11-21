"""Normalizacja tekstu: porównywanie nazw niezależnie od polskich znaków i wielkości liter."""

from __future__ import annotations

# unicodedata.normalize("NFKD") nie rozkłada litery "ł", więc mapujemy jawnie
_FOLD_TABLE = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def fold(text: str) -> str:
    """Sprowadza tekst do postaci porównawczej: bez polskich znaków, małe litery,
    pojedyncze spacje. Np. "  Łódź-Bałuty " -> "lodz-baluty"."""
    return " ".join(text.translate(_FOLD_TABLE).casefold().split())


def has_polish_chars(text: str) -> bool:
    """Czy tekst zawiera polskie znaki diakrytyczne."""
    return text.translate(_FOLD_TABLE) != text
