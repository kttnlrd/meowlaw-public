//! The section reader: markdown blocks laid out and drawn with the web app's reader styles
//! (`.lk-reader-*`, `.lk-note`, `.lk-para-marker`, `.lk-link` in frontend/src/lawkitty-ui.css).

use crate::gfx;
use crate::md::{Block, Doc, Inline, Style};
use crate::theme::{self, Theme};
use eframe::egui::{self, text::CCursor, text::LayoutJob, Align, Color32, Galley, Pos2, Rect, Stroke, TextFormat, Ui, Vec2};
use std::ops::Range;
use std::sync::Arc;

/// `max-width` of the reading column.
pub const COLUMN: f32 = 740.0;

/// The type of a run of body text: size, line height, colour and weight.
#[derive(Clone, Copy)]
struct Type {
    size: f32,
    line: f32,
    color: Color32,
    weight: u16,
    italic: bool,
    letter_spacing: f32,
}

pub struct Text {
    /// Top left, from the top left of the document.
    pub pos: Pos2,
    pub galley: Arc<Galley>,
    /// Link areas, relative to the galley, with the index of their target.
    pub links: Vec<(Rect, usize)>,
    /// Dotted underlines: left, right and top, relative to the galley.
    pub underlines: Vec<(f32, f32, f32)>,
}

pub enum Item {
    Text(Text),
    Box { rect: Rect, radius: f32, fill: Color32, border: f32, border_color: Color32 },
    Bullet { pos: Pos2, color: Color32 },
}

pub struct Layout {
    pub width: f32,
    pub height: f32,
    pub items: Vec<Item>,
    pub hrefs: Vec<String>,
    /// Where each top-level block starts, to jump to anchors.
    pub tops: Vec<f32>,
}

struct Built {
    job: LayoutJob,
    /// Character ranges of links and their targets.
    links: Vec<(Range<usize>, usize)>,
    /// How far to move the laid-out text so its baseline is where CSS puts it.
    shift: f32,
}

/// Turns inline spans into one layout job. A run in another typeface (a `(1)` marker, the
/// section number) sits on the text's baseline, as an inline box does in CSS, and a line that
/// holds one grows by the difference between the two fonts' baselines.
fn build(ui: &Ui, spans: &Inline, t: Type, th: &Theme, width: f32, hrefs: &mut Vec<String>) -> Built {
    let base_font = theme::sans(t.size, t.weight);
    let (base_ascent, base_height) = gfx::raw_metrics(ui, &base_font);
    // Where the text engine puts the baseline in a plain line, and where CSS does.
    let laid = gfx::px(ui, 0.5 * (t.line - base_height) + base_ascent);
    let (css, _) = gfx::css_baseline(ui, &base_font, Some(t.line));
    let odd_font = |style: Style| match style {
        Style::Marker => Some(theme::mono(14.0, 600)),
        Style::Number => Some(theme::mono(t.size, 600)),
        Style::Code => Some(theme::mono((t.size * 0.9).round(), 400)),
        _ => None,
    };
    // The first odd run decides how much taller its line is, and which way the text leans in it.
    let lean = spans.iter().find_map(|s| odd_font(s.style)).map(|f| css - gfx::css_baseline(ui, &f, Some(t.line)).0).unwrap_or(0.0);
    let base_valign = if lean >= 0.0 { Align::TOP } else { Align::BOTTOM };

    let mut job = LayoutJob::default();
    job.wrap.max_width = width.max(0.0);
    job.round_output_to_gui = false;
    let mut links: Vec<(Range<usize>, usize)> = Vec::new();
    let mut chars = 0usize;
    let mut margin = 0.0;
    for s in spans {
        let weight = if s.bold { 700 } else { t.weight };
        let mut f = TextFormat {
            font_id: theme::sans(t.size, weight),
            color: t.color,
            line_height: Some(t.line),
            italics: s.italic || t.italic,
            extra_letter_spacing: t.letter_spacing,
            valign: base_valign,
            ..TextFormat::default()
        };
        let mut after = 0.0;
        let odd = odd_font(s.style);
        match s.style {
            Style::Marker => {
                f.color = th.text;
                f.italics = false;
                after = 6.0;
            }
            Style::Number => after = 0.35 * t.size,
            Style::NoteLabel => {
                f.font_id = theme::sans(t.size, 700);
                f.color = th.text_muted;
            }
            Style::Code | Style::Plain => {}
        }
        if let Some(font) = &odd {
            // A line height that centres this font's baseline on the text's baseline.
            let (ascent, height) = gfx::raw_metrics(ui, font);
            let target = laid + (-lean).max(0.0);
            f.font_id = font.clone();
            f.line_height = Some((2.0 * (target - ascent) + height).clamp(1.0, t.line + lean.abs()));
            f.valign = Align::TOP;
        }
        if let Some(href) = &s.link {
            f.color = th.accent;
            let index = hrefs.iter().position(|h| h == href).unwrap_or_else(|| {
                hrefs.push(href.clone());
                hrefs.len() - 1
            });
            let n = s.text.chars().count();
            match links.last_mut() {
                Some((range, i)) if *i == index && range.end == chars => range.end += n,
                _ => links.push((chars..chars + n, index)),
            }
        }
        gfx::append(&mut job, &s.text, margin, f, gfx::Fallback::Sans);
        chars += s.text.chars().count();
        margin = after;
        if odd.is_some() && lean != 0.0 {
            // A strut: a hair of space whose line box is the taller line.
            let strut = TextFormat { font_id: theme::sans(1.0, 400), line_height: Some(t.line + lean.abs()), color: Color32::TRANSPARENT, ..TextFormat::default() };
            job.append("\u{a0}", 0.0, strut);
            chars += 1;
        }
    }
    Built { job, links, shift: css - laid }
}

