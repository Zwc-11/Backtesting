"use strict";

/* xasset lab: paper desk, strategies, backtests, relationships and data health.
   Plain DOM, no dependencies; every request goes to this host's read-only API. */

const $ = (id) => document.getElementById(id);
const MINUS = "−";
const DASH = "–";
const VIEWS = ["desk", "strategies", "backtests", "relationships", "data"];
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
};
const state = {
  view: null,
  desks: [],
  book: store.get("xasset.book"),
  boardKey: null,
  picked: null,
  results: { key: null, at: 0 },
  catalog: null,
  runs: null,
  run: null,
  scenario: "base",
  strategyFilter: null,
  mapRequest: 0,
};

/* ---------- formatting -------------------------------------------------- */
const formats = new Map();
function nf(digits) {
  if (!formats.has(digits)) {
    formats.set(digits, new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }));
  }
  return formats.get(digits);
}
const finite = (v) => v !== null && v !== undefined && Number.isFinite(Number(v));
function num(v, digits = 2) { return finite(v) ? nf(digits).format(Number(v)).replace("-", MINUS) : DASH; }
function signed(v, digits = 2) {
  if (!finite(v)) return DASH;
  const n = Number(v);
  const text = nf(digits).format(Math.abs(n));
  return n > 0 ? "+" + text : n < 0 ? MINUS + text : text;
}
function pct(v, digits = 2) { return finite(v) ? signed(Number(v) * 100, digits) + "%" : DASH; }
function plainPct(v, digits = 1) { return finite(v) ? num(Number(v) * 100, digits) + "%" : DASH; }
function price(v) {
  if (!finite(v)) return DASH;
  const n = Math.abs(Number(v));
  const digits = n >= 1000 ? 2 : n >= 1 ? 4 : n >= 0.01 ? 6 : 8;
  return num(v, digits);
}
function qty(v) {
  if (!finite(v)) return DASH;
  const n = Math.abs(Number(v));
  return num(v, n >= 100 ? 0 : n >= 1 ? 3 : 5);
}
function tone(v) { return !finite(v) || Number(v) === 0 ? "" : Number(v) > 0 ? "pos" : "neg"; }
const utc = { timeZone: "UTC" };
function clockTime(iso) {
  if (!iso) return DASH;
  return new Date(iso).toLocaleTimeString("en-GB", { ...utc, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function shortTime(iso) {
  if (!iso) return DASH;
  return new Date(iso).toLocaleTimeString("en-GB", { ...utc, hour: "2-digit", minute: "2-digit" });
}
function dayTime(iso) {
  if (!iso) return DASH;
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { ...utc, day: "numeric", month: "short" }) + " " + shortTime(iso);
}
function day(iso) {
  if (!iso) return DASH;
  return new Date(iso).toLocaleDateString("en-GB", { ...utc, day: "numeric", month: "short", year: "numeric" });
}
function age(seconds) {
  if (!finite(seconds)) return DASH;
  const s = Math.max(0, Number(seconds));
  if (s < 10) return num(s, 1) + " s";
  if (s < 90) return Math.round(s) + " s";
  if (s < 5400) return Math.round(s / 60) + " min";
  if (s < 172800) return Math.round(s / 3600) + " h";
  return Math.round(s / 86400) + " days";
}
function short(symbol) { return String(symbol).replace(/-PERP-HL$/, "").replace(/USDT$/, "").replace(/_SIP$/, ""); }
function words(value) {
  const text = String(value || "").replaceAll("_", " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/* ---------- DOM helpers --------------------------------------------------- */
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function svg(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  return node;
}
function head(table, columns) {
  const row = el("tr");
  for (const column of columns) {
    const [label, numeric] = Array.isArray(column) ? column : [column, false];
    row.append(el("th", { class: numeric ? "num" : null, scope: "col" }, label));
  }
  table.replaceChildren(el("thead", null, row), el("tbody"));
  return table.tBodies[0];
}
function cells(values) {
  const row = el("tr");
  for (const value of values) {
    if (value && value.cell) row.append(el("td", { class: value.class }, ...(value.children || [value.text])));
    else row.append(el("td", null, value instanceof Node ? value : String(value ?? DASH)));
  }
  return row;
}
const n = (text, extra) => ({ cell: true, class: "num" + (extra ? " " + extra : ""), text });
function emptyRow(body, columns, message) {
  body.append(el("tr", null, el("td", { class: "empty", colspan: String(columns) }, message)));
}
function notice(message) { $("notice").textContent = message || ""; $("notice").hidden = !message; }

/* ---------- API --------------------------------------------------------- */
class AuthRequired extends Error {}
async function api(path) {
  const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (response.status === 401) {
    if (!$("login").open) $("login").showModal();
    throw new AuthRequired("Enter the access token to continue.");
  }
  if (!response.ok) throw new Error(`The lab API answered ${response.status} for ${path}.`);
  return response.json();
}
function report(error) {
  if (error instanceof AuthRequired) return;
  notice(error && error.message ? error.message : "The lab API is unavailable. Is xasset-app serve running?");
}

/* ---------- routing ------------------------------------------------------- */
function route() {
  const requested = location.hash.replace("#", "");
  const view = VIEWS.includes(requested) ? requested : "desk";
  state.view = view;
  for (const name of VIEWS) $("view-" + name).hidden = name !== view;
  for (const link of document.querySelectorAll(".bar nav a")) {
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = (view === "desk" ? "Paper desk" : words(view)) + " \u2013 xasset lab";
  load(view).catch(report);
}
async function load(view) {
  if (view === "desk" || view === "data") await refreshDesk();
  if (view === "strategies") await loadCatalog();
  if (view === "backtests") await loadRuns();
  if (view === "relationships") await loadMap();
  if (view === "data") await loadHealth();
}

/* ---------- paper desk ---------------------------------------------------- */
function currentDesk() {
  const desks = state.desks || [];
  return desks.find((d) => d.running) || desks.slice().sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))[0] || null;
}
async function refreshDesk() {
  const data = await api("/api/paper");
  state.desks = data.desks || [];
  renderStatus();
  if (state.view === "desk") renderDesk();
  if (state.view === "data") renderDeskData();
  notice("");
}
function renderStatus() {
  const desk = currentDesk();
  const lamp = $("desk-lamp");
  lamp.className = "lamp";
  let label = "Desk not started";
  if (desk && desk.running) {
    const warming = (desk.books || []).some((b) => b.status !== "live");
    lamp.classList.add(warming ? "wait" : "on");
    label = warming ? "Desk warming up" : "Desk live";
  } else if (desk) {
    lamp.classList.add("off");
    label = "Desk stopped";
  }
  $("desk-state").textContent = label;
}
function bookLabel(book) { return book.basis === "mid" ? "Quote book" : "Trade-bar book"; }
function selectedBook(desk) {
  const books = desk.books || [];
  return books.find((b) => b.id === state.book)
    || books.find((b) => b.status === "live" && b.calibration && b.calibration.ready)
    || books.find((b) => b.status === "live") || books[0] || null;
}
function renderDesk() {
  const desk = currentDesk();
  const running = Boolean(desk && desk.running);
  $("desk-empty").hidden = running;
  $("desk-body").hidden = !running;
  $("book-switch").hidden = !running;
  if (!running) {
    $("desk-last-seen").textContent = desk && desk.updated_at ? `Last update ${dayTime(desk.updated_at)} UTC.` : "";
    return;
  }
  const book = selectedBook(desk);
  if (!book) return;
  renderBookSwitch(desk, book);
  renderFigures(desk, book);
  renderBoard(desk, book);
  renderPick(desk, book);
  renderPositions(book);
  renderActivity(book);
  refreshResults(desk, book).catch(report);
}
function renderBookSwitch(desk, book) {
  const host = $("book-switch");
  const key = (desk.books || []).map((b) => b.id).join(",");
  if (host.dataset.key !== key) {
    host.replaceChildren();
    for (const item of desk.books || []) {
      const button = el("button", { type: "button", "data-book": item.id }, bookLabel(item));
      button.addEventListener("click", () => {
        state.book = item.id;
        store.set("xasset.book", item.id);
        state.picked = null;
        renderDesk();
      });
      host.append(button);
    }
    host.dataset.key = key;
  }
  for (const button of host.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.book === book.id));
  }
}
function figure(label, value, sub, extraClass) {
  return el("div", null, el("dt", null, label), el("dd", { class: extraClass || null }, value, sub ? el("small", null, sub) : null));
}
function renderFigures(desk, book) {
  const pnl = Number(book.nav) - Number(book.initial_nav);
  const cal = book.calibration || {};
  const box = $("book-figures");
  box.replaceChildren(
    figure("Net asset value", num(book.nav, 2), `from ${num(book.initial_nav, 0)}`),
    figure("Since start", signed(pnl, 2), pct(pnl / Number(book.initial_nav), 3), tone(pnl)),
    figure("Open positions", String((book.positions || []).length), `gross ${num(book.gross, 0)}`),
  );
  if (book.status === "live") {
    const progress = el("span", { class: "progress", "aria-hidden": "true" }, el("i"));
    progress.firstChild.style.width = Math.min(100, (100 * (cal.sessions || 0)) / Math.max(1, cal.required || 20)) + "%";
    const value = cal.ready ? "Ready" : `${cal.sessions || 0} of ${cal.required || 20}`;
    const node = figure("Calibration", value, cal.ready ? `${cal.sessions} sessions` : "sessions");
    node.append(progress);
    box.append(node);
  } else {
    box.append(figure("Status", book.status === "failed" ? "Failed" : "Replaying history", book.warm ? `${num(book.warm.minutes, 0)} minutes` : ""));
  }
  $("book-note").textContent = bookNote(desk, book);
}
function bookNote(desk, book) {
  const cal = book.calibration || {};
  if (book.status === "failed") return book.error || "Warm-up failed. Check the desk's terminal output.";
  if (book.status !== "live") {
    return `Replaying history so every calibration starts from prior sessions${desk.warm_message ? " (" + desk.warm_message + ")" : ""}. Live minutes are queued and processed before the book starts trading.`;
  }
  const source = book.warmup === "recorded"
    ? "This book learns only from quotes this desk has recorded, so it needs the desk running across whole UTC days."
    : "Calibrations were primed from Binance kline archives; Hyperliquid perpetuals calibrate from recorded sessions.";
  if (!cal.ready) {
    return `Setups arm once ${cal.required} complete UTC-day sessions are on record (strategy 8 needs ${cal.lag_required}). ${source}`;
  }
  return `Calibrated on the previous ${cal.required} sessions; strategy 8's stability screen has ${Math.min(cal.lag_sessions, cal.lag_required)} of ${cal.lag_required}. ${source}`;
}

function boardStates(book) {
  const states = new Map();
  for (const strategy of book.strategies || []) {
    for (const symbol of strategy.cooling || []) states.set(strategy.id + "|" + symbol, { kind: "cool" });
  }
  for (const setup of book.setups || []) {
    const kind = setup.state === "OPEN" ? "open" : setup.state === "ORDERED" ? "ordered" : "armed";
    states.set(setup.strategy + "|" + setup.symbol, { kind, setup });
  }
  for (const position of book.positions || []) {
    const key = position.strategy + "|" + position.symbol;
    states.set(key, { ...(states.get(key) || {}), kind: "open", position });
  }
  return states;
}
function renderBoard(desk, book) {
  const table = $("board");
  const symbols = desk.symbols || [];
  const strategies = book.strategies || [];
  const key = book.id + "|" + symbols.map((s) => s.id).join(",") + "|" + strategies.map((s) => s.id).join(",");
  if (state.boardKey !== key) {
    buildBoard(table, symbols, strategies);
    state.boardKey = key;
  }
  const states = boardStates(book);
  for (const cell of table.querySelectorAll("td[data-symbol]")) {
    const lamp = cell.firstChild;
    if (!lamp) continue;
    const info = states.get(cell.dataset.strategy + "|" + cell.dataset.symbol);
    lamp.className = info ? "lamp " + info.kind : "";
    cell.setAttribute("aria-label", `${cell.dataset.symbol}, ${cell.dataset.strategy}: ${info ? stateWord(info.kind) : "watching"}`);
  }
}
function stateWord(kind) {
  return { armed: "armed", ordered: "confirmed, order pending", open: "position open", cool: "cooling down" }[kind] || "watching";
}
function buildBoard(table, symbols, strategies) {
  const groups = [
    { label: "Binance spot", items: symbols.filter((s) => s.kind !== "perp") },
    { label: "Hyperliquid perpetuals", items: symbols.filter((s) => s.kind === "perp") },
  ].filter((g) => g.items.length);
  const top = el("tr", null, el("th", { scope: "col", class: "group" }, ""));
  const labels = el("tr", null, el("th", { scope: "col" }, ""));
  const order = [];
  groups.forEach((group, index) => {
    if (index) { top.append(el("th", { class: "gap" })); labels.append(el("th", { class: "gap" })); order.push(null); }
    top.append(el("th", { scope: "colgroup", colspan: String(group.items.length), class: "group" }, group.label));
    for (const item of group.items) {
      labels.append(el("th", { scope: "col", class: "col", title: item.id }, el("span", null, short(item.id))));
      order.push(item.id);
    }
  });
  const body = el("tbody");
  strategies.forEach((strategy) => {
    const row = el("tr", null, el("th", { scope: "row", title: strategy.title }, el("b", null, strategy.id), el("span", null, strategy.title)));
    const targets = new Set(strategy.targets || []);
    for (const symbol of order) {
      if (symbol === null) { row.append(el("td", { class: "gap off-track" })); continue; }
      if (!targets.has(symbol)) {
        row.append(el("td", { class: "off-track", "aria-hidden": "true" }));
        continue;
      }
      row.append(el("td", { class: "on-track", tabindex: "-1", role: "button", "data-symbol": symbol, "data-strategy": strategy.id }, el("span")));
    }
    body.append(row);
  });
  table.replaceChildren(el("thead", null, top, labels), body);
  const first = table.querySelector("td[data-symbol]");
  if (first) first.tabIndex = 0;
}
function boardCells() { return Array.from($("board").querySelectorAll("td[data-symbol]")); }
function pickCell(cell) {
  if (!cell) return;
  for (const item of $("board").querySelectorAll("td.picked")) item.classList.remove("picked");
  cell.classList.add("picked");
  state.picked = { symbol: cell.dataset.symbol, strategy: cell.dataset.strategy };
  const desk = currentDesk();
  if (desk) renderPick(desk, selectedBook(desk));
}
function crosshair(cell) {
  const table = $("board");
  for (const item of table.querySelectorAll(".cross")) item.classList.remove("cross");
  if (!cell) return;
  const row = cell.parentElement;
  const index = Array.prototype.indexOf.call(row.children, cell);
  row.firstChild.classList.add("cross");
  const header = table.tHead.rows[1] && table.tHead.rows[1].children[index];
  if (header) header.classList.add("cross");
}
function setupBoardEvents() {
  const table = $("board");
  table.addEventListener("click", (event) => pickCell(event.target.closest("td[data-symbol]")));
  table.addEventListener("pointerover", (event) => crosshair(event.target.closest("td[data-symbol]")));
  table.addEventListener("pointerleave", () => crosshair(null));
  table.addEventListener("keydown", (event) => {
    const cell = event.target.closest("td[data-symbol]");
    if (!cell) return;
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pickCell(cell); return; }
    const rows = Array.from(table.tBodies[0].rows);
    const r = rows.indexOf(cell.parentElement);
    const c = Array.prototype.indexOf.call(cell.parentElement.children, cell);
    const moves = { ArrowRight: [0, 1], ArrowLeft: [0, -1], ArrowDown: [1, 0], ArrowUp: [-1, 0] };
    if (!moves[event.key]) return;
    event.preventDefault();
    const [dr, dc] = moves[event.key];
    let rr = r, cc = c;
    for (let step = 0; step < 60; step++) {
      rr += dr; cc += dc;
      const target = rows[rr] && rows[rr].children[cc];
      if (!target) return;
      if (target.matches("td[data-symbol]")) {
        for (const item of boardCells()) item.tabIndex = -1;
        target.tabIndex = 0;
        target.focus();
        crosshair(target);
        return;
      }
    }
  });
}
function renderPick(desk, book) {
  const box = $("pick");
  if (!state.picked) { box.textContent = "Select a lamp to see the setup behind it."; return; }
  const { symbol, strategy } = state.picked;
  const info = boardStates(book).get(strategy + "|" + symbol);
  const name = el("strong", null, `${symbol} on ${strategy}`);
  const title = ". ";
  let text = "Watching: no setup is armed.";
  if (info && info.kind === "open" && info.position) {
    const p = info.position;
    text = `${p.direction > 0 ? "Long" : "Short"} ${qty(p.qty)} at ${price(p.entry_price)} since ${clockTime(p.entry_time)} UTC. Stop ${price(p.stop)}, target ${price(p.target)}, time exit ${shortTime(p.deadline)} UTC. Unrealized ${signed(p.unrealized)}.`;
  } else if (info && info.kind === "ordered") {
    text = `Confirmed at ${clockTime(info.setup.armed_at)} UTC; the order fills at the next eligible quote.`;
  } else if (info && info.kind === "armed") {
    text = `Armed at ${clockTime(info.setup.armed_at)} UTC, now in state ${words(info.setup.state).toLowerCase()}; waiting for confirmation.`;
  } else if (info && info.kind === "cool") {
    text = "Cooling down for 15 minutes after its last setup ended.";
  }
  box.replaceChildren(name, title, text);
}
function renderPositions(book) {
  const body = head($("positions"), ["Instrument", "Strategy", "Side", ["Quantity", true], ["Entry", true], ["Mark", true], ["Stop", true], ["Target", true], ["Unrealized", true], "Exit by"]);
  const positions = book.positions || [];
  for (const p of positions) {
    body.append(cells([
      el("span", { class: "id" }, p.symbol),
      p.strategy,
      p.direction > 0 ? "Long" : "Short",
      n(qty(p.qty)), n(price(p.entry_price)), n(price(p.mark)), n(price(p.stop)), n(price(p.target)),
      n(signed(p.unrealized), tone(p.unrealized)),
      p.exiting ? `Exiting (${words(p.exiting).toLowerCase()})` : shortTime(p.deadline) + " UTC",
    ]));
  }
  if (!positions.length) emptyRow(body, 10, "No open positions. Lamps turn solid green when a setup fills.");
}
function eventText(event) {
  const detail = event.detail || {};
  const reason = detail.reason ? `: ${detail.reason}` : "";
  switch (event.event) {
    case "armed": return `armed (${words(event.state).toLowerCase()})`;
    case "confirmed": return "confirmed; order submitted";
    case "filled": return `filled at ${price(detail.price)}`;
    case "exited": return `exited${reason}`;
    case "expired": return `expired${reason}`;
    case "cancelled": return `order cancelled${reason}`;
    case "rejected": return `rejected${reason}`;
    case "live": return detail.restored ? "book resumed from its saved state" : "book went live";
    default: return `${words(event.event).toLowerCase()} (${words(event.state).toLowerCase()})`;
  }
}
function renderActivity(book) {
  const list = $("activity");
  const events = (book.recent_events || []).slice().reverse();
  list.replaceChildren();
  for (const event of events) {
    const who = event.strategy === "desk" ? "" : event.symbol;
    list.append(el("li", null,
      el("time", { datetime: event.at }, clockTime(event.at)),
      el("span", { class: "id" }, event.strategy === "desk" ? "desk" : event.strategy),
      el("span", { class: "what" }, who ? who + " " : "", eventText(event))));
  }
  if (!events.length) list.append(el("li", null, el("span", { class: "empty" }, "Nothing has happened yet. Setups appear here as strategies arm, confirm, fill and exit.")));
}
async function refreshResults(desk, book) {
  const key = desk.id + "/" + book.id;
  const now = Date.now();
  if (state.results.key === key && now - state.results.at < 30000) return;
  state.results = { key, at: now };
  const [nav, trades] = await Promise.all([
    api(`/api/paper/${encodeURIComponent(desk.id)}/${encodeURIComponent(book.id)}/nav?points=600`),
    api(`/api/paper/${encodeURIComponent(desk.id)}/${encodeURIComponent(book.id)}/trades?limit=200`),
  ]);
  lineChart($("paper-chart"), (nav.points || []).map(([at, value]) => [at, value]), Number(book.initial_nav),
    "No minutes recorded yet. The line starts once the book is live.", true);
  const summary = head($("paper-strategies"), ["Strategy", ["Trades", true], ["Win rate", true], ["Net", true], ["Fees", true]]);
  const rows = Object.entries(trades.by_strategy || {}).sort((a, b) => a[0].localeCompare(b[0]));
  for (const [sid, item] of rows) {
    summary.append(cells([el("span", { class: "id" }, sid), n(num(item.trades, 0)), n(plainPct(item.wins / Math.max(1, item.trades))), n(signed(item.net), tone(item.net)), n(num(item.fees))]));
  }
  if (!rows.length) emptyRow(summary, 5, "No closed trades yet.");
  const body = head($("paper-trades"), ["Closed", "Instrument", "Strategy", "Side", ["Entry", true], ["Exit", true], "Reason", ["Net", true]]);
  for (const t of trades.trades || []) {
    body.append(cells([dayTime(t.exit_time), el("span", { class: "id" }, t.symbol), t.strategy, t.direction > 0 ? "Long" : "Short", n(price(t.entry_price)), n(price(t.exit_price)), words(t.exit_reason), n(signed(t.net), tone(t.net))]));
  }
  if (!(trades.trades || []).length) emptyRow(body, 8, "No closed trades yet.");
}

