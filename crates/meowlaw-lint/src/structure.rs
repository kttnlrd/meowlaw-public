//! Structure rules: headings the profile recognises, provision numbering,
//! empty and oversized provisions, judgment paragraph numbering, citations
//! and bracket balance. Works on the same blocks the ingester structures.

use crate::text::Lines;
use crate::{Cx, Finding, Loc, Severity, plural};
use meowlaw_core::{CorpusKind, NodeKind, Profile};
use meowlaw_ingest::{Block, Format};
use regex::Regex;
use std::collections::HashMap;
use std::sync::LazyLock;

#[derive(Debug, Clone, PartialEq)]
pub(crate) enum SKind {
    Heading(Option<u8>),
    Para,
    Element {
        depth: u8,
        label: String,
        number: Option<String>,
    },
}

/// A block with its location in the source.
#[derive(Debug, Clone)]
pub(crate) struct SBlock {
    pub kind: SKind,
    pub text: String,
    pub loc: Loc,
}

fn convert(b: Block) -> (SKind, String) {
    match b {
        Block::Heading { depth, text } => (SKind::Heading(depth), text),
        Block::Para(t) => (SKind::Para, t),
        Block::Element {
            depth,
            label,
            number,
            title,
        } => {
            let text = title.unwrap_or_else(|| {
                format!("{label} {}", number.clone().unwrap_or_default())
                    .trim()
                    .to_string()
            });
            (
                SKind::Element {
                    depth,
                    label,
                    number,
                },
                text,
            )
        }
    }
}

/// Blocks without positions (DOCX, HTML, XML).
pub(crate) fn unlocated(blocks: Vec<Block>) -> Vec<SBlock> {
    blocks
        .into_iter()
        .map(|b| {
            let (kind, text) = convert(b);
            SBlock {
                kind,
                text,
                loc: Loc::default(),
            }
        })
        .collect()
}

/// Find each block's first line by scanning forward through the source lines.
pub(crate) fn locate(blocks: Vec<Block>, lines: &Lines, skip: usize) -> Vec<SBlock> {
    let mut cursor = skip;
    let mut out = Vec::with_capacity(blocks.len());
    for b in blocks {
        let (kind, text) = convert(b);
        let first = text.lines().next().unwrap_or("").trim();
        let key: String = first.chars().take(24).collect();
        let mut loc = Loc::default();
        if !key.is_empty() {
            let end = (cursor + 3000).min(lines.len());
            for j in cursor..end {
                let l = lines.lines[j].trim();
                if l.is_empty() {
                    continue;
                }
                if l.contains(key.as_str()) || (l.len() >= 3 && key.starts_with(l)) {
                    loc = lines.loc(j);
                    cursor = j + 1;
                    break;
                }
            }
        }
        out.push(SBlock { kind, text, loc });
    }
    out
}

fn strip_md(s: &str) -> String {
    s.trim()
        .trim_matches('*')
        .trim_matches('_')
        .trim()
        .to_string()
}

const PROVISION_LABELS: &[&str] = &["Section", "Article", "Rule", "Regulation", "Clause"];

#[derive(Debug, Clone)]
struct Heading {
    block: usize,
    label: String,
    rank: u16,
    provision: bool,
    schedule: bool,
    number: Option<String>,
}

fn match_block(profile: &Profile, b: &SBlock, idx: usize) -> Option<Heading> {
    match &b.kind {
        SKind::Heading(_) => profile.match_heading(&strip_md(&b.text)).map(|m| Heading {
            block: idx,
            provision: m.kind == NodeKind::Provision,
            schedule: m.kind == NodeKind::Schedule,
            label: m.label,
            rank: m.rank as u16,
            number: m.number,
        }),
        SKind::Element {
            depth,
            label,
            number,
        } => Some(Heading {
            block: idx,
            label: label.clone(),
            rank: *depth as u16 * 10,
            provision: PROVISION_LABELS.contains(&label.as_str()),
            schedule: matches!(label.as_str(), "Schedule" | "Annex" | "Appendix"),
            number: number.clone(),
        }),
        SKind::Para => None,
    }
}

