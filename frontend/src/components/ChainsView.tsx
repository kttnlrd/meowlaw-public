import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import ForceGraph2D from 'react-force-graph-2d'
import { api } from '../api'

// Interpretive chains (ALPHA). Every node and edge comes from tax-encode via
// /api/tax/chains; nodes are questions and provisions, never conclusions.

type CNode = {
  id: string; kind: string; label?: string; title?: string; role?: string
  provision?: string; act?: string; section?: string; division?: string
  shared?: boolean; consumer?: boolean; fact?: string; gate?: boolean
  x?: number; y?: number; fx?: number; fy?: number
}
type CEdge = { source: string | CNode; target: string | CNode; kind: string; note?: string }
type Payload = {
  act?: string; section?: string; title?: string; division?: string
  nodes?: CNode[]; edges?: CEdge[]; counts?: Record<string, number>; error?: string; detail?: string
}

// tax-encode covers the two ITAA Acts. Everything else is served from the corpus graph (NZ Acts
// today), and /api/tax/health names those in corpus_acts, so the picker stays truthful as more
// Acts are covered.
const ACTS: [string, string][] = [['itaa-1997', 'ITAA 1997'], ['itaa-1936', 'ITAA 1936']]
const ACT_LABELS: Record<string, string> = {
  'nz-it-2007': 'NZ Income Tax Act 2007',
  'nz-taa-1994': 'NZ Tax Administration Act 1994',
  'nz-gst-1985': 'NZ Goods and Services Tax Act 1985',
  'nz-kiwisaver-2006': 'NZ KiwiSaver Act 2006',
  'nz-sl-2011': 'NZ Student Loan Scheme Act 2011',
  'nz-egda-1968': 'NZ Estate and Gift Duties Act 1968',
  'nz-scd-1971': 'NZ Stamp and Cheque Duties Act 1971',
}
const EDGE_KINDS = ['contains', 'gate', 'audited_from', 'may_come_from', 'feeds']
const DASH: Record<string, number[] | null> = { may_come_from: [4, 3], feeds: [1, 3] }

const idOf = (v: string | CNode) => (typeof v === 'string' ? v : v.id)
const actOf = (n: CNode) => n.act || n.id.split(':')[1] || ''
const trunc = (s: string) => (s.length > 40 ? s.slice(0, 39) + '…' : s)
const nameOf = (n: CNode) => n.label || n.id

// Ids reachable from root along edges; reverse walks target -> source (ancestors).
export function reach(root: string, edges: CEdge[], reverse = false): Set<string> {
  const out = new Map<string, string[]>()
  for (const e of edges) {
    const [a, b] = reverse ? [idOf(e.target), idOf(e.source)] : [idOf(e.source), idOf(e.target)]
    out.set(a, [...(out.get(a) || []), b])
  }
  const seen = new Set([root])
  const stack = [root]
  while (stack.length) for (const n of out.get(stack.pop()!) || []) if (!seen.has(n)) { seen.add(n); stack.push(n) }
  return seen
}

type Hide = { id: string; mode: 'branch' | 'node' | 'focus' }

// "152-10" -> [defaultAct, "152-10"]; "itaa-1936 340" -> ["itaa-1936", "340"]
export function parseQuery(q: string, defaultAct: string): [string, string] | null {
  const parts = q.trim().replace(/^s\s+/i, '').split(/\s+/).filter(Boolean)
  if (parts.length === 1) return [defaultAct, parts[0].replace(/^s(?=\d)/, '')]
  if (parts.length === 2 && /^[a-z]/i.test(parts[0])) return [parts[0].toLowerCase(), parts[1]]
  return null
}

function tokens() {
  const css = getComputedStyle(document.documentElement)
  const v = (k: string) => css.getPropertyValue(k).trim()
  return {
    accent: v('--catnip'), limb: v('--gavel'), gold: v('--tabby'), amber: v('--tabby-deep'),
    green: v('--catnip'), muted: v('--color-text-muted'), ink: v('--color-text'), line: v('--line-strong'), font: v('--font-sans') || 'sans-serif',
  }
}

