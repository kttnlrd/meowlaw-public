//! Section markdown, parsed into the blocks the reader draws.
//!
//! Follows what the web app does with react-markdown (frontend/src/components/
//! MarkdownRenderers.tsx): `**(a)**` becomes a paragraph marker and sets the indent, a
//! blockquote that opens with `Note:` or `Example:` becomes a note box, the section number is
//! split from the heading, and `<a id="...">` anchors are kept so links can jump to them.

use pulldown_cmark::{Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use regex::Regex;
use std::sync::LazyLock;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Style {
    Plain,
    /// `(1)`, `(a)`, `(ii)`: monospace, a little smaller.
    Marker,
    /// `Note:`, `Example 2:`: bold and muted.
    NoteLabel,
    /// The section number at the start of the main heading.
    Number,
    Code,
}

#[derive(Clone, Debug)]
pub struct Span {
    pub text: String,
    pub bold: bool,
    pub italic: bool,
    pub style: Style,
    pub link: Option<String>,
}

pub type Inline = Vec<Span>;

#[derive(Debug)]
pub enum Block {
    Heading { level: u8, text: Inline },
    /// `indent` is the sub-paragraph level: 0 for `(1)`, 1 for `(a)`, 2 for `(i)`, 3 for `(A)`.
    /// `small` is the muted line under a rule ("Last updated ...").
    Para { text: Inline, indent: u8, small: bool },
    Note { paras: Vec<(Inline, u8)> },
    Table { head: Vec<Inline>, rows: Vec<Vec<Inline>> },
    List { ordered: bool, start: u64, items: Vec<Inline> },
    Rule,
    Code { text: String },
}

pub struct Doc {
    pub blocks: Vec<Block>,
    /// Anchor id -> index of the block that carries it.
    pub anchors: Vec<(String, usize)>,
}

static ANCHOR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"(?i)<a\s+(?:id|name)\s*=\s*["']([^"']+)["']"#).unwrap());
static BREAK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^<br\s*/?>$").unwrap());
static TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]*>").unwrap());
static MARKER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\([0-9A-Za-z]{1,6}\)$").unwrap());
static NOTE_LABEL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(Notes?|Examples?|Exceptions?)(\s+\d+[A-Z]?)?:$").unwrap());
static NOTE_BLOCK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(Notes?|Examples?|Exceptions?)(\s+\d+[A-Z]?)?:").unwrap());
static LEVEL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\(([^)]{1,6})\)").unwrap());
static ROMAN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^(i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|xiii|xiv|xv|xvi|xvii|xviii|xix|xx)$").unwrap());
static SECTION_NUM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)^\s*((?:s\s?)?[0-9][0-9A-Za-z.\-]*)\s+(.*)$").unwrap());

pub fn plain(text: &Inline) -> String {
    text.iter().map(|s| s.text.as_str()).collect()
}

/// `subparagraphLevel`.
fn level(text: &Inline) -> u8 {
    let all = plain(text);
    let Some(c) = LEVEL.captures(all.trim()) else { return 0 };
    let tok = &c[1];
    if tok.chars().all(|c| c.is_ascii_digit()) {
        0
    } else if ROMAN.is_match(tok) {
        2
    } else if tok.len() <= 3 && tok.chars().all(|c| c.is_ascii_lowercase()) {
        1
    } else if tok.len() == 1 && tok.chars().all(|c| c.is_ascii_uppercase()) {
        3
    } else {
        0
    }
}

/// Collects inline events into spans, collapsing white space the way HTML does.
#[derive(Default)]
struct Line {
    spans: Inline,
    bold: u32,
    italic: u32,
    link: Vec<String>,
    /// Text of the `**...**` run being read, to classify it when it closes.
    strong_from: Option<usize>,
    space_pending: bool,
    any: bool,
}

impl Line {
    fn push(&mut self, text: &str, style: Style) {
        for word in text.split_inclusive(char::is_whitespace) {
            let (body, ends_space) = match word.char_indices().next_back() {
                Some((i, c)) if c.is_whitespace() => (&word[..i], true),
                _ => (word, false),
            };
            if !body.is_empty() {
                let space = self.space_pending && self.any;
                self.space_pending = false;
                self.any = true;
                let (bold, italic, link) = (self.bold > 0, self.italic > 0, self.link.last().cloned());
                let simple = |link: &Option<String>, style: Style| link.is_none() && style == Style::Plain;
                match self.spans.last_mut() {
                    Some(last) if last.bold == bold && last.italic == italic && last.style == style && last.link == link => {
                        if space {
                            last.text.push(' ');
                        }
                        last.text.push_str(body);
                    }
                    last => {
                        // A space between two runs lies outside the markup: it goes with the run
                        // that is plain text, so a link or a marker never starts or ends with it.
                        let mut lead = "";
                        if space {
                            match last {
                                Some(last) if simple(&last.link, last.style) => last.text.push(' '),
                                _ if simple(&link, style) => lead = " ",
                                _ => self.spans.push(Span { text: " ".into(), bold: false, italic: false, style: Style::Plain, link: None }),
                            }
                        }
                        self.spans.push(Span { text: format!("{lead}{body}"), bold, italic, style, link });
                    }
                }
            }
            if ends_space {
                self.space_pending = true;
            }
        }
    }

