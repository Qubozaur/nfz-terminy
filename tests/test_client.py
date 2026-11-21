from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import respx

from nfz_terminy.client import DEFAULT_BASE_URL, USER_AGENT, NfzApiError, NfzClient

ORTOPEDIA = "ŚWIADCZENIA Z ZAKRESU ORTOPEDII I TRAUMATOLOGII NARZĄDU RUCHU"


class FakeTime:
    """Zegar i sleep bez prawdziwego czekania; zapisuje żądane przerwy."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def client(fake_time: FakeTime) -> Iterator[NfzClient]:
    with NfzClient(min_interval=0.4, sleep=fake_time.sleep, clock=fake_time.clock) as c:
        yield c


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=DEFAULT_BASE_URL, assert_all_called=False) as router:
        yield router


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_sends_user_agent_and_api_version(
    client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
) -> None:
    route = api.get("/benefits").mock(
        return_value=json_response(fixture_json("benefits_orto.json"))
    )

    names = client.search_benefits("orto")

    assert ORTOPEDIA in names
    request = route.calls.last.request
    assert request.headers["user-agent"] == USER_AGENT
    assert request.url.params["api-version"] == "1.4"
    assert request.url.params["format"] == "json"
    assert request.url.params["name"] == "orto"
    assert request.url.params["limit"] == "25"


def test_paginates_by_page_number_keeping_filters(
    client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
) -> None:
    pages = {
        "1": fixture_json("queues_dermatologia_lodz_p1.json"),
        "2": fixture_json("queues_dermatologia_lodz_p2.json"),
    }
    route = api.get("/queues").mock(
        side_effect=lambda request: json_response(pages[request.url.params["page"]])
    )
    progress: list[tuple[int, int]] = []

    queues = client.queues(
        benefit="PORADNIA DERMATOLOGICZNA",
        province="05",
        locality="ŁÓDŹ",
        on_progress=lambda page, total: progress.append((page, total)),
    )

    assert len(queues) == 36
    assert route.call_count == 2
    for call in route.calls:
        params = call.request.url.params
        assert params["province"] == "05"
        assert params["locality"] == "ŁÓDŹ"
        assert params["benefit"] == "PORADNIA DERMATOLOGICZNA"
        assert params["case"] == "1"
    assert progress == [(1, 2), (2, 2)]


def test_urgent_sets_case_2(
    client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
) -> None:
    route = api.get("/queues").mock(
        return_value=json_response(fixture_json("queues_ortopedia_katowice.json"))
    )
    client.queues(benefit=ORTOPEDIA, urgent=True)
    assert route.calls.last.request.url.params["case"] == "2"


def test_dictionary_pagination(
    client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
) -> None:
    pages = {
        "1": fixture_json("dictionaries_localities_05_p1.json"),
        "2": fixture_json("dictionaries_localities_05_p2.json"),
    }
    api.get("/dictionaries/localities").mock(
        side_effect=lambda request: json_response(pages[request.url.params["page"]])
    )
    entries = client.list_localities("05")
    assert "ŁÓDŹ" in [e.name for e in entries]
    assert len(entries) == 14


def test_throttles_between_requests(
    client: NfzClient,
    api: respx.MockRouter,
    fake_time: FakeTime,
    fixture_json: Callable[[str], Any],
) -> None:
    api.get("/benefits").mock(return_value=json_response(fixture_json("benefits_orto.json")))
    client.search_benefits("orto")
    client.search_benefits("orto")
    assert fake_time.sleeps == [pytest.approx(0.4)]


def test_retries_429_respecting_retry_after(
    client: NfzClient,
    api: respx.MockRouter,
    fake_time: FakeTime,
    fixture_json: Callable[[str], Any],
) -> None:
    route = api.get("/benefits").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(429),
            json_response(fixture_json("benefits_orto.json")),
        ]
    )
    assert client.search_benefits("orto")
    assert route.call_count == 3
    # 3 s z Retry-After, potem backoff 2 s (1 * 2**1); throttling mieści się w tych przerwach
    assert fake_time.sleeps == [3.0, 2.0]


def test_retries_server_errors_and_network_failures(
    client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
) -> None:
    route = api.get("/benefits").mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectTimeout("timeout"),
            json_response(fixture_json("benefits_orto.json")),
        ]
    )
    assert client.search_benefits("orto")
    assert route.call_count == 3


def test_gives_up_after_max_retries(fake_time: FakeTime, api: respx.MockRouter) -> None:
    route = api.get("/benefits").mock(return_value=httpx.Response(429))
    with (
        NfzClient(max_retries=2, sleep=fake_time.sleep, clock=fake_time.clock) as c,
        pytest.raises(NfzApiError, match="429"),
    ):
        c.search_benefits("orto")
    assert route.call_count == 3


def test_waf_html_page_is_retried_then_reported(fake_time: FakeTime, api: respx.MockRouter) -> None:
    html = httpx.Response(200, text="<html>Incapsula</html>", headers={"content-type": "text/html"})
    api.get("/benefits").mock(return_value=html)
    with (
        NfzClient(max_retries=1, sleep=fake_time.sleep, clock=fake_time.clock) as c,
        pytest.raises(NfzApiError, match="HTML"),
    ):
        c.search_benefits("orto")


def test_400_error_message_comes_from_api(
    client: NfzClient, api: respx.MockRouter, fixture_json: Callable[[str], Any]
) -> None:
    route = api.get("/localities").mock(
        return_value=json_response(fixture_json("error_400_name_too_short.json"), status=400)
    )
    with pytest.raises(NfzApiError, match="wymagany atrybut name"):
        client.search_localities("abc")
    assert route.call_count == 1  # błędów 4xx nie ponawiamy


def test_rejects_too_short_name_without_request(client: NfzClient, api: respx.MockRouter) -> None:
    route = api.get("/benefits")
    with pytest.raises(ValueError, match="3 znaki"):
        client.search_benefits("or")
    assert route.call_count == 0


def test_unexpected_payload_shape(client: NfzClient, api: respx.MockRouter) -> None:
    api.get("/benefits").mock(return_value=json_response({"data": [{"nie": "napis"}]}))
    with pytest.raises(NfzApiError, match="format odpowiedzi"):
        client.search_benefits("orto")
