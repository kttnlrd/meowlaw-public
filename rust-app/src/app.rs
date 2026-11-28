//! The window: sidebar, home screen, contents page, section reader and search, laid out to the
//! measurements of the web app (frontend/src/App.tsx and lawkitty-ui.css).

use crate::engine::{self, Answer, Engine, Licence, Request, SearchDoc, SectionDoc, SourceGroup, TreeDoc};
use crate::gfx::{self, Icon, Icons, Scroll};
use crate::reader;
use crate::theme::{self, Theme};
use crate::treeview::{self, Action, Rows};
use crate::yarn::Yarn;
use eframe::egui::{self, Align, Color32, Id, Key, Pos2, Rect, Sense, Stroke, StrokeKind, TextureHandle, TextureOptions, Ui, Vec2};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::PathBuf;
use std::time::{Duration, Instant};

pub struct Options {
    pub data: PathBuf,
    pub state_dir: PathBuf,
    /// `itaa-1997`, `itaa-1997/8-1` or `search:words`: the screen to start on.
    pub open: Option<String>,
    pub theme: Option<String>,
    pub zoom: Option<f32>,
    /// Write start-up and loading times to this file, open ITAA 1997 and s 8-1, then quit.
    pub bench: Option<PathBuf>,
    pub started: Instant,
}

#[derive(Serialize, Deserialize, Clone, PartialEq)]
struct Pin {
    act: String,
    section: String,
    title: String,
}

/// What the web app keeps in the browser's local storage.
#[derive(Serialize, Deserialize, Default)]
struct WindowState {
    #[serde(default)]
    sidebar_width: Option<f32>,
    #[serde(default)]
    pins: Vec<Pin>,
}

#[derive(Clone, PartialEq, Default)]
struct Place {
    act: String,
    section: String,
    browsing: bool,
    search: bool,
}

#[derive(PartialEq)]
enum Modal {
    None,
    Settings,
    Changelog,
}

enum Bench {
    Off,
    WaitFirstFrame,
    WaitTree(Instant),
    WaitSection(Instant),
    Done,
}

pub struct LawKitty {
    eng: Engine,
    opts: Options,
    th: Theme,
    theme_name: String,
    groups: Vec<SourceGroup>,
    pinned: Vec<String>,
    window: WindowState,

    at: Place,
    history: Vec<Place>,
    future: Vec<Place>,

    tree: Option<TreeDoc>,
    tree_act: String,
    tree_error: Option<String>,
    side_open: HashSet<String>,
    main_open: HashSet<String>,
    asked_parts: HashSet<String>,
    side_rows: Option<Rows>,
    main_rows: Option<Rows>,

    doc: Option<SectionDoc>,
    doc_layout: Option<reader::Layout>,
    doc_error: Option<String>,
    licence_open: bool,
    anchor: Option<String>,

    side_scroll: Scroll,
    main_scroll: Scroll,
    modal_scroll: Scroll,
    resizing: bool,

    home_query: String,
    search_query: String,
    search: Option<SearchDoc>,
    searching: bool,
    search_page: usize,
    focus_search: bool,

    yarn: Yarn,
    yarn_at: Instant,
    yarn_image: Option<(TextureHandle, [usize; 2])>,
    lockup: Option<(TextureHandle, bool, u32)>,
    icons: Icons,
    modal: Modal,

    frames: u64,
    bench: Bench,
    log: Vec<String>,
}

const LOCKUP_DARK: &[u8] = include_bytes!("../assets/lawkitty-lockup-cat.png");
const LOCKUP_LIGHT: &[u8] = include_bytes!("../assets/lawkitty-lockup-cat-light.png");

fn app_version() -> String {
    serde_json::from_str::<serde_json::Value>(lawkitty_local::INFO_JSON).ok().and_then(|v| v["version"].as_str().map(String::from)).unwrap_or_default()
}

impl LawKitty {
    pub fn new(cc: &eframe::CreationContext<'_>, opts: Options) -> LawKitty {
        let ctx = cc.egui_ctx.clone();
        let eng = Engine::start(&opts.data, opts.state_dir.join("state.json"), ctx.clone());
        let groups = engine::source_groups(&eng.acts);
        let window: WindowState = std::fs::read(opts.state_dir.join("desktop.json")).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        let theme_name = opts.theme.clone().unwrap_or_else(|| eng.theme());
        let bench = if opts.bench.is_some() { Bench::WaitFirstFrame } else { Bench::Off };
        let mut app = LawKitty {
            pinned: eng.pinned(),
            eng,
            th: theme::by_name(&theme_name),
            theme_name,
            groups,
            window,
            at: Place::default(),
            history: Vec::new(),
            future: Vec::new(),
            tree: None,
            tree_act: String::new(),
            tree_error: None,
            side_open: HashSet::new(),
            main_open: HashSet::new(),
            asked_parts: HashSet::new(),
            side_rows: None,
            main_rows: None,
            doc: None,
            doc_layout: None,
            doc_error: None,
            licence_open: false,
            anchor: None,
            side_scroll: Scroll::default(),
            main_scroll: Scroll::default(),
            modal_scroll: Scroll::default(),
            resizing: false,
            home_query: String::new(),
            search_query: String::new(),
            search: None,
            searching: false,
            search_page: 0,
            focus_search: false,
            yarn: Yarn::new(),
            yarn_at: Instant::now(),
            yarn_image: None,
            lockup: None,
            icons: Icons::default(),
            modal: Modal::None,
            frames: 0,
            bench,
            log: Vec::new(),
            opts,
        };
        app.apply_theme(&ctx);
        if let Some(open) = app.opts.open.clone() {
            if let Some(q) = open.strip_prefix("search:") {
                app.open_search(q);
            } else if let Some((act, section)) = open.split_once('/') {
                app.open_section(act, section.strip_prefix('s').filter(|s| s.starts_with(|c: char| c.is_ascii_digit())).unwrap_or(section), None);
            } else if !open.is_empty() {
                app.open_source(&open);
            }
            app.history.clear();
        }
        app
    }

    fn apply_theme(&mut self, ctx: &egui::Context) {
        self.th = theme::by_name(&self.theme_name);
        let th = self.th;
        ctx.set_theme(if th.dark { egui::Theme::Dark } else { egui::Theme::Light });
        let mut visuals = if th.dark { egui::Visuals::dark() } else { egui::Visuals::light() };
        visuals.panel_fill = th.bg;
        visuals.window_fill = th.surface;
        visuals.override_text_color = Some(th.text);
        visuals.selection.bg_fill = Color32::from_rgb(0, 120, 215);
        visuals.selection.stroke = Stroke::new(1.0, Color32::WHITE);
        visuals.text_cursor.stroke = Stroke::new(1.0, th.text);
        ctx.set_visuals(visuals);
        self.doc_layout = None;
        self.lockup = None;
    }

    fn save_window(&self) {
        let _ = std::fs::create_dir_all(&self.opts.state_dir);
        let _ = std::fs::write(self.opts.state_dir.join("desktop.json"), serde_json::to_vec_pretty(&self.window).unwrap_or_default());
    }

    // -----------------------------------------------------------------------
    // Navigation
    // -----------------------------------------------------------------------

    fn has_content(&self) -> bool {
        !self.at.search && (!self.at.section.is_empty() || self.at.browsing)
    }

    fn remember(&mut self) {
        if self.history.last() != Some(&self.at) {
            self.history.push(self.at.clone());
        }
        self.future.clear();
    }

    fn go_home(&mut self) {
        self.remember();
        self.at = Place { act: self.at.act.clone(), ..Place::default() };
        self.doc = None;
        self.doc_layout = None;
        self.main_scroll.offset = 0.0;
        self.side_scroll.offset = 0.0;
    }

    fn load_tree(&mut self, act: &str, open: Option<&str>) {
        if self.tree_act == act && (self.tree.is_some() || self.tree_error.is_none()) && !self.tree_act.is_empty() {
            return;
        }
        self.tree = None;
        self.tree_error = None;
        self.tree_act = act.to_string();
        self.side_open.clear();
        self.main_open.clear();
        self.asked_parts.clear();
        self.side_rows = None;
        self.main_rows = None;
        self.eng.ask(Request::Tree { act: act.to_string(), open: open.map(String::from) });
    }

    fn open_source(&mut self, act: &str) {
        self.remember();
        self.at = Place { act: act.to_string(), section: String::new(), browsing: true, search: false };
        self.doc = None;
        self.doc_layout = None;
        self.doc_error = None;
        self.main_scroll.offset = 0.0;
        self.side_scroll.offset = 0.0;
        self.load_tree(act, None);
    }

    fn open_section(&mut self, act: &str, section: &str, anchor: Option<String>) {
        self.remember();
        let same = self.at.act == act && self.at.section == section && self.doc.is_some();
        self.at = Place { act: act.to_string(), section: section.to_string(), browsing: self.at.browsing && self.at.act == act, search: false };
        self.anchor = anchor;
        self.load_tree(act, Some(section));
        self.reveal_section();
        if !same {
            self.doc_error = None;
            self.eng.ask(Request::Section { act: act.to_string(), section: section.to_string() });
        }
    }

    fn open_search(&mut self, query: &str) {
        self.remember();
        self.at = Place { act: self.at.act.clone(), section: String::new(), browsing: false, search: true };
        self.doc = None;
        self.doc_layout = None;
        self.main_scroll.offset = 0.0;
        self.focus_search = true;
        self.search_query = query.to_string();
        if !query.trim().is_empty() {
            self.run_search();
        }
    }

    fn run_search(&mut self) {
        let query = self.search_query.trim().to_string();
        if query.is_empty() {
            return;
        }
        self.searching = true;
        self.search_page = 0;
        self.main_scroll.offset = 0.0;
        self.eng.ask(Request::Search { query, first_act: self.at.act.clone() });
    }

    fn restore(&mut self, place: Place) {
        self.at = place;
        self.main_scroll.offset = 0.0;
        if self.at.search {
            self.doc = None;
            self.doc_layout = None;
            return;
        }
        if !self.at.act.is_empty() && (self.at.browsing || !self.at.section.is_empty()) {
            let (act, section) = (self.at.act.clone(), self.at.section.clone());
            self.load_tree(&act, (!section.is_empty()).then_some(section.as_str()));
        }
        if self.at.section.is_empty() {
            self.doc = None;
            self.doc_layout = None;
        } else {
            self.reveal_section();
            self.eng.ask(Request::Section { act: self.at.act.clone(), section: self.at.section.clone() });
        }
    }