/// Comparable provision number: `8-1` → [(8,""),(1,"")], `12A` → [(12,"A")], `IV` → [(4,"")].
fn num_key(s: &str) -> Option<Vec<(u64, String)>> {
    let s = s.trim().trim_end_matches('.');
    if s.is_empty() {
        return None;
    }
    if s.chars().all(|c| matches!(c, 'I' | 'V' | 'X' | 'L' | 'C')) {
        return roman(s).map(|n| vec![(n, String::new())]);
    }
    let mut out = Vec::new();
    for seg in s.split(['-', '.', ':']) {
        let digits: String = seg.chars().take_while(|c| c.is_ascii_digit()).collect();
        let rest = seg[digits.len()..].to_uppercase();
        let n = if digits.is_empty() {
            0
        } else {
            digits.parse().ok()?
        };
        out.push((n, rest));
    }
    Some(out)
}

fn roman(s: &str) -> Option<u64> {
    let val = |c| match c {
        'I' => 1,
        'V' => 5,
        'X' => 10,
        'L' => 50,
        'C' => 100,
        _ => 0,
    };
    let v: Vec<u64> = s.chars().map(val).collect();
    if v.contains(&0) {
        return None;
    }
    let mut total = 0i64;
    for (i, x) in v.iter().enumerate() {
        if v.get(i + 1).is_some_and(|n| n > x) {
            total -= *x as i64;
        } else {
            total += *x as i64;
        }
    }
    (total > 0).then_some(total as u64)
}

fn single_int(k: &[(u64, String)]) -> Option<u64> {
    match k {
        [(n, s)] if s.is_empty() => Some(*n),
        _ => None,
    }
}

fn describe(h: &Heading) -> String {
    format!("{} {}", h.label, h.number.as_deref().unwrap_or("?"))
}

static NOT_OPERATIVE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(?:repealed|omitted|not used|reserved|spent|deleted|blank)\b").unwrap()
});

pub(crate) fn check(
    cx: &mut Cx,
    blocks: &[SBlock],
    profile: &Profile,
    kind: Option<CorpusKind>,
    format: Format,
    summary_only: bool,
) {
    cx.stat("blocks", blocks.len());
    let body_chars: usize = blocks.iter().map(|b| b.text.len()).sum();
    if kind == Some(CorpusKind::Case) {
        // A summary of a judgment (content = "summary") has no paragraphs to number.
        if !summary_only {
            check_case(cx, blocks, profile);
        }
    } else {
        check_provisions(cx, blocks, profile, kind, body_chars);
        check_unmarked_headings(cx, blocks, profile, format);
    }
    check_citations(cx, blocks, profile);
    check_balance(cx, blocks, profile, kind);
}

