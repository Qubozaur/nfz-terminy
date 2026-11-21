// Strona "Kolejki do specjalistów NFZ" - interfejs (DOM).
// Dane pochodzą z plików data/<woj>-<przypadek>.json generowanych przez nfz_terminy.export.

import {
  decodeRows,
  inPoland,
  fold,
  mapHref,
  plural,
  rankBenefits,
  telHref,
  titleCase,
  websiteHref,
} from "./logic.js";

const DATA_DIR = "data";
const PAGE_SIZE = 30;
const STORAGE_KEY = "nfz-terminy:woj";
const APPROX_LOCATION_BIT = 64; // położenie przybliżone (środek ulicy) - nfz_terminy/export.py

// Leaflet ładowany dopiero przy pierwszym otwarciu mapy (wersje przypięte)
const LEAFLET = {
  css: [
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css",
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.min.css",
  ],
  js: [
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.min.js",
  ],
};

// Bity pola "flags" (zgodne z nfz_terminy/export.py)
const FILTERS = [
  { key: "podjazd", bit: 1, label: "podjazd dla niepełnosprawnych", badge: "podjazd" },
  { key: "winda", bit: 2, label: "winda", badge: "winda" },
  { key: "lazienka", bit: 4, label: "łazienka dla niepełnosprawnych", badge: "łazienka dla niepełnosprawnych" },
  { key: "parking", bit: 8, label: "parking", badge: "parking" },
  { key: "wozki", bit: 16, label: "wózki dla niepełnosprawnych", badge: "wózki" },
  { key: "dzieci", bit: 32, label: "przyjmuje dzieci", badge: "przyjmuje dzieci" },
];

const $ = (id) => document.getElementById(id);
const ui = {
  form: $("szukaj"),
  province: $("woj"),
  benefit: $("swiadczenie"),
  listbox: $("swiadczenia-lista"),
  city: $("miasto"),
  filters: $("filtry"),
  status: $("status"),
  list: $("lista"),
  more: $("wiecej"),
  updated: $("aktualizacja"),
  map: $("mapa"),
  mapInfo: $("mapa-info"),
  viewButtons: document.querySelectorAll(".view-switch button"),
};

const state = {
  province: "",
  urgent: false,
  benefit: "", // dokładna nazwa świadczenia z danych
  city: "",
  filters: new Set(),
  shown: PAGE_SIZE,
  view: "lista", // "lista" albo "mapa"
};

let manifest = null;
let dataset = null; // { benefits: [{name, folded, count, missing}], rows, generated }
const datasetCache = new Map();
let suggestions = [];
let activeSuggestion = -1;

// --- pomocnicze ---

/** Tworzy element DOM; tekst zawsze przez textContent (dane z zewnątrz nie trafiają do innerHTML). */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (name === "class") node.className = value;
    else node.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function formatDate(iso, withTime = false) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const options = { day: "2-digit", month: "2-digit", year: "numeric" };
  if (withTime) Object.assign(options, { hour: "2-digit", minute: "2-digit" });
  return date.toLocaleString("pl-PL", options);
}

function setStatus(...parts) {
  ui.status.replaceChildren(...parts);
}

function storageGet() {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function storageSet(value) {
  try {
    localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // tryb prywatny lub zablokowane dane strony - zapamiętanie województwa to tylko wygoda
  }
}

// --- stan w adresie URL (żeby można było wysłać komuś link) ---

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  return {
    province: params.get("woj") ?? "",
    urgent: params.get("pilne") === "1",
    benefit: params.get("s") ?? "",
    city: params.get("m") ?? "",
    filters: new Set((params.get("f") ?? "").split(",").filter(Boolean)),
    view: params.get("w") === "mapa" ? "mapa" : "lista",
  };
}

function writeHash() {
  const params = new URLSearchParams();
  if (state.province) params.set("woj", state.province);
  if (state.urgent) params.set("pilne", "1");
  if (state.benefit) params.set("s", state.benefit);
  if (state.city) params.set("m", state.city);
  if (state.filters.size) params.set("f", [...state.filters].join(","));
  if (state.view === "mapa") params.set("w", "mapa");
  const hash = params.toString();
  history.replaceState(null, "", hash ? `#${hash}` : location.pathname + location.search);
}

