//! End-to-end lint tests on small inline fixtures.

use meowlaw_core::{CorpusKind, ProfileRegistry};
use meowlaw_lint::{LintOptions, Report, Severity, fix_text, lint_file, lint_path};
use std::io::Write;
use std::path::{Path, PathBuf};

fn write(dir: &Path, name: &str, bytes: &[u8]) -> PathBuf {
    let p = dir.join(name);
    std::fs::write(&p, bytes).unwrap();
    p
}

fn lint(dir: &Path, name: &str, src: &str) -> Report {
    lint_opts(dir, name, src, &LintOptions::default())
}

fn lint_opts(dir: &Path, name: &str, src: &str, opts: &LintOptions) -> Report {
    let p = write(dir, name, src.as_bytes());
    lint_file(&p, opts).unwrap()
}

fn kind(k: CorpusKind) -> LintOptions {
    LintOptions {
        profile: None,
        kind: Some(k),
    }
}

fn rules(r: &Report) -> Vec<&str> {
    r.findings.iter().map(|f| f.rule.as_str()).collect()
}

fn has(r: &Report, rule: &str) -> bool {
    r.findings.iter().any(|f| f.rule == rule)
}

fn get<'a>(r: &'a Report, rule: &str) -> &'a meowlaw_lint::Finding {
    r.findings
        .iter()
        .find(|f| f.rule == rule)
        .unwrap_or_else(|| panic!("no {rule} in {:#?}", rules(r)))
}

#[track_caller]
fn assert_rules(r: &Report, want: &[&str]) {
    for w in want {
        assert!(has(r, w), "expected `{w}`; got {:#?}", r.findings);
    }
}

#[track_caller]
fn assert_no_problems(r: &Report) {
    let bad: Vec<_> = r
        .findings
        .iter()
        .filter(|f| f.severity != Severity::Info)
        .collect();
    assert!(bad.is_empty(), "unexpected findings: {bad:#?}");
}

const CLEAN_MD: &str = r#"+++
title = "Example Act 2024"
kind = "legislation"
jurisdiction = "AU"
licence = "CC BY 4.0"
as_at = "2024-07-01"
+++

# Part 1 Preliminary

## 1 Short title

This Act may be cited as the Example Act 2024.

## 2 Commencement

This Act commences on the day after it receives the Royal Assent.

# Part 2 Liability

## 3 Liability to pay

(1) A person who carries on a business (the "operator") must pay the levy.

(2) Subsection (1) does not apply to a person exempted under section 4.

## 4 Exemptions

The Minister may, by legislative instrument, exempt a class of persons.
"#;

#[test]
fn clean_markdown_has_no_problems() {
    let d = tempfile::tempdir().unwrap();
    let r = lint(d.path(), "act.md", CLEAN_MD);
    assert_no_problems(&r);
    assert_eq!(r.format, "markdown");
    assert_eq!(r.stats["provisions_found"], 4);
    assert_eq!(r.stats["containers_found"], 2);
    assert_eq!(r.stats["kind"], "legislation");
    // Stats and serialisation.
    let v = serde_json::to_value(&r).unwrap();
    assert_eq!(v["format"], "markdown");
    assert!(v["stats"]["chars"].as_u64().unwrap() > 100);
}

#[test]
fn text_hygiene_rules() {
    let d = tempfile::tempdir().unwrap();
    // A long repeated paragraph (short repeated subparagraphs are normal in statutes).
    let dup = "The Commissioner may, by written notice, require a person to give information relevant to an assessment. ".repeat(4);
    let src = format!(
        "\u{feff}Section 1 Interpretation\r\n\
         In this Act\u{a0}income means assess-\n\
         able income and the ﬁnancial year.\n\
         It is a com\u{ad}plex\u{200b} rule\u{1}.\tA tab here.\n\
         Text with mojibake: donâ€™t and cafÃ© \0 here.\n\
         \n\
         {dup}\n\
         \n\
         {dup}\n\
         \n\
         12\n\
         \n\
         Page 3 of 10\n\
         \n\
         Section 2 Application\n\
         This Act applies to everyone in the State who earns income.\n"
    );
    let r = lint(d.path(), "messy.txt", &src);
    assert_rules(
        &r,
        &[
            "encoding.bom",
            "encoding.mojibake",
            "chars.nbsp",
            "chars.soft-hyphen",
            "chars.zero-width",
            "chars.ligature",
            "chars.control",
            "chars.nul",
            "whitespace.mixed-line-endings",
            "whitespace.tabs",
            "text.hyphenated-line-break",
            "text.duplicate-paragraph",
            "furniture.page-number",
            "furniture.page-x-of-y",
        ],
    );
    // A BOM in plain text breaks the first heading.
    assert_eq!(get(&r, "encoding.bom").severity, Severity::Warning);
    assert_eq!(get(&r, "chars.nul").severity, Severity::Error);
    assert_eq!(get(&r, "text.duplicate-paragraph").line, Some(9));
    assert_eq!(get(&r, "text.duplicate-paragraph").severity, Severity::Info);
    assert_eq!(get(&r, "furniture.page-number").line, Some(11));
    assert!(
        get(&r, "chars.nbsp")
            .excerpt
            .as_deref()
            .unwrap()
            .contains("⟨NBSP⟩")
    );
    // Findings are sorted: errors first.
    assert_eq!(r.findings[0].severity, Severity::Error);

    // After fix_text the safe problems are gone.
    let (fixed, applied) = fix_text(&src);
    assert!(applied.len() >= 8, "{applied:?}");
    let r2 = lint(d.path(), "fixed.txt", &fixed);
    for gone in [
        "encoding.bom",
        "chars.nbsp",
        "chars.soft-hyphen",
        "chars.zero-width",
        "chars.ligature",
        "chars.control",
        "chars.nul",
        "whitespace.mixed-line-endings",
        "text.hyphenated-line-break",
        "furniture.page-number",
        "furniture.page-x-of-y",
    ] {
        assert!(
            !has(&r2, gone),
            "{gone} survived fix_text: {:#?}",
            r2.findings
        );
    }
    assert!(fixed.contains("assessable income"));
    assert!(fixed.contains("financial year"));
    // Wording that is not safe to change is untouched.
    assert!(fixed.contains("donâ€™t"));
}

