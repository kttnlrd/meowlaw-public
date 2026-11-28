import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { createMarkdownComponents } from './MarkdownRenderers'
import SmartLinkPanel from './SmartLinkPanel'
import { LicenceNotice } from './SectionContent'

// ---------------------------------------------------------------------------
// Private ruling detail view — one of the 57,608 ATO private rulings.
// Shape: { authorisation_number, name, date_of_advice, subject, qa_pairs,
//         applies_for_periods, scheme_commenced, facts, relevant_legislation,
//         reasons_for_decision, case_references, formatted_text, graph_key }
// ---------------------------------------------------------------------------

type PrivateRulingContentProps = {
  data: any
  isMobile: boolean
  renderLink?: (href?: string, children?: React.ReactNode) => React.ReactNode | null
  onNavigate: (act: string, section: string, anchor?: string) => void
  onNavigateRuling: (citation: string) => void
  onNavigateCase?: (citation: string) => void
}

// Mirrors scripts/build_private_ruling_outcomes.py: a search aid showing how the
// ATO answered the taxpayer's own question, not a legal characterisation.
type Outcome = 'yes' | 'no' | 'mixed' | ''
function answerOutcome(answer: unknown): Outcome {
  const a = textBlock(answer)
  if (/^\s*yes\b/i.test(a)) return 'yes'
  if (/^\s*no\b/i.test(a)) return 'no'
  return 'mixed'
}
function overallOutcome(qas: { answer?: unknown }[]): Outcome {
  if (qas.length === 0) return ''
  const flags = new Set(qas.map(q => answerOutcome(q.answer)))
  if (flags.size === 1 && flags.has('yes')) return 'yes'
  if (flags.size === 1 && flags.has('no')) return 'no'
  return 'mixed'
}
function OutcomeBadge({ outcome }: { outcome: Outcome }) {
  if (!outcome) return null
  return (
    <span className={`lk-outcome lk-outcome--${outcome}`}>
      {outcome === 'yes' ? 'Yes' : outcome === 'no' ? 'No' : 'Mixed'}
    </span>
  )
}

function textBlock(value: unknown): string {
  if (Array.isArray(value)) return value.join('\n\n')
  return String(value ?? '')
}

export default function PrivateRulingContent({
  data,
  isMobile,
  renderLink,
  onNavigate,
  onNavigateRuling,
  onNavigateCase,
}: PrivateRulingContentProps) {
  const auth = data.authorisation_number || ''
  const name = data.name || data.subject || 'Private ruling'
  const date = data.date_of_advice || ''
  const qaPairs: { question?: string; answer?: string }[] = data.qa_pairs || []
  const facts = textBlock(data.facts)
  const reasons = textBlock(data.reasons_for_decision)
  const periods = textBlock(data.applies_for_periods)
  const commenced = textBlock(data.scheme_commenced)
  const legRefs: unknown[] = data.relevant_legislation || []
  const caseRefs: unknown[] = data.case_references || []
  const formatted = textBlock(data.formatted_text)
  const graphKey: string = data.graph_key || ''
  const atoUrl: string = data.ato_url || ''
  const downloadUrl: string = data.download_url || ''
  const outcome = overallOutcome(qaPairs)
  const baseComponents = createMarkdownComponents(isMobile, 'private-rulings', onNavigate, onNavigateRuling, renderLink)

  const md = (src: string) => (
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]} components={baseComponents}>
      {src}
    </ReactMarkdown>
  )

  const refList = (items: unknown[]) => {
    if (!items || items.length === 0) return null
    return (
      <div style={{ marginTop: 24, maxWidth: 740 }}>
        <div className="lk-reader-label">
          {Array.isArray(items[0]) && typeof items[0][0] === 'string' ? 'Relevant legislation' : 'References'}
        </div>
        <ul className="lk-reader-cites">
          {items.map((it, i) => (
            <li key={i}>{typeof it === 'string' ? it : JSON.stringify(it)}</li>
          ))}
        </ul>
      </div>
    )
  }

  const block = (label: string, src: string) => (
    <div style={{ marginTop: 24 }}>
      <div className="lk-reader-label">{label}</div>
      <div className="lk-reader-body">{md(src)}</div>
    </div>
  )

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <nav aria-label="Breadcrumb" className="lk-reader-crumb">
        <span className="lk-badge lk-badge--private-ruling">Private ruling</span>
        <span className="lk-reader-cite" style={{ color: 'var(--color-text)', letterSpacing: 0, textTransform: 'none' }}>EV/{auth}</span>
        {outcome && <span style={{ marginLeft: 10 }}><OutcomeBadge outcome={outcome} /></span>}
      </nav>
      <div className="lk-reader-head" style={{ marginBottom: 16 }}>
        <h1 className="lk-reader-title">{name}</h1>
        {downloadUrl && (
          <div className="lk-reader-actions">
            <a
              href={downloadUrl}
              download
              className="lk-reader-btn lk-reader-btn--primary"
              title="Download original ATO page"
            >
              Download
            </a>
          </div>
        )}
      </div>
      <div className="lk-reader-meta" style={{ maxWidth: 740, boxSizing: 'border-box', marginBottom: 8 }}>
        <span className="lk-reader-meta__label">Date of advice</span>
        <span className="lk-reader-meta__value">{date ? <span className="lk-reader-cite" style={{ color: 'var(--color-text)' }}>{date}</span> : 'Undated'}</span>
      </div>
      <div className="lk-reader-meta" style={{ maxWidth: 740, boxSizing: 'border-box', marginBottom: 24 }}>
        <span className="lk-reader-meta__label">ATO reference</span>
        <span className="lk-reader-meta__value">
          <span className="lk-reader-cite" style={{ color: 'var(--color-text)' }}>EV/{auth}</span>
          {atoUrl && (
            <>
              {' · '}
              <a href={atoUrl} target="_blank" rel="noopener noreferrer" className="lk-link">
                View on ATO website ↗
              </a>
            </>
          )}
        </span>
      </div>

      {formatted && (
        <div className="lk-reader-body">
          {md(formatted)}
        </div>
      )}

      {qaPairs.length > 0 && (
        <div className="lk-pr-qas">
          <div className="lk-reader-label" style={{ margin: 0 }}>Questions and answers</div>
          {qaPairs.map((qa, i) => (
            <div key={i} className="lk-pr-qa">
              {qa.question && (
                <div className="lk-pr-qa__head">
                  <span className="lk-pr-qa__num">Q{i + 1}</span>
                  <span className="lk-pr-qa__q">{qa.question}</span>
                  {qa.answer && answerOutcome(qa.answer) !== 'mixed' && <OutcomeBadge outcome={answerOutcome(qa.answer)} />}
                </div>
              )}
              {qa.answer && (
                <div className="lk-reader-body">
                  {md(textBlock(qa.answer))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {facts && block('Facts', facts)}
      {reasons && block('Reasons for decision', reasons)}
      {periods && block('Applies for periods', periods)}
      {commenced && block('Scheme commenced', commenced)}

      {refList(legRefs)}
      {refList(caseRefs)}

      {graphKey && (
        <div className="lk-reader-divider">
          <SmartLinkPanel
            act="private-rulings"
            section={auth}
            graphKey={graphKey}
            onNavigate={onNavigate}
            onNavigateRuling={onNavigateRuling}
            onNavigateCase={onNavigateCase}
          />
        </div>
      )}
      <LicenceNotice licence={data.licence} />
    </div>
  )
}
