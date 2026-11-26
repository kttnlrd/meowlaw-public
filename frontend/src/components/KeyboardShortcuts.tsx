
import React from 'react';
import { COLORS } from './common/types';

type KeyboardShortcutsProps = {
  showShortcuts: boolean;
  setShowShortcuts: (show: boolean) => void;
};

export default function KeyboardShortcuts({ showShortcuts, setShowShortcuts }: KeyboardShortcutsProps) {
  if (!showShortcuts) return null;

  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
        zIndex: 200, display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
      onClick={() => setShowShortcuts(false)}
    >
      <div
        style={{
          background: COLORS.surface, border: `1px solid ${COLORS.border}`,
          borderRadius: 8, padding: 24, maxWidth: 400, width: '90%',
        }}
        onClick={e => e.stopPropagation()}
      >
        <h2 style={{ color: COLORS.heading, marginTop: 0, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", fontSize: 18 }}>
          Keyboard Shortcuts
        </h2>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 16 }}>
          {[
            ['j', 'Next section'],
            ['k', 'Previous section'],
            ['/', 'Focus search'],
            ['Esc', 'Close drawer / blur search'],
            ['?', 'Toggle this help'],
          ].map(([key, desc]) => (
            <div key={key} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', color: COLORS.text, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", fontSize: 13 }}>
              <span>{desc}</span>
              <kbd style={{ background: COLORS.bg, padding: '2px 8px', borderRadius: 4, border: `1px solid ${COLORS.border}`, color: COLORS.heading, fontFamily: 'monospace', fontSize: 12 }}>
                {key}
              </kbd>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
