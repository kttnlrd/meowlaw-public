import React from 'react'
import { COLORS } from './common/types'

type NavigateFn = (act: string, section: string, anchor?: string) => void
type NavigateRulingFn = (citation: string) => void
export type RenderLinkFn = (href?: string, children?: React.ReactNode) => React.ReactNode | null

// Subparagraph hierarchy from the leading **(n)** marker in a paragraph:
//   **(1)** digits -> level 0 (flush)
//   **(a)** single lowercase letter -> level 1
//   **(i)** roman numeral -> level 2
//   **(A)** single uppercase letter -> level 3
const _ROMAN = /^(i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|xiii|xiv|xv|xvi|xvii|xviii|xix|xx)$/i

export function subparagraphLevel(children: React.ReactNode): number {
  const text = React.Children.toArray(children)
    .map((c) => (typeof c === 'string' ? c : (c as React.ReactElement).props?.children))
    .flat()
    .join('')
    .replace(/<[^>]*>/g, ' ') // drop raw anchor tags like <a id="s333-1"></a>
    .trim()
  const m = text.match(/^\(([^)]{1,6})\)/)
  if (!m) return 0
  const tok = m[1]
  if (/^\d+$/.test(tok)) return 0
  if (_ROMAN.test(tok)) return 2
  if (/^[a-z]{1,3}$/.test(tok)) return 1 // (a), and inserted paragraphs (aa), (ba), (caa)
  if (/^[A-Z]$/.test(tok)) return 3
  return 0
}

// Flatten React children (including nested elements) into plain text.
export function nodeText(node: React.ReactNode): string {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(nodeText).join('')
  if (React.isValidElement(node)) return nodeText((node.props as any)?.children)
  return ''
}

// **(1)**, **(a)**, **(ii)**, **(A)**, **(1A)** -> subsection / paragraph marker
const MARKER_RE = /^\([0-9A-Za-z]{1,6}\)$/
// **Note:**, **Note 2:**, **Example 1:**, **Examples:**, **Exception:**
const NOTE_LABEL_RE = /^(Notes?|Examples?|Exceptions?)(\s+\d+[A-Z]?)?:$/
const NOTE_BLOCK_RE = /^(Notes?|Examples?|Exceptions?)(\s+\d+[A-Z]?)?:/

// Split "104-10  Disposal of a CGT asset" into a mono section number + title.
const SECTION_NUM_RE = /^\s*((?:s\s?)?[0-9][0-9A-Za-z.\-]*)\s+([\s\S]*)$/

export function splitSectionHeading(children: React.ReactNode): React.ReactNode {
  const arr = React.Children.toArray(children)
  const first = arr[0]
  if (typeof first !== 'string') return children
  const m = first.match(SECTION_NUM_RE)
  if (!m) return children
  return [
    <span key="lk-num" className="lk-reader-title__num">{m[1]}</span>,
    m[2],
    ...arr.slice(1),
  ]
}

