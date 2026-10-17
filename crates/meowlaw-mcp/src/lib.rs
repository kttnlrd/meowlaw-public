//! Model Context Protocol server.
//!
//! Transport-independent JSON-RPC handler ([`McpServer::handle`]) plus a stdio
//! loop. The HTTP transport (Streamable HTTP, JSON responses) lives in
//! `meowlaw-server` and calls the same handler.

pub mod access;
pub mod tools;

use meowlaw_search::Engine;
use serde_json::{Value, json};
use std::io::{BufRead, Write};
use std::sync::Arc;

/// Protocol revisions this server speaks, newest first.
pub const PROTOCOL_VERSIONS: &[&str] = &["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];

#[cfg(test)]
mod tests;

#[derive(Clone)]
pub struct McpServer {
    pub engine: Arc<Engine>,
    pub name: String,
    pub instructions: String,
}

impl McpServer {
    pub fn new(engine: Arc<Engine>, name: impl Into<String>) -> Self {
        Self {
            engine,
            name: name.into(),
            instructions: DEFAULT_INSTRUCTIONS.to_string(),
        }
    }

    /// Handle one JSON-RPC message. Returns `None` for notifications and for
    /// responses sent by the client (we never send requests, so we ignore them).
    pub fn handle(&self, msg: &Value) -> Option<Value> {
        self.handle_scoped(msg, &access::Scope::all())
    }

    /// As [`handle`](Self::handle), reading only what `scope` allows.
    pub fn handle_scoped(&self, msg: &Value, scope: &access::Scope) -> Option<Value> {
        let err = |id: Value, code: i64, message: &str| {
            Some(
                json!({ "jsonrpc": "2.0", "id": id, "error": { "code": code, "message": message } }),
            )
        };
        let Some(obj) = msg.as_object() else {
            return err(Value::Null, -32600, "Invalid request: not an object");
        };
        let id = obj.get("id").cloned();
        if let Some(id) = &id
            && !(id.is_string() || id.is_number() || id.is_null())
        {
            return err(
                Value::Null,
                -32600,
                "Invalid request: id must be a string or number",
            );
        }
        if obj.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
            return err(
                id.unwrap_or(Value::Null),
                -32600,
                "Invalid request: jsonrpc must be \"2.0\"",
            );
        }
        let Some(method) = obj.get("method").and_then(Value::as_str) else {
            // A response from the client (result or error, no method).
            if obj.contains_key("result") || obj.contains_key("error") {
                return None;
            }
            return err(
                id.unwrap_or(Value::Null),
                -32600,
                "Invalid request: no method",
            );
        };
        let params = obj.get("params").cloned().unwrap_or(Value::Null);
        // Notifications (no id) never get a response.
        let id = id?;
        let result = match method {
            "initialize" => Ok(self.initialize(&params)),
            "ping" => Ok(json!({})),
            "tools/list" => Ok(json!({ "tools": tools::definitions() })),
            "tools/call" => Ok(self.call_tool(&params, scope)),
            "resources/list" => Ok(json!({ "resources": [] })),
            "resources/templates/list" => Ok(json!({ "resourceTemplates": [] })),
            "prompts/list" => Ok(json!({ "prompts": [] })),
            m => Err((-32601, format!("Method not found: {m}"))),
        };
        Some(match result {
            Ok(r) => json!({ "jsonrpc": "2.0", "id": id, "result": r }),
            Err((code, message)) => {
                json!({ "jsonrpc": "2.0", "id": id, "error": { "code": code, "message": message } })
            }
        })
    }

    /// Handle a body that may be a single message or a batch (batches exist in
    /// protocol revisions before 2025-06-18; accepted for older clients).
    pub fn handle_body(&self, body: &Value) -> Option<Value> {
        self.handle_body_scoped(body, &access::Scope::all())
    }

    pub fn handle_body_scoped(&self, body: &Value, scope: &access::Scope) -> Option<Value> {
        match body {
            Value::Array(items) if items.is_empty() => Some(json!({
                "jsonrpc": "2.0", "id": null,
                "error": { "code": -32600, "message": "Invalid request: empty batch" }
            })),
            Value::Array(items) => {
                let out: Vec<Value> = items.iter().filter_map(|m| self.handle_scoped(m, scope)).collect();
                (!out.is_empty()).then_some(Value::Array(out))
            }
            m => self.handle_scoped(m, scope),
        }
    }

    fn initialize(&self, params: &Value) -> Value {
        let requested = params
            .get("protocolVersion")
            .and_then(Value::as_str)
            .unwrap_or(PROTOCOL_VERSIONS[0]);
        let version = if PROTOCOL_VERSIONS.contains(&requested) {
            requested
        } else {
            PROTOCOL_VERSIONS[0]
        };
        json!({
            "protocolVersion": version,
            "capabilities": { "tools": { "listChanged": false } },
            "serverInfo": { "name": self.name, "version": env!("CARGO_PKG_VERSION") },
            "instructions": self.instructions,
        })
    }

    fn call_tool(&self, params: &Value, scope: &access::Scope) -> Value {
        let name = params.get("name").and_then(Value::as_str).unwrap_or("");
        let args = params.get("arguments").cloned().unwrap_or(json!({}));
        match access::call_scoped(&self.engine, name, &args, scope) {
            Ok(v) => json!({
                "content": [{ "type": "text", "text": serde_json::to_string_pretty(&v).unwrap_or_default() }],
                "structuredContent": v,
                "isError": false,
            }),
            Err(e) => json!({
                "content": [{ "type": "text", "text": format!("Error: {e:#}") }],
                "isError": true,
            }),
        }
    }

    /// Newline-delimited JSON-RPC over stdin/stdout. Logs must go to stderr.
    pub fn run_stdio(&self) -> anyhow::Result<()> {
        let stdin = std::io::stdin();
        let mut stdout = std::io::stdout().lock();
        for line in stdin.lock().lines() {
            let line = line?;
            if line.trim().is_empty() {
                continue;
            }
            let reply = match serde_json::from_str::<Value>(&line) {
                Ok(v) => self.handle_body(&v),
                Err(e) => Some(json!({
                    "jsonrpc": "2.0", "id": null,
                    "error": { "code": -32700, "message": format!("Parse error: {e}") }
                })),
            };
            if let Some(r) = reply {
                writeln!(stdout, "{}", serde_json::to_string(&r)?)?;
                stdout.flush()?;
            }
        }
        Ok(())
    }
}

pub const DEFAULT_INSTRUCTIONS: &str = "MeowLaw serves primary and secondary legal sources from any jurisdiction. \
Call get_info first to see which corpora are loaded. Workflow: search (or resolve for a citation) to find node IDs, \
fetch to read the text, related / graph_neighbourhood to follow citations and defined terms, get_definition for terms. \
IDs look like `<corpus>:<abbrev><number>` (e.g. `example-act-2020:s6`, `gdpr:art5`), `<case-corpus>:para<n>` for judgment \
paragraphs and `def:<corpus>:<term>` for definitions. Always quote the text you rely on and cite the node ID; \
check the corpus `as_at` date and licence returned by fetch.";
