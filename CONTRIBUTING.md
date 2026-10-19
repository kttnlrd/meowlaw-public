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

The project is licensed under the GNU Affero General Public License v3.0 or
later, and the copyright holder also offers commercial licences.

By submitting a contribution you grant the copyright holder a perpetual,
worldwide, royalty-free, irrevocable licence to use, reproduce, modify and
distribute your contribution under the AGPL and under any commercial licence,
and you confirm you have the right to grant it. Do not contribute third-party
code or legal text you cannot license on those terms.
