//! Section content: the file is read when asked for, then run through the same passes the
//! Python app applies before it serves a section.
//!
//! Ports `get_act_section_content` (backend/services/data_loader.py), `strip_scraped_markup`
//! (backend/services/text_cleaner.py) and the definition passes in
//! backend/processors/markdown.py. The `regex` crate has no look-around, so the three Python
//! patterns that use it are matched by hand below.

use crate::catalog::{py_is_space, py_strip, py_truthy};
use crate::tree::{normalise_text, Trees};
use regex::{Captures, Regex};
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex, OnceLock};

/// Python's `\s` also matches the four ASCII separators U+001C..U+001F.
fn rx(pattern: &str) -> Regex {
    Regex::new(&pattern.replace(r"\s", r"[\s\x1c-\x1f]")).expect("static pattern")
}

macro_rules! re {
    ($name:ident, $pat:expr) => {
        static $name: LazyLock<Regex> = LazyLock::new(|| rx($pat));
    };
}

// ---------------------------------------------------------------------------
// strip_scraped_markup
// ---------------------------------------------------------------------------

re!(TAG, r"<[^>]+>");
// The Python pattern is `<(?!a\s+id="[^"]*"\s*>|/a\s*>)[^>]+>`: paragraph anchors survive.
re!(TAG_KEEP, r#"^(?:a\s+id="[^"]*"\s*>|/a\s*>)"#);

static SCRAPED: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    [
        r"(?s)//\s*\(function\s*\([^)]*\).*?;?\s*$",
        r#"bazadebezolkohpepadr="[^"]*""#,
        r"\$\([^)]+\)\.\w+\([^)]*\);?",
        r"window\.dataLayer.*?;",
        r"gtag\([^)]*\);?",
        r"(?i)Legal\s+database\s*(/\s*(Contents|Download|Email|Print|Back\s+to\s+browse)\s*)+",
        r"(?im)^(Legal\s+database\s*\n){1,2}(Contents\s*\n)?(Download\s*\n)?(Email\s*\n)?(Print\s*\n)?(Back\s+to\s+browse\s*\n)?(\d+\s+related\s+documents\s*\n)?",
        r"(?i)Generated\s+on:\s*\d+\s+\w+\s+\d{4},\s*\d+:\d+:\d+\s*(AM|PM)",
        r"(?i)Page\s+\d+\s+of\s+\d+",
        r"(?i)You\s+are\s+here\s*:?\s*[^\n]*",
        r"(?im)^Back\s+to\s+top\s*$",
        r"(?im)^ato\.gov\.au\s*$",
        r"(?im)^©\s*AUSTRALIAN\s+TAXATION\s+OFFICE.*$",
        r"(?im)^You\s+are\s+free\s+to\s+copy.*$",
        r"(?im)^Copyright\s+(notice|policy).*$",
        r"(?im)^Disclaimers?\s*$",
        r"(?im)^Privacy\s+Policy\s*$",
        r"(?im)^Feedback\s*$",
        r"(?im)^AustLII\s*:?\s*$",
        r"(?im)^Home\s*$",
        r"(?im)^Databases\s*$",
        r"(?im)^WorldLII\s*$",
        r"(?im)^Search\s*$",
        r"(?m)^[A-Z]{2,6}\s+\d{4}/\d+\s*$",
        r"(?m)&#169;.*$",
        r"\s*\[(Noteup|Download|Help|Home|Databases|WorldLII|Search|Feedback|Database\s+Search|Name\s+Search|Recent\s+Decisions|Index)\]\s*",
    ]
    .iter()
    .map(|p| rx(p))
    .collect()
});

re!(
    TRAILING_META,
    r"(?i)\n\s*(Keywords|Related\s+Public\s+Rulings?|Other\s+References?|Business\s+Line|Date\s+of\s+publication|ISSN|history|Archived|Original\s+statement|Legislative\s+References)\s*[:：]?\s*\n"
);
re!(BLANK_RUN, r"\n{3,}");
re!(BLANK_LINE, r"(?m)^\s*\n");

fn strip_tags(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let (mut last, mut pos) = (0, 0);
    while let Some(m) = TAG.find_at(text, pos) {
        if TAG_KEEP.is_match(&text[m.start() + 1..]) {
            pos = m.start() + 1;
            continue;
        }
        out.push_str(&text[last..m.start()]);
        last = m.end();
        pos = m.end();
    }
    out.push_str(&text[last..]);
    out
}

