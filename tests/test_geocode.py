from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from nfz_terminy.geocode import UUG_URL, Address, Geocoder, GeoPoint, split_address
from nfz_terminy.text import city_of

TODAY = datetime(2026, 11, 21, tzinfo=UTC)


def uug_result(city: str, x: float, y: float, teryt: str) -> dict[str, Any]:
    """Odpowiedź UUG w formacie zaobserwowanym w prawdziwej usłudze (wyniki numerowane od 1)."""
    return {
        "type": "address",
        "found objects": 1,
        "returned objects": 1,
        "results": {"1": {"city": city, "x": str(x), "y": str(y), "teryt": teryt, "accuracy": "1"}},
    }


NOT_FOUND = {"type": "address", "found objects": 0, "returned objects": 0}


@pytest.fixture
def uug() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def make_geocoder(cache: Path | None = None, today: datetime = TODAY) -> Geocoder:
    return Geocoder(cache, sleep=lambda _: None, today=lambda: today)


def answering(
    table: dict[tuple[str, str | None], dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Odpowiedzi UUG zależne od (address, teryt); brak w tabeli = nie znaleziono."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        return httpx.Response(
            200, json=table.get((params["address"], params.get("teryt")), NOT_FOUND)
        )

    return handler


class TestSplitAddress:
    @pytest.mark.parametrize(
        ("city", "address", "expected"),
        [
            ("KATOWICE", "UL. MORAWA 31", Address("KATOWICE", "MORAWA", "31")),
            ("GRODKÓW", "UL.SZPITALNA 13", Address("GRODKÓW", "SZPITALNA", "13")),
            ("KATOWICE", "PCK 1", Address("KATOWICE", "PCK", "1")),
            # liczby w nazwie ulicy - numer budynku to ostatnia liczba
            (
                "KATOWICE",
                "HARCERZY WRZEŚNIA 1939 5",
                Address("KATOWICE", "HARCERZY WRZEŚNIA 1939", "5"),
            ),
            ("RADOM", "UL. 25 CZERWCA 70", Address("RADOM", "25 CZERWCA", "70")),
            # "AL." zostaje - odróżnia aleję od placu o tej samej nazwie
            (
                "WARSZAWA",
                "AL. WOJSKA POLSKIEGO 25",
                Address("WARSZAWA", "AL. WOJSKA POLSKIEGO", "25"),
            ),
            # numer lokalu, zakres, litera oddzielona spacją, podwójny ukośnik
            ("WARSZAWA", "POTOCKA 14/5", Address("WARSZAWA", "POTOCKA", "14")),
            ("KROTOSZYN", "BOLEWSKIEGO 4-8", Address("KROTOSZYN", "BOLEWSKIEGO", "4")),
            ("WARSZAWA", "MARCINA KASPRZAKA 17 A", Address("WARSZAWA", "MARCINA KASPRZAKA", "17A")),
            ("ŚWIECIE", "SĄDOWA 18//19", Address("ŚWIECIE", "SĄDOWA", "18")),
            # inicjały i tytuły
            ("GOSTYŃ", "PL.K. MARCINKOWSKIEGO 8/9", Address("GOSTYŃ", "PL. MARCINKOWSKIEGO", "8")),
            ("SKAWINA", "KS. J. POPIEŁUSZKI 5", Address("SKAWINA", "POPIEŁUSZKI", "5")),
            # wieś bez ulic
            ("STRACHOCINA", "STRACHOCINA 341", Address("STRACHOCINA", None, "341")),
        ],
    )
    def test_real_nfz_addresses(self, city: str, address: str, expected: Address) -> None:
        assert split_address(city, address) == expected


def test_city_of_maps_districts() -> None:
    assert city_of("WARSZAWA MOKOTÓW", "WARSZAWA") == "WARSZAWA"
    assert city_of("KRAKÓW-KROWODRZA", "KRAKÓW") == "KRAKÓW"
    assert city_of("BIAŁA PODLASKA", "BIAŁA PODLASKA") == "BIAŁA PODLASKA"
    assert city_of("KATOWICE GÓRNE", "BĘDZIŃSKI") == "KATOWICE GÓRNE"


class TestLocate:
    def test_address_limited_to_gmina(self, uug: respx.MockRouter) -> None:
        route = uug.get(UUG_URL).mock(
            side_effect=answering(
                {("Katowice, PCK 1", "246901"): uug_result("Katowice", 19.0151, 50.2525, "246901")}
            )
        )
        with make_geocoder() as geo:
            point = geo.locate("2469011", "KATOWICE", "PCK 1")
        assert point == GeoPoint(lat=50.2525, lon=19.0151, approximate=False)
        params = route.calls.last.request.url.params
        assert params["srid"] == "4326"
        assert params["teryt"] == "246901"

    def test_city_district_falls_back_to_same_county(self, uug: respx.MockRouter) -> None:
        # NFZ: kod dzielnicy 146507, GUGiK: kod miasta 146501 - ten sam powiat 1465
        uug.get(UUG_URL).mock(
            side_effect=answering(
                {
                    ("Warszawa, POTOCKA 14", None): uug_result("Warszawa", 20.97, 52.27, "146501"),
                }
            )
        )
        with make_geocoder() as geo:
            point = geo.locate("1465078", "WARSZAWA", "POTOCKA 14/5")
        assert point is not None
        assert not point.approximate

    def test_rejects_result_from_other_town(self, uug: respx.MockRouter) -> None:
        # zaobserwowane: ten sam adres w sąsiednim mieście tego samego powiatu
        uug.get(UUG_URL).mock(
            side_effect=answering(
                {
                    ("Pieszyce, KRÓLOWEJ JADWIGI 1", None): uug_result(
                        "Dzierżoniów", 16.6, 50.7, "020201"
                    ),
                }
            )
        )
        with make_geocoder() as geo:
            assert geo.locate("0202031", "PIESZYCE", "KRÓLOWEJ JADWIGI 1") is None

    def test_rejects_other_county_without_gmina_match(self, uug: respx.MockRouter) -> None:
        uug.get(UUG_URL).mock(
            side_effect=answering(
                {("Jasienica 123", None): uug_result("Jasienica", 19.8, 49.8, "120903")}
            )
        )
        with make_geocoder() as geo:
            assert geo.locate("2402032", "JASIENICA", "JASIENICA 123") is None

    def test_village_without_streets(self, uug: respx.MockRouter) -> None:
        uug.get(UUG_URL).mock(
            side_effect=answering(
                {("Strachocina 341", "181705"): uug_result("Strachocina", 22.1, 49.6, "181705")}
            )
        )
        with make_geocoder() as geo:
            point = geo.locate("1817052", "STRACHOCINA", "STRACHOCINA 341")
        assert point is not None
        assert not point.approximate

    def test_street_center_is_marked_approximate(self, uug: respx.MockRouter) -> None:
        uug.get(UUG_URL).mock(
            side_effect=answering(
                {
                    ("Stargard, AL. ŻOŁNIERZA", "321401"): uug_result(
                        "Stargard", 15.0, 53.3, "321401"
                    )
                }
            )
        )
        with make_geocoder() as geo:
            point = geo.locate("3214011", "STARGARD", "AL. ŻOŁNIERZA 37")
        assert point is not None
        assert point.approximate

    def test_network_errors_are_retried(self, uug: respx.MockRouter) -> None:
        route = uug.get(UUG_URL).mock(
            side_effect=[
                httpx.ConnectTimeout("timeout"),
                httpx.Response(503),
                httpx.Response(200, json=uug_result("Katowice", 19.0, 50.2, "246901")),
            ]
        )
        with make_geocoder() as geo:
            assert geo.locate("2469011", "KATOWICE", "PCK 1") is not None
        assert route.call_count == 3


class TestCache:
    def test_found_and_missing_are_cached_on_disk(
        self, uug: respx.MockRouter, tmp_path: Path
    ) -> None:
        route = uug.get(UUG_URL).mock(
            side_effect=answering(
                {("Katowice, PCK 1", "246901"): uug_result("Katowice", 19.0151, 50.2525, "246901")}
            )
        )
        cache = tmp_path / "geo.json"
        with make_geocoder(cache) as geo:
            geo.locate("2469011", "KATOWICE", "PCK 1")
            geo.locate("2469011", "KATOWICE", "NIEISTNIEJĄCA 999")
            geo.save()
        calls = route.call_count

        with make_geocoder(cache) as geo:
            assert geo.locate("2469011", "KATOWICE", "PCK 1") is not None
            assert geo.locate("2469011", "KATOWICE", "NIEISTNIEJĄCA 999") is None
        assert route.call_count == calls  # wszystko z cache

    def test_missing_address_is_retried_after_30_days(
        self, uug: respx.MockRouter, tmp_path: Path
    ) -> None:
        route = uug.get(UUG_URL).mock(return_value=httpx.Response(200, json=NOT_FOUND))
        cache = tmp_path / "geo.json"
        with make_geocoder(cache) as geo:
            geo.locate("2469011", "KATOWICE", "NOWA 1")
            geo.save()
        calls = route.call_count

        with make_geocoder(cache, today=datetime(2026, 12, 10, tzinfo=UTC)) as geo:
            geo.locate("2469011", "KATOWICE", "NOWA 1")
        assert route.call_count == calls

        with make_geocoder(cache, today=datetime(2027, 1, 5, tzinfo=UTC)) as geo:
            geo.locate("2469011", "KATOWICE", "NOWA 1")
        assert route.call_count > calls

    def test_corrupted_cache_is_ignored(self, tmp_path: Path) -> None:
        cache = tmp_path / "geo.json"
        cache.write_text("{to nie jest json", encoding="utf-8")
        with make_geocoder(cache) as geo:
            assert geo.cached("2469011", "KATOWICE", "PCK 1") == (False, None)


class TestLocateMany:
    def test_deduplicates_and_counts(self, uug: respx.MockRouter) -> None:
        route = uug.get(UUG_URL).mock(
            side_effect=answering(
                {("Katowice, PCK 1", "246901"): uug_result("Katowice", 19.0, 50.2, "246901")}
            )
        )
        items = [("2469011", "KATOWICE", "PCK 1")] * 3 + [("2469011", "KATOWICE", "BRAK 1")]
        with make_geocoder() as geo:
            batch = geo.locate_many(items, workers=2)
        assert (batch.found, batch.not_found, batch.skipped) == (1, 1, 0)
        assert len(batch.points) == 2
        # PCK 1 trafione w pierwszym zapytaniu; BRAK 1: ulica+numer i sama ulica, każde 2 razy
        assert route.call_count == 1 + 4

    def test_deadline_skips_without_caching(self, uug: respx.MockRouter) -> None:
        route = uug.get(UUG_URL)
        with make_geocoder() as geo:
            batch = geo.locate_many(
                [("2469011", "KATOWICE", "PCK 1")], deadline=10.0, clock=lambda: 11.0
            )
            assert batch.skipped == 1
            assert geo.cached("2469011", "KATOWICE", "PCK 1") == (False, None)
        assert route.call_count == 0
