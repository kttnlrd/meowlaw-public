// MeowLaw UI. No build step: plain ES2020 against the REST API.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

// UI strings live in ui/i18n/<locale>.json (locale from meowlaw.toml
// [branding] locale). English is loaded first as the fallback.
let STRINGS = {};
function t(key, vars = {}) {
  const s = STRINGS[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`));
}
async function loadStrings(locale) {
  const get = async (l) => { try { const r = await fetch(`/i18n/${l}.json`); return r.ok ? r.json() : {}; } catch (_) { return {}; } };
  STRINGS = await get("en");
  if (locale && locale !== "en") Object.assign(STRINGS, await get(locale));
  // Static markup: data-i18n (text), data-i18n-ph (placeholder), data-i18n-label (aria-label / title).
  $$("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $$("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  $$("[data-i18n-label]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.i18nLabel)); el.title = t(el.dataset.i18nLabel); });
  document.documentElement.lang = locale || "en";
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Escape, then render the only inline Markdown the ingesters keep: **bold** and *italic*.
const md = (s) => esc(s).replace(/\*\*\*([^*\n]+)\*\*\*/g, "<strong><em>$1</em></strong>")
  .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
const nodeHref = (id) => "#/node/" + encodeURIComponent(id);
const kindLabel = (k) => (STRINGS["kind_" + k] ? t("kind_" + k) : k || "");
const kindBadge = (k) => k ? `<span class="badge badge--kind badge--${esc(k)}">${esc(kindLabel(k))}</span>` : "";

function getToken() { try { return localStorage.getItem("meowlaw-token") || ""; } catch (_) { return ""; } }

async function api(path, params = {}, retry = true) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
  const token = getToken();
  const r = await fetch(path + (qs.toString() ? "?" + qs : ""), token ? { headers: { Authorization: "Bearer " + token } } : {});
  // Protected API: ask once for the token and retry.
  if (r.status === 401 && retry) {
    const given = prompt(t("token_prompt"));
    if (given) { try { localStorage.setItem("meowlaw-token", given.trim()); } catch (_) {} return api(path, params, false); }
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

// Each navigation gets a number; async renders check it so a slow response
// cannot overwrite the page the user has since moved to.
let routeSeq = 0;
const current = (seq) => seq === routeSeq;

// ------------------------------------------------------------ preferences
// Appearance lives in this browser only: theme, accent and text size.
const ACCENT_PRESETS = ["#c6f432", "#ff7ac8", "#3fd6b8", "#ffb347", "#6aa8ff", "#a78bfa", "#f87171", "#f5f5f5"];
const DEFAULT_PREFS = { theme: "light", accent: "", size: "normal" };
let PREFS = { ...DEFAULT_PREFS };
function loadPrefs() {
  try { PREFS = { ...DEFAULT_PREFS, ...(JSON.parse(localStorage.getItem("meowlaw-prefs") || "{}") || {}) }; } catch (_) {}
}
function savePrefs() { try { localStorage.setItem("meowlaw-prefs", JSON.stringify(PREFS)); } catch (_) {} }

// Dark text on light accents, light text on dark ones.
function contrastFor(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return "";
  const n = parseInt(m[1], 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  return lum > 0.55 ? "#120d17" : "#fbf6ee";
}

function applyPrefs() {
  const d = document.documentElement;
  let theme = PREFS.theme;
  if (theme === "system") theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  d.dataset.theme = theme;
  if (PREFS.accent) {
    d.style.setProperty("--primary", PREFS.accent);
    d.style.setProperty("--primary-contrast", contrastFor(PREFS.accent));
    // In-text links follow the accent only where it stays readable on the page.
    const readable = theme === "dark" ? contrastFor(PREFS.accent) === "#120d17" : contrastFor(PREFS.accent) !== "#120d17";
    if (readable) d.style.setProperty("--text-link", PREFS.accent); else d.style.removeProperty("--text-link");
  } else { d.style.removeProperty("--primary"); d.style.removeProperty("--primary-contrast"); d.style.removeProperty("--text-link"); }
  if (PREFS.size === "large") d.dataset.size = "large"; else delete d.dataset.size;
  // Reflect in the settings panel.
  $$("#theme-seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeChoice === PREFS.theme)));
  $$("#size-seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.sizeChoice === PREFS.size)));
  const cur = PREFS.accent || getComputedStyle(d).getPropertyValue("--primary").trim();
  $("#accent-swatch").style.background = cur;
  $("#accent-hex").textContent = cur;
  if (/^#[0-9a-f]{6}$/i.test(cur)) $("#accent-input").value = cur;
  $$("#accent-presets button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.accent.toLowerCase() === cur.toLowerCase())));
}

function initSettings() {
  loadPrefs();
  $("#accent-presets").innerHTML = ACCENT_PRESETS.map((c) => `<button type="button" data-accent="${c}" title="${c}" aria-label="${c}" style="background:${c}"></button>`).join("");
  applyPrefs();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (PREFS.theme === "system") applyPrefs(); });
  $("#theme-seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; PREFS.theme = b.dataset.themeChoice; savePrefs(); applyPrefs(); });
  $("#size-seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; PREFS.size = b.dataset.sizeChoice; savePrefs(); applyPrefs(); });
  $("#accent-presets").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; PREFS.accent = b.dataset.accent; savePrefs(); applyPrefs(); });
  $("#accent-input").addEventListener("input", (e) => { PREFS.accent = e.target.value; savePrefs(); applyPrefs(); });
  $("#prefs-reset").addEventListener("click", () => { PREFS = { ...DEFAULT_PREFS }; savePrefs(); applyPrefs(); });
  $("#settings-btn").addEventListener("click", () => openSettings(true));
  $("#settings-close").addEventListener("click", () => openSettings(false));
  $("#settings").addEventListener("click", (e) => { if (e.target === e.currentTarget) openSettings(false); });
  $$("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    const txt = $(b.dataset.copy).textContent;
    try { await navigator.clipboard.writeText(txt); } catch (_) {}
    const was = b.textContent; b.textContent = t("copied"); setTimeout(() => { b.textContent = was; }, 1500);
  }));
}
function openSettings(open) {
  $("#settings").hidden = !open;
  document.body.style.overflow = open ? "hidden" : "";
  if (open) { refreshAccess(); $("#settings-close").focus(); }
}

// ----------------------------------------------------------------- branding
let BRAND = {};
async function initBranding() {
  try {
    const b = await api("/api/branding");
    BRAND = b;
    document.title = b.name;
    $("#brand-name").textContent = b.name;
    $("#brand-footer").textContent = b.footer || "";
    if (b.logo) { const img = $("#brand-logo"); img.src = b.logo; img.hidden = false; }
    if (Array.isArray(b.source_groups) && b.source_groups.length) sourceGroups = b.source_groups;
    await loadStrings(b.locale);
    // Link to the ingest page only when the server has it on, so a read-only
    // instance never logs a 404 for a page it does not serve.
    if (b.allow_ingest) {
      fetch("/api/admin/status").then((r) => { if (r.ok) $("#admin-link").hidden = false; }).catch(() => {});
    }
    // MCP snippets for the settings panel.
    const url = location.origin + "/mcp";
    const name = (b.mcp_name || "meowlaw").replace(/[^a-z0-9_-]/gi, "") || "meowlaw";
    $("#mcp-url").textContent = url;
    $("#mcp-code").textContent = `claude mcp add --transport http ${name} ${url}` + (b.mcp_token_required ? ` --header "Authorization: Bearer <token>"` : "");
    const json = { mcpServers: { [name]: { type: "http", url } } };
    if (b.mcp_token_required) json.mcpServers[name].headers = { Authorization: "Bearer <token>" };
    $("#mcp-json").textContent = JSON.stringify(json, null, 2);
    $("#mcp-note").textContent = t(b.mcp_token_required ? "mcp_note_token" : "mcp_note_open");
    api("/api/info").then((info) => {
      $("#about-line").textContent = t("about_line", { name: b.name, version: info.version ? "v" + info.version : "", corpora: (info.corpora || []).length });
    }).catch(() => {});
  } catch (_) { await loadStrings("en"); }
  refreshAccess();
}

// Access card in settings: sign in with a token or forget it.
function refreshAccess() {
  const btn = $("#token-button");
  const has = !!getToken();
  const restricted = BRAND.access_control || BRAND.protect_api;
  $("#access-hint").textContent = has ? t("access_signed_in") : restricted ? t("access_token_hint") : t("access_open");
  btn.textContent = t(has ? "token_forget" : "token_use");
  btn.hidden = !(restricted || has);
  btn.onclick = () => {
    try {
      if (has) localStorage.removeItem("meowlaw-token");
      else { const v = prompt(t("token_prompt")); if (!v) return; localStorage.setItem("meowlaw-token", v.trim()); }
    } catch (_) {}
    location.reload();
  };
}

// ---------------------------------------------------------------- sidebar
let corporaCache = [];

// Source groups for the drawer come from meowlaw.toml ([[source_groups]],
// served with the branding). A corpus whose metadata sets `group` goes to that
// group; otherwise to the first group listing its kind; otherwise "Other".
let sourceGroups = [
  { label: "Legislation", kinds: ["legislation", "instrument"] },
  { label: "Case law", kinds: ["case"] },
  { label: "Regulatory resources", kinds: ["guidance", "treaty", "rule_set", "secondary"] },
];

function groupOf(c) {
  const explicit = c.meta && typeof c.meta.group === "string" && c.meta.group.trim();
  if (explicit) return explicit;
  const g = sourceGroups.find((g) => (g.kinds || []).includes(c.kind));
  return g ? g.label : t("group_other");
}

function grouped(corpora) {
  const order = sourceGroups.map((g) => g.label);
  const by = new Map();
  for (const c of corpora) {
    const label = groupOf(c);
    if (!by.has(label)) by.set(label, []);
    by.get(label).push(c);
  }
  // Configured groups first, in order; then any named only by corpus metadata.
  const labels = [...order.filter((l) => by.has(l)), ...[...by.keys()].filter((l) => !order.includes(l)).sort()];
  return labels.map((l) => [l, by.get(l)]);
}

const lang = (c) => (c.meta && c.meta.language ? ` (${c.meta.language})` : "");
// Short name for breadcrumbs and citations: the first alias when it is
// shorter than the title ("ITAA 1997" for the Income Tax Assessment Act 1997).
function shortName(c) {
  if (!c) return "";
  const a = (c.aliases || []).find((x) => x && x.length < (c.title || "").length);
  return a || c.title || c.id || "";
}
const corpusById = (id) => corporaCache.find((x) => x.id === id);

async function loadCorpora() {
  const all = (await api("/api/corpora")).corpora;
  corporaCache = all;
  // Earlier compilations stay reachable through each provision's versions.
  const corpora = all.filter((c) => !c.superseded);
  const groups = grouped(corpora);
  $("#corpora").innerHTML = corpora.length ? groups.map(([label, items]) => `
    <section><h2>${esc(label)}</h2><ul>${items.map((c) => `
      <li><a href="#/corpus/${encodeURIComponent(c.id)}" data-id="${esc(c.id)}" title="${esc(c.title)}">${kindBadge(c.kind)}<span class="t">${esc(c.title + lang(c))}</span>${c.nodes ? `<span class="k">${esc(String(c.nodes))}</span>` : ""}</a></li>`).join("")}</ul></section>`).join("")
    : `<p class="empty">${t("nothing_ingested")}</p>`;
  setPill(null);
  let saved = "";
  try { saved = localStorage.getItem("meowlaw-source") || ""; } catch (_) {}
  if (saved && corpora.some((c) => c.id === saved) && !treeState.corpus) await selectSource(saved);
}

// The pill above the drawer names the open source (badge + title) or offers
// the list of all sources. Clicking it returns to the list when a source tree
// is open, and collapses/expands the list when it is not.
let listCollapsed = false;
function syncPill() {
  $("#source-pill").setAttribute("aria-expanded", String(!!treeState.corpus || !listCollapsed));
}

function setPill(c) {
  const badge = $("#pill-badge");
  if (c) {
    badge.className = `badge badge--kind badge--${c.kind}`; badge.textContent = kindLabel(c.kind); badge.hidden = false;
    $("#pill-title").textContent = c.title + lang(c);
  } else {
    badge.hidden = true;
    $("#pill-title").textContent = t("all_sources");
  }
  syncPill();
}

// ------------------------------------------------------------------- tree
// The drawer shows one source's navigation tree (JSON from /api/tree), built
// lazily: a node's children are rendered the first time it is expanded.
const treeCache = new Map();
const treeState = { corpus: "", byId: new Map(), parent: new Map(), active: "" };

async function selectSource(id) {
  try { localStorage.setItem("meowlaw-source", id); } catch (_) {}
  const showTree = !!id;
  const body = $(".sb-body");
  if (body) body.hidden = false;
  $("#tree").hidden = !showTree;
  $("#tree-filter").hidden = !showTree;
  $("#corpora").hidden = showTree || listCollapsed;
  if (!showTree) { treeState.corpus = ""; setPill(null); return; }
  if (treeState.corpus === id) { setPill(corporaCache.find((c) => c.id === id)); return; }
  treeState.corpus = id;
  setPill(corporaCache.find((c) => c.id === id) || { id, title: id, kind: "" });
  $("#tree").innerHTML = `<p class="empty">${esc(t("loading"))}</p>`;
  $("#tree-filter").value = "";
  let tree = treeCache.get(id);
  if (!tree) {
    try { tree = await api("/api/tree", { corpus: id }); } catch (e) { $("#tree").innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
    treeCache.set(id, tree);
  }
  if (treeState.corpus !== id) return;
  treeState.byId = new Map(); treeState.parent = new Map();
  const index = (nodes, parent) => nodes.forEach((n) => {
    treeState.byId.set(n.id, n); treeState.parent.set(n.id, parent);
    if (n.children) index(n.children, n.id);
  });
  index(tree.children || [], null);
  const root = $("#tree");
  // A source without a tree is one document: its title opens it.
  const titleHref = (tree.children || []).length ? `#/corpus/${encodeURIComponent(id)}` : nodeHref(id);
  root.innerHTML = `<a class="tree-title" href="${titleHref}">${esc(tree.title)}</a>`;
  const list = document.createElement("div");
  list.className = "tlist";
  (tree.children || []).forEach((n) => list.appendChild(treeRow(n)));
  root.appendChild(list);
  if (treeState.active) revealInTree(treeState.active);
}

function rowLabel(n) {
  const num = n.number ? (n.kind === "provision" ? n.number : `${n.label ? n.label + " " : ""}${n.number}`) : "";
  return `${num ? `<b>${esc(num)}</b>` : ""}${esc(n.title || (num ? "" : n.id))}`;
}

function treeRow(n) {
  const wrap = document.createElement("div");
  wrap.className = "tnode";
  wrap.dataset.id = n.id;
  const kids = n.children && n.children.length;
  // A node with children is a container (Part, Division, Subdivision): clicking
  // it expands the branch. Only leaves (sections, paragraphs) navigate.
  const label = kids
    ? `<button class="tlabel" type="button" role="treeitem" aria-expanded="false" title="${esc(n.title || n.id)}">${rowLabel(n)}</button>`
    : `<a href="${nodeHref(n.id)}" title="${esc(n.title || n.id)}" role="treeitem">${rowLabel(n)}</a>`;
  wrap.innerHTML = `<div class="row${n.id === treeState.active ? " active" : ""}">
    <button class="tg" type="button" tabindex="-1" aria-label="${esc(t("expand"))}"${kids ? "" : " disabled"}>${kids ? "▶" : ""}</button>${label}</div>`;
  if (kids) {
    const toggle = () => toggleNode(wrap, n);
    wrap.querySelector(".tg").addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
    wrap.querySelector(".tlabel").addEventListener("click", toggle);
  }
  return wrap;
}

function toggleNode(wrap, n, open) {
  let box = wrap.querySelector(":scope > .kids");
  const isOpen = !!box && !box.hidden;
  const want = open === undefined ? !isOpen : open;
  if (want && !box) {
    box = document.createElement("div");
    box.className = "kids";
    n.children.forEach((c) => box.appendChild(treeRow(c)));
    wrap.appendChild(box);
  }
  if (box) box.hidden = !want;
  const row = wrap.querySelector(":scope > .row");
  row.setAttribute("aria-expanded", String(want));
  row.classList.toggle("open", want);
  const lab = row.querySelector(".tlabel");
  if (lab) lab.setAttribute("aria-expanded", String(want));
  row.querySelector(".tg").textContent = want ? "▼" : "▶";
}

// Leaves in tree order: the only nodes a reader can open, so "next" and
// j/k step between them and never land on a Part or Division page.
function leafIds() {
  return [...treeState.byId.entries()]
    .filter(([, n]) => !(n.children && n.children.length))
    .map(([id]) => id);
}

// The leaf after `id` (delta 1) or before it (delta -1), within the open source.
// `byId` is filled by a pre-order walk, so its key order is document order;
// a container (Part, Division) steps to the first leaf after it or the last
// leaf before it, never by comparing id strings.
// An id below the tree's depth (a subsection, a paragraph) steps from its
// nearest ancestor in the tree; backwards that ancestor itself is "previous".
function leafNear(id, delta = 1) {
  const all = [...treeState.byId.keys()];
  const isLeaf = (x) => { const n = treeState.byId.get(x); return !!n && !(n.children && n.children.length); };
  let cur = id;
  while (cur && !treeState.byId.has(cur)) {
    const cut = cur.lastIndexOf("/");
    cur = cut > 0 ? cur.slice(0, cut) : "";
  }
  if (!cur) return "";
  if (cur !== id && delta < 0 && isLeaf(cur)) return cur;
  for (let i = all.indexOf(cur) + delta; i >= 0 && i < all.length; i += delta) {
    if (isLeaf(all[i])) return all[i];
  }
  return "";
}

// The first leaf inside container `id`, by the tree; "" when `id` is not a
// container the tree knows.
function firstLeafUnder(id) {
  let n = treeState.byId.get(id);
  while (n && n.children && n.children.length) n = n.children[0];
  return n && n.id !== id ? n.id : "";
}

// Expand the ancestors of `id` (or its nearest ancestor present in the tree),
// highlight it and scroll it into view.
function revealInTree(id) {
  treeState.active = id;
  if (!treeState.byId.has(id)) return;
  const chain = [];
  for (let cur = id; cur; cur = treeState.parent.get(cur)) chain.unshift(cur);
  let el = null;
  for (const cid of chain) {
    el = $$("#tree .tnode").find((x) => x.dataset.id === cid);
    if (!el) return;
    if (cid !== id) toggleNode(el, treeState.byId.get(cid), true);
  }
  $$("#tree .row.active").forEach((r) => r.classList.remove("active"));
  if (el) {
    const row = el.querySelector(":scope > .row");
    row.classList.add("active");
    row.scrollIntoView({ block: "nearest" });
  }
}

// Filter: flat list of matching tree entries (number or title).
function filterTree(q) {
  const list = $("#tree .tlist");
  let res = $("#tree .tresults");
  if (!q) { if (res) res.remove(); if (list) list.hidden = false; return; }
  const needle = q.toLowerCase();
  const hits = [];
  for (const n of treeState.byId.values()) {
    const hay = `${n.label || ""} ${n.number || ""} ${n.title || ""}`.toLowerCase();
    if (hay.includes(needle)) hits.push(n);
    if (hits.length >= 200) break;
  }
  if (!res) { res = document.createElement("div"); res.className = "tresults"; $("#tree").appendChild(res); }
  if (list) list.hidden = true;
  const isBranch = (n) => !!(n.children && n.children.length);
  res.innerHTML = hits.length ? hits.map((n) => `<div class="row"><span class="tg"></span>${isBranch(n)
      ? `<button class="tlabel" type="button" data-id="${esc(n.id)}">${rowLabel(n)}</button>`
      : `<a href="${nodeHref(n.id)}">${rowLabel(n)}</a>`}</div>`).join("")
    : `<p class="empty">${esc(t("no_matches"))}</p>`;
  // A container hit opens its branch in the tree instead of a contents page.
  $$("#tree .tresults button.tlabel").forEach((b) => b.addEventListener("click", () => {
    $("#tree-filter").value = "";
    filterTree("");
    revealInTree(b.dataset.id);
  }));
}

// Keep the drawer on the source being read and reveal the open node.
async function markActiveCorpus(id, path = []) {
  $$(".sources-card a").forEach((a) => a.classList.toggle("active", a.dataset.id === id));
  if (!id) return;
  if (treeState.corpus !== id) await selectSource(id);
  const target = [...path].reverse().find((p) => treeState.byId.has(p));
  if (target) revealInTree(target);
}

// ------------------------------------------------------------------ pins
// Provisions the reader keeps to hand, in this browser.
let PINS = [];
function loadPins() { try { PINS = JSON.parse(localStorage.getItem("meowlaw-pins") || "[]") || []; } catch (_) { PINS = []; } }
function savePins() { try { localStorage.setItem("meowlaw-pins", JSON.stringify(PINS)); } catch (_) {} }
const isPinned = (id) => PINS.some((p) => p.id === id);
function togglePin(n) {
  if (isPinned(n.id)) PINS = PINS.filter((p) => p.id !== n.id);
  else PINS.push({ id: n.id, title: n.heading, corpus: shortName(n.corpus) || (n.corpus && n.corpus.title) || "" });
  savePins(); renderPins();
  $$(".pin-btn").forEach((b) => { b.setAttribute("aria-pressed", String(isPinned(n.id))); b.lastChild.textContent = " " + t(isPinned(n.id) ? "unpin" : "pin"); });
}
function renderPins() {
  const box = $("#pins");
  if (!PINS.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = `<h2>${esc(t("pinned"))}</h2><ul>${PINS.map((p) => `<li><a href="${nodeHref(p.id)}" title="${esc(p.title)}"><span class="pc mono">${esc(p.corpus)}</span><span class="pt">${esc(p.title)}</span></a><button type="button" class="unpin" data-id="${esc(p.id)}" aria-label="${esc(t("unpin"))}">×</button></li>`).join("")}</ul>`;
  $$("#pins .unpin").forEach((b) => b.addEventListener("click", () => { PINS = PINS.filter((p) => p.id !== b.dataset.id); savePins(); renderPins(); $$(".pin-btn").forEach((x) => x.setAttribute("aria-pressed", "false")); }));
}

function setDrawer(open) {
  document.body.classList.toggle("drawer-open", open);
  $("#scrim").hidden = !open;
  $("#drawer-toggle").setAttribute("aria-expanded", String(open));
}

// ----------------------------------------------------------------- search
const searchIcon = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>`;
function searchForm(q = "") {
  const ph = BRAND.search_hint || t("search_placeholder");
  return `<form class="search-form" role="search">${searchIcon}
    <input name="q" type="search" value="${esc(q)}" placeholder="${esc(ph)}" aria-label="${esc(t("search"))}" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
    <kbd>/</kbd><button type="submit">${esc(t("search"))}</button></form>`;
}
const disclaimer = () => BRAND.footer ? `<p class="disclaimer">${esc(BRAND.footer)}</p>` : "";

let lastSearch = { kind: "", corpus: "" };
function gotoSearch(q, extra = {}) {
  const p = new URLSearchParams();
  if (q) p.set("q", q);
  const kind = "kind" in extra ? extra.kind : lastSearch.kind;
  const corpus = "corpus" in extra ? extra.corpus : lastSearch.corpus;
  if (kind) p.set("kind", kind);
  if (corpus) p.set("corpus", corpus);
  location.hash = "#/search" + (p.toString() ? "?" + p : "");
}

async function showSearch(params) {
  const q = params.get("q") || "";
  const kind = params.get("kind") || "";
  const corpus = params.get("corpus") || "";
  lastSearch = { kind, corpus };
  const main = $("#main");
  const seq = routeSeq;
  main.innerHTML = `<div class="search-page">${searchForm(q)}${q ? `<p class="empty">${esc(t("searching"))}</p>` : ""}</div>`;
  $("#aside").innerHTML = filtersCard(kind, corpus);
  bindFilters();
  const input = $(".search-form input", main);
  if (!q) { input.focus(); return; }
  let r;
  try { r = await api("/api/search", { q, kind, corpus, limit: 25 }); }
  catch (e) { if (current(seq)) $(".search-page").innerHTML = searchForm(q) + `<p class="empty">${esc(e.message)}</p>`; return; }
  if (!current(seq)) return;
  const cites = (r.citations || []).map((c) => `
    <div class="card"><h4>${esc(t("citation"))}: <span class="mono">${esc(c.raw)}</span></h4>${c.targets.length
      ? c.targets.map((x) => `<a href="${nodeHref(x)}">${esc(x)}</a>`).join(" · ")
      : `<span class="badge">${esc(t("not_loaded"))}</span>`}</div>`).join("");
  const defs = (r.definitions || []).slice(0, 4).map((d) => `
    <div class="card"><h4>“${esc(d.term)}” <span class="badge">${esc(t("definition"))}</span></h4>
      <div>${md(d.text)}</div>
      <div class="meta"><a href="${nodeHref(d.source)}">${esc(d.source)}</a></div></div>`).join("");
  const hits = r.results.map((h) => `
    <a class="hit" href="${nodeHref(h.id)}">
      <div class="hit__head">${kindBadge(h.corpus_kind)}<span class="hit__cite" title="${esc(h.corpus_title)}">${esc(shortName(corpusById(h.corpus)) || h.corpus_title)}</span><span class="hit__title">${esc(h.heading)}</span></div>
      ${h.path.length ? `<div class="hit__path">${h.path.map(esc).join(" › ")}</div>` : ""}
      <p>${highlight(h.snippet)}</p>
      <div class="why">${h.matched_by.map((m) => `<span class="badge badge--why ${esc(m)}">${esc(m)}</span>`).join("")}</div>
    </a>`).join("");
  const n = r.results.length;
  $(".search-page").innerHTML = `${searchForm(q)}
    <div class="results-head"><h2>${esc(n === 1 ? t("results_one") : t("results_n", { n }))}</h2><span class="q">${esc(t("results_for", { q }))}</span></div>
    ${cites}${defs}<div class="results">${hits || `<p class="empty">${esc(t("no_results"))}</p>`}</div>${disclaimer()}`;
}

function filtersCard(kind, corpus) {
  const kinds = sourceGroups.map((g) => `<option value="${esc((g.kinds || []).join(","))}"${(g.kinds || []).join(",") === kind ? " selected" : ""}>${esc(g.label)}</option>`).join("");
  const sources = grouped(corporaCache.filter((c) => !c.superseded)).map(([label, items]) =>
    `<optgroup label="${esc(label)}">${items.map((c) => `<option value="${esc(c.id)}"${c.id === corpus ? " selected" : ""}>${esc(c.title + lang(c))}</option>`).join("")}</optgroup>`).join("");
  return `<div class="filters"><h3>${esc(t("filters"))}</h3>
    <label>${esc(t("source_type"))}<select id="f-kind"><option value="">${esc(t("all_sources"))}</option>${kinds}</select></label>
    <label>${esc(t("source"))}<select id="f-corpus"><option value="">${esc(t("all_sources"))}</option>${sources}</select></label></div>`;
}
function bindFilters() {
  const q = () => ($(".search-form input") || {}).value || "";
  const k = $("#f-kind"), c = $("#f-corpus");
  if (k) k.addEventListener("change", () => gotoSearch(q().trim(), { kind: k.value, corpus: "" }));
  if (c) c.addEventListener("change", () => gotoSearch(q().trim(), { corpus: c.value, kind: "" }));
}

// Snippets mark matches with «»; turn them into <mark> after escaping.
function highlight(s) {
  return md(s).replace(/«/g, "<mark>").replace(/»/g, "</mark>");
}

// ----------------------------------------------------------------- reader
const linkIcon = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;
function readerActions(n) {
  const links = (n.corpus && n.corpus.links || []).slice();
  const c = n.corpus || {};
  if (c.source_url && !links.some((l) => l.url === c.source_url)) links.push({ label: t("source"), url: c.source_url });
  const ext = links.filter((l) => /^https?:\/\//i.test(l.url)).map((l) => `<a class="btn btn--sm" href="${esc(l.url)}" rel="noopener noreferrer" target="_blank">${esc(l.label)} ↗</a>`).join("");
  const pinned = isPinned(n.id);
  return `<div class="reader-actions"><button class="btn btn--sm copy-link" type="button">${linkIcon} ${esc(t("copy_link"))}</button>
    <button class="btn btn--sm pin-btn" type="button" aria-pressed="${pinned}">${pinIcon} ${esc(t(pinned ? "unpin" : "pin"))}</button><span class="next-slot"></span>${ext}</div>`;
}

// "Next" continues to the following provision in the open source. It is filled
// after the tree is ready, because that is what defines the reading order.
function fillNext(main, n) {
  const slot = $(".next-slot", main);
  if (!slot) return;
  const nextId = leafNear(n.id, 1);
  if (!nextId) { slot.remove(); return; }
  const nx = treeState.byId.get(nextId) || {};
  const num = nx.number ? `${nx.label ? nx.label + " " : ""}${nx.number}` : "";
  const href = nodeHref(nextId);
  slot.innerHTML = `<a class="btn btn--sm next-link" href="${href}" title="${esc(nx.title || nextId)}">${esc(t("next"))}${num ? ` <span class="mono">${esc(num)}</span>` : ""} →</a>`;
}
const pinIcon = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5M9 3h6l1 7 3 3H5l3-3z"/></svg>`;
function bindCopyLink(main, n) {
  const pb = $(".pin-btn", main);
  if (pb && n) pb.addEventListener("click", () => togglePin(n));
  const b = $(".copy-link", main);
  if (!b) return;
  b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(location.href); } catch (_) {}
    const was = b.innerHTML; b.textContent = t("copied"); setTimeout(() => { b.innerHTML = was; }, 1500);
  });
}
function titleHtml(n) {
  if (n.kind === "paragraph" && !n.label && n.number) return `<span class="num">[${esc(n.number)}]</span>`;
  const num = n.number ? `<span class="num">${esc(n.number)}</span>` : "";
  const label = n.label && !n.title && !n.number ? esc(n.label) : "";
  return `${num}${esc(n.title || label || (num ? "" : n.heading))}`;
}

