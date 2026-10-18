//! MCP tool definitions and implementations. The same functions back the REST API.

use anyhow::{Context, Result, bail};
use meowlaw_core::EdgeKind;
use meowlaw_search::{Engine, Filters, Mode};
use serde_json::{Value, json};

fn s<'a>(args: &'a Value, k: &str) -> Option<&'a str> {
    args.get(k)
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
}

fn n(args: &Value, k: &str, default: usize, max: usize) -> usize {
    args.get(k)
        .and_then(Value::as_u64)
        .map(|v| v as usize)
        .unwrap_or(default)
        .clamp(1, max)
}

fn kinds(args: &Value) -> Vec<EdgeKind> {
    args.get("edge_kinds")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .filter_map(EdgeKind::parse)
                .collect()
        })
        .unwrap_or_default()
}

pub fn definitions() -> Value {
    let edge_kinds = json!({
        "type": "array",
        "items": { "type": "string", "enum": ["cites", "defines", "uses_term", "amends", "related", "same_as", "similar", "contains"] },
        "description": "Edge kinds to follow. Default: all except contains."
    });
    json!([
        {
            "name": "get_info",
            "description": "Server version, loaded corpora with counts, ID grammar and usage notes. Call first.",
            "inputSchema": { "type": "object", "properties": {} },
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "list_corpora",
            "description": "List ingested corpora (Acts, codes, rule sets, cases, guidance, books), optionally filtered. Versions of one instrument share `meta.work`; `superseded` marks compilations replaced by a later one.",
            "inputSchema": { "type": "object", "properties": {
                "kind": { "type": "string", "enum": ["legislation", "instrument", "rule_set", "case", "treaty", "guidance", "secondary"] },
                "jurisdiction": { "type": "string", "description": "Jurisdiction code as ingested (free text, e.g. GB, US-CA, EU)." }
            }},
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "search",
            "description": "Search all sources. mode=enriched resolves citations in the query, adds definitions of terms used, expands along the citation graph and fuses rankings; mode=basic is plain BM25. Omit mode to use the server's configured default. Each result says why it matched.",
            "inputSchema": { "type": "object", "required": ["query"], "properties": {
                "query": { "type": "string" },
                "mode": { "type": "string", "enum": ["enriched", "basic"] },
                "corpus": { "type": "string", "description": "Restrict to one corpus ID." },
                "kind": { "type": "string", "description": "Restrict to a corpus kind, or several separated by commas (legislation, instrument, rule_set, case, treaty, guidance, secondary)." },
                "jurisdiction": { "type": "string" },
                "as_at": { "type": "string", "description": "YYYY-MM-DD: search the versions of each work in force on that date. Default: current versions only." },
                "limit": { "type": "integer", "minimum": 1, "maximum": 100 }
            }},
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "fetch",
            "description": "Read one node by ID, or by citation text (e.g. 's 12 Example Act 2020', 'Article 5(1) of Regulation (EU) 2016/679', '[2024] UKSC 1 at [30]'). Returns text, location in the tree, children (paragraph children include their text), the document summary when reading a document root, outbound references, definitions in scope, inbound citation count, source links, and the corpus licence and as-at date.",
            "inputSchema": { "type": "object", "properties": {
                "id": { "type": "string" },
                "citation": { "type": "string" },
                "max_chars": { "type": "integer", "minimum": 200, "maximum": 200000 }
            }},
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "outline",
            "description": "Table of contents under a corpus or node (structure only; paragraphs and summaries are returned by fetch).",
            "inputSchema": { "type": "object", "required": ["id"], "properties": {
                "id": { "type": "string", "description": "Corpus ID or node ID." },
                "depth": { "type": "integer", "minimum": 1, "maximum": 6 }
            }},
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "resolve",
            "description": "Find and resolve every citation in a piece of text to node IDs. Unresolved citations come back with no targets (the source may not be ingested).",
            "inputSchema": { "type": "object", "required": ["text"], "properties": {
                "text": { "type": "string" },
                "corpus": { "type": "string", "description": "Corpus to assume for bare citations like 's 12'." }
            }},
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "get_definition",
            "description": "Definitions of a term, with the defining provision, its scope, and any provision it points to.",
            "inputSchema": { "type": "object", "required": ["term"], "properties": {
                "term": { "type": "string" },
                "corpus": { "type": "string" },
                "limit": { "type": "integer", "minimum": 1, "maximum": 50 }
            }},
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "related",
            "description": "What a node cites, what cites it, terms it defines, defined terms it uses, and its most semantically similar nodes (cosine-weighted, from embeddings).",
            "inputSchema": { "type": "object", "required": ["id"], "properties": {
                "id": { "type": "string" },
                "limit": { "type": "integer", "minimum": 1, "maximum": 500 }
            }},
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "graph_neighbourhood",
            "description": "Nodes and edges within N hops of a node (both directions).",
            "inputSchema": { "type": "object", "required": ["id"], "properties": {
                "id": { "type": "string" },
                "depth": { "type": "integer", "minimum": 1, "maximum": 3 },
                "limit": { "type": "integer", "minimum": 1, "maximum": 500 },
                "edge_kinds": edge_kinds
            }},
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "graph_path",
            "description": "Shortest chain of citations/definitions linking two nodes, with each edge's direction and evidence.",
            "inputSchema": { "type": "object", "required": ["from", "to"], "properties": {
                "from": { "type": "string" },
                "to": { "type": "string" },
                "max_hops": { "type": "integer", "minimum": 1, "maximum": 8 },
                "edge_kinds": edge_kinds
            }},
            "annotations": { "readOnlyHint": true }
        },
        {
            "name": "unresolved_references",
            "description": "Citations found in the text that did not resolve, grouped and counted. Use to find missing sources or broken patterns.",
            "inputSchema": { "type": "object", "properties": {
                "corpus": { "type": "string" },
                "limit": { "type": "integer", "minimum": 1, "maximum": 500 }
            }},
            "annotations": { "readOnlyHint": true }
        }
    ])
}

