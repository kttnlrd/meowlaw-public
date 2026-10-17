//! Pre-ingest linter for legal source files.
//!
//! Tells a user what is wrong with a file *before* it is ingested: encoding
//! damage, invisible characters, page furniture left over from PDF exports,
//! flattened tables, headings the profile will not recognise, numbering that
//! goes backwards, tracked changes and automatic numbering hidden in DOCX, and
//! so on. Every finding carries a stable rule id (see [`RULES`]), a severity,
//! a location where one is known and, where possible, a hint on how to fix it.
//!
//! The linter reads files with the same readers as `meowlaw-ingest`, so what it
//! reports about structure is what the ingester will actually see.
//!
//! [`fix_text`] applies the safe, wording-preserving subset of fixes to plain
//! text and Markdown.

mod docx;
mod fix;
mod markup;
mod meta;
mod pdf;
mod structure;
mod text;

use anyhow::{Context, Result};
use meowlaw_core::{CorpusKind, Profile, ProfileRegistry};
use meowlaw_ingest::Format;
use serde::{Serialize, Serializer};
use std::collections::HashMap;
use std::path::Path;
use std::sync::{LazyLock, Mutex};

pub use fix::fix_text;

/// How bad a finding is.
///
/// * `Error`: ingest will fail or silently produce badly wrong output.
/// * `Warning`: ingest works but the result will be degraded (missed
///   headings, garbage in the text, lost numbers).
/// * `Info`: worth knowing; usually harmless.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Severity {
    Error,
    Warning,
    Info,
}

impl Severity {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Error => "error",
            Self::Warning => "warning",
            Self::Info => "info",
        }
    }
}

/// One problem found in a file.
#[derive(Debug, Clone, Serialize)]
pub struct Finding {
    /// Stable rule id, e.g. `pdf.no-text-layer`.
    pub rule: String,
    pub severity: Severity,
    pub message: String,
    /// 1-based line in the file (text formats) where known.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line: Option<usize>,
    /// 1-based page (PDF, or text with form feeds) where known.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page: Option<usize>,
    /// The offending text, with invisible characters made visible.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub excerpt: Option<String>,
    /// A human hint on how to fix it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fix: Option<String>,
}

/// Lint result for one file.
#[derive(Debug, Clone, Serialize)]
pub struct Report {
    pub path: String,
    pub format: String,
    /// Sorted: errors first, then warnings, then info; by position within each.
    pub findings: Vec<Finding>,
    /// Counts and measurements (chars, lines, pages, headings found, ...).
    pub stats: serde_json::Value,
}

impl Report {
    /// Number of findings at a severity.
    pub fn count(&self, severity: Severity) -> usize {
        self.findings
            .iter()
            .filter(|f| f.severity == severity)
            .count()
    }
}

/// What to lint against. Both default to what the file's own metadata
/// (front matter, sidecar, `corpus.toml`) says, then to the `generic` profile
/// and an unknown kind.
#[derive(Debug, Clone, Default, Serialize)]
pub struct LintOptions {
    #[serde(serialize_with = "ser_profile")]
    pub profile: Option<Profile>,
    pub kind: Option<CorpusKind>,
}

fn ser_profile<S: Serializer>(p: &Option<Profile>, s: S) -> Result<S::Ok, S::Error> {
    match p {
        Some(p) => s.serialize_some(p.id()),
        None => s.serialize_none(),
    }
}

