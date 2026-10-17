//! `meowlaw` command line.

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use meowlaw_core::ProfileRegistry;
use meowlaw_ingest::{Ingester, Manifest};
use meowlaw_mcp::McpServer;
use meowlaw_search::{Engine, Filters, Mode};
use meowlaw_server::{AppState, Config};
use meowlaw_store::Store;
use serde_json::json;
use std::path::{Path, PathBuf};
use std::sync::Arc;

#[derive(Parser)]
#[command(
    name = "meowlaw",
    version,
    about = "Ingest, link and search legal sources from any jurisdiction"
)]
struct Cli {
    /// Config file.
    #[arg(
        long,
        short,
        global = true,
        default_value = "meowlaw.toml",
        env = "MEOWLAW_CONFIG"
    )]
    config: PathBuf,
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Write a starter meowlaw.toml in the current directory.
    Init,
    /// Check source files before ingesting: encoding, invisible characters,
    /// page furniture, tables, headings the profile misses, numbering, DOCX
    /// tracked changes and auto-numbering, PDF text layers. Exits non-zero on errors.
    Lint {
        /// File or directory.
        path: PathBuf,
        #[arg(long)]
        profile: Option<String>,
        #[arg(long)]
        kind: Option<String>,
        /// Lowest severity shown: error, warning or info.
        #[arg(long, default_value = "info")]
        min: String,
        /// Apply the safe fixes to .txt and .md files (keeps a .bak copy).
        #[arg(long)]
        fix: bool,
        #[arg(long)]
        json: bool,
        /// List every rule and exit.
        #[arg(long)]
        rules: bool,
    },
    /// Ingest a file or directory, then re-run extraction and indexing.
    Ingest {
        path: PathBuf,
        /// legislation | instrument | rule_set | case | treaty | guidance | secondary
        #[arg(long)]
        kind: Option<String>,
        /// Profile id (generic, au, gb, us, eu, de, or your own).
        #[arg(long)]
        profile: Option<String>,
        #[arg(long)]
        id: Option<String>,
        #[arg(long)]
        title: Option<String>,
        #[arg(long)]
        jurisdiction: Option<String>,
        /// md | txt | html | xml | docx | pdf | json (default: from extension)
        #[arg(long)]
        format: Option<String>,
        /// Short name that should resolve to this corpus (repeatable).
        #[arg(long = "alias")]
        aliases: Vec<String>,
        #[arg(long)]
        licence: Option<String>,
        #[arg(long)]
        as_at: Option<String>,
        #[arg(long)]
        source_url: Option<String>,
        /// Skip extraction/indexing (run `meowlaw rebuild` after a batch).
        #[arg(long)]
        no_rebuild: bool,
        /// Parse and print the tree without storing it.
        #[arg(long)]
        dry_run: bool,
    },
    /// Re-run reference/definition extraction over everything and rebuild the index.
    Rebuild,
    /// Bring the store in line with a sources.toml: skip unchanged files, diff
    /// and apply changed ones, rebuild, embed, and write a report.
    Sync {
        /// sources.toml with one [[source]] per file or directory.
        sources: PathBuf,
        /// Remove every listed source and ingest it again.
        #[arg(long)]
        fresh: bool,
        /// Show what would change; change nothing.
        #[arg(long)]
        dry_run: bool,
        /// Re-ingest sources even when their files are unchanged.
        #[arg(long)]
        force: bool,
        /// Skip embeddings and similarity edges afterwards.
        #[arg(long)]
        no_embed: bool,
        #[arg(long)]
        /// Do not lint changed sources.
        #[arg(long)]
        no_lint: bool,
        json: bool,
    },
    /// Remove a corpus.
    Remove { corpus: String },
    /// List corpora with counts.
    Corpora,
    /// Store statistics.
    Stats,
    /// Search.
    Search {
        query: String,
        #[arg(long, default_value = "enriched")]
        mode: String,
        #[arg(long)]
        corpus: Option<String>,
        #[arg(long)]
        kind: Option<String>,
        #[arg(long)]
        jurisdiction: Option<String>,
        /// Search the versions in force on this date (YYYY-MM-DD).
        #[arg(long)]
        as_at: Option<String>,
        #[arg(long, default_value_t = 10)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    /// Show a node (or `--citation "s 12 Example Act"`).
    Show {
        id: Option<String>,
        #[arg(long)]
        citation: Option<String>,
    },
    /// Table of contents under a corpus or node.
    Outline {
        id: String,
        #[arg(long, default_value_t = 2)]
        depth: usize,
    },
    /// Look up a defined term.
    Define {
        term: String,
        #[arg(long)]
        corpus: Option<String>,
    },
    /// Resolve the citations in a piece of text.
    Resolve {
        text: String,
        #[arg(long)]
        corpus: Option<String>,
    },
    /// Unresolved references, grouped (data-quality report).
    Unresolved {
        #[arg(long)]
        corpus: Option<String>,
        #[arg(long, default_value_t = 50)]
        limit: usize,
    },
    /// Graph queries and export.
    Graph {
        #[command(subcommand)]
        cmd: GraphCmd,
    },
    /// Profiles.
    Profiles {
        #[command(subcommand)]
        cmd: ProfileCmd,
    },
    /// Compute embeddings for nodes that lack them, then rebuild `similar` edges.
    Embed,
    /// Embedding models: list presets or download one.
    Models {
        #[command(subcommand)]
        cmd: ModelCmd,
    },
    /// Rebuild `similar` edges from stored embeddings (after changing [similarity]).
    Similarity,
    /// Replace placeholder summaries (opening text) with machine-generated
    /// ones from the [summaries] endpoint. Labelled as machine-generated.
    Summarise {
        /// Only this corpus.
        #[arg(long)]
        corpus: Option<String>,
        /// List what would be summarised; call nothing.
        #[arg(long)]
        dry_run: bool,
        /// Skip embeddings afterwards.
        #[arg(long)]
        no_embed: bool,
    },
    /// Run the HTTP server (UI, REST API, MCP at /mcp).
    Serve {
        #[arg(long)]
        bind: Option<String>,
    },
    /// Run the MCP server over stdio (for desktop clients).
    Mcp,
}

