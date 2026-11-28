//! The ball of yarn on the home screen.
//!
//! A port of `frontend/public/assets/yarn.js` (as the web app calls it: no cat, no cat shadow):
//! the same wound ball, the same rope laid on the floor, the same physics and random numbers.
//! Each frame is drawn with tiny-skia, which strokes and fills as an HTML canvas does, into an
//! image the window shows.

use tiny_skia::{Color, FillRule, GradientStop, LineCap, LineJoin, Paint, Path, PathBuilder, Pixmap, Point, RadialGradient, Shader, SpreadMode, Stroke, Transform};

const W: f64 = 1440.0;
const H: f64 = 780.0;
const F: f64 = 900.0;
const D: f64 = 900.0;
const CH: f64 = 260.0;
const HOR: f64 = 520.0;
const R: f64 = 95.0;
const CX0: f64 = 720.0;
const SC: f64 = 1.0;
const SXMIN: f64 = 120.0;
const SXMAX: f64 = 1320.0;
const SXBEHIND: f64 = 1400.0;
const ZMIN: f64 = 120.0;
const ZMAX: f64 = 560.0;
const ZBEHIND: f64 = 400.0;
const DAMP: f64 = 0.9;
const TUG: f64 = 0.9;
const BAT: [f64; 2] = [2600.0, 2200.0];
const HIDE: f64 = 0.12;
const SAMPLES: usize = 56;
const SEG: f64 = 9.0 * SC;
const MAXPTS: usize = 1600;
const TAU: f64 = 6.283;

type V3 = [f64; 3];

fn norm(v: V3) -> V3 {
    let l = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
    let l = if l == 0.0 { 1.0 } else { l };
    [v[0] / l, v[1] / l, v[2] / l]
}

fn cross(a: V3, b: V3) -> V3 {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}

fn proj(x: f64, y: f64, z: f64) -> [f64; 3] {
    let s = F / (z + D);
    [CX0 + x * s, HOR + (CH - y) * s, s]
}

struct Ball {
    x: f64,
    z: f64,
    y: f64,
    vx: f64,
    vz: f64,
    vy: f64,
    curl: f64,
}

pub struct Yarn {
    seed: f64,
    bands: Vec<Vec<Vec<f32>>>,
    fibres: Vec<(V3, V3, f64)>,
    pal: Vec<Color>,
    light: V3,
    m: [f64; 9],
    rope: Vec<[f64; 4]>,
    ball: Ball,
    next_bat: f64,
    t_now: f64,
    frame: u64,
    rope_moving: usize,
    /// Where the ball was last drawn, in canvas units: centre and radius.
    ball_screen: Option<[f64; 3]>,
    last_sig: String,
    lite: bool,
    slow: f64,
}

struct Run {
    p: Vec<[f64; 2]>,
    w: Vec<f64>,
    s: Vec<[f64; 2]>,
}

fn rgba(r: u8, g: u8, b: u8, a: f64) -> Color {
    Color::from_rgba8(r, g, b, (a * 255.0).round().clamp(0.0, 255.0) as u8)
}

fn solid(color: Color) -> Paint<'static> {
    let mut paint = Paint::default();
    paint.set_color(color);
    paint.anti_alias = true;
    paint
}

fn round_stroke(width: f64) -> Stroke {
    Stroke { width: width as f32, line_cap: LineCap::Round, line_join: LineJoin::Round, ..Stroke::default() }
}

fn poly_path(pts: &[[f64; 2]]) -> Option<Path> {
    let mut pb = PathBuilder::new();
    pb.move_to(pts[0][0] as f32, pts[0][1] as f32);
    for p in &pts[1..] {
        pb.line_to(p[0] as f32, p[1] as f32);
    }
    pb.finish()
}

fn chaikin(run: Run) -> Run {
    if run.p.len() < 3 {
        return run;
    }
    let n = run.p.len();
    let mut out = Run { p: vec![run.p[0]], w: vec![run.w[0]], s: vec![run.s[0]] };
    for i in 0..n - 1 {
        let (a, b, sa, sb) = (run.p[i], run.p[i + 1], run.s[i], run.s[i + 1]);
        out.p.push([a[0] * 0.75 + b[0] * 0.25, a[1] * 0.75 + b[1] * 0.25]);
        out.p.push([a[0] * 0.25 + b[0] * 0.75, a[1] * 0.25 + b[1] * 0.75]);
        out.s.push([sa[0] * 0.75 + sb[0] * 0.25, sa[1] * 0.75 + sb[1] * 0.25]);
        out.s.push([sa[0] * 0.25 + sb[0] * 0.75, sa[1] * 0.25 + sb[1] * 0.75]);
        out.w.push(run.w[i] * 0.75 + run.w[i + 1] * 0.25);
        out.w.push(run.w[i] * 0.25 + run.w[i + 1] * 0.75);
    }
    out.p.push(run.p[n - 1]);
    out.w.push(run.w[n - 1]);
    out.s.push(run.s[n - 1]);
    out
}

