//! Stable, readable node IDs.
//!
//! `{corpus}` is the root. Provisions are `{corpus}:{abbrev}{number}`
//! (`privacy-act-1988:s6`, `gdpr:art5`). Containers carry their ancestry so
//! "Part 2" in two Chapters stays unique (`code:ch1/pt2`). Paragraphs of a case
//! are `{corpus}:para{n}`, chunks of secondary material `{corpus}:c{n}`.
//! Definitions are `def:{corpus}:{term-slug}` (suffixed on collision).

/// Lowercase ASCII slug. Keeps digits, letters, `-` and `.`; folds runs of
/// anything else to a single `-`.
pub fn slug(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut dash = false;
    for ch in s.chars().flat_map(char::to_lowercase) {
        let c = fold(ch);
        if c.is_ascii_alphanumeric() || c == '.' {
            out.push(c.to_ascii_lowercase());
            dash = false;
        } else if !dash && !out.is_empty() {
            out.push('-');
            dash = true;
        }
    }
    while out.ends_with('-') {
        out.pop();
    }
    out
}

/// Number slug: `8-1(1)(a)` → `8-1.1.a`, `12A` → `12a`.
pub fn number_slug(s: &str) -> String {
    let s = s.replace(['(', '['], ".").replace([')', ']'], "");
    let s = slug(&s);
    s.trim_matches('.').replace("..", ".")
}

/// Normalise a provision number for lookups. A dot after a letter prefix
/// is dropped, so the French citation form "L. 624-5" matches the heading
/// "L624-5"; decimal numbers ("120.31") keep theirs.
pub fn number_key(s: &str) -> String {
    let k: String = s
        .chars()
        .filter(|c| !c.is_whitespace())
        .map(|c| match c {
            '–' | '—' | '‑' => '-',
            c => c.to_ascii_lowercase(),
        })
        .collect::<String>()
        .trim_end_matches('.')
        .to_string();
    let prefix = k.chars().take_while(|c| c.is_alphabetic()).count();
    let rest = &k[k.char_indices().nth(prefix).map_or(k.len(), |(i, _)| i)..];
    if prefix > 0 && rest.starts_with('.') && rest[1..].starts_with(|c: char| c.is_ascii_digit()) {
        format!("{}{}", &k[..k.len() - rest.len()], &rest[1..])
    } else {
        k
    }
}

/// Strip the last bracketed group: `8-1(1)(a)` → `8-1(1)`. `None` when none left.
pub fn strip_last_subdivision(s: &str) -> Option<String> {
    let s = s.trim_end();
    if s.ends_with(')')
        && let Some(i) = s.rfind('(')
    {
        let head = s[..i].trim_end();
        if !head.is_empty() {
            return Some(head.to_string());
        }
    }
    None
}

/// Normalise a case citation for lookups: drop dots, split letters from
/// digits, collapse whitespace, uppercase. `[2024]  hca 1` → `[2024] HCA 1`;
/// `410 U.S. 113` → `410 US 113`; `2023INSC1` → `2023 INSC 1`.
pub fn citation_key(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 4);
    let mut prev: Option<char> = None;
    for c in s.chars().filter(|c| *c != '.') {
        if let Some(p) = prev
            && ((p.is_ascii_digit() && c.is_alphabetic()) || (p.is_alphabetic() && c.is_ascii_digit()))
        {
            out.push(' ');
        }
        out.push(c);
        prev = Some(c);
    }
    out.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_uppercase()
}

fn fold(c: char) -> char {
    match c {
        'à' | 'á' | 'â' | 'ã' | 'ä' | 'å' => 'a',
        'ç' => 'c',
        'è' | 'é' | 'ê' | 'ë' => 'e',
        'ì' | 'í' | 'î' | 'ï' => 'i',
        'ñ' => 'n',
        'ò' | 'ó' | 'ô' | 'õ' | 'ö' => 'o',
        'ù' | 'ú' | 'û' | 'ü' => 'u',
        'ß' => 's',
        '§' => 's',
        c => c,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugs() {
        assert_eq!(slug("Privacy Act 1988"), "privacy-act-1988");
        assert_eq!(slug("  Über §  "), "uber-s");
        assert_eq!(number_slug("8-1(1)(a)"), "8-1.1.a");
        assert_eq!(number_key("8–1 "), "8-1");
        assert_eq!(number_key("L. 624-5"), number_key("L624-5"));
        assert_eq!(number_key("120.31"), "120.31");
        assert_eq!(citation_key("2023INSC1"), citation_key("2023 INSC 1"));
        assert_eq!(citation_key("410 U.S. 113"), "410 US 113");
        assert_eq!(
            strip_last_subdivision("8-1(1)(a)").as_deref(),
            Some("8-1(1)")
        );
        assert_eq!(strip_last_subdivision("8-1"), None);
        assert_eq!(citation_key("[2024]  hca 1"), "[2024] HCA 1");
        assert_eq!(citation_key("410 U.S. 113"), "410 US 113");
    }
}
