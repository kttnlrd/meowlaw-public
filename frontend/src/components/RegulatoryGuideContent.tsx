import React from 'react'
import { LicenceNotice } from './SectionContent'

type RegulatoryGuideContentProps = {
  sectionData: any
  isMobile: boolean
}

// PDF extraction leaves hard line breaks per visual line. Flow them into
// clean prose, preserving only true paragraph breaks (blank lines).
const flowParas = (t: string): string[] =>
  String(t || '')
    .replace(/\r/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .split(/\n\n+/)
    .map((seg) => seg.replace(/\n+/g, ' ').replace(/[ \t]+/g, ' ').trim())
    .filter(Boolean)

export default function RegulatoryGuideContent({
  sectionData,
  isMobile,
}: RegulatoryGuideContentProps) {
  const citation: string = sectionData?.citation || ''
  const title: string = sectionData?.descriptive_title || ''
  const status: string = sectionData?.status || ''
  const statusKey: string = sectionData?.status_key || ''
  const body: string = sectionData?.body || ''
  const hasPdf: boolean = sectionData?.has_pdf || false
  const pageUrl: string = sectionData?.page_url || ''
  const pdfUrl: string = sectionData?.pdf_url || ''
  const date: string = sectionData?.date || ''
  const paragraphs: Array<{ num: string; text: string }> = sectionData?.paragraphs || []

  const subject: string = sectionData?.subject || ''
  const background: string = sectionData?.background || ''
  const guidance: string = sectionData?.guidance || sectionData?.ruling || ''
  const casesReferenced: string[] = sectionData?.cases_referenced || []
  const legislationReferenced: string[] = sectionData?.legislation_referenced || []
  const relatedRulings: string[] = sectionData?.related_rulings || []
  const hasSummary: boolean = sectionData?.has_summary || false

  const isWithdrawn = statusKey === 'withdrawn'

  const summarySection = (label: string, text: string) =>
    text ? (
      <div style={{ marginBottom: 16 }}>
        <div className="lk-reader-label">{label}</div>
        <div className="lk-legal-note" style={{ color: 'var(--color-text)' }}>{text}</div>
      </div>
    ) : null

  const refList = (label: string, items: string[], emptyMsg: string) => (
    <div style={{ marginBottom: 16 }}>
      <div className="lk-reader-label">
        {label} ({items.length})
      </div>
      {items.length > 0 ? (
        <ul className="lk-reader-cites">
          {items.map((item, i) => (
            <li key={i}>{item}</li>
          ))}
        </ul>
      ) : (
        <div style={{ fontSize: 14, color: 'var(--color-text-muted)' }}>{emptyMsg}</div>
      )}
    </div>
  )

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <nav aria-label="Breadcrumb" className="lk-reader-crumb">
        <span className="lk-badge lk-badge--ruling">Guide</span>
        <span>ASIC Regulatory Guide</span>
        <span className="lk-reader-crumb__sep" aria-hidden="true">/</span>
        <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-text)', textTransform: 'none', letterSpacing: 0 }}>{citation}</span>
      </nav>

      <div className="lk-reader-head">
        <h1 className="lk-reader-title">
          <span className="lk-reader-title__num">{citation}</span>
          {title}
        </h1>
        {status && (
          <div className="lk-reader-actions">
            <span className="lk-badge" style={{
              color: isWithdrawn ? 'var(--tabby)' : 'var(--gavel)',
              background: isWithdrawn ? 'var(--tabby-soft)' : 'var(--gavel-soft)',
            }}>
              {status}
            </span>
          </div>
        )}
      </div>

      {/* Metadata + download panel */}
      <div className="lk-reader-panel">
        {status && <p><span className="lk-note-label">Status:</span> {status}</p>}
        {date && date !== 'unknown' && <p><span className="lk-note-label">Last updated:</span> <span className="lk-cite">{date}</span></p>}
        {hasPdf && pdfUrl && (
          <p style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
            <a
              href={pdfUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="lk-reader-btn lk-reader-btn--primary"
            >
              Download PDF
            </a>
            <span style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>
              (from download.asic.gov.au)
            </span>
          </p>
        )}
        {pageUrl && (
          <p style={{ margin: 0 }}>
            <a href={pageUrl} target="_blank" rel="noopener noreferrer" className="lk-link">
              View on ASIC website ↗
            </a>
          </p>
        )}
      </div>

      {/* Structured summary panel (only when a real summary exists) */}
      {hasSummary ? (
        <div className="lk-reader-panel">
          {summarySection('Subject', subject)}
          {summarySection('Background', background)}
          {summarySection('ASIC position', guidance)}
          {refList('Cases referenced', casesReferenced, 'No cases referenced')}
          {refList('Legislation referenced', legislationReferenced, 'No legislation referenced')}
        </div>
      ) : isWithdrawn ? (
        <div className="lk-reader-panel lk-reader-panel--dashed" style={{ fontSize: 14 }}>
          This guide has been withdrawn. See the ASIC website link above for historical context.
        </div>
      ) : null}

      {/* Full text: always shown on launch */}
      {paragraphs.length > 0 ? (
        <div className="lk-reader-body" style={{ marginBottom: 24 }}>
          {paragraphs.map((p) => (
            <div key={p.num} id={`rg-para-${p.num}`} className="rg-para">
              <span className="rg-para__num">{p.num}</span>
              <div className="rg-para__body">
                {flowParas(p.text).map((seg, i) => (
                  <p key={i}>{seg}</p>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : body ? (
        <div className="lk-reader-body" style={{ marginBottom: 24 }}>
          {flowParas(body).map((seg, i) => (
            <p key={i} style={{ marginBottom: 16 }}>{seg}</p>
          ))}
        </div>
      ) : null}
      <LicenceNotice licence={sectionData.licence} />
    </div>
  )
}
