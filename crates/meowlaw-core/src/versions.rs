//! Versions of a work: language versions and point-in-time compilations.
//!
//! Each version is its own corpus. Corpora that are versions of the same
//! instrument share a `work` (corpus metadata), and may set `language`. Their
//! `as_at` dates order the compilations: within one work and language, the
//! version with the latest `as_at` is current and the others are superseded.
//! Dates are ISO 8601 (`YYYY-MM-DD`) and compare as text.

use crate::Corpus;
use std::collections::{BTreeMap, HashSet};

pub fn work_of(c: &Corpus) -> Option<&str> {
    c.meta.get("work").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
}

pub fn language_of(c: &Corpus) -> Option<&str> {
    c.meta.get("language").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
}

/// Versions grouped by (work, language), oldest first.
fn groups<'a>(corpora: impl IntoIterator<Item = &'a Corpus>) -> BTreeMap<(String, String), Vec<&'a Corpus>> {
    let mut g: BTreeMap<(String, String), Vec<&Corpus>> = BTreeMap::new();
    for c in corpora {
        if let Some(w) = work_of(c) {
            g.entry((w.to_string(), language_of(c).unwrap_or("").to_string()))
                .or_default()
                .push(c);
        }
    }
    for v in g.values_mut() {
        v.sort_by(|a, b| a.as_at.cmp(&b.as_at).then(a.id.cmp(&b.id)));
    }
    g
}

/// Corpora replaced by a later compilation of the same work and language.
pub fn superseded<'a>(corpora: impl IntoIterator<Item = &'a Corpus>) -> HashSet<String> {
    groups(corpora)
        .into_values()
        .flat_map(|v| {
            let n = v.len();
            v.into_iter().take(n.saturating_sub(1)).map(|c| c.id.clone())
        })
        .collect()
}

/// Corpora not in force on `date`: for each work and language, every
/// version except the latest with `as_at <= date` (all of them when none
/// is that old). Corpora without a `work` are always in force.
pub fn not_in_force_at<'a>(corpora: impl IntoIterator<Item = &'a Corpus>, date: &str) -> HashSet<String> {
    let mut out = HashSet::new();
    for v in groups(corpora).into_values() {
        let keep = v
            .iter()
            .rposition(|c| c.as_at.as_deref().is_some_and(|d| d <= date))
            // A version with no date is taken to be in force at any date.
            .or_else(|| v.iter().position(|c| c.as_at.is_none()));
        for (i, c) in v.iter().enumerate() {
            if Some(i) != keep {
                out.insert(c.id.clone());
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{CorpusKind, Meta};

    fn c(id: &str, work: &str, lang: &str, as_at: &str) -> Corpus {
        let mut meta = Meta::new();
        meta.insert("work".into(), work.into());
        if !lang.is_empty() {
            meta.insert("language".into(), lang.into());
        }
        Corpus {
            id: id.into(),
            title: id.into(),
            kind: CorpusKind::Legislation,
            jurisdiction: None,
            profile: "generic".into(),
            aliases: vec![],
            citations: vec![],
            source_url: None,
            as_at: Some(as_at.into()),
            licence: None,
            meta,
        }
    }

    #[test]
    fn current_and_point_in_time() {
        let cs = vec![
            c("act-2020", "act", "en", "2020-01-01"),
            c("act-2023", "act", "en", "2023-07-01"),
            c("act-2025", "act", "en", "2025-03-01"),
            c("act-fr-2025", "act", "fr", "2025-03-01"),
        ];
        let s = superseded(&cs);
        assert_eq!(s, HashSet::from(["act-2020".to_string(), "act-2023".to_string()]));
        let at = not_in_force_at(&cs, "2024-01-01");
        assert!(!at.contains("act-2023"));
        assert!(at.contains("act-2025") && at.contains("act-2020"));
        // No French version existed yet.
        assert!(at.contains("act-fr-2025"));
    }
}
