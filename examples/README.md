# Examples

Every source here is **fictional**, written to exercise the parsers. None of it
is real law.

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
