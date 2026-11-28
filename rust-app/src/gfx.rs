//! Drawing helpers: text, the web app's line icons, and a scrolling pane with the same thin
//! scrollbar the browser draws.

use crate::theme::Theme;
use eframe::egui::{self, text::LayoutJob, Align, Color32, ColorImage, CornerRadius, FontId, Galley, Id, Pos2, Rect, Sense, Stroke, StrokeKind, TextFormat, TextureHandle, TextureOptions, Ui, Vec2};
use std::collections::HashMap;
use std::sync::Arc;
use tiny_skia::{LineCap, LineJoin, Paint, PathBuilder, Pixmap, Transform};

pub fn px(ui: &Ui, v: f32) -> f32 {
    let ppp = ui.ctx().pixels_per_point();
    (v * ppp).round() / ppp
}

pub fn radius(r: f32) -> CornerRadius {
    CornerRadius::same(r.round().clamp(0.0, 255.0) as u8)
}

/// A filled box with a CSS border: the border lies inside the box.
pub fn panel(ui: &Ui, rect: Rect, r: f32, fill: Color32, border: f32, border_color: Color32) {
    ui.painter().rect(rect, radius(r), fill, Stroke::new(border, border_color), StrokeKind::Inside);
}

pub fn format(font: FontId, color: Color32) -> TextFormat {
    TextFormat { font_id: font, color, ..TextFormat::default() }
}

/// Laid-out text with the line box CSS gives it.
///
/// A browser rounds a font's ascent and descent to whole pixels and puts the baseline
/// `ascent + floor((line-height - ascent - descent) / 2)` below the top of the line; the text
/// engine here centres unrounded metrics. Placing by the CSS baseline keeps every label on the
/// pixel row the web app has it on.
#[derive(Clone)]
pub struct Label {
    pub galley: Arc<Galley>,
    /// Height of the line box (all rows).
    h: f32,
    /// Baseline of the first row, from the top of the line box, as CSS has it.
    base: f32,
    /// Baseline of the first row, from the top of the galley, as laid out.
    own: f32,
}

/// A font's ascent and descent as a browser uses them: each rounded to a whole pixel.
pub fn css_metrics(ui: &Ui, font: &FontId) -> (f32, f32) {
    let (ascent, height) = raw_metrics(ui, font);
    (ascent.round(), (height - ascent).round())
}

/// Ascent and height of a font as the text engine has them.
pub fn raw_metrics(ui: &Ui, font: &FontId) -> (f32, f32) {
    let probe = ui.painter().layout_job(LayoutJob::single_section("x".to_owned(), format(font.clone(), Color32::WHITE)));
    probe.rows.first().and_then(|r| r.row.glyphs.first()).map(|g| (g.font_face_ascent, g.font_face_height)).unwrap_or((font.size * 0.95, font.size * 1.2))
}

/// The CSS baseline in a line of `line_height`, and the height of the line; `None` is
/// `line-height: normal`.
pub fn css_baseline(ui: &Ui, font: &FontId, line_height: Option<f32>) -> (f32, f32) {
    let (ascent, descent) = css_metrics(ui, font);
    match line_height {
        Some(lh) => (ascent + ((lh - (ascent + descent)) / 2.0).floor(), lh),
        None => (ascent, ascent + descent),
    }
}

impl Label {
    /// A label from text laid out elsewhere, in `font` with a CSS `line_height`.
    pub fn new(ui: &Ui, galley: Arc<Galley>, font: &FontId, line_height: Option<f32>) -> Label {
        let (base, line) = css_baseline(ui, font, line_height);
        let own = galley.rows.first().and_then(|r| r.row.glyphs.first().map(|g| r.pos.y + g.pos.y)).unwrap_or(base);
        let rows = galley.rows.len().max(1) as f32;
        Label { galley, h: line * rows, base, own }
    }

    /// Width of the text and height of its line box.
    pub fn size(&self) -> Vec2 {
        Vec2::new(self.galley.size().x, self.h)
    }

    /// Paints with the top left of the line box at (`x`, `top`). Returns the line box.
    pub fn paint(&self, ui: &Ui, x: f32, top: f32) -> Rect {
        let x = px(ui, x);
        let baseline = px(ui, top + self.base);
        ui.painter().galley(Pos2::new(x, baseline - self.own), self.galley.clone(), Color32::PLACEHOLDER);
        Rect::from_min_size(Pos2::new(x, px(ui, top)), self.size())
    }

    /// The y of the first row's baseline when the line box starts at `top`.
    pub fn baseline(&self, ui: &Ui, top: f32) -> f32 {
        px(ui, top + self.base)
    }
}