// --- dane ---

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-cache" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function loadDataset(province, urgent) {
  const key = `${province}-${urgent ? 2 : 1}`;
  if (!datasetCache.has(key)) {
    const promise = fetchJson(`${DATA_DIR}/${key}.json`).then((data) => {
      const rows = decodeRows(data);
      const counts = new Array(data.benefits.length).fill(0);
      for (const row of rows) counts[row.benefit] += 1;
      return {
        rows,
        generated: data.generated,
        benefits: data.benefits.map((name, i) => ({
          name,
          folded: fold(name),
          count: counts[i],
          missing: data.without_forecast[i] ?? 0,
          index: i,
        })),
      };
    });
    datasetCache.set(key, promise);
    promise.catch(() => datasetCache.delete(key));
  }
  return datasetCache.get(key);
}

function currentBenefit() {
  return dataset?.benefits.find((b) => b.name === state.benefit) ?? null;
}

function matchingRows() {
  const benefit = currentBenefit();
  if (!benefit) return [];
  const required = FILTERS.filter((f) => state.filters.has(f.key)).reduce((m, f) => m | f.bit, 0);
  return dataset.rows.filter(
    (row) =>
      row.benefit === benefit.index &&
      (!state.city || row.city === state.city) &&
      (row.flags & required) === required,
  );
}

// --- podpowiedzi świadczeń (combobox) ---

function openSuggestions() {
  if (!dataset) return;
  suggestions = rankBenefits(dataset.benefits, ui.benefit.value, 15);
  activeSuggestion = -1;
  const items = suggestions.map((b, i) =>
    el(
      "li",
      { id: `opcja-${i}`, role: "option", "aria-selected": "false", "data-index": i },
      el("span", {}, b.name),
      el("span", { class: "count" }, `${b.count} ${plural(b.count, ["placówka", "placówki", "placówek"])}`),
    ),
  );
  if (!items.length) {
    items.push(el("li", { class: "empty", role: "option", "aria-disabled": "true" }, "Nic nie pasuje - spróbuj innej nazwy"));
  }
  ui.listbox.replaceChildren(...items);
  ui.listbox.hidden = false;
  ui.benefit.setAttribute("aria-expanded", "true");
  ui.benefit.removeAttribute("aria-activedescendant");
}

function closeSuggestions() {
  ui.listbox.hidden = true;
  ui.benefit.setAttribute("aria-expanded", "false");
  ui.benefit.removeAttribute("aria-activedescendant");
  activeSuggestion = -1;
}

function highlight(index) {
  const items = ui.listbox.querySelectorAll('[role="option"]:not([aria-disabled])');
  if (!items.length) return;
  activeSuggestion = (index + items.length) % items.length;
  items.forEach((item, i) => item.setAttribute("aria-selected", String(i === activeSuggestion)));
  const active = items[activeSuggestion];
  ui.benefit.setAttribute("aria-activedescendant", active.id);
  active.scrollIntoView({ block: "nearest" });
}

function chooseBenefit(name) {
  state.benefit = name;
  state.city = "";
  state.shown = PAGE_SIZE;
  ui.benefit.value = name;
  closeSuggestions();
  renderCities();
  render();
}

// --- mapa ---

let leafletPromise = null;
let map = null;
let clusterLayer = null;

function loadAsset(url, kind) {
  return new Promise((resolve, reject) => {
    const node =
      kind === "css"
        ? el("link", { rel: "stylesheet", href: url, crossorigin: "anonymous" })
        : el("script", { src: url, crossorigin: "anonymous" });
    node.addEventListener("load", resolve);
    node.addEventListener("error", () => reject(new Error(`Nie wczytano ${url}`)));
    document.head.append(node);
  });
}

function loadLeaflet() {
  if (!leafletPromise) {
    leafletPromise = (async () => {
      await Promise.all(LEAFLET.css.map((url) => loadAsset(url, "css")));
      for (const url of LEAFLET.js) await loadAsset(url, "js"); // wtyczka wymaga Leafleta
      return window.L;
    })();
    leafletPromise.catch(() => {
      leafletPromise = null;
    });
  }
  return leafletPromise;
}