/// Every rule the linter can emit: (id, one-line description).
pub const RULES: &[(&str, &str)] = &[
    ("file.empty", "File is empty or whitespace only"),
    ("file.tiny", "File has very little text"),
    (
        "file.unsupported-format",
        "Extension is not an ingestible format",
    ),
    (
        "encoding.utf16",
        "File is UTF-16; the ingester expects UTF-8",
    ),
    (
        "encoding.invalid-utf8",
        "Not valid UTF-8; the ingester will fall back to Latin-1",
    ),
    (
        "encoding.mojibake",
        "Double-encoded UTF-8 such as \"â€™\" or \"Ã©\"",
    ),
    ("encoding.bom", "Byte-order mark at the start of the file"),
    ("chars.nul", "NUL characters"),
    ("chars.control", "Control characters (C0/C1)"),
    ("chars.bom", "Byte-order mark inside the text"),
    (
        "chars.nbsp",
        "Non-breaking or other unusual space characters",
    ),
    (
        "chars.soft-hyphen",
        "Soft hyphens (invisible, break search)",
    ),
    (
        "chars.zero-width",
        "Zero-width characters (invisible, break search)",
    ),
    (
        "chars.ligature",
        "Typographic ligatures such as \"ﬁ\" (break search)",
    ),
    (
        "chars.private-use",
        "Private-use code points (symbol-font glyphs)",
    ),
    (
        "chars.replacement",
        "U+FFFD replacement characters (text already lost)",
    ),
    (
        "whitespace.mixed-line-endings",
        "Mixture of CRLF, LF and CR line endings",
    ),
    ("whitespace.cr-line-endings", "Old Mac CR-only line endings"),
    ("whitespace.tabs", "Tabs inside prose"),
    ("whitespace.trailing", "Many lines with trailing whitespace"),
    (
        "lines.very-long",
        "Very long lines (no line or paragraph breaks)",
    ),
    ("quotes.mixed", "Mixture of straight and curly quotes"),
    (
        "text.hyphenated-line-break",
        "Words split across lines by a hyphen",
    ),
    ("text.duplicate-paragraph", "Paragraph repeated verbatim"),
    (
        "text.table-of-contents",
        "Table of contents (entries become empty or duplicate headings)",
    ),
    ("furniture.page-number", "Bare page-number lines"),
    ("furniture.page-x-of-y", "\"Page X of Y\" lines"),
    (
        "furniture.repeated-line",
        "Running header/footer repeated through the text",
    ),
    (
        "table.column-layout",
        "Lines laid out in columns (a table flattened to text)",
    ),
    (
        "table.flattened",
        "Run of short cell-like lines (a table flattened one cell per line)",
    ),
    (
        "md.table-columns",
        "Markdown table rows with inconsistent column counts",
    ),
    (
        "md.table-no-separator",
        "Markdown table without a header separator row",
    ),
    (
        "md.heading-level-jump",
        "Markdown heading skips a level (# then ###)",
    ),
    (
        "md.heading-no-space",
        "\"#Heading\" without a space is not a heading",
    ),
    ("md.empty-heading", "Markdown heading with no text"),
    (
        "md.setext-heading",
        "Underlined (setext) heading; only # headings are recognised",
    ),
    ("md.unclosed-fence", "Code fence never closed"),
    (
        "md.tilde-fence",
        "~~~ fences are not treated as code by the ingester",
    ),
    ("md.raw-html", "Raw HTML tags or comments in Markdown"),
    ("md.broken-link", "Malformed Markdown link"),
    (
        "meta.no-front-matter",
        "No front matter or sidecar metadata",
    ),
    (
        "meta.yaml-front-matter",
        "YAML (---) front matter; MeowLaw uses TOML (+++)",
    ),
    (
        "meta.front-matter-unterminated",
        "+++ front matter never closed",
    ),
    (
        "meta.front-matter-not-at-start",
        "+++ front matter not on the first line",
    ),
    (
        "meta.front-matter-in-body",
        "Front matter in a .txt file is also ingested as text",
    ),
    (
        "meta.front-matter-parse",
        "Front matter / sidecar is not valid TOML/JSON for a manifest",
    ),
    (
        "meta.unknown-key",
        "Metadata key the manifest does not know (silently ignored)",
    ),
    (
        "meta.invalid-value",
        "Metadata value the ingester will reject",
    ),
    (
        "meta.unknown-profile",
        "Profile id that is not a built-in profile",
    ),
    (
        "meta.missing-recommended",
        "Recommended metadata missing (title, kind, licence, ...)",
    ),
    (
        "structure.no-headings",
        "No headings recognised by the profile",
    ),
    (
        "structure.numbering-order",
        "Provision number lower than the previous one",
    ),
    (
        "structure.duplicate-number",
        "Same provision number used twice",
    ),
    (
        "structure.numbering-gap",
        "Numbers skipped between consecutive provisions",
    ),
    ("structure.empty-provision", "Heading with no body text"),
    (
        "structure.large-provision",
        "Provision over 50,000 characters (missed headings?)",
    ),
    (
        "structure.unmarked-heading",
        "Body paragraph that looks like a heading",
    ),
    (
        "structure.heading-not-matched",
        "Marked heading that matches no profile level",
    ),
    (
        "structure.unbalanced-brackets",
        "Unbalanced brackets or quotes in a provision",
    ),
    (
        "citation.malformed",
        "Citation-like text the profile will not recognise",
    ),
    (
        "case.no-numbered-paragraphs",
        "Judgment with no numbered paragraphs",
    ),
    ("case.paragraph-gap", "Judgment paragraph numbers skip"),
    (
        "case.paragraph-restart",
        "Judgment paragraph numbering restarts or repeats",
    ),
    (
        "pdf.pdftotext-missing",
        "pdftotext (poppler-utils) not installed",
    ),
    (
        "pdf.pdfinfo-missing",
        "pdfinfo not installed (page checks degraded)",
    ),
    ("pdf.not-pdf", "File does not start with a PDF header"),
    ("pdf.encrypted", "PDF is encrypted"),
    ("pdf.extract-failed", "pdftotext failed"),
    ("pdf.no-text-layer", "No text layer at all (scanned)"),
    ("pdf.empty-pages", "Pages with no text"),
    ("pdf.low-text-density", "Pages with very little text"),
    (
        "pdf.two-column",
        "Two-column layout (lines will interleave)",
    ),
    ("pdf.garbled", "Letter-spaced, rotated or garbled text"),
    ("pdf.rotated", "Rotated pages"),
    ("pdf.ocr", "Text layer produced by OCR"),
    (
        "pdf.paragraph-numbers-dropped",
        "Bare number lines inside text that the PDF reader deletes",
    ),
    ("docx.not-zip", "Not a ZIP container"),
    ("docx.no-document-xml", "No word/document.xml"),
    ("docx.xml-parse", "word/document.xml is not well-formed"),
    ("docx.tracked-changes", "Unresolved tracked changes"),
    ("docx.comments", "Comments present (not ingested)"),
    (
        "docx.no-heading-styles",
        "No Heading/Title styles: no headings will be detected",
    ),
    (
        "docx.custom-heading-styles",
        "Heading-like styles the ingester does not recognise",
    ),
    (
        "docx.auto-numbering",
        "Automatic numbering: numbers are NOT in the text",
    ),
    ("docx.fields", "Field codes (cross-references, TOC) present"),
    (
        "docx.footnotes",
        "Footnotes/endnotes present (not ingested)",
    ),
    ("docx.tables", "Tables present (flattened to paragraphs)"),
    ("docx.text-boxes", "Text boxes (read out of order)"),
    ("docx.hidden-text", "Hidden text (ingested as if visible)"),
    ("docx.images-only", "Images but no text (scanned document)"),
    ("html.no-blocks", "No paragraphs or headings found"),
    (
        "html.text-outside-blocks",
        "Visible text outside p/li/h*/tr elements is dropped",
    ),
    ("html.parse-errors", "HTML parse errors"),
    ("html.tables", "Tables present (flattened to rows)"),
    ("html.not-html", "Does not look like HTML"),
    ("xml.parse", "XML is not well-formed"),
    (
        "xml.unknown-schema",
        "Not Akoma Ntoso/USLM-like (no num/heading elements)",
    ),
    ("xml.unknown-entity", "Named entities the reader drops"),
    ("xml.missing-num", "Structural elements without a <num>"),
    ("json.invalid", "Not a valid MeowLaw document JSON"),
];

