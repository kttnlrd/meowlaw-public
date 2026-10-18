//! JSON-RPC conformance of the transport-independent handler.

use super::*;
use meowlaw_core::ProfileRegistry;
use meowlaw_search::Engine;
use meowlaw_store::Store;

fn server() -> McpServer {
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = N.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("meowlaw-mcp-test-{}-{n}", std::process::id()));
    let store = Store::open(dir.join("db.sqlite")).unwrap();
    let engine = Engine::open(
        store,
        &dir.join("index"),
        ProfileRegistry::builtin().unwrap(),
    )
    .unwrap();
    McpServer::new(Arc::new(engine), "meowlaw")
}

#[test]
fn jsonrpc_rules() {
    let s = server();
    let code = |v: Option<Value>| v.and_then(|v| v["error"]["code"].as_i64());
    assert_eq!(code(s.handle_body(&json!([]))), Some(-32600));
    assert_eq!(code(s.handle_body(&json!("hello"))), Some(-32600));
    let batch = s.handle_body(&json!([1, 2])).unwrap();
    assert_eq!(batch.as_array().unwrap().len(), 2);
    assert_eq!(
        code(s.handle(&json!({"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"}))),
        Some(-32600)
    );
    assert_eq!(
        code(s.handle(&json!({"id": 1, "method": "ping"}))),
        Some(-32600)
    );
    assert_eq!(
        code(s.handle(&json!({"jsonrpc": "2.0", "id": 1, "method": "nope"}))),
        Some(-32601)
    );
    // Client responses and notifications get no reply.
    assert!(
        s.handle(&json!({"jsonrpc": "2.0", "id": 1, "result": {}}))
            .is_none()
    );
    assert!(
        s.handle(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            .is_none()
    );
    let r = s.handle(&json!({"jsonrpc": "2.0", "id": "a", "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})).unwrap();
    assert_eq!(r["result"]["protocolVersion"], "2025-06-18");
    let r = s.handle(&json!({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "1997"}}})).unwrap();
    assert_eq!(r["result"]["isError"], false, "{r}");
}
