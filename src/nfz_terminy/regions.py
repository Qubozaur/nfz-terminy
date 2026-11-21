"""Kody województw używane przez API NFZ (01-16, alfabetycznie)."""

from __future__ import annotations

from nfz_terminy.text import fold

PROVINCES: dict[str, str] = {
    "01": "dolnośląskie",
    "02": "kujawsko-pomorskie",
    "03": "lubelskie",
    "04": "lubuskie",
    "05": "łódzkie",
    "06": "małopolskie",
    "07": "mazowieckie",
    "08": "opolskie",
    "09": "podkarpackie",
    "10": "podlaskie",
    "11": "pomorskie",
    "12": "śląskie",
    "13": "świętokrzyskie",
    "14": "warmińsko-mazurskie",
    "15": "wielkopolskie",
    "16": "zachodniopomorskie",
}

# Minimalna długość skrótu nazwy (np. "maz" -> mazowieckie), żeby uniknąć przypadkowych trafień
_MIN_PREFIX = 3


def _normalize(name: str) -> str:
    """Nazwa województwa do porównań: bez polskich znaków, bez "woj.", spacje jak myślniki."""
    value = fold(name)
    for prefix in ("województwo ", "wojewodztwo ", "woj. ", "woj "):
        value = value.removeprefix(prefix)
    return value.replace(" ", "-")


_BY_NAME = {_normalize(name): code for code, name in PROVINCES.items()}


def parse_province(value: str) -> str:
    """Zamienia kod lub nazwę województwa na dwucyfrowy kod NFZ.

    Akceptuje: "12", "2" (-> "02"), "śląskie", "slaskie", "Śląskie", "woj. śląskie",
    "kujawsko pomorskie" oraz jednoznaczne skróty od 3 znaków ("maz", "wielkop").
    Rzuca ValueError z podpowiedzią, gdy nie da się dopasować.
    """
    raw = value.strip()
    if raw.isdigit():
        code = raw.zfill(2)
        if code in PROVINCES:
            return code
        raise ValueError(f"Nieznany kod województwa: {raw!r}. Dostępne kody: 01-16.")

    key = _normalize(raw)
    if key in _BY_NAME:
        return _BY_NAME[key]

    if len(key) >= _MIN_PREFIX:
        matches = sorted({code for name, code in _BY_NAME.items() if name.startswith(key)})
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(PROVINCES[c] for c in matches)
            raise ValueError(f"Niejednoznaczne województwo {raw!r}: pasuje {names}.")

    raise ValueError(
        f"Nieznane województwo: {raw!r}. Przykłady: śląskie, mazowieckie, 12. "
        "Pełną listę pokaże komenda: nfz wojewodztwa"
    )
