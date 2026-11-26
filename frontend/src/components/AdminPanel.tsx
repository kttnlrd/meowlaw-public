import React, { useEffect, useRef, useState } from 'react'
import UsageDashboard from './UsageDashboard'

interface AdminUser {
  id: number
  email: string
  name: string | null
  provider: string | null
  tier: string
  last_login_at: string | null
  created_at: string | null
}

interface Stats {
  total_users: number
  by_tier: Record<string, number>
  active_7d: number
  total_mcp_tokens: number
  total_mcp_calls: number
  mcp_calls_by_user: { email: string; calls: number; tokens: number }[]
}

const TIERS = [
  { key: 'trial', name: 'Kitten' },
  { key: 'solo', name: 'House cat' },
  { key: 'power', name: 'Maine Coon' },
]

interface ContentType {
  key: string
  label: string
  auth: 'user' | 'admin'
  enabled: boolean
  available: boolean
}

/** Fire a JSON request; resolves to '' on success or an error message (never throws). */
const send = async (method: string, url: string, body?: unknown): Promise<string> => {
  try {
    const r = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    return r.ok ? '' : (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`
  } catch (e) {
    return String(e)
  }
}

const grantsUrl = (email: string) => `/api/admin/users/${encodeURIComponent(email)}/content-types`

function ContentTypesAdmin({ users }: { users: AdminUser[] }) {
  const [types, setTypes] = useState<ContentType[]>([])
  const [typesError, setTypesError] = useState('')
  const [email, setEmail] = useState('')
  const emailRef = useRef(email)
  emailRef.current = email
  // null = not loaded (or load failed): edits are blocked so a failed load can't wipe real grants
  const [granted, setGranted] = useState<Set<string> | null>(null)
  const [grantsError, setGrantsError] = useState(false)
  const [busy, setBusy] = useState(false)

  const loadTypes = () =>
    fetch('/api/admin/content-types')
      .then(async r => {
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`)
        return r.json()
      })
      .then(d => { setTypes(d); setTypesError('') })
      .catch(e => { setTypes([]); setTypesError(e instanceof Error ? e.message : String(e)) })
  useEffect(() => { loadTypes() }, [])

  useEffect(() => {
    let live = true // drop responses for a user we've since switched away from
    setGranted(null)
    setGrantsError(false)
    if (email) {
      fetch(grantsUrl(email))
        .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
        .then(d => { if (live) setGranted(new Set(d.granted)) })
        .catch(() => { if (live) setGrantsError(true) })
    }
    return () => { live = false }
  }, [email])

  const toggleGlobal = async (t: ContentType) => {
    if (busy) return
    setBusy(true)
    try {
      const err = await send('PUT', `/api/admin/content-types/${encodeURIComponent(t.key)}`, { enabled: !t.enabled })
      if (err) alert(`Failed: ${err}`)
    } finally {
      setBusy(false)
      loadTypes()
    }
  }

  const setAll = async (on: boolean) => {
    if (!on && !confirm('Switch off ALL content types for ALL users, admins included?')) return
    setBusy(true)
    try {
      const errs = (await Promise.all(types.filter(t => t.enabled !== on).map(t =>
        send('PUT', `/api/admin/content-types/${encodeURIComponent(t.key)}`, { enabled: on })))).filter(Boolean)
      if (errs.length) alert(`${errs.length} change(s) failed: ${errs[0]}`)
    } finally {
      setBusy(false)
      loadTypes()
    }
  }

  // Per-key POST/DELETE: no full-set replace, so overlapping or stale clicks can't revoke other grants.
  const toggleGrant = async (key: string) => {
    if (!granted || busy) return
    const forEmail = email
    const on = !granted.has(key)
    setBusy(true)
    try {
      const err = await send(on ? 'POST' : 'DELETE', `${grantsUrl(forEmail)}/${encodeURIComponent(key)}`)
      if (err) alert(`Failed: ${err}`)
      else if (forEmail === emailRef.current) {
        setGranted(prev => {
          if (!prev) return prev
          const next = new Set(prev)
          if (on) next.add(key)
          else next.delete(key)
          return next
        })
      }
    } finally {
      setBusy(false)
    }
  }

  const pubTypes = types.filter(t => t.auth === 'user')
  const privTypes = types.filter(t => t.auth === 'admin')
  const allEnabled = types.length > 0 && types.every(t => t.enabled)
  const allDisabled = types.length > 0 && types.every(t => !t.enabled)

  const typeTable = (rows: ContentType[], showAccess = true) => (
    <div className="lk-admin__table-wrap" style={{ marginBottom: 16 }}>
      <table className="lk-admin__table">
        <thead>
          <tr><th>Type</th><th>Key</th>{showAccess && <th>Default access</th>}<th>Enabled</th></tr>
        </thead>
        <tbody>
          {rows.map(t => (
            <tr key={t.key}>
              <td style={{ fontWeight: 600 }}>{t.label}</td>
              <td><code>{t.key}</code></td>
              {showAccess && (
                <td style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>{t.auth === 'admin' ? 'Admin + granted' : 'All users'}</td>
              )}
              <td>
                <button
                  type="button"
                  className="lk-toggle"
                  aria-pressed={t.enabled}
                  onClick={() => toggleGlobal(t)}
                  disabled={busy}
                  aria-label={`Toggle ${t.label}`}
                >
                  {t.enabled ? 'On' : 'Off'}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )

  return (
    <>
      {typesError && <p className="lk-admin__note lk-admin__note--error">Couldn't load content types: {typesError}</p>}
      <div className="lk-admin__section">
        <div className="lk-admin__actions">
          <button type="button" className="lk-btn-primary" onClick={() => setAll(true)} disabled={busy || allEnabled}>
            Enable all
          </button>
          <button type="button" className="lk-btn-primary" onClick={() => setAll(false)} disabled={busy || allDisabled}
            style={{ background: 'var(--hiss)', borderColor: 'var(--hiss)' }}>
            Disable all
          </button>
        </div>

        <h2 className="lk-usage__h">Public content</h2>
        {typeTable(pubTypes)}
      </div>

      <div className="lk-admin__section">
        <h2 className="lk-usage__h">Private content</h2>
        <p className="lk-admin__note" style={{ margin: '0 0 10px' }}>
          These types are admin-only by default. Grant access to individual users below.
        </p>
        {typeTable(privTypes, false)}

        <h3 style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-heading)', margin: '20px 0 8px' }}>Per-user content grants</h3>
        <select className="lk-admin__select" value={email} onChange={e => setEmail(e.target.value)} aria-label="User for content grants">
          <option value="">Select a user…</option>
          {users.map(u => <option key={u.id} value={u.email}>{u.email}</option>)}
        </select>
        {email && grantsError && <p className="lk-admin__note lk-admin__note--error">Couldn't load grants for {email}.</p>}
        {email && !grantsError && !granted && <p className="lk-admin__note">Loading grants…</p>}
        {email && granted && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px', margin: '10px 0 0' }}>
            {privTypes.map(t => (
              <button
                key={t.key}
                type="button"
                className="lk-toggle"
                aria-pressed={granted.has(t.key)}
                onClick={() => toggleGrant(t.key)}
                disabled={busy}
                style={{ fontSize: 12 }}
                title={t.enabled ? undefined : 'Globally off: a grant has no effect until it is switched on'}
              >
                {t.label}{t.enabled ? '' : ' (globally off)'}
              </button>
            ))}
          </div>
        )}
      </div>
    </>
  )
}

const fmtDate = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '—')

export default function AdminPanel() {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'denied' | 'error'>('loading')
  const [error, setError] = useState('')
  const [tab, setTab] = useState(0)
  const [selectedUsers, setSelectedUsers] = useState<Set<number>>(new Set())
  const [bulkTier, setBulkTier] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)

  const load = async () => {
    try {
      const [s, u] = await Promise.all([fetch('/api/admin/stats'), fetch('/api/admin/users')])
      if ([s, u].some(r => r.status === 401 || r.status === 403)) return setStatus('denied')
      if (!s.ok || !u.ok) throw new Error(`HTTP ${s.ok ? u.status : s.status}`)
      setStats(await s.json())
      const list: AdminUser[] = (await u.json()).users
      setUsers(list)
      // drop selected ids that no longer exist (e.g. deleted users)
      setSelectedUsers(prev => new Set(list.map(x => x.id).filter(id => prev.has(id))))
      setStatus('ready')
    } catch (e) {
      setError(String(e))
      setStatus('error')
    }
  }

  useEffect(() => { load() }, [])

  const post = async (url: string, body?: unknown) => {
    const err = await send('POST', url, body)
    if (err) alert(`Failed: ${err}`)
    load()
  }

  const setTier = (u: AdminUser, tier: string) => post(`/api/admin/users/${u.id}/tier`, { tier })
  const remove = (u: AdminUser) => {
    if (confirm(`Delete ${u.email}? This revokes their MCP tokens and cannot be undone.`)) {
      post(`/api/admin/users/${u.id}/delete`)
    }
  }

  const toggleUser = (id: number) => {
    setSelectedUsers(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAllUsers = () => {
    if (selectedUsers.size === users.length) setSelectedUsers(new Set())
    else setSelectedUsers(new Set(users.map(u => u.id)))
  }

  // ponytail: N parallel per-user POSTs; add a server-side bulk endpoint if user counts grow.
  const applyBulkTier = async () => {
    if (!bulkTier || bulkBusy) return
    const todo = users.filter(u => selectedUsers.has(u.id) && u.tier !== bulkTier)
    setBulkBusy(true)
    try {
      const errs = await Promise.all(todo.map(u => send('POST', `/api/admin/users/${u.id}/tier`, { tier: bulkTier })))
      const failed = todo.filter((_, i) => errs[i])
      setSelectedUsers(new Set(failed.map(u => u.id))) // keep failures selected for retry
      if (failed.length) alert(`${failed.length} tier change(s) failed: ${errs.find(Boolean)}`)
      else setBulkTier('')
    } finally {
      setBulkBusy(false)
      load() // one reload, after every write has settled
    }
  }

  const cards: [string, number][] = stats ? [
    ['Total users', stats.total_users],
    ...TIERS.map(t => [t.name, stats.by_tier[t.key] ?? 0] as [string, number]),
    ['Active 7 days', stats.active_7d],
    ['MCP tokens', stats.total_mcp_tokens],
    ['MCP calls', stats.total_mcp_calls],
  ] : []

  const TABS = ['Overview', 'Usage', 'Content types', 'Users']

  return (
    <div className="lk-admin">
      <h1 className="lk-admin__title">Admin</h1>

      {status === 'loading' && <p className="lk-admin__note">Loading…</p>}
      {status === 'denied' && <p className="lk-admin__note">Admin only — sign in as the admin account.</p>}
      {status === 'error' && <p className="lk-admin__note lk-admin__note--error">Couldn't load admin data: {error}</p>}

      {status === 'ready' && (
        <>
          <div className="lk-tabs" role="tablist">
            {TABS.map((label, i) => (
              <button
                key={label}
                type="button"
                className="lk-tabs__btn"
                role="tab"
                aria-selected={tab === i}
                onClick={() => setTab(i)}
              >
                {label}
              </button>
            ))}
          </div>

          {tab === 0 && (
            <>
              <div className="lk-admin__stats">
                {cards.map(([label, value]) => (
                  <div key={label} className="lk-admin__stat">
                    <div className="lk-admin__stat-value">{value.toLocaleString()}</div>
                    <div className="lk-admin__stat-label">{label}</div>
                  </div>
                ))}
              </div>

              {stats && stats.mcp_calls_by_user && stats.mcp_calls_by_user.length > 0 && (
                <div style={{ marginBottom: 24 }}>
                  <h2 className="lk-usage__h">MCP calls by user</h2>
                  <div className="lk-admin__table-wrap">
                    <table className="lk-admin__table">
                      <thead>
                        <tr><th>User</th><th>Calls</th><th>Tokens</th></tr>
                      </thead>
                      <tbody>
                        {stats.mcp_calls_by_user.map(r => (
                          <tr key={r.email}>
                            <td>{r.email}</td>
                            <td>{r.calls.toLocaleString()}</td>
                            <td>{r.tokens.toLocaleString()}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}

          {tab === 1 && <UsageDashboard />}

          {tab === 2 && <ContentTypesAdmin users={users} />}

          {tab === 3 && (
            <>
              <h2 className="lk-usage__h">Users</h2>
              {users.length === 0 ? (
                <p className="lk-admin__note">No users yet.</p>
              ) : (
                <>
                  {selectedUsers.size > 0 && (
                    <div className="lk-admin__actions" style={{ marginBottom: 12 }}>
                      <span style={{ font: '600 13px/32px var(--font-sans)', color: 'var(--color-text-muted)' }}>
                        {selectedUsers.size} selected
                      </span>
                      <select className="lk-admin__select" value={bulkTier} onChange={e => setBulkTier(e.target.value)}>
                        <option value="">Change tier to…</option>
                        {TIERS.map(t => <option key={t.key} value={t.key}>{t.name}</option>)}
                      </select>
                      <button type="button" className="lk-btn-primary" onClick={applyBulkTier} disabled={!bulkTier || bulkBusy}>
                        Apply
                      </button>
                    </div>
                  )}
                  <div className="lk-admin__table-wrap">
                    <table className="lk-admin__table">
                      <thead>
                        <tr>
                          <th style={{ width: 40 }}>
                            <input type="checkbox" checked={selectedUsers.size === users.length && users.length > 0}
                              onChange={selectAllUsers} aria-label="Select all" />
                          </th>
                          <th>Email</th><th>Name</th><th>Provider</th><th>Tier</th>
                          <th>Last login</th><th>Created</th><th />
                        </tr>
                      </thead>
                      <tbody>
                        {users.map(u => (
                          <tr key={u.id}>
                            <td>
                              <input type="checkbox" checked={selectedUsers.has(u.id)}
                                onChange={() => toggleUser(u.id)} aria-label={`Select ${u.email}`} />
                            </td>
                            <td>{u.email}</td>
                            <td>{u.name || '—'}</td>
                            <td>{u.provider || '—'}</td>
                            <td>
                              <select
                                className="lk-admin__select"
                                value={u.tier}
                                onChange={e => setTier(u, e.target.value)}
                                aria-label={`Tier for ${u.email}`}
                              >
                                {TIERS.map(t => <option key={t.key} value={t.key}>{t.name}</option>)}
                              </select>
                            </td>
                            <td>{fmtDate(u.last_login_at)}</td>
                            <td>{fmtDate(u.created_at)}</td>
                            <td>
                              <button type="button" className="lk-admin__delete" onClick={() => remove(u)}>Delete</button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}