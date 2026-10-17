//! Extraction tests over small fictional sources.

use super::*;
use meowlaw_core::{CorpusKind, Meta};
use meowlaw_ingest::{Ingester, Manifest};

const ACT: &str = r#"+++
id = "widget-act"
title = "Widget Act 2024"
kind = "legislation"
aliases = ["WA 2024"]
+++
# Part 1 Preliminary

## Section 1 Short title

This Act is the Widget Act 2024.

## Section 2 Definitions

In this Act:

"widget" means a small mechanical device.

**dealer** has the meaning given by section 5.

**retail price** means the price on the label.

# Part 2 Dealing

## Section 3 Offence

(1) A dealer must not sell a widget except as permitted by section 4 or sections 5 to 6.

(2) Subsection (1) does not apply to a gift.

## Section 4 Permitted sales

A dealer may sell a widget at the retail price.

## Section 5 Meaning of dealer

In this section, "trade" means business.

A dealer is a person who sells widgets in trade.

## Section 6 Penalty

A person who contravenes section 3 commits an offence.
"#;

const CASE: &str = r#"+++
kind = "case"
citations = ["[2024] EXSC 1"]
title = "Smith v Jones"
+++
Smith v Jones [2024] EXSC 1

1 This appeal concerns the Widget Act 2024.

2 Section 3 creates the offence. Section 4 of the Widget Act 2024 permits sales, and s 5 WA 2024 defines a dealer.

3 We follow [2023] EXSC 9.
"#;

const HANDBOOK: &str = r#"# Staff handbook

This handbook applies to Example Pty Ltd (the "Company").

## Leave

The Company grants leave under section 4 of the Widget Act 2024.
"#;

