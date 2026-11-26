import React, { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { COLORS } from './common/types'

function isPlainClick(e: React.MouseEvent) {
  return !(e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0)
}

function extractText(node: React.ReactNode): string {
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (React.isValidElement(node)) {
    return extractText(node.props.children)
  }
  return ''
}

type DefinitionData = {
  term: string
  act: string
  section: string
  anchor: string
  text: string
  path: string
}

export default function DefinitionPopover({
  act,
  children,
  href,
  onNavigate,
}: {
  act: string
  children: React.ReactNode
  href?: string
  onNavigate: (section: string, anchor?: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [data, setData] = useState<DefinitionData | null>(null)
  const [error, setError] = useState('')
  const containerRef = useRef<HTMLSpanElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)

  const termText = extractText(children).trim().replace(/^\*+|\*+$/g, '')

  useEffect(() => {
    if (!open) return
    function handleClickOutside(e: MouseEvent) {
      if (
        popoverRef.current &&
        !popoverRef.current.contains(e.target as Node) &&
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [open])

  const handleOpen = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (open) {
      setOpen(false)
      return
    }
    setOpen(true)
    if (data) return
    setLoading(true)
    setError('')
    try {
      const res = await api.definitionText(act, termText)
      setData(res)
    } catch (err: any) {
      setError(err.message || 'Failed to load definition')
    } finally {
      setLoading(false)
    }
  }

  const handleNavigate = () => {
    if (!data) return
    setOpen(false)
    onNavigate(data.section, data.anchor)
  }

  const triggerStyle: React.CSSProperties = {
    color: COLORS.accent,
    cursor: 'pointer',
    textDecoration: 'underline dotted',
    textDecorationThickness: '2px',
    textUnderlineOffset: '4px',
  }

  return (
    <span ref={containerRef} style={{ position: 'relative', display: 'inline' }}>
      {href ? (
        <Link
          to={href}
          onClick={(e) => {
            if (!isPlainClick(e)) return
            e.preventDefault()
            handleOpen(e)
          }}
          className="lk-defined-term"
          style={triggerStyle}
        >
          {children}
        </Link>
      ) : (
        <span onClick={handleOpen} className="lk-defined-term" style={triggerStyle}>
          {children}
        </span>
      )}
      {open && (
        <div
          ref={popoverRef}
          style={{
            position: 'absolute',
            zIndex: 1000,
            top: 'calc(100% + 8px)',
            left: 0,
            maxWidth: 400,
            minWidth: 280,
            background: COLORS.surface,
            border: `var(--border-chunky, 2px) solid var(--line-strong, ${COLORS.border})`,
            borderRadius: 'var(--radius-md, 14px)',
            padding: '12px 16px',
            boxShadow: 'var(--shadow-pounce, 0 4px 20px rgba(0,0,0,0.4))',
            fontFamily: "var(--font-body, 'Figtree'), serif",
          }}
        >
          <div
            style={{
              position: 'absolute',
              top: -6,
              left: 16,
              width: 10,
              height: 10,
              background: COLORS.surface,
              borderLeft: `1px solid ${COLORS.border}`,
              borderTop: `1px solid ${COLORS.border}`,
              transform: 'rotate(45deg)',
            }}
          />
          {loading && (
            <div style={{ color: COLORS.textMuted, fontSize: 13 }}>Loading definition...</div>
          )}
          {error && (
            <div style={{ color: '#ef4444', fontSize: 13 }}>{error}</div>
          )}
          {data && (
            <div>
              <div
                style={{
                  color: COLORS.heading,
                  fontWeight: 600,
                  fontSize: 14,
                  marginBottom: 8,
                  fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                }}
              >
                {data.term}
              </div>
              <div
                style={{
                  color: COLORS.text,
                  fontSize: 13,
                  lineHeight: 1.6,
                  marginBottom: 10,
                  overflowWrap: 'break-word',
                  wordBreak: 'break-word',
                  maxHeight: 240,
                  overflowY: 'auto',
                }}
              >
                {data.text}
              </div>
              <Link
                to={`/${act}/${data.section}`}
                onClick={(e) => {
                  if (!isPlainClick(e)) return
                  e.preventDefault()
                  handleNavigate()
                }}
                style={{
                  color: COLORS.accent,
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: 'pointer',
                  textDecoration: 'none',
                  fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                }}
              >
                Go to definition →
              </Link>
            </div>
          )}
        </div>
      )}
    </span>
  )
}