async function showNode(id, item) {
  const main = $("#main");
  main.innerHTML = `<p class="empty">${esc(t("loading"))}</p>`;
  const seq = routeSeq;
  let n;
  try { n = await api("/api/node", { id }); }
  catch (e) { if (current(seq)) main.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  if (!current(seq)) return;
  if (n.definition) { return showDefinition(n.definition); }
  const treeReady = markActiveCorpus(n.corpus && n.corpus.id, [...n.path.map((p) => p.id), n.id]);
  const c = n.corpus || {};
  // Containers (Part, Division, Subdivision) have no page of their own: the
  // drawer shows the hierarchy, so the reader lands on the first provision
  // inside. A source's root that only holds containers goes to the source page.
  if (n.kind === "container" && n.children.length) {
    try { await treeReady; } catch (_) {}
    if (!current(seq)) return;
    location.replace(nodeHref(firstLeafUnder(id) || n.children[0].id));
    return;
  }
  if (n.kind === "root" && !n.text && n.children.length && n.children.every((ch) => ch.kind !== "paragraph")) {
    location.replace(`#/corpus/${encodeURIComponent(c.id || id)}`);
    return;
  }
  const full = corpusById(c.id) || c;
  // Ancestors are containers, so the crumbs name them without linking.
  const crumbs = [`<a href="#/corpus/${encodeURIComponent(c.id || "")}" title="${esc(c.title || "")}">${esc(shortName(full))}</a>`,
    ...n.path.slice(1).map((p) => `<span>${esc(p.heading)}</span>`)].join(`<span class="sep">/</span>`);
  main.innerHTML = `
    <div class="reader">
      ${readerActions(n)}
      <div class="crumbs">${kindBadge(c.kind)}${crumbs}</div>
      <h1>${titleHtml(n)}</h1>
      ${renderVersions(n)}
      ${n.summary ? `<div class="card summary"><h4>${esc(t("summary"))} <span class="badge">${esc(n.summary.source || "")}</span></h4><div class="text">${renderText({ text: n.summary.text, references: n.summary.references, definitions_in_scope: n.definitions_in_scope })}</div></div>` : ""}
      <div class="text">${renderText(n)}</div>
      ${n.truncated ? `<p class="empty text-trunc">${esc(t("text_truncated"))}</p>` : ""}
      ${renderChildren(n.children)}
      ${disclaimer()}
    </div>`;
  bindTerms(n);
  bindCopyLink(main, n);
  treeReady.finally(() => { if (current(seq)) fillNext(main, n); });
  showRelated(id, n.summary && n.summary.id, full);
  main.focus();
  const row = item && document.getElementById("item-" + itemKey(item));
  if (row) { row.classList.add("target"); row.scrollIntoView({ block: "center" }); }
}

// Paragraphs render inline, numbered and individually linkable; other
// children (Parts, sections, headings) render as a contents list.
function renderChildren(children) {
  if (!children.length) return "";
  const paras = children.filter((c) => c.kind === "paragraph");
  const others = children.filter((c) => c.kind !== "paragraph");
  let out = "";
  if (paras.length) {
    out += `<div class="paras">${paras.map((p) => `
      <div class="para"><a class="pnum" href="${nodeHref(p.id)}" title="${esc(p.id)}">${esc(p.number || "")}</a>
        <div class="text">${tableize(md(p.text || ""))}</div></div>`).join("")}</div>`;
  }
  if (others.length) {
    out += `<h3>${esc(t("contents"))}</h3><ul class="children">${others.map((ch) => `<li><a href="${nodeHref(ch.id)}">${esc(ch.heading)}</a></li>`).join("")}</ul>`;
  }
  return out;
}

// Other versions of this provision: language versions and compilations at
// other dates. Same-language versions whose text differs can be compared.
function renderVersions(n) {
  const vs = n.versions || [];
  if (!vs.length) return "";
  const c = n.corpus || {};
  const here = [c.language, c.as_at && t("as_at", { date: c.as_at })].filter(Boolean).join(", ");
  const sorted = vs.slice().sort((a, b) => String(b.as_at || "").localeCompare(String(a.as_at || "")) || String(a.language || "").localeCompare(String(b.language || "")));
  return `<div class="versions"><span class="k">${esc(t("versions"))}${here ? ` (${esc(t("this_version", { v: here }))})` : ""}:</span> ${sorted.map((v) => {
    const label = esc(v.label || v.corpus);
    const changed = v.text_differs && (v.language || "") === (c.language || "");
    return `<a class="button" href="${nodeHref(v.id)}">${label}</a>${changed
      ? ` <a class="compare" href="#/compare?a=${encodeURIComponent(v.id)}&b=${encodeURIComponent(n.id)}">${esc(t("compare"))}</a>` : ""}`;
  }).join(" ")}</div>`;
}

// Word-level comparison of two versions of a provision (a = older).
async function showCompare(params) {
  const main = $("#main");
  const seq = routeSeq;
  main.innerHTML = `<p class="empty">${esc(t("loading"))}</p>`;
  let a, b;
  try { [a, b] = await Promise.all([api("/api/node", { id: params.get("a") }), api("/api/node", { id: params.get("b") })]); }
  catch (e) { if (current(seq)) main.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  if (!current(seq)) return;
  if (String(a.corpus.as_at || "") > String(b.corpus.as_at || "")) [a, b] = [b, a];
  const name = (x) => esc([x.corpus.language, x.corpus.as_at && t("as_at", { date: x.corpus.as_at })].filter(Boolean).join(", ") || x.corpus.id);
  main.innerHTML = `<div class="reader">
    <h1>${titleHtml(b)}</h1>
    <p class="corpus-meta">${esc(t("compare_title"))} <a href="${nodeHref(a.id)}">${name(a)}</a> → <a href="${nodeHref(b.id)}">${name(b)}</a>
      <span class="legend"><del>${esc(t("removed"))}</del> <ins>${esc(t("added"))}</ins></span></p>
    <div class="text diff">${wordDiff(a.text || "", b.text || "")}</div></div>`;
  $("#aside").innerHTML = "";
  main.focus();
}

function wordDiff(x, y) {
  const A = x.split(/(\s+)/), B = y.split(/(\s+)/);
  const n = A.length, m = B.length;
  if (n * m > 6e6) return `<p class="empty">${esc(t("compare_too_long"))}</p>`;
  // LCS table, row-major, lengths of suffixes.
  const L = new Uint32Array((n + 1) * (m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    L[i * (m + 1) + j] = A[i] === B[j] ? L[(i + 1) * (m + 1) + j + 1] + 1 : Math.max(L[(i + 1) * (m + 1) + j], L[i * (m + 1) + j + 1]);
  let out = "", i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { out += esc(A[i]); i++; j++; }
    else if (L[(i + 1) * (m + 1) + j] >= L[i * (m + 1) + j + 1]) { out += `<del>${esc(A[i++])}</del>`; }
    else { out += `<ins>${esc(B[j++])}</ins>`; }
  }
  while (i < n) out += `<del>${esc(A[i++])}</del>`;
  while (j < m) out += `<ins>${esc(B[j++])}</ins>`;
  return out.replace(/<\/del><del>/g, "").replace(/<\/ins><ins>/g, "");
}

// Link references by their byte offsets, then mark defined terms in the plain parts.
function renderText(n) {
  const text = n.text || "";
  const enc = new TextEncoder(), dec = new TextDecoder();
  const bytes = enc.encode(text);
  const refs = (n.references || []).filter((r) => r.end > r.start).sort((a, b) => a.start - b.start);
  const seen = new Set(); let out = "", pos = 0;
  const terms = (n.definitions_in_scope || []).map((d) => d.term).filter((t) => t.length > 2)
    .sort((a, b) => b.length - a.length);
  const termRe = terms.length ? new RegExp("\\b(" + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")\\b", "gi") : null;
  const plain = (s) => {
    const e = md(s);
    return termRe ? e.replace(termRe, (m) => `<span class="term" data-term="${m}">${m}</span>`) : e;
  };
  for (const r of refs) {
    const key = r.start + ":" + r.end;
    if (r.start < pos || seen.has(key)) continue;
    seen.add(key);
    out += plain(dec.decode(bytes.slice(pos, r.start)));
    const label = esc(dec.decode(bytes.slice(r.start, r.end)));
    const targets = refs.filter((x) => x.start === r.start && x.end === r.end && x.target);
    const pin = targets.length && targets[0].pin ? "?item=" + encodeURIComponent(targets[0].pin) : "";
    if (targets.length) out += `<a class="ref" href="${nodeHref(targets[0].target)}${pin}" title="${esc(targets.map((t) => t.target + (t.pin ? " item " + t.pin : "")).join(", "))}">${label}</a>`;
    else out += `<a class="ref unresolved" title="${esc(t("not_loaded_title"))}">${label}</a>`;
    pos = r.end;
  }
  out += plain(dec.decode(bytes.slice(pos)));
  return tableize(out);
}

// Pipe tables (every reader stores tables this way) become HTML tables. Rows
// whose first cell is an item number get an anchor, `#item-3`, so citations
// to "item 3 of the table" land on the row.
const ITEM_CELL = /^\(?([0-9]{1,3}[A-Z]{0,3}(?:\.[0-9]{1,3}[A-Z]{0,2})?)\)?\.?$/;
const itemKey = (s) => String(s).replace(/[^A-Za-z0-9.]/g, "");
function tableize(html) {
  const lines = html.split("\n");
  const isRow = (l) => l.trimStart().startsWith("|");
  const isRule = (l) => /^\s*\|(\s*:?-{3,}:?\s*\|)+\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "").split(/(?<!\\)\|/)
    .map((c) => c.trim().replace(/\\\|/g, "|"));
  const out = [];
  for (let i = 0; i < lines.length;) {
    if (!(isRow(lines[i]) && i + 1 < lines.length && isRule(lines[i + 1]))) { out.push(lines[i++]); continue; }
    const head = cells(lines[i]);
    let j = i + 2, body = "";
    for (; j < lines.length && isRow(lines[j]); j++) {
      const r = cells(lines[j]);
      const m = ITEM_CELL.exec(r[0].replace(/<[^>]+>/g, "").trim());
      body += `<tr${m ? ` id="item-${itemKey(m[1])}"` : ""}>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`;
    }
    out.push(`<div class="tablewrap"><table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`);
    // The block element ends the line; drop the newline that would follow it.
    if (j < lines.length) out.push((out.pop()) + lines[j++]);
    i = j;
  }
  return out.join("\n");
}

const defCache = new Map();

function bindTerms(n) {
  const pop = $("#popover");
  let pinnedOpen = false;
  const hide = () => { pop.hidden = true; pinnedOpen = false; };
  document.addEventListener("click", (e) => { if (!pop.hidden && !pop.contains(e.target) && !e.target.closest(".term")) hide(); });
  $$(".text .term").forEach((el) => {
    const show = async (sticky) => {
      const term = el.dataset.term;
      const corpus = n.corpus && n.corpus.id;
      try {
        const key = corpus + "\u0000" + term.toLowerCase();
        if (!defCache.has(key)) defCache.set(key, api("/api/definitions", { term, corpus, limit: 3 }));
        const r = await defCache.get(key).catch((e) => { defCache.delete(key); throw e; });
        const d = r.definitions[0];
        if (!d) return;
        pop.innerHTML = `<h4>“${esc(d.definition.term)}”</h4><div class="pop-text">${md(d.definition.text)}</div>
          <a class="goto" href="${nodeHref(d.definition.source)}">${esc(t("go_to_definition"))} <span aria-hidden="true">→</span></a>
          <div class="ev">${esc(d.source_heading || d.definition.source)}</div>`;
        const rect = el.getBoundingClientRect();
        pop.style.left = Math.max(8, Math.min(rect.left + scrollX, innerWidth - 440)) + "px";
        pop.style.top = rect.bottom + scrollY + 6 + "px";
        pop.hidden = false;
        if (sticky) pinnedOpen = true;
      } catch (_) {}
    };
    el.addEventListener("mouseenter", () => { if (!pinnedOpen) show(false); });
    el.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); if (!pop.hidden && pinnedOpen) hide(); else show(true); });
    el.addEventListener("mouseleave", () => { setTimeout(() => { if (!pinnedOpen && !pop.matches(":hover")) pop.hidden = true; }, 250); });
  });
  pop.onmouseleave = () => { if (!pinnedOpen) pop.hidden = true; };
}

