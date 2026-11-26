import React, { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { rulingSlug } from '../utils/display'
import { LicenceNotice } from './SectionContent'

function isPlainClick(e: React.MouseEvent) {
  return !(e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0)
}

type TaxCaseContentProps = {
  caseData: any
  isMobile: boolean
  onNavigate?: (act: string, section: string) => void
  onNavigateRuling?: (citation: string) => void
}

// Parse legislation citation like "Income Tax Assessment Act 1997 (Cth) s 8-1" → {act, section}
function parseLegislationRef(ref: string): { act: string; section: string } | null {
  // Patterns: "Act Name (Cth) s X", "Act Name (Cth) ss X, Y", "Act Name X s Y"
  const trimmed = ref.trim()
  // Try to match common formats
  let m = trimmed.match(/s{1,2}\s+([\d]+(?:[A-Z])?(?:\([\d]+\))?(?:-\d+(?:\([\d]+\))?)*)/i)
  if (!m) m = trimmed.match(/section\s+([\d][\dA-Za-z\-\(\)]+\d?)/i)
  if (!m) return null
  
  const section = m[1]
  
  // Map common act names to act IDs
  const actMap: Record<string, string> = {
    'income tax assessment act 1997': 'itaa-1997',
    'income tax assessment act 1936': 'itaa-1936',
    'taxation administration act 1953': 'taa-1953',
    'a new tax system (goods and services tax) act 1999': 'gst-1999',
    'goods and services tax act 1999': 'gst-1999',
    'federal court of australia act 1976': 'itaa-1997',
    'administrative appeals tribunal act 1975': 'itaa-1997',
    'fringe benefits tax assessment act 1986': 'itaa-1997',
    'superannuation industry (supervision) act 1993': 'itaa-1997',
    'taxation administration act 1999': 'taa-1953',
    'income tax (transitional provisions) act 1997': 'itaa-1997',
    'international tax agreements act 1953': 'itaa-1997',
    'customs act 1901': 'itaa-1997',
    'federal proceedings (costs) act 1981': 'itaa-1997',
    'federal court rules 2011': 'itaa-1997',
    'a new tax system (australian business number) act 1999': 'itaa-1997',
    'customs tariff act 1995': 'itaa-1997',
    'superannuation guarantee (administration) act 1992': 'itaa-1997',
  }
  
  const lower = trimmed.toLowerCase()
  let matchedAct = 'itaa-1997' // default fallback
  for (const [name, actId] of Object.entries(actMap)) {
    if (lower.includes(name)) {
      matchedAct = actId
      break
    }
  }
  
  return { act: matchedAct, section }
}

// Extract citation from cases_cited entry like "[2025] FCAFC 11 — Rusanov v Commissioner"
function extractCaseCitation(entry: string | any): string {
  if (typeof entry === 'string') {
    const m = entry.match(/^(\[[^\]]+\]\s+\S+\s+\S+)/)
    return m ? m[1] : entry.split(' — ')[0].trim()
  }
  return entry.citation || ''
}

