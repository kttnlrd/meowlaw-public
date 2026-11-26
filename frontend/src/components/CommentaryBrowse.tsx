import React, { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { api } from '../api'

// CCH commentary (admin-gated). LES indexes commentary by the section it discusses, so this
// browses by provision: /commentary/:act/:section lists every commentary heading citing it.
type Block = { text: string }
type Entry = {
  publication: string; chapter_number?: string; chapter_title?: string
  heading_title: string; paragraph_number?: string
  content_blocks?: Block[]; sub_headings?: { title?: string; content_blocks?: Block[] }[]
}
const ACTS: [string, string][] = [
  ['itaa-1997', 'ITAA 1997'], ['itaa-1936', 'ITAA 1936'], ['taa-1953', 'TAA 1953'],
  ['gst-1999', 'GST Act 1999'], ['fbt-1986', 'FBTAA 1986'], ['sis-1993', 'SIS Act 1993'],
]

export default function CommentaryBrowse({ isMobile }: { isMobile: boolean }) {
  const navigate = useNavigate()
  const [, , urlAct, urlSection] = useLocation().pathname.split('/').map(decodeURIComponent)
  const [act, setAct] = useState(urlAct || 'itaa-1997')
  const [section, setSection] = useState(urlSection || '')
  const [entries, setEntries] = useState<Entry[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    setError('')
    setEntries(null)
    if (!urlAct || !urlSection) return
    api.gated(`/commentary/${encodeURIComponent(urlAct)}/${encodeURIComponent(urlSection)}`)
      .then(d => setEntries(d.commentary || []))
      .catch((e: Error) => setError(e.message))
  }, [urlAct, urlSection])

  const go = (e: React.FormEvent) => {
    e.preventDefault()
    const s = section.trim().replace(/^s\s*/i, '')
    if (s) navigate(`/commentary/${act}/${encodeURIComponent(s)}`)
  }

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <nav aria-label="Breadcrumb" className="lk-reader-crumb">
        <span className="lk-badge lk-badge--section">Commentary</span>
        <span>CCH commentary by provision</span>
      </nav>
      <div className="lk-reader-head"><h1 className="lk-reader-title">Commentary</h1></div>
      <form onSubmit={go} className="lk-reader-panel" style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <select value={act} onChange={e => setAct(e.target.value)} aria-label="Act" style={{ minHeight: 44 }}>
          {ACTS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
        <input value={section} onChange={e => setSection(e.target.value)} placeholder="Section, e.g. 8-1"
               aria-label="Section" style={{ flex: 1, minHeight: 44 }} />
        <button type="submit" className="lk-reader-btn lk-reader-btn--primary" style={{ minHeight: 44 }}>Show</button>
      </form>
      {error ? (
        <div className="lk-empty-state" role="alert"><p className="lk-empty-state__title">{error}</p></div>
      ) : !urlSection ? null : !entries ? <p>Loading…</p> : entries.length === 0 ? (
        <div className="lk-empty-state"><p className="lk-empty-state__title">No commentary cites s {urlSection}.</p></div>
      ) : (
        <div className="lk-reader-body">
          {entries.map((en, i) => (
            <article key={i} className="lk-reader-panel">
              <h2 className="lk-reader-h2">{en.paragraph_number ? `${en.paragraph_number} ` : ''}{en.heading_title}</h2>
              <p className="lk-reader-meta">{en.publication}{en.chapter_title ? ` · Ch ${en.chapter_number} ${en.chapter_title}` : ''}</p>
              {(en.content_blocks || []).map((b, j) => <p key={j} style={{ whiteSpace: 'pre-line' }}>{b.text}</p>)}
              {(en.sub_headings || []).map((sh, j) => (
                <section key={j}>
                  {sh.title && <h3>{sh.title}</h3>}
                  {(sh.content_blocks || []).map((b, k) => <p key={k} style={{ whiteSpace: 'pre-line' }}>{b.text}</p>)}
                </section>
              ))}
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