export default function ChainsView({ isMobile }: { isMobile: boolean }) {
  const location = useLocation()
  const navigate = useNavigate()
  const [, , urlAct, urlSection] = location.pathname.split('/').map(decodeURIComponent)
  const [act, setAct] = useState(urlAct || 'itaa-1997')
  const [query, setQuery] = useState(urlSection || '')
  const [head, setHead] = useState<Payload | null>(null)
  const [nodes, setNodes] = useState<CNode[]>([])
  const [edges, setEdges] = useState<CEdge[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<CNode | null>(null)
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [hides, setHides] = useState<Hide[]>([])
  const [pinned, setPinned] = useState<Set<string>>(new Set())
  const [frozen, setFrozen] = useState(false)
  const fitted = useRef(false)
  const [size, setSize] = useState({ w: 800, h: 600 })
  const boxRef = useRef<HTMLDivElement>(null)
  const fgRef = useRef<any>(null)
  const lastClick = useRef({ id: '', t: 0 })
  const c = useMemo(tokens, [])
  // Acts the corpus can serve in addition to tax-encode's two, straight from /api/tax/health.
  const [acts, setActs] = useState<[string, string][]>(ACTS)
  useEffect(() => {
    api.taxHealth()
      .then((h: any) => {
        const extra: string[] = Array.isArray(h?.corpus_acts) ? h.corpus_acts : []
        const merged: [string, string][] = [...ACTS]
        for (const a of extra) if (!merged.some(([k]) => k === a)) merged.push([a, ACT_LABELS[a] || a])
        setActs(merged)
      })
      .catch(() => setActs(ACTS))
  }, [])

  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Primary load: the URL decides the graph; replaces whatever is shown.
  useEffect(() => {
    if (!urlAct || !urlSection) { setHead(null); setNodes([]); setEdges([]); return }
    setAct(urlAct); setQuery(urlSection); setSelected(null); setLoading(true); setError('')
    setHides([]); setPinned(new Set()); setFrozen(false); fitted.current = false
    api.taxChains(urlAct, urlSection)
      .then((d: Payload) => {
        setHead(d)
        if (d.error) setError(d.error)
        setNodes(d.nodes || [])
        setEdges(d.edges || [])
      })
      .catch((e: Error) => { setHead(null); setNodes([]); setEdges([]); setError(e.message) })
      .finally(() => setLoading(false))
  }, [urlAct, urlSection])

  // Merge another section's graph in, deduped by node id and edge identity.
  const mergeIn = (a: string, s: string) => {
    api.taxChains(a, s).then((d: Payload) => {
      if (!d.nodes) return
      setFrozen(false) // new nodes settle, then onEngineStop freezes again
      setNodes((prev) => {
        const seen = new Set(prev.map((n) => n.id))
        return [...prev, ...d.nodes!.filter((n) => !seen.has(n.id))]
      })
      setEdges((prev) => {
        const key = (e: CEdge) => `${idOf(e.source)}|${idOf(e.target)}|${e.kind}`
        const seen = new Set(prev.map(key))
        return [...prev, ...(d.edges || []).filter((e) => !seen.has(key(e)))]
      })
    }).catch(() => {})
  }

  const graphData = useMemo(() => {
    const ids = new Set(nodes.map((n) => n.id))
    // force-graph throws on dangling ends; drop them rather than invent nodes
    return { nodes, links: edges.filter((e) => ids.has(idOf(e.source)) && ids.has(idOf(e.target))) }
  }, [nodes, edges])

  // Hidden node ids, derived from the hide list; the loaded data is never touched.
  const hiddenIds = useMemo(() => {
    const out = new Set<string>()
    for (const h of hides) {
      if (h.mode === 'node') out.add(h.id)
      else if (h.mode === 'branch') reach(h.id, graphData.links).forEach((i) => out.add(i))
      else {
        const keep = new Set([...reach(h.id, graphData.links), ...reach(h.id, graphData.links, true), ...pinned])
        nodes.forEach((n) => { if (!keep.has(n.id)) out.add(n.id) })
      }
    }
    return out
  }, [hides, graphData, nodes, pinned])

  const hide = (mode: Hide['mode']) => {
    if (!selected) return
    setHides((h) => [...h.filter((x) => x.id !== selected.id), { id: selected.id, mode }])
    if (mode !== 'focus') setSelected(null)
  }

  // Freeze = fix every node where it stands, so no tick can move anything.
  // pinned holds only the user's drags; those survive Reheat.
  const freeze = () => {
    nodes.forEach((n) => { n.fx = n.x; n.fy = n.y })
    setFrozen(true)
  }
  const reheat = (keep: Set<string>) => {
    nodes.forEach((n) => { if (!keep.has(n.id)) { n.fx = undefined; n.fy = undefined } })
    setFrozen(false)
    fgRef.current?.d3ReheatSimulation()
  }
  const releaseAll = () => { setPinned(new Set()); reheat(new Set()) }

  const go = (e: React.FormEvent) => {
    e.preventDefault()
    const p = parseQuery(query, act)
    if (p) navigate(`/chains/${encodeURIComponent(p[0])}/${encodeURIComponent(p[1])}`)
  }

  const onNodeClick = (n: CNode) => {
    const now = Date.now()
    const dbl = lastClick.current.id === n.id && now - lastClick.current.t < 350
    lastClick.current = { id: n.id, t: now }
    setSelected(n)
    if (dbl && n.kind === 'limb' && n.section) mergeIn(actOf(n), n.section)
  }

  const edgeColour: Record<string, string> = {
    contains: c.line, gate: c.limb, audited_from: c.gold, may_come_from: c.amber, feeds: c.green,
  }

  const drawNode = (n: CNode, ctx: CanvasRenderingContext2D, scale: number) => {
    const r = n.kind === 'section' ? 8 : n.kind === 'limb' ? 5 : 3
    const fill = n.kind === 'section' ? c.accent : n.kind === 'limb' ? c.limb : c.muted
    ctx.beginPath()
    ctx.arc(n.x!, n.y!, r, 0, 2 * Math.PI)
    ctx.fillStyle = fill
    ctx.fill()
    if (n.kind === 'limb' && n.shared) {
      ctx.lineWidth = 2 / scale
      ctx.strokeStyle = c.gold
      ctx.beginPath(); ctx.arc(n.x!, n.y!, r + 2.5, 0, 2 * Math.PI); ctx.stroke()
    }
    if (n.kind === 'fact' && n.consumer) {
      ctx.lineWidth = 1 / scale
      ctx.setLineDash([2 / scale, 2 / scale])
      ctx.strokeStyle = c.ink
      ctx.beginPath(); ctx.arc(n.x!, n.y!, r + 2, 0, 2 * Math.PI); ctx.stroke()
      ctx.setLineDash([])
    }
    if (pinned.has(n.id)) {
      ctx.lineWidth = 0.75 / scale
      ctx.strokeStyle = c.ink
      ctx.beginPath(); ctx.arc(n.x!, n.y!, r + 1.5, 0, 2 * Math.PI); ctx.stroke()
    }
    if (selected?.id === n.id) {
      ctx.lineWidth = 1.5 / scale
      ctx.strokeStyle = c.ink
      ctx.beginPath(); ctx.arc(n.x!, n.y!, r + 4, 0, 2 * Math.PI); ctx.stroke()
    }
    const fs = Math.max(10 / scale, n.kind === 'fact' ? 2.5 : 3.5)
    ctx.font = `${fs}px ${c.font}`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'top'
    ctx.fillStyle = n.kind === 'fact' ? c.muted : c.ink
    ctx.fillText(trunc(nameOf(n)), n.x!, n.y! + r + 1.5)
  }

  const toggle = (k: string) => setHidden((h) => {
    const next = new Set(h)
    if (next.has(k)) next.delete(k); else next.add(k)
    return next
  })

  const counts = head?.counts
  const empty = !loading && urlSection && nodes.length === 0

  return (
    <div className={`lk-chains${isMobile ? ' lk-chains--mobile' : ''}`}>
      <form className="lk-chains__search" onSubmit={go}>
        <select value={act} onChange={(e) => setAct(e.target.value)} aria-label="Act">
          {acts.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="152-10 or itaa-1936 340"
          aria-label="Section"
        />
        <button type="submit" className="lk-reader-btn lk-reader-btn--primary">Go</button>
      </form>

      <div className="lk-chains__head">
        <span className="lk-chains__alpha">ALPHA</span>
        <strong>Interpretive chains</strong>
        {head?.section && (
          <>
            <span>{head.act} s {head.section}</span>
            {head.title && <span className="lk-chains__muted">{head.title}</span>}
            {head.division && <span className="lk-chains__muted">Div {head.division}</span>}
          </>
        )}
        {counts && (
          <span className="lk-chains__muted">
            {Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(' · ')}
          </span>
        )}
      </div>

      <div className="lk-chains__chips">
        {EDGE_KINDS.map((k) => (
          <button
            key={k}
            type="button"
            aria-pressed={!hidden.has(k)}
            className={`lk-chains__chip lk-chains__chip--${k}${hidden.has(k) ? ' is-off' : ''}`}
            onClick={() => toggle(k)}
          >{k.replace(/_/g, ' ')}</button>
        ))}
        <button type="button" className="lk-chains__chip" onClick={() => fgRef.current?.zoomToFit(400, 40)}>Reset view</button>
        {nodes.length > 0 && (
          <>
            <button type="button" className="lk-chains__chip" aria-pressed={frozen} onClick={() => (frozen ? reheat(pinned) : freeze())}>{frozen ? 'Reheat' : 'Freeze'}</button>
            {pinned.size > 0 && <button type="button" className="lk-chains__chip" onClick={releaseAll}>Release all ({pinned.size})</button>}
          </>
        )}
        {hides.length > 0 && (
          <details className="lk-chains__hidden">
            <summary className="lk-chains__chip">Hidden ({hiddenIds.size})</summary>
            <div className="lk-chains__hidden-list">
              {hides.map((h) => {
                const n = nodes.find((x) => x.id === h.id)
                return (
                  <button key={h.id} type="button" className="lk-chains__chip" title="Restore"
                    onClick={() => setHides((l) => l.filter((x) => x.id !== h.id))}>
                    {h.mode === 'focus' ? 'focus' : h.mode}: {trunc(n ? nameOf(n) : h.id)} ×
                  </button>
                )
              })}
              <button type="button" className="lk-chains__chip" onClick={() => setHides([])}>Unhide all</button>
            </div>
          </details>
        )}
      </div>

      <div className="lk-chains__body">
        <div className="lk-chains__graph" ref={boxRef}>
          {loading && <div className="lk-chains__state">Loading</div>}
          {empty && (
            <div className="lk-chains__state">
              <p>No chains recorded for this provision.</p>
              {error === 'tests unavailable' && <p className="lk-chains__muted">The chains service is not responding.</p>}
              <p className="lk-chains__muted">Try 152-10, or ITAA 1936 340.</p>
            </div>
          )}
          {!urlSection && (
            <div className="lk-chains__state">
              <p className="lk-chains__muted">Enter a section to see where it sits in the chains. Try 152-10, or ITAA 1936 340.</p>
            </div>
          )}
          {nodes.length > 0 && (
            <ForceGraph2D
              ref={fgRef}
              width={size.w}
              height={size.h}
              graphData={graphData}
              nodeId="id"
              nodeLabel={(n) => (n as CNode).title || nameOf(n as CNode)}
              nodeCanvasObject={(n, ctx, s) => drawNode(n as CNode, ctx, s)}
              nodePointerAreaPaint={(n, colour, ctx) => {
                const o = n as CNode
                ctx.fillStyle = colour
                ctx.beginPath(); ctx.arc(o.x!, o.y!, o.kind === 'section' ? 10 : 7, 0, 2 * Math.PI); ctx.fill()
              }}
              nodeVisibility={(n) => !hiddenIds.has((n as CNode).id)}
              linkVisibility={(e) => {
                const l = e as CEdge
                return !hidden.has(l.kind) && !hiddenIds.has(idOf(l.source)) && !hiddenIds.has(idOf(l.target))
              }}
              linkColor={(e) => edgeColour[(e as CEdge).kind] || c.line}
              linkWidth={(e) => ((e as CEdge).kind === 'contains' ? 0.5 : 1.2)}
              linkLineDash={(e) => DASH[(e as CEdge).kind] || null}
              linkLabel={(e) => (e as CEdge).note || (e as CEdge).kind}
              linkDirectionalArrowLength={4}
              linkDirectionalArrowRelPos={1}
              linkDirectionalArrowColor={(e) => edgeColour[(e as CEdge).kind] || c.line}
              onNodeClick={(n) => onNodeClick(n as CNode)}
              onBackgroundClick={() => setSelected(null)}
              onNodeDragEnd={(n) => {
                const o = n as CNode
                o.fx = o.x; o.fy = o.y
                setPinned((p) => new Set(p).add(o.id))
              }}
              cooldownTicks={150}
              d3AlphaDecay={0.05}
              onEngineStop={() => {
                freeze()
                if (!fitted.current) { fitted.current = true; fgRef.current?.zoomToFit(400, 40) }
              }}
            />
          )}
        </div>

        {selected && (
          <aside className="lk-chains__panel" aria-label="Node detail">
            <button type="button" className="lk-chains__close" onClick={() => setSelected(null)} aria-label="Close">×</button>
            <div className="lk-chains__kind">{selected.kind}{selected.shared ? ' · shared' : ''}{selected.consumer ? ' · consumer' : ''}</div>
            <h3>{nameOf(selected)}</h3>
            <dl>
              {selected.role && (<><dt>Role</dt><dd>{selected.role}</dd></>)}
              {selected.provision && (<><dt>Provision</dt><dd>{selected.provision}</dd></>)}
              {selected.title && (<><dt>{selected.kind === 'fact' ? 'Question' : 'Text'}</dt><dd>{selected.title}</dd></>)}
              {selected.fact && (<><dt>Fact</dt><dd className="lk-chains__mono">{selected.fact}</dd></>)}
              <dt>Id</dt><dd className="lk-chains__mono">{selected.id}</dd>
            </dl>
            <div className="lk-chains__actions">
              {selected.section && (
                <button type="button" className="lk-reader-btn" onClick={() => navigate(`/${actOf(selected)}/${selected.section}`)}>Open section</button>
              )}
              {selected.kind === 'limb' && selected.section && (
                <button type="button" className="lk-reader-btn lk-reader-btn--primary" onClick={() => mergeIn(actOf(selected), selected.section!)}>Show chains</button>
              )}
              <button type="button" className="lk-reader-btn" onClick={() => hide('branch')}>Hide branch</button>
              <button type="button" className="lk-reader-btn" onClick={() => hide('node')}>Hide node</button>
              <button type="button" className="lk-reader-btn" onClick={() => hide('focus')}>Focus branch</button>
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}
