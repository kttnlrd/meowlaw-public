//! The source catalogue: everything `/api/acts` lists, built at startup without reading a tree.
//!
//! Mirrors `load_acts_meta` / `licence_notice` (backend/services/data_loader.py) and
//! backend/services/collections.py. Each `data/<id>/tree.json` gives the act name and
//! compilation; only the first few KB are read, because those scalars sit ahead of `parts`
//! in almost every tree. A tree that orders them differently is scanned in full the first
//! time `/api/acts` is asked for, never at startup.

use crate::pyjson;
use serde::de::{DeserializeSeed, Error as _, IgnoredAny, MapAccess, Visitor};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::fmt;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

const HEAD_BYTES: usize = 16 * 1024;

#[derive(Default, Clone)]
struct Head {
    act: Option<Value>,
    compilation_no: Option<Value>,
    compilation_date: Option<Value>,
}

impl Head {
    fn complete(&self) -> bool {
        self.act.is_some() && self.compilation_no.is_some() && self.compilation_date.is_some()
    }
}

/// Reads the three top-level scalars and skips every other value unparsed. With `bail`, stops
/// as soon as all three are in hand, so a tree whose `parts` come last is never read past its
/// first block.
struct HeadSeed<'a> {
    head: &'a mut Head,
    bail: bool,
}

impl<'de> DeserializeSeed<'de> for HeadSeed<'_> {
    type Value = ();
    fn deserialize<D: serde::Deserializer<'de>>(self, d: D) -> Result<(), D::Error> {
        d.deserialize_map(self)
    }
}

impl<'de> Visitor<'de> for HeadSeed<'_> {
    type Value = ();
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("a tree object")
    }
    fn visit_map<A: MapAccess<'de>>(self, mut m: A) -> Result<(), A::Error> {
        while let Some(k) = m.next_key::<String>()? {
            match k.as_str() {
                "act" => self.head.act = Some(m.next_value()?),
                "compilation_no" => self.head.compilation_no = Some(m.next_value()?),
                "compilation_date" => self.head.compilation_date = Some(m.next_value()?),
                _ => {
                    m.next_value::<IgnoredAny>()?;
                }
            }
            if self.bail && self.head.complete() {
                return Err(A::Error::custom("head complete"));
            }
        }
        Ok(())
    }
}

/// The scalars from the first block of a tree.json, and whether they are known to be final.
fn read_head(path: &Path) -> Option<(Head, bool)> {
    let mut buf = Vec::with_capacity(HEAD_BYTES);
    fs::File::open(path).ok()?.take(HEAD_BYTES as u64).read_to_end(&mut buf).ok()?;
    let mut head = Head::default();
    let mut de = serde_json::Deserializer::from_slice(&buf);
    let whole = HeadSeed { head: &mut head, bail: true }.deserialize(&mut de).is_ok();
    let done = whole || head.complete();
    Some((head, done))
}

fn read_head_full(path: &Path) -> Head {
    let mut head = Head::default();
    if let Ok(buf) = fs::read(path) {
        let mut de = serde_json::Deserializer::from_slice(&buf);
        let _ = HeadSeed { head: &mut head, bail: false }.deserialize(&mut de);
    }
    head
}

pub struct Collection {
    pub title: String,
    pub group: String,
    pub access: String,
}

pub struct Source {
    pub id: String,
    tree_path: PathBuf,
    head: OnceLock<Head>,
    jurisdiction: String,
    pub licence: Option<Value>,
}

pub struct Catalog {
    pub sources: Vec<Source>,
    index: HashMap<String, usize>,
    /// Keyed by lower-cased folder name, as in the Python app.
    pub collections: HashMap<String, Collection>,
    acts_all: OnceLock<Vec<Value>>,
}

impl Catalog {
    pub fn build(data: &Path) -> Catalog {
        let mut names: Vec<String> = fs::read_dir(data)
            .map(|rd| rd.flatten().filter_map(|e| e.file_name().into_string().ok()).collect())
            .unwrap_or_default();
        names.sort();
        let mut sources = Vec::new();
        let mut collections = HashMap::new();
        for name in names {
            let dir = data.join(&name);
            if let Ok(text) = fs::read_to_string(dir.join("collection.yaml")) {
                collections.insert(name.to_lowercase(), parse_collection(&name.to_lowercase(), &text));
            }
            let tree_path = dir.join("tree.json");
            if !tree_path.is_file() {
                continue;
            }
            let lic: Option<Value> =
                fs::read(dir.join("licence.json")).ok().and_then(|b| serde_json::from_slice(&b).ok());
            let jur = lic.as_ref().and_then(|l| l.get("jurisdiction")).and_then(Value::as_str).filter(|s| !s.is_empty());
            let jurisdiction =
                jur.unwrap_or(if name.starts_with("nz-") { "NZ" } else { "AU" }).to_uppercase();
            let head = OnceLock::new();
            if let Some((h, true)) = read_head(&tree_path) {
                let _ = head.set(h);
            }
            sources.push(Source {
                id: name,
                tree_path,
                head,
                jurisdiction,
                licence: lic.as_ref().and_then(licence_notice),
            });
        }
        let index = sources.iter().enumerate().map(|(i, s)| (s.id.clone(), i)).collect();
        Catalog { sources, index, collections, acts_all: OnceLock::new() }
    }

    pub fn has(&self, id: &str) -> bool {
        self.index.contains_key(id)
    }

