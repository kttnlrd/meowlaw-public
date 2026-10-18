//! Graph queries over the `edges` table: neighbourhoods, shortest paths, export.

use anyhow::Result;
use meowlaw_core::{Edge, EdgeKind};
use serde::Serialize;
use std::collections::{HashMap, HashSet, VecDeque};

use crate::Store;

/// Edge kinds traversed by default (the tree is reachable through `outline`).
pub const DEFAULT_KINDS: &[EdgeKind] = &[
    EdgeKind::Cites,
    EdgeKind::Defines,
    EdgeKind::UsesTerm,
    EdgeKind::Amends,
    EdgeKind::Related,
    EdgeKind::SameAs,
    EdgeKind::Similar,
];

#[derive(Debug, Clone, Serialize)]
pub struct GraphNode {
    pub id: String,
    /// `node` or `definition`.
    pub entity: &'static str,
    pub corpus: String,
    pub heading: String,
    /// Node kind (`provision`, `paragraph`, ...) or `definition`.
    pub kind: String,
    pub depth: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct Neighbourhood {
    pub center: GraphNode,
    pub nodes: Vec<GraphNode>,
    pub edges: Vec<Edge>,
    pub truncated: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct PathStep {
    pub from: String,
    pub to: String,
    pub kind: EdgeKind,
    /// `forward` when the edge runs from → to, `backward` when it runs to → from.
    pub direction: &'static str,
    pub evidence: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GraphPath {
    pub found: bool,
    pub hops: usize,
    pub nodes: Vec<GraphNode>,
    pub steps: Vec<PathStep>,
}

impl Store {
    pub fn graph_node(&self, id: &str, depth: usize) -> Result<Option<GraphNode>> {
        if id.starts_with("def:") {
            return Ok(self.definition(id)?.map(|d| GraphNode {
                id: d.id,
                entity: "definition",
                corpus: d.corpus,
                heading: format!("“{}”", d.term),
                kind: "definition".into(),
                depth,
            }));
        }
        Ok(self.node(id)?.map(|n| GraphNode {
            heading: n.heading(),
            id: n.id,
            entity: "node",
            corpus: n.corpus,
            kind: n.kind.as_str().into(),
            depth,
        }))
    }

    /// Breadth-first neighbourhood in both directions.
    pub fn neighbourhood(
        &self,
        id: &str,
        depth: usize,
        kinds: &[EdgeKind],
        limit: usize,
    ) -> Result<Option<Neighbourhood>> {
        let kinds = if kinds.is_empty() {
            DEFAULT_KINDS
        } else {
            kinds
        };
        let Some(center) = self.graph_node(id, 0)? else {
            return Ok(None);
        };
        let mut seen: HashMap<String, usize> = HashMap::from([(id.to_string(), 0)]);
        let mut edges: Vec<Edge> = Vec::new();
        let mut edge_keys = HashSet::new();
        let mut queue = VecDeque::from([(id.to_string(), 0usize)]);
        let mut truncated = false;
        while let Some((cur, d)) = queue.pop_front() {
            if d >= depth {
                continue;
            }
            // `seen` includes the centre, so the limit applies to the others.
            if truncated {
                break;
            }
            for e in self.edges_touching(&cur, kinds)? {
                let other = if e.source == cur {
                    &e.target
                } else {
                    &e.source
                };
                if !seen.contains_key(other) {
                    if seen.len() > limit {
                        truncated = true;
                        continue;
                    }
                    seen.insert(other.clone(), d + 1);
                    queue.push_back((other.clone(), d + 1));
                }
                if edge_keys.insert((e.source.clone(), e.target.clone(), e.kind)) {
                    edges.push(e);
                }
            }
        }
        let mut nodes = Vec::new();
        for (nid, d) in &seen {
            if nid == id {
                continue;
            }
            if let Some(n) = self.graph_node(nid, *d)? {
                nodes.push(n);
            }
        }
        nodes.sort_by(|a, b| a.depth.cmp(&b.depth).then(a.id.cmp(&b.id)));
        // Only edges between returned nodes.
        let keep: HashSet<&str> = nodes.iter().map(|n| n.id.as_str()).chain([id]).collect();
        edges.retain(|e| keep.contains(e.source.as_str()) && keep.contains(e.target.as_str()));
        Ok(Some(Neighbourhood {
            center,
            nodes,
            edges,
            truncated,
        }))
    }

    /// Shortest undirected path, reporting each edge's real direction.
    pub fn graph_path(
        &self,
        from: &str,
        to: &str,
        max_hops: usize,
        kinds: &[EdgeKind],
    ) -> Result<GraphPath> {
        let kinds = if kinds.is_empty() {
            DEFAULT_KINDS
        } else {
            kinds
        };
        let not_found = GraphPath {
            found: false,
            hops: 0,
            nodes: vec![],
            steps: vec![],
        };
        if self.graph_node(from, 0)?.is_none() || self.graph_node(to, 0)?.is_none() {
            return Ok(not_found);
        }
        // Bidirectional BFS: expand the smaller frontier each round. Each side
        // records, per node, the neighbour it was reached from and the edge used.
        let mut prev_f: HashMap<String, Option<(String, Edge)>> =
            HashMap::from([(from.to_string(), None)]);
        let mut prev_b: HashMap<String, Option<(String, Edge)>> =
            HashMap::from([(to.to_string(), None)]);
        let mut front_f = vec![from.to_string()];
        let mut front_b = vec![to.to_string()];
        let mut meet: Option<String> = (from == to).then(|| from.to_string());
        let mut hops = 0;
        while meet.is_none() && !front_f.is_empty() && !front_b.is_empty() && hops < max_hops {
            hops += 1;
            let forward = front_f.len() <= front_b.len();
            let (front, prev, other_side) = if forward {
                (&mut front_f, &mut prev_f, &prev_b)
            } else {
                (&mut front_b, &mut prev_b, &prev_f)
            };
            let mut next = Vec::new();
            'expand: for cur in front.iter() {
                for e in self.edges_touching(cur, kinds)? {
                    let other = if &e.source == cur {
                        e.target.clone()
                    } else {
                        e.source.clone()
                    };
                    if prev.contains_key(&other) {
                        continue;
                    }
                    prev.insert(other.clone(), Some((cur.clone(), e)));
                    if other_side.contains_key(&other) {
                        meet = Some(other);
                        break 'expand;
                    }
                    next.push(other);
                }
            }
            next.truncate(50_000);
            *front = next;
        }
        let Some(meet) = meet else {
            return Ok(not_found);
        };
        let step = |a: &str, b: &str, e: &Edge| PathStep {
            direction: if e.source == a { "forward" } else { "backward" },
            from: a.to_string(),
            to: b.to_string(),
            kind: e.kind,
            evidence: e.evidence.clone(),
        };
        // from .. meet, walking prev_f back from meet.
        let mut steps = Vec::new();
        let mut cur = meet.clone();
        while let Some(Some((p, e))) = prev_f.get(&cur) {
            steps.push(step(p, &cur, e));
            cur = p.clone();
        }
        steps.reverse();
        // meet .. to, walking prev_b forward from meet.
        let mut cur = meet;
        while let Some(Some((n, e))) = prev_b.get(&cur) {
            steps.push(step(&cur, n, e));
            cur = n.clone();
        }
        let mut nodes = Vec::new();
        if let Some(n) = self.graph_node(from, 0)? {
            nodes.push(n);
        }
        for (i, s) in steps.iter().enumerate() {
            if let Some(n) = self.graph_node(&s.to, i + 1)? {
                nodes.push(n);
            }
        }
        Ok(GraphPath {
            found: true,
            hops: steps.len(),
            nodes,
            steps,
        })
    }

    /// Export the graph. `corpus` limits to edges touching that corpus.
    pub fn export_graph(
        &self,
        format: &str,
        corpus: Option<&str>,
        include_tree: bool,
    ) -> Result<String> {
        let edges: Vec<Edge> = self
            .all_edges()?
            .into_iter()
            .filter(|e| include_tree || e.kind != EdgeKind::Contains)
            .filter(|e| {
                corpus.is_none_or(|c| {
                    crate::corpus_of(&e.source) == c || crate::corpus_of(&e.target) == c
                })
            })
            .collect();
        let mut ids: Vec<&str> = edges
            .iter()
            .flat_map(|e| [e.source.as_str(), e.target.as_str()])
            .collect();
        ids.sort();
        ids.dedup();
        let mut nodes = Vec::new();
        for id in &ids {
            if let Some(n) = self.graph_node(id, 0)? {
                nodes.push(n);
            }
        }
        Ok(match format {
            "graphml" => graphml(&nodes, &edges),
            "dot" => dot(&nodes, &edges),
            _ => serde_json::to_string_pretty(
                &serde_json::json!({ "nodes": nodes, "edges": edges }),
            )?,
        })
    }
}

fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn graphml(nodes: &[GraphNode], edges: &[Edge]) -> String {
    let mut s = String::from(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <key id="label" for="node" attr.name="label" attr.type="string"/>
  <key id="corpus" for="node" attr.name="corpus" attr.type="string"/>
  <key id="kind" for="node" attr.name="kind" attr.type="string"/>
  <key id="ekind" for="edge" attr.name="kind" attr.type="string"/>
  <key id="evidence" for="edge" attr.name="evidence" attr.type="string"/>
  <graph id="meowlaw" edgedefault="directed">
"#,
    );
    for n in nodes {
        s.push_str(&format!(
            "    <node id=\"{}\"><data key=\"label\">{}</data><data key=\"corpus\">{}</data><data key=\"kind\">{}</data></node>\n",
            esc(&n.id), esc(&n.heading), esc(&n.corpus), esc(&n.kind)
        ));
    }
    for e in edges {
        s.push_str(&format!(
            "    <edge source=\"{}\" target=\"{}\"><data key=\"ekind\">{}</data><data key=\"evidence\">{}</data></edge>\n",
            esc(&e.source), esc(&e.target), e.kind.as_str(), esc(e.evidence.as_deref().unwrap_or(""))
        ));
    }
    s.push_str("  </graph>\n</graphml>\n");
    s
}

fn dot(nodes: &[GraphNode], edges: &[Edge]) -> String {
    let q = |s: &str| format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""));
    let mut s =
        String::from("digraph meowlaw {\n  rankdir=LR;\n  node [shape=box, fontsize=10];\n");
    for n in nodes {
        let shape = if n.entity == "definition" {
            ", shape=ellipse"
        } else {
            ""
        };
        s.push_str(&format!(
            "  {} [label={}{}];\n",
            q(&n.id),
            q(&n.heading),
            shape
        ));
    }
    for e in edges {
        s.push_str(&format!(
            "  {} -> {} [label={}];\n",
            q(&e.source),
            q(&e.target),
            q(e.kind.as_str())
        ));
    }
    s.push_str("}\n");
    s
}
