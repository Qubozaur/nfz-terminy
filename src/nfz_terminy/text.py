"""Normalizacja tekstu: porównywanie nazw niezależnie od polskich znaków i wielkości liter."""

from __future__ import annotations

# unicodedata.normalize("NFKD") nie rozkłada litery "ł", więc mapujemy jawnie
_FOLD_TABLE = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def fold(text: str) -> str:
    """Sprowadza tekst do postaci porównawczej: bez polskich znaków, małe litery,
    pojedyncze spacje. Np. "  Łódź-Bałuty " -> "lodz-baluty"."""
    return " ".join(text.translate(_FOLD_TABLE).casefold().split())


def city_of(locality: str, county: str | None) -> str:
    """Miasto, do którego należy miejscowość z danych NFZ.

    Dzielnice dużych miast mają postać "WARSZAWA MOKOTÓW" albo "ŁÓDŹ-BAŁUTY", a ich powiat
    to samo miasto: city_of("WARSZAWA MOKOTÓW", "WARSZAWA") -> "WARSZAWA". Inne miejscowości
    zostają bez zmian ("BIAŁA PODLASKA" ma własny powiat). Odpowiednik cityOf w site/logic.js.
    """
    if county and fold(locality) != fold(county):
        have, base = fold(locality), fold(county)
        if have.startswith((base + " ", base + "-")):
            return county
    return locality


def has_polish_chars(text: str) -> bool:
    """Czy tekst zawiera polskie znaki diakrytyczne."""
    return text.translate(_FOLD_TABLE) != text
