// MeowLaw UI. No build step: plain ES2020 against the REST API.
"use strict";

const $ = (s, el = document) => el.querySelector(s);

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
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll("[data-i18n-label]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.i18nLabel)); el.title = t(el.dataset.i18nLabel); });
  document.documentElement.lang = locale || "en";
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Escape, then render the only inline Markdown the ingesters keep: **bold** and *italic*.
const md = (s) => esc(s).replace(/\*\*\*([^*\n]+)\*\*\*/g, "<strong><em>$1</em></strong>")
  .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
const nodeHref = (id) => "#/node/" + encodeURIComponent(id);

function getToken() { try { return localStorage.getItem("meowlaw-token") || ""; } catch (_) { return ""; } }

async function api(path, params = {}, retry = true) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
  const token = getToken();
  const r = await fetch(path + (qs.toString() ? "?" + qs : ""), token ? { headers: { Authorization: "Bearer " + token } } : {});
  // Protected API: ask once for the token and retry.
  if (r.status === 401 && retry) {
    const t = prompt(t("token_prompt"));
    if (t) { try { localStorage.setItem("meowlaw-token", t.trim()); } catch (_) {} return api(path, params, false); }
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

// Each navigation gets a number; async renders check it so a slow response
// cannot overwrite the page the user has since moved to.
let routeSeq = 0;
const current = (seq) => seq === routeSeq;

// ------------------------------------------------------------------ theme
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("meowlaw-theme"); } catch (_) {}
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("meowlaw-theme", next); } catch (_) {}
  });
}

