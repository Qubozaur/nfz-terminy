"""Modele odpowiedzi API NFZ "Terminy Leczenia" w wersji 1.4.

Struktura ustalona na podstawie prawdziwych odpowiedzi (patrz tests/fixtures/) oraz
swagger.json API. Nazwy pól w JSON-ie zawierają myślniki, więc mapujemy je aliasami.
Nieznane pola są ignorowane, żeby drobne zmiany po stronie NFZ nie psuły narzędzia.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nfz_terminy.pcus import parse_pcus

T = TypeVar("T")


class ApiModel(BaseModel):
    """Baza modeli API: aliasy z myślnikami, ignorowanie nieznanych pól."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)


class Meta(ApiModel):
    count: int = 0
    page: int = 1
    limit: int = 10
    date_modified: str | None = Field(default=None, alias="date-modified")


class Links(ApiModel):
    first: str | None = None
    prev: str | None = None
    self_: str | None = Field(default=None, alias="self")
    next: str | None = None
    last: str | None = None


class Page(ApiModel, Generic[T]):
    """Jedna strona wyników (/queues, /benefits, /localities, /dictionaries/...)."""

    meta: Meta = Field(default_factory=Meta)
    links: Links = Field(default_factory=Links)
    data: list[T] = Field(default_factory=list)

    @field_validator("data", mode="before")
    @classmethod
    def _null_data_as_empty(cls, value: Any) -> Any:
        return [] if value is None else value

    @property
    def has_next(self) -> bool:
        return bool(self.links.next) and bool(self.data)


class EstimatedTime(ApiModel):
    """Pole `dates`: PCUS - prognozowany czas udzielenia świadczenia."""

    applicable: bool | None = None
    pcus: str | None = None
    situation_as_at: date | None = Field(default=None, alias="date-situation-as-at")


class ProviderData(ApiModel):
    awaiting: int | None = None
    removed: int | None = None
    average_period: int | None = Field(default=None, alias="average-period")
    update: str | None = None


class ComputedData(ApiModel):
    average_period: int | None = Field(default=None, alias="average-period")
    update: str | None = None


class Statistics(ApiModel):
    provider_data: ProviderData | None = Field(default=None, alias="provider-data")
    computed_data: ComputedData | None = Field(default=None, alias="computed-data")


class QueueAttributes(ApiModel):
    case: int
    benefit: str | None = None
    many_places: str | None = Field(default=None, alias="many-places")
    provider: str | None = None
    provider_code: str | None = Field(default=None, alias="provider-code")
    place: str | None = None
    address: str | None = None
    locality: str | None = None
    county: str | None = None
    phone: str | None = None
    registration_phone: str | None = Field(default=None, alias="registration-phone")
    website: str | None = None
    benefits_for_children: str | None = Field(default=None, alias="benefits-for-children")
    age_range: str | None = Field(default=None, alias="age-range")
    toilet: str | None = None
    ramp: str | None = None
    car_park: str | None = Field(default=None, alias="car-park")
    elevator: str | None = None
    wheelchairs: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    statistics: Statistics | None = None
    dates: EstimatedTime | None = None


class Queue(ApiModel):
    id: str
    type: str | None = None
    attributes: QueueAttributes


class LocalityEntry(ApiModel):
    """Pozycja słownika /dictionaries/localities."""

    name: str
    province: str | None = None
    county: str | None = None


class ApiErrorItem(ApiModel):
    error_result: str | None = Field(default=None, alias="error-result")
    error_reason: str | None = Field(default=None, alias="error-reason")
    error_solution: str | None = Field(default=None, alias="error-solution")
    error_code: int | None = Field(default=None, alias="error-code")


class ApiErrorResponse(ApiModel):
    errors: list[ApiErrorItem] = Field(default_factory=list)


def _flag(value: str | None) -> bool | None:
    """Znaczniki API "Y"/"N" -> True/False, brak informacji -> None."""
    if value == "Y":
        return True
    if value == "N":
        return False
    return None


def _clean(value: str | None) -> str | None:
    """Usuwa nadmiarowe spacje (w danych NFZ zdarza się np. "PCK  1"); pusty napis -> None."""
    if not value:
        return None
    return " ".join(value.split()) or None


class QueueEntry(BaseModel):
    """Spłaszczony wynik wyszukiwania, wygodny dla CLI, bota i strony WWW."""

    model_config = ConfigDict(frozen=True)

    id: str
    benefit: str
    case: int
    pcus: str
    wait_days: int | None
    situation_as_at: date | None
    provider: str | None
    place: str | None
    address: str | None
    locality: str | None
    county: str | None
    phone: str | None
    registration_phone: str | None
    website: str | None
    awaiting: int | None
    average_period: int | None
    for_children: bool | None
    ramp: bool | None
    elevator: bool | None
    toilet: bool | None
    car_park: bool | None
    wheelchairs: bool | None
    latitude: float | None
    longitude: float | None

    @classmethod
    def from_queue(cls, queue: Queue) -> QueueEntry | None:
        """Tworzy wpis z kolejki API; zwraca None, gdy placówka nie podała prognozy."""
        a = queue.attributes
        if a.dates is None or not a.dates.pcus:
            return None
        provider_stats = a.statistics.provider_data if a.statistics else None
        return cls(
            id=queue.id,
            benefit=a.benefit or "",
            case=a.case,
            pcus=a.dates.pcus,
            wait_days=parse_pcus(a.dates.pcus),
            situation_as_at=a.dates.situation_as_at,
            provider=_clean(a.provider),
            place=_clean(a.place),
            address=_clean(a.address),
            locality=_clean(a.locality),
            county=_clean(a.county),
            phone=_clean(a.phone),
            registration_phone=_clean(a.registration_phone),
            website=_clean(a.website),
            awaiting=provider_stats.awaiting if provider_stats else None,
            average_period=provider_stats.average_period if provider_stats else None,
            for_children=_flag(a.benefits_for_children),
            ramp=_flag(a.ramp),
            elevator=_flag(a.elevator),
            toilet=_flag(a.toilet),
            car_park=_flag(a.car_park),
            wheelchairs=_flag(a.wheelchairs),
            latitude=a.latitude,
            longitude=a.longitude,
        )