fn check_provisions(
    cx: &mut Cx,
    blocks: &[SBlock],
    profile: &Profile,
    kind: Option<CorpusKind>,
    body_chars: usize,
) {
    // Table-of-contents entries are reported once by `text.table-of-contents`.
    let in_toc = |b: &SBlock| {
        b.loc
            .line
            .is_some_and(|l| cx.toc_lines.iter().any(|(a, z)| (*a..=*z).contains(&l)))
    };
    let heads: Vec<Heading> = blocks
        .iter()
        .enumerate()
        .filter(|(_, b)| !in_toc(b))
        .filter_map(|(i, b)| match_block(profile, b, i))
        .collect();
    let candidates = blocks
        .iter()
        .filter(|b| !matches!(b.kind, SKind::Para))
        .count();
    let provisions = heads.iter().filter(|h| h.provision).count();
    cx.stat("heading_candidates", candidates);
    cx.stat("headings_found", heads.len());
    cx.stat("provisions_found", provisions);
    cx.stat("containers_found", heads.len() - provisions);

    let structured = kind.is_some_and(|k| k.is_structured());
    if heads.is_empty() {
        let depth_headings = blocks
            .iter()
            .filter(|b| matches!(b.kind, SKind::Heading(Some(_))))
            .count();
        let (sev, msg) = if structured {
            (
                Severity::Error,
                format!(
                    "no headings match the `{}` profile: the whole {} will be one root node with no provisions",
                    profile.id(),
                    kind.map(|k| k.as_str()).unwrap_or("document")
                ),
            )
        } else if depth_headings == 0 && body_chars > 6000 {
            (
                Severity::Warning,
                format!(
                    "no headings at all (none marked, none matching the `{}` profile): the text will be cut into arbitrary fixed-size chunks",
                    profile.id()
                ),
            )
        } else if depth_headings == 0 {
            (
                Severity::Info,
                format!("no headings recognised by the `{}` profile", profile.id()),
            )
        } else {
            (Severity::Info, String::new())
        };
        if !msg.is_empty() {
            cx.add(
                Finding::new("structure.no-headings", sev, msg).fix(
                    "Headings must start a line and look like \"Part 2 Liability\", \"Section 5 Definitions\", \"Article 3\" or \"12 Application of Act\" (see the profile); in Markdown mark them with #; in DOCX use Heading styles. Or choose a profile that fits the source.",
                ),
            );
        }
        return;
    }

    // Numbering. Provisions are checked across the whole document (restarting
    // inside each schedule); containers restart under each outer container.
    let mut last: HashMap<String, (Vec<(u64, String)>, usize)> = HashMap::new();
    let mut seen: HashMap<(usize, String, String), usize> = HashMap::new();
    let mut scope = 0usize;
    for (hi, h) in heads.iter().enumerate() {
        if h.schedule {
            scope += 1;
            last.retain(|_, (_, j)| !heads[*j].provision);
        }
        // Containers nest: an outer heading resets numbering of inner containers.
        last.retain(|_, (_, j)| heads[*j].provision || heads[*j].rank <= h.rank);
        let Some(num) = h.number.as_deref() else {
            continue;
        };
        let Some(key) = num_key(num) else { continue };
        let loc = blocks[h.block].loc;
        let excerpt = blocks[h.block].text.as_str();
        if h.provision {
            let k = (scope, h.label.clone(), num.to_uppercase());
            if let Some(first) = seen.get(&k) {
                let first_loc = blocks[heads[*first].block].loc;
                cx.add(
                    Finding::new(
                        "structure.duplicate-number",
                        Severity::Warning,
                        format!(
                            "{} appears twice{}; the second gets a suffixed id and citations to it resolve to the first",
                            describe(h),
                            where_suffix(first_loc)
                        ),
                    )
                    .at(loc)
                    .excerpt(excerpt)
                    .fix("Check for a duplicated page, a running header that looks like a heading, or a numbering typo."),
                );
                last.insert(h.label.clone(), (key, hi));
                continue;
            }
            seen.insert(k, hi);
        }
        if let Some((pk, pj)) = last.get(&h.label) {
            let prev = &heads[*pj];
            if key < *pk {
                cx.add(
                    Finding::new(
                        "structure.numbering-order",
                        Severity::Warning,
                        format!(
                            "{} comes after {}: numbers go backwards (out-of-order pages, a cross-reference read as a heading, or a misread number?)",
                            describe(h),
                            describe(prev)
                        ),
                    )
                    .at(loc)
                    .excerpt(excerpt),
                );
            } else if key == *pk && !h.provision {
                cx.add(
                    Finding::new(
                        "structure.duplicate-number",
                        Severity::Warning,
                        format!("{} repeats the previous {}", describe(h), describe(prev)),
                    )
                    .at(loc)
                    .excerpt(excerpt)
                    .fix("A running header repeating the current Part/Chapter often causes this; remove it."),
                );
            } else if h.provision
                && let (Some(a), Some(b)) = (single_int(pk), single_int(&key))
                && b > a + 1
            {
                cx.add(
                    Finding::new(
                        "structure.numbering-gap",
                        Severity::Info,
                        format!(
                            "{} follows {}: {} missing (repealed, or a heading the profile did not recognise?)",
                            describe(h),
                            describe(prev),
                            plural((b - a - 1) as usize, "number")
                        ),
                    )
                    .at(loc)
                    .excerpt(excerpt),
                );
            }
        }
        last.insert(h.label.clone(), (key, hi));
    }

    // Bodies: empty and oversized provisions.
    let first_head = heads[0].block;
    let preamble: usize = blocks[..first_head].iter().map(|b| b.text.len()).sum();
    if preamble > 50_000 {
        cx.add(
            Finding::new(
                "structure.large-provision",
                Severity::Warning,
                format!(
                    "{} characters of text before the first recognised heading; headings there were probably missed",
                    preamble
                ),
            )
            .at(blocks[0].loc),
        );
    }
    let mut largest = 0usize;
    for (hi, h) in heads.iter().enumerate() {
        let end = heads.get(hi + 1).map(|n| n.block).unwrap_or(blocks.len());
        let body: usize = blocks[h.block + 1..end].iter().map(|b| b.text.len()).sum();
        largest = largest.max(body);
        let b = &blocks[h.block];
        let title_says_empty = NOT_OPERATIVE.is_match(&b.text);
        let last_one = hi + 1 == heads.len();
        if body == 0 && !title_says_empty && (h.provision || last_one) {
            cx.add(
                Finding::new(
                    "structure.empty-provision",
                    Severity::Warning,
                    format!(
                        "{} has no body text{}",
                        describe(h),
                        if h.provision {
                            " (text split off by a page break, or a table-of-contents entry?)"
                        } else {
                            " and nothing follows it"
                        }
                    ),
                )
                .at(b.loc)
                .excerpt(&b.text)
                .fix(
                    "Remove table-of-contents lines before ingesting; check the body was not lost.",
                ),
            );
        }
        if body > 50_000 {
            cx.add(
                Finding::new(
                    "structure.large-provision",
                    Severity::Warning,
                    format!(
                        "{} holds {} characters; headings inside it were probably not recognised",
                        describe(h),
                        body
                    ),
                )
                .at(b.loc)
                .excerpt(&b.text)
                .fix("Check how the headings inside are written against the profile's patterns."),
            );
        }
    }
    cx.stat("largest_provision_chars", largest);
}