/// Lint a single file.
pub fn lint_file(path: &Path, opts: &LintOptions) -> Result<Report> {
    let md = std::fs::metadata(path).with_context(|| format!("reading {}", path.display()))?;
    anyhow::ensure!(md.is_file(), "{} is not a file", path.display());
    let format = Format::from_path(path);
    let mut cx = Cx::new(path);
    cx.stat("bytes", md.len());
    let fmt_name = match format {
        None => {
            cx.sink.add(
                Finding::new(
                    "file.unsupported-format",
                    Severity::Error,
                    "file extension is not an ingestible format",
                )
                .fix("Use .md, .txt, .html, .xml, .docx, .pdf or .json, or pass the format explicitly when ingesting."),
            );
            "unknown"
        }
        Some(f) => {
            match f {
                Format::Markdown | Format::Text => lint_textual(&mut cx, f, opts)?,
                Format::Html => markup::lint_html(&mut cx, opts)?,
                Format::Xml => markup::lint_xml(&mut cx, opts)?,
                Format::Docx => docx::lint(&mut cx, opts)?,
                Format::Pdf => pdf::lint(&mut cx, opts)?,
                Format::Json => lint_json(&mut cx),
            }
            format_name(f)
        }
    };
    Ok(cx.finish(fmt_name))
}

