//! The data side: the `lawkitty-local` engine behind a worker thread.
//!
//! Only the source catalogue is read at startup. A tree, a part of a tree, a section or a search
//! is asked for when the screen needs it; the answer comes back on a channel and the window is
//! woken to show it. Nothing here opens a socket.

use crate::md;
use eframe::egui;
use lawkitty_local::{catalog::Catalog, pyjson::V, search::Search, section::Sections, state::LocalState, tree::Trees};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::Arc;
use std::time::Instant;

pub struct Act {
    pub id: String,
    pub name: String,
    /// Set for collections: the sidebar group named in `collection.yaml`.
    pub group: Option<String>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Part,
    Division,
    Subdivision,
    Section,
    Signpost,
    Other,
}

pub struct Node {
    pub id: String,
    pub title: String,
    pub kind: Kind,
    /// A shallow tree's shell: its children arrive when it is opened.
    pub lazy: bool,
    pub children: Vec<Node>,
}

#[derive(Clone, Default)]
pub struct Licence {
    pub badge: String,
    pub notice_line: Option<String>,
    pub attribution: Option<String>,
    pub disclaimer: Option<String>,
    pub unofficial_notice: Option<String>,
    pub endorsement_notice: Option<String>,
    pub licence_name: String,
    pub licence_url: String,
    pub source_url: String,
    pub terms_url: String,
    /// Absent for sources that carry no version line; `Some(None)` when the date is not printed.
    pub version_date: Option<Option<String>>,
}

pub struct TreeDoc {
    pub shallow: bool,
    pub parts: Vec<Node>,
    pub licence: Option<Licence>,
}

pub struct SectionDoc {
    pub act: String,
    pub section: String,
    pub title: String,
    pub crumbs: Vec<String>,
    pub doc: md::Doc,
    pub licence: Option<Licence>,
}

#[derive(Clone)]
pub struct Hit {
    pub act: String,
    pub section: String,
    pub title: String,
    pub snippet: String,
}

pub struct SearchDoc {
    pub query: String,
    pub hits: Vec<Hit>,
    pub total: usize,
    /// False when there is no `search_index.db`: section numbers and titles only.
    pub full_text: bool,
}

pub enum Request {
    Tree { act: String, open: Option<String> },
    Part { act: String, id: Option<String>, section: Option<String> },
    Section { act: String, section: String },
    Search { query: String, first_act: String },
}

pub enum Answer {
    Tree { act: String, result: Result<TreeDoc, String>, ms: f64 },
    Part { act: String, key: String, result: Option<Node> },
    Section { act: String, section: String, result: Result<SectionDoc, String>, ms: f64 },
    Search { doc: SearchDoc },
}

struct Core {
    catalog: Catalog,
    trees: Trees,
    sections: Sections,
    search: Search,
    /// Source ids in sidebar order: the order title matches are listed in.
    order: Vec<String>,
}

pub struct Engine {
    core: Arc<Core>,
    pub local: LocalState,
    requests: Sender<(Request, Instant)>,
    pub answers: Receiver<Answer>,
    pub acts: Vec<Act>,
}

