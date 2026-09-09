/* Аналитический налоговый портал — клиентская часть.
   Состояние живёт в адресной строке (#tax=tn&year=2023&region=77&tab=benefits),
   поэтому любой экран можно сохранить в закладки и переслать коллеге. */

const API = {
  async get(path, params = {}) {
    const url = new URL(path, location.origin);
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
    });
    const response = await fetch(url);
    if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
    return response.json();
  },
  url(path, params = {}) {
    const url = new URL(path, location.origin);
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
    });
    return url.toString();
  },
};

const PAYERS = { fl: "Физические лица", ip: "Индивидуальные предприниматели", ul: "Юридические лица", all: "Без разделения" };
const PAYER_SHORT = { fl: "ФЛ", ip: "ИП", ul: "ЮЛ", all: "прочие" };

const state = {
  tax: "tn", year: null, region: null, tab: "overview",
  ratePayer: "", benefitPayer: "fl",
  indicators: [], anPayer: "total", anChart: "dynamics", anYearFrom: null, anYearTo: null,
  cloudPayer: "fl", cloudScope: "rf", cloudYears: "current", cloudMode: "freq",
  cloudRegions: [], cloudStopwords: "",
};

let meta = null;
let regions = [];
let indicatorCache = {};
const charts = {};

/* ----------------------------------------------------------------- утилиты */
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const nf0 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });

function num(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const abs = Math.abs(value);
  return (abs >= 1000 || digits === 0 ? nf0 : nf).format(value);
}
function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}
function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.add("hidden"), 2600);
}
function empty(message) {
  return `<div class="empty">${esc(message)}</div>`;
}

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  for (const key of ["tax", "region", "tab", "benefitPayer", "ratePayer", "cloudPayer", "anPayer", "anChart"]) {
    if (params.get(key)) state[key] = params.get(key);
  }
  if (params.get("year")) state.year = Number(params.get("year"));
  if (params.get("indicators")) state.indicators = params.get("indicators").split(",").map(Number);
}
function writeHash() {
  const params = new URLSearchParams();
  params.set("tax", state.tax);
  if (state.year) params.set("year", state.year);
  if (state.region) params.set("region", state.region);
  params.set("tab", state.tab);
  if (state.indicators.length) params.set("indicators", state.indicators.join(","));
  history.replaceState(null, "", `#${params.toString()}`);
}

/* --------------------------------------------------------------- запуск */
async function init() {
  readHash();
  meta = await API.get("/api/meta");
  const counts = meta.counts;
  $("#db-info").textContent =
    `${nf0.format(counts.facts)} значений форм · ${nf0.format(counts.rates)} ставок · ` +
    `${nf0.format(counts.benefits)} льгот · нормализация: ${meta.lemmatizer}`;

  const taxSelect = $("#tax-select");
  taxSelect.innerHTML = meta.taxes.map((tax) =>
    `<option value="${tax.code}">${esc(tax.name)}</option>`).join("");
  if (!meta.taxes.some((tax) => tax.code === state.tax) && meta.taxes.length) state.tax = meta.taxes[0].code;
  taxSelect.value = state.tax;

  await reloadYears();
  await reloadRegions();
  bindEvents();
  setTab(state.tab, true);
}

async function reloadYears() {
  const years = await API.get("/api/years", { tax: state.tax });
  const select = $("#year-select");
  select.innerHTML = years.map((year) => `<option value="${year}">${year}</option>`).join("");
  if (!years.includes(state.year)) state.year = years[years.length - 1] ?? null;
  select.value = state.year ?? "";
  const options = years.map((year) => `<option value="${year}">${year}</option>`).join("");
  $("#an-year-from").innerHTML = options;
  $("#an-year-to").innerHTML = options;
  state.anYearFrom = state.anYearFrom ?? years[0];
  state.anYearTo = state.anYearTo ?? years[years.length - 1];
  $("#an-year-from").value = state.anYearFrom;
  $("#an-year-to").value = state.anYearTo;
}

async function reloadRegions() {
  regions = await API.get("/api/regions", { tax: state.tax, year: state.year });
  const districts = [...new Set(regions.map((r) => r.federal_district).filter(Boolean))].sort();
  $("#district-filter").innerHTML =
    `<option value="">Все округа</option>` +
    districts.map((district) => `<option value="${esc(district)}">${esc(district)}</option>`).join("");
  if (!state.region) state.region = regions.find((r) => r.kind === "subject" && r.has_stats)?.code || regions[0]?.code;
  renderRegions();
}