/* ---------- charts -------------------------------------------------------- */
const charts = new Map();
function lineChart(host, points, baseline, emptyMessage, intraday) {
  charts.set(host, [points, baseline, emptyMessage, intraday]);
  host.replaceChildren();
  const values = points.filter((p) => finite(p[1]));
  if (values.length < 2) { host.append(el("p", { class: "muted" }, emptyMessage)); return; }
  const width = Math.max(280, host.clientWidth - 28), height = 190, left = 64, right = 8, top = 12, bottom = 24;
  const ys = values.map((p) => Number(p[1]));
  let low = Math.min(...ys, finite(baseline) ? baseline : Infinity);
  let high = Math.max(...ys, finite(baseline) ? baseline : -Infinity);
  if (high - low < 1e-9) { high += 1; low -= 1; }
  const pad = (high - low) * 0.08;
  low -= pad; high += pad;
  const x = (i) => left + (i / (values.length - 1)) * (width - left - right);
  const y = (v) => top + (1 - (v - low) / (high - low)) * (height - top - bottom);
  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": "Net asset value over time" });
  if (finite(baseline)) chart.append(svg("line", { class: "base", x1: left, x2: width - right, y1: y(baseline), y2: y(baseline) }));
  chart.append(svg("polyline", { class: "line", points: values.map((p, i) => `${x(i).toFixed(1)},${y(Number(p[1])).toFixed(1)}`).join(" ") }));
  const label = (text, xx, yy, anchor) => { const t = svg("text", { class: "axis", x: xx, y: yy, "text-anchor": anchor }); t.textContent = text; chart.append(t); };
  label(num(high - pad, 0), left - 8, top + 10, "end");
  label(num(low + pad, 0), left - 8, height - bottom, "end");
  const stamp = (iso) => (intraday ? dayTime(iso) : day(iso));
  label(stamp(values[0][0]), left, height - 4, "start");
  label(stamp(values[values.length - 1][0]), width - right, height - 4, "end");
  const cursor = svg("line", { class: "cursor", y1: top, y2: height - bottom, x1: -10, x2: -10 });
  chart.append(cursor);
  const last = values[values.length - 1];
  const readout = el("span", { class: "readout" }, `${stamp(last[0])}: ${num(last[1], 2)}`);
  chart.addEventListener("pointermove", (event) => {
    const box = chart.getBoundingClientRect();
    const ratio = ((event.clientX - box.left) / box.width * width - left) / (width - left - right);
    const i = Math.max(0, Math.min(values.length - 1, Math.round(ratio * (values.length - 1))));
    cursor.setAttribute("x1", x(i)); cursor.setAttribute("x2", x(i));
    readout.textContent = `${stamp(values[i][0])}: ${num(values[i][1], 2)}`;
  });
  chart.addEventListener("pointerleave", () => {
    cursor.setAttribute("x1", -10); cursor.setAttribute("x2", -10);
    readout.textContent = `${stamp(last[0])}: ${num(last[1], 2)}`;
  });
  host.append(chart, readout);
}
let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    for (const [host, args] of charts) if (host.isConnected && host.offsetParent !== null) lineChart(host, ...args);
  }, 150);
});

