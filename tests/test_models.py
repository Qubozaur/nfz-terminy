from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from nfz_terminy.models import ApiErrorResponse, LocalityEntry, Page, Queue, QueueEntry


def test_queues_page_parses_real_response(fixture_json: Callable[[str], Any]) -> None:
    page = Page[Queue].model_validate(fixture_json("queues_ortopedia_katowice.json"))

    assert page.meta.count == 23
    assert len(page.data) == 23
    assert not page.has_next

    first = page.data[0].attributes
    assert first.benefit == "ŚWIADCZENIA Z ZAKRESU ORTOPEDII I TRAUMATOLOGII NARZĄDU RUCHU"
    assert first.locality == "KATOWICE"
    assert first.dates is not None
    assert first.dates.applicable is True
    assert first.dates.pcus == "24 dni"
    assert first.dates.situation_as_at == date(2026, 11, 4)
    assert first.statistics is not None
    assert first.statistics.provider_data is not None
    assert first.statistics.provider_data.awaiting == 119


def test_page_with_next_link(fixture_json: Callable[[str], Any]) -> None:
    page = Page[Queue].model_validate(fixture_json("queues_dermatologia_lodz_p1.json"))
    assert page.meta.count == 36
    assert page.has_next


def test_string_and_dictionary_pages(fixture_json: Callable[[str], Any]) -> None:
    benefits = Page[str].model_validate(fixture_json("benefits_orto.json"))
    assert "ŚWIADCZENIA Z ZAKRESU ORTOPEDII I TRAUMATOLOGII NARZĄDU RUCHU" in benefits.data

    localities = Page[LocalityEntry].model_validate(
        fixture_json("dictionaries_localities_05_p2.json")
    )
    assert any(entry.name == "ŁÓDŹ" for entry in localities.data)
    assert all(entry.province == "05" for entry in localities.data)


def test_error_response(fixture_json: Callable[[str], Any]) -> None:
    err = ApiErrorResponse.model_validate(fixture_json("error_400_name_too_short.json"))
    assert err.errors[0].error_code == 1200022
    assert "name" in (err.errors[0].error_reason or "")


def test_queue_entry_flattens_and_converts_flags(fixture_json: Callable[[str], Any]) -> None:
    page = Page[Queue].model_validate(fixture_json("queues_ortopedia_katowice.json"))
    entry = QueueEntry.from_queue(page.data[0])

    assert entry is not None
    assert entry.pcus == "24 dni"
    assert entry.wait_days == 24
    assert entry.phone == "+48 32 259 51 45"
    assert entry.elevator is True
    assert entry.ramp is False
    assert entry.awaiting == 119


def test_queue_entry_without_forecast_is_skipped(fixture_json: Callable[[str], Any]) -> None:
    page = Page[Queue].model_validate(fixture_json("queues_endoprotezoplastyka_06_p2.json"))
    without = [q for q in page.data if q.attributes.dates is None]

    assert without, "fixture powinien zawierać wpisy bez prognozy"
    assert all(QueueEntry.from_queue(q) is None for q in without)
