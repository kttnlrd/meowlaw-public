//! Per-corpus access control for tool calls.
//!
//! A [`Scope`] is the set of corpora a caller may read (or everything).
//! [`call_scoped`] enforces it for every tool, so the REST API, the UI and
//! MCP share one rule:
//!
//! - a request naming a hidden node, definition or corpus fails as if it did
//!   not exist;
//! - results are redacted: list items that are or point at hidden nodes are
//!   dropped, and citation targets inside hidden corpora read as unresolved.
//!
//! Node ids start with their corpus id (`<corpus>:...`, `def:<corpus>:...`),
//! which is what makes a single redaction pass over any result possible.

use crate::tools;
use anyhow::{Result, bail};
use meowlaw_search::Engine;
use serde_json::Value;
use std::collections::HashSet;

#[derive(Debug, Clone, Default)]
pub struct Scope {
    /// Hidden corpus ids; `None` = everything visible.
    hidden: Option<HashSet<String>>,
}

impl Scope {
    pub fn all() -> Self {
        Self { hidden: None }
    }

    /// Everything except these corpora.
    pub fn hiding(hidden: HashSet<String>) -> Self {
        Self {
            hidden: (!hidden.is_empty()).then_some(hidden),
        }
    }

    pub fn is_unrestricted(&self) -> bool {
        self.hidden.is_none()
    }

    pub fn corpus_visible(&self, corpus: &str) -> bool {
        self.hidden.as_ref().is_none_or(|h| !h.contains(corpus))
    }

    /// Whether `id` (node, definition or corpus id) is in a hidden corpus.
    pub fn hides(&self, id: &str) -> bool {
        let Some(h) = &self.hidden else {
            return false;
        };
        let id = id.strip_prefix("def:").unwrap_or(id);
        let corpus = id.split(':').next().unwrap_or(id);
        h.contains(corpus)
    }
}

/// Arguments naming one thing to read; hidden ones are refused up front.
const ID_ARGS: &[&str] = &["id", "from", "to", "corpus"];

pub fn call_scoped(engine: &Engine, name: &str, args: &Value, scope: &Scope) -> Result<Value> {
    if !args.is_object() {
        bail!("tool arguments must be a JSON object");
    }
    if scope.is_unrestricted() {
        return tools::call(engine, name, args);
    }
    for k in ID_ARGS {
        if let Some(v) = args.get(*k).and_then(Value::as_str)
            && scope.hides(v)
        {
            bail!(match *k {
                "corpus" => format!("no corpus `{v}`"),
                _ => format!("no node `{v}`"),
            });
        }
    }
    let mut args = args.clone();
    // Search excludes hidden corpora before ranking, so results fill the page.
    if name == "search" {
        args["_exclude"] = Value::from(scope.hidden.iter().flatten().cloned().collect::<Vec<_>>());
    }
    let mut v = tools::call(engine, name, &args)?;
    // A path through a hidden node is no path for this caller.
    if name == "graph_path" && mentions_hidden(&v, scope) {
        bail!("no path found");
    }
    // Totals would reveal how much is hidden.
    if name == "get_info"
        && let Some(o) = v.as_object_mut()
    {
        o.remove("stats");
    }
    // A citation fetch that resolved into a hidden corpus did not resolve.
    if name == "fetch"
        && let Some(id) = v.get("id").and_then(Value::as_str)
        && scope.hides(id)
    {
        bail!("no node `{id}`");
    }
    redact(&mut v, scope);
    Ok(v)
}

/// Keys whose string value is the id of the item an object describes, or of
/// a node it links.
const LINK_KEYS: &[&str] = &[
    "id",
    "source",
    "target",
    "scope",
    "corpus",
    "example_source",
    "points_to",
    "node_id",
];
/// Keys whose object value describes the item (`{ "definition": {...} }`).
const NESTED: &[&str] = &["definition", "corpus", "node"];

fn object_hidden(o: &serde_json::Map<String, Value>, scope: &Scope) -> bool {
    // A citation (it has `raw`) stays: its text is part of a visible node.
    // Only where it was found matters; its target is blanked in `redact`.
    let is_ref = o.contains_key("raw");
    LINK_KEYS.iter().any(|k| {
        if is_ref && matches!(*k, "target" | "id") {
            return false;
        }
        o.get(*k)
            .and_then(Value::as_str)
            .is_some_and(|s| scope.hides(s))
    }) || NESTED.iter().any(|k| {
        o.get(*k)
            .and_then(|n| n.get("id"))
            .and_then(Value::as_str)
            .is_some_and(|s| scope.hides(s))
    })
}

fn mentions_hidden(v: &Value, scope: &Scope) -> bool {
    match v {
        Value::String(s) => scope.hides(s),
        Value::Array(a) => a.iter().any(|x| mentions_hidden(x, scope)),
        Value::Object(o) => o.values().any(|x| mentions_hidden(x, scope)),
        _ => false,
    }
}

pub fn redact(v: &mut Value, scope: &Scope) {
    match v {
        Value::Array(items) => {
            items.retain(|x| match x {
                Value::Object(o) => !object_hidden(o, scope),
                Value::String(s) => !scope.hides(s),
                _ => true,
            });
            for x in items.iter_mut() {
                redact(x, scope);
            }
        }
        Value::Object(o) => {
            if o.contains_key("raw") {
                for k in ["target", "id"] {
                    if o.get(k)
                        .and_then(Value::as_str)
                        .is_some_and(|s| scope.hides(s))
                    {
                        o.insert(k.into(), Value::Null);
                    }
                }
            }
            for (_, x) in o.iter_mut() {
                redact(x, scope);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn scope_hides_by_corpus_prefix() {
        let scope = Scope::hiding(["handbook".to_string()].into());
        assert!(scope.hides("handbook"));
        assert!(scope.hides("handbook:p1"));
        assert!(scope.hides("def:handbook:leave"));
        assert!(!scope.hides("handbook-2:p1"));
        assert!(!scope.hides("other:handbook"));
    }

    #[test]
    fn redacts_hidden_corpora() {
        let scope = Scope::hiding(["handbook".to_string()].into());
        assert!(scope.hides("handbook:p3"));
        assert!(scope.hides("def:handbook:leave"));
        assert!(!scope.hides("itaa-1997:s8-1"));
        let mut v = json!({
            "results": [{ "id": "handbook:p1" }, { "id": "itaa-1997:s8-1" }],
            "references": [{ "raw": "clause 3 of the handbook", "target": "handbook:c3" }],
            "cited_by": [{ "id": "handbook:p2", "heading": "Leave" }],
            "nodes": [{ "id": "itaa-1997:s8-1" }],
            "edges": [{ "source": "handbook:p2", "target": "itaa-1997:s8-1" }],
            "definitions": [{ "definition": { "id": "def:handbook:x" } }],
            "targets": ["handbook:c3", "itaa-1997:s8-1"],
            "corpora": [{ "corpus": { "id": "handbook" } }, { "corpus": { "id": "itaa-1997" } }],
        });
        redact(&mut v, &scope);
        assert_eq!(v["results"], json!([{ "id": "itaa-1997:s8-1" }]));
        assert_eq!(v["references"][0]["target"], Value::Null);
        assert_eq!(v["cited_by"], json!([]));
        assert_eq!(v["edges"], json!([]));
        assert_eq!(v["definitions"], json!([]));
        assert_eq!(v["targets"], json!(["itaa-1997:s8-1"]));
        assert_eq!(v["corpora"].as_array().unwrap().len(), 1);
    }
}
