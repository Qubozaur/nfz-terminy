"""Parser PCUS - prognozowanego czasu udzielenia świadczenia.

API NFZ 1.4 nie podaje konkretnej daty pierwszego wolnego terminu, tylko tekstową prognozę
czasu oczekiwania, np. "0 dni", "1 dzień", "24 dni", "2 mies.", "1 mies. 1 tydz.",
"3 mies. 2 tyg.". Do sortowania i porównań zamieniamy ją na przybliżoną liczbę dni.
"""

from __future__ import annotations

import re

DAYS_IN_WEEK = 7
# Przybliżenie: miesiąc w prognozie NFZ traktujemy jako 30 dni
DAYS_IN_MONTH = 30
DAYS_IN_YEAR = 365

_UNITS: dict[str, int] = {
    "d": 1,
    "t": DAYS_IN_WEEK,
    "m": DAYS_IN_MONTH,
    "r": DAYS_IN_YEAR,
    "l": DAYS_IN_YEAR,
}

# liczba + jednostka; jednostkę rozpoznajemy po pierwszej literze (dni/dzień, tydz./tyg.,
# mies./miesiące, rok/lata/lat) - w danych z API występują tylko dni, tygodnie i miesiące.
# Dłuższe formy muszą być przed krótszymi, bo alternatywa bierze pierwsze trafienie.
_PART = re.compile(
    r"(\d+)\s*("
    r"dnia|dni|dzień|dzien"
    r"|tygodnie|tygodni|tydzień|tydzien|tydz\.?|tyg\.?"
    r"|miesiące|miesiace|miesięcy|miesiecy|miesiąc|miesiac|mies\.?"
    r"|lata|lat|rok"
    r")(?![^\W\d_])",
    re.IGNORECASE,
)


def parse_pcus(text: str | None) -> int | None:
    """Zamienia prognozę PCUS na przybliżoną liczbę dni.

    Zwraca None, gdy prognozy brak albo format jest nieznany (wtedy lepiej nie zgadywać).
    """
    if not text:
        return None
    total = 0
    found = False
    position = 0
    for match in _PART.finditer(text):
        # między rozpoznanymi częściami mogą być tylko spacje
        if text[position : match.start()].strip():
            return None
        found = True
        total += int(match.group(1)) * _UNITS[match.group(2)[0].lower()]
        position = match.end()
    if not found or text[position:].strip():
        return None
    return total