pub fn strip_scraped_markup(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let mut result = strip_tags(text);
    for p in SCRAPED.iter() {
        if let std::borrow::Cow::Owned(s) = p.replace_all(&result, "") {
            result = s;
        }
    }
    if let Some(m) = TRAILING_META.find(&result) {
        result.truncate(m.start());
    }
    let result = py_strip(&result);
    let result = BLANK_RUN.replace_all(result, "\n\n");
    let result = BLANK_LINE.replace_all(&result, "\n");
    py_strip(&result).to_string()
}

// ---------------------------------------------------------------------------
// Definitions
// ---------------------------------------------------------------------------

pub struct Info {
    section: String,
    anchor: String,
    truthy: bool,
}

/// One act's definition index: `load_definitions(act)`, in the store's key order.
pub struct Defs {
    entries: Vec<(String, Info)>,
    map: HashMap<String, usize>,
    /// Longest term first; equal lengths keep store order (Python's stable sort).
    by_len: Vec<usize>,
}

impl Defs {
    fn get(&self, key: &str) -> Option<&Info> {
        self.map.get(key).map(|&i| &self.entries[i].1).filter(|i| i.truthy)
    }
}

fn resolve_act_id(act: &str) -> &str {
    match py_strip(act).to_lowercase().as_str() {
        "1936" | "itaa36" => "itaa-1936",
        "1997" | "itaa97" => "itaa-1997",
        "gst" | "gst1999" => "gst-1999",
        "fbt" | "fbtaa" => "fbt-1986",
        "taa" => "taa-1953",
        "sis" => "sis-1993",
        "corps" => "corporations-act-2001",
        "aml" | "amlctf" => "aml-ctf-2006",
        "nz" | "nzit" => "nz-it-2007",
        _ => act,
    }
}

/// `_canon_section_id`: '6f' -> '6F'.
fn canon_section_id(section: &str) -> String {
    let cut = section.trim_end_matches(|c: char| c.is_ascii_lowercase()).len();
    format!("{}{}", &section[..cut], section[cut..].to_ascii_uppercase())
}

fn build_defs(store: &Map<String, Value>, act: &str) -> Defs {
    let mut defs = Defs { entries: Vec::new(), map: HashMap::new(), by_len: Vec::new() };
    let terms = store.get(resolve_act_id(act)).and_then(|a| a.get("terms")).and_then(Value::as_object);
    for (term, info) in terms.into_iter().flatten() {
        let text = |k: &str| info.get(k).and_then(Value::as_str).unwrap_or("");
        let section = match info.get("section") {
            Some(v) if py_truthy(v) => canon_section_id(text("section")),
            _ => String::new(),
        };
        let value = Info {
            section,
            anchor: text("anchor").to_string(),
            truthy: info.as_object().is_some_and(|o| !o.is_empty()),
        };
        let key = term.to_lowercase().replace(['\u{2018}', '\u{2019}'], "'");
        match defs.map.get(&key) {
            Some(&i) => defs.entries[i].1 = value,
            None => {
                defs.map.insert(key.clone(), defs.entries.len());
                defs.entries.push((key, value));
            }
        }
    }
    defs.by_len = (0..defs.entries.len()).collect();
    defs.by_len.sort_by_key(|&i| std::cmp::Reverse(defs.entries[i].0.chars().count()));
    defs
}

// ---------------------------------------------------------------------------
// format_definition_terms
// ---------------------------------------------------------------------------

