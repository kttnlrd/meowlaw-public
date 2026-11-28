import React, { useState } from 'react'
import { useTheme, FONTS } from '../ThemeContext'
import { COLORS } from './common/types'
import { api } from '../api'

const ACCENT_PRESETS = [
  '#c6f432', '#2563eb', '#7c3aed', '#059669',
  '#d97706', '#dc2626', '#e11d48', '#0891b2',
]

const SETUP_GUIDE_URL = 'https://lawkitty.app/setup.html'

/** Settings cards — rendered inside AccountPanel (Account + Settings are one panel). */
export default function SettingsPanel() {
  const {
    theme, themes, isCustomizable, accentColor, headingFont, bodyFont,
    userPrefs, setTheme, setAccentColor,
    setHeadingFont, setBodyFont,
    resetTheme, savePrefs,
  } = useTheme()

  const handleThemeToggle = (t: string) => {
    setTheme(t)
    savePrefs({ theme: t } as any)
  }

  const handleAccent = (color: string) => {
    setAccentColor(color)
    savePrefs({ accent_color: color } as any)
  }

  const handleHeadingFont = (f: string) => {
    setHeadingFont(f)
    savePrefs({ heading_font: f } as any)
  }

  const handleBodyFont = (f: string) => {
    setBodyFont(f)
    savePrefs({ body_font: f } as any)
  }

  const handleReset = () => {
    resetTheme()
    fetch('/api/user/prefs/reset', { method: 'POST' }).catch(() => {})
  }

  return (
    <>
      {/* MCP connector */}
      <MCPTabContent />

      {/* Appearance */}
      <section className="lk-settings__card" aria-labelledby="lk-set-appearance">
        <div className="lk-settings__card-head">
          <div className="lk-settings__card-intro">
            <h2 id="lk-set-appearance" className="lk-settings__h2">Appearance</h2>
            <p className="lk-settings__desc">Make the reader yours.</p>
          </div>
          <div className="lk-settings__theme-select">
            <label htmlFor="lk-set-theme" className="lk-settings__label">Theme</label>
            <select
              id="lk-set-theme"
              className="lk-settings__select"
              value={theme}
              onChange={e => handleThemeToggle(e.target.value)}
            >
              {themes.map(t => (
                <option key={t.id} value={t.id}>{t.label}</option>
              ))}
            </select>
          </div>
        </div>

        {isCustomizable && (
          <div className="lk-settings__grid lk-settings__grid--3">
            <div className="lk-settings__field">
              <span className="lk-settings__label" id="lk-set-accent">Accent</span>
              <label className="lk-settings__swatch-btn">
                <span className="lk-settings__swatch" style={{ background: accentColor }} aria-hidden="true" />
                {accentColor}
                <input
                  type="color"
                  aria-labelledby="lk-set-accent"
                  value={accentColor}
                  onChange={e => handleAccent(e.target.value)}
                />
              </label>
              <div className="lk-settings__presets" role="group" aria-label="Accent presets">
                {ACCENT_PRESETS.map(color => (
                  <button
                    key={color}
                    type="button"
                    className="lk-settings__preset"
                    onClick={() => handleAccent(color)}
                    title={color}
                    aria-label={`Accent ${color}`}
                    aria-pressed={accentColor === color}
                    style={{ background: color }}
                  />
                ))}
              </div>
            </div>
          </div>
        )}

        {isCustomizable && (
          <div className="lk-settings__grid">
            <div className="lk-settings__field">
              <label htmlFor="lk-set-fh" className="lk-settings__label">Heading font</label>
              <select
                id="lk-set-fh"
                className="lk-settings__select"
                value={headingFont}
                onChange={e => handleHeadingFont(e.target.value)}
              >
                {FONTS.heading.map(f => (
                  <option key={f} value={f} style={{ fontFamily: f }}>{f}</option>
                ))}
              </select>
            </div>
            <div className="lk-settings__field">
              <label htmlFor="lk-set-fb" className="lk-settings__label">Body font</label>
              <select
                id="lk-set-fb"
                className="lk-settings__select"
                value={bodyFont}
                onChange={e => handleBodyFont(e.target.value)}
              >
                {FONTS.body.map(f => (
                  <option key={f} value={f} style={{ fontFamily: f }}>{f}</option>
                ))}
              </select>
            </div>
          </div>
        )}

        <div className="lk-settings__foot">
          <p
            className="lk-settings__preview"
            style={{ fontFamily: `'${bodyFont}', ${bodyFont === 'serif' ? 'serif' : 'sans-serif'}` }}
          >
            Preview: CGT event A1 happens if you dispose of a CGT asset (<span className="lk-settings__preview-cite">s 104-10 ITAA 1997</span>).
          </p>
          <button type="button" className="lk-settings__btn" onClick={handleReset}>
            Reset to defaults
          </button>
        </div>
      </section>
    </>
  )
}