/* ---------- strategies ---------------------------------------------------- */
const MODES = {
  "backtest + paper": ["both", "Backtest and paper"],
  "paper only": ["paper", "Paper desk only"],
  "backtest only": ["backtest", "Backtest only"],
  blocked: ["blocked", "Blocked"],
};
async function loadCatalog() {
  if (!state.catalog) state.catalog = await api("/api/lab/catalog");
  const handbook = state.catalog.filter((s) => s.id.startsWith("h"));
  const notebook = state.catalog.filter((s) => s.id.startsWith("n"));
  renderCatalog($("handbook"), handbook);
  renderCatalog($("notebook"), notebook);
}
function renderCatalog(table, items) {
  const body = head(table, ["Strategy", "Where it runs", "Instruments", "Data it needs"]);
  for (const item of items) {
    const [cls, label] = MODES[item.mode] || ["blocked", words(item.mode)];
    const where = el("div", null, el("span", { class: "mode " + cls }, el("i"), label));
    const placesText = [
      (item.backtest || []).length ? "Replay: " + item.backtest.join(", ") : "",
      (item.paper || []).length ? "Paper: " + item.paper.join(", ") : "",
    ].filter(Boolean).join(". ");
    if (placesText) where.append(el("span", { class: "cell-sub" }, placesText));
    if (item.blocked) where.append(el("span", { class: "blocked-reason" }, item.blocked));
    else if (item.note) where.append(el("span", { class: "blocked-reason" }, item.note));
    const name = el("div", null,
      el("span", { class: "id" }, item.id + "  "),
      item.title || "",
      item.direction ? el("span", { class: "muted" }, ` (${item.direction}${item.time_exit_minutes ? ", " + item.time_exit_minutes + "-minute time exit" : ""})`) : null,
      el("span", { class: "cell-sub" }, item.idea || ""));
    body.append(cells([name, where, item.assets || DASH, el("span", { class: "needs" }, (item.data || []).join(", "))]));
  }
}

