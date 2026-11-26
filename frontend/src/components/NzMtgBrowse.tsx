import React, { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'

// NZ Master Tax Guide (admin-gated). /nz-mtg lists chapters and topics, /nz-mtg/:id reads one.
type Topic = { id: string; title: string; paragraph?: string }
type Chapter = { id: string; title: string; sections: Topic[] }

export default function NzMtgBrowse({ isMobile }: { isMobile: boolean }) {
  const id = decodeURIComponent(useLocation().pathname.split('/')[2] || '')
  const [chapters, setChapters] = useState<Chapter[] | null>(null)
  const [detail, setDetail] = useState<any>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    setError('')
    setDetail(null)
    const req = id ? api.gated(`/nz-mtg/section/${encodeURIComponent(id)}`).then(setDetail)
                   : api.gated('/nz-mtg/chapters').then(d => setChapters(d.chapters || []))
    req.catch((e: Error) => setError(e.message))
  }, [id])

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <nav aria-label="Breadcrumb" className="lk-reader-crumb">
        <span className="lk-badge lk-badge--section">NZ MTG</span>
        <Link to="/nz-mtg" className="lk-link">NZ Master Tax Guide</Link>
        {detail?.paragraph && <><span className="lk-reader-crumb__sep">/</span><span>{detail.paragraph}</span></>}
      </nav>
      {error ? (
        <div className="lk-empty-state" role="alert"><p className="lk-empty-state__title">{error}</p></div>
      ) : id ? (
        !detail ? <p>Loading…</p> : (
          // The markdown body carries its own H1 (title + paragraph number).
          <div className="lk-reader-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{detail.content || ''}</ReactMarkdown>
          </div>
        )
      ) : !chapters ? <p>Loading…</p> : (
        <>
          <div className="lk-reader-head"><h1 className="lk-reader-title">NZ Master Tax Guide</h1></div>
          {chapters.map(c => (
            <details key={c.id} className="lk-reader-panel">
              <summary style={{ cursor: 'pointer', minHeight: 44, display: 'flex', alignItems: 'center' }}>{c.title}</summary>
              <ul>
                {c.sections.map(s => (
                  <li key={s.id}>
                    <Link className="lk-link" to={`/nz-mtg/${encodeURIComponent(s.id)}`}>{s.paragraph ? `${s.paragraph} ` : ''}{s.title}</Link>
                  </li>
                ))}
              </ul>
            </details>
          ))}
        </>
      )}
    </div>
  )
}