/// Lint a file, or every supported file under a directory (skipping
/// `*.meta.*` sidecars, `corpus.toml` and hidden files).
pub fn lint_path(path: &Path, opts: &LintOptions) -> Result<Vec<Report>> {
    if !path.is_dir() {
        return Ok(vec![lint_file(path, opts)?]);
    }
    let mut files: Vec<_> = walkdir::WalkDir::new(path)
        .sort_by_file_name()
        .into_iter()
        .filter_entry(|e| {
            e.depth() == 0 || !e.file_name().to_str().is_some_and(|n| n.starts_with('.'))
        })
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .map(|e| e.into_path())
        .filter(|p| is_lintable(p))
        .collect();
    files.sort();
    files.iter().map(|f| lint_file(f, opts)).collect()
}

fn is_lintable(p: &Path) -> bool {
    let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
    if name.starts_with('.') || name == "corpus.toml" || name.contains(".meta.") {
        return false;
    }
    Format::from_path(p).is_some()
}

fn format_name(f: Format) -> &'static str {
    match f {
        Format::Markdown => "markdown",
        Format::Text => "text",
        Format::Html => "html",
        Format::Xml => "xml",
        Format::Docx => "docx",
        Format::Pdf => "pdf",
        Format::Json => "json",
    }
}

// ---------------------------------------------------------------------------
// Shared plumbing
// ---------------------------------------------------------------------------

/// Detailed findings kept per rule; the rest are summarised in one line.
const MAX_PER_RULE: usize = 25;

impl Finding {
    pub(crate) fn new(rule: &str, severity: Severity, message: impl Into<String>) -> Self {
        Self {
            rule: rule.to_string(),
            severity,
            message: message.into(),
            line: None,
            page: None,
            excerpt: None,
            fix: None,
        }
    }
    pub(crate) fn at(mut self, loc: Loc) -> Self {
        self.line = loc.line;
        self.page = loc.page;
        self
    }
    pub(crate) fn excerpt(mut self, s: &str) -> Self {
        self.excerpt = Some(excerpt(s));
        self
    }
    pub(crate) fn fix(mut self, s: impl Into<String>) -> Self {
        self.fix = Some(s.into());
        self
    }
}

/// A position in the source.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub(crate) struct Loc {
    pub line: Option<usize>,
    pub page: Option<usize>,
}

impl Loc {
    pub fn line(l: usize) -> Self {
        Self {
            line: Some(l),
            page: None,
        }
    }
}

#[derive(Default)]
pub(crate) struct Sink {
    findings: Vec<Finding>,
    counts: HashMap<String, (usize, Severity)>,
}

