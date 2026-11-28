import React, { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

// Usage analytics over /api/admin/usage/*. Every slicer lives in the URL, so a
// drill-down is a link that re-fetches the same rows cold.

type Row = Record<string, any>
interface Facet { value: string; count: number }
type Facets = Record<'user' | 'tool' | 'content_type' | 'act' | 'status', Facet[]>

const MULTI = ['user', 'tool', 'status', 'content_type', 'act'] as const
type Dim = typeof MULTI[number]
const DIM_LABEL: Record<Dim, string> = { user: 'User', tool: 'Tool', status: 'Status', content_type: 'Content type', act: 'Act' }
const STATUS_LABEL: Record<string, string> = { ok: 'OK', empty: 'Empty', error: 'Error', failed_recall: 'Failed recall' }
const SERIES = ['ok', 'empty', 'error', 'legacy'] as const
const PRESETS = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['month', 'This month'], ['all', 'All']] as const
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

const fmtN = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString())
const fmtMs = (n: number | null | undefined) => (n == null ? '—' : `${Math.round(n).toLocaleString()} ms`)
const fmtPct = (r: number | null | undefined) => (r == null ? '—' : `${(r * 100).toFixed(1)}%`)
const fmtTime = (t: number) => new Date(t * 1000).toLocaleString()
const isoLocal = (d: Date) => {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}

/** Preset -> [from, to) as local ISO strings (the API reads bare times in `tz`). */
function presetRange(p: string): [string, string] | null {
  const now = new Date(), d0 = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const tomorrow = new Date(d0.getTime() + 86400000)
  if (p === 'today') return [isoLocal(d0), isoLocal(tomorrow)]
  if (p === '7d') return [isoLocal(new Date(d0.getTime() - 6 * 86400000)), isoLocal(tomorrow)]
  if (p === '30d') return [isoLocal(new Date(d0.getTime() - 29 * 86400000)), isoLocal(tomorrow)]
  if (p === 'month') return [isoLocal(new Date(now.getFullYear(), now.getMonth(), 1)), isoLocal(tomorrow)]
  return null
}

/** The API query string for the current URL state (preset resolved to from/to). */
function apiQuery(sp: URLSearchParams, extra: Record<string, string | number> = {}): string {
  const q = new URLSearchParams()
  const preset = sp.get('range') || '30d'
  const r = preset === 'custom' ? [sp.get('from') || '', sp.get('to') || ''] : presetRange(preset)
  if (r?.[0]) q.set('from', r[0])
  if (r?.[1]) q.set('to', r[1])
  for (const d of MULTI) sp.getAll(d).forEach(v => q.append(d, v))
  for (const k of ['q', 'include_legacy', 'include_protocol']) if (sp.get(k)) q.set(k, sp.get(k)!)
  q.set('tz', TZ)
  Object.entries(extra).forEach(([k, v]) => q.set(k, String(v)))
  return q.toString()
}

async function getJson(url: string) {
  const r = await fetch(url)
  if (r.status === 401 || r.status === 403) throw Object.assign(new Error('denied'), { denied: true })
  if (!r.ok) throw new Error(`${url.split('?')[0]}: HTTP ${r.status} ${(await r.json().catch(() => ({}))).detail || ''}`)
  return r.json()
}

