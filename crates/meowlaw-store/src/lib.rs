//! SQLite store for corpora, nodes, references, definitions and the graph.
//!
//! One file (`meowlaw.db`). Connections are opened per operation (SQLite opens
//! are cheap and this keeps the store `Send + Sync` without a pool).

pub mod graph;

use anyhow::{Context, Result};
use meowlaw_core::{
    Corpus, CorpusKind, Definition, Document, Edge, EdgeKind, Method, Node, NodeKind, Reference,
};
use rusqlite::{Connection, OptionalExtension, Row, params};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

const SCHEMA: &str = r#"
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS corpora (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  jurisdiction TEXT,
  profile TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  citations TEXT NOT NULL DEFAULT '[]',
  source_url TEXT,
  as_at TEXT,
  licence TEXT,
  meta TEXT NOT NULL DEFAULT '{}',
  ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  corpus TEXT NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  label TEXT,
  number TEXT,
  title TEXT,
  text TEXT NOT NULL DEFAULT '',
  parent TEXT,
  ordinal INTEGER NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nodes_corpus ON nodes(corpus, seq);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent, ordinal);
CREATE TABLE IF NOT EXISTS edges (
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  kind TEXT NOT NULL,
  method TEXT NOT NULL,
  evidence TEXT,
  weight REAL NOT NULL DEFAULT 1.0,
  UNIQUE(source, target, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source, kind);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target, kind);
