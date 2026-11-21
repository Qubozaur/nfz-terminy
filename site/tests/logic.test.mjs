// Uruchomienie: node --test site/tests/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  cityOf,
  decodeRows,
  fold,
  mapHref,
  plural,
  rankBenefits,
  telHref,
  titleCase,
  websiteHref,
} from "../logic.js";

const benefit = (name, count = 10) => ({ name, folded: fold(name), count });

test("fold usuwa polskie znaki, także ł", () => {
  assert.equal(fold("  ŁÓDŹ-Bałuty "), "lodz-baluty");
  assert.equal(fold("ŚLĄSKIE"), "slaskie");
});

test("rankBenefits: rdzeń słowa, bez polskich znaków, poradnie przed oddziałami", () => {
  const all = [
    benefit("ODDZIAŁ CHIRURGII URAZOWO-ORTOPEDYCZNEJ", 40),
    benefit("ODDZIAŁ CHIRURGII URAZOWO-ORTOPEDYCZNEJ DLA DZIECI", 5),
    benefit("ŚWIADCZENIA Z ZAKRESU ORTOPEDII I TRAUMATOLOGII NARZĄDU RUCHU", 23),
    benefit("ŚWIADCZENIA Z ZAKRESU OKULISTYKI", 50),
  ];
  assert.deepEqual(
    rankBenefits(all, "ortopeda").map((b) => b.name),
    [
      "ŚWIADCZENIA Z ZAKRESU ORTOPEDII I TRAUMATOLOGII NARZĄDU RUCHU",
      "ODDZIAŁ CHIRURGII URAZOWO-ORTOPEDYCZNEJ",
      "ODDZIAŁ CHIRURGII URAZOWO-ORTOPEDYCZNEJ DLA DZIECI",
    ],
  );
  assert.deepEqual(
    rankBenefits([benefit("ŚWIADCZENIA Z ZAKRESU GRUŹLICY I CHORÓB PŁUC")], "pluca").length,
    1,
  );
});

test("rankBenefits: aliasy, wiele słów i pomijanie świadczeń bez placówek", () => {
  const all = [
    benefit("TOMOGRAFIA KOMPUTEROWA"),
    benefit("PORADNIA CHIRURGII NACZYNIOWEJ"),
    benefit("PORADNIA CHIRURGII OGÓLNEJ"),
    benefit("PORADNIA ZDROWIA PSYCHICZNEGO", 0),
  ];
  assert.deepEqual(rankBenefits(all, "TK").map((b) => b.name), ["TOMOGRAFIA KOMPUTEROWA"]);
  assert.deepEqual(rankBenefits(all, "chirurg naczyniowy").map((b) => b.name), [
    "PORADNIA CHIRURGII NACZYNIOWEJ",
  ]);
  assert.equal(rankBenefits(all, "psychiatra").length, 0);
});

test("rankBenefits bez zapytania zwraca najpopularniejsze poradnie", () => {
  const all = [benefit("ODDZIAŁ X", 100), benefit("PORADNIA A", 5), benefit("PORADNIA B", 50)];
  assert.deepEqual(rankBenefits(all, "", 2).map((b) => b.name), ["PORADNIA B", "PORADNIA A"]);
});

test("cityOf łączy dzielnice z miastem po powiecie", () => {
  assert.equal(cityOf("WARSZAWA MOKOTÓW", "WARSZAWA"), "WARSZAWA");
  assert.equal(cityOf("ŁÓDŹ-BAŁUTY", "ŁÓDŹ"), "ŁÓDŹ");
  assert.equal(cityOf("KATOWICE", "KATOWICE"), "KATOWICE");
  assert.equal(cityOf("BIAŁA PODLASKA", "BIAŁA PODLASKA"), "BIAŁA PODLASKA");
  assert.equal(cityOf("KATOWICE GÓRNE", "BĘDZIŃSKI"), "KATOWICE GÓRNE");
});

test("titleCase dla nazw miejscowości", () => {
  assert.equal(titleCase("BIELSKO-BIAŁA"), "Bielsko-Biała");
  assert.equal(titleCase("KOSTRZYN NAD ODRĄ"), "Kostrzyn nad Odrą");
  assert.equal(titleCase("ŁÓDŹ"), "Łódź");
});

test("plural - polska odmiana", () => {
  const forms = ["placówka", "placówki", "placówek"];
  assert.deepEqual(
    [1, 2, 4, 5, 12, 14, 22, 25, 101, 112].map((n) => plural(n, forms)),
    ["placówka", "placówki", "placówki", "placówek", "placówek", "placówek", "placówki", "placówek", "placówek", "placówek"],
  );
});

test("decodeRows zamienia tablice na obiekty według fields", () => {
  const data = {
    fields: ["benefit", "pcus", "days", "as_of", "provider", "place", "address", "locality", "county", "phone", "website", "lat", "lon", "flags", "awaiting"],
    rows: [[0, "24 dni", 24, "2026-11-04", "P", "PORADNIA", "PCK 1", "WARSZAWA WOLA", "WARSZAWA", "+48 1", null, null, null, 2, 119]],
  };
  const [row] = decodeRows(data);
  assert.equal(row.days, 24);
  assert.equal(row.city, "WARSZAWA");
  assert.equal(row.flags, 2);
});

test("linki: telefon, strona, mapa", () => {
  assert.equal(telHref("+48 32 259 51 45"), "tel:+48322595145");
  assert.equal(telHref("(0-22) 560-00-70"), "tel:0225600070");
  assert.equal(telHref("brak"), null);

  assert.equal(websiteHref("www.spzla-katowice.pl"), "http://www.spzla-katowice.pl/");
  assert.equal(websiteHref("https://example.pl/a"), "https://example.pl/a");
  assert.equal(websiteHref("javascript:alert(1)"), null);
  assert.equal(websiteHref("nie ma"), null);
  assert.equal(websiteHref(""), null);

  assert.match(mapHref({ lat: 51.7, lon: 19.4 }), /query=51\.7%2C19\.4$/);
  assert.match(mapHref({ address: "PCK 1", locality: "KATOWICE" }), /query=PCK%201%2C%20KATOWICE$/);
});
