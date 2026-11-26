import React from 'react'

type AfsaGuideContentProps = {
  sectionData: any
  isMobile: boolean
}

const flowParas = (t: string): string[] =>
  String(t || '')
    .replace(/\r/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .split(/\n\n+/)
    .map((seg) => seg.replace(/\n+/g, ' ').replace(/[ \t]+/g, ' ').trim())
    .filter(Boolean)

export default function AfsaGuideContent({
  sectionData,
  isMobile,
}: AfsaGuideContentProps) {
  const title: string = sectionData?.descriptive_title || ''
  const body: string = sectionData?.body || ''
  const pageUrl: string = sectionData?.page_url || ''
  const paragraphs: Array<{ num: string; heading?: string; text: string }> = sectionData?.paragraphs || []

  // Group paragraphs by section heading (new heading starts a new block).
  const blocks: Array<{ heading: string; paras: Array<{ num: string; text: string }> }> = []
  let cur: { heading: string; paras: Array<{ num: string; text: string }> } | null = null
  for (const p of paragraphs) {
    const h = (p.heading || '').trim()
    if (h && (!cur || h !== cur.heading)) {
      cur = { heading: h, paras: [] }
      blocks.push(cur)
    } else if (!cur) {
      cur = { heading: '', paras: [] }
      blocks.push(cur)
    }
    if (cur) cur.paras.push({ num: p.num, text: p.text })
  }

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <nav aria-label="Breadcrumb" className="lk-reader-crumb">
        <span className="lk-badge lk-badge--ruling">Guide</span>
        <span>AFSA Practice Guidance</span>
      </nav>

      <div className="lk-reader-head">
        <h1 className="lk-reader-title">{title}</h1>
      </div>

      <div className="lk-reader-panel">
        {pageUrl && (
          <p style={{ margin: 0 }}>
            <a href={pageUrl} target="_blank" rel="noopener noreferrer" className="lk-link">
              View on AFSA website ↗
            </a>
          </p>
        )}
      </div>

      {blocks.length > 0 ? (
        <div className="lk-reader-body" style={{ marginBottom: 24 }}>
          {blocks.map((b, bi) => (
            <div key={bi} style={{ marginBottom: 16 }}>
              {b.heading && (
                <h2 className="lk-reader-h2" style={{ margin: '20px 0 8px' }}>{b.heading}</h2>
              )}
              {b.paras.map((p) => (
                <div key={p.num} id={`afsa-para-${p.num}`} className="rg-para">
                  <span className="rg-para__num">{p.num}</span>
                  <div className="rg-para__body">
                    {flowParas(p.text).map((seg, i) => (
                      <p key={i}>{seg}</p>
                    ))}
                  </div>
                </div>
              ))}
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
    </div>
  )
}