re!(
    DEF_PREDICATE,
    r"(?im)^(\*\*\(\d+\)\*\*\s+)?([A-Za-z0-9\-'%*()][A-Za-z0-9\-'%*() ]{0,80}?)\s+(has the meaning given by|means|has the same meaning as(?: in)?|has (?:a|the) meaning affected by|includes)"
);
re!(
    DEF_COLON,
    r"(?im)^(\*\*\(\d+\)\*\*\s+)?([A-Za-z0-9\-'%*][A-Za-z0-9\-'%* ]{0,80}?):\s+(?:>|[A-Z]|see\s|a\s|an\s|the\s|this\s|that\s|it\s)"
);
re!(
    GENERAL_PREDICATE,
    r"(?i)([A-Za-z0-9\-'%*][A-Za-z0-9\-'%* ]{0,80}?)\s+(has the meaning given by|means|has the same meaning as(?: in)?|has (?:a|the) meaning affected by|includes)"
);
re!(SUB1, r#"(<a id="[^"]+"></a>\n)?\*\*\(1\)\*\*"#);
re!(NEXT_SUB, r"\n\*\*\(\d+\)\*\*");
re!(HEADING, r"(?m)^# .*\n");
re!(ANCHOR_ID, r#"<a id="([^"]+)""#);

const FALSE_STARTS: [&str; 25] = [
    "The ", "This ", "Note:", "Section ", "Division ", "Part ", "For ", "If ", "It ", "There ", "Subject ",
    "Without ", "Under ", "Over ", "From ", "With ", "By ", "At ", "In ", "On ", "To ", "Of ", "And ", "Or ",
    "But ",
];

static STOP_WORDS: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    "a an and or the to of in for on at by with from as is it its be been being have has had do does did \
     will would could should may might must shall can need dare ought used if then than that this these \
     those such so not no nor but yet however therefore thus hence when where why how what which who whom \
     whose all any both each every few more most other some only own same too very just also now here \
     there up out down off over under again further once about into through during before after above \
     below between among within without against"
        .split_whitespace()
        .collect()
});

fn is_false_positive(term: &str) -> bool {
    let t = py_strip(term);
    if t.chars().count() < 2 {
        return true;
    }
    if FALSE_STARTS.iter().any(|p| t.starts_with(p)) {
        return true;
    }
    let lowered = t.to_lowercase();
    if STOP_WORDS.contains(lowered.as_str()) {
        return true;
    }
    (lowered.contains("subsection") || lowered.contains(" section"))
        && lowered.split(py_is_space).any(|w| w == "section" || w == "subsection")
}

fn format_definition_terms(markdown: &str, section: &str, defs: &Defs) -> String {
    let (start, end) = if let Some(m) = SUB1.find(markdown) {
        let end = NEXT_SUB.find(&markdown[m.end()..]).map_or(markdown.len(), |n| m.end() + n.start());
        (m.start(), end)
    } else if section == "195-1" {
        (HEADING.find(markdown).map_or(0, |h| h.end()), markdown.len())
    } else {
        return markdown.to_string();
    };
    let sub1 = &markdown[start..end];
    let mut existing: HashSet<String> = ANCHOR_ID.captures_iter(markdown).map(|c| c[1].to_string()).collect();

    let mut anchor_for = |term: &str| -> String {
        let Some(info) = defs.get(&term.replace('*', "").to_lowercase()) else { return String::new() };
        if info.anchor.is_empty() || info.section != section || existing.contains(&info.anchor) {
            return String::new();
        }
        existing.insert(info.anchor.clone());
        format!("<a id=\"{}\"></a>", info.anchor)
    };
    let wrap = |term: &str, anchor: &str| {
        if term.contains('*') {
            format!("{anchor}{term}")
        } else {
            format!("{anchor}***{term}***")
        }
    };

    let result = DEF_PREDICATE.replace_all(sub1, |c: &Captures| {
        let prefix = c.get(1).map_or("", |m| m.as_str());
        let term = py_strip(&c[2]);
        if is_false_positive(&term.replace('*', "")) {
            return c[0].to_string();
        }
        let anchor = anchor_for(term);
        format!("{prefix}{} {}", wrap(term, &anchor), &c[3])
    });
    let mut result = DEF_COLON
        .replace_all(&result, |c: &Captures| {
            let term = py_strip(&c[2]);
            if is_false_positive(&term.replace('*', "")) {
                return c[0].to_string();
            }
            let anchor = anchor_for(term);
            c[0].replacen(term, &wrap(term, &anchor), 1)
        })
        .into_owned();

    // ITAA 1936 s 6 and the GST dictionary run definitions together on one line.
    if (section == "6" || section == "195-1") && !result.contains("***") {
        result = GENERAL_PREDICATE
            .replace_all(sub1, |c: &Captures| {
                let term = py_strip(&c[1]);
                if is_false_positive(&term.replace('*', "")) {
                    return c[0].to_string();
                }
                let anchor = anchor_for(term);
                format!("{} {}", wrap(term, &anchor), &c[2])
            })
            .into_owned();
    }
    format!("{}{}{}", &markdown[..start], result, &markdown[end..])
}