fn text(v: &Value, key: &str) -> String {
    match v.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

fn opt_text(v: &Value, key: &str) -> Option<String> {
    v.get(key).and_then(Value::as_str).filter(|s| !s.is_empty()).map(String::from)
}

fn licence(v: Option<&Value>) -> Option<Licence> {
    let v = v.filter(|v| v.is_object())?;
    let badge = opt_text(v, "notice_badge").unwrap_or_else(|| text(v, "jurisdiction"));
    Some(Licence {
        badge,
        notice_line: opt_text(v, "notice_line"),
        attribution: opt_text(v, "attribution"),
        disclaimer: opt_text(v, "disclaimer"),
        unofficial_notice: opt_text(v, "unofficial_notice"),
        endorsement_notice: opt_text(v, "endorsement_notice"),
        licence_name: v.get("licence").map(|l| text(l, "name")).unwrap_or_default(),
        licence_url: v.get("licence").map(|l| text(l, "url")).unwrap_or_default(),
        source_url: text(v, "source_url"),
        terms_url: text(v, "terms_url"),
        version_date: v.get("version_date").map(|d| d.as_str().filter(|s| !s.is_empty()).map(String::from)),
    })
}

/// One tree node, classified by the keys it carries, exactly as `TreeNode.tsx` does.
fn node(v: &Value) -> Node {
    let has = |k: &str| v.get(k).is_some();
    let list = |k: &str| v.get(k).and_then(Value::as_array).map(|a| a.iter().map(node).collect::<Vec<_>>()).unwrap_or_default();
    let signpost = v.get("is_signpost").is_some_and(|s| s.as_bool().unwrap_or(false));
    let kind = if signpost {
        Kind::Signpost
    } else if has("path") {
        Kind::Section
    } else if has("divisions") {
        Kind::Part
    } else if has("subdivisions") && has("sections") {
        Kind::Division
    } else if has("sections") {
        Kind::Subdivision
    } else {
        Kind::Other
    };
    let mut children = Vec::new();
    match kind {
        Kind::Part => {
            children = list("divisions");
            children.extend(list("sections"));
        }
        Kind::Division => {
            children = list("sections");
            children.extend(list("subdivisions"));
        }
        Kind::Subdivision => children = list("sections"),
        _ => {}
    }
    let lazy = kind != Kind::Signpost && v.get("lazy").is_some_and(|l| l.as_bool().unwrap_or(false));
    Node { id: text(v, "id"), title: text(v, "title"), kind, lazy, children }
}

impl Core {
    fn tree(&self, act: &str, open: Option<&str>) -> Result<TreeDoc, String> {
        let tree = self.trees.get(act).map_err(|(_, m)| m)?;
        let body = tree.render(self.catalog.licence(act), true, open);
        let v: Value = serde_json::from_slice(&body).map_err(|e| e.to_string())?;
        Ok(TreeDoc {
            shallow: v.get("shallow").and_then(Value::as_bool).unwrap_or(false),
            parts: v.get("parts").and_then(Value::as_array).map(|a| a.iter().map(node).collect()).unwrap_or_default(),
            licence: licence(v.get("licence")),
        })
    }

    fn part(&self, act: &str, id: Option<&str>, section: Option<&str>) -> Option<Node> {
        let tree = self.trees.get(act).ok()?;
        let body = tree.part(id, section)?;
        serde_json::from_slice::<Value>(&body).ok().map(|v| node(&v))
    }

    fn section(&self, act: &str, section: &str) -> Result<SectionDoc, String> {
        let rendered = self.sections.render(&self.trees, act, section).map_err(|(_, m)| m)?;
        let fm = Value::Object(rendered.frontmatter.clone());
        let mut crumbs = Vec::new();
        let mut crumb = |label: &str, key: &str| {
            let v = text(&fm, key);
            if !v.is_empty() {
                crumbs.push(if label.is_empty() { v } else { format!("{label} {v}") });
            }
        };
        crumb("", "act");
        crumb("Part", "part");
        crumb("Division", "division");
        crumb("Subdivision", "subdivision");
        let title = [text(&fm, "title"), text(&fm, "section_title")].into_iter().find(|t| !t.is_empty()).unwrap_or_else(|| section.to_string());
        Ok(SectionDoc {
            act: act.to_string(),
            section: section.to_string(),
            title,
            crumbs,
            doc: md::parse(&rendered.body, act != "rulings" && act != "private-rulings"),
            licence: licence(self.catalog.licence(act)),
        })
    }

    fn search(&self, query: &str, first_act: &str) -> SearchDoc {
        let query = query.trim();
        let mut hits = Vec::new();
        let full_text = self.search.path().is_some();
        let mut total = 0;
        if full_text {
            let (rows, count) = self.search.sections(query, &[], 100, "AND");
            total = count.max(0) as usize;
            hits.extend(rows.iter().map(|r| Hit { act: text(r, "act"), section: text(r, "section"), title: text(r, "title"), snippet: text(r, "snippet") }));
        }
        // No index, or nothing in it: section numbers and titles from the trees, the open source first.
        if hits.is_empty() && !query.is_empty() {
            let needle = query.to_lowercase();
            let bare = needle.strip_prefix("s ").or_else(|| needle.strip_prefix("section ")).unwrap_or(&needle).trim().to_string();
            let mut ids: Vec<&str> = self.order.iter().map(String::as_str).collect();
            ids.sort_by_key(|id| *id != first_act);
            let mut ranked: Vec<(u8, usize, Hit)> = Vec::new();
            for (order, act) in ids.into_iter().enumerate() {
                let Ok(tree) = self.trees.get(act) else { continue };
                for s in tree.sections() {
                    let id = s.get("id").and_then(V::as_str).unwrap_or("");
                    let title = s.get("title").and_then(V::as_str).unwrap_or("");
                    let (id_l, title_l) = (id.to_lowercase(), title.to_lowercase());
                    let rank = if id_l == bare {
                        0
                    } else if id_l.starts_with(&bare) {
                        1
                    } else if title_l.contains(&needle) {
                        2
                    } else if id_l.contains(&bare) {
                        3
                    } else {
                        continue;
                    };
                    ranked.push((rank, order, Hit { act: act.to_string(), section: id.to_string(), title: title.to_string(), snippet: String::new() }));
                }
            }
            ranked.sort_by_key(|(rank, order, _)| (*rank, *order));
            total = ranked.len();
            hits = ranked.into_iter().take(500).map(|(_, _, h)| h).collect();
        }
        SearchDoc { query: query.to_string(), total: total.max(hits.len()), hits, full_text }
    }
}

impl Engine {
    pub fn start(data: &Path, state: PathBuf, wake: egui::Context) -> Engine {
        let catalog = Catalog::build(data);
        let acts = serde_json::from_slice::<Value>(&catalog.acts_json(None))
            .ok()
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default()
            .iter()
            .map(|a| Act { id: text(a, "id"), name: text(a, "name"), group: opt_text(a, "group") })
            .collect::<Vec<_>>();
        let order = source_groups(&acts).into_iter().flat_map(|g| g.items.into_iter().map(|i| i.id)).collect();
        let core = Arc::new(Core {
            order,
            catalog,
            trees: Trees::new(data.to_path_buf()),
            sections: Sections::new(data.to_path_buf()),
            search: Search::locate(data, None),
        });
        let (requests, inbox) = channel::<(Request, Instant)>();
        let (outbox, answers) = channel();
        let worker = core.clone();
        std::thread::Builder::new()
            .name("lawkitty-data".into())
            .spawn(move || {
                for (request, asked) in inbox {
                    let ms = |asked: Instant| asked.elapsed().as_secs_f64() * 1000.0;
                    let answer = match request {
                        Request::Tree { act, open } => {
                            let result = worker.tree(&act, open.as_deref());
                            Answer::Tree { act, result, ms: ms(asked) }
                        }
                        Request::Part { act, id, section } => {
                            let result = worker.part(&act, id.as_deref(), section.as_deref());
                            let key = id.unwrap_or_else(|| format!("section:{}", section.unwrap_or_default()));
                            Answer::Part { act, key, result }
                        }
                        Request::Section { act, section } => {
                            let result = worker.section(&act, &section);
                            Answer::Section { act, section, result, ms: ms(asked) }
                        }
                        Request::Search { query, first_act } => Answer::Search { doc: worker.search(&query, &first_act) },
                    };
                    if outbox.send(answer).is_err() {
                        break;
                    }
                    wake.request_repaint();
                }
            })
            .expect("data thread");
        Engine { core, local: LocalState::new(state), requests, answers, acts }
    }

    pub fn ask(&self, request: Request) {
        let _ = self.requests.send((request, Instant::now()));
    }

    pub fn has_index(&self) -> bool {
        self.core.search.path().is_some()
    }

    pub fn has_act(&self, act: &str) -> bool {
        self.core.catalog.has(act)
    }

    pub fn pinned(&self) -> Vec<String> {
        self.local.pinned()["sources"].as_array().map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect()).unwrap_or_default()
    }

    pub fn set_pinned(&self, ids: &[String]) -> Vec<String> {
        let values: Vec<Value> = ids.iter().map(|s| json!(s)).collect();
        self.local.set_pinned(&values)["sources"].as_array().map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect()).unwrap_or_default()
    }

    pub fn theme(&self) -> String {
        self.local.prefs()["theme"].as_str().unwrap_or("dark").to_string()
    }

    pub fn set_theme(&self, theme: &str) {
        self.local.update_prefs(&json!({ "theme": theme }));
    }
}

