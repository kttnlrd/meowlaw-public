//! Act trees: loaded on first request, then cached. The shallow / split-part logic is a port
//! of backend/routes/acts.py; `load` is `load_tree` from backend/services/data_loader.py.

use crate::pyjson::{self, merged, Node, V};
use bytes::Bytes;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};

/// A tree with more sections than this is served shallow when asked (`?shallow=1`).
const SHALLOW_MIN_SECTIONS: usize = 400;
/// A part this large is sent as its divisions (each lazy) rather than as one lazy block.
const SPLIT_PART_MIN: usize = 2000;

static EMPTY: Vec<V> = Vec::new();
static NULL: V = V::Null;

pub struct Tree {
    root: V,
    /// Sections under each part, in part order.
    counts: Vec<usize>,
    /// The `?shallow=1` body with no `open`, which is what every source page asks for first.
    shallow: OnceLock<Bytes>,
}

fn list<'a>(node: &'a V, key: &str) -> &'a Vec<V> {
    node.get(key).and_then(V::as_array).unwrap_or(&EMPTY)
}

/// `len(_sections_in(node))`.
fn count(node: &V) -> usize {
    list(node, "sections").len()
        + list(node, "divisions").iter().map(count).sum::<usize>()
        + list(node, "subdivisions").iter().map(count).sum::<usize>()
}

/// `str(s.get("id")) == section`.
fn id_is(node: &V, want: &str) -> bool {
    match node.get("id") {
        Some(V::Str(s)) => **s == *want,
        Some(V::Num(n)) => n.to_string() == want,
        Some(V::Bool(b)) => want == if *b { "True" } else { "False" },
        Some(V::Null) | None => want == "None",
        Some(_) => false,
    }
}

fn holds(node: &V, section: &str) -> bool {
    list(node, "sections").iter().any(|s| id_is(s, section))
        || list(node, "divisions").iter().any(|d| holds(d, section))
        || list(node, "subdivisions").iter().any(|d| holds(d, section))
}

const LIGATURES: [(char, &str); 7] = [
    ('\u{fb00}', "ff"),
    ('\u{fb01}', "fi"),
    ('\u{fb02}', "fl"),
    ('\u{fb03}', "ffi"),
    ('\u{fb04}', "ffl"),
    ('\u{fb05}', "st"),
    ('\u{fb06}', "st"),
];

/// `_normalise_text`: typographic ligatures do not render on every device.
pub fn normalise_text(s: &str) -> std::borrow::Cow<'_, str> {
    if !s.contains(|c| ('\u{fb00}'..='\u{fb06}').contains(&c)) {
        return std::borrow::Cow::Borrowed(s);
    }
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        match LIGATURES.iter().find(|(l, _)| *l == c) {
            Some((_, rep)) => out.push_str(rep),
            None => out.push(c),
        }
    }
    std::borrow::Cow::Owned(out)
}

fn norm_title(node: &mut V, insert_missing: bool) {
    if !matches!(node, V::Obj(_)) {
        return;
    }
    match node.get_mut("title") {
        Some(V::Str(s)) => {
            if let std::borrow::Cow::Owned(fixed) = normalise_text(s) {
                *s = fixed.into();
            }
        }
        None if insert_missing => node.push("title", V::Str("".into())),
        _ => {}
    }
}

fn each_mut<'a>(node: &'a mut V, key: &str) -> std::slice::IterMut<'a, V> {
    let items: &mut [V] = match node.get_mut(key).and_then(V::as_array_mut) {
        Some(a) => a,
        None => Default::default(),
    };
    items.iter_mut()
}

impl Tree {
    fn load(path: &PathBuf) -> Result<Tree, String> {
        let bytes = fs::read(path).map_err(|e| e.to_string())?;
        let mut root: V = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
        drop(bytes);
        if !matches!(root, V::Obj(_)) {
            return Err("not a JSON object".into());
        }
        if let Some(parts) = root.get_mut("parts").and_then(V::as_array_mut) {
            for part in parts.iter_mut() {
                each_mut(part, "sections").for_each(|s| norm_title(s, false));
                for div in each_mut(part, "divisions") {
                    each_mut(div, "sections").for_each(|s| norm_title(s, false));
                    for sub in each_mut(div, "subdivisions") {
                        each_mut(sub, "sections").for_each(|s| norm_title(s, false));
                        norm_title(sub, true);
                    }
                    norm_title(div, true);
                }
                norm_title(part, true);
            }
        }
        let counts = list(&root, "parts").iter().map(count).collect();
        Ok(Tree { root, counts, shallow: OnceLock::new() })
    }

