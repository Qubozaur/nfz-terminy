from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from nfz_terminy import cli
from nfz_terminy.cache import LocalityCache
from nfz_terminy.client import DEFAULT_BASE_URL, NfzClient

ORTOPEDIA = "ŚWIADCZENIA Z ZAKRESU ORTOPEDII I TRAUMATOLOGII NARZĄDU RUCHU"
EMPTY = {"meta": {"count": 0}, "links": {"next": None}, "data": []}

runner = CliRunner()


@pytest.fixture(autouse=True)
def fast_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Klient bez odstępów między zapytaniami i cache w katalogu tymczasowym."""
    monkeypatch.setattr(cli, "make_client", lambda: NfzClient(min_interval=0, sleep=lambda _: None))
    monkeypatch.setattr(cli, "make_cache", lambda: LocalityCache(tmp_path))
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)


@pytest.fixture
def api(fixture_json: Callable[[str], Any]) -> Iterator[respx.MockRouter]:
    """API z odpowiedziami z fixture'ów dla scenariusza: ortopedia w Katowicach / Łodzi."""
    benefits = {
        "ortopeda": fixture_json("benefits_ortopeda.json"),
        "orto": fixture_json("benefits_orto.json"),
        ORTOPEDIA: fixture_json("benefits_orto.json"),
        "poradnia dermatologiczna": {
            "meta": {"count": 2},
            "links": {"next": None},
            "data": ["PORADNIA DERMATOLOGICZNA", "PORADNIA DERMATOLOGICZNA DLA DZIECI"],
        },
    }
    localities = {
        ("katowice", "12"): fixture_json("localities_katow_12.json"),
        ("lodz", "05"): fixture_json("localities_lodz_05.json"),
    }
    dictionary = {
        "1": fixture_json("dictionaries_localities_05_p1.json"),
        "2": fixture_json("dictionaries_localities_05_p2.json"),
    }
    dermatologia = {
        "1": fixture_json("queues_dermatologia_lodz_p1.json"),
        "2": fixture_json("queues_dermatologia_lodz_p2.json"),
    }

    def queues(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params["benefit"] == ORTOPEDIA:
            return httpx.Response(200, json=fixture_json("queues_ortopedia_katowice.json"))
        if params["benefit"] == "PORADNIA DERMATOLOGICZNA":
            return httpx.Response(200, json=dermatologia[params["page"]])
        return httpx.Response(200, json=EMPTY)

    with respx.mock(base_url=DEFAULT_BASE_URL, assert_all_called=False) as router:
        router.get("/benefits").mock(
            side_effect=lambda r: httpx.Response(
                200, json=benefits.get(r.url.params["name"], EMPTY)
            )
        )
        router.get("/localities").mock(
            side_effect=lambda r: httpx.Response(
                200,
                json=localities.get((r.url.params["name"], r.url.params.get("province")), EMPTY),
            )
        )
        router.get("/dictionaries/localities").mock(
            side_effect=lambda r: httpx.Response(200, json=dictionary[r.url.params["page"]])
        )
        router.get("/queues").mock(side_effect=queues)
        yield router


def invoke(*args: str, input: str | None = None) -> Any:
    return runner.invoke(cli.app, list(args), input=input, env={"COLUMNS": "220"})


def test_table_output(api: respx.MockRouter) -> None:
    result = invoke(
        "szukaj",
        "ortopeda",
        "--woj",
        "slaskie",
        "--miasto",
        "katowice",
        "-n",
        "5",
        "--wybierz",
        "1",
    )

    assert result.exit_code == 0, result.output
    assert ORTOPEDIA in result.stdout
    assert "24 dni" in result.stdout
    assert "PCK 1" in result.stdout  # podwójna spacja z danych NFZ usunięta
    assert "Pokazano 5 z 23" in result.stdout
    assert "potwierdź termin telefonicznie" in result.stdout

    queue_params = api.routes[3].calls.last.request.url.params
    assert queue_params["province"] == "12"
    assert queue_params["locality"] == "KATOWICE"
    assert queue_params["case"] == "1"


def test_json_output_with_ascii_city(api: respx.MockRouter) -> None:
    result = invoke(
        "szukaj",
        "poradnia dermatologiczna",
        "--woj",
        "łódzkie",
        "--miasto",
        "lodz",
        "--json",
        "--limit",
        "3",
        "--wybierz",
        "1",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["query"] == {
        "benefit": "PORADNIA DERMATOLOGICZNA",
        "province": "05",
        "province_name": "łódzkie",
        "locality": "ŁÓDŹ",
        "urgent": False,
    }
    assert len(payload["results"]) == 3
    assert payload["total"] > 3
    days = [r["wait_days"] for r in payload["results"]]
    assert days == sorted(days)
    assert {r["benefit"] for r in payload["results"]} == {"PORADNIA DERMATOLOGICZNA"}


def test_urgent_flag(api: respx.MockRouter) -> None:
    result = invoke("szukaj", ORTOPEDIA, "--woj", "12", "--pilny", "--json")
    assert result.exit_code == 0, result.output
    assert api.routes[3].calls.last.request.url.params["case"] == "2"
    assert json.loads(result.stdout)["query"]["urgent"] is True


def test_ambiguous_benefit_non_interactive(api: respx.MockRouter) -> None:
    result = invoke("szukaj", "ortopeda", "--woj", "slaskie")
    assert result.exit_code == 2
    assert "--wybierz" in result.stderr
    assert ORTOPEDIA in result.stderr


def test_ambiguous_benefit_interactive_prompt(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    result = invoke("szukaj", "ortopeda", "--woj", "slaskie", "--json", input="1\n")
    # przy --json nie pytamy nawet w terminalu
    assert result.exit_code == 2

    result = invoke("szukaj", "ortopeda", "--woj", "slaskie", input="1\n")
    assert result.exit_code == 0, result.output
    assert "24 dni" in result.stdout


def test_unknown_benefit(api: respx.MockRouter) -> None:
    result = invoke("szukaj", "xyzxyz")
    assert result.exit_code == 1
    assert "Nie znaleziono świadczenia" in result.stderr


def test_unknown_city_without_province_hints_about_polish_chars(api: respx.MockRouter) -> None:
    result = invoke("szukaj", ORTOPEDIA, "--miasto", "lodz")
    assert result.exit_code == 1
    assert "polskie znaki" in result.stderr


def test_bad_province() -> None:
    result = invoke("szukaj", "ortopeda", "--woj", "pod")
    assert result.exit_code == 2
    assert "Niejednoznaczne" in result.output


def test_api_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    with respx.mock(base_url=DEFAULT_BASE_URL) as router:
        router.get("/benefits").mock(return_value=httpx.Response(429))
        monkeypatch.setattr(
            cli,
            "make_client",
            lambda: NfzClient(min_interval=0, max_retries=1, sleep=lambda _: None),
        )
        result = invoke("szukaj", "ortopeda")
    assert result.exit_code == 1
    assert "429" in result.stderr


def test_benefits_command(api: respx.MockRouter) -> None:
    result = invoke("swiadczenia", "orto")
    assert result.exit_code == 0
    assert ORTOPEDIA in result.stdout


def test_provinces_command() -> None:
    result = invoke("wojewodztwa")
    assert result.exit_code == 0
    assert "śląskie" in result.stdout
    assert "16" in result.stdout


def test_version() -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert "nfz-terminy" in result.stdout
