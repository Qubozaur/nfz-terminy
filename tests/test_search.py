from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from nfz_terminy.cache import LocalityCache
from nfz_terminy.client import DEFAULT_BASE_URL, NfzClient
from nfz_terminy.search import find_queues, resolve_benefit, resolve_locality

ORTOPEDIA = "ŚWIADCZENIA Z ZAKRESU ORTOPEDII I TRAUMATOLOGII NARZĄDU RUCHU"
EMPTY = {"meta": {"count": 0}, "links": {"next": None}, "data": []}


def names_page(names: list[str]) -> dict[str, Any]:
    return {"meta": {"count": len(names)}, "links": {"next": None}, "data": names}


@pytest.fixture
def client() -> Iterator[NfzClient]:
    with NfzClient(min_interval=0, sleep=lambda _: None) as c:
        yield c


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=DEFAULT_BASE_URL, assert_all_called=False) as router:
        yield router


def by_name(mapping: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Odpowiedź zależna od parametru name (brak w mapie = 0 wyników)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mapping.get(request.url.params["name"], EMPTY))

    return handler


class TestResolveBenefit:
    def test_exact_name_is_unambiguous(
        self, client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
    ) -> None:
        api.get("/benefits").mock(
            side_effect=by_name({ORTOPEDIA.lower(): fixture_json("benefits_orto.json")})
        )
        assert resolve_benefit(client, ORTOPEDIA.lower()) == [ORTOPEDIA]

    def test_outpatient_first_children_last(
        self, client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
    ) -> None:
        api.get("/benefits").mock(
            side_effect=by_name({"ortopeda": fixture_json("benefits_ortopeda.json")})
        )
        assert resolve_benefit(client, "ortopeda") == [
            ORTOPEDIA,
            "ODDZIAŁ CHIRURGII URAZOWO-ORTOPEDYCZNEJ",
            "ODDZIAŁ CHIRURGII URAZOWO-ORTOPEDYCZNEJ DLA DZIECI",
        ]

    def test_relevant_names_before_fuzzy_noise(
        self, client: NfzClient, api: respx.MockRouter
    ) -> None:
        # API 1.4 dorzuca luźno powiązane wyniki (zaobserwowane dla "endokrynolog")
        api.get("/benefits").mock(
            side_effect=by_name(
                {
                    "endokrynolog": names_page(
                        [
                            "ODDZIAŁ CHIRURGICZNY OGÓLNY",
                            "PORADNIA CHIRURGII OGÓLNEJ",
                            "ODDZIAŁ ENDOKRYNOLOGICZNY",
                            "ŚWIADCZENIA Z ZAKRESU ENDOKRYNOLOGII",
                        ]
                    )
                }
            )
        )
        result = resolve_benefit(client, "endokrynolog")
        assert result[:2] == ["ŚWIADCZENIA Z ZAKRESU ENDOKRYNOLOGII", "ODDZIAŁ ENDOKRYNOLOGICZNY"]

    def test_alias_for_word_without_polish_chars(
        self, client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
    ) -> None:
        route = api.get("/benefits").mock(
            side_effect=by_name(
                {
                    "pluc": fixture_json("benefits_pluc.json"),  # prawdziwe 0 wyników
                    "PŁUC": names_page(["ŚWIADCZENIA Z ZAKRESU GRUŹLICY I CHORÓB PŁUC"]),
                }
            )
        )
        assert resolve_benefit(client, "pluc") == ["ŚWIADCZENIA Z ZAKRESU GRUŹLICY I CHORÓB PŁUC"]
        assert [c.request.url.params["name"] for c in route.calls] == ["pluc", "PŁUC"]

    def test_short_alias_skips_direct_query(self, client: NfzClient, api: respx.MockRouter) -> None:
        route = api.get("/benefits").mock(
            side_effect=by_name({"TOMOGRAFIA KOMPUTEROWA": names_page(["TOMOGRAFIA KOMPUTEROWA"])})
        )
        assert resolve_benefit(client, "TK") == ["TOMOGRAFIA KOMPUTEROWA"]
        assert route.call_count == 1

    def test_suffix_shortening_with_other_words_as_filter(
        self, client: NfzClient, api: respx.MockRouter
    ) -> None:
        api.get("/benefits").mock(
            side_effect=by_name(
                {
                    "naczyniow": names_page(
                        ["PORADNIA CHIRURGII NACZYNIOWEJ", "ODDZIAŁ NACZYNIOWY"]
                    ),
                }
            )
        )
        # "chirurg naczyniowy" -> 0, potem najdłuższe słowo skracane: "naczyniow" -> filtr "chirurg"
        assert resolve_benefit(client, "chirurg naczyniowy") == ["PORADNIA CHIRURGII NACZYNIOWEJ"]

    def test_nothing_found(self, client: NfzClient, api: respx.MockRouter) -> None:
        api.get("/benefits").mock(side_effect=by_name({}))
        assert resolve_benefit(client, "xyzxyz") == []

    def test_too_short(self, client: NfzClient) -> None:
        with pytest.raises(ValueError, match="3 znaki"):
            resolve_benefit(client, "ab")