fn where_suffix(loc: Loc) -> String {
    match (loc.line, loc.page) {
        (Some(l), _) => format!(" (first at line {l})"),
        (None, Some(p)) => format!(" (first on page {p})"),
        _ => String::new(),
    }
}

/// Paragraphs that look like headings but will not be read as headings, and
/// marked headings the profile does not recognise.
fn check_unmarked_headings(cx: &mut Cx, blocks: &[SBlock], profile: &Profile, format: Format) {
    let how = match format {
        Format::Markdown => "is not marked with #",
        Format::Docx => "is not in a Heading style",
        Format::Html => "is not in an h1–h6 element",
        _ => "",
    };
    let fix = match format {
        Format::Markdown => "Prefix the line with # (or ##, ### by level).",
        Format::Docx => "Apply a Heading 1–9 style to the paragraph in Word.",
        Format::Html => "Wrap it in an h1–h6 element.",
        _ => {
            "Delete the full stop at the end of heading lines (fix the export, or search and replace)."
        }
    };
    let mut n = 0;
    let mut unmatched: Vec<&SBlock> = Vec::new();
    let mut punct_only = 0usize;
    for b in blocks {
        let t = strip_md(&b.text);
        let bare = t.trim_end_matches(['.', ',', ';', ':']).trim_end();
        let punct = bare.len() != t.len();
        match b.kind {
            SKind::Para => {
                if t.contains('\n') || t.chars().count() > 120 || t.starts_with(['-', '|', '(']) {
                    continue;
                }
                // Plain text and PDF lines are heading candidates unless they end in punctuation.
                if how.is_empty() && !punct {
                    continue;
                }
                let Some(m) = profile.match_heading(bare) else {
                    continue;
                };
                n += 1;
                let why = match (how.is_empty(), punct) {
                    (true, _) => "ends with punctuation, and the plain-text reader never treats such a line as a heading".to_string(),
                    (false, true) => format!("{how} (and ends with punctuation)"),
                    (false, false) => how.to_string(),
                };
                cx.add(
                    Finding::new(
                        "structure.unmarked-heading",
                        Severity::Warning,
                        format!(
                            "looks like a {} heading but {why}, so it is read as body text",
                            m.label
                        ),
                    )
                    .at(b.loc)
                    .excerpt(&t)
                    .fix(fix),
                );
            }
            SKind::Heading(Some(_)) if profile.match_heading(&t).is_none() => {
                if punct && profile.match_heading(bare).is_some() {
                    punct_only += 1;
                }
                unmatched.push(b);
            }
            _ => {}
        }
    }
    cx.stat("unmarked_headings", n);
    cx.stat("headings_not_matched", unmatched.len());
    if let Some(first) = unmatched.first() {
        let examples = unmatched
            .iter()
            .take(3)
            .map(|b| format!("\"{}\"", b.text.chars().take(50).collect::<String>()))
            .collect::<Vec<_>>()
            .join(", ");
        let (sev, msg) = if punct_only > 0 {
            (
                Severity::Warning,
                format!(
                    "{} match no level of the `{}` profile; {punct_only} would match without their trailing punctuation (e.g. {examples})",
                    plural(unmatched.len(), "marked heading"),
                    profile.id()
                ),
            )
        } else {
            (
                Severity::Info,
                format!(
                    "{} match no level of the `{}` profile and become untitled containers or chunks rather than provisions (e.g. {examples})",
                    plural(unmatched.len(), "marked heading"),
                    profile.id()
                ),
            )
        };
        cx.add(
            Finding::new("structure.heading-not-matched", sev, msg)
                .at(first.loc)
                .excerpt(&first.text)
                .fix("Fine for descriptive headings; for provisions, write them as the profile expects (\"Section 5 Title\", \"5 Title\", \"Part 2 Title\")."),
        );
    }
}

