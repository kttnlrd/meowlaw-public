import React, { useEffect, useRef, useState } from 'react'
import { COLORS } from './common/types'
import { api } from '../api'

interface AtoModalProps {
  open: boolean
  onClose: () => void
}

interface AtoResult {
  docid: string
  prefix: string
  type: string
  title: string
  year: string
  link: string
}

const PAGE_SIZE = 20

export default function AtoSearchModal({ open, onClose }: AtoModalProps) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<AtoResult[]>([])
  const [total, setTotal] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [start, setStart] = useState(1)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (open) {
      setError(null)
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [open])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && open) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const doSearch = async (pageStart = 1) => {
    const q = query.trim()
    if (q.length < 2) return
    setLoading(true)
    setError(null)
    try {
      const data = await api.atoSearch(q, pageStart, PAGE_SIZE, '')
      setResults(data.results || [])
      setTotal(data.total ?? null)
      setStart(pageStart)
    } catch (e: any) {
      setError(e.message || 'ATO search failed')
      setResults([])
      setTotal(null)
    } finally {
      setLoading(false)
    }
  }

  if (!open) return null

  const totalPages = total != null ? Math.max(1, Math.ceil(total / PAGE_SIZE)) : 1
  const page = Math.floor((start - 1) / PAGE_SIZE) + 1

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000,
        background: 'rgba(0,0,0,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        padding: 16,
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: COLORS.surface, border: `1px solid ${COLORS.border}`,
          borderRadius: 10, width: '100%', maxWidth: 640, maxHeight: '80vh',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
          boxShadow: '0 8px 40px rgba(0,0,0,0.35)',
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '14px 18px', borderBottom: `1px solid ${COLORS.border}` }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 700, color: COLORS.heading, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
              ATO Legal Search
            </div>
            <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 2 }}>
              Live query against the ATO Legal Database (rulings, determinations, ATO IDs)
            </div>
          </div>
          <button onClick={onClose} title="Close" style={{ background: 'none', border: 'none', color: COLORS.textMuted, cursor: 'pointer', fontSize: 18, lineHeight: 1 }}>
            ✕
          </button>
        </div>

        {/* Query row */}
        <div style={{ display: 'flex', gap: 8, padding: '12px 18px', borderBottom: `1px solid ${COLORS.border}` }}>
          <input
            ref={inputRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') doSearch(1) }}
            placeholder="e.g. employee share scheme, PSI, Part IVA..."
            style={{
              flex: 1, padding: '8px 10px', borderRadius: 6,
              background: COLORS.bg, color: COLORS.heading,
              border: `1px solid ${COLORS.border}`, fontSize: 13,
              fontFamily: "var(--font-ui, 'Figtree'), sans-serif", outline: 'none',
            }}
          />
          <button
            onClick={() => doSearch(1)}
            disabled={loading || query.trim().length < 2}
            style={{
              padding: '8px 16px', borderRadius: 6,
              background: COLORS.accent, color: 'var(--on-catnip)',
              border: 'none', fontSize: 13, cursor: loading ? 'default' : 'pointer',
              fontWeight: 600, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", opacity: query.trim().length < 2 ? 0.5 : 1,
            }}
          >
            {loading ? 'Searching…' : 'Search ATO'}
          </button>
        </div>

        {/* Results */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '8px 18px 16px' }}>
          {error && (
            <div style={{ fontSize: 12, color: '#e05b5b', padding: '10px 0' }}>
              {error}
            </div>
          )}
          {!loading && !error && total != null && (
            <div style={{ fontSize: 11, color: COLORS.textMuted, padding: '8px 0 4px' }}>
              {total.toLocaleString()} results · page {page}/{totalPages}
            </div>
          )}
          {!loading && !error && results.length === 0 && (
            <div style={{ fontSize: 12, color: COLORS.textMuted, padding: '14px 0' }}>
              {total != null ? 'No results.' : 'Enter a query to search the ATO Legal Database.'}
            </div>
          )}
          {results.map(r => (
            <div key={r.docid} style={{ padding: '10px 0', borderBottom: `1px solid ${COLORS.border}`, display: 'flex', flexDirection: 'column', gap: 4 }}>
              <a
                href={r.link}
                target="_blank"
                rel="noopener noreferrer"
                style={{ fontSize: 13, color: COLORS.accent, textDecoration: 'none', fontWeight: 600, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", lineHeight: 1.4 }}
              >
                {r.title}
              </a>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 11, color: COLORS.textMuted }}>
                <span style={{ background: COLORS.bg, border: `1px solid ${COLORS.border}`, borderRadius: 4, padding: '1px 6px' }}>
                  {r.type}
                </span>
                {r.year && <span>{r.year}</span>}
                <span style={{ opacity: 0.6 }}>{r.docid}</span>
              </div>
            </div>
          ))}
          {/* Pagination */}
          {!loading && !error && results.length > 0 && (
            <div style={{ display: 'flex', gap: 8, justifyContent: 'center', paddingTop: 14 }}>
              <button
                onClick={() => doSearch(Math.max(1, start - PAGE_SIZE))}
                disabled={start <= 1}
                style={pageBtnStyle(COLORS, start <= 1)}
              >
                ← Prev
              </button>
              <button
                onClick={() => doSearch(start + PAGE_SIZE)}
                disabled={page >= totalPages}
                style={pageBtnStyle(COLORS, page >= totalPages)}
              >
                Next →
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function pageBtnStyle(COLORS: any, disabled: boolean): React.CSSProperties {
  return {
    padding: '6px 12px', borderRadius: 6,
    background: COLORS.surface, color: disabled ? COLORS.textMuted : COLORS.heading,
    border: `1px solid ${COLORS.border}`, fontSize: 12, cursor: disabled ? 'default' : 'pointer',
    fontFamily: "var(--font-ui, 'Figtree'), sans-serif", opacity: disabled ? 0.5 : 1,
  }
}