#[test]
fn encodings() {
    let d = tempfile::tempdir().unwrap();
    let mut latin = b"Section 1 Definitions\nThe taxpayer\x92s income is assessable under this Act and is taxed.\n".to_vec();
    latin.extend(b"More text to make the file long enough to avoid the tiny file rule. ".repeat(4));
    let p = write(d.path(), "latin.txt", &latin);
    let r = lint_file(&p, &LintOptions::default()).unwrap();
    let f = get(&r, "encoding.invalid-utf8");
    assert_eq!(f.line, Some(2));
    assert!(f.message.contains("Windows-1252"));
    assert!(has(&r, "chars.control"), "0x92 decodes to a C1 control");

    let utf16: Vec<u8> = "\u{feff}Section 1 Short title\nThis Act is the Example Act.\n"
        .encode_utf16()
        .flat_map(|u| u.to_le_bytes())
        .collect();
    let p = write(d.path(), "utf16.txt", &utf16);
    let r = lint_file(&p, &LintOptions::default()).unwrap();
    assert_rules(&r, &["encoding.utf16"]);

    let cr = "Section 1 Short title\rThis Act is the Example Act.\r";
    let r = lint(d.path(), "cr.txt", cr);
    assert_rules(&r, &["whitespace.cr-line-endings"]);
}

#[test]
fn empty_and_tiny() {
    let d = tempfile::tempdir().unwrap();
    let r = lint(d.path(), "empty.md", "  \n\n");
    assert_eq!(rules(&r), vec!["file.empty"]);
    let r = lint(d.path(), "tiny.txt", "Section 1 Short title\nThis Act.\n");
    assert_rules(&r, &["file.tiny"]);
    let r = lint(d.path(), "x.rtf", "{\\rtf1 hello}");
    assert_rules(&r, &["file.unsupported-format"]);
    assert_eq!(r.format, "unknown");
}

#[test]
fn provision_numbering() {
    let d = tempfile::tempdir().unwrap();
    let src = "Part 1 Preliminary\n\n\
        Section 5 Definitions\nIn this Act, income means income (as defined.\n\n\
        Section 7 Application\nThis Act applies to all persons resident here.\n\n\
        Section 6 Exemptions\nThe Minister may exempt a class of persons.\n\n\
        Section 7 Penalties\nA person who fails to comply commits an offence.\n\n\
        Section 9 Regulations\n\n\
        Section 10 Review\nThe Minister must review the operation of this Act.\n\n\
        Part 1 Again\nThis Part restates the first Part for no good reason.\n";
    let r = lint_opts(d.path(), "act.txt", src, &kind(CorpusKind::Legislation));
    assert_rules(
        &r,
        &[
            "structure.numbering-gap",
            "structure.numbering-order",
            "structure.duplicate-number",
            "structure.empty-provision",
            "structure.unbalanced-brackets",
        ],
    );
    let order = get(&r, "structure.numbering-order");
    assert_eq!(order.line, Some(9));
    assert!(order.message.contains("Section 6"), "{}", order.message);
    let dups: Vec<_> = r
        .findings
        .iter()
        .filter(|f| f.rule == "structure.duplicate-number")
        .collect();
    assert_eq!(dups.len(), 2, "Section 7 twice and Part 1 twice: {dups:#?}");
    assert!(
        get(&r, "structure.empty-provision")
            .message
            .contains("Section 9")
    );
    assert_eq!(r.stats["provisions_found"], 6);

    // AU-style hyphenated numbers that skip are fine; going backwards is not.
    let au = ProfileRegistry::builtin().unwrap().get("au").unwrap();
    let src = "8-1 General deductions\nYou can deduct from your assessable income any loss.\n\n\
               8-5 Specific deductions\nYou can also deduct from your assessable income an amount.\n\n\
               8-10 No double deductions\nIf 2 or more provisions allow a deduction you can deduct only once.\n";
    let opts = LintOptions {
        profile: Some(au),
        kind: Some(CorpusKind::Legislation),
    };
    let r = lint_opts(d.path(), "itaa.txt", src, &opts);
    assert!(!has(&r, "structure.numbering-order"), "{:#?}", r.findings);
    assert!(!has(&r, "structure.numbering-gap"));
    assert_eq!(r.stats["profile"], "au");
    assert_eq!(r.stats["provisions_found"], 3);
}

