import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { createMarkdownComponents } from './MarkdownRenderers'
import { shortActName, rulingSlug } from '../utils/display'
import { api } from '../api'
import { LicenceNotice } from './SectionContent'

type RulingContentProps = {
  rulingData: any
  isMobile: boolean
  renderLink?: (href?: string, children?: React.ReactNode) => React.ReactNode | null
  onNavigate: (act: string, section: string, anchor?: string) => void
  onNavigateRuling: (citation: string) => void
}

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

function extractHeaderText(node: React.ReactNode): string {
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractHeaderText).join('')
  if (React.isValidElement(node)) {
    const children = (node.props as any)?.children
    return children ? extractHeaderText(children) : ''
  }
  return ''
}

type TocItem = { level: number; text: string; id: string }

function parseToc(body: string): TocItem[] {
  const items: TocItem[] = []
  const re = /^(#{2,3})\s+(.+)$/gm
  let match
  while ((match = re.exec(body)) !== null) {
    const level = match[1].length // 2 for h2, 3 for h3
    const text = match[2].replace(/\*{1,2}|`{1,2}|\[([^\]]*)\]\([^)]+\)/g, '$1').trim()
    const id = slugify(text)
    items.push({ level, text, id })
  }
  return items
}

type RelatedCase = {
  citation: string
  title: string
  court?: string
  year?: string
}

export default function RulingContent({
  rulingData,
  isMobile,
  renderLink,
  onNavigate,
  onNavigateRuling,
}: RulingContentProps) {
  const fm = rulingData?.frontmatter || {}
  const body: string = rulingData?.body || ''
  const descriptiveTitle: string = rulingData?.descriptive_title || ''
  const subject: string = rulingData?.subject || ''
  const question: string = rulingData?.question || ''
  const background: string = rulingData?.background || ''
  const rulingText: string = rulingData?.ruling || ''
  const notice: string = rulingData?.notice || ''
  const decision: string = rulingData?.decision || ''
  const casesReferenced: string[] = rulingData?.cases_referenced || []
  const legislationReferenced: string[] = rulingData?.legislation_referenced || []
  const atoUrl: string = rulingData?.ato_url || ''
  const status: string = rulingData?.status || ''
  const baseComponents = createMarkdownComponents(isMobile, 'rulings', onNavigate, onNavigateRuling, renderLink)

  // Parse table of contents from ##/### headers
  const toc = parseToc(body)

  // Override h2/h3 to include anchor IDs
  const components = {
    ...baseComponents,
    h2: ({ children, ...rest }: { children?: React.ReactNode; [key: string]: any }) => {
      const text = extractHeaderText(children)
      const id = slugify(text)
      const base = baseComponents.h2({ children })
      return React.cloneElement(base as React.ReactElement, { id, ...rest }, children)
    },
    h3: ({ children, ...rest }: { children?: React.ReactNode; [key: string]: any }) => {
      const text = extractHeaderText(children)
      const id = slugify(text)
      const base = baseComponents.h3({ children })
      return React.cloneElement(base as React.ReactElement, { id, ...rest }, children)
    },
  }

  // Related cases state
  const [relatedCases, setRelatedCases] = useState<RelatedCase[]>([])
  const [relatedLoading, setRelatedLoading] = useState(false)

  useEffect(() => {
    const refs = rulingData?.referenced_sections || []
    if (refs.length === 0) {
      setRelatedCases([])
      return
    }
    setRelatedLoading(true)
    const seen = new Set<string>()
    const promises = refs.map((ref: { act: string; section: string }) =>
      api.cases(ref.act, ref.section).catch(() => ({ cases: [] }))
    )
    Promise.all(promises).then((results) => {
      const all: RelatedCase[] = []
      for (const result of results) {
        const casesList = result.cases || []
        for (const c of casesList) {
          if (!seen.has(c.citation)) {
            seen.add(c.citation)
            all.push({ citation: c.citation, title: c.title, court: c.court, year: c.year })
          }
        }
      }
      setRelatedCases(all)
      setRelatedLoading(false)
    })
  }, [rulingData])

  const pageTitle = `${fm.title || rulingData.citation}${descriptiveTitle && descriptiveTitle !== (fm.title || rulingData.citation) ? ` — ${descriptiveTitle}` : ''}`

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <nav aria-label="Breadcrumb" className="lk-reader-crumb">
        <span className="lk-badge lk-badge--ruling">Ruling</span>
        <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-text)', textTransform: 'none', letterSpacing: 0 }}>{rulingData.citation}</span>
      </nav>
      <div className="lk-reader-head">
        <h1 className="lk-reader-title">{pageTitle}</h1>
        <div className="lk-reader-actions">
          <a
            href={`/api/ruling/${encodeURIComponent(rulingData.citation)}/download`}
            download
            className="lk-reader-btn lk-reader-btn--primary"
            title="Download raw text"
          >
            Download
          </a>
        </div>
      </div>

      {/* AI Summary — inline, always visible (not for ATO IDs — full body renders instead) */}
      {rulingData?.type !== 'ATO ID' && (status || subject || question || decision || background || rulingText || notice || legislationReferenced.length > 0 || casesReferenced.length > 0 || atoUrl) && (
      <div className="lk-reader-panel">
        {status && <p><span className="lk-note-label">Status:</span> {status}</p>}
        {subject && <p><span className="lk-note-label">Subject:</span> {subject}</p>}
        {question && <p><span className="lk-note-label">Question:</span> {question}</p>}
        {decision && <p><span className="lk-note-label">Decision:</span> {decision}</p>}
        {background && <p><span className="lk-note-label">Background:</span> {background}</p>}
        {rulingText && <p><span className="lk-note-label">Ruling:</span> {rulingText}</p>}
        {notice && (
          <div style={{
            margin: '0 0 10px', padding: '10px 14px', fontSize: 14, lineHeight: '22px',
            color: 'var(--color-text)', background: 'var(--tabby-soft)',
            border: 'var(--border-chunky) solid var(--tabby)', borderRadius: 'var(--radius-sm)',
          }}>
            {notice}
          </div>
        )}
        {legislationReferenced.length > 0 && (
          <div style={{ marginBottom: 10 }}>
            <div className="lk-reader-label">Legislation</div>
            <ul className="lk-reader-cites">
              {legislationReferenced.map((leg, i) => <li key={i}>{leg}</li>)}
            </ul>
          </div>
        )}
        {casesReferenced.length > 0 && (
          <div style={{ marginBottom: 10 }}>
            <div className="lk-reader-label">Cases</div>
            <ul className="lk-reader-cites">
              {casesReferenced.map((c, i) => <li key={i}>{c}</li>)}
            </ul>
          </div>
        )}
        {atoUrl && (
          <p style={{ margin: 0 }}>
            <a href={atoUrl} target="_blank" rel="noopener noreferrer" className="lk-link">
              View on ATO website ↗
            </a>
          </p>
        )}
      </div>
      )}

      {/* Table of Contents — only when ## headers exist */}
      {toc.length > 0 && (
        <nav aria-label="Contents" className="lk-reader-panel">
          <div className="lk-reader-label" style={{ marginBottom: 10 }}>Contents</div>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {toc.map((item) => (
              <li
                key={item.id}
                style={{
                  paddingLeft: item.level === 3 ? 16 : 0,
                  marginBottom: 4,
                  fontSize: 14,
                  lineHeight: '22px',
                }}
              >
                <a
                  href={`#${item.id}`}
                  onClick={(e) => {
                    e.preventDefault()
                    const el = document.getElementById(item.id)
                    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
                  }}
                  className="lk-link"
                >
                  {item.text}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      )}

      {/* Ruling body */}
      <div className="lk-reader-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]} components={components}>
          {body}
        </ReactMarkdown>
      </div>

      {/* Referenced Sections */}
      {rulingData.referenced_sections?.length > 0 && (
        <div className="lk-reader-divider">
          <h2 className="lk-reader-h2">Referenced sections</h2>
          <ul className="lk-reader-cites">
            {rulingData.referenced_sections.map((ref: { act: string; section: string; title?: string }) => (
              <li key={`${ref.act}-${ref.section}`}>
                <Link
                  to={`/${ref.act}/${ref.section}`}
                  onClick={(e) => {
                    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return
                    e.preventDefault()
                    onNavigate(ref.act, ref.section)
                  }}
                  className="lk-link"
                >
                  {shortActName(ref.act)} s{ref.section}
                </Link>
                {ref.title && <span style={{ fontFamily: 'var(--font-sans)', fontSize: 14 }}> — {ref.title}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Related Cases */}
      {relatedCases.length > 0 && (
        <div className="lk-reader-divider" style={{ marginTop: 32 }}>
          <h2 className="lk-reader-h2">
            Related cases <span className="lk-reader-cite" style={{ fontSize: 14 }}>({relatedCases.length})</span>
          </h2>
          {relatedLoading ? (
            <p style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Sniffing out related cases…</p>
          ) : (
            <ul className="lk-reader-cites" style={{ fontFamily: 'var(--font-sans)', fontSize: 14, lineHeight: '22px' }}>
              {relatedCases.map((c) => (
                <li key={c.citation}>
                  <Link
                    to={`/rulings/${rulingSlug(c.citation)}`}
                    onClick={(e) => {
                      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return
                      e.preventDefault()
                      onNavigateRuling(c.citation)
                    }}
                    className="lk-link"
                  >
                    {c.title || c.citation}
                  </Link>
                  {c.year && (
                    <span className="lk-reader-cite" style={{ fontSize: 12, marginLeft: 6 }}>
                      ({c.year})
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <LicenceNotice licence={rulingData.licence} />
    </div>
  )
}
