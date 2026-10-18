//! `same_as` edges between corresponding provisions of versions of a work
//! (language versions, point-in-time compilations). See
//! `meowlaw_core::versions`.
//!
//! Trees are aligned top-down: children of matched nodes match on kind and
//! number ("Article 5" = "Artikel 5" = "article 5"); unnumbered children of
//! one kind match by position when both sides have the same count.

use meowlaw_core::ids::number_key;
use meowlaw_core::versions::{language_of, work_of};
use meowlaw_core::{Document, Edge, EdgeKind, Method, Node, NodeKind};
use std::collections::{BTreeMap, HashMap};

/// How a version is named in edge evidence: "fr", "as at 2025-03-01", "fr, as at 2025-03-01".
pub fn version_label(c: &meowlaw_core::Corpus) -> String {
    let mut parts = Vec::new();
    if let Some(l) = language_of(c) {
        parts.push(l.to_string());
    }
    if let Some(d) = &c.as_at {
        parts.push(format!("as at {d}"));
    }
    if parts.is_empty() {
        parts.push(c.id.clone());
    }
    parts.join(", ")
}

pub fn version_edges(docs: &[Document]) -> Vec<Edge> {
    let mut works: BTreeMap<&str, Vec<&Document>> = BTreeMap::new();
    for d in docs {
        if let Some(w) = work_of(&d.corpus) {
            works.entry(w).or_default().push(d);
        }
    }
    let mut out = Vec::new();
    for versions in works.values() {
        for (i, a) in versions.iter().enumerate() {
            for b in &versions[i + 1..] {
                let (la, lb) = (version_label(&a.corpus), version_label(&b.corpus));
                for (x, y) in align(a, b) {
                    out.push(edge(&x, &y, &lb));
                    out.push(edge(&y, &x, &la));
                }
            }
        }
    }
    out
}

fn edge(source: &str, target: &str, evidence: &str) -> Edge {
    Edge {
        source: source.to_string(),
        target: target.to_string(),
        kind: EdgeKind::SameAs,
        method: Method::Structure,
        weight: 1.0,
        evidence: Some(evidence.to_string()),
    }
}

fn children(doc: &Document) -> HashMap<&str, Vec<&Node>> {
    let mut m: HashMap<&str, Vec<&Node>> = HashMap::new();
    for n in &doc.nodes {
        if let Some(p) = &n.parent
            && n.kind != NodeKind::Summary
        {
            m.entry(p.as_str()).or_default().push(n);
        }
    }
    for v in m.values_mut() {
        v.sort_by_key(|n| n.ordinal);
    }
    m
}

/// Matching node id pairs, roots included.
pub fn align(a: &Document, b: &Document) -> Vec<(String, String)> {
    let (Some(ra), Some(rb)) = (
        a.nodes.iter().find(|n| n.parent.is_none()),
        b.nodes.iter().find(|n| n.parent.is_none()),
    ) else {
        return Vec::new();
    };
    let (ca, cb) = (children(a), children(b));
    let mut out = Vec::new();
    let mut stack = vec![(ra, rb)];
    while let Some((x, y)) = stack.pop() {
        out.push((x.id.clone(), y.id.clone()));
        let empty = Vec::new();
        let kx = ca.get(x.id.as_str()).unwrap_or(&empty);
        let ky = cb.get(y.id.as_str()).unwrap_or(&empty);
        let numbered: HashMap<(NodeKind, String), &Node> = ky
            .iter()
            .filter_map(|n| n.number.as_ref().map(|num| ((n.kind, number_key(num)), *n)))
            .collect();
        let mut unnumbered_x: BTreeMap<&str, Vec<&Node>> = BTreeMap::new();
        for n in kx {
            match &n.number {
                Some(num) => {
                    if let Some(m) = numbered.get(&(n.kind, number_key(num))) {
                        stack.push((n, m));
                    }
                }
                None => unnumbered_x.entry(n.kind.as_str()).or_default().push(n),
            }
        }
        let mut unnumbered_y: BTreeMap<&str, Vec<&Node>> = BTreeMap::new();
        for n in ky.iter().filter(|n| n.number.is_none()) {
            unnumbered_y.entry(n.kind.as_str()).or_default().push(n);
        }
        for (kind, xs) in unnumbered_x {
            if let Some(ys) = unnumbered_y.get(kind)
                && ys.len() == xs.len()
            {
                stack.extend(xs.into_iter().zip(ys.iter().copied()));
            }
        }
    }
    out
}