// ---------------------------------------------------------------------------
// Names and sidebar groups: ports of frontend/src/utils/display.ts and sourceGroups.ts
// ---------------------------------------------------------------------------

const STATE_TAX_ACTS: [(&str, &str, &str); 16] = [
    ("qld-duties-2001", "Duties Act 2001", "Qld"),
    ("qld-landtax-2010", "Land Tax Act 2010", "Qld"),
    ("qld-payrolltax-1971", "Payroll Tax Act 1971", "Qld"),
    ("tas-duties-2001", "Duties Act 2001", "Tas"),
    ("tas-landtax-2000", "Land Tax Act 2000", "Tas"),
    ("tas-payrolltax-2008", "Payroll Tax Act 2008", "Tas"),
    ("sa-stampduties-1923", "Stamp Duties Act 1923", "SA"),
    ("sa-landtax-1936", "Land Tax Act 1936", "SA"),
    ("sa-payrolltax-2009", "Payroll Tax Act 2009", "SA"),
    ("wa-duties-2008", "Duties Act 2008", "WA"),
    ("wa-landtaxassessment-2002", "Land Tax Assessment Act 2002", "WA"),
    ("wa-payrolltaxassessment-2002", "Pay-roll Tax Assessment Act 2002", "WA"),
    ("nsw-duties-1997", "Duties Act 1997", "NSW"),
    ("nsw-landtaxmgmt-1956", "Land Tax Management Act 1956", "NSW"),
    ("nsw-landtax-1956", "Land Tax Act 1956", "NSW"),
    ("nsw-payrolltax-2007", "Payroll Tax Act 2007", "NSW"),
];

