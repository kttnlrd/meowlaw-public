import React, { useEffect, useMemo, useState } from 'react'

const API = ''

interface MapMeta {
  id: string
  title: string
  short?: string
  refs?: string
  act: string
  division: string
  subdivision: string
  summary: string
  node_count: number
  edge_count: number
}

const ACT_LABELS: Record<string, string> = {
  'itaa-1997': 'Income Tax Assessment Act 1997',
  'itaa-1936': 'Income Tax Assessment Act 1936',
  'gst-1999': 'GST Act 1999',
  'taa-1953': 'Taxation Administration Act 1953',
  'fbt-1986': 'FBT Assessment Act 1986',
  'sis-1993': 'Superannuation Industry (Supervision) Act 1993',
}

interface Props {
  onClose: () => void
  onOpen: (mapId: string) => void
}

export default function MapBrowser({ onClose, onOpen }: Props) {
  const [maps, setMaps] = useState<MapMeta[] | null>(null)
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch(`${API}/api/maps`)
      .then(r => { if (!r.ok) throw new Error('failed to load maps'); return r.json() })
      .then((data: MapMeta[]) => {
        setMaps(data)
        // auto-expand all acts by default
        const acts = new Set(data.map(m => m.act))
        setExpanded(acts)
      })
      .catch(e => setError(e.message))
  }, [])

  const grouped = useMemo(() => {
    if (!maps) return []
    const q = query.trim().toLowerCase()
    const filtered = q ? maps.filter(m =>
      (m.title || '').toLowerCase().includes(q) ||
      (m.subdivision || '').toLowerCase().includes(q) ||
      (m.summary || '').toLowerCase().includes(q) ||
      (m.id || '').toLowerCase().includes(q)
    ) : maps
    const byAct = new Map<string, MapMeta[]>()
    for (const m of filtered) {
      if (!byAct.has(m.act)) byAct.set(m.act, [])
      byAct.get(m.act)!.push(m)
    }
    return [...byAct.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [maps, query])

  const toggle = (act: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(act)) next.delete(act); else next.add(act)
      return next
    })
  }

  const chevron = (open: boolean) => (
    <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d={open ? 'M6 9l6 6 6-6' : 'M9 6l6 6-6 6'} />
    </svg>
  )

  return (
    <div className="lk-map-browser__overlay" onClick={onClose}>
      <div className="lk-map-browser" role="dialog" aria-modal="true" aria-labelledby="lk-map-browser-title" onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="lk-map-browser__head">
          <div>
            <h2 id="lk-map-browser-title" className="lk-map-browser__title">Maps</h2>
            <p className="lk-map-browser__sub">
              {maps ? `${maps.length} map${maps.length === 1 ? '' : 's'}: decision flows through a provision, with statute, cases and definitions at each step` : 'Unrolling the maps...'}
            </p>
          </div>
          <button type="button" className="lk-map__iconbtn lk-map__iconbtn--bare" onClick={onClose} aria-label="Close">
            <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d="M6 6l12 12M18 6L6 18" /></svg>
          </button>
        </div>

        {/* Search */}
        <div className="lk-map-browser__search">
          <input
            className="lk-map-browser__input"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search maps (e.g. roll-over, 122-A, restructure)"
            aria-label="Search maps"
          />
        </div>

        {/* Tree */}
        <div className="lk-map-browser__tree">
          {error ? (
            <div className="lk-empty-state">
              <p className="lk-empty-state__title">The maps are hiding.</p>
              <div>{error}</div>
            </div>
          ) : !maps ? (
            <div className="lk-search-loading" role="status">Sniffing out the maps...</div>
          ) : grouped.length === 0 ? (
            <div className="lk-empty-state">
              <p className="lk-empty-state__title">No maps match.</p>
              <div>Nothing for "{query}". The cat checked twice.</div>
            </div>
          ) : (
            grouped.map(([act, actMaps]) => (
              <div key={act}>
                {/* Act header */}
                <button type="button" className="lk-map-browser__act" onClick={() => toggle(act)} aria-expanded={expanded.has(act)}>
                  {chevron(expanded.has(act))}
                  <span>{ACT_LABELS[act] || act}</span>
                  <span className="lk-map-browser__count">({actMaps.length})</span>
                </button>
                {expanded.has(act) && (
                  <div className="lk-map-browser__list">
                    {actMaps.map(m => (
                      <button key={m.id} type="button" className="lk-result-card" onClick={() => onOpen(m.id)}>
                        <div className="lk-result-card__head">
                          <span className="lk-badge lk-badge--map">Map</span>
                          <span className="lk-result-card__title">{m.short || m.title}</span>
                        </div>
                        {m.refs && <div className="lk-result-card__cite">{m.refs}</div>}
                        {m.summary && (
                          <div className="lk-result-card__snippet">{m.summary}</div>
                        )}
                        <div className="lk-map-browser__stats">
                          {m.node_count} steps · {m.edge_count} paths
                        </div>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
