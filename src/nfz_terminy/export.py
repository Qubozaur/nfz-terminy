"""Eksport wszystkich kolejek do statycznych plików JSON dla strony WWW (GitHub Pages).

API NFZ nie pozwala na zapytania z przeglądarki (brak nagłówków CORS), więc strona
korzysta z plików wygenerowanych wcześniej przez ten moduł - w GitHub Actions raz na dobę.
Dane źródłowe placówki aktualizują mniej więcej raz w tygodniu, więc to wystarcza.

Pliki (jeden na województwo i rodzaj przypadku, np. `12-1.json`) mają zwarty format:
nazwy świadczeń są w osobnej liście, a każdy wiersz to tablica w kolejności `fields`.

Współrzędne placówek uzupełnia geokodowanie adresów w GUGiK (moduł geocode.py), bo NFZ
podaje je tylko dla części placówek, a część podanych jest błędna.

Użycie:
    python -m nfz_terminy.export --out site/data            # cała Polska (ok. 1,5-2 h)
    python -m nfz_terminy.export --out site/data --woj opolskie --przypadek 1
    python -m nfz_terminy.export --out site/data --woj opolskie --geokoduj --geokod-cache geo.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nfz_terminy import __version__
from nfz_terminy.client import DEFAULT_BASE_URL, NfzApiError, NfzClient
from nfz_terminy.geocode import Geocoder, GeoPoint
from nfz_terminy.models import Queue, QueueEntry
from nfz_terminy.regions import PROVINCES, parse_province
from nfz_terminy.text import city_of, fold

FORMAT_VERSION = 1

# Kolejność pól w wierszu pliku danych (strona WWW czyta ją z pliku, nie zgaduje)
FIELDS = [
    "benefit",  # indeks w liście "benefits"
    "pcus",
    "days",
    "as_of",
    "provider",
    "place",
    "address",
    "locality",
    "county",
    "phone",
    "website",
    "lat",
    "lon",
    "flags",
    "awaiting",
]

# Bity pola "flags"
FLAG_RAMP = 1
FLAG_ELEVATOR = 2
FLAG_TOILET = 4
FLAG_CAR_PARK = 8
FLAG_WHEELCHAIRS = 16
FLAG_CHILDREN = 32
# położenie przybliżone (środek ulicy, nie konkretny budynek)
FLAG_APPROX_LOCATION = 64

# Ile razy ponowić całe województwo, gdy klient wyczerpie swoje próby (np. dłuższa awaria)
PROVINCE_ATTEMPTS = 3
PROVINCE_RETRY_PAUSE = 60.0
# Mazowieckie ma ok. 415 stron po 25 kolejek; zapas na wzrost liczby kolejek
EXPORT_MAX_PAGES = 2000

Log = Callable[[str], None]
Locator = Callable[[QueueEntry], GeoPoint | None]


def _flags(entry: QueueEntry) -> int:
    pairs = (
        (entry.ramp, FLAG_RAMP),
        (entry.elevator, FLAG_ELEVATOR),
        (entry.toilet, FLAG_TOILET),
        (entry.car_park, FLAG_CAR_PARK),
        (entry.wheelchairs, FLAG_WHEELCHAIRS),
        (entry.for_children, FLAG_CHILDREN),
    )
    return sum(bit for value, bit in pairs if value)


def _coordinate(value: float | None) -> float | None:
    # 5 miejsc po przecinku to ok. 1 m - więcej tylko powiększa plik
    return round(value, 5) if value is not None else None


# Prostokąt obejmujący Polskę (z małym zapasem) - do odsiewania błędnych współrzędnych
POLAND_LAT = (48.9, 55.0)
POLAND_LON = (14.0, 24.3)


def in_poland(lat: float, lon: float) -> bool:
    return POLAND_LAT[0] <= lat <= POLAND_LAT[1] and POLAND_LON[0] <= lon <= POLAND_LON[1]


def nfz_coordinates(entry: QueueEntry) -> tuple[float, float] | None:
    """Współrzędne z NFZ po sprawdzeniu.

    NFZ czasem zamienia szerokość z długością (zaobserwowane: Bytom jako 18.91, 50.35, czyli
    punkt nad Morzem Arabskim) - takie poprawiamy; inne spoza Polski odrzucamy.
    """
    lat, lon = entry.latitude, entry.longitude
    if lat is None or lon is None:
        return None
    if in_poland(lat, lon):
        return lat, lon
    if in_poland(lon, lat):
        return lon, lat
    return None


def _location(entry: QueueEntry, locate: Locator | None) -> tuple[float | None, float | None, int]:
    """Współrzędne wpisu i ewentualny bit "przybliżone".

    Kolejność: punkt adresowy z GUGiK, potem współrzędne z NFZ (bywają błędne - np. punkt
    zastępczy w polu albo sąsiedni budynek), na końcu środek ulicy z GUGiK.
    """
    point = locate(entry) if locate else None
    if point and not in_poland(point.lat, point.lon):
        point = None
    if point and not point.approximate:
        return point.lat, point.lon, 0
    nfz = nfz_coordinates(entry)
    if nfz:
        return nfz[0], nfz[1], 0
    if point:
        return point.lat, point.lon, FLAG_APPROX_LOCATION
    return None, None, 0


def geocode_key(entry: QueueEntry) -> tuple[str | None, str, str] | None:
    """Adres wpisu w postaci dla Geocoder: (teryt, miasto, adres)."""
    if not entry.locality or not entry.address:
        return None
    return entry.teryt_place, city_of(entry.locality, entry.county), entry.address


def build_province_file(
    queues: Iterable[Queue],
    *,
    province: str,
    urgent: bool,
    generated: datetime,
    locate: Locator | None = None,
) -> dict[str, Any]:
    """Buduje zawartość pliku danych dla jednego województwa i rodzaju przypadku."""
    entries: list[QueueEntry] = []
    missing: Counter[str] = Counter()
    for queue in queues:
        entry = QueueEntry.from_queue(queue)
        if entry is None:
            missing[queue.attributes.benefit or ""] += 1
        else:
            entries.append(entry)

    benefits = sorted({e.benefit for e in entries} | set(missing), key=fold)
    index = {name: i for i, name in enumerate(benefits)}
    entries.sort(
        key=lambda e: (
            index[e.benefit],
            e.wait_days is None,
            e.wait_days if e.wait_days is not None else 0,
            fold(e.provider or ""),
        )
    )
    rows = []
    for e in entries:
        lat, lon, location_flag = _location(e, locate)
        rows.append(
            [
                index[e.benefit],
                e.pcus,
                e.wait_days,
                e.situation_as_at.isoformat() if e.situation_as_at else None,
                e.provider,
                e.place,
                e.address,
                e.locality,
                e.county,
                e.registration_phone or e.phone,
                e.website,
                _coordinate(lat),
                _coordinate(lon),
                _flags(e) | location_flag,
                e.awaiting,
            ]
        )
    return {
        "version": FORMAT_VERSION,
        "province": province,
        "province_name": PROVINCES[province],
        "case": 2 if urgent else 1,
        "generated": generated.isoformat(timespec="seconds"),
        "fields": FIELDS,
        "benefits": benefits,
        "without_forecast": [missing.get(name, 0) for name in benefits],
        "rows": rows,
    }


def _fetch_province(
    client: NfzClient,
    province: str,
    urgent: bool,
    log: Log,
    sleep: Callable[[float], None],
) -> list[Queue]:
    def on_progress(page: int, total: int) -> None:
        if page == 1 or page % 50 == 0 or page == total:
            log(f"    strona {page}/{total}")

    for attempt in range(1, PROVINCE_ATTEMPTS + 1):
        try:
            return client.queues(province=province, urgent=urgent, on_progress=on_progress)
        except NfzApiError as exc:
            if attempt == PROVINCE_ATTEMPTS:
                raise
            log(f"    błąd ({exc}); ponawiam za {PROVINCE_RETRY_PAUSE:.0f} s")
            sleep(PROVINCE_RETRY_PAUSE)
    raise AssertionError("nieosiągalne")


def _geocode(geocoder: Geocoder, queues: list[Queue], deadline: float | None, log: Log) -> Locator:
    """Geokoduje adresy z jednego pliku i zwraca funkcję podającą punkt dla wpisu."""
    keys = {}
    for queue in queues:
        entry = QueueEntry.from_queue(queue)
        key = geocode_key(entry) if entry else None
        if key:
            keys[Geocoder.key(*key)] = key
    batch = geocoder.locate_many(keys.values(), deadline=deadline)
    geocoder.save()
    log(
        f"    geokodowanie: {len(keys)} adresów, z cache {batch.from_cache}, "
        f"nowe {batch.found}, nieznalezione {batch.not_found}, odłożone {batch.skipped}"
    )

    def locate(entry: QueueEntry) -> GeoPoint | None:
        key = geocode_key(entry)
        return batch.points.get(Geocoder.key(*key)) if key else None

    return locate


def export_all(
    client: NfzClient,
    out_dir: Path,
    *,
    provinces: Sequence[str] = tuple(PROVINCES),
    cases: Sequence[int] = (1, 2),
    geocoder: Geocoder | None = None,
    geocode_deadline: float | None = None,
    log: Log = print,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Pobiera dane i zapisuje pliki `<woj>-<przypadek>.json` oraz `index.json`.

    Z `geocoder` współrzędne są uzupełniane z GUGiK; po `geocode_deadline` (time.monotonic)
    nowych adresów już nie geokodujemy - dokończy to kolejne uruchomienie (cache).

    Rzuca NfzApiError, gdy któregoś województwa nie da się pobrać albo gdy wynik jest
    podejrzanie pusty - wtedy lepiej nie publikować niczego i zostawić poprzednie dane.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = now()
    files: dict[str, dict[str, int]] = {}
    started = time.monotonic()

    for province in provinces:
        for case in cases:
            name = f"{province}-{case}"
            log(f"[{name}] {PROVINCES[province]}, przypadek {'pilny' if case == 2 else 'stabilny'}")
            queues = _fetch_province(client, province, case == 2, log, sleep)
            locate = _geocode(geocoder, queues, geocode_deadline, log) if geocoder else None
            data = build_province_file(
                queues, province=province, urgent=case == 2, generated=generated, locate=locate
            )
            if not data["rows"]:
                raise NfzApiError(
                    f"Brak jakichkolwiek prognoz dla {name} ({len(queues)} kolejek) - "
                    "przerywam, żeby nie opublikować pustych danych."
                )
            path = out_dir / f"{name}.json"
            path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            lat_index = FIELDS.index("lat")
            located = sum(1 for row in data["rows"] if row[lat_index] is not None)
            files[name] = {
                "rows": len(data["rows"]),
                "benefits": len(data["benefits"]),
                "located": located,
            }
            elapsed = (time.monotonic() - started) / 60
            log(
                f"    zapisano {len(data['rows'])} wierszy, {located} z położeniem "
                f"({elapsed:.1f} min od startu)"
            )

    manifest = {
        "version": FORMAT_VERSION,
        "generated": generated.isoformat(timespec="seconds"),
        "source": DEFAULT_BASE_URL,
        "generator": f"nfz-terminy {__version__}",
        "provinces": PROVINCES,
        "files": files,
    }
    (out_dir / "index.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nfz_terminy.export",
        description="Eksport kolejek NFZ do plików JSON dla strony WWW.",
    )
    parser.add_argument("--out", type=Path, required=True, help="katalog docelowy")
    parser.add_argument(
        "--woj",
        action="append",
        help="województwo (nazwa lub kod); można podać kilka razy, domyślnie wszystkie",
    )
    parser.add_argument(
        "--przypadek",
        type=int,
        choices=(1, 2),
        action="append",
        help="1 = stabilny, 2 = pilny; domyślnie oba",
    )
    parser.add_argument(
        "--geokoduj", action="store_true", help="uzupełnij współrzędne adresów z GUGiK"
    )
    parser.add_argument(
        "--geokod-cache",
        type=Path,
        help="plik cache geokodowania (zalecany - bez niego każdy adres jest sprawdzany od nowa)",
    )
    parser.add_argument(
        "--geokod-minut",
        type=float,
        default=210.0,
        help="po tylu minutach od startu nie geokoduj nowych adresów (domyślnie 210)",
    )
    args = parser.parse_args(argv)

    try:
        provinces = [parse_province(v) for v in args.woj] if args.woj else list(PROVINCES)
    except ValueError as exc:
        parser.error(str(exc))
    cases = args.przypadek or [1, 2]

    def log(message: str) -> None:
        print(message, flush=True)

    deadline = time.monotonic() + args.geokod_minut * 60
    geocoder = Geocoder(args.geokod_cache) if args.geokoduj else None
    # eksport trwa długo, więc klient jest cierpliwszy niż w CLI
    # max_pages: mazowieckie to ponad 400 stron (10 tys. kolejek) - limit CLI by je uciął
    with NfzClient(
        min_interval=0.3, max_retries=8, max_backoff=120.0, max_pages=EXPORT_MAX_PAGES
    ) as client:
        try:
            manifest = export_all(
                client,
                args.out,
                provinces=provinces,
                cases=cases,
                geocoder=geocoder,
                geocode_deadline=deadline,
                log=log,
            )
        except NfzApiError as exc:
            print(f"BŁĄD: {exc}", file=sys.stderr)
            return 1
        finally:
            if geocoder:
                geocoder.save()
                geocoder.close()
    total = sum(f["rows"] for f in manifest["files"].values())
    located = sum(f["located"] for f in manifest["files"].values())
    log(f"Gotowe: {len(manifest['files'])} plików, {total} wierszy ({located} z położeniem)")
    if geocoder:
        log(f"Zapytań do GUGiK: {geocoder.requests}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
