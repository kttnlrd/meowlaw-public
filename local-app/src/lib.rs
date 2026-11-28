//! lawkitty-local: the read-only LawKitty data engine, and a single-user server on top of it.
//!
//! The library reads a data folder on disk. Nothing but the source catalogue is read at
//! startup: a tree is parsed the first time it is asked for, a section file when it is opened,
//! the search index when it is searched.
//!
//! With the `server` feature (on by default) it also serves the built React app at `/app/` and
//! the browse API it calls; that is the `lawkitty-local` binary. The native desktop app in
//! `desktop/` uses the library alone, without the feature, so it carries no web server.

pub mod catalog;
pub mod pyjson;
pub mod search;
pub mod section;
#[cfg(feature = "server")]
mod server;
pub mod state;
pub mod tree;

#[cfg(feature = "server")]
pub use server::router;

use std::path::PathBuf;

/// `{name, version, changelog}` exactly as backend/routes/api.py serves it. Regenerate with
/// tests/gen_golden.py when the Python changelog changes.
pub const INFO_JSON: &str = include_str!("info.json");

pub struct Config {
    pub data: PathBuf,
    pub dist: PathBuf,
    pub state: PathBuf,
    pub search_db: Option<PathBuf>,
}

pub struct App {
    pub dist: PathBuf,
    pub catalog: catalog::Catalog,
    pub trees: tree::Trees,
    pub sections: section::Sections,
    pub search: search::Search,
    pub local: state::LocalState,
}

impl App {
    pub fn new(cfg: Config) -> App {
        App {
            catalog: catalog::Catalog::build(&cfg.data),
            trees: tree::Trees::new(cfg.data.clone()),
            sections: section::Sections::new(cfg.data.clone()),
            search: search::Search::locate(&cfg.data, cfg.search_db),
            local: state::LocalState::new(cfg.state),
            dist: cfg.dist,
        }
    }
}

