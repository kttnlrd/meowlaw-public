# Tables

Every reader stores a table as one Markdown pipe table inside the node text:

```
| Item | In this case: | The cost is: |
| --- | --- | --- |
| 1 | A depreciating asset you hold under an arrangement | its market value at that time |
| 2 | A depreciating asset you acquired by gift | the amount you paid |
```

That one form survives paragraph splitting, is searchable like any other text,
renders as an HTML table in the UI, and keeps rows addressable.

## Where tables come from

| Source | How tables are read |
| --- | --- |
| Markdown | Pipe tables are kept as one block. |
| HTML | `<table>` rows and `td`/`th` cells. A first row of only `th` cells is the header. |
| DOCX | `w:tbl` rows and cells. Paragraphs inside a cell join into the cell text. Nested tables fold into the outer cell. |
| Akoma Ntoso, USLM | `<table>`, `<tr>`, `<td>`/`<th>`. Inline markup inside cells is kept as text. |
| PDF, plain text | Fixed-width column layouts (as `pdftotext -layout` prints them) are detected and rebuilt. |

Layout detection in PDF and text needs at least three lines whose cells (split
on two or more spaces) line up, and either three or more columns or rows that
start with an item number ("1", "2A", "3.1", "(4)"). The second test stops
two-column page layouts being read as tables. Lines that only fill later
columns continue the row above (wrapped cell text). Lines before the first
item row become the header.

Layout detection is a heuristic. Check PDF output with
`meowlaw profiles test <file>` or `meowlaw lint <file>`, and convert to DOCX or
HTML first when the PDF's tables are irregular.

When a table has no header row, one is generated: `Item` for a first column of
item numbers, otherwise `Column 1`, `Column 2`, ...

## Item citations

Rows whose first cell is an item number get an anchor. The profile's
`[tables]` section turns citations of those rows into pinpointed references:

```toml
[tables]
item = '\b(?i:table\s+)?(?i:items?)\s+(?P<item>[0-9]{1,3}[A-Z]{0,3}(?:\.[0-9]{1,3}[A-Z]{0,2})?)\s+(?:of|in)\s+(?:the|this|that)\s+table\b'
link = '\s+(?:in|at|of|to)\s+'
```

- "item 3 of the table in section 40-60": when `link` joins the item to a
  provision citation that follows, the two become one reference to that
  provision with `pin = "3"`.
- "item 2 of this table": with nothing after it, the reference is to the
  citing provision's own table.

These patterns are English and live in `generic.toml`; profiles that extend
it inherit them. A profile for another language sets its own `[tables]`.

References carry the pin (`"pin": "3"` in `/api/node` and the MCP `fetch`
tool). The UI links them to `#/node/<id>?item=3`, which scrolls to and
highlights the row.
