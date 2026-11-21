"""Interfejs wiersza poleceń: `nfz szukaj ortopeda --woj slaskie --miasto katowice`."""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.prompt import IntPrompt
from rich.table import Table
from rich.text import Text

from nfz_terminy import __version__
from nfz_terminy.cache import LocalityCache
from nfz_terminy.client import DEFAULT_BASE_URL, NfzApiError, NfzClient
from nfz_terminy.models import QueueEntry
from nfz_terminy.regions import PROVINCES, parse_province
from nfz_terminy.search import SearchResult, find_queues, resolve_benefit, resolve_locality
from nfz_terminy.text import has_polish_chars

DISCLAIMER = (
    "Prognoza NFZ (PCUS) to przewidywany czas oczekiwania według stanu na podany dzień. "
    "Placówki aktualizują dane mniej więcej raz w tygodniu - zawsze potwierdź termin "
    "telefonicznie. Źródło danych: Narodowy Fundusz Zdrowia, https://api.nfz.gov.pl/"
)

app = typer.Typer(
    name="nfz",
    help="Najkrótsze kolejki do specjalistów na NFZ (dane: API NFZ Terminy Leczenia).",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

out = Console()
err = Console(stderr=True)


def make_client() -> NfzClient:
    """Tworzy klienta API (adres można nadpisać zmienną NFZ_API_URL)."""
    return NfzClient(base_url=os.environ.get("NFZ_API_URL", DEFAULT_BASE_URL))


def make_cache() -> LocalityCache:
    return LocalityCache()


def _version_callback(value: bool) -> None:
    if value:
        out.print(f"nfz-terminy {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", help="Pokaż wersję i zakończ.", callback=_version_callback, is_eager=True
        ),
    ] = False,
) -> None:
    """Najkrótsze kolejki do specjalistów na NFZ (dane: API NFZ Terminy Leczenia)."""


def _stdin_is_interactive() -> bool:
    """Czy można zadać pytanie użytkownikowi.

    Na Windows `isatty()` zwraca True także dla urządzenia NUL (np. `< NUL` w skryptach),
    więc dodatkowo sprawdzamy, czy uchwyt to naprawdę konsola.
    """
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt

    handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    mode = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def _fail(message: str, code: int = 1) -> typer.Exit:
    err.print(Text(message, style="bold red"))
    return typer.Exit(code)


def _choose(options: list[str], what: str, *, preselected: int | None, interactive: bool) -> str:
    """Wybór jednej pozycji z listy: automatycznie, z --wybierz albo interaktywnie."""
    if len(options) == 1:
        return options[0]
    if preselected is not None:
        if 1 <= preselected <= len(options):
            return options[preselected - 1]
        raise _fail(f"--wybierz {preselected}: dostępne numery 1-{len(options)}.", 2)

    err.print(f"Pasuje kilka pozycji ({what}):")
    for number, option in enumerate(options, start=1):
        err.print(Text(f"  {number:>2}. {option}"))
    if not interactive:
        raise _fail(
            f"Doprecyzuj nazwę albo wskaż numer opcją --wybierz N ({what}).",
            2,
        )
    while True:
        choice = IntPrompt.ask("Wybierz numer", default=1, console=err)
        if 1 <= choice <= len(options):
            return options[choice - 1]
        err.print(f"Podaj liczbę od 1 do {len(options)}.")


def _entries_table(entries: list[QueueEntry], title: str) -> Table:
    table = Table(title=title, title_justify="left", show_lines=False, expand=False)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Prognoza", style="bold green", no_wrap=True)
    table.add_column("Dni", justify="right", no_wrap=True)
    table.add_column("Stan na", no_wrap=True, style="dim")
    table.add_column("Placówka", ratio=3)
    table.add_column("Adres", ratio=2)
    table.add_column("Miasto", ratio=1)
    table.add_column("Telefon", no_wrap=True)

    for number, entry in enumerate(entries, start=1):
        placowka = Text(entry.provider or "-")
        if entry.place:
            placowka.append(f"\n{entry.place}", style="dim")
        table.add_row(
            str(number),
            entry.pcus,
            f"~{entry.wait_days}" if entry.wait_days is not None else "?",
            entry.situation_as_at.strftime("%d.%m.%Y") if entry.situation_as_at else "-",
            placowka,
            Text(entry.address or "-"),
            Text(entry.locality or "-"),
            entry.registration_phone or entry.phone or "-",
        )
    return table


def _print_json(payload: dict[str, Any]) -> None:
    # niezależnie od strony kodowej konsoli Windows wyjście maszynowe ma być w UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


