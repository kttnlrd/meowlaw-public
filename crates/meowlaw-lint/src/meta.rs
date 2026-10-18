//! Metadata (front matter, sidecars, `corpus.toml`) and Markdown syntax rules.

use crate::text::Lines;
use crate::{Cx, Finding, Loc, Severity, builtin_profile_ids, plural};
use meowlaw_core::CorpusKind;
use meowlaw_ingest::{Format, Manifest};
use regex::Regex;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

/// Fields of [`Manifest`]; anything else in front matter is silently ignored.
const KNOWN_KEYS: &[&str] = &[
    "id",
    "title",
    "kind",
    "jurisdiction",
    "profile",
    "aliases",
    "citations",
    "source_url",
    "as_at",
    "licence",
    "files",
    "format",
    "summary",
    "summary_source",
    "content",
    "work",
    "language",
    "meta",
];

/// Common names people use for manifest fields.
const KEY_ALIASES: &[(&str, &str)] = &[
    ("license", "licence"),
    ("licensed", "licence"),
    ("copyright", "licence"),
    ("date", "as_at"),
    ("as_of", "as_at"),
    ("asat", "as_at"),
    ("as-at", "as_at"),
    ("current_as_at", "as_at"),
    ("url", "source_url"),
    ("source", "source_url"),
    ("link", "source_url"),
    ("type", "kind"),
    ("category", "kind"),
    ("name", "title"),
    ("alias", "aliases"),
    ("citation", "citations"),
    ("cite", "citations"),
    ("country", "jurisdiction"),
    ("jurisdictions", "jurisdiction"),
    ("abstract", "summary"),
    ("description", "summary"),
    ("tags", "meta"),
    ("author", "meta"),
    ("authors", "meta"),
];

const RECOMMENDED: &[&str] = &["title", "kind", "licence", "as_at", "jurisdiction"];

pub(crate) struct FrontMatter {
    pub present: bool,
    pub manifest: Option<Manifest>,
    /// Index of the first body line (0 when there is no front matter).
    pub body_start_line: usize,
}

/// Find and check `+++` TOML front matter (Markdown and plain text).
pub(crate) fn front_matter(cx: &mut Cx, src: &str, format: Format) -> FrontMatter {
    let mut out = FrontMatter {
        present: false,
        manifest: None,
        body_start_line: 0,
    };
    let s = src.trim_start_matches('\u{feff}');
    let opens = s.starts_with("+++\n") || s.starts_with("+++\r\n");
    if !opens {
        let first = s.lines().position(|l| !l.trim().is_empty());
        if let Some(i) = first
            && s.lines().nth(i).is_some_and(|l| l.trim() == "+++")
        {
            cx.add(
                Finding::new(
                    "meta.front-matter-not-at-start",
                    Severity::Error,
                    "`+++` front matter must start on the very first line; as written it is ingested as body text and its metadata ignored",
                )
                .at(Loc::line(i + 1))
                .fix("Delete the blank lines or whitespace before the opening +++."),
            );
        } else if (s.starts_with("---\n") || s.starts_with("---\r\n"))
            && s.lines().skip(1).take(60).any(|l| l.trim_end() == "---")
        {
            cx.add(
                Finding::new(
                    "meta.yaml-front-matter",
                    Severity::Warning,
                    "YAML (---) front matter is not read: MeowLaw uses TOML front matter between +++ lines, so this block is ingested as text",
                )
                .at(Loc::line(1))
                .fix("Rewrite as TOML between +++ lines, e.g. title = \"…\" (strings need quotes)."),
            );
        }
        return out;
    }
    let (fm, _) = meowlaw_ingest::blocks::front_matter(src);
    let Some(fm) = fm else {
        cx.add(
            Finding::new(
                "meta.front-matter-unterminated",
                Severity::Error,
                "front matter opened with +++ is never closed; the whole file will be treated as body text",
            )
            .at(Loc::line(1))
            .fix("Add a closing +++ line after the metadata."),
        );
        return out;
    };
    out.present = true;
    out.body_start_line = fm.split('\n').count() + 2;
    if format == Format::Text {
        cx.add(
            Finding::new(
                "meta.front-matter-in-body",
                Severity::Warning,
                "the plain-text reader does not strip front matter: its metadata is used, but the +++ block is also ingested as the first lines of the text",
            )
            .at(Loc::line(1))
            .fix("Rename the file to .md, or move the metadata to a sidecar <file>.meta.toml."),
        );
    }
    out.manifest = parse_manifest_toml(cx, fm, "front matter", Some(1));
    if let Some(m) = &out.manifest {
        validate(cx, m, "front matter", Loc::line(1));
    }
    out
}