fn state_tax_name(id: &str) -> Option<String> {
    STATE_TAX_ACTS.iter().find(|(i, _, _)| *i == id).map(|(_, title, jur)| format!("{title} ({jur})"))
}

fn title_case(id: &str) -> String {
    let mut out = String::with_capacity(id.len());
    let mut boundary = true;
    for c in id.chars() {
        let c = if c == '-' { ' ' } else { c };
        let word = c.is_ascii_alphanumeric() || c == '_';
        out.extend(if word && boundary { c.to_uppercase().collect::<Vec<_>>() } else { vec![c] });
        boundary = !word;
    }
    out
}

/// `shortActName`: the compact name in the sidebar header and the contents page.
pub fn short_act_name(id: &str) -> String {
    let known = match id {
        "itaa-1997" => "ITAA97",
        "itaa-1936" => "ITAA36",
        "gst-1999" => "GST99",
        "taa-1953" => "TAA53",
        "fbt-1986" => "FBTAA 1986",
        "sis-1993" => "SIS 1993",
        "nz-it-2007" => "NZ IT07",
        "corporations-act-2001" => "Corps Act",
        "bankruptcy-act-1966" => "Bankruptcy Act",
        "aml-ctf-2006" => "AML/CTF Act",
        "aml-ctf-rules-2007" => "AML/CTF Rules",
        _ => "",
    };
    if !known.is_empty() {
        return known.to_string();
    }
    state_tax_name(id).unwrap_or_else(|| title_case(id))
}

/// `actTitleName`: abbreviation and year, for search result titles.
pub fn act_title_name(id: &str) -> String {
    let known = match id {
        "itaa-1997" => "ITAA 1997",
        "itaa-1936" => "ITAA 1936",
        "gst-1999" => "GST 1999",
        "taa-1953" => "TAA 1953",
        "fbt-1986" => "FBTAA 1986",
        "sis-1993" => "SIS 1993",
        "nz-it-2007" => "NZ IT 2007",
        "corporations-act-2001" => "Corporations Act 2001",
        "bankruptcy-act-1966" => "Bankruptcy Act 1966",
        "aml-ctf-2006" => "AML/CTF Act 2006",
        "aml-ctf-rules-2007" => "AML/CTF Rules 2007",
        _ => "",
    };
    if !known.is_empty() {
        return known.to_string();
    }
    state_tax_name(id).unwrap_or_else(|| short_act_name(id))
}

/// The dictionary section of an act, where it has one: where the Definitions button goes.
pub fn dictionary_section(act: &str) -> Option<&'static str> {
    Some(match act {
        "itaa-1997" => "995-1",
        "itaa-1936" => "6",
        "gst-1999" => "195-1",
        "fbt-1986" => "136",
        "taa-1953" => "2",
        "sis-1993" => "10",
        "aml-ctf-2006" => "5",
        "nz-it-2007" => "YA-1",
        "corporations-act-2001" => "9",
        _ => return None,
    })
}

#[derive(Clone)]
pub struct SourceItem {
    pub id: String,
    pub label: String,
}

pub struct SourceGroup {
    pub label: String,
    pub items: Vec<SourceItem>,
}

const CORE_ACTS: [&str; 5] = ["itaa-1997", "itaa-1936", "corporations-act-2001", "gst-1999", "taa-1953"];
const FEDERAL_TAX: [&str; 10] = [
    "itaa-1997", "itaa-1936", "gst-1999", "fbt-1986", "taa-1953", "sis-1993",
    "aml-ctf-2006", "aml-ctf-rules-2007", "aml-ctf-rules-2025", "aml-ctf-transitional-rules-2026",
];
const STATE_PREFIXES: [&str; 8] = ["nsw-", "qld-", "tas-", "sa-", "wa-", "vic-", "nt-", "act-"];
/// Browse-only surfaces that are not legislation; none of them is part of this build.
const NON_ACT_IDS: [&str; 13] = [
    "private-rulings", "rulings", "nz-rulings", "treaties", "definitions", "maps", "afsa-guides",
    "regulatory-guides", "proposed-law", "spec", "commentary", "insolvency", "nz-mtg",
];
const GROUP_ORDER: [&str; 3] = ["Tax resources", "Professional", "Tools"];
const LEAD_GROUPS: [&str; 1] = ["Case law"];

