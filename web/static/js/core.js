/* Shared helpers for every screen. Plain JavaScript, no libraries. */
"use strict";

const App = {};

/* ------------------------------------------------------------------ formatting */
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const LAKH0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const LAKH2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Whole taka, lakh/crore grouping, negatives in brackets, zero as "-" (like the sheet). */
App.money = function (x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "";
  const r = Math.round(x);
  if (r === 0) return "-";
  return r < 0 ? "(" + LAKH0.format(-r) + ")" : LAKH0.format(r);
};
/** Two decimals, lakh/crore grouping (for detail views and input boxes). */
App.money2 = function (x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "";
  return x < 0 ? "(" + LAKH2.format(-x) + ")" : LAKH2.format(x);
};
App.pct = function (x, dp) {
  if (x === null || x === undefined || Number.isNaN(x)) return "";
  return (x * 100).toFixed(dp === undefined ? 4 : dp) + "%";
};
/** ISO "2024-03-27" -> "27-Mar-24" */
App.date = function (iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  return String(d).padStart(2, "0") + "-" + MONTHS[m - 1] + "-" + String(y % 100).padStart(2, "0");
};
App.dateLong = function (iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  return d + " " + ["January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December"][m - 1] + " " + y;
};
App.dateTime = function (isoDT) {
  if (!isoDT) return "";
  const [d, t] = isoDT.split("T");
  return App.date(d) + " " + (t || "").slice(0, 5);
};

/* ------------------------------------------------------------------ parsing */
/** "27-Mar-24", "27-Mar-2024", "27 mar 24", "27/Mar/2024", "2024-03-27" -> "2024-03-27"; else null.
 *  Two-digit years are 2000-2099. */
App.parseDate = function (text) {
  if (!text) return null;
  const s = text.trim();
  let y, m, d;
  let mt = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (mt) {
    [y, m, d] = [Number(mt[1]), Number(mt[2]), Number(mt[3])];
  } else {
    mt = s.match(/^(\d{1,2})[-\s/]([A-Za-z]{3})[A-Za-z]*[-\s/,]+(\d{2}|\d{4})$/);
    if (!mt) return null;
    d = Number(mt[1]);
    m = MONTHS.findIndex((x) => x.toLowerCase() === mt[2].toLowerCase()) + 1;
    if (m === 0) return null;
    y = mt[3].length === 2 ? 2000 + Number(mt[3]) : Number(mt[3]);
  }
  const dt = new Date(Date.UTC(y, m - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) return null;
  return String(y).padStart(4, "0") + "-" + String(m).padStart(2, "0") + "-" + String(d).padStart(2, "0");
};
/** "23,09,01,872.50" or "230901872.5" -> 230901872.5 ; "" -> null ; junk -> NaN */
App.parseNumber = function (text) {
  if (text === null || text === undefined) return null;
  const s = String(text).replace(/[,\s]/g, "");
  if (s === "") return null;
  if (!/^-?\d*\.?\d+$/.test(s)) return NaN;
  return Number(s);
};

/* ------------------------------------------------------------------ DOM helpers */
App.$ = (sel, root) => (root || document).querySelector(sel);
App.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
App.esc = function (s) {
  return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
};
App.qs = (name) => new URLSearchParams(location.search).get(name);
/** A "next" page address is followed only if it is a path inside this app ("/fund?code=X").
 *  Anything else ("javascript:...", "//evil.com", "/\\evil.com", "https://...") goes to "/". */
App.safeNext = function (n) {
  if (typeof n !== "string" || n.length > 500) return "/";
  if (!/^\/(?![\/\\])/.test(n)) return "/";
  if (/[\u0000-\u001f\\]/.test(n)) return "/";
  return n;
};
App.url = function (path, params) {
  const q = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => { if (v !== null && v !== undefined && v !== "") q.set(k, v); });
  const s = q.toString();
  return path + (s ? "?" + s : "");
};

/** Show a message box. kind: ok | warn | bad. items: optional list of lines. */
App.message = function (el, kind, text, items) {
  if (typeof el === "string") el = App.$(el);
  if (!el) return;
  if (!text) { el.hidden = true; el.innerHTML = ""; return; }
  el.className = "msg " + kind;
  el.innerHTML = App.esc(text) + (items && items.length ? "<ul>" + items.map((i) => "<li>" + App.esc(i) + "</li>").join("") + "</ul>" : "");
  el.hidden = false;
};