    fn back(&mut self) {
        if let Some(place) = self.history.pop() {
            self.future.push(self.at.clone());
            self.restore(place);
        }
    }

    fn forward(&mut self) {
        if let Some(place) = self.future.pop() {
            self.history.push(self.at.clone());
            self.restore(place);
        }
    }

    /// Opens the sidebar tree down to the section being read, fetching its part if the tree
    /// is shallow and the part has not come yet.
    fn reveal_section(&mut self) {
        let (Some(tree), false) = (&self.tree, self.at.section.is_empty()) else { return };
        if self.tree_act != self.at.act {
            return;
        }
        match treeview::path_to(&tree.parts, &self.at.section) {
            Some(trail) => {
                let before = self.side_open.len();
                self.side_open.extend(trail);
                if self.side_open.len() != before {
                    self.side_rows = None;
                }
            }
            None if tree.shallow => {
                let key = format!("section:{}", self.at.section);
                if self.asked_parts.insert(key) {
                    self.eng.ask(Request::Part { act: self.at.act.clone(), id: None, section: Some(self.at.section.clone()) });
                }
            }
            None => {}
        }
    }

    fn follow_link(&mut self, ctx: &egui::Context, href: &str) {
        if href.starts_with("http://") || href.starts_with("https://") || href.starts_with("mailto:") {
            ctx.open_url(egui::OpenUrl::new_tab(href));
            return;
        }
        // "/itaa-1997/s995-1#s995-1-deduct", "/itaa-1936/s6", "#anchor".
        let (path, anchor) = match href.split_once('#') {
            Some((p, a)) => (p, Some(a.to_string())),
            None => (href, None),
        };
        let mut parts = path.trim_matches('/').splitn(2, '/');
        let (act, section) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""));
        if act.is_empty() {
            self.anchor = anchor;
            return;
        }
        if !self.eng.has_act(act) {
            return;
        }
        let section = match section.strip_prefix('s') {
            Some(rest) if rest.starts_with(|c: char| c.is_ascii_digit()) => rest,
            _ => section,
        };
        if section.is_empty() {
            self.open_source(act);
        } else {
            self.open_section(act, section, anchor);
        }
    }

    // -----------------------------------------------------------------------
    // Answers from the data thread
    // -----------------------------------------------------------------------

    fn pump(&mut self) {
        while let Ok(answer) = self.eng.answers.try_recv() {
            match answer {
                Answer::Tree { act, result, ms } => {
                    if act != self.tree_act {
                        continue;
                    }
                    self.log.push(format!("tree {act}: read and parsed in {ms:.1} ms"));
                    match result {
                        Ok(tree) => {
                            self.main_open.clear();
                            treeview::all_keys(&tree.parts, "", &mut self.main_open);
                            self.tree = Some(tree);
                            self.side_rows = None;
                            self.main_rows = None;
                            self.reveal_section();
                        }
                        Err(e) => self.tree_error = Some(e),
                    }
                }
                Answer::Part { act, key, result } => {
                    if act != self.tree_act {
                        continue;
                    }
                    let (Some(tree), Some(part)) = (&mut self.tree, result) else {
                        self.asked_parts.remove(&key);
                        continue;
                    };
                    // The loaded node replaces its lazy shell: a whole part, or one division of a split part.
                    fn place(nodes: &mut [engine::Node], part: &mut Option<engine::Node>) {
                        for n in nodes.iter_mut() {
                            let Some(p) = part.as_ref() else { return };
                            if n.lazy && n.id == p.id {
                                *n = part.take().expect("checked");
                                return;
                            }
                            if n.kind != engine::Kind::Section && !n.lazy {
                                place(&mut n.children, part);
                            }
                        }
                    }
                    let mut slot = Some(part);
                    place(&mut tree.parts, &mut slot);
                    // The contents page opens everything that is loaded.
                    treeview::all_keys(&tree.parts, "", &mut self.main_open);
                    self.side_rows = None;
                    self.main_rows = None;
                    self.reveal_section();
                }
                Answer::Section { act, section, result, ms } => {
                    if act != self.at.act || section != self.at.section {
                        continue;
                    }
                    self.log.push(format!("section {act} {section}: read and prepared in {ms:.1} ms"));
                    match result {
                        Ok(doc) => {
                            self.doc = Some(doc);
                            self.doc_layout = None;
                            self.doc_error = None;
                            self.licence_open = false;
                            self.main_scroll.offset = 0.0;
                        }
                        Err(e) => {
                            self.doc = None;
                            self.doc_layout = None;
                            self.doc_error = Some(e);
                        }
                    }
                }
                Answer::Search { doc } => {
                    if doc.query == self.search_query.trim() {
                        self.search = Some(doc);
                        self.searching = false;
                    }
                }
            }
        }
    }

    // -----------------------------------------------------------------------
    // Frame
    // -----------------------------------------------------------------------

    fn keys(&mut self, ctx: &egui::Context) {
        let typing = ctx.memory(|m| m.focused().is_some());
        let (slash, escape, back, forward) = ctx.input(|i| {
            (
                i.key_pressed(Key::Slash) && !i.modifiers.ctrl && !i.modifiers.alt,
                i.key_pressed(Key::Escape),
                i.modifiers.alt && i.key_pressed(Key::ArrowLeft) || i.pointer.button_pressed(egui::PointerButton::Extra1),
                i.modifiers.alt && i.key_pressed(Key::ArrowRight) || i.pointer.button_pressed(egui::PointerButton::Extra2),
            )
        });
        if escape && self.modal != Modal::None {
            self.modal = Modal::None;
        } else if slash && !typing && self.modal == Modal::None {
            self.open_search("");
        } else if back {
            self.back();
        } else if forward {
            self.forward();
        }
    }

    pub fn frame(&mut self, ui: &mut Ui) {
        self.frames += 1;
        let ctx = ui.ctx().clone();
        if let Some(zoom) = self.opts.zoom {
            if (ctx.pixels_per_point() - zoom).abs() > 0.001 {
                ctx.set_pixels_per_point(zoom);
            }
        }
        self.pump();
        self.keys(&ctx);
        if self.frames == 1 {
            // Development aid: LAWKITTY_PROBE names a file of "weight size text" lines; the width
            // of each, as laid out here, is written beside it.
            if let Some(path) = std::env::var_os("LAWKITTY_PROBE") {
                let mut out = String::new();
                for line in std::fs::read_to_string(&path).unwrap_or_default().lines() {
                    let mut parts = line.splitn(3, ' ');
                    let (Some(weight), Some(size), Some(text)) = (parts.next(), parts.next(), parts.next()) else { continue };
                    let font = if weight == "mono" { theme::mono(size.parse().unwrap_or(12.0), 400) } else { theme::sans(size.parse().unwrap_or(12.0), weight.parse().unwrap_or(400)) };
                    out.push_str(&format!("{:.3}	{line}
", gfx::line(ui, text, font, Color32::WHITE).galley.size().x));
                }
                let _ = std::fs::write(std::path::Path::new(&path).with_extension("out"), out);
            }
        }
        let th = self.th;
        let screen = ctx.content_rect();
        ui.painter().rect_filled(screen, 0.0, th.bg);
        let enabled = self.modal == Modal::None;

        let sidebar_w = self.window.sidebar_width.unwrap_or(400.0).clamp(280.0, 600.0).min((screen.width() - 320.0).max(280.0));
        let side = Rect::from_min_size(screen.min, Vec2::new(sidebar_w, screen.height()));
        self.sidebar(ui, side, enabled);

        // The resize handle: four pixels between the sidebar and the page.
        let handle = Rect::from_min_size(Pos2::new(side.right(), screen.top()), Vec2::new(4.0, screen.height()));
        let grip = ui.interact(handle, Id::unique("resize"), if enabled { Sense::drag() } else { Sense::hover() });
        if grip.hovered() || grip.dragged() {
            ctx.set_cursor_icon(egui::CursorIcon::ResizeColumn);
            ui.painter().rect_filled(handle, 0.0, theme::rgb(0xc6f432));
        }
        if grip.dragged() {
            if let Some(p) = grip.interact_pointer_pos() {
                self.window.sidebar_width = Some((p.x - screen.left()).clamp(280.0, 600.0));
                self.resizing = true;
            }
        } else if self.resizing {
            self.resizing = false;
            self.save_window();
        }

        // The page: at most 960 wide, centred in what the sidebar leaves.
        let left = handle.right();
        let room = (screen.right() - left).max(200.0);
        let width = room.min(960.0);
        let main = Rect::from_min_size(Pos2::new(gfx::px(ui, left + (room - width) / 2.0), screen.top()), Vec2::new(width, screen.height()));
        self.main(ui, main, enabled);
        self.modals(&ctx, screen);
        self.run_bench(&ctx);
    }

    fn run_bench(&mut self, ctx: &egui::Context) {
        let ms = |t: Instant| t.elapsed().as_secs_f64() * 1000.0;
        match self.bench {
            Bench::Off | Bench::Done => {}
            Bench::WaitFirstFrame => {
                if self.frames == 1 {
                    self.log.push(format!("startup: first frame drawn {:.1} ms after the program started ({} sources)", ms(self.opts.started), self.eng.acts.len()));
                    ctx.request_repaint();
                } else {
                    self.log.push(format!("startup: first frame on screen {:.1} ms after the program started", ms(self.opts.started)));
                    self.open_source("itaa-1997");
                    self.bench = Bench::WaitTree(Instant::now());
                }
            }
            Bench::WaitTree(t) => {
                if self.tree.is_some() && self.side_rows.is_some() {
                    self.log.push(format!("open ITAA 1997: contents drawn {:.1} ms after the click", ms(t)));
                    self.open_section("itaa-1997", "8-1", None);
                    self.bench = Bench::WaitSection(Instant::now());
                }
                ctx.request_repaint();
            }
            Bench::WaitSection(t) => {
                if self.doc.is_some() && self.doc_layout.is_some() {
                    self.log.push(format!("open s 8-1: section drawn {:.1} ms after the click", ms(t)));
                    if let Some(path) = &self.opts.bench {
                        let _ = std::fs::write(path, self.log.join("\n") + "\n");
                    }
                    self.bench = Bench::Done;
                    ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                }
                ctx.request_repaint();
            }
        }
    }

    // -----------------------------------------------------------------------
    // Sidebar
    // -----------------------------------------------------------------------

    fn sidebar(&mut self, ui: &mut Ui, rect: Rect, enabled: bool) {
        let th = self.th;
        let has_content = self.has_content();
        ui.painter().rect_filled(rect, 0.0, th.surface);
        ui.painter().vline(rect.right() - 0.5, rect.y_range(), Stroke::new(1.0, th.border));
        let inner = Rect::from_min_max(rect.min, Pos2::new(rect.right() - 1.0, rect.bottom()));

        // Header.
        let header_h = if has_content { 12.0 + 36.0 + 12.0 + 1.0 } else { gfx::px(ui, 12.0 + 6.0 + 13.2 + 2.0 + 12.0 + 1.0) };
        let header = Rect::from_min_size(inner.min, Vec2::new(inner.width(), header_h));
        ui.painter().hline(header.x_range(), header.bottom() - 0.5, Stroke::new(1.0, th.border));
        if has_content {
            let label = gfx::line(ui, "← Sources", theme::sans(12.0, 600), th.accent);
            let button = Rect::from_min_size(Pos2::new(header.left() + 14.0, header.top() + 12.0 + (36.0 - 30.0) / 2.0), Vec2::new(label.size().x + 20.0 + 4.0, 30.0));
            let response = gfx::button(ui, Id::unique("sources"), button, enabled);
            gfx::panel(ui, button, 14.0, Color32::TRANSPARENT, 2.0, th.line_strong);
            gfx::paint_centered(ui, button.center(), &label);
            if response.clicked() {
                self.go_home();
            }
            let name = gfx::elided(ui, &engine::short_act_name(&self.at.act), theme::sans(13.0, 600), th.text, header.right() - 14.0 - (button.right() + 8.0));
            gfx::paint_left(ui, button.right() + 8.0, header.top() + 12.0 + 18.0, &name);
        } else {
            let label = gfx::spaced(ui, "SOURCES", theme::sans(11.0, 700), th.text_muted, 0.8);
            gfx::paint_left(ui, header.left() + 14.0 + 4.0, header.top() + 12.0 + 6.0 + 6.6, &label);
        }

        // Footer: Search, then the local user.
        let account_h = gfx::px(ui, 6.0 + 13.2 + 6.0 + 2.0);
        let footer_h = 1.0 + 12.0 + 56.0 + 8.0 + account_h + 12.0;
        let footer = Rect::from_min_max(Pos2::new(inner.left(), inner.bottom() - footer_h), inner.max);
        ui.painter().hline(footer.x_range(), footer.top() + 0.5, Stroke::new(1.0, th.border));
        let search = Rect::from_min_size(Pos2::new(footer.left() + 12.0, footer.top() + 13.0), Vec2::new(footer.width() - 24.0, 56.0));
        let response = gfx::button(ui, Id::unique("side-search"), search, enabled);
        gfx::panel(ui, search, 14.0, th.surface, 2.0, th.accent);
        self.icons.paint(ui, Icon::SearchBold, Rect::from_min_size(Pos2::new(search.left() + 18.0, search.center().y - 10.0), Vec2::splat(20.0)), th.accent);
        let label = gfx::line(ui, "Search", theme::sans(16.0, 700), th.text);
        gfx::paint_left(ui, search.left() + 18.0 + 20.0 + 10.0, search.center().y, &label);
        let slash = gfx::line(ui, "/", theme::mono(12.0, 400), th.text_muted);
        let kbd = Vec2::new(slash.size().x + 14.0 + 4.0, gfx::px(ui, slash.size().y + 4.0 + 4.0));
        let kbd = Rect::from_min_size(Pos2::new(search.right() - 18.0 - kbd.x, gfx::px(ui, search.center().y - kbd.y / 2.0)), kbd);
        gfx::panel(ui, kbd, 6.0, Color32::TRANSPARENT, 2.0, th.border);
        gfx::paint_centered(ui, kbd.center(), &slash);
        if response.clicked() {
            self.open_search("");
        }
        let account = Rect::from_min_size(Pos2::new(footer.left() + 12.0, search.bottom() + 8.0), Vec2::new(footer.width() - 24.0, account_h));
        let response = gfx::button(ui, Id::unique("account"), account, enabled).on_hover_text("Settings");
        gfx::panel(ui, account, 6.0, th.bg, 1.0, th.border);
        self.icons.paint(ui, Icon::User, Rect::from_min_size(Pos2::new(account.left() + 9.0, account.center().y - 6.5), Vec2::splat(13.0)), th.text);
        let label = gfx::line(ui, "Local user", theme::sans(11.0, 600), th.text);
        gfx::paint_left(ui, account.left() + 9.0 + 13.0 + 6.0, account.center().y, &label);
        if response.clicked() {
            self.modal = Modal::Settings;
        }

        // Body: the open source's contents, or the list of sources.
        let body = Rect::from_min_max(Pos2::new(inner.left(), header.bottom()), Pos2::new(inner.right(), footer.top()));
        let clip = ui.clip_rect();
        ui.set_clip_rect(body);
        if has_content {
            self.side_tree(ui, body, enabled);
        } else {
            self.source_list(ui, body, enabled);
        }
        ui.set_clip_rect(clip);
    }

    fn side_tree(&mut self, ui: &mut Ui, body: Rect, enabled: bool) {
        let th = self.th;
        let Some(tree) = &self.tree else {
            let text = self.tree_error.clone().unwrap_or_else(|| "Loading...".into());
            let g = gfx::wrapped(ui, &text, theme::sans(13.0, 400), th.text_muted, body.width() - 64.0, 18.0);
            gfx::paint_at(ui, body.min + Vec2::new(32.0, 32.0), &g);
            return;
        };
        if self.side_rows.is_none() {
            self.side_rows = Some(treeview::build(&tree.parts, &self.side_open, &self.at.act));
        }
        let rows = self.side_rows.as_ref().expect("built above");
        let content = rows.height + 16.0;
        let offset = self.side_scroll.run(ui, Id::unique("side-scroll"), body, content, &th, enabled);
        let width = Scroll::inner_width(body, content) - 16.0;
        let origin = Pos2::new(body.left() + 8.0, body.top() + 8.0 - offset);
        let action = treeview::paint(ui, Id::unique("side-tree"), rows, origin, width, body, &self.at.section, &th, th.surface, enabled);
        self.tree_action(action, true);
    }

    fn tree_action(&mut self, action: Option<Action>, side: bool) {
        match action {
            Some(Action::Select(id)) => {
                let act = self.at.act.clone();
                self.open_section(&act, &id, None);
            }
            Some(Action::Toggle { key, id, lazy, open }) => {
                let set = if side { &mut self.side_open } else { &mut self.main_open };
                if open {
                    set.insert(key);
                    if lazy && self.asked_parts.insert(id.clone()) {
                        self.eng.ask(Request::Part { act: self.at.act.clone(), id: Some(id), section: None });
                    }
                } else {
                    set.remove(&key);
                }
                if side {
                    self.side_rows = None;
                } else {
                    self.main_rows = None;
                }
            }
            None => {}
        }
    }

    fn source_list(&mut self, ui: &mut Ui, body: Rect, enabled: bool) {
        let th = self.th;
        // Pinned sources first, then the groups.
        let all: Vec<&engine::SourceItem> = self.groups.iter().flat_map(|g| g.items.iter()).collect();
        let pinned: Vec<engine::SourceItem> = self.pinned.iter().filter_map(|id| all.iter().find(|i| i.id == *id).map(|i| (*i).clone())).collect();
        let mut groups: Vec<(&str, Vec<engine::SourceItem>)> = Vec::new();
        if !pinned.is_empty() {
            groups.push(("Pinned", pinned));
        }
        groups.extend(self.groups.iter().map(|g| (g.label.as_str(), g.items.clone())));

        let font = theme::sans(13.0, 400);
        let measure = |ui: &Ui, bar: f32| -> f32 {
            let text_w = body.width() - 28.0 - bar - 28.0 - 20.0;
            let mut y = 12.0;
            for (_, items) in &groups {
                y += gfx::px(ui, 13.2) + 6.0;
                for item in items {
                    y += gfx::wrapped(ui, &item.label, font.clone(), th.text, text_w, 20.0).size().y + 10.0;
                }
                y += 18.0;
            }
            y + 16.0
        };
        let mut content = measure(ui, 0.0);
        if content > body.height() {
            content = measure(ui, gfx::BAR);
        }
        let offset = self.side_scroll.run(ui, Id::unique("side-scroll"), body, content, &th, enabled);
        let width = Scroll::inner_width(body, content);
        let (left, right) = (body.left() + 14.0, body.left() + width - 14.0);
        let text_w = right - left - 28.0 - 20.0;
        let mut y = body.top() + 12.0 - offset;
        let mut open: Option<String> = None;
        let mut toggle: Option<String> = None;
        for (label, items) in &groups {
            let heading = gfx::spaced(ui, &label.to_uppercase(), theme::sans(11.0, 700), th.text_muted, 0.8);
            let heading_h = gfx::px(ui, 13.2);
            if y + heading_h > body.top() && y < body.bottom() {
                gfx::paint_left(ui, left + 8.0, y + heading_h / 2.0, &heading);
            }
            y += heading_h + 6.0;
            for item in items {
                let g = gfx::wrapped(ui, &item.label, font.clone(), th.text, text_w, 20.0);
                let h = g.size().y + 10.0;
                let row = Rect::from_min_size(Pos2::new(left, y), Vec2::new(right - left, h));
                if row.bottom() > body.top() && row.top() < body.bottom() {
                    let id = Id::unique(("source", *label, &item.id));
                    let response = gfx::button(ui, id, row.intersect(body), enabled);
                    let star = Rect::from_min_size(Pos2::new(row.right() - 28.0, gfx::px(ui, row.center().y - 13.0)), Vec2::new(28.0, 26.0));
                    let star_response = gfx::button(ui, id.with("pin"), star.intersect(body), enabled);
                    let hovered = response.hovered() || star_response.hovered();
                    if hovered {
                        ui.painter().rect_filled(row, gfx::radius(8.0), th.bg);
                    }
                    gfx::paint_at(ui, Pos2::new(left + 10.0, y + 5.0), &g);
                    let is_pinned = self.pinned.contains(&item.id);
                    if hovered || is_pinned {
                        let glyph = gfx::line(ui, if is_pinned { "★" } else { "☆" }, theme::system(13.0), if is_pinned { th.accent } else { th.text_muted });
                        gfx::paint_centered(ui, star.center(), &glyph);
                    }
                    if star_response.clicked() {
                        toggle = Some(item.id.clone());
                    } else if response.clicked() {
                        open = Some(item.id.clone());
                    }
                }
                y += h;
            }
            y += 18.0;
        }
        if let Some(id) = toggle {
            let mut next = self.pinned.clone();
            match next.iter().position(|p| *p == id) {
                Some(i) => {
                    next.remove(i);
                }
                None => next.push(id),
            }
            self.pinned = self.eng.set_pinned(&next);
        }
        if let Some(id) = open {
            self.open_source(&id);
        }
    }

    // -----------------------------------------------------------------------
    // The page
    // -----------------------------------------------------------------------

    fn main(&mut self, ui: &mut Ui, main: Rect, enabled: bool) {
        let clip = ui.clip_rect();
        ui.set_clip_rect(main);
        if self.at.search {
            self.search_screen(ui, main, enabled);
        } else if self.has_content() {
            self.content_screen(ui, main, enabled);
        } else {
            self.home_screen(ui, main, enabled);
        }
        ui.set_clip_rect(clip);
    }

    /// The panel of pinned sections at the top of the page. Returns its height, with its bottom gap.
    fn pins_panel(&mut self, ui: &mut Ui, origin: Pos2, view: Rect, enabled: bool, measure_only: bool) -> f32 {
        if self.window.pins.is_empty() {
            return 0.0;
        }
        let th = self.th;
        let header_h = gfx::px(ui, 14.0 + 14.4 + 14.0 + 1.0);
        let row_h = gfx::px(ui, 6.0 + 13.2 + 14.4 + 6.0);
        let height = header_h + 8.0 + self.window.pins.len() as f32 * (row_h + 6.0) + 8.0;
        if measure_only {
            return height;
        }
        let rect = Rect::from_min_size(origin, Vec2::new(260.0, height));
        if !rect.intersects(view) {
            return height;
        }
        ui.painter().rect_filled(rect, 0.0, th.surface);
        ui.painter().vline(rect.left() + 0.5, rect.y_range(), Stroke::new(1.0, th.border));
        ui.painter().hline(rect.x_range(), rect.top() + header_h - 0.5, Stroke::new(1.0, th.border));
        let title = gfx::spaced(ui, "PINNED", theme::sans(12.0, 600), th.text_muted, 0.4);
        gfx::paint_left(ui, rect.left() + 14.0, rect.top() + header_h / 2.0, &title);
        let mut y = rect.top() + header_h + 8.0;
        let mut go: Option<Pin> = None;
        let mut drop: Option<Pin> = None;
        for pin in &self.window.pins {
            let row = Rect::from_min_size(Pos2::new(rect.left() + 8.0, y), Vec2::new(rect.width() - 16.0, row_h));
            let active = pin.act == self.at.act && pin.section == self.at.section;
            if active {
                ui.painter().rect_filled(row, gfx::radius(4.0), theme::blend(th.surface, Color32::from_rgb(39, 158, 136), 0.12));
            }
            let id = Id::unique(("pin", &pin.act, &pin.section));
            let response = gfx::button(ui, id, row.intersect(view), enabled);
            let close = Rect::from_min_size(Pos2::new(row.right() - 8.0 - 18.0, row.center().y - 11.0), Vec2::new(18.0, 22.0));
            let close_response = gfx::button(ui, id.with("x"), close.intersect(view), enabled);
            let label = gfx::line(ui, &format!("{} › {}", pin.act, pin.section), theme::sans(11.0, 600), th.accent);
            gfx::paint_at(ui, Pos2::new(row.left() + 8.0, row.top() + 6.0), &label);
            let title = gfx::elided(ui, &pin.title, theme::sans(12.0, 400), th.text, close.left() - 6.0 - (row.left() + 8.0));
            gfx::paint_at(ui, Pos2::new(row.left() + 8.0, row.top() + 6.0 + gfx::px(ui, 13.2)), &title);
            let x = gfx::line(ui, "×", theme::system(18.0), th.text_muted);
            gfx::paint_centered(ui, close.center(), &x);
            if close_response.clicked() {
                drop = Some(pin.clone());
            } else if response.clicked() {
                go = Some(pin.clone());
            }
            y += row_h + 6.0;
        }
        if let Some(pin) = drop {
            self.window.pins.retain(|p| *p != pin);
            self.save_window();
        }
        if let Some(pin) = go {
            self.open_section(&pin.act, &pin.section, None);
        }
        height
    }

    fn home_screen(&mut self, ui: &mut Ui, main: Rect, enabled: bool) {
        let th = self.th;
        let screen_h = ui.ctx().content_rect().height();
        let pins_h = self.pins_panel(ui, Pos2::ZERO, main, enabled, true);
        let inner_w = main.width() - 80.0;
        let column = inner_w.min(640.0) - 32.0;
        let canvas_w = column.min(520.0);
        let canvas_h = canvas_w * 780.0 / 1440.0;
        let version_h = gfx::px(ui, 17.16 + 4.0);
        let block = 32.0 + 64.0 + 20.0 + 4.0 + canvas_h + 20.0 + 8.0 + 62.0 + 20.0 + version_h + 32.0;
        let wrap = (0.8 * screen_h).max(block);
        let content = 20.0 + pins_h + wrap + 20.0;
        let offset = self.main_scroll.run(ui, Id::unique("main-scroll"), main, content, &th, enabled);
        let top = main.top() + 20.0 - offset;
        self.pins_panel(ui, Pos2::new(main.left() + 40.0, top), main, enabled, false);
        let cx = main.left() + 40.0 + inner_w / 2.0;
        let mut y = top + pins_h + (wrap - block) / 2.0 + 32.0;

        // The lockup: cat and wordmark, 64 high.
        let ppp = ui.ctx().pixels_per_point();
        let px_h = (64.0 * ppp).round() as u32;
        if self.lockup.as_ref().is_none_or(|(_, dark, h)| *dark != th.dark || *h != px_h) {
            let bytes = if th.dark { LOCKUP_DARK } else { LOCKUP_LIGHT };
            if let Ok(img) = image::load_from_memory_with_format(bytes, image::ImageFormat::Png) {
                let px_w = (img.width() as f32 * px_h as f32 / img.height() as f32).round() as u32;
                let small = image::imageops::resize(&img.to_rgba8(), px_w, px_h, image::imageops::FilterType::CatmullRom);
                let image = egui::ColorImage::from_rgba_unmultiplied([px_w as usize, px_h as usize], small.as_raw());
                self.lockup = Some((ui.ctx().load_texture("lockup", image, TextureOptions::LINEAR), th.dark, px_h));
            }
        }
        if let Some((texture, _, _)) = &self.lockup {
            let size = texture.size_vec2() / ppp;
            let rect = Rect::from_min_size(Pos2::new(gfx::px(ui, cx - size.x / 2.0), gfx::px(ui, y)), size);
            ui.painter().image(texture.id(), rect, Rect::from_min_max(Pos2::ZERO, Pos2::new(1.0, 1.0)), Color32::WHITE);
        }
        y += 64.0 + 20.0 + 4.0;

        // The yarn.
        let canvas = Rect::from_min_size(Pos2::new(gfx::px(ui, cx - canvas_w / 2.0), gfx::px(ui, y)), Vec2::new(canvas_w, canvas_h));
        self.yarn_canvas(ui, canvas, enabled && main.contains(canvas.center()));
        y += canvas_h + 20.0 + 8.0;

        // The search box.
        let form = Rect::from_min_size(Pos2::new(gfx::px(ui, cx - column / 2.0), gfx::px(ui, y)), Vec2::new(column, 62.0));
        let query = self.search_box(ui, Id::unique("home-search"), form, 16.0, 8.0, 14.0, 18.0, "Search acts, rulings, cases and treaties", false, enabled);
        if let Some(q) = query {
            self.open_search(&q);
            return;
        }
        y += 62.0 + 20.0;

        // The version, which opens the changelog.
        let version = format!("v{}", app_version());
        let g = gfx::line(ui, &version, theme::mono(13.0, 400), th.text_muted);
        let rect = Rect::from_center_size(Pos2::new(cx, y + version_h / 2.0), g.size() + Vec2::new(8.0, 4.0));
        let response = gfx::button(ui, Id::unique("version"), rect, enabled);
        let color = if response.hovered() { th.yarn } else { th.text_muted };
        let g = gfx::line(ui, &version, theme::mono(13.0, 400), color);
        let at = gfx::paint_centered(ui, rect.center(), &g);
        ui.painter().hline(at.x_range(), g.baseline(ui, at.top()) + 3.0 + 0.5, Stroke::new(1.0, color));
        if response.clicked() {
            self.modal = Modal::Changelog;
            self.modal_scroll.offset = 0.0;
        }
    }

    fn yarn_canvas(&mut self, ui: &mut Ui, rect: Rect, enabled: bool) {
        let ctx = ui.ctx().clone();
        let ppp = ctx.pixels_per_point();
        let size = [(rect.width() * ppp).round().max(1.0) as usize, (rect.height() * ppp).round().max(1.0) as usize];
        let now = Instant::now();
        let dt = now.duration_since(self.yarn_at).as_secs_f64();
        self.yarn_at = now;
        let changed = self.yarn.advance(dt);
        let stale = self.yarn_image.as_ref().is_none_or(|(_, s)| *s != size);
        if changed || stale {
            if let Some(mut pm) = tiny_skia::Pixmap::new(size[0] as u32, size[1] as u32) {
                self.yarn.draw(&mut pm);
                let image = egui::ColorImage::from_rgba_premultiplied(size, pm.data());
                match &mut self.yarn_image {
                    Some((texture, s)) => {
                        texture.set(image, TextureOptions::LINEAR);
                        *s = size;
                    }
                    None => self.yarn_image = Some((ctx.load_texture("yarn", image, TextureOptions::LINEAR), size)),
                }
            }
            ctx.request_repaint();
        } else {
            ctx.request_repaint_after(Duration::from_millis(self.yarn.idle_for().max(16.0) as u64));
        }
        if let Some((texture, _)) = &self.yarn_image {
            ui.painter().image(texture.id(), rect, Rect::from_min_max(Pos2::ZERO, Pos2::new(1.0, 1.0)), Color32::WHITE);
        }
        if enabled {
            if let Some(p) = ui.input(|i| i.pointer.hover_pos()).filter(|p| rect.contains(*p)) {
                let (mx, my) = (((p.x - rect.left()) * 1440.0 / rect.width()) as f64, ((p.y - rect.top()) * 780.0 / rect.height()) as f64);
                if self.yarn.hit(mx, my) {
                    ctx.set_cursor_icon(egui::CursorIcon::PointingHand);
                    if ui.input(|i| i.pointer.primary_clicked()) {
                        self.yarn.click(mx, my);
                        ctx.request_repaint();
                    }
                }
            }
        }
    }

    /// A search box (`.lk-welcome__form`, `.lk-search__form`): icon, input and the Pounce button
    /// in a chunky outlined pill with a hard shadow. Returns the query when it is submitted.
    #[allow(clippy::too_many_arguments)]
    fn search_box(&mut self, ui: &mut Ui, id: Id, form: Rect, pad_left: f32, gap: f32, button_radius: f32, button_pad: f32, hint: &str, search_page: bool, enabled: bool) -> Option<String> {
        let th = self.th;
        let hovered = enabled && ui.rect_contains_pointer(form);
        let focused = ui.ctx().memory(|m| m.has_focus(id));
        // The home box lifts two pixels on hover and focus.
        let lift = if !search_page && (hovered || focused) { Vec2::splat(-2.0) } else { Vec2::ZERO };
        let rect = form.translate(lift);
        ui.painter().rect_filled(rect.translate(Vec2::splat(4.0)), gfx::radius(20.0), th.shadow_pounce);
        gfx::panel(ui, rect, 20.0, if lift != Vec2::ZERO { th.surface_hover } else { th.surface }, 2.0, th.accent);
        if focused && !search_page {
            ui.painter().rect_stroke(rect.expand(3.0), gfx::radius(23.0), Stroke::new(2.0, th.yarn), StrokeKind::Outside);
        }
        self.icons.paint(ui, Icon::Search, Rect::from_min_size(Pos2::new(rect.left() + 2.0 + pad_left, rect.center().y - 10.0), Vec2::splat(20.0)), th.text_muted);

        let label = gfx::line(ui, "Pounce", theme::sans(15.0, 700), th.on_catnip);
        let button_w = gfx::px(ui, label.size().x + button_pad * 2.0);
        let button = Rect::from_min_size(Pos2::new(rect.right() - 2.0 - 6.0 - button_w, rect.top() + 2.0 + 6.0), Vec2::new(button_w, rect.height() - 16.0));
        let response = gfx::button(ui, id.with("go"), button, enabled);
        ui.painter().rect_filled(button, gfx::radius(button_radius), if response.hovered() && !search_page { th.accent_hover } else { th.accent });
        gfx::paint_centered(ui, button.center(), &label);

        let input_left = rect.left() + 2.0 + pad_left + 20.0 + gap;
        let font = theme::sans(17.0, 400);
        let row_h = gfx::line(ui, "x", font.clone(), th.text).size().y;
        let input = Rect::from_min_max(Pos2::new(input_left, gfx::px(ui, rect.center().y - row_h / 2.0)), Pos2::new(button.left() - gap, gfx::px(ui, rect.center().y - row_h / 2.0) + row_h));
        let mut child = ui.new_child(egui::UiBuilder::new().max_rect(input).layout(egui::Layout::left_to_right(Align::Center)));
        let text = if search_page { &mut self.search_query } else { &mut self.home_query };
        let mut edit = egui::TextEdit::singleline(text)
            .id(id)
            .frame(egui::Frame::NONE)
            .margin(egui::Margin::ZERO)
            .desired_width(input.width())
            .font(font.clone())
            .text_color(th.text)
            .hint_text(egui::RichText::new(hint).font(font).color(th.text_muted));
        if !enabled {
            edit = edit.interactive(false);
        }
        let out = edit.show(&mut child);
        if search_page && self.focus_search {
            out.response.request_focus();
            self.focus_search = false;
        }
        let entered = out.response.lost_focus() && ui.input(|i| i.key_pressed(Key::Enter));
        if entered || response.clicked() {
            let text = if search_page { &self.search_query } else { &self.home_query };
            return Some(text.trim().to_string());
        }
        None
    }

    fn toolbar(&mut self, ui: &mut Ui, origin: Pos2, view: Rect, enabled: bool) -> f32 {
        let th = self.th;
        let height = 28.0;
        let mut x = origin.x;
        let mut clicked = None;
        let dictionary = engine::dictionary_section(&self.at.act).filter(|s| *s != self.at.section);
        let buttons: [(Icon, &str, bool); 3] = [(Icon::Home, "Home", true), (Icon::Link, "", true), (Icon::Hash, "Definitions", dictionary.is_some())];
        for (i, (icon, text, shown)) in buttons.into_iter().enumerate() {
            if !shown {
                continue;
            }
            let label = (!text.is_empty()).then(|| gfx::line(ui, text, theme::system(11.0), th.text_muted));
            let w = gfx::px(ui, 1.0 + 8.0 + 14.0 + label.as_ref().map_or(0.0, |g| 4.0 + g.size().x) + 8.0 + 1.0);
            let rect = Rect::from_min_size(Pos2::new(x, origin.y), Vec2::new(w, height));
            if rect.intersects(view) {
                let tip = ["Home", "Copy reference", "Open this Act's dictionary of defined terms"][i];
                let response = gfx::button(ui, Id::unique(("toolbar", i)), rect.intersect(view), enabled).on_hover_text(tip);
                gfx::panel(ui, rect, 6.0, th.surface, 1.0, th.border);
                self.icons.paint(ui, icon, Rect::from_min_size(Pos2::new(rect.left() + 9.0, rect.center().y - 7.0), Vec2::splat(14.0)), th.text_muted);
                if let Some(g) = &label {
                    gfx::paint_left(ui, rect.left() + 9.0 + 14.0 + 4.0, rect.center().y, g);
                }
                if response.clicked() {
                    clicked = Some(i);
                }
            }
            x += w + 8.0;
        }
        match clicked {
            Some(0) => {
                self.go_home();
                self.at.act.clear();
            }
            Some(1) => {
                let reference = if self.at.section.is_empty() { self.at.act.clone() } else { format!("{}/{}", self.at.act, self.at.section) };
                ui.ctx().copy_text(reference);
            }
            Some(2) => {
                if let Some(section) = dictionary {
                    let act = self.at.act.clone();
                    self.open_section(&act, section, None);
                }
            }
            _ => {}
        }
        height
    }

    /// The licence line under a document (`LicenceNotice`). Returns its height.
    fn licence_notice(&mut self, ui: &mut Ui, licence: &Licence, origin: Pos2, width: f32, view: Rect, enabled: bool, paint: bool) -> f32 {
        let th = self.th;
        let width = width.min(reader::COLUMN);
        let font = theme::sans(12.0, 400);
        let badge = gfx::spaced(ui, &licence.badge.to_uppercase(), theme::sans(10.0, 400), th.text_muted, 0.4);
        let badge_w = gfx::px(ui, badge.size().x + 10.0 + 2.0);
        let toggle_text = if self.licence_open { "Hide details" } else { "Details" };
        let toggle = gfx::line(ui, toggle_text, theme::sans(11.0, 400), th.accent);
        let text_w = width - 2.0 - 28.0 - badge_w - 8.0 - 8.0 - toggle.size().x;
        let line = licence.notice_line.as_deref().map(|l| gfx::wrapped(ui, l, font.clone(), th.text_muted, text_w, 18.0));
        // The badge (15 line, 1 padding, 1 border) is the tallest thing on a one-line notice.
        let line_h = line.as_ref().map_or(19.0, |g| g.size().y.max(19.0));
        let mut paras: Vec<gfx::Label> = Vec::new();
        let mut links: Vec<(String, String)> = Vec::new();
        if self.licence_open {
            let inner = width - 2.0 - 28.0;
            let mut para = |text: &str, bold: bool, italic: bool| {
                let mut job = egui::text::LayoutJob::single_section(
                    text.to_owned(),
                    egui::TextFormat { font_id: theme::sans(12.0, if bold { 600 } else { 400 }), color: if bold { th.text } else { th.text_muted }, italics: italic, line_height: Some(18.0), ..Default::default() },
                );
                job.wrap.max_width = inner;
                job.round_output_to_gui = false;
                paras.push(gfx::Label::new(ui, ui.painter().layout_job(job), &theme::sans(12.0, 400), Some(18.0)));
            };
            if let Some(t) = &licence.unofficial_notice {
                para(t, true, false);
            }
            if let Some(t) = &licence.attribution {
                para(t, false, false);
            }
            if let Some(t) = &licence.disclaimer {
                para(t, false, true);
            }
            if let Some(t) = &licence.endorsement_notice {
                para(t, false, false);
            }
            if let Some(date) = &licence.version_date {
                para(&date.as_ref().map(|d| format!("Version current from {d}.")).unwrap_or_else(|| "Version date not printed on the copy held.".into()), false, false);
            }
            links = vec![("Source document".into(), licence.source_url.clone()), (licence.licence_name.clone(), licence.licence_url.clone()), ("Copyright terms".into(), licence.terms_url.clone())];
        }
        let details_h: f32 = if self.licence_open { 1.0 + 2.0 + 7.0 + paras.iter().map(|g| g.size().y + 6.0).sum::<f32>() + 18.0 + 9.0 } else { 0.0 };
        let height = 1.0 + 7.0 + line_h + 7.0 + details_h + 1.0;
        let rect = Rect::from_min_size(Pos2::new(gfx::px(ui, origin.x), gfx::px(ui, origin.y)), Vec2::new(width, gfx::px(ui, height)));
        if !paint || !rect.intersects(view) {
            return height;
        }
        gfx::panel(ui, rect, 6.0, Color32::TRANSPARENT, 1.0, th.border);
        let x = rect.left() + 1.0 + 14.0;
        let mid = rect.top() + 1.0 + 7.0 + 9.5;
        let badge_rect = Rect::from_min_size(Pos2::new(x, rect.top() + 8.0), Vec2::new(badge_w, 19.0));
        gfx::panel(ui, badge_rect, 4.0, Color32::TRANSPARENT, 1.0, th.border);
        gfx::paint_centered(ui, badge_rect.center(), &badge);
        if let Some(g) = &line {
            gfx::paint_at(ui, Pos2::new(x + badge_w + 8.0, rect.top() + 8.0), g);
        }
        let toggle_at = Rect::from_min_size(Pos2::new(rect.right() - 15.0 - toggle.size().x, rect.top() + 8.0 + 13.0 - 10.0), toggle.size());
        let response = gfx::button(ui, Id::unique("licence-toggle"), toggle_at.expand(3.0).intersect(view), enabled);
        // Baseline-aligned with the text: 13 below the top of the line.
        let at = toggle.paint(ui, toggle_at.left(), rect.top() + 8.0 + 13.0 - gfx::css_baseline(ui, &theme::sans(11.0, 400), None).0);
        ui.painter().hline(at.x_range(), toggle.baseline(ui, at.top()) + 1.0 + 0.5, Stroke::new(1.0, th.accent));
        let _ = mid;
        if response.clicked() {
            self.licence_open = !self.licence_open;
        }
        if details_h > 0.0 {
            let mut y = rect.top() + 1.0 + 7.0 + line_h + 7.0;
            ui.painter().hline((rect.left() + 15.0)..=(rect.right() - 15.0), gfx::px(ui, y) + 0.5, Stroke::new(1.0, th.border));
            y += 1.0 + 2.0 + 7.0;
            for g in &paras {
                gfx::paint_at(ui, Pos2::new(x, y), g);
                y += g.size().y + 6.0;
            }
            let mut lx = x;
            for (i, (label, url)) in links.iter().enumerate() {
                if i > 0 {
                    let dot = gfx::line(ui, " · ", font.clone(), th.text_muted);
                    gfx::paint_left(ui, lx, y + 9.0, &dot);
                    lx += dot.size().x;
                }
                let g = gfx::line(ui, label, font.clone(), th.accent);
                let at = gfx::paint_left(ui, lx, y + 9.0, &g);
                ui.painter().hline(at.x_range(), gfx::px(ui, at.bottom() - 2.0) + 0.5, Stroke::new(1.0, th.accent));
                if gfx::button(ui, Id::unique(("licence-link", i)), at.intersect(view), enabled).on_hover_text(url.as_str()).clicked() {
                    ui.ctx().open_url(egui::OpenUrl::new_tab(url));
                }
                lx += g.size().x;
            }
        }
        height
    }

    /// A source's contents page or a section: both under the toolbar.
    fn content_screen(&mut self, ui: &mut Ui, main: Rect, enabled: bool) {
        let th = self.th;
        let pins_h = self.pins_panel(ui, Pos2::ZERO, main, enabled, true);
        let reading = !self.at.section.is_empty();
        let licence = if reading { self.doc.as_ref().and_then(|d| d.licence.clone()) } else { self.tree.as_ref().and_then(|t| t.licence.clone()) };

        // Lay out, to know the height before anything is drawn.
        let width_for = |bar: f32| main.width() - 80.0 - bar;
        let mut bar = if self.main_scroll.offset > 0.0 { gfx::BAR } else { 0.0 };
        let mut content;
        loop {
            let width = width_for(bar);
            let mut h = 12.0 + pins_h + 28.0 + 16.0;
            if reading {
                if let Some(doc) = &self.doc {
                    if self.doc_layout.as_ref().is_none_or(|l| (l.width - width).abs() > 0.5) {
                        self.doc_layout = Some(reader::Layout::new(ui, &doc.doc, width, &th));
                    }
                    h += 36.0 + 16.0 + self.doc_layout.as_ref().map_or(0.0, |l| l.height);
                    if let Some(l) = &licence {
                        h += 24.0 + self.licence_notice(ui, l, Pos2::ZERO, width, main, false, false);
                    }
                    h += 40.0 + 2.0 + 20.0;
                } else {
                    h += 60.0;
                }
            } else {
                h += 22.0;
                if let Some(l) = &licence {
                    h += 24.0 + self.licence_notice(ui, l, Pos2::ZERO, width, main, false, false) + 8.0;
                } else {
                    h += 16.0;
                }
                if let Some(tree) = &self.tree {
                    if self.main_rows.is_none() {
                        self.main_rows = Some(treeview::build(&tree.parts, &self.main_open, &self.at.act));
                    }
                }
                h += 1.0 + 8.0 + self.main_rows.as_ref().map_or(40.0, |r| r.height);
            }
            content = h + 20.0;
            if content > main.height() && bar == 0.0 {
                bar = gfx::BAR;
                continue;
            }
            break;
        }
        let width = width_for(if content > main.height() { gfx::BAR } else { 0.0 });

        // A link to an anchor: scroll to the block that carries it, once the section is laid out.
        if let (Some(anchor), Some(doc), Some(layout)) = (&self.anchor, &self.doc, &self.doc_layout) {
            if doc.act == self.at.act && doc.section == self.at.section {
                if let Some((_, block)) = doc.doc.anchors.iter().find(|(id, _)| id == anchor) {
                    let top = layout.tops.get(*block).copied().unwrap_or(0.0);
                    self.main_scroll.offset = (12.0 + pins_h + 28.0 + 16.0 + 36.0 + 16.0 + top - 16.0).max(0.0);
                }
                self.anchor = None;
            }
        }
        let offset = self.main_scroll.run(ui, Id::unique("main-scroll"), main, content, &th, enabled);
        let x = main.left() + 40.0;
        let mut y = main.top() + 12.0 - offset;
        y += self.pins_panel(ui, Pos2::new(x, y), main, enabled, false);
        y += self.toolbar(ui, Pos2::new(x, y), main, enabled) + 16.0;

        if reading {
            let Some(doc) = &self.doc else {
                if let Some(e) = &self.doc_error {
                    let g = gfx::wrapped(ui, &format!("Error: {e}"), theme::sans(15.0, 400), Color32::from_rgb(0xef, 0x44, 0x44), width, 22.0);
                    gfx::paint_at(ui, Pos2::new(x, y), &g);
                }
                return;
            };
            // Breadcrumb and the Pin button.
            let column = width.min(reader::COLUMN);
            let is_pinned = self.window.pins.iter().any(|p| p.act == doc.act && p.section == doc.section);
            let label = gfx::line(ui, if is_pinned { "Unpin" } else { "Pin" }, theme::sans(13.0, if is_pinned { 700 } else { 600 }), if is_pinned { th.on_catnip } else { th.text });
            let pin = Rect::from_min_size(Pos2::new(gfx::px(ui, x + column - (label.size().x + 28.0)), gfx::px(ui, y)), Vec2::new(gfx::px(ui, label.size().x + 28.0), 36.0));
            let response = gfx::button(ui, Id::unique("pin-section"), pin.intersect(main), enabled);
            let (fill, border) = if is_pinned { (th.accent, th.accent) } else if response.hovered() { (th.surface_hover, th.line_strong) } else { (th.surface, th.border) };
            gfx::panel(ui, pin, 14.0, fill, 2.0, border);
            gfx::paint_centered(ui, pin.center(), &label);
            let mut cx = x;
            for (i, crumb) in doc.crumbs.iter().enumerate() {
                if i > 0 {
                    let sep = gfx::line(ui, "/", theme::sans(12.0, 700), th.line_strong);
                    gfx::paint_left(ui, cx + 6.0, y + 10.0 + 8.0, &sep);
                    cx += 6.0 + sep.size().x + 0.72 + 6.0;
                }
                let g = gfx::spaced(ui, &crumb.to_uppercase(), theme::sans(12.0, 700), th.text_muted, 0.72);
                gfx::paint_left(ui, cx, y + 10.0 + 8.0, &g);
                // letter-spacing follows the last letter too.
                cx += g.size().x + 0.72;
            }
            let pin_now = response.clicked().then(|| Pin { act: doc.act.clone(), section: doc.section.clone(), title: doc.title.clone() });
            y += 36.0 + 16.0;

            let mut follow = None;
            let body_h = self.doc_layout.as_ref().map_or(0.0, |l| l.height);
            if let Some(layout) = &self.doc_layout {
                let hit = reader::paint(ui, layout, Pos2::new(x, y), &th, enabled);
                follow = hit.clicked.and_then(|i| layout.hrefs.get(i).cloned());
            }
            y += body_h;
            if let Some(l) = &licence {
                y += 24.0;
                y += self.licence_notice(ui, l, Pos2::new(x, y), width, main, enabled, true);
            }
            y += 40.0;
            ui.painter().rect_filled(Rect::from_min_size(Pos2::new(x, gfx::px(ui, y)), Vec2::new(column, 2.0)), 0.0, th.border);
            if let Some(pin) = pin_now {
                match self.window.pins.iter().position(|p| p.act == pin.act && p.section == pin.section) {
                    Some(i) => {
                        self.window.pins.remove(i);
                    }
                    None => self.window.pins.push(pin),
                }
                self.save_window();
            }
            if let Some(href) = follow {
                let ctx = ui.ctx().clone();
                self.follow_link(&ctx, &href);
            }
        } else {
            // The contents page: the source's name, its licence line, then every part.
            let name = gfx::line(ui, &engine::short_act_name(&self.at.act), theme::sans(14.0, 600), th.heading);
            gfx::paint_left(ui, x, y + 11.0, &name);
            if let Some(section) = engine::dictionary_section(&self.at.act) {
                let label = gfx::line(ui, "Definitions", theme::system(12.0), th.accent);
                let rect = Rect::from_min_size(Pos2::new(gfx::px(ui, x + name.size().x + 8.0), gfx::px(ui, y)), Vec2::new(gfx::px(ui, label.size().x + 22.0), 22.0));
                let response = gfx::button(ui, Id::unique("act-definitions"), rect.intersect(main), enabled);
                gfx::panel(ui, rect, 6.0, Color32::TRANSPARENT, 1.0, th.border);
                gfx::paint_centered(ui, rect.center(), &label);
                if response.clicked() {
                    let act = self.at.act.clone();
                    self.open_section(&act, section, None);
                    return;
                }
            }
            y += 22.0;
            if let Some(l) = &licence {
                y += 24.0;
                y += self.licence_notice(ui, l, Pos2::new(x, y), width, main, enabled, true) + 8.0;
            } else {
                y += 16.0;
            }
            ui.painter().hline(x..=(x + width), gfx::px(ui, y) + 0.5, Stroke::new(1.0, th.border));
            y += 1.0 + 8.0;
            match (&self.main_rows, &self.tree_error) {
                (Some(rows), _) => {
                    let action = treeview::paint(ui, Id::unique("main-tree"), rows, Pos2::new(x, y), width, main, &self.at.section, &th, th.bg, enabled);
                    self.tree_action(action, false);
                }
                (None, error) => {
                    let g = gfx::line(ui, error.as_deref().unwrap_or("Loading..."), theme::sans(13.0, 400), th.text_muted);
                    gfx::paint_at(ui, Pos2::new(x, y + 8.0), &g);
                }
            }
        }
    }

    // -----------------------------------------------------------------------
    // Search
    // -----------------------------------------------------------------------

    fn search_screen(&mut self, ui: &mut Ui, main: Rect, enabled: bool) {
        const PAGE: usize = 25;
        let th = self.th;
        let pins_h = self.pins_panel(ui, Pos2::ZERO, main, enabled, true);
        let full_text = self.eng.has_index();

        // Measure the result cards for the height of the page.
        struct Card {
            title: gfx::Label,
            snippet: Option<gfx::Label>,
            h: f32,
            index: usize,
        }
        let build = |ui: &Ui, this: &LawKitty, width: f32| -> (Vec<Card>, usize, usize) {
            let Some(found) = &this.search else { return (Vec::new(), 0, 0) };
            let pages = found.hits.len().div_ceil(PAGE).max(1);
            let page = this.search_page.min(pages - 1);
            let mut cards = Vec::new();
            for (index, hit) in found.hits.iter().enumerate().skip(page * PAGE).take(PAGE) {
                let heading = if !hit.title.is_empty() && hit.title != hit.section { format!(": {}", hit.title) } else { String::new() };
                let title = format!("s {} {}{}", hit.section, engine::act_title_name(&hit.act), heading);
                let badge_w = 70.0;
                let title = gfx::wrapped(ui, &title, theme::sans(16.0, 600), th.text, width - 44.0 - badge_w - 12.0, 22.0);
                let snippet = (!hit.snippet.is_empty()).then(|| snippet_galley(ui, &hit.snippet, width - 44.0, &th));
                let h = 2.0 + 14.0 + title.size().y.max(20.0) + snippet.as_ref().map_or(0.0, |g| 6.0 + g.size().y) + 14.0 + 2.0;
                cards.push(Card { title, snippet, h, index });
            }
            (cards, page, pages)
        };
        let measure = |cards: &[Card], this: &LawKitty, pages: usize| -> f32 {
            let mut h = 20.0 + pins_h + 4.0 + 62.0 + 4.0 + 24.0;
            if this.searching {
                h += 60.0;
            } else if let Some(found) = &this.search {
                if found.hits.is_empty() {
                    h += 48.0 + 30.0 + 8.0 + 20.0 + 48.0;
                } else {
                    h += 40.0 + if full_text { 0.0 } else { 24.0 } + 24.0 + cards.iter().map(|c| c.h + 10.0).sum::<f32>();
                    if pages > 1 {
                        h += 24.0 + 52.0;
                    }
                }
            }
            h + 20.0
        };
        let mut width = main.width() - 80.0;
        let (mut cards, mut page, mut pages) = build(ui, self, width);
        let mut content = measure(&cards, self, pages);
        if content > main.height() {
            width -= gfx::BAR;
            (cards, page, pages) = build(ui, self, width);
            content = measure(&cards, self, pages);
        }
        let offset = self.main_scroll.run(ui, Id::unique("main-scroll"), main, content, &th, enabled);
        let x = main.left() + 40.0;
        let mut y = main.top() + 20.0 - offset;
        y += self.pins_panel(ui, Pos2::new(x, y), main, enabled, false) + 4.0;

        let form = Rect::from_min_size(Pos2::new(x, gfx::px(ui, y)), Vec2::new(width - 4.0, 62.0));
        let hint = if full_text { "Search legislation…" } else { "Search section numbers and titles…" };
        if let Some(q) = self.search_box(ui, Id::unique("search-page"), form, 20.0, 12.0, 255.0, 22.0, hint, true, enabled) {
            self.search_query = q;
            self.run_search();
        }
        y += 62.0 + 4.0 + 24.0;

        if self.searching {
            let g = gfx::line(ui, "Sniffing through the ITAA…", theme::sans(15.0, 400), th.text_muted);
            gfx::paint_at(ui, Pos2::new(x + 4.0, y + 16.0), &g);
            return;
        }
        let Some(found) = &self.search else { return };
        if found.hits.is_empty() {
            let cx = x + width / 2.0;
            let title = gfx::line(ui, "Nothing sniffed out.", theme::display(26.0), th.text);
            gfx::paint_centered(ui, Pos2::new(cx, y + 48.0 + 15.0), &title);
            let hint = gfx::line(ui, "Try different words.", theme::sans(14.0, 400), th.text_muted);
            gfx::paint_centered(ui, Pos2::new(cx, y + 48.0 + 30.0 + 8.0 + 10.0), &hint);
            return;
        }
        // "42 results for “words”".
        let count = if found.total > found.hits.len() { format!("{}+", found.hits.len()) } else { found.hits.len().to_string() };
        let heading = gfx::line(ui, &format!("{count} {}", if found.hits.len() == 1 { "result" } else { "results" }), theme::display(36.0), th.text);
        let at = gfx::paint_left(ui, x, y + 20.0, &heading);
        let query = gfx::elided(ui, &format!("for \u{201c}{}\u{201d}", found.query), theme::sans(15.0, 400), th.text_muted, width - at.width() - 180.0);
        let base = at.bottom() - 9.0;
        gfx::paint_at(ui, Pos2::new(at.right() + 12.0, base - query.size().y + 3.0), &query);
        let showing = format!("Showing {}–{}{}", page * PAGE + 1, page * PAGE + cards.len(), if pages > 1 { format!(" · page {} of {}", page + 1, pages) } else { String::new() });
        let showing = gfx::line(ui, &showing, theme::sans(12.0, 400), th.text_muted);
        gfx::paint_at(ui, Pos2::new(x + width - showing.size().x, base - showing.size().y + 3.0), &showing);
        y += 40.0;
        if !found.full_text {
            let note = gfx::line(ui, "No search_index.db in the data folder: matching section numbers and titles only.", theme::sans(13.0, 400), th.text_muted);
            gfx::paint_at(ui, Pos2::new(x, y + 4.0), &note);
            y += 24.0;
        }
        y += 24.0;

        let mut go: Option<(String, String)> = None;
        for card in &cards {
            let rect = Rect::from_min_size(Pos2::new(x, gfx::px(ui, y)), Vec2::new(width - 4.0, gfx::px(ui, card.h)));
            if rect.intersects(main) {
                let response = gfx::button(ui, Id::unique(("hit", card.index)), rect.intersect(main), enabled);
                let lift = if response.hovered() { Vec2::splat(-2.0) } else { Vec2::ZERO };
                let r = rect.translate(lift);
                if response.hovered() {
                    ui.painter().rect_filled(r.translate(Vec2::splat(4.0)), gfx::radius(18.0), th.shadow_pounce_yarn);
                }
                gfx::panel(ui, r, 18.0, th.surface, 2.0, if response.hovered() { th.yarn } else { th.border });
                let badge = gfx::spaced(ui, "SECTION", theme::sans(11.0, 700), th.accent, 0.55);
                let badge_rect = Rect::from_min_size(Pos2::new(r.left() + 22.0, r.top() + 16.0 + (card.title.size().y.max(20.0) - 20.0) / 2.0), Vec2::new(gfx::px(ui, badge.size().x + 16.0), 20.0));
                ui.painter().rect_filled(badge_rect, gfx::radius(8.0), th.catnip_soft);
                gfx::paint_centered(ui, badge_rect.center(), &badge);
                gfx::paint_at(ui, Pos2::new(badge_rect.right() + 12.0, r.top() + 16.0), &card.title);
                if let Some(snippet) = &card.snippet {
                    gfx::paint_at(ui, Pos2::new(r.left() + 22.0, r.top() + 16.0 + card.title.size().y.max(20.0) + 6.0), snippet);
                }
                if response.clicked() {
                    let hit = &found.hits[card.index];
                    go = Some((hit.act.clone(), hit.section.clone()));
                }
            }
            y += card.h + 10.0;
        }
        // Pages.
        let mut turn: Option<usize> = None;
        if pages > 1 {
            y += 14.0 + 8.0;
            let start = page.saturating_sub(3).min(pages.saturating_sub(7));
            let mut labels: Vec<(String, Option<usize>, bool)> = vec![("← Previous".into(), page.checked_sub(1), false)];
            labels.extend((start..(start + 7).min(pages)).map(|p| ((p + 1).to_string(), Some(p), p == page)));
            labels.push(("Next →".into(), (page + 1 < pages).then_some(page + 1), false));
            let sized: Vec<_> = labels
                .into_iter()
                .map(|(text, target, current)| {
                    let color = if current { th.accent } else if target.is_none() { th.text_muted } else { th.text };
                    let g = gfx::line(ui, &text, theme::sans(13.0, 600), color);
                    let w = gfx::px(ui, (g.size().x + 28.0).max(36.0));
                    (g, w, target, current)
                })
                .collect();
            let total: f32 = sized.iter().map(|s| s.1).sum::<f32>() + 8.0 * (sized.len() as f32 - 1.0);
            let mut bx = gfx::px(ui, x + (width - total) / 2.0);
            for (i, (g, w, target, current)) in sized.into_iter().enumerate() {
                let rect = Rect::from_min_size(Pos2::new(bx, gfx::px(ui, y)), Vec2::new(w, 36.0));
                let live = target.is_some() && !current;
                let response = gfx::button(ui, Id::unique(("page", i)), rect.intersect(main), enabled && live);
                let border = if current { th.accent } else if response.hovered() && live { th.line_strong } else { th.border };
                gfx::panel(ui, rect, 18.0, if target.is_none() { Color32::TRANSPARENT } else { th.surface }, 2.0, border);
                gfx::paint_centered(ui, rect.center(), &g);
                if response.clicked() {
                    turn = target;
                }
                bx += w + 8.0;
            }
        }
        if let Some(p) = turn {
            self.search_page = p;
            self.main_scroll.offset = 0.0;
        }
        if let Some((act, section)) = go {
            self.open_section(&act, &section, None);
        }
    }

    // -----------------------------------------------------------------------
    // Windows over the page
    // -----------------------------------------------------------------------

    fn modals(&mut self, ctx: &egui::Context, screen: Rect) {
        if self.modal == Modal::None {
            return;
        }
        let th = self.th;
        let mut close = false;
        let mut theme_pick: Option<&'static str> = None;
        egui::Area::new(Id::unique("modal")).order(egui::Order::Foreground).fixed_pos(screen.min).show(ctx, |ui| {
            ui.set_clip_rect(screen);
            let backdrop = ui.interact(screen, Id::unique("modal-backdrop"), Sense::click());
            ui.painter().rect_filled(screen, 0.0, Color32::from_black_alpha(153));
            let width = (screen.width() * 0.9).min(520.0);
            match self.modal {
                Modal::Settings => {
                    let data = self.opts.data.display().to_string();
                    let path = gfx::wrapped(ui, &data, theme::mono(12.0, 400), th.text, width - 48.0, 18.0);
                    let height = 24.0 + 22.0 + 20.0 + 16.0 + 6.0 + 40.0 + 20.0 + 16.0 + 6.0 + path.size().y + 16.0 + 36.0 + 24.0;
                    let rect = Rect::from_center_size(screen.center(), Vec2::new(width, height));
                    let panel = ui.interact(rect, Id::unique("modal-panel"), Sense::click());
                    ui.painter().rect_filled(rect, gfx::radius(12.0), th.surface);
                    let x = rect.left() + 24.0;
                    let mut y = rect.top() + 24.0;
                    let title = gfx::line(ui, "Settings", theme::sans(15.0, 600), th.heading);
                    gfx::paint_at(ui, Pos2::new(x, y), &title);
                    y += 22.0 + 20.0;
                    let label = gfx::spaced(ui, "THEME", theme::sans(12.0, 700), th.text_muted, 0.72);
                    gfx::paint_at(ui, Pos2::new(x, y), &label);
                    y += 16.0 + 6.0;
                    let mut bx = x;
                    for (name, text) in [("dark", "Dark"), ("light", "Light")] {
                        let on = self.theme_name == name;
                        let g = gfx::line(ui, text, theme::sans(13.0, if on { 700 } else { 600 }), if on { th.on_catnip } else { th.text });
                        let button = Rect::from_min_size(Pos2::new(bx, y), Vec2::new(gfx::px(ui, g.size().x + 36.0), 36.0));
                        let response = gfx::button(ui, Id::unique(("theme", name)), button, true);
                        gfx::panel(ui, button, 14.0, if on { th.accent } else { th.bg }, 2.0, if on { th.accent } else { th.border });
                        gfx::paint_centered(ui, button.center(), &g);
                        if response.clicked() && !on {
                            theme_pick = Some(name);
                        }
                        bx += button.width() + 8.0;
                    }
                    y += 40.0 + 20.0;
                    let label = gfx::spaced(ui, "DATA FOLDER", theme::sans(12.0, 700), th.text_muted, 0.72);
                    gfx::paint_at(ui, Pos2::new(x, y), &label);
                    y += 16.0 + 6.0;
                    gfx::paint_at(ui, Pos2::new(x, y), &path);
                    y += path.size().y + 16.0;
                    let note = format!("{} sources · {} · lawkitty v{}", self.eng.acts.len(), if self.eng.has_index() { "full-text search" } else { "no search index: section numbers and titles" }, app_version());
                    let note = gfx::elided(ui, &note, theme::sans(12.0, 400), th.text_muted, width - 48.0 - 90.0);
                    gfx::paint_left(ui, x, y + 18.0, &note);
                    let g = gfx::line(ui, "Close", theme::sans(13.0, 600), th.text);
                    let button = Rect::from_min_size(Pos2::new(rect.right() - 24.0 - (g.size().x + 28.0), y), Vec2::new(gfx::px(ui, g.size().x + 28.0), 36.0));
                    let response = gfx::button(ui, Id::unique("modal-close"), button, true);
                    gfx::panel(ui, button, 14.0, if response.hovered() { th.surface_hover } else { th.surface }, 2.0, th.border);
                    gfx::paint_centered(ui, button.center(), &g);
                    close = response.clicked() || (backdrop.clicked() && !panel.hovered());
                }
                Modal::Changelog => {
                    let info: serde_json::Value = serde_json::from_str(lawkitty_local::INFO_JSON).unwrap_or_default();
                    let entries = info["changelog"].as_array().cloned().unwrap_or_default();
                    let inner = width - 48.0 - gfx::BAR;
                    // Lay the entries out, then show what fits in 60% of the window.
                    let mut blocks: Vec<(f32, gfx::Label)> = Vec::new();
                    let mut total = 0.0;
                    for (i, e) in entries.iter().enumerate() {
                        let head = gfx::line(ui, &format!("v{} — {}", e["version"].as_str().unwrap_or(""), e["date"].as_str().unwrap_or("")), theme::sans(13.0, 600), th.accent);
                        blocks.push((0.0, head));
                        let title = gfx::wrapped(ui, e["title"].as_str().unwrap_or(""), theme::sans(11.0, 400), th.text_muted, inner, 14.0);
                        blocks.push((0.0, title));
                        for c in e["changes"].as_array().into_iter().flatten() {
                            let g = gfx::wrapped(ui, &format!("•  {}", c.as_str().unwrap_or("")), theme::sans(11.0, 400), th.text, inner - 4.0, 17.6);
                            blocks.push((4.0, g));
                        }
                        blocks.push((if i + 1 < entries.len() { -1.0 } else { -2.0 }, gfx::line(ui, "", theme::sans(1.0, 400), th.text)));
                    }
                    for (kind, g) in &blocks {
                        total += if *kind < 0.0 { 32.0 } else { g.size().y + 2.0 };
                    }
                    let view_h = (screen.height() * 0.6).min(total);
                    let rect = Rect::from_center_size(screen.center(), Vec2::new(width, 24.0 + 22.0 + 16.0 + view_h + 24.0));
                    let panel = ui.interact(rect, Id::unique("modal-panel"), Sense::click());
                    ui.painter().rect_filled(rect, gfx::radius(12.0), th.surface);
                    let title = gfx::line(ui, "Changelog", theme::sans(15.0, 600), th.heading);
                    gfx::paint_at(ui, Pos2::new(rect.left() + 24.0, rect.top() + 24.0), &title);
                    let view = Rect::from_min_size(Pos2::new(rect.left() + 24.0, rect.top() + 24.0 + 22.0 + 16.0), Vec2::new(width - 48.0, view_h));
                    let offset = self.modal_scroll.run(ui, Id::unique("modal-scroll"), view, total, &th, true);
                    ui.set_clip_rect(view);
                    let mut y = view.top() - offset;
                    for (kind, g) in &blocks {
                        if *kind < 0.0 {
                            if *kind > -1.5 {
                                ui.painter().hline(view.left()..=(view.right() - gfx::BAR), gfx::px(ui, y + 16.0) + 0.5, Stroke::new(1.0, th.border));
                            }
                            y += 32.0;
                        } else {
                            gfx::paint_at(ui, Pos2::new(view.left() + kind, y), g);
                            y += g.size().y + 2.0;
                        }
                    }
                    ui.set_clip_rect(screen);
                    close = backdrop.clicked() && !panel.hovered();
                }
                Modal::None => {}
            }
        });
        if let Some(name) = theme_pick {
            self.theme_name = name.to_string();
            self.eng.set_theme(name);
            self.apply_theme(ctx);
        }
        if close {
            self.modal = Modal::None;
        }
    }
}

/// A search snippet: `<mark>` runs highlighted as the web app highlights them.
fn snippet_galley(ui: &Ui, snippet: &str, width: f32, th: &Theme) -> gfx::Label {
    let mut job = egui::text::LayoutJob::default();
    job.wrap.max_width = width;
    job.round_output_to_gui = false;
    let plain = egui::TextFormat { font_id: theme::sans(14.0, 400), color: th.text_muted, line_height: Some(22.0), ..Default::default() };
    let marked = egui::TextFormat { font_id: theme::sans(14.0, 600), color: th.accent, background: th.catnip_soft, line_height: Some(22.0), ..Default::default() };
    let clean = snippet.replace(['\n', '\r'], " ");
    let mut rest = clean.as_str();
    while let Some(open) = rest.find("<mark>") {
        job.append(&rest[..open], 0.0, plain.clone());
        rest = &rest[open + 6..];
        let end = rest.find("</mark>").unwrap_or(rest.len());
        job.append(&rest[..end], 0.0, marked.clone());
        rest = rest.get(end + 7..).unwrap_or("");
    }
    job.append(rest, 0.0, plain);
    gfx::Label::new(ui, ui.painter().layout_job(job), &theme::sans(14.0, 400), Some(22.0))
}

impl eframe::App for LawKitty {
    fn ui(&mut self, ui: &mut Ui, _frame: &mut eframe::Frame) {
        self.frame(ui);
    }

    fn clear_color(&self, _visuals: &egui::Visuals) -> [f32; 4] {
        self.th.bg.to_normalized_gamma_f32()
    }
}