function renderRegions() {
  const query = ($("#region-search").value || "").toLowerCase().trim();
  const district = $("#district-filter").value;
  const list = regions.filter((region) => {
    if (district && region.federal_district !== district) return false;
    if (!query) return true;
    return region.name.toLowerCase().includes(query) || region.code.includes(query);
  });
  $("#region-list").innerHTML = list.map((region) => {
    const hasData = region.has_stats || region.has_rates || region.has_benefits;
    const marks = [region.has_stats ? "Ф" : "", region.has_rates ? "С" : "", region.has_benefits ? "Л" : ""]
      .filter(Boolean).join("");
    return `<li data-code="${region.code}" class="${region.code === state.region ? "active" : ""} ${hasData ? "" : "no-data"}"
              title="Ф — показатели формы, С — ставки, Л — льготы">
        <span>${esc(region.short_name || region.name)}</span>
        <span class="code">${esc(marks)} ${region.code}</span>
      </li>`;
  }).join("");
  $("#region-count").textContent = `территорий: ${list.length} из ${regions.length}`;
}

function bindEvents() {
  $("#tax-select").addEventListener("change", async (event) => {
    state.tax = event.target.value;
    indicatorCache = {};
    await reloadYears();
    await reloadRegions();
    refresh();
  });
  $("#year-select").addEventListener("change", async (event) => {
    state.year = Number(event.target.value);
    await reloadRegions();
    refresh();
  });
  $("#region-search").addEventListener("input", renderRegions);
  $("#district-filter").addEventListener("change", renderRegions);
  $("#region-list").addEventListener("click", (event) => {
    const item = event.target.closest("li[data-code]");
    if (!item) return;
    state.region = item.dataset.code;
    renderRegions();
    refresh();
  });
  $("#tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (button) setTab(button.dataset.tab);
  });
  $("#btn-help").addEventListener("click", () => $("#help-dialog").showModal());

  $("#rate-search").addEventListener("input", debounce(renderRates, 300));
  $("#benefit-search").addEventListener("input", debounce(renderBenefits, 300));
  $("#stats-only-filled").addEventListener("change", renderStats);
  $("#stats-compare").addEventListener("change", renderStats);
  $("#stats-to-chart").addEventListener("click", () => {
    const checked = $$("#stats-body input[type=checkbox][data-indicator]:checked").map((input) => Number(input.dataset.indicator));
    if (!checked.length) return toast("Отметьте показатели галочками");
    state.indicators = checked;
    setTab("analytics");
  });
  ["an-payer", "an-chart", "an-year-from", "an-year-to"].forEach((id) => {
    $(`#${id}`).addEventListener("change", () => {
      state.anPayer = $("#an-payer").value;
      state.anChart = $("#an-chart").value;
      state.anYearFrom = Number($("#an-year-from").value);
      state.anYearTo = Number($("#an-year-to").value);
      renderAnalyticsChart();
    });
  });
  $("#an-indicator-search").addEventListener("input", debounce(() => renderIndicatorPicker(), 250));
  ["cloud-scope", "cloud-years", "cloud-mode", "cloud-bigrams"].forEach((id) => {
    $(`#${id}`).addEventListener("change", () => {
      state.cloudScope = $("#cloud-scope").value;
      state.cloudYears = $("#cloud-years").value;
      state.cloudMode = $("#cloud-mode").value;
      $("#cloud-regions").classList.toggle("hidden", state.cloudScope !== "custom");
      renderCloud();
    });
  });
  $("#cloud-stopwords").addEventListener("input", debounce(() => {
    state.cloudStopwords = $("#cloud-stopwords").value;
    renderCloud();
  }, 350));
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-export]");
    if (button) exportCsv(button.dataset.export);
  });
  window.addEventListener("resize", debounce(() => {
    Object.values(charts).forEach((chart) => chart && chart.resize());
  }, 200));
}

function debounce(fn, delay) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}

function setTab(tab, silent = false) {
  state.tab = tab;
  $$("#tabs button").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  $$(".panel").forEach((panel) => panel.classList.add("hidden"));
  $(`#panel-${tab}`).classList.remove("hidden");
  if (!silent) writeHash();
  refresh();
}

