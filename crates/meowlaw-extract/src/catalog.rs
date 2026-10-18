//! Lookup tables used to resolve references across every ingested corpus.

use meowlaw_core::ids::{citation_key, number_key, strip_last_subdivision};
use meowlaw_core::{Corpus, CorpusKind, Node, NodeKind};
use std::collections::HashMap;

#[derive(Debug, Default, Clone)]
pub struct Catalog {
    pub corpora: HashMap<String, Corpus>,
    /// Normalised name → corpus id ("privacy act 1988", "gdpr").
    names: HashMap<String, String>,
    /// Aliases checked verbatim right after a citation ("s 8-1 ITAA 1997"), longest first.
    suffixes: Vec<(String, String)>,
    /// (corpus, label, number) → node id.
    provisions: HashMap<(String, String, String), String>,
    /// (corpus, number) → node ids, for citations without a label.
    by_number: HashMap<(String, String), Vec<String>>,
    /// Normalised case citation → corpus id.
    cases: HashMap<String, String>,
    /// node id → (parent id, kind, label).
    pub tree: HashMap<String, (Option<String>, NodeKind, Option<String>)>,
    /// Compilations replaced by a later one (see `meowlaw_core::versions`).
    pub superseded: std::collections::HashSet<String>,
}

pub fn name_key(s: &str) -> String {
    let s = s.trim().trim_end_matches(['.', ',', ';']);
    let s = s
        .strip_prefix("the ")
        .or_else(|| s.strip_prefix("The "))
        .unwrap_or(s);
    // Drop a trailing jurisdiction tag: "(Cth)", "(UK)", "(NSW)".
    let s = match s.rfind(" (") {
        Some(i) if s.ends_with(')') && s.len() - i <= 8 => &s[..i],
        _ => s,
    };
    s.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .replace(['’', '‘'], "'")
        .to_lowercase()
}

impl Catalog {
    pub fn add_corpus(&mut self, c: &Corpus, profile_aliases: &[(String, String)]) {
        let mut names = vec![c.title.clone(), c.id.clone()];
        names.extend(c.aliases.iter().cloned());
        for n in names {
            self.names.insert(name_key(&n), c.id.clone());
        }
        for a in &c.aliases {
            self.suffixes.push((a.clone(), c.id.clone()));
        }
        if c.kind != CorpusKind::Case {
            self.suffixes.push((c.title.clone(), c.id.clone()));
        }
        for (alias, target) in profile_aliases {
            if target == &c.id {
                self.names.insert(name_key(alias), c.id.clone());
                self.suffixes.push((alias.clone(), c.id.clone()));
            }
        }
        for cit in &c.citations {
            self.cases.insert(citation_key(cit), c.id.clone());
        }
        self.corpora.insert(c.id.clone(), c.clone());
        self.suffixes
            .sort_by(|a, b| b.0.len().cmp(&a.0.len()).then(a.0.cmp(&b.0)));
        self.suffixes.dedup();
    }

    /// The version of `target`'s work a citation from `citing` means: the
    /// citing corpus itself when it is a version of that work; else the
    /// current version in the citing language; else the current version in
    /// the target's language; else `target`.
    pub fn prefer_version(&self, target: &str, citing: &Corpus) -> String {
        use meowlaw_core::versions::{language_of, work_of};
        let Some(t) = self.corpora.get(target) else {
            return target.to_string();
        };
        let Some(work) = work_of(t) else {
            return target.to_string();
        };
        if work_of(citing) == Some(work) {
            return citing.id.clone();
        }
        let current: Vec<&Corpus> = self
            .corpora
            .values()
            .filter(|c| work_of(c) == Some(work) && !self.superseded.contains(&c.id))
            .collect();
        let pick = |lang: Option<&str>| {
            lang.and_then(|l| {
                current
                    .iter()
                    .filter(|c| language_of(c) == Some(l))
                    .map(|c| c.id.clone())
                    .min()
            })
        };
        pick(language_of(citing))
            .or_else(|| (!self.superseded.contains(target)).then(|| target.to_string()))
            .or_else(|| pick(language_of(t)))
            .unwrap_or_else(|| target.to_string())
    }

    pub fn add_node(&mut self, n: &Node) {
        self.tree
            .insert(n.id.clone(), (n.parent.clone(), n.kind, n.label.clone()));
        if let Some(num) = &n.number {
            let key = number_key(num);
            if let Some(l) = &n.label {
                self.provisions
                    .entry((n.corpus.clone(), l.to_lowercase(), key.clone()))
                    .or_insert_with(|| n.id.clone());
            }
            if matches!(
                n.kind,
                NodeKind::Provision
                    | NodeKind::Schedule
                    | NodeKind::Container
                    | NodeKind::Paragraph
            ) {
                self.by_number
                    .entry((n.corpus.clone(), key))
                    .or_default()
                    .push(n.id.clone());
            }
        }
    }

