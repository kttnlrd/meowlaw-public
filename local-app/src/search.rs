//! Keyword search over the Python app's SQLite FTS5 index, opened in place and read-only.
//!
//! Only the legislation lane is served: `sections_fts` / `sections_meta`, as built by
//! `init_search_index` and scripts/rebuild_sections_fts.py and queried by `search_sections`
//! (backend/services/search_service.py). Embeddings, rulings, cases and the fused ranking are
//! not part of this build. With no index file, search answers empty and says why.

use regex::Regex;
use rusqlite::types::Value as Sql;
use rusqlite::{params_from_iter, Connection, OpenFlags};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};

pub const NO_INDEX_NOTE: &str =
    "No search index (search_index.db) was found next to the data folder, so full-text search is off.";

/// Tax-terminology synonyms, applied to unquoted query text only (`SYNONYMS`).
const SYNONYMS: &[(&str, &[&str])] = &[
    ("main residence", &["principal place of residence", "PPOR", "family home"]),
    ("cgt", &["capital gains tax", "capital gain"]),
    ("rollover", &["roll-over", "roll over"]),
    ("payroll", &["pay-roll"]),
    ("pay-roll", &["payroll"]),
    ("personal services income", &["PSI"]),
    ("psi", &["personal services income"]),
    ("input tax credit", &["ITC"]),
    ("imputation", &["franking", "dividend imputation"]),
    ("small business", &["SBE"]),
    ("employee share scheme", &["ESS"]),
    ("deceased estate", &["estate of a deceased person"]),
    ("trading stock", &["stock in trade", "inventory"]),
    ("superannuation", &["super", "SMSF"]),
    ("margin scheme", &["GST margin"]),
    ("non-commercial loss", &["hobby loss"]),
    ("foreign resident", &["non-resident", "nonresident"]),
    ("staking", &["staking rewards", "rewards for holding tokens", "holding tokens"]),
    ("ncp", &["non-cash payment", "non cash payment"]),
    ("afsl", &["australian financial services licence", "financial services licence"]),
    ("financial services licence", &["afsl", "australian financial services licence"]),
];

static SYNONYM_RE: LazyLock<Regex> = LazyLock::new(|| {
    let mut keys: Vec<&str> = SYNONYMS.iter().map(|(k, _)| *k).collect();
    keys.sort_by_key(|k| std::cmp::Reverse(k.len()));
    let alternatives: Vec<String> = keys.iter().map(|k| regex::escape(k)).collect();
    Regex::new(&format!(r"(?i)\b({})\b", alternatives.join("|"))).expect("synonym pattern")
});
static TOKEN_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#""([^"]*)"|(\S+)"#).expect("token pattern"));
static PHRASE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"^\s*"(.+)"\s*$"#).expect("phrase pattern"));
static SECTION_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(\d+[A-Za-z]*-\d+(?:[A-Za-z]*(?:\(\d+(?:\)[a-z])?\))?)?)$").expect("section pattern"));