/* ---------- backtests ------------------------------------------------------- */
async function loadRuns() {
  const data = await api("/api/lab/runs");
  state.runs = data;
  const registry = $("registry");
  registry.replaceChildren();
  for (const record of data.registry || []) {
    const reg = record.registration || {};
    const holdout = record.holdout;
    const holdoutText = holdout
      ? `Holdout opened ${day(holdout.opened_at)} (${holdout.reason})${holdout.run ? ", run once" : ", not yet run"}.`
      : `Holdout ${day(reg.holdout_start)} to ${day(reg.end)} is sealed.`;
    registry.append(el("p", null,
      el("span", { class: "id" }, reg.book), ` registered ${day(reg.registered_at)}. Discovery ${day(reg.start)} to ${day(reg.holdout_start)}. `,
      holdoutText, el("span", { class: "muted" }, ` ${(record.runs || []).length} run${(record.runs || []).length === 1 ? "" : "s"} recorded.`)));
  }
  if (!(data.registry || []).length) {
    registry.append(el("p", { class: "muted" }, "No book is registered yet. Register one with "), el("code", null, "uv run xasset lab register config/lab/crypto-book.yaml --start 2026-01-01 --end 2026-10-01"));
  }
  const body = head($("runs"), ["Book", "Phase", "Reported period", "Variant", "Status", ["Net P&L", true], "Finished"]);
  for (const run of data.runs || []) {
    const pnl = finite(run.portfolio.end_nav) ? run.portfolio.end_nav - run.portfolio.start_nav : null;
    const row = cells([
      el("span", { class: "id" }, run.book),
      words(run.phase),
      run.period ? `${day(run.period.report_from)} to ${day(run.period.end)}` : DASH,
      run.variant === "trade-bar" ? "Trade bars" : run.variant === "daily-model" ? "Daily models" : "Quotes",
      el("span", null, words(run.status), run.amended_code ? el("span", { class: "flag", title: "The lab code changed after the book was registered" }, "amended code") : null),
      n(signed(pnl), tone(pnl)),
      dayTime(run.finished_at),
    ]);
    row.className = "selectable";
    row.tabIndex = 0;
    row.dataset.run = run.id;
    row.setAttribute("aria-selected", String(state.run && state.run.id === run.id));
    const open = () => { state.strategyFilter = null; selectRun(run.id).catch(report); };
    row.addEventListener("click", open);
    row.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
    body.append(row);
  }
  if (!(data.runs || []).length) emptyRow(body, 7, "No runs yet. Start one with: uv run xasset lab run config/lab/crypto-book.yaml");
  const first = (data.runs || []).find((r) => r.status === "completed");
  if (!state.run && first) await selectRun(first.id);
  else if (state.run) await selectRun(state.run.id);
}
async function selectRun(id) {
  const detail = await api(`/api/lab/runs/${encodeURIComponent(id)}?scenario=${state.scenario}`);
  state.run = { id, detail };
  for (const row of $("runs").querySelectorAll("tr[data-run]")) row.setAttribute("aria-selected", String(row.dataset.run === id));
  renderRun(detail);
  await loadRunTrades();
}
function renderRun(detail) {
  $("run-detail").hidden = false;
  const run = detail.run;
  const study = run.kind === "notebook-study";
  $("run-title").textContent = `${run.book}, ${run.phase} run`;
  const period = run.period || {};
  let variant = "Quote versions";
  if (run.variant === "trade-bar") variant = "Trade-bar variants (archives carry trades, no quotes)";
  if (study) variant = `Daily model decisions over purged walk-forward folds (${(run.folds || []).length} test windows); models, penalties and thresholds are chosen inside each fold's training data`;
  $("run-subtitle").textContent = `${variant}. Reported ${day(period.report_from)} to ${day(period.end)}; history from ${day(period.replay_start)}.`;
  if (run.amended_code) $("run-subtitle").append(el("span", { class: "flag" }, "code changed after registration"));
  for (const button of $("scenario-switch").querySelectorAll("button")) button.setAttribute("aria-pressed", String(button.dataset.scenario === detail.scenario));
  const portfolio = detail.portfolio || {};
  const pnl = portfolio.end_nav - portfolio.start_nav;
  const windows = study ? portfolio.sessions5 || {} : portfolio.episodes14 || {};
  $("run-figures").replaceChildren(
    figure("Net asset value", num(portfolio.end_nav, 0), `from ${num(portfolio.start_nav, 0)}`),
    figure("Return", pct(pnl / portfolio.start_nav, 2), signed(pnl, 0), tone(pnl)),
    figure(study ? "Five-session windows" : "Fourteen-day windows", num(windows.episodes, 0), `${num(windows.at_least_10pct || 0, 0)} reached +10%`),
    figure("Worst window", pct(windows.worst, 2), `best ${pct(windows.best, 2)}`, tone(windows.worst)),
  );
  lineChart($("run-chart"), portfolio.nav_daily || [], portfolio.start_nav, "This scenario recorded no daily marks.", false);
  const body = head($("run-strategies"), ["Strategy", ["Trades", true], ["Win rate", true], ["Net P&L", true], ["Mean net", true], ["Daily t (HAC)", true], ["Adjusted p", true], ["Max drawdown", true], "Exits"]);
  const strategies = detail.strategies || {};
  for (const [sid, data] of Object.entries(strategies)) {
    const s = data.summary || {};
    const e = data.events || {};
    const p = (detail.multiplicity || {})[sid] || {};
    const exits = Object.entries(s.exit_reasons || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${words(k).toLowerCase()} ${v}`).join(", ");
    let sub;
    if (data.baseline_of) sub = "same number of names each day, ranked by the controls-only model";
    else if (study) sub = `${num(e.decisions || 0, 0)} decisions, ${num(e.filled || 0, 0)} filled${data.sessions5 ? `; ${num(data.sessions5.at_least_10pct || 0, 0)} of ${num(data.sessions5.episodes || 0, 0)} five-session windows reached +10%` : ""}`;
    else sub = `armed ${num(e.armed || 0, 0)}, confirmed ${num(e.confirmed || 0, 0)}, filled ${num(e.filled || 0, 0)}, rejected ${num(e.rejected || 0, 0)}`;
    const name = el("div", null, el("span", { class: "id" }, sid + "  "), data.title || "", el("span", { class: "cell-sub" }, sub));
    const row = cells([
      name, n(num(s.trades || 0, 0)), n(plainPct(s.win_rate)), n(signed(s.net_pnl), tone(s.net_pnl)),
      n(finite(s.mean_net_bps) ? signed(s.mean_net_bps, 1) + " bps" : DASH, tone(s.mean_net_bps)),
      n(signed(s.daily_t_hac, 2)), n(finite(p.p_adjusted) ? num(p.p_adjusted, 3) : DASH),
      n(finite(s.max_drawdown) ? plainPct(s.max_drawdown, 2) : DASH), exits || DASH,
    ]);
    row.className = "selectable";
    row.tabIndex = 0;
    row.setAttribute("aria-selected", String(state.strategyFilter === sid));
    const pick = () => { state.strategyFilter = state.strategyFilter === sid ? null : sid; renderRun(detail); loadRunTrades().catch(report); };
    row.addEventListener("click", pick);
    row.addEventListener("keydown", (ev) => { if (ev.key === "Enter") pick(); });
    body.append(row);
  }
  if (!Object.keys(strategies).length) emptyRow(body, 9, "This run has no strategy results.");
  $("run-note").textContent = study
    ? "Adjusted p comes from a block-bootstrap max-T test across every sleeve in this run, baselines included. Each strategy trades its own sleeve with equal starting capital; the chart combines the strategy sleeves. Select a row to filter the trades below."
    : "Adjusted p comes from a block-bootstrap max-T test across this book's strategies, so it accounts for testing several at once. Mean net is per trade after costs. Select a strategy to filter the trades below.";
  renderEvidence(detail, study);
}
function checksText(sid, checks) {
  if (!checks) return DASH;
  const parts = [];
  for (const key of ["p_up10", "p_down10"]) {
    if (checks[key] && finite(checks[key].skill)) parts.push(`${key === "p_up10" ? "+10%" : "−10%"} touch skill ${signed(checks[key].skill, 3)}`);
  }
  if (checks.breakdown_model && finite(checks.breakdown_model.skill)) parts.push(`breakdown model skill ${signed(checks.breakdown_model.skill, 3)} on ${num(checks.breakdown_model.events, 0)} events`);
  for (const [key, label] of [["signal_volatility_rank_correlation", "signal vs volatility"], ["signal_sigma20_correlation", "signal vs volatility"], ["signal_beta60_correlation", "signal vs beta"], ["signal_beta_correlation", "signal vs beta"], ["signal_trend_correlation", "signal vs trend"], ["rank_correlation_with_next_day", "next-day rank correlation"], ["rank_correlation_dropping_unfinished", "same, dropping unfinished"]]) {
    if (finite(checks[key])) parts.push(`${label} ${signed(checks[key], 3)}`);
  }
  if (finite(checks.candidate_mean_beta60)) parts.push(`candidate beta ${num(checks.candidate_mean_beta60, 2)} vs universe ${num(checks.universe_mean_beta60, 2)}`);
  return parts.length ? parts.join("; ") : DASH;
}
function renderEvidence(detail, study) {
  $("run-evidence").hidden = !study;
  if (!study) return;
  const body = head($("run-evidence-table"), ["Strategy", ["Test rows", true], ["Incremental R²", true], ["Rank correlation", true], ["Slope θ", true], ["95% interval", true], "Checks in the last fold"]);
  const folds = head($("run-folds"), ["Strategy", "Test window", ["Penalty", true], ["Signal quantile", true], ["Threshold", true], ["Allowance", true], ["Training rows", true], ["Decisions", true]]);
  for (const [sid, data] of Object.entries(detail.strategies || {})) {
    const d = data.diagnostics;
    if (!d) continue;
    const inc = d.incremental || {};
    const last = (d.folds || [])[d.folds.length - 1] || {};
    const interval = inc.theta_95 ? `${signed(inc.theta_95[0], 4)} to ${signed(inc.theta_95[1], 4)}` : DASH;
    body.append(cells([
      el("span", { class: "id" }, sid), n(num(inc.rows, 0)), n(finite(inc.r2_incremental) ? signed(inc.r2_incremental * 100, 3) + "%" : DASH, tone(inc.r2_incremental)),
      n(signed(inc.ic_incremental, 3), tone(inc.ic_incremental)), n(signed(inc.theta, 4), tone(inc.theta)), n(interval), checksText(sid, last.checks),
    ]));
    for (const fold of d.folds || []) {
      folds.append(cells([el("span", { class: "id" }, sid), fold.test, n(fold.ridge_penalty), n(num(fold.signal_quantile, 2)), n(num(fold.threshold, 4)), n(num(fold.allowance, 5)), n(num(fold.training_rows, 0)), n(num(fold.decisions, 0))]));
    }
  }
}
async function loadRunTrades() {
  if (!state.run) return;
  const filter = state.strategyFilter ? `&strategy=${encodeURIComponent(state.strategyFilter)}` : "";
  const data = await api(`/api/lab/runs/${encodeURIComponent(state.run.id)}/trades?scenario=${state.scenario}&limit=300${filter}`);
  $("run-trades-title").textContent = state.strategyFilter ? `Trades for ${state.strategyFilter}` : "Trades";
  const body = head($("run-trades"), ["Entry", "Instrument", "Strategy", "Side", ["Entry price", true], ["Exit price", true], "Exit", ["Net", true], ["R", true]]);
  for (const t of data.trades || []) {
    const risk = Math.abs(t.entry_price - t.stop) * t.qty;
    const r = risk > 0 ? t.net / risk : null;
    body.append(cells([dayTime(t.entry_time), el("span", { class: "id" }, t.symbol), t.strategy, t.direction > 0 ? "Long" : "Short", n(price(t.entry_price)), n(price(t.exit_price)), words(t.exit_reason) + (t.ambiguous_bar ? " (stop and target in one bar)" : ""), n(signed(t.net), tone(t.net)), n(signed(r, 2), tone(r))]));
  }
  if (!(data.trades || []).length) emptyRow(body, 9, "No trades in this selection.");
  else if (data.total > (data.trades || []).length) body.append(el("tr", null, el("td", { class: "empty", colspan: "9" }, `Showing the latest ${data.trades.length} of ${num(data.total, 0)} trades.`)));
}

/* ---------- relationships ------------------------------------------------------ */
function selectEdge(edge) {
  const box = $("edge-detail");
  const dl = el("dl");
  const rows = {
    Kind: words(edge.kind), Horizon: edge.horizon, Lag: edge.lag, Status: words(edge.status),
    "First window strength": num(edge.train.strength, 3), "Following window strength": num(edge.validation.strength, 3),
    "First adjusted p": num(edge.q_train, 5), "Following adjusted p": num(edge.q_validation, 5),
    "First samples": num(edge.train.n, 0), "Following samples": num(edge.validation.n, 0),
  };
  for (const [k, v] of Object.entries(rows)) dl.append(el("dt", null, k), el("dd", null, String(v)));
  box.replaceChildren(el("h3", null, `${short(edge.source)} and ${short(edge.target)}`), dl,
    el("p", { class: "muted" }, edge.validation.reason || edge.train.reason || "Stable means the same sign, at least half the strength and corrected significance in both windows."));
}
function drawGraph(nodes, edges) {
  const g = $("graph");
  g.replaceChildren();
  const positions = new Map();
  nodes.forEach((node, i) => {
    const angle = (2 * Math.PI * i) / Math.max(nodes.length, 1) - Math.PI / 2;
    positions.set(node.id, { x: 425 + 300 * Math.cos(angle), y: 280 + 215 * Math.sin(angle) });
  });
  const lines = [];
  for (const edge of edges.slice(0, 150)) {
    const a = positions.get(edge.source), b = positions.get(edge.target);
    if (!a || !b) continue;
    const line = svg("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: "edge " + ((edge.train.strength || 0) >= 0 ? "pos-edge" : "neg-edge"), tabindex: 0 });
    line.addEventListener("click", () => selectEdge(edge));
    line.addEventListener("keydown", (e) => { if (e.key === "Enter") selectEdge(edge); });
    g.append(line);
    lines.push({ line, edge });
  }
  for (const node of nodes) {
    const p = positions.get(node.id);
    const group = svg("g", { class: "node " + node.asset_class, transform: `translate(${p.x} ${p.y})` });
    group.append(svg("circle", { r: 7 }));
    const left = p.x < 425;
    const label = svg("text", { x: left ? -11 : 11, y: 4, "text-anchor": left ? "end" : "start" });
    label.textContent = short(node.id);
    group.append(label);
    g.append(group);
    let dragging = false;
    group.addEventListener("pointerdown", (e) => { dragging = true; group.setPointerCapture(e.pointerId); });
    group.addEventListener("pointerup", () => { dragging = false; });
    group.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const point = g.createSVGPoint();
      point.x = e.clientX; point.y = e.clientY;
      const local = point.matrixTransform(g.getScreenCTM().inverse());
      p.x = Math.max(20, Math.min(800, local.x)); p.y = Math.max(20, Math.min(540, local.y));
      group.setAttribute("transform", `translate(${p.x} ${p.y})`);
      for (const { line, edge } of lines) {
        const a = positions.get(edge.source), b = positions.get(edge.target);
        line.setAttribute("x1", a.x); line.setAttribute("y1", a.y); line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
      }
    });
  }
}
async function loadMap() {
  const request = ++state.mapRequest;
  const query = new URLSearchParams({ horizon: $("horizon").value, kind: $("kind").value, state: $("edge-state").value });
  const data = await api("/api/map?" + query);
  if (request !== state.mapRequest) return;
  drawGraph(data.nodes || [], data.edges || []);
  const design = data.design ? ` Discovery window ${day(data.design.start)} to ${day(data.design.end)}.` : "";
  $("edge-total").textContent = `${num(data.total, 0)} relationships match; the network shows up to 150 and the table up to 400.${design}`;
  const body = head($("edges"), ["Driver or pair", "Follower", "Kind", ["Lag", true], ["First window", true], ["Following window", true], ["Adjusted p", true], "Status"]);
  for (const edge of data.edges || []) {
    const row = cells([short(edge.source), short(edge.target), words(edge.kind), n(edge.lag), n(num(edge.train.strength, 3)), n(num(edge.validation.strength, 3)), n(num(edge.q_validation, 5)), words(edge.status)]);
    row.className = "selectable";
    row.tabIndex = 0;
    row.addEventListener("click", () => selectEdge(edge));
    row.addEventListener("keydown", (e) => { if (e.key === "Enter") selectEdge(edge); });
    body.append(row);
  }
  if (!(data.edges || []).length) emptyRow(body, 8, "No relationship meets this filter. Choose All candidates to see every tested pair.");
}

/* ---------- data health ------------------------------------------------------------ */
function renderDeskData() {
  const desk = currentDesk();
  const feeds = head($("feeds"), ["Feed", "Status", ["Last message", true], ["Median delay", true], ["95th percentile", true], ["Connections", true], "Last error"]);
  for (const feed of (desk && desk.running && desk.feeds) || []) {
    const lag = feed.lag || {};
    feeds.append(cells([
      el("span", { class: "id" }, words(feed.name)),
      el("span", { class: "state" }, el("span", { class: "lamp " + (feed.connected ? "on" : "off") }), feed.connected ? "Connected" : "Reconnecting"),
      n(age(feed.last_message_age) + " ago"), n(finite(lag.median_ms) ? num(lag.median_ms, 0) + " ms" : DASH),
      n(finite(lag.p95_ms) ? num(lag.p95_ms, 0) + " ms" : DASH), n(num(feed.connects, 0)), feed.last_error || DASH,
    ]));
  }
  if (!(desk && desk.running)) emptyRow(feeds, 7, "The paper desk is not running, so no live feed is open.");
  const clock = desk && desk.clock;
  $("clock-note").textContent = clock && desk.running
    ? (clock.error ? `Clock check failed (${clock.error}); the desk uses this computer's clock.` : `The desk corrects this computer's clock by ${signed(clock.offset_ms, 1)} ms against Binance server time (measured with a ${num(clock.round_trip_ms, 0)} ms round trip). Delay is receipt time minus exchange trade time.`)
    : "";
  const body = head($("instruments"), ["Instrument", "Venue", ["Bid", true], ["Ask", true], ["Spread", true], ["Quote age", true], ["Last trade", true], ["Bars", true], ["Skipped", true], ["Late prints", true]]);
  for (const s of (desk && desk.running && desk.symbols) || []) {
    body.append(cells([
      el("span", { class: "state" }, el("span", { class: "lamp " + (s.covered ? "on" : "off") }), el("span", { class: "id" }, s.id)),
      `${words(s.feed)} ${s.venue_symbol}`, n(price(s.bid)), n(price(s.ask)),
      n(finite(s.spread_bps) ? num(s.spread_bps, s.spread_bps < 1 ? 3 : 2) + " bps" : DASH),
      n(age(s.quote_age)), n(age(s.trade_age)), n(num(s.bars, 0)), n(num(s.skipped, 0)), n(num(s.late_prints, 0)),
    ]));
  }
  if (!(desk && desk.running)) emptyRow(body, 10, "Start the paper desk to see live quotes for every instrument.");
}
async function loadHealth() {
  const data = await api("/api/health");
  const monitor = head($("monitor-feeds"), ["Instrument", "Source", "Status", "Last complete bar", ["Bars", true]]);
  for (const feed of data.live || []) {
    monitor.append(cells([el("span", { class: "id" }, feed.symbol), words(feed.source), words(feed.status), dayTime(feed.latest_at), n(num(feed.rows, 0))]));
  }
  if (!(data.live || []).length) emptyRow(monitor, 5, "The market monitor has not completed a cycle. Run uv run xasset-app monitor --once.");
  const coverage = head($("coverage"), ["Instrument", "Feed", ["Minute bars", true], ["Missing minutes", true], ["Coverage", true], "Evidence"]);
  for (const item of data.equities || []) {
    coverage.append(cells([el("span", { class: "id" }, item.symbol), item.feed, n(num(item.rows, 0)), n(num(item.missing_minutes, 0)), n(plainPct(item.coverage, 2)), item.evidence_ok ? "Verified" : "Failed"]));
  }
  if (!(data.equities || []).length) emptyRow(coverage, 6, "No equity audit has been published yet.");
}

/* ---------- start ------------------------------------------------------------------ */
function tickClock() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString("en-GB", { ...utc, hour: "2-digit", minute: "2-digit", second: "2-digit" }) + " UTC";
  $("clock").title = "Local time " + now.toLocaleTimeString();
}
function start() {
  setupBoardEvents();
  for (const button of $("scenario-switch").querySelectorAll("button")) {
    button.addEventListener("click", () => {
      state.scenario = button.dataset.scenario;
      if (state.run) selectRun(state.run.id).catch(report);
    });
  }
  for (const id of ["horizon", "kind", "edge-state"]) $(id).addEventListener("change", () => loadMap().catch(report));
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const response = await fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: $("token").value }) });
    if (response.ok) { $("token").value = ""; $("login-error").textContent = ""; $("login").close(); route(); }
    else $("login-error").textContent = "That token was not accepted.";
  });
  window.addEventListener("hashchange", route);
  tickClock();
  setInterval(tickClock, 1000);
  refreshDesk().catch(report);
  let ticks = 0;
  setInterval(() => {
    ticks += 1;
    const live = state.view === "desk" || state.view === "data";
    if (live || ticks % 5 === 0) refreshDesk().catch(report);
  }, 2000);
  route();
}
start();