    fn parts(&self) -> &Vec<V> {
        list(&self.root, "parts")
    }

    fn split(&self, pi: usize) -> bool {
        self.counts[pi] > SPLIT_PART_MIN && !list(&self.parts()[pi], "divisions").is_empty()
    }

    /// `_part_holding`: the smallest lazily loaded node holding a section, as (part, division).
    fn holding(&self, section: &str) -> Option<(usize, Option<usize>)> {
        for (pi, p) in self.parts().iter().enumerate() {
            if holds(p, section) {
                if self.split(pi) {
                    if let Some(di) = list(p, "divisions").iter().position(|d| holds(d, section)) {
                        return Some((pi, Some(di)));
                    }
                }
                return Some((pi, None));
            }
        }
        None
    }

    fn shell(node: &V, n: usize) -> Node<'_> {
        Node::Obj(vec![
            ("id", Node::Ref(node.get("id").unwrap_or(&NULL))),
            ("title", Node::Ref(node.get("title").unwrap_or(&NULL))),
            ("count", Node::Owned(json!(n))),
            ("lazy", Node::Owned(json!(true))),
            ("divisions", Node::Owned(json!([]))),
            ("sections", Node::Owned(json!([]))),
        ])
    }

    /// `/api/tree/{act}` body, in `json.dumps` spacing.
    pub fn render(&self, licence: Option<&Value>, shallow: bool, open: Option<&str>) -> Bytes {
        let is_shallow = shallow && self.counts.iter().sum::<usize>() > SHALLOW_MIN_SECTIONS;
        if is_shallow && open.is_none() {
            return self.shallow.get_or_init(|| self.build(licence, true, None)).clone();
        }
        self.build(licence, is_shallow, open)
    }

    fn build(&self, licence: Option<&Value>, shallow: bool, open: Option<&str>) -> Bytes {
        let mut over: Vec<(&str, Node)> = Vec::new();
        if let Some(lic) = licence {
            over.push(("licence", Node::Json(lic)));
        }
        if shallow {
            let keep = open.and_then(|s| self.holding(s));
            let mut parts = Vec::with_capacity(self.counts.len());
            for (pi, p) in self.parts().iter().enumerate() {
                if keep == Some((pi, None)) {
                    parts.push(Node::Ref(p));
                } else if self.split(pi) {
                    let divisions = list(p, "divisions")
                        .iter()
                        .enumerate()
                        .map(|(di, d)| if keep == Some((pi, Some(di))) { Node::Ref(d) } else { Self::shell(d, count(d)) })
                        .collect();
                    parts.push(merged(p, vec![("sections", Node::Owned(json!([]))), ("divisions", Node::Arr(divisions))]));
                } else {
                    parts.push(Self::shell(p, self.counts[pi]));
                }
            }
            over.push(("shallow", Node::Owned(json!(true))));
            over.push(("parts", Node::Arr(parts)));
        }
        Bytes::from(pyjson::dumps(&merged(&self.root, over)))
    }

    /// `/api/tree/{act}/part` body (compact): by id, or the node holding a section.
    pub fn part(&self, id: Option<&str>, section: Option<&str>) -> Option<Vec<u8>> {
        let parts = self.parts();
        let node = match section {
            Some(s) => self.holding(s).map(|(pi, di)| match di {
                Some(di) => &list(&parts[pi], "divisions")[di],
                None => &parts[pi],
            }),
            None => {
                let id = id?;
                parts
                    .iter()
                    .find(|p| id_is(p, id))
                    .or_else(|| parts.iter().flat_map(|p| list(p, "divisions").iter()).find(|d| id_is(d, id)))
            }
        };
        node.map(pyjson::compact)
    }

    /// Visits every section the Python loaders see: part, division and subdivision level.
    pub fn sections(&self) -> impl Iterator<Item = &V> {
        self.parts().iter().flat_map(|p| {
            list(p, "sections").iter().chain(list(p, "divisions").iter().flat_map(|d| {
                list(d, "sections").iter().chain(list(d, "subdivisions").iter().flat_map(|s| list(s, "sections").iter()))
            }))
        })
    }

    /// `find_section_path`: the file of the section whose id matches, ignoring case.
    pub fn section_path(&self, section: &str) -> Option<&str> {
        let want = section.to_lowercase();
        self.sections()
            .find(|s| match s.get("id").and_then(V::as_str) {
                Some(id) => id == section || (id.len() == want.len() || !id.is_ascii()) && id.to_lowercase() == want,
                None => false,
            })
            .and_then(|s| s.get("path").and_then(V::as_str))
    }
}