/* ------------------------------------------------------------------ inputs with live feedback */
/** Date box: shows the understood full date under the field, marks bad input. */
App.dateInput = function (input, hintEl) {
  const update = () => {
    const iso = App.parseDate(input.value);
    if (!input.value.trim()) { input.classList.remove("invalid"); if (hintEl) hintEl.textContent = ""; return; }
    input.classList.toggle("invalid", !iso);
    if (hintEl) hintEl.textContent = iso ? "= " + App.dateLong(iso) : "Use the form 27-Mar-24";
  };
  input.addEventListener("input", update);
  input.addEventListener("blur", () => { const iso = App.parseDate(input.value); if (iso) input.value = App.date(iso); update(); });
  input.setIso = (iso) => { input.value = iso ? App.date(iso) : ""; update(); };
  input.getIso = () => App.parseDate(input.value);
  update();
};
/** Number box: lakh/crore grouping on blur, marks bad input. */
App.numberInput = function (input, decimals) {
  const dp = decimals === undefined ? 2 : decimals;
  const fmt = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: dp });
  const check = () => { const v = App.parseNumber(input.value); input.classList.toggle("invalid", Number.isNaN(v)); };
  input.addEventListener("input", check);
  input.addEventListener("blur", () => { const v = App.parseNumber(input.value); if (v !== null && !Number.isNaN(v)) input.value = fmt.format(v); check(); });
  input.setNum = (v) => { input.value = v === null || v === undefined ? "" : fmt.format(v); check(); };
  input.getNum = () => App.parseNumber(input.value);
};

/* ------------------------------------------------------------------ API */
class ApiError extends Error {
  constructor(status, detail, errors) { super(detail); this.status = status; this.errors = errors || []; }
}
App.ApiError = ApiError;

App.api = async function (method, url, body) {
  const opts = { method, credentials: "same-origin", headers: { "X-Requested-With": "bondapp" } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opts); } catch (e) { throw new ApiError(0, "Cannot reach the server. Is run.py still running?"); }
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch (e) { data = { detail: text }; } }
  if (res.status === 401 && !url.startsWith("/api/auth/login")) {
    location.href = App.url("/login", { next: location.pathname + location.search, expired: 1 });
    throw new ApiError(401, "Session expired");
  }
  if (res.status === 403 && data && data.detail === "you must change your password first") {
    location.href = "/account?must=1";
    throw new ApiError(403, data.detail);
  }
  if (!res.ok) throw new ApiError(res.status, (data && data.detail) || res.statusText, data && data.errors);
  return data;
};

/** Turn a 422 error list into readable lines; optionally mark fields by data-field="bond.isin". */
App.showErrors = function (msgEl, err, root) {
  App.$$(".invalid[data-field]", root).forEach((e) => e.classList.remove("invalid"));
  App.$$(".err[data-err]", root).forEach((e) => { e.textContent = ""; });
  const lines = [];
  (err.errors || []).forEach((e) => {
    const loc = (e.loc || []).filter((p) => p !== "body");
    const path = loc.join(".");
    const msg = String(e.msg || "").replace(/^Value error, /, "");
    const input = root && App.$('[data-field="' + path + '"]', root);
    const slot = root && App.$('[data-err="' + path + '"]', root);
    if (input) input.classList.add("invalid");
    // whole-record checks (loc = ["body","bond"] or ["body"]) need no field prefix
    const whole = loc.length <= 1;
    if (slot) slot.textContent = msg; else lines.push((!whole && !input ? App.label(path) + ": " : "") + msg);
  });
  App.message(msgEl, "bad", err.message || "Something went wrong", lines);
};
App.label = function (path) {
  const last = path.split(".").filter((p) => !/^\d+$/.test(p)).pop() || path;
  const s = last.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
};

/* ------------------------------------------------------------------ session & top bar */
App.RANK = { viewer: 1, editor: 2, admin: 3 };
App.can = (role) => App.user && App.RANK[App.user.role] >= App.RANK[role];