fn fts_phrase(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

/// `_expand_query`: the FTS5 MATCH string. Quoted phrases pass through; recognised terms in
/// the free text become OR-groups with their synonyms.
pub fn expand_query(q: &str, operator: &str) -> String {
    let mut tokens: Vec<String> = Vec::new();
    for c in TOKEN_RE.captures_iter(q) {
        let phrase = c.get(1).map_or("", |m| m.as_str());
        let word = c.get(2).map_or("", |m| m.as_str());
        if !phrase.is_empty() {
            tokens.push(fts_phrase(phrase));
        } else if word.ends_with('*') && word.chars().count() > 1 {
            tokens.push(format!("{}*", fts_phrase(&word[..word.len() - 1])));
        } else {
            tokens.push(word.to_string());
        }
    }
    let mut expanded: Vec<String> = Vec::new();
    let mut covered: Vec<String> = Vec::new();
    let mut buf: Vec<String> = Vec::new();
    let mut flush = |buf: &mut Vec<String>, expanded: &mut Vec<String>| {
        if buf.is_empty() {
            return;
        }
        let text = buf.join(" ");
        buf.clear();
        let mut pos = 0;
        for m in SYNONYM_RE.find_iter(&text) {
            expanded.extend(text[pos..m.start()].split_whitespace().map(fts_phrase));
            let key = m.as_str().to_lowercase();
            let alts = SYNONYMS.iter().find(|(k, _)| *k == key).map_or(&[][..], |(_, a)| *a);
            let all: Vec<&str> = std::iter::once(m.as_str()).chain(alts.iter().copied()).collect();
            covered.extend(all.iter().map(|a| a.to_lowercase()));
            expanded.push(format!("({})", all.iter().map(|a| fts_phrase(a)).collect::<Vec<_>>().join(" OR ")));
            pos = m.end();
        }
        expanded.extend(text[pos..].split_whitespace().map(fts_phrase));
    };
    for tok in tokens {
        if tok.starts_with('"') || tok.starts_with('(') {
            flush(&mut buf, &mut expanded);
            expanded.push(tok);
        } else {
            buf.push(tok);
        }
    }
    flush(&mut buf, &mut expanded);
    expanded.retain(|t| t.starts_with('(') || !covered.contains(&t.trim_matches('"').to_lowercase()));
    expanded.join(if operator == "OR" { " OR " } else { " AND " })
}

pub struct Search {
    path: Option<PathBuf>,
    conn: Mutex<Option<Connection>>,
}

fn row(act: String, section: String, title: String, part: String, division: String, snippet: String) -> Value {
    json!({"act": act, "section": section, "title": title, "part": part, "division": division, "snippet": snippet})
}

impl Search {
    /// Looks for the index by name only; the file is opened on the first search.
    pub fn locate(data: &Path, explicit: Option<PathBuf>) -> Search {
        let candidates = [data.join("search_index.db"), data.join("..").join("search_index.db")];
        let path = explicit.or_else(|| candidates.into_iter().find(|p| p.is_file()));
        Search { path, conn: Mutex::new(None) }
    }

    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    fn with<T>(&self, f: impl FnOnce(&Connection) -> rusqlite::Result<T>) -> Option<T> {
        let path = self.path.as_ref()?;
        let mut guard = self.conn.lock().unwrap();
        if guard.is_none() {
            let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
            match Connection::open_with_flags(path, flags) {
                Ok(c) => *guard = Some(c),
                Err(e) => {
                    eprintln!("lawkitty-local: cannot open {}: {e}", path.display());
                    return None;
                }
            }
        }
        match f(guard.as_ref()?) {
            Ok(v) => Some(v),
            Err(e) => {
                eprintln!("lawkitty-local: search query failed: {e}");
                None
            }
        }
    }

    /// `search_sections`: BM25-ranked rows and the total match count. `acts` empty = every act.
    pub fn sections(&self, q: &str, acts: &[String], limit: usize, operator: &str) -> (Vec<Value>, i64) {
        let act_where = |col: &str| match acts.len() {
            0 => String::new(),
            1 => format!(" AND {col} = ?"),
            n => format!(" AND {col} IN ({})", vec!["?"; n].join(",")),
        };
        let act_params = || acts.iter().map(|a| Sql::Text(a.clone()));
        let read = |r: &rusqlite::Row| -> rusqlite::Result<Value> {
            let text = |i: usize| r.get::<_, Option<String>>(i).map(Option::unwrap_or_default);
            Ok(row(text(0)?, text(1)?, text(2)?, text(3)?, text(4)?, text(5)?))
        };

        // A fully quoted phrase is matched literally, bypassing stemming.
        if let Some(c) = PHRASE_RE.captures(q) {
            let like = Sql::Text(format!("%{}%", &c[1]));
            let found = self.with(|conn| {
                let sql = format!(
                    "SELECT s.act, s.section, s.title, m.part, m.division, '' FROM sections_fts s \
                     JOIN sections_meta m ON s.act = m.act AND s.section = m.section \
                     WHERE (s.content LIKE ? OR s.title LIKE ?){} \
                     ORDER BY CASE WHEN s.title LIKE ? THEN 0 ELSE 1 END LIMIT ?",
                    act_where("s.act")
                );
                let params: Vec<Sql> = [like.clone(), like.clone()]
                    .into_iter()
                    .chain(act_params())
                    .chain([like.clone(), Sql::Integer(limit as i64)])
                    .collect();
                let rows = conn.prepare(&sql)?.query_map(params_from_iter(params), read)?.collect::<Result<Vec<_>, _>>()?;
                let total: i64 = conn.query_row(
                    &format!("SELECT COUNT(*) FROM sections_fts WHERE (content LIKE ? OR title LIKE ?){}", act_where("act")),
                    params_from_iter([like.clone(), like.clone()].into_iter().chain(act_params())),
                    |r| r.get(0),
                )?;
                Ok((rows, total))
            });
            return found.unwrap_or_default();
        }

        let matcher = expand_query(q, operator);
        if q.trim().is_empty() || matcher.is_empty() {
            return (Vec::new(), 0);
        }
        let found = self.with(|conn| {
            let total: i64 = conn.query_row(
                &format!("SELECT COUNT(*) FROM sections_fts WHERE sections_fts MATCH ?{}", act_where("sections_fts.act")),
                params_from_iter(std::iter::once(Sql::Text(matcher.clone())).chain(act_params())),
                |r| r.get(0),
            )?;
            let sql = format!(
                "SELECT sections_fts.act, sections_fts.section, sections_fts.title, m.part, m.division, \
                 snippet(sections_fts, 3, '<mark>', '</mark>', '...', 32) \
                 FROM sections_fts JOIN sections_meta m ON sections_fts.act = m.act AND sections_fts.section = m.section \
                 WHERE sections_fts MATCH ?{} ORDER BY bm25(sections_fts, 0.0, 1.0, 10.0, 1.0) LIMIT ?",
                act_where("sections_fts.act")
            );
            let params: Vec<Sql> = std::iter::once(Sql::Text(matcher.clone()))
                .chain(act_params())
                .chain([Sql::Integer(limit as i64)])
                .collect();
            let mut rows = conn.prepare(&sql)?.query_map(params_from_iter(params), read)?.collect::<Result<Vec<_>, _>>()?;

            // A query that is a section number puts that section first.
            if let Some(c) = SECTION_RE.captures(q.trim()) {
                let sql = format!(
                    "SELECT act, section, sections_fts.title, part, division, \
                     snippet(sections_fts, 3, '<mark>', '</mark>', '...', 32) \
                     FROM sections_fts JOIN sections_meta m USING(act, section) WHERE sections_fts.section = ?{}",
                    act_where("sections_fts.act")
                );
                let params: Vec<Sql> = std::iter::once(Sql::Text(c[1].to_string())).chain(act_params()).collect();
                if let Some(exact) = conn.prepare(&sql)?.query_map(params_from_iter(params), read)?.next().transpose()? {
                    rows.retain(|r| !(r["act"] == exact["act"] && r["section"] == exact["section"]));
                    rows.insert(0, exact);
                    rows.truncate(limit);
                }
            }
            Ok((rows, total))
        });
        found.unwrap_or_default()
    }

    /// The section whose id is exactly `q` in one act (`sections_meta`).
    pub fn exact(&self, act: &str, q: &str) -> Option<Value> {
        self.with(|conn| {
            conn.prepare("SELECT act, section, title, part, division FROM sections_meta WHERE act = ? AND section = ?")?
                .query_map([act, q], |r| {
                    let text = |i: usize| r.get::<_, Option<String>>(i).map(Option::unwrap_or_default);
                    Ok(json!({"act": text(0)?, "section": text(1)?, "title": text(2)?, "part": text(3)?,
                              "division": text(4)?, "exact_match": true}))
                })?
                .next()
                .transpose()
        })
        .flatten()
    }

    /// `/api/search/suggest`: prefix matches on sections, in rank order.
    pub fn suggest(&self, q: &str, limit: usize) -> Vec<Value> {
        let prefix = format!("{}*", fts_phrase(q));
        self.with(|conn| {
            conn.prepare("SELECT act, section, title FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?")?
                .query_map((prefix, limit as i64), |r| {
                    let text = |i: usize| r.get::<_, Option<String>>(i).map(Option::unwrap_or_default);
                    Ok(json!({"act": text(0)?, "section": text(1)?, "title": text(2)?, "type": "section"}))
                })?
                .collect()
        })
        .unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expands_synonyms_and_keeps_phrases() {
        assert_eq!(expand_query("deduction", "AND"), "\"deduction\"");
        assert_eq!(expand_query("\"distributable surplus\" loan", "AND"), "\"distributable surplus\" AND \"loan\"");
        assert_eq!(expand_query("cgt event", "OR"), "(\"cgt\" OR \"capital gains tax\" OR \"capital gain\") OR \"event\"");
        assert_eq!(expand_query("employee share scheme ESS", "AND"), "(\"employee share scheme\" OR \"ESS\")");
        assert_eq!(expand_query("deduct*", "AND"), "\"deduct\"*");
    }

    #[test]
    fn reads_an_fts_index_in_place() {
        let dir = std::env::temp_dir().join(format!("lawkitty-local-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("search_index.db");
        let _ = std::fs::remove_file(&db);
        let conn = Connection::open(&db).unwrap();
        conn.execute_batch(
            "CREATE VIRTUAL TABLE sections_fts USING fts5(act, section, title, content, tokenize='porter');
             CREATE TABLE sections_meta (act TEXT, section TEXT, title TEXT, part TEXT, division TEXT, UNIQUE (act, section));
             INSERT INTO sections_fts VALUES ('itaa-1997', '8-1', 'General deductions', 'You can deduct from your assessable income any loss or outgoing');
             INSERT INTO sections_meta VALUES ('itaa-1997', '8-1', 'General deductions', '2-1', '8');
             INSERT INTO sections_fts VALUES ('gst-1999', '9-5', 'Taxable supplies', 'You make a taxable supply if you make the supply for consideration');
             INSERT INTO sections_meta VALUES ('gst-1999', '9-5', 'Taxable supplies', '2-2', '9');",
        )
        .unwrap();
        drop(conn);
        let s = Search::locate(&dir, None);
        let (rows, total) = s.sections("deductions", &[], 10, "AND");
        assert_eq!(total, 1);
        assert_eq!(rows[0]["section"], "8-1");
        assert_eq!(rows[0]["part"], "2-1");
        assert!(rows[0]["snippet"].as_str().unwrap().contains("<mark>deduct</mark>"));
        assert_eq!(s.sections("supply", &["itaa-1997".to_string()], 10, "AND").1, 0);
        assert_eq!(s.sections("\"taxable supply\"", &[], 10, "AND").0[0]["section"], "9-5");
        assert_eq!(s.sections("8-1", &[], 10, "AND").0[0]["section"], "8-1");
        assert_eq!(s.exact("itaa-1997", "8-1").unwrap()["exact_match"], true);
        assert_eq!(s.suggest("taxab", 5)[0]["section"], "9-5");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