#[test]
fn no_headings_in_legislation_is_an_error() {
    let d = tempfile::tempdir().unwrap();
    let src = "the act\n\nit says that people must pay tax on their income every year.\n\nand other things too, which matter a great deal to everyone.\n";
    let r = lint_opts(d.path(), "flat.txt", src, &kind(CorpusKind::Legislation));
    assert_eq!(get(&r, "structure.no-headings").severity, Severity::Error);
    // Unknown kind: only info on a short file.
    let r = lint(d.path(), "flat2.txt", src);
    assert!(
        r.findings
            .iter()
            .all(|f| f.rule != "structure.no-headings" || f.severity == Severity::Info)
    );
}

#[test]
fn large_provision() {
    let d = tempfile::tempdir().unwrap();
    let body = "The operator must keep records of every transaction for seven years. ".repeat(800);
    let src = format!("Section 1 Records\n{body}\n\nSection 2 Review\nThe Minister may review.\n");
    let r = lint_opts(d.path(), "big.txt", &src, &kind(CorpusKind::Legislation));
    assert_rules(&r, &["structure.large-provision", "lines.very-long"]);
}

#[test]
fn case_paragraphs() {
    let d = tempfile::tempdir().unwrap();
    let opts = kind(CorpusKind::Case);
    let src = "Smith v Jones [2024] HCA 1\n\n\
        1 The appellant appeals against the decision of the Full Court.\n\n\
        2 The facts are not in dispute and may be shortly stated.\n\n\
        3 The appellant was employed by the respondent for ten years.\n\n\
        9 The primary judge found for the respondent on every issue.\n\n\
        4 The Full Court dismissed the appeal from that decision.\n\n\
        5 The appellant now appeals by special leave to this Court.\n\n\
        1 I agree with the reasons of the Chief Justice in this matter.\n\n\
        2 The appeal should be dismissed with costs in the usual way.\n\n\
        3 I add only a few words of my own on the construction point.\n\n\
        Cited: [2019]HCA 7 and (1995)184 CLR 1.\n";
    let r = lint_opts(d.path(), "case.txt", src, &opts);
    assert_rules(
        &r,
        &[
            "case.paragraph-gap",
            "case.paragraph-restart",
            "citation.malformed",
        ],
    );
    assert_eq!(get(&r, "case.paragraph-gap").severity, Severity::Warning);
    assert_eq!(get(&r, "case.paragraph-gap").line, Some(9));
    let cites: Vec<_> = r
        .findings
        .iter()
        .filter(|f| f.rule == "citation.malformed")
        .map(|f| f.excerpt.clone().unwrap())
        .collect();
    assert!(
        cites.iter().any(|c| c.starts_with("[2019]HCA")),
        "{cites:?}"
    );
    assert!(
        cites.iter().any(|c| c.starts_with("(1995)184")),
        "{cites:?}"
    );
    // The well-formed citation in the title is not flagged.
    assert!(!cites.iter().any(|c| c.contains("[2024] HCA 1")));
    assert_eq!(r.stats["case_paragraphs"], 5);

    let r = lint_opts(
        d.path(),
        "case2.txt",
        "Smith v Jones\n\nThe appellant appeals. The facts are not in dispute and may be stated shortly here.\n",
        &opts,
    );
    assert_rules(&r, &["case.no-numbered-paragraphs"]);
}

