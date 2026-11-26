// Theme registry.
//
// A theme is a set of the canonical --color-* vars (+ --font-ui/--font-body/--font-mono),
// defined either in tokens.css ([data-theme] block, built-in themes) or in a
// theme-pack CSS file under public/themes/ (loaded dynamically, see `css`).
//
// Add a theme pack by dropping its .css into public/themes/ and adding an entry here.

export interface ThemeDef {
  id: string
  label: string
  /** Per-user accent/font overrides apply. Fixed presets ignore them. */
  customizable: boolean
  /** Theme-pack CSS file loaded dynamically when this theme is active (public/themes/<css>). */
  css?: string
  /** Theme-pack JS file loaded dynamically when this theme is active (public/themes/<js>). */
  js?: string
}

export const THEMES: ThemeDef[] = [
  { id: 'dark', label: 'Dark', customizable: true },
  { id: 'light', label: 'Light', customizable: true },
  { id: 'classic', label: 'Classic (AustLII)', customizable: false },
  { id: 'modern', label: 'Modern (AustLII)', customizable: false },
  { id: 'federal-register', label: 'Federal Register', customizable: false, css: 'federal-register.css' },
  { id: 'terminal', label: 'Terminal', customizable: false, css: 'terminal.css' },
  { id: 'laser-pointer', label: 'Laser Pointer', customizable: false, css: 'laser-pointer.css' },
  { id: 'barristers-chambers', label: "Barrister's Chambers", customizable: false, css: 'barristers-chambers.css' },
  { id: 'hairball', label: 'Hairball', customizable: false, css: 'hairball.css' },
  { id: 'comic-book', label: 'Comic Book', customizable: false, css: 'comic-book.css' },
  { id: 'classic-mac', label: 'Classic Mac', customizable: false, css: 'classic-mac.css' },
  { id: 'modern-mac', label: 'Modern Mac', customizable: false, css: 'modern-mac.css' },
  { id: 'windows-95', label: 'Windows 95', customizable: false, css: 'windows-95.css' },
  { id: 'windows-xp', label: 'Windows XP', customizable: false, css: 'windows-xp.css' },
  { id: 'windows-vista', label: 'Windows Vista', customizable: false, css: 'windows-vista.css' },
  { id: 'simpsons', label: 'Simpsons', customizable: false, css: 'simpsons.css' },
  { id: 'squishy', label: 'Squishy', customizable: false, css: 'squishy.css', js: 'squishy.js' },
]

export const DEFAULT_THEME = 'dark'

export function themeDef(id: string): ThemeDef | undefined {
  return THEMES.find((t) => t.id === id)
}
