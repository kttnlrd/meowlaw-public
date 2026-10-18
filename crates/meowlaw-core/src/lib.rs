//! MeowLaw core: the data model, ID scheme and jurisdiction profiles shared by
//! every other crate.

pub mod ids;
pub mod model;
pub mod profile;
pub mod versions;

pub use model::*;
pub use profile::{Link, Profile, ProfileRegistry, ProfileSpec, TargetSpec};

/// Everything an ingester produces for one source.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct Document {
    pub corpus: Corpus,
    pub nodes: Vec<Node>,
}