/// Parse TOML into a manifest, reporting syntax errors, type errors and unknown keys.
/// `line_offset`: file line of the first TOML line minus one, or `None` when the
/// TOML lives in another file (no line numbers then).
fn parse_manifest_toml(
    cx: &mut Cx,
    src: &str,
    origin: &str,
    line_offset: Option<usize>,
) -> Option<Manifest> {
    let table: toml::Table = match toml::from_str(src) {
        Ok(t) => t,
        Err(e) => {
            let line = line_offset.and_then(|off| {
                e.span()
                    .map(|s| src[..s.start.min(src.len())].matches('\n').count() + 1 + off)
            });
            cx.add(
                Finding::new(
                    "meta.front-matter-parse",
                    Severity::Error,
                    format!("{origin} is not valid TOML; ingest will fail: {}", one_line(&e.to_string())),
                )
                .at(Loc { line, page: None })
                .fix("Strings need double quotes (title = \"…\"), lists use [\"a\", \"b\"], keys use = not :."),
            );
            return None;
        }
    };
    check_keys(
        cx,
        table.keys().map(String::as_str),
        origin,
        line_offset,
        src,
    );
    match toml::from_str::<Manifest>(src) {
        Ok(m) => Some(m),
        Err(e) => {
            cx.add(
                Finding::new(
                    "meta.front-matter-parse",
                    Severity::Error,
                    format!("{origin} has a field of the wrong type; ingest will fail: {}", one_line(&e.to_string())),
                )
                .at(Loc {
                    line: line_offset.map(|o| o + 1),
                    page: None,
                })
                .fix("aliases, citations and files are lists of strings; meta is a table; everything else is a string."),
            );
            None
        }
    }
}

fn one_line(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn check_keys<'a>(
    cx: &mut Cx,
    keys: impl Iterator<Item = &'a str>,
    origin: &str,
    line_offset: Option<usize>,
    src: &str,
) {
    for k in keys {
        if KNOWN_KEYS.contains(&k) {
            continue;
        }
        let suggestion = KEY_ALIASES
            .iter()
            .find(|(a, _)| a.eq_ignore_ascii_case(k))
            .map(|(_, t)| *t)
            .or_else(|| {
                KNOWN_KEYS
                    .iter()
                    .copied()
                    .filter(|known| edit_distance(&k.to_lowercase(), known) <= 2)
                    .min_by_key(|known| edit_distance(&k.to_lowercase(), known))
            });
        let line = line_offset.and_then(|off| {
            src.lines()
                .position(|l| l.trim_start().starts_with(k))
                .map(|i| i + 1 + off)
        });
        let fix = match suggestion {
            Some("meta") => format!("Put free-form fields under a [meta] table: [meta] {k} = …"),
            Some(s) => format!("Did you mean `{s}`?"),
            None => format!(
                "Known fields: {}. Put anything else under [meta].",
                KNOWN_KEYS.join(", ")
            ),
        };
        cx.add(
            Finding::new(
                "meta.unknown-key",
                Severity::Warning,
                format!("{origin} key `{k}` is not a manifest field and will be silently ignored"),
            )
            .at(Loc { line, page: None })
            .fix(fix),
        );
    }
}

fn edit_distance(a: &str, b: &str) -> usize {
    let b: Vec<char> = b.chars().collect();
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    for (i, ca) in a.chars().enumerate() {
        let mut cur = vec![i + 1; b.len() + 1];
        for (j, cb) in b.iter().enumerate() {
            cur[j + 1] = (prev[j] + usize::from(ca != *cb))
                .min(prev[j + 1] + 1)
                .min(cur[j] + 1);
        }
        prev = cur;
    }
    prev[b.len()]
}

static ISO_DATE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$").unwrap());

