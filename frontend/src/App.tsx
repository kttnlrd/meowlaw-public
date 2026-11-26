import React, { useEffect, useState, useRef, useMemo } from 'react'
import { Routes, Route, Link, useNavigate, useLocation, useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { api } from './api'
import { Tree, PinItem, COLORS } from './components/common/types'
import { TreeNode, findExpandedIds } from './components/TreeNode'
import MCPModal from './components/MCPModal'
import KeyboardShortcuts from './components/KeyboardShortcuts'
import PinnedTabs from './components/PinnedTabs'
import SmartLinkPanel from './components/SmartLinkPanel'
import DefinitionPopover from './components/DefinitionPopover'
import DefinitionsBrowser from './components/DefinitionsBrowser'
import SectionContent, { LicenceNotice } from './components/SectionContent'
import RulingContent from './components/RulingContent'
import PrivateRulingsBrowser from './components/PrivateRulingsBrowser'
import PrivateRulingContent from './components/PrivateRulingContent'
import RegulatoryGuideContent from './components/RegulatoryGuideContent'
import AfsaGuideContent from './components/AfsaGuideContent'
import TaxCaseContent from './components/TaxCaseContent'
import AdminPanel from './components/AdminPanel'
import AccountPanel from './components/AccountPanel'
import McpTokenBanner from './components/McpTokenBanner'
import MapView from './components/MapView'
import IssuesModal from './components/IssuesModal'
import SearchPanel, { warmSearch } from './components/SearchPanel'
import YarnBall from './components/YarnBall'
import TreatyContent from './components/TreatyContent'
import { ThemeProvider } from './ThemeContext'
import { shortActName, rulingSlug } from './utils/display'
import { buildSourceGroups } from './utils/sourceGroups'
import ChainsView from './components/ChainsView'
import CommentaryBrowse from './components/CommentaryBrowse'
import InsolvencyBrowse from './components/InsolvencyBrowse'
import NzMtgBrowse from './components/NzMtgBrowse'

// Domain groupings for the act picker
// All individual treaty country slugs — kept for isTreaty() / routing
const TREATY_SLUGS = [
  'argentina', 'austria', 'belgium', 'canada', 'chile', 'china', 'czech-republic',
  'denmark', 'fiji', 'finland', 'france', 'hungary', 'iceland', 'india', 'indonesia',
  'ireland', 'israel', 'italy', 'kiribati', 'korea', 'malaysia', 'malta', 'mexico',
  'netherlands', 'new-zealand', 'norway', 'papua-new-guinea', 'philippines', 'poland',
  'romania', 'russia', 'singapore', 'slovakia', 'south-africa', 'spain', 'sri-lanka',
  'sweden', 'taipei', 'thailand', 'turkey', 'usa', 'vietnam',
  'treaties',  // meta-act: shows country list in the tree
]
const TREATY_SET = new Set(TREATY_SLUGS)
const isTreaty = (id: string) => TREATY_SET.has(id) && id !== 'treaties'

// Sidebar source browser — three flat groups (no dropdown). Replaced the old
// act-picker dropdown at Harry's direction: "no drop down, instead the sidebar
// should list legislation, case law, resources".
// Routes matched by the generic /:act pattern that are not acts: /api/tree/<them> 404s, so they
// must be resolved to a real act before fetching. One list, used by both the tree effect and the
// browsingAct reset, because two copies had already drifted.
const NON_ACT_ROUTES = new Set(['search', 'admin', 'account', 'settings', 'definitions',
                                'rulings', 'nz-rulings', 'tax-cases', 'private-rulings'])
// private-rulings is excluded: its pane renders only while browsingAct is true.
const NOT_AN_ACT_PANE = new Set([...NON_ACT_ROUTES].filter(a => a !== 'private-rulings'))

// Gated professional sources: own pages rendered from the path, never an act tree.
const PATH_VIEWS = new Set(['commentary', 'insolvency', 'nz-mtg'])

// Sidebar item id -> content-type key (/api/content-types). Only unambiguous items;
// legislation, search, account and admin are never hidden.
const NAV_CONTENT_KEY: Record<string, string> = {
  'rulings': 'ruling', 'nz-rulings': 'nz-ruling', 'tax-cases': 'case',
  'private-rulings': 'ato-private-ruling', 'treaties': 'treaty', 'maps': 'maps',
  'chains': 'chains', 'afsa-guides': 'afsa-guide', 'regulatory-guides': 'regulatory-guide',
  'commentary': 'commentary', 'insolvency': 'insolvency', 'nz-mtg': 'nz-mtg',
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const DICT_SECTIONS = new Set(['995-1', '195-1', '6'])

function isDefinitionLink(href?: string) {
  if (!href) return false
  const m = href.match(/\/([a-z0-9-]+)\/s([^#]+)(?:#(.+))?/)
  if (!m) return false
  return DICT_SECTIONS.has(m[2])
}

// ---------------------------------------------------------------------------
// Route → state sync (F-16)
// ---------------------------------------------------------------------------
// react-router now owns the browser history; these tiny components replace
// the old popstate + regex parser. Each one mounts only when its route
// matches (cold load, back/forward, or a navigate() call) and pushes the
// route's params into the SAME state setters the rest of App already uses —
// the fetch/render logic downstream is completely unchanged.
type NavSetters = {
  setAct: (a: string) => void
  setActiveSection: (s: string) => void
  setActiveRuling: (r: string | null) => void
  setActivePrivateRuling: (r: string | null) => void
  setPrivateRulingsYear: (y: number | 'undated' | null) => void
  setSearchPage: (b: boolean) => void
  setActiveMap: (m: string | null) => void
  setActiveDefinitions: (d: string | null) => void
  setBrowsingAct: (b: boolean) => void
}

// /chains (and /commentary, /insolvency, /nz-mtg) render from the path; this only clears the other views.
function RouteChains({ n }: { n: NavSetters }) {
  useEffect(() => {
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    n.setActiveMap(null)
    n.setActiveSection('')
    n.setActiveRuling(null)
    n.setActivePrivateRuling(null)
  }, [])
  return null
}

function RouteHome({ n }: { n: NavSetters }) {
  useEffect(() => {
    n.setActiveMap(null)
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    n.setActiveSection('')
    n.setActiveRuling(null)
    n.setActivePrivateRuling(null)
    n.setBrowsingAct(false)
  }, [])
  return null
}

function RouteSearch({ n }: { n: NavSetters }) {
  useEffect(() => {
    n.setActiveMap(null)
    n.setActiveDefinitions(null)
    n.setActiveSection('')
    n.setActiveRuling(null)
    n.setActivePrivateRuling(null)
    n.setSearchPage(true)
  }, [])
  return null
}

function RouteDefinitions({ n }: { n: NavSetters }) {
  const { act } = useParams()
  useEffect(() => {
    n.setActiveDefinitions(act || '')
    n.setActiveMap(null)
    n.setSearchPage(false)
    n.setActiveSection('')
    n.setActiveRuling(null)
    n.setActivePrivateRuling(null)
  }, [act])
  return null
}

function RouteMaps({ n }: { n: NavSetters }) {
  const { id } = useParams()
  useEffect(() => {
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    if (id) {
      n.setActiveMap(decodeURIComponent(id))
      n.setActiveSection('')
      n.setActiveRuling(null)
    } else {
      n.setActiveMap(null)
      n.setActiveSection('')
      n.setActiveRuling(null)
      n.setAct('maps')
      n.setBrowsingAct(true)
    }
  }, [id])
  return null
}

function RoutePrivateRulingsIndex({ n }: { n: NavSetters }) {
  useEffect(() => {
    n.setActiveMap(null)
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    n.setAct('private-rulings')
    n.setActiveSection('')
    n.setActiveRuling(null)
    n.setActivePrivateRuling(null)
    n.setBrowsingAct(true)
  }, [])
  return null
}

function RoutePrivateRulingsYear({ n }: { n: NavSetters }) {
  const { year } = useParams()
  useEffect(() => {
    n.setActiveMap(null)
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    n.setAct('private-rulings')
    n.setActiveSection('')
    n.setActiveRuling(null)
    n.setActivePrivateRuling(null)
    n.setBrowsingAct(true)
    if (year) n.setPrivateRulingsYear(year === 'undated' ? 'undated' : Number(year))
  }, [year])
  return null
}

function RoutePrivateRuling({ n }: { n: NavSetters }) {
  const { authnum } = useParams()
  useEffect(() => {
    n.setActiveMap(null)
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    n.setAct('private-rulings')
    n.setActivePrivateRuling(authnum ? decodeURIComponent(authnum) : null)
    n.setActiveSection('')
    n.setActiveRuling(null)
    n.setBrowsingAct(true)
  }, [authnum])
  return null
}

function RouteRuling({ n }: { n: NavSetters }) {
  const { citation } = useParams()
  useEffect(() => {
    n.setActiveMap(null)
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    n.setAct('rulings')
    n.setActiveRuling(citation ? decodeURIComponent(citation) : null)
    n.setActiveSection('')
    n.setActivePrivateRuling(null)
  }, [citation])
  return null
}

function RouteTaxCase({ n }: { n: NavSetters }) {
  const { slug } = useParams()
  useEffect(() => {
    n.setActiveMap(null)
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    n.setAct('tax-cases')
    n.setActiveSection(slug ? decodeURIComponent(slug) : '')
    n.setActiveRuling(null)
    n.setActivePrivateRuling(null)
  }, [slug])
  return null
}

function RouteActOnly({ n }: { n: NavSetters }) {
  const { act } = useParams()
  useEffect(() => {
    n.setActiveMap(null)
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    if (act) n.setAct(act)
    n.setActiveSection('')
    n.setActiveRuling(null)
    n.setActivePrivateRuling(null)
    n.setBrowsingAct(true)
  }, [act])
  return null
}

function RouteActSection({ n }: { n: NavSetters }) {
  const { act, section } = useParams()
  useEffect(() => {
    n.setActiveMap(null)
    n.setActiveDefinitions(null)
    n.setSearchPage(false)
    if (act) n.setAct(act)
    const raw = section ? decodeURIComponent(section) : ''
    // Strip leading 's' prefix from section id for defense-in-depth (ROUTE-001).
    const cleaned = raw.replace(/^s(?=\d)/, '')
    n.setActiveSection(cleaned)
    n.setActiveRuling(null)
    n.setActivePrivateRuling(null)
  }, [act, section])
  return null
}

// ---------------------------------------------------------------------------
// Error boundary — a render-phase crash must never blank the whole screen
// ---------------------------------------------------------------------------
class ErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) { return { error } }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 40, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", color: COLORS.text, background: COLORS.bg, minHeight: '100vh' }}>
          <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 8 }}>Something went wrong</div>
          <div style={{ fontSize: 12, color: COLORS.textMuted, marginBottom: 16, fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>{this.state.error.message}</div>
          <button
            onClick={() => { this.setState({ error: null }); window.location.reload() }}
            style={{ padding: '6px 14px', borderRadius: 6, border: '1px solid ' + COLORS.border, background: COLORS.surface, color: COLORS.text, cursor: 'pointer', fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}
          >Reload</button>
        </div>
      )
    }
    return this.props.children
  }
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App() {
  const navigate = useNavigate()
  const location = useLocation()
  const [act, setAct] = useState('itaa-1997')
  const [tree, setTree] = useState<Tree | null>(null)
  const [acts, setActs] = useState<any[]>([])
  // The chains button only appears when tax-encode answers: no dead entry point.
  const [chainsUp, setChainsUp] = useState(false)
  const [activeSection, setActiveSection] = useState('')
  const [sectionData, setSectionData] = useState<any>(null)
  const [search, setSearch] = useState('')
  const [searchResults, setSearchResults] = useState<any[]>([])
  const [error, setError] = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [isMobile, setIsMobile] = useState(false)
  const [browsingAct, setBrowsingAct] = useState(false)
  // browsingAct latched on leaving an act page, so /search kept rendering the previous act's
  // tree instead of its own pane. Re-derive it whenever the route stops being an act.
  useEffect(() => {
    if (!act || NOT_AN_ACT_PANE.has(act)) setBrowsingAct(false)
  }, [act])

  // Sidebar width with localStorage persistence
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    try {
      const saved = localStorage.getItem('legislation-sidebar-width')
      return saved ? Math.max(280, Math.min(600, parseInt(saved, 10))) : 400
    } catch { return 400 }
  })
  const [isResizing, setIsResizing] = useState(false)

  const [activeRuling, setActiveRuling] = useState<string | null>(null)
  const [rulingData, setRulingData] = useState<any>(null)
  const [activePrivateRuling, setActivePrivateRuling] = useState<string | null>(null)
  const [privateRulingData, setPrivateRulingData] = useState<any>(null)
  const [privateRulingsYear, setPrivateRulingsYear] = useState<number | 'undated' | null>(null)
  // Lazy month → ruling sections injected into the sidebar tree for private rulings
  const [prMonthSections, setPrMonthSections] = useState<Record<string, { id: string; title: string; path: string }[]>>({})
  const [prExpanded, setPrExpanded] = useState<Set<string>>(new Set())
  const [casesData, setCasesData] = useState<any>(null)
  const [rulingsForSectionData, setRulingsForSectionData] = useState<any>(null)

  const [mcpOpen, setMcpOpen] = useState(false)
  const [searchPage, setSearchPage] = useState(false)
  const [homeQuery, setHomeQuery] = useState('')
  const [showShortcuts, setShowShortcuts] = useState(false)
  const [searchResultsCount, setSearchResultsCount] = useState(0)
  const [pins, setPins] = useState<PinItem[]>(() => {
    try { return JSON.parse(localStorage.getItem('legislation-pins') || '[]') }
    catch { return [] }
  })

  const [appInfo, setAppInfo] = useState<any>(null)
  const [user, setUser] = useState<any>(null)
  const [authLoading, setAuthLoading] = useState(true)

  useEffect(() => {
    api.info().then(setAppInfo).catch(() => {})
  }, [])

  useEffect(() => {
    fetch('/auth/me')
      .then(r => r.ok ? r.json() : null)
      .then(u => { setUser(u); setAuthLoading(false) })
      .catch(() => { setUser(null); setAuthLoading(false) })
  }, [])

  // null until loaded (or on failure): show everything rather than flash-hide nav.
  const [availableTypes, setAvailableTypes] = useState<Set<string> | null>(null)
  useEffect(() => {
    if (!user) return
    fetch('/api/content-types')
      .then(r => (r.ok ? r.json() : null))
      .then((rows: { key: string; available: boolean }[] | null) => {
        if (Array.isArray(rows)) setAvailableTypes(new Set(rows.filter(t => t.available).map(t => t.key)))
      })
      .catch(() => {})
  }, [user])
  const navAllowed = (id: string) => !availableTypes || !NAV_CONTENT_KEY[id] || availableTypes.has(NAV_CONTENT_KEY[id])

  const [accountOpen, setAccountOpen] = useState(false)
  const [issuesOpen, setIssuesOpen] = useState(false)
  const [changelogOpen, setChangelogOpen] = useState(false)
  const [activeMap, setActiveMap] = useState<string | null>(null)
  // Definitions browser: null = off, '' = act picker, 'itaa-1936' = act view
  const [activeDefinitions, setActiveDefinitions] = useState<string | null>(null)
  const [mapsList, setMapsList] = useState<any[] | null>(null)
  const [selectedRulingSection, setSelectedRulingSection] = useState<string | null>(null)

  // Bundled setters handed to the route-sync components (see top of file) —
  // each mounts only when its <Route> matches and pushes params into these.
  const navSetters: NavSetters = {
    setAct, setActiveSection, setActiveRuling, setActivePrivateRuling,
    setPrivateRulingsYear, setSearchPage, setActiveMap, setActiveDefinitions,
    setBrowsingAct,
  }

  // Pins
  const togglePin = () => {
    if (!activeSection || !sectionData) return
    const newPin = { act, section: activeSection, title: sectionData.frontmatter?.title || activeSection }
    const exists = pins.some(p => p.act === act && p.section === activeSection)
    const nextPins = exists
      ? pins.filter(p => !(p.act === act && p.section === activeSection))
      : [...pins, newPin]
    setPins(nextPins)
    localStorage.setItem('legislation-pins', JSON.stringify(nextPins))
  }
  const unpin = (pin: PinItem) => {
    const nextPins = pins.filter(p => !(p.act === pin.act && p.section === pin.section))
    setPins(nextPins)
    localStorage.setItem('legislation-pins', JSON.stringify(nextPins))
  }
  const isPinned = pins.some(p => p.act === act && p.section === activeSection)

  // Definition link popover
  const renderLink = (href?: string, children?: React.ReactNode) => {
    if (!isDefinitionLink(href)) return null
    const m = href!.match(/\/([a-z0-9-]+)\/s([^#]+)(?:#(.+))?/)
    const linkAct = m ? m[1] : act
    return (
      <DefinitionPopover
        act={linkAct}
        href={href}
        onNavigate={(section, anchor) => {
          if (linkAct === act) {
            setActiveSection(section)
            setActiveRuling(null)
            if (anchor) {
              setTimeout(() => {
                const el = document.getElementById(anchor)
                if (el) el.scrollIntoView({ behavior: 'smooth' })
              }, 150)
            }
          }
        }}
      >
        {children}
      </DefinitionPopover>
    )
  }

  // Navigation wrappers for child components
  const onNavigate = (targetAct: string, section: string, anchor?: string) => {
    setAct(targetAct)
    setActiveSection(section)
    setActiveRuling(null)
    setActivePrivateRuling(null)
    setSearchPage(false)
    if (anchor) {
      setTimeout(() => {
        const el = document.getElementById(anchor)
        if (el) el.scrollIntoView({ behavior: 'smooth' })
      }, 150)
    }
  }
  const onNavigateRuling = (citation: string) => {
    setActiveRuling(citation)
    setActiveSection('')
    setActivePrivateRuling(null)
    setSearchPage(false)
  }
  const onNavigateCase = (citation: string) => {
    navigate(`/tax-cases/${encodeURIComponent(citation)}`)
    setAct('tax-cases')
    setActiveSection(citation)
    setActiveRuling(null)
    setActivePrivateRuling(null)
    setSearchPage(false)
    setActiveMap(null)
  }

  // Open a source from the sidebar list (mirrors the old act-picker selectAct).
  const openSource = (id: string) => {
    setSearchPage(false)
    setActiveSection('')
    setActiveRuling(null)
    setActivePrivateRuling(null)
    setActiveMap(null)
    setSectionData(null)
    if (id === 'definitions') {
      setActiveDefinitions('')
      setBrowsingAct(false)
    } else if (PATH_VIEWS.has(id)) {
      // Rendered from the path, not an act: setAct would fetch /api/tree/<id> and 404.
      setActiveDefinitions(null)
      setBrowsingAct(false)
    } else {
      setAct(id)
      setActiveDefinitions(null)
      setBrowsingAct(true)
    }
    if (isMobile) setDrawerOpen(false)
  }

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === '?' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault()
        setShowShortcuts(s => !s)
      } else if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        // "/" opens search (advertised by the kbd hints and the shortcuts sheet)
        const t = e.target as HTMLElement | null
        if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
        e.preventDefault()
        setSearchPage(true)
        navigate('/search')
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  // Mobile detection
  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 768)
    check()
    window.addEventListener('resize', check)
    return () => window.removeEventListener('resize', check)
  }, [])

  // Load acts list
  useEffect(() => {
    api.acts().then(data => setActs(data)).catch(() => setActs([]))
    api.taxHealth().then((h: any) => setChainsUp(Boolean(h && (h.ok || h.status === 'ok')))).catch(() => setChainsUp(false))
  }, [])

  // Load procedural maps list (for the act picker domain)
  useEffect(() => {
    fetch('/api/maps')
      .then(r => (r.ok ? r.json() : []))
      .then(setMapsList)
      .catch(() => setMapsList([]))
  }, [])

  // Load tree when act changes — use ref to avoid stale-race from default act
  const treeGenRef = useRef(0)
  useEffect(() => {
    const gen = ++treeGenRef.current
    setTree(null)

    // Treaties hub: nested country -> article tree (countries expandable)
    if (act === 'treaties') {
      api.treatyFullTree().then(data => {
        if (gen !== treeGenRef.current) return
        setTree({
          act: 'treaties',
          parts: [{
            id: 'treaties',
            title: 'Double Tax Agreements',
            divisions: (data.countries || []).map((c: any) => ({
              id: c.slug,
              title: c.treaty,
              subdivisions: [],
              sections: (c.articles || []).map((a: any) => ({
                id: `${c.slug}/${a.article}`,
                title: a.title,
                path: a.slug,
              })),
            })),
            sections: [],
          }],
        } as Tree)
        setError('')
      }).catch(e => {
        if (gen === treeGenRef.current) setError(e.message)
      })
      setDrawerOpen(false)
      return
    }

    // Maps hub: handled by its own effect (needs mapsList)
    if (act === 'maps') {
      setTree(null)
      setDrawerOpen(false)
      return
    }

    // Some routes are not acts at all — /search is one, and its first path segment ('search')
    // 404s against /api/tree/. Fetching it left the tree null, so the whole app rendered
    // "Loading..." forever after going back to a search page from a ruling. Resolve the route
    // to a real act BEFORE fetching (a fallback after the failure still raced the guard).
    const treeAct = (!act || NON_ACT_ROUTES.has(act)) ? 'itaa-1997' : act
    const load = isTreaty(treeAct) ? api.treatyTree(treeAct) : api.tree(treeAct)
    load.then(data => {
      if (gen !== treeGenRef.current) return
      if (isTreaty(treeAct)) {
        setTree({
          // label with the act whose tree was actually fetched, not the raw route segment
          act: treeAct,
          parts: [{
            id: treeAct,
            title: data.treaty,
            divisions: [],
            sections: (data.articles || []).map((a: any) => ({ id: String(a.article), title: a.title, path: a.slug })),
          }],
        } as Tree)
      } else {
        setTree(data)
      }
      setError('')
    }).catch(e => {
      if (gen === treeGenRef.current) setError(e.message)
    })
    setDrawerOpen(false)
  }, [act])

  // -------------------------------------------------------------------------
  // Private rulings tree: year → month → lazy ruling list
  // -------------------------------------------------------------------------
  const PR_MONTH_RE = /^(\d{4})-(\d{1,2})$/
  const PR_MORE_RE = /^__more__:(.+):(\d+)$/
  const PR_PAGE = 200

  const loadPrivateRulingsMonth = (monthId: string, offset = 0) => {
    let req: Promise<any>
    if (monthId === 'undated-all') {
      req = api.privateRulingsUndated(PR_PAGE, offset)
    } else {
      const m = PR_MONTH_RE.exec(monthId)
      if (!m) return
      req = api.privateRulingsByMonth(Number(m[1]), Number(m[2]), PR_PAGE, offset)
    }
    req.then(d => {
      const rulings = d.rulings || []
      const sections = rulings.map((r: any) => ({
        id: r.authnum,
        title: `${r.name || 'Untitled ruling'} — EV/${String(r.authnum).slice(-6)}`,
        path: r.authnum,
      }))
      const total = d.total || 0
      const next = offset + rulings.length
      if (next < total) {
        const moreId = `__more__:${monthId}:${next}`
        sections.push({ id: moreId, title: `Load more (${(total - next).toLocaleString()} remaining)`, path: moreId })
      }
      setPrMonthSections(prev => {
        const existing = (prev[monthId] || []).filter(s => !s.id.startsWith('__more__:'))
        return { ...prev, [monthId]: [...existing, ...sections] }
      })
      setPrExpanded(prev => new Set(prev).add(monthId))
      setError('')
    }).catch(e => setError(e.message))
  }

  // Inject loaded month sections into the tree for rendering
  const treeForRender = useMemo(() => {
    if (act !== 'private-rulings' || !tree || Object.keys(prMonthSections).length === 0) return tree
    const clone: Tree = JSON.parse(JSON.stringify(tree))
    for (const part of clone.parts) {
      for (const div of (part as any).divisions || []) {
        for (const sub of (div as any).subdivisions || []) {
          if (prMonthSections[sub.id]) {
            (sub as any).sections = prMonthSections[sub.id]
          }
        }
      }
    }
    return clone
  }, [tree, prMonthSections, act])

  // Merge auto-expanded ancestors with manually expanded private-ruling months
  const expandedIds = useMemo(() => {
    const s = activeSection ? findExpandedIds(treeForRender, activeSection) : new Set<string>()
    prExpanded.forEach(x => s.add(x))
    return s
  }, [treeForRender, activeSection, prExpanded])

  // Canonical href for a tree row's id, given the current act context — used
  // both by handleTreeSelect (for act-relative branches without their own
  // route, e.g. maps/private-ruling years) and to give TreeNode real <Link>
  // targets so right-click / middle-click / ctrl-click work (F-15).
  const sectionHref = (e: string): string | null => {
    if (act === 'maps') return `/maps/${e}`
    if (act === 'treaties') {
      const s = e.indexOf('/')
      return s > -1 ? `/${e.slice(0, s)}/${e.slice(s + 1)}` : `/${e}`
    }
    if (act === 'rulings' || act === 'nz-rulings') return `/rulings/${rulingSlug(e)}`
    if (act === 'private-rulings') {
      if (e.startsWith('__more__:') || PR_MONTH_RE.test(e) || e === 'undated-all') return null
      if (e === 'undated' || /^\d{4}$/.test(e)) return `/private-rulings/year/${e}`
      return `/private-rulings/${e}`
    }
    return `/${act}/${e}`
  }

  const handleTreeSelect = (e: string) => {
    setSearchPage(false)
    if (act === 'maps') {
      setActiveSection(''); setActiveRuling(null); setSectionData(null); setActiveMap(e)
      navigate(`/maps/${e}`)
    } else if (act === 'treaties') {
      const s = e.indexOf('/')
      if (s > -1) { setAct(e.slice(0, s)); setActiveSection(e.slice(s + 1)) } else { setAct(e); setActiveSection('') }
    } else if (act === 'rulings' || act === 'nz-rulings') {
      setActiveRuling(e)
    } else if (act === 'private-rulings') {
      if (e.startsWith('__more__:')) {
        const mm = PR_MORE_RE.exec(e)
        if (mm) loadPrivateRulingsMonth(mm[1], Number(mm[2]))
      } else if (PR_MONTH_RE.test(e) || e === 'undated-all') {
        loadPrivateRulingsMonth(e)
      } else if (e === 'undated' || /^\d{4}$/.test(e)) {
        setPrivateRulingsYear(e === 'undated' ? 'undated' : Number(e))
        setActivePrivateRuling(null)
        navigate(`/private-rulings/year/${e}`)
      } else {
        setActivePrivateRuling(e)
      }
      setActiveSection('')
    } else {
      setActiveSection(e)
    }
    if (isMobile) setDrawerOpen(false)
  }

  // Maps hub tree: act-grouped map list, same navigation pattern as any act
  useEffect(() => {
    if (act !== 'maps') return
    const MAP_ACT_LABELS: Record<string, string> = {
      'itaa-1997': 'Income Tax Assessment Act 1997',
      'itaa-1936': 'Income Tax Assessment Act 1936',
      'gst-1999': 'GST Act 1999',
      'taa-1953': 'Taxation Administration Act 1953',
      'fbt-1986': 'FBT Assessment Act 1986',
      'sis-1993': 'Superannuation Industry (Supervision) Act 1993',
    }
    const grouped = new Map<string, any[]>()
    for (const m of (mapsList || [])) {
      if (!grouped.has(m.act)) grouped.set(m.act, [])
      grouped.get(m.act)!.push(m)
    }
    setTree({
      act: 'maps',
      parts: [...grouped.entries()].map(([mapAct, ms]) => ({
        id: mapAct,
        title: MAP_ACT_LABELS[mapAct] || shortActName(mapAct),
        divisions: [],
        sections: ms.map(m => ({
          id: m.id,
          title: m.short ? (m.refs ? `${m.short} — ${m.refs}` : m.short) : m.title,
          path: m.id,
        })),
      })),
    } as Tree)
    setError('')
    setDrawerOpen(false)
  }, [act, mapsList])

  // Load section / ruling content
  useEffect(() => {
    if (!activeSection && !activeRuling && !activePrivateRuling) {
      setSectionData(null)
      setRulingData(null)
      setPrivateRulingData(null)
      setCasesData(null)
      setRulingsForSectionData(null)
      return
    }

    // Navigating into a section/ruling leaves the map page
    setActiveMap(null)

    if (activePrivateRuling) {
      api.privateRuling(activePrivateRuling)
        .then(data => { setPrivateRulingData(data); setError('') })
        .catch(e => { setPrivateRulingData(null); setError(e.message) })
      const path = `/private-rulings/${activePrivateRuling}`
      if (location.pathname !== path) navigate(path)
    } else if (activeRuling) {
      api.ruling(activeRuling)
        .then(data => { setRulingData(data); setError('') })
        .catch(e => { setRulingData(null); setError(e.message) })
      const path = `/rulings/${rulingSlug(activeRuling)}`
      if (location.pathname !== path) navigate(path)
    } else if (activeSection && isTreaty(act)) {
      api.treatyArticle(act, activeSection)
        .then(data => { setSectionData(data); setError('') })
        .catch(e => {
          if (e.message?.includes('404')) {
            setActiveSection('')
            setSectionData(null)
          } else {
            setError(e.message)
          }
        })
      const path = `/${act}/${activeSection}`
      if (location.pathname !== path) navigate(path)
    } else if (activeSection) {
      api.section(act, activeSection)
        .then(data => { setSectionData(data); setError('') })
        .catch(e => {
          if (e.message?.includes('404')) {
            setActiveSection('')
            setSectionData(null)
          } else {
            setError(e.message)
          }
        })
      api.cases(act, activeSection).then(setCasesData).catch(() => {})
      api.rulings(act, activeSection).then(setRulingsForSectionData).catch(() => {})
      const path = `/${act}/${activeSection}`
      if (location.pathname !== path) navigate(path)
    }
    if (isMobile) setDrawerOpen(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [act, activeSection, activeRuling, activePrivateRuling, isMobile])

  // Leaving the definitions browser: any in-app navigation to a section,
  // ruling or map closes it (route changes are handled by the route sync
  // components above via <Routes>).
  useEffect(() => {
    if (activeDefinitions !== null && (activeSection || activeRuling || activePrivateRuling || activeMap)) {
      setActiveDefinitions(null)
    }
  }, [activeDefinitions, activeSection, activeRuling, activePrivateRuling, activeMap])

  // Resize handlers
  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!isResizing) return
      const newWidth = Math.max(280, Math.min(600, e.clientX))
      setSidebarWidth(newWidth)
      localStorage.setItem('legislation-sidebar-width', String(newWidth))
    }
    const onMouseUp = () => setIsResizing(false)
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    return () => {
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onMouseUp)
    }
  }, [isResizing])

  const doSearch = async () => {
    if (!search.trim()) return
    const data = await api.search(search, act)
    setSearchResults(data.results)
  }

  // Route sync — rendered regardless of loading/error state below so a cold
  // load or Back/Forward navigation updates state even before the initial
  // tree fetch resolves (F-16). Renders nothing visible; see NavSetters above.
  const routeSync = (
    <Routes>
      <Route path="/" element={<RouteHome n={navSetters} />} />
      <Route path="/search" element={<RouteSearch n={navSetters} />} />
      <Route path="/admin" element={<RouteHome n={navSetters} />} />
      <Route path="/definitions" element={<RouteDefinitions n={navSetters} />} />
      <Route path="/definitions/:act" element={<RouteDefinitions n={navSetters} />} />
      <Route path="/definitions/:act/:term" element={<RouteDefinitions n={navSetters} />} />
      <Route path="/maps" element={<RouteMaps n={navSetters} />} />
      <Route path="/maps/:id" element={<RouteMaps n={navSetters} />} />
      <Route path="/chains" element={<RouteChains n={navSetters} />} />
      <Route path="/chains/:act/:section" element={<RouteChains n={navSetters} />} />
      <Route path="/commentary" element={<RouteChains n={navSetters} />} />
      <Route path="/commentary/:act/:section" element={<RouteChains n={navSetters} />} />
      <Route path="/insolvency" element={<RouteChains n={navSetters} />} />
      <Route path="/insolvency/:chapter" element={<RouteChains n={navSetters} />} />
      <Route path="/nz-mtg" element={<RouteChains n={navSetters} />} />
      <Route path="/nz-mtg/:id" element={<RouteChains n={navSetters} />} />
      <Route path="/private-rulings" element={<RoutePrivateRulingsIndex n={navSetters} />} />
      <Route path="/private-rulings/year/:year" element={<RoutePrivateRulingsYear n={navSetters} />} />
      <Route path="/private-rulings/:authnum" element={<RoutePrivateRuling n={navSetters} />} />
      <Route path="/rulings/:citation" element={<RouteRuling n={navSetters} />} />
      <Route path="/tax-cases/:slug" element={<RouteTaxCase n={navSetters} />} />
      <Route path="/:act" element={<RouteActOnly n={navSetters} />} />
      <Route path="/:act/:section" element={<RouteActSection n={navSetters} />} />
      <Route path="*" element={<RouteHome n={navSetters} />} />
    </Routes>
  )

  if (error) return <>{routeSync}<div style={{ padding: 20, color: '#ef4444' }}>Error: {error}</div></>
  if (!tree) return <>{routeSync}<div style={{ padding: 20, color: COLORS.textMuted }}>Loading...</div></>

  const mobileSidebarWidth = isMobile ? Math.min(window.innerWidth * 0.85, 380) : sidebarWidth
  const adminView = location.pathname === '/admin'
  const hasContent = !!(activeSection || activeRuling || activePrivateRuling || browsingAct || activeMap || activeDefinitions !== null)

  return (
    <ErrorBoundary>
    <ThemeProvider>
      <style>{`
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: ${COLORS.border}; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: ${COLORS.textMuted}; }
        * { scrollbar-width: thin; scrollbar-color: ${COLORS.border} transparent; }
      `}</style>
      {routeSync}
      <div style={{ display: 'flex', height: '100vh', background: 'var(--color-desktop, var(--color-bg))' }}>
      <>

      {/* Mobile close button — inside sidebar header (absolute positioned) */}
      {/* Mobile backdrop */}
      {isMobile && drawerOpen && (
        <div
          onClick={() => setDrawerOpen(false)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', zIndex: 90 }}
        />
      )}

      {/* Sidebar */}
      <div className="lk-sidebar" style={{
        width: mobileSidebarWidth,
        background: COLORS.surface,
        borderRight: `1px solid ${COLORS.border}`,
        display: 'flex', flexDirection: 'column',
        position: isMobile ? 'fixed' : 'relative',
        transform: isMobile
          ? (drawerOpen ? 'translateX(0)' : 'translateX(-101%)')
          : undefined,
        top: 0, bottom: 0, zIndex: 100,
        willChange: isMobile ? 'transform' : undefined,
        transition: isMobile ? 'transform 0.15s ease' : 'none',
      }}>
        {/* Sidebar header: act picker + mobile close button */}
        <header style={{ padding: isMobile ? '12px 14px' : '12px 14px', borderBottom: `1px solid ${COLORS.border}`, position: 'relative' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, paddingRight: isMobile && drawerOpen ? 36 : 0 }}>
            {hasContent ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, minHeight: 36 }}>
                <button
                  onClick={() => navigate('/')}
                  style={{
                    padding: '6px 10px', borderRadius: 'var(--radius-md, 14px)',
                    background: 'transparent', color: COLORS.accent,
                    border: `var(--border-chunky, 2px) solid var(--line-strong, ${COLORS.border})`,
                    fontSize: 12, fontWeight: 600, cursor: 'pointer',
                    fontFamily: "var(--font-ui, 'Figtree'), sans-serif", whiteSpace: 'nowrap',
                  }}
                >
                  ← Sources
                </button>
                <span style={{ fontSize: 13, fontWeight: 600, color: COLORS.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{shortActName(act)}</span>
              </div>
            ) : (
              <span style={{ fontSize: 11, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.8, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", padding: '6px 4px 2px' }}>Sources</span>
            )}
          </div>
          {isMobile && drawerOpen && (
            <button
              onClick={() => setDrawerOpen(false)}
              style={{
                position: 'absolute', top: 12, right: 14, zIndex: 200,
                background: 'transparent', color: COLORS.heading,
                border: 'none',
                fontSize: 20, cursor: 'pointer', lineHeight: 1,
                width: 36, height: 36,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              {'\u2715'}
            </button>
          )}
        </header>

        {/* Sidebar body: section tree (browsing) or source list (home) */}
        {hasContent ? (
          <div style={{ flex: 1, overflow: 'auto', padding: isMobile ? '6px 8px' : 8 }}>
            {(treeForRender?.parts || []).map(p => (
              <TreeNode key={p.id} node={p} level={0} activeSection={act === 'maps' ? (activeMap || '') : activeSection} onSelect={handleTreeSelect} getHref={sectionHref} isMobile={isMobile} expandedIds={expandedIds} act={act} />
            ))}
          </div>
        ) : (
          <div style={{ flex: 1, overflow: 'auto', padding: '12px 14px 16px' }}>
            {buildSourceGroups(acts, { proposedLaw: false, guides: false })
              .map(g => ({ ...g, items: g.items.filter(i => navAllowed(i.id)) }))
              .filter(g => g.items.length > 0)
              .map(g => (
              <section key={g.label} style={{ marginBottom: 18 }}>
                <h2 style={{ margin: '0 0 6px 8px', fontSize: 11, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.8, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>{g.label}</h2>
                <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                  {g.items.map(item => (
                    <li key={item.id}>
                      <Link
                        to={item.to}
                        onClick={() => openSource(item.id)}
                        style={{ display: 'block', padding: '5px 10px', borderRadius: 8, color: COLORS.text, textDecoration: 'none', fontSize: 13, lineHeight: '20px', fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}
                        onMouseEnter={e => { e.currentTarget.style.background = COLORS.bg }}
                        onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}
                      >
                        {item.label}
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
        )}

        {/* Sidebar bottom: search (dominant), then account / admin / bugs */}
        <footer style={{
          borderTop: `1px solid ${COLORS.border}`,
          padding: isMobile ? '12px' : '12px',
          display: 'flex', flexDirection: 'column', gap: 8,
        }}>
          <button
            onClick={() => { setSearchPage(true); navigate('/search'); if (isMobile) setDrawerOpen(false) }}
            onPointerEnter={warmSearch}
            onFocus={warmSearch}
            title="Search with advanced filters"
            style={{
              width: '100%', minHeight: 56, padding: '14px 16px', borderRadius: 'var(--radius-md, 14px)',
              background: COLORS.surface,
              color: COLORS.text,
              border: `var(--border-chunky, 2px) solid ${COLORS.accent}`, cursor: 'pointer',
              display: 'flex', alignItems: 'center', gap: 10,
              fontSize: 16, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", fontWeight: 700,
            }}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={COLORS.accent} strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <span style={{ flex: 1, textAlign: 'left' }}>Search</span>
            <span style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 12, color: COLORS.textMuted, border: `var(--border-chunky, 2px) solid ${COLORS.border}`, borderRadius: 6, padding: '2px 7px' }}>/</span>
          </button>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <button
              onClick={() => setAccountOpen(true)}
              title={user ? `Account & settings — ${user.email}` : 'Account & settings'}
              style={{
                padding: isMobile ? '7px 9px' : '6px 8px', borderRadius: 6,
                flex: 1, minWidth: 0,
                background: COLORS.bg, color: COLORS.text,
                border: `1px solid ${COLORS.border}`, cursor: 'pointer',
                display: 'flex', alignItems: 'center', gap: 6,
                fontSize: 11, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", fontWeight: 600,
              }}
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                <circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>
              </svg>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{user?.name || user?.email || 'Account'}</span>
            </button>
            {user?.is_admin && (
              <button
                onClick={() => { navigate('/admin'); if (isMobile) setDrawerOpen(false) }}
                title="Admin dashboard"
                style={{
                  padding: isMobile ? '7px 9px' : '6px 8px', borderRadius: 6,
                  background: location.pathname === '/admin' ? COLORS.accent : COLORS.bg,
                  color: location.pathname === '/admin' ? 'var(--on-catnip)' : COLORS.text,
                  border: `1px solid ${COLORS.border}`, cursor: 'pointer',
                  fontSize: 11, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", fontWeight: 500,
                }}
              >
                Admin
              </button>
            )}
            <button
              onClick={() => setIssuesOpen(true)}
              title="Report or view bugs"
              style={{
                padding: isMobile ? '7px 9px' : '6px 8px', borderRadius: 6,
                background: COLORS.bg, color: COLORS.textMuted,
                border: `1px solid ${COLORS.border}`, cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 5,
                fontSize: 11, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", fontWeight: 500,
              }}
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
              </svg>
              Bugs
            </button>
          </div>
        </footer>
      </div>

      {/* Resize handle */}
      {!isMobile && (
        <div
          className={`resize-handle${isResizing ? ' dragging' : ''}`}
          onMouseDown={() => setIsResizing(true)}
          style={{
            width: 4,
            background: isResizing ? '#c6f432' : 'transparent',
            position: 'relative',
            zIndex: 101,
            flexShrink: 0,
          }}
        />
      )}

      {/* Main content */}
      <main style={{
        flex: 1, overflow: 'auto',
        padding: isMobile ? '16px 12px 24px' : '20px 40px',
        paddingTop: isMobile ? (hasContent ? 12 : 16) : (hasContent ? 12 : 20),
        maxWidth: activeMap ? 1400 : adminView ? 1200 : 960, margin: '0 auto',
        fontFamily: "var(--font-body, 'Figtree'), serif",
        color: COLORS.text,
        display: 'flex', flexDirection: 'column',
        position: 'relative',
      }}>
        {isMobile && !drawerOpen && (
          <button
            onClick={() => setDrawerOpen(true)}
            title="Open sidebar"
            style={{
              position: 'absolute', top: 4, left: 4, zIndex: 60,
              background: COLORS.surface, color: COLORS.heading,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 6, padding: '7px 10px',
              cursor: 'pointer', lineHeight: 1, fontSize: 13,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>
        )}
        {/* MCP token prompt — hidden once a token exists or the user dismisses it */}
        <McpTokenBanner onGenerate={() => setAccountOpen(true)} />

        {/* Sticky search bar — removed in v3.0: search lives on /search with advanced filters */}
        {pins.length > 0 && (
          <PinnedTabs
            pins={pins}
            act={act}
            activeSection={activeSection}
            isMobile={isMobile}
            setAct={setAct}
            setActiveSection={setActiveSection}
            unpin={unpin}
          />
        )}

        {hasContent && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
            <button
              onClick={() => {
                setAct('')
                setActiveSection('')
                setActiveRuling(null)
                setActivePrivateRuling(null)
                setActiveMap(null)
                setActiveDefinitions(null)
                setSearchPage(false)
                navigate('/')
                if (isMobile) setDrawerOpen(false)
              }}
              aria-label="Home"
              title="Home"
              style={{
                padding: '6px 8px', borderRadius: 6,
                background: COLORS.surface, color: COLORS.textMuted,
                border: `1px solid ${COLORS.border}`, cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4,
              }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 10.5 12 3l9 7.5" />
                <path d="M5 9.5V21h14V9.5" />
              </svg>
              <span style={{ fontSize: 11 }}>Home</span>
            </button>
            <button
              onClick={() => {
                navigator.clipboard.writeText(window.location.href).catch(() => {})
              }}
              title="Copy link"
              style={{
                padding: '6px 8px', borderRadius: 6,
                background: COLORS.surface, color: COLORS.textMuted,
                border: `1px solid ${COLORS.border}`, cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
                <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
              </svg>
            </button>
            <button
              onClick={() => {
                setActiveDefinitions(act && ['itaa-1997','itaa-1936','gst-1999','corporations-act-2001','fbt-1986','taa-1953','sis-1993','aml-ctf-2006','nz-it-2007'].includes(act) ? act : '')
                setSearchPage(false)
                setActiveSection('')
                setActiveRuling(null)
                setActivePrivateRuling(null)
                setActiveMap(null)
                navigate('/definitions')
                if (isMobile) setDrawerOpen(false)
              }}
              aria-label="Definitions"
              title="Browse defined terms"
              style={{
                padding: '6px 8px', borderRadius: 6,
                background: activeDefinitions !== null ? COLORS.accent : COLORS.surface,
                color: activeDefinitions !== null ? 'var(--on-catnip)' : COLORS.textMuted,
                border: `1px solid ${COLORS.border}`, cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4,
              }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="4" y1="9" x2="20" y2="9"/>
                <line x1="4" y1="15" x2="20" y2="15"/>
                <line x1="10" y1="3" x2="8" y2="21"/>
                <line x1="16" y1="3" x2="14" y2="21"/>
              </svg>
              <span style={{ fontSize: 11 }}>Definitions</span>
              </button>

              {chainsUp && navAllowed('chains') && (
              <button
                onClick={() => {
                  setActiveDefinitions(null)
                  setSearchPage(false)
                  setActiveMap(null)
                  setActiveSection('')
                  navigate('/chains')
                  if (isMobile) setDrawerOpen(false)
                }}
                aria-label="Interpretive chains"
                title="Interpretive chains (alpha): a provision's limbs, the facts each limb gates, and what it feeds"
                style={{
                  padding: '6px 8px', borderRadius: 6,
                  background: location.pathname.startsWith('/chains') ? COLORS.accent : COLORS.surface,
                  color: location.pathname.startsWith('/chains') ? 'var(--on-catnip)' : COLORS.textMuted,
                  border: `1px solid ${COLORS.border}`, cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4,
                }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M6 3v6a3 3 0 0 0 3 3h6a3 3 0 0 1 3 3v6" />
                  <circle cx="6" cy="3" r="1.6" />
                  <circle cx="18" cy="21" r="1.6" />
                  <circle cx="12" cy="12" r="1.6" />
                </svg>
                <span style={{ fontSize: 11 }}>Chains</span>
              </button>
              )}

          </div>
        )}

        {adminView ? (
          <AdminPanel />
        ) : searchPage ? (
          <div style={{ marginTop: 4 }}>
            <SearchPanel
              acts={acts}
              onNavigate={(targetAct, section) => {
                if (targetAct === 'tax-cases') {
                  onNavigateCase(section)
                } else if (targetAct === 'private-rulings') {
                  setAct(targetAct)
                  setSearchPage(false)
                  setActivePrivateRuling(section)
                  setActiveSection('')
                  setActiveRuling(null)
                } else {
                  setAct(targetAct)
                  setSearchPage(false)
                  if (targetAct === 'rulings' || targetAct === 'nz-rulings') {
                    setActiveRuling(section)
                    setActiveSection('')
                  } else {
                    setActiveSection(section)
                    setActiveRuling(null)
                  }
                }
              }}
              isMobile={isMobile}
              onResultsChange={setSearchResultsCount}
            />
          </div>
        ) : location.pathname.startsWith('/chains') ? (
          <ChainsView isMobile={isMobile} />
        ) : /^\/commentary(\/|$)/.test(location.pathname) ? (
          <CommentaryBrowse isMobile={isMobile} />
        ) : /^\/insolvency(\/|$)/.test(location.pathname) ? (
          <InsolvencyBrowse isMobile={isMobile} />
        ) : /^\/nz-mtg(\/|$)/.test(location.pathname) ? (
          <NzMtgBrowse isMobile={isMobile} />
        ) : activeDefinitions !== null ? (
          <DefinitionsBrowser
            act={activeDefinitions}
            onSelectAct={(a) => {
              setActiveDefinitions(a)
              navigate(a ? `/definitions/${a}` : '/definitions')
            }}
            onNavigate={(a, s, anchor) => {
              setActiveDefinitions(null)
              onNavigate(a, s, anchor)
            }}
          />
        ) : activeMap ? (
          <MapView
            mapId={activeMap}
            onClose={() => {
              const back = act === 'maps' ? '/maps' : (act ? `/${act}` : '/itaa-1997')
              setActiveMap(null)
              navigate(back)
            }}
            onOpenSection={(a, s) => {
              setActiveMap(null)
              if (a === 'rulings' || a === 'nz-rulings') {
                onNavigateRuling(s)
              } else {
                onNavigate(a, s)
              }
            }}
            height="calc(100vh - 150px)"
            isMobile={isMobile}
          />
        ) : activePrivateRuling && privateRulingData ? (
          <PrivateRulingContent
            data={privateRulingData}
            isMobile={isMobile}
            renderLink={renderLink}
            onNavigate={onNavigate}
            onNavigateRuling={onNavigateRuling}
            onNavigateCase={onNavigateCase}
          />
        ) : activeRuling && rulingData ? (
          <RulingContent
            rulingData={rulingData}
            isMobile={isMobile}
            renderLink={renderLink}
            onNavigate={onNavigate}
            onNavigateRuling={onNavigateRuling}
          />
        ) : act === 'regulatory-guides' && sectionData ? (
          <RegulatoryGuideContent
            sectionData={sectionData}
            isMobile={isMobile}
          />
        ) : act === 'afsa-guides' && sectionData ? (
          <AfsaGuideContent
            sectionData={sectionData}
            isMobile={isMobile}
          />
        ) : act === 'tax-cases' && sectionData ? (
          <TaxCaseContent
            caseData={sectionData}
            isMobile={isMobile}
            onNavigate={onNavigate}
            onNavigateRuling={onNavigateRuling}
          />
        ) : isTreaty(act) && sectionData ? (
          <TreatyContent
            country={sectionData.country || shortActName(act)}
            articleData={sectionData}
            isMobile={isMobile}
            onNavigate={onNavigate}
            onNavigateRuling={onNavigateRuling}
          />
        ) : sectionData ? (
          <SectionContent
            act={act}
            sectionData={sectionData}
            isMobile={isMobile}
            isPinned={isPinned}
            togglePin={togglePin}
            renderLink={renderLink}
            onNavigate={onNavigate}
            onNavigateRuling={onNavigateRuling}
            onNavigateCase={onNavigateCase}
          />
        ) : act === 'private-rulings' && browsingAct ? (
          <PrivateRulingsBrowser
            year={privateRulingsYear}
            onYearChange={setPrivateRulingsYear}
            isMobile={isMobile}
            onOpen={(authnum) => { setActivePrivateRuling(authnum); setActiveSection(''); setActiveRuling(null); if (isMobile) setDrawerOpen(false) }}
          />
        ) : browsingAct && tree && act !== 'rulings' && act !== 'nz-rulings' && act !== 'tax-cases' && act !== 'private-rulings' ? (
          <div style={{ fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
              <span style={{ fontSize: 14, fontWeight: 600, color: COLORS.heading }}>
                {shortActName(act)}
              </span>
              {act !== 'maps' && act !== 'treaties' && (
                <button
                  onClick={() => {
                    setActiveDefinitions(act)
                    navigate(`/definitions/${act}`)
                  }}
                  style={{
                    background: 'none', border: `1px solid ${COLORS.border}`, borderRadius: 6,
                    padding: '3px 10px', cursor: 'pointer', color: COLORS.accent, fontSize: 12,
                  }}
                >
                  Definitions
                </button>
              )}
            </div>
            <LicenceNotice licence={tree.licence} />
            <div style={{ borderTop: `1px solid ${COLORS.border}`, paddingTop: 8 }}>
              {(() => {
                // Collect all parent IDs to expand everything
                const allIds = new Set<string>()
                const collectIds = (parts: any[]) => {
                  for (const p of parts) {
                    allIds.add(p.id)
                    if (p.divisions) {
                      for (const d of p.divisions) {
                        allIds.add(d.id)
                        if (d.subdivisions) {
                          for (const s of d.subdivisions) {
                            allIds.add(s.id)
                          }
                        }
                      }
                    }
                  }
                }
                collectIds(tree.parts || [])
                return (tree.parts || []).map(p => (
                  <TreeNode key={p.id} node={p} level={0} activeSection={act === 'maps' ? (activeMap || '') : activeSection} onSelect={handleTreeSelect} getHref={sectionHref} isMobile={isMobile} expandedIds={allIds} act={act} />
                ))
              })()}
            </div>
          </div>
        ) : (
          <div className={`lk-welcome-wrap${isMobile ? ' lk-welcome-wrap--mobile' : ''}`}>
            <div className="lk-welcome">
              <div className="lk-welcome__lockup" role="img" aria-label="lawkitty">
                <img src="/app/lawkitty-lockup-cat.png" alt="" className="lk-welcome__lockup-dark" />
                <img src="/app/lawkitty-lockup-cat-light.png" alt="" className="lk-welcome__lockup-light" />
              </div>
              <YarnBall />
              <form
                className="lk-welcome__form"
                onSubmit={(e) => {
                  e.preventDefault()
                  const q = homeQuery.trim()
                  setSearchPage(true)
                  navigate(q ? `/search?q=${encodeURIComponent(q)}` : '/search')
                }}
              >
                <label htmlFor="lk-home-search" style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>Search</label>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, color: 'var(--color-text-muted)' }} aria-hidden="true">
                  <circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>
                </svg>
                <input
                  id="lk-home-search"
                  className="lk-welcome__search-input"
                  placeholder="Search acts, rulings, cases and treaties"
                  value={homeQuery}
                  onChange={(e) => setHomeQuery(e.target.value)}
                  autoComplete="off"
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                />
                <button type="submit" className="lk-welcome__pounce">Pounce</button>
              </form>
              <button
                type="button"
                className="lk-welcome__version"
                onClick={() => setChangelogOpen(true)}
              >
                v{appInfo?.version || '2.7.0'}
              </button>
            </div>
          </div>
        )}
      </main>
      </>

      <MCPModal open={mcpOpen} onClose={() => setMcpOpen(false)} />
      {accountOpen && <AccountPanel onClose={() => setAccountOpen(false)} />}
      <KeyboardShortcuts showShortcuts={showShortcuts} setShowShortcuts={setShowShortcuts} />

      {/* Issues modal */}
      {issuesOpen && (
        <IssuesModal onClose={() => setIssuesOpen(false)} />
      )}

      {/* Changelog modal */}
      {changelogOpen && appInfo?.changelog && (
        <ModalOverlay onClose={() => setChangelogOpen(false)}>
          <div style={{ fontSize: 15, fontWeight: 600, color: COLORS.heading, marginBottom: 16, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
            Changelog
          </div>
          <div style={{ maxHeight: '60vh', overflow: 'auto' }}>
            {appInfo.changelog.map((entry: any, i: number) => (
              <div key={i} style={{ marginBottom: 16, paddingBottom: 16, borderBottom: i < appInfo.changelog.length - 1 ? `1px solid ${COLORS.border}` : 'none' }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: COLORS.accent, marginBottom: 2, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
                  v{entry.version} — {entry.date}
                </div>
                <div style={{ fontSize: 11, color: COLORS.textMuted, marginBottom: 6, fontFamily: "var(--font-ui, 'Figtree'), sans-serif" }}>
                  {entry.title}
                </div>
                <ul style={{ margin: 0, paddingLeft: 16, fontSize: 11, color: COLORS.text, fontFamily: "var(--font-ui, 'Figtree'), sans-serif", lineHeight: 1.6 }}>
                  {entry.changes.map((c: string, j: number) => (
                    <li key={j}>{c}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </ModalOverlay>
      )}

    </div>
    </ThemeProvider>
    </ErrorBoundary>
  )
}

// ---------------------------------------------------------------------------
// ModalOverlay — shared backdrop + container
// ---------------------------------------------------------------------------

function ModalOverlay({ onClose, children }: { onClose: () => void; children: React.ReactNode }) {
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000,
        background: 'rgba(0,0,0,0.6)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: COLORS.surface, borderRadius: 12,
          padding: 24, width: '90%', maxWidth: 520,
          boxShadow: '0 16px 48px rgba(0,0,0,0.5)',
        }}
      >
        {children}
      </div>
    </div>
  )
}