App.init = async function (opts) {
  opts = opts || {};
  let me;
  try { me = await App.api("GET", "/api/auth/me"); } catch (e) { return null; }
  App.user = me;
  if (me.must_change_password && location.pathname !== "/account") { location.href = "/account?must=1"; return null; }
  if (opts.role && !App.can(opts.role)) {
    App.$$("body > *:not(script)").forEach((el) => el.remove());
    const m = document.createElement("main");
    m.className = "narrow";
    m.innerHTML = '<div class="msg bad">You do not have permission to open this page (needs ' + App.esc(opts.role) +
      " role).</div><a href=\"/\">Back to companies</a>";
    document.body.prepend(m);
    App.topbar(opts.nav);        // keep the menu and Log out button
    return null;
  }
  App.topbar(opts.nav);
  return me;
};

App.topbar = function (active) {
  const bar = document.createElement("header");
  bar.className = "topbar";
  const links = [["funds", "/", "Companies"]];
  if (App.can("admin")) links.push(["users", "/users", "Users"]);
  links.push(["account", "/account", "My account"]);
  bar.innerHTML =
    '<a class="brand" href="/">Bond Amortization</a><nav>' +
    links.map(([k, href, t]) => '<a href="' + href + '"' + (k === active ? ' class="active"' : "") + ">" + t + "</a>").join("") +
    '</nav><div class="who"><span>' + App.esc(App.user.full_name || App.user.username) + " · " + App.esc(App.user.role) +
    '</span><button type="button" id="logout">Log out</button></div>';
  document.body.prepend(bar);
  App.$("#logout").addEventListener("click", async () => {
    try { await App.api("POST", "/api/auth/logout"); } finally { location.href = "/login"; }
  });
};

/* ------------------------------------------------------------------ tables */
/** Build a table body from rows and column specs: {key|get, label, type: money|money2|date|pct|text, cls} */
App.renderTable = function (table, columns, rows, footer) {
  const cell = (c, r) => {
    const v = c.get ? c.get(r) : r[c.key];
    if (c.html) return "<td" + (c.cls ? ' class="' + c.cls + '"' : "") + ">" + v + "</td>";
    let txt, n = false, neg = false;
    switch (c.type) {
      case "money": txt = App.money(v); n = true; neg = v < -0.5; break;
      case "money2": txt = App.money2(v); n = true; neg = v < 0; break;
      case "pct": txt = App.pct(v, c.dp); n = true; break;
      case "date": txt = App.date(v); break;
      case "int": txt = v === null || v === undefined ? "" : String(v); n = true; break;
      default: txt = App.esc(v);
    }
    const cls = [c.cls, n ? "n" : "", neg ? "neg" : ""].filter(Boolean).join(" ");
    const title = (c.type === "money" && typeof v === "number") ? ' title="' + App.money2(v) + '"' : "";
    return "<td" + (cls ? ' class="' + cls + '"' : "") + title + ">" + (c.type ? txt : txt) + "</td>";
  };
  const thead = table.querySelector("thead") || table.createTHead();
  if (!table.dataset.keepHead) {
    thead.innerHTML = "<tr>" + columns.map((c) => '<th class="' + [c.cls, ["money", "money2", "pct", "int"].includes(c.type) ? "n" : "", c.wrap ? "wrap" : ""].filter(Boolean).join(" ") + '">' + App.esc(c.label) + "</th>").join("") + "</tr>";
  }
  const tbody = table.tBodies[0] || table.createTBody();
  tbody.innerHTML = rows.length ? rows.map((r) => "<tr>" + columns.map((c) => cell(c, r)).join("") + "</tr>").join("")
    : '<tr><td class="empty" colspan="' + columns.length + '">Nothing to show</td></tr>';
  // Footer: an object of totals keyed by column key; footer._label goes in the first column.
  let tfoot = table.tFoot;
  if (footer) {
    if (!tfoot) tfoot = table.createTFoot();
    tfoot.innerHTML = "<tr>" + columns.map((c, i) => {
      if (i === 0 && footer._label !== undefined) return '<td class="' + (c.cls || "") + '">' + App.esc(footer._label) + "</td>";
      if (c.key && footer[c.key] !== undefined) return cell({ ...c, get: null, html: false }, footer);
      return '<td class="' + (c.cls || "") + '"></td>';
    }).join("") + "</tr>";
  } else if (tfoot) tfoot.remove();
};

App.sum = (rows, key) => rows.reduce((s, r) => s + (r[key] || 0), 0);
