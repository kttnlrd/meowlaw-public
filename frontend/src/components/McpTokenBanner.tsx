import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { COLORS } from './common/types'

const DISMISS_KEY = 'mcp-token-banner-dismissed'

/** Top banner prompting the user to generate an MCP token for Claude/ChatGPT.
 *  Shown only when the user has no MCP tokens and hasn't dismissed it. */
export default function McpTokenBanner({ onGenerate }: { onGenerate: () => void }) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    if (localStorage.getItem(DISMISS_KEY) === '1') return
    api.listMcpTokens()
      .then((d: any) => {
        const tokens = d?.tokens || []
        if (tokens.length === 0) setVisible(true)
      })
      .catch(() => {})
  }, [])

  if (!visible) return null

  const dismiss = () => {
    localStorage.setItem(DISMISS_KEY, '1')
    setVisible(false)
  }

  return (
    <div
      role="region"
      aria-label="MCP token"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        background: COLORS.accent,
        color: 'var(--on-catnip, #fff)',
        borderRadius: 10,
        padding: '12px 16px',
        marginBottom: 16,
      }}
    >
      <div style={{ flex: 1, fontSize: 14, fontWeight: 600, lineHeight: 1.4 }}>
        Generate an MCP token to connect Claude or ChatGPT.
      </div>
      <button
        type="button"
        onClick={onGenerate}
        style={{
          background: 'var(--on-catnip, #fff)',
          color: COLORS.accent,
          border: 'none',
          borderRadius: 6,
          padding: '7px 14px',
          fontWeight: 700,
          fontSize: 13,
          cursor: 'pointer',
          whiteSpace: 'nowrap',
        }}
      >
        Generate
      </button>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss"
        title="Dismiss"
        style={{
          background: 'transparent',
          border: 'none',
          color: 'var(--on-catnip, #fff)',
          cursor: 'pointer',
          fontSize: 20,
          lineHeight: 1,
          padding: '2px 6px',
        }}
      >
        &times;
      </button>
    </div>
  )
}