/// Which system font takes the characters the web fonts do not cover.
#[derive(Clone, Copy, PartialEq)]
pub enum Fallback {
    /// `font-family: Figtree, sans-serif` (the sidebar and other chrome): Arial.
    Ui,
    /// `var(--font-sans)` (the reader, search): Segoe UI.
    Sans,
}

/// The characters Google Fonts serves Figtree and JetBrains Mono for (the `unicode-range` of the
/// latin and latin-ext subsets). A browser draws anything else, such as `←`, in a system font
/// even though the font file has a glyph for it; so does this.
pub fn in_web_font(c: char) -> bool {
    matches!(c as u32,
        0x0000..=0x02FF | 0x0304 | 0x0308 | 0x0329 | 0x1D00..=0x1DBF | 0x1E00..=0x1E9F | 0x1EF2..=0x1EFF
        | 0x2000..=0x206F | 0x20A0..=0x20C0 | 0x2113 | 0x2122 | 0x2191 | 0x2193 | 0x2212 | 0x2215
        | 0x2C60..=0x2C7F | 0xA720..=0xA7FF | 0xFEFF | 0xFFFD)
}

/// Appends `text` to a layout job, sending characters outside the web fonts to the system font.
pub fn append(job: &mut LayoutJob, text: &str, leading_space: f32, format: TextFormat, fallback: Fallback) {
    let bundled = matches!(&format.font_id.family, egui::FontFamily::Name(n) if !n.starts_with("system") && !n.starts_with("fallback"));
    if !bundled || text.chars().all(in_web_font) {
        job.append(text, leading_space, format);
        return;
    }
    let other = egui::FontFamily::Name(if fallback == Fallback::Ui { "fallback-ui" } else { "fallback-sans" }.into());
    let mut lead = leading_space;
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let end = chars.peek().map_or(text.len(), |(j, _)| *j);
        let run_ends = chars.peek().is_none_or(|(_, next)| in_web_font(*next) != in_web_font(c));
        if run_ends {
            let mut f = format.clone();
            if !in_web_font(c) {
                f.font_id.family = other.clone();
            }
            job.append(&text[start..end], lead, f);
            lead = 0.0;
            start = end;
        }
        let _ = i;
    }
}

fn job(text: &str, format: TextFormat, max_width: f32) -> LayoutJob {
    let mut job = LayoutJob::default();
    append(&mut job, text, 0.0, format, Fallback::Ui);
    job.wrap.max_width = max_width;
    // Wrap and cut at exactly the width given, as a browser does.
    job.round_output_to_gui = false;
    job
}

/// One line of text, never wrapped.
pub fn line(ui: &Ui, text: &str, font: FontId, color: Color32) -> Label {
    spaced(ui, text, font, color, 0.0)
}

/// One line with CSS `letter-spacing`.
pub fn spaced(ui: &Ui, text: &str, font: FontId, color: Color32, letter_spacing: f32) -> Label {
    let job = job(text, TextFormat { extra_letter_spacing: letter_spacing, ..format(font.clone(), color) }, f32::INFINITY);
    Label::new(ui, ui.painter().layout_job(job), &font, None)
}