/// Lays a built job out and finds where its links and their dotted underlines fall.
fn place(ui: &Ui, built: Built, pos: Pos2) -> Text {
    let pos = Pos2::new(pos.x, pos.y + built.shift);
    let galley = ui.painter().layout_job(built.job);
    let mut links = Vec::new();
    let mut underlines = Vec::new();
    for (range, index) in built.links {
        let start = galley.pos_from_cursor(CCursor { index: range.start.into(), prefer_next_row: true });
        let end = galley.pos_from_cursor(CCursor { index: range.end.into(), prefer_next_row: false });
        for placed in &galley.rows {
            let row = placed.rect();
            if row.bottom() <= start.top() + 0.5 || row.top() >= end.bottom() - 0.5 {
                continue;
            }
            let text = placed.rect_without_leading_space();
            let x0 = if (row.top() - start.top()).abs() < 1.0 || row.contains(start.center()) { start.left() } else { text.left() };
            let x1 = if (row.top() - end.top()).abs() < 1.0 || row.contains(end.center()) { end.left() } else { text.right() };
            if x1 <= x0 {
                continue;
            }
            // The baseline of the row's text: 4 px below it is the underline (text-underline-offset).
            let baseline = placed.row.glyphs.iter().find(|g| g.pos.x >= x0 - placed.pos.x - 0.5).or(placed.row.glyphs.first()).map(|g| placed.pos.y + g.pos.y).unwrap_or(row.bottom());
            links.push((Rect::from_min_max(Pos2::new(x0, row.top()), Pos2::new(x1, row.bottom())), index));
            underlines.push((x0, x1, baseline + 4.0));
        }
    }
    Text { pos, galley, links, underlines }
}