fn validate(cx: &mut Cx, m: &Manifest, origin: &str, loc: Loc) {
    let bad = |cx: &mut Cx, sev: Severity, msg: String, fix: &str| {
        cx.add(
            Finding::new("meta.invalid-value", sev, format!("{origin}: {msg}"))
                .at(loc)
                .fix(fix),
        );
    };
    if let Some(k) = &m.kind
        && CorpusKind::parse(k).is_none()
    {
        bad(
            cx,
            Severity::Error,
            format!("kind = \"{k}\" is not a corpus kind; ingest will fail"),
            "Use one of: legislation, instrument, rule_set, case, treaty, guidance, secondary.",
        );
    }
    if let Some(f) = &m.format
        && Format::parse(f).is_none()
    {
        bad(
            cx,
            Severity::Error,
            format!("format = \"{f}\" is not a known format; ingest will fail"),
            "Use one of: markdown, text, html, xml, docx, pdf, json.",
        );
    }
    if let Some(id) = &m.id
        && (id.is_empty() || id.contains(':') || id.contains('/'))
    {
        bad(
            cx,
            Severity::Error,
            format!("id = \"{id}\" must be a non-empty slug without `:` or `/`; ingest will fail"),
            "Use a slug such as itaa-1997.",
        );
    }
    if let Some(c) = &m.content
        && c != "full"
        && c != "summary"
    {
        bad(
            cx,
            Severity::Warning,
            format!("content = \"{c}\" is neither \"full\" nor \"summary\"; treated as full text"),
            "Use content = \"full\" or content = \"summary\".",
        );
    }
    if let Some(d) = &m.as_at
        && !ISO_DATE.is_match(d.trim())
    {
        bad(
            cx,
            Severity::Info,
            format!("as_at = \"{d}\" is not an ISO date (YYYY-MM-DD)"),
            "Write dates as YYYY-MM-DD so they sort and compare correctly.",
        );
    }
    if let Some(u) = &m.source_url
        && !(u.starts_with("http://") || u.starts_with("https://"))
    {
        bad(
            cx,
            Severity::Info,
            format!("source_url = \"{u}\" is not an http(s) URL"),
            "Give the full URL including https://.",
        );
    }
    if !m.files.is_empty() && origin != "corpus.toml" {
        bad(
            cx,
            Severity::Warning,
            "`files` is only used in a directory's corpus.toml; it is ignored here".into(),
            "Move `files` to corpus.toml.",
        );
    }
    if let Some(p) = &m.profile {
        let ids = builtin_profile_ids();
        if !ids.iter().any(|i| i == p) {
            cx.add(
                Finding::new(
                    "meta.unknown-profile",
                    Severity::Warning,
                    format!(
                        "{origin}: profile = \"{p}\" is not a built-in profile ({}); ingest fails unless it is in your profiles directory. Linting with `generic`.",
                        ids.join(", ")
                    ),
                )
                .at(loc),
            );
        }
    }
}

fn sidecar_paths(path: &Path) -> [PathBuf; 2] {
    let base = path.with_extension("");
    [
        PathBuf::from(format!("{}.meta.toml", base.display())),
        PathBuf::from(format!("{}.meta.json", base.display())),
    ]
}

