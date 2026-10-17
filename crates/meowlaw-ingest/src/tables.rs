//! Tables.
//!
//! Every reader stores a table as one GitHub-flavoured Markdown pipe table in
//! the node text, so tables survive paragraph splitting, render as tables, and
//! keep their rows addressable ("item 3 of the table in section 12-5" links to
//! the row whose first cell is `3`).
//!
//! Sources:
//! - Markdown: pipe tables are kept as one block.
//! - HTML, DOCX, Akoma Ntoso/USLM: rows and cells are read from the markup.
//! - PDF and plain text: fixed-width column layouts (as `pdftotext -layout`
//!   prints them) are detected and rebuilt, see [`detect_layout_tables`].

use regex::Regex;
use std::sync::LazyLock;

/// Cells of one row.
pub type Row = Vec<String>;

/// Render rows as a pipe table. The first row is the header; when `header` is
/// false a numbered header is generated so every table has one.
pub fn to_markdown(rows: &[Row], header: bool) -> String {
    let width = rows.iter().map(Vec::len).max().unwrap_or(0);
    if width == 0 {
        return String::new();
    }
    let cell = |s: &str| {
        s.split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .replace('|', "\\|")
    };
    let line = |r: &Row| {
        let mut cells: Vec<String> = r.iter().map(|c| cell(c)).collect();
        cells.resize(width, String::new());
        format!("| {} |", cells.join(" | "))
    };
    let mut out = Vec::new();
    let body: &[Row] = if header {
        out.push(line(&rows[0]));
        &rows[1..]
    } else {
        let generated: Row = (1..=width)
            .map(|i| {
                if i == 1 && first_col_is_items(rows) {
                    "Item".to_string()
                } else {
                    format!("Column {i}")
                }
            })
            .collect();
        out.push(line(&generated));
        rows
    };
    out.push(format!("|{}", " --- |".repeat(width)));
    out.extend(body.iter().map(line));
    out.join("\n")
}

static ITEM_ID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^\(?[0-9]{1,3}[A-Z]{0,3}(?:\.[0-9]{1,3}[A-Z]{0,2})?\)?\.?$").unwrap()
});

/// "1", "2A", "3.1", "(4)": an item identifier in a table's first column.
pub fn is_item_id(s: &str) -> bool {
    ITEM_ID.is_match(s.trim())
}

fn first_col_is_items(rows: &[Row]) -> bool {
    let ids = rows
        .iter()
        .filter(|r| r.first().is_some_and(|c| is_item_id(c)))
        .count();
    ids * 2 >= rows.len().max(1)
}

/// A segment of laid-out text: prose, or a table rebuilt as Markdown.
#[derive(Debug, Clone, PartialEq)]
pub enum Segment {
    Text(String),
    Table(String),
}

/// Columns are separated by at least this many spaces.
const GAP: usize = 2;
/// Column starts within this many characters are the same column.
const TOL: usize = 2;

/// Cells of a line: (start column, text), split on runs of `GAP`+ spaces.
fn runs(line: &str) -> Vec<(usize, String)> {
    let chars: Vec<char> = line.chars().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        while i < chars.len() && chars[i] == ' ' {
            i += 1;
        }
        if i >= chars.len() {
            break;
        }
        let start = i;
        let mut end = i;
        let mut spaces = 0;
        while i < chars.len() {
            if chars[i] == ' ' {
                spaces += 1;
                if spaces >= GAP {
                    break;
                }
            } else {
                spaces = 0;
                end = i + 1;
            }
            i += 1;
        }
        out.push((start, chars[start..end].iter().collect()));
    }
    out
}

/// Find fixed-width tables in laid-out text and rebuild them.
///
/// A block qualifies when at least 3 lines have 2+ cells whose start columns
/// line up, and either it has 3+ columns or most rows start with an item
/// identifier. That second test keeps two-column page layouts and indented
/// prose out. Lines with a single cell under a column continue the cell above
/// (wrapped text); a line whose first cell is filled starts a new row.
pub fn detect_layout_tables(text: &str) -> Vec<Segment> {
    let lines: Vec<&str> = text.lines().collect();
    let parsed: Vec<Vec<(usize, String)>> = lines.iter().map(|l| runs(l)).collect();
    let mut out = Vec::new();
    let mut prose: Vec<&str> = Vec::new();
    let mut i = 0;
    while i < lines.len() {
        if let Some((end, table)) = table_at(&parsed, i) {
            if !prose.is_empty() {
                out.push(Segment::Text(prose.join("\n")));
                prose.clear();
            }
            out.push(Segment::Table(table));
            i = end;
        } else {
            prose.push(lines[i]);
            i += 1;
        }
    }
    if !prose.is_empty() {
        out.push(Segment::Text(prose.join("\n")));
    }
    out
}