    fn newline(&mut self) {
        if let Some(last) = self.spans.last_mut() {
            last.text.push('\n');
        }
        self.space_pending = false;
    }

    fn take(&mut self) -> Inline {
        self.space_pending = false;
        self.any = false;
        std::mem::take(&mut self.spans)
    }
}

enum Frame {
    Quote(Vec<Block>),
    List { ordered: bool, start: u64, items: Vec<Inline> },
    Table { head: Vec<Inline>, rows: Vec<Vec<Inline>>, row: Vec<Inline>, in_head: bool },
}

pub fn parse(body: &str, legislation: bool) -> Doc {
    let mut blocks: Vec<Block> = Vec::new();
    let mut anchors: Vec<(String, usize)> = Vec::new();
    let mut stack: Vec<Frame> = Vec::new();
    let mut line = Line::default();
    let mut after_rule = false;
    let mut heading: Option<u8> = None;
    let mut code: Option<String> = None;
    let mut item_depth = 0usize;

    // A finished block goes to the innermost open blockquote, else to the document.
    fn emit(stack: &mut [Frame], blocks: &mut Vec<Block>, block: Block) {
        for frame in stack.iter_mut().rev() {
            if let Frame::Quote(inner) = frame {
                inner.push(block);
                return;
            }
        }
        blocks.push(block);
    }

    let options = Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH;
    for event in Parser::new_ext(body, options) {
        match event {
            Event::Start(tag) => match tag {
                Tag::Heading { level, .. } => {
                    heading = Some(match level {
                        HeadingLevel::H1 => 1,
                        HeadingLevel::H2 => 2,
                        _ => 3,
                    })
                }
                Tag::BlockQuote(_) => stack.push(Frame::Quote(Vec::new())),
                Tag::List(start) => stack.push(Frame::List { ordered: start.is_some(), start: start.unwrap_or(1), items: Vec::new() }),
                Tag::Item => item_depth += 1,
                Tag::Table(_) => stack.push(Frame::Table { head: Vec::new(), rows: Vec::new(), row: Vec::new(), in_head: false }),
                Tag::TableHead => {
                    if let Some(Frame::Table { in_head, .. }) = stack.last_mut() {
                        *in_head = true;
                    }
                }
                Tag::Strong => {
                    line.bold += 1;
                    if line.bold == 1 {
                        line.strong_from = Some(line.spans.len());
                    }
                }
                Tag::Emphasis => line.italic += 1,
                Tag::Link { dest_url, .. } => line.link.push(dest_url.to_string()),
                Tag::CodeBlock(_) => code = Some(String::new()),
                _ => {}
            },
            Event::End(tag) => match tag {
                TagEnd::Heading(_) => {
                    let mut text = line.take();
                    let level = heading.take().unwrap_or(1);
                    if level == 1 && legislation {
                        // "104-10  Disposal of a CGT asset": a monospace number, then the title.
                        let split = text.first().filter(|s| s.link.is_none()).and_then(|s| SECTION_NUM.captures(&s.text)).map(|c| (c[1].to_string(), c[2].to_string()));
                        if let Some((num, rest)) = split {
                            let first = text[0].clone();
                            text[0] = Span { text: rest, ..first.clone() };
                            text.insert(0, Span { text: num, style: Style::Number, ..first });
                        }
                    }
                    emit(&mut stack, &mut blocks, Block::Heading { level, text });
                    after_rule = false;
                }
                TagEnd::Paragraph => {
                    let text = line.take();
                    if item_depth > 0 {
                        // A loose list item: keep its text for the item.
                        line.spans = text;
                        line.any = !line.spans.is_empty();
                        line.space_pending = line.any;
                    } else if !text.is_empty() {
                        let indent = level(&text);
                        emit(&mut stack, &mut blocks, Block::Para { text, indent, small: after_rule });
                        after_rule = false;
                    }
                }
                TagEnd::BlockQuote(_) => {
                    if let Some(Frame::Quote(inner)) = stack.pop() {
                        let first_text = inner.iter().find_map(|b| match b {
                            Block::Para { text, .. } => Some(plain(text)),
                            _ => None,
                        });
                        let all_paras = inner.iter().all(|b| matches!(b, Block::Para { .. }));
                        if all_paras && first_text.is_some_and(|t| NOTE_BLOCK.is_match(t.trim())) {
                            let paras = inner
                                .into_iter()
                                .filter_map(|b| match b {
                                    Block::Para { text, indent, .. } => Some((text, indent)),
                                    _ => None,
                                })
                                .collect();
                            // A note is one block: anchors inside it point at the note.
                            if stack.is_empty() {
                                let at = blocks.len();
                                anchors.iter_mut().filter(|(_, i)| *i > at).for_each(|(_, i)| *i = at);
                            }
                            emit(&mut stack, &mut blocks, Block::Note { paras });
                        } else {
                            for b in inner {
                                emit(&mut stack, &mut blocks, b);
                            }
                        }
                    }
                }
                TagEnd::Item => {
                    item_depth = item_depth.saturating_sub(1);
                    let text = line.take();
                    for frame in stack.iter_mut().rev() {
                        if let Frame::List { items, .. } = frame {
                            items.push(text);
                            break;
                        }
                    }
                }
                TagEnd::List(_) => {
                    if let Some(Frame::List { ordered, start, items }) = stack.pop() {
                        if item_depth > 0 {
                            // A nested list: fold its items into the parent item's text.
                            for item in items {
                                line.newline();
                                line.spans.extend(item);
                            }
                        } else {
                            emit(&mut stack, &mut blocks, Block::List { ordered, start, items });
                        }
                    }
                }
                TagEnd::TableCell => {
                    let cell = line.take();
                    if let Some(Frame::Table { row, .. }) = stack.last_mut() {
                        row.push(cell);
                    }
                }
                TagEnd::TableHead => {
                    if let Some(Frame::Table { head, row, in_head, .. }) = stack.last_mut() {
                        *head = std::mem::take(row);
                        *in_head = false;
                    }
                }
                TagEnd::TableRow => {
                    if let Some(Frame::Table { rows, row, .. }) = stack.last_mut() {
                        rows.push(std::mem::take(row));
                    }
                }
                TagEnd::Table => {
                    if let Some(Frame::Table { head, rows, .. }) = stack.pop() {
                        emit(&mut stack, &mut blocks, Block::Table { head, rows });
                    }
                }
                TagEnd::Strong => {
                    line.bold = line.bold.saturating_sub(1);
                    if line.bold == 0 {
                        if let Some(from) = line.strong_from.take() {
                            let run: String = line.spans[from.min(line.spans.len())..].iter().filter(|s| s.bold).map(|s| s.text.as_str()).collect();
                            let style = if MARKER.is_match(run.trim()) {
                                Some(Style::Marker)
                            } else if NOTE_LABEL.is_match(run.trim()) {
                                Some(Style::NoteLabel)
                            } else {
                                None
                            };
                            if let Some(style) = style {
                                // The run may share its first span with text before it: split there.
                                let start = line.spans[..from.min(line.spans.len())].len();
                                for span in line.spans[start..].iter_mut().filter(|s| s.bold && s.style == Style::Plain) {
                                    span.style = style;
                                }
                            }
                        }
                    }
                }
                TagEnd::Emphasis => line.italic = line.italic.saturating_sub(1),
                TagEnd::Link => {
                    line.link.pop();
                }
                TagEnd::CodeBlock => {
                    if let Some(text) = code.take() {
                        emit(&mut stack, &mut blocks, Block::Code { text: text.trim_end().to_string() });
                    }
                }
                _ => {}
            },
            Event::Text(t) => match &mut code {
                Some(c) => c.push_str(&t),
                None => line.push(&t, Style::Plain),
            },
            Event::Code(t) => line.push(&t, Style::Code),
            Event::SoftBreak => line.push(" ", Style::Plain),
            Event::HardBreak => line.newline(),
            Event::Html(h) | Event::InlineHtml(h) => {
                // The block being read lands after everything finished so far, counting the
                // paragraphs waiting in open blockquotes.
                let waiting: usize = stack.iter().map(|f| if let Frame::Quote(inner) = f { inner.len() } else { 0 }).sum();
                for c in ANCHOR.captures_iter(&h) {
                    anchors.push((c[1].to_string(), blocks.len() + waiting));
                }
                if BREAK.is_match(h.trim()) {
                    line.newline();
                } else {
                    // Any other markup is dropped; text between tags arrives as its own events.
                    let rest = TAG.replace_all(&h, "");
                    if !rest.trim().is_empty() {
                        line.push(&rest, Style::Plain);
                    }
                }
            }
            Event::Rule => {
                emit(&mut stack, &mut blocks, Block::Rule);
                after_rule = true;
            }
            _ => {}
        }
    }
    // Anchors read inside a blockquote point past the last finished block until it closes.
    let last = blocks.len().saturating_sub(1);
    for (_, at) in anchors.iter_mut() {
        *at = (*at).min(last);
    }
    Doc { blocks, anchors }
}
