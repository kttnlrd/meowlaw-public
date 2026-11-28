import React, { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { APP_BASE } from '../appBase'
import { shortActName, rulingSlug, actTitleName, normalizeCaseCitation, STATE_TAX_ACTS, JURISDICTION_NAME } from '../utils/display'

const PAGE_SIZE = 25

// Snippets are FTS5 snippet() output: plain text with each match wrapped in
// <mark>. Render those matches as React <mark> elements and everything else as
// text — never as raw HTML (L9). Anything that looks like a tag is shown literally.
const SNIPPET_MARK_RE = /(<mark>[\s\S]*?<\/mark>)/

function renderSnippet(snippet: string) {
  return snippet.split(SNIPPET_MARK_RE).map((part, i) => {
    if (i % 2 === 1) {
      const inner = part.replace(/^<mark>/, '').replace(/<\/mark>$/, '')
      return <mark key={i}>{inner}</mark>
    }
    return <React.Fragment key={i}>{part}</React.Fragment>
  })
}

interface FlatResult {
  act: string
  act_name: string
  section: string
  title: string
  headline: string
  match_type: string
  score: number
  snippet?: string
  type?: string
  outcome?: string
  qa?: { q: string; a: string }[]
  withdrawal_status?: string
  own_citation?: string
  exact_match?: boolean
}

interface Facet { value: string; count: number }
// /api/search/facets `library`: per source, the documents in the whole library
// that contain the query's words (not the candidates one search drew).
interface Library {
  types: Facet[]; acts: Facet[]; series: Facet[]; year: Facet[]; court: Facet[]
  all_series: string[]
}

// The server leaves these out of any search that doesn't name them.
const OPT_IN_SERIES = ['CR', 'PR']
const SERIES_LABEL: Record<string, string> = { AID: 'ATO ID' }

// Row 2 "Search in": one content class at a time → the backend `type` filter.
// `need` is the source the area must hold for the option to exist at all.
const SEARCH_IN: { key: string; label: string; types: string[]; need?: string[] }[] = [
  { key: '', label: 'Everything', types: [] },
  { key: 'section', label: 'Legislation', types: ['section'] },
  { key: 'ruling', label: 'ATO public rulings', types: ['ruling'], need: ['rulings'] },
  { key: 'case', label: 'Cases', types: ['case'], need: ['tax-cases'] },
  { key: 'commentary', label: 'Commentary', types: ['regulatory_guide', 'afsa_guide'], need: ['regulatory-guides', 'afsa-guides'] },
]
const NOT_LEGISLATION = ['rulings', 'private-rulings', 'tax-cases', 'treaties', 'regulatory-guides', 'afsa-guides']
const fmt = (n: number) => n.toLocaleString('en-AU')

interface SearchPanelProps {
  acts: { id: string; name: string }[]
  onNavigate: (act: string, section: string) => void
  isMobile: boolean
  onResultsChange?: (count: number) => void
}

// Practice areas → backend search scope + the sources searched within each.
// Row 1 of the search filter is the area (maps to a backend `scope`).
const PRACTICE_AREAS: { label: string; scope: string; ids: string[] }[] = [
  { label: 'Tax', scope: 'au-tax', ids: ['itaa-1997','itaa-1936','gst-1999','fbt-1986','sis-1993','taa-1953','tax-cases','rulings','private-rulings','treaties', ...STATE_TAX_ACTS.map(a => a.id)] },
  { label: 'Corporate', scope: 'corporate-asic', ids: ['corporations-act-2001','regulatory-guides'] },
  { label: 'Bankruptcy', scope: 'bankruptcy', ids: ['bankruptcy-act-1966','afsa-guides'] },
  { label: 'AML/CTF', scope: 'aml-ctf', ids: ['aml-ctf-2006','aml-ctf-rules-2007'] },
  { label: 'NZ Tax', scope: 'nz-tax', ids: ['nz-it-2007'] },
  { label: 'General', scope: 'general', ids: ['constitution','judiciary-act-1903','adjr-act-1977','migration-act-1958','ndis-act-2013','evidence-act-1995','fair-work-2009','privacy-act-1988','competition-consumer-2010','criminal-code-1995'] },
  { label: 'State taxes', scope: 'state-tax', ids: STATE_TAX_ACTS.map(a => a.id) },
]

// State taxes: the Act dropdown is grouped by jurisdiction, each with an "all Acts of this State"
// choice (a comma list, which the backend `act` filter accepts).
const STATE_TAX_JURS = [...new Set(STATE_TAX_ACTS.map(a => a.jur))].map(jur => ({
  jur, ids: STATE_TAX_ACTS.filter(a => a.jur === jur).map(a => a.id),
}))

// Fire-and-forget warm-up of the backend's lazy search init (first search after a
// restart is ~2-3s, later ones ~130ms). At most once per page session; never awaited,
// never surfaces an error.
let searchWarmed = false
export function warmSearch() {
  if (searchWarmed) return
  searchWarmed = true
  Promise.resolve().then(() => api.searchWarm()).catch(() => {})
}

export default function SearchPanel({ acts, onNavigate, isMobile, onResultsChange }: SearchPanelProps) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<FlatResult[]>([])
  const [totalCount, setTotalCount] = useState(0)
  const [unfilteredResults, setUnfilteredResults] = useState<FlatResult[]>([])
  const [filterOpen, setFilterOpen] = useState(false)
  const [sortMode, setSortMode] = useState<'bestmatch' | 'bysection' | 'byact'>('bestmatch')
  const [loading, setLoading] = useState(false)
  const [hasSearched, setHasSearched] = useState(false)
  // Row 3 "Narrow by" Act: one legislation source ('' = Any)
  const [actFilter, setActFilter] = useState<string>('')
  const [selectedArea, setSelectedArea] = useState<string>(PRACTICE_AREAS[0].label)
  const [currentPage, setCurrentPage] = useState(0)
  // Row 2 "Search in": a SEARCH_IN key ('' = Everything)
  const [searchIn, setSearchIn] = useState<string>('')
  // Row 4: private, class and product rulings switched on
  const [includeExcluded, setIncludeExcluded] = useState(false)
  const [operator, setOperator] = useState<'AND' | 'OR'>('AND')
  const [rtype, setRtype] = useState<string>('')
  const [outcome, setOutcome] = useState<string>('')
  const [yearFrom, setYearFrom] = useState<string>('')
  const [yearTo, setYearTo] = useState<string>('')
  const [court, setCourt] = useState<string>('')
  // Library counts for the searched words in this area (null = not loaded: no counts shown)
  const [library, setLibrary] = useState<Library | null>(null)
  const libraryKey = useRef('')
  const facetSeq = useRef(0)
  // The term the current results were fetched for (header shows it even while the box is edited)
  const [searchedTerm, setSearchedTerm] = useState('')
  const firstRun = useRef(true)
  // Live autocomplete: top-5 matches shown inline in the main results area while typing.
  const [suggestions, setSuggestions] = useState<FlatResult[]>([])
  const suggestSeq = useRef(0)

  const activeArea = PRACTICE_AREAS.find(a => a.label === selectedArea) || PRACTICE_AREAS[0]
  const inArea = (ids?: string[]) => !ids || ids.some(id => activeArea.ids.includes(id))
  const searchInOptions = SEARCH_IN.filter(o => inArea(o.need))
  const legislationIds = activeArea.ids.filter(id => !NOT_LEGISLATION.includes(id))
  const hasExcluded = activeArea.scope === 'au-tax'
  // Which narrow-by controls apply to the current "Search in" choice. A control
  // that cannot change the results is not shown (and its value is cleared).
  const applies = {
    act: (searchIn === '' || searchIn === 'section') && legislationIds.length > 1,
    series: hasExcluded && (searchIn === '' || searchIn === 'ruling'),
    year: hasExcluded && searchIn !== 'section' && searchIn !== 'commentary',
    court: hasExcluded && (searchIn === '' || searchIn === 'case'),
    excluded: hasExcluded && (searchIn === '' || searchIn === 'ruling'),
  }

  // The hybrid/facets params every request sends for the current controls.
  const requestParams = () => {
    const opt = SEARCH_IN.find(o => o.key === searchIn) || SEARCH_IN[0]
    const incl = includeExcluded && applies.excluded
    let types = opt.types.filter(t => t !== 'regulatory_guide' || activeArea.ids.includes('regulatory-guides'))
      .filter(t => t !== 'afsa_guide' || activeArea.ids.includes('afsa-guides'))
    // Naming private_ruling is the server's opt-in (Everything only — they are not public
    // rulings); with Everything, the other types must then be named too.
    if (incl && !searchIn) types = ['section', 'ruling', 'case', 'private_ruling']
    const series = applies.series && rtype ? rtype
      : incl ? (library?.all_series || []).join(',') || undefined : undefined
    return {
      type: types.join(',') || undefined,
      rtype: series,
      outcome: incl && !searchIn && outcome ? outcome : undefined,
      act: applies.act && actFilter ? actFilter : undefined,
      yearFrom: applies.year ? yearFrom || undefined : undefined,
      yearTo: applies.year ? yearTo || undefined : undefined,
      court: applies.court ? court || undefined : undefined,
    }
  }

  useEffect(() => { warmSearch() }, [])

  // Re-run the search when any slicer changes (skip the mount)
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    if (query.trim()) doSearch()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actFilter, selectedArea, searchIn, includeExcluded, rtype, outcome, yearFrom, yearTo, court])

  // Notify parent of results count
  useEffect(() => {
    onResultsChange?.(results.length)
  }, [results.length, onResultsChange])

  // Restore query from URL on direct load / back-nav (e.g. /search?q=...)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const q = params.get('q')
    if (q) {
      setQuery(q)
      doSearch(q)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Live autocomplete (CDN-0099): ≥2 chars, 250 ms debounce, top-5 matches rendered
  // inline in the main results area — not a dropdown. Committing (Enter / Pounce)
  // runs the full search and replaces these.
  useEffect(() => {
    const term = query.trim()
    const seq = ++suggestSeq.current
    if (term.length < 2) { setSuggestions([]); return }
    const t = setTimeout(async () => {
      try {
        const data = await api.suggest(term, 5)
        if (seq !== suggestSeq.current) return
        setSuggestions((data.suggestions || []).map((r: any) => ({
          act: r.act || '',
          act_name: '',
          section: r.section || '',
          title: r.title || '',
          headline: '',
          match_type: '',
          score: 0,
          type: r.type || 'section',
          withdrawal_status: r.withdrawal_status,
          own_citation: r.own_citation,
        })))
      } catch {
        if (seq === suggestSeq.current) setSuggestions([])
      }
    }, 250)
    return () => clearTimeout(t)
  }, [query])

  // Fetches one page of hybrid ("best match") results at the given server offset.
  // Server caps limit at 50/request, so paging must walk `offset` rather than
  // requesting a bigger limit and slicing client-side.
  const fetchHybridPage = async (term: string, offset: number) => {
    const p = requestParams()
    const data = await api.searchHybrid(term, p.type, PAGE_SIZE, {
      ...p,
      operator,
      scope: activeArea.scope,
      offset,
    })
    const pageResults: FlatResult[] = (data.results || data || []).map((r: any) => ({
      act: r.act || '',
      act_name: r.act_name || '',
      section: r.section || '',
      title: r.title || '',
      headline: '',
      match_type: '',
      score: r.fusion_score || r.score || 0,
      snippet: r.snippet || '',
      type: r.source_type || r.type || 'section',
      outcome: r.outcome || '',
      qa: r.qa || undefined,
      withdrawal_status: r.withdrawal_status,
      own_citation: r.own_citation,
      exact_match: !!r.exact_match,
    }))
    setUnfilteredResults(pageResults)
    setTotalCount(typeof data.total === 'number' ? data.total : pageResults.length)
    setResults(pageResults)
  }

  // Jumps to page `n` of results. For best-match mode this re-fetches that page
  // from the server (offset = n * PAGE_SIZE) so every row the server reports in
  // `total` is reachable, not just the first batch received.
  const goToPage = (n: number) => {
    const clamped = Math.max(0, Math.min(totalPages - 1, n))
    setCurrentPage(clamped)
    if (sortMode === 'bestmatch' && hasSearched && searchedTerm) {
      fetchHybridPage(searchedTerm, clamped * PAGE_SIZE).catch(() => {})
    }
  }

  const doSearch = async (q?: string) => {
    const term = (q || query).trim()
    if (!term) return

    setLoading(true)
    setHasSearched(true)
    setSearchedTerm(term)
    try {
      // Keep the query in the URL so direct loads / back-nav restore it. The path MUST carry
      // the /app base — a bare '/search?q=…' is outside the router's basename and re-renders
      // as a blank page when the user navigates back from a result.
      window.history.replaceState(null, '', `${APP_BASE}/search?q=` + encodeURIComponent(term))
      if (sortMode === 'bestmatch') {
        // Library counts depend only on the words, the area and AND/OR — fetched once per
        // combination. Best-effort: a failed or missing call never costs the results.
        const key = [term, activeArea.scope, operator].join('\u0000')
        if (key !== libraryKey.current) {
          libraryKey.current = key
          const seq = ++facetSeq.current
          setLibrary(null)
          Promise.resolve().then(() => api.searchFacets(term, {
            scope: activeArea.scope,
            operator: operator !== 'AND' ? operator : undefined,
          })).then(d => { if (seq === facetSeq.current) setLibrary(d?.library || null) })
            .catch(() => { if (seq === facetSeq.current) { setLibrary(null); libraryKey.current = '' } })
        }
        await fetchHybridPage(term, 0)
      } else {
        // Per-act search, scoped to the selected area (or the Act chosen in row 3).
        // Private rulings are opt-in: never searched on this path.
        const targets = actFilter
          ? acts.filter(a => actFilter.split(',').includes(a.id))
          : acts.filter(a => activeArea.ids.includes(a.id) && a.id !== 'private-rulings')
        const all: FlatResult[] = []
        for (const a of targets) {
          try {
            const data = await api.search(term, a.id)
            if (data.results) {
              all.push(...data.results.map((r: any) => ({
                act: a.id,
                act_name: a.name,
                section: r.section,
                title: r.title,
                headline: '',
                match_type: '',
                score: 0,
                type: r.type,
                withdrawal_status: r.withdrawal_status,
                own_citation: r.own_citation,
              })))
            }
          } catch { /* skip */ }
        }
        setUnfilteredResults(all)
        setResults(all)
      }
    } catch { setUnfilteredResults([]); setResults([]) }
    setLoading(false)
    setCurrentPage(0)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      doSearch()
    }
  }

  const handleSelect = (r: FlatResult) => {
    setQuery('')
    setResults([])
    if (r.type === 'case' && r.section) {
      onNavigate('tax-cases', r.section)
    } else if (r.section) {
      onNavigate(r.act, r.section)
    }
  }

  // Canonical href for a result row, mirroring handleSelect's act resolution,
  // so the row can render as a real <Link> (F-15).
  const hrefForResult = (r: FlatResult): string | null => {
    if (!r.section) return null
    const targetAct = r.type === 'case' ? 'tax-cases' : r.act
    if (targetAct === 'tax-cases') return `/tax-cases/${encodeURIComponent(r.section)}`
    if (targetAct === 'private-rulings') return `/private-rulings/${encodeURIComponent(r.section)}`
    if (targetAct === 'rulings') return `/rulings/${rulingSlug(r.section)}`
    return `/${targetAct}/${r.section}`
  }

  // Library counts (undefined while not loaded, or outside best match).
  const lib = sortMode === 'bestmatch' ? library : null
  const libCount = (list: Facet[] | undefined, value: string) => list?.find(f => f.value === value)?.count ?? 0
  const optInSeries = OPT_IN_SERIES.reduce((n, k) => n + libCount(lib?.series, k), 0)
  const excludedCounts = [
    { n: libCount(lib?.types, 'private_ruling') }, { n: libCount(lib?.series, 'CR') }, { n: libCount(lib?.series, 'PR') },
  ]
  const searchInCount = (o: typeof SEARCH_IN[number]): number | undefined => {
    if (!lib) return undefined
    const incl = includeExcluded && hasExcluded
    const types = o.types.length ? o.types : ['section', 'ruling', 'case', 'regulatory_guide', 'afsa_guide']
    let n = types.reduce((sum, t) => sum + libCount(lib.types, t), 0)
    if (incl && o.key === 'ruling') n += optInSeries
    if (incl && o.key === '') n += optInSeries + libCount(lib.types, 'private_ruling')
    return n
  }
  const countBadge = (n: number | undefined) =>
    n === undefined ? null : <span className="lk-row__count">{fmt(n)}</span>
  // Dropdown options: the values the library holds for these words. Values with
  // no matching document go to row 4, still selectable — never hidden.
  const actOptions = legislationIds.map(id => ({ id, n: libCount(lib?.acts, id) }))
  const seriesOptions = (lib?.all_series || [])
    .filter(k => includeExcluded || !OPT_IN_SERIES.includes(k))
    .map(k => ({ id: k, n: libCount(lib?.series, k) }))
  const yearKeys = [...new Set([...(lib?.year || []).map(f => f.value), ...[yearFrom, yearTo].filter(Boolean)])].sort()
  const courtKeys = [...new Set([...(lib?.court || []).map(f => f.value), ...(court ? [court] : [])])]
  // A dropdown is shown only when it has a choice besides "Any" (its 0-count options sit in row 4).
  const show = {
    act: applies.act && (actOptions.some(o => o.n > 0) || !!actFilter),
    series: applies.series && (seriesOptions.some(o => o.n > 0) || !!rtype),
    year: applies.year && yearKeys.length > 0,
    court: applies.court && courtKeys.length > 0,
    outcome: includeExcluded && applies.excluded && !searchIn,
  }
  // Row 4 collects every option with no matching document, by row.
  const zeroOptions: { label: string; onPick: () => void }[] = lib ? [
    ...searchInOptions.filter(o => o.key && searchInCount(o) === 0)
      .map(o => ({ label: o.label, onPick: () => pickSearchIn(o.key) })),
    ...(applies.act ? actOptions.filter(o => o.n === 0 && o.id !== actFilter)
      .map(o => ({ label: actTitleName(o.id), onPick: () => setActFilter(o.id) })) : []),
    ...(applies.series ? seriesOptions.filter(o => o.n === 0 && o.id !== rtype)
      .map(o => ({ label: `${SERIES_LABEL[o.id] || o.id} series`, onPick: () => setRtype(o.id) })) : []),
  ] : []

  // Choosing what to search in clears any narrow-by control that no longer applies.
  const pickSearchIn = (key: string) => {
    setSearchIn(key)
    setCurrentPage(0)
    const leg = key === '' || key === 'section'
    if (!leg) setActFilter('')
    if (!(key === '' || key === 'ruling')) { setRtype(''); setOutcome('') }
    if (key === 'section' || key === 'commentary') { setYearFrom(''); setYearTo('') }
    if (!(key === '' || key === 'case')) setCourt('')
  }

  const bestMatch = sortMode === 'bestmatch'

  // Pagination calculations. Best-match mode pages on the server (`results` already
  // holds just the current page, fetched at offset = currentPage * PAGE_SIZE), so
  // totalPages comes from the server-reported total, not the rows in hand.
  const totalPages = bestMatch
    ? Math.max(1, Math.ceil(totalCount / PAGE_SIZE))
    : Math.max(1, Math.ceil(results.length / PAGE_SIZE))
  const pageStart = currentPage * PAGE_SIZE
  const pageResults = bestMatch ? results : results.slice(pageStart, pageStart + PAGE_SIZE)

  // Document-type badge: label + lk-badge modifier for every result type we render.
  const badgeFor = (r: FlatResult): { label: string; cls: string } => {
    if (r.type === 'case' || r.act === 'tax-cases') return { label: 'Case', cls: 'case' }
    if (r.type === 'private_ruling' || r.act === 'private-rulings') return { label: 'Private ruling', cls: 'private-ruling' }
    if (r.type === 'ruling' || r.act === 'rulings') return { label: 'Ruling', cls: 'ruling' }
    if (r.act === 'treaties' || /treaty|convention|double tax/i.test(r.act_name)) return { label: 'Treaty', cls: 'treaty' }
    if (r.act === 'regulatory-guides') return { label: 'Guide', cls: 'act' }
    if (r.act === 'afsa-guides') return { label: 'AFSA', cls: 'act' }
    return { label: 'Section', cls: 'section' }
  }

  // Answer-pill classification for private-ruling Q&A. Collapses OCR spacing
  // and punctuation ("Ye s" → "Yes", ": No" → "No") so classification is robust
  // to ingest artefacts; "not…" stays Qualified.
  const answerPill = (a: string): { label: string; cls: string } => {
    const v = (a || '').toLowerCase().replace(/[^a-z]/g, '')
    if (v.startsWith('yes')) return { label: 'Yes', cls: 'yes' }
    if (v.startsWith('no') && !v.startsWith('not')) return { label: 'No', cls: 'no' }
    return { label: 'Qualified', cls: 'qualified' }
  }
  const truncate = (s: string, n: number): string =>
    s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s

  const chevronSvg = (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M6 9l6 6 6-6" />
    </svg>
  )

  const filterButtonSvg = (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 6h16M7 12h10M10 18h4" />
    </svg>
  )

  const anyQueryFilter = !!(searchIn || rtype || outcome || yearFrom || yearTo || court || actFilter || includeExcluded)
  const showRows = bestMatch && hasSearched
  // A pasted authorisation number pins its private ruling even while they are left out.
  const pinnedPrivate = !(includeExcluded && applies.excluded) && currentPage === 0
    && results.some(r => r.exact_match && badgeFor(r).cls === 'private-ruling')
  // Live autocomplete is "live" while the box differs from the committed term.
  const isSuggesting = !loading && suggestions.length > 0 && query.trim() !== searchedTerm

  // Shared result-card renderer (used for both live suggestions and full results).
  const renderCard = (r: FlatResult, key: string) => {
    const badge = badgeFor(r)
    const isCase = badge.cls === 'case'
    const isPrivate = badge.cls === 'private-ruling'
    const isSection = badge.cls === 'section'
    const isRuling = badge.cls === 'ruling'

    // Title — identify the document first, then the heading.
    let title: string
    if (isCase) {
      title = r.title || normalizeCaseCitation(r.section) || ''
    } else if (isPrivate) {
      // "Private ruling 1051476678819 — Fringe benefits tax" → "1051476678819: Fringe benefits tax"
      let t = (r.title || r.section || '')
      t = t.replace(/^Private ruling\s+\S+\s*[—–-]\s*/i, '')
      t = t.replace(/\s*[—–]\s*/g, ': ')
      title = t.trim() || r.section || ''
    } else if (isSection && r.section) {
      const heading = r.title && r.title !== r.section ? r.title : ''
      title = `s ${r.section} ${actTitleName(r.act)}${heading ? `: ${heading}` : ''}`
    } else if (isRuling) {
      // Lead with the document's own citation (TD 93/12W, ATO ID 2001/1) —
      // never a paragraph pinpoint; drop a leading ref already in the title.
      const cite = r.own_citation || r.section.replace(/_(\d+)_(\d+)$/, ' $1/$2').replace(/_/g, ' ')
      const body = (r.title || '').replace(/^(?:ATO ID|[A-Z]+(?: LA)?)\s+\d+(?:\/\d+)?W?\s*(?:[—–:-]\s*|$)/, '')
      title = body ? `${cite} — ${body}` : cite
    } else {
      title = r.title || ''
    }

    const resultHref = hrefForResult(r)
    const RowTag: any = resultHref ? Link : 'div'
    const rowProps: any = resultHref
      ? {
          to: resultHref,
          onClick: (e: React.MouseEvent) => {
            if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return
            e.preventDefault()
            handleSelect(r)
          },
        }
      : {
          onClick: () => handleSelect(r),
          role: 'button',
          tabIndex: 0,
          onKeyDown: (e: React.KeyboardEvent) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleSelect(r) }
          },
        }

    const qaItems = isPrivate && r.qa && r.qa.length ? r.qa : []
    const qaShown = qaItems.slice(0, 3)
    const qaMore = qaItems.length - qaShown.length

    return (
      <RowTag key={key} className="lk-result-card" {...rowProps}>
        <div className="lk-result-card__head">
          <span className={`lk-badge lk-badge--${badge.cls}`}>{badge.label}</span>
          <span className="lk-result-card__title">{title}</span>
          {r.withdrawal_status === 'withdrawn' && (
            <span className="lk-badge lk-badge--withdrawn" title="Withdrawn version — not a current ATO view. Check what replaced it before citing.">
              Withdrawn
            </span>
          )}
          {isPrivate && r.outcome && (
            <span
              className={`lk-outcome lk-outcome--${r.outcome === 'yes' ? 'yes' : r.outcome === 'no' ? 'no' : 'mixed'}`}
              title={r.outcome === 'mixed' ? 'Some answers favourable, some not' : undefined}
            >
              {r.outcome === 'yes' ? 'Yes' : r.outcome === 'no' ? 'No' : 'Mixed'}
            </span>
          )}
        </div>
        {!isPrivate && r.snippet && (
          <div className="lk-result-card__snippet">{renderSnippet(r.snippet)}</div>
        )}
        {isPrivate && qaShown.length > 0 && (
          <div className="lk-result-card__qa">
            {qaShown.map((qa, qi) => (
              <div key={qi} className="lk-result-card__qa-row">
                <span className="lk-result-card__q">{truncate(qa.q, 120)}</span>
                <span className={`lk-answer lk-answer--${answerPill(qa.a).cls}`}>{answerPill(qa.a).label}</span>
              </div>
            ))}
            {qaMore > 0 && <span className="lk-result-card__qa-more">+{qaMore} more</span>}
          </div>
        )}
      </RowTag>
    )
  }

  const filtersAside = filterOpen && (
    <aside className="lk-filters" aria-label="Search options">
      <span className="lk-filters__caption">Search options</span>
      <div className="lk-filters__field">
        <label htmlFor="lk-fmatch" className="lk-filters__label">Match</label>
        <select
          id="lk-fmatch"
          className="lk-filters__select"
          value={operator}
          onChange={e => setOperator(e.target.value as 'AND' | 'OR')}
        >
          <option value="AND">All terms (AND)</option>
          <option value="OR">Any term (OR)</option>
        </select>
      </div>
      <div className="lk-filters__field">
        <label htmlFor="lk-fsort" className="lk-filters__label">Sort</label>
        <select
          id="lk-fsort"
          className="lk-filters__select"
          value={sortMode}
          onChange={e => setSortMode(e.target.value as 'bestmatch' | 'bysection' | 'byact')}
        >
          <option value="bestmatch">Best match</option>
          <option value="bysection">By section</option>
          <option value="byact">By act</option>
        </select>
      </div>
    </aside>
  )

  return (
    <div className={'lk-search' + (isMobile ? ' lk-search--mobile' : '')}>
      {/* Search input row */}
      <div className="lk-search__bar">
        <div className="lk-search__form" role="search">
          <svg className="lk-search__icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>
          </svg>
          <label htmlFor="lk-search-q" className="lk-sr-only">Search legislation</label>
          <input
            id="lk-search-q"
            className="lk-search__input"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Search legislation, rulings, cases…"
            autoComplete="off"
          />
          <button
            type="button"
            className="lk-search__submit"
            onClick={() => doSearch()}
            onMouseDown={e => e.preventDefault()}
          >
            Pounce
          </button>
        </div>
        <button
          type="button"
          className="lk-search__filters-btn"
          onClick={() => setFilterOpen(!filterOpen)}
          title="Search options: match all or any words, sort order"
          aria-expanded={filterOpen}
        >
          {filterButtonSvg}
          <span className="lk-search__filters-label">Options</span>
        </button>
      </div>

      {/* Row 1 — practice area: which body of law (backend `scope`) */}
      <div className="lk-practice" role="group" aria-label="Practice areas">
        <div className="lk-practice__areas">
          {PRACTICE_AREAS.map(a => (
            <button
              key={a.label}
              type="button"
              className="lk-practice__area"
              aria-pressed={selectedArea === a.label}
              onClick={() => {
                setSelectedArea(a.label)
                setSearchIn(''); setActFilter(''); setRtype(''); setOutcome('')
                setYearFrom(''); setYearTo(''); setCourt(''); setIncludeExcluded(false)
              }}
            >
              {a.label}
            </button>
          ))}
        </div>
      </div>

      {showRows && (
        <div className="lk-rows">
          {/* Row 2 — what class of content to search; counts are library documents with these words */}
          <div className="lk-row" role="group" aria-labelledby="lk-row-in">
            <span id="lk-row-in" className="lk-row__label">Search in</span>
            {searchInOptions.filter(o => !o.key || searchIn === o.key || searchInCount(o) !== 0).map(o => (
              <button
                key={o.key}
                type="button"
                className="lk-row__opt"
                aria-pressed={searchIn === o.key}
                onClick={() => pickSearchIn(o.key)}
              >
                {o.label}{countBadge(searchInCount(o))}
              </button>
            ))}
          </div>

          {/* Row 3 — narrow within that choice; "Any" is the default */}
          {(show.act || show.series || show.year || show.court || show.outcome) && (
            <div className="lk-row" role="group" aria-labelledby="lk-row-narrow">
              <span id="lk-row-narrow" className="lk-row__label">Narrow by</span>
              {show.act && (
                <label className="lk-drop" data-set={!!actFilter}>
                  <span>Act</span>
                  <select value={actFilter} onChange={e => { setActFilter(e.target.value); setCurrentPage(0) }}>
                    <option value="">Any</option>
                    {activeArea.scope === 'state-tax' ? STATE_TAX_JURS.map(g => {
                      const opts = actOptions.filter(o => g.ids.includes(o.id) && (o.n > 0 || o.id === actFilter))
                      const all = g.ids.join(',')
                      const n = actOptions.filter(o => g.ids.includes(o.id)).reduce((t, o) => t + o.n, 0)
                      if (!opts.length && actFilter !== all) return null
                      return (
                        <optgroup key={g.jur} label={JURISDICTION_NAME[g.jur]}>
                          <option value={all}>All {JURISDICTION_NAME[g.jur]} Acts{lib ? ` (${fmt(n)})` : ''}</option>
                          {opts.map(o => (
                            <option key={o.id} value={o.id}>{actTitleName(o.id)}{lib ? ` (${fmt(o.n)})` : ''}</option>
                          ))}
                        </optgroup>
                      )
                    }) : actOptions.filter(o => o.n > 0 || o.id === actFilter).map(o => (
                      <option key={o.id} value={o.id}>{actTitleName(o.id)}{lib ? ` (${fmt(o.n)})` : ''}</option>
                    ))}
                  </select>
                </label>
              )}
              {show.series && (
                <label className="lk-drop" data-set={!!rtype}>
                  <span>Series</span>
                  <select value={rtype} onChange={e => { setRtype(e.target.value); setCurrentPage(0) }}>
                    <option value="">Any</option>
                    {seriesOptions.filter(o => o.n > 0 || o.id === rtype).map(o => (
                      <option key={o.id} value={o.id}>{SERIES_LABEL[o.id] || o.id} ({fmt(o.n)})</option>
                    ))}
                  </select>
                </label>
              )}
              {show.year && (
                <span className="lk-drop" data-set={!!(yearFrom || yearTo)}>
                  <label htmlFor="lk-year-from">Year</label>
                  <select id="lk-year-from" aria-label="Year from" value={yearFrom} onChange={e => { setYearFrom(e.target.value); setCurrentPage(0) }}>
                    <option value="">Any</option>
                    {yearKeys.map(y => <option key={y} value={y} disabled={!!yearTo && y > yearTo}>{y}</option>)}
                  </select>
                  <span aria-hidden="true">–</span>
                  <select aria-label="Year to" value={yearTo} onChange={e => { setYearTo(e.target.value); setCurrentPage(0) }}>
                    <option value="">Any</option>
                    {yearKeys.map(y => <option key={y} value={y} disabled={!!yearFrom && y < yearFrom}>{y}</option>)}
                  </select>
                </span>
              )}
              {show.court && (
                <label className="lk-drop" data-set={!!court}>
                  <span>Court</span>
                  <select value={court} onChange={e => { setCourt(e.target.value); setCurrentPage(0) }}>
                    <option value="">Any</option>
                    {courtKeys.map(c => <option key={c} value={c}>{c} ({fmt(libCount(lib?.court, c))})</option>)}
                  </select>
                </label>
              )}
              {show.outcome && (
                <label className="lk-drop" data-set={!!outcome}>
                  <span>Private ruling outcome</span>
                  <select value={outcome} onChange={e => { setOutcome(e.target.value); setCurrentPage(0) }}>
                    <option value="">Any</option>
                    <option value="yes">ATO said Yes</option>
                    <option value="no">ATO said No</option>
                    <option value="mixed">Mixed</option>
                  </select>
                </label>
              )}
            </div>
          )}

          {/* Row 4 — everything switched off by default, on one muted line */}
          {lib && (zeroOptions.length > 0 || applies.excluded) && (
            <details className="lk-row4">
              <summary>
                {[
                  zeroOptions.length > 0 && `${zeroOptions.length} option${zeroOptions.length === 1 ? ' has' : 's have'} no matching documents`,
                  applies.excluded && `${searchIn ? 'class and product' : 'private, class and product'} rulings ${includeExcluded ? 'included' : 'excluded'}`,
                ].filter(Boolean).join(' · ')}
              </summary>
              {applies.excluded && (
                <p className="lk-row4__line">
                  {searchIn ? 'Class' : <>Private (<span className="lk-row4__n">{fmt(excludedCounts[0].n)}</span>), class</>} (<span className="lk-row4__n">{fmt(excludedCounts[1].n)}</span>) and product rulings (<span className="lk-row4__n">{fmt(excludedCounts[2].n)}</span>) with your words are {includeExcluded ? 'included' : 'left out'}.{' '}
                  <button type="button" className="lk-row4__link" onClick={() => { setIncludeExcluded(!includeExcluded); setOutcome(''); if (OPT_IN_SERIES.includes(rtype)) setRtype(''); setCurrentPage(0) }}>
                    {includeExcluded ? 'Leave them out' : 'Include them'}
                  </button>
                  {' '}— or paste an authorisation number to open one.
                </p>
              )}
              {zeroOptions.length > 0 && (
                <div className="lk-row4__zero" role="group" aria-label="Options with no matching documents">
                  {zeroOptions.map(o => (
                    <button key={o.label} type="button" className="lk-row__opt" onClick={() => { o.onPick(); setCurrentPage(0) }}>
                      {o.label}<span className="lk-row__count">0</span>
                    </button>
                  ))}
                </div>
              )}
            </details>
          )}
        </div>
      )}

      {/* Results header — the number and page are the server's */}
      {results.length > 0 && !loading && (
        <div className="lk-search-header">
          <h1 className="lk-search-header__count">{bestMatch ? fmt(totalCount) : (totalCount > results.length ? `${results.length}+` : results.length)} {(bestMatch ? totalCount : results.length) === 1 ? 'result' : 'results'}</h1>
          {searchedTerm && (
            <span className="lk-search-header__query">for &#8220;{searchedTerm}&#8221;</span>
          )}
          <span className="lk-search-header__page">
            Showing {fmt(pageStart + 1)}–{fmt(pageStart + pageResults.length)}{totalPages > 1 ? ` · page ${currentPage + 1} of ${totalPages}` : ''}
          </span>
          {bestMatch && lib && (
            <p className="lk-search-header__note">
              {pinnedPrivate
                ? 'This private ruling opened from its authorisation number. ATO private rulings are otherwise left out, so the counts above do not include it.'
                : 'Results are the best matches by wording and meaning. The counts above are every document in the library that contains your words.'}
            </p>
          )}
        </div>
      )}

      <div className="lk-search__body">
        <div className="lk-search__main">
          {loading && (
            <div className="lk-search-loading" role="status">
              <img src="/app/favicon.png" alt="" />
              Sniffing through the ITAA…
            </div>
          )}
          {isSuggesting && (
            <div className="lk-results">
              <div className="lk-search-header">
                <h1 className="lk-search-header__count">{suggestions.length} quick match{suggestions.length === 1 ? '' : 'es'}</h1>
                <span className="lk-search-header__query">keep typing, or press Enter to search everything</span>
              </div>
              {suggestions.map((r, i) => renderCard(r, `sug-${r.act}-${r.section}-${i}`))}
            </div>
          )}
          {!loading && !isSuggesting && hasSearched && results.length === 0 && unfilteredResults.length === 0 && (
            <div className="lk-empty-state" role="status">
              <p className="lk-empty-state__title">Nothing sniffed out.</p>
              <span>{anyQueryFilter ? 'Try fewer filters or different words.' : 'Try different words.'}</span>
            </div>
          )}

          {!isSuggesting && results.length > 0 && !loading && (
            <>
              {/* Results list */}
              <div className="lk-results">
                {pageResults.map((r, i) => renderCard(r, `${r.act}-${r.section}-${pageStart + i}`))}
              </div>

              {/* Pagination */}
              {totalPages > 1 && (
                <nav className="lk-pager" aria-label="Results pages">
                  <button
                    type="button"
                    className="lk-pager__btn"
                    onClick={() => goToPage(currentPage - 1)}
                    disabled={currentPage === 0}
                  >
                    ← Previous
                  </button>
                  {Array.from({ length: Math.min(totalPages, 7) }, (_, i) => {
                    // Show pages around current
                    const start = Math.max(0, Math.min(currentPage - 3, totalPages - 7))
                    const pageNum = start + i
                    if (pageNum >= totalPages) return null
                    return (
                      <button
                        key={pageNum}
                        type="button"
                        className="lk-pager__btn"
                        aria-current={pageNum === currentPage ? 'page' : undefined}
                        onClick={() => goToPage(pageNum)}
                      >
                        {pageNum + 1}
                      </button>
                    )
                  })}
                  <button
                    type="button"
                    className="lk-pager__btn"
                    onClick={() => goToPage(currentPage + 1)}
                    disabled={currentPage >= totalPages - 1}
                  >
                    Next →
                  </button>
                </nav>
              )}
            </>
          )}
        </div>
        {filtersAside}
      </div>
    </div>
  )
}