/// Merge corpus.toml < sidecar < front matter, reporting problems with each,
/// and flag missing recommended metadata.
pub(crate) fn collect(cx: &mut Cx, front: Option<Manifest>, front_present: bool) -> Manifest {
    let mut merged = Manifest::default();
    let mut any = front_present;
    if let Some(dir) = cx.path.parent() {
        let ct = dir.join("corpus.toml");
        if ct.is_file() {
            any = true;
            if let Ok(src) = std::fs::read_to_string(&ct) {
                let origin = "corpus.toml";
                if let Some(m) = parse_manifest_toml(cx, &src, origin, None) {
                    validate(cx, &m, origin, Loc::default());
                    merged = merged.merge(m);
                }
            }
        }
    }
    let mut sidecar_found = false;
    for p in sidecar_paths(cx.path) {
        if !p.is_file() {
            continue;
        }
        sidecar_found = true;
        any = true;
        let name = p
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("sidecar")
            .to_string();
        let Ok(src) = std::fs::read_to_string(&p) else {
            continue;
        };
        let parsed = if name.ends_with(".json") {
            match serde_json::from_str::<serde_json::Value>(&src) {
                Err(e) => {
                    cx.add(
                        Finding::new(
                            "meta.front-matter-parse",
                            Severity::Error,
                            format!("sidecar {name} is not valid JSON; ingest will fail: {e}"),
                        )
                        .fix("Fix the JSON syntax."),
                    );
                    None
                }
                Ok(v) => {
                    if let Some(obj) = v.as_object() {
                        check_keys(
                            cx,
                            obj.keys().map(String::as_str),
                            &format!("sidecar {name}"),
                            None,
                            "",
                        );
                    }
                    match serde_json::from_value::<Manifest>(v) {
                        Ok(m) => Some(m),
                        Err(e) => {
                            cx.add(Finding::new(
                                "meta.front-matter-parse",
                                Severity::Error,
                                format!("sidecar {name}: {e}; ingest will fail"),
                            ));
                            None
                        }
                    }
                }
            }
        } else {
            parse_manifest_toml(cx, &src, &format!("sidecar {name}"), None)
        };
        if let Some(m) = parsed {
            validate(cx, &m, &format!("sidecar {name}"), Loc::default());
            merged = merged.merge(m);
        }
        break;
    }
    if let Some(f) = front {
        merged = merged.merge(f);
    }
    cx.stat("has_metadata", any);
    cx.stat("has_sidecar", sidecar_found);
    let missing: Vec<&str> = RECOMMENDED
        .iter()
        .copied()
        .filter(|k| match *k {
            "title" => merged.title.is_none(),
            "kind" => merged.kind.is_none(),
            "licence" => merged.licence.is_none(),
            "as_at" => merged.as_at.is_none(),
            "jurisdiction" => merged.jurisdiction.is_none(),
            _ => false,
        })
        .collect();
    if !any {
        let textual = matches!(
            Format::from_path(cx.path),
            Some(Format::Markdown | Format::Text)
        );
        let how = if textual {
            "Add TOML front matter between +++ lines at the top, or a sidecar <file>.meta.toml"
        } else {
            "Add a sidecar <file>.meta.toml next to the file"
        };
        cx.add(
            Finding::new(
                "meta.no-front-matter",
                Severity::Info,
                "no metadata (front matter, sidecar or corpus.toml): title, id and kind will be guessed from the file name and first heading; kind defaults to secondary",
            )
            .fix(format!(
                "{how} with at least: {}.",
                RECOMMENDED.join(", ")
            )),
        );
    } else if !missing.is_empty() {
        cx.add(
            Finding::new(
                "meta.missing-recommended",
                Severity::Info,
                format!("recommended metadata missing: {}", missing.join(", ")),
            )
            .fix("Add them to the front matter or sidecar; kind drives how structure is detected."),
        );
    }
    merged
}

// ---------------------------------------------------------------------------
// Markdown syntax
// ---------------------------------------------------------------------------

