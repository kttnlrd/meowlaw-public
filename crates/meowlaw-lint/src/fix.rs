//! Safe automatic fixes for plain text and Markdown. Nothing here changes
//! wording: only invisible characters, line endings, typographic ligatures,
//! words split by line-end hyphens, page-number furniture and blank lines.

use crate::text::{PAGE_NUM, PAGE_X_OF_Y, is_hyphen_split};
use std::collections::HashSet;

/// Apply the safe fixes. Returns the new text and a description of each kind
/// of fix applied (empty when nothing changed).
///
/// * strip a leading byte-order mark;
/// * normalise CRLF and CR line endings to LF; form feeds become line breaks;
/// * remove NUL, other control characters, zero-width characters and soft hyphens;
/// * replace non-breaking and other unusual spaces with a plain space;
/// * expand ligatures (ﬁ → fi);
/// * join words split by a hyphen at a line end when both halves are
///   lowercase letters (the hyphen is kept when the hyphenated form occurs
///   elsewhere in the text and the joined form does not);
/// * remove "Page X of Y" lines, and bare page-number lines that stand alone
///   between blank lines;
/// * collapse runs of three or more blank lines to one.
///
/// Fenced code blocks (```) are left alone by the line-level fixes.
pub fn fix_text(text: &str) -> (String, Vec<String>) {
    let mut applied = Vec::new();
    let mut note = |n: usize, what: &str| {
        if n > 0 {
            applied.push(format!("{what}: {n}"));
        }
    };

    let mut s = text;
    let bom = s.starts_with('\u{feff}');
    if bom {
        s = &s[3..];
    }
    note(usize::from(bom), "stripped byte-order mark");

    // Character-level pass.
    let (mut crlf, mut cr, mut ff, mut nul, mut ctl, mut zw, mut shy, mut sp, mut lig) =
        (0, 0, 0, 0, 0, 0, 0, 0, 0);
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\r' => {
                if chars.peek() == Some(&'\n') {
                    chars.next();
                    crlf += 1;
                } else {
                    cr += 1;
                }
                out.push('\n');
            }
            '\u{c}' => {
                ff += 1;
                out.push('\n');
            }
            '\0' => nul += 1,
            '\n' | '\t' => out.push(c),
            c if (c as u32) < 0x20 || ('\u{7f}'..='\u{9f}').contains(&c) => ctl += 1,
            '\u{200b}' | '\u{200c}' | '\u{200d}' | '\u{2060}' | '\u{feff}' => zw += 1,
            '\u{ad}' => shy += 1,
            '\u{a0}' | '\u{2000}'..='\u{200a}' | '\u{202f}' | '\u{205f}' | '\u{3000}' => {
                sp += 1;
                out.push(' ');
            }
            '\u{fb00}'..='\u{fb06}' => {
                lig += 1;
                out.push_str(match c {
                    '\u{fb00}' => "ff",
                    '\u{fb01}' => "fi",
                    '\u{fb02}' => "fl",
                    '\u{fb03}' => "ffi",
                    '\u{fb04}' => "ffl",
                    _ => "st",
                });
            }
            c => out.push(c),
        }
    }
    note(crlf, "converted CRLF line endings to LF");
    note(cr, "converted CR line endings to LF");
    note(ff, "converted form feeds to line breaks");
    note(nul, "removed NUL characters");
    note(ctl, "removed control characters");
    note(zw, "removed zero-width characters");
    note(shy, "removed soft hyphens");
    note(sp, "replaced non-breaking/unusual spaces with spaces");
    note(lig, "expanded ligatures");

    let trailing_newline = out.ends_with('\n');
    let lines: Vec<&str> = out.split('\n').collect();
    let lines = if trailing_newline {
        &lines[..lines.len() - 1]
    } else {
        &lines[..]
    };

    // Words seen in the text, to decide whether a split word keeps its hyphen.
    let mut words: HashSet<String> = HashSet::new();
    for w in out.split(|c: char| !(c.is_alphabetic() || c == '-')) {
        let w = w.trim_matches('-');
        if w.len() > 2 {
            words.insert(w.to_lowercase());
        }
    }

    let blank =
        |i: Option<usize>| i.is_none_or(|i| lines.get(i).is_none_or(|l| l.trim().is_empty()));
    let mut kept: Vec<String> = Vec::with_capacity(lines.len());
    let (mut joined, mut page_nums, mut page_x) = (0, 0, 0);
    let mut in_fence = false;
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        let t = line.trim();
        if t.starts_with("```") {
            in_fence = !in_fence;
            kept.push(line.to_string());
            i += 1;
            continue;
        }
        if in_fence {
            kept.push(line.to_string());
            i += 1;
            continue;
        }
        if !t.is_empty() && PAGE_X_OF_Y.is_match(t) {
            page_x += 1;
            i += 1;
            continue;
        }
        if !t.is_empty() && PAGE_NUM.is_match(t) && blank(i.checked_sub(1)) && blank(Some(i + 1)) {
            page_nums += 1;
            i += 1;
            continue;
        }
        // Join hyphen splits, possibly several in a row.
        let mut cur = line.to_string();
        while let Some(next) = lines.get(i + 1)
            && is_hyphen_split(&cur, next)
        {
            let stem = cur.trim_end().strip_suffix('-').unwrap_or(&cur).to_string();
            let head: String = stem
                .chars()
                .rev()
                .take_while(|c| c.is_alphabetic())
                .collect::<Vec<_>>()
                .into_iter()
                .rev()
                .collect();
            let next_t = next.trim_start();
            let tail: String = next_t.chars().take_while(|c| c.is_alphabetic()).collect();
            let hyphenated = format!("{head}-{tail}").to_lowercase();
            let solid = format!("{head}{tail}").to_lowercase();
            let keep_hyphen = words.contains(&hyphenated) && !words.contains(&solid);
            cur = if keep_hyphen {
                format!("{stem}-{next_t}")
            } else {
                format!("{stem}{next_t}")
            };
            joined += 1;
            i += 1;
        }
        kept.push(cur);
        i += 1;
    }
    note(joined, "joined words split by a line-end hyphen");
    note(page_nums, "removed bare page-number lines");
    note(page_x, "removed \"Page X of Y\" lines");

    // Collapse 3+ blank lines (outside fences).
    let mut result = String::with_capacity(out.len());
    let mut blank_run: Vec<&str> = Vec::new();
    let mut collapsed = 0;
    let mut in_fence = false;
    let mut first = true;
    let mut push_line = |result: &mut String, l: &str| {
        if !first {
            result.push('\n');
        }
        first = false;
        result.push_str(l);
    };
    for l in &kept {
        if l.trim().starts_with("```") {
            in_fence = !in_fence;
        }
        if !in_fence && l.trim().is_empty() {
            blank_run.push(l);
            continue;
        }
        if blank_run.len() >= 3 {
            collapsed += 1;
            push_line(&mut result, "");
        } else {
            for b in &blank_run {
                push_line(&mut result, b);
            }
        }
        blank_run.clear();
        push_line(&mut result, l);
    }
    if blank_run.len() >= 3 {
        collapsed += 1;
        push_line(&mut result, "");
    } else {
        for b in &blank_run {
            push_line(&mut result, b);
        }
    }
    if trailing_newline {
        result.push('\n');
    }
    note(collapsed, "collapsed runs of 3+ blank lines");
    (result, applied)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn noop_on_clean_text() {
        let t =
            "# Part 1 Preliminary\n\n1 Short title\n\nThis Act may be cited as the Example Act.\n";
        let (out, applied) = fix_text(t);
        assert_eq!(out, t);
        assert!(applied.is_empty());
    }

    #[test]
    fn fixes_everything_safe() {
        let t = "\u{feff}The ﬁnancial year\r\nis assess-\r\nable in\u{a0}full\u{ad}y.\r\n\r\n\r\n\r\n\r\n12\r\n\r\nPage 3 of 10\r\nzero\u{200b}width\0 end\r\n";
        let (out, applied) = fix_text(t);
        assert_eq!(
            out,
            "The financial year\nis assessable in fully.\n\nzerowidth end\n"
        );
        let all = applied.join("; ");
        for k in [
            "byte-order mark",
            "CRLF",
            "ligatures",
            "hyphen",
            "page-number",
            "Page X of Y",
            "soft hyphens",
            "zero-width",
            "NUL",
            "non-breaking",
            "blank lines",
        ] {
            assert!(all.contains(k), "missing {k} in {all}");
        }
    }

    #[test]
    fn keeps_hyphen_for_known_compounds() {
        let t = "a self-assessment regime\nunder self-\nassessment rules\n";
        let (out, _) = fix_text(t);
        assert!(out.contains("under self-assessment rules"), "{out}");
    }

    #[test]
    fn page_numbers_need_blank_lines() {
        // A number glued to text may be a paragraph number: kept.
        let t = "text\n12\nmore text\n";
        assert_eq!(fix_text(t).0, t);
    }

    #[test]
    fn fences_untouched() {
        let t = "```\nfoo-\nbar\n\n\n\n\nbaz\n```\n";
        assert_eq!(fix_text(t).0, t);
    }
}