pub fn call(engine: &Engine, name: &str, args: &Value) -> Result<Value> {
    let store = &engine.store;
    match name {
        "get_info" => {
            let stats = store.stats()?;
            let corpora = store.corpus_summaries()?;
            Ok(json!({
                "name": "meowlaw",
                "version": env!("CARGO_PKG_VERSION"),
                "stats": stats,
                "corpora": corpora.iter().map(|c| json!({
                    "id": c.corpus.id, "title": c.corpus.title, "kind": c.corpus.kind,
                    "jurisdiction": c.corpus.jurisdiction, "as_at": c.corpus.as_at,
                    "nodes": c.nodes, "definitions": c.definitions,
                })).collect::<Vec<_>>(),
                "id_grammar": {
                    "corpus_root": "<corpus>",
                    "provision": "<corpus>:<abbrev><number>  e.g. privacy-act-1988:s6, gdpr:art5, usc-26:s162",
                    "nested": "<corpus>:<container path>/<abbrev><number> when a number repeats (schedules)",
                    "case_paragraph": "<case-corpus>:para<n>",
                    "chunk": "<corpus>:<heading-slug>[/<heading-slug>...]",
                    "definition": "def:<corpus>:<term-slug>"
                },
                "notes": [
                    "Text is as ingested; check each corpus's as_at date and licence before relying on it.",
                    "Unresolved citations usually mean the cited source is not loaded."
                ]
            }))
        }
        "list_corpora" => {
            let kind = s(args, "kind");
            let jur = s(args, "jurisdiction");
            let all = store.corpus_summaries()?;
            let superseded = meowlaw_core::versions::superseded(all.iter().map(|c| &c.corpus));
            let rows: Vec<Value> = all
                .into_iter()
                .filter(|c| kind.is_none_or(|k| c.corpus.kind.as_str() == k))
                .filter(|c| jur.is_none_or(|j| c.corpus.jurisdiction.as_deref() == Some(j)))
                .map(|c| {
                    let mut v = serde_json::to_value(&c).unwrap_or_default();
                    v["superseded"] = json!(superseded.contains(&c.corpus.id));
                    v
                })
                .collect();
            Ok(json!({ "corpora": rows }))
        }
        "search" => {
            let q = s(args, "query").context("`query` is required")?;
            let mode = s(args, "mode")
                .and_then(Mode::parse)
                .unwrap_or(engine.default_mode);
            let filters = Filters {
                corpus: s(args, "corpus").map(String::from),
                kind: s(args, "kind").map(String::from),
                jurisdiction: s(args, "jurisdiction").map(String::from),
                exclude: args
                    .get("_exclude")
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str().map(String::from))
                            .collect()
                    })
                    .unwrap_or_default(),
                as_at: s(args, "as_at").map(String::from),
            };
            Ok(serde_json::to_value(engine.search(
                q,
                mode,
                &filters,
                n(args, "limit", 10, 100),
            )?)?)
        }
        "fetch" => fetch(engine, args),
        "outline" => {
            let id = s(args, "id").context("`id` is required")?;
            let depth = n(args, "depth", 2, 6);
            let root = store.node(id)?.with_context(|| format!("no node `{id}`"))?;
            Ok(
                json!({ "id": id, "heading": root.heading(), "children": outline(engine, id, depth)? }),
            )
        }
        "resolve" => {
            let text = s(args, "text").context("`text` is required")?;
            Ok(json!({ "citations": engine.resolve_citations(text, s(args, "corpus"))? }))
        }
        "get_definition" => {
            let term = s(args, "term").context("`term` is required")?;
            let defs = store.definitions_for(term, s(args, "corpus"), n(args, "limit", 10, 50))?;
            let mut out = Vec::new();
            for d in defs {
                let source = store.node(&d.source)?.map(|n| n.heading());
                let scope = store.node(&d.scope)?.map(|n| n.heading());
                out.push(
                    json!({ "definition": d, "source_heading": source, "scope_heading": scope }),
                );
            }
            Ok(json!({ "term": term, "definitions": out }))
        }
        "related" => {
            let id = s(args, "id").context("`id` is required")?;
            let limit = n(args, "limit", 100, 500);
            let heading = |nid: &str| -> Option<String> {
                store.graph_node(nid, 0).ok().flatten().map(|g| g.heading)
            };
            let group = |edges: Vec<meowlaw_core::Edge>, outbound: bool| -> Vec<Value> {
                edges
                    .into_iter()
                    .take(limit)
                    .map(|e| {
                        let other = if outbound { e.target } else { e.source };
                        json!({ "id": other, "heading": heading(&other), "evidence": e.evidence })
                    })
                    .collect()
            };
            let mut similar = store.edges_from(id, &[EdgeKind::Similar])?;
            similar.extend(
                store
                    .edges_to(id, &[EdgeKind::Similar])?
                    .into_iter()
                    .map(|mut x| {
                        std::mem::swap(&mut x.source, &mut x.target);
                        x
                    }),
            );
            similar.sort_by(|a, b| b.weight.total_cmp(&a.weight));
            let mut seen = std::collections::HashSet::new();
            similar.retain(|e| seen.insert(e.target.clone()));
            Ok(json!({
                "id": id,
                "cites": group(store.edges_from(id, &[EdgeKind::Cites])?, true),
                "cited_by": group(store.edges_to(id, &[EdgeKind::Cites])?, false),
                "defines": group(store.edges_from(id, &[EdgeKind::Defines])?, true),
                "uses_terms": group(store.edges_from(id, &[EdgeKind::UsesTerm])?, true),
                "used_by": group(store.edges_to(id, &[EdgeKind::UsesTerm])?, false),
                "related": group(store.edges_from(id, &[EdgeKind::Related, EdgeKind::SameAs, EdgeKind::Amends])?, true),
                "similar": group(similar, true),
            }))
        }
        "graph_neighbourhood" => {
            let id = s(args, "id").context("`id` is required")?;
            let nb = store
                .neighbourhood(
                    id,
                    n(args, "depth", 1, 3),
                    &kinds(args),
                    n(args, "limit", 100, 500),
                )?
                .with_context(|| format!("no node `{id}`"))?;
            Ok(serde_json::to_value(nb)?)
        }
        "graph_path" => {
            let from = s(args, "from").context("`from` is required")?;
            let to = s(args, "to").context("`to` is required")?;
            Ok(serde_json::to_value(store.graph_path(
                from,
                to,
                n(args, "max_hops", 6, 8),
                &kinds(args),
            )?)?)
        }
        "unresolved_references" => Ok(json!({
            "groups": store.unresolved(s(args, "corpus"), n(args, "limit", 50, 500))?
        })),
        other => bail!("unknown tool `{other}`"),
    }
}

