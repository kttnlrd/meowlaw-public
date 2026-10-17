//! `cargo run -p meowlaw-lint --example lint -- <path> [kind] [profile]`
//! Prints one line per finding, then the stats, for each file.

use meowlaw_core::{CorpusKind, ProfileRegistry};
use meowlaw_lint::{LintOptions, lint_path};

fn main() -> anyhow::Result<()> {
    let mut args = std::env::args().skip(1);
    let path = args.next().expect("usage: lint <path> [kind] [profile]");
    let kind = args.next().and_then(|k| CorpusKind::parse(&k));
    let profile = match args.next() {
        Some(p) => Some(ProfileRegistry::builtin()?.get(&p)?),
        None => None,
    };
    for r in lint_path(std::path::Path::new(&path), &LintOptions { profile, kind })? {
        println!("== {} ({})", r.path, r.format);
        for f in &r.findings {
            let at = match (f.page, f.line) {
                (Some(p), Some(l)) => format!("p{p}:{l}"),
                (Some(p), None) => format!("p{p}"),
                (None, Some(l)) => format!("{l}"),
                _ => "-".into(),
            };
            println!(
                "{:7} {:32} {:>6}  {}",
                f.severity.as_str(),
                f.rule,
                at,
                f.message
            );
            if let Some(e) = &f.excerpt {
                println!("{:48}> {e}", "");
            }
        }
        println!("{}", r.stats);
    }
    Ok(())
}
