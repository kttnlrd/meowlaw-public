import React, { useState, useEffect } from 'react';
import { COLORS } from './common/types';
import { api } from '../api';

interface MCPModalProps {
  open: boolean;
  onClose: () => void;
}

type TokenInfo = {
  id: number;
  name: string;
  created_by: string;
  created_at: number;
  last_used: number | null;
  request_count: number;
};

const MCPModal: React.FC<MCPModalProps> = ({ open, onClose }) => {
  const [generatedToken, setGeneratedToken] = useState<string | null>(null);
  const [tokens, setTokens] = useState<TokenInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const [copiedToken, setCopiedToken] = useState(false);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [copiedSnippet, setCopiedSnippet] = useState<string | null>(null);

  const baseUrl = `${window.location.origin}/mcp`;
  const fullUrl = generatedToken ? `${baseUrl}/${generatedToken}` : baseUrl;

  const claudeDesktopJson = generatedToken
    ? `{\n  "mcpServers": {\n    "lawkitty": {\n      "type": "http",\n      "url": "${fullUrl}"\n    }\n  }\n}`
    : '';
  const claudeCodeCmd = generatedToken ? `claude mcp add --transport http lawkitty ${fullUrl}` : '';

  useEffect(() => {
    if (open) {
      loadTokens();
      setGeneratedToken(null);
      setError(null);
    }
  }, [open]);

  const loadTokens = async () => {
    try {
      const data = await api.listMcpTokens();
      setTokens(data.tokens || []);
    } catch (e: any) {
      setError(e.message);
    }
  };

  const generateToken = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.generateMcpToken();
      setGeneratedToken(data.token);
      loadTokens();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const revokeToken = async (tokenId: number) => {
    setError(null);
    try {
      await api.revokeMcpToken(String(tokenId));
      loadTokens();
    } catch (e: any) {
      setError(e.message);
    }
  };

  const startRename = (token: TokenInfo) => {
    setRenamingId(token.id);
    setRenameValue(token.name || '');
  };

  const submitRename = async () => {
    if (renamingId === null) return;
    setError(null);
    try {
      await api.renameMcpToken(renamingId, renameValue.trim() || 'Untitled');
      setRenamingId(null);
      loadTokens();
    } catch (e: any) {
      setError(e.message);
    }
  };

  const cancelRename = () => {
    setRenamingId(null);
    setRenameValue('');
  };

  const copyToClipboard = (text: string, type: 'url' | 'token') => {
    navigator.clipboard.writeText(text).then(() => {
      if (type === 'url') {
        setCopiedUrl(true);
        setTimeout(() => setCopiedUrl(false), 2000);
      } else {
        setCopiedToken(true);
        setTimeout(() => setCopiedToken(false), 2000);
      }
    }).catch(err => {
      console.error('Failed to copy: ', err);
    });
  };

  const copySnippet = (key: string, text: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopiedSnippet(key);
      setTimeout(() => setCopiedSnippet(null), 2000);
    }).catch(() => {});
  };

  const formatDate = (ts: number | null) => {
    if (!ts) return 'Never';
    return new Date(ts * 1000).toLocaleString();
  };

  if (!open) return null;

  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
        zIndex: 200, display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: COLORS.surface, border: `1px solid ${COLORS.border}`,
          borderRadius: 8, padding: 24, maxWidth: 640, width: '90%',
          overflowY: 'auto', maxHeight: '90vh', position: 'relative',
        }}
        onClick={e => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          style={{
            position: 'absolute', top: 12, right: 12,
            background: 'transparent', border: 'none',
            color: COLORS.textMuted, fontSize: 24, cursor: 'pointer',
            lineHeight: 1,
          }}
        >
          &times;
        </button>

        <h2 style={{ color: COLORS.heading, marginTop: 0, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", fontSize: 18 }}>
          MCP Server
        </h2>

        <p style={{ color: COLORS.textMuted, fontSize: 13, lineHeight: 1.5 }}>
          Connect Claude Desktop or other MCP clients to this lawkitty instance.
        </p>

        {/* Generate Token */}
        <div style={{ marginTop: 20 }}>
          <button
            onClick={generateToken}
            disabled={loading}
            style={{
              padding: '10px 18px', borderRadius: 6,
              background: COLORS.accent, color: 'var(--on-catnip)',
              border: 'none', fontSize: 14, cursor: loading ? 'not-allowed' : 'pointer',
              fontWeight: 600, fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
              opacity: loading ? 0.7 : 1,
            }}
          >
            {loading ? 'Generating...' : 'Generate New Token'}
          </button>
        </div>

        {error && (
          <div style={{ marginTop: 12, padding: 10, borderRadius: 6, background: 'rgba(239,68,68,0.1)', color: '#ef4444', fontSize: 13 }}>
            {error}
          </div>
        )}

        {/* Newly generated token */}
        {generatedToken && (
          <div style={{
            marginTop: 20, padding: 16, borderRadius: 6,
            background: 'rgba(39,158,136,0.08)', border: `1px solid ${COLORS.accent}`,
          }}>
            <div style={{ color: COLORS.accent, fontSize: 12, fontWeight: 600, marginBottom: 8, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
              Copy this token now. It will not be shown again
            </div>
            <div style={{ position: 'relative' }}>
              <pre style={{
                background: COLORS.bg, color: COLORS.text,
                padding: 12, borderRadius: 6, fontSize: 12,
                overflowX: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                fontFamily: 'monospace', margin: 0,
                border: `1px solid ${COLORS.border}`,
              }}>
                {generatedToken}
              </pre>
              <button
                onClick={() => copyToClipboard(generatedToken, 'token')}
                style={{
                  position: 'absolute', top: 8, right: 8,
                  padding: '4px 10px', borderRadius: 4,
                  background: copiedToken ? COLORS.accent : COLORS.surface,
                  color: copiedToken ? 'var(--on-catnip)' : COLORS.text,
                  border: `1px solid ${COLORS.border}`, fontSize: 11, cursor: 'pointer',
                  fontWeight: 600, fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                }}
              >
                {copiedToken ? 'Copied!' : 'Copy'}
              </button>
            </div>
          </div>
        )}

        {/* Per-connection copy snippets */}
        {generatedToken && (
          <div style={{ marginTop: 24 }}>
            <div style={{ color: COLORS.heading, fontSize: 13, fontWeight: 600, marginBottom: 8, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
              Copy for your AI client
            </div>
            {[
              { key: 'claude-desktop', label: 'Claude Desktop (config JSON)', text: claudeDesktopJson },
              { key: 'claude-code', label: 'Claude Code', text: claudeCodeCmd },
              { key: 'chatgpt', label: 'ChatGPT (connector URL)', text: fullUrl },
            ].map(s => (
              <div key={s.key} style={{ position: 'relative', marginBottom: 12 }}>
                <div style={{ color: COLORS.textMuted, fontSize: 12, fontWeight: 600, marginBottom: 4, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>{s.label}</div>
                <pre style={{
                  background: COLORS.bg, color: COLORS.text, padding: 12, paddingRight: 84,
                  borderRadius: 6, fontSize: 12, overflowX: 'auto', whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all', fontFamily: 'monospace', margin: 0,
                  border: `1px solid ${COLORS.border}`, minHeight: 36,
                }}>
                  {s.text}
                </pre>
                <button
                  onClick={() => copySnippet(s.key, s.text)}
                  style={{
                    position: 'absolute', top: 26, right: 8, padding: '4px 10px', borderRadius: 4,
                    background: copiedSnippet === s.key ? COLORS.accent : COLORS.surface,
                    color: copiedSnippet === s.key ? 'var(--on-catnip)' : COLORS.text,
                    border: `1px solid ${COLORS.border}`, fontSize: 11, cursor: 'pointer', fontWeight: 600,
                    fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                  }}
                >
                  {copiedSnippet === s.key ? 'Copied!' : 'Copy'}
                </button>
              </div>
            ))}
          </div>
        )}

        {/* MCP Endpoint URL */}
        <div style={{ marginTop: 24 }}>
          <div style={{ color: COLORS.heading, fontSize: 13, fontWeight: 600, marginBottom: 8, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
            MCP Endpoint URL
          </div>
          <div style={{ position: 'relative' }}>
            <pre style={{
              background: COLORS.bg, color: COLORS.text,
              padding: 14, borderRadius: 6, fontSize: 12,
              overflowX: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
              fontFamily: 'monospace', margin: 0,
              border: `1px solid ${COLORS.border}`,
              minHeight: 40,
              display: 'flex', alignItems: 'center',
            }}>
              {fullUrl}
            </pre>
            <button
              onClick={() => copyToClipboard(fullUrl, 'url')}
              style={{
                position: 'absolute', top: 8, right: 8,
                padding: '6px 12px', borderRadius: 4,
                background: copiedUrl ? COLORS.accent : COLORS.surface,
                color: copiedUrl ? 'var(--on-catnip)' : COLORS.text,
                border: `1px solid ${COLORS.border}`, fontSize: 12, cursor: 'pointer',
                fontWeight: 600, fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                transition: 'background 0.2s ease, color 0.2s ease',
              }}
            >
              {copiedUrl ? 'Copied!' : 'Copy URL'}
            </button>
          </div>
        </div>

        {/* Token list */}
        {tokens.length > 0 && (
          <div style={{ marginTop: 24 }}>
            <div style={{ color: COLORS.heading, fontSize: 13, fontWeight: 600, marginBottom: 12, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
              Your Tokens ({tokens.length})
            </div>
            <div style={{
              maxHeight: 320, overflowY: 'auto',
              border: `1px solid ${COLORS.border}`, borderRadius: 6,
            }}>
              {tokens.map(t => (
                <div key={t.id} style={{
                  padding: '12px 14px',
                  borderBottom: `1px solid ${COLORS.border}`,
                  fontSize: 12, color: COLORS.text,
                  fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      {renamingId === t.id ? (
                        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                          <input
                            value={renameValue}
                            onChange={e => setRenameValue(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter') submitRename(); if (e.key === 'Escape') cancelRename(); }}
                            autoFocus
                            style={{
                              flex: 1, padding: '4px 8px', borderRadius: 4, fontSize: 12,
                              background: COLORS.bg, color: COLORS.heading,
                              border: `1px solid ${COLORS.accent}`, outline: 'none',
                              fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                            }}
                          />
                          <button onClick={submitRename} style={{ padding: '4px 8px', borderRadius: 4, background: COLORS.accent, color: 'var(--on-catnip)', border: 'none', cursor: 'pointer', fontSize: 10, fontWeight: 600 }}>Save</button>
                          <button onClick={cancelRename} style={{ padding: '4px 8px', borderRadius: 4, background: COLORS.bg, color: COLORS.textMuted, border: `1px solid ${COLORS.border}`, cursor: 'pointer', fontSize: 10 }}>Cancel</button>
                        </div>
                      ) : (
                        <div
                          onClick={() => startRename(t)}
                          style={{ color: COLORS.heading, fontWeight: 600, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4 }}
                          title="Click to rename"
                        >
                          {t.name || `Token #${t.id}`}
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={COLORS.textMuted} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>
                          </svg>
                        </div>
                      )}
                    </div>
                    <button
                      onClick={() => revokeToken(t.id)}
                      title="Revoke token"
                      style={{
                        padding: '4px 8px', borderRadius: 4,
                        background: 'rgba(239,68,68,0.1)', color: '#ef4444',
                        border: 'none', cursor: 'pointer',
                        fontSize: 10, fontWeight: 600, fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                        whiteSpace: 'nowrap', flexShrink: 0,
                      }}
                    >
                      Revoke
                    </button>
                  </div>
                  <div style={{ display: 'flex', gap: 12, marginTop: 6, color: COLORS.textMuted, fontSize: 11 }}>
                    <span><strong style={{ color: COLORS.accent }}>{t.request_count}</strong> calls</span>
                    <span>Created: {formatDate(t.created_at)}</span>
                    <span>Last used: {formatDate(t.last_used)}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {tokens.length === 0 && !generatedToken && (
          <div style={{ marginTop: 20, color: COLORS.textMuted, fontSize: 12, fontStyle: 'italic' }}>
            No tokens yet. Generate one above to get started.
          </div>
        )}

      <p style={{ color: COLORS.textMuted, fontSize: 12, margin: '20px 0 0 0', lineHeight: 1.4 }}>
        In Claude Desktop, go to <strong>Settings → Developer → Connectors</strong>, click <strong>Add Custom Connector</strong>:
      </p>
      <div style={{
        background: COLORS.bg, color: COLORS.text,
        padding: 12, borderRadius: 6, fontSize: 11,
        fontFamily: 'monospace', margin: '8px 0 0 0',
        border: `1px solid ${COLORS.border}`,
        lineHeight: 1.5,
      }}>
        <div><strong>Name:</strong> lawkitty</div>
        <div><strong>URL:</strong> <code style={{wordBreak: 'break-all'}}>{fullUrl}</code></div>
        <div style={{marginTop: 4, color: COLORS.textMuted}}>Leave OAuth optional items blank.</div>
      </div>
      </div>
    </div>
  );
};

export default MCPModal;