fn check_case(cx: &mut Cx, blocks: &[SBlock], profile: &Profile) {
    let Some(re) = profile.case_paragraph.as_ref() else {
        return;
    };
    let mut last = 0u32;
    let mut started = false;
    let mut count = 0usize;
    let mut first_other: Option<(u32, Loc, String)> = None;
    // Restart at 1 confirmed once 2 and 3 follow it.
    let mut restart: Option<(Loc, String, u32, u32)> = None;
    for b in blocks {
        if matches!(b.kind, SKind::Heading(Some(_)) | SKind::Element { .. }) {
            continue;
        }
        let Some(n) = re
            .captures(&b.text)
            .and_then(|c| c.name("num"))
            .and_then(|m| m.as_str().parse::<u32>().ok())
        else {
            continue;
        };
        if !started {
            if n == 1 {
                started = true;
                last = 1;
                count = 1;
            } else if n <= 20 && first_other.is_none() {
                first_other = Some((n, b.loc, b.text.clone()));
            }
            continue;
        }
        if let Some((rloc, rtext, expect, seen)) = restart.as_mut()
            && n == *expect
        {
            *expect += 1;
            *seen += 1;
            if *seen == 3 {
                cx.add(
                    Finding::new(
                        "case.paragraph-restart",
                        Severity::Warning,
                        format!("paragraph numbering restarts at 1 after [{last}] (a second judgment or an annexure?); the ingester only accepts increasing numbers, so these paragraphs are merged into [{last}]"),
                    )
                    .at(*rloc)
                    .excerpt(rtext)
                    .fix("Split separately numbered judgments into separate files, or renumber."),
                );
                restart = None;
            }
            continue;
        }
        if n == last + 1 {
            last = n;
            count += 1;
            restart = None;
        } else if n > last + 1 && n <= last + 3 {
            cx.add(
                Finding::new(
                    "case.paragraph-gap",
                    Severity::Info,
                    format!("paragraph [{n}] follows [{last}] ({} missing; the ingester tolerates this)", plural((n - last - 1) as usize, "number")),
                )
                .at(b.loc)
                .excerpt(&b.text),
            );
            last = n;
            count += 1;
        } else if n > last + 3 && n <= last + 20 {
            cx.add(
                Finding::new(
                    "case.paragraph-gap",
                    Severity::Warning,
                    format!("paragraph [{n}] follows [{last}]: the ingester only accepts jumps of up to 3, so [{n}] and everything after it until [{}] appears will be merged into [{last}]", last + 1),
                )
                .at(b.loc)
                .excerpt(&b.text)
                .fix("Find the missing paragraphs (lost page?) or the misread number."),
            );
        } else if n == last {
            cx.add(
                Finding::new(
                    "case.paragraph-restart",
                    Severity::Warning,
                    format!(
                        "paragraph number [{n}] is used twice; the second is merged into the first"
                    ),
                )
                .at(b.loc)
                .excerpt(&b.text),
            );
        } else if n == 1 && last >= 3 {
            restart = Some((b.loc, b.text.clone(), 2, 1));
        }
    }
    cx.stat("case_paragraphs", count);
    if !started {
        let (msg, loc, ex) = match first_other {
            Some((n, loc, text)) => (
                format!("numbered paragraphs start at [{n}], not [1]; the ingester requires [1] first, so no paragraphs will be split out"),
                loc,
                Some(text),
            ),
            None => (
                "no numbered paragraphs found (expected \"1 The appellant…\", \"1. …\" or \"[1] …\" at the start of a paragraph); the judgment will be one undivided block".to_string(),
                Loc::default(),
                None,
            ),
        };
        let mut f = Finding::new("case.no-numbered-paragraphs", Severity::Warning, msg)
            .at(loc)
            .fix("Paragraph numbers must start the paragraph. In DOCX, automatic numbering is not in the text: convert numbering to text first.");
        if let Some(e) = ex {
            f = f.excerpt(&e);
        }
        cx.add(f);
    }
}