function showDefinition(d) {
  $("#main").innerHTML = `<div class="reader"><h1>“${esc(d.term)}”</h1>
    <div class="text">${md(d.text)}</div>
    <p>${t("defined_in", { source: `<a href="${nodeHref(d.source)}">${esc(d.source)}</a>`, scope: `<a href="${nodeHref(d.scope)}">${esc(d.scope)}</a>` })}
    ${d.points_to ? t("points_to", { target: `<a href="${nodeHref(d.points_to)}">${esc(d.points_to)}</a>` }) : ""}</p></div>`;
  showRelated(d.id);
}

// "Compilation 266" when the corpus metadata records one.
function compilation(c) {
  const m = c && c.meta || {};
  const n = m.compilation_no ?? m.compilation ?? m.version;
  return n !== undefined && n !== null && n !== "" ? t("compilation", { n }) : "";
}

// Related column: one collapsible card per relationship, plus the source's
// provenance (last updated, compilation, licence) and the citation graph.
const chev = `<svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>`;
function relCard(title, body, count, open) {
  return `<details class="rel-card"${open ? " open" : ""}><summary><h3>${esc(title)}</h3>${count !== undefined ? `<span class="n">${count}</span>` : ""}${chev}</summary><div class="body">${body}</div></details>`;
}
async function showRelated(id, summaryId, corpus) {
  const aside = $("#aside");
  aside.innerHTML = `<p class="empty">${esc(t("loading"))}</p>`;
  const seq = routeSeq;
  try {
    const [r, g, rs] = await Promise.all([
      api("/api/related", { id, limit: 50 }),
      api("/api/graph/neighbourhood", { id, depth: 1, limit: 40 }),
      summaryId ? api("/api/related", { id: summaryId, limit: 50 }) : Promise.resolve(null),
    ]);
    if (!current(seq)) return;
    // A document's citations live on its summary and paragraphs; show the
    // summary's alongside the document's own (similar edges sit on the root).
    if (rs) for (const k of ["cites", "uses_terms"]) {
      const have = new Set(r[k].map((x) => x.id));
      r[k] = r[k].concat(rs[k].filter((x) => !have.has(x.id)));
    }
    if (rs) {
      r.cited_by = r.cited_by.filter((x) => x.id !== summaryId);
      r.cites = r.cites.filter((x) => x.id !== id); // a summary naming its own case
    }
    const list = (title, items, open) => items.length ? relCard(title, `<ul>${items.map((x) => `
      <li><a href="${nodeHref(x.id)}">${esc(x.heading || x.id)}</a>${x.evidence ? `<span class="ev">${esc(x.evidence)}</span>` : ""}</li>`).join("")}</ul>`, items.length, open) : "";
    const c = corpus || {};
    const comp = compilation(c);
    const prov = c.id ? relCard(t("source"), `
      <div class="kv"><a href="#/corpus/${encodeURIComponent(c.id)}">${esc(c.title || c.id)}</a></div>
      ${c.as_at ? `<div class="kv"><b>${esc(t("last_updated"))}</b><span class="mono">${esc(c.as_at)}</span>${comp ? `<span class="sub">${esc(comp)}</span>` : ""}</div>` : comp ? `<div class="kv"><b>${esc(t("compilation_label"))}</b>${esc(comp)}</div>` : ""}
      ${c.jurisdiction ? `<div class="kv"><b>${esc(t("jurisdiction"))}</b>${esc(c.jurisdiction)}</div>` : ""}
      ${c.licence ? `<div class="kv"><b>${esc(t("licence"))}</b>${esc(c.licence)}</div>` : ""}`, undefined, true) : "";
    aside.innerHTML = `${prov}
      ${list(t("cites"), r.cites, true)}${list(t("cited_by"), r.cited_by, true)}${list(t("defines"), r.defines)}
      ${list(t("uses_terms"), r.uses_terms)}${list(t("used_by"), r.used_by)}${list(t("related_manual"), r.related)}
      ${list(t("similar"), (r.similar || []).slice(0, 10))}
      ${relCard(t("graph"), graphSvg(g))}`;
    $$("svg.graph circle", aside).forEach((el) => el.addEventListener("click", () => { location.hash = nodeHref(el.dataset.id); }));
  } catch (e) { if (current(seq)) aside.innerHTML = `<p class="empty">${esc(e.message)}</p>`; }
}