/// `is_plain_id`: a bare directory-safe id, so nothing from a URL can walk the filesystem.
pub fn is_plain_id(name: &str) -> bool {
    let ok = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '-';
    !name.is_empty()
        && name.starts_with(|c: char| c.is_ascii_alphanumeric())
        && name.split('.').enumerate().all(|(i, seg)| (i == 0 || !seg.is_empty()) && seg.chars().all(ok))
}

type Slot = Arc<Mutex<Option<Arc<Tree>>>>;

pub struct Trees {
    data: PathBuf,
    slots: Mutex<HashMap<String, Slot>>,
}

impl Trees {
    pub fn new(data: PathBuf) -> Trees {
        Trees { data, slots: Mutex::new(HashMap::new()) }
    }

    /// The act's tree, read and parsed on first use. Concurrent first requests for one act
    /// wait on the same load. Err carries (status, detail) as the Python app answers.
    pub fn get(&self, act: &str) -> Result<Arc<Tree>, (u16, String)> {
        let path = self.data.join(act).join("tree.json");
        if !is_plain_id(act) || !path.is_file() {
            return Err((404, format!("Act {act} not found")));
        }
        let slot = self.slots.lock().unwrap().entry(act.to_string()).or_default().clone();
        let mut guard = slot.lock().unwrap();
        if let Some(t) = guard.as_ref() {
            return Ok(t.clone());
        }
        let t = Arc::new(Tree::load(&path).map_err(|e| (500, format!("Could not read {act}/tree.json: {e}")))?);
        *guard = Some(t.clone());
        Ok(t)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tree(v: Value) -> Tree {
        let root: V = serde_json::from_value(v).unwrap();
        let counts = list(&root, "parts").iter().map(count).collect();
        Tree { root, counts, shallow: OnceLock::new() }
    }

    #[test]
    fn plain_ids() {
        assert!(is_plain_id("itaa-1997") && is_plain_id("a.b_c"));
        assert!(!is_plain_id("..") && !is_plain_id(".x") && !is_plain_id("a/b") && !is_plain_id("a..b") && !is_plain_id(""));
    }

    #[test]
    fn small_tree_is_never_shallow() {
        let t = tree(json!({"act": "A", "parts": [{"id": "1", "title": "P", "divisions": [], "sections": [{"id": "1", "title": "x", "path": "1.md"}]}]}));
        let body = t.render(None, true, None);
        assert_eq!(
            std::str::from_utf8(&body).unwrap(),
            r#"{"act": "A", "parts": [{"id": "1", "title": "P", "divisions": [], "sections": [{"id": "1", "title": "x", "path": "1.md"}]}]}"#
        );
    }

    #[test]
    fn shallow_shells_and_keeps_the_open_part() {
        let secs = |from: usize, n: usize| -> Vec<Value> { (from..from + n).map(|i| json!({"id": i.to_string(), "title": "t", "path": "p"})).collect() };
        let t = tree(json!({"act": "A", "parts": [
            {"id": "p1", "title": "One", "divisions": [], "sections": secs(0, 300)},
            {"id": "p2", "title": "Two", "divisions": [{"id": "d1", "title": "D", "sections": secs(300, 2500)}], "sections": []},
        ]}));
        let v: Value = serde_json::from_slice(&t.render(None, true, Some("5"))).unwrap();
        assert_eq!(v["shallow"], json!(true));
        assert_eq!(v["parts"][0]["sections"].as_array().unwrap().len(), 300);
        assert_eq!(v["parts"][1]["divisions"][0], json!({"id": "d1", "title": "D", "count": 2500, "lazy": true, "divisions": [], "sections": []}));
        assert_eq!(t.holding("400"), Some((1, Some(0))));
        let v: Value = serde_json::from_slice(&t.part(None, Some("400")).unwrap()).unwrap();
        assert_eq!(v["id"], "d1");
        assert!(t.part(Some("nope"), None).is_none());
    }
}