    pub fn licence(&self, id: &str) -> Option<&Value> {
        self.index.get(id).and_then(|&i| self.sources[i].licence.as_ref())
    }

    fn all(&self) -> &Vec<Value> {
        self.acts_all.get_or_init(|| {
            self.sources
                .iter()
                .map(|s| {
                    let h = s.head.get_or_init(|| read_head_full(&s.tree_path));
                    let comp_no = match h.compilation_no.clone().unwrap_or(Value::Null) {
                        // 0 is a placeholder for 'not recorded'. Python's `in (0, "0", "")`
                        // also catches 0.0 and False.
                        Value::Number(n) if n.as_f64() == Some(0.0) => Value::Null,
                        Value::Bool(false) => Value::Null,
                        Value::String(t) if t == "0" || t.is_empty() => Value::Null,
                        v => v,
                    };
                    let mut a = Map::new();
                    a.insert("id".into(), json!(s.id));
                    a.insert("name".into(), h.act.clone().unwrap_or_else(|| json!(s.id)));
                    a.insert("compilation_no".into(), comp_no);
                    a.insert("compilation_date".into(), h.compilation_date.clone().unwrap_or(Value::Null));
                    a.insert("jurisdiction".into(), json!(s.jurisdiction));
                    if let Some(c) = self.collections.get(&s.id.to_lowercase()) {
                        a.insert("name".into(), json!(c.title));
                        a.insert("group".into(), json!(c.group));
                        a.insert("access".into(), json!(c.access));
                    }
                    Value::Object(a)
                })
                .collect()
        })
    }

    /// `/api/acts` body. Only tree-backed sources are listed: the rulings, tax-case,
    /// private-ruling, guide, treaty and map browsers are not part of this build.
    pub fn acts_json(&self, jurisdiction: Option<&str>) -> Vec<u8> {
        let all = self.all();
        match jurisdiction.filter(|j| !j.is_empty()) {
            None => pyjson::compact(all),
            Some(j) => {
                let want = py_strip(j).to_uppercase();
                let kept: Vec<&Value> = all
                    .iter()
                    .filter(|a| a.get("jurisdiction").and_then(Value::as_str).unwrap_or("").to_uppercase() == want)
                    .collect();
                pyjson::compact(&kept)
            }
        }
    }
}

/// What a view must display for one source, from `data/<source>/licence.json`. None where the
/// record is not cleared to render or lacks a field the Python app requires.
fn licence_notice(lic: &Value) -> Option<Value> {
    if lic.get("render") == Some(&Value::Bool(false)) {
        return None;
    }
    let get = |k: &str| lic.get(k).cloned().unwrap_or(Value::Null);
    let badge = match lic.get("notice_badge") {
        Some(v) if py_truthy(v) => v.clone(),
        _ => get("jurisdiction"),
    };
    let inner = lic.get("licence")?;
    let mut out = Map::new();
    out.insert("jurisdiction".into(), lic.get("jurisdiction")?.clone());
    out.insert("attribution".into(), get("attribution_display"));
    out.insert("notice_badge".into(), badge);
    out.insert("notice_line".into(), get("notice_line"));
    out.insert("disclaimer".into(), get("disclaimer"));
    out.insert("unofficial_notice".into(), get("unofficial_notice"));
    out.insert("endorsement_notice".into(), get("endorsement_notice"));
    out.insert("licence".into(), json!({"name": inner.get("name")?, "url": inner.get("url")?}));
    out.insert("source_url".into(), lic.get("source_url")?.clone());
    out.insert("fetched".into(), get("fetched"));
    out.insert("terms_url".into(), lic.get("terms_url")?.clone());
    if let Some(version) = lic.get("version") {
        out.insert("version_date".into(), version.get("date")?.clone());
    }
    Some(Value::Object(out))
}

pub fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's `str.isspace()` for the characters that turn up in this corpus.
pub fn py_is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

/// collection.yaml holds three plain scalars (title, group, access), so it is read line by
/// line rather than through a YAML parser. Anything unreadable falls back to the safest value.
fn parse_collection(cid: &str, text: &str) -> Collection {
    let mut meta: HashMap<String, String> = HashMap::new();
    for line in text.lines() {
        if line.starts_with([' ', '\t', '#', '-']) {
            continue;
        }
        let Some((k, v)) = line.split_once(':') else { continue };
        let v = v.trim();
        let v = if (v.starts_with('"') && v.ends_with('"') || v.starts_with('\'') && v.ends_with('\'')) && v.len() >= 2 {
            &v[1..v.len() - 1]
        } else {
            let v = v.split(" #").next().unwrap_or("").trim();
            if matches!(v, "~" | "null" | "Null" | "NULL") {
                ""
            } else {
                v
            }
        };
        meta.insert(k.trim().to_string(), v.to_string());
    }
    let non_empty = |k: &str| meta.get(k).filter(|v| !v.is_empty()).cloned();
    let access = meta.get("access").map(|a| a.to_lowercase()).unwrap_or_else(|| "restricted".into());
    Collection {
        title: non_empty("title").unwrap_or_else(|| cid.to_string()),
        group: non_empty("group").unwrap_or_else(|| "Collections".into()),
        access: if matches!(access.as_str(), "public" | "signed-in" | "restricted") {
            access
        } else {
            "restricted".into()
        },
    }
}