// Radial layout: centre node, neighbours on a ring, coloured by entity kind.
function graphSvg(g) {
  const W = 300, H = 240, cx = W / 2, cy = H / 2, R = 92;
  const nodes = g.nodes.slice(0, 30);
  const pos = new Map([[g.center.id, [cx, cy]]]);
  nodes.forEach((n, i) => { const a = (2 * Math.PI * i) / Math.max(nodes.length, 1) - Math.PI / 2; pos.set(n.id, [cx + R * Math.cos(a), cy + R * Math.sin(a)]); });
  const colour = (n) => n.entity === "definition" ? "var(--warn, #ffb347)" : n.kind === "paragraph" ? "var(--accent)" : "var(--primary)";
  const lines = g.edges.filter((e) => pos.has(e.source) && pos.has(e.target)).map((e) => {
    const [x1, y1] = pos.get(e.source), [x2, y2] = pos.get(e.target);
    return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"><title>${esc(e.kind)}</title></line>`;
  }).join("");
  const dots = [g.center, ...nodes].map((n) => {
    const [x, y] = pos.get(n.id); const r = n.id === g.center.id ? 9 : 6;
    const label = (n.heading || n.id).slice(0, 22);
    return `<circle cx="${x}" cy="${y}" r="${r}" fill="${colour(n)}" data-id="${esc(n.id)}"><title>${esc(n.heading)} (${esc(n.id)})</title></circle>
      <text x="${x}" y="${y + r + 10}" text-anchor="middle">${esc(label)}</text>`;
  }).join("");
  return `<svg class="graph" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(t("citation_graph"))}">${lines}${dots}</svg>
    ${g.truncated ? `<p class="ev">${esc(t("showing_first", { n: nodes.length }))}</p>` : ""}`;
}

