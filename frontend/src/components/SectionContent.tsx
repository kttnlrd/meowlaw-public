import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { createMarkdownComponents, RenderLinkFn } from './MarkdownRenderers'
import SmartLinkPanel from './SmartLinkPanel'
import type { Licence } from './common/types'

interface SectionContentProps {
  act: string
  sectionData: any
  isMobile: boolean
  isPinned?: boolean
  togglePin?: () => void
  renderLink?: RenderLinkFn
  onNavigate: (act: string, section: string, anchor?: string) => void
  onNavigateRuling: (citation: string) => void
  onNavigateCase: (citation: string) => void
}

/** Source, attribution, mandatory disclaimer and unofficial-copy / no-endorsement notice for any licensed
 *  document (legislation, rulings, regulatory guides, cases). Wording comes verbatim from the
 *  data/<act or source>/licence.json record via the API — never edit it here.
 *  Default is ONE muted line (the record's notice_line); the full mandated wording is one click away. */
export const LicenceNotice: React.FC<{ licence?: Licence | null }> = ({ licence }) => {
  const [open, setOpen] = React.useState(false)
  if (!licence) return null
  const line = licence.notice_line
  return (
    <aside className={`lk-licence${open ? ' lk-licence--open' : ''}`} aria-label="Source and licence">
      <p className="lk-licence__line">
        <span className="lk-licence__badge">{licence.notice_badge || licence.jurisdiction}</span>
        {line && <span className="lk-licence__text">{line}</span>}
        <button
          type="button"
          className="lk-licence__toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? 'Hide details' : 'Details'}
        </button>
      </p>
      {open && (
        <div className="lk-licence__full">
          {licence.unofficial_notice && <p className="lk-licence__unofficial">{licence.unofficial_notice}</p>}
          {licence.attribution && <p>{licence.attribution}</p>}
          {licence.disclaimer && <p className="lk-licence__disclaimer">{licence.disclaimer}</p>}
          {licence.endorsement_notice && <p>{licence.endorsement_notice}</p>}
          <p className="lk-licence__links">
            {/* version_date is absent (not null) for rulings and cases: they carry no version line */}
            {licence.version_date !== undefined &&
              (licence.version_date ? `Version current from ${licence.version_date}. ` : 'Version date not printed on the copy held. ')}
            <a href={licence.source_url} target="_blank" rel="noopener noreferrer">Source document</a>
            {' · '}
            <a href={licence.licence.url} target="_blank" rel="noopener noreferrer">{licence.licence.name}</a>
            {' · '}
            <a href={licence.terms_url} target="_blank" rel="noopener noreferrer">Copyright terms</a>
          </p>
        </div>
      )}
    </aside>
  )
}

const SectionContent: React.FC<SectionContentProps> = ({
  act,
  sectionData,
  isMobile,
  isPinned,
  togglePin,
  renderLink,
  onNavigate,
  onNavigateRuling,
  onNavigateCase,
}) => {
  const fm = sectionData?.frontmatter || {}
  const components = createMarkdownComponents(isMobile, act, onNavigate, onNavigateRuling, renderLink)

  const sectionId = fm.section || ''

  const sep = <span className="lk-reader-crumb__sep" aria-hidden="true">/</span>
  const crumbs = [
    fm.act,
    fm.part && `Part ${fm.part}`,
    fm.division && `Division ${fm.division}`,
    fm.subdivision && `Subdivision ${fm.subdivision}`,
  ].filter(Boolean) as string[]

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 16, maxWidth: 740 }}>
        <nav aria-label="Breadcrumb" className="lk-reader-crumb" style={{ margin: 0, flex: 1, minWidth: 0, paddingTop: 10 }}>
          {crumbs.map((c, i) => (
            <React.Fragment key={i}>
              {i > 0 && sep}
              <span>{c}</span>
            </React.Fragment>
          ))}
        </nav>
        <button
          type="button"
          onClick={togglePin}
          aria-pressed={!!isPinned}
          className={`lk-reader-btn${isPinned ? ' lk-reader-btn--primary' : ''}`}
          title={isPinned ? 'Unpin this section' : 'Pin this section'}
        >
          {isPinned ? 'Unpin' : 'Pin'}
        </button>
      </div>

      <div className="lk-reader-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]} components={components}>
          {sectionData.body || sectionData.markdown}
        </ReactMarkdown>
      </div>

      <LicenceNotice licence={sectionData.licence} />

      {/* Smart Links — Related content */}
      <div className="lk-reader-divider">
        <SmartLinkPanel
          act={act}
          section={sectionId}
          onNavigate={onNavigate}
          onNavigateRuling={onNavigateRuling}
          onNavigateCase={onNavigateCase}
        />
      </div>
    </div>
  )
}

export default SectionContent
