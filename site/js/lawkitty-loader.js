/* lawkitty loaders: claw scratches across the screen.
   LawkittyLoader.show({ text })  -> full-screen loader; returns a handle with .hide()
   LawkittyLoader.hide()          -> finishes with one big swipe, then fades out
   LawkittyLoader.inline(el, { size, text }) -> small looping scratch mark inside el; returns { stop() }
   No dependencies. Respects prefers-reduced-motion. */
(function (global) {
  var NS = 'http://www.w3.org/2000/svg';
  var C = { void: '#120d17', ink: '#f7eee2', muted: '#b8a9bf', gouge: '#3a2d45', tabby: '#ffb347', catnip: '#c6f432' };
  var LINES = ['Sharpening claws...', 'Sniffing through the ITAA...', 'Chasing a cross-reference...', 'Knocking rulings off the desk...', 'Checking under the couch for case law...'];
  var reduced = !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var HEAD = (document.currentScript && document.currentScript.getAttribute('data-cat')) || 'assets/lawkitty-cat-head.png';

  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function css() {
    if (document.getElementById('lk-loader-css')) return;
    var s = document.createElement('style'); s.id = 'lk-loader-css';
    s.textContent =
      '.lk-loader{position:fixed;inset:0;z-index:9999;background:' + C.void + ';display:flex;align-items:center;justify-content:center;overflow:hidden;transition:opacity .45s ease,transform .45s ease}' +
      '.lk-loader.lk-out{opacity:0;transform:scale(1.03)}' +
      '.lk-loader svg.lk-scratches{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}' +
      '.lk-loader .lk-core{position:relative;display:flex;flex-direction:column;align-items:center;gap:20px;text-align:center;font-family:Figtree,ui-sans-serif,system-ui,sans-serif}' +
      '.lk-loader .lk-cat{width:132px;height:auto;animation:lk-cat-bob 1.8s ease-in-out infinite;transform-origin:50% 90%}' +
      '.lk-loader .lk-text{font-size:17px;line-height:24px;font-weight:600;color:' + C.muted + ';min-height:24px;transition:opacity .25s}' +
      '.lk-loader.lk-shake{animation:lk-shake .22s linear}' +
      '@keyframes lk-cat-bob{0%,100%{transform:translateY(0) rotate(0)}50%{transform:translateY(-6px) rotate(-3deg)}}' +
      '@keyframes lk-shake{0%,100%{translate:0 0}25%{translate:3px -2px}50%{translate:-3px 2px}75%{translate:2px 1px}}' +
      '.lk-inline{display:inline-flex;align-items:center;gap:12px;font-family:Figtree,ui-sans-serif,system-ui,sans-serif;color:' + C.muted + ';font-size:14px;font-weight:600}' +
      '.lk-inline svg{flex-shrink:0;overflow:visible}' +
      '@media (prefers-reduced-motion:reduce){.lk-loader .lk-cat{animation:none}}';
    document.head.appendChild(s);
  }

  // one tapered claw mark along a gentle curve, from (x0,y0) to (x1,y1)
  function clawPath(x0, y0, x1, y1, width, bend) {
    var mx = (x0 + x1) / 2, my = (y0 + y1) / 2, dx = x1 - x0, dy = y1 - y0, len = Math.hypot(dx, dy);
    var nx = -dy / len, ny = dx / len, cx = mx + nx * bend, cy = my + ny * bend;
    var N = 28, L = [], R = [], centre = [];
    for (var i = 0; i <= N; i++) {
      var t = i / N, it = 1 - t;
      var x = it * it * x0 + 2 * it * t * cx + t * t * x1, y = it * it * y0 + 2 * it * t * cy + t * t * y1;
      var tx = 2 * it * (cx - x0) + 2 * t * (x1 - cx), ty = 2 * it * (cy - y0) + 2 * t * (y1 - cy), tl = Math.hypot(tx, ty) || 1;
      var w = width * Math.pow(Math.sin(Math.PI * Math.min(1, t * 1.15)), 0.75) * (1 - 0.35 * t);
      var ox = -ty / tl * w / 2, oy = tx / tl * w / 2;
      L.push([x + ox, y + oy]); R.push([x - ox, y - oy]); centre.push([x, y]);
    }
    var d = 'M' + L.map(function (p) { return p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' L');
    d += ' L' + R.reverse().map(function (p) { return p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' L') + ' Z';
    var c = 'M' + centre.map(function (p) { return p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' L');
    return { d: d, c: c, len: len * 1.08, end: centre[N], dir: [dx / len, dy / len] };
  }

  var uid = 0;
  // a set of four parallel claw marks, drawn one after another like a swipe
  function swipe(svg, box, opts) {
    opts = opts || {};
    var W = box.w, H = box.h;
    var big = !!opts.big;
    var len = big ? Math.hypot(W, H) * 0.9 : Math.min(W, H) * (0.34 + Math.random() * 0.2);
    var ang = (big ? -0.62 : (-0.9 + Math.random() * 0.5)) + (Math.random() < 0.5 ? 0 : Math.PI);
    var cx = big ? W / 2 : W * (0.15 + Math.random() * 0.7), cy = big ? H / 2 : H * (0.15 + Math.random() * 0.7);
    var ux = Math.cos(ang), uy = Math.sin(ang), px = -uy, py = ux;
    var gap = big ? Math.min(W, H) * 0.07 : len * 0.1, width = big ? Math.min(W, H) * 0.028 : len * 0.045;
    var bend = len * 0.06 * (Math.random() < 0.5 ? -1 : 1);
    var g = el('g', { 'class': 'lk-swipe' }, svg);
    var marks = [];
    for (var k = 0; k < 4; k++) {
      var off = (k - 1.5) * gap, shorten = (k === 0 || k === 3) ? 0.82 : 1;
      var l = len * shorten, sx = cx + px * off - ux * l / 2, sy = cy + py * off - uy * l / 2;
      var ex = sx + ux * l, ey = sy + uy * l;
      var cp = clawPath(sx, sy, ex, ey, width * (k === 0 || k === 3 ? 0.8 : 1), bend);
      var id = 'lkm' + (++uid);
      var mask = el('mask', { id: id, maskUnits: 'userSpaceOnUse' }, g);
      var mp = el('path', { d: cp.c, fill: 'none', stroke: '#fff', 'stroke-width': width * 3, 'stroke-linecap': 'round', 'stroke-dasharray': cp.len, 'stroke-dashoffset': cp.len }, mask);
      var mg = el('g', { mask: 'url(#' + id + ')' }, g);
      el('path', { d: cp.d, fill: C.tabby, opacity: '0.55', filter: 'url(#lk-glow)' }, mg);
      el('path', { d: cp.d, fill: C.ink }, mg);
      var inner = clawPath(sx + ux * l * 0.06, sy + uy * l * 0.06, ex - ux * l * 0.08, ey - uy * l * 0.08, width * 0.42, bend);
      el('path', { d: inner.d, fill: C.gouge }, mg);
      marks.push({ mp: mp, len: cp.len, end: cp.end, dir: cp.dir, delay: k * 55 });
    }
    // draw on
    marks.forEach(function (m) {
      m.mp.animate([{ strokeDashoffset: m.len }, { strokeDashoffset: 0 }], { duration: big ? 260 : 190, delay: m.delay, easing: 'cubic-bezier(.2,.7,.2,1)', fill: 'forwards' });
      // flecks thrown off the end of each claw
      for (var f = 0; f < (big ? 7 : 4); f++) {
        var fx = m.end[0], fy = m.end[1], s = 1 + Math.random() * (big ? 3 : 2);
        var dot = el('circle', { cx: fx, cy: fy, r: s, fill: Math.random() < 0.5 ? C.ink : C.tabby, opacity: 0 }, g);
        var tx = (m.dir[0] + (Math.random() - 0.5) * 1.2) * (20 + Math.random() * 50), ty = (m.dir[1] + (Math.random() - 0.5) * 1.2) * (20 + Math.random() * 50) + 18;
        dot.animate([{ opacity: 0.9, transform: 'translate(0,0)' }, { opacity: 0, transform: 'translate(' + tx + 'px,' + ty + 'px)' }], { duration: 520, delay: m.delay + 150, easing: 'cubic-bezier(.1,.6,.4,1)', fill: 'forwards' });
      }
    });
    // linger as a faint scar, then go
    g.animate([{ opacity: 1 }, { opacity: 1, offset: 0.35 }, { opacity: big ? 1 : 0.16 }], { duration: big ? 520 : 1400, delay: 260, fill: 'forwards' });
    if (!big) setTimeout(function () { g.animate([{ opacity: 0.16 }, { opacity: 0 }], { duration: 900, fill: 'forwards' }).onfinish = function () { g.remove(); }; }, 2600);
    return g;
  }
  function defs(svg) {
    var d = el('defs', {}, svg), f = el('filter', { id: 'lk-glow', x: '-20%', y: '-20%', width: '140%', height: '140%' }, d);
    el('feGaussianBlur', { stdDeviation: '5' }, f);
  }

  var current = null;
  function show(opts) {
    opts = opts || {};
    css();
    if (current) return current;
    var root = document.createElement('div'); root.className = 'lk-loader'; root.setAttribute('role', 'status'); root.setAttribute('aria-live', 'polite');
    var svg = el('svg', { 'class': 'lk-scratches', 'aria-hidden': 'true' }); root.appendChild(svg); defs(svg);
    var core = document.createElement('div'); core.className = 'lk-core';
    var img = document.createElement('img'); img.className = 'lk-cat'; img.alt = ''; img.src = opts.cat || HEAD;
    var txt = document.createElement('div'); txt.className = 'lk-text'; txt.textContent = opts.text || LINES[0];
    core.appendChild(img); core.appendChild(txt); root.appendChild(core);
    document.body.appendChild(root);
    var box = function () { var r = root.getBoundingClientRect(); svg.setAttribute('viewBox', '0 0 ' + r.width + ' ' + r.height); return { w: r.width, h: r.height }; };
    var li = 0, timers = [];
    if (!opts.text) timers.push(setInterval(function () { txt.style.opacity = 0; setTimeout(function () { li = (li + 1) % LINES.length; txt.textContent = LINES[li]; txt.style.opacity = 1; }, 250); }, 1900));
    function one() {
      if (reduced) return;
      swipe(svg, box());
      root.classList.remove('lk-shake'); void root.offsetWidth; root.classList.add('lk-shake');
    }
    if (!reduced) { timers.push(setTimeout(one, 250)); timers.push(setInterval(one, 1300)); }
    current = {
      el: root,
      hide: function () {
        timers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
        var done = function () { root.classList.add('lk-out'); setTimeout(function () { root.remove(); }, 480); };
        if (reduced) done(); else { swipe(svg, box(), { big: true }); root.classList.remove('lk-shake'); void root.offsetWidth; root.classList.add('lk-shake'); setTimeout(done, 420); }
        current = null;
      }
    };
    return current;
  }
  function hide() { if (current) current.hide(); }

  // small looping version for buttons, panels and search results
  function inline(target, opts) {
    opts = opts || {};
    css();
    var size = opts.size || 40;
    var wrap = document.createElement('span'); wrap.className = 'lk-inline'; wrap.setAttribute('role', 'status');
    var svg = el('svg', { width: size, height: size, viewBox: '0 0 100 100', 'aria-hidden': 'true' }); wrap.appendChild(svg); defs(svg);
    if (opts.text !== false) { var t = document.createElement('span'); t.textContent = opts.text || 'Sniffing through the ITAA...'; wrap.appendChild(t); }
    target.appendChild(wrap);
    var marks = [];
    for (var k = 0; k < 3; k++) {
      var off = (k - 1) * 20, cp = clawPath(24 + off, 84, 74 + off, 18, 11, 7);
      var id = 'lki' + (++uid), mask = el('mask', { id: id, maskUnits: 'userSpaceOnUse' }, svg);
      var mp = el('path', { d: cp.c, fill: 'none', stroke: '#fff', 'stroke-width': 30, 'stroke-linecap': 'round', 'stroke-dasharray': cp.len, 'stroke-dashoffset': cp.len }, mask);
      var g = el('g', { mask: 'url(#' + id + ')' }, svg);
      el('path', { d: cp.d, fill: C.tabby, opacity: '0.5', filter: 'url(#lk-glow)' }, g);
      el('path', { d: cp.d, fill: C.ink }, g);
      marks.push({ mp: mp, g: g, len: cp.len });
    }
    if (reduced) { marks.forEach(function (m) { m.mp.setAttribute('stroke-dashoffset', 0); }); return { stop: function () { wrap.remove(); } }; }
    var anims = [];
    marks.forEach(function (m, k) {
      anims.push(m.mp.animate([{ strokeDashoffset: m.len, offset: 0 }, { strokeDashoffset: m.len, offset: k * 0.07 }, { strokeDashoffset: 0, offset: 0.2 + k * 0.07 }, { strokeDashoffset: 0, offset: 1 }], { duration: 1400, iterations: Infinity, easing: 'cubic-bezier(.2,.7,.2,1)' }));
      anims.push(m.g.animate([{ opacity: 1, offset: 0 }, { opacity: 1, offset: 0.62 }, { opacity: 0, offset: 0.9 }, { opacity: 0, offset: 1 }], { duration: 1400, iterations: Infinity }));
    });
    return { stop: function () { anims.forEach(function (a) { a.cancel(); }); wrap.remove(); } };
  }

  global.LawkittyLoader = { show: show, hide: hide, inline: inline, lines: LINES };
})(window);
