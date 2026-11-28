(function () {
  var TOOLS = [], current = null, values = {};
  var PREFILL = {
    search: { query: 'capital gains' },
    fetch: { ref: 's 8-1 ITAA 1997' },
    get_definition: { act: 'itaa-1997', term: 'resident' },
    resolve_alias: { reference: 's 8-1' },
    related: { id: 'leg:itaa-1997:8-1' },
    graph_neighbourhood: { key: 'section:itaa-1997:8-1' },
    graph_path: { from_key: 'section:itaa-1997:8-1', to_key: 'public_ruling:TR 2024/1' },
    report_issue: { category: 'bug' }
  };

  var auth = document.getElementById('pg-auth'),
      main = document.getElementById('pg-main'),
      toolSel = document.getElementById('pg-tool'),
      desc = document.getElementById('pg-desc'),
      args = document.getElementById('pg-args'),
      raw = document.getElementById('pg-raw'),
      rawWrap = document.getElementById('pg-raw-wrap'),
      rawJson = document.getElementById('pg-raw-json'),
      runBtn = document.getElementById('pg-run'),
      err = document.getElementById('pg-err'),
      resultWrap = document.getElementById('pg-result-wrap'),
      result = document.getElementById('pg-result');

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function paramType(p) {
    var t = p.type;
    if (t && t !== 'null') return t;
    if (p.anyOf && p.anyOf.length) {
      for (var i = 0; i < p.anyOf.length; i++) {
        var o = p.anyOf[i];
        if (o && o.type && o.type !== 'null') return o.type;
      }
    }
    return 'string';
  }

  function paramItems(p) {
    if (p.items) return p.items;
    if (p.anyOf && p.anyOf.length) {
      for (var i = 0; i < p.anyOf.length; i++) {
        if (p.anyOf[i] && p.anyOf[i].items) return p.anyOf[i].items;
      }
    }
    return null;
  }

  function labelText(k, p, required) {
    return esc(k) + (required.indexOf(k) >= 0 ? ' *' : '') + ' <span class="muted">(' + esc(paramType(p)) + ')</span>';
  }

  function renderArgs(tool) {
    var props = (tool.parameters && tool.parameters.properties) || {};
    var required = (tool.parameters && tool.parameters.required) || [];
    var keys = Object.keys(props);
    values = {};
    var html = keys.length === 0 ? '<p class="muted">No arguments.</p>' : '';
    keys.forEach(function (k) {
      var p = props[k];
      var pt = paramType(p);
      var pre = (PREFILL[tool.name] || {})[k];
      var def = '';
      if (pre !== undefined && pre !== null) def = String(pre);
      else if (p.default !== undefined && p.default !== null) def = String(p.default);
      values[k] = pt === 'boolean' ? (def === 'true' ? 'true' : 'false') : def;
      html += '<div class="pg-arg-row">';
      if (pt === 'boolean') {
        html += '<label class="pg-check"><input type="checkbox" data-arg="' + esc(k) + '"' + (values[k] === 'true' ? ' checked' : '') + '> ' + labelText(k, p, required) + '</label>';
      } else {
        html += '<label for="arg-' + esc(k) + '">' + labelText(k, p, required) + '</label>';
        if (pt === 'string' && p.enum && p.enum.length) {
          html += '<select id="arg-' + esc(k) + '" data-arg="' + esc(k) + '"><option value="">—</option>';
          p.enum.forEach(function (v) { html += '<option value="' + esc(v) + '"' + (values[k] === v ? ' selected' : '') + '>' + esc(v) + '</option>'; });
          html += '</select>';
        } else if (pt === 'object') {
          html += '<textarea id="arg-' + esc(k) + '" data-arg="' + esc(k) + '" rows="3" placeholder="{&quot;key&quot;: &quot;value&quot;}">' + esc(values[k]) + '</textarea>';
        } else {
          html += '<input id="arg-' + esc(k) + '" data-arg="' + esc(k) + '" type="text" placeholder="' + (pt === 'array' ? 'a, b, c' : '') + '" value="' + esc(values[k]) + '">';
        }
      }
      html += '</div>';
    });
    args.innerHTML = html;
    args.querySelectorAll('[data-arg]').forEach(function (el) {
      var k = el.getAttribute('data-arg'), p = props[k];
      if (paramType(p) === 'boolean') {
        el.addEventListener('change', function () { values[k] = el.checked ? 'true' : 'false'; });
      } else {
        el.addEventListener('input', function () { values[k] = el.value; });
      }
    });
  }

  function selectTool(name) {
    current = TOOLS.find(function (t) { return t.name === name; });
    desc.textContent = current ? (current.description || '') : '';
    desc.hidden = !current;
    resultWrap.hidden = true;
    err.hidden = true;
    if (current) { renderArgs(current); if (raw.checked) syncRawJson(); }
  }

  function toTyped(v, p) {
    var t = paramType(p);
    if (t === 'integer') return parseInt(v, 10);
    if (t === 'number') return parseFloat(v);
    if (t === 'boolean') return v === 'true';
    if (t === 'array') {
      var items = paramItems(p);
      if (items && items.type === 'string') return v.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      try { return JSON.parse(v); } catch (e) { return v.split(',').map(function (s) { return s.trim(); }).filter(Boolean); }
    }
    if (t === 'object') { try { return JSON.parse(v); } catch (e) { return v; } }
    return v;
  }

  function formToArgs() {
    var props = (current && current.parameters && current.parameters.properties) || {};
    var out = {};
    Object.keys(values).forEach(function (k) {
      if (values[k] === '' || values[k] === undefined) return;
      out[k] = toTyped(values[k], props[k] || {});
    });
    return out;
  }

  function syncRawJson() {
    rawJson.value = JSON.stringify(formToArgs(), null, 2);
  }

  fetch('/api/playground/tools', { credentials: 'same-origin' })
    .then(function (r) {
      if (r.status === 401) throw { auth: true };
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (d) {
      TOOLS = (d.tools || []).slice().sort(function (a, b) { return a.name < b.name ? -1 : 1; });
      toolSel.innerHTML = TOOLS.map(function (t) { return '<option value="' + esc(t.name) + '">' + esc(t.name) + '</option>'; }).join('');
      main.hidden = false;
      var first = TOOLS.find(function (t) { return t.name === 'search'; }) || TOOLS[0];
      if (first) selectTool(first.name);
    })
    .catch(function (e) {
      if (e && e.auth) { auth.hidden = false; }
      else { err.textContent = "Couldn't load tools: " + (e && e.message ? e.message : e); err.hidden = false; }
    });

  toolSel.addEventListener('change', function () { selectTool(toolSel.value); });
  raw.addEventListener('change', function () {
    rawWrap.hidden = !raw.checked;
    if (raw.checked) syncRawJson();
  });

  runBtn.addEventListener('click', function () {
    if (!current) return;
    var callArgs = {};
    try {
      if (raw.checked) {
        callArgs = rawJson.value.trim() ? JSON.parse(rawJson.value) : {};
      } else {
        callArgs = formToArgs();
      }
    } catch (e) {
      err.textContent = 'Invalid JSON: ' + e.message;
      err.hidden = false;
      return;
    }
    runBtn.disabled = true;
    runBtn.textContent = 'Running…';
    err.hidden = true;
    resultWrap.hidden = true;
    fetch('/api/playground', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool: current.name, args: callArgs })
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (x) {
        if (x.ok) {
          var out = x.j.result !== undefined ? x.j.result : x.j;
          result.textContent = typeof out === 'string' ? out : JSON.stringify(out, null, 2);
          resultWrap.hidden = false;
        } else {
          err.textContent = (x.j && (x.j.error || x.j.detail)) || 'Request failed';
          err.hidden = false;
        }
      })
      .catch(function (e) { err.textContent = 'Request failed: ' + e.message; err.hidden = false; })
      .finally(function () { runBtn.disabled = false; runBtn.textContent = 'Run'; });
  });
})();