/// One line cut short with an ellipsis when it is wider than `max_width`: as many whole
/// characters as fit with the ellipsis after them, which is how a browser cuts
/// `text-overflow: ellipsis`.
pub fn elided(ui: &Ui, text: &str, font: FontId, color: Color32, max_width: f32) -> Label {
    let full = line(ui, text, font.clone(), color);
    if full.galley.size().x <= max_width {
        return full;
    }
    let dots = line(ui, "…", font.clone(), color).galley.size().x;
    let ends: Vec<usize> = text.char_indices().map(|(i, c)| i + c.len_utf8()).collect();
    let fits = |n: usize| n == 0 || line(ui, &text[..ends[n - 1]], font.clone(), color).galley.size().x + dots <= max_width;
    let (mut lo, mut hi) = (0, ends.len());
    while lo < hi {
        let mid = (lo + hi + 1) / 2;
        if fits(mid) {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    let kept = if lo == 0 { "" } else { &text[..ends[lo - 1]] };
    line(ui, &format!("{kept}…"), font, color)
}

/// Wrapped text with a CSS line height.
pub fn wrapped(ui: &Ui, text: &str, font: FontId, color: Color32, width: f32, line_height: f32) -> Label {
    let job = job(text, TextFormat { line_height: Some(line_height), ..format(font.clone(), color) }, width.max(0.0));
    Label::new(ui, ui.painter().layout_job(job), &font, Some(line_height))
}

/// Paints a label with its left edge at `x` and its line box centred on `y`.
pub fn paint_left(ui: &Ui, x: f32, y: f32, label: &Label) -> Rect {
    label.paint(ui, x, y - label.h / 2.0)
}

pub fn paint_at(ui: &Ui, pos: Pos2, label: &Label) -> Rect {
    label.paint(ui, pos.x, pos.y)
}

pub fn paint_centered(ui: &Ui, center: Pos2, label: &Label) -> Rect {
    label.paint(ui, center.x - label.galley.size().x / 2.0, center.y - label.h / 2.0)
}

/// A CSS dotted underline two pixels thick: square dots, one dot on, one dot off.
pub fn dotted(ui: &Ui, x0: f32, x1: f32, y: f32, color: Color32) {
    let (x0, y) = (px(ui, x0), px(ui, y));
    let mut x = x0;
    while x + 1.0 < x1 {
        ui.painter().rect_filled(Rect::from_min_size(Pos2::new(x, y), Vec2::splat(2.0)), 0.0, color);
        x += 4.0;
    }
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub enum Icon {
    /// The sidebar's Search button.
    SearchBold,
    /// The search boxes.
    Search,
    User,
    Home,
    Link,
    Hash,
}

impl Icon {
    /// SVG path data on a 24 by 24 grid, and the stroke width, as the web app's inline SVGs.
    fn art(self) -> (&'static [&'static str], f32) {
        match self {
            Icon::SearchBold => (&["M3 11a8 8 0 1 0 16 0a8 8 0 1 0 -16 0", "M21 21L16.65 16.65"], 2.25),
            Icon::Search => (&["M4 11a7 7 0 1 0 14 0a7 7 0 1 0 -14 0", "M20 20l-3.5-3.5"], 2.0),
            Icon::User => (&["M8 8a4 4 0 1 0 8 0a4 4 0 1 0 -8 0", "M4 21a8 8 0 0 1 16 0"], 2.0),
            Icon::Home => (&["M3 10.5 12 3l9 7.5", "M5 9.5V21h14V9.5"], 2.0),
            Icon::Link => (
                &["M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71", "M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"],
                2.0,
            ),
            Icon::Hash => (&["M4 9H20", "M4 15H20", "M10 3L8 21", "M16 3L14 21"], 2.0),
        }
    }
}

#[derive(Default)]
pub struct Icons {
    cache: HashMap<(Icon, u32, [u8; 4]), TextureHandle>,
}

impl Icons {
    /// Draws `icon` to fill `rect` (a square), stroked in `color` with round caps and joins.
    pub fn paint(&mut self, ui: &Ui, icon: Icon, rect: Rect, color: Color32) {
        let ppp = ui.ctx().pixels_per_point();
        let side = (rect.width() * ppp).round().max(1.0) as u32;
        let key = (icon, side, color.to_array());
        let texture = self.cache.entry(key).or_insert_with(|| {
            let mut pm = Pixmap::new(side, side).expect("icon size");
            let (paths, width) = icon.art();
            let mut paint = Paint::default();
            paint.set_color_rgba8(color.r(), color.g(), color.b(), color.a());
            paint.anti_alias = true;
            let stroke = tiny_skia::Stroke { width, line_cap: LineCap::Round, line_join: LineJoin::Round, ..Default::default() };
            let tf = Transform::from_scale(side as f32 / 24.0, side as f32 / 24.0);
            for d in paths {
                let Ok(bez) = kurbo::BezPath::from_svg(d) else { continue };
                let mut pb = PathBuilder::new();
                for el in bez.elements() {
                    match *el {
                        kurbo::PathEl::MoveTo(p) => pb.move_to(p.x as f32, p.y as f32),
                        kurbo::PathEl::LineTo(p) => pb.line_to(p.x as f32, p.y as f32),
                        kurbo::PathEl::QuadTo(a, p) => pb.quad_to(a.x as f32, a.y as f32, p.x as f32, p.y as f32),
                        kurbo::PathEl::CurveTo(a, b, p) => pb.cubic_to(a.x as f32, a.y as f32, b.x as f32, b.y as f32, p.x as f32, p.y as f32),
                        kurbo::PathEl::ClosePath => pb.close(),
                    }
                }
                if let Some(path) = pb.finish() {
                    pm.stroke_path(&path, &paint, &stroke, tf, None);
                }
            }
            let image = ColorImage::from_rgba_premultiplied([side as usize, side as usize], pm.data());
            ui.ctx().load_texture("icon", image, TextureOptions::LINEAR)
        });
        let min = Pos2::new(px(ui, rect.min.x), px(ui, rect.min.y));
        let rect = Rect::from_min_size(min, Vec2::splat(side as f32 / ppp));
        ui.painter().image(texture.id(), rect, Rect::from_min_max(Pos2::ZERO, Pos2::new(1.0, 1.0)), Color32::WHITE);
    }
}

// ---------------------------------------------------------------------------
// Scrolling
// ---------------------------------------------------------------------------

/// The browser's thin scrollbar: ten pixels wide, a six-pixel thumb, an arrow at each end.
pub const BAR: f32 = 10.0;

#[derive(Default)]
pub struct Scroll {
    pub offset: f32,
    grab: Option<f32>,
}

impl Scroll {
    /// Handles the wheel and the scrollbar for a pane showing `content` points of content in
    /// `rect`, paints the bar, and returns the offset to draw the content at.
    pub fn run(&mut self, ui: &Ui, id: Id, rect: Rect, content: f32, th: &Theme, enabled: bool) -> f32 {
        let max = (content - rect.height()).max(0.0);
        if enabled && ui.rect_contains_pointer(rect) {
            let delta = ui.input(|i| i.smooth_scroll_delta);
            self.offset -= delta.y;
        }
        if max <= 0.0 {
            self.offset = 0.0;
            self.grab = None;
            return 0.0;
        }
        let bar = Rect::from_min_max(Pos2::new(rect.right() - BAR, rect.top()), rect.max);
        let track = Rect::from_min_max(Pos2::new(bar.left() + 2.0, bar.top() + 12.0), Pos2::new(bar.right() - 2.0, bar.bottom() - 12.0));
        let length = track.height().max(1.0);
        let thumb_len = (length * rect.height() / content).clamp(18f32.min(length), length);
        let travel = (length - thumb_len).max(1.0);
        let thumb_top = |offset: f32| track.top() + travel * (offset / max).clamp(0.0, 1.0);
        let response = ui.interact(bar, id.with("scrollbar"), Sense::click_and_drag());
        if enabled {
            if let Some(pos) = response.interact_pointer_pos() {
                if response.drag_started() || response.is_pointer_button_down_on() && self.grab.is_none() {
                    let top = thumb_top(self.offset);
                    if pos.y < bar.top() + 10.0 {
                        self.offset -= 40.0;
                    } else if pos.y > bar.bottom() - 10.0 {
                        self.offset += 40.0;
                    } else if pos.y >= top && pos.y <= top + thumb_len {
                        self.grab = Some(pos.y - top);
                    } else {
                        // A click on the track puts the thumb under the pointer.
                        self.grab = Some(thumb_len / 2.0);
                    }
                }
                if let Some(grab) = self.grab {
                    self.offset = (pos.y - grab - track.top()) / travel * max;
                }
            }
        }
        if !response.is_pointer_button_down_on() {
            self.grab = None;
        }
        self.offset = self.offset.clamp(0.0, max);
        let top = thumb_top(self.offset);
        let color = if response.hovered() || self.grab.is_some() { th.text_muted } else { th.border };
        let painter = ui.painter();
        painter.rect_filled(Rect::from_min_max(Pos2::new(track.left(), top), Pos2::new(track.right(), top + thumb_len)), radius(3.0), color);
        let cx = bar.center().x;
        let arrow = |tip_y: f32, dir: f32| {
            let points = vec![Pos2::new(cx, tip_y), Pos2::new(cx - 3.0, tip_y + 4.0 * dir), Pos2::new(cx + 3.0, tip_y + 4.0 * dir)];
            painter.add(egui::Shape::convex_polygon(points, th.border, Stroke::NONE));
        };
        arrow(bar.top() + 4.0, 1.0);
        arrow(bar.bottom() - 4.0, -1.0);
        self.offset
    }

    /// The width left for content: the pane's, less the bar when there is something to scroll.
    pub fn inner_width(rect: Rect, content: f32) -> f32 {
        if content > rect.height() {
            rect.width() - BAR
        } else {
            rect.width()
        }
    }
}

/// A click target with a pointing-hand cursor.
pub fn button(ui: &Ui, id: Id, rect: Rect, enabled: bool) -> egui::Response {
    let response = ui.interact(rect, id, if enabled { Sense::click() } else { Sense::hover() });
    if enabled && response.hovered() {
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
    }
    response
}

#[allow(dead_code)]
pub fn align_top() -> Align {
    Align::TOP
}
