# Fixture'y testowe

Prawdziwe odpowiedzi API NFZ „Terminy Leczenia” w wersji 1.4
(`https://apinfz.nfz.gov.pl/app-itl-api-pcus/`), pobrane 2026-11-21.

| Plik | Zapytanie |
|---|---|
| `benefits_orto.json` | `GET /benefits?name=orto` |
| `benefits_ortopeda.json` | `GET /benefits?name=ortopeda` |
| `benefits_kardio.json` | `GET /benefits?name=kardio` |
| `benefits_pluc.json` | `GET /benefits?name=pluc` (0 wyników: wyszukiwanie rozróżnia polskie znaki) |
| `localities_katow_12.json` | `GET /localities?name=katow&province=12` |
| `localities_lodz_05.json` | `GET /localities?name=lodz&province=05` (0 wyników, jak wyżej) |
| `queues_ortopedia_katowice.json` | `GET /queues?case=1&province=12&benefit=ŚWIADCZENIA Z ZAKRESU ORTOPEDII I TRAUMATOLOGII NARZĄDU RUCHU&locality=KATOWICE` |
| `queues_dermatologia_lodz_p{1,2}.json` | `GET /queues?case=1&province=05&benefit=PORADNIA DERMATOLOGICZNA&locality=ŁÓDŹ` strony 1 i 2 (zawiera też „… DLA DZIECI”) |
| `queues_endoprotezoplastyka_06_p{1,2}.json` | `GET /queues?case=1&province=06&benefit=ENDOPROTEZOPLASTYKA STAWU BIODROWEGO` strony 1 i 2 (zawiera wpisy bez prognozy) |
| `error_400_name_too_short.json` | `GET /benefits?name=xq` (błąd 400) |
| `dictionaries_localities_05_p{1,2}.json` | `GET /dictionaries/localities?province=05`, **przycięte**: wybrane prawdziwe wpisy z 8 stron rozłożone na 2 strony, żeby test paginacji był krótki |

Wszystkie zapytania wysłano z `limit=25&format=json&api-version=1.4`.
