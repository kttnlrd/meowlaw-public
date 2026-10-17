//! `meowlaw.toml`: one file for storage, server, branding and embeddings.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct Config {
    pub storage: Storage,
    pub server: Server,
    pub branding: Branding,
    /// How sources are grouped in the UI drawer, in display order.
    #[serde(default = "default_source_groups")]
    pub source_groups: Vec<SourceGroup>,
    pub embeddings: Option<Embeddings>,
    pub search: Search,
    /// Thresholds for `similar` edges built from embeddings.
    pub similarity: meowlaw_search::vector::SimilarityConfig,
    /// Per-corpus access control. Absent: every caller reads everything.
    pub access: Option<Access>,
    /// Optional machine-generated summaries via an OpenAI-compatible chat endpoint.
    pub summaries: Option<meowlaw_search::summarise::SummaryConfig>,
    /// Directory the config was loaded from; relative paths resolve against it.
    #[serde(skip)]
    pub base: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Storage {
    /// Holds `meowlaw.db` and `index/`.
    pub data_dir: PathBuf,
    /// Extra profiles (`*.toml`), overriding built-ins with the same id.
    pub profiles_dir: Option<PathBuf>,
}

impl Default for Storage {
    fn default() -> Self {
        Self {
            data_dir: "data".into(),
            profiles_dir: Some("profiles".into()),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Server {
    pub bind: String,
    /// Environment variable holding the bearer token. If the variable is unset
    /// the server is open: bind to localhost only in that case.
    pub token_env: String,
    /// Require the token for the REST API as well as `/mcp`.
    pub protect_api: bool,
    /// Origins allowed to call `/mcp` from a browser (DNS-rebinding protection).
    /// Requests without an Origin header (CLI and desktop MCP clients) are allowed.
    pub allowed_origins: Vec<String>,
    /// Static UI directory.
    pub ui_dir: PathBuf,
    /// Name reported to MCP clients.
    pub mcp_name: String,
    /// Enable the ingest page (`/admin.html`) and its write endpoints. With a
    /// token configured they need the token; without one, only a loopback
    /// bind may enable them.
    pub allow_ingest: bool,
    /// Largest accepted upload, in megabytes.
    pub max_upload_mb: usize,
}

impl Default for Server {
    fn default() -> Self {
        Self {
            bind: "127.0.0.1:8080".into(),
            token_env: "MEOWLAW_TOKEN".into(),
            protect_api: false,
            allowed_origins: vec![
                "http://localhost:8080".into(),
                "http://127.0.0.1:8080".into(),
            ],
            ui_dir: "ui".into(),
            mcp_name: "meowlaw".into(),
            allow_ingest: false,
            max_upload_mb: 200,
        }
    }
}

/// Everything a deployment changes to rebrand the UI. See docs/branding.md.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Branding {
    pub name: String,
    pub tagline: String,
    /// Image file (SVG/PNG) shown in the header. Relative to the config file.
    pub logo: Option<PathBuf>,
    pub favicon: Option<PathBuf>,
    /// Footer text, e.g. a disclaimer or firm name.
    pub footer: String,
    /// Placeholder in the search box. Use citation forms your users know.
    pub search_hint: String,
    /// UI language: loads `ui/i18n/<locale>.json` (falls back to English).
    pub locale: String,
    /// CSS custom properties for light mode, without the `--` prefix.
    pub colors: BTreeMap<String, String>,
    /// Overrides for dark mode.
    pub dark: BTreeMap<String, String>,
    pub font_body: String,
    pub font_heading: String,
    pub font_mono: String,
}

impl Default for Branding {
    fn default() -> Self {
        let colors = [
            ("bg", "#fbfaf7"),
            ("surface", "#ffffff"),
            ("surface-2", "#f3f1ec"),
            ("text", "#1d1b18"),
            ("muted", "#6b665e"),
            ("border", "#e4e0d8"),
            ("primary", "#5a3fd6"),
            ("primary-contrast", "#ffffff"),
            ("accent", "#e0832b"),
            ("highlight", "#fff1b8"),
            ("link", "#4630b8"),
        ];
        let dark = [
            ("bg", "#141311"),
            ("surface", "#1c1b18"),
            ("surface-2", "#25231f"),
            ("text", "#ece9e2"),
            ("muted", "#a39d92"),
            ("border", "#34312b"),
            ("primary", "#9d8bff"),
            ("primary-contrast", "#141311"),
            ("accent", "#f0a259"),
            ("highlight", "#5a4a12"),
            ("link", "#b7a9ff"),
        ];
        Self {
            name: "MeowLaw".into(),
            tagline: "Open legal research for any jurisdiction".into(),
            logo: None,
            favicon: None,
            footer: "Not legal advice. Check every source against the official version.".into(),
            search_hint: "Search, or paste a citation".into(),
            locale: "en".into(),
            colors: colors
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            dark: dark
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            font_body: "system-ui, -apple-system, 'Segoe UI', sans-serif".into(),
            font_heading: "Georgia, 'Times New Roman', serif".into(),
            font_mono: "ui-monospace, 'SFMono-Regular', Menlo, monospace".into(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Search {
    /// Stemmer for keyword search: english, french, german, spanish, ... or
    /// `none`. Changing it rebuilds the search index on the next start.
    pub language: String,
}

impl Default for Search {
    fn default() -> Self {
        Self {
            language: "english".into(),
        }
    }
}

/// A group of sources in the drawer. A corpus joins the first group whose
/// `kinds` include its kind, unless its metadata names a group explicitly
/// (`group = "..."` in front matter, a sidecar or corpus.toml `[meta]`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceGroup {
    pub label: String,
    /// Corpus kinds: legislation, instrument, rule_set, case, treaty, guidance, secondary.
    #[serde(default)]
    pub kinds: Vec<String>,
}

pub fn default_source_groups() -> Vec<SourceGroup> {
    let g = |label: &str, kinds: &[&str]| SourceGroup {
        label: label.into(),
        kinds: kinds.iter().map(|k| k.to_string()).collect(),
    };
    vec![
        g("Legislation", &["legislation", "instrument"]),
        g("Case law", &["case"]),
        g(
            "Regulatory resources",
            &["guidance", "treaty", "rule_set", "secondary"],
        ),
    ]
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Embeddings {
    /// `local` (ONNX model run in-process) or `http` (OpenAI-compatible endpoint).
    #[serde(default = "default_provider")]
    pub provider: String,
    /// Name stored with each vector. Change it when you change models.
    pub model: String,
    /// local: directory holding `model.onnx` and `tokenizer.json`.
    pub model_dir: Option<PathBuf>,
    /// local: token window per chunk.
    #[serde(default = "default_seq")]
    pub seq: usize,
    /// local: `cls` or `mean`. Set by `models pull`; must match the model.
    #[serde(default = "default_pooling")]
    pub pooling: String,
    /// local: prepended to queries / indexed text (E5 models need "query: " / "passage: ").
    #[serde(default)]
    pub query_prefix: String,
    #[serde(default)]
    pub passage_prefix: String,
    /// http: e.g. http://localhost:11434/v1/embeddings
    pub url: Option<String>,
    /// http: environment variable holding the API key, if needed.
    pub api_key_env: Option<String>,
    #[serde(default = "default_batch")]
    pub batch: usize,
}

fn default_provider() -> String {
    "local".into()
}

fn default_pooling() -> String {
    "cls".into()
}

fn default_seq() -> usize {
    256
}

fn default_batch() -> usize {
    16
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let mut c: Config = if path.exists() {
            let src = std::fs::read_to_string(path)?;
            toml::from_str(&src).with_context(|| format!("parsing {}", path.display()))?
        } else {
            Config::default()
        };
        // Defaults for colour keys a partial config left out.
        let def = Branding::default();
        for (k, v) in def.colors {
            c.branding.colors.entry(k).or_insert(v);
        }
        for (k, v) in def.dark {
            c.branding.dark.entry(k).or_insert(v);
        }
        c.base = path
            .parent()
            .map(Path::to_path_buf)
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| PathBuf::from("."));
        Ok(c)
    }

    pub fn resolve(&self, p: &Path) -> PathBuf {
        if p.is_absolute() {
            p.to_path_buf()
        } else {
            self.base.join(p)
        }
    }

    pub fn db_path(&self) -> PathBuf {
        self.resolve(&self.storage.data_dir).join("meowlaw.db")
    }

    pub fn index_dir(&self) -> PathBuf {
        self.resolve(&self.storage.data_dir).join("index")
    }

    pub fn profiles_dir(&self) -> Option<PathBuf> {
        self.storage.profiles_dir.as_ref().map(|p| self.resolve(p))
    }

    /// Build the configured embedder, if any.
    pub fn embedder(&self) -> Result<Option<Box<dyn meowlaw_search::Embedder>>> {
        let Some(em) = &self.embeddings else {
            return Ok(None);
        };
        match em.provider.as_str() {
            "http" => {
                let url = em
                    .url
                    .clone()
                    .context("[embeddings] provider = \"http\" needs `url`")?;
                let key = em.api_key_env.as_ref().and_then(|v| std::env::var(v).ok());
                Ok(Some(Box::new(meowlaw_search::vector::HttpEmbedder::new(
                    url, &em.model, key,
                ))))
            }
            #[cfg(feature = "local-embed")]
            "local" => {
                let dir = em
                    .model_dir
                    .as_ref()
                    .map(|d| self.resolve(d))
                    .context("[embeddings] provider = \"local\" needs `model_dir`")?;
                Ok(Some(Box::new(
                    meowlaw_search::vector::local::LocalEmbedder::load(
                        &dir,
                        &em.model,
                        meowlaw_search::vector::local::LocalOptions {
                            batch: em.batch,
                            seq: em.seq,
                            pooling: meowlaw_search::vector::local::Pooling::parse(&em.pooling)?,
                            query_prefix: em.query_prefix.clone(),
                            passage_prefix: em.passage_prefix.clone(),
                        },
                    )?,
                )))
            }
            other => anyhow::bail!("unknown embeddings provider `{other}`"),
        }
    }

    pub fn token(&self) -> Option<String> {
        std::env::var(&self.server.token_env)
            .ok()
            .filter(|t| !t.is_empty())
    }

    /// `:root` custom properties for the configured brand.
    pub fn theme_css(&self) -> String {
        let b = &self.branding;
        let vars = |m: &BTreeMap<String, String>| -> String {
            m.iter()
                .filter(|(k, v)| safe_css_ident(k) && safe_css_value(v))
                .map(|(k, v)| format!("  --{k}: {v};\n"))
                .collect()
        };
        let fonts = format!(
            "  --font-body: {};\n  --font-heading: {};\n  --font-mono: {};\n",
            css_or(&b.font_body, "sans-serif"),
            css_or(&b.font_heading, "serif"),
            css_or(&b.font_mono, "monospace"),
        );
        let dark = vars(&b.dark);
        format!(
            "/* Generated from meowlaw.toml [branding]. */\n:root {{\n{}{fonts}  color-scheme: light dark;\n}}\n\
@media (prefers-color-scheme: dark) {{\n:root:not([data-theme=\"light\"]) {{\n{dark}}}\n}}\n\
:root[data-theme=\"dark\"] {{\n{dark}}}\n",
            vars(&b.colors),
        )
    }
}

fn css_or<'a>(v: &'a str, fallback: &'a str) -> &'a str {
    if safe_css_value(v) { v } else { fallback }
}

fn safe_css_ident(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
}

/// Reject values that could break out of the declaration.
fn safe_css_value(s: &str) -> bool {
    !s.is_empty() && !s.contains([';', '{', '}', '<', '>']) && !s.contains("url(")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn theme_css_escapes() {
        let mut c = Config::default();
        c.branding.colors.insert("primary".into(), "#123456".into());
        c.branding
            .colors
            .insert("evil".into(), "red; } body { display:none".into());
        let css = c.theme_css();
        assert!(css.contains("--primary: #123456;"));
        assert!(!css.contains("evil"));
        assert!(css.contains("prefers-color-scheme: dark"));
    }
}

/// Per-corpus access control. Each corpus has one access tag: its `access`
/// metadata, else the first matching `corpora` pattern, else `default`.
/// Callers read corpora whose tag they hold: `anonymous` without a token, a
/// token's `tags` with it. The main token (`server.token_env`) reads all.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Access {
    pub default: String,
    pub anonymous: Vec<String>,
    /// Corpus id pattern (`*` wildcards) → tag, e.g. `"handbook-*" = "internal"`.
    pub corpora: BTreeMap<String, String>,
    pub tokens: Vec<AccessToken>,
}

impl Default for Access {
    fn default() -> Self {
        Self {
            default: "public".into(),
            anonymous: vec!["public".into()],
            corpora: BTreeMap::new(),
            tokens: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccessToken {
    pub name: String,
    /// Environment variable holding this token.
    pub env: String,
    pub tags: Vec<String>,
}

impl Access {
    /// Access tag of a corpus.
    pub fn tag_of(&self, corpus: &meowlaw_core::Corpus) -> String {
        if let Some(t) = corpus.meta.get("access").and_then(|v| v.as_str()) {
            return t.to_string();
        }
        self.corpora
            .iter()
            .find(|(pat, _)| glob(pat, &corpus.id))
            .map(|(_, t)| t.clone())
            .unwrap_or_else(|| self.default.clone())
    }

    /// Tags granted by a bearer token: `Some(tags)` for a known token.
    pub fn tags_for_token(&self, given: &str) -> Option<Vec<String>> {
        self.tokens.iter().find_map(|t| {
            let want = std::env::var(&t.env).ok().filter(|v| !v.is_empty())?;
            crate::constant_time_eq(given.as_bytes(), want.as_bytes()).then(|| t.tags.clone())
        })
    }
}

/// `*` matches any run of characters; everything else is literal.
pub fn glob(pat: &str, s: &str) -> bool {
    let parts: Vec<&str> = pat.split('*').collect();
    if parts.len() == 1 {
        return pat == s;
    }
    let (first, last) = (parts[0], parts[parts.len() - 1]);
    if !s.starts_with(first)
        || !s[first.len()..].ends_with(last)
        || s.len() < first.len() + last.len()
    {
        return false;
    }
    let mut rest = &s[first.len()..s.len() - last.len()];
    for p in &parts[1..parts.len() - 1] {
        match rest.find(p) {
            Some(i) => rest = &rest[i + p.len()..],
            None => return false,
        }
    }
    true
}

#[cfg(test)]
mod access_tests {
    use super::glob;

    #[test]
    fn globs() {
        assert!(glob("handbook-*", "handbook-2026"));
        assert!(glob("*-internal", "leave-internal"));
        assert!(glob("a*b*c", "axxbyyc"));
        assert!(!glob("handbook-*", "itaa-1997"));
        assert!(glob("itaa-1997", "itaa-1997"));
        assert!(!glob("ab*ba", "aba"));
    }
}