fn table_at(parsed: &[Vec<(usize, String)>], start: usize) -> Option<(usize, String)> {
    if parsed[start].len() < 2 {
        return None;
    }
    // Grow the block: multi-cell lines, single-cell continuation lines, and at
    // most one blank line between rows.
    let mut end = start;
    let mut blanks = 0;
    let mut multi = 0;
    while end < parsed.len() {
        let r = &parsed[end];
        if r.is_empty() {
            blanks += 1;
            if blanks > 1 {
                break;
            }
        } else {
            blanks = 0;
            if r.len() >= 2 {
                multi += 1;
            } else if r[0].0 < 2 && multi > 0 && !is_item_id(&r[0].1) {
                // Flush-left single cell after the table: prose resumes.
                break;
            }
        }
        end += 1;
    }
    while end > start && parsed[end - 1].is_empty() {
        end -= 1;
    }
    if multi < 3 {
        return None;
    }
    // Column starts: cluster the start positions of cells on multi-cell lines.
    let mut starts: Vec<usize> = parsed[start..end]
        .iter()
        .filter(|r| r.len() >= 2)
        .flat_map(|r| r.iter().map(|(c, _)| *c))
        .collect();
    starts.sort_unstable();
    let mut cols: Vec<(usize, usize)> = Vec::new(); // (start, support)
    for s in starts {
        match cols.last_mut() {
            Some((c, n)) if s <= *c + TOL => *n += 1,
            _ => cols.push((s, 1)),
        }
    }
    let min_support = (multi / 3).max(2);
    let cols: Vec<usize> = cols
        .into_iter()
        .filter(|(_, n)| *n >= min_support)
        .map(|(c, _)| c)
        .collect();
    if cols.len() < 2 {
        return None;
    }
    let col_of = |pos: usize| cols.iter().rposition(|c| pos + TOL >= *c).unwrap_or(0);
    // Build rows.
    let mut rows: Vec<Row> = Vec::new();
    for r in &parsed[start..end] {
        if r.is_empty() {
            continue;
        }
        let mut cells: Row = vec![String::new(); cols.len()];
        for (pos, text) in r {
            let c = col_of(*pos);
            if !cells[c].is_empty() {
                cells[c].push(' ');
            }
            cells[c].push_str(text);
        }
        let starts_row = !cells[0].is_empty() || rows.is_empty();
        if starts_row {
            rows.push(cells);
        } else if let Some(last) = rows.last_mut() {
            for (a, b) in last.iter_mut().zip(cells) {
                if !b.is_empty() {
                    if !a.is_empty() {
                        a.push(' ');
                    }
                    a.push_str(&b);
                }
            }
        }
    }
    let items = rows.iter().skip(1).filter(|r| is_item_id(&r[0])).count();
    let itemised = items * 2 >= rows.len().saturating_sub(1).max(1);
    if cols.len() < 3 && !itemised {
        return None;
    }
    // Header: rows before the first item row, merged column-wise.
    let first_item = rows.iter().position(|r| is_item_id(&r[0]));
    let (header, body) = match first_item {
        Some(k) if k > 0 && itemised => {
            let mut h: Row = vec![String::new(); cols.len()];
            for r in &rows[..k] {
                for (a, b) in h.iter_mut().zip(r) {
                    if !b.is_empty() {
                        if !a.is_empty() {
                            a.push(' ');
                        }
                        a.push_str(b);
                    }
                }
            }
            (Some(h), rows[k..].to_vec())
        }
        _ => (None, rows),
    };
    let md = match header {
        Some(h) => {
            let mut all = vec![h];
            all.extend(body);
            to_markdown(&all, true)
        }
        None => to_markdown(&body, false),
    };
    Some((end, md))
}

/// Pipe-table rows in Markdown text: (item id of the first cell, line index).
pub fn table_items(text: &str) -> Vec<String> {
    text.lines()
        .filter(|l| l.trim_start().starts_with('|'))
        .filter_map(|l| {
            l.trim()
                .trim_matches('|')
                .split('|')
                .next()
                .map(|c| c.trim().to_string())
        })
        .filter(|c| is_item_id(c))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const LAYOUT: &str = "\
Some lead-in prose that introduces the table below.

Item    In this case:                        The cost is:
1       A depreciating asset you hold        its market value
        under an arrangement                 at that time
2       A depreciating asset you             the amount you paid
        acquired by gift
3       Any other case                       nil

Prose resumes here after the table.";

    #[test]
    fn rebuilds_layout_table() {
        let segs = detect_layout_tables(LAYOUT);
        assert_eq!(segs.len(), 3, "{segs:#?}");
        let Segment::Table(t) = &segs[1] else {
            panic!("{segs:#?}")
        };
        let lines: Vec<&str> = t.lines().collect();
        assert_eq!(lines[0], "| Item | In this case: | The cost is: |");
        assert_eq!(
            lines[2],
            "| 1 | A depreciating asset you hold under an arrangement | its market value at that time |"
        );
        assert_eq!(
            lines[3],
            "| 2 | A depreciating asset you acquired by gift | the amount you paid |"
        );
        assert_eq!(lines[4], "| 3 | Any other case | nil |");
        assert!(matches!(&segs[2], Segment::Text(s) if s.contains("Prose resumes")));
        assert_eq!(table_items(t), vec!["1", "2", "3"]);
    }

    #[test]
    fn two_column_prose_is_not_a_table() {
        let two_col = "\
The Commissioner may        whether or not the
make a determination        taxpayer has lodged
under this section          a return for the year
at any time                 in question.";
        let segs = detect_layout_tables(two_col);
        assert!(
            segs.iter().all(|s| matches!(s, Segment::Text(_))),
            "{segs:#?}"
        );
    }

    #[test]
    fn markdown_rendering() {
        let rows = vec![vec!["1".into(), "a|b".into()], vec!["2".into(), "c".into()]];
        assert_eq!(
            to_markdown(&rows, false),
            "| Item | Column 2 |\n| --- | --- |\n| 1 | a\\|b |\n| 2 | c |"
        );
    }
}
