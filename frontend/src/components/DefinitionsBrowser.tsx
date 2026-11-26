import React, { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { COLORS } from './common/types'
import { shortActName } from '../utils/display'
import { api } from '../api'

interface DefTerm {
  term: string
  section: string
  anchor: string
  text: string
}

interface ActCount {
  act: string
  count: number
}

interface Props {
  act: string // '' = no act selected yet (act picker)
  onSelectAct: (act: string) => void
  onNavigate: (act: string, section: string, anchor?: string) => void
}

const LIST_CAP = 200

export default function DefinitionsBrowser({ act, onSelectAct, onNavigate }: Props) {
  const [acts, setActs] = useState<ActCount[] | null>(null)
  const [terms, setTerms] = useState<DefTerm[] | null>(null)
  const [results, setResults] = useState<DefTerm[] | null>(null)
  const [query, setQuery] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.definitionsAll()
      .then((data: { acts: ActCount[] }) => setActs(data.acts.filter(a => a.count > 0)))
      .catch(e => setError(e.message))
  }, [])

  useEffect(() => {
    setTerms(null)
    setResults(null)
    setQuery('')
    if (!act) return
    api.definitions(act)
      .then((data: { terms: DefTerm[] }) => setTerms(data.terms))
      .catch(e => setError(e.message))
  }, [act])

  // Debounced search (300ms)
  useEffect(() => {
    if (!act) return
    const q = query.trim()
    if (!q) { setResults(null); return }
    const t = setTimeout(() => {
      api.definitionsSearch(act, q)
        .then((data: { terms: DefTerm[] }) => setResults(data.terms))
        .catch(() => setResults([]))
    }, 300)
    return () => clearTimeout(t)
  }, [act, query])

  const shown = useMemo(() => {
    const list = results ?? terms ?? []
    return results ? list : list.slice(0, LIST_CAP)
  }, [results, terms])

  const total = terms?.length ?? 0

  return (
    <div style={{ fontFamily: "var(--font-ui, 'Figtree'), sans-serif", maxWidth: 860, margin: '0 auto' }}>
      <div style={{ fontSize: 18, fontWeight: 700, color: COLORS.heading, marginBottom: 4 }}>
        Defined terms
      </div>
      <div style={{ fontSize: 12.5, color: COLORS.textMuted, marginBottom: 16 }}>
        Every definition in an act, with its full text. Pick an act, then search by term or definition text.
      </div>

      <div style={{ display: 'flex', gap: 10, marginBottom: 16, flexWrap: 'wrap' }}>
        <select
          value={act}
          onChange={e => onSelectAct(e.target.value)}
          style={{
            padding: '9px 12px', borderRadius: 8, background: COLORS.bg,
            color: COLORS.text, border: `1px solid ${COLORS.border}`, fontSize: 13,
            outline: 'none', minWidth: 260,
          }}
        >
          <option value="">Select an act…</option>
          {(acts || []).map(a => (
            <option key={a.act} value={a.act}>
              {shortActName(a.act)} ({a.count} terms)
            </option>
          ))}
        </select>
        {act && (
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search terms and definition text…"
            style={{
              flex: 1, minWidth: 220, padding: '9px 12px', borderRadius: 8,
              background: COLORS.bg, color: COLORS.text,
              border: `1px solid ${COLORS.border}`, fontSize: 13, outline: 'none',
            }}
          />
        )}
      </div>

      {error ? (
        <div style={{ color: '#ef4444', fontSize: 13 }}>Error: {error}</div>
      ) : !act ? (
        <div style={{ color: COLORS.textMuted, fontSize: 13 }}>
          {acts ? 'Select an act above to browse its defined terms.' : 'Loading acts…'}
        </div>
      ) : !terms ? (
        <div style={{ color: COLORS.textMuted, fontSize: 13 }}>Loading definitions…</div>
      ) : shown.length === 0 ? (
        <div style={{ color: COLORS.textMuted, fontSize: 13 }}>
          {query.trim() ? `No definitions match "${query}".` : 'No definitions indexed for this act.'}
        </div>
      ) : (
        <>
          <div style={{ fontSize: 12, color: COLORS.textMuted, marginBottom: 10 }}>
            {results
              ? `${results.length} match${results.length === 1 ? '' : 'es'} for "${query.trim()}"`
              : total > LIST_CAP
                ? `Showing first ${LIST_CAP} of ${total} terms. Search to narrow down`
                : `${total} term${total === 1 ? '' : 's'}`}
          </div>
          {shown.map(t => (
            <div
              key={`${t.term}|${t.section}`}
              style={{
                padding: '12px 14px', marginBottom: 8, borderRadius: 8,
                background: COLORS.surface, border: `1px solid ${COLORS.border}`,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
                <span style={{ fontSize: 13.5, fontWeight: 700, color: COLORS.heading }}>{t.term}</span>
                {t.section && (
                  <Link
                    to={`/${act}/${t.section}`}
                    onClick={(e) => {
                      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return
                      e.preventDefault()
                      onNavigate(act, t.section, t.anchor || undefined)
                    }}
                    style={{
                      background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                      color: COLORS.accent, fontSize: 12, textDecoration: 'underline',
                    }}
                  >
                    s {t.section}
                  </Link>
                )}
              </div>
              {t.text && (
                <div style={{ fontSize: 12.5, color: COLORS.text, marginTop: 6, lineHeight: 1.55 }}>
                  {t.text}
                </div>
              )}
            </div>
          ))}
        </>
      )}
    </div>
  )
}