impl Sink {
    pub fn add(&mut self, f: Finding) {
        let e = self.counts.entry(f.rule.clone()).or_insert((0, f.severity));
        e.0 += 1;
        e.1 = e.1.min(f.severity);
        if e.0 <= MAX_PER_RULE {
            self.findings.push(f);
        }
    }

    fn finish(mut self) -> Vec<Finding> {
        let mut extra: Vec<Finding> = self
            .counts
            .iter()
            .filter(|(_, (n, _))| *n > MAX_PER_RULE)
            .map(|(rule, (n, _))| {
                Finding::new(
                    rule,
                    Severity::Info,
                    format!("{} more `{rule}` findings not shown", n - MAX_PER_RULE),
                )
            })
            .collect();
        extra.sort_by(|a, b| a.rule.cmp(&b.rule));
        self.findings.extend(extra);
        // Stable: equal keys keep emission order.
        self.findings
            .sort_by_key(|f| (f.severity, f.page.unwrap_or(0), f.line.unwrap_or(0)));
        self.findings
    }
}

/// Per-file lint context.
pub(crate) struct Cx<'a> {
    pub path: &'a Path,
    pub sink: Sink,
    pub stats: serde_json::Map<String, serde_json::Value>,
    /// 1-based inclusive line ranges holding a table of contents.
    pub toc_lines: Vec<(usize, usize)>,
}

impl<'a> Cx<'a> {
    fn new(path: &'a Path) -> Self {
        Self {
            path,
            sink: Sink::default(),
            stats: serde_json::Map::new(),
            toc_lines: Vec::new(),
        }
    }

    pub fn stat(&mut self, k: &str, v: impl Into<serde_json::Value>) {
        self.stats.insert(k.to_string(), v.into());
    }

    pub fn add(&mut self, f: Finding) {
        self.sink.add(f);
    }

    fn finish(mut self, format: &str) -> Report {
        let findings = std::mem::take(&mut self.sink).finish();
        let n = |s| findings.iter().filter(|f| f.severity == s).count();
        self.stat("errors", n(Severity::Error));
        self.stat("warnings", n(Severity::Warning));
        self.stat("infos", n(Severity::Info));
        Report {
            path: self.path.display().to_string(),
            format: format.to_string(),
            findings,
            stats: serde_json::Value::Object(self.stats),
        }
    }
}

/// Make invisible characters visible and keep excerpts short.
pub(crate) fn excerpt(s: &str) -> String {
    const MAX: usize = 160;
    let mut out = String::new();
    for (n, c) in s.trim_end_matches(['\r', '\n']).chars().enumerate() {
        if n >= MAX {
            out.push('…');
            break;
        }
        match c {
            '\t' => out.push('→'),
            '\u{a0}' => out.push_str("⟨NBSP⟩"),
            '\u{ad}' => out.push_str("⟨SHY⟩"),
            '\u{feff}' => out.push_str("⟨BOM⟩"),
            '\u{200b}' | '\u{200c}' | '\u{200d}' | '\u{2060}' => out.push_str("⟨ZW⟩"),
            '\u{0}' => out.push_str("⟨NUL⟩"),
            c if c.is_control() || ('\u{2000}'..='\u{200a}').contains(&c) => {
                out.push_str(&format!("⟨U+{:04X}⟩", c as u32))
            }
            c => out.push(c),
        }
    }
    out
}

/// Plural helper: `plural(3, "line")` → "3 lines".
pub(crate) fn plural(n: usize, word: &str) -> String {
    if n == 1 {
        format!("1 {word}")
    } else {
        format!("{n} {word}s")
    }
}

/// "3, 7, 12" plus "…" when more.
pub(crate) fn list_nums(v: &[usize], max: usize) -> String {
    let mut s = v
        .iter()
        .take(max)
        .map(|n| n.to_string())
        .collect::<Vec<_>>()
        .join(", ");
    if v.len() > max {
        s.push_str(", …");
    }
    s
}

static REGISTRY: LazyLock<Option<ProfileRegistry>> =
    LazyLock::new(|| ProfileRegistry::builtin().ok());