#[test]
fn markdown_rules() {
    let d = tempfile::tempdir().unwrap();
    let src = "+++\ntitle = \"Guide\"\nlicense = \"CC BY\"\nkind = \"handbook\"\nwibble = 1\n+++\n\n\
        # Guide to things\n\n\
        ### Too deep\n\n\
        Some text with <b>bold</b> and a [broken]() link and [x](https://example.com/a\n\n\
        #NoSpace heading\n\n\
        Underlined heading\n\
        ==================\n\n\
        | a | b |\n\
        | c | d | e |\n\n\
        Part 3 Liability\n\n\
        ```\n\
        code that never closes\n";
    let r = lint(d.path(), "guide.md", src);
    assert_rules(
        &r,
        &[
            "meta.unknown-key",
            "md.heading-level-jump",
            "md.raw-html",
            "md.broken-link",
            "md.heading-no-space",
            "md.setext-heading",
            "md.table-no-separator",
            "md.table-columns",
            "md.unclosed-fence",
            "meta.missing-recommended",
            "structure.unmarked-heading",
        ],
    );
    let lic = r
        .findings
        .iter()
        .find(|f| f.rule == "meta.unknown-key" && f.message.contains("license"))
        .unwrap();
    assert_eq!(lic.fix.as_deref(), Some("Did you mean `licence`?"));
    assert_eq!(lic.line, Some(3));
    assert_eq!(get(&r, "md.heading-level-jump").line, Some(10));
    assert_eq!(get(&r, "md.unclosed-fence").severity, Severity::Error);
    // "handbook" is a valid kind alias.
    assert!(!has(&r, "meta.invalid-value"));

    // Broken front matter.
    let r = lint(
        d.path(),
        "bad.md",
        "+++\ntitle = Unquoted\n+++\n# Part 1 X\n\nBody text.\n",
    );
    let f = get(&r, "meta.front-matter-parse");
    assert_eq!(f.severity, Severity::Error);
    assert_eq!(f.line, Some(2));
    let r = lint(
        d.path(),
        "kind.md",
        "+++\nkind = \"statute-ish\"\nprofile = \"zz\"\n+++\n# Part 1 X\n\nBody.\n",
    );
    assert_rules(&r, &["meta.invalid-value", "meta.unknown-profile"]);
    let r = lint(
        d.path(),
        "open.md",
        "+++\ntitle = \"x\"\n\n# Part 1 X\n\nBody.\n",
    );
    assert_rules(&r, &["meta.front-matter-unterminated"]);
    let r = lint(
        d.path(),
        "yaml.md",
        "---\ntitle: x\n---\n# Part 1 X\n\nBody.\n",
    );
    assert_rules(&r, &["meta.yaml-front-matter", "meta.no-front-matter"]);
    let r = lint(
        d.path(),
        "late.md",
        "\n+++\ntitle = \"x\"\n+++\n# Part 1 X\n\nBody.\n",
    );
    assert_rules(&r, &["meta.front-matter-not-at-start"]);
}

#[test]
fn front_matter_in_text_is_ingested_as_body() {
    let d = tempfile::tempdir().unwrap();
    let r = lint(
        d.path(),
        "t.txt",
        "+++\ntitle = \"x\"\n+++\nSection 1 Short title\nThis Act.\n",
    );
    assert_rules(&r, &["meta.front-matter-in-body"]);
}

#[test]
fn running_headers_and_tables() {
    let d = tempfile::tempdir().unwrap();
    let mut src = String::new();
    for page in 1..=5 {
        src.push_str("Income Tax Assessment Act 1997\n\n");
        for i in 0..30 {
            src.push_str(&format!(
                "Line {i} of page {page} discussing the deduction rules in prose.\n"
            ));
        }
        src.push('\n');
    }
    src.push_str("\nItem    2019      2020      2021\nWages   1,000     1,100     1,200\nRent    500       550       600\nTotal   1,500     1,650     1,800\n\n");
    src.push_str("Year\n2019\nAmount\n$1,000\n2020\n$1,100\n2021\n$1,200\n2022\n$1,300\n");
    let r = lint(d.path(), "furniture.txt", &src);
    let f = get(&r, "furniture.repeated-line");
    assert_eq!(f.line, Some(1));
    assert!(f.message.contains("5 times"), "{}", f.message);
    assert_rules(&r, &["table.column-layout", "table.flattened"]);
    assert_eq!(r.stats["tables_suspected"], 2);

    // Form feeds: detection by page position.
    let mut paged = String::new();
    for page in 1..=4 {
        paged.push_str(&format!(
            "Example Act 2024 — Compilation No. {page}\n\nSection {page} Something\nBody text for section {page} goes here at some length.\n\n{page}\n\u{c}"
        ));
    }
    let r = lint(d.path(), "paged.txt", &paged);
    let f = get(&r, "furniture.repeated-line");
    assert!(f.message.contains("4 of 4 pages"), "{}", f.message);
    assert_eq!(f.page, Some(1));
}

fn docx(dir: &Path, name: &str, parts: &[(&str, &str)]) -> PathBuf {
    let p = dir.join(name);
    let f = std::fs::File::create(&p).unwrap();
    let mut z = zip::ZipWriter::new(f);
    let opts =
        zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Stored);
    for (n, body) in parts {
        z.start_file(*n, opts).unwrap();
        z.write_all(body.as_bytes()).unwrap();
    }
    z.finish().unwrap();
    p
}

