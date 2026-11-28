const $ = (id) => document.getElementById(id);

function showErr(msg) {
  const e = $('err');
  if (msg) { e.textContent = msg; e.style.display = 'block'; }
  else e.style.display = 'none';
}

async function get(path) {
  const r = await fetch(path, { credentials: 'same-origin' });
  if (r.status === 401) { showErr('Not authenticated — sign in at /login.html first.'); return null; }
  if (!r.ok) { showErr(path + ' -> ' + r.status); return null; }
  return r.json();
}

function fmtUptime(s) {
  if (s == null) return '—';
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function renderSeries(el, xel, series, color) {
  const max = Math.max(1, ...series.map(p => p.count));
  el.innerHTML = '';
  xel.innerHTML = '';
  series.forEach(p => {
    const h = Math.max(2, Math.round(p.count / max * 120));
    const b = document.createElement('div');
    b.className = 'bar';
    b.style.height = h + 'px';
    if (color) b.style.background = color;
    const t = document.createElement('div');
    t.className = 'tip';
    t.textContent = `${p.date.slice(5)} — ${p.count}`;
    b.appendChild(t);
    el.appendChild(b);
    const x = document.createElement('span');
    x.textContent = p.date.slice(5);
    xel.appendChild(x);
  });
}

async function loadStats() {
  const s = await get('/api/admin/stats');
  if (!s) return;
  $('s-users').textContent = s.total_users;
  $('s-active').textContent = s.active_7d;
  $('s-tokens').textContent = s.total_mcp_tokens;
  $('s-calls').textContent = s.total_mcp_calls;
  renderSeries($('chart-signups'), $('x-signups'), s.signups_per_day);
  renderSeries($('chart-calls'), $('x-calls'), s.mcp_calls_per_day, 'var(--yarn)');
}

async function loadUsers() {
  const u = await get('/api/admin/users');
  if (!u || !u.users) return;
  const users = u.users;
  const rows = users.map(x => {
    const created = (x.created_at || '').slice(0, 10) || '—';
    const login = (x.last_login_at || '').slice(0, 10) || 'never';
    return `<tr><td class="mono">${escapeHtml(x.email || '')}</td><td class="mono">${created}</td><td class="mono">${login}</td></tr>`;
  }).join('');
  $('users-table').innerHTML = rows
    ? `<table><thead><tr><th>Email</th><th>Created</th><th>Last login</th></tr></thead><tbody>${rows}</tbody></table>`
    : '<span class="empty">no users yet</span>';
  $('tier-summary').textContent = users.length + ' total';
}

async function loadHealth() {
  const h = await get('/api/admin/health');
  if (!h) return;
  const db = h.database || {};
  $('uptime').textContent = fmtUptime((h.backend || {}).uptime_seconds);
  $('ver').textContent = (h.backend || {}).version || '—';
  const items = [
    ['sections', db.sections], ['rulings', db.rulings], ['private rulings', db.private_rulings],
    ['case summaries', db.case_summaries], ['treaties', db.treaties], ['mcp tokens', db.mcp_tokens],
  ];
  $('health').innerHTML = items.map(([k, v]) => `<div class="item"><b>${k}</b><span>${v == null ? '—' : v}</span></div>`).join('');
}

async function loadLogs() {
  const l = await get('/api/admin/logs?lines=120');
  if (!l || !l.logs) return;
  const html = l.logs.map(line => {
    const isErr = /error|traceback|exception|critical|fatal/i.test(line);
    return (isErr ? '<span class="err">' : '') + escapeHtml(line) + (isErr ? '</span>' : '');
  }).join('\n');
  $('logs').innerHTML = html || '<span class="empty">no log lines</span>';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function loadAll() {
  showErr(null);
  await Promise.all([loadStats(), loadUsers(), loadHealth(), loadLogs()]);
}

$('refresh').addEventListener('click', loadAll);
loadAll();
setInterval(loadAll, 60000);
