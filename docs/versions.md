# Versions: languages and points in time

Each version of an instrument is ingested as its own source. Versions of the
same instrument share a `work` id; each may set a `language` and an `as_at`
date (`YYYY-MM-DD`):

```toml
+++
id = "test-act-2024"
title = "Test Act 2020"
kind = "legislation"
work = "test-act"
language = "en"
as_at = "2024-07-01"
+++
```

A French version would set `language = "fr"` (and its own profile); the 2020
compilation the same `work` and `language` with `as_at = "2020-01-01"`.

## What MeowLaw does with them

- **Linking.** Corresponding provisions of every pair of versions are linked
  with `same_as` edges. Trees are aligned from the top: children match on
  kind and number ("Section 2" = "Article 2"), and unnumbered children
  of one kind match by position when both sides have the same number of them.
- **Current versions.** Within one work and language, the version with the
  latest `as_at` is current and the others are superseded. Superseded
  versions are left out of search and the source drawer, and citations
  resolve to current versions.
- **Point in time.** `meowlaw search "..." --as-at 2021-01-01` (or `as_at`
  in the API and MCP `search`) searches the version of each work in force on
  that date: the latest with `as_at` on or before it. Works with no version
  that old are left out.
- **Citations.** A citation from one version of a work to that work resolves
  within the citing version. Other citations resolve to the current version
  in the citing document's language when there is one, else the current
  version in the cited version's language.
- **Reader.** Each provision lists its other versions. Same-language
  versions whose text differs have a **compare** link showing the words
  removed and added. MCP `fetch` returns the same list as `versions`, with
  `text_differs`.

`as_at` means the date the compilation is current from. Commencement,
application and transitional provisions are not modelled: check the
instrument itself before relying on a version for a date.

Full-text search uses one stemming language (`[search] language`). For
corpora in several languages, local multilingual embeddings
(`meowlaw models pull multilingual-e5-small`) let a query in one language
find text in another.
