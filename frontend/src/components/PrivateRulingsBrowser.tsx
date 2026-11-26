import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

function isPlainClick(e: React.MouseEvent) {
  return !(e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0)
}

// ---------------------------------------------------------------------------
// Private rulings browser — year grid + per-year list (57,608 rulings).
// Controlled: `year` lives in App so the sidebar tree can drive it too.
// year: number = a year, 'undated' = undated bucket, null = nothing selected.
// ---------------------------------------------------------------------------

type YearEntry = { year: number; count: number }
type RulingItem = { authnum: string; name: string; date_of_advice: string; ato_url?: string; outcome?: string }
export type PrivateRulingsYear = number | 'undated' | null

const PAGE = 50

export default function PrivateRulingsBrowser({
  year,
  onYearChange,
  isMobile,
  onOpen,
}: {
  year: PrivateRulingsYear
  onYearChange: (y: PrivateRulingsYear) => void
  isMobile: boolean
  onOpen: (authnum: string) => void
}) {
  const [years, setYears] = useState<YearEntry[]>([])
  const [undated, setUndated] = useState(0)
  const [total, setTotal] = useState(0)
  const [rulings, setRulings] = useState<RulingItem[]>([])
  const [listTotal, setListTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loadingList, setLoadingList] = useState(year !== null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.privateRulingsTree()
      .then(d => {
        setYears(d.years || [])
        setUndated(d.undated || 0)
        setTotal(d.total || 0)
        setError('')
      })
      .catch(e => setError(e.message))
  }, [])

  useEffect(() => {
    if (year === null) return
    setLoadingList(true)
    setRulings([])
    setOffset(0)
    const fetchList = year === 'undated'
      ? api.privateRulingsUndated(PAGE, 0)
      : api.privateRulingsByYear(year, PAGE, 0)
    fetchList
      .then(d => {
        setRulings(d.rulings || [])
        setListTotal(d.total || 0)
        setLoadingList(false)
        setError('')
      })
      .catch(e => { setLoadingList(false); setError(e.message) })
  }, [year])

  const loadMore = () => {
    if (year === null) return
    const next = offset + PAGE
    setLoadingList(true)
    const fetchList = year === 'undated'
      ? api.privateRulingsUndated(PAGE, next)
      : api.privateRulingsByYear(year, PAGE, next)
    fetchList
      .then(d => {
        setRulings(prev => [...prev, ...(d.rulings || [])])
        setOffset(next)
        setLoadingList(false)
      })
      .catch(e => { setLoadingList(false); setError(e.message) })
  }

  const yearCard = (key: string, label: string, count: number, to: string, value: PrivateRulingsYear) => (
    <Link
      key={key}
      to={to}
      className="lk-pr-year"
      aria-current={year === value ? 'true' : undefined}
      onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); onYearChange(value) }}
    >
      <span className="lk-pr-year__label">{label}</span>
      <span className="lk-pr-year__count">{count.toLocaleString()}</span>
    </Link>
  )

  const outcomeBadge = (o?: string) => {
    if (o !== 'yes' && o !== 'no' && o !== 'mixed') return null
    return (
      <span className={`lk-outcome lk-outcome--${o}`}>
        {o === 'yes' ? 'Yes' : o === 'no' ? 'No' : 'Mixed'}
      </span>
    )
  }

  return (
    <div className={`lk-pr${isMobile ? ' lk-pr--mobile' : ''}`}>
      <div className="lk-pr-head">
        <h1 className="lk-h1 lk-pr-title">ATO private rulings</h1>
        <span className="lk-pr-count">
          {total.toLocaleString()} rulings
          {undated > 0 && ` · ${undated.toLocaleString()} undated`}
        </span>
      </div>

      {error && (
        <div className="lk-pr-error" role="alert">{error}</div>
      )}

      <nav className="lk-pr-years" aria-label="ATO private rulings by year">
        {years.map(y => yearCard(String(y.year), String(y.year), y.count, `/private-rulings/year/${y.year}`, y.year))}
        {undated > 0 && yearCard('undated', 'Undated', undated, '/private-rulings/year/undated', 'undated')}
      </nav>

      {year === null ? (
        <div className="lk-pr-empty">
          <img src="/app/lawkitty-cat-head.png" alt="" />
          <div className="lk-pr-empty__text">
            <p className="lk-pr-empty__title">Pick a year to browse its rulings.</p>
            <p className="lk-pr-empty__sub">The cat will fetch.</p>
          </div>
        </div>
      ) : (
        <section className="lk-pr-list" aria-label={`ATO private rulings ${year === 'undated' ? 'undated' : year}`}>
          <div className="lk-pr-list__head">
            <h2 className="lk-h2 lk-pr-list__title">{year === 'undated' ? 'Undated' : year}</h2>
            <span className="lk-pr-count">{listTotal.toLocaleString()} rulings</span>
          </div>
          {loadingList && rulings.length === 0 ? (
            <div className="lk-search-loading" role="status">
              <img src="/app/favicon.png" alt="" />
              Fetching rulings. The cat is on it…
            </div>
          ) : rulings.length === 0 ? (
            !error && (
              <div className="lk-empty-state" role="status">
                <p className="lk-empty-state__title">Nothing here yet.</p>
                <span>The cat checked twice.</span>
              </div>
            )
          ) : (
            <div className="lk-results">
              {rulings.map(r => (
                <div key={r.authnum} className="lk-pr-item">
                  <Link
                    to={`/private-rulings/${r.authnum}`}
                    className="lk-result-card"
                    onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); onOpen(r.authnum) }}
                  >
                    <div className="lk-result-card__head">
                      <span className="lk-badge lk-badge--private-ruling">Private ruling</span>
                      <span className="lk-result-card__title">{r.name || 'Untitled ruling'}</span>
                      {outcomeBadge(r.outcome)}
                    </div>
                    <span className="lk-result-card__cite">
                      EV/{r.authnum} · {r.date_of_advice || 'Undated'}
                    </span>
                  </Link>
                  {r.ato_url && (
                    <a
                      href={r.ato_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      onClick={e => e.stopPropagation()}
                      aria-label="View on ATO website"
                      title="View on ATO website"
                      className="lk-reader-btn lk-pr-item__ato"
                    >
                      ATO ↗
                    </a>
                  )}
                </div>
              ))}
              {rulings.length < listTotal && (
                <button
                  type="button"
                  onClick={loadMore}
                  disabled={loadingList}
                  className="lk-reader-btn lk-pr-more"
                >
                  {loadingList ? 'Fetching…' : `Load more (${(listTotal - rulings.length).toLocaleString()} remaining)`}
                </button>
              )}
            </div>
          )}
        </section>
      )}
    </div>
  )
}