fn label_for(a: &Act) -> String {
    let served = a.name.trim();
    if !served.is_empty() && served.to_lowercase() != a.id.to_lowercase() && served.chars().count() > 2 {
        return served.to_string();
    }
    match a.id.as_str() {
        "itaa-1997" => "Income Tax Assessment Act 1997".into(),
        "itaa-1936" => "Income Tax Assessment Act 1936".into(),
        "fbt-1986" => "Fringe Benefits Tax Assessment Act 1986".into(),
        "sis-1993" => "Superannuation Industry (Supervision) Act 1993".into(),
        "taa-1953" => "Taxation Administration Act 1953".into(),
        "gst-1999" => "A New Tax System (Goods and Services Tax) Act 1999".into(),
        "corporations-act-2001" => "Corporations Act 2001".into(),
        _ => a.id.clone(),
    }
}

/// `buildSourceGroups`: the Acts people open first, then by jurisdiction, then everything else.
pub fn source_groups(acts: &[Act]) -> Vec<SourceGroup> {
    let by_label = |items: &mut Vec<SourceItem>| items.sort_by_key(|i| i.label.to_lowercase());
    let mut collections: Vec<(String, Vec<SourceItem>)> = GROUP_ORDER.iter().map(|g| (g.to_string(), Vec::new())).collect();
    for a in acts.iter().filter(|a| !a.id.is_empty()) {
        if let Some(g) = &a.group {
            let item = SourceItem { id: a.id.clone(), label: label_for(a) };
            match collections.iter_mut().find(|(label, _)| label == g) {
                Some((_, items)) => items.push(item),
                None => collections.push((g.clone(), vec![item])),
            }
        }
    }
    let mut items: Vec<SourceItem> = acts
        .iter()
        .filter(|a| !a.id.is_empty() && a.group.is_none() && !NON_ACT_IDS.contains(&a.id.as_str()))
        .map(|a| SourceItem { id: a.id.clone(), label: label_for(a) })
        .collect();
    let mut take = |test: &dyn Fn(&SourceItem) -> bool| -> Vec<SourceItem> {
        let (taken, rest): (Vec<_>, Vec<_>) = std::mem::take(&mut items).into_iter().partition(|i| test(i));
        items = rest;
        taken
    };
    let mut core = take(&|i| CORE_ACTS.contains(&i.id.as_str()));
    core.sort_by_key(|i| CORE_ACTS.iter().position(|c| *c == i.id));
    let mut federal = take(&|i| FEDERAL_TAX.contains(&i.id.as_str()));
    let mut state = take(&|i| STATE_TAX_ACTS.iter().any(|(id, _, _)| *id == i.id) || STATE_PREFIXES.iter().any(|p| i.id.starts_with(p)));
    let mut nz = take(&|i| i.id.starts_with("nz-"));
    let tax_cases = take(&|i| i.id == "tax-cases");
    let mut other = take(&|_| true);
    by_label(&mut federal);
    by_label(&mut state);
    by_label(&mut nz);
    by_label(&mut other);
    let (lead, rest): (Vec<_>, Vec<_>) = collections.into_iter().partition(|(label, _)| LEAD_GROUPS.contains(&label.as_str()));

    let mut groups = vec![SourceGroup { label: "Core legislation".into(), items: core }];
    let sorted = |(label, mut items): (String, Vec<SourceItem>)| {
        items.sort_by_key(|i| i.label.to_lowercase());
        SourceGroup { label, items }
    };
    groups.extend(lead.into_iter().map(sorted));
    groups.push(SourceGroup { label: "Federal tax".into(), items: federal });
    groups.push(SourceGroup { label: "State tax".into(), items: state });
    groups.push(SourceGroup { label: "New Zealand legislation".into(), items: nz });
    groups.push(SourceGroup { label: "Other legislation".into(), items: tax_cases.into_iter().chain(other).collect() });
    groups.extend(rest.into_iter().map(sorted));
    groups.retain(|g| !g.items.is_empty());
    groups
}
