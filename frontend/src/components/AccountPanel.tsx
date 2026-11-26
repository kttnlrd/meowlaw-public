import React, { useEffect, useState } from 'react'
import SettingsPanel from './SettingsPanel'

interface Account {
  email: string
  name: string
  provider: string
  tier: string
  plan: { key: string; name: string; price: string; mcp_daily: number | null }
  billing_enabled: boolean
}

const PROVIDER_LABEL: Record<string, string> = { google: 'Google', microsoft: 'Microsoft', magic: 'Email link' }

async function postJson(url: string, body?: unknown) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  return r.json()
}

export default function AccountPanel({ onClose }: { onClose: () => void }) {
  const [account, setAccount] = useState<Account | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'signed-out' | 'error'>('loading')
  const [name, setName] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)

  useEffect(() => {
    fetch('/api/account')
      .then(r => {
        if (r.status === 401) { setStatus('signed-out'); return null }
        if (!r.ok) throw new Error(String(r.status))
        return r.json()
      })
      .then((a: Account | null) => {
        if (!a) return
        setAccount(a)
        setName(a.name)
        setStatus('ready')
      })
      .catch(() => setStatus('error'))
  }, [])

  const saveName = async () => {
    setSaving(true)
    const r = await fetch('/api/account', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
    if (r.ok) {
      setAccount(await r.json())
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    }
    setSaving(false)
  }

  const deleteAccount = async () => {
    setDeleting(true)
    await postJson('/api/account/delete').catch(() => {})
    window.location.href = '/'
  }

  return (
    <div className="lk-settings-overlay" onClick={onClose}>
      <div
        className="lk-settings"
        role="dialog"
        aria-modal="true"
        aria-labelledby="lk-account-title"
        onClick={e => e.stopPropagation()}
      >
        <div className="lk-settings__head">
          <h1 id="lk-account-title" className="lk-settings__title">Account &amp; settings</h1>
          <button type="button" className="lk-settings__close" onClick={onClose} aria-label="Close account">
            <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {status === 'loading' && <p className="lk-settings__desc">Loading…</p>}

        {status === 'error' && <p className="lk-settings__error">Couldn't load your account. Try again shortly.</p>}

        {status === 'signed-out' && (
          <section className="lk-settings__card" aria-label="Sign in">
            <div className="lk-settings__empty">
              <p className="lk-settings__empty-title">You're not signed in</p>
              <p className="lk-settings__empty-sub">Sign in to access the library and your MCP connector.</p>
            </div>
          </section>
        )}

        {status === 'ready' && account && (
          <>
            {/* Profile */}
            <section className="lk-settings__card" aria-labelledby="lk-acc-profile">
              <div className="lk-settings__card-head">
                <div className="lk-settings__card-intro">
                  <h2 id="lk-acc-profile" className="lk-settings__h2">Profile</h2>
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 16 }}>
                <div
                  aria-hidden="true"
                  style={{
                    width: 48, height: 48, borderRadius: '50%', flexShrink: 0,
                    background: 'var(--catnip, #c6f432)', color: 'var(--on-catnip, #120d17)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 20, fontWeight: 700, fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                  }}
                >
                  {account.email.charAt(0).toUpperCase()}
                </div>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis' }}>{account.email}</div>
                  <div className="lk-settings__hint">Signed in with {PROVIDER_LABEL[account.provider] || account.provider || 'unknown'}</div>
                </div>
              </div>
              <div className="lk-settings__field">
                <label htmlFor="lk-acc-name" className="lk-settings__label">Name</label>
                <div className="lk-settings__row">
                  <input
                    id="lk-acc-name"
                    className="lk-settings__input"
                    value={name}
                    onChange={e => setName(e.target.value)}
                    placeholder="Your name"
                    maxLength={200}
                    onKeyDown={e => { if (e.key === 'Enter') saveName() }}
                  />
                  <button
                    type="button"
                    className={`lk-settings__btn${saved ? ' lk-settings__btn--ok' : ''}`}
                    style={{ minHeight: 44 }}
                    onClick={saveName}
                    disabled={saving}
                  >
                    {saved ? 'Saved!' : saving ? 'Saving...' : 'Save'}
                  </button>
                </div>
              </div>
            </section>

            {/* Plan, billing and danger zone live below the settings panel. */}
          </>
        )}

        <SettingsPanel />

        {status === 'ready' && account && (
          <>
            {/* Danger zone — kept at the very bottom */}
            <section className="lk-settings__card" aria-labelledby="lk-acc-danger">
              <div className="lk-settings__card-head">
                <div className="lk-settings__card-intro">
                  <h2 id="lk-acc-danger" className="lk-settings__h2">Danger zone</h2>
                  <p className="lk-settings__desc">Deleting your account removes your profile, preferences and MCP tokens. This can't be undone.</p>
                </div>
              </div>
              <div className="lk-settings__row" style={{ gap: 8 }}>
                {confirmDelete ? (
                  <>
                    <button type="button" className="lk-settings__btn lk-settings__btn--danger" onClick={deleteAccount} disabled={deleting}>
                      {deleting ? 'Deleting…' : 'Yes, delete my account'}
                    </button>
                    <button type="button" className="lk-settings__btn" onClick={() => setConfirmDelete(false)} disabled={deleting}>
                      Cancel
                    </button>
                  </>
                ) : (
                  <button type="button" className="lk-settings__btn lk-settings__btn--danger" onClick={() => setConfirmDelete(true)}>
                    Delete account
                  </button>
                )}
              </div>
            </section>

            <div className="lk-settings__row">
              <button type="button" className="lk-settings__btn" onClick={() => { window.location.href = '/auth/logout' }}>
                Sign out
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