// ---------------------------------------------------------------------------
// link_definitions / auto_link_definitions
// ---------------------------------------------------------------------------

// Code spans, fenced blocks and existing links are passed through untouched.
re!(PROTECTED, r"(```[\s\S]*?```|`[^`]+`|\[[^\]]+\]\([^)]+\))");
// Python: `(?<!\*\*)\*(?!\s)([\w%][\w\s%()-]*)`. The look-behind is checked in the loop.
re!(STAR, r"\*([\w%][\w\s%()-]*)");

fn segments(markdown: &str) -> Vec<(bool, &str)> {
    let mut out = Vec::new();
    let mut last = 0;
    for m in PROTECTED.find_iter(markdown) {
        if m.start() > last {
            out.push((true, &markdown[last..m.start()]));
        }
        out.push((false, m.as_str()));
        last = m.end();
    }
    if last < markdown.len() {
        out.push((true, &markdown[last..]));
    }
    out
}

fn link_definitions(markdown: &str, act: &str, defs: &Defs) -> String {
    if defs.entries.is_empty() {
        return markdown.to_string();
    }
    let mut out = String::with_capacity(markdown.len() + markdown.len() / 4);
    for (is_text, seg) in segments(markdown) {
        if !is_text {
            out.push_str(seg);
            continue;
        }
        let (mut last, mut pos) = (0, 0);
        while let Some(c) = STAR.captures_at(seg, pos) {
            let m = c.get(0).expect("group 0");
            if seg[..m.start()].ends_with("**") {
                pos = m.start() + 1;
                continue;
            }
            out.push_str(&seg[last..m.start()]);
            last = m.end();
            pos = m.end();
            let candidate = &c[1];
            let words: Vec<&str> = candidate.split(py_is_space).filter(|w| !w.is_empty()).collect();
            let hit = (1..=words.len()).rev().find_map(|i| {
                let prefix = words[..i].join(" ");
                defs.get(&prefix.to_lowercase()).map(|info| (prefix, info))
            });
            match hit {
                Some((prefix, info)) => {
                    let remainder: String = candidate.chars().skip(prefix.chars().count()).collect();
                    let rest = &seg[m.end()..];
                    // A bare closing '*' (italic close) is taken into the link text.
                    let close = if rest.starts_with('*') && !rest[1..].starts_with('*') { "*" } else { "" };
                    out.push_str(&format!("[*{prefix}{close}](/{act}/s{}#{}){remainder}", info.section, info.anchor));
                }
                None => out.push_str(m.as_str()),
            }
        }
        out.push_str(&seg[last..]);
    }
    out
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Lower-cases without moving any byte offset, so a match in the folded text can be cut from
/// the original.
fn fold(s: &str) -> String {
    s.chars()
        .map(|c| {
            let mut l = c.to_lowercase();
            match (l.next(), l.next()) {
                (Some(x), None) if x.len_utf8() == c.len_utf8() => x,
                _ => c,
            }
        })
        .collect()
}

/// First `(?<!\w)term(?!\w)` match, case-insensitive.
fn find_term(folded: &str, term: &str) -> Option<usize> {
    let mut from = 0;
    while let Some(i) = folded[from..].find(term) {
        let at = from + i;
        let before = folded[..at].chars().next_back().is_some_and(is_word);
        let after = folded[at + term.len()..].chars().next().is_some_and(is_word);
        if !before && !after {
            return Some(at);
        }
        from = at + folded[at..].chars().next().map_or(1, char::len_utf8);
    }
    None
}

/// In non-dictionary sections, link the first occurrence of each defined term.
fn auto_link_definitions(markdown: &str, act: &str, section: &str, defs: &Defs) -> String {
    if matches!(section, "995-1" | "6" | "195-1") || defs.entries.is_empty() {
        return markdown.to_string();
    }
    let mut out = String::with_capacity(markdown.len() + markdown.len() / 4);
    let mut linked: HashSet<usize> = HashSet::new();
    for (is_text, seg) in segments(markdown) {
        if !is_text {
            out.push_str(seg);
            continue;
        }
        let mut current = seg.to_string();
        let mut folded = fold(seg);
        let mut placeholders: Vec<(String, String)> = Vec::new();
        for &ti in &defs.by_len {
            if linked.contains(&ti) {
                continue;
            }
            let (term, info) = &defs.entries[ti];
            if term.is_empty() {
                continue;
            }
            let Some(at) = find_term(&folded, term) else { continue };
            let found = &current[at..at + term.len()];
            let link = if !info.section.is_empty() && !info.anchor.is_empty() {
                format!("[*{found}*](/{act}/s{}#{})", info.section, info.anchor)
            } else {
                format!("[*{found}*]")
            };
            let placeholder = format!("__LINK_PH_{}__", placeholders.len());
            current.replace_range(at..at + term.len(), &placeholder);
            folded.replace_range(at..at + term.len(), &placeholder.to_lowercase());
            placeholders.push((placeholder, link));
            linked.insert(ti);
        }
        for (ph, link) in &placeholders {
            current = current.replace(ph, link);
        }
        out.push_str(&current);
    }
    out
}

// ---------------------------------------------------------------------------
// Reading and rendering a section
// ---------------------------------------------------------------------------

re!(FM_END, r"\n---\s*\n");

fn find_md(dir: &Path, rel: &Path, want: &str) -> Option<PathBuf> {
    let mut dirs = Vec::new();
    for e in fs::read_dir(dir.join(rel)).ok()?.flatten() {
        let name = e.file_name();
        let Some(name) = name.to_str() else { continue };
        let path = rel.join(name);
        match e.file_type() {
            Ok(t) if t.is_dir() => dirs.push(path),
            _ => {
                if name.strip_suffix(".md").is_some_and(|stem| stem.to_lowercase() == want) {
                    return Some(path);
                }
            }
        }
    }
    dirs.into_iter().find_map(|d| find_md(dir, &d, want))
}

/// Python's `str.splitlines()` boundaries.
fn is_line_break(c: char) -> bool {
    matches!(c, '\n' | '\r' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

pub struct Rendered {
    pub frontmatter: Map<String, Value>,
    pub body: String,
}

pub struct Sections {
    data: PathBuf,
    store: OnceLock<Map<String, Value>>,
    defs: Mutex<HashMap<String, Arc<Defs>>>,
    cache: Mutex<HashMap<(String, String), Arc<Rendered>>>,
}

/// Rendered sections kept in memory; the cache is dropped whole when it fills.
const CACHE_MAX: usize = 512;

impl Sections {
    pub fn new(data: PathBuf) -> Sections {
        Sections { data, store: OnceLock::new(), defs: Mutex::new(HashMap::new()), cache: Mutex::new(HashMap::new()) }
    }

    fn defs(&self, act: &str) -> Arc<Defs> {
        if let Some(d) = self.defs.lock().unwrap().get(act) {
            return d.clone();
        }
        // definitions_all.json, else the act-keyed definitions.json: read on first use.
        let store = self.store.get_or_init(|| {
            ["definitions_all.json", "definitions.json"]
                .iter()
                .find_map(|name| fs::read(self.data.join(name)).ok())
                .and_then(|b| serde_json::from_slice(&b).ok())
                .unwrap_or_default()
        });
        let d = Arc::new(build_defs(store, act));
        self.defs.lock().unwrap().insert(act.to_string(), d.clone());
        d
    }

    /// `get_act_section_content`: (frontmatter, body) of the section file.
    fn content(&self, trees: &Trees, act: &str, section: &str) -> Result<(Map<String, Value>, String), (u16, String)> {
        let tree = trees.get(act)?;
        let sections_dir = self.data.join(act).join("sections");
        let rel = match tree.section_path(section) {
            Some(p) => PathBuf::from(p),
            None => find_md(&sections_dir, Path::new(""), &section.to_lowercase())
                .ok_or_else(|| (404, format!("Section {section} not found")))?,
        };
        let Ok(bytes) = fs::read(sections_dir.join(rel)) else {
            return Ok((Map::new(), String::new()));
        };
        // Python reads in text mode: universal newlines, then ligatures.
        let text = String::from_utf8_lossy(&bytes).replace("\r\n", "\n").replace('\r', "\n");
        let content = normalise_text(&text);
        let mut fm = Map::new();
        let mut body: &str = &content;
        if content.starts_with("---") {
            if let Some(end) = FM_END.find(&content) {
                if end.start() >= 3 {
                    for line in py_strip(&content[3..end.start()]).split(is_line_break) {
                        if let Some((k, v)) = line.split_once(':') {
                            fm.insert(py_strip(k).to_string(), json!(py_strip(v).trim_matches('"')));
                        }
                    }
                }
                body = &content[end.end()..];
            }
        }
        Ok((fm, body.to_string()))
    }

    /// `_render_section`: the section as `/api/section` serves it.
    pub fn render(&self, trees: &Trees, act: &str, section: &str) -> Result<Arc<Rendered>, (u16, String)> {
        let key = (act.to_string(), section.to_string());
        if let Some(hit) = self.cache.lock().unwrap().get(&key) {
            return Ok(hit.clone());
        }
        let (frontmatter, body) = self.content(trees, act, section)?;
        let defs = self.defs(act);
        let mut body = strip_scraped_markup(&body);
        if matches!(section, "995-1" | "6" | "195-1") {
            body = format_definition_terms(&body, section, &defs);
        }
        body = link_definitions(&body, act, &defs);
        body = auto_link_definitions(&body, act, section, &defs);
        let rendered = Arc::new(Rendered { frontmatter, body });
        let mut cache = self.cache.lock().unwrap();
        if cache.len() >= CACHE_MAX {
            cache.clear();
        }
        cache.insert(key, rendered.clone());
        Ok(rendered)
    }
}

/// The hint every act's dictionary section carries (backend/routes/acts.py `_DICT_SECTIONS`).
pub fn dictionary_hint(act: &str, section: &str) -> Option<String> {
    let (label, reference) = match (act, section) {
        ("itaa-1997", "995-1") => ("the ITAA 1997", "s995-1"),
        ("itaa-1936", "317") => ("Part X (CFC measures) of the ITAA 1936", "s317"),
        ("itaa-1936", "6") => ("the ITAA 1936", "s6"),
        ("gst-1999", "195-1") => ("the GST Act", "s195-1"),
        ("fbt-1986", "136") => ("the FBT Act", "s136"),
        ("taa-1953", "2") => ("the TAA 1953", "s2"),
        ("sis-1993", "10") => ("the SIS Act", "s10"),
        ("aml-ctf-2006", "5") => ("the AML/CTF Act", "s5"),
        ("nz-it-2007", "YA-1") => ("the NZ IT Act 2007", "YA 1"),
        ("corporations-act-2001", "9") => ("the Corporations Act 2001", "s9"),
        _ => return None,
    };
    Some(format!(
        "This is an interpretation/definitions section for {label} ({reference}). Use the definition library: \
         /api/definitions/{act} (all terms) or /api/definition/{act}/TERM (single term) instead of reading the \
         full section text."
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn defs(pairs: &[(&str, &str, &str)]) -> Defs {
        let mut terms = Map::new();
        for (t, s, a) in pairs {
            terms.insert(t.to_string(), json!({"section": s, "anchor": a}));
        }
        let mut store = Map::new();
        store.insert("x".into(), json!({ "terms": terms }));
        build_defs(&store, "x")
    }

    #[test]
    fn keeps_paragraph_anchors_and_drops_other_tags() {
        assert_eq!(strip_scraped_markup("<a id=\"s1\"></a>\n<div>Text</div>"), "<a id=\"s1\"></a>\nText");
    }

    #[test]
    fn star_terms_become_links() {
        let d = defs(&[("capital gain", "995-1", "capital_gain")]);
        assert_eq!(
            link_definitions("a *capital gain made", "x", &d),
            "a [*capital gain](/x/s995-1#capital_gain) made"
        );
        assert_eq!(link_definitions("***capital gain*** x", "x", &d), "***capital gain*** x");
    }

    #[test]
    fn first_mention_is_linked_once() {
        let d = defs(&[("capital gain", "995-1", "cg"), ("gain", "995-1", "g")]);
        assert_eq!(
            auto_link_definitions("A Capital Gain is a gain; capital gain.", "x", "8-1", &d),
            "A [*Capital Gain*](/x/s995-1#cg) is a [*gain*](/x/s995-1#g); capital gain."
        );
    }

    #[test]
    fn canonical_section_ids() {
        assert_eq!(canon_section_id("6ab"), "6AB");
        assert_eq!(canon_section_id("995-1"), "995-1");
    }
}
