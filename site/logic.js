// Czysta logika strony (bez DOM) - testowana w site/tests/logic.test.mjs.
// Odpowiada funkcjom z pakietu Pythona (text.fold, search._stem, search._matches_locality).

const FOLD_MAP = { ą: "a", ć: "c", ę: "e", ł: "l", ń: "n", ó: "o", ś: "s", ź: "z", ż: "z" };

/** Tekst do porównań: małe litery, bez polskich znaków, pojedyncze spacje. */
export function fold(text) {
  return String(text)
    .toLowerCase()
    .replace(/[ąćęłńóśźż]/g, (ch) => FOLD_MAP[ch])
    .replace(/\s+/g, " ")
    .trim();
}

/** Przybliżony rdzeń słowa ("ortopeda" -> "ortop"), żeby "ortopeda" znalazło "ORTOPEDII". */
export function stem(word) {
  return word.length <= 4 ? word : word.slice(0, Math.max(4, word.length - 3));
}

// Potoczne nazwy i skróty, których nie da się dopasować po rdzeniu słowa
export const ALIASES = {
  tk: "tomografia komputerowa",
  ct: "tomografia komputerowa",
  tomograf: "tomografia komputerowa",
  mri: "rezonans magnetyczny",
  mr: "rezonans magnetyczny",
  rezonans: "rezonans magnetyczny",
  dentysta: "stomatolog",
  psychiatra: "zdrowia psychicznego",
  pulmonolog: "pluc",
};

function benefitGroup(folded) {
  if (folded.startsWith("poradnia") || folded.startsWith("swiadczenia z zakresu")) return 0;
  if (folded.startsWith("oddzial")) return 2;
  return 1;
}

/**
 * Świadczenia pasujące do wpisanego tekstu, najlepsze na początku:
 * poradnie przed oddziałami szpitalnymi, wersje dla dzieci niżej, potem liczba placówek.
 * @param {{name: string, folded: string, count: number}[]} benefits
 */
export function rankBenefits(benefits, query, limit = 15) {
  let folded = fold(query);
  folded = ALIASES[folded] ?? folded;
  const stems = folded ? folded.split(" ").map(stem) : [];
  return benefits
    .filter((b) => b.count > 0 && stems.every((s) => b.folded.includes(s)))
    .sort(
      (a, b) =>
        benefitGroup(a.folded) - benefitGroup(b.folded) ||
        Number(a.folded.includes("dziec")) - Number(b.folded.includes("dziec")) ||
        b.count - a.count ||
        a.folded.localeCompare(b.folded, "pl"),
    )
    .slice(0, limit);
}

/**
 * Miasto, do którego należy placówka. Dzielnice mają w danych NFZ postać
 * "WARSZAWA MOKOTÓW" albo "ŁÓDŹ-BAŁUTY", a ich powiat to samo miasto.
 */
export function cityOf(locality, county) {
  if (!locality) return "";
  if (
    county &&
    locality !== county &&
    (locality.startsWith(county + " ") || locality.startsWith(county + "-"))
  ) {
    return county;
  }
  return locality;
}

const LOWERCASE_WORDS = new Set(["nad", "pod", "w", "i", "z", "na", "przy"]);

/** "BIELSKO-BIAŁA" -> "Bielsko-Biała", "KOSTRZYN NAD ODRĄ" -> "Kostrzyn nad Odrą". */
export function titleCase(name) {
  return name
    .toLowerCase()
    .split(" ")
    .map((word, i) =>
      i > 0 && LOWERCASE_WORDS.has(word)
        ? word
        : word
            .split("-")
            .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
            .join("-"),
    )
    .join(" ");
}

/** Polska odmiana: plural(5, ["placówka", "placówki", "placówek"]) -> "placówek". */
export function plural(n, [one, few, many]) {
  if (n === 1) return one;
  const lastDigit = n % 10;
  const lastTwo = n % 100;
  if (lastDigit >= 2 && lastDigit <= 4 && (lastTwo < 12 || lastTwo > 14)) return few;
  return many;
}

/** Wiersze z pliku danych (tablice w kolejności `fields`) -> obiekty. */
export function decodeRows(data) {
  const idx = Object.fromEntries(data.fields.map((name, i) => [name, i]));
  return data.rows.map((row) => {
    const locality = row[idx.locality];
    const county = row[idx.county];
    return {
      benefit: row[idx.benefit],
      pcus: row[idx.pcus],
      days: row[idx.days],
      asOf: row[idx.as_of],
      provider: row[idx.provider],
      place: row[idx.place],
      address: row[idx.address],
      locality,
      county,
      city: cityOf(locality, county),
      phone: row[idx.phone],
      website: row[idx.website],
      lat: row[idx.lat],
      lon: row[idx.lon],
      flags: row[idx.flags],
      awaiting: row[idx.awaiting],
    };
  });
}

/** Numer telefonu do linku tel: (bez spacji, nawiasów i myślników). */
export function telHref(phone) {
  const digits = String(phone).replace(/[^\d+]/g, "");
  return digits.length >= 7 ? `tel:${digits}` : null;
}

/** Adres strony placówki jako bezpieczny link http(s) albo null. */
export function websiteHref(website) {
  const value = String(website ?? "").trim();
  if (!value || /\s/.test(value)) return null;
  const url = /^https?:\/\//i.test(value) ? value : `http://${value}`;
  try {
    const parsed = new URL(url);
    return parsed.hostname.includes(".") ? parsed.href : null;
  } catch {
    return null;
  }
}

/** Link do mapy: po współrzędnych, jeśli są, inaczej po adresie. */
export function mapHref(row) {
  const query =
    row.lat != null && row.lon != null
      ? `${row.lat},${row.lon}`
      : [row.address, row.locality].filter(Boolean).join(", ");
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`;
}

/** Czy punkt leży w Polsce (prostokąt z zapasem, jak POLAND_LAT/LON w export.py). */
export function inPoland(lat, lon) {
  return lat != null && lon != null && lat >= 48.9 && lat <= 55.0 && lon >= 14.0 && lon <= 24.3;
}
