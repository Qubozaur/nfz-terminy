"""Generuje docs/demo.svg - zrzut działania CLI na prawdziwych danych z API NFZ.

Użycie (z katalogu repozytorium):
    python scripts/zrzut_ekranu.py
"""

from __future__ import annotations

import io
import shlex
from pathlib import Path

from rich.console import Console

from nfz_terminy import cli

ARGS = ["szukaj", "ortopeda", "--woj", "slaskie", "--miasto", "katowice", "--limit", "8"]
WIDTH = 132
OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "demo.svg"


def main() -> None:
    console = Console(record=True, width=WIDTH, force_terminal=True)
    cli.out = console
    # komunikaty postępu (spinner) idą na stderr - w zrzucie ich nie chcemy
    cli.err = Console(file=io.StringIO())
    cli._stdin_is_interactive = lambda: False  # type: ignore[assignment]

    console.print(f"[bold]$[/] nfz {shlex.join(ARGS)} --wybierz 1")
    try:
        cli.app([*ARGS, "--wybierz", "1"], standalone_mode=False)
    finally:
        OUTPUT.parent.mkdir(exist_ok=True)
        console.save_svg(str(OUTPUT), title="nfz-terminy")
    print(f"Zapisano {OUTPUT}")


if __name__ == "__main__":
    main()
