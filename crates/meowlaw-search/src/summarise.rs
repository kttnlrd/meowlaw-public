//! Optional machine-generated summaries (`meowlaw summarise`).
//!
//! Rulings, cases, guidance and books without a supplied summary get one
//! from the opening text at ingestion. With a `[summaries]` section in
//! `meowlaw.toml`, this replaces those placeholders with a summary written by
//! any OpenAI-compatible chat endpoint (a local model or a hosted one).
//!
//! Every such summary is labelled as machine-generated, in the summary
//! node's `summary_source` (shown in the UI and in MCP `fetch`) and in its
//! `machine_generated` metadata. Supplied summaries are never replaced.
//! Results are cached by a hash of the input, so re-running after a sync
//! only calls the endpoint for documents whose text changed.

use anyhow::{Context, Result, bail};
use meowlaw_core::NodeKind;
use meowlaw_store::Store;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const DEFAULT_PROMPT: &str = "Summarise the following document in plain language in no more than {words} words. Say what kind of document it is, what it is about, and its main conclusions, rules or holdings. Use only what the text says. Do not add facts, opinions or citations that are not in the text.";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct SummaryConfig {
    /// Chat completions endpoint, e.g. http://localhost:11434/v1/chat/completions
    pub url: String,
    pub model: String,
    /// Environment variable holding the API key, if the endpoint needs one.
    pub api_key_env: Option<String>,
    /// Instruction sent with each document. `{words}` is replaced by `words`.
    pub prompt: String,
    pub words: usize,
    /// Longest input sent, in characters; the rest of the document is cut.
    pub max_input_chars: usize,
    /// Run after `meowlaw sync` and ingest-page uploads.
    pub auto: bool,
}