#[derive(Subcommand)]
enum GraphCmd {
    Neighbourhood {
        id: String,
        #[arg(long, default_value_t = 1)]
        depth: usize,
        #[arg(long, default_value_t = 100)]
        limit: usize,
    },
    Path {
        from: String,
        to: String,
        #[arg(long, default_value_t = 6)]
        max_hops: usize,
    },
    /// Export for Gephi / Cytoscape / Graphviz.
    Export {
        /// json | graphml | dot
        #[arg(long, default_value = "json")]
        format: String,
        #[arg(long)]
        corpus: Option<String>,
        /// Include parent→child tree edges.
        #[arg(long)]
        tree: bool,
        #[arg(long)]
        out: Option<PathBuf>,
    },
}

#[derive(Subcommand)]
enum ModelCmd {
    /// Presets that `pull` can fetch.
    List,
    /// Download a preset into <data_dir>/models/<name> and print the config to use.
    Pull {
        #[arg(default_value = "bge-small-en-v1.5")]
        name: String,
    },
}

#[derive(Subcommand)]
enum ProfileCmd {
    /// Run a profile over a sample file: headings, citations and definitions
    /// it matched or missed. Nothing is stored.
    Test {
        file: PathBuf,
        #[arg(long)]
        profile: Option<String>,
        #[arg(long)]
        kind: Option<String>,
        /// Do not resolve against sources already in the store.
        #[arg(long)]
        standalone: bool,
        #[arg(long)]
        json: bool,
    },
    List,
    /// Print a profile with inheritance resolved.
    Show {
        id: String,
    },
}

fn registry(cfg: &Config) -> Result<ProfileRegistry> {
    let mut r = ProfileRegistry::builtin()?;
    if let Some(dir) = cfg.profiles_dir() {
        r = r.with_dir(&dir)?;
    }
    Ok(r)
}

fn engine(cfg: &Config) -> Result<Engine> {
    let store = Store::open(cfg.db_path())?;
    let mut e = Engine::open_with_language(
        store,
        &cfg.index_dir(),
        registry(cfg)?,
        &cfg.search.language,
    )?;
    if let Some(em) = cfg.embedder()? {
        e = e.with_embedder(em)?;
    }
    Ok(e)
}