export default function TaxCaseContent({ caseData, isMobile, onNavigate, onNavigateRuling }: TaxCaseContentProps) {
  if (!caseData) return null

  const {
    citation,
    title,
    court_label,
    decision_date,
    judges,
    outcome,
    catchwords,
    related_provisions,
    related_rulings,
    section_refs,
    paragraph_count,
    content_length,
    cited_by_count,
    austlii_url,
    hca_url,
    fedcourt_url,
  } = caseData

  // Summary — skip fetch for headnote-only cases (< 10 paragraphs)
  const [summaryData, setSummaryData] = useState<any>(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const hasSubstance = true // always show summary if available; paragraph_count may be unreliable

  useEffect(() => {
    setSummaryData(null)
    if (!citation || !hasSubstance) return
    const safe = citation.replace(/ /g, '_').replace(/\//g, '_').replace(/\[/g, '').replace(/\]/g, '')
    setSummaryLoading(true)
    fetch(`/static/cleaned/summaries/${safe}.json`)
      .then(r => r.ok ? r.json() : null)
      .then(data => setSummaryData(data))
      .catch(() => setSummaryData(null))
      .finally(() => setSummaryLoading(false))
  }, [citation, hasSubstance])

  const secondaryBtn = 'lk-reader-btn'

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <nav aria-label="Breadcrumb" className="lk-reader-crumb">
        <span className="lk-badge lk-badge--case">Case</span>
        {court_label && <span>{court_label}</span>}
        {court_label && citation && <span className="lk-reader-crumb__sep" aria-hidden="true">/</span>}
        {citation && <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-text)', textTransform: 'none', letterSpacing: 0 }}>{citation}</span>}
      </nav>
      <h1 className="lk-reader-title" style={{ marginBottom: 8 }}>
        {title || citation}
      </h1>
      {title && citation && (
        <div className="lk-cite" style={{ color: 'var(--color-text-muted)', marginBottom: 24 }}>{citation}</div>
      )}

      {/* Metadata table */}
      <div style={{
        display: 'flex', flexDirection: 'column', gap: 6,
        marginBottom: 24, maxWidth: 740,
      }}>
        {citation && <MetadataRow label="Citation" value={citation} mono />}
        {court_label && <MetadataRow label="Court" value={court_label} />}
        {decision_date && <MetadataRow label="Decision date" value={decision_date} mono />}
        {judges && <MetadataRow label="Judges" value={Array.isArray(judges) ? judges.join(', ') : judges} />}
        {outcome && <MetadataRow label="Outcome" value={outcome} />}
        {catchwords && <MetadataRow label="Catchwords" value={catchwords} />}
        {paragraph_count !== undefined && paragraph_count !== null && (
          <MetadataRow label="Paragraphs" value={String(paragraph_count)} mono />
        )}
        {content_length !== undefined && content_length !== null && (
          <MetadataRow label="Content length" value={`${(content_length / 1024).toFixed(1)} KB`} mono />
        )}
        {cited_by_count !== undefined && cited_by_count !== null && (
          <MetadataRow label="Cited by" value={String(cited_by_count)} mono />
        )}
      </div>

      {/* Links */}
      {(austlii_url || hca_url || fedcourt_url || citation) && (
        <div className="lk-reader-actions" style={{ marginBottom: 24 }}>
          {austlii_url && (
            <a href={austlii_url} target="_blank" rel="noopener noreferrer" className="lk-reader-btn lk-reader-btn--primary">
              View on AustLII &rarr;
            </a>
          )}
          {hca_url && (
            <a href={hca_url} target="_blank" rel="noopener noreferrer" className={secondaryBtn}>
              View on HCA &rarr;
            </a>
          )}
          {fedcourt_url && (
            <a href={fedcourt_url} target="_blank" rel="noopener noreferrer" className={secondaryBtn}>
              View on FedCourt &rarr;
            </a>
          )}
          {citation && (
            <a href={`/api/tax-cases/case/${encodeURIComponent(citation)}/download`} className={secondaryBtn}>
              Download HTML &darr;
            </a>
          )}
        </div>
      )}

      {/* Case Summary — only for cases with substantive text (>= 10 paragraphs) */}
      {hasSubstance && (
        <div className="lk-reader-divider" style={{ marginTop: 0, marginBottom: 24 }}>
          <h2 className="lk-reader-h2">Case summary</h2>
          {summaryLoading ? (
            <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>
              Sniffing through the judgment…
            </div>
          ) : summaryData && !summaryData.error ? (
            <div style={{ maxWidth: 740 }}>
              <div style={{ marginBottom: 16 }}>
                <div className="lk-reader-label">Facts</div>
                <div className="lk-legal-note" style={{ color: 'var(--color-text)' }}>{summaryData.facts}</div>
              </div>
              <div style={{ marginBottom: 16 }}>
                <div className="lk-reader-label">Issues</div>
                <ol className="lk-legal-note" style={{ margin: 0, paddingLeft: 20, color: 'var(--color-text)' }}>
                  {(summaryData.issues || []).map((i: string, idx: number) => (
                    <li key={idx} style={{ marginBottom: 4 }}>{i}</li>
                  ))}
                </ol>
              </div>
              <div style={{ marginBottom: 16 }}>
                <div className="lk-reader-label">Held</div>
                <div className="lk-legal-note" style={{ color: 'var(--color-text)' }}>{summaryData.held}</div>
              </div>
              <div style={{ marginBottom: 16 }}>
                <div className="lk-reader-label">Reasoning</div>
                <div className="lk-legal-note" style={{ color: 'var(--color-text)' }}>{summaryData.reasoning}</div>
              </div>
              <div style={{ marginBottom: 16 }}>
                <div className="lk-reader-label">Outcome</div>
                <div className="lk-legal-note" style={{ color: 'var(--color-text)' }}>{summaryData.outcome}</div>
              </div>
              {(summaryData.cases_cited || []).length > 0 && (
                <div style={{ marginBottom: 16 }}>
                  <div className="lk-reader-label">
                    Cases cited ({summaryData.cases_cited.length})
                  </div>
                  <div className="lk-reader-cites">
                    {(summaryData.cases_cited || []).map((c: any, idx: number) => {
                      const cit = typeof c === 'string' ? c : c.citation || ''
                      const name = typeof c === 'string' ? '' : c.name || ''
                      const linkCit = extractCaseCitation(c)
                      return (
                        <Link key={idx}
                          to={`/tax-cases/${encodeURIComponent(linkCit)}`}
                          className="lk-link" style={{ alignSelf: 'flex-start' }}
                          onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); onNavigate?.('tax-cases', linkCit) }}
                        >
                          {cit}{name ? ` — ${name}` : ''}
                        </Link>
                      )
                    })}
                  </div>
                </div>
              )}
              {(summaryData.legislation_cited || []).length > 0 && (
                <div>
                  <div className="lk-reader-label">
                    Legislation cited ({summaryData.legislation_cited.length})
                  </div>
                  <div className="lk-reader-cites">
                    {(summaryData.legislation_cited || []).map((l: string, idx: number) => {
                      const parsed = parseLegislationRef(l)
                      return parsed ? (
                        <Link key={idx}
                          to={`/${parsed.act}/${parsed.section}`}
                          className="lk-link" style={{ alignSelf: 'flex-start' }}
                          onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); onNavigate?.(parsed.act, parsed.section) }}
                        >
                          {l}
                        </Link>
                      ) : (
                        <div key={idx} style={{ cursor: 'default' }}>
                          {l}
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>
              AI-powered case summary not available yet. Processing in progress.
            </div>
          )}
        </div>
      )}

      {/* Related provisions */}
      {related_provisions && related_provisions.length > 0 && (
        <div className="lk-reader-divider" style={{ marginTop: 24 }}>
          <h2 className="lk-reader-h2">Related provisions</h2>
          <div className="lk-cite" style={{ color: 'var(--color-text-muted)', maxWidth: 740 }}>
            {Array.isArray(related_provisions) ? related_provisions.map((prov: string, i: number) => {
              // Try to parse act/section from provision like "ITAA 1997 s 8-1"
              const parsed = parseLegislationRef(prov)
              return parsed ? (
                <Link key={i}
                  to={`/${parsed.act}/${parsed.section}`}
                  className="lk-link"
                  onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); onNavigate?.(parsed.act, parsed.section) }}
                >
                  {prov}{i < related_provisions.length - 1 ? ', ' : ''}
                </Link>
              ) : (
                <span key={i}>{prov}{i < related_provisions.length - 1 ? ', ' : ''}</span>
              )
            }) : related_provisions}
          </div>
        </div>
      )}

      {/* Section references — structured refs from case_data */}
      {section_refs && section_refs.length > 0 && (
        <div className="lk-reader-divider" style={{ marginTop: 24 }}>
          <h2 className="lk-reader-h2">Section references</h2>
          <div className="lk-cite" style={{ color: 'var(--color-text-muted)', maxWidth: 740 }}>
            {section_refs.map((ref: any, i: number) => (
              <Link key={i}
                to={`/${ref.act}/${ref.section}`}
                className="lk-link"
                onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); onNavigate?.(ref.act, ref.section) }}
              >
                {ref.act && ref.section ? `${ref.act} s ${ref.section}` : ref.section || ref.base || ref}{i < section_refs.length - 1 ? ', ' : ''}
              </Link>
            ))}
          </div>
        </div>
      )}

      {/* Related rulings */}
      {related_rulings && related_rulings.length > 0 && (
        <div className="lk-reader-divider" style={{ marginTop: 24 }}>
          <h2 className="lk-reader-h2">Related rulings</h2>
          <ul className="lk-reader-cites">
            {related_rulings.map((ruling: string, i: number) => (
              <li key={i}>
                <Link to={`/rulings/${rulingSlug(ruling)}`} className="lk-link"
                  onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); onNavigateRuling?.(ruling) }}
                >
                  {ruling}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
      <LicenceNotice licence={caseData.licence} />
    </div>
  )
}

function MetadataRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="lk-reader-meta">
      <span className="lk-reader-meta__label">{label}</span>
      <span className={`lk-reader-meta__value${mono ? ' lk-cite' : ''}`}>{value}</span>
    </div>
  )
}