export default function UsageDashboard() {
  const [sp, setSp] = useSearchParams()
  const [summary, setSummary] = useState<Row | null>(null)
  const [facets, setFacets] = useState<Facets | null>(null)
  const [content, setContent] = useState<Row | null>(null)
  const [events, setEvents] = useState<Row | null>(null)
  const [err, setErr] = useState('')
  const [denied, setDenied] = useState(false)
  const [hover, setHover] = useState<{ x: number; y: number; row: Row } | null>(null)

  const qs = apiQuery(sp)
  const sort = sp.get('sort') || 'created_at', dir = sp.get('dir') || 'desc'
  const offset = Number(sp.get('offset') || 0), limit = 50
  const open = sp.get('open')

  /** Update URL state; any filter change resets paging. */
  const patch = (fn: (p: URLSearchParams) => void, keepPage = false) => {
    const p = new URLSearchParams(sp)
    fn(p)
    if (!keepPage) p.delete('offset')
    setSp(p, { replace: false })
  }
  const setOne = (k: string, v: string | null) => patch(p => (v == null || v === '' ? p.delete(k) : p.set(k, v)))
  const toggle = (d: Dim, v: string) => patch(p => {
    const cur = p.getAll(d)
    p.delete(d)
    ;(cur.includes(v) ? cur.filter(x => x !== v) : [...cur, v]).forEach(x => p.append(d, x))
  })
  const only = (d: Dim, v: string) => patch(p => { p.delete(d); p.append(d, v) })

  useEffect(() => {
    let live = true
    setErr('')
    Promise.all([
      getJson(`/api/admin/usage/summary?${qs}`),
      getJson(`/api/admin/usage/facets?${qs}`),
      getJson(`/api/admin/usage/content?${qs}`),
    ]).then(([s, f, c]) => { if (live) { setSummary(s); setFacets(f.facets); setContent(c) } })
      .catch(e => live && (e.denied ? setDenied(true) : setErr(String(e.message || e))))
    return () => { live = false }
  }, [qs])

  useEffect(() => {
    let live = true
    getJson(`/api/admin/usage/events?${apiQuery(sp, { limit, offset, sort, dir })}`)
      .then(e => live && setEvents(e))
      .catch(e => live && (e.denied ? setDenied(true) : setErr(String(e.message || e))))
    return () => { live = false }
  }, [qs, offset, sort, dir])

  const k = summary?.kpis
  const total = k?.total_calls ?? 0

  // Which active filter emptied the result: the first slicer whose own facet
  // (counted without it) still has rows.
  const emptyCause = useMemo(() => {
    if (!facets || total > 0) return ''
    for (const d of MULTI) {
      if (sp.getAll(d).length && facets[d].reduce((n, f) => n + (d === 'status' && f.value === 'failed_recall' ? 0 : f.count), 0) > 0)
        return `the ${DIM_LABEL[d].toLowerCase()} filter (${sp.getAll(d).join(', ')})`
    }
    if (sp.get('q')) return `the search "${sp.get('q')}"`
    return `the date range (${PRESETS.find(p => p[0] === (sp.get('range') || '30d'))?.[1] || 'custom'})`
  }, [facets, total, sp])

  if (denied) return <p className="lk-admin__note">Admin only: sign in as the admin account.</p>

  const range = sp.get('range') || '30d'
  const custom = range === 'custom'
  const kpiTiles: { label: string; value: string; hint?: string; act: () => void; on?: boolean }[] = k ? [
    { label: 'Calls', value: fmtN(k.total_calls), act: () => setOne('status', null), on: !sp.getAll('status').length },
    { label: 'Unique users', value: fmtN(k.unique_users), act: () => document.getElementById('usage-by-user')?.scrollIntoView({ behavior: 'smooth' }) },
    { label: 'Error rate', value: fmtPct(k.error_rate), hint: `${fmtN(k.error)} errors`, act: () => only('status', 'error'), on: sp.getAll('status').join() === 'error' },
    { label: 'Failed recalls', value: fmtN(k.failed_recall), hint: fmtPct(k.failed_recall_rate), act: () => only('status', 'failed_recall'), on: sp.getAll('status').join() === 'failed_recall' },
    { label: 'Zero-result calls', value: fmtN(k.zero_results), act: () => only('status', 'empty'), on: sp.getAll('status').join() === 'empty' },
    { label: 'p95 latency', value: fmtMs(k.latency_p95_ms), hint: `p50 ${fmtMs(k.latency_p50_ms)} · max ${fmtMs(k.latency_max_ms)}`, act: () => patch(p => { p.set('sort', 'latency_ms'); p.set('dir', 'desc') }), on: sort === 'latency_ms' },
  ] : []

  return (
    <section className="lk-usage" aria-label="Usage analytics">
      <h2 className="lk-usage__h">Usage</h2>

      {/* ── Slicer bar ── */}
      <div className="lk-usage__bar">
        <div className="lk-chips" role="group" aria-label="Date range">
          {PRESETS.map(([key, label]) => (
            <button key={key} type="button" className="lk-chip" aria-pressed={range === key}
              onClick={() => patch(p => { p.set('range', key); p.delete('from'); p.delete('to') })}>{label}</button>
          ))}
          <button type="button" className="lk-chip" aria-pressed={custom}
            onClick={() => patch(p => { const r = presetRange(range) || presetRange('30d')!; p.set('range', 'custom'); p.set('from', r[0]); p.set('to', r[1]) })}>Custom</button>
        </div>
        {custom && (
          <span className="lk-usage__dates">
            <label>From <input type="datetime-local" className="lk-admin__select" value={sp.get('from') || ''} onChange={e => setOne('from', e.target.value)} /></label>
            <label>To <input type="datetime-local" className="lk-admin__select" value={sp.get('to') || ''} onChange={e => setOne('to', e.target.value)} /></label>
          </span>
        )}
        {MULTI.map(d => {
          const sel = sp.getAll(d)
          const opts = facets?.[d] || []
          const shown = d === 'status' ? opts.reduce((n, f) => n + (f.value === 'failed_recall' ? 0 : f.count), 0) : opts.reduce((n, f) => n + f.count, 0)
          return (
            <details key={d} className="lk-usage__slicer">
              <summary className="lk-chip" aria-pressed={sel.length > 0}>
                {DIM_LABEL[d]}{sel.length ? `: ${sel.length === 1 ? (STATUS_LABEL[sel[0]] || sel[0]) : `${sel.length} selected`}` : ''}
                <span className="lk-chip__count">{fmtN(shown)}</span>
              </summary>
              <div className="lk-usage__menu" role="group" aria-label={DIM_LABEL[d]}>
                {opts.length === 0 && <p className="lk-admin__note">No values recorded yet.</p>}
                {opts.map(f => (
                  <button key={f.value} type="button" className={`lk-usage__opt${f.count === 0 ? ' lk-usage__opt--zero' : ''}`}
                    aria-pressed={sel.includes(f.value)} onClick={() => toggle(d, f.value)}
                    title={f.value === 'failed_recall' ? summary?.failed_recall_definition : undefined}>
                    <span>{d === 'status' ? STATUS_LABEL[f.value] : f.value}</span><span className="lk-chip__count">{fmtN(f.count)}</span>
                  </button>
                ))}
                {sel.length > 0 && <button type="button" className="lk-usage__link" onClick={() => patch(p => p.delete(d))}>Clear {DIM_LABEL[d].toLowerCase()}</button>}
              </div>
            </details>
          )
        })}
        <input type="search" className="lk-admin__select lk-usage__search" placeholder="Search query or reference"
          aria-label="Search query or content reference" defaultValue={sp.get('q') || ''}
          onKeyDown={e => { if (e.key === 'Enter') setOne('q', (e.target as HTMLInputElement).value.trim()) }}
          onBlur={e => { if (e.target.value.trim() !== (sp.get('q') || '')) setOne('q', e.target.value.trim()) }} />
        <label className="lk-usage__toggle" title={summary?.legacy_note}>
          <input type="checkbox" checked={sp.get('include_legacy') === 'true'} onChange={e => setOne('include_legacy', e.target.checked ? 'true' : null)} />
          Legacy rows{summary ? ` (${fmtN(summary.legacy_rows_in_range)})` : ''}
        </label>
        <label className="lk-usage__toggle" title="MCP protocol requests (initialize, tools/list): they count toward the daily cap but are not tool calls">
          <input type="checkbox" checked={sp.get('include_protocol') === 'true'} onChange={e => setOne('include_protocol', e.target.checked ? 'true' : null)} />
          Protocol requests
        </label>
        <a className="lk-usage__btn" href={`/api/admin/usage/export.csv?${apiQuery(sp, { sort, dir })}`} download>Export CSV</a>
        {sp.toString() && <button type="button" className="lk-usage__link" onClick={() => setSp(new URLSearchParams())}>Reset filters</button>}
      </div>

      {err && <p className="lk-admin__note lk-admin__note--error">Couldn't load usage: {err}</p>}
      {!summary && !err && <p className="lk-admin__note">Loading usage…</p>}

      {summary && total === 0 && (
        <p className="lk-admin__note lk-usage__empty">No calls match: {emptyCause} excludes every row.</p>
      )}

      {summary && (
        <>
          {/* ── KPI tiles ── */}
          <div className="lk-admin__stats">
            {kpiTiles.map(t => (
              <button key={t.label} type="button" className="lk-admin__stat lk-usage__kpi" aria-pressed={!!t.on} onClick={t.act}>
                <div className="lk-admin__stat-value">{t.value}</div>
                <div className="lk-admin__stat-label">{t.label}{t.hint ? ` · ${t.hint}` : ''}</div>
              </button>
            ))}
          </div>
          <p className="lk-facets__note">
            {summary.failed_recall_definition}. {fmtN(k!.unique_tokens)} tokens · {fmtN(k!.new_users)} new users · {(k!.response_bytes / 1e6).toFixed(2)} MB returned ·{' '}
            {summary.includes_legacy ? 'includes' : 'excludes'} {fmtN(summary.legacy_rows_in_range)} legacy rows (no tool recorded). Times in {TZ}.
          </p>

          {/* ── Calls over time ── */}
          {total > 0 && <Chart series={summary.series} grain={summary.grain} hover={hover} setHover={setHover}
            onPick={(bucket, status) => patch(p => {
              const start = new Date(bucket)
              const end = new Date(start.getTime() + (summary.grain === 'hour' ? 3600000 : 86400000))
              p.set('range', 'custom'); p.set('from', isoLocal(start)); p.set('to', isoLocal(end))
              p.delete('status'); if (status !== 'legacy') p.append('status', status)
            })} />}

          {/* ── Breakdowns ── */}
          {total > 0 && (
            <div className="lk-usage__grid">
              <Breakdown id="usage-by-user" title="By user" rows={summary.breakdowns.user} onPick={v => only('user', v)} />
              <Breakdown title="By tool" rows={summary.breakdowns.tool} onPick={v => only('tool', v)} />
              <Breakdown title="By content type" rows={summary.breakdowns.content_type} onPick={v => only('content_type', v)} />
              <Breakdown title={`By hour of day (${summary.breakdowns.hour.filter((r: Row) => !r.calls).length} hours with no calls hidden)`}
                rows={summary.breakdowns.hour.filter((r: Row) => r.calls)} keyLabel={h => `${String(h).padStart(2, '0')}:00`} />
            </div>
          )}

          {/* ── Top content ── */}
          {content && total > 0 && (
            <div className="lk-usage__grid">
              {([['Zero-result queries', 'zero_result_queries'], ['Failed-recall queries', 'failed_recall_queries'],
                ['Search queries', 'queries'], ['Acts', 'acts'], ['Sections', 'sections'], ['Rulings', 'rulings'], ['Cases', 'cases']] as const)
                .map(([title, key]) => (
                  <TopList key={key} title={title} rows={content[key]} onPick={v => key === 'acts' ? only('act', v) : setOne('q', v)} />
                ))}
            </div>
          )}
        </>
      )}

      {/* ── Event table (drill-down) ── */}
      {events && (
        <>
          <h3 className="lk-usage__h3">Events <span className="lk-chip__count">{fmtN(events.total)}</span></h3>
          {events.total === 0 ? <p className="lk-admin__note">No events for this filter.</p> : (
            <div className="lk-admin__table-wrap">
              <table className="lk-admin__table lk-usage__events">
                <thead>
                  <tr>
                    <SortTh label="Time" col="created_at" sort={sort} dir={dir} patch={patch} />
                    <th>User</th><th>Tool</th><th>Content ref</th>
                    <SortTh label="Results" col="result_count" sort={sort} dir={dir} patch={patch} />
                    <SortTh label="Latency" col="latency_ms" sort={sort} dir={dir} patch={patch} />
                    <th>Status</th><th>Error kind</th><th />
                  </tr>
                </thead>
                <tbody>
                  {events.events.map((e: Row) => (
                    <React.Fragment key={e.id}>
                      <tr>
                        <td>{fmtTime(e.created_at)}</td>
                        <td>{e.user_email || '(unknown)'}</td>
                        <td>{e.tool || '(legacy)'}<span className="lk-usage__muted"> {e.transport || ''}</span></td>
                        <td className="lk-usage__ref">{e.content_ref || e.query || '—'}</td>
                        <td>{fmtN(e.result_count)}</td>
                        <td>{fmtMs(e.latency_ms)}</td>
                        <td><StatusTag s={e.status} failed={e.failed_recall} /></td>
                        <td>{e.error_kind || '—'}</td>
                        <td><button type="button" className="lk-usage__link" aria-expanded={open === String(e.id)}
                          onClick={() => patch(p => (open === String(e.id) ? p.delete('open') : p.set('open', String(e.id))), true)}>
                          {open === String(e.id) ? 'Hide' : 'Details'}</button></td>
                      </tr>
                      {open === String(e.id) && <tr className="lk-usage__detail"><td colSpan={9}><EventDetail e={e} onSaved={fb => setEvents({ ...events, events: events.events.map((x: Row) => x.id === e.id ? { ...x, feedback: fb } : x) })} /></td></tr>}
                    </React.Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {events.total > limit && (
            <div className="lk-usage__pager">
              <button type="button" className="lk-usage__btn" disabled={offset === 0} onClick={() => setOne('offset', String(Math.max(0, offset - limit)))}>Previous</button>
              <span>{fmtN(offset + 1)}–{fmtN(Math.min(offset + limit, events.total))} of {fmtN(events.total)}</span>
              <button type="button" className="lk-usage__btn" disabled={offset + limit >= events.total} onClick={() => patch(p => p.set('offset', String(offset + limit)), true)}>Next</button>
            </div>
          )}
        </>
      )}
    </section>
  )
}

function StatusTag({ s, failed }: { s: string | null; failed?: boolean }) {
  return (
    <span className={`lk-usage__status lk-usage__status--${s || 'legacy'}`}>
      {s ? STATUS_LABEL[s] : 'Legacy'}{failed ? ' · failed recall' : ''}
    </span>
  )
}

function SortTh({ label, col, sort, dir, patch }: { label: string; col: string; sort: string; dir: string; patch: (fn: (p: URLSearchParams) => void) => void }) {
  const on = sort === col
  return (
    <th aria-sort={on ? (dir === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button type="button" className="lk-usage__th" onClick={() => patch(p => { p.set('sort', col); p.set('dir', on && dir === 'desc' ? 'asc' : 'desc') })}>
        {label}{on ? (dir === 'asc' ? ' ↑' : ' ↓') : ''}
      </button>
    </th>
  )
}

function Breakdown({ id, title, rows, onPick, keyLabel }: { id?: string; title: string; rows: Row[]; onPick?: (v: string) => void; keyLabel?: (k: any) => string }) {
  const [by, setBy] = useState<'calls' | 'errors' | 'failed_recall'>('calls')
  const sorted = [...rows].sort((a, b) => keyLabel ? 0 : (b[by] ?? 0) - (a[by] ?? 0))
  const max = Math.max(1, ...rows.map(r => r.calls))
  const head = (c: typeof by, label: string) => (
    <th aria-sort={by === c ? 'descending' : 'none'}>
      {keyLabel ? label : <button type="button" className="lk-usage__th" onClick={() => setBy(c)}>{label}{by === c ? ' ↓' : ''}</button>}
    </th>
  )
  return (
    <div className="lk-usage__card" id={id}>
      <h3 className="lk-usage__h3">{title}</h3>
      <table className="lk-admin__table">
        <thead><tr><th>{keyLabel ? 'Hour' : 'Value'}</th>{head('calls', 'Calls')}{head('errors', 'Errors')}{head('failed_recall', 'Failed recall')}</tr></thead>
        <tbody>
          {sorted.map(r => (
            <tr key={String(r.key)} className={onPick ? 'lk-usage__click' : undefined} onClick={onPick ? () => onPick(String(r.key)) : undefined}>
              <td className="lk-usage__ref">
                {keyLabel ? keyLabel(r.key) : String(r.key)}
                <span className="lk-usage__meter" style={{ width: `${(r.calls / max) * 100}%` }} aria-hidden />
              </td>
              <td>{fmtN(r.calls)}</td><td>{fmtN(r.errors)}</td><td>{fmtN(r.failed_recall)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TopList({ title, rows, onPick }: { title: string; rows: Row[]; onPick: (v: string) => void }) {
  return (
    <div className="lk-usage__card">
      <h3 className="lk-usage__h3">{title}</h3>
      {rows.length === 0 ? <p className="lk-admin__note">None in this range.</p> : (
        <table className="lk-admin__table">
          <thead><tr><th>Value</th><th>Calls</th><th>Users</th><th>Failed recall</th></tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.label} className="lk-usage__click" onClick={() => onPick(r.label)}>
                <td className="lk-usage__ref">{r.label}</td><td>{fmtN(r.calls)}</td><td>{fmtN(r.users)}</td><td>{fmtN(r.failed_recall)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function Chart({ series, grain, hover, setHover, onPick }: {
  series: Row[]; grain: string; hover: { x: number; y: number; row: Row } | null
  setHover: (h: { x: number; y: number; row: Row } | null) => void; onPick: (bucket: string, status: string) => void
}) {
  const W = 960, H = 200, padL = 40, padB = 22
  const max = Math.max(1, ...series.map(r => r.total))
  const step = (W - padL) / Math.max(1, series.length)
  const bw = Math.max(2, Math.min(28, step - 2))
  const y = (v: number) => (v / max) * (H - padB - 8)
  const label = (b: string) => grain === 'hour' ? b.slice(11, 16) : b.slice(5, 10)
  const every = Math.ceil(series.length / 12)
  return (
    <div className="lk-usage__card lk-usage__chart">
      <h3 className="lk-usage__h3">Calls per {grain}, by status</h3>
      <div className="lk-usage__legend">
        {SERIES.map(s => <span key={s}><i className={`lk-usage__sw lk-usage__sw--${s}`} />{s === 'legacy' ? 'Legacy' : STATUS_LABEL[s]}</span>)}
        <span className="lk-usage__muted">Click a segment to drill into that {grain} and status.</span>
      </div>
      <div style={{ position: 'relative' }} onMouseLeave={() => setHover(null)}>
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`Calls per ${grain}`}>
          {[0, 0.5, 1].map(f => (
            <g key={f}>
              <line x1={padL} x2={W} y1={H - padB - y(max * f)} y2={H - padB - y(max * f)} className="lk-usage__grid-line" />
              <text x={padL - 6} y={H - padB - y(max * f) + 4} textAnchor="end" className="lk-usage__axis">{Math.round(max * f)}</text>
            </g>
          ))}
          {series.map((r, i) => {
            const x = padL + i * step + (step - bw) / 2
            let base = H - padB
            return (
              <g key={r.bucket} onMouseMove={e => setHover({ x: (e.nativeEvent.offsetX), y: e.nativeEvent.offsetY, row: r })}>
                <rect x={padL + i * step} y={0} width={step} height={H - padB} fill="transparent" />
                {SERIES.map(s => {
                  const h = y(r[s]); if (!r[s]) return null
                  base -= h
                  return <rect key={s} x={x} y={base} width={bw} height={Math.max(1, h - 2)} rx={2}
                    className={`lk-usage__seg lk-usage__seg--${s}`} onClick={() => onPick(r.bucket, s)}>
                    <title>{`${r.bucket}: ${r[s]} ${s}`}</title></rect>
                })}
                {i % every === 0 && <text x={x + bw / 2} y={H - 6} textAnchor="middle" className="lk-usage__axis">{label(r.bucket)}</text>}
              </g>
            )
          })}
        </svg>
        {hover && (
          <div className="lk-usage__tip" style={{ left: Math.min(hover.x + 12, 760), top: hover.y + 12 }}>
            <strong>{hover.row.bucket.replace('T', ' ')}</strong>
            <div>Total {hover.row.total}</div>
            {SERIES.map(s => hover.row[s] ? <div key={s}><i className={`lk-usage__sw lk-usage__sw--${s}`} />{s === 'legacy' ? 'Legacy' : STATUS_LABEL[s]} {hover.row[s]}</div> : null)}
          </div>
        )}
      </div>
    </div>
  )
}

function EventDetail({ e, onSaved }: { e: Row; onSaved: (fb: string | null) => void }) {
  const [saving, setSaving] = useState(false)
  const send = async (feedback: string | null) => {
    setSaving(true)
    const r = await fetch('/api/recall-feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event_id: e.id, feedback }) })
    setSaving(false)
    if (r.ok) onSaved(feedback)
    else alert(`Feedback not saved: HTTP ${r.status}`)
  }
  let params = e.params
  try { params = JSON.stringify(JSON.parse(e.params), null, 2) } catch { /* stored as text */ }
  return (
    <div className="lk-usage__detail-body">
      <dl>
        <dt>Event</dt><dd>#{e.id} · request {e.request_id || '—'} · {e.transport || 'legacy'} · token {e.token_name || e.token_id || '—'} · IP {e.client_ip || '—'}</dd>
        <dt>Query</dt><dd>{e.query || '—'}</dd>
        <dt>Content</dt><dd>{e.content_type || '—'} · {e.content_ref || '—'} {e.act ? `· ${e.act}` : ''}</dd>
        <dt>Result</dt><dd>{fmtN(e.result_count)} results · top score {e.top_score ?? '—'} · citation-shaped {String(e.citation_shaped ?? '—')} · exact match {String(e.exact_match ?? '—')} · {fmtN(e.response_bytes)} bytes</dd>
        {e.error_detail && <><dt>Error</dt><dd className="lk-admin__note--error">{e.error_kind}: {e.error_detail}</dd></>}
        <dt>Feedback</dt>
        <dd className="lk-chips">
          {(['good', 'bad'] as const).map(f => (
            <button key={f} type="button" className="lk-chip" aria-pressed={e.feedback === f} disabled={saving}
              onClick={() => send(e.feedback === f ? null : f)}>{f === 'good' ? 'Good recall' : 'Bad recall'}</button>
          ))}
          {e.feedback_by && <span className="lk-usage__muted">set by {e.feedback_by}</span>}
        </dd>
      </dl>
      {params && <pre className="lk-usage__pre">{params}</pre>}
    </div>
  )
}