fn print(v: &serde_json::Value) {
    println!("{}", serde_json::to_string_pretty(v).unwrap_or_default());
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .with_writer(std::io::stderr)
        .init();
    let cfg = Config::load(&cli.config)?;
    match cli.cmd {
        Cmd::Init => init(&cli.config),
        Cmd::Ingest {
            path,
            kind,
            profile,
            id,
            title,
            jurisdiction,
            format,
            aliases,
            licence,
            as_at,
            source_url,
            no_rebuild,
            dry_run,
        } => {
            let overrides = Manifest {
                id,
                title,
                kind,
                jurisdiction,
                profile,
                aliases,
                source_url,
                as_at,
                licence,
                format,
                ..Default::default()
            };
            let ing = Ingester::new(registry(&cfg)?);
            let docs = ing.ingest_path(&path, &overrides)?;
            if dry_run {
                for d in &docs {
                    println!(
                        "{} [{}] {} nodes",
                        d.corpus.id,
                        d.corpus.kind.as_str(),
                        d.nodes.len()
                    );
                    for n in d.nodes.iter().take(200) {
                        println!("  {:<40} {}", n.id, n.heading());
                    }
                }
                return Ok(());
            }
            let e = engine(&cfg)?;
            for d in &docs {
                meowlaw_ingest::validate(d)?;
                e.store.put_document(d)?;
                eprintln!("ingested {} ({} nodes)", d.corpus.id, d.nodes.len());
            }
            if !no_rebuild {
                print(&serde_json::to_value(meowlaw_search::rebuild(&e)?)?);
            }
            Ok(())
        }
        Cmd::Sync {
            sources,
            fresh,
            dry_run,
            force,
            no_embed,
            no_lint,
            json: as_json,
        } => {
            let e = engine(&cfg)?;
            let (file, base) = meowlaw_search::sync::SourcesFile::load(&sources)?;
            let opts = meowlaw_search::sync::SyncOptions {
                fresh,
                dry_run,
                force,
                skip_lint: no_lint,
            };
            let report = meowlaw_search::sync::sync(&e, &registry(&cfg)?, &file.sources, &base, opts)?;
            let md = report.to_markdown();
            if !dry_run {
                let dir = cfg.resolve(&cfg.storage.data_dir).join("reports");
                std::fs::create_dir_all(&dir)?;
                let path = dir.join(format!("sync-{}.md", report.started_unix));
                std::fs::write(&path, &md)?;
                eprintln!("report: {}", path.display());
            }
            if as_json {
                print(&serde_json::to_value(&report)?);
            } else {
                print!("{md}");
            }
            if report.changed() && !dry_run && cfg.summaries.as_ref().is_some_and(|s| s.auto) {
                summarise(&cfg, &e, None, false)?;
            }
            if report.changed() && !dry_run && !no_embed && cfg.embeddings.is_some() {
                drop(e);
                embed(&cfg)?;
            }
            if report.sources.iter().any(|s| s.status == "error") {
                bail!("some sources failed; see the report");
            }
            Ok(())
        }
        Cmd::Summarise {
            corpus,
            dry_run,
            no_embed,
        } => {
            let e = engine(&cfg)?;
            let changed = summarise(&cfg, &e, corpus.as_deref(), dry_run)?;
            if changed && !no_embed && cfg.embeddings.is_some() {
                drop(e);
                embed(&cfg)?;
            }
            Ok(())
        }
        Cmd::Lint {
            path,
            profile,
            kind,
            min,
            fix,
            json: as_json,
            rules,
        } => {
            use meowlaw_lint::Severity;
            if rules {
                for (id, what) in meowlaw_lint::RULES {
                    println!("{id:34} {what}");
                }
                return Ok(());
            }
            let min = match min.as_str() {
                "error" => Severity::Error,
                "warning" => Severity::Warning,
                "info" => Severity::Info,
                other => bail!("--min is error, warning or info, not `{other}`"),
            };
            let opts = meowlaw_lint::LintOptions {
                profile: match profile {
                    Some(p) => Some(registry(&cfg)?.get(&p)?),
                    None => None,
                },
                kind: kind
                    .map(|k| meowlaw_core::CorpusKind::parse(&k).with_context(|| format!("unknown kind `{k}`")))
                    .transpose()?,
            };
            if fix {
                let files: Vec<PathBuf> = if path.is_dir() {
                    walkdir::WalkDir::new(&path)
                        .into_iter()
                        .filter_map(|e| e.ok())
                        .map(|e| e.into_path())
                        .collect()
                } else {
                    vec![path.clone()]
                };
                for f in files.iter().filter(|f| {
                    matches!(f.extension().and_then(|e| e.to_str()), Some("txt" | "md" | "markdown"))
                        && !f.to_string_lossy().contains(".meta.")
                }) {
                    let src = std::fs::read_to_string(f).with_context(|| format!("reading {}", f.display()))?;
                    let (fixed, applied) = meowlaw_lint::fix_text(&src);
                    if fixed != src {
                        std::fs::write(PathBuf::from(format!("{}.bak", f.display())), &src)?;
                        std::fs::write(f, &fixed)?;
                        eprintln!("fixed {}: {}", f.display(), applied.join(", "));
                    }
                }
            }
            let reports = meowlaw_lint::lint_path(&path, &opts)?;
            let errors: usize = reports.iter().map(|r| r.count(Severity::Error)).sum();
            if as_json {
                print(&serde_json::to_value(&reports)?);
            } else {
                for r in &reports {
                    let shown: Vec<_> = r.findings.iter().filter(|f| f.severity <= min).collect();
                    println!(
                        "{} ({}): {} errors, {} warnings, {} info",
                        r.path,
                        r.format,
                        r.count(Severity::Error),
                        r.count(Severity::Warning),
                        r.count(Severity::Info)
                    );
                    for f in shown {
                        let at = match (f.page, f.line) {
                            (Some(p), Some(l)) => format!("p{p}:{l}"),
                            (Some(p), None) => format!("p{p}"),
                            (None, Some(l)) => format!("{l}"),
                            _ => String::new(),
                        };
                        println!("  {:7} {:30} {:>7}  {}", f.severity.as_str(), f.rule, at, f.message);
                        if let Some(e) = &f.excerpt {
                            println!("  {:47}> {e}", "");
                        }
                        if let Some(x) = &f.fix {
                            println!("  {:47}fix: {x}", "");
                        }
                    }
                }
            }
            if errors > 0 {
                bail!("{errors} error(s); see above");
            }
            Ok(())
        }
        Cmd::Rebuild => {
            let e = engine(&cfg)?;
            print(&serde_json::to_value(meowlaw_search::rebuild(&e)?)?);
            Ok(())
        }
        Cmd::Remove { corpus } => {
            let e = engine(&cfg)?;
            if !e.store.delete_corpus(&corpus)? {
                bail!("no corpus `{corpus}`");
            }
            print(&serde_json::to_value(meowlaw_search::rebuild(&e)?)?);
            Ok(())
        }
        Cmd::Corpora => {
            let e = engine(&cfg)?;
            for c in e.store.corpus_summaries()? {
                println!(
                    "{:<32} {:<12} {:<8} nodes={:<6} defs={:<5} refs={} (unresolved {})  {}",
                    c.corpus.id,
                    c.corpus.kind.as_str(),
                    c.corpus.jurisdiction.as_deref().unwrap_or("-"),
                    c.nodes,
                    c.definitions,
                    c.references_out,
                    c.unresolved_out,
                    c.corpus.title
                );
            }
            Ok(())
        }
        Cmd::Stats => {
            let e = engine(&cfg)?;
            print(&serde_json::to_value(e.store.stats()?)?);
            Ok(())
        }
        Cmd::Search {
            query,
            mode,
            corpus,
            kind,
            jurisdiction,
            as_at,
            limit,
            json: as_json,
        } => {
            let e = engine(&cfg)?;
            let mode = Mode::parse(&mode).context("mode is basic or enriched")?;
            let r = e.search(
                &query,
                mode,
                &Filters {
                    corpus,
                    kind,
                    jurisdiction,
                    exclude: Vec::new(),
                    as_at,
                },
                limit,
            )?;
            if as_json {
                print(&serde_json::to_value(&r)?);
                return Ok(());
            }
            for c in &r.citations {
                println!(
                    "citation  {}  →  {}",
                    c.raw,
                    if c.targets.is_empty() {
                        "(unresolved)".into()
                    } else {
                        c.targets.join(", ")
                    }
                );
            }
            for d in &r.definitions {
                println!(
                    "defined   “{}” ({})  {}",
                    d.term,
                    d.source,
                    meowlaw_search::index::truncate(&d.text, 120)
                );
            }
            for (i, h) in r.results.iter().enumerate() {
                println!(
                    "{:>2}. {}  [{}]\n    {} · {}\n    {}",
                    i + 1,
                    h.heading,
                    h.matched_by.join("+"),
                    h.corpus_title,
                    h.id,
                    h.snippet.replace('\n', " ")
                );
            }
            Ok(())
        }
        Cmd::Show { id, citation } => {
            let e = engine(&cfg)?;
            print(&meowlaw_mcp::tools::fetch(
                &e,
                &json!({ "id": id, "citation": citation }),
            )?);
            Ok(())
        }
        Cmd::Outline { id, depth } => {
            let e = engine(&cfg)?;
            print(&meowlaw_mcp::tools::call(
                &e,
                "outline",
                &json!({ "id": id, "depth": depth }),
            )?);
            Ok(())
        }
        Cmd::Define { term, corpus } => {
            let e = engine(&cfg)?;
            print(&meowlaw_mcp::tools::call(
                &e,
                "get_definition",
                &json!({ "term": term, "corpus": corpus }),
            )?);
            Ok(())
        }
        Cmd::Resolve { text, corpus } => {
            let e = engine(&cfg)?;
            print(&json!({ "citations": e.resolve_citations(&text, corpus.as_deref())? }));
            Ok(())
        }
        Cmd::Unresolved { corpus, limit } => {
            let e = engine(&cfg)?;
            for g in e.store.unresolved(corpus.as_deref(), limit)? {
                println!(
                    "{:>6}  {:<18} {:<40} e.g. {}",
                    g.count, g.pattern, g.raw, g.example_source
                );
            }
            Ok(())
        }
        Cmd::Graph { cmd } => {
            let e = engine(&cfg)?;
            match cmd {
                GraphCmd::Neighbourhood { id, depth, limit } => {
                    let nb = e
                        .store
                        .neighbourhood(&id, depth, &[], limit)?
                        .with_context(|| format!("no node `{id}`"))?;
                    print(&serde_json::to_value(nb)?);
                }
                GraphCmd::Path { from, to, max_hops } => {
                    print(&serde_json::to_value(e.store.graph_path(
                        &from,
                        &to,
                        max_hops,
                        &[],
                    )?)?);
                }
                GraphCmd::Export {
                    format,
                    corpus,
                    tree,
                    out,
                } => {
                    let s = e.store.export_graph(&format, corpus.as_deref(), tree)?;
                    match out {
                        Some(p) => std::fs::write(&p, s)?,
                        None => print!("{s}"),
                    }
                }
            }
            Ok(())
        }
        Cmd::Profiles { cmd } => {
            let r = registry(&cfg)?;
            match cmd {
                ProfileCmd::Test {
                    file,
                    profile,
                    kind,
                    standalone,
                    json: as_json,
                } => {
                    let overrides = Manifest {
                        profile,
                        kind,
                        ..Default::default()
                    };
                    let store = if standalone || !cfg.db_path().exists() {
                        None
                    } else {
                        Some(Store::open(cfg.db_path())?)
                    };
                    let rep =
                        meowlaw_search::diagnose::profile_test(&file, &overrides, &r, store.as_ref())?;
                    if as_json {
                        print(&serde_json::to_value(&rep)?);
                    } else {
                        print!("{}", rep.render());
                    }
                }
                ProfileCmd::List => {
                    for id in r.ids() {
                        let s = r.spec(&id).unwrap();
                        println!("{:<10} {}", id, s.name.as_deref().unwrap_or(""));
                    }
                }
                ProfileCmd::Show { id } => {
                    let p = r.get(&id)?;
                    println!("{}", toml::to_string_pretty(&p.spec)?);
                }
            }
            Ok(())
        }
        Cmd::Embed => embed(&cfg),
        Cmd::Models { cmd } => {
            use meowlaw_search::vector::models;
            match cmd {
                ModelCmd::List => {
                    for p in models::PRESETS {
                        println!("{:<40} {:<11} {:<22} {}", p.name, p.licence, p.languages, p.note);
                    }
                }
                ModelCmd::Pull { name } => {
                    let p = models::preset(&name).with_context(|| {
                        format!("unknown model `{name}`; see `meowlaw models list`")
                    })?;
                    let dir = cfg.resolve(&cfg.storage.data_dir).join("models");
                    let target = models::pull(&dir, p, |file, bytes| {
                        if bytes == 0 {
                            eprintln!("{file}: already present");
                        } else {
                            eprintln!("{file}: {:.1} MB", bytes as f64 / 1e6);
                        }
                    })?;
                    println!(
                        "Add to meowlaw.toml, then run `meowlaw embed`:\n\n[embeddings]\nprovider = \"local\"\nmodel = \"{}\"\nmodel_dir = \"{}\"\npooling = \"{}\"\nquery_prefix = \"{}\"\npassage_prefix = \"{}\"\n\nModel licence: {} ({}).",
                        p.name,
                        target.display(),
                        p.pooling,
                        p.query_prefix,
                        p.passage_prefix,
                        p.licence,
                        p.repo
                    );
                }
            }
            Ok(())
        }
        Cmd::Similarity => {
            let em = cfg
                .embeddings
                .clone()
                .context("add an [embeddings] section to meowlaw.toml")?;
            let store = Store::open(cfg.db_path())?;
            let t = std::time::Instant::now();
            let r = meowlaw_search::vector::build_similarity(&store, &em.model, cfg.similarity)?;
            eprintln!(
                "similarity edges built in {:.1}s",
                t.elapsed().as_secs_f32()
            );
            print(&serde_json::to_value(r)?);
            Ok(())
        }
        Cmd::Serve { bind } => {
            let mut cfg = cfg;
            if let Some(b) = bind {
                cfg.server.bind = b;
            }
            let e = Arc::new(engine(&cfg)?);
            if e.bm.num_docs() == 0 && e.store.stats()?.nodes > 0 {
                tracing::warn!("search index is empty: running `rebuild` before serving");
                meowlaw_search::rebuild(&e)?;
            }
            let token = cfg.token();
            let mcp = McpServer::new(e.clone(), cfg.server.mcp_name.clone());
            let state = AppState {
                engine: e,
                mcp,
                config: Arc::new(cfg),
                token,
            };
            tokio::runtime::Runtime::new()?.block_on(meowlaw_server::serve(state))
        }
        Cmd::Mcp => {
            let e = Arc::new(engine(&cfg)?);
            McpServer::new(e, cfg.server.mcp_name.clone()).run_stdio()
        }
    }
}

