from __future__ import annotations

import pytest

from nfz_terminy.pcus import parse_pcus
from nfz_terminy.regions import PROVINCES, parse_province
from nfz_terminy.text import fold, has_polish_chars


class TestFold:
    def test_removes_polish_chars_including_l_stroke(self) -> None:
        assert fold("ŁÓDŹ-BAŁUTY") == "lodz-baluty"
        assert fold("Śląskie") == "slaskie"
        assert fold("zażółć gęślą jaźń") == "zazolc gesla jazn"

    def test_collapses_whitespace(self) -> None:
        assert fold("  Bielsko   Biała ") == "bielsko biala"

    def test_has_polish_chars(self) -> None:
        assert has_polish_chars("łódź")
        assert not has_polish_chars("lodz")


class TestParseProvince:
    @pytest.mark.parametrize(
        ("value", "code"),
        [
            ("12", "12"),
            ("2", "02"),
            ("śląskie", "12"),
            ("slaskie", "12"),
            ("Śląskie", "12"),
            ("ŚLĄSKIE", "12"),
            ("woj. śląskie", "12"),
            ("kujawsko pomorskie", "02"),
            ("kujawsko-pomorskie", "02"),
            ("lodzkie", "05"),
            ("maz", "07"),
            ("swietokrzyskie", "13"),
            ("zachodniopomorskie", "16"),
        ],
    )
    def test_accepts_codes_names_and_prefixes(self, value: str, code: str) -> None:
        assert parse_province(value) == code

    def test_all_names_roundtrip(self) -> None:
        for code, name in PROVINCES.items():
            assert parse_province(name) == code

    def test_ambiguous_prefix(self) -> None:
        # "pod" -> podkarpackie i podlaskie
        with pytest.raises(ValueError, match="Niejednoznaczne"):
            parse_province("pod")

    @pytest.mark.parametrize("value", ["17", "00", "atlantyda", "sl"])
    def test_unknown(self, value: str) -> None:
        with pytest.raises(ValueError, match="Nieznan"):
            parse_province(value)


class TestParsePcus:
    @pytest.mark.parametrize(
        ("text", "days"),
        [
            # formaty spotkane w prawdziwych odpowiedziach API 1.4
            ("0 dni", 0),
            ("1 dzień", 1),
            ("24 dni", 24),
            ("2 mies.", 60),
            ("1 mies. 1 tydz.", 37),
            ("3 mies. 2 tyg.", 104),
            ("49 mies. 1 tydz.", 1477),
            # warianty na zapas
            ("2 tygodnie", 14),
            ("1 dnia", 1),
            ("1 rok 2 mies.", 425),
            ("  5 DNI ", 5),
        ],
    )
    def test_known_formats(self, text: str, days: int) -> None:
        assert parse_pcus(text) == days

    @pytest.mark.parametrize("text", [None, "", "brak", "około 2 mies.", "2 mies. lub dłużej", "5"])
    def test_unknown_or_missing_returns_none(self, text: str | None) -> None:
        assert parse_pcus(text) is None