impl Yarn {
    fn rnd(&mut self) -> f64 {
        self.seed = (self.seed * 16807.0) % 2147483647.0;
        self.seed / 2147483647.0
    }

    fn runit(&mut self) -> V3 {
        let z = self.rnd() * 2.0 - 1.0;
        let t = self.rnd() * std::f64::consts::PI * 2.0;
        let r = (1.0 - z * z).sqrt();
        [r * t.cos(), z, r * t.sin()]
    }

    pub fn new() -> Yarn {
        let mut y = Yarn {
            seed: 11.0,
            bands: Vec::new(),
            fibres: Vec::new(),
            pal: Vec::new(),
            light: norm([-0.55, 0.85, -0.55]),
            m: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            rope: Vec::new(),
            ball: Ball { x: 0.0, z: 0.0, y: 0.0, vx: 0.0, vz: 0.0, vy: 0.0, curl: 0.0 },
            next_bat: 1400.0,
            t_now: 0.0,
            frame: 0,
            rope_moving: 0,
            ball_screen: None,
            last_sig: String::new(),
            lite: false,
            slow: 0.0,
        };
        // Strands: bands of near-parallel great circles.
        for b in 0..36 {
            let ax = y.runit();
            let u = norm(cross(ax, if ax[1].abs() < 0.9 { [0.0, 1.0, 0.0] } else { [1.0, 0.0, 0.0] }));
            let v = cross(ax, u);
            let nk = 6 + (y.rnd() * 6.0).floor() as usize;
            let off = (y.rnd() - 0.5) * 0.5;
            let gap = 0.03 + y.rnd() * 0.012;
            let mut list = Vec::new();
            for k in 0..nk {
                let hgt = off + (k as f64 - nk as f64 / 2.0) * gap;
                let ring = (1.0 - hgt * hgt).max(0.0).sqrt();
                let rad = 1.0 + b as f64 * 0.0007 + y.rnd() * 0.006;
                let ph = y.rnd() * TAU;
                let wob = y.rnd() * 0.012;
                let mut pts = vec![0f32; SAMPLES * 3];
                for i in 0..SAMPLES {
                    let t = ph + i as f64 / SAMPLES as f64 * TAU;
                    let hh = hgt + wob * (t * 3.0 + ph).sin();
                    let (c, s) = (t.cos() * ring, t.sin() * ring);
                    pts[i * 3] = ((u[0] * c + v[0] * s + ax[0] * hh) * rad) as f32;
                    pts[i * 3 + 1] = ((u[1] * c + v[1] * s + ax[1] * hh) * rad) as f32;
                    pts[i * 3 + 2] = ((u[2] * c + v[2] * s + ax[2] * hh) * rad) as f32;
                }
                list.push(pts);
            }
            y.bands.push(list);
        }
        for _ in 0..420 {
            let n0 = y.runit();
            let other = y.runit();
            let tt = norm(cross(n0, other));
            let len = 0.035 + y.rnd() * 0.07;
            y.fibres.push((n0, tt, len));
        }
        // Shade palette.
        let mix = |a: V3, b: V3, t: f64| [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
        let (dark, base, lite) = ([74.0, 14.0, 50.0], [226.0, 84.0, 154.0], [255.0, 204.0, 234.0]);
        for q in 0..14 {
            let tq = q as f64 / 13.0;
            let col = if tq < 0.62 { mix(dark, base, tq / 0.62) } else { mix(base, lite, (tq - 0.62) / 0.38) };
            y.pal.push(Color::from_rgba8(col[0] as u8, col[1] as u8, col[2] as u8, 255));
        }
        // Rope laid on the floor: an irregular trochoid ending at the ball.
        let (x0, x1, z0, amp0, loops, z_wave) = (-1400.0, 330.0, 270.0, 135.0, 3.5, 60.0);
        let mut raw = Vec::with_capacity(5001);
        for s2 in 0..=5000 {
            let uu = s2 as f64 / 5000.0;
            let amp = amp0 * (0.3 + 0.7 * (TAU * 1.7 * uu).sin().abs());
            raw.push([x0 + (x1 - x0) * uu + amp * (TAU * loops * uu).sin(), z0 + amp * (TAU * loops * uu).cos() + z_wave * (TAU * 1.3 * uu).sin()]);
        }
        y.rope.push([raw[0][0], raw[0][1], raw[0][0], raw[0][1]]);
        for r in &raw[1..] {
            let lp = y.rope[y.rope.len() - 1];
            if (r[0] - lp[0]).hypot(r[1] - lp[1]) >= SEG {
                y.rope.push([r[0], r[1], r[0], r[1]]);
            }
        }
        for _ in 0..6 {
            for q in 1..y.rope.len() - 1 {
                let (a, c) = (y.rope[q - 1], y.rope[q + 1]);
                let p = &mut y.rope[q];
                p[0] += ((a[0] + c[0]) / 2.0 - p[0]) * 0.5;
                p[1] += ((a[1] + c[1]) / 2.0 - p[1]) * 0.5;
                p[2] = p[0];
                p[3] = p[1];
            }
        }
        let last = y.rope[y.rope.len() - 1];
        y.ball.x = last[0];
        y.ball.z = last[1];
        y
    }

    fn rot_m(&mut self, ax: f64, ay: f64, az: f64, ang: f64) {
        let (c, s) = (ang.cos(), ang.sin());
        let t = 1.0 - c;
        let r = [
            t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay,
            t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax,
            t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c,
        ];
        let m = self.m;
        let mut o = [0.0; 9];
        for i in 0..3 {
            for j in 0..3 {
                o[i * 3 + j] = r[i * 3] * m[j] + r[i * 3 + 1] * m[3 + j] + r[i * 3 + 2] * m[6 + j];
            }
        }
        self.m = o;
    }

    fn ortho(&mut self) {
        let m = self.m;
        let c0 = norm([m[0], m[3], m[6]]);
        let c1 = [m[1], m[4], m[7]];
        let d = c0[0] * c1[0] + c0[1] * c1[1] + c0[2] * c1[2];
        let c1 = norm([c1[0] - d * c0[0], c1[1] - d * c0[1], c1[2] - d * c0[2]]);
        let c2 = cross(c0, c1);
        self.m = [c0[0], c1[0], c2[0], c0[1], c1[1], c2[1], c0[2], c1[2], c2[2]];
    }

    fn bounds(&self) -> (f64, f64, bool) {
        let s = F / (self.ball.z + D);
        let r = R * s;
        let behind = self.ball.z > ZBEHIND;
        ((SXMIN + r - CX0) / s, ((if behind { SXBEHIND } else { SXMAX }) - r - CX0) / s, behind)
    }

    fn kick(&mut self, vx: f64, vz: f64, hop: Option<f64>) {
        self.ball.vx += vx;
        self.ball.vz += vz;
        let sp = self.ball.vx.hypot(self.ball.vz);
        if sp > 720.0 * SC {
            self.ball.vx *= 720.0 * SC / sp;
            self.ball.vz *= 720.0 * SC / sp;
        }
        self.ball.curl = (self.rnd() - 0.5) * 2.4;
        if self.ball.y < 2.0 {
            self.ball.vy = match hop {
                None => 90.0 + self.rnd() * 110.0,
                Some(h) => h,
            } * SC;
        }
    }

    fn auto_bat(&mut self) -> bool {
        let hide = self.rnd() < HIDE && self.ball.z < ZBEHIND;
        let tz = if hide { ZBEHIND + 70.0 + self.rnd() * (ZMAX - ZBEHIND - 70.0) } else { ZMIN + 10.0 + self.rnd() * (250f64).min(ZBEHIND - ZMIN - 20.0) };
        let st = F / (tz + D);
        let rt = R * st;
        let tx_min = (SXMIN + rt - CX0) / st;
        let tx_max = ((if hide { SXBEHIND - 70.0 } else { SXMAX - 10.0 }) - rt - CX0) / st;
        let tx = tx_min + self.rnd() * (tx_max - tx_min);
        let kx = (tx - self.ball.x) * (1.3 + self.rnd() * 0.7);
        let kz = (tz - self.ball.z) * (1.3 + self.rnd() * 0.7);
        self.kick(kx, kz, None);
        hide
    }

    fn step(&mut self, dt: f64) {
        self.t_now += dt * 1000.0;
        if self.t_now > self.next_bat {
            let hid = self.auto_bat();
            self.next_bat = self.t_now + if hid { 1700.0 } else { BAT[0] + self.rnd() * BAT[1] };
        }
        if self.ball.z > ZBEHIND && self.next_bat - self.t_now > 1800.0 {
            self.next_bat = self.t_now + 1800.0;
        }
        let sp0 = self.ball.vx.hypot(self.ball.vz);
        if sp0 > 20.0 {
            let ca = self.ball.curl * dt * (sp0 / 220.0).min(1.0);
            let (cc, ss, vx0) = (ca.cos(), ca.sin(), self.ball.vx);
            self.ball.vx = vx0 * cc - self.ball.vz * ss;
            self.ball.vz = vx0 * ss + self.ball.vz * cc;
        }
        let (px, pz, was_behind) = (self.ball.x, self.ball.z, self.ball.z > ZBEHIND);
        self.ball.x += self.ball.vx * dt;
        self.ball.z += self.ball.vz * dt;
        // Hop.
        if self.ball.y > 0.0 || self.ball.vy > 0.0 {
            self.ball.vy -= 1500.0 * SC * dt;
            self.ball.y += self.ball.vy * dt;
            if self.ball.y <= 0.0 {
                self.ball.y = 0.0;
                self.ball.vy = if self.ball.vy < -120.0 * SC { -self.ball.vy * 0.32 } else { 0.0 };
            }
        }
        let fr = (if self.ball.y > 0.0 { 0.8f64 } else { 0.3 }).powf(dt);
        self.ball.vx *= fr;
        self.ball.vz *= fr;
        if self.ball.z < ZMIN {
            self.ball.z = ZMIN;
            self.ball.vz = self.ball.vz.abs() * 0.5;
        }
        if self.ball.z > ZMAX {
            self.ball.z = ZMAX;
            self.ball.vz = -self.ball.vz.abs() * 0.5;
        }
        let bd = self.bounds();
        if self.ball.x < bd.0 {
            self.ball.x = bd.0;
            self.ball.vx = self.ball.vx.abs() * 0.5;
        }
        if self.ball.x > bd.1 {
            if was_behind && !bd.2 {
                self.ball.z = ZBEHIND + 0.5;
                self.ball.vz = self.ball.vz.abs() * 0.45;
            } else {
                self.ball.x = bd.1;
                self.ball.vx = -self.ball.vx.abs() * 0.5;
            }
        }
        // Rolling without slipping.
        let (dx, dz) = (self.ball.x - px, self.ball.z - pz);
        let dist = dx.hypot(dz);
        if dist > 1e-4 && self.ball.y < 1.0 {
            self.rot_m(dz / dist, 0.0, -dx / dist, dist / R);
        }
        self.frame += 1;
        if self.frame % 90 == 0 {
            self.ortho();
        }
        // Yarn: a verlet chain lying on the floor with friction; the ball tugs it and pays out new yarn.
        let mut moving = 0;
        for p in self.rope.iter_mut().skip(1) {
            let (vx, vz) = ((p[0] - p[2]) * DAMP, (p[1] - p[3]) * DAMP);
            p[2] = p[0];
            p[3] = p[1];
            if vx * vx + vz * vz < 0.0004 * SC {
                continue;
            }
            p[0] += vx;
            p[1] += vz;
            moving += 1;
        }
        let lp = self.rope[self.rope.len() - 1];
        let (dx, dz) = (self.ball.x - lp[0], self.ball.z - lp[1]);
        let dl = dx.hypot(dz);
        if self.ball.y < 3.0 * SC {
            if dl > SEG * 6.0 {
                let n = ((dl - SEG * 0.6) / SEG).floor() as usize;
                for i in 1..=n {
                    let nx = lp[0] + dx * i as f64 / (n as f64 + 0.6);
                    let nz = lp[1] + dz * i as f64 / (n as f64 + 0.6);
                    self.rope.push([nx, nz, nx, nz]);
                }
            }
            while self.rope.len() > 3 {
                let p2 = self.rope[self.rope.len() - 2];
                if (self.ball.x - p2[0]).hypot(self.ball.z - p2[1]) < SEG * 0.75 {
                    self.rope.pop();
                } else {
                    break;
                }
            }
        }
        let l = self.rope.len();
        let lp = self.rope[l - 1];
        let (dx, dz) = (self.ball.x - lp[0], self.ball.z - lp[1]);
        let dl = dx.hypot(dz);
        if dl > SEG {
            let k = TUG * (dl - SEG) / dl;
            self.rope[l - 1][0] += dx * k;
            self.rope[l - 1][1] += dz * k;
        }
        // Stretch-only constraints so a tug travels down the yarn; point 0 is pinned off screen.
        for _ in 0..6 {
            for i in (1..l).rev() {
                let (a0, b0) = (self.rope[i - 1], self.rope[i]);
                let (ex, ez) = (b0[0] - a0[0], b0[1] - a0[1]);
                let d = ex.hypot(ez);
                if d <= SEG || d < 1e-6 {
                    continue;
                }
                let c = (d - SEG) / d;
                if i - 1 == 0 {
                    self.rope[i][0] -= ex * c;
                    self.rope[i][1] -= ez * c;
                } else {
                    self.rope[i][0] -= ex * c * 0.5;
                    self.rope[i][1] -= ez * c * 0.5;
                    self.rope[i - 1][0] += ex * c * 0.5;
                    self.rope[i - 1][1] += ez * c * 0.5;
                }
            }
        }
        if self.rope.len() > MAXPTS {
            let cut = self.rope.len() - MAXPTS;
            self.rope.drain(0..cut);
        }
        self.rope_moving = moving;
    }

    /// Moves time on by `dt` seconds. True when the picture changed and must be drawn again.
    pub fn advance(&mut self, dt: f64) -> bool {
        self.step(dt.min(0.033));
        // The page was not being drawn for a while: nothing moved, but the clock did.
        if dt > 0.033 {
            self.t_now += (dt - 0.033) * 1000.0;
        }
        let mut sig = format!("{:.2},{:.2},{:.2},{}", self.ball.x, self.ball.z, self.ball.y, self.rope.len());
        if self.rope_moving > 0 {
            sig.push_str(&format!(",{}", self.t_now));
        }
        let changed = sig != self.last_sig;
        self.last_sig = sig;
        changed
    }

    /// Milliseconds until the ball is next batted.
    pub fn idle_for(&self) -> f64 {
        (self.next_bat - self.t_now).max(0.0)
    }

    pub fn hit(&self, mx: f64, my: f64) -> bool {
        self.ball_screen.is_some_and(|b| (mx - b[0]).hypot(my - b[1]) <= b[2] + 8.0)
    }

    /// A small push away from where the ball was clicked, with a little hop.
    pub fn click(&mut self, mx: f64, my: f64) -> bool {
        let Some(b) = self.ball_screen else { return false };
        if !self.hit(mx, my) {
            return false;
        }
        let (mut ux, mut uy) = (b[0] - mx, b[1] - my);
        let mut ul = ux.hypot(uy);
        if ul < b[2] * 0.25 {
            ux = self.rnd() - 0.5;
            uy = self.rnd() - 0.5;
            ul = ux.hypot(uy);
        }
        let ul = if ul == 0.0 { 1.0 } else { ul };
        self.kick(ux / ul * 640.0 * SC, -uy / ul * 460.0 * SC, Some(160.0));
        self.next_bat = self.t_now + 3500.0;
        true
    }

    // ---------- drawing ----------

    fn stroke_run(&self, pm: &mut Pixmap, tf: Transform, run: Run, seed0: u64) {
        if run.p.len() < 2 {
            return;
        }
        let Run { p: pts, w: ws, s: shadow } = chaikin(chaikin(run));
        let avg = ws.iter().sum::<f64>() / ws.len() as f64;
        // Soft floor shadow in two passes.
        if let Some(sp) = poly_path(&shadow) {
            pm.stroke_path(&sp, &solid(rgba(0, 0, 0, 0.12)), &round_stroke(avg * 3.0), tf, None);
            pm.stroke_path(&sp, &solid(rgba(0, 0, 0, 0.26)), &round_stroke(avg * 1.7), tf, None);
        }
        // Fuzzy halo so the edge is not hard.
        if let Some(body) = poly_path(&pts) {
            pm.stroke_path(&body, &solid(rgba(236, 110, 178, 0.28)), &round_stroke(avg * 1.45), tf, None);
        }
        let body_paint = solid(Color::from_rgba8(0xd2, 0x4d, 0x93, 255));
        let mut i = 0;
        while i < pts.len() - 1 {
            let e = (pts.len() - 1).min(i + 16);
            let mut w = 0.0;
            for wj in &ws[i + 1..=e] {
                w += wj;
            }
            if let Some(path) = poly_path(&pts[i..=e]) {
                pm.stroke_path(&path, &body_paint, &round_stroke(w / (e - i) as f64), tf, None);
            }
            i += 16;
        }
        let (mut tick, mut hi, mut fz) = (PathBuilder::new(), PathBuilder::new(), PathBuilder::new());
        let (mut acc, mut started) = (0.0, false);
        for i in 0..pts.len() - 1 {
            let (x0, y0, x1, y1) = (pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]);
            let sl = (x1 - x0).hypot(y1 - y0);
            if sl < 0.01 {
                continue;
            }
            let (tx, ty) = ((x1 - x0) / sl, (y1 - y0) / sl);
            let (mut nx, mut ny, ww) = (-ty, tx, ws[i]);
            if ny > 0.0 {
                nx = -nx;
                ny = -ny;
            }
            let (hx, hy) = (x0 + nx * ww * 0.2, y0 + ny * ww * 0.2);
            if !started {
                hi.move_to(hx as f32, hy as f32);
                started = true;
            } else {
                hi.line_to(hx as f32, hy as f32);
            }
            let step_l = (ww * 0.85).max(1.6);
            while acc < sl {
                let (cx, cy, h) = (x0 + tx * acc, y0 + ty * acc, ww * 0.42);
                tick.move_to((cx - nx * h - tx * h * 0.7) as f32, (cy - ny * h - ty * h * 0.7) as f32);
                tick.line_to((cx + nx * h + tx * h * 0.7) as f32, (cy + ny * h + ty * h * 0.7) as f32);
                acc += step_l;
            }
            acc -= sl;
            let hsh = ((i as u64 + seed0).wrapping_mul(2654435761)) & 0xffff_ffff;
            if hsh % 9 == 0 {
                let side = if hsh & 16 != 0 { 1.0 } else { -1.0 };
                let l2 = ww * (0.6 + (hsh % 7) as f64 * 0.12);
                let (bx, by) = (x0 + nx * ww * 0.45 * side, y0 + ny * ww * 0.45 * side);
                fz.move_to(bx as f32, by as f32);
                fz.quad_to(
                    (bx + nx * l2 * side + tx * l2 * 0.6) as f32,
                    (by + ny * l2 * side + ty * l2 * 0.6) as f32,
                    (bx + nx * l2 * 0.4 * side + tx * l2 * 1.3) as f32,
                    (by + ny * l2 * 0.4 * side + ty * l2 * 1.3) as f32,
                );
            }
        }
        if let Some(p) = tick.finish() {
            pm.stroke_path(&p, &solid(rgba(92, 16, 60, 0.38)), &round_stroke((avg * 0.26).max(0.7)), tf, None);
        }
        if let Some(p) = hi.finish() {
            pm.stroke_path(&p, &solid(rgba(255, 196, 230, 0.32)), &round_stroke((avg * 0.22).max(0.6)), tf, None);
        }
        if let Some(p) = fz.finish() {
            pm.stroke_path(&p, &solid(rgba(246, 160, 208, 0.45)), &round_stroke((avg * 0.1).max(0.5)), tf, None);
        }
    }

