import React, { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { COLORS } from './common/types'
import { shortActName, rulingSlug } from '../utils/display'

function isPlainClick(e: React.MouseEvent) {
  return !(e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0)
}

// ---------------------------------------------------------------- types

interface GraphItem {
  key: string
  label: string
  node_type: string
  url: string | null
  name?: string
  year?: number
}

interface GraphGroup {
  edge_type: string
  display: string
  total: number
  items: GraphItem[]
}

interface RelatedSection {
  id: string
  act: string
  title: string
}

interface DefinedTerm {
  term: string
  section: string
  anchor: string
  title: string
}

interface SmartLinkPanelProps {
  act: string
  section: string
  /** Override the graph key (e.g. "private_ruling:EV/123") — when set,
   *  only the graph-driven groups render (no Sections/Definitions). */
  graphKey?: string
  onNavigate?: (act: string, section: string, anchor?: string) => void
  onNavigateRuling?: (citation: string) => void
  onNavigateCase?: (citation: string) => void
}

// Backend caps /api/graph/related at 100 items per group
const GRAPH_LIMIT = 100

// Collapsible dropdown group
function CollapsibleGroup({
  title, count, open, setOpen, children, footer,
}: {
  title: string; count: number; open: boolean; setOpen: (v: boolean) => void
  children: React.ReactNode; footer?: React.ReactNode
}) {
  return (
    <div style={{
      background: COLORS.surface, borderRadius: 6, border: `1px solid ${COLORS.border}`,
      overflow: 'hidden',
    }}>
      <div
        style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '10px 14px', cursor: 'pointer',
          fontSize: 15, fontWeight: 600, color: COLORS.heading,
        }}
        onClick={() => setOpen(!open)}
      >
        <span>{title} <span style={{ color: COLORS.textMuted, fontWeight: 400 }}>({count})</span></span>
        <span style={{ color: COLORS.textMuted, fontSize: 14 }}>{open ? '\u25b2' : '\u25bc'}</span>
      </div>
      {open && (
        <div style={{ padding: '4px 10px 10px', display: 'flex', flexDirection: 'column', gap: 4 }}>
          {children}
          {footer}
        </div>
      )}
    </div>
  )
}

// Clickable item style
function itemStyle(clickable: boolean): React.CSSProperties {
  const base: React.CSSProperties = {
    padding: '7px 12px', borderRadius: 4, fontSize: 15, lineHeight: 1.4,
    background: COLORS.surface, border: `1px solid ${COLORS.border}`,
  }
  if (clickable) {
    return { ...base, cursor: 'pointer', color: COLORS.accent }
  }
  return base
}