fn outline(engine: &Engine, id: &str, depth: usize) -> Result<Vec<Value>> {
    let mut out = Vec::new();
    for c in engine.store.children(id)? {
        // Paragraphs and summaries are read inline, not listed as contents.
        if matches!(
            c.kind,
            meowlaw_core::NodeKind::Paragraph | meowlaw_core::NodeKind::Summary
        ) {
            continue;
        }
        let children = if depth > 1 {
            outline(engine, &c.id, depth - 1)?
        } else {
            vec![]
        };
        let mut v = json!({ "id": c.id, "heading": c.heading(), "kind": c.kind });
        if !children.is_empty() {
            v["children"] = Value::Array(children);
        }
        out.push(v);
    }
    Ok(out)
}

pub fn fetch(engine: &Engine, args: &Value) -> Result<Value> {
    let store = &engine.store;
    let id = match (s(args, "id"), s(args, "citation")) {
        (Some(id), _) => id.to_string(),
        (None, Some(c)) => {
            let resolved = engine.resolve_citations(c, None)?;
            let targets: Vec<String> = resolved.into_iter().flat_map(|r| r.targets).collect();
            match targets.len() {
                0 => bail!("citation `{c}` did not resolve to a loaded source"),
                1 => targets[0].clone(),
                _ => {
                    return Ok(json!({ "ambiguous": true, "citation": c, "candidates": targets }));
                }
            }
        }
        (None, None) => bail!("give `id` or `citation`"),
    };
    if id.starts_with("def:") {
        let d = store
            .definition(&id)?
            .with_context(|| format!("no definition `{id}`"))?;
        return Ok(json!({ "definition": d }));
    }
    let node = store
        .node(&id)?
        .with_context(|| format!("no node `{id}`"))?;
    let corpus = store.corpus(&node.corpus)?;
    let max = args
        .get("max_chars")
        .and_then(Value::as_u64)
        .map(|v| v as usize)
        .unwrap_or(50_000)
        .clamp(200, 200_000);
    let truncated = node.text.len() > max;
    let text = meowlaw_search::index::truncate(&node.text, max);
    let path: Vec<Value> = store
        .ancestors(&id)?
        .into_iter()
        .map(|a| json!({ "id": a.id, "heading": a.heading() }))
        .collect();
    // Paragraph children carry their text so a reader can show the full text
    // in order; each stays separately addressable by its id.
    let mut summary: Option<Value> = None;
    let mut children: Vec<Value> = Vec::new();
    for c in store.children(&id)? {
        if c.kind == meowlaw_core::NodeKind::Summary {
            let refs: Vec<Value> = store
                .refs_from(&c.id)?
                .into_iter()
                .map(
                    |r| json!({ "raw": r.raw, "target": r.target, "pin": r.pin, "start": r.start, "end": r.end }),
                )
                .collect();
            summary = Some(json!({
                "id": c.id,
                "text": c.text,
                "source": c.meta.get("summary_source"),
                "references": refs,
            }));
            continue;
        }
        let mut v = json!({ "id": c.id, "heading": c.heading(), "kind": c.kind });
        if c.kind == meowlaw_core::NodeKind::Paragraph {
            v["number"] = json!(c.number);
            v["text"] = json!(meowlaw_search::index::truncate(&c.text, 4_000));
        }
        children.push(v);
    }
    let refs: Vec<Value> = store
        .refs_from(&id)?
        .into_iter()
        .filter(|r| r.end <= max)
        .map(|r| json!({ "raw": r.raw, "target": r.target, "pin": r.pin, "start": r.start, "end": r.end }))
        .collect();
    // Only definitions whose term occurs in this text (a dictionary of the whole
    // Act applies everywhere, but listing 1,500 terms on every fetch is noise).
    let lower = node.text.to_lowercase();
    let defs: Vec<Value> = store
        .definitions_applying(&id)?
        .into_iter()
        .filter(|d| d.source == id || contains_word(&lower, &d.term.to_lowercase()))
        .map(|d| json!({ "term": d.term, "id": d.id }))
        .collect();
    let cited_by = store.edges_to(&id, &[EdgeKind::Cites])?.len();
    // Other versions of this provision (languages, compilations), with
    // whether their text differs from this one.
    let mut versions = Vec::new();
    for e in store.edges_from(&id, &[EdgeKind::SameAs])? {
        let Some(other) = store.node(&e.target)? else {
            continue;
        };
        let oc = store.corpus(&other.corpus)?;
        versions.push(json!({
            "id": other.id,
            "corpus": other.corpus,
            "label": e.evidence,
            "language": oc.as_ref().and_then(|c| meowlaw_core::versions::language_of(c).map(String::from)),
            "as_at": oc.as_ref().and_then(|c| c.as_at.clone()),
            "text_differs": other.text != node.text,
        }));
    }
    let links = match &corpus {
        Some(c) => engine.links_for(c)?,
        None => vec![],
    };
    Ok(json!({
        "id": node.id,
        "heading": node.heading(),
        "kind": node.kind,
        "label": node.label,
        "number": node.number,
        "title": node.title,
        "text": text,
        "truncated": truncated,
        "path": path,
        "children": children,
        "summary": summary,
        "references": refs,
        "definitions_in_scope": defs,
        "cited_by_count": cited_by,
        "versions": versions,
        "corpus": corpus.map(|c| json!({
            "id": c.id, "title": c.title, "kind": c.kind, "jurisdiction": c.jurisdiction,
            "as_at": c.as_at, "licence": c.licence, "source_url": c.source_url, "citations": c.citations,
            "work": meowlaw_core::versions::work_of(&c), "language": meowlaw_core::versions::language_of(&c),
            "aliases": c.aliases, "meta": c.meta,
            "links": links,
        })),
    }))
}

/// Whole-word, already-lowercased containment.
fn contains_word(hay: &str, needle: &str) -> bool {
    if needle.is_empty() {
        return false;
    }
    let is_word = |c: char| c.is_alphanumeric() || c == '_';
    hay.match_indices(needle).any(|(i, m)| {
        hay[..i].chars().next_back().is_none_or(|c| !is_word(c))
            && hay[i + m.len()..]
                .chars()
                .next()
                .is_none_or(|c| !is_word(c))
    })
}