class TestResolveLocality:
    def test_direct_match(
        self,
        client: NfzClient,
        api: respx.MockRouter,
        fixture_json: Callable[[str], Any],
        tmp_path: Path,
    ) -> None:
        api.get("/localities").mock(
            return_value=httpx.Response(200, json=fixture_json("localities_katow_12.json"))
        )
        assert resolve_locality(client, "katowice", "12", LocalityCache(tmp_path)) == ["KATOWICE"]

    def test_single_prefix_match_wins(self, client: NfzClient, api: respx.MockRouter) -> None:
        # bez województwa API zwraca też "ŚWIEKATOWO" (podciąg)
        api.get("/localities").mock(
            return_value=httpx.Response(200, json=names_page(["KATOWICE", "ŚWIEKATOWO"]))
        )
        assert resolve_locality(client, "katow", None) == ["KATOWICE"]

    def test_ascii_input_falls_back_to_cached_dictionary(
        self,
        client: NfzClient,
        api: respx.MockRouter,
        fixture_json: Callable[[str], Any],
        tmp_path: Path,
    ) -> None:
        api.get("/localities").mock(
            return_value=httpx.Response(200, json=fixture_json("localities_lodz_05.json"))
        )
        pages = {
            "1": fixture_json("dictionaries_localities_05_p1.json"),
            "2": fixture_json("dictionaries_localities_05_p2.json"),
        }
        dictionary = api.get("/dictionaries/localities").mock(
            side_effect=lambda r: httpx.Response(200, json=pages[r.url.params["page"]])
        )
        cache = LocalityCache(tmp_path)

        assert resolve_locality(client, "lodz", "05", cache) == ["ŁÓDŹ"]
        assert dictionary.call_count == 2
        assert dictionary.calls[0].request.url.params["province"] == "05"

        # drugi raz słownik pochodzi z cache
        assert resolve_locality(client, "Lodz", "05", cache) == ["ŁÓDŹ"]
        assert dictionary.call_count == 2

    def test_ascii_fallback_lists_candidates(
        self, client: NfzClient, api: respx.MockRouter, tmp_path: Path
    ) -> None:
        api.get("/localities").mock(return_value=httpx.Response(200, json=EMPTY))
        cache = LocalityCache(tmp_path)
        cache.put("05", ["ALEKSANDRÓW ŁÓDZKI", "KONSTANTYNÓW ŁÓDZKI", "ZGIERZ"])
        assert resolve_locality(client, "lodzki", "05", cache) == [
            "ALEKSANDRÓW ŁÓDZKI",
            "KONSTANTYNÓW ŁÓDZKI",
        ]

    def test_no_province_no_fallback(self, client: NfzClient, api: respx.MockRouter) -> None:
        api.get("/localities").mock(return_value=httpx.Response(200, json=EMPTY))
        dictionary = api.get("/dictionaries/localities")
        assert resolve_locality(client, "lodz", None) == []
        assert dictionary.call_count == 0


class TestFindQueues:
    def test_sorted_by_forecast_and_exact_benefit_only(
        self, client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
    ) -> None:
        pages = {
            "1": fixture_json("queues_dermatologia_lodz_p1.json"),
            "2": fixture_json("queues_dermatologia_lodz_p2.json"),
        }
        api.get("/queues").mock(
            side_effect=lambda r: httpx.Response(200, json=pages[r.url.params["page"]])
        )

        result = find_queues(
            client, benefit="PORADNIA DERMATOLOGICZNA", province="05", locality="ŁÓDŹ"
        )

        raw = [q for p in pages.values() for q in p["data"]]
        adults = [q for q in raw if q["attributes"]["benefit"] == "PORADNIA DERMATOLOGICZNA"]
        assert len(adults) < len(raw), "fixture zawiera też poradnie dla dzieci"
        assert len(result.entries) + result.without_forecast == len(adults)
        assert {e.benefit for e in result.entries} == {"PORADNIA DERMATOLOGICZNA"}
        days = [e.wait_days for e in result.entries]
        assert days == sorted(days, key=lambda d: (d is None, d or 0))
        assert all(e.locality and e.locality.startswith("ŁÓDŹ") for e in result.entries)

    def test_entries_without_forecast_are_counted_not_returned(
        self, client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
    ) -> None:
        pages = {
            "1": fixture_json("queues_endoprotezoplastyka_06_p1.json"),
            "2": fixture_json("queues_endoprotezoplastyka_06_p2.json"),
        }
        api.get("/queues").mock(
            side_effect=lambda r: httpx.Response(200, json=pages[r.url.params["page"]])
        )

        result = find_queues(client, benefit="ENDOPROTEZOPLASTYKA STAWU BIODROWEGO", province="06")

        assert result.without_forecast > 0
        assert len(result.entries) + result.without_forecast == 28
        assert all(e.pcus for e in result.entries)

    def test_district_matching(
        self, client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
    ) -> None:
        payload = fixture_json("queues_ortopedia_katowice.json")
        payload["data"][0]["attributes"]["locality"] = "KATOWICE-SZOPIENICE"
        payload["data"][1]["attributes"]["locality"] = "KATOWICEWO"
        api.get("/queues").mock(return_value=httpx.Response(200, json=payload))

        result = find_queues(client, benefit=ORTOPEDIA, province="12", locality="KATOWICE")

        localities = {e.locality for e in result.entries}
        assert "KATOWICE-SZOPIENICE" in localities
        assert "KATOWICEWO" not in localities
        assert len(result.entries) == 22