const SmartLinkPanel: React.FC<SmartLinkPanelProps> = ({
  act, section, graphKey, onNavigate, onNavigateRuling, onNavigateCase,
}) => {
  const [graphGroups, setGraphGroups] = useState<GraphGroup[]>([])
  const [relatedSections, setRelatedSections] = useState<RelatedSection[]>([])
  const [citedBy, setCitedBy] = useState<RelatedSection[]>([])
  const [definedTerms, setDefinedTerms] = useState<DefinedTerm[]>([])
  const [loading, setLoading] = useState<boolean>(true)

  // Dropdown open states — all default closed
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({})
  const toggleOpen = (key: string) => setOpenGroups(o => ({ ...o, [key]: !o[key] }))

  const graphKeyResolved = graphKey || `section:${act}:${section}`
  const isGraphOnly = !!graphKey

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true)
      setGraphGroups([])
      try {
        const rel = await api.graphRelated(graphKeyResolved, GRAPH_LIMIT).catch(() => ({ groups: [] }))
        setGraphGroups(rel.groups || [])
        if (!isGraphOnly) {
          const refs = await api.sectionRefs(act, section).catch(() => ({ sections: [], cited_by: [], definitions: [] }))
          setRelatedSections(refs.sections || [])
          setCitedBy(refs.cited_by || [])

          const refDefs: DefinedTerm[] = (refs.definitions || []).map((d: any) => ({
            term: d.term || d.id || '',
            section: d.section || '',
            anchor: d.anchor || '',
            title: d.title || `s ${d.section}`,
          }))
          const seen = new Set<string>()
          setDefinedTerms(refDefs.filter(d => {
            if (seen.has(d.term.toLowerCase())) return false
            seen.add(d.term.toLowerCase())
            return true
          }))
        }
      } catch {
        // partial data still better than nothing
      } finally {
        setLoading(false)
      }
    }

    fetchData()
  }, [act, section, graphKeyResolved, isGraphOnly])

  const handleSectionClick = (link: RelatedSection) => {
    if (onNavigate) onNavigate(link.act, link.id)
  }

  const handleDefinitionClick = (def: DefinedTerm) => {
    if (onNavigate) onNavigate(act, def.section, def.anchor)
  }

  const hrefForGraphItem = (item: GraphItem): string | null => {
    if ((item.node_type === 'section' || item.node_type === 'commentary') && item.url) {
      const parts = item.url.split('/').filter(Boolean)
      return parts.length >= 2 ? `/${parts[0]}/${parts[1]}` : null
    }
    if (item.node_type === 'public_ruling') return `/rulings/${rulingSlug(item.label)}`
    if (item.node_type === 'case') return `/tax-cases/${encodeURIComponent(item.label)}`
    if (item.node_type === 'private_ruling' && item.url) return item.url
    return null
  }

  const handleGraphItemClick = (item: GraphItem) => {
    if ((item.node_type === 'section' || item.node_type === 'commentary') && item.url) {
      const parts = item.url.split('/').filter(Boolean)
      if (parts.length >= 2 && onNavigate) onNavigate(parts[0], parts[1])
    } else if (item.node_type === 'public_ruling' && onNavigateRuling) {
      onNavigateRuling(item.label)
    } else if (item.node_type === 'case' && onNavigateCase) {
      onNavigateCase(item.label)
    } else if (item.node_type === 'private_ruling' && item.url) {
      window.location.assign(item.url)
    }
  }

  const hasContent = graphGroups.length > 0 || relatedSections.length > 0 || citedBy.length > 0 || definedTerms.length > 0

  if (loading) {
    return <div style={{ padding: '12px 0', color: COLORS.textMuted, fontSize: 13 }}>Loading related information...</div>
  }

  if (!hasContent) {
    return null
  }

  const sameActSections = relatedSections.filter(s => s.act === act)
  const crossActSections = relatedSections.filter(s => s.act !== act)
  const showSectionRefs = !isGraphOnly && (sameActSections.length > 0 || crossActSections.length > 0 || citedBy.length > 0)

  return (
    <div style={{
      background: COLORS.surface, borderRadius: 8, padding: 12,
      border: `1px solid ${COLORS.border}`, boxShadow: `0 2px 4px rgba(0,0,0,0.2)`,
    }}>
      <h3 style={{ color: COLORS.heading, fontSize: 18, fontWeight: 600, margin: '0 0 12px' }}>Related</h3>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {/* Sections — from in-text references (not graph: no section→section edges) */}
        {showSectionRefs && (
          <CollapsibleGroup
            title="Sections"
            count={sameActSections.length + crossActSections.length + citedBy.length}
            open={!!openGroups.sections}
            setOpen={() => toggleOpen('sections')}
          >
            {sameActSections.length > 0 && (
              <>
                <div style={{ color: COLORS.textMuted, fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4, marginTop: 2 }}>
                  Same Act
                </div>
                {sameActSections.map((link) => (
                  <Link
                    key={'sa-' + link.id}
                    to={`/${link.act}/${link.id}`}
                    style={{ ...itemStyle(true), display: 'block', textDecoration: 'none' }}
                    onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); handleSectionClick(link) }}
                    onMouseEnter={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surfaceHover }}
                    onMouseLeave={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surface }}
                  >
                    s{link.id}{link.title ? ` \u2014 ${link.title}` : ''}
                  </Link>
                ))}
              </>
            )}
            {crossActSections.length > 0 && (
              <>
                <div style={{ color: COLORS.textMuted, fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4, marginTop: sameActSections.length > 0 ? 8 : 2 }}>
                  Cross-Act
                </div>
                {crossActSections.map((link) => (
                  <Link
                    key={'ca-' + link.act + '-' + link.id}
                    to={`/${link.act}/${link.id}`}
                    style={{ ...itemStyle(true), display: 'block', textDecoration: 'none' }}
                    onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); handleSectionClick(link) }}
                    onMouseEnter={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surfaceHover }}
                    onMouseLeave={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surface }}
                  >
                    {shortActName(link.act)} s{link.id}{link.title ? ` \u2014 ${link.title}` : ''}
                  </Link>
                ))}
              </>
            )}
            {citedBy.length > 0 && (
              <>
                <div style={{ color: COLORS.textMuted, fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4, marginTop: sameActSections.length + crossActSections.length > 0 ? 8 : 2 }}>
                  Cited by
                </div>
                {citedBy.map((link) => (
                  <Link
                    key={'cb-' + link.id}
                    to={`/${link.act}/${link.id}`}
                    style={{ ...itemStyle(true), display: 'block', textDecoration: 'none' }}
                    onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); handleSectionClick(link) }}
                    onMouseEnter={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surfaceHover }}
                    onMouseLeave={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surface }}
                  >
                    s{link.id}{link.title ? ` \u2014 ${link.title}` : ''}
                  </Link>
                ))}
              </>
            )}
          </CollapsibleGroup>
        )}

        {/* Definitions — from section-refs scan */}
        {!isGraphOnly && definedTerms.length > 0 && (
          <CollapsibleGroup
            title="Definitions"
            count={definedTerms.length}
            open={!!openGroups.definitions}
            setOpen={() => toggleOpen('definitions')}
          >
            {definedTerms.map((def) => (
              <Link
                key={'def-' + def.term}
                to={`/${act}/${def.section}`}
                style={{ ...itemStyle(true), display: 'block', textDecoration: 'none' }}
                onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); handleDefinitionClick(def) }}
                onMouseEnter={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surfaceHover }}
                onMouseLeave={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surface }}
              >
                {def.term}{def.section ? ` \u2014 s ${def.section}` : ''}
              </Link>
            ))}
          </CollapsibleGroup>
        )}

        {/* Graph-driven groups: Rulings, Private Rulings, Cases, Commentary */}
        {graphGroups.map((group) => (
          <CollapsibleGroup
            key={group.edge_type}
            title={group.display}
            count={group.total}
            open={!!openGroups[group.edge_type]}
            setOpen={() => toggleOpen(group.edge_type)}
            footer={
              group.items.length >= GRAPH_LIMIT ? (
                <div style={{ color: COLORS.textMuted, fontSize: 13, padding: '4px 10px' }}>
                  Top {GRAPH_LIMIT} shown, ranked by citation weight
                </div>
              ) : undefined
            }
          >
            {group.items.map((item) => {
              const clickable = !!item.url
              const href = clickable ? hrefForGraphItem(item) : null
              const content = (
                <>
                  {item.label}
                  {item.node_type === 'public_ruling' && item.year ? (
                    <span style={{ color: COLORS.textMuted, fontSize: 13 }}> ({item.year})</span>
                  ) : null}
                  {item.node_type === 'private_ruling' && (
                    <span style={{ color: COLORS.textMuted, fontSize: 13 }}> (private)</span>
                  )}
                  {(item.node_type === 'case' || item.node_type === 'public_ruling') && item.name ? (
                    <span style={{ color: COLORS.textMuted, fontSize: 13 }}> — {item.name}</span>
                  ) : null}
                </>
              )
              if (href) {
                return (
                  <Link
                    key={item.key}
                    to={href}
                    style={{ ...itemStyle(true), display: 'block', textDecoration: 'none' }}
                    onClick={(e) => { if (!isPlainClick(e)) return; e.preventDefault(); handleGraphItemClick(item) }}
                    onMouseEnter={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surfaceHover }}
                    onMouseLeave={e => { (e.currentTarget as HTMLAnchorElement).style.background = COLORS.surface }}
                  >
                    {content}
                  </Link>
                )
              }
              return (
                <div
                  key={item.key}
                  style={itemStyle(clickable)}
                  onClick={() => clickable && handleGraphItemClick(item)}
                  onMouseEnter={e => {
                    if (clickable) (e.currentTarget as HTMLDivElement).style.background = COLORS.surfaceHover
                  }}
                  onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.background = COLORS.surface }}
                >
                  {content}
                </div>
              )
            })}
          </CollapsibleGroup>
        ))}
      </div>
    </div>
  )
}

export default SmartLinkPanel