function ensureMap(L) {
  if (map) return;
  map = L.map(ui.map, { scrollWheelZoom: true }).setView([52.1, 19.4], 6);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  clusterLayer = L.markerClusterGroup({
    showCoverageOnHover: false,
    maxClusterRadius: 50,
    // klaster pokazuje najkrótszą prognozę wśród swoich placówek
    iconCreateFunction(cluster) {
      const markers = cluster.getAllChildMarkers();
      const best = Math.min(...markers.map((m) => m.options.days ?? Infinity));
      const known = Number.isFinite(best);
      return L.divIcon({
        // tylko liczby - żadnych danych z zewnątrz w HTML
        html: `<span>${known ? `${best} d` : "?"}</span><small>${markers.length}</small>`,
        className: `cluster ${waitClass(known ? best : null)}`,
        iconSize: [46, 46],
      });
    },
  });
  map.addLayer(clusterLayer);
}

function renderPopup(row) {
  const tel = row.phone ? telHref(row.phone) : null;
  return el(
    "div",
    { class: "popup" },
    el("p", { class: "pcus" }, row.pcus),
    el("p", {}, el("strong", {}, row.provider ?? "Placówka")),
    row.place && el("p", { class: "muted" }, row.place),
    el("p", {}, [row.address, row.locality && titleCase(row.locality)].filter(Boolean).join(", ")),
    tel && el("p", {}, el("a", { href: tel }, row.phone)),
    (row.flags & APPROX_LOCATION_BIT) !== 0 &&
      el("p", { class: "muted" }, "Położenie przybliżone (ulica, bez numeru budynku)."),
    el("p", {}, el("a", { href: mapHref(row), target: "_blank", rel: "noopener" }, "Wyznacz trasę")),
  );
}

async function renderMap(rows) {
  let L;
  try {
    L = await loadLeaflet();
  } catch (error) {
    console.error(error);
    ui.mapInfo.hidden = false;
    ui.mapInfo.textContent = "Nie udało się wczytać mapy. Sprawdź połączenie lub skorzystaj z listy.";
    return;
  }
  if (state.view !== "mapa") return; // użytkownik zdążył wrócić do listy
  ensureMap(L);
  map.invalidateSize();
  clusterLayer.clearLayers();

  const located = rows.filter((row) => inPoland(row.lat, row.lon));
  const markers = located.map((row) => {
    const approx = (row.flags & APPROX_LOCATION_BIT) !== 0 ? " approx" : "";
    const marker = L.marker([row.lat, row.lon], {
      days: row.days,
      title: `${row.pcus} - ${row.provider ?? ""}`,
      icon: L.divIcon({
        html: `<span>${row.days != null ? Number(row.days) : "?"}</span>`,
        className: `pin ${waitClass(row.days)}${approx}`,
        iconSize: null,
      }),
    });
    marker.bindPopup(() => renderPopup(row));
    return marker;
  });
  clusterLayer.addLayers(markers);
  if (markers.length) {
    map.fitBounds(L.latLngBounds(located.map((row) => [row.lat, row.lon])), {
      padding: [30, 30],
      maxZoom: 15,
    });
  }

  const missing = rows.length - located.length;
  ui.mapInfo.hidden = false;
  ui.mapInfo.textContent =
    "Liczba na pinezce to prognoza w dniach, a na kółku - najkrótsza prognoza w okolicy." +
    (missing > 0
      ? ` ${missing} ${plural(missing, ["placówki", "placówek", "placówek"])} nie ma na mapie (nieznane położenie) - są na liście.`
      : "");
}

// --- renderowanie ---

function renderProvinces() {
  const options = Object.entries(manifest.provinces).map(([code, name]) =>
    el("option", { value: code }, name),
  );
  ui.province.append(...options);
}

function renderFilters() {
  ui.filters.replaceChildren(
    ...FILTERS.map((f) =>
      el(
        "label",
        {},
        el("input", { type: "checkbox", value: f.key, checked: state.filters.has(f.key) }),
        f.label,
      ),
    ),
  );
}

