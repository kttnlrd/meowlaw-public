// Shared types and theme for the lawkitty frontend

export const COLORS = {
  bg: 'var(--color-bg)',
  surface: 'var(--color-surface)',
  surfaceHover: 'var(--color-surface-hover)',
  border: 'var(--color-border)',
  text: 'var(--color-text)',
  textMuted: 'var(--color-text-muted)',
  accent: 'var(--color-accent)',
  accentHover: 'var(--color-accent-hover)',
  heading: 'var(--color-heading)',
}

export type Section = { id: string; title: string; path: string; ato_url?: string; download_url?: string }
export type Subdivision = { id: string; title: string; sections: Section[] }
export type Division = { id: string; title: string; subdivisions: Subdivision[]; sections: Section[] }
export type Part = { id: string; title: string; divisions: Division[]; sections: Section[]; lazy?: boolean; count?: number }
export type Signpost = { id: string; title: string; is_signpost: true }
/** Licence + attribution a document's view must display (data/<act or source>/licence.json). */
export type Licence = {
  jurisdiction: string
  notice_line?: string | null
  notice_badge?: string | null
  attribution?: string | null
  disclaimer?: string | null
  unofficial_notice?: string | null
  endorsement_notice?: string | null
  licence: { name: string; url: string }
  source_url: string
  version_date?: string | null
  terms_url: string
}
export type Tree = { act: string; parts: (Part | Division | Signpost)[]; licence?: Licence }

export type PinItem = { act: string; section: string; title: string }
export type HistoryItem = { act: string; section: string; title: string; timestamp: number }