impl Default for SummaryConfig {
    fn default() -> Self {
        Self {
            url: String::new(),
            model: String::new(),
            api_key_env: None,
            prompt: DEFAULT_PROMPT.into(),
            words: 200,
            max_input_chars: 24_000,
            auto: false,
        }
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct SummariseOptions<'a> {
    /// Only this corpus.
    pub corpus: Option<&'a str>,
    /// List what would be summarised, call nothing.
    pub dry_run: bool,
}

#[derive(Debug, Default, Serialize)]
pub struct SummariseReport {
    /// Corpora given a new machine summary.
    pub generated: Vec<String>,
    /// Corpora whose cached summary was still current.
    pub cached: Vec<String>,
    /// Corpora that would be summarised (dry run).
    pub pending: Vec<String>,
    pub errors: Vec<(String, String)>,
}

impl SummariseReport {
    pub fn changed(&self) -> bool {
        !self.generated.is_empty()
    }
}

/// Label stored as the summary's source.
pub fn label(model: &str) -> String {
    format!("machine-generated ({model}), not reviewed")
}

pub fn summarise(
    store: &Store,
    cfg: &SummaryConfig,
    opts: SummariseOptions,
) -> Result<SummariseReport> {
    if cfg.url.is_empty() || cfg.model.is_empty() {
        bail!("set url and model under [summaries] in meowlaw.toml");
    }
    let api_key = cfg
        .api_key_env
        .as_deref()
        .and_then(|v| std::env::var(v).ok())
        .filter(|k| !k.is_empty());
    let agent = crate::vector::http_agent(Some(std::time::Duration::from_secs(300)));
    let prompt = cfg.prompt.replace("{words}", &cfg.words.to_string());
    let mut report = SummariseReport::default();
    for mut doc in store.documents()? {
        if opts.corpus.is_some_and(|c| c != doc.corpus.id) {
            continue;
        }
        let Some(si) = doc.nodes.iter().position(|n| n.kind == NodeKind::Summary) else {
            continue;
        };
        let source = doc.nodes[si]
            .meta
            .get("summary_source")
            .and_then(Value::as_str)
            .unwrap_or("");
        // Only placeholders (opening text) and earlier machine summaries.
        let machine = doc.nodes[si].meta.contains_key("machine_generated");
        if !(source.starts_with("extract:") || machine) {
            continue;
        }
        let input = document_text(&doc, cfg.max_input_chars);
        if input.trim().is_empty() {
            continue;
        }
        let hash: String = Sha256::new()
            .chain_update(cfg.model.as_bytes())
            .chain_update(prompt.as_bytes())
            .chain_update(input.as_bytes())
            .finalize()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let key = format!("machine_summary:{}", doc.corpus.id);
        let cached: Option<Value> = store
            .get_kv(&key)?
            .and_then(|s| serde_json::from_str(&s).ok());
        let id = doc.corpus.id.clone();
        let text = match cached.filter(|c| c["hash"] == hash.as_str()) {
            Some(c) => {
                if machine && doc.nodes[si].text == c["text"].as_str().unwrap_or("") {
                    report.cached.push(id);
                    continue;
                }
                c["text"].as_str().unwrap_or("").to_string()
            }
            None if opts.dry_run => {
                report.pending.push(id);
                continue;
            }
            None => match complete(
                &agent,
                cfg,
                api_key.as_deref(),
                &prompt,
                &doc.corpus.title,
                &input,
            ) {
                Ok(t) => {
                    store.set_kv(
                        &key,
                        &json!({ "hash": hash, "text": t, "model": cfg.model }).to_string(),
                    )?;
                    t
                }
                Err(e) => {
                    report.errors.push((id, format!("{e:#}")));
                    continue;
                }
            },
        };
        if opts.dry_run {
            report.pending.push(id);
            continue;
        }
        let n = &mut doc.nodes[si];
        n.text = text;
        n.meta
            .insert("summary_source".into(), Value::String(label(&cfg.model)));
        n.meta.insert("machine_generated".into(), Value::Bool(true));
        store.put_document(&doc)?;
        report.generated.push(id);
    }
    Ok(report)
}

/// The document's own text, in reading order, without the placeholder summary.
fn document_text(doc: &meowlaw_core::Document, max: usize) -> String {
    let mut t = String::new();
    for n in &doc.nodes {
        if n.kind == NodeKind::Summary {
            continue;
        }
        if let Some(title) = &n.title
            && n.kind != NodeKind::Paragraph
        {
            t.push_str(title);
            t.push_str("\n\n");
        }
        if !n.text.trim().is_empty() {
            t.push_str(n.text.trim());
            t.push_str("\n\n");
        }
        if t.len() >= max {
            break;
        }
    }
    let mut end = t.len().min(max);
    while !t.is_char_boundary(end) {
        end -= 1;
    }
    t.truncate(end);
    t
}

fn complete(
    agent: &ureq::Agent,
    cfg: &SummaryConfig,
    api_key: Option<&str>,
    prompt: &str,
    title: &str,
    input: &str,
) -> Result<String> {
    let body = json!({
        "model": cfg.model,
        "temperature": 0,
        "messages": [
            { "role": "system", "content": prompt },
            { "role": "user", "content": format!("Title: {title}\n\n{input}") },
        ],
    });
    let mut req = agent
        .post(&cfg.url)
        .header("Content-Type", "application/json");
    if let Some(k) = api_key {
        req = req.header("Authorization", &format!("Bearer {k}"));
    }
    let mut resp = req
        .send(serde_json::to_string(&body)?.as_bytes())
        .with_context(|| format!("POST {}", cfg.url))?;
    let reader = resp
        .body_mut()
        .with_config()
        .limit(crate::vector::HTTP_RESPONSE_LIMIT)
        .reader();
    let v: Value = serde_json::from_reader(reader)
        .with_context(|| format!("reading the response from {}", cfg.url))?;
    let text = v["choices"][0]["message"]["content"]
        .as_str()
        .with_context(|| format!("no choices[0].message.content in the response: {v}"))?
        .trim()
        .to_string();
    if text.is_empty() {
        bail!("the endpoint returned an empty summary");
    }
    Ok(text)
}
