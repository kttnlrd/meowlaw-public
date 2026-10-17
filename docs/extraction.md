# Extraction

Extraction runs over **every** corpus on each `rebuild`, so a newly ingested
Act resolves citations to it that were dangling before. It is deterministic
(regex + lookup tables); every edge records its method and the text that
produced it.

## References

For each node, each citation pattern of the corpus's profile is applied. A span
claimed by one pattern is not matched again. Resolution:

1. **Named corpus.** A `corpus` group ("of the Widget Act 2024") or a known
   alias directly after the citation ("s 8-1 ITAA 1997", "§ 823 BGB") is
   looked up against corpus titles and aliases (case-insensitive, "the" and a
   trailing "(Cth)"-style tag ignored).
2. **Context.** Otherwise the most recent corpus named earlier in the same
   node (judgments name the Act once and then cite bare sections).
3. **Self.** Otherwise, for structured corpora (legislation, instruments, rule
   sets, treaties), the citing corpus itself.
4. **Never guess.** If a corpus is named but not loaded, the reference stays
   unresolved rather than falling back to the citing corpus.

Within the corpus, the number is looked up by `(label, number)`, then by number
alone if unambiguous and the label does not contradict. Trailing subdivisions
are dropped until a node matches (`8-1(1)(a)` → `8-1(1)` → `8-1`).

Case citations are normalised (whitespace collapsed, dots removed, uppercase)
and matched against every case corpus's `citations`.

Unresolved references are kept. `meowlaw unresolved` (or the MCP tool
`unresolved_references`) groups them by text: this is the main quality report.

## Definitions

Each paragraph (and each line within it) is tested against the profile's
definition patterns:

- quoted terms: `"widget" means`, `‘seller’ means`, `“X” includes`, `“X” has the meaning`;
- emphasised terms: `**dealer** has the meaning given by section 5` (also `***x***`, `*x*`);
- `the term "X" means` anywhere in a paragraph;
- bare terms (`retail price means`) only inside a definitions/interpretation provision;
- parenthetical defined labels: `(the "Company")`, `(each an "Employee")`.

**Scope.** "In this Part/Division/section ..." scopes the definition to the
nearest enclosing node with that label ("Act", "Code", "Agreement" and similar
scope to the whole corpus). Without a scope phrase: a definitions provision,
a parenthetical label or any non-provision node scopes to the whole corpus; an
inline definition in an ordinary provision scopes to that provision.

**Pointers.** For "has the meaning given by section 5", the resolved reference
in the same text is stored as `points_to`.

## Term usage

For each corpus, every defined term is searched (case-insensitive, whole words)
in every node inside the definition's scope. Matches produce `uses_term` edges,
capped at 100 per node. This is what powers the defined-term hover in the UI
and the "uses defined terms" list in `related`.

## Edges produced

| Kind | From → to | Method |
|---|---|---|
| `contains` | parent → child | structure |
| `cites` | citing node → cited node | pattern |
| `defines` | defining node → definition | pattern |
| `uses_term` | node → definition | pattern |
| `related`, `same_as`, `amends` | any → any | manual (survive rebuilds) |