    /// The floor part up to the edge of the ball, then a lifted tail to the anchor.
    fn rope_geometry(&self) -> Vec<V3> {
        let ball = &self.ball;
        let mut j = self.rope.len() - 1;
        while j > 0 && (self.rope[j][0] - ball.x).hypot(self.rope[j][1] - ball.z) < R * 1.45 {
            j -= 1;
        }
        let mut pts3: Vec<V3> = self.rope[..=j].iter().map(|p| [p[0], 0.0, p[1]]).collect();
        let e = self.rope[j];
        let (dxr, dzr) = (e[0] - ball.x, e[1] - ball.z);
        let dl = dxr.hypot(dzr);
        let dl = if dl == 0.0 { 1.0 } else { dl };
        let (ax, az, ay) = (ball.x + dxr / dl * R * 0.93, ball.z + dzr / dl * R * 0.93, ball.y + R * 0.3);
        for k in 1..=8 {
            let t = k as f64 / 8.0;
            pts3.push([e[0] + (ax - e[0]) * t, ay * t * t, e[1] + (az - e[1]) * t]);
        }
        pts3
    }

    fn radial(cx: f64, cy: f64, fx: f64, fy: f64, radius: f64, stops: &[(f64, Color)]) -> Option<Shader<'static>> {
        RadialGradient::new(
            Point::from_xy(fx as f32, fy as f32),
            Point::from_xy(cx as f32, cy as f32),
            radius as f32,
            stops.iter().map(|(t, c)| GradientStop::new(*t as f32, *c)).collect(),
            SpreadMode::Pad,
            Transform::identity(),
        )
    }

    fn fill_circle(pm: &mut Pixmap, tf: Transform, cx: f64, cy: f64, r: f64, shader: Option<Shader<'static>>) {
        let (Some(path), Some(shader)) = (PathBuilder::from_circle(cx as f32, cy as f32, r as f32), shader) else { return };
        let paint = Paint { shader, anti_alias: true, ..Paint::default() };
        pm.fill_path(&path, &paint, FillRule::Winding, tf, None);
    }

    fn draw_ball(&mut self, pm: &mut Pixmap, tf: Transform) {
        let (bx, by, bz) = (self.ball.x, self.ball.y, self.ball.z);
        let cy = by + R;
        let p = proj(bx, cy, bz);
        let (sx, sy, s) = (p[0], p[1], p[2]);
        let r = R * s;
        let cam = norm([-bx, CH - cy, -D - bz]);
        let m = self.m;
        // Body.
        let body = Self::radial(sx, sy, sx - r * 0.38, sy - r * 0.42, r * 1.05, &[
            (0.0, Color::from_rgba8(0xd9, 0x55, 0x9a, 255)),
            (0.55, Color::from_rgba8(0xa3, 0x35, 0x72, 255)),
            (1.0, Color::from_rgba8(0x3a, 0x0c, 0x2a, 255)),
        ]);
        Self::fill_circle(pm, tf, sx, sy, r * 0.99, body);
        let sw = (0.03 * R * s).max(1.2);
        let step = if self.lite { 2 } else { 1 };
        let under_paint = solid(rgba(40, 6, 28, 0.6));
        for list in &self.bands {
            let mut under = PathBuilder::new();
            let mut buckets: Vec<Option<PathBuilder>> = (0..self.pal.len()).map(|_| None).collect();
            for pts in list.iter().step_by(step) {
                let (mut prev_v, mut pxs, mut pys) = (false, 0.0, 0.0);
                for i in 0..=SAMPLES {
                    let ii = (i % SAMPLES) * 3;
                    let (x, y, z) = (pts[ii] as f64, pts[ii + 1] as f64, pts[ii + 2] as f64);
                    let nx = m[0] * x + m[1] * y + m[2] * z;
                    let ny = m[3] * x + m[4] * y + m[5] * z;
                    let nz = m[6] * x + m[7] * y + m[8] * z;
                    let facing = nx * cam[0] + ny * cam[1] + nz * cam[2];
                    let vis = facing > 0.015;
                    let pp = proj(bx + nx * R, cy + ny * R, bz + nz * R);
                    if vis && prev_v {
                        let dif = (nx * self.light[0] + ny * self.light[1] + nz * self.light[2]).max(0.0);
                        let val = (0.3 + 0.7 * dif) * (0.45 + 0.55 * (facing * 1.3).min(1.0));
                        let bi = (self.pal.len() - 1).min((val * self.pal.len() as f64).floor() as usize);
                        let bucket = buckets[bi].get_or_insert_with(PathBuilder::new);
                        bucket.move_to(pxs as f32, pys as f32);
                        bucket.line_to(pp[0] as f32, pp[1] as f32);
                        under.move_to(pxs as f32, pys as f32);
                        under.line_to(pp[0] as f32, pp[1] as f32);
                    }
                    prev_v = vis;
                    pxs = pp[0];
                    pys = pp[1];
                }
            }
            if let Some(path) = under.finish() {
                pm.stroke_path(&path, &under_paint, &Stroke { width: (sw * 1.75) as f32, line_cap: LineCap::Round, ..Stroke::default() }, tf, None);
            }
            for (q, bucket) in buckets.into_iter().enumerate() {
                if let Some(path) = bucket.and_then(PathBuilder::finish) {
                    pm.stroke_path(&path, &solid(self.pal[q]), &Stroke { width: sw as f32, line_cap: LineCap::Round, ..Stroke::default() }, tf, None);
                }
            }
        }
        // Stray fibres on the silhouette.
        let mut fp = PathBuilder::new();
        for (n0, t0, len) in &self.fibres {
            let rot = |v: &V3| [m[0] * v[0] + m[1] * v[1] + m[2] * v[2], m[3] * v[0] + m[4] * v[1] + m[5] * v[2], m[6] * v[0] + m[7] * v[1] + m[8] * v[2]];
            let n = rot(n0);
            let fc = n[0] * cam[0] + n[1] * cam[1] + n[2] * cam[2];
            if !(-0.02..=0.3).contains(&fc) {
                continue;
            }
            let t = rot(t0);
            let a = proj(bx + n[0] * R, cy + n[1] * R, bz + n[2] * R);
            let k = 1.0 + len * 0.7;
            let e = proj(bx + (n[0] * k + t[0] * len) * R, cy + (n[1] * k + t[1] * len) * R, bz + (n[2] * k + t[2] * len) * R);
            let (mx, my) = ((a[0] + e[0]) / 2.0 + (e[1] - a[1]) * 0.25, (a[1] + e[1]) / 2.0 - (e[0] - a[0]) * 0.25);
            fp.move_to(a[0] as f32, a[1] as f32);
            fp.quad_to(mx as f32, my as f32, e[0] as f32, e[1] as f32);
        }
        if let Some(path) = fp.finish() {
            pm.stroke_path(&path, &solid(rgba(246, 150, 205, 0.55)), &Stroke { width: (0.009 * R * s).max(0.6) as f32, line_cap: LineCap::Round, ..Stroke::default() }, tf, None);
        }
        // Soft sheen and occlusion, fixed to the light.
        let (hx, hy) = (sx - r * 0.42, sy - r * 0.48);
        Self::fill_circle(pm, tf, sx, sy, r, Self::radial(hx, hy, hx, hy, r * 0.75, &[(0.0, rgba(255, 236, 247, 0.22)), (1.0, rgba(255, 236, 247, 0.0))]));
        // The canvas gradient starts at a circle of 0.55 r; here the clear centre is a stop.
        Self::fill_circle(pm, tf, sx, sy, r, Self::radial(sx, sy, sx - r * 0.2, sy - r * 0.25, r * 1.02, &[(0.0, rgba(20, 4, 14, 0.0)), (0.54, rgba(20, 4, 14, 0.0)), (1.0, rgba(20, 4, 14, 0.55))]));
        // Floor bounce light on the underside.
        Self::fill_circle(pm, tf, sx, sy, r, Self::radial(sx, sy + r, sx, sy + r, r * 0.8, &[(0.0, rgba(255, 122, 200, 0.16)), (1.0, rgba(255, 122, 200, 0.0))]));
        self.ball_screen = Some([sx, sy, r]);
    }

    fn floor_ellipse(pm: &mut Pixmap, tf: Transform, x: f64, z: f64, rx: f64, alpha: f64, squash: f64) {
        let p = proj(x, 0.0, z);
        let local = tf.pre_translate(p[0] as f32, p[1] as f32).pre_scale(1.0, squash as f32);
        let radius = rx * p[2];
        Self::fill_circle(pm, local, 0.0, 0.0, radius, Self::radial(0.0, 0.0, 0.0, 0.0, radius, &[(0.0, rgba(0, 0, 0, alpha)), (1.0, rgba(0, 0, 0, 0.0))]));
    }

    /// Draws the scene (1440 by 780 canvas units) into `pm`, scaled to fill it.
    pub fn draw(&mut self, pm: &mut Pixmap) {
        let started = std::time::Instant::now();
        pm.fill(Color::TRANSPARENT);
        let tf = Transform::from_scale(pm.width() as f32 / W as f32, pm.height() as f32 / H as f32);
        // A soft pool of light on the floor.
        let pool = tf.pre_translate(980.0, 700.0).pre_scale(1.0, 0.2);
        Self::fill_circle(pm, pool, 0.0, 0.0, 640.0, Self::radial(0.0, 0.0, 0.0, 0.0, 640.0, &[(0.0, rgba(255, 214, 236, 0.07)), (1.0, rgba(255, 214, 236, 0.0))]));
        let g3 = self.rope_geometry();
        let split = self.ball.z - R * 0.15;
        let (mut back, mut front): (Vec<Run>, Vec<Run>) = (Vec::new(), Vec::new());
        let mut cur: Option<(bool, usize)> = None;
        for p in &g3 {
            let is_front = p[2] < split;
            if cur.map(|(f, _)| f) != Some(is_front) {
                let mut run = Run { p: Vec::new(), w: Vec::new(), s: Vec::new() };
                if let Some((f, i)) = cur {
                    let prev = if f { &front[i] } else { &back[i] };
                    if let (Some(a), Some(b), Some(c)) = (prev.p.last(), prev.w.last(), prev.s.last()) {
                        run.p.push(*a);
                        run.w.push(*b);
                        run.s.push(*c);
                    }
                }
                let list = if is_front { &mut front } else { &mut back };
                list.push(run);
                cur = Some((is_front, list.len() - 1));
            }
            let a = proj(p[0], p[1] + 4.1 * SC, p[2]);
            let sh = proj(p[0] + 2.0 * SC, 0.0, p[2] - 2.0 * SC);
            let (f, i) = cur.expect("set above");
            let run = if f { &mut front[i] } else { &mut back[i] };
            run.p.push([a[0], a[1]]);
            run.w.push(8.2 * SC * a[2]);
            run.s.push([sh[0] + 1.5, sh[1] + 1.5]);
        }
        for (i, run) in back.into_iter().enumerate() {
            self.stroke_run(pm, tf, run, i as u64 * 97);
        }
        let hop_k = (1.0 - self.ball.y / (160.0 * SC)).max(0.35);
        Self::floor_ellipse(pm, tf, self.ball.x + 10.0 * SC, self.ball.z + 14.0 * SC, R * 1.25, 0.72 * hop_k, 0.24);
        Self::floor_ellipse(pm, tf, self.ball.x + 4.0 * SC, self.ball.z + 4.0 * SC, R * 0.6, 0.65 * hop_k, 0.22);
        self.draw_ball(pm, tf);
        for (i, run) in front.into_iter().enumerate() {
            self.stroke_run(pm, tf, run, 500 + i as u64 * 97);
        }
        // As the page does: drop every second strand when drawing is slow.
        let ms = started.elapsed().as_secs_f64() * 1000.0;
        self.slow = self.slow * 0.9 + if ms > 14.0 { 0.1 } else { 0.0 };
        if !self.lite && self.slow > 0.6 {
            self.lite = true;
        }
    }
}
