# Examples

Two kinds of source live here.

`widget-act`, `cases`, `handbook` and `xml` are **fictional**, written to
exercise the parsers. None of it is real law.

`au-tax` is real Australian material: the Income Tax Assessment Acts 1997 and
1936, TR 2018/5 and a summary of *Commissioner of Taxation v Bendel*
[2026] HCA 18. It carries third-party terms of its own; read
[au-tax/sources/SOURCES.md](au-tax/sources/SOURCES.md) before reusing it.

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
