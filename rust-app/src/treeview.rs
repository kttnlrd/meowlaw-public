//! A source's contents as rows: the port of `frontend/src/components/TreeNode.tsx`.
//!
//! The tree is flattened to the rows that are open, each with its place, so only the rows in
//! view are drawn however large the Act.

use crate::engine::{Kind, Node};
use crate::gfx;
use crate::theme::{self, Theme};
use eframe::egui::{self, Color32, Id, Pos2, Rect, Stroke, Ui, Vec2};
use regex::Regex;
use std::collections::HashSet;
use std::sync::LazyLock;

static SLUG: LazyLock<[Regex; 5]> = LazyLock::new(|| {
    [
        Regex::new(r"(?i)^(ch|topic)-\d+$").unwrap(),
        Regex::new(r"(?i)^(tax|asic|other)-\d{4}$").unwrap(),
        Regex::new(r"(?i)^(tax|asic|other)$").unwrap(),
        Regex::new(r"(?i)^\d{4}-[a-z]+$").unwrap(),
        Regex::new(r"(?i)^[a-z]+-\d{4}$").unwrap(),
    ]
});
static SECTION_SLUG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[a-z].*-[a-z]").unwrap());

pub const ROW: f32 = 28.0;
const LOADING_ROW: f32 = 17.0;

pub enum What {
    Node,
    Signpost,
    Loading,
}

pub struct Row {
    pub what: What,
    /// The node's place in the tree: unique, and stable when a lazy part is filled in.
    pub key: String,
    pub id: String,
    pub is_section: bool,
    pub has_children: bool,
    pub open: bool,
    pub lazy: bool,
    /// The left edge of the row, from the left of the list.
    pub indent: f32,
    pub y: f32,
    pub h: f32,
    pub label_id: Option<String>,
    pub title: String,
}

pub struct Rows {
    pub rows: Vec<Row>,
    pub height: f32,
}

pub enum Action {
    Select(String),
    Toggle { key: String, id: String, lazy: bool, open: bool },
}

fn walk(nodes: &[Node], level: usize, parent_indent: f32, prefix: &str, open: &HashSet<String>, act: &str, out: &mut Rows) {
    for (i, n) in nodes.iter().enumerate() {
        let key = if prefix.is_empty() { i.to_string() } else { format!("{prefix}/{i}") };
        // Each level is indented from its parent's own indent, as nested boxes are.
        let indent = parent_indent + level as f32 * 14.0;
        let is_section = n.kind == Kind::Section;
        if n.kind == Kind::Signpost {
            out.rows.push(Row { what: What::Signpost, key, id: n.id.clone(), is_section: false, has_children: false, open: false, lazy: false, indent, y: out.height, h: ROW, label_id: None, title: format!("Part {} — {}", n.id, n.title) });
            out.height += ROW;
            continue;
        }
        let slug_like = SLUG.iter().any(|r| r.is_match(&n.id)) || (is_section && SECTION_SLUG.is_match(&n.id));
        let show_id = !slug_like && n.id != n.title && act != "treaties" && act != "private-rulings";
        let label_id = show_id.then(|| if n.kind == Kind::Division && !matches!(act, "cases" | "rulings" | "tax-cases") { format!("Division {}", n.id) } else { n.id.clone() });
        let has_children = !is_section && (n.lazy || !n.children.is_empty());
        let is_open = has_children && open.contains(&key);
        out.rows.push(Row { what: What::Node, key: key.clone(), id: n.id.clone(), is_section, has_children, open: is_open, lazy: n.lazy, indent, y: out.height, h: ROW, label_id, title: n.title.clone() });
        out.height += ROW;
        if is_open && n.lazy {
            out.rows.push(Row { what: What::Loading, key: format!("{key}/…"), id: String::new(), is_section: false, has_children: false, open: false, lazy: false, indent: indent + level as f32 * 14.0 + 34.0, y: out.height, h: LOADING_ROW, label_id: None, title: "Loading…".into() });
            out.height += LOADING_ROW;
        } else if is_open {
            walk(&n.children, level + 1, indent, &key, open, act, out);
        }
    }
}

pub fn build(parts: &[Node], open: &HashSet<String>, act: &str) -> Rows {
    let mut out = Rows { rows: Vec::new(), height: 0.0 };
    walk(parts, 0, 0.0, "", open, act, &mut out);
    out
}

