from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from nfz_terminy.client import DEFAULT_BASE_URL, NfzApiError, NfzClient
from nfz_terminy.export import (
    FIELDS,
    FLAG_CHILDREN,
    FLAG_ELEVATOR,
    FLAG_RAMP,
    build_province_file,
    export_all,
    main,
)
from nfz_terminy.models import Page, Queue

GENERATED = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)


def queues_from(fixture_json: Callable[[str], Any], *names: str) -> list[Queue]:
    return [q for name in names for q in Page[Queue].model_validate(fixture_json(name)).data]


def as_dicts(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(data["fields"], row, strict=True)) for row in data["rows"]]


@pytest.fixture
def client() -> Iterator[NfzClient]:
    with NfzClient(min_interval=0, sleep=lambda _: None) as c:
        yield c


class TestBuildProvinceFile:
    def test_structure_and_sorting(self, fixture_json: Callable[[str], Any]) -> None:
        queues = queues_from(
            fixture_json, "queues_dermatologia_lodz_p1.json", "queues_dermatologia_lodz_p2.json"
        )
        data = build_province_file(queues, province="05", urgent=False, generated=GENERATED)

        assert data["province"] == "05"
        assert data["province_name"] == "łódzkie"
        assert data["case"] == 1
        assert data["generated"] == "2026-11-21T03:00:00+00:00"
        assert data["fields"] == FIELDS
        assert data["benefits"] == [
            "PORADNIA DERMATOLOGICZNA",
            "PORADNIA DERMATOLOGICZNA DLA DZIECI",
        ]
        assert len(data["rows"]) + sum(data["without_forecast"]) == 36

        rows = as_dicts(data)
        for benefit_index in range(len(data["benefits"])):
            days = [r["days"] for r in rows if r["benefit"] == benefit_index]
            assert days == sorted(days)
        # wiersze pogrupowane po świadczeniu
        assert [r["benefit"] for r in rows] == sorted(r["benefit"] for r in rows)

    def test_row_values(self, fixture_json: Callable[[str], Any]) -> None:
        queues = queues_from(fixture_json, "queues_ortopedia_katowice.json")
        data = build_province_file(queues, province="12", urgent=True, generated=GENERATED)

        first = as_dicts(data)[0]
        assert data["case"] == 2
        assert first["pcus"] == "24 dni"
        assert first["days"] == 24
        assert first["as_of"] == "2026-11-04"
        assert first["address"] == "PCK 1"
        assert first["county"] == "KATOWICE"
        assert first["phone"] == "+48 32 259 51 45"
        # winda: tak, podjazd: nie, dla dzieci: nie
        assert first["flags"] & FLAG_ELEVATOR
        assert not first["flags"] & FLAG_RAMP
        assert not first["flags"] & FLAG_CHILDREN

    def test_counts_entries_without_forecast(self, fixture_json: Callable[[str], Any]) -> None:
        queues = queues_from(
            fixture_json,
            "queues_endoprotezoplastyka_06_p1.json",
            "queues_endoprotezoplastyka_06_p2.json",
        )
        data = build_province_file(queues, province="06", urgent=False, generated=GENERATED)
        assert sum(data["without_forecast"]) > 0
        assert all(row[FIELDS.index("pcus")] for row in data["rows"])


def mock_province_api(router: respx.MockRouter, pages: dict[str, Any]) -> respx.Route:
    return router.get("/queues").mock(
        side_effect=lambda r: httpx.Response(200, json=pages[r.url.params["page"]])
    )


def test_export_all_writes_files_and_manifest(
    client: NfzClient, fixture_json: Callable[[str], Any], tmp_path: Path
) -> None:
    pages = {
        "1": fixture_json("queues_dermatologia_lodz_p1.json"),
        "2": fixture_json("queues_dermatologia_lodz_p2.json"),
    }
    with respx.mock(base_url=DEFAULT_BASE_URL) as router:
        route = mock_province_api(router, pages)
        manifest = export_all(
            client, tmp_path, provinces=["05"], log=lambda _: None, now=lambda: GENERATED
        )

    # bez parametru benefit, za to z województwem i oboma przypadkami
    params = [call.request.url.params for call in route.calls]
    assert all("benefit" not in p and p["province"] == "05" for p in params)
    assert sorted({p["case"] for p in params}) == ["1", "2"]

    assert set(manifest["files"]) == {"05-1", "05-2"}
    saved_manifest = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert saved_manifest["generated"] == "2026-11-21T03:00:00+00:00"
    assert saved_manifest["provinces"]["12"] == "śląskie"

    data = json.loads((tmp_path / "05-2.json").read_text(encoding="utf-8"))
    assert data["case"] == 2
    assert len(data["rows"]) == manifest["files"]["05-2"]["rows"]


def test_export_refuses_to_publish_empty_data(client: NfzClient, tmp_path: Path) -> None:
    empty = {"meta": {"count": 0}, "links": {"next": None}, "data": []}
    with respx.mock(base_url=DEFAULT_BASE_URL) as router:
        router.get("/queues").mock(return_value=httpx.Response(200, json=empty))
        with pytest.raises(NfzApiError, match="pustych danych"):
            export_all(client, tmp_path, provinces=["05"], cases=[1], log=lambda _: None)
    assert not (tmp_path / "index.json").exists()


def test_export_retries_province_after_client_gives_up(
    fixture_json: Callable[[str], Any], tmp_path: Path
) -> None:
    payload = fixture_json("queues_ortopedia_katowice.json")
    pauses: list[float] = []
    with (
        NfzClient(min_interval=0, max_retries=0, sleep=lambda _: None) as client,
        respx.mock(base_url=DEFAULT_BASE_URL) as router,
    ):
        router.get("/queues").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json=payload)]
        )
        manifest = export_all(
            client, tmp_path, provinces=["12"], cases=[1], log=lambda _: None, sleep=pauses.append
        )
    assert manifest["files"]["12-1"]["rows"] == 23
    assert pauses == [60.0]


def test_main_rejects_unknown_province(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--out", str(tmp_path), "--woj", "atlantyda"])
