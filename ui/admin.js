// MeowLaw ingest page. Talks to /api/admin/* (off unless server.allow_ingest).
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let STRINGS = {};
function t(key, vars = {}) {
  const s = STRINGS[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`));
}
async function loadStrings(locale) {
  const get = async (l) => { try { const r = await fetch(`/i18n/${l}.json`); return r.ok ? r.json() : {}; } catch (_) { return {}; } };
  STRINGS = await get("en");
  if (locale && locale !== "en") Object.assign(STRINGS, await get(locale));
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.documentElement.lang = locale || "en";
}

function getToken() { try { return localStorage.getItem("meowlaw-token") || ""; } catch (_) { return ""; } }

async function call(path, opts = {}, retry = true) {
  const token = getToken();
  const headers = Object.assign({}, opts.headers || {}, token ? { Authorization: "Bearer " + token } : {});
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401 && retry) {
    const v = prompt(t("token_prompt"));
    if (v) { try { localStorage.setItem("meowlaw-token", v.trim()); } catch (_) {} return call(path, opts, false); }
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(body.error || r.statusText); e.status = r.status; throw e; }
  return body;
}

// ------------------------------------------------------------------ form
function manifest() {
  const v = (id) => $("#f-" + id).value.trim();
  const list = (id) => v(id).split(",").map((s) => s.trim()).filter(Boolean);
  const m = {};
  for (const k of ["id", "title", "kind", "jurisdiction", "profile", "as_at", "source_url", "licence", "content", "summary", "summary_source"]) {
    if (v(k)) m[k] = v(k);
  }
  if (list("citations").length) m.citations = list("citations");
  if (list("aliases").length) m.aliases = list("aliases");
  return m;
}

function formData(dryRun) {
  const fd = new FormData();
  fd.append("manifest", JSON.stringify(manifest()));
  if (dryRun) fd.append("dry_run", "true");
  fd.append("file", $("#f-file").files[0]);
  return fd;
}

async function run(label, path, dryRun, render) {
  if (!$("#f-file").files.length) { $("#f-file").reportValidity(); return; }
  const buttons = document.querySelectorAll("#upload button");
  buttons.forEach((b) => { b.disabled = true; });
  $("#busy").hidden = false; $("#busy").textContent = t(label);
  try {
    render(await call(path, { method: "POST", body: formData(dryRun) }));
  } catch (e) {
    $("#result").innerHTML = `<div class="card error">${esc(e.message)}</div>`;
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
    $("#busy").hidden = true;
  }
}

// --------------------------------------------------------------- reports
function renderPreview(r) {
  const p = r.profile;
  const kinds = Object.entries(p.node_kinds).map(([k, n]) => `${esc(k)} ${n}`).join(", ");
  const headings = Object.entries(p.headings).map(([label, b]) =>
    `<tr><td>${esc(label)}</td><td>${b.count}</td><td>${b.examples.map(esc).join("<br>")}</td></tr>`).join("");
  const cites = Object.entries(p.citations).map(([name, b]) =>
    `<tr><td>${esc(name)}</td><td>${b.count}</td><td>${b.resolved}</td><td>${
      b.examples_resolved.map((e) => `<span class="ok">✓</span> ${esc(e)}`).concat(b.examples_unresolved.map((e) => `<span class="bad">✗</span> ${esc(e)}`)).join("<br>")}</td></tr>`).join("");
  const defs = p.definitions.map((d) => `<li>“${esc(d.term)}” <span class="hint">${esc(d.source)} → ${esc(d.scope)}</span></li>`).join("");
  const lint = r.lint ? renderLint(r.lint) : "";
  $("#result").innerHTML = `<div class="card report">
    <h2>${esc(t("admin_preview_of", { file: p.file.split("/").pop() }))}</h2>
    ${lint}
    <p>${esc(t("admin_preview_corpus", { corpus: p.corpus, kind: p.kind, profile: p.profile }))}<br><span class="hint">${esc(t("admin_nodes", { n: p.nodes }))}: ${kinds}</span></p>
    ${p.notes.length ? `<ul class="notes">${p.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    <h3>${esc(t("admin_headings"))}</h3>
    ${headings ? `<div class="tablewrap"><table><thead><tr><th>${esc(t("admin_level"))}</th><th>#</th><th>${esc(t("admin_examples"))}</th></tr></thead><tbody>${headings}</tbody></table></div>` : `<p class="hint">${esc(t("admin_none"))}</p>`}
    ${p.unmatched_headings.count ? `<p>${esc(t("admin_unmatched", { n: p.unmatched_headings.count }))}</p><ul>${p.unmatched_headings.examples.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
    <h3>${esc(t("admin_citations_found"))}</h3>
    ${cites ? `<div class="tablewrap"><table><thead><tr><th>${esc(t("admin_pattern"))}</th><th>${esc(t("admin_found"))}</th><th>${esc(t("admin_resolved"))}</th><th>${esc(t("admin_examples"))}</th></tr></thead><tbody>${cites}</tbody></table></div>` : `<p class="hint">${esc(t("admin_none"))}</p>`}
    <h3>${esc(t("admin_definitions", { n: p.definitions_total }))}</h3>
    ${defs ? `<ul>${defs}</ul>` : `<p class="hint">${esc(t("admin_none"))}</p>`}
  </div>`;
}

// Lint findings: errors and warnings shown, info folded away.
function renderLint(l) {
  const sev = (s) => l.findings.filter((f) => f.severity === s);
  const row = (f) => `<li><span class="badge sev-${esc(f.severity)}">${esc(f.severity)}</span> <code>${esc(f.rule)}</code>${
    f.page ? ` p${f.page}` : ""}${f.line ? ` ${esc(t("admin_line", { n: f.line }))}` : ""}: ${esc(f.message)}${
    f.excerpt ? `<br><span class="hint">› ${esc(f.excerpt)}</span>` : ""}${f.fix ? `<br><span class="hint">${esc(f.fix)}</span>` : ""}</li>`;
  const main = sev("error").concat(sev("warning"));
  const info = sev("info");
  return `<h3>${esc(t("admin_lint", { e: sev("error").length, w: sev("warning").length, i: info.length }))}</h3>
    ${main.length ? `<ul class="lint">${main.map(row).join("")}</ul>` : `<p class="hint">${esc(t("admin_lint_clean"))}</p>`}
    ${info.length ? `<details><summary>${esc(t("admin_lint_info", { n: info.length }))}</summary><ul class="lint">${info.map(row).join("")}</ul></details>` : ""}`;
}

function renderSync(r) {
  const rep = r.report;
  const rows = rep.sources.flatMap((s) => (s.diffs.length ? s.diffs : [null]).map((d) => ({ s, d })));
  const list = (title, ids) => ids && ids.length
    ? `<p><strong>${esc(title)}</strong> (${ids.length}): ${ids.slice(0, 60).map((id) => `<a href="/#/node/${encodeURIComponent(id)}">${esc(id)}</a>`).join(", ")}${ids.length > 60 ? " …" : ""}</p>` : "";
  const body = rows.map(({ s, d }) => `
    <h3>${esc(d ? d.corpus : s.path.split("/").pop())} <span class="badge">${esc(t("admin_status_" + s.status))}</span></h3>
    ${s.error ? `<p class="bad">${esc(s.error)}</p>` : ""}
    ${s.lint && s.lint.findings.length ? `<p>${esc(t("admin_lint_sync", { e: s.lint.errors, w: s.lint.warnings }))}</p><ul class="lint">${s.lint.findings.map(([, rule, line, msg]) => `<li><code>${esc(rule)}</code>${line ? " " + esc(t("admin_line", { n: line })) : ""}: ${esc(msg)}</li>`).join("")}</ul>` : ""}
    ${d && d.metadata_changed ? `<p>${esc(t("admin_metadata_changed"))}</p>` : ""}
    ${d ? list(t("admin_added"), d.added) + list(t("admin_changed"), d.changed) + list(t("admin_removed"), d.removed) : ""}
    ${d && d.breaks.length ? `<p><strong>${esc(t("admin_breaks", { n: d.breaks.length }))}</strong></p><ul>${d.breaks.slice(0, 50).map((b) => `<li><a href="/#/node/${encodeURIComponent(b.source)}">${esc(b.source)}</a> ${esc(b.raw)} → ${esc(b.target)}</li>`).join("")}</ul>` : ""}
  `).join("");
  const unresolved = rep.mode === "dry-run" ? "" : `<p>${esc(t("admin_unresolved_counts", { before: rep.unresolved_before, after: rep.unresolved_after, fixed: rep.newly_resolved }))}</p>
    ${rep.newly_unresolved.length ? `<p><strong>${esc(t("admin_newly_unresolved"))}</strong></p><ul>${rep.newly_unresolved.slice(0, 100).map(([s, raw]) => `<li><a href="/#/node/${encodeURIComponent(s)}">${esc(s)}</a>: ${esc(raw)}</li>`).join("")}</ul>` : ""}`;
  const embedded = r.embedded ? `<p class="hint">${esc(t("admin_embedded", { n: r.embedded.nodes }))}</p>` : "";
  $("#result").innerHTML = `<div class="card report"><h2>${esc(t(rep.mode === "dry-run" ? "admin_dry_title" : "admin_sync_title"))}</h2>${body}${unresolved}${embedded}</div>`;
  if (rep.mode !== "dry-run") { loadUploads(); loadUnresolved(); }
}

// ------------------------------------------------------------- side lists
async function loadUploads() {
  const s = await call("/api/admin/status");
  const el = $("#uploads");
  if (!s.uploads.length) { el.innerHTML = `<p class="hint">${esc(t("admin_no_uploads"))}</p>`; return s; }
  el.innerHTML = `<div class="tablewrap"><table><thead><tr><th>${esc(t("admin_file"))}</th><th>${esc(t("admin_id_col"))}</th><th>${esc(t("admin_kind"))}</th><th></th></tr></thead><tbody>${
    s.uploads.map((u) => `<tr><td>${esc(u.file)}</td><td>${u.id ? `<a href="/#/corpus/${encodeURIComponent(u.id)}">${esc(u.id)}</a>` : ""}</td><td>${esc(u.kind || "")}</td>
      <td>${u.id ? `<button type="button" class="remove" data-id="${esc(u.id)}">${esc(t("admin_remove"))}</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  el.querySelectorAll("button.remove").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(t("admin_remove_confirm", { id: b.dataset.id }))) return;
    b.disabled = true;
    try {
      await call("/api/admin/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ corpus: b.dataset.id }) });
      loadUploads(); loadUnresolved();
    } catch (e) { alert(e.message); b.disabled = false; }
  }));
  return s;
}

async function loadUnresolved() {
  const r = await call("/api/unresolved?limit=30");
  const groups = r.groups || [];
  $("#unresolved").innerHTML = groups.length
    ? `<div class="tablewrap"><table><thead><tr><th>${esc(t("citation"))}</th><th>#</th><th>${esc(t("admin_example"))}</th></tr></thead><tbody>${
      groups.map((g) => `<tr><td>${esc(g.raw)}</td><td>${g.count}</td><td><a href="/#/node/${encodeURIComponent(g.example_source)}">${esc(g.example_source)}</a></td></tr>`).join("")}</tbody></table></div>`
    : `<p class="hint">${esc(t("admin_none"))}</p>`;
}

// ------------------------------------------------------------------- boot
(async function boot() {
  let b = {};
  try { b = await (await fetch("/api/branding")).json(); } catch (_) {}
  await loadStrings(b.locale);
  if (b.name) { $("#brand-name").textContent = b.name; document.title = b.name + " · " + t("admin_title"); }
  if (b.logo) { const img = $("#brand-logo"); img.src = b.logo; img.hidden = false; }
  let status;
  try { status = await loadUploads(); }
  catch (e) {
    $("#disabled").hidden = false;
    $("#disabled").textContent = e.status === 404 ? t("admin_disabled") : e.message;
    return;
  }
  $("#f-kind").innerHTML = `<option value="">${esc(t("admin_auto"))}</option>` + status.kinds.map((k) => `<option value="${esc(k)}">${esc(t("kind_" + k))}</option>`).join("");
  $("#f-profile").innerHTML = `<option value="">${esc(t("admin_auto"))}</option>` + status.profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.id)}${p.name ? " · " + esc(p.name) : ""}</option>`).join("");
  $("#panel").hidden = false;
  loadUnresolved().catch(() => {});
  $("#b-preview").addEventListener("click", () => run("admin_working_preview", "/api/admin/preview", false, renderPreview));
  $("#b-dry").addEventListener("click", () => run("admin_working_check", "/api/admin/ingest", true, renderSync));
  $("#upload").addEventListener("submit", (e) => { e.preventDefault(); run("admin_working_ingest", "/api/admin/ingest", false, renderSync); });
})();
