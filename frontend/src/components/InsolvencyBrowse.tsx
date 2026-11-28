import React, { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'

// Keays Insolvency (admin-gated). /insolvency lists chapters, /insolvency/:n reads one.
type Chapter = { chapter: number; title: string; lines?: number }

export default function InsolvencyBrowse({ isMobile }: { isMobile: boolean }) {
  const n = useLocation().pathname.split('/')[2] || ''
  const [chapters, setChapters] = useState<Chapter[] | null>(null)
  const [detail, setDetail] = useState<any>(null)
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<any[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    setError('')
    setDetail(null)
    const req = n ? api.gated(`/insolvency/chapter/${encodeURIComponent(n)}`).then(setDetail)
                  : api.gated('/insolvency/chapters').then(d => setChapters(d.chapters || []))
    req.catch((e: Error) => setError(e.message))
  }, [n])

  const search = (e: React.FormEvent) => {
    e.preventDefault()
    if (!q.trim()) return setHits(null)
    api.gated(`/insolvency/search?q=${encodeURIComponent(q.trim())}`)
      .then(d => setHits(d.results || []))
      .catch((e: Error) => setError(e.message))
  }

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <nav aria-label="Breadcrumb" className="lk-reader-crumb">
        <span className="lk-badge lk-badge--section">Insolvency</span>
        <Link to="/insolvency" className="lk-link">Keays Insolvency</Link>
        {n && <><span className="lk-reader-crumb__sep">/</span><span>Chapter {n}</span></>}
      </nav>
      {error ? (
        <div className="lk-empty-state" role="alert"><p className="lk-empty-state__title">{error}</p></div>
      ) : n ? (
        !detail ? <p>Loading…</p> : (
          <>
            <div className="lk-reader-head">
              <h1 className="lk-reader-title"><span className="lk-reader-title__num">Ch {detail.chapter}</span> {detail.title}</h1>
            </div>
            <div className="lk-reader-body">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{detail.content || ''}</ReactMarkdown>
            </div>
          </>
        )
      ) : (
        <>
          <div className="lk-reader-head"><h1 className="lk-reader-title">Keays Insolvency</h1></div>
          <form onSubmit={search} className="lk-reader-panel" role="search">
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search the textbook"
                   aria-label="Search insolvency textbook" style={{ width: '100%', minHeight: 44 }} />
          </form>
          {hits ? (
            <ul className="lk-reader-body">
              {hits.length === 0 && <li>No matches.</li>}
              {hits.map((h, i) => (
                <li key={i}><Link className="lk-link" to={`/insolvency/${h.chapter}`}>Ch {h.chapter}: {h.title}</Link></li>
              ))}
            </ul>
          ) : !chapters ? <p>Loading…</p> : (
            <ol className="lk-reader-body">
              {chapters.map(c => (
                <li key={c.chapter} value={c.chapter}><Link className="lk-link" to={`/insolvency/${c.chapter}`}>{c.title}</Link></li>
              ))}
            </ol>
          )}
        </>
      )}
    </div>
  )
}