/// Patterns that look like citations but are formatted so the profile will
/// not match them. Each is only flagged when no profile pattern matches it.
static CITE_SUSPECTS: LazyLock<Vec<(Regex, &'static str)>> = LazyLock::new(|| {
    [
        (r"\[(?:1[89]|20)[0-9]{2}\][A-Z][A-Za-z]{1,9}\s*[0-9]{1,5}", "add a space after the year: \"[2024] HCA 1\""),
        (r"\[(?:1[89]|20)[0-9]{2}\]\s+[A-Z][A-Za-z]{0,8}[A-Z][0-9]{1,5}\b", "add a space before the number: \"[2024] HCA 1\""),
        (r"\[(?:1[89]|20)[0-9]{2}\s+[A-Z][A-Za-z]{1,9}[A-Z]\s+[0-9]{1,5}\b", "close the year bracket: \"[2024] HCA 1\""),
        (r"\((?:1[89]|20)[0-9]{2}\)[0-9]{1,4}\s+[A-Z][A-Za-z]{1,6}\s+[0-9]{1,5}", "add a space after the year: \"(1995) 184 CLR 1\""),
        (r"\b(?:s|ss|sec|cl|r|reg|art)\.\s*[0-9][0-9A-Za-z]*(?:[-.][0-9]+[A-Za-z]*)*", "write it as the profile expects, e.g. \"s 8-1\""),
        (r"\b(?:s|ss|section)\s+[0-9]+[A-Z]{0,3}\s+-\s*[0-9]+\b|\b(?:s|ss|section)\s+[0-9]+[A-Z]{0,3}-\s+[0-9]+\b", "remove the space around the hyphen: \"s 8-1\""),
    ]
    .into_iter()
    .map(|(p, f)| (Regex::new(p).unwrap(), f))
    .collect()
});

fn check_citations(cx: &mut Cx, blocks: &[SBlock], profile: &Profile) {
    let mut n = 0;
    for b in blocks {
        for (re, fix) in CITE_SUSPECTS.iter() {
            for m in re.find_iter(&b.text) {
                let s = m.as_str();
                let covered = profile
                    .citations
                    .iter()
                    .any(|c| c.re.find(s).is_some_and(|cm| cm.len() * 10 >= s.len() * 8));
                if covered {
                    continue;
                }
                n += 1;
                cx.add(
                    Finding::new(
                        "citation.malformed",
                        Severity::Info,
                        format!("\"{s}\" looks like a citation but the `{}` profile will not recognise it, so it will not be linked", profile.id()),
                    )
                    .at(b.loc)
                    .excerpt(s)
                    .fix(*fix),
                );
            }
        }
    }
    cx.stat("citations_malformed", n);
}

static LIST_MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?m)^\s*[a-zA-Z0-9]{1,4}\)\s").unwrap());