function renderCities() {
  const benefit = currentBenefit();
  const counts = new Map();
  if (benefit) {
    for (const row of dataset.rows) {
      if (row.benefit === benefit.index) counts.set(row.city, (counts.get(row.city) ?? 0) + 1);
    }
  }
  const cities = [...counts.keys()].sort((a, b) => a.localeCompare(b, "pl"));
  if (state.city && !counts.has(state.city)) state.city = "";
  ui.city.replaceChildren(
    el("option", { value: "" }, "Całe województwo"),
    ...cities.map((city) =>
      el("option", { value: city, selected: city === state.city }, `${titleCase(city)} (${counts.get(city)})`),
    ),
  );
  ui.city.disabled = !benefit;
}

function waitClass(days) {
  if (days == null) return "";
  if (days <= 30) return "fast";
  if (days <= 90) return "medium";
  return "slow";
}

function renderCard(row) {
  const approx =
    row.days != null && !/^\d+ (dni|dzień)$/.test(row.pcus) ? `ok. ${row.days} dni` : null;
  const tel = row.phone ? telHref(row.phone) : null;
  const website = websiteHref(row.website);
  const badges = FILTERS.filter((f) => row.flags & f.bit).map((f) => el("li", {}, f.badge));
  const meta = [
    row.asOf ? `prognoza z ${formatDate(row.asOf)}` : null,
    row.awaiting != null ? `w kolejce: ${row.awaiting} ${plural(row.awaiting, ["osoba", "osoby", "osób"])}` : null,
  ].filter(Boolean);

  return el(
    "li",
    { class: "card" },
    el(
      "div",
      { class: `wait ${waitClass(row.days)}` },
      el("span", { class: "pcus" }, row.pcus),
      approx && el("span", { class: "approx" }, approx),
    ),
    el(
      "div",
      { class: "body" },
      el("h3", {}, row.provider ?? "Placówka"),
      row.place && el("p", { class: "place" }, row.place),
      el(
        "p",
        { class: "address" },
        [row.address, row.locality && titleCase(row.locality)].filter(Boolean).join(", "),
        " · ",
        el("a", { href: mapHref(row), target: "_blank", rel: "noopener" }, "mapa"),
      ),
      el(
        "p",
        { class: "contact" },
        tel ? el("a", { class: "phone", href: tel }, row.phone) : row.phone && el("span", { class: "phone" }, row.phone),
        website && el("a", { href: website, target: "_blank", rel: "noopener nofollow" }, "strona placówki"),
      ),
      badges.length > 0 && el("ul", { class: "badges", "aria-label": "Udogodnienia" }, ...badges),
      meta.length > 0 && el("p", { class: "meta" }, meta.join(" · ")),
    ),
  );
}

function setView(view) {
  state.view = view;
  ui.viewButtons.forEach((button) =>
    button.setAttribute("aria-pressed", String(button.dataset.view === view)),
  );
}

function render() {
  writeHash();
  ui.more.hidden = true;
  ui.map.hidden = true;
  ui.mapInfo.hidden = true;
  ui.list.hidden = false;

  if (!state.province) {
    ui.list.replaceChildren();
    setStatus("Wybierz województwo, a potem specjalistę.");
    return;
  }
  if (!dataset) return; // trwa wczytywanie
  const benefit = currentBenefit();
  if (!benefit) {
    ui.list.replaceChildren();
    setStatus(
      state.benefit
        ? `Nie ma danych dla „${state.benefit}” w tym województwie.`
        : "Wpisz specjalistę i wybierz go z listy podpowiedzi.",
    );
    return;
  }

  const rows = matchingRows();
  if (state.view === "mapa" && rows.length) {
    ui.list.hidden = true;
    ui.list.replaceChildren();
    ui.map.hidden = false;
    renderMap(rows);
  } else {
    const shown = rows.slice(0, state.shown);
    ui.list.replaceChildren(...shown.map(renderCard));
    ui.more.hidden = rows.length <= shown.length;
  }

  if (!rows.length) {
    setStatus("Brak placówek spełniających wybrane warunki. Spróbuj usunąć część filtrów lub wybrać całe województwo.");
    return;
  }
  const parts = [
    el("strong", {}, `${rows.length} ${plural(rows.length, ["placówka", "placówki", "placówek"])}`),
    ` od najkrótszej prognozy · ${state.urgent ? "skierowanie pilne" : "skierowanie stabilne"}`,
  ];
  if (benefit.missing && !state.city && state.filters.size === 0) {
    parts.push(` · ${benefit.missing} bez prognozy (pominięte)`);
  }
  setStatus(...parts);
}

