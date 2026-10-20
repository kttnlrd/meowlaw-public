//! Claims from the OpenCodeReview scan about jurisdiction profiles, settled
//! against the real patterns. Each test states the behaviour the profile is
//! supposed to have; the one marked `#[ignore]` is a confirmed gap.

use meowlaw_core::profile::{Profile, ProfileRegistry};
use meowlaw_extract::{Cited, parse_citations};

fn profile(id: &str) -> Profile {
    ProfileRegistry::builtin().unwrap().get(id).unwrap()
}

fn provision_corpus(cits: &[meowlaw_extract::ParsedCitation]) -> Option<String> {
    cits.iter().find_map(|c| match &c.cited {
        Cited::Provision { corpus_name, .. } => corpus_name.clone(),
        _ => None,
    })
}

fn provision_numbers(cits: &[meowlaw_extract::ParsedCitation]) -> Vec<String> {
    cits.iter()
        .find_map(|c| match &c.cited {
            Cited::Provision { numbers, .. } => Some(numbers.clone()),
            _ => None,
        })
        .unwrap_or_default()
}

fn case_citations(cits: &[meowlaw_extract::ParsedCitation]) -> Vec<String> {
    cits.iter()
        .filter_map(|c| match &c.cited {
            Cited::Case { citation, .. } => Some(citation.clone()),
            _ => None,
        })
        .collect()
}

#[test]
fn us_code_names_the_title_and_the_section() {
    let cits = parse_citations("26 U.S.C. § 1", &profile("us"), None);
    assert_eq!(provision_corpus(&cits).as_deref(), Some("26 U.S.C."));
    assert_eq!(provision_numbers(&cits), vec!["1".to_string()]);
}

#[test]
fn nz_reports_accept_a_multi_digit_volume() {
    let cits = parse_citations("[2009] 10 NZLR 1", &profile("nz"), None);
    assert_eq!(case_citations(&cits), vec!["[2009] 10 NZLR 1".to_string()]);
}

#[test]
fn nz_corpus_does_not_swallow_the_leading_sentence_word() {
    let cits = parse_citations("See Income Tax Act 2004, s 1", &profile("nz"), None);
    assert_eq!(
        provision_corpus(&cits).as_deref(),
        Some("Income Tax Act 2004")
    );
}

#[test]
fn sg_rejects_a_volume_with_a_stray_decimal_point() {
    let cits = parse_citations("[2019] 5.1 SLR 130", &profile("sg"), None);
    assert!(
        case_citations(&cits).is_empty(),
        "malformed volume matched: {:?}",
        case_citations(&cits)
    );
}

#[test]
fn au_ruling_citation_is_the_bare_identifier() {
    let cits = parse_citations("IT 2019/1", &profile("au"), None);
    assert_eq!(case_citations(&cits), vec!["IT 2019/1".to_string()]);
}

/// Confirmed gap: German pinpoints ("Abs. 1", "Satz 2") are matched by the
/// pattern but not captured, so a subsection cannot resolve. Left ignored
/// until pinpoint support is designed for the starter profiles.
#[test]
#[ignore = "known gap: Abs./Satz pinpoints are dropped"]
fn de_abs_pinpoint_is_recorded() {
    let cits = parse_citations("§ 823 Abs. 1 BGB", &profile("de"), None);
    let c = cits
        .iter()
        .find(|c| matches!(c.cited, Cited::Provision { .. }))
        .expect("a provision");
    let drawn = format!("{:?}", c.cited);
    assert!(drawn.contains("Abs"), "pinpoint not captured: {drawn}");
}