static RAW_HTML: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)</?(?:div|span|p|br|hr|table|thead|tbody|tr|td|th|sup|sub|a|b|i|u|em|strong|font|img|ul|ol|li|h[1-6]|center|blockquote|pre|code|section|article|small|big|abbr|ins|del|mark|style|script)\b[^<>]*>|<!--").unwrap()
});
static LINK_EMPTY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\[[^\]]*\]\(\s*\)").unwrap());
static LINK_UNCLOSED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\]\((?:https?://|www\.|/|\.|#)[^)\s]*$").unwrap());
static LINK_SPACED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\]\s+\((?:https?://|www\.)").unwrap());
static SETEXT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?:=+|-{2,})$").unwrap());

pub(crate) fn check_markdown(cx: &mut Cx, lines: &Lines, skip: usize) {
    let mut fence_open: Option<usize> = None;
    let mut tilde_seen = false;
    let mut prev_level = 0u8;
    let mut html_lines: Vec<usize> = Vec::new();
    let mut html_count = 0;
    let mut headings = 0;
    for i in skip..lines.len() {
        let line = lines.lines[i];
        let t = line.trim();
        if t.starts_with("```") {
            fence_open = match fence_open {
                Some(_) => None,
                None => Some(i),
            };
            continue;
        }
        if fence_open.is_some() {
            continue;
        }
        if t.starts_with("~~~") && !tilde_seen {
            tilde_seen = true;
            cx.add(
                Finding::new(
                    "md.tilde-fence",
                    Severity::Info,
                    "~~~ code fences are not recognised by the ingester; their content is treated as ordinary text (lines inside may become headings)",
                )
                .at(lines.loc(i))
                .fix("Use ``` fences."),
            );
        }
        // ATX headings, as the ingester reads them.
        let lead = line.trim_start();
        let hashes = lead.bytes().take_while(|b| *b == b'#').count();
        if (1..=6).contains(&hashes) {
            let rest = &lead[hashes..];
            if rest.starts_with(' ') || rest.is_empty() {
                let text = rest.trim().trim_end_matches('#').trim();
                if text.trim_matches('*').trim().is_empty() {
                    cx.add(
                        Finding::new(
                            "md.empty-heading",
                            Severity::Warning,
                            "heading with no text",
                        )
                        .at(lines.loc(i))
                        .excerpt(line),
                    );
                }
                if rest.starts_with(' ') {
                    headings += 1;
                    let level = hashes as u8;
                    if prev_level > 0 && level > prev_level + 1 {
                        cx.add(
                            Finding::new(
                                "md.heading-level-jump",
                                Severity::Warning,
                                format!("heading level jumps from {prev_level} to {level}; for secondary material nesting follows heading depth, so this heading will sit under the wrong parent"),
                            )
                            .at(lines.loc(i))
                            .excerpt(line)
                            .fix(format!("Use {} for this heading, or add the missing intermediate level.", "#".repeat(prev_level as usize + 1))),
                        );
                    }
                    prev_level = level;
                }
            } else if rest.chars().next().is_some_and(|c| c.is_alphanumeric()) {
                cx.add(
                    Finding::new(
                        "md.heading-no-space",
                        Severity::Warning,
                        "line starts with # but has no space after it, so it is not a heading",
                    )
                    .at(lines.loc(i))
                    .excerpt(line)
                    .fix(format!(
                        "Write `{} {}`.",
                        &lead[..hashes],
                        rest.trim()
                    )),
                );
            }
        }
        // Setext headings (underlined with === or ---).
        if SETEXT.is_match(t)
            && i > skip
            && let Some(prev) = lines.lines.get(i - 1)
        {
            let p = prev.trim();
            if !p.is_empty()
                && !p.starts_with(['#', '|', '-', '*', '>', '+'])
                && !p.starts_with("```")
                && !SETEXT.is_match(p)
            {
                cx.add(
                    Finding::new(
                        "md.setext-heading",
                        Severity::Warning,
                        "underlined (setext) heading: the ingester only recognises # headings, so this is body text and the underline becomes text",
                    )
                    .at(lines.loc(i - 1))
                    .excerpt(p)
                    .fix(format!("Write `{} {p}` and delete the underline (or put a blank line above a --- rule).", if t.starts_with('=') { "#" } else { "##" })),
                );
            }
        }
        if RAW_HTML.is_match(line) {
            html_count += RAW_HTML.find_iter(line).count();
            if html_lines.len() < 8 {
                html_lines.push(i);
            }
        }
        if LINK_EMPTY.is_match(line) {
            cx.add(
                Finding::new(
                    "md.broken-link",
                    Severity::Warning,
                    "link with an empty target: [text]()",
                )
                .at(lines.loc(i))
                .excerpt(line),
            );
        } else if LINK_UNCLOSED.is_match(line) {
            cx.add(
                Finding::new(
                    "md.broken-link",
                    Severity::Warning,
                    "link target is not closed with )",
                )
                .at(lines.loc(i))
                .excerpt(line),
            );
        } else if LINK_SPACED.is_match(line) {
            cx.add(
                Finding::new(
                    "md.broken-link",
                    Severity::Warning,
                    "space between ] and ( so this is not a link",
                )
                .at(lines.loc(i))
                .excerpt(line),
            );
        }
    }
    if let Some(open) = fence_open {
        cx.add(
            Finding::new(
                "md.unclosed-fence",
                Severity::Error,
                format!(
                    "code fence opened here is never closed: the remaining {} of the file become one code paragraph and no headings after it are detected",
                    plural(lines.len() - open, "line")
                ),
            )
            .at(lines.loc(open))
            .excerpt(lines.lines[open])
            .fix("Add a closing ``` line (or remove the stray fence)."),
        );
    }
    if html_count > 0 {
        cx.add(
            Finding::new(
                "md.raw-html",
                Severity::Info,
                format!(
                    "{} raw HTML tags/comments {}; the ingester keeps them as literal text",
                    html_count,
                    lines.where_(&html_lines)
                ),
            )
            .at(lines.loc(html_lines[0]))
            .excerpt(lines.lines[html_lines[0]])
            .fix("Convert to Markdown (<b> → **, <br> → line break, tables → | rows) or delete."),
        );
    }
    cx.stat("markdown_headings", headings);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every `Manifest` field must be a known key, or the linter tells people
    /// to move a working field under `[meta]`.
    #[test]
    fn known_keys_cover_every_manifest_field() {
        let v = serde_json::to_value(Manifest::default()).unwrap();
        for k in v.as_object().unwrap().keys() {
            assert!(
                KNOWN_KEYS.contains(&k.as_str()),
                "Manifest field `{k}` is missing from KNOWN_KEYS"
            );
        }
    }

    #[test]
    fn distance() {
        assert_eq!(edit_distance("licnce", "licence"), 1);
        assert_eq!(edit_distance("title", "title"), 0);
    }
}
