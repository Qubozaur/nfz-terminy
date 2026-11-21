"""Logika wyszukiwania: dopasowanie nazw świadczeń i miejscowości oraz zbieranie kolejek.

Warstwa pomiędzy surowym klientem API a interfejsami (CLI, bot, WWW). Nie wypisuje nic
na ekran i nie pyta użytkownika - zwraca listy kandydatów, a wybór zostawia wywołującemu.
"""

from __future__ import annotations

from dataclasses import dataclass

from nfz_terminy.cache import LocalityCache
from nfz_terminy.client import MIN_NAME_LENGTH, NfzClient, ProgressCallback
from nfz_terminy.models import QueueEntry
from nfz_terminy.text import fold

# Potoczne nazwy, których API nie znajduje samo - głównie przez polskie znaki
# (wyszukiwanie po stronie NFZ rozróżnia "pluc" i "płuc") i skróty badań.
BENEFIT_ALIASES: dict[str, str] = {
    "pluca": "PŁUC",
    "pluc": "PŁUC",
    "pulmonolog": "PŁUC",
    "pulmonologia": "PŁUC",
    "gruzlica": "GRUŹLIC",
    "tomograf": "TOMOGRAFIA KOMPUTEROWA",
    "tomografia": "TOMOGRAFIA KOMPUTEROWA",
    "tk": "TOMOGRAFIA KOMPUTEROWA",
    "ct": "TOMOGRAFIA KOMPUTEROWA",
    "mri": "REZONANS MAGNETYCZNY",
    "mr": "REZONANS MAGNETYCZNY",
    "rezonans": "REZONANS MAGNETYCZNY",
    "dentysta": "STOMATOLOG",
}

# Ile liter maksymalnie obcinamy z końca słowa, szukając rdzenia ("ortopeda" -> "ortoped")
_MAX_SUFFIX_CUT = 3

_OUTPATIENT_PREFIXES = ("poradnia", "swiadczenia z zakresu")


@dataclass(frozen=True)
class SearchResult:
    """Wynik wyszukiwania kolejek do jednego świadczenia."""

    entries: list[QueueEntry]
    without_forecast: int


def _stem(word: str) -> str:
    """Przybliżony rdzeń słowa do porównań ("naczyniowy" -> "naczyni")."""
    return word if len(word) <= 4 else word[: max(4, len(word) - 3)]


def _rank_benefits(names: list[str], query: str) -> list[str]:
    """Kolejność kandydatów: pasujące do wpisanych słów, poradnie przed oddziałami,
    wersje dla dzieci na końcu grupy."""
    stems = [_stem(w) for w in fold(query).split()]

    def key(name: str) -> tuple[bool, int, bool, str]:
        folded = fold(name)
        relevant = all(s in folded for s in stems)
        if folded.startswith(_OUTPATIENT_PREFIXES):
            group = 0
        elif folded.startswith("oddzial"):
            group = 2
        else:
            group = 1
        return (not relevant, group, "dziec" in folded, folded)

    return sorted(dict.fromkeys(names), key=key)


def resolve_benefit(client: NfzClient, query: str) -> list[str]:
    """Zwraca pasujące nazwy świadczeń NFZ, najlepsze na początku.

    Jedna nazwa = jednoznaczne dopasowanie. Pusta lista = nic nie znaleziono.
    Kolejne próby: dokładnie to, co wpisano -> potoczna nazwa z BENEFIT_ALIASES ->
    najdłuższe słowo ze skracaną końcówką (wyniki filtrowane pozostałymi słowami).
    """
    text = " ".join(query.split())
    folded = fold(text)
    if len(text) < MIN_NAME_LENGTH and folded not in BENEFIT_ALIASES:
        raise ValueError(f"Nazwa świadczenia musi mieć co najmniej {MIN_NAME_LENGTH} znaki.")

    candidates: list[str] = []
    if len(text) >= MIN_NAME_LENGTH:
        candidates = client.search_benefits(text)

    if not candidates and folded in BENEFIT_ALIASES:
        candidates = client.search_benefits(BENEFIT_ALIASES[folded])

    if not candidates:
        words = folded.split()
        longest = max(words, key=len)
        other_stems = [_stem(w) for w in words if w != longest]
        for cut in range(1, _MAX_SUFFIX_CUT + 1):
            shorter = longest[:-cut]
            if len(shorter) < MIN_NAME_LENGTH + 1:
                break
            found = client.search_benefits(shorter)
            found = [n for n in found if all(s in fold(n) for s in other_stems)]
            if found:
                candidates = found
                break

    exact = [n for n in candidates if fold(n) == folded]
    if exact:
        return exact[:1]
    return _rank_benefits(candidates, text)


