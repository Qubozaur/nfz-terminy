"""Geokodowanie adresów placówek przez Uniwersalną Usługę Geokodowania GUGiK (UUG).

API NFZ podaje współrzędne tylko dla części placówek (np. w Łodzi prawie wszystkich,
w Katowicach żadnej), więc brakujące uzupełniamy na podstawie adresu. UUG korzysta
z państwowego rejestru punktów adresowych, a kod gminy TERYT z danych NFZ pozwala
odróżnić miejscowości o tej samej nazwie (np. kilka wsi "Jasienica").

Kolejne próby dla jednego adresu:
1. "Miasto, Ulica Numer" zawężone do gminy (parametr teryt),
2. to samo bez zawężenia, ale wynik musi leżeć w tym samym powiecie - dzielnice
   Warszawy czy Poznania mają w NFZ kod dzielnicy, a w GUGiK kod całego miasta,
3. "Wieś Numer" - dla miejscowości bez ulic,
4. sama ulica (jej środek) - wynik oznaczony jako przybliżony.

Wyniki (także nieudane) trafiają do cache w pliku JSON, żeby każdy adres sprawdzać raz.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

from nfz_terminy import __version__
from nfz_terminy.text import fold

UUG_URL = "https://services.gugik.gov.pl/uug/"
USER_AGENT = f"nfz-terminy/{__version__}"

# Po ilu dniach ponowić adres, którego nie udało się znaleźć (rejestr adresów się zmienia)
RETRY_NOT_FOUND_AFTER = timedelta(days=30)

# przedrostek "UL." na początku; "AL.", "PL.", "OS." zostają, bo odróżniają np. aleję od placu
_PREFIX = re.compile(r"^(?:UL|ULICA)\b\.?\s*", re.I)
# inicjały imion i skróty tytułów w nazwach ulic ("K. MARCINKOWSKIEGO", "KS. J. POPIEŁUSZKI") -
# w rejestrze adresów są rozwinięte ("Księdza Jerzego"), a samo nazwisko UUG dopasowuje lepiej
_INITIALS = re.compile(
    r"(?<![\w.])(?:[A-ZĄĆĘŁŃÓŚŹŻ]|KS|GEN|PROF|DR|MJR|PŁK|KPT|KARD|BPA?|ABPA?)\.\s*", re.I
)
# numer budynku = ostatnia liczba w adresie: "13", "5A", "30 A", "14/5" (lokal), "4-8" (zakres);
# wcześniejsze liczby należą do nazwy ulicy ("HARCERZY WRZEŚNIA 1939 5", "3 MAJA 10")
_NUMBER = re.compile(r"\s(\d+)\s?([A-Z])?(?:\s*[/-]+\s*\w+)?\s*$", re.I)


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float
    approximate: bool  # True = środek ulicy, a nie konkretny budynek


@dataclass
class GeocodeBatch:
    """Wynik locate_many: punkty wg klucza cache oraz statystyka do logów."""

    points: dict[str, GeoPoint | None] = field(default_factory=dict)
    from_cache: int = 0
    found: int = 0
    not_found: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class Address:
    """Adres rozłożony na części do zapytań UUG."""

    city: str
    street: str | None
    number: str | None


def split_address(city: str, address: str) -> Address:
    """Rozkłada adres z NFZ na ulicę i numer, usuwając "UL.", inicjały i numer lokalu.

    >>> split_address("KATOWICE", "UL. MORAWA 31")
    Address(city='KATOWICE', street='MORAWA', number='31')
    >>> split_address("STRACHOCINA", "STRACHOCINA 341")
    Address(city='STRACHOCINA', street=None, number='341')
    """
    text = " ".join(address.replace(".", ". ").split())
    match = _NUMBER.search(" " + text)
    number = None
    if match:
        number = match.group(1) + (match.group(2) or "").upper()
        text = (" " + text)[: match.start()].strip()
    cleaned = " ".join(_INITIALS.sub(" ", _PREFIX.sub("", text)).split()).strip(" ,")
    street: str | None = cleaned or None
    # wieś bez ulic: NFZ powtarza nazwę miejscowości w polu adresu
    if street and fold(street) == fold(city):
        street = None
    return Address(city=city, street=street, number=number)


def _queries(addr: Address) -> list[tuple[str, bool]]:
    """Zapytania tekstowe w kolejności prób: (tekst, czy wynik przybliżony)."""
    city = addr.city.title()
    queries: list[tuple[str, bool]] = []
    if addr.street and addr.number:
        queries.append((f"{city}, {addr.street} {addr.number}", False))
    if addr.number and not addr.street:
        queries.append((f"{city} {addr.number}", False))
    if addr.street:
        queries.append((f"{city}, {addr.street}", True))
    return queries


class Geocoder:
    """Klient UUG z cache w pliku JSON.

    Klucz cache to kod gminy + miejscowość + adres; wartość to [lat, lon, przybliżony]
    albo {"brak": data}, gdy adresu nie znaleziono.
    """

    def __init__(
        self,
        cache_path: Path | None = None,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        http_client: httpx.Client | None = None,
        today: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cache_path = cache_path
        self._cache: dict[str, Any] = self._load_cache()
        self._http = http_client or httpx.Client(
            timeout=timeout, headers={"User-Agent": USER_AGENT}
        )
        self._owns_http = http_client is None
        self._max_retries = max_retries
        self._sleep = sleep
        self._today = today
        self.requests = 0  # liczba zapytań do UUG (statystyka dla logów)
        self._lock = threading.Lock()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # --- cache ---

    def _load_cache(self) -> dict[str, Any]:
        if not self._cache_path:
            return {}
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def key(teryt: str | None, city: str, address: str) -> str:
        return f"{(teryt or '')[:6]}|{fold(city)}|{fold(address)}"

    def cached(self, teryt: str | None, city: str, address: str) -> tuple[bool, GeoPoint | None]:
        """Wynik z cache jako (czy wiadomo, punkt).

        (True, punkt) - znany adres; (True, None) - niedawno sprawdzony i nieznaleziony;
        (False, None) - nie sprawdzano albo minęło RETRY_NOT_FOUND_AFTER.
        """
        value = self._cache.get(self.key(teryt, city, address))
        if isinstance(value, list) and len(value) == 3:
            return True, GeoPoint(float(value[0]), float(value[1]), bool(value[2]))
        if isinstance(value, dict) and "brak" in value:
            try:
                checked = date.fromisoformat(str(value["brak"]))
            except ValueError:
                return False, None
            if self._today().date() - checked < RETRY_NOT_FOUND_AFTER:
                return True, None
        return False, None

    def _remember(self, key: str, point: GeoPoint | None) -> None:
        if point:
            self._cache[key] = [round(point.lat, 6), round(point.lon, 6), point.approximate]
        else:
            self._cache[key] = {"brak": self._today().date().isoformat()}

    # --- geokodowanie ---

    def locate(self, teryt: str | None, city: str, address: str) -> GeoPoint | None:
        """Współrzędne adresu (z cache albo z UUG); None, gdy nie udało się ustalić."""
        known, point = self.cached(teryt, city, address)
        if known:
            return point
        point = self._lookup(teryt, split_address(city, address))
        self._remember(self.key(teryt, city, address), point)
        return point

    def locate_many(
        self,
        items: Iterable[tuple[str | None, str, str]],
        *,
        deadline: float | None = None,
        workers: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> GeocodeBatch:
        """Geokoduje wiele adresów (teryt, miasto, adres) równolegle.

        Adresy z cache nie generują zapytań. Po upływie `deadline` (wg `clock`) nowe adresy
        są pomijane i nie trafiają do cache - zostaną sprawdzone przy następnym uruchomieniu.
        """
        batch = GeocodeBatch()
        pending: dict[str, tuple[str | None, str, str]] = {}
        for teryt, city, address in items:
            key = self.key(teryt, city, address)
            if key in batch.points or key in pending:
                continue
            known, point = self.cached(teryt, city, address)
            if known:
                batch.points[key] = point
                batch.from_cache += 1
            else:
                pending[key] = (teryt, city, address)

        def work(item: tuple[str | None, str, str]) -> tuple[bool, GeoPoint | None]:
            if deadline is not None and clock() > deadline:
                return False, None  # zabrakło czasu
            teryt, city, address = item
            return True, self._lookup(teryt, split_address(city, address))

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = pool.map(work, pending.values())
            for key, (done, point) in zip(pending, results, strict=True):
                if not done:
                    batch.skipped += 1
                    continue
                batch.points[key] = point
                self._remember(key, point)
                if point:
                    batch.found += 1
                else:
                    batch.not_found += 1
        return batch

    def _lookup(self, teryt: str | None, addr: Address) -> GeoPoint | None:
        gmina = (teryt or "")[:6] if teryt and len(teryt) >= 6 else None
        wanted_city = fold(addr.city)

        def accept(result: dict[str, Any] | None) -> bool:
            # wynik musi dotyczyć tej samej miejscowości - inaczej UUG potrafi dopasować
            # ten sam adres w sąsiednim mieście (zaobserwowane: 35 km od właściwego miejsca)
            if not result:
                return False
            return fold(str(result.get("city") or "")) == wanted_city

        for text, approximate in _queries(addr):
            if gmina:
                result = self._request(text, gmina)
                if result and accept(result):
                    return self._point(result, approximate)
            # dzielnice dużych miast: inny kod gminy w NFZ i w GUGiK, ale ten sam powiat
            result = self._request(text, None)
            same_county = not gmina or str((result or {}).get("teryt", ""))[:4] == gmina[:4]
            if result and accept(result) and same_county:
                return self._point(result, approximate)
        return None

    @staticmethod
    def _point(result: dict[str, Any], approximate: bool) -> GeoPoint | None:
        try:
            return GeoPoint(lat=float(result["y"]), lon=float(result["x"]), approximate=approximate)
        except (KeyError, TypeError, ValueError):
            return None

    def _request(self, address: str, teryt: str | None) -> dict[str, Any] | None:
        params = {"request": "GetAddress", "address": address, "srid": "4326"}
        if teryt:
            params["teryt"] = teryt
        for attempt in range(self._max_retries + 1):
            with self._lock:
                self.requests += 1
            try:
                response = self._http.get(UUG_URL, params=params)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                if attempt == self._max_retries:
                    return None
                self._sleep(2.0 * 2**attempt)
                continue
            results = payload.get("results") if isinstance(payload, dict) else None
            if isinstance(results, dict) and results:
                # wyniki są ponumerowane od 1 w kolejności dopasowania
                first = results[min(results, key=lambda k: int(k) if str(k).isdigit() else 0)]
                return first if isinstance(first, dict) else None
            if isinstance(results, list) and results and isinstance(results[0], dict):
                return results[0]
            return None
        return None
