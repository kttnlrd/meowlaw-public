# Contributing

```sh
cargo fmt --all
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

- Keep jurisdiction knowledge in `profiles/`, not in Rust code.
- New profile patterns need a test with a short, fictional or properly
  licensed sample.
- Never commit third-party legal text unless its licence allows
  redistribution; say which licence in the PR.
- Extraction must stay explainable: every edge records its method and the text
  that produced it.

## Licence of contributions

The project is licensed under AGPL-3.0-or-later, and the copyright holder also
grants commercial licences. Contributions are accepted only under a
contributor licence agreement that lets the copyright holder do both: by
opening a pull request you confirm you wrote the change, or have the right to
submit it, and you grant the copyright holder a perpetual, worldwide,
royalty-free licence to use it under the AGPL and under any commercial licence.
