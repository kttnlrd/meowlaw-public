import React, { useState } from 'react'
import { COLORS } from './common/types'

const API = ''

export default function IssuesModal({ onClose }: { onClose: () => void }) {
  const [reportText, setReportText] = useState('')
  const [reportSent, setReportSent] = useState(false)
  const [sending, setSending] = useState(false)

  const handleSubmit = async () => {
    if (!reportText.trim()) return
    setSending(true)
    try {
      const r = await fetch(`${API}/api/issues`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: reportText.trim(), category: 'bug' }),
      })
      const data = await r.json()
      if (data.ticket) setReportSent(true)
    } catch {}
    setSending(false)
  }

  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, zIndex: 10000,
      background: 'rgba(0,0,0,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: COLORS?.surface || '#1a1a2e', borderRadius: 12,
        width: '90%', maxWidth: 520,
        boxShadow: '0 16px 48px rgba(0,0,0,0.5)',
      }}>
        {/* Header */}
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '16px 20px', borderBottom: `1px solid ${COLORS?.border || '#333'}`,
        }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: COLORS?.heading || '#fff', fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
            Report a bug
          </span>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', color: COLORS?.textMuted || '#888',
            cursor: 'pointer', fontSize: 20, lineHeight: 1, padding: '4px 8px',
          }}>✕</button>
        </div>

        <div style={{ padding: '16px 20px' }}>
          {reportSent ? (
            <div style={{ fontSize: 13, color: COLORS?.heading || '#fff', lineHeight: 1.5 }}>
              ✓ Thanks, your report is in. We'll chase it up.
            </div>
          ) : (
            <>
              <div style={{ fontSize: 12, color: COLORS?.textMuted || '#888', marginBottom: 8 }}>
                Something broken? Tell us what happened and we'll fix it.
              </div>
              <textarea
                value={reportText}
                onChange={e => setReportText(e.target.value)}
                placeholder="Describe what went wrong..."
                rows={4}
                style={{
                  width: '100%', padding: 10, borderRadius: 6,
                  background: COLORS?.bg || '#0e0e1e', color: COLORS?.heading || '#fff',
                  border: `1px solid ${COLORS?.border || '#333'}`, fontSize: 13,
                  fontFamily: "var(--font-ui, 'Figtree'), sans-serif", resize: 'vertical',
                  outline: 'none', boxSizing: 'border-box',
                }}
              />
              <button
                onClick={handleSubmit}
                disabled={!reportText.trim() || sending}
                style={{
                  marginTop: 10, padding: '8px 16px', borderRadius: 6,
                  background: reportText.trim() ? (COLORS?.accent || '#c6f432') : (COLORS?.border || '#555'),
                  color: reportText.trim() ? 'var(--on-catnip)' : (COLORS?.textMuted || '#888'),
                  border: 'none', cursor: reportText.trim() ? 'pointer' : 'default',
                  fontSize: 12, fontWeight: 600,
                  fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
                }}
              >
                {sending ? 'Sending...' : 'Submit'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
