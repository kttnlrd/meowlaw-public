import React, { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { THEMES, DEFAULT_THEME, themeDef } from './themes'

const FONTS = {
  heading: ['Figtree', 'ui-sans-serif', 'system-ui', 'sans-serif'],
  body: ['Figtree', 'ui-sans-serif', 'system-ui', 'sans-serif'],
}

export interface UserPrefs {
  display_name: string
  default_act: string
  theme: string
  accent_color: string
  text_color: string
  bg_color: string
  heading_font: string
  body_font: string
}

interface ThemeContextValue {
  theme: string
  themes: typeof THEMES
  isCustomizable: boolean
  accentColor: string
  headingFont: string
  bodyFont: string
  userPrefs: UserPrefs | null
  setTheme: (t: string) => void
  setAccentColor: (c: string) => void
  setHeadingFont: (f: string) => void
  setBodyFont: (f: string) => void
  setDisplayName: (n: string) => void
  setDefaultAct: (a: string) => void
  resetTheme: () => void
  refreshPrefs: () => Promise<void>
  savePrefs: (updates: Partial<UserPrefs>) => Promise<void>
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

const DEFAULT_PREFS: UserPrefs = {
  display_name: '',
  default_act: 'itaa-1997',
  theme: DEFAULT_THEME,
  accent_color: '#c6f432',
  text_color: '',
  bg_color: '',
  heading_font: 'Figtree',
  body_font: 'Figtree',
}

// Load/remove the dynamically-served theme-pack CSS (public/themes/<file>).
function applyThemePackCss(cssFile: string | undefined) {
  const existing = document.getElementById('lk-theme-pack') as HTMLLinkElement | null
  if (!cssFile) {
    if (existing) existing.remove()
    return
  }
  const href = `/app/themes/${cssFile}?v=10`
  if (existing) {
    if (existing.getAttribute('href') === href) return
    existing.remove()
  }
  const link = document.createElement('link')
  link.id = 'lk-theme-pack'
  link.rel = 'stylesheet'
  link.href = href
  document.head.appendChild(link)
}

// Load/remove the dynamically-served theme-pack JS (public/themes/<file>).
function applyThemePackJs(jsFile: string | undefined) {
  const existing = document.getElementById('lk-theme-pack-js') as HTMLScriptElement | null
  if (!jsFile) {
    if (existing) existing.remove()
    return
  }
  const src = `/app/themes/${jsFile}?v=10`
  if (existing) {
    if (existing.getAttribute('src') === src) return
    existing.remove()
  }
  const script = document.createElement('script')
  script.id = 'lk-theme-pack-js'
  script.src = src
  document.head.appendChild(script)
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [userPrefs, setUserPrefs] = useState<UserPrefs | null>(() => {
    try {
      const cached = localStorage.getItem('legislation-user-prefs')
      return cached ? JSON.parse(cached) : null
    } catch { return null }
  })

  const theme = userPrefs?.theme || DEFAULT_THEME
  const def = themeDef(theme)
  const isCustomizable = def?.customizable ?? true

  const accentColor = userPrefs?.accent_color || '#c6f432'
  const headingFont = FONTS.heading.includes(userPrefs?.heading_font || '') ? userPrefs!.heading_font : 'Figtree'
  const bodyFont = FONTS.body.includes(userPrefs?.body_font || '') ? userPrefs!.body_font : 'Figtree'

  const setTheme = useCallback((t: string) => {
    setUserPrefs(prev => {
      const next = { ...(prev || DEFAULT_PREFS), theme: t }
      localStorage.setItem('legislation-user-prefs', JSON.stringify(next))
      return next
    })
  }, [])

  const setAccentColor = useCallback((c: string) => {
    setUserPrefs(prev => {
      const next = { ...(prev || DEFAULT_PREFS), accent_color: c }
      localStorage.setItem('legislation-user-prefs', JSON.stringify(next))
      return next
    })
  }, [])

  const setHeadingFont = useCallback((f: string) => {
    setUserPrefs(prev => {
      const next = { ...(prev || DEFAULT_PREFS), heading_font: f }
      localStorage.setItem('legislation-user-prefs', JSON.stringify(next))
      return next
    })
  }, [])

  const setBodyFont = useCallback((f: string) => {
    setUserPrefs(prev => {
      const next = { ...(prev || DEFAULT_PREFS), body_font: f }
      localStorage.setItem('legislation-user-prefs', JSON.stringify(next))
      return next
    })
  }, [])

  const setDisplayName = useCallback((n: string) => {
    setUserPrefs(prev => {
      const next = { ...(prev || DEFAULT_PREFS), display_name: n }
      localStorage.setItem('legislation-user-prefs', JSON.stringify(next))
      return next
    })
  }, [])

  const setDefaultAct = useCallback((a: string) => {
    setUserPrefs(prev => {
      const next = { ...(prev || DEFAULT_PREFS), default_act: a }
      localStorage.setItem('legislation-user-prefs', JSON.stringify(next))
      return next
    })
  }, [])

  const resetTheme = useCallback(() => {
    const defaults = { ...DEFAULT_PREFS }
    setUserPrefs(defaults)
    localStorage.setItem('legislation-user-prefs', JSON.stringify(defaults))
  }, [])

  const refreshPrefs = useCallback(async () => {
    try {
      const r = await fetch('/api/user/prefs')
      if (r.ok) {
        const data = await r.json()
        setUserPrefs(data)
        localStorage.setItem('legislation-user-prefs', JSON.stringify(data))
      }
    } catch {}
  }, [])

  const savePrefs = useCallback(async (updates: Partial<UserPrefs>) => {
    try {
      const r = await fetch('/api/user/prefs', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates),
      })
      if (r.ok) {
        const data = await r.json()
        setUserPrefs(data)
        localStorage.setItem('legislation-user-prefs', JSON.stringify(data))
      }
    } catch {}
  }, [])

  // Sync from backend on mount (if logged in)
  useEffect(() => {
    refreshPrefs()
  }, [refreshPrefs])

  // Apply the active theme: data-theme attr + per-user accent/font overrides
  // (customizable themes only) + dynamic theme-pack CSS.
  useEffect(() => {
    const root = document.documentElement
    root.setAttribute('data-theme', theme)
    applyThemePackCss(def?.css)
    applyThemePackJs(def?.js)

    // Clear the first-paint FLASH inline colors (index.html sets them from localStorage,
    // which can lag the backend theme) so the CSS [data-theme] blocks drive bg/text.
    root.style.removeProperty('--color-bg')
    root.style.removeProperty('--color-text')

    if (isCustomizable) {
      root.style.setProperty('--color-accent', accentColor)
      root.style.setProperty('--font-ui', headingFont)
      root.style.setProperty('--font-body', bodyFont)
    } else {
      ;['--color-accent', '--font-ui', '--font-body'].forEach((p) => root.style.removeProperty(p))
    }
  }, [theme, def, isCustomizable, accentColor, headingFont, bodyFont])

  const value: ThemeContextValue = {
    theme,
    themes: THEMES,
    isCustomizable,
    accentColor,
    headingFont,
    bodyFont,
    userPrefs,
    setTheme,
    setAccentColor,
    setHeadingFont,
    setBodyFont,
    setDisplayName,
    setDefaultAct,
    resetTheme,
    refreshPrefs,
    savePrefs,
  }

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within ThemeProvider')
  return ctx
}

export { FONTS }
