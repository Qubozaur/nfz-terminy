"""Wspólne fixture'y testów: wczytywanie zapisanych odpowiedzi API."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def fixture_json() -> Callable[[str], Any]:
    return load_fixture