const W: &str = r#"xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main""#;

fn para(style: Option<&str>, num: bool, text: &str) -> String {
    let mut ppr = String::new();
    if let Some(s) = style {
        ppr.push_str(&format!(r#"<w:pStyle w:val="{s}"/>"#));
    }
    if num {
        ppr.push_str(r#"<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>"#);
    }
    format!(r#"<w:p><w:pPr>{ppr}</w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>"#)
}

#[test]
fn docx_rules() {
    let d = tempfile::tempdir().unwrap();
    let body = [
        para(Some("Heading1"), true, "Part 1 Preliminary"),
        para(Some("Heading2"), false, "Section 1 Short title"),
        para(None, true, "This Act may be cited as the Example Act 2024 and applies to all."),
        r#"<w:p><w:r><w:t xml:space="preserve">Inserted </w:t></w:r><w:ins w:id="1" w:author="A"><w:r><w:t>new words</w:t></w:r></w:ins><w:del w:id="2" w:author="A"><w:r><w:delText>old words</w:delText></w:r></w:del></w:p>"#.to_string(),
        r#"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"#.to_string(),
        r#"<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>secret</w:t></w:r></w:p>"#.to_string(),
        r#"<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>TOC \o</w:instrText></w:r></w:p>"#.to_string(),
        para(Some("ActHead5"), false, "Section 2 Commencement"),
    ]
    .concat();
    let doc =
        format!(r#"<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>"#);
    let styles = format!(
        r#"<w:styles {W}><w:style w:type="paragraph" w:styleId="ActHead5"><w:name w:val="ActHead 5"/></w:style></w:styles>"#
    );
    let numbering = format!(
        r#"<w:numbering {W}><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>"#
    );
    let footnotes = format!(
        r#"<w:footnotes {W}><w:footnote w:type="separator" w:id="-1"/><w:footnote w:id="1"><w:p/></w:footnote></w:footnotes>"#
    );
    let comments =
        format!(r#"<w:comments {W}><w:comment w:id="0"><w:p/></w:comment></w:comments>"#);
    let p = docx(
        d.path(),
        "act.docx",
        &[
            ("word/document.xml", &doc),
            ("word/styles.xml", &styles),
            ("word/numbering.xml", &numbering),
            ("word/footnotes.xml", &footnotes),
            ("word/comments.xml", &comments),
        ],
    );
    let r = lint_file(&p, &LintOptions::default()).unwrap();
    assert_eq!(r.format, "docx");
    assert_rules(
        &r,
        &[
            "docx.tracked-changes",
            "docx.comments",
            "docx.auto-numbering",
            "docx.footnotes",
            "docx.tables",
            "docx.hidden-text",
            "docx.fields",
            "docx.custom-heading-styles",
        ],
    );
    assert_eq!(get(&r, "docx.tracked-changes").severity, Severity::Error);
    let num = get(&r, "docx.auto-numbering");
    assert_eq!(
        num.severity,
        Severity::Error,
        "a numbered heading loses its number"
    );
    assert!(num.message.contains("2 paragraphs"), "{}", num.message);
    assert!(get(&r, "docx.footnotes").message.contains("1 footnote"));
    assert!(
        get(&r, "docx.custom-heading-styles")
            .message
            .contains("ActHead5")
    );
    assert!(!has(&r, "docx.no-heading-styles"));
    assert_eq!(r.stats["headings_found"], 2);

    // No heading styles at all: nothing will be a heading.
    let body = [
        para(None, false, "Part 1 Preliminary"),
        para(None, false, "Section 1 Short title"),
        para(
            None,
            false,
            "This Act may be cited as the Example Act 2024 and applies to all.",
        ),
    ]
    .concat();
    let doc = format!(r#"<w:document {W}><w:body>{body}</w:body></w:document>"#);
    let p = docx(d.path(), "plain.docx", &[("word/document.xml", &doc)]);
    let r = lint_file(&p, &kind(CorpusKind::Legislation)).unwrap();
    assert_rules(
        &r,
        &[
            "docx.no-heading-styles",
            "structure.unmarked-heading",
            "structure.no-headings",
        ],
    );
    assert_eq!(get(&r, "structure.no-headings").severity, Severity::Error);

    // Broken containers.
    let p = write(d.path(), "fake.docx", b"not a zip at all");
    assert_rules(
        &lint_file(&p, &LintOptions::default()).unwrap(),
        &["docx.not-zip"],
    );
    let p = docx(d.path(), "nodoc.docx", &[("xl/workbook.xml", "<x/>")]);
    assert_rules(
        &lint_file(&p, &LintOptions::default()).unwrap(),
        &["docx.no-document-xml"],
    );
    let p = docx(
        d.path(),
        "img.docx",
        &[(
            "word/document.xml",
            &format!(
                r#"<w:document {W}><w:body><w:p><w:r><w:drawing/></w:r></w:p></w:body></w:document>"#
            ),
        )],
    );
    assert_rules(
        &lint_file(&p, &LintOptions::default()).unwrap(),
        &["docx.images-only"],
    );
}

/// A minimal PDF with Helvetica text at the given positions, one Vec per page.
fn make_pdf(pages: &[Vec<(u32, u32, String)>]) -> Vec<u8> {
    let mut objs: Vec<String> = Vec::new();
    let n = pages.len();
    let kids: Vec<String> = (0..n).map(|i| format!("{} 0 R", 4 + 2 * i)).collect();
    objs.push("<< /Type /Catalog /Pages 2 0 R >>".into());
    objs.push(format!(
        "<< /Type /Pages /Kids [{}] /Count {n} >>",
        kids.join(" ")
    ));
    objs.push("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".into());
    for (i, page) in pages.iter().enumerate() {
        let content: String = page
            .iter()
            .map(|(x, y, t)| {
                let t = t
                    .replace('\\', "\\\\")
                    .replace('(', "\\(")
                    .replace(')', "\\)");
                format!("BT /F1 10 Tf {x} {y} Td ({t}) Tj ET\n")
            })
            .collect();
        objs.push(format!(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {} 0 R >>",
            5 + 2 * i
        ));
        objs.push(format!(
            "<< /Length {} >>\nstream\n{content}endstream",
            content.len()
        ));
    }
    let mut out = b"%PDF-1.4\n".to_vec();
    let mut offsets = Vec::new();
    for (i, o) in objs.iter().enumerate() {
        offsets.push(out.len());
        out.extend(format!("{} 0 obj\n{o}\nendobj\n", i + 1).as_bytes());
    }
    let xref = out.len();
    out.extend(format!("xref\n0 {}\n0000000000 65535 f \n", objs.len() + 1).as_bytes());
    for o in offsets {
        out.extend(format!("{o:010} 00000 n \n").as_bytes());
    }
    out.extend(
        format!(
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
            objs.len() + 1
        )
        .as_bytes(),
    );
    out
}

fn have_pdftotext() -> bool {
    std::process::Command::new("pdftotext")
        .arg("-v")
        .output()
        .is_ok()
}

fn text_page(lines: &[&str]) -> Vec<(u32, u32, String)> {
    lines
        .iter()
        .enumerate()
        .map(|(i, l)| (72, 720 - 14 * i as u32, l.to_string()))
        .collect()
}

#[test]
fn pdf_rules() {
    if !have_pdftotext() {
        eprintln!("skipping: pdftotext not on PATH");
        return;
    }
    let d = tempfile::tempdir().unwrap();
    let mut pages = Vec::new();
    for p in 1..=4 {
        let s1 = format!("Section {p} Heading number {p}");
        let body =
            format!("The operator must comply with the requirements in section {p} of this Act.");
        let more =
            "A person who contravenes this section commits an offence and is liable to a fine.";
        let assess = "income that is assess-";
        let able = "able under the Act is included.";
        let page_no = format!("{p}");
        pages.push(text_page(&[
            "Example Act 2024 Compilation",
            "",
            &s1,
            &body,
            more,
            assess,
            able,
            "",
            "",
            &page_no,
        ]));
    }
    pages.push(vec![]); // a blank (or scanned) page
    let p = write(d.path(), "act.pdf", &make_pdf(&pages));
    let r = lint_file(&p, &kind(CorpusKind::Legislation)).unwrap();
    assert_eq!(r.format, "pdf");
    assert_eq!(r.stats["pages"], 5);
    assert_rules(
        &r,
        &[
            "pdf.empty-pages",
            "furniture.repeated-line",
            "text.hyphenated-line-break",
            "furniture.page-number",
        ],
    );
    let empty = get(&r, "pdf.empty-pages");
    assert_eq!(empty.page, Some(5));
    assert!(empty.line.is_none(), "PDF findings carry pages, not lines");
    assert_eq!(get(&r, "furniture.page-number").severity, Severity::Info);
    assert_eq!(r.stats["provisions_found"], 4);
    assert!(!has(&r, "structure.no-headings"));

    // Scanned: no text at all.
    let p = write(d.path(), "scan.pdf", &make_pdf(&[vec![], vec![]]));
    let r = lint_file(&p, &LintOptions::default()).unwrap();
    let f = get(&r, "pdf.no-text-layer");
    assert_eq!(f.severity, Severity::Error);
    assert!(f.fix.as_deref().unwrap().contains("ocrmypdf"));

    // Two columns.
    let mut col = Vec::new();
    for i in 0..20u32 {
        col.push((
            50,
            720 - 14 * i,
            format!("Left column sentence number {i} goes"),
        ));
        col.push((
            330,
            720 - 14 * i,
            format!("Right column sentence number {i} here"),
        ));
    }
    let p = write(d.path(), "cols.pdf", &make_pdf(&[col]));
    let r = lint_file(&p, &LintOptions::default()).unwrap();
    assert_rules(&r, &["pdf.two-column"]);

    // Not a PDF at all.
    let p = write(d.path(), "fake.pdf", b"<html>error</html>");
    assert_rules(
        &lint_file(&p, &LintOptions::default()).unwrap(),
        &["pdf.not-pdf"],
    );
}

#[test]
fn html_rules() {
    let d = tempfile::tempdir().unwrap();
    let words =
        "The Commissioner may issue a determination about this matter at any time. ".repeat(10);
    let src = format!(
        "<html><body><h1>Example Act</h1><div>{words}</div><div><span>{words}</span></div><p>Short para.</p><p><b>Section 5 Definitions</b></p><table><tr><td>a</td><td>b</td></tr></table></body></html>"
    );
    let r = lint(d.path(), "page.html", &src);
    assert_rules(
        &r,
        &[
            "html.text-outside-blocks",
            "html.tables",
            "structure.unmarked-heading",
        ],
    );
    let r = lint(
        d.path(),
        "js.html",
        &format!("<html><body><div id=app>{words}</div><script>render()</script></body></html>"),
    );
    assert_rules(&r, &["html.no-blocks"]);
}

#[test]
fn xml_rules() {
    let d = tempfile::tempdir().unwrap();
    let r = lint(
        d.path(),
        "bad.xml",
        "<akomaNtoso>\n<act>\n<section><num>1</num></act>\n</akomaNtoso>\n",
    );
    let f = get(&r, "xml.parse");
    assert_eq!(f.severity, Severity::Error);
    assert_eq!(f.line, Some(3));
    let r = lint(
        d.path(),
        "other.xml",
        "<root><para>Just some text in a paragraph that is long enough to be read.</para></root>",
    );
    assert_rules(&r, &["xml.unknown-schema"]);
    let akn = "<akomaNtoso><act><body><section><num>1.</num><heading>Short title</heading><content><p>See &sect; 2 of this Act.</p></content></section><section><heading>Untitled</heading><content><p>More.</p></content></section></body></act></akomaNtoso>";
    let r = lint(d.path(), "akn.xml", akn);
    assert_rules(&r, &["xml.unknown-entity", "xml.missing-num"]);
    assert_eq!(r.stats["xml_schema"], "akoma-ntoso");
    assert!(!has(&r, "xml.unknown-schema"));
}

#[test]
fn duplicated_blocks() {
    let d = tempfile::tempdir().unwrap();
    let paras = [
        "(a) the conduct constituting the alleged offence occurs wholly in Australia; or",
        "(b) the conduct occurs wholly outside Australia and a result of it occurs in Australia; or",
        "(c) the conduct occurs wholly outside Australia and the person is an Australian citizen.",
    ];
    // Two short repeated subparagraphs: normal drafting, not reported.
    let src = format!(
        "Section 1 One\n\n{a}\n\n{b}\n\nSection 2 Two\n\n{a}\n\n{b}\n",
        a = paras[0],
        b = paras[1]
    );
    let r = lint(d.path(), "ok.txt", &src);
    assert!(!has(&r, "text.duplicate-paragraph"), "{:#?}", r.findings);
    // A pasted block of three: reported once.
    let block = paras.join("\n\n");
    let src = format!("Section 1 One\n\n{block}\n\nSection 2 Two\n\n{block}\n");
    let r = lint(d.path(), "dup.txt", &src);
    let f = get(&r, "text.duplicate-paragraph");
    assert!(
        f.message.contains("3 consecutive paragraphs"),
        "{}",
        f.message
    );
    assert!(f.message.contains("line 3"), "{}", f.message);
    assert_eq!(f.line, Some(11));
}

#[test]
fn table_of_contents() {
    let d = tempfile::tempdir().unwrap();
    let src = "Contents\n\nPart 1 Preliminary ........ 1\n1 Short title ........ 1\n2 Commencement ........ 1\n\nPart 2 Liability ........ 2\n3 Liability to pay ........ 2\n4 Exemptions ........ 3\n\nPart 1 Preliminary\n\n1 Short title\nThis Act may be cited as the Example Act.\n";
    let r = lint_opts(d.path(), "toc.txt", src, &kind(CorpusKind::Legislation));
    let f = get(&r, "text.table-of-contents");
    assert_eq!(f.line, Some(3));
    assert!(f.message.contains("6 entries"), "{}", f.message);
}

#[test]
fn case_summary_needs_no_paragraphs() {
    let d = tempfile::tempdir().unwrap();
    let src = "+++\ntitle = \"A v B\"\nkind = \"case\"\ncontent = \"summary\"\n+++\nA v B [2024] HCA 3\n\nThe court held that the appeal should be allowed for the reasons given.\n";
    let r = lint(d.path(), "sum.md", src);
    assert!(!has(&r, "case.no-numbered-paragraphs"), "{:#?}", r.findings);
}

#[test]
fn repeated_prose_fragments_are_not_furniture() {
    let d = tempfile::tempdir().unwrap();
    let mut src = String::new();
    for i in 0..6 {
        src.push_str("See the decision in\nCesena Sulphur\n) at 446 which held that the company was resident.\n");
        for j in 0..(20 + i * 13) {
            src.push_str(&format!(
                "Filler line {j} of discussion about central management and control.\n"
            ));
        }
    }
    let r = lint(d.path(), "frag.txt", &src);
    assert!(!has(&r, "furniture.repeated-line"), "{:#?}", r.findings);
}

#[test]
fn json_rules() {
    let d = tempfile::tempdir().unwrap();
    let r = lint(d.path(), "doc.json", "{\"not\": \"a document\"}");
    assert_rules(&r, &["json.invalid"]);
}

#[test]
fn directories_and_sidecars() {
    let d = tempfile::tempdir().unwrap();
    let dir = d.path();
    write(dir, "a.md", CLEAN_MD.as_bytes());
    write(dir, "b.txt", b"Section 1 Short title\nThis Act may be cited as the Example Act 2024 and so on and so forth for a while.\n");
    write(
        dir,
        "b.meta.toml",
        b"title = \"B\"\nkind = \"legislation\"\nlicense = \"x\"\n",
    );
    write(dir, "c.meta.json", b"{}");
    write(dir, "notes.rtf", b"ignored");
    std::fs::create_dir(dir.join(".hidden")).unwrap();
    write(&dir.join(".hidden"), "x.md", b"# hidden");
    let reports = lint_path(dir, &LintOptions::default()).unwrap();
    let names: Vec<_> = reports
        .iter()
        .map(|r| {
            Path::new(&r.path)
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
                .to_string()
        })
        .collect();
    assert_eq!(names, vec!["a.md", "b.txt"]);
    let b = &reports[1];
    assert!(!has(b, "meta.no-front-matter"));
    assert_eq!(b.stats["kind"], "legislation", "sidecar kind is used");
    let k = get(b, "meta.unknown-key");
    assert!(k.message.contains("sidecar b.meta.toml"));
    assert!(
        k.line.is_none(),
        "line numbers would point into another file"
    );

    // Single file through lint_path.
    let one = lint_path(&dir.join("a.md"), &LintOptions::default()).unwrap();
    assert_eq!(one.len(), 1);

    // Broken sidecar is an error.
    write(dir, "b.meta.toml", b"title = \n");
    let r = lint_file(&dir.join("b.txt"), &LintOptions::default()).unwrap();
    assert_eq!(get(&r, "meta.front-matter-parse").severity, Severity::Error);
}

#[test]
fn finding_cap_per_rule() {
    let d = tempfile::tempdir().unwrap();
    let mut src = String::from("+++\ntitle = \"x\"\n+++\n");
    for i in 0..40 {
        src.push_str(&format!("\n#Bad{i}\n"));
    }
    let r = lint(d.path(), "many.md", &src);
    let n = r
        .findings
        .iter()
        .filter(|f| f.rule == "md.heading-no-space")
        .count();
    assert_eq!(n, 26, "25 detailed plus one summary");
    assert!(r.findings.iter().any(|f| f.message.contains("15 more")));
}

#[test]
fn rules_table_covers_emitted_rules() {
    // Every rule id used in the sources is documented in RULES.
    let ids: std::collections::HashSet<&str> =
        meowlaw_lint::RULES.iter().map(|(id, _)| *id).collect();
    let src_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let re = regex::Regex::new(r#""([a-z]+\.[a-z0-9-]+)""#).unwrap();
    for e in std::fs::read_dir(src_dir).unwrap() {
        let p = e.unwrap().path();
        let s = std::fs::read_to_string(&p).unwrap();
        for c in re.captures_iter(&s) {
            let id = &c[1];
            let prefix = id.split('.').next().unwrap();
            if [
                "file",
                "encoding",
                "chars",
                "whitespace",
                "lines",
                "quotes",
                "text",
                "furniture",
                "table",
                "md",
                "meta",
                "structure",
                "citation",
                "case",
                "pdf",
                "docx",
                "html",
                "xml",
                "json",
            ]
            .contains(&prefix)
                && !id.ends_with(".xml")
                && !id.ends_with(".toml")
                && !id.ends_with(".json")
                && !id.ends_with(".md")
            {
                assert!(
                    ids.contains(id),
                    "{id} used in {} but not in RULES",
                    p.display()
                );
            }
        }
    }
}