// --- przełączanie województwa / rodzaju skierowania ---

async function reloadDataset() {
  dataset = null;
  ui.benefit.disabled = true;
  ui.city.disabled = true;
  if (!state.province) {
    render();
    return;
  }
  setStatus("Wczytywanie danych województwa...");
  ui.list.replaceChildren();
  const requested = `${state.province}-${state.urgent}`;
  try {
    const loaded = await loadDataset(state.province, state.urgent);
    if (requested !== `${state.province}-${state.urgent}`) return; // użytkownik zdążył zmienić wybór
    dataset = loaded;
  } catch (error) {
    console.error(error);
    setStatus("Nie udało się wczytać danych. Odśwież stronę lub spróbuj później.");
    return;
  }
  ui.benefit.disabled = false;
  renderCities();
  render();
}

// --- zdarzenia ---

function bindEvents() {
  ui.form.addEventListener("submit", (event) => event.preventDefault());

  ui.province.addEventListener("change", () => {
    state.province = ui.province.value;
    state.shown = PAGE_SIZE;
    storageSet(state.province);
    reloadDataset();
  });

  ui.form.addEventListener("change", (event) => {
    const target = event.target;
    if (target.name === "przypadek") {
      state.urgent = target.value === "2";
      state.shown = PAGE_SIZE;
      reloadDataset();
    } else if (target.type === "checkbox") {
      if (target.checked) state.filters.add(target.value);
      else state.filters.delete(target.value);
      state.shown = PAGE_SIZE;
      render();
    }
  });

  ui.city.addEventListener("change", () => {
    state.city = ui.city.value;
    state.shown = PAGE_SIZE;
    render();
  });

  ui.benefit.addEventListener("input", () => {
    if (state.benefit) {
      state.benefit = "";
      renderCities();
      render();
    }
    openSuggestions();
  });
  ui.benefit.addEventListener("focus", () => {
    // wybrane świadczenie zaznaczamy w całości - wpisanie nowej nazwy od razu je zastąpi
    if (state.benefit) ui.benefit.select();
    else openSuggestions();
  });
  ui.benefit.addEventListener("blur", () => setTimeout(closeSuggestions, 120));
  ui.benefit.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (ui.listbox.hidden) openSuggestions();
      highlight(activeSuggestion + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const pick = suggestions[activeSuggestion >= 0 ? activeSuggestion : 0];
      if (pick && !ui.listbox.hidden) chooseBenefit(pick.name);
    } else if (event.key === "Escape") {
      closeSuggestions();
    }
  });
  // mousedown zamiast click - żeby wybór nastąpił przed utratą fokusu przez pole
  ui.listbox.addEventListener("mousedown", (event) => {
    const item = event.target.closest("[data-index]");
    if (!item) return;
    event.preventDefault();
    chooseBenefit(suggestions[Number(item.dataset.index)].name);
  });

  ui.viewButtons.forEach((button) =>
    button.addEventListener("click", () => {
      setView(button.dataset.view);
      render();
    }),
  );

  ui.more.addEventListener("click", () => {
    state.shown += PAGE_SIZE;
    render();
  });
}

// --- start ---

async function init() {
  const fromHash = readHash();
  try {
    manifest = await fetchJson(`${DATA_DIR}/index.json`);
  } catch (error) {
    console.error(error);
    setStatus("Nie udało się wczytać danych. Odśwież stronę lub spróbuj później.");
    return;
  }
  ui.updated.textContent = `Ostatnie pobranie danych: ${formatDate(manifest.generated, true)}.`;
  renderProvinces();

  const available = (code) => code in manifest.provinces;
  state.province = available(fromHash.province) ? fromHash.province : available(storageGet()) ? storageGet() : "";
  state.urgent = fromHash.urgent;
  state.benefit = fromHash.benefit;
  state.city = fromHash.city;
  state.filters = new Set([...fromHash.filters].filter((key) => FILTERS.some((f) => f.key === key)));
  setView(fromHash.view);

  ui.province.value = state.province;
  document.getElementById(state.urgent ? "przypadek-2" : "przypadek-1").checked = true;
  ui.benefit.value = state.benefit;
  renderFilters();
  bindEvents();
  await reloadDataset();
}

init();