static PROFILES: LazyLock<Mutex<HashMap<String, Profile>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// A built-in profile by id, compiled once.
pub(crate) fn builtin_profile(id: &str) -> Option<Profile> {
    if let Some(p) = PROFILES.lock().ok()?.get(id) {
        return Some(p.clone());
    }
    let p = REGISTRY.as_ref()?.get(id).ok()?;
    if let Ok(mut m) = PROFILES.lock() {
        m.insert(id.to_string(), p.clone());
    }
    Some(p)
}

pub(crate) fn builtin_profile_ids() -> Vec<String> {
    REGISTRY.as_ref().map(|r| r.ids()).unwrap_or_default()
}

/// Profile and kind to lint against: options, then file metadata, then generic.
pub(crate) fn resolve_target(
    cx: &mut Cx,
    opts: &LintOptions,
    m: &meowlaw_ingest::Manifest,
) -> Result<(Profile, Option<CorpusKind>)> {
    let kind = opts
        .kind
        .or_else(|| m.kind.as_deref().and_then(CorpusKind::parse));
    let profile = match &opts.profile {
        Some(p) => p.clone(),
        None => {
            let id = m.profile.as_deref().unwrap_or("generic");
            match builtin_profile(id) {
                Some(p) => p,
                None => builtin_profile("generic").context("built-in `generic` profile")?,
            }
        }
    };
    cx.stat("profile", profile.id());
    cx.stat("kind", kind.map(|k| k.as_str()).unwrap_or("unknown"));
    Ok((profile, kind))
}

/// Markdown and plain text.
fn lint_textual(cx: &mut Cx, format: Format, opts: &LintOptions) -> Result<()> {
    let bytes = std::fs::read(cx.path).with_context(|| format!("reading {}", cx.path.display()))?;
    let decoded = text::decode(&bytes);
    text::report_decoding(cx, &decoded, &bytes);
    let src = decoded.text.as_str();
    if text::check_empty(cx, src) {
        return Ok(());
    }
    let fm = meta::front_matter(cx, src, format);
    let manifest = meta::collect(cx, fm.manifest.clone(), fm.present);
    let (profile, kind) = resolve_target(cx, opts, &manifest)?;

    // Body line numbers are file line numbers: lint the whole file, but skip
    // the front matter lines for text rules.
    let lines = text::Lines::from_text(src, false);
    let rules = text::TextRules {
        hygiene: true,
        furniture: true,
        hyphenation: true,
        layout_tables: format == Format::Text,
        markdown: format == Format::Markdown,
        bom_matters: format == Format::Text,
        skip_lines: fm.body_start_line,
    };
    text::check_text(cx, &lines, src, &rules);
    if format == Format::Markdown {
        meta::check_markdown(cx, &lines, fm.body_start_line);
    }
    let blocks = if format == Format::Markdown {
        meowlaw_ingest::blocks::markdown(src)
    } else {
        // The ingester does not strip front matter from plain text bodies in
        // the reader; mirror it exactly.
        meowlaw_ingest::blocks::plain_text(src)
    };
    let sblocks = structure::locate(blocks, &lines, fm.body_start_line);
    let paras = text::paragraphs_from_lines(&lines, fm.body_start_line);
    text::check_duplicates(cx, &paras);
    let summary_only = manifest.content.as_deref() == Some("summary");
    structure::check(cx, &sblocks, &profile, kind, format, summary_only);
    Ok(())
}

fn lint_json(cx: &mut Cx) {
    match meowlaw_ingest::read_json_documents(cx.path) {
        Ok(docs) => {
            cx.stat("documents", docs.len());
            cx.stat("nodes", docs.iter().map(|d| d.nodes.len()).sum::<usize>());
        }
        Err(e) => cx.add(
            Finding::new(
                "json.invalid",
                Severity::Error,
                format!("not a valid MeowLaw document: {e:#}"),
            )
            .fix("JSON input must be one Document ({corpus, nodes}) or an array of them."),
        ),
    }
}