type TokenInfo = {
  id: number;
  name: string;
  created_by: string;
  created_at: number;
  last_used: number | null;
  request_count: number;
};

function MCPTabContent() {
  const MCP_LINK = `${window.location.origin}/mcp`;
  const [linkCopied, setLinkCopied] = useState(false);
  const [generatedToken, setGeneratedToken] = useState<string | null>(null);
  const [tokens, setTokens] = useState<TokenInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState('');

  const baseUrl = `${window.location.origin}/mcp`;
  const fullUrl = generatedToken ? `${baseUrl}/${generatedToken}` : baseUrl;
  const claudeDesktopJson = generatedToken
    ? JSON.stringify({ mcpServers: { lawkitty: { type: 'http', url: fullUrl } } }, null, 2)
    : '';
  const claudeCodeCmd = generatedToken ? `claude mcp add --transport http lawkitty ${fullUrl}` : '';

  React.useEffect(() => {
    loadTokens();
    setGeneratedToken(null);
    setError(null);
  }, []);

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

  const copyText = (key: string, text: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(null), 2000);
    }).catch(() => {});
  };

  const formatDate = (ts: number | null) => {
    if (!ts) return 'Never';
    return new Date(ts * 1000).toLocaleString();
  };

  return (
    <section className="lk-settings__card" aria-labelledby="lk-set-mcp">
      <div className="lk-settings__card-head">
        <div className="lk-settings__card-intro">
          <div className="lk-settings__h2-row">
            <h2 id="lk-set-mcp" className="lk-settings__h2">MCP connector</h2>
          </div>
          <p className="lk-settings__desc">Connect lawkitty to Claude, ChatGPT, Claude Code or any MCP client.</p>
        </div>
      </div>

      {/* Connect with a link: the AI app opens lawkitty to sign in (Google, Microsoft or a code) */}
      <div className="lk-settings__field">
        <span className="lk-settings__label">Connect with a link</span>
        <div className="lk-settings__row">
          <code style={{ flex: 1, minWidth: 0, padding: '10px 12px', borderRadius: 8, border: '1px solid var(--color-border)', background: 'var(--color-bg)', fontSize: 14, wordBreak: 'break-all' }}>{MCP_LINK}</code>
          <button type="button" className="lk-btn-primary" style={{ minHeight: 44, padding: '0 18px' }}
            onClick={() => { navigator.clipboard?.writeText(MCP_LINK).catch(() => {}); setLinkCopied(true); setTimeout(() => setLinkCopied(false), 1500) }}>
            {linkCopied ? 'Copied!' : 'Copy link'}
          </button>
        </div>
        <p className="lk-settings__desc" style={{ fontSize: 14, marginTop: 8 }}>
          In Claude: Settings → Connectors → <strong>Add custom connector</strong>, paste the link, then <strong>Connect</strong>. lawkitty opens to sign you in with Google, Microsoft or a login code; click <strong>Allow</strong>. ChatGPT and Claude Code work the same way. No token to copy or keep safe.
        </p>
      </div>

      <div className="lk-settings__field" style={{ marginTop: 8 }}>
        <span className="lk-settings__label">Advanced: connect with a token</span>
        <p className="lk-settings__desc" style={{ fontSize: 14 }}>For clients that cannot sign in through a browser. Anyone holding the token can use your access.</p>
        <button
          type="button"
          className="lk-settings__btn"
          style={{ alignSelf: 'flex-start', marginTop: 8 }}
          onClick={generateToken}
          disabled={loading}
        >
          {loading ? 'Fetching a token...' : 'Generate token'}
        </button>
      </div>

      {error && (
        <div className="lk-settings__error" role="alert">{error}</div>
      )}

      {/* Newly generated token */}
      {generatedToken && (
        <div className="lk-settings__fresh">
          <div className="lk-settings__fresh-title">Copy your token now. It won't be shown again.</div>
          <CopyBlock
            label="Token"
            text={generatedToken}
            copied={copiedKey === 'token'}
            onCopy={() => copyText('token', generatedToken)}
          />
        </div>
      )}

      {/* Per-client copy snippets — only after a token is generated */}
      {generatedToken && (
        <div className="lk-settings__field">
          <span className="lk-settings__label">Copy for your AI client</span>
          <CopyBlock
            label="Claude Desktop (config JSON)"
            text={claudeDesktopJson}
            copied={copiedKey === 'claude-desktop'}
            onCopy={() => copyText('claude-desktop', claudeDesktopJson)}
          />
          <CopyBlock
            label="Claude Code"
            text={claudeCodeCmd}
            copied={copiedKey === 'claude-code'}
            onCopy={() => copyText('claude-code', claudeCodeCmd)}
          />
          <CopyBlock
            label="ChatGPT (connector URL)"
            text={fullUrl}
            copied={copiedKey === 'chatgpt'}
            onCopy={() => copyText('chatgpt', fullUrl)}
          />
          <span className="lk-settings__hint">Leave OAuth optional items blank.</span>
        </div>
      )}

      {/* Token list */}
      {tokens.length > 0 && (
        <div className="lk-settings__field">
          <span className="lk-settings__label">Your tokens ({tokens.length})</span>
          <div className="lk-settings__tokens">
            {tokens.map(t => (
              <div key={t.id} className="lk-settings__token">
                <div className="lk-settings__token-row">
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {renamingId === t.id ? (
                      <div className="lk-settings__row" style={{ alignItems: 'center' }}>
                        <input
                          className="lk-settings__input"
                          style={{ minHeight: 34, fontSize: 14 }}
                          aria-label="Token name"
                          value={renameValue}
                          onChange={e => setRenameValue(e.target.value)}
                          onKeyDown={e => { if (e.key === 'Enter') submitRename(); if (e.key === 'Escape') cancelRename(); }}
                          autoFocus
                        />
                        <button type="button" className="lk-settings__btn lk-settings__btn--sm lk-settings__btn--ok" onClick={submitRename}>Save</button>
                        <button type="button" className="lk-settings__btn lk-settings__btn--sm" onClick={cancelRename}>Cancel</button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        className="lk-settings__token-name"
                        onClick={() => startRename(t)}
                        title="Click to rename"
                      >
                        {t.name || `Token #${t.id}`}
                        <svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>
                        </svg>
                      </button>
                    )}
                  </div>
                  <button
                    type="button"
                    className="lk-settings__btn lk-settings__btn--sm lk-settings__btn--danger"
                    onClick={() => revokeToken(t.id)}
                    title="Revoke token"
                  >
                    Revoke
                  </button>
                </div>
                <div className="lk-settings__token-meta">
                  <span><strong>{t.request_count}</strong> calls</span>
                  <span>Created: {formatDate(t.created_at)}</span>
                  <span>Last used: {formatDate(t.last_used)}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {tokens.length === 0 && !generatedToken && (
        <div className="lk-settings__empty">
          <img src="/app/lawkitty-cat-head.png" alt="" />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <span className="lk-settings__empty-title">No tokens yet. The cat checked twice.</span>
            <span className="lk-settings__empty-sub">Generate one above to get started.</span>
          </div>
        </div>
      )}

      <div className="lk-settings__field">
        <p className="lk-settings__desc" style={{ fontSize: 14 }}>
          Need more detail? The full setup guide walks through each client.
        </p>
        <a className="lk-settings__btn" href={SETUP_GUIDE_URL} target="_blank" rel="noopener noreferrer" style={{ alignSelf: 'flex-start', textDecoration: 'none', marginTop: 8 }}>
          Connect to AI · full setup guide ↗
        </a>
      </div>
    </section>
  );
}

function CopyBlock({ label, text, copied, onCopy }: { label: string; text: string; copied: boolean; onCopy: () => void }) {
  return (
    <div style={{ border: `1px solid ${COLORS?.border || '#333'}`, borderRadius: 8, overflow: 'hidden', marginTop: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 12px', background: COLORS?.bg || '#0e0e1e', borderBottom: `1px solid ${COLORS?.border || '#333'}` }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: COLORS?.textMuted || '#888', fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>{label}</span>
        <button
          type="button"
          onClick={onCopy}
          style={{
            background: copied ? (COLORS?.accent || '#c6f432') : 'transparent',
            color: copied ? 'var(--on-catnip)' : (COLORS?.textMuted || '#888'),
            border: `1px solid ${COLORS?.border || '#333'}`, borderRadius: 6,
            padding: '3px 12px', fontSize: 11, fontWeight: 600, cursor: 'pointer',
            fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
          }}
        >
          {copied ? 'Copied!' : 'Copy'}
        </button>
      </div>
      <pre style={{ margin: 0, padding: 12, fontSize: 12, lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-all', fontFamily: 'monospace', color: COLORS?.heading || '#fff', overflowX: 'auto', background: COLORS?.surface || '#1a1a2e' }}>{text}</pre>
    </div>
  )
}