async function refresh() {
  writeHash();
  if (!state.region || !state.year) return;
  await renderProfileHead();
  const renderers = {
    overview: renderOverview, rates: renderRates, benefits: renderBenefits,
    stats: renderStats, analytics: renderAnalytics, cloud: renderCloud,
  };
  await renderers[state.tab]();
}

/* ------------------------------------------------------------- шапка профиля */
let profileCache = null;
async function renderProfileHead() {
  const profile = await API.get("/api/profile", { tax: state.tax, year: state.year, region: state.region });
  profileCache = profile;
  $("#profile-head").classList.remove("hidden");
  $("#region-title").textContent = profile.region.name;
  const bits = [
    profile.region.federal_district,
    `код ${profile.region.code}`,
    `${meta.taxes.find((t) => t.code === state.tax)?.name ?? state.tax}, ${state.year} год`,
  ].filter(Boolean);
  $("#region-meta").textContent = bits.join(" · ");
  const counts = profile.counts || {};
  const byPayer = profile.benefits_by_payer || {};
  $("#profile-counters").innerHTML = `
    <div><b>${nf0.format(counts.rates || 0)}</b><span class="muted">ставок</span></div>
    <div><b>${nf0.format(counts.benefits || 0)}</b><span class="muted">льгот</span></div>
    <div><b>${nf0.format(byPayer.fl || 0)}</b><span class="muted">льгот ФЛ</span></div>
    <div><b>${nf0.format(byPayer.ip || 0)}</b><span class="muted">льгот ИП</span></div>
    <div><b>${nf0.format(byPayer.ul || 0)}</b><span class="muted">льгот ЮЛ</span></div>`;
}

/* -------------------------------------------------------------------- обзор */
async function renderOverview() {
  const profile = profileCache;
  const cards = profile.key_indicators.map((row) => {
    const total = row.total || {};
    const ratio = total.ratio_to_avg;
    const badge = ratio == null ? "" :
      `<span class="badge ${ratio >= 1 ? "up" : "down"}">${ratio >= 1 ? "↑" : "↓"} ${num(ratio * 100, 0)}% от среднего</span>`;
    const rank = total.rank ? `<span class="badge rank">${total.rank} место из ${total.subjects}</span>` : "";
    return `<div class="card">
      <div class="label">${esc(row.name)}</div>
      <div class="value">${num(total.value)}<span class="unit">${esc(row.unit || "")}</span></div>
      <div class="split"><span>ЮЛ: ${num((row.ul || {}).value)}</span><span>ФЛ: ${num((row.fl || {}).value)}</span></div>
      <div>${badge} ${rank}</div>
    </div>`;
  }).join("");

  $("#panel-overview").innerHTML = `
    <h2>Ключевые показатели за ${state.year} год</h2>
    <div class="cards">${cards || empty("За этот год нет данных статотчётности. Загрузите формы командой load-forms.")}</div>
    <h2 style="margin-top:18px">Динамика</h2>
    <div id="overview-chart" class="chart" style="height:320px"></div>`;

  const ids = profile.key_indicators.slice(0, 3).map((row) => row.indicator_id);
  if (!ids.length) return;
  const data = await API.get("/api/series", {
    tax: state.tax, region: state.region, indicators: ids.join(","), payer: "total",
  });
  const series = Object.values(data.series).map((item) => ({
    name: shorten(item.name), type: "line", smooth: true, symbolSize: 6,
    connectNulls: true,
    yAxisIndex: item.unit && item.unit.includes("руб") ? 1 : 0,
    data: item.points.map((point) => point.value),
  }));
  drawChart("overview-chart", {
    tooltip: { trigger: "axis" },
    legend: { bottom: 0, type: "scroll" },
    grid: { left: 70, right: 70, top: 20, bottom: 50 },
    xAxis: { type: "category", data: data.years },
    yAxis: [
      { type: "value", name: "единиц", axisLabel: { formatter: (v) => nf0.format(v) } },
      { type: "value", name: "тыс. руб.", axisLabel: { formatter: (v) => nf0.format(v) } },
    ],
    series,
  });
}

