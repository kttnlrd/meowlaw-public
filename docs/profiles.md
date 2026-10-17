# Profiles

A profile describes how a family of sources is written. The engine has no
built-in knowledge of any country; it reads everything from the profile.

Built-ins (`meowlaw profiles list`):

| id | Covers |
|---|---|
| `generic` | English-language conventions common to most jurisdictions. Every other English profile extends it. |
| `au` | Australian drafting: `8-1` style numbers, Subdivision letters, Roman Part numbers, authorised report series. |
| `gb` | UK: Law Reports/WLR/All ER series, SI numbers. |
| `us` | US federal: U.S.C., C.F.R., Title/Subtitle/Chapter/Subchapter/Part, U.S. Reports and Federal Reporter. |
| `eu` | EU: Title > Chapter > Section > Article structure, Regulation/Directive identifiers, CJEU case numbers. |
| `de` | German starter: §/Artikel structure and citation forms. Needs review by a German-qualified lawyer before definition extraction is relied on. |

Starter profiles. Each pattern cites the official page it was checked
against, and each needs review by a lawyer qualified in that jurisdiction
before it is relied on. What each one leaves out is listed in its file header.

| id | Covers |
|---|---|
| `ca` | Canada (federal): Justice Laws structure, SCC/FCA/FC/TCC neutral citations, S.C.R., R.S.C. and S.C. chapters, CanLII links. |
| `nz` | New Zealand: Part/Subpart and `CB 1` sections, neutral citations and report series per the NZ Law Style Guide. |
| `sg` | Singapore: SGCA/SGHC citations and divisions, SLR, eLitigation links. |
| `hk` | Hong Kong: court codes from Practice Direction 5.5, HKCFAR/HKLRD, `Cap 4` Ordinances. |
| `in` | India: `2024 INSC 756`, SCC, "Section 32 of the Income Tax Act, 1961". |
| `ie` | Ireland: IESC/IECA/IEHC, IR/ILRM, S.I. numbers. |
| `za` | South Africa: ZACC/ZASCA neutral citations, SA/BCLR/SACR, "Act 73 of 1998", SAFLII links. |
| `fr` | France: LIVRE/TITRE/Chapitre/Article structure, "article L. 521-2 du code de commerce", lois, décrets, ECLI. Stands alone (French). |

Put your own profiles in `profiles/` (see `storage.profiles_dir`). A file with
the same `id` as a built-in overrides it. `meowlaw profiles show <id>` prints a
profile with inheritance resolved.

## Writing a profile

```toml
id = "xx"
name = "Exampleland"
extends = "generic"          # inherit and add
language = "en"
number = '(?:[0-9]+[A-Z]?)'  # substituted for {NUM} in every pattern

[[levels]]                   # structure, outermost first; first match wins
label = "Part"
abbrev = "pt"                # used in IDs
rank = 30                    # lower = outer
kind = "container"           # container | provision | schedule
pattern = '(?i)^part\s+(?P<num>{NUM})\b\s*(?P<title>.*)$'

[[citations]]
name = "section"
target = { type = "provision", label = "Section" }
pattern = '\bs\s+(?P<nums>{NUM}(?:\s*(?:,|and)\s*{NUM})*)(?:\s+of\s+the\s+(?P<corpus>[A-Z][\w ]+Act))?'

[[citations]]
name = "report"
target = { type = "case" }
pattern = '(?P<cite>\([0-9]{4}\)\s+[0-9]+\s+XLR\s+[0-9]+)'

[definitions]
section_titles = '(?i)\b(definitions|interpretation)\b'
scope_phrase = '(?i)\bin this (?P<label>Act|Part|section)\b'

[[definitions.patterns]]
name = "quoted_means"
pattern = '^\s*[“"](?P<term>[^”"]+)[”"]\s+means\b(?P<rest>.*)'

[cases]
paragraph = '^\s*\[?(?P<num>[0-9]{1,4})\]?\s+\S'

[label_aliases]
"s" = "Section"

[corpus_aliases]
"XA 2020" = "example-act-2020"
```

### Citation targets

| `type` | Meaning | Groups |
|---|---|---|
| `provision` | A numbered unit of a structured corpus. | `num` or `nums` (list: `5, 6 and 8 to 10`), optional `label` (overrides the spec label), optional `corpus` (named corpus). |
| `relative` | A subdivision of the citing provision ("subsection (2)"). Resolves to the enclosing provision. | `num`/`nums` |
| `case` | A case, matched against corpus `citations`. | `cite` (else the whole match) |
| `corpus` | A whole corpus named in text ("Regulation (EU) 2016/679"). Sets context for later bare citations in the same node. | `corpus` |

Ranges expand only when both ends are plain integers less than 200 apart.

### Tables

`[tables]` has two regexes: `item` finds a table item citation (group
`item`), and `link` joins it to a provision citation that follows. See
[tables](tables.md).

### Inheritance

`extends` merges: `levels` from the child if it has any, otherwise the
parent's; `citations` and definition `patterns` are the child's followed by the
parent's (so the child wins overlapping matches); maps merge with the child
winning.

### Testing a profile

1. `meowlaw ingest <file> --profile xx --dry-run` and check the tree.
2. Ingest, then `meowlaw unresolved`: each group is a citation that matched a
   pattern but found no target. Most are sources you have not loaded; the rest
   point at a pattern or alias to fix.
3. `meowlaw define <term>` to check definitions and their scope.

## Source links

A profile can generate links to external copies of a source from its
citations. Named groups in `citation` are substituted into `url`
(`{court_lower}` gives the lowercase form of `court`):

```toml
[[links]]
label = "AustLII"
citation = '^\[(?P<year>[12][0-9]{3})\]\s+(?P<court>HCA|FCA|FCAFC|AATA)\s+(?P<num>[0-9]+)$'
url = "https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/cth/{court}/{year}/{num}.html"
```

Links that cannot be derived from a citation (a court's own website) go on
the corpus, in front matter or a sidecar:

```toml
[[meta.links]]
label = "High Court of Australia"
url = "https://www.hcourt.gov.au/..."
```

Both appear as buttons on the reader screen and in `fetch` output
(`corpus.links`). Only `http` and `https` URLs are shown.