impl Layout {
    pub fn new(ui: &Ui, doc: &Doc, avail: f32, th: &Theme) -> Layout {
        let width = avail.min(COLUMN);
        let mut out = Layout { width: avail, height: 0.0, items: Vec::new(), hrefs: Vec::new(), tops: Vec::with_capacity(doc.blocks.len()) };
        let body = Type { size: 17.0, line: 29.0, color: th.text, weight: 400, italic: false, letter_spacing: 0.0 };
        let mut y = 0.0f32;
        // Bottom margin of the block above: vertical margins collapse to the larger one.
        let mut gap = 0.0f32;
        let text = |out: &mut Layout, ui: &Ui, spans: &Inline, t: Type, x: f32, w: f32, y: f32| -> f32 {
            let built = build(ui, spans, t, th, w, &mut out.hrefs);
            let placed = place(ui, built, Pos2::new(x, y));
            let h = placed.galley.size().y;
            out.items.push(Item::Text(placed));
            h
        };
        for block in &doc.blocks {
            match block {
                Block::Heading { level, text: spans } => {
                    let (t, above, below) = match level {
                        1 => (Type { size: 32.0, line: 40.0, weight: 700, letter_spacing: -0.32, ..body }, 0.0, 24.0),
                        2 => (Type { size: 22.0, line: 28.0, weight: 700, ..body }, 28.0, 12.0),
                        _ => (Type { size: 17.0, line: 24.0, weight: 700, color: th.heading, ..body }, 20.0, 10.0),
                    };
                    y += gap.max(above);
                    out.tops.push(y);
                    y += text(&mut out, ui, spans, t, 0.0, width, y);
                    gap = below;
                }
                Block::Para { text: spans, indent, small } => {
                    let t = if *small { Type { size: 13.0, line: 20.0, color: th.text_muted, ..body } } else { body };
                    let x = *indent as f32 * 28.0;
                    y += gap;
                    out.tops.push(y);
                    y += text(&mut out, ui, spans, t, x, width - x, y);
                    gap = 16.0;
                }
                Block::Note { paras } => {
                    y += gap;
                    out.tops.push(y);
                    let at = out.items.len();
                    let note = Type { size: 15.0, line: 25.0, ..body };
                    let mut inner = y + 14.0;
                    for (i, (spans, indent)) in paras.iter().enumerate() {
                        if i > 0 {
                            inner += 8.0;
                        }
                        let x = 18.0 + *indent as f32 * 28.0;
                        inner += text(&mut out, ui, spans, note, x, width - 18.0 - x, inner);
                    }
                    let rect = Rect::from_min_max(Pos2::new(0.0, y), Pos2::new(width, inner + 14.0));
                    out.items.insert(at, Item::Box { rect, radius: 14.0, fill: th.surface, border: 0.0, border_color: th.surface });
                    y = rect.bottom();
                    gap = 16.0;
                }
                Block::List { ordered, start, items } => {
                    y += gap;
                    out.tops.push(y);
                    for (i, spans) in items.iter().enumerate() {
                        if *ordered {
                            let label = gfx::wrapped(ui, &format!("{}.", start + i as u64), theme::sans(17.0, 400), th.text, 200.0, 29.0);
                            let x = 20.0 - 6.0 - label.size().x;
                            out.items.push(Item::Text(Text { pos: Pos2::new(x, y), galley: label.galley, links: Vec::new(), underlines: Vec::new() }));
                        } else {
                            out.items.push(Item::Bullet { pos: Pos2::new(20.0 - 11.0, y + 15.0), color: th.text });
                        }
                        y += text(&mut out, ui, spans, body, 20.0, width - 20.0, y) + 4.0;
                    }
                    gap = 12.0 - 4.0;
                }
                Block::Rule => {
                    y += gap.max(24.0);
                    out.tops.push(y);
                    out.items.push(Item::Box { rect: Rect::from_min_size(Pos2::new(0.0, y), Vec2::new(width, 2.0)), radius: 0.0, fill: th.border, border: 0.0, border_color: th.border });
                    y += 2.0;
                    gap = 12.0;
                }
                Block::Code { text: code } => {
                    y += gap;
                    out.tops.push(y);
                    let g = gfx::wrapped(ui, code, theme::mono(14.0, 400), th.text, width, 22.0);
                    let h = g.size().y;
                    out.items.push(Item::Text(Text { pos: Pos2::new(0.0, y), galley: g.galley, links: Vec::new(), underlines: Vec::new() }));
                    y += h;
                    gap = 16.0;
                }
                Block::Table { head, rows } => {
                    y += gap;
                    out.tops.push(y);
                    y += table(&mut out, ui, head, rows, avail, y, th);
                    gap = 16.0;
                }
            }
        }
        out.height = y;
        out
    }
}