// ---------------------------------------------------------------- corpus
async function showCorpus(id) {
  markActiveCorpus(id);
  const main = $("#main");
  main.innerHTML = `<p class="empty">${esc(t("loading"))}</p>`;
  const seq = routeSeq;
  try {
    const o = await api("/api/outline", { id, depth: 3 });
    if (!current(seq)) return;
    // A single-document source (a case, a ruling without headings) has no
    // outline to land on: it reads as one document, so open it directly.
    if (!(o.children || []).length) { location.replace(nodeHref(id)); return; }
    const tree = (items) => `<ul>${items.map((i) => `<li><a href="${nodeHref(i.id)}">${esc(i.heading)}</a>${i.children ? tree(i.children) : ""}</li>`).join("")}</ul>`;
    // "Read from the start" opens the first provision, never a container.
    let first = o.children[0];
    while (first.children && first.children.length) first = first.children[0];
    const c = corporaCache.find((x) => x.id === id) || {};
    main.innerHTML = `<div class="reader">
      <div class="crumbs">${kindBadge(c.kind)}${esc(c.jurisdiction || "")}</div>
      <h1>${esc(o.heading)}</h1>
      <div class="corpus-meta">${[c.as_at && t("as_at", { date: c.as_at }), c.nodes && t("nodes", { n: c.nodes }), c.definitions && t("definitions_n", { n: c.definitions })].filter(Boolean).map(esc).join(" · ")}</div>
      <div class="reader-actions"><a class="btn btn--sm btn--primary" href="${nodeHref(first.id)}">${esc(t("read_from_start"))}</a></div>
      <h3>${esc(t("contents"))}</h3><div class="outline">${tree(o.children)}</div>${disclaimer()}</div>`;
    showRelated(id, undefined, c);
  } catch (e) { main.innerHTML = `<p class="empty">${esc(e.message)}</p>`; }
}

