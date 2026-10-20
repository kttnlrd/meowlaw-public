//! `meowlaw sync`: bring the store in line with a list of sources.
//!
//! - **Update** (default): a source whose files have not changed since the
//!   last sync is skipped (content hash). Changed sources are diffed against
//!   the store (provisions added, removed, changed; citations elsewhere that
//!   will break) and then replaced.
//! - **Fresh** (`fresh`): every listed source is removed and ingested again.
//! - **Dry run**: report the diffs, change nothing.
//!
//! After applying, extraction and the search index are rebuilt once, and the
//! report lists citations that became unresolved or resolved.

use anyhow::{Context, Result};
use meowlaw_core::ProfileRegistry;
use meowlaw_ingest::{Ingester, Manifest};
use meowlaw_store::DocDiff;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use crate::{Engine, RebuildReport};

/// `sources.toml`: one `[[source]]` per file or directory, with any manifest
/// field as an override (kind, profile, id, title, jurisdiction, ...).
#[derive(Debug, Deserialize)]
pub struct SourcesFile {
    #[serde(rename = "source", default)]
    pub sources: Vec<SourceEntry>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SourceEntry {
    pub path: PathBuf,
    #[serde(flatten)]
    pub manifest: Manifest,
}

impl SourcesFile {
    pub fn load(path: &Path) -> Result<(Self, PathBuf)> {
        let src =
            std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        let f: SourcesFile =
            toml::from_str(&src).with_context(|| format!("parsing {}", path.display()))?;
        let base = path.parent().map(Path::to_path_buf).unwrap_or_default();
        Ok((f, base))
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct SyncOptions {
    pub fresh: bool,
    pub dry_run: bool,
    /// Re-ingest even when the files are unchanged.
    pub force: bool,
    /// Skip linting changed sources.
    pub skip_lint: bool,
}

/// Lint results for one source: counts and the worst findings.
#[derive(Debug, Serialize)]
pub struct LintSummary {
    pub errors: usize,
    pub warnings: usize,
    /// Errors and warnings, worst first, at most 20: (file, rule, line, message).
    pub findings: Vec<(String, String, Option<usize>, String)>,
}

fn lint_source(path: &Path, m: &Manifest, registry: &ProfileRegistry) -> Option<LintSummary> {
    use meowlaw_lint::Severity;
    let opts = meowlaw_lint::LintOptions {
        profile: m.profile.as_deref().and_then(|p| registry.get(p).ok()),
        kind: m.kind.as_deref().and_then(meowlaw_core::CorpusKind::parse),
    };
    let reports = meowlaw_lint::lint_path(path, &opts).ok()?;
    let mut findings: Vec<_> = reports
        .iter()
        .flat_map(|r| r.findings.iter().map(move |f| (r, f)))
        .filter(|(_, f)| f.severity != Severity::Info)
        .collect();
    findings.sort_by_key(|(_, f)| f.severity);
    Some(LintSummary {
        errors: reports.iter().map(|r| r.count(Severity::Error)).sum(),
        warnings: reports.iter().map(|r| r.count(Severity::Warning)).sum(),
        findings: findings
            .into_iter()
            .take(20)
            .map(|(r, f)| (r.path.clone(), f.rule.clone(), f.line, f.message.clone()))
            .collect(),
    })
}

#[derive(Debug, Serialize)]
pub struct SourceResult {
    pub path: String,
    /// `unchanged`, `new`, `updated`, `no_changes`, `error`.
    pub status: String,
    pub diffs: Vec<DocDiff>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lint: Option<LintSummary>,
}

#[derive(Debug, Serialize)]
pub struct SyncReport {
    pub mode: String,
    pub started_unix: u64,
    pub sources: Vec<SourceResult>,
    pub rebuild: Option<RebuildReport>,
    pub unresolved_before: usize,
    pub unresolved_after: usize,
    /// Citations that resolved before this sync and do not now (source node, citation).
    pub newly_unresolved: Vec<(String, String)>,
    pub newly_resolved: usize,
}

impl SyncReport {
    pub fn changed(&self) -> bool {
        self.sources
            .iter()
            .any(|s| s.status == "new" || s.status == "updated")
    }
}

/// Hash of every content file (and sidecar/manifest) under `path`.
pub fn source_hash(path: &Path) -> Result<String> {
    let mut files: Vec<PathBuf> = if path.is_dir() {
        walkdir::WalkDir::new(path)
            .sort_by_file_name()
            .into_iter()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_type().is_file())
            .map(|e| e.into_path())
            .collect()
    } else {
        let mut v = vec![path.to_path_buf()];
        let stem = path.with_extension("");
        for ext in ["meta.toml", "meta.json"] {
            let p = PathBuf::from(format!("{}.{ext}", stem.display()));
            if p.exists() {
                v.push(p);
            }
        }
        v
    };
    files.sort();
    let mut h = Sha256::new();
    for f in files {
        h.update(f.to_string_lossy().as_bytes());
        h.update(std::fs::read(&f).with_context(|| format!("reading {}", f.display()))?);
    }
    Ok(h.finalize().iter().map(|b| format!("{b:02x}")).collect())
}

/// Hash of the content plus the per-source manifest overrides.
///
/// The bytes on disk are not the whole input: `sources.toml` can change the
/// kind, profile, id, aliases or format without touching the file. Hashing only
/// the file left those edits invisible, so sync reported "unchanged" and the
/// corpus kept the old settings.
pub fn source_hash_with_manifest(path: &Path, manifest: &Manifest) -> Result<String> {
    let mut h = Sha256::new();
    h.update(source_hash(path)?.as_bytes());
    h.update(serde_json::to_vec(manifest).context("serialising the source manifest")?);
    Ok(h.finalize().iter().map(|b| format!("{b:02x}")).collect())
}

fn unresolved_set(engine: &Engine) -> Result<BTreeSet<(String, String)>> {
    let c = engine.store.conn()?;
    let mut st = c.prepare("SELECT source, raw FROM refs WHERE target IS NULL")?;
    let rows = st
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

pub fn sync(
    engine: &Engine,
    registry: &ProfileRegistry,
    sources: &[SourceEntry],
    base: &Path,
    opts: SyncOptions,
) -> Result<SyncReport> {
    let started_unix = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let before = unresolved_set(engine)?;
    let ing = Ingester::new(registry.clone());
    let mut results = Vec::new();
    let mut applied = false;
    for src in sources {
        let path = if src.path.is_absolute() {
            src.path.clone()
        } else {
            base.join(&src.path)
        };
        let key = format!("source_hash:{}", path.display());
        let res: Result<SourceResult> = (|| {
            let hash = source_hash_with_manifest(&path, &src.manifest)?;
            let unchanged = engine.store.get_kv(&key)?.as_deref() == Some(hash.as_str());
            if unchanged && !opts.force && !opts.fresh {
                return Ok(SourceResult {
                    path: path.display().to_string(),
                    status: "unchanged".into(),
                    diffs: vec![],
                    error: None,
                    lint: None,
                });
            }
            let lint = if opts.skip_lint {
                None
            } else {
                lint_source(&path, &src.manifest, registry)
            };
            let docs = ing.ingest_path(&path, &src.manifest)?;
            let mut diffs = Vec::new();
            for d in &docs {
                meowlaw_ingest::validate(d)?;
                diffs.push(engine.store.diff_document(d)?);
            }
            let any_change = opts.fresh || diffs.iter().any(|d| !d.is_empty());
            let status = if diffs.iter().all(|d| d.new_corpus) {
                "new"
            } else if any_change {
                "updated"
            } else {
                "no_changes"
            };
            if !opts.dry_run {
                for d in &docs {
                    if opts.fresh {
                        engine.store.delete_corpus(&d.corpus.id)?;
                    }
                    if opts.fresh
                        || diffs
                            .iter()
                            .any(|x| x.corpus == d.corpus.id && !x.is_empty())
                    {
                        engine.store.put_document(d)?;
                    }
                }
                engine.store.set_kv(&key, &hash)?;
            }
            Ok(SourceResult {
                path: path.display().to_string(),
                status: status.into(),
                diffs,
                error: None,
                lint,
            })
        })();
        match res {
            Ok(r) => {
                applied |= !opts.dry_run && (r.status == "new" || r.status == "updated");
                results.push(r);
            }
            Err(e) => results.push(SourceResult {
                path: path.display().to_string(),
                status: "error".into(),
                diffs: vec![],
                error: Some(format!("{e:#}")),
                lint: None,
            }),
        }
    }
    let rebuild = if applied {
        Some(crate::rebuild(engine)?)
    } else {
        None
    };
    let after = if applied {
        unresolved_set(engine)?
    } else {
        before.clone()
    };
    Ok(SyncReport {
        mode: match (opts.fresh, opts.dry_run) {
            (_, true) => "dry-run",
            (true, false) => "fresh",
            (false, false) => "update",
        }
        .into(),
        started_unix,
        unresolved_before: before.len(),
        unresolved_after: after.len(),
        newly_unresolved: after.difference(&before).take(500).cloned().collect(),
        newly_resolved: before.difference(&after).count(),
        sources: results,
        rebuild,
    })
}

impl SyncReport {
    /// Markdown report, written to `<data_dir>/reports/` by the CLI.
    pub fn to_markdown(&self) -> String {
        let mut o = format!(
            "# Sync report\n\nMode: {}. Started (unix time): {}.\n\n| Source | Status | Added | Changed | Removed | Breaks | Lint |\n|---|---|---|---|---|---|---|\n",
            self.mode, self.started_unix
        );
        for s in &self.sources {
            let sum = |f: fn(&DocDiff) -> usize| s.diffs.iter().map(f).sum::<usize>();
            o.push_str(&format!(
                "| {} | {} | {} | {} | {} | {} | {} |\n",
                s.path,
                s.status,
                sum(|d| d.added.len()),
                sum(|d| d.changed.len()),
                sum(|d| d.removed.len()),
                sum(|d| d.breaks.len()),
                s.lint.as_ref().map_or(String::new(), |l| format!(
                    "{} errors, {} warnings",
                    l.errors, l.warnings
                )),
            ));
        }
        for s in &self.sources {
            if let Some(e) = &s.error {
                o.push_str(&format!("\n**Error in {}:** {e}\n", s.path));
            }
            if let Some(l) = s.lint.as_ref().filter(|l| !l.findings.is_empty()) {
                o.push_str(&format!(
                    "\n**Lint ({}):** run `meowlaw lint` on it for details.\n\n",
                    s.path
                ));
                for (file, rule, line, msg) in &l.findings {
                    let at = line.map(|n| format!(":{n}")).unwrap_or_default();
                    o.push_str(&format!("- `{rule}` {file}{at}: {msg}\n"));
                }
            }
            for d in &s.diffs {
                if d.is_empty() {
                    continue;
                }
                o.push_str(&format!("\n## {}\n\n", d.corpus));
                let list = |o: &mut String, title: &str, ids: &[String]| {
                    if !ids.is_empty() {
                        o.push_str(&format!(
                            "**{title}** ({}): {}\n\n",
                            ids.len(),
                            ids.iter().take(50).cloned().collect::<Vec<_>>().join(", ")
                        ));
                    }
                };
                if d.metadata_changed {
                    o.push_str("Metadata changed.\n\n");
                }
                list(&mut o, "Added", &d.added);
                list(&mut o, "Changed", &d.changed);
                list(&mut o, "Removed", &d.removed);
                if !d.breaks.is_empty() {
                    o.push_str(&format!(
                        "**Citations that will no longer resolve** ({}):\n\n",
                        d.breaks.len()
                    ));
                    for b in d.breaks.iter().take(50) {
                        o.push_str(&format!("- {} cites {} ({})\n", b.source, b.target, b.raw));
                    }
                    o.push('\n');
                }
            }
        }
        o.push_str(&format!(
            "\nUnresolved citations: {} before, {} after ({} newly resolved).\n",
            self.unresolved_before, self.unresolved_after, self.newly_resolved
        ));
        if !self.newly_unresolved.is_empty() {
            o.push_str("\n**Newly unresolved:**\n\n");
            for (s, r) in self.newly_unresolved.iter().take(100) {
                o.push_str(&format!("- {s}: {r}\n"));
            }
        }
        o
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_edits_change_the_source_hash() {
        let dir = std::env::temp_dir().join(format!(
            "meowlaw-hash-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("act.md");
        std::fs::write(&file, "1 Short title\n").unwrap();

        let legislation = Manifest {
            kind: Some("legislation".into()),
            ..Default::default()
        };
        let case = Manifest {
            kind: Some("case".into()),
            ..Default::default()
        };

        let first = source_hash_with_manifest(&file, &legislation).unwrap();
        assert_eq!(
            first,
            source_hash_with_manifest(&file, &legislation).unwrap(),
            "the same inputs must hash the same"
        );
        assert_ne!(
            first,
            source_hash_with_manifest(&file, &case).unwrap(),
            "a changed manifest must not report the source as unchanged"
        );
        std::fs::remove_dir_all(&dir).ok();
    }
}
