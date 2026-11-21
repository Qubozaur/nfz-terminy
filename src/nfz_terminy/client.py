"""Klient API NFZ "Terminy Leczenia" (wersja 1.4).

Klient jest niezależny od CLI, żeby można go było użyć w bocie i na stronie WWW.

Uwagi z obserwacji prawdziwego API:
- API 1.3 (api.nfz.gov.pl/app-itl-api) jest wycofane i zwraca puste daty; używamy 1.4.
- Przed API stoi WAF, który domyślnym User-Agentom bibliotek (python-httpx, Python-urllib)
  zwraca stronę HTML z wyzwaniem zamiast JSON-a - dlatego wysyłamy własny User-Agent.
- Przy zbyt szybkich zapytaniach API zwraca 429 - stąd odstęp między zapytaniami i retry.
- Linki `links.next` w słownikach gubią parametry filtrów (np. province), więc paginujemy
  po numerze strony z własnymi parametrami, a z linków bierzemy tylko informację,
  czy następna strona istnieje.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx
from pydantic import ValidationError

from nfz_terminy import __version__
from nfz_terminy.models import ApiErrorResponse, LocalityEntry, Page, Queue

DEFAULT_BASE_URL = "https://apinfz.nfz.gov.pl/app-itl-api-pcus"
API_VERSION = "1.4"
USER_AGENT = f"nfz-terminy/{__version__}"
MAX_PAGE_SIZE = 25
MIN_NAME_LENGTH = 3

T = TypeVar("T")

# (numer pobranej strony, liczba wszystkich stron) - do paska postępu w CLI
ProgressCallback = Callable[[int, int], None]


class NfzApiError(Exception):
    """Błąd komunikacji z API NFZ (z komunikatem zrozumiałym dla użytkownika)."""


class NfzClient:
    """Synchroniczny klient API z odstępem między zapytaniami i ponawianiem prób.

    Parametry `sleep` i `clock` można podmienić w testach, żeby nie czekać naprawdę.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        min_interval: float = 0.4,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        max_backoff: float = 30.0,
        max_pages: int = 400,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._http = http_client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._owns_http = http_client is None
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._max_pages = max_pages
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    # --- zarządzanie zasobami ---

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- publiczne metody ---

    def search_benefits(self, name: str) -> list[str]:
        """Nazwy świadczeń zawierające podany tekst (GET /benefits)."""
        return list(self._paginate("/benefits", {"name": _require_name(name)}, str))

    def search_localities(self, name: str, province: str | None = None) -> list[str]:
        """Nazwy miejscowości zawierające podany tekst (GET /localities)."""
        params: dict[str, Any] = {"name": _require_name(name)}
        if province:
            params["province"] = province
        return list(self._paginate("/localities", params, str))

    def list_localities(self, province: str) -> list[LocalityEntry]:
        """Pełny słownik miejscowości województwa (GET /dictionaries/localities)."""
        return list(
            self._paginate("/dictionaries/localities", {"province": province}, LocalityEntry)
        )

    def queues(
        self,
        *,
        benefit: str,
        province: str | None = None,
        locality: str | None = None,
        urgent: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> list[Queue]:
        """Wszystkie kolejki do świadczenia (GET /queues, wszystkie strony).

        Uwaga: API dopasowuje `benefit` i `locality` jako podciąg, więc wyniki mogą
        zawierać np. "PORADNIA X DLA DZIECI" przy zapytaniu o "PORADNIA X".
        Filtrowanie dokładnych nazw robi warstwa wyszukiwania (search.py).
        """
        params: dict[str, Any] = {"benefit": benefit, "case": 2 if urgent else 1}
        if province:
            params["province"] = province
        if locality:
            params["locality"] = locality
        return list(self._paginate("/queues", params, Queue, on_progress))

    # --- wnętrze ---

    def _paginate(
        self,
        path: str,
        params: dict[str, Any],
        item_type: type[T],
        on_progress: ProgressCallback | None = None,
    ) -> Iterator[T]:
        page_number = 1
        while True:
            payload = self._get(path, {**params, "page": page_number, "limit": MAX_PAGE_SIZE})
            try:
                page = Page[item_type].model_validate(payload)  # type: ignore[valid-type]
            except ValidationError as exc:
                raise NfzApiError(
                    f"Nieoczekiwany format odpowiedzi API dla {path}. "
                    "Możliwe, że NFZ zmienił API - zgłoś problem w repozytorium."
                ) from exc
            if on_progress:
                total_pages = max(1, -(-page.meta.count // MAX_PAGE_SIZE))
                on_progress(page_number, total_pages)
            yield from page.data
            if not page.has_next:
                return
            page_number += 1
            if page_number > self._max_pages:
                raise NfzApiError(
                    f"Przekroczono limit {self._max_pages} stron wyników. Zawęź wyszukiwanie "
                    "(podaj województwo lub miejscowość)."
                )

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        query = {**params, "format": "json", "api-version": API_VERSION}
        last_problem = "nieznany błąd"
        for attempt in range(self._max_retries + 1):
            is_last = attempt == self._max_retries
            self._throttle()
            try:
                response = self._http.get(path, params=query)
            except httpx.TransportError as exc:
                last_problem = f"błąd połączenia ({exc.__class__.__name__})"
                if is_last:
                    break
                self._sleep(self._backoff(attempt))
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                last_problem = (
                    "zbyt wiele zapytań (HTTP 429)" if status == 429 else f"błąd serwera ({status})"
                )
                if is_last:
                    break
                self._sleep(self._retry_delay(response, attempt))
                continue

            if status >= 400:
                raise NfzApiError(_describe_error(response))

            if "json" not in response.headers.get("content-type", ""):
                # WAF potrafi zwrócić stronę HTML z kodem 200 - ponawiamy jak błąd przejściowy
                last_problem = "API zwróciło stronę HTML zamiast danych (zabezpieczenie WAF)"
                if is_last:
                    break
                self._sleep(self._backoff(attempt))
                continue

            try:
                return response.json()
            except ValueError as exc:
                raise NfzApiError("API zwróciło niepoprawny JSON.") from exc

        raise NfzApiError(
            f"Nie udało się pobrać danych z API NFZ po {self._max_retries + 1} próbach: "
            f"{last_problem}. Spróbuj ponownie za chwilę."
        )

    def _throttle(self) -> None:
        """Pilnuje minimalnego odstępu między zapytaniami (API odpowiada 429 przy burstach)."""
        now = self._clock()
        if self._last_request is not None:
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_request = now

    def _backoff(self, attempt: int) -> float:
        return float(min(self._max_backoff, self._backoff_base * 2**attempt))

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        """Przerwa przed ponowieniem: Retry-After (w sekundach), jeśli jest, inaczej backoff."""
        retry_after = response.headers.get("retry-after", "")
        if retry_after.strip().isdigit():
            return float(min(self._max_backoff, int(retry_after)))
        return self._backoff(attempt)


def _require_name(name: str) -> str:
    value = name.strip()
    if len(value) < MIN_NAME_LENGTH:
        raise ValueError(f"Podaj co najmniej {MIN_NAME_LENGTH} znaki (API NFZ tego wymaga).")
    return value


def _describe_error(response: httpx.Response) -> str:
    """Wyciąga czytelny opis z odpowiedzi błędu API (pole errors[].error-reason)."""
    try:
        errors = ApiErrorResponse.model_validate(response.json()).errors
    except (ValueError, ValidationError):
        errors = []
    if errors:
        first = errors[0]
        details = " ".join(p for p in (first.error_reason, first.error_solution) if p)
        return f"API NFZ odrzuciło zapytanie (HTTP {response.status_code}): {details}"
    return f"API NFZ odrzuciło zapytanie (HTTP {response.status_code})."