// ------------------------------------------------------------------ home
async function showHome() {
  markActiveCorpus(null);
  selectSource(""); // home shows the grouped source list in the drawer
  const seq = routeSeq;
  let info = {};
  try { info = await api("/api/info"); } catch (_) {}
  if (!current(seq)) return;
  // Store-wide totals are withheld from readers under access control.
  const s = info.stats;
  const stats = s ? `<p class="welcome__stats">${esc(t("home_stats", { corpora: s.corpora ?? 0, nodes: s.nodes ?? 0, definitions: s.definitions ?? 0, edges: s.edges ?? 0 }))}</p>` : "";
  const quick = corporaCache.filter((c) => !c.superseded).slice(0, 6).map((c) => `<a href="#/corpus/${encodeURIComponent(c.id)}">${esc(c.title)}</a>`).join("");
  $("#main").innerHTML = `<div class="welcome"><div class="welcome__inner">
    ${BRAND.logo ? `<img class="welcome__logo" src="${esc(BRAND.logo)}" alt="">` : ""}
    <h1>${esc(BRAND.name || "MeowLaw")}</h1>
    ${BRAND.tagline ? `<p class="welcome__lead">${esc(BRAND.tagline)}</p>` : ""}
    <p class="welcome__help">${esc(t("home_help"))}</p>
    ${searchForm("")}
    ${quick ? `<div class="welcome__quick">${quick}</div>` : ""}
    ${stats}
    ${disclaimer()}
  </div></div>`;
  $("#aside").innerHTML = "";
}