/// A table sized as a browser sizes one with `width: 100%`: columns share the width in
/// proportion to their content, and no column is narrower than its longest word.
fn table(out: &mut Layout, ui: &Ui, head: &[Inline], rows: &[Vec<Inline>], width: f32, top: f32, th: &Theme) -> f32 {
    let columns = rows.iter().map(Vec::len).chain([head.len()]).max().unwrap_or(0);
    if columns == 0 {
        return 0.0;
    }
    let cell = Type { size: 15.0, line: 24.0, color: th.text, weight: 400, italic: false, letter_spacing: 0.0 };
    let head_type = Type { weight: 600, color: th.heading, ..cell };
    let pad = Vec2::new(12.0, 8.0);
    let mut scratch = Vec::new();
    let mut widest = vec![0f32; columns];
    let mut narrowest = vec![0f32; columns];
    let all = std::iter::once((head, head_type)).filter(|(h, _)| !h.is_empty()).chain(rows.iter().map(|r| (r.as_slice(), cell)));
    for (cells, t) in all.clone() {
        for (c, spans) in cells.iter().enumerate() {
            let max = ui.painter().layout_job(build(ui, spans, t, th, f32::INFINITY, &mut scratch).job).size().x;
            let min = ui.painter().layout_job(build(ui, spans, t, th, 1.0, &mut scratch).job).size().x;
            widest[c] = widest[c].max(max.ceil() + 1.0);
            narrowest[c] = narrowest[c].max(min.ceil() + 1.0);
        }
    }
    let chrome = columns as f32 * (pad.x * 2.0) + columns as f32 + 1.0;
    let room = (width - chrome).max(columns as f32 * 20.0);
    let (sum_max, sum_min): (f32, f32) = (widest.iter().sum(), narrowest.iter().sum());
    let widths: Vec<f32> = if sum_max <= room {
        widest.iter().map(|w| w + (room - sum_max) * w / sum_max.max(1.0)).collect()
    } else if sum_min >= room {
        narrowest.iter().map(|w| w * room / sum_min).collect()
    } else {
        let spare = room - sum_min;
        let want: f32 = sum_max - sum_min;
        widest.iter().zip(&narrowest).map(|(max, min)| min + spare * (max - min) / want.max(1.0)).collect()
    };
    let total: f32 = widths.iter().sum::<f32>() + chrome;
    let mut y = top;
    let line = Stroke::new(1.0, th.border);
    let mut edges = vec![0.0f32];
    for w in &widths {
        edges.push(edges.last().unwrap() + w + pad.x * 2.0 + 1.0);
    }
    let mut row_tops = vec![y];
    for (index, (cells, t)) in all.enumerate() {
        let is_head = index == 0 && !head.is_empty();
        let at = out.items.len();
        let mut tallest = 0f32;
        for (c, spans) in cells.iter().enumerate() {
            let built = build(ui, spans, t, th, widths[c], &mut out.hrefs);
            let placed = place(ui, built, Pos2::new(edges[c] + 1.0 + pad.x, y + 1.0 + pad.y));
            tallest = tallest.max(placed.galley.size().y);
            out.items.push(Item::Text(placed));
        }
        let h = tallest.max(t.line) + pad.y * 2.0 + 1.0;
        if is_head {
            out.items.insert(at, Item::Box { rect: Rect::from_min_size(Pos2::new(0.0, y), Vec2::new(total, h)), radius: 0.0, fill: th.surface_hover, border: 0.0, border_color: th.border });
        }
        y += h;
        row_tops.push(y);
    }
    let bottom = y + 1.0;
    for x in &edges {
        out.items.push(Item::Box { rect: Rect::from_min_size(Pos2::new(*x, top), Vec2::new(line.width, bottom - top)), radius: 0.0, fill: th.border, border: 0.0, border_color: th.border });
    }
    for ry in &row_tops {
        out.items.push(Item::Box { rect: Rect::from_min_size(Pos2::new(0.0, *ry), Vec2::new(total, line.width)), radius: 0.0, fill: th.border, border: 0.0, border_color: th.border });
    }
    bottom - top
}

/// What the pointer did to a laid-out document this frame.
pub struct Hit {
    pub clicked: Option<usize>,
}

/// Draws the items that fall inside the clip of `ui`, the document starting at `origin`. Text is
/// selectable; links take the pointer.
pub fn paint(ui: &mut Ui, layout: &Layout, origin: Pos2, th: &Theme, enabled: bool) -> Hit {
    let view = ui.clip_rect();
    let pointer = ui.input(|i| i.pointer.hover_pos()).filter(|p| view.contains(*p) && enabled);
    let clicked = enabled && ui.input(|i| i.pointer.primary_clicked());
    let mut hit = Hit { clicked: None };
    for item in &layout.items {
        match item {
            Item::Box { rect, radius, fill, border, border_color } => {
                let rect = rect.translate(origin.to_vec2());
                if rect.intersects(view) {
                    let rect = Rect::from_min_max(Pos2::new(gfx::px(ui, rect.min.x), gfx::px(ui, rect.min.y)), Pos2::new(gfx::px(ui, rect.max.x), gfx::px(ui, rect.max.y)));
                    gfx::panel(ui, rect, *radius, *fill, *border, *border_color);
                }
            }
            Item::Bullet { pos, color } => {
                let p = *pos + origin.to_vec2();
                if view.expand(8.0).contains(p) {
                    ui.painter().circle_filled(p, 2.5, *color);
                }
            }
            Item::Text(t) => {
                let pos = Pos2::new(gfx::px(ui, origin.x + t.pos.x), gfx::px(ui, origin.y + t.pos.y));
                let rect = Rect::from_min_size(pos, t.galley.size());
                // Every text is offered to the window, in view or not, so a selection can run
                // past the edge of the pane.
                ui.put(rect, egui::Label::new(t.galley.clone()).selectable(true));
                if !rect.intersects(view) {
                    continue;
                }
                for (x0, x1, y) in &t.underlines {
                    gfx::dotted(ui, pos.x + x0, pos.x + x1, pos.y + y, th.accent);
                }
                if let Some(p) = pointer {
                    for (area, index) in &t.links {
                        if area.translate(pos.to_vec2()).contains(p) {
                            ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
                            if clicked {
                                hit.clicked = Some(*index);
                            }
                        }
                    }
                }
            }
        }
    }
    hit
}