    pub fn corpus_by_name(&self, s: &str) -> Option<&str> {
        let k = name_key(s);
        if let Some(v) = self.names.get(&k) {
            return Some(v);
        }
        // A capitalised word swept in before the name ("See Income Tax ... Act 1997"):
        // retry without leading words.
        let words: Vec<&str> = k.split(' ').collect();
        for i in 1..words.len().min(4) {
            if let Some(v) = self.names.get(&words[i..].join(" ")) {
                return Some(v);
            }
        }
        // "Income Tax Assessment Act 1997 (Cth)" vs a title without year, and the
        // reverse. Longest known name wins, so the result is deterministic.
        self.names
            .iter()
            .filter(|(n, _)| k.starts_with(n.as_str()) && n.len() > 6 && k.len() - n.len() <= 7)
            .max_by(|a, b| a.0.len().cmp(&b.0.len()).then(b.0.cmp(a.0)))
            .map(|(_, v)| v.as_str())
    }

    /// Alias that begins `rest` (after optional whitespace/"of the"), with its length.
    pub fn corpus_suffix(&self, rest: &str) -> Option<(&str, usize)> {
        let mut trimmed = rest.trim_start();
        if trimmed.len() + 2 < rest.len() {
            return None;
        }
        // "s 6-25 of the ITAA 1997", "§ 823 of the BGB"
        for p in ["of the ", "of "] {
            if let Some(t) = trimmed.strip_prefix(p) {
                trimmed = t;
                break;
            }
        }
        let lead = rest.len() - trimmed.len();
        for (alias, id) in &self.suffixes {
            if trimmed.starts_with(alias.as_str()) {
                let end = lead + alias.len();
                let next = rest[end..].chars().next();
                if next.is_none_or(|c| !c.is_alphanumeric()) {
                    return Some((id.as_str(), end));
                }
            }
        }
        None
    }

    /// Resolve a provision number, dropping trailing `(1)(a)` groups until a node matches.
    pub fn provision(&self, corpus: &str, label: Option<&str>, number: &str) -> Option<&str> {
        let mut num = number.trim().to_string();
        loop {
            let key = number_key(&num);
            if let Some(l) = label
                && let Some(id) =
                    self.provisions
                        .get(&(corpus.to_string(), l.to_lowercase(), key.clone()))
            {
                return Some(id);
            }
            // No exact label match: accept the number only when it is unambiguous
            // in the corpus and the node's label does not contradict the citation.
            if let Some(ids) = self.by_number.get(&(corpus.to_string(), key))
                && ids.len() == 1
                && self.label_compatible(&ids[0], label)
            {
                return Some(&ids[0]);
            }
            {
                let n = strip_last_subdivision(&num)?;
                num = n
            }
        }
    }

    fn label_compatible(&self, id: &str, label: Option<&str>) -> bool {
        match (label, self.tree.get(id)) {
            (Some(l), Some((_, kind, nl))) => {
                nl.as_deref().is_none_or(|nl| nl.eq_ignore_ascii_case(l))
                    || (*kind == NodeKind::Provision
                        && matches!(
                            l.to_lowercase().as_str(),
                            "section" | "subsection" | "article" | "paragraph"
                        ))
            }
            _ => true,
        }
    }

    /// A cited document, or one of its paragraphs when `pin` names one that exists.
    pub fn document(&self, citation: &str, pin: Option<&str>) -> Option<String> {
        let root = self.case(citation)?;
        if let Some(p) = pin {
            let id = format!("{root}:para{p}");
            if self.tree.contains_key(&id) {
                return Some(id);
            }
        }
        Some(root.to_string())
    }

    pub fn case(&self, citation: &str) -> Option<&str> {
        self.cases.get(&citation_key(citation)).map(String::as_str)
    }

    pub fn parent(&self, id: &str) -> Option<&str> {
        self.tree.get(id).and_then(|(p, _, _)| p.as_deref())
    }

    /// Self and ancestors, innermost first.
    pub fn ancestry(&self, id: &str) -> Vec<String> {
        let mut out = vec![id.to_string()];
        let mut cur = id;
        while let Some(p) = self.parent(cur) {
            out.push(p.to_string());
            cur = p;
            if out.len() > 64 {
                break;
            }
        }
        out
    }

    /// Nearest ancestor-or-self that is a provision.
    pub fn enclosing_provision(&self, id: &str) -> Option<String> {
        self.ancestry(id).into_iter().find(|a| {
            self.tree
                .get(a)
                .is_some_and(|(_, k, _)| *k == NodeKind::Provision)
        })
    }

    /// Nearest ancestor-or-self with the given label; the root for "Act"-like labels.
    pub fn enclosing_labelled(&self, id: &str, label: &str) -> Option<String> {
        let l = label.to_lowercase();
        let anc = self.ancestry(id);
        if matches!(
            l.as_str(),
            "act"
                | "code"
                | "instrument"
                | "agreement"
                | "handbook"
                | "policy"
                | "guide"
                | "regulation"
                | "regulations"
                | "rules"
                | "constitution"
        ) && !self.has_label_in(&anc, &l)
        {
            return anc.last().cloned();
        }
        if (l == "section" || l == "article")
            && let Some(p) = self.enclosing_provision(id)
        {
            return Some(p);
        }
        anc.into_iter().find(|a| {
            self.tree
                .get(a)
                .and_then(|(_, _, nl)| nl.as_deref())
                .is_some_and(|nl| nl.eq_ignore_ascii_case(&l))
        })
    }

    fn has_label_in(&self, ids: &[String], l: &str) -> bool {
        ids.iter().any(|a| {
            self.tree
                .get(a)
                .and_then(|(_, _, nl)| nl.as_deref())
                .is_some_and(|nl| nl.eq_ignore_ascii_case(l))
        })
    }
}