/// Describe an imbalance of (), [], “” or an odd number of straight quotes.
fn imbalance(text: &str) -> Option<String> {
    let text = LIST_MARKER.replace_all(text, "");
    let (mut po, mut pc, mut bo, mut bc, mut qo, mut qc, mut sq) = (0, 0, 0, 0, 0, 0, 0);
    for c in text.chars() {
        match c {
            '(' => po += 1,
            ')' => pc += 1,
            '[' => bo += 1,
            ']' => bc += 1,
            '\u{201c}' => qo += 1,
            '\u{201d}' => qc += 1,
            '"' => sq += 1,
            _ => {}
        }
    }
    let mut parts = Vec::new();
    if po != pc {
        parts.push(format!("{po} '(' vs {pc} ')'"));
    }
    if bo != bc {
        parts.push(format!("{bo} '[' vs {bc} ']'"));
    }
    // Multi-paragraph quotations open each paragraph and close only the last.
    if qc > qo || qo > qc + 1 {
        parts.push(format!("{qo} '“' vs {qc} '”'"));
    }
    if sq % 2 == 1 {
        parts.push(format!("{sq} straight '\"'"));
    }
    (!parts.is_empty()).then(|| parts.join(", "))
}

fn check_balance(cx: &mut Cx, blocks: &[SBlock], profile: &Profile, kind: Option<CorpusKind>) {
    // Per provision when there are provisions; otherwise per paragraph (one
    // count over a whole unstructured document says nothing).
    let per_provision = kind.is_none_or(|k| k.is_structured())
        && blocks
            .iter()
            .enumerate()
            .any(|(i, b)| match_block(profile, b, i).is_some());
    let mut emit = |cx: &mut Cx, text: &str, loc: Loc, what: &str| {
        if let Some(d) = imbalance(text) {
            cx.add(
                Finding::new(
                    "structure.unbalanced-brackets",
                    Severity::Warning,
                    format!("unbalanced brackets or quotes in {what}: {d} (text lost or merged at a page break, or an OCR error?)"),
                )
                .at(loc)
                .excerpt(text.lines().next().unwrap_or("")),
            );
        }
    };
    if per_provision {
        let mut buf = String::new();
        let mut start: Option<usize> = None;
        let flush = |cx: &mut Cx,
                     buf: &mut String,
                     start: Option<usize>,
                     emit: &mut dyn FnMut(&mut Cx, &str, Loc, &str)| {
            if !buf.is_empty() {
                let (loc, what) = match start {
                    Some(i) => (
                        blocks[i].loc,
                        format!(
                            "\"{}\"",
                            blocks[i].text.chars().take(60).collect::<String>()
                        ),
                    ),
                    None => (
                        Loc::default(),
                        "the text before the first heading".to_string(),
                    ),
                };
                emit(cx, buf, loc, &what);
                buf.clear();
            }
        };
        for (i, b) in blocks.iter().enumerate() {
            if match_block(profile, b, i).is_some() {
                flush(cx, &mut buf, start, &mut emit);
                start = Some(i);
                continue;
            }
            buf.push_str(&b.text);
            buf.push('\n');
        }
        flush(cx, &mut buf, start, &mut emit);
    } else {
        for b in blocks {
            if b.kind == SKind::Para {
                emit(cx, &b.text, b.loc, "this paragraph");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn number_keys() {
        assert!(num_key("8-1") < num_key("8-5"));
        assert!(num_key("12") < num_key("12A"));
        assert!(num_key("12A") < num_key("13"));
        assert!(num_key("IV") > num_key("III"));
        assert!(num_key("5.3.2") > num_key("5.3"));
        assert_eq!(single_int(&num_key("7").unwrap()), Some(7));
        assert_eq!(single_int(&num_key("8-1").unwrap()), None);
    }

    #[test]
    fn imbalance_detection() {
        assert!(imbalance("A person (the taxpayer) may [deduct] “x”.").is_none());
        assert!(imbalance("A person (the taxpayer may deduct").is_some());
        assert!(imbalance("a) first\nb) second").is_none());
        assert!(imbalance("“Opening of a quotation that continues").is_none());
        assert!(imbalance("he said \"hello").is_some());
    }
}
