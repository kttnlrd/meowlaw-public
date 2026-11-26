
import React from 'react';
import { Link } from 'react-router-dom';
import { COLORS, PinItem } from './common/types';

type PinnedTabsProps = {
  pins: PinItem[];
  act: string;
  activeSection: string;
  isMobile: boolean;
  setAct: (act: string) => void;
  setActiveSection: (section: string) => void;
  unpin: (pin: PinItem) => void;
};

export default function PinnedTabs({
  pins,
  act,
  activeSection,
  isMobile,
  setAct,
  setActiveSection,
  unpin,
}: PinnedTabsProps) {
  if (pins.length === 0) return null;

  // Mobile pin tabs
  if (isMobile) {
    return (
      <div style={{ display: 'flex', gap: 8, marginBottom: 16, overflow: 'auto', paddingLeft: 16, paddingRight: 16 }}>
        {pins.map(p => (
          <div
            key={`${p.act}-${p.section}`}
            style={{
              display: 'flex', alignItems: 'center', gap: 4,
              padding: '6px 10px', borderRadius: 6,
              background: p.act === act && p.section === activeSection ? 'rgba(39,158,136,0.2)' : COLORS.surface,
              border: `1px solid ${COLORS.border}`, flexShrink: 0,
            }}
          >
            <Link
              to={`/${p.act}/${p.section}`}
              style={{ fontSize: 12, color: COLORS.text, cursor: 'pointer', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 140, textDecoration: 'none' }}
              onClick={(e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); setAct(p.act); setActiveSection(p.section) }}
            >
              {p.act} › {p.section}
            </Link>
            <button
              onClick={() => unpin(p)}
              style={{
                background: 'none', border: 'none', color: COLORS.textMuted,
                cursor: 'pointer', fontSize: 16, padding: '0 2px', lineHeight: 1,
              }}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    );
  }

  // Desktop pin panel
  return (
    <div
      style={{
        width: 260,
        background: COLORS.surface,
        borderLeft: `1px solid ${COLORS.border}`,
        display: 'flex', flexDirection: 'column',
        flexShrink: 0,
      }}
    >
      <div
        style={{
          padding: '14px', borderBottom: `1px solid ${COLORS.border}`,
          fontSize: 12, fontWeight: 600, color: COLORS.textMuted,
          textTransform: 'uppercase', fontFamily: "var(--font-ui, 'Figtree'), sans-serif",
          letterSpacing: 0.4,
        }}
      >
        Pinned
      </div>
      <div className="drawer-scroll" style={{ flex: 1, overflow: 'auto', padding: 8 }}>
        {pins.map(p => (
          <div
            key={`${p.act}-${p.section}`}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '6px 8px', borderRadius: 4,
              background: p.act === act && p.section === activeSection ? 'rgba(39,158,136,0.12)' : 'transparent',
              marginBottom: 6,
            }}
          >
            <Link
              to={`/${p.act}/${p.section}`}
              style={{ flex: 1, cursor: 'pointer', overflow: 'hidden', textDecoration: 'none' }}
              onClick={(e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); setAct(p.act); setActiveSection(p.section) }}
            >
              <div style={{ fontSize: 11, color: COLORS.accent, fontWeight: 600, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
                {p.act} › {p.section}
              </div>
              <div style={{ fontSize: 12, color: COLORS.text, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
                {p.title}
              </div>
            </Link>
            <button
              onClick={() => unpin(p)}
              style={{
                background: 'none', border: 'none', color: COLORS.textMuted,
                cursor: 'pointer', fontSize: 18, padding: '2px 4px', lineHeight: 1,
              }}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