/// The keys of every node on the way to `section`, so its ancestors can be opened.
pub fn path_to(parts: &[Node], section: &str) -> Option<Vec<String>> {
    fn find(nodes: &[Node], prefix: &str, section: &str, trail: &mut Vec<String>) -> bool {
        for (i, n) in nodes.iter().enumerate() {
            let key = if prefix.is_empty() { i.to_string() } else { format!("{prefix}/{i}") };
            if n.kind == Kind::Section {
                if n.id == section {
                    return true;
                }
                continue;
            }
            trail.push(key.clone());
            if find(&n.children, &key, section, trail) {
                return true;
            }
            trail.pop();
        }
        false
    }
    let mut trail = Vec::new();
    find(parts, "", section, &mut trail).then_some(trail)
}

/// Every key that holds children and is already loaded: the contents page opens them all.
pub fn all_keys(nodes: &[Node], prefix: &str, out: &mut HashSet<String>) {
    for (i, n) in nodes.iter().enumerate() {
        let key = if prefix.is_empty() { i.to_string() } else { format!("{prefix}/{i}") };
        if n.kind == Kind::Section || n.kind == Kind::Signpost || n.lazy {
            continue;
        }
        all_keys(&n.children, &key, out);
        out.insert(key);
    }
}

/// Draws the rows that fall inside `view`, the list starting at `origin`. Returns what was clicked.
pub fn paint(ui: &Ui, id: Id, rows: &Rows, origin: Pos2, width: f32, view: Rect, active: &str, th: &Theme, panel: Color32, enabled: bool) -> Option<Action> {
    let mut action = None;
    let first = rows.rows.partition_point(|r| origin.y + r.y + r.h < view.top());
    for row in &rows.rows[first..] {
        let top = origin.y + row.y;
        if top > view.bottom() {
            break;
        }
        let left = origin.x + row.indent;
        let rect = Rect::from_min_size(Pos2::new(left, top), Vec2::new((width - row.indent).max(40.0), row.h));
        let mid = rect.center().y;
        match row.what {
            What::Loading => {
                let g = gfx::line(ui, &row.title, theme::sans(11.0, 400), th.text_muted);
                gfx::paint_left(ui, left, mid, &g);
                continue;
            }
            What::Signpost => {
                ui.painter().hline(rect.x_range(), gfx::px(ui, top) + 0.5, Stroke::new(1.0, th.border));
                let g = gfx::elided(ui, &row.title.to_uppercase(), theme::sans(10.0, 600), th.text_muted, rect.width() - 12.0);
                gfx::paint_left(ui, left + 6.0, mid, &g);
                continue;
            }
            What::Node => {}
        }
        let is_active = row.is_section && !active.is_empty() && row.id == active;
        let fill = if is_active {
            // rgba(39,158,136,0.12) over the panel.
            let fill = theme::blend(panel, Color32::from_rgb(39, 158, 136), 0.12);
            ui.painter().rect_filled(rect, gfx::radius(4.0), fill);
            fill
        } else {
            panel
        };
        let response = gfx::button(ui, id.with(&row.key), rect.intersect(view), enabled);
        if response.clicked() {
            action = Some(if row.is_section { Action::Select(row.id.clone()) } else { Action::Toggle { key: row.key.clone(), id: row.id.clone(), lazy: row.lazy, open: !row.open } });
        }
        if row.has_children {
            // The ▶ / ▼ of the web app: a small solid triangle in a 28-pixel slot.
            let c = Pos2::new(gfx::px(ui, left + 6.0 + 14.0), gfx::px(ui, mid));
            let points = if row.open {
                vec![Pos2::new(c.x - 4.0, c.y - 2.5), Pos2::new(c.x + 4.0, c.y - 2.5), Pos2::new(c.x, c.y + 3.5)]
            } else {
                vec![Pos2::new(c.x - 3.0, c.y - 4.0), Pos2::new(c.x + 4.0, c.y), Pos2::new(c.x - 3.0, c.y + 4.0)]
            };
            ui.painter().add(egui::Shape::convex_polygon(points, th.text_muted, Stroke::NONE));
        }
        let row_color = if row.is_section { th.text } else { th.text_muted };
        let weight = if row.is_section { 400 } else { 500 };
        // After the 28-pixel slot: the id, 4 in; then the title, 6 after the id or 4 in.
        let slot_end = left + 6.0 + 28.0;
        let right = rect.right() - 6.0;
        let mut x = slot_end + 4.0;
        if let Some(label) = &row.label_id {
            let g = gfx::line(ui, label, theme::sans(12.0, weight), th.heading);
            gfx::paint_left(ui, x, mid, &g);
            x += g.size().x + 6.0;
        }
        if !row.title.is_empty() {
            let text = if row.label_id.is_some() { format!("— {}", row.title) } else { row.title.clone() };
            // opacity 0.85 on the row's colour.
            let color = theme::blend(fill, row_color, 0.85);
            let g = gfx::elided(ui, &text, theme::sans(12.0, 400), color, right - x);
            gfx::paint_left(ui, x, mid, &g);
        }
    }
    action
}