/// Embed nodes that lack vectors, then rebuild `similar` edges.
/// Run `meowlaw summarise`; rebuild extraction when summaries changed.
fn summarise(cfg: &Config, e: &Engine, corpus: Option<&str>, dry_run: bool) -> Result<bool> {
    let sc = cfg
        .summaries
        .as_ref()
        .context("add a [summaries] section to meowlaw.toml")?;
    let r = meowlaw_search::summarise::summarise(
        &e.store,
        sc,
        meowlaw_search::summarise::SummariseOptions { corpus, dry_run },
    )?;
    for (id, err) in &r.errors {
        eprintln!("summary for {id} failed: {err}");
    }
    print(&serde_json::to_value(&r)?);
    if r.changed() {
        meowlaw_search::rebuild(e)?;
    }
    Ok(r.changed())
}

fn embed(cfg: &Config) -> Result<()> {
    let embedder = cfg
        .embedder()?
        .context("add an [embeddings] section to meowlaw.toml")?;
    let batch = cfg.embeddings.as_ref().map(|e| e.batch).unwrap_or(16);
    let store = Store::open(cfg.db_path())?;
    // Citation stripping for summaries uses the profiles via an engine
    // without its own embedder (the model is already loaded above).
    let stripper = Engine::open_with_language(
        store.clone(),
        &cfg.index_dir(),
        registry(cfg)?,
        &cfg.search.language,
    )?;
    let strip = |c: &meowlaw_core::Corpus, t: &str| {
        stripper
            .strip_citations(c, t)
            .unwrap_or_else(|_| t.to_string())
    };
    let t = std::time::Instant::now();
    let (nodes, chunks) = meowlaw_search::vector::embed_all(
        &store,
        embedder.as_ref(),
        batch * 4,
        &strip,
        |done, total| {
            eprint!("\rembedded {done}/{total} nodes");
        },
    )?;
    eprintln!(
        "\nembedded {nodes} nodes ({chunks} chunks) with {} in {:.1}s",
        embedder.model(),
        t.elapsed().as_secs_f32()
    );
    let t = std::time::Instant::now();
    let r =
        meowlaw_search::vector::build_similarity(&store, embedder.model(), cfg.similarity)?;
    eprintln!(
        "similarity edges built in {:.1}s",
        t.elapsed().as_secs_f32()
    );
    print(&serde_json::to_value(r)?);
    Ok(())
        }

fn init(path: &Path) -> Result<()> {
    if path.exists() {
        bail!("{} already exists", path.display());
    }
    std::fs::write(path, include_str!("../../../meowlaw.example.toml"))?;
    std::fs::create_dir_all("profiles")?;
    std::fs::create_dir_all("data")?;
    eprintln!(
        "wrote {}; next: meowlaw ingest <path> --kind legislation --profile generic",
        path.display()
    );
    Ok(())
}