function shorten(text, limit = 58) {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/* -------------------------------------------------------------------- ставки */
async function renderRates() {
  renderPayerChips("#rate-payer-chips", state.ratePayer, (payer) => {
    state.ratePayer = payer;
    renderRates();
  }, { "": "Все" });
  const data = await API.get("/api/rates", {
    tax: state.tax, year: state.year, region: state.region,
    payer: state.ratePayer, search: $("#rate-search").value, limit: 1000,
  });
  if (!data.items.length) {
    $("#rates-body").innerHTML = empty("Ставки не найдены. Проверьте год или загрузите набор ставок и льгот.");
    return;
  }
  const rows = data.items.map((item) => `
    <tr>
      <td>${esc(item.object_name || "—")}</td>
      <td class="num">${item.rate_value == null ? esc(item.rate_text || "—") : num(item.rate_value, 2)}</td>
      <td>${esc(item.rate_unit || "")}</td>
      <td><span class="pill">${esc(PAYER_SHORT[item.payer] || item.payer)}</span> ${esc(item.payer_text || "")}</td>
      <td>${esc(item.mo_name || "")}</td>
      <td>${esc(item.condition || "")}</td>
      <td class="muted">${esc([item.npa_name, item.npa_number, item.npa_date].filter(Boolean).join(", "))}</td>
    </tr>`).join("");
  $("#rates-body").innerHTML = `
    <div class="muted" style="margin-bottom:6px">Показано ${data.items.length} из ${data.total} записей</div>
    <div class="table-wrap"><table>
      <thead><tr><th>Объект налогообложения</th><th class="num">Ставка</th><th>Ед.</th>
        <th>Плательщик</th><th>Муниципалитет</th><th>Условие</th><th>НПА</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

function renderPayerChips(selector, active, onSelect, extra = {}) {
  const options = { ...extra, fl: "Физические лица", ip: "Индивидуальные предприниматели", ul: "Юридические лица" };
  const counts = (profileCache && profileCache.benefits_by_payer) || {};
  const showCounts = selector.includes("benefit") || selector.includes("cloud");
  const box = $(selector);
  box.innerHTML = Object.entries(options).map(([key, label]) => {
    // ФНС помечает льготу всеми категориями, к которым она относится,
    // поэтому сумма по категориям больше общего числа — берём готовый total
    const count = !showCounts ? null : (key === "" ? counts.total : counts[key]);
    return `<button class="chip ${key === active ? "active" : ""}" data-payer="${key}">
        ${esc(label)}${count != null ? `<span class="count">${count}</span>` : ""}</button>`;
  }).join("");
  box.onclick = (event) => {
    const chip = event.target.closest(".chip");
    if (chip) onSelect(chip.dataset.payer);
  };
}

/* -------------------------------------------------------------------- льготы */
async function renderBenefits() {
  renderPayerChips("#benefit-payer-chips", state.benefitPayer, (payer) => {
    state.benefitPayer = payer;
    renderBenefits();
  }, { "": "Все категории", all: "Без разделения" });

  const data = await API.get("/api/benefits", {
    tax: state.tax, year: state.year, region: state.region,
    payer: state.benefitPayer, search: $("#benefit-search").value, limit: 1000,
  });
  if (!data.items.length) {
    $("#benefits-body").innerHTML = empty("Льготы не найдены для выбранной категории и года.");
    return;
  }
  const cards = data.items.map((item) => {
    const period = item.year_to ? `${item.year_from}–${item.year_to}` : `с ${item.year_from ?? "?"}`;
    return `<div class="benefit" data-payer="${esc(item.payer)}">
      <div class="category">${esc(item.category || "категория не указана")}</div>
      <div class="meta">
        <span class="pill">${esc(PAYERS[item.payer] || item.payer)}</span>
        ${item.kind ? `<span>${esc(item.kind)}</span>` : ""}
        ${item.size_text ? `<span>размер: <b>${esc(item.size_text)}</b></span>` : ""}
        <span>период: ${esc(period)}</span>
        ${item.mo_name ? `<span>${esc(item.mo_name)}</span>` : ""}
      </div>
      ${item.condition ? `<div class="muted" style="margin-top:5px">Условие: ${esc(item.condition)}</div>` : ""}
      <div class="npa">${esc([item.basis, item.npa_name, item.npa_number, item.npa_date, item.npa_authority]
        .filter(Boolean).join(" · "))}</div>
    </div>`;
  }).join("");
  $("#benefits-body").innerHTML = `
    <div class="muted" style="margin-bottom:8px">Показано ${data.items.length} из ${data.total} льгот</div>${cards}`;
}

/* ------------------------------------------------------- показатели формы */
async function renderStats() {
  const rows = await API.get("/api/form", { tax: state.tax, year: state.year, region: state.region });
  if (!rows.length) {
    $("#stats-body").innerHTML = empty("Нет данных формы за этот год для выбранной территории.");
    return;
  }
  const compare = $("#stats-compare").checked;
  const onlyFilled = $("#stats-only-filled").checked;
  let currentSection = null;
  const body = rows.filter((row) => !onlyFilled || (row.total || {}).value != null).map((row) => {
    let head = "";
    if (row.section !== currentSection) {
      currentSection = row.section;
      head = `<tr class="section-row"><td colspan="${compare ? 8 : 5}">Раздел ${esc(row.section)}</td></tr>`;
    }
    const cell = (payer) => `<td class="num">${num((row[payer] || {}).value)}</td>`;
    const total = row.total || {};
    const extra = compare ? `
      <td class="num">${num(total.avg)}</td>
      <td class="num">${total.ratio_to_avg == null ? "—" : `${num(total.ratio_to_avg * 100, 0)}%`}</td>
      <td class="num">${total.rank ?? "—"}</td>` : "";
    return `${head}<tr class="level-${row.level}">
      <td><label class="inline"><input type="checkbox" data-indicator="${row.indicator_id}"
        ${state.indicators.includes(row.indicator_id) ? "checked" : ""}> ${esc(row.name)}</label></td>
      ${cell("ul")}${cell("fl")}${cell("total")}<td class="muted">${esc(row.unit || "")}</td>${extra}</tr>`;
  }).join("");
  $("#stats-body").innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Показатель</th><th class="num">Юрлица</th><th class="num">Физлица</th>
        <th class="num">Всего</th><th>Ед. изм.</th>
        ${compare ? '<th class="num">Среднее по субъектам</th><th class="num">К среднему</th><th class="num">Ранг</th>' : ""}
      </tr></thead><tbody>${body}</tbody></table></div>`;
}

/* ----------------------------------------------------------------- аналитика */
async function renderAnalytics() {
  await renderIndicatorPicker();
  await renderAnalyticsChart();
}

async function loadIndicators() {
  const key = `${state.tax}:${state.year}`;
  if (!indicatorCache[key]) {
    indicatorCache[key] = await API.get("/api/indicators", { tax: state.tax, year: state.year });
  }
  return indicatorCache[key];
}

async function renderIndicatorPicker() {
  const items = await loadIndicators();
  const query = ($("#an-indicator-search").value || "").toLowerCase().trim();
  if (!state.indicators.length && items.length) state.indicators = [items[0].id];
  let section = null;
  const html = items.filter((item) => !query || item.name.toLowerCase().includes(query)).map((item) => {
    let head = "";
    if (item.section !== section) {
      section = item.section;
      head = `<div class="sec">Раздел ${esc(section)}</div>`;
    }
    return `${head}<label class="lvl-${item.level}"><input type="checkbox" value="${item.id}"
        ${state.indicators.includes(item.id) ? "checked" : ""}>
      <span>${esc(item.name)} <span class="muted">${esc(item.unit || "")}</span></span></label>`;
  }).join("");
  const box = $("#an-indicators");
  box.innerHTML = html || empty("Показатели не найдены");
  box.onchange = (event) => {
    const input = event.target;
    if (input.type !== "checkbox") return;
    const id = Number(input.value);
    state.indicators = input.checked
      ? [...new Set([...state.indicators, id])]
      : state.indicators.filter((value) => value !== id);
    writeHash();
    renderAnalyticsChart();
  };
}

async function renderAnalyticsChart() {
  if (!state.indicators.length) {
    $("#an-chart-box").innerHTML = "";
    $("#an-table").innerHTML = empty("Отметьте показатели слева");
    return;
  }
  const kind = state.anChart;
  if (kind === "ranking") return renderRankingChart();
  if (kind === "structure") return renderStructureChart();

  const data = await API.get("/api/series", {
    tax: state.tax, region: state.region, indicators: state.indicators.join(","),
    payer: state.anPayer, year_from: state.anYearFrom, year_to: state.anYearTo,
  });
  const items = Object.values(data.series);
  const series = [];
  items.forEach((item) => {
    const moneyAxis = item.unit && item.unit.includes("руб") ? 1 : 0;
    series.push({
      name: shorten(item.name), type: "line", smooth: true, symbolSize: 6, yAxisIndex: moneyAxis,
      data: item.points.map((point) => point.value),
    });
    if (kind === "vs-average") {
      series.push({
        name: `${shorten(item.name)} — среднее по субъектам`, type: "line", yAxisIndex: moneyAxis,
        lineStyle: { type: "dashed" }, symbol: "none",
        data: item.points.map((point) => point.avg ?? null),
      });
    }
  });
  drawChart("an-chart-box", {
    tooltip: { trigger: "axis" },
    legend: { bottom: 0, type: "scroll" },
    grid: { left: 75, right: 75, top: 24, bottom: 60 },
    xAxis: { type: "category", data: data.years },
    yAxis: [
      { type: "value", name: "единиц", axisLabel: { formatter: (v) => nf0.format(v) } },
      { type: "value", name: "тыс. руб.", axisLabel: { formatter: (v) => nf0.format(v) } },
    ],
    series,
  });

  const header = ["Показатель", ...data.years];
  const rows = items.flatMap((item) => {
    const own = [`<tr><td>${esc(item.name)}</td>${item.points.map((p) => `<td class="num">${num(p.value)}</td>`).join("")}</tr>`];
    if (kind === "vs-average") {
      own.push(`<tr><td class="muted">↳ среднее по субъектам</td>${item.points
        .map((p) => `<td class="num muted">${num(p.avg)}</td>`).join("")}</tr>`);
      own.push(`<tr><td class="muted">↳ место среди субъектов</td>${item.points
        .map((p) => `<td class="num muted">${p.rank ?? "—"}</td>`).join("")}</tr>`);
    }
    return own;
  }).join("");
  $("#an-table").innerHTML = `<div class="table-wrap" style="margin-top:12px"><table>
      <thead><tr>${header.map((cell, index) => `<th class="${index ? "num" : ""}">${cell}</th>`).join("")}</tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

async function renderRankingChart() {
  const indicatorId = state.indicators[0];
  const data = await API.get("/api/ranking", {
    tax: state.tax, year: state.year, indicator: indicatorId, payer: state.anPayer, limit: 30,
  });
  const items = [...data].reverse();
  drawChart("an-chart-box", {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 170, right: 40, top: 20, bottom: 40 },
    xAxis: { type: "value", axisLabel: { formatter: (v) => nf0.format(v) } },
    yAxis: { type: "category", data: items.map((item) => item.short_name || item.region_name) },
    series: [{
      type: "bar",
      data: items.map((item) => ({
        value: item.value,
        itemStyle: { color: item.region_code === state.region ? "#1f5fa9" : "#9fb6d0" },
      })),
    }],
  });
  $("#an-table").innerHTML = `<div class="muted" style="margin-top:10px">
    Топ-30 субъектов за ${state.year} год. Текущая территория выделена цветом.</div>`;
}

async function renderStructureChart() {
  const rows = await API.get("/api/form", { tax: state.tax, year: state.year, region: state.region });
  const selected = rows.filter((row) => state.indicators.includes(row.indicator_id));
  const data = selected.map((row) => ({ name: shorten(row.name, 40), value: (row[state.anPayer] || {}).value || 0 }));
  drawChart("an-chart-box", {
    tooltip: { trigger: "item", formatter: (p) => `${p.name}<br>${num(p.value)} (${p.percent}%)` },
    legend: { bottom: 0, type: "scroll" },
    series: [{
      type: "pie", radius: ["38%", "68%"], center: ["50%", "44%"],
      label: { formatter: "{b}: {d}%" }, data,
    }],
  });
  $("#an-table").innerHTML = `<div class="muted" style="margin-top:10px">
    Доли отмеченных показателей за ${state.year} год, категория: ${esc(state.anPayer)}.</div>`;
}

/* ---------------------------------------------------------------- облака слов */
async function renderCloud() {
  renderPayerChips("#cloud-payer-chips", state.cloudPayer, (payer) => {
    state.cloudPayer = payer || "fl";
    renderCloud();
  });
  if (state.cloudScope === "custom" && !$("#cloud-regions").dataset.filled) {
    $("#cloud-regions").innerHTML = regions.filter((region) => region.kind === "subject")
      .map((region) => `<label><input type="checkbox" value="${region.code}"> ${esc(region.short_name || region.name)}</label>`)
      .join("");
    $("#cloud-regions").dataset.filled = "1";
    $("#cloud-regions").onchange = () => {
      state.cloudRegions = $$("#cloud-regions input:checked").map((input) => input.value);
      renderCloud();
    };
  }
  const params = {
    tax: state.tax, payer: state.cloudPayer, limit: 200,
    mode: state.cloudMode, bigrams: $("#cloud-bigrams").checked,
  };
  if (state.cloudYears === "current") params.years = state.year;
  if (state.cloudScope === "current") params.regions = state.region;
  if (state.cloudScope === "custom") params.regions = state.cloudRegions.join(",");
  const data = await API.get("/api/wordcloud", params);

  const extra = state.cloudStopwords.split(",").map((word) => word.trim().toLowerCase()).filter(Boolean);
  const items = data.items.filter((item) => !extra.some((word) => item.text.toLowerCase().includes(word)));
  if (!items.length) {
    $("#cloud-box").innerHTML = empty("Нет данных для облака. Постройте словарь командой build-terms.");
    $("#cloud-table").innerHTML = "";
    return;
  }
  const palette = ["#1f5fa9", "#2f6fb5", "#4c7a2f", "#8a5b1f", "#217a4b", "#5b4a9e", "#b3452f"];
  drawChart("cloud-box", {
    tooltip: { show: true, formatter: (p) => `${p.name}<br>вхождений: ${nf0.format(p.value)}` },
    series: [{
      type: "wordCloud", shape: "circle", gridSize: 6, sizeRange: [13, 58],
      rotationRange: [0, 0], width: "96%", height: "94%", drawOutOfBound: false,
      textStyle: { color: () => palette[Math.floor(Math.random() * palette.length)] },
      data: items.map((item) => ({ name: item.text, value: Math.round(item.score || item.freq), freq: item.freq })),
    }],
  }, (chart) => {
    chart.off("click");
    chart.on("click", (params) => {
      $("#benefit-search").value = params.name;
      state.benefitPayer = state.cloudPayer;
      setTab("benefits");
    });
  });

  const scope = state.cloudScope === "rf" ? "все субъекты"
    : state.cloudScope === "current" ? (profileCache?.region?.name || state.region)
    : `${state.cloudRegions.length} территорий`;
  $("#cloud-table").innerHTML = `
    <div class="muted" style="margin-bottom:8px">
      ${esc(PAYERS[state.cloudPayer])} · ${esc(scope)} ·
      ${state.cloudYears === "current" ? state.year : "все годы"} ·
      всего терминов: ${data.total_terms}. Клик по слову — переход к льготам с этим словом.</div>
    <div class="table-wrap" style="max-height:440px"><table>
      <thead><tr><th>Термин</th><th class="num">Вхождений</th><th class="num">Льгот</th></tr></thead>
      <tbody>${items.slice(0, 60).map((item) => `<tr><td>${esc(item.text)}</td>
        <td class="num">${nf0.format(item.freq)}</td><td class="num">${nf0.format(item.docs)}</td></tr>`).join("")}</tbody>
    </table></div>`;
}

/* ------------------------------------------------------------------- графики */
function drawChart(id, option, after) {
  const box = document.getElementById(id);
  if (!box) return;
  box.innerHTML = "";
  if (charts[id]) charts[id].dispose();
  const chart = echarts.init(box, null, { renderer: "canvas" });
  chart.setOption(option);
  charts[id] = chart;
  if (after) after(chart);
}

/* -------------------------------------------------------------------- экспорт */
function exportCsv(kind) {
  const params = { tax: state.tax, year: state.year, region: state.region };
  if (kind === "rates") params.payer = state.ratePayer;
  if (kind === "benefits") { params.payer = state.benefitPayer; params.search = $("#benefit-search").value; }
  if (kind === "series") { params.indicators = state.indicators.join(","); params.payer = state.anPayer; }
  if (kind === "wordcloud") {
    params.payer = state.cloudPayer;
    params.indicators = state.cloudYears === "current" ? state.year : "";
    params.region = state.cloudScope === "current" ? state.region
      : state.cloudScope === "custom" ? state.cloudRegions.join(",") : "";
  }
  window.location = API.url(`/api/export/${kind}.csv`, params);
}

init().catch((error) => {
  document.body.insertAdjacentHTML("afterbegin",
    `<div class="empty">Ошибка запуска портала: ${esc(error.message)}</div>`);
});