@app.command("szukaj")
def search_command(
    swiadczenie: Annotated[
        str,
        typer.Argument(
            help='Świadczenie lub specjalista, np. ortopeda, okulista, "poradnia dermatologiczna".',
            show_default=False,
        ),
    ],
    woj: Annotated[
        str | None,
        typer.Option("--woj", "-w", help="Województwo: nazwa (śląskie / slaskie) albo kod 01-16."),
    ] = None,
    miasto: Annotated[
        str | None,
        typer.Option("--miasto", "-m", help="Miejscowość (z polskimi znakami lub bez)."),
    ] = None,
    pilny: Annotated[
        bool, typer.Option("--pilny", help="Przypadek pilny zamiast stabilnego.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Ile placówek pokazać.")] = 20,
    json_output: Annotated[
        bool, typer.Option("--json", help="Wynik w formacie JSON (dla skryptów).")
    ] = False,
    wybierz: Annotated[
        int | None,
        typer.Option(
            "--wybierz",
            min=1,
            help="Numer świadczenia z listy, gdy nazwa pasuje do kilku (bez pytania).",
        ),
    ] = None,
) -> None:
    """Szukaj placówek z najkrótszą prognozą czasu oczekiwania."""
    try:
        province = parse_province(woj) if woj else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--woj") from exc

    interactive = not json_output and _stdin_is_interactive()

    try:
        with make_client() as client:
            with err.status("Szukam świadczenia..."):
                benefits = resolve_benefit(client, swiadczenie)
            if not benefits:
                raise _fail(
                    f"Nie znaleziono świadczenia pasującego do {swiadczenie!r}. "
                    "Spróbuj innej nazwy lub sprawdź: nfz swiadczenia <fraza>"
                )
            benefit = _choose(benefits, "świadczenie", preselected=wybierz, interactive=interactive)

            locality: str | None = None
            if miasto:
                with err.status("Szukam miejscowości..."):
                    localities = resolve_locality(client, miasto, province, make_cache())
                if not localities:
                    hint = ""
                    if not province and not has_polish_chars(miasto):
                        hint = (
                            " Wyszukiwarka NFZ rozróżnia polskie znaki: wpisz nazwę z nimi"
                            " albo podaj województwo (--woj), a dopasuję ją sam."
                        )
                    raise _fail(f"Nie znaleziono miejscowości {miasto!r}.{hint}")
                locality = _choose(
                    localities, "miejscowość", preselected=None, interactive=interactive
                )

            with err.status("Pobieram kolejki...") as status:

                def on_progress(page: int, total: int) -> None:
                    status.update(f"Pobieram kolejki: strona {page}/{total}")

                result = find_queues(
                    client,
                    benefit=benefit,
                    province=province,
                    locality=locality,
                    urgent=pilny,
                    on_progress=on_progress,
                )
    except NfzApiError as exc:
        raise _fail(str(exc)) from exc
    except ValueError as exc:
        raise _fail(str(exc), 2) from exc

    if json_output:
        _print_json(_json_payload(result, benefit, province, locality, pilny, limit))
        return
    _print_results(result, benefit, province, locality, pilny, limit)


def _json_payload(
    result: SearchResult,
    benefit: str,
    province: str | None,
    locality: str | None,
    urgent: bool,
    limit: int,
) -> dict[str, Any]:
    return {
        "query": {
            "benefit": benefit,
            "province": province,
            "province_name": PROVINCES.get(province) if province else None,
            "locality": locality,
            "urgent": urgent,
        },
        "source": DEFAULT_BASE_URL,
        "total": len(result.entries),
        "without_forecast": result.without_forecast,
        "results": [e.model_dump(mode="json") for e in result.entries[:limit]],
    }


def _print_results(
    result: SearchResult,
    benefit: str,
    province: str | None,
    locality: str | None,
    urgent: bool,
    limit: int,
) -> None:
    where = ", ".join(
        part
        for part in (locality, f"woj. {PROVINCES[province]}" if province else "cała Polska")
        if part
    )
    case = "pilny" if urgent else "stabilny"
    title = f"{benefit}\n{where} | przypadek {case}"

    if not result.entries:
        out.print(Text(title, style="bold"))
        skipped = (
            f" ({result.without_forecast} placówek nie podało prognozy)"
            if result.without_forecast
            else ""
        )
        out.print(f"Brak placówek z prognozą czasu oczekiwania{skipped}.")
        return

    shown = result.entries[:limit]
    out.print(_entries_table(shown, title))
    summary = f"Pokazano {len(shown)} z {len(result.entries)} placówek z prognozą"
    if result.without_forecast:
        summary += f" (pominięto {result.without_forecast} bez prognozy)"
    out.print(summary + ".")
    out.print(Text(DISCLAIMER, style="dim"))


@app.command("swiadczenia")
def benefits_command(
    fraza: Annotated[
        str, typer.Argument(help="Fragment nazwy, np. orto, kardio.", show_default=False)
    ],
) -> None:
    """Pokaż nazwy świadczeń NFZ pasujące do frazy."""
    try:
        with make_client() as client, err.status("Szukam świadczeń..."):
            names = resolve_benefit(client, fraza)
    except NfzApiError as exc:
        raise _fail(str(exc)) from exc
    except ValueError as exc:
        raise _fail(str(exc), 2) from exc
    if not names:
        raise _fail(f"Brak świadczeń pasujących do {fraza!r}.")
    for name in names:
        out.print(Text(name))


@app.command("wojewodztwa")
def provinces_command() -> None:
    """Pokaż kody województw używane przez NFZ."""
    table = Table("Kod", "Województwo")
    for code, name in PROVINCES.items():
        table.add_row(code, name)
    out.print(table)