export function createMarkdownComponents(
  isMobile: boolean,
  act: string,
  onNavigate: NavigateFn,
  onNavigateRuling: NavigateRulingFn,
  renderLink?: RenderLinkFn,
) {
  const isLegislation = act !== 'rulings' && act !== 'private-rulings'
  return {
    h1: ({ children }: { children?: React.ReactNode }) => (
      <h1 className="lk-reader-title" style={isMobile ? { fontSize: 23, lineHeight: '30px' } : undefined}>
        {isLegislation ? splitSectionHeading(children) : children}
      </h1>
    ),
    h2: ({ children }: { children?: React.ReactNode }) => (
      <h2 className="lk-reader-h2" style={{ fontSize: isMobile ? 18 : 22, lineHeight: isMobile ? '24px' : '28px', marginTop: 28 }}>
        {children}
      </h2>
    ),
    h3: ({ children }: { children?: React.ReactNode }) => (
      <h3 style={{ color: COLORS.heading, fontWeight: 700, fontFamily: 'var(--font-sans)', fontSize: isMobile ? 15 : 17, lineHeight: '24px', marginTop: 20, marginBottom: 10 }}>
        {children}
      </h3>
    ),
    p: ({ children }: { children?: React.ReactNode }) => {
      const level = subparagraphLevel(children)
      const indent = level * (isMobile ? 16 : 28)
      return (
        <p
          className="lk-legal"
          // margins and mobile size come from .lk-reader-body / .lk-note CSS
          style={{ marginLeft: indent || undefined, color: COLORS.text }}
        >
          {children}
        </p>
      )
    },
    strong: ({ children }: { children?: React.ReactNode }) => {
      const text = nodeText(children).trim()
      if (MARKER_RE.test(text)) return <b className="lk-para-marker">{children}</b>
      if (NOTE_LABEL_RE.test(text)) return <strong className="lk-note-label">{children}</strong>
      return <strong>{children}</strong>
    },
    a: ({ children, href }: { children?: React.ReactNode; href?: string }) => {
      if (renderLink) {
        const popover = renderLink(href, children)
        if (popover) return popover
      }
      const handleClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
        if (!href) return
        const sectionMatch = href.match(/\/(itaa-\d{4})\/s([^#]+)(?:#(.+))?/)
        const rulingMatch = href.match(/\/rulings\/(.+)/)
        if (sectionMatch) {
          const targetAct = sectionMatch[1]
          const targetSection = sectionMatch[2]
          const anchor = sectionMatch[3]
          if (targetAct === act) {
            e.preventDefault()
            onNavigate(targetAct, targetSection, anchor)
          }
        } else if (rulingMatch) {
          const targetRuling = decodeURIComponent(rulingMatch[1])
          e.preventDefault()
          onNavigateRuling(targetRuling)
        }
      }
      return (
        <a
          href={href}
          onClick={handleClick}
          className="lk-link"
        >
          {children}
        </a>
      )
    },
    blockquote: ({ children }: { children?: React.ReactNode }) => {
      // Note:/Example: blocks become surface boxes; other blockquotes carry
      // (a)/(i) paragraphs whose indent comes from the <p> marker level.
      if (NOTE_BLOCK_RE.test(nodeText(children).trim())) {
        return <div className="lk-note">{children}</div>
      }
      return <blockquote style={{ margin: 0, padding: 0, border: 0 }}>{children}</blockquote>
    },
    ul: ({ children }: { children?: React.ReactNode }) => <ul style={{ marginLeft: 20, marginBottom: 12 }}>{children}</ul>,
    ol: ({ children }: { children?: React.ReactNode }) => <ol style={{ marginLeft: 20, marginBottom: 12 }}>{children}</ol>,
    li: ({ children }: { children?: React.ReactNode }) => <li style={{ marginBottom: 4 }}>{children}</li>,
    table: ({ children }: { children?: React.ReactNode }) => (
      <table style={{ borderCollapse: 'collapse', width: '100%', marginBottom: 16, border: `1px solid ${COLORS.border}`, fontSize: 'inherit' }}>
        {children}
      </table>
    ),
    thead: ({ children }: { children?: React.ReactNode }) => <thead style={{ background: COLORS.surfaceHover }}>{children}</thead>,
    tbody: ({ children }: { children?: React.ReactNode }) => <tbody>{children}</tbody>,
    tr: ({ children }: { children?: React.ReactNode }) => <tr style={{ borderBottom: `1px solid ${COLORS.border}` }}>{children}</tr>,
    th: ({ children }: { children?: React.ReactNode }) => (
      <th style={{ border: `1px solid ${COLORS.border}`, padding: '8px 12px', textAlign: 'left', fontWeight: 600, color: COLORS.heading }}>
        {children}
      </th>
    ),
    td: ({ children }: { children?: React.ReactNode }) => (
      <td style={{ border: `1px solid ${COLORS.border}`, padding: '8px 12px', color: COLORS.text }}>
        {children}
      </td>
    ),
  }
}
