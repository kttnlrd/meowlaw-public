# Examples

Most sources here are **fictional**, written to exercise the parsers.

`au-tax/` is the exception: it commits real Australian material (ITAA 1997,
ITAA 1936, TR 2018/5 and a summary of *Bendel* [2026] HCA 18) so that example
runs without any external corpus. Those files are **real law and real
commentary**, each under its own licence and attribution terms, recorded in
[`au-tax/sources/SOURCES.md`](au-tax/sources/SOURCES.md). The AGPL does not
cover them.

```sh
meowlaw ingest examples/widget-act            # multi-file Act with corpus.toml
meowlaw ingest examples/cases --kind case     # a folder of judgments, one corpus each
meowlaw ingest examples/handbook/staff-handbook.md --kind secondary
meowlaw ingest examples/xml/model-code.xml --kind legislation --profile eu
meowlaw search "dealer must not sell"
meowlaw search "s 4 WA 2024"
meowlaw show widget-act:s3
meowlaw unresolved
```
