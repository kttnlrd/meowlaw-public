function lkYarn(cv, opts) {
  opts = opts || {};
  function o(k, d) { return opts[k] == null ? d : opts[k]; }
  var W = o('W', 1440), H = o('H', 780), F = o('F', 900), D = o('D', 900), CH = o('CH', 260), HOR = o('HOR', 520), R = o('R', 95), CX0 = o('CX0', 720);
  var SC = R / 95;
  var SXMIN = o('sxMin', 120), SXMAX = o('sxMax', 1320), SXBEHIND = o('sxMaxBehind', 1400), ZMIN = o('zMin', 120), ZMAX = o('zMax', 560), ZBEHIND = o('zBehind', 400);
  var ROPE = o('rope', { x0: -1400, x1: 330, z0: 270, amp: 135, loops: 3.5, zWave: 60 });
  var AUTO = o('autoBat', true), DAMP = o('ropeFriction', 0.9), TUG = o('tug', 0.9), BAT = o('batEvery', [2600, 2200]), CAT = o('catShadow', true), POOL = o('pool', true), HIDE = o('hideChance', 0.12);
  var CAT_RECT = o('cat', { x: 1150, y: 364, w: 270, h: 351.45 }), CAT_Z = o('catZ', 297);
  var CAT_IMG = new Image(), catLoaded = false;
  if (o('showCat', true)) {
    CAT_IMG.onload = function () { catLoaded = true; dirty = true; };
    CAT_IMG.src = o('catSrc', 'assets/lawkitty-cat-sitting.png');
  }
  var dpr = Math.min(2, window.devicePixelRatio || 1);
  cv.width = W * dpr; cv.height = H * dpr;
  var ctx = cv.getContext('2d');
  var reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var seed = 11;
  function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
  function norm(v) { var l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function runit() { var z = rnd() * 2 - 1, t = rnd() * Math.PI * 2, r = Math.sqrt(1 - z * z); return [r * Math.cos(t), z, r * Math.sin(t)]; }
  function proj(x, y, z) { var s = F / (z + D); return [CX0 + x * s, HOR + (CH - y) * s, s]; }

  // strands: bands of near-parallel great circles
  var SAMPLES = 56, bands = [];
  for (var b = 0; b < 36; b++) {
    var ax = runit(), list = [];
    var u = norm(cross(ax, Math.abs(ax[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0])), v = cross(ax, u);
    var nk = 6 + Math.floor(rnd() * 6), off = (rnd() - 0.5) * 0.5, gap = 0.03 + rnd() * 0.012;
    for (var k = 0; k < nk; k++) {
      var hgt = off + (k - nk / 2) * gap, ring = Math.sqrt(Math.max(0, 1 - hgt * hgt));
      var rad = 1 + b * 0.0007 + rnd() * 0.006, ph = rnd() * 6.283, wob = rnd() * 0.012;
      var pts = new Float32Array(SAMPLES * 3);
      for (var i = 0; i < SAMPLES; i++) {
        var t = ph + i / SAMPLES * 6.283, hh = hgt + wob * Math.sin(t * 3 + ph);
        var c = Math.cos(t) * ring, s = Math.sin(t) * ring;
        var x = (u[0] * c + v[0] * s + ax[0] * hh) * rad, y = (u[1] * c + v[1] * s + ax[1] * hh) * rad, z = (u[2] * c + v[2] * s + ax[2] * hh) * rad;
        pts[i * 3] = x; pts[i * 3 + 1] = y; pts[i * 3 + 2] = z;
      }
      list.push(pts);
    }
    bands.push(list);
  }
  var fibres = [];
  for (var f = 0; f < 420; f++) {
    var n0 = runit(), tt = norm(cross(n0, runit()));
    fibres.push([n0, tt, 0.035 + rnd() * 0.07]);
  }
  // shade palette
  function mix(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
  var DARK = [74, 14, 50], BASE = [226, 84, 154], LITE = [255, 204, 234], PAL = [];
  for (var q = 0; q < 14; q++) {
    var tq = q / 13, col = tq < 0.62 ? mix(DARK, BASE, tq / 0.62) : mix(BASE, LITE, (tq - 0.62) / 0.38);
    PAL.push('rgb(' + (col[0] | 0) + ',' + (col[1] | 0) + ',' + (col[2] | 0) + ')');
  }
  var LIGHT = norm([-0.55, 0.85, -0.55]);

  // state
  var M = [1, 0, 0, 0, 1, 0, 0, 0, 1];
  function rotM(ax, ay, az, ang) {
    var c = Math.cos(ang), s = Math.sin(ang), t = 1 - c;
    var r = [t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay,
             t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax,
             t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c];
    var o = new Array(9);
    for (var i = 0; i < 3; i++) for (var j2 = 0; j2 < 3; j2++)
      o[i * 3 + j2] = r[i * 3] * M[j2] + r[i * 3 + 1] * M[3 + j2] + r[i * 3 + 2] * M[6 + j2];
    M = o;
  }
  function ortho() {
    var c0 = norm([M[0], M[3], M[6]]), c1 = [M[1], M[4], M[7]];
    var d = c0[0] * c1[0] + c0[1] * c1[1] + c0[2] * c1[2];
    c1 = norm([c1[0] - d * c0[0], c1[1] - d * c0[1], c1[2] - d * c0[2]]);
    var c2 = cross(c0, c1);
    M = [c0[0], c1[0], c2[0], c0[1], c1[1], c2[1], c0[2], c1[2], c2[2]];
  }

  // rope laid on the floor, generated as an irregular trochoid ending at the ball
  var SEG = 9 * SC, rope = [], raw = [];
  for (var s2 = 0; s2 <= 5000; s2++) {
    var uu = s2 / 5000, amp = ROPE.amp * (0.3 + 0.7 * Math.abs(Math.sin(6.283 * 1.7 * uu)));
    raw.push([ROPE.x0 + (ROPE.x1 - ROPE.x0) * uu + amp * Math.sin(6.283 * ROPE.loops * uu), ROPE.z0 + amp * Math.cos(6.283 * ROPE.loops * uu) + ROPE.zWave * Math.sin(6.283 * 1.3 * uu)]);
  }
  rope.push([raw[0][0], raw[0][1], raw[0][0], raw[0][1]]);
  for (var r2 = 1; r2 < raw.length; r2++) {
    var lp = rope[rope.length - 1];
    if (Math.hypot(raw[r2][0] - lp[0], raw[r2][1] - lp[1]) >= SEG) rope.push([raw[r2][0], raw[r2][1], raw[r2][0], raw[r2][1]]);
  }
  for (var sm0 = 0; sm0 < 6; sm0++) for (var q0 = 1; q0 < rope.length - 1; q0++) {
    var A0 = rope[q0 - 1], C0 = rope[q0 + 1], P0 = rope[q0];
    P0[0] += ((A0[0] + C0[0]) / 2 - P0[0]) * 0.5; P0[1] += ((A0[1] + C0[1]) / 2 - P0[1]) * 0.5; P0[2] = P0[0]; P0[3] = P0[1];
  }
  var last = rope[rope.length - 1];
  var ball = { x: last[0], z: last[1], y: 0, vx: 0, vz: 0, vy: 0, curl: 0 };
  var nextBat = 1400, tNow = 0, frame = 0, ropeMoving = 0;
  var MAXPTS = 1600;

  function bounds() {
    var s = F / (ball.z + D), r = R * s;
    var behind = ball.z > ZBEHIND;
    var xmin = (SXMIN + r - CX0) / s, xmax = ((behind ? SXBEHIND : SXMAX) - r - CX0) / s;
    return [xmin, xmax, behind];
  }
  function kick(vx, vz, hop) {
    ball.vx += vx; ball.vz += vz;
    var sp = Math.hypot(ball.vx, ball.vz);
    if (sp > 720 * SC) { ball.vx *= 720 * SC / sp; ball.vz *= 720 * SC / sp; }
    ball.curl = (rnd() - 0.5) * 2.4;
    if (ball.y < 2) ball.vy = (hop == null ? 90 + rnd() * 110 : hop) * SC;
  }
  function autoBat() {
    var s = F / (ball.z + D), r = R * s, bd = bounds();
    var hide = rnd() < HIDE && ball.z < ZBEHIND;
    var tz = hide ? ZBEHIND + 70 + rnd() * (ZMAX - ZBEHIND - 70) : ZMIN + 10 + rnd() * Math.min(250, ZBEHIND - ZMIN - 20);
    var st = F / (tz + D), rt = R * st;
    var txMin = (SXMIN + rt - CX0) / st, txMax = ((hide ? SXBEHIND - 70 : SXMAX - 10) - rt - CX0) / st;
    var tx = txMin + rnd() * (txMax - txMin);
    kick((tx - ball.x) * (1.3 + rnd() * 0.7), (tz - ball.z) * (1.3 + rnd() * 0.7));
    return hide;
  }
  function step(dt) {
    tNow += dt * 1000;
    if (AUTO && tNow > nextBat) { var hid = autoBat(); nextBat = tNow + (hid ? 1700 : BAT[0] + rnd() * BAT[1]); }
    if (AUTO && ball.z > ZBEHIND && nextBat - tNow > 1800) nextBat = tNow + 1800;
    var sp0 = Math.hypot(ball.vx, ball.vz);
    if (sp0 > 20) { var ca = ball.curl * dt * Math.min(1, sp0 / 220), cc = Math.cos(ca), ss = Math.sin(ca), vx0 = ball.vx; ball.vx = vx0 * cc - ball.vz * ss; ball.vz = vx0 * ss + ball.vz * cc; }
    var px = ball.x, pz = ball.z, wasBehind = ball.z > ZBEHIND;
    ball.x += ball.vx * dt; ball.z += ball.vz * dt;
    // hop
    if (ball.y > 0 || ball.vy > 0) { ball.vy -= 1500 * SC * dt; ball.y += ball.vy * dt; if (ball.y <= 0) { ball.y = 0; ball.vy = ball.vy < -120 * SC ? -ball.vy * 0.32 : 0; } }
    var fr = Math.pow(ball.y > 0 ? 0.8 : 0.3, dt); ball.vx *= fr; ball.vz *= fr;
    if (ball.z < ZMIN) { ball.z = ZMIN; ball.vz = Math.abs(ball.vz) * 0.5; }
    if (ball.z > ZMAX) { ball.z = ZMAX; ball.vz = -Math.abs(ball.vz) * 0.5; }
    var bd = bounds();
    if (ball.x < bd[0]) { ball.x = bd[0]; ball.vx = Math.abs(ball.vx) * 0.5; }
    if (ball.x > bd[1]) {
      if (wasBehind && !bd[2]) { ball.z = ZBEHIND + 0.5; ball.vz = Math.abs(ball.vz) * 0.45; }
      else { ball.x = bd[1]; ball.vx = -Math.abs(ball.vx) * 0.5; }
    }
    // rolling without slipping
    var dx = ball.x - px, dz = ball.z - pz, dist = Math.hypot(dx, dz);
    if (dist > 1e-4 && ball.y < 1) rotM(dz / dist, 0, -dx / dist, dist / R);
    if (++frame % 90 === 0) ortho();
    // yarn: verlet chain lying on the floor with friction; the ball tugs it and pays out new yarn
    var L = rope.length, i, p, moving = 0;
    for (i = 1; i < L; i++) {
      p = rope[i];
      var vx = (p[0] - p[2]) * DAMP, vz = (p[1] - p[3]) * DAMP;
      p[2] = p[0]; p[3] = p[1];
      if (vx * vx + vz * vz < 0.0004 * SC) continue;
      p[0] += vx; p[1] += vz; moving++;
    }
    var lp = rope[L - 1], dx = ball.x - lp[0], dz = ball.z - lp[1], dl = Math.hypot(dx, dz);
    if (ball.y < 3 * SC) {
      if (dl > SEG * o('payout', 6)) {
        var n = Math.floor((dl - SEG * 0.6) / SEG);
        for (i = 1; i <= n; i++) { var nx = lp[0] + dx * i / (n + 0.6), nz = lp[1] + dz * i / (n + 0.6); rope.push([nx, nz, nx, nz]); }
      }
      while (rope.length > 3) {
        var p2 = rope[rope.length - 2];
        if (Math.hypot(ball.x - p2[0], ball.z - p2[1]) < SEG * 0.75) rope.pop(); else break;
      }
    }
    L = rope.length; lp = rope[L - 1]; dx = ball.x - lp[0]; dz = ball.z - lp[1]; dl = Math.hypot(dx, dz);
    if (dl > SEG) { var k = TUG * (dl - SEG) / dl; lp[0] += dx * k; lp[1] += dz * k; }
    // stretch-only constraints so a tug travels down the yarn; point 0 is pinned off screen
    for (var it = 0; it < 6; it++) {
      for (i = L - 1; i >= 1; i--) {
        var a0 = rope[i - 1], b0 = rope[i], ex = b0[0] - a0[0], ez = b0[1] - a0[1], d = Math.hypot(ex, ez);
        if (d <= SEG || d < 1e-6) continue;
        var c = (d - SEG) / d;
        if (i - 1 === 0) { b0[0] -= ex * c; b0[1] -= ez * c; }
        else { b0[0] -= ex * c * 0.5; b0[1] -= ez * c * 0.5; a0[0] += ex * c * 0.5; a0[1] += ez * c * 0.5; }
      }
    }
    if (rope.length > MAXPTS) rope.splice(0, rope.length - MAXPTS);
    ropeMoving = moving;
  }

  // ---------- drawing ----------
  function chaikin(pts, ws, sh) {
    if (pts.length < 3) return [pts, ws, sh];
    var P = [pts[0]], Wd = [ws[0]], S = [sh[0]];
    for (var i = 0; i < pts.length - 1; i++) {
      var a = pts[i], b = pts[i + 1], sa = sh[i], sb = sh[i + 1];
      P.push([a[0] * 0.75 + b[0] * 0.25, a[1] * 0.75 + b[1] * 0.25], [a[0] * 0.25 + b[0] * 0.75, a[1] * 0.25 + b[1] * 0.75]);
      S.push([sa[0] * 0.75 + sb[0] * 0.25, sa[1] * 0.75 + sb[1] * 0.25], [sa[0] * 0.25 + sb[0] * 0.75, sa[1] * 0.25 + sb[1] * 0.75]);
      Wd.push(ws[i] * 0.75 + ws[i + 1] * 0.25, ws[i] * 0.25 + ws[i + 1] * 0.75);
    }
    P.push(pts[pts.length - 1]); Wd.push(ws[ws.length - 1]); S.push(sh[sh.length - 1]);
    return [P, Wd, S];
  }
  function polyPath(pts) {
    var p = new Path2D(); p.moveTo(pts[0][0], pts[0][1]);
    for (var i = 1; i < pts.length; i++) p.lineTo(pts[i][0], pts[i][1]);
    return p;
  }
  function strokeRun(pts0, ws0, sh0, seed0) {
    if (pts0.length < 2) return;
    var sm = chaikin(pts0, ws0, sh0); sm = chaikin(sm[0], sm[1], sm[2]);
    var pts = sm[0], ws = sm[1], shadow = sm[2];
    var i, avg = 0;
    for (i = 0; i < ws.length; i++) avg += ws[i]; avg /= ws.length;
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    // soft floor shadow in two passes
    var sp = polyPath(shadow);
    ctx.strokeStyle = 'rgba(0,0,0,0.12)'; ctx.lineWidth = avg * 3; ctx.stroke(sp);
    ctx.strokeStyle = 'rgba(0,0,0,0.26)'; ctx.lineWidth = avg * 1.7; ctx.stroke(sp);
    var body = polyPath(pts);
    // fuzzy halo so the edge isn't hard
    ctx.strokeStyle = 'rgba(236,110,178,0.28)'; ctx.lineWidth = avg * 1.45; ctx.stroke(body);
    ctx.strokeStyle = '#d24d93';
    for (i = 0; i < pts.length - 1; i += 16) {
      var e = Math.min(pts.length - 1, i + 16), w = 0;
      ctx.beginPath(); ctx.moveTo(pts[i][0], pts[i][1]);
      for (var j = i + 1; j <= e; j++) { ctx.lineTo(pts[j][0], pts[j][1]); w += ws[j]; }
      ctx.lineWidth = w / (e - i); ctx.stroke();
    }
    var acc = 0, tick = new Path2D(), hi = new Path2D(), fz = new Path2D(), started = false;
    for (i = 0; i < pts.length - 1; i++) {
      var x0 = pts[i][0], y0 = pts[i][1], x1 = pts[i + 1][0], y1 = pts[i + 1][1];
      var sl = Math.hypot(x1 - x0, y1 - y0); if (sl < 0.01) continue;
      var tx = (x1 - x0) / sl, ty = (y1 - y0) / sl, nx = -ty, ny = tx, ww = ws[i];
      if (ny > 0) { nx = -nx; ny = -ny; }
      var hx = x0 + nx * ww * 0.2, hy = y0 + ny * ww * 0.2;
      if (!started) { hi.moveTo(hx, hy); started = true; } else hi.lineTo(hx, hy);
      var stepL = Math.max(1.6, ww * 0.85);
      while (acc < sl) {
        var cx = x0 + tx * acc, cy = y0 + ty * acc, h = ww * 0.42;
        tick.moveTo(cx - nx * h - tx * h * 0.7, cy - ny * h - ty * h * 0.7);
        tick.lineTo(cx + nx * h + tx * h * 0.7, cy + ny * h + ty * h * 0.7);
        acc += stepL;
      }
      acc -= sl;
      var hsh = ((i + seed0) * 2654435761) >>> 0;
      if (hsh % 9 === 0) {
        var side = (hsh & 16) ? 1 : -1, L2 = ww * (0.6 + (hsh % 7) * 0.12);
        var bx = x0 + nx * ww * 0.45 * side, by = y0 + ny * ww * 0.45 * side;
        fz.moveTo(bx, by); fz.quadraticCurveTo(bx + nx * L2 * side + tx * L2 * 0.6, by + ny * L2 * side + ty * L2 * 0.6, bx + nx * L2 * 0.4 * side + tx * L2 * 1.3, by + ny * L2 * 0.4 * side + ty * L2 * 1.3);
      }
    }
    ctx.lineCap = 'round';
    ctx.strokeStyle = 'rgba(92,16,60,0.38)'; ctx.lineWidth = Math.max(0.7, avg * 0.26); ctx.stroke(tick);
    ctx.strokeStyle = 'rgba(255,196,230,0.32)'; ctx.lineWidth = Math.max(0.6, avg * 0.22); ctx.stroke(hi);
    ctx.strokeStyle = 'rgba(246,160,208,0.45)'; ctx.lineWidth = Math.max(0.5, avg * 0.1); ctx.stroke(fz);
  }

  function ropeGeometry() {
    // floor part up to the edge of the ball, then a lifted tail to the anchor
    var L = rope.length, j = L - 1;
    while (j > 0 && Math.hypot(rope[j][0] - ball.x, rope[j][1] - ball.z) < R * 1.45) j--;
    var pts3 = [];
    for (var i = 0; i <= j; i++) pts3.push([rope[i][0], 0, rope[i][1]]);
    var e = rope[j], dxr = e[0] - ball.x, dzr = e[1] - ball.z, dl = Math.hypot(dxr, dzr) || 1;
    var ax = ball.x + dxr / dl * R * 0.93, az = ball.z + dzr / dl * R * 0.93, ay = ball.y + R * 0.3;
    for (var k = 1; k <= 8; k++) {
      var t = k / 8, sm = t * t * (3 - 2 * t);
      pts3.push([e[0] + (ax - e[0]) * t, ay * t * t, e[1] + (az - e[1]) * t]);
    }
    return pts3;
  }

  function drawBall(bx, by, bz) {
    var cy = by + R, P = proj(bx, cy, bz), sx = P[0], sy = P[1], s = P[2], r = R * s;
    var cam = norm([-bx, CH - cy, -D - bz]);
    // body
    var g = ctx.createRadialGradient(sx - r * 0.38, sy - r * 0.42, r * 0.05, sx, sy, r * 1.05);
    g.addColorStop(0, '#d9559a'); g.addColorStop(0.55, '#a33572'); g.addColorStop(1, '#3a0c2a');
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(sx, sy, r * 0.99, 0, 6.283); ctx.fill();
    var sw = Math.max(1.2, 0.03 * R * s);
    ctx.lineCap = 'round';
    for (var b = 0; b < bands.length; b++) {
      var under = new Path2D(), buckets = [];
      for (var q = 0; q < PAL.length; q++) buckets.push(null);
      var list = bands[b];
      for (var k = 0; k < list.length; k += (lite ? 2 : 1)) {
        var pts = list[k], prevV = false, pxs = 0, pys = 0;
        for (var i = 0; i <= SAMPLES; i++) {
          var ii = (i % SAMPLES) * 3, x = pts[ii], y = pts[ii + 1], z = pts[ii + 2];
          var nx = M[0] * x + M[1] * y + M[2] * z, ny = M[3] * x + M[4] * y + M[5] * z, nz = M[6] * x + M[7] * y + M[8] * z;
          var facing = nx * cam[0] + ny * cam[1] + nz * cam[2];
          var vis = facing > 0.015;
          var pp = proj(bx + nx * R, cy + ny * R, bz + nz * R);
          if (vis && prevV) {
            var dif = Math.max(0, nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2]);
            var val = (0.3 + 0.7 * dif) * (0.45 + 0.55 * Math.min(1, facing * 1.3));
            var bi = Math.min(PAL.length - 1, Math.floor(val * PAL.length));
            if (!buckets[bi]) buckets[bi] = new Path2D();
            buckets[bi].moveTo(pxs, pys); buckets[bi].lineTo(pp[0], pp[1]);
            under.moveTo(pxs, pys); under.lineTo(pp[0], pp[1]);
          }
          prevV = vis; pxs = pp[0]; pys = pp[1];
        }
      }
      ctx.strokeStyle = 'rgba(40,6,28,0.6)'; ctx.lineWidth = sw * 1.75; ctx.stroke(under);
      ctx.lineWidth = sw;
      for (var q2 = 0; q2 < buckets.length; q2++) if (buckets[q2]) { ctx.strokeStyle = PAL[q2]; ctx.stroke(buckets[q2]); }
    }
    // stray fibres on the silhouette
    var fp = new Path2D();
    for (var f = 0; f < fibres.length; f++) {
      var n0 = fibres[f][0], t0 = fibres[f][1], len = fibres[f][2];
      var nx2 = M[0] * n0[0] + M[1] * n0[1] + M[2] * n0[2], ny2 = M[3] * n0[0] + M[4] * n0[1] + M[5] * n0[2], nz2 = M[6] * n0[0] + M[7] * n0[1] + M[8] * n0[2];
      var fc = nx2 * cam[0] + ny2 * cam[1] + nz2 * cam[2];
      if (fc < -0.02 || fc > 0.3) continue;
      var tx = M[0] * t0[0] + M[1] * t0[1] + M[2] * t0[2], ty = M[3] * t0[0] + M[4] * t0[1] + M[5] * t0[2], tz = M[6] * t0[0] + M[7] * t0[1] + M[8] * t0[2];
      var a = proj(bx + nx2 * R, cy + ny2 * R, bz + nz2 * R);
      var e = proj(bx + (nx2 * (1 + len * 0.7) + tx * len) * R, cy + (ny2 * (1 + len * 0.7) + ty * len) * R, bz + (nz2 * (1 + len * 0.7) + tz * len) * R);
      var mx = (a[0] + e[0]) / 2 + (e[1] - a[1]) * 0.25, my = (a[1] + e[1]) / 2 - (e[0] - a[0]) * 0.25;
      fp.moveTo(a[0], a[1]); fp.quadraticCurveTo(mx, my, e[0], e[1]);
    }
    ctx.strokeStyle = 'rgba(246,150,205,0.55)'; ctx.lineWidth = Math.max(0.6, 0.009 * R * s); ctx.stroke(fp);
    // soft sheen and occlusion, fixed to the light
    var sh = ctx.createRadialGradient(sx - r * 0.42, sy - r * 0.48, 0, sx - r * 0.42, sy - r * 0.48, r * 0.75);
    sh.addColorStop(0, 'rgba(255,236,247,0.22)'); sh.addColorStop(1, 'rgba(255,236,247,0)');
    ctx.fillStyle = sh; ctx.beginPath(); ctx.arc(sx, sy, r, 0, 6.283); ctx.fill();
    var ao = ctx.createRadialGradient(sx - r * 0.2, sy - r * 0.25, r * 0.55, sx, sy, r * 1.02);
    ao.addColorStop(0, 'rgba(20,4,14,0)'); ao.addColorStop(1, 'rgba(20,4,14,0.55)');
    ctx.fillStyle = ao; ctx.beginPath(); ctx.arc(sx, sy, r, 0, 6.283); ctx.fill();
    // floor bounce light on the underside
    var bl = ctx.createRadialGradient(sx, sy + r, 0, sx, sy + r, r * 0.8);
    bl.addColorStop(0, 'rgba(255,122,200,0.16)'); bl.addColorStop(1, 'rgba(255,122,200,0)');
    ctx.fillStyle = bl; ctx.beginPath(); ctx.arc(sx, sy, r, 0, 6.283); ctx.fill();
    return [sx, sy, r];
  }

  function floorEllipse(x, z, rx, alpha, squash) {
    var P = proj(x, 0, z);
    ctx.save(); ctx.translate(P[0], P[1]); ctx.scale(1, squash);
    var g = ctx.createRadialGradient(0, 0, 0, 0, 0, rx * P[2]);
    g.addColorStop(0, 'rgba(0,0,0,' + alpha + ')'); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, rx * P[2], 0, 6.283); ctx.fill(); ctx.restore();
  }

  var api = { ballScreen: null };
  function drawCat() {
    if (!catLoaded) return;
    ctx.drawImage(CAT_IMG, CAT_RECT.x, CAT_RECT.y, CAT_RECT.w, CAT_RECT.h);
  }
  function draw() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    // a soft pool of light on the floor
    if (POOL) {
      ctx.save(); ctx.translate(980, 700); ctx.scale(1, 0.2);
      var pool = ctx.createRadialGradient(0, 0, 0, 0, 0, 640);
      pool.addColorStop(0, 'rgba(255,214,236,0.07)'); pool.addColorStop(1, 'rgba(255,214,236,0)');
      ctx.fillStyle = pool; ctx.beginPath(); ctx.arc(0, 0, 640, 0, 6.283); ctx.fill(); ctx.restore();
    }
    if (CAT) floorEllipse(760, 300, 190, 0.7, 0.16);
    var g3 = ropeGeometry(), split = ball.z - R * 0.15;
    var back = [], front = [], cur = null, curFront = null;
    for (var i = 0; i < g3.length; i++) {
      var p = g3[i], isFront = p[2] < split;
      if (cur === null || isFront !== curFront) {
        var run = { p: [], w: [], s: [] };
        if (cur && cur.p.length) { run.p.push(cur.p[cur.p.length - 1]); run.w.push(cur.w[cur.w.length - 1]); run.s.push(cur.s[cur.s.length - 1]); }
        (isFront ? front : back).push(run); cur = run; curFront = isFront;
      }
      var a = proj(p[0], p[1] + 4.1 * SC, p[2]), sh = proj(p[0] + 2 * SC, 0, p[2] - 2 * SC);
      cur.p.push([a[0], a[1]]); cur.w.push(8.2 * SC * a[2]); cur.s.push([sh[0] + 1.5, sh[1] + 1.5]);
    }
    for (var b2 = 0; b2 < back.length; b2++) strokeRun(back[b2].p, back[b2].w, back[b2].s, b2 * 97);
    var hopK = Math.max(0.35, 1 - ball.y / (160 * SC));
    var ballBehindCat = ball.z >= CAT_Z;
    if (ballBehindCat) {
      floorEllipse(ball.x + 10 * SC, ball.z + 14 * SC, R * 1.25, 0.72 * hopK, 0.24);
      floorEllipse(ball.x + 4 * SC, ball.z + 4 * SC, R * 0.6, 0.65 * hopK, 0.22);
      api.ballScreen = drawBall(ball.x, ball.y, ball.z);
    }
    drawCat();
    if (!ballBehindCat) {
      floorEllipse(ball.x + 10 * SC, ball.z + 14 * SC, R * 1.25, 0.72 * hopK, 0.24);
      floorEllipse(ball.x + 4 * SC, ball.z + 4 * SC, R * 0.6, 0.65 * hopK, 0.22);
      api.ballScreen = drawBall(ball.x, ball.y, ball.z);
    }
    for (var f2 = 0; f2 < front.length; f2++) strokeRun(front[f2].p, front[f2].w, front[f2].s, 500 + f2 * 97);
  }

  var raf = 0, lastT = 0, stopped = false, dirty = true, slow = 0, lite = false, lastSig = '';
  function loop(t) {
    if (stopped) return;
    var dt = lastT ? Math.min(0.033, (t - lastT) / 1000) : 0.016; lastT = t;
    step(dt);
    var sig = ball.x.toFixed(2) + ',' + ball.z.toFixed(2) + ',' + ball.y.toFixed(2) + ',' + rope.length + (ropeMoving ? ',' + tNow : '');
    if (sig !== lastSig || dirty) {
      var t0 = performance.now(); draw(); lastSig = sig; dirty = false;
      var ms = performance.now() - t0;
      slow = slow * 0.9 + (ms > 14 ? 1 : 0) * 0.1;
      if (!lite && slow > 0.6) lite = true;
    }
    raf = requestAnimationFrame(loop);
  }
  if (opts.warmup) for (var w2 = 0; w2 < opts.warmup; w2++) step(1 / 60);
  if (reduced) draw(); else raf = requestAnimationFrame(loop);

  api.stop = function () { stopped = true; cancelAnimationFrame(raf); };
  api.hit = function (mx, my) {
    var b = api.ballScreen; return !!b && Math.hypot(mx - b[0], my - b[1]) <= b[2] + 8;
  };
  // click: a small push away from where the ball was clicked, with a little hop
  api.click = function (mx, my) {
    var b = api.ballScreen; if (!b || reduced || !api.hit(mx, my)) return false;
    var ux = b[0] - mx, uy = b[1] - my, ul = Math.hypot(ux, uy) || 1;
    if (ul < b[2] * 0.25) { ux = rnd() - 0.5; uy = rnd() - 0.5; ul = Math.hypot(ux, uy) || 1; }
    kick(ux / ul * 640 * SC, -uy / ul * 460 * SC, 160);
    nextBat = tNow + 3500;
    return true;
  };
  // scroll: roll a little in proportion to how far the page moved
  api.nudge = function (amount) {
    if (reduced) return;
    var a = Math.max(-260, Math.min(260, amount)) * SC;
    ball.vx += a; ball.curl = (rnd() - 0.5) * 1.2;
  };
  api._state = function () { return { ball: ball, n: rope.length }; };
  return api;
}