async function initBranding() {
  try {
    const b = await api("/api/branding");
    document.title = b.name;
    $("#brand-name").textContent = b.name;
    $("#brand-tagline").textContent = b.tagline || "";
    // With per-corpus access control, readers with a token sign in here.
    if (b.access_control) {
      const btn = $("#token-button");
      const label = () => { btn.textContent = t(getToken() ? "token_forget" : "token_use"); };
      label(); btn.hidden = false;
      btn.addEventListener("click", () => {
        try {
          if (getToken()) localStorage.removeItem("meowlaw-token");
          else { const v = prompt(t("token_prompt")); if (!v) return; localStorage.setItem("meowlaw-token", v.trim()); }
        } catch (_) {}
        location.reload();
      });
    }
    // Link to the ingest page when the server has it on (404 when off).
    fetch("/api/admin/status").then((r) => { if (r.status !== 404 && r.status !== 403) $("#admin-link").hidden = false; }).catch(() => {});
    $("#brand-footer").textContent = b.footer || "";
    if (b.logo) { const img = $("#brand-logo"); img.src = b.logo; img.hidden = false; }
    if (Array.isArray(b.source_groups) && b.source_groups.length) sourceGroups = b.source_groups;
    await loadStrings(b.locale);
    if (b.search_hint) $("#q").placeholder = b.search_hint;
    $("#kind").innerHTML = `<option value="">${esc(t("all_sources"))}</option>` +
      sourceGroups.map((g) => `<option value="${esc((g.kinds || []).join(","))}">${esc(g.label)}</option>`).join("");
  } catch (_) { await loadStrings("en"); }
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

async function loadCorpora() {
  const all = (await api("/api/corpora")).corpora;
  corporaCache = all;
  // Earlier compilations stay reachable through each provision's versions.
  const corpora = all.filter((c) => !c.superseded);
  const lang = (c) => (c.meta && c.meta.language ? ` (${c.meta.language})` : "");
  const groups = grouped(corpora);
  $("#source").innerHTML = `<option value="">${esc(t("all_sources"))}</option>` + groups.map(([label, items]) =>
    `<optgroup label="${esc(label)}">${items.map((c) => `<option value="${esc(c.id)}">${esc(c.title + lang(c))}</option>`).join("")}</optgroup>`).join("");
  $("#corpora").innerHTML = corpora.length ? groups.map(([label, items]) => `
    <section><h2>${esc(label)}</h2><ul>${items.map((c) => `
      <li><a href="#/corpus/${encodeURIComponent(c.id)}" data-id="${esc(c.id)}">${esc(c.title + lang(c))}
        <span class="k">${[c.jurisdiction, t("nodes", { n: c.nodes })].filter(Boolean).map(esc).join(" · ")}</span></a></li>`).join("")}</ul></section>`).join("")
    : `<p class="empty">${t("nothing_ingested")}</p>`;
  let saved = "";
  try { saved = localStorage.getItem("meowlaw-source") || ""; } catch (_) {}
  if (saved && corpora.some((c) => c.id === saved) && !treeState.corpus) await selectSource(saved);
}

// ------------------------------------------------------------------- tree
// The drawer shows one source's navigation tree (JSON from /api/tree), built
// lazily: a node's children are rendered the first time it is expanded.
const treeCache = new Map();
const treeState = { corpus: "", byId: new Map(), parent: new Map(), active: "" };

async function selectSource(id) {
  $("#source").value = id;
  try { localStorage.setItem("meowlaw-source", id); } catch (_) {}
  const showTree = !!id;
  $("#tree").hidden = !showTree;
  $("#tree-filter").hidden = !showTree;
  $("#corpora").hidden = showTree;
  if (!showTree) { treeState.corpus = ""; return; }
  if (treeState.corpus === id) return;
  treeState.corpus = id;
  $("#tree").innerHTML = `<p class="empty">${esc(t("loading"))}</p>`;
  $("#tree-filter").value = "";
  let t = treeCache.get(id);
  if (!t) {
    try { t = await api("/api/tree", { corpus: id }); } catch (e) { $("#tree").innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
    treeCache.set(id, t);
  }
  if (treeState.corpus !== id) return;
  treeState.byId = new Map(); treeState.parent = new Map();
  const index = (nodes, parent) => nodes.forEach((n) => {
    treeState.byId.set(n.id, n); treeState.parent.set(n.id, parent);
    if (n.children) index(n.children, n.id);
  });
  index(t.children || [], null);
  const root = $("#tree");
  root.innerHTML = `<a class="tree-title" href="#/corpus/${encodeURIComponent(id)}">${esc(t.title)}</a>`;
  const list = document.createElement("div");
  list.className = "tlist";
  (t.children || []).forEach((n) => list.appendChild(treeRow(n)));
  root.appendChild(list);
  if (treeState.active) revealInTree(treeState.active);
}

function rowLabel(n) {
  const num = n.number ? (n.kind === "provision" ? n.number : `${n.label ? n.label + " " : ""}${n.number}`) : "";
  return `${num ? `<b>${esc(num)}</b> ` : ""}${esc(n.title || (num ? "" : n.id))}`;
}

function treeRow(n) {
  const wrap = document.createElement("div");
  wrap.className = "tnode";
  wrap.dataset.id = n.id;
  const kids = n.children && n.children.length;
  wrap.innerHTML = `<div class="row${n.id === treeState.active ? " active" : ""}" role="treeitem" aria-expanded="${kids ? "false" : ""}">
    <button class="tg" type="button" tabindex="-1" aria-label="${esc(t("expand"))}"${kids ? "" : " disabled"}>${kids ? "▶" : ""}</button>
    <a href="${nodeHref(n.id)}" title="${esc((n.title || n.id))}">${rowLabel(n)}</a></div>`;
  if (kids) wrap.querySelector(".tg").addEventListener("click", () => toggleNode(wrap, n));
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
  row.querySelector(".tg").textContent = want ? "▼" : "▶";
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
    el = [...$("#tree").querySelectorAll(".tnode")].find((x) => x.dataset.id === cid);
    if (!el) return;
    if (cid !== id) toggleNode(el, treeState.byId.get(cid), true);
  }
  $("#tree").querySelectorAll(".row.active").forEach((r) => r.classList.remove("active"));
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
  res.innerHTML = hits.length ? hits.map((n) => `<div class="row"><span class="tg"></span><a href="${nodeHref(n.id)}">${rowLabel(n)}</a></div>`).join("")
    : `<p class="empty">${esc(t("no_matches"))}</p>`;
}

// Keep the drawer on the source being read and reveal the open node.
async function markActiveCorpus(id, path = []) {
  document.querySelectorAll(".corpora a").forEach((a) => a.classList.toggle("active", a.dataset.id === id));
  if (!id) return;
  if (treeState.corpus !== id) await selectSource(id);
  const target = [...path].reverse().find((p) => treeState.byId.has(p));
  if (target) revealInTree(target);
}

function setDrawer(open) {
  document.body.classList.toggle("drawer-open", open);
  $("#scrim").hidden = !open;
  $("#drawer-toggle").setAttribute("aria-expanded", String(open));
}

// ----------------------------------------------------------------- search
async function showSearch(params) {
  const q = params.get("q") || "";
  const mode = params.get("mode") || "enriched";
  const kind = params.get("kind") || "";
  $("#q").value = q; $("#mode").value = mode; $("#kind").value = kind;
  const main = $("#main");
  if (!q) { return showHome(); }
  main.innerHTML = `<p class="empty">${esc(t("searching"))}</p>`;
  const seq = routeSeq;
  let r;
  try { r = await api("/api/search", { q, mode, kind, limit: 25 }); }
  catch (e) { if (current(seq)) main.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  if (!current(seq)) return;
  const cites = (r.citations || []).map((c) => `
    <div class="card"><h4>${esc(t("citation"))}: ${esc(c.raw)}</h4>${c.targets.length
      ? c.targets.map((t) => `<a href="${nodeHref(t)}">${esc(t)}</a>`).join(" · ")
      : `<span class="badge">${esc(t("not_loaded"))}</span>`}</div>`).join("");
  const defs = (r.definitions || []).slice(0, 4).map((d) => `
    <div class="card"><h4>“${esc(d.term)}” <span class="badge">${esc(t("definition"))}</span></h4>
      <div>${md(d.text)}</div>
      <div class="meta"><a href="${nodeHref(d.source)}">${esc(d.source)}</a></div></div>`).join("");
  const hits = r.results.map((h) => `
    <article class="hit">
      <h3><a href="${nodeHref(h.id)}">${esc(h.heading)}</a></h3>
      <div class="meta">${esc(h.corpus_title)}${h.path.length ? " › " + h.path.map(esc).join(" › ") : ""}</div>
      <p>${highlight(h.snippet)}</p>
      <div>${h.matched_by.map((m) => `<span class="badge ${esc(m)}">${esc(m)}</span>`).join("")}</div>
    </article>`).join("");
  main.innerHTML = `<h2>${esc(t("results_for", { q }))}</h2>${cites}${defs}${hits || `<p class="empty">${esc(t("no_results"))}</p>`}`;
  $("#aside").innerHTML = "";
}

// Snippets mark matches with «»; turn them into <mark> after escaping.
function highlight(s) {
  return esc(s).replace(/«/g, "<mark>").replace(/»/g, "</mark>");
}

// ----------------------------------------------------------------- reader
async function showNode(id, item) {
  const main = $("#main");
  main.innerHTML = `<p class="empty">${esc(t("loading"))}</p>`;
  const seq = routeSeq;
  let n;
  try { n = await api("/api/node", { id }); }
  catch (e) { if (current(seq)) main.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  if (!current(seq)) return;
  if (n.definition) { return showDefinition(n.definition); }
  markActiveCorpus(n.corpus && n.corpus.id, [...n.path.map((p) => p.id), n.id]);
  const crumbs = n.path.map((p) => `<a href="${nodeHref(p.id)}">${esc(p.heading)}</a>`).join(" › ");
  const c = n.corpus || {};
  const meta = [c.kind, c.jurisdiction, c.as_at && t("as_at", { date: c.as_at }), c.licence].filter(Boolean).map(esc).join(" · ");
  main.innerHTML = `
    <div class="reader">
      <div class="crumbs">${crumbs}</div>
      <h1>${esc(n.heading)}</h1>
      <div class="corpus-meta">${esc(c.title || "")}${meta ? " · " + meta : ""}</div>
      ${sourceLinks(c)}
      ${renderVersions(n)}
      ${n.summary ? `<div class="card summary"><h4>${esc(t("summary"))} <span class="badge">${esc(n.summary.source || "")}</span></h4><div class="text">${renderText({ text: n.summary.text, references: n.summary.references, definitions_in_scope: n.definitions_in_scope })}</div></div>` : ""}
      <div class="text">${renderText(n)}</div>
      ${n.truncated ? `<p class="empty">${esc(t("text_truncated"))}</p>` : ""}
      ${renderChildren(n.children)}
    </div>`;
  bindTerms(n);
  showRelated(id, n.summary && n.summary.id);
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
    <h1>${esc(b.heading)}</h1>
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

// External copies of the source: official site, AustLII, court website...
function sourceLinks(c) {
  const links = (c.links || []).slice();
  if (c.source_url && !links.some((l) => l.url === c.source_url)) links.push({ label: t("source"), url: c.source_url });
  const ok = links.filter((l) => /^https?:\/\//i.test(l.url));
  if (!ok.length) return "";
  return `<div class="links">${ok.map((l) => `<a class="button" href="${esc(l.url)}" rel="noopener noreferrer" target="_blank">${esc(l.label)} ↗</a>`).join("")}</div>`;
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
  document.querySelectorAll(".text .term").forEach((el) => {
    el.addEventListener("mouseenter", async () => {
      const term = el.dataset.term;
      const corpus = n.corpus && n.corpus.id;
      try {
        const key = corpus + "\u0000" + term.toLowerCase();
        if (!defCache.has(key)) defCache.set(key, api("/api/definitions", { term, corpus, limit: 3 }));
        const r = await defCache.get(key).catch((e) => { defCache.delete(key); throw e; });
        const d = r.definitions[0];
        if (!d) return;
        pop.innerHTML = `<h4>“${esc(d.definition.term)}”</h4><div>${md(d.definition.text)}</div>
          <div class="ev"><a href="${nodeHref(d.definition.source)}">${esc(d.source_heading || d.definition.source)}</a></div>`;
        const rect = el.getBoundingClientRect();
        pop.style.left = Math.min(rect.left + scrollX, innerWidth - 440) + "px";
        pop.style.top = rect.bottom + scrollY + 6 + "px";
        pop.hidden = false;
      } catch (_) {}
    });
    el.addEventListener("mouseleave", () => { setTimeout(() => { if (!pop.matches(":hover")) pop.hidden = true; }, 250); });
  });
  pop.onmouseleave = () => { pop.hidden = true; };
}

function showDefinition(d) {
  $("#main").innerHTML = `<div class="reader"><h1>“${esc(d.term)}”</h1>
    <div class="text">${md(d.text)}</div>
    <p>${t("defined_in", { source: `<a href="${nodeHref(d.source)}">${esc(d.source)}</a>`, scope: `<a href="${nodeHref(d.scope)}">${esc(d.scope)}</a>` })}
    ${d.points_to ? t("points_to", { target: `<a href="${nodeHref(d.points_to)}">${esc(d.points_to)}</a>` }) : ""}</p></div>`;
  showRelated(d.id);
}

async function showRelated(id, summaryId) {
  const aside = $("#aside");
  aside.innerHTML = `<h2>${esc(t("related"))}</h2><p class="empty">${esc(t("loading"))}</p>`;
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
    const list = (title, items) => items.length ? `<h3>${title} (${items.length})</h3><ul>${items.map((x) => `
      <li><a href="${nodeHref(x.id)}">${esc(x.heading || x.id)}</a>${x.evidence ? ` <span class="ev">${esc(x.evidence)}</span>` : ""}</li>`).join("")}</ul>` : "";
    aside.innerHTML = `<h2>${esc(t("graph"))}</h2>${graphSvg(g)}<div class="rel">
      ${list(t("cites"), r.cites)}${list(t("cited_by"), r.cited_by)}${list(t("defines"), r.defines)}
      ${list(t("uses_terms"), r.uses_terms)}${list(t("used_by"), r.used_by)}${list(t("related_manual"), r.related)}
      ${list(t("similar"), (r.similar || []).slice(0, 10))}</div>`;
    aside.querySelectorAll("svg.graph circle").forEach((c) => c.addEventListener("click", () => { location.hash = nodeHref(c.dataset.id); }));
  } catch (e) { if (current(seq)) aside.innerHTML = `<h2>${esc(t("related"))}</h2><p class="empty">${esc(e.message)}</p>`; }
}

// Radial layout: centre node, neighbours on a ring, coloured by entity kind.
function graphSvg(g) {
  const W = 300, H = 280, cx = W / 2, cy = H / 2, R = 105;
  const nodes = g.nodes.slice(0, 30);
  const pos = new Map([[g.center.id, [cx, cy]]]);
  nodes.forEach((n, i) => { const a = (2 * Math.PI * i) / Math.max(nodes.length, 1) - Math.PI / 2; pos.set(n.id, [cx + R * Math.cos(a), cy + R * Math.sin(a)]); });
  const colour = (n) => n.entity === "definition" ? "var(--accent)" : n.kind === "paragraph" ? "var(--muted)" : "var(--primary)";
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
    const tree = (items) => `<ul>${items.map((i) => `<li><a href="${nodeHref(i.id)}">${esc(i.heading)}</a>${i.children ? tree(i.children) : ""}</li>`).join("")}</ul>`;
    const c = corporaCache.find((x) => x.id === id) || {};
    main.innerHTML = `<div class="reader"><h1>${esc(o.heading)}</h1>
      <div class="corpus-meta">${[c.kind, c.jurisdiction, c.as_at && t("as_at", { date: c.as_at }), c.licence, c.nodes && t("nodes", { n: c.nodes }), c.definitions && t("definitions_n", { n: c.definitions })].filter(Boolean).map(esc).join(" · ")}</div>
      <p><a href="${nodeHref(id)}">${esc(t("read_from_start"))}</a></p>
      <div class="outline">${tree(o.children)}</div></div>`;
    showRelated(id);
  } catch (e) { main.innerHTML = `<p class="empty">${esc(e.message)}</p>`; }
}

async function showHome() {
  markActiveCorpus(null);
  selectSource(""); // home shows the grouped source list, as in LawKitty
  let info = {};
  try { info = await api("/api/info"); } catch (_) {}
  const s = info.stats || {};
  $("#main").innerHTML = `<div class="reader"><h1>${esc($("#brand-name").textContent)}</h1>
    <p>${esc(t("home_stats", { corpora: s.corpora ?? 0, nodes: s.nodes ?? 0, definitions: s.definitions ?? 0, edges: s.edges ?? 0 }))}</p>
    <p>${t("home_help")}</p>
    <p>${t("home_mcp", { url: esc(location.origin + "/mcp") })}</p></div>`;
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

$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const p = new URLSearchParams({ q: $("#q").value.trim(), mode: $("#mode").value });
  if ($("#kind").value) p.set("kind", $("#kind").value);
  location.hash = "#/search?" + p;
});
addEventListener("hashchange", () => { setDrawer(false); route(); });
$("#source").addEventListener("change", (e) => {
  const id = e.target.value;
  selectSource(id);
  location.hash = id ? "#/corpus/" + encodeURIComponent(id) : "#/";
});
let filterTimer = 0;
$("#tree-filter").addEventListener("input", (e) => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => filterTree(e.target.value.trim()), 120);
});
$("#drawer-toggle").addEventListener("click", () => setDrawer(!document.body.classList.contains("drawer-open")));
$("#scrim").addEventListener("click", () => setDrawer(false));

initTheme();
initBranding().finally(() => loadCorpora().catch(() => {}).finally(route));