// ---------------------------------------------------------------- routing
function route() {
  routeSeq++;
  const h = location.hash.slice(1) || "/";
  const [path, query] = h.split("?");
  const params = new URLSearchParams(query || "");
  $("#popover").hidden = true;
  if (path.startsWith("/node/")) return showNode(decodeURIComponent(path.slice(6)), params.get("item"));
  if (path.startsWith("/corpus/")) return showCorpus(decodeURIComponent(path.slice(8)));
  if (path.startsWith("/search")) return showSearch(params);
  if (path.startsWith("/compare")) return showCompare(params);
  return showHome();
}

// Search forms are rendered into the page; one delegated handler serves them all.
document.addEventListener("submit", (e) => {
  const f = e.target.closest("form.search-form");
  if (!f) return;
  e.preventDefault();
  gotoSearch(f.q.value.trim());
});
$("#search-btn").addEventListener("click", () => {
  setDrawer(false);
  const input = $(".search-form input");
  if (input && location.hash.startsWith("#/search")) input.focus(); else gotoSearch("");
});
addEventListener("hashchange", () => { setDrawer(false); openSettings(false); route(); });
$("#source-pill").addEventListener("click", () => {
  // Open source: back to the list. On the list: collapse/expand it.
  if (treeState.corpus) { listCollapsed = false; selectSource(""); }
  else {
    listCollapsed = !listCollapsed;
    const body = $(".sb-body");
    if (body) body.hidden = listCollapsed;
    $("#corpora").hidden = listCollapsed;
    syncPill();
  }
});
let filterTimer = 0;
$("#tree-filter").addEventListener("input", (e) => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => filterTree(e.target.value.trim()), 120);
});
$("#drawer-toggle").addEventListener("click", () => setDrawer(!document.body.classList.contains("drawer-open")));
$("#drawer-close").addEventListener("click", () => setDrawer(false));
$("#scrim").addEventListener("click", () => setDrawer(false));
// Next / previous entry in the open source's tree. Only leaves are stepped to,
// so j/k never land on a Part or Division contents page.
function stepInTree(delta) {
  if (!treeState.active) {
    const first = leafIds()[0];
    if (first) location.hash = nodeHref(first);
    return;
  }
  const next = leafNear(treeState.active, delta);
  if (next && next !== treeState.active) location.hash = nodeHref(next);
}
addEventListener("keydown", (e) => {
  if (e.key === "Escape") { openSettings(false); setDrawer(false); $("#popover").hidden = true; return; }
  const el = e.target;
  const typing = el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (e.key === "j") { e.preventDefault(); return stepInTree(1); }
    if (e.key === "k") { e.preventDefault(); return stepInTree(-1); }
    if (e.key === "?") { e.preventDefault(); return openSettings($("#settings").hidden); }
  }
  if (e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (typing) return;
    e.preventDefault();
    const input = $(".search-form input");
    if (input) { input.focus(); input.select(); } else gotoSearch("");
  }
});

initSettings();
loadPins(); renderPins();
initBranding().finally(() => loadCorpora().catch(() => {}).finally(route));