CREATE TABLE IF NOT EXISTS refs (
  source TEXT NOT NULL,
  raw TEXT NOT NULL,
  start INTEGER NOT NULL,
  end INTEGER NOT NULL,
  pattern TEXT NOT NULL,
  target TEXT,
  pin TEXT
);
CREATE INDEX IF NOT EXISTS idx_refs_source ON refs(source);
CREATE INDEX IF NOT EXISTS idx_refs_target ON refs(target);
CREATE TABLE IF NOT EXISTS definitions (
  id TEXT PRIMARY KEY,
  term TEXT NOT NULL,
  term_key TEXT NOT NULL,
  source TEXT NOT NULL,
  scope TEXT NOT NULL,
  corpus TEXT NOT NULL,
  text TEXT NOT NULL,
  pattern TEXT NOT NULL,
  points_to TEXT
);
CREATE INDEX IF NOT EXISTS idx_definitions_term ON definitions(term_key);
CREATE INDEX IF NOT EXISTS idx_definitions_corpus ON definitions(corpus);
CREATE TABLE IF NOT EXISTS embeddings (
  node_id TEXT NOT NULL,
  chunk INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  PRIMARY KEY (node_id, chunk, model)
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"#;

/// Cheap to clone; clones share a small pool of open connections.
#[derive(Debug, Clone)]
pub struct Store {
    path: PathBuf,
    pool: Arc<Mutex<Vec<Connection>>>,
}

/// Idle connections kept per store.
const POOL_SIZE: usize = 8;

/// A pooled connection; returned to the pool on drop. Statements prepared with
/// `prepare_cached` survive across uses.
pub struct Conn {
    c: Option<Connection>,
    pool: Arc<Mutex<Vec<Connection>>>,
}

impl std::ops::Deref for Conn {
    type Target = Connection;
    fn deref(&self) -> &Connection {
        self.c.as_ref().expect("live connection")
    }
}

impl std::ops::DerefMut for Conn {
    fn deref_mut(&mut self) -> &mut Connection {
        self.c.as_mut().expect("live connection")
    }
}

impl Drop for Conn {
    fn drop(&mut self) {
        if let Some(c) = self.c.take()
            && let Ok(mut p) = self.pool.lock()
            && p.len() < POOL_SIZE
        {
            p.push(c);
        }
    }
}

/// Corpus id of a node or definition id.
pub fn corpus_of(id: &str) -> &str {
    let id = id.strip_prefix("def:").unwrap_or(id);
    id.split(':').next().unwrap_or(id)
}

#[derive(Debug, Clone, Serialize)]
pub struct CorpusSummary {
    #[serde(flatten)]
    pub corpus: Corpus,
    pub nodes: i64,
    pub definitions: i64,
    pub references_out: i64,
    pub unresolved_out: i64,
}

/// What re-ingesting a document would change.
#[derive(Debug, Clone, Default, Serialize)]
pub struct DocDiff {
    pub corpus: String,
    pub new_corpus: bool,
    pub metadata_changed: bool,
    pub added: Vec<String>,
    pub removed: Vec<String>,
    pub changed: Vec<String>,
    pub unchanged: usize,
    /// Citations in other sources that point at removed nodes.
    pub breaks: Vec<BrokenLink>,
}

impl DocDiff {
    pub fn is_empty(&self) -> bool {
        !self.new_corpus
            && !self.metadata_changed
            && self.added.is_empty()
            && self.removed.is_empty()
            && self.changed.is_empty()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct BrokenLink {
    pub source: String,
    pub raw: String,
    pub target: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Stats {
    pub corpora: i64,
    pub nodes: i64,
    pub edges: i64,
    pub references: i64,
    pub unresolved: i64,
    pub definitions: i64,
    pub embeddings: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct UnresolvedGroup {
    pub pattern: String,
    pub raw: String,
    pub count: i64,
    pub example_source: String,
}

impl Store {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).ok();
        }
        let s = Self {
            path,
            pool: Arc::new(Mutex::new(Vec::new())),
        };
        let c = s.conn()?;
        migrate(&c)?;
        c.execute_batch(SCHEMA).context("creating schema")?;
        Ok(s)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn conn(&self) -> Result<Conn> {
        if let Some(c) = self.pool.lock().ok().and_then(|mut p| p.pop()) {
            return Ok(Conn {
                c: Some(c),
                pool: self.pool.clone(),
            });
        }
        let c = Connection::open(&self.path)
            .with_context(|| format!("opening {}", self.path.display()))?;
        c.execute_batch("PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;")?;
        c.set_prepared_statement_cache_capacity(64);
        Ok(Conn {
            c: Some(c),
            pool: self.pool.clone(),
        })
    }

    // ------------------------------------------------------------------ write

    /// Insert or replace a corpus and all its nodes.
    pub fn put_document(&self, doc: &Document) -> Result<()> {
        let mut c = self.conn()?;
        let tx = c.transaction()?;
        let cp = &doc.corpus;
        // Vectors and similarity edges of nodes whose text changed or that are
        // gone are stale: drop them so `embed` recomputes them.
        let old: std::collections::HashMap<String, String> = {
            let mut st = tx.prepare("SELECT id, text FROM nodes WHERE corpus = ?1")?;
            st.query_map([&cp.id], |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?
        };
        let new: std::collections::HashMap<&str, &str> = doc
            .nodes
            .iter()
            .map(|n| (n.id.as_str(), n.text.as_str()))
            .collect();
        {
            let mut del_emb = tx.prepare("DELETE FROM embeddings WHERE node_id = ?1")?;
            let mut del_sim = tx.prepare(
                "DELETE FROM edges WHERE kind = 'similar' AND (source = ?1 OR target = ?1)",
            )?;
            for (id, text) in &old {
                if new.get(id.as_str()) != Some(&text.as_str()) {
                    del_emb.execute([id])?;
                    del_sim.execute([id])?;
                    // The root's vector is built from the summary.
                    if id.ends_with(":summary") {
                        del_emb.execute([&cp.id])?;
                        del_sim.execute([&cp.id])?;
                    }
                }
            }
            // A summary that is new this time also invalidates the root vector.
            if new
                .keys()
                .any(|k| k.ends_with(":summary") && !old.contains_key(*k))
            {
                del_emb.execute([&cp.id])?;
                del_sim.execute([&cp.id])?;
            }
        }
        tx.execute("DELETE FROM nodes WHERE corpus = ?1", [&cp.id])?;
        tx.execute(
            "INSERT OR REPLACE INTO corpora (id, title, kind, jurisdiction, profile, aliases, citations, source_url, as_at, licence, meta)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                cp.id,
                cp.title,
                cp.kind.as_str(),
                cp.jurisdiction,
                cp.profile,
                serde_json::to_string(&cp.aliases)?,
                serde_json::to_string(&cp.citations)?,
                cp.source_url,
                cp.as_at,
                cp.licence,
                serde_json::to_string(&cp.meta)?,
            ],
        )?;
        {
            let mut st = tx.prepare(
                "INSERT INTO nodes (id, corpus, kind, label, number, title, text, parent, ordinal, seq, meta)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            )?;
            for (seq, n) in doc.nodes.iter().enumerate() {
                st.execute(params![
                    n.id,
                    n.corpus,
                    n.kind.as_str(),
                    n.label,
                    n.number,
                    n.title,
                    n.text,
                    n.parent,
                    n.ordinal,
                    seq as i64,
                    serde_json::to_string(&n.meta)?,
                ])?;
            }
        }
        tx.commit()?;
        drop(c);
        self.bump_generation()?;
        Ok(())
    }

    /// Compare a document with what is stored under the same corpus id.
    pub fn diff_document(&self, doc: &Document) -> Result<DocDiff> {
        let c = self.conn()?;
        let mut st = c.prepare_cached("SELECT id, text, title FROM nodes WHERE corpus = ?1")?;
        let old: std::collections::HashMap<String, (String, Option<String>)> = st
            .query_map([&doc.corpus.id], |r| {
                Ok((r.get(0)?, (r.get(1)?, r.get(2)?)))
            })?
            .collect::<rusqlite::Result<_>>()?;
        let existed = self.corpus(&doc.corpus.id)?;
        let mut d = DocDiff {
            corpus: doc.corpus.id.clone(),
            new_corpus: existed.is_none(),
            metadata_changed: existed.as_ref().is_some_and(|c| c != &doc.corpus),
            ..Default::default()
        };
        let mut seen = std::collections::HashSet::new();
        for n in &doc.nodes {
            seen.insert(n.id.as_str());
            match old.get(&n.id) {
                None => d.added.push(n.id.clone()),
                Some((text, title)) if text != &n.text || title != &n.title => {
                    d.changed.push(n.id.clone())
                }
                Some(_) => d.unchanged += 1,
            }
        }
        d.removed = old
            .keys()
            .filter(|k| !seen.contains(k.as_str()))
            .cloned()
            .collect();
        d.removed.sort();
        // Citations elsewhere that point at nodes about to disappear.
        let mut st = c.prepare_cached(
            "SELECT source, raw FROM refs WHERE target = ?1 AND substr(source, 1, length(?2) + 1) != ?2 || ':' AND source != ?2",
        )?;
        for id in &d.removed {
            for row in st.query_map(params![id, doc.corpus.id], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })? {
                let (src, raw) = row?;
                d.breaks.push(BrokenLink {
                    source: src,
                    raw,
                    target: id.clone(),
                });
            }
        }
        Ok(d)
    }

    /// Remove a corpus and everything derived from it, in one transaction.
    pub fn delete_corpus(&self, id: &str) -> Result<bool> {
        let mut c = self.conn()?;
        let tx = c.transaction()?;
        // Ids of this corpus: `<id>`, `<id>:...`, `def:<id>:...`.
        let own = |col: &str| {
            format!(
                "({col} = ?1 OR substr({col}, 1, length(?1) + 1) = ?1 || ':' \
                 OR substr({col}, 1, length(?1) + 5) = 'def:' || ?1 || ':')"
            )
        };
        tx.execute(
            &format!("DELETE FROM embeddings WHERE {}", own("node_id")),
            [id],
        )?;
        tx.execute(
            &format!(
                "DELETE FROM edges WHERE {} OR {}",
                own("source"),
                own("target")
            ),
            [id],
        )?;
        tx.execute(&format!("DELETE FROM refs WHERE {}", own("source")), [id])?;
        tx.execute("DELETE FROM nodes WHERE corpus = ?1", [id])?;
        tx.execute("DELETE FROM definitions WHERE corpus = ?1", [id])?;
        let n = tx.execute("DELETE FROM corpora WHERE id = ?1", [id])?;
        tx.commit()?;
        drop(c);
        self.bump_generation()?;
        Ok(n > 0)
    }

    /// Replace all extracted data (references, definitions, edges).
    /// Extraction always runs over the whole store so that a newly ingested
    /// corpus resolves references that were previously dangling.
    pub fn replace_extraction(
        &self,
        refs: &[Reference],
        defs: &[Definition],
        edges: &[Edge],
    ) -> Result<()> {
        let mut c = self.conn()?;
        let tx = c.transaction()?;
        tx.execute_batch(
            "DELETE FROM refs; DELETE FROM definitions; DELETE FROM edges WHERE method IN ('structure', 'pattern', 'alias');",
        )?;
        {
            let mut st = tx.prepare(
                "INSERT INTO refs (source, raw, start, end, pattern, target, pin) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            )?;
            for r in refs {
                st.execute(params![
                    r.source,
                    r.raw,
                    r.start as i64,
                    r.end as i64,
                    r.pattern,
                    r.target,
                    r.pin
                ])?;
            }
            let mut st = tx.prepare(
                "INSERT OR REPLACE INTO definitions (id, term, term_key, source, scope, corpus, text, pattern, points_to)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            )?;
            for d in defs {
                st.execute(params![
                    d.id,
                    d.term,
                    term_key(&d.term),
                    d.source,
                    d.scope,
                    d.corpus,
                    d.text,
                    d.pattern,
                    d.points_to
                ])?;
            }
            let mut st = tx.prepare(
                "INSERT OR IGNORE INTO edges (source, target, kind, method, evidence, weight) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            )?;
            for e in edges {
                st.execute(params![
                    e.source,
                    e.target,
                    e.kind.as_str(),
                    e.method.as_str(),
                    e.evidence,
                    e.weight
                ])?;
            }
        }
        tx.commit()?;
        drop(c);
        self.bump_generation()?;
        Ok(())
    }

    /// Add a manually asserted edge (survives re-extraction).
    pub fn add_manual_edge(
        &self,
        source: &str,
        target: &str,
        kind: EdgeKind,
        note: Option<&str>,
    ) -> Result<()> {
        self.conn()?.execute(
            "INSERT OR REPLACE INTO edges (source, target, kind, method, evidence) VALUES (?1, ?2, ?3, 'manual', ?4)",
            params![source, target, kind.as_str(), note],
        )?;
        Ok(())
    }

    /// Data generation: bumped by every write that changes what readers see,
    /// so a long-running server can notice another process's ingest or rebuild.
    pub fn generation(&self) -> Result<u64> {
        Ok(self
            .get_kv("generation")?
            .and_then(|v| v.parse().ok())
            .unwrap_or(0))
    }

    pub fn bump_generation(&self) -> Result<()> {
        self.conn()?.execute(
            "INSERT INTO kv (key, value) VALUES ('generation', '1')
             ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
            [],
        )?;
        Ok(())
    }

    pub fn set_kv(&self, key: &str, value: &str) -> Result<()> {
        self.conn()?.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?1, ?2)",
            params![key, value],
        )?;
        Ok(())
    }

    pub fn get_kv(&self, key: &str) -> Result<Option<String>> {
        Ok(self
            .conn()?
            .query_row("SELECT value FROM kv WHERE key = ?1", [key], |r| r.get(0))
            .optional()?)
    }

    // ------------------------------------------------------------------- read

    pub fn corpora(&self) -> Result<Vec<Corpus>> {
        let c = self.conn()?;
        let mut st = c.prepare_cached(&format!(
            "SELECT {CORPUS_COLS} FROM corpora ORDER BY kind, title"
        ))?;
        let rows = st
            .query_map([], corpus_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    pub fn corpus(&self, id: &str) -> Result<Option<Corpus>> {
        let c = self.conn()?;
        Ok(c.query_row(
            &format!("SELECT {CORPUS_COLS} FROM corpora WHERE id = ?1"),
            [id],
            corpus_row,
        )
        .optional()?)
    }

    pub fn corpus_summaries(&self) -> Result<Vec<CorpusSummary>> {
        let corpora = self.corpora()?;
        let c = self.conn()?;
        let mut out = Vec::new();
        for cp in corpora {
            let nodes: i64 = c.query_row(
                "SELECT COUNT(*) FROM nodes WHERE corpus = ?1",
                [&cp.id],
                |r| r.get(0),
            )?;
            let definitions: i64 = c.query_row(
                "SELECT COUNT(*) FROM definitions WHERE corpus = ?1",
                [&cp.id],
                |r| r.get(0),
            )?;
            let (references_out, unresolved_out): (i64, i64) = c.query_row(
                "SELECT COUNT(*), COALESCE(SUM(target IS NULL), 0) FROM refs r JOIN nodes n ON n.id = r.source WHERE n.corpus = ?1",
                [&cp.id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            out.push(CorpusSummary {
                corpus: cp,
                nodes,
                definitions,
                references_out,
                unresolved_out,
            });
        }
        Ok(out)
    }

    pub fn stats(&self) -> Result<Stats> {
        let c = self.conn()?;
        let q = |sql: &str| -> Result<i64> { Ok(c.query_row(sql, [], |r| r.get(0))?) };
        Ok(Stats {
            corpora: q("SELECT COUNT(*) FROM corpora")?,
            nodes: q("SELECT COUNT(*) FROM nodes")?,
            edges: q("SELECT COUNT(*) FROM edges")?,
            references: q("SELECT COUNT(*) FROM refs")?,
            unresolved: q("SELECT COUNT(*) FROM refs WHERE target IS NULL")?,
            definitions: q("SELECT COUNT(*) FROM definitions")?,
            embeddings: q("SELECT COUNT(*) FROM embeddings")?,
        })
    }

    pub fn node(&self, id: &str) -> Result<Option<Node>> {
        let c = self.conn()?;
        Ok(c.query_row(
            &format!("SELECT {NODE_COLS} FROM nodes WHERE id = ?1"),
            [id],
            node_row,
        )
        .optional()?)
    }

    pub fn nodes(&self, ids: &[String]) -> Result<Vec<Node>> {
        let mut out = Vec::with_capacity(ids.len());
        let c = self.conn()?;
        let mut st = c.prepare_cached(&format!("SELECT {NODE_COLS} FROM nodes WHERE id = ?1"))?;
        for id in ids {
            if let Some(n) = st.query_row([id], node_row).optional()? {
                out.push(n);
            }
        }
        Ok(out)
    }

    pub fn children(&self, id: &str) -> Result<Vec<Node>> {
        let c = self.conn()?;
        let mut st = c.prepare_cached(&format!(
            "SELECT {NODE_COLS} FROM nodes WHERE parent = ?1 ORDER BY ordinal, seq"
        ))?;
        let rows = st
            .query_map([id], node_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Ancestors from the root down to (excluding) `id`.
    pub fn ancestors(&self, id: &str) -> Result<Vec<Node>> {
        let c = self.conn()?;
        let cols = NODE_COLS
            .split(", ")
            .map(|c| format!("n.{c}"))
            .collect::<Vec<_>>()
            .join(", ");
        let mut st = c.prepare_cached(&format!(
            "WITH RECURSIVE a(id, parent, depth) AS (
               SELECT id, parent, 0 FROM nodes WHERE id = ?1
               UNION ALL
               SELECT p.id, p.parent, a.depth + 1 FROM nodes p JOIN a ON p.id = a.parent
               WHERE a.depth < 64)
             SELECT {cols} FROM a JOIN nodes n ON n.id = a.id WHERE a.depth > 0 ORDER BY a.depth DESC"
        ))?;
        let rows = st
            .query_map([id], node_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// The navigation tree of a corpus as nested JSON (no text): what the UI
    /// drawer renders. Paragraphs and the summary are left out; they are shown
    /// inline in the reader.
    pub fn corpus_tree(&self, corpus: &str) -> Result<Option<serde_json::Value>> {
        use serde_json::{Value, json};
        let Some(cp) = self.corpus(corpus)? else {
            return Ok(None);
        };
        let c = self.conn()?;
        let mut st = c.prepare_cached(
            "SELECT id, parent, kind, label, number, title FROM nodes
             WHERE corpus = ?1 AND kind NOT IN ('paragraph', 'summary') ORDER BY seq",
        )?;
        type Row = (
            String,
            Option<String>,
            String,
            Option<String>,
            Option<String>,
            Option<String>,
        );
        let rows: Vec<Row> = st
            .query_map([corpus], |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                ))
            })?
            .collect::<rusqlite::Result<_>>()?;
        let mut children: std::collections::HashMap<String, Vec<usize>> = Default::default();
        for (i, r) in rows.iter().enumerate() {
            if let Some(p) = &r.1 {
                children.entry(p.clone()).or_default().push(i);
            }
        }
        fn build(
            i: usize,
            rows: &[Row],
            children: &std::collections::HashMap<String, Vec<usize>>,
            depth: usize,
        ) -> Value {
            let (id, _, kind, label, number, title) = &rows[i];
            let mut v = json!({ "id": id, "kind": kind });
            if let Some(l) = label {
                v["label"] = json!(l);
            }
            if let Some(n) = number {
                v["number"] = json!(n);
            }
            if let Some(t) = title {
                v["title"] = json!(t);
            }
            if depth < 32
                && let Some(kids) = children.get(id)
            {
                v["children"] = Value::Array(
                    kids.iter()
                        .map(|&k| build(k, rows, children, depth + 1))
                        .collect(),
                );
            }
            v
        }
        let root = rows.iter().position(|r| r.1.is_none()).unwrap_or(0);
        if rows.is_empty() {
            return Ok(Some(
                json!({ "corpus": cp.id, "title": cp.title, "children": [] }),
            ));
        }
        let tree = build(root, &rows, &children, 0);
        Ok(Some(json!({
            "corpus": cp.id,
            "title": cp.title,
            "kind": cp.kind,
            "children": tree.get("children").cloned().unwrap_or(json!([])),
        })))
    }

    pub fn corpus_nodes(&self, corpus: &str) -> Result<Vec<Node>> {
        let c = self.conn()?;
        let mut st = c.prepare_cached(&format!(
            "SELECT {NODE_COLS} FROM nodes WHERE corpus = ?1 ORDER BY seq"
        ))?;
        let rows = st
            .query_map([corpus], node_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Every document in the store, for extraction and indexing.
    pub fn documents(&self) -> Result<Vec<Document>> {
        let mut out = Vec::new();
        for corpus in self.corpora()? {
            let nodes = self.corpus_nodes(&corpus.id)?;
            out.push(Document { corpus, nodes });
        }
        Ok(out)
    }

    pub fn refs_from(&self, id: &str) -> Result<Vec<Reference>> {
        self.refs_where("source = ?1", id)
    }

    pub fn refs_to(&self, id: &str) -> Result<Vec<Reference>> {
        self.refs_where("target = ?1", id)
    }

    fn refs_where(&self, cond: &str, id: &str) -> Result<Vec<Reference>> {
        let c = self.conn()?;
        let mut st = c.prepare_cached(&format!(
            "SELECT source, raw, start, end, pattern, target, pin FROM refs WHERE {cond} ORDER BY source, start"
        ))?;
        let rows = st
            .query_map([id], |r| {
                Ok(Reference {
                    source: r.get(0)?,
                    raw: r.get(1)?,
                    start: r.get::<_, i64>(2)? as usize,
                    end: r.get::<_, i64>(3)? as usize,
                    pattern: r.get(4)?,
                    target: r.get(5)?,
                    pin: r.get(6)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Unresolved references grouped by citation text, most frequent first.
    /// This is the main data-quality report: each row is a missing corpus or a
    /// pattern that needs tuning.
    pub fn unresolved(&self, corpus: Option<&str>, limit: usize) -> Result<Vec<UnresolvedGroup>> {
        let c = self.conn()?;
        let sql = "SELECT r.pattern, r.raw, COUNT(*) AS n, MIN(r.source)
                   FROM refs r JOIN nodes nd ON nd.id = r.source
                   WHERE r.target IS NULL AND (?1 IS NULL OR nd.corpus = ?1)
                   GROUP BY r.pattern, r.raw ORDER BY n DESC LIMIT ?2";
        let mut st = c.prepare(sql)?;
        let rows = st
            .query_map(params![corpus, limit as i64], |r| {
                Ok(UnresolvedGroup {
                    pattern: r.get(0)?,
                    raw: r.get(1)?,
                    count: r.get(2)?,
                    example_source: r.get(3)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    pub fn definition(&self, id: &str) -> Result<Option<Definition>> {
        let c = self.conn()?;
        Ok(c.query_row(
            &format!("SELECT {DEF_COLS} FROM definitions WHERE id = ?1"),
            [id],
            def_row,
        )
        .optional()?)
    }

    /// Definitions of a term. Exact match first, then prefix matches.
    pub fn definitions_for(
        &self,
        term: &str,
        corpus: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Definition>> {
        let c = self.conn()?;
        let key = term_key(term);
        let mut st = c.prepare_cached(&format!(
            "SELECT {DEF_COLS} FROM definitions
             WHERE (term_key = ?1 OR term_key LIKE ?2 ESCAPE '\\') AND (?3 IS NULL OR corpus = ?3)
             ORDER BY term_key != ?1, length(term_key), corpus LIMIT ?4"
        ))?;
        let like = format!("{}%", key.replace('%', "\\%").replace('_', "\\_"));
        let rows = st
            .query_map(params![key, like, corpus, limit as i64], def_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    pub fn definitions_in(&self, corpus: &str) -> Result<Vec<Definition>> {
        let c = self.conn()?;
        let mut st = c.prepare_cached(&format!(
            "SELECT {DEF_COLS} FROM definitions WHERE corpus = ?1 ORDER BY term_key"
        ))?;
        let rows = st
            .query_map([corpus], def_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Definitions whose scope covers `node_id` (scope is the node or an ancestor).
    pub fn definitions_applying(&self, node_id: &str) -> Result<Vec<Definition>> {
        let mut scopes: Vec<String> = self.ancestors(node_id)?.into_iter().map(|n| n.id).collect();
        scopes.push(node_id.to_string());
        let c = self.conn()?;
        let mut st = c.prepare_cached(&format!(
            "SELECT {DEF_COLS} FROM definitions WHERE scope = ?1 ORDER BY term_key"
        ))?;
        let mut out = Vec::new();
        for s in scopes {
            out.extend(
                st.query_map([s], def_row)?
                    .collect::<rusqlite::Result<Vec<_>>>()?,
            );
        }
        Ok(out)
    }

    pub fn all_definitions(&self) -> Result<Vec<Definition>> {
        let c = self.conn()?;
        let mut st = c.prepare_cached(&format!("SELECT {DEF_COLS} FROM definitions"))?;
        let rows = st
            .query_map([], def_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    pub fn edges_from(&self, id: &str, kinds: &[EdgeKind]) -> Result<Vec<Edge>> {
        self.edges_where("source", id, kinds)
    }

    pub fn edges_to(&self, id: &str, kinds: &[EdgeKind]) -> Result<Vec<Edge>> {
        self.edges_where("target", id, kinds)
    }

    fn edges_where(&self, col: &str, id: &str, kinds: &[EdgeKind]) -> Result<Vec<Edge>> {
        let c = self.conn()?;
        // Kind names are internal constants, so inlining them is safe; it keeps
        // the filter in SQL where the (col, kind) index applies.
        let filter = if kinds.is_empty() {
            String::new()
        } else {
            let list: Vec<String> = kinds.iter().map(|k| format!("'{}'", k.as_str())).collect();
            format!(" AND kind IN ({})", list.join(","))
        };
        let mut st = c.prepare_cached(&format!(
            "SELECT source, target, kind, method, evidence, weight FROM edges WHERE {col} = ?1{filter}"
        ))?;
        let rows = st
            .query_map([id], edge_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows.into_iter().flatten().collect())
    }

    /// Edges touching `id` in either direction.
    pub fn edges_touching(&self, id: &str, kinds: &[EdgeKind]) -> Result<Vec<Edge>> {
        let mut v = self.edges_where("source", id, kinds)?;
        v.extend(self.edges_where("target", id, kinds)?);
        Ok(v)
    }

    pub fn all_edges(&self) -> Result<Vec<Edge>> {
        let c = self.conn()?;
        let mut st =
            c.prepare_cached("SELECT source, target, kind, method, evidence, weight FROM edges")?;
        let rows = st
            .query_map([], edge_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows.into_iter().flatten().collect())
    }

    /// Inbound `cites` counts per node: a cheap authority signal for ranking.
    pub fn citation_counts(&self) -> Result<std::collections::HashMap<String, u32>> {
        let c = self.conn()?;
        let mut st = c.prepare_cached(
            "SELECT target, COUNT(*) FROM edges WHERE kind = 'cites' GROUP BY target",
        )?;
        let rows = st
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, u32>(1)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows.into_iter().collect())
    }

    // ------------------------------------------------------------- embeddings

    /// Store the vectors for one node (one per chunk), replacing any it had for `model`.
    pub fn put_embeddings(&self, node_id: &str, model: &str, chunks: &[Vec<f32>]) -> Result<()> {
        let mut c = self.conn()?;
        let tx = c.transaction()?;
        tx.execute(
            "DELETE FROM embeddings WHERE node_id = ?1 AND model = ?2",
            params![node_id, model],
        )?;
        for (i, v) in chunks.iter().enumerate() {
            let bytes: Vec<u8> = v.iter().flat_map(|f| f.to_le_bytes()).collect();
            tx.execute(
                "INSERT INTO embeddings (node_id, chunk, model, dim, vector) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![node_id, i as i64, model, v.len() as i64, bytes],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    /// Corpora that have a `<corpus>:summary` node.
    pub fn summarised_corpora(&self) -> Result<std::collections::HashSet<String>> {
        let c = self.conn()?;
        let mut st =
            c.prepare_cached("SELECT DISTINCT corpus FROM nodes WHERE kind = 'summary'")?;
        let rows = st
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<_>>()?;
        Ok(rows)
    }

    /// Nodes that already have vectors for `model`.
    pub fn embedded_nodes(&self, model: &str) -> Result<std::collections::HashSet<String>> {
        let c = self.conn()?;
        let mut st =
            c.prepare_cached("SELECT DISTINCT node_id FROM embeddings WHERE model = ?1")?;
        let rows = st
            .query_map([model], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<_>>()?;
        Ok(rows)
    }

    /// All chunk vectors for `model`: (node id, chunk, vector).
    pub fn embeddings(&self, model: &str) -> Result<Vec<(String, u32, Vec<f32>)>> {
        let c = self.conn()?;
        let mut st = c.prepare(
            "SELECT node_id, chunk, vector FROM embeddings WHERE model = ?1 ORDER BY node_id, chunk",
        )?;
        let rows = st
            .query_map([model], |r| {
                let id: String = r.get(0)?;
                let chunk: u32 = r.get(1)?;
                let b: Vec<u8> = r.get(2)?;
                let v = b
                    .chunks_exact(4)
                    .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                    .collect();
                Ok((id, chunk, v))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Replace all `similar` edges.
    pub fn replace_similarity_edges(&self, edges: &[Edge]) -> Result<()> {
        let mut c = self.conn()?;
        let tx = c.transaction()?;
        tx.execute("DELETE FROM edges WHERE kind = 'similar'", [])?;
        {
            let mut st = tx.prepare(
                "INSERT OR REPLACE INTO edges (source, target, kind, method, evidence, weight) VALUES (?1, ?2, 'similar', 'model', ?3, ?4)",
            )?;
            for e in edges {
                st.execute(params![e.source, e.target, e.evidence, e.weight as f64])?;
            }
        }
        tx.commit()?;
        drop(c);
        self.bump_generation()?;
        Ok(())
    }
}

/// Bring stores created by earlier versions up to the current schema.
fn migrate(c: &Connection) -> Result<()> {
    let has_col = |table: &str, col: &str| -> Result<bool> {
        let mut st = c.prepare_cached(&format!("PRAGMA table_info({table})"))?;
        let cols: Vec<String> = st
            .query_map([], |r| r.get::<_, String>(1))?
            .collect::<rusqlite::Result<_>>()?;
        Ok(cols.iter().any(|c| c == col))
    };
    let exists = |table: &str| -> Result<bool> {
        Ok(c.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
            [table],
            |r| r.get::<_, i64>(0),
        )? > 0)
    };
    if exists("edges")? && !has_col("edges", "weight")? {
        c.execute_batch("ALTER TABLE edges ADD COLUMN weight REAL NOT NULL DEFAULT 1.0;")?;
    }
    if exists("refs")? && !has_col("refs", "pin")? {
        c.execute_batch("ALTER TABLE refs ADD COLUMN pin TEXT;")?;
    }
    // Vectors are regenerable; drop the pre-chunking table.
    if exists("embeddings")? && !has_col("embeddings", "chunk")? {
        c.execute_batch("DROP TABLE embeddings;")?;
    }
    Ok(())
}

pub fn term_key(term: &str) -> String {
    term.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

const CORPUS_COLS: &str =
    "id, title, kind, jurisdiction, profile, aliases, citations, source_url, as_at, licence, meta";
const NODE_COLS: &str = "id, corpus, kind, label, number, title, text, parent, ordinal, meta";
const DEF_COLS: &str = "id, term, source, scope, corpus, text, pattern, points_to";

fn json_col<T: serde::de::DeserializeOwned + Default>(r: &Row, i: usize) -> rusqlite::Result<T> {
    let s: String = r.get(i)?;
    Ok(serde_json::from_str(&s).unwrap_or_default())
}

fn corpus_row(r: &Row) -> rusqlite::Result<Corpus> {
    let kind: String = r.get(2)?;
    Ok(Corpus {
        id: r.get(0)?,
        title: r.get(1)?,
        kind: CorpusKind::parse(&kind).unwrap_or(CorpusKind::Secondary),
        jurisdiction: r.get(3)?,
        profile: r.get(4)?,
        aliases: json_col(r, 5)?,
        citations: json_col(r, 6)?,
        source_url: r.get(7)?,
        as_at: r.get(8)?,
        licence: r.get(9)?,
        meta: json_col(r, 10)?,
    })
}

fn node_row(r: &Row) -> rusqlite::Result<Node> {
    let kind: String = r.get(2)?;
    Ok(Node {
        id: r.get(0)?,
        corpus: r.get(1)?,
        kind: NodeKind::parse(&kind).unwrap_or(NodeKind::Chunk),
        label: r.get(3)?,
        number: r.get(4)?,
        title: r.get(5)?,
        text: r.get(6)?,
        parent: r.get(7)?,
        ordinal: r.get(8)?,
        meta: json_col(r, 9)?,
    })
}

fn def_row(r: &Row) -> rusqlite::Result<Definition> {
    Ok(Definition {
        id: r.get(0)?,
        term: r.get(1)?,
        source: r.get(2)?,
        scope: r.get(3)?,
        corpus: r.get(4)?,
        text: r.get(5)?,
        pattern: r.get(6)?,
        points_to: r.get(7)?,
    })
}

fn edge_row(r: &Row) -> rusqlite::Result<Option<Edge>> {
    let kind: String = r.get(2)?;
    let method: String = r.get(3)?;
    let Some(kind) = EdgeKind::parse(&kind) else {
        return Ok(None);
    };
    let method = match method.as_str() {
        "structure" => Method::Structure,
        "alias" => Method::Alias,
        "manual" => Method::Manual,
        "model" => Method::Model,
        _ => Method::Pattern,
    };
    Ok(Some(Edge {
        source: r.get(0)?,
        target: r.get(1)?,
        kind,
        method,
        evidence: r.get(4)?,
        weight: r.get::<_, f64>(5)? as f32,
    }))
}