def resolve_locality(
    client: NfzClient,
    query: str,
    province: str | None,
    cache: LocalityCache | None = None,
) -> list[str]:
    """Zwraca pasujące nazwy miejscowości, najlepsze na początku.

    API rozróżnia polskie znaki ("lodz" nie znajduje "ŁÓDŹ"), więc gdy zwykłe wyszukiwanie
    nic nie da, a znamy województwo, pobieramy jego słownik miejscowości (z cache)
    i porównujemy nazwy bez polskich znaków.
    """
    text = " ".join(query.split())
    folded = fold(text)

    names: list[str] = []
    if len(text) >= MIN_NAME_LENGTH:
        names = client.search_localities(text, province)

    if not names and province:
        names = [n for n in _province_localities(client, province, cache) if folded in fold(n)]

    exact = [n for n in names if fold(n) == folded]
    if exact:
        return exact[:1]
    starting = [n for n in names if fold(n).startswith(folded)]
    if len(starting) == 1:
        return starting
    return sorted(dict.fromkeys(names), key=lambda n: (not fold(n).startswith(folded), len(n), n))


def _province_localities(
    client: NfzClient, province: str, cache: LocalityCache | None
) -> list[str]:
    cached = cache.get(province) if cache else None
    if cached is not None:
        return cached
    names = [entry.name for entry in client.list_localities(province)]
    if cache:
        cache.put(province, names)
    return names


def _matches_locality(entry_locality: str | None, wanted: str) -> bool:
    """Miejscowość wpisu musi być tą samą miejscowością albo jej dzielnicą
    ("ŁÓDŹ-BAŁUTY" pasuje do "ŁÓDŹ", ale "BIAŁA PODLASKA" nie pasuje do "BIAŁA")."""
    if not entry_locality:
        return False
    have, want = fold(entry_locality), fold(wanted)
    return have == want or have.startswith(want + "-")


def find_queues(
    client: NfzClient,
    *,
    benefit: str,
    province: str | None = None,
    locality: str | None = None,
    urgent: bool = False,
    on_progress: ProgressCallback | None = None,
) -> SearchResult:
    """Pobiera wszystkie kolejki do świadczenia i zwraca je posortowane od najkrótszej prognozy.

    `benefit` i `locality` powinny być już dokładnymi nazwami z resolve_benefit/
    resolve_locality. Wpisy bez prognozy są pomijane (liczba w `without_forecast`).
    """
    queues = client.queues(
        benefit=benefit,
        province=province,
        locality=locality,
        urgent=urgent,
        on_progress=on_progress,
    )
    wanted_benefit = fold(benefit)
    entries: list[QueueEntry] = []
    without_forecast = 0
    for queue in queues:
        attributes = queue.attributes
        # API dopasowuje podciąg: odrzucamy np. "... DLA DZIECI" przy zapytaniu o wersję ogólną
        if fold(attributes.benefit or "") != wanted_benefit:
            continue
        if locality and not _matches_locality(attributes.locality, locality):
            continue
        entry = QueueEntry.from_queue(queue)
        if entry is None:
            without_forecast += 1
        else:
            entries.append(entry)

    entries.sort(
        key=lambda e: (
            e.wait_days is None,
            e.wait_days if e.wait_days is not None else 0,
            fold(e.provider or ""),
            fold(e.address or ""),
        )
    )
    return SearchResult(entries=entries, without_forecast=without_forecast)