fn ingest() -> (Vec<Document>, Catalog, HashMap<String, Profile>) {
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = N.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("meowlaw-extract-test-{}-{n}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("act.md"), ACT).unwrap();
    std::fs::write(dir.join("case.md"), CASE).unwrap();
    std::fs::write(dir.join("handbook.md"), HANDBOOK).unwrap();
    let reg = ProfileRegistry::builtin().unwrap();
    let ing = Ingester::new(reg.clone());
    let mut docs = Vec::new();
    docs.extend(
        ing.ingest_file(&dir.join("act.md"), &Manifest::default())
            .unwrap(),
    );
    docs.extend(
        ing.ingest_file(&dir.join("case.md"), &Manifest::default())
            .unwrap(),
    );
    docs.extend(
        ing.ingest_file(&dir.join("handbook.md"), &Manifest::default())
            .unwrap(),
    );
    let profiles = load_profiles(&reg, docs.iter().map(|d| &d.corpus)).unwrap();
    let cat = build_catalog(
        docs.iter().map(|d| (&d.corpus, d.nodes.as_slice())),
        &profiles,
    );
    std::fs::remove_dir_all(&dir).ok();
    (docs, cat, profiles)
}

fn targets(ex: &Extraction, source: &str) -> Vec<Option<String>> {
    ex.references
        .iter()
        .filter(|r| r.source == source)
        .map(|r| r.target.clone())
        .collect()
}

#[test]
fn same_act_references() {
    let (docs, cat, profiles) = ingest();
    let ex = extract(&docs, &cat, &profiles);
    let t = targets(&ex, "widget-act:s3");
    assert!(t.contains(&Some("widget-act:s4".into())), "{t:?}");
    assert!(t.contains(&Some("widget-act:s5".into())));
    assert!(
        t.contains(&Some("widget-act:s6".into())),
        "range 5 to 6 expands"
    );
    let t = targets(&ex, "widget-act:s6");
    assert_eq!(t, vec![Some("widget-act:s3".into())]);
}

#[test]
fn cross_corpus_references() {
    let (docs, cat, profiles) = ingest();
    let ex = extract(&docs, &cat, &profiles);
    let p2 = targets(&ex, "2024-exsc-1:para2");
    // "Section 3" (bare) resolves via the Act named earlier in para 1? No: context is per node,
    // and para 2 names the Act after "Section 3", so the bare cite is unresolved.
    assert!(p2.contains(&Some("widget-act:s4".into())), "{p2:?}");
    assert!(
        p2.contains(&Some("widget-act:s5".into())),
        "suffix alias WA 2024"
    );
    let p3 = &ex
        .references
        .iter()
        .find(|r| r.source == "2024-exsc-1:para3")
        .unwrap();
    assert_eq!(p3.raw, "[2023] EXSC 9");
    assert_eq!(p3.target, None, "unknown case stays unresolved");
    // Handbook text now sits on paragraph nodes under the "Leave" chunk.
    let hb: Vec<Option<String>> = ex
        .references
        .iter()
        .filter(|r| r.source.starts_with("handbook:p"))
        .map(|r| r.target.clone())
        .collect();
    assert_eq!(hb, vec![Some("widget-act:s4".into())]);
}

#[test]
fn case_citation_resolves() {
    let (docs, cat, profiles) = ingest();
    assert_eq!(cat.case("[2024]  exsc 1"), Some("2024-exsc-1"));
    let _ = (docs, profiles);
}

#[test]
fn definitions_and_scope() {
    let (docs, cat, profiles) = ingest();
    let ex = extract(&docs, &cat, &profiles);
    let get = |term: &str| ex.definitions.iter().find(|d| d.term == term).unwrap();
    let w = get("widget");
    assert_eq!(w.source, "widget-act:s2");
    assert_eq!(w.scope, "widget-act");
    let d = get("dealer");
    assert_eq!(d.points_to.as_deref(), Some("widget-act:s5"));
    let t = get("trade");
    assert_eq!(t.scope, "widget-act:s5", "In this section");
    let c = get("Company");
    assert_eq!(c.pattern, "parenthetical");
    assert_eq!(c.scope, "handbook");
    assert!(ex.definitions.iter().all(|d| d.term != "This Act"));
}

#[test]
fn term_usage() {
    let (docs, cat, profiles) = ingest();
    let ex = extract(&docs, &cat, &profiles);
    let uses: Vec<(&str, &str)> = ex
        .edges
        .iter()
        .filter(|e| e.kind == EdgeKind::UsesTerm)
        .map(|e| (e.source.as_str(), e.target.as_str()))
        .collect();
    assert!(uses.contains(&("widget-act:s4", "def:widget-act:retail-price")));
    assert!(uses.contains(&("widget-act:s3", "def:widget-act:widget")));
    // "trade" is scoped to s5, so s3 must not link to it.
    assert!(
        !uses
            .iter()
            .any(|(s, t)| *s == "widget-act:s3" && t.ends_with(":trade"))
    );
}

#[test]
fn number_lists() {
    assert_eq!(
        split_numbers("5, 6 and 8 to 10"),
        vec!["5", "6", "8", "9", "10"]
    );
    assert_eq!(split_numbers("8-1"), vec!["8-1"]);
    assert_eq!(split_numbers("8-1 and 8-5"), vec!["8-1", "8-5"]);
    let _ = Meta::new();
    let _ = CorpusKind::Case;
}

#[test]
fn eu_quoted_definition() {
    let reg = ProfileRegistry::builtin().unwrap();
    let p = reg.get("eu").unwrap();
    let node = Node {
        id: "mc:art2".into(),
        corpus: "mc".into(),
        kind: NodeKind::Provision,
        label: Some("Article".into()),
        number: Some("2".into()),
        title: Some("Definitions".into()),
        text: "1. ‘seller’ means any person;\n\n2. ‘consumer’ means a natural person.".into(),
        parent: Some("mc".into()),
        ordinal: 1,
        meta: Meta::new(),
    };
    let mut cat = Catalog::default();
    cat.add_node(&node);
    let defs = extract_definitions(&node, &p, &cat, &mut HashSet::new());
    let terms: Vec<&str> = defs.iter().map(|d| d.term.as_str()).collect();
    assert_eq!(terms, vec!["seller", "consumer"]);
}

#[test]
fn au_act_then_section_and_dictionary() {
    let reg = ProfileRegistry::builtin().unwrap();
    let p = reg.get("au").unwrap();
    let pcs = parse_citations(
        "Income Tax Assessment Act 1936 (Cth) s 109D; see subsection 43-145(2).",
        &p,
        None,
    );
    let got: Vec<(String, Cited)> = pcs.into_iter().map(|c| (c.pattern, c.cited)).collect();
    assert_eq!(got[0].0, "au_act_then_section");
    assert_eq!(
        got[0].1,
        Cited::Provision {
            label: Some("Section".into()),
            numbers: vec!["109D".into()],
            corpus_name: Some("Income Tax Assessment Act 1936".into()),
            relative: false,
            item: None
        }
    );
    assert_eq!(got[1].0, "au_pinpoint");

    let node = Node {
        id: "a:s995-1".into(),
        corpus: "a".into(),
        kind: NodeKind::Provision,
        label: Some("Section".into()),
        number: Some("995-1".into()),
        title: Some("Definitions".into()),
        text: "**(1)** In this Act, except so far as the contrary intention appears: 4% manner has the meaning given by section 43-145. abnormal trading has the meaning given by Subdivision 960-H. ABN has the meaning given by the A New Tax System (Australian Business Number) Act 1999.".into(),
        parent: Some("a".into()),
        ordinal: 1,
        meta: Meta::new(),
    };
    let mut cat = Catalog::default();
    cat.add_node(&node);
    let defs = extract_definitions(&node, &p, &cat, &mut HashSet::new());
    let terms: Vec<&str> = defs.iter().map(|d| d.term.as_str()).collect();
    assert_eq!(terms, vec!["4% manner", "abnormal trading", "ABN"]);
}

fn node(id: &str, title: &str, text: &str) -> Node {
    Node {
        id: id.into(),
        corpus: id.split(':').next().unwrap().into(),
        kind: NodeKind::Provision,
        label: Some("Section".into()),
        number: Some(id.split(":s").nth(1).unwrap_or("1").into()),
        title: Some(title.into()),
        text: text.into(),
        parent: Some(id.split(':').next().unwrap().into()),
        ordinal: 1,
        meta: Meta::new(),
    }
}

#[test]
fn audit_regressions_citations() {
    let p = ProfileRegistry::builtin().unwrap().get("au").unwrap();
    // Possessive "entity's CGT" is not "s CGT".
    let pcs = parse_citations(
        "the entity's CGT assets and the individual's 2012-13 income",
        &p,
        None,
    );
    assert!(pcs.is_empty(), "{pcs:?}");
    // Schedule-to-another-Act citations name that Act.
    let pcs = parse_citations(
        "Division 280 in Schedule 1 to the Taxation Administration Act 1953",
        &p,
        None,
    );
    assert_eq!(pcs.len(), 1);
    match &pcs[0].cited {
        Cited::Provision {
            corpus_name, label, ..
        } => {
            assert_eq!(
                corpus_name.as_deref(),
                Some("Taxation Administration Act 1953")
            );
            assert_eq!(label.as_deref(), Some("Division"));
        }
        c => panic!("{c:?}"),
    }
}

#[test]
fn audit_regressions_terms() {
    let p = ProfileRegistry::builtin().unwrap().get("generic").unwrap();
    let root = Node {
        id: "a".into(),
        corpus: "a".into(),
        kind: NodeKind::Root,
        label: None,
        number: None,
        title: Some("A".into()),
        text: String::new(),
        parent: None,
        ordinal: 0,
        meta: Meta::new(),
    };
    let s1 = node(
        "a:s1",
        "Definitions",
        "\"net income\" means x.\n\n\"income\" means y.\n\n**(b)** means nothing.",
    );
    let s2 = node("a:s2", "Use", "The cabinet income—and ‘income’ here.");
    let mut cat = Catalog::default();
    for n in [&root, &s1, &s2] {
        cat.add_node(n);
    }
    let defs = extract_definitions(&s1, &p, &cat, &mut HashSet::new());
    let terms: Vec<&str> = defs.iter().map(|d| d.term.as_str()).collect();
    assert_eq!(
        terms,
        vec!["net income", "income"],
        "enumerator (b) rejected"
    );
    let refs: Vec<&Definition> = defs.iter().collect();
    let edges = term_usage_edges(std::slice::from_ref(&s2), &refs, &cat);
    let targets: Vec<&str> = edges.iter().map(|e| e.target.as_str()).collect();
    assert_eq!(
        targets,
        vec!["def:a:income"],
        "shorter term found despite longer false match; em dash and curly quotes are boundaries"
    );
}

#[test]
fn pinpoints_and_stripping() {
    let reg = ProfileRegistry::builtin().unwrap();
    let au = reg.get("au").unwrap();
    let pcs = parse_citations(
        "see [2026] HCA 18 at [30] and TR 2018/5 para 12; also TR 2018/5 at [7]",
        &au,
        None,
    );
    let pins: Vec<(String, Option<String>)> = pcs
        .iter()
        .filter_map(|p| match &p.cited {
            Cited::Case { citation, pin } => Some((citation.clone(), pin.clone())),
            _ => None,
        })
        .collect();
    assert_eq!(
        pins,
        vec![
            ("[2026] HCA 18".to_string(), Some("30".to_string())),
            ("TR 2018/5".to_string(), Some("12".to_string())),
            ("TR 2018/5".to_string(), Some("7".to_string())),
        ]
    );
    let generic = reg.get("generic").unwrap();
    let own = parse_citations("(see paragraph 30 of this Ruling)", &generic, None);
    assert!(
        matches!(&own[0].cited, Cited::Provision { corpus_name: Some(c), .. } if c == "this Ruling")
    );

    let text = "The Commissioner treated the entitlements as loans under s 109D of the Income Tax Assessment Act 1936 (Cth), following Bywater [2016] HCA 45.";
    let stripped = strip_citations(text, &[&au, &generic]);
    assert_eq!(
        stripped,
        "The Commissioner treated the entitlements as loans, following Bywater."
    );
}

#[test]
fn table_item_citations() {
    let p = ProfileRegistry::builtin().unwrap().get("au").unwrap();
    let pcs = parse_citations(
        "Use item 3 of the table in section 40-60 unless item 2 of this table applies.",
        &p,
        None,
    );
    assert_eq!(pcs.len(), 2, "{pcs:#?}");
    assert_eq!(pcs[0].raw, "item 3 of the table in section 40-60");
    match &pcs[0].cited {
        Cited::Provision {
            numbers,
            item,
            relative,
            ..
        } => {
            assert_eq!(numbers, &vec!["40-60".to_string()]);
            assert_eq!(item.as_deref(), Some("3"));
            assert!(!relative);
        }
        c => panic!("{c:?}"),
    }
    assert!(
        matches!(&pcs[1].cited, Cited::Provision { relative: true, item: Some(i), .. } if i == "2")
    );
}
