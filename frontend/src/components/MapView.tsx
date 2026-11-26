import React, { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import dagre from 'dagre'

const API = ''

interface MapNode {
  id: string
  type: 'start' | 'event' | 'decision' | 'action' | 'outcome' | 'end'
  label: string
  body: string
  statute?: { act: string; section: string; title?: string }[]
  rulings?: { id: string; title?: string; note?: string }[]
  cases?: { citation: string; note?: string }[]
  definitions?: string[]
}

interface MapEdge {
  from: string
  to: string
  label?: string
}

interface ProceduralMap {
  id: string
  title: string
  short?: string
  refs?: string
  act: string
  division: string
  subdivision: string
  summary: string
  nodes: MapNode[]
  edges: MapEdge[]
}

// Node shape per step type. Colours live in CSS (.lk-map__node--<type>) so
// they follow the lawkitty tokens and the light/dark theme.
const NODE_STYLES: Record<string, { shape: 'rect' | 'diamond' | 'ellipse' }> = {
  start:     { shape: 'rect' },
  event:     { shape: 'rect' },
  decision:  { shape: 'diamond' },
  action:    { shape: 'rect' },
  outcome:   { shape: 'rect' },
  end:       { shape: 'rect' },
}

const LEGEND: { type: string; label: string }[] = [
  { type: 'start', label: 'start' },
  { type: 'event', label: 'event' },
  { type: 'decision', label: 'decision' },
  { type: 'action', label: 'action' },
  { type: 'outcome', label: 'outcome' },
  { type: 'end', label: 'no relief / end' },
]

const nodeTypeClass = (t: string) => (NODE_STYLES[t] ? t : 'start')

const Icon = ({ d, size = 16 }: { d: React.ReactNode; size?: number }) => (
  <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
    {d}
  </svg>
)
const ICON_CLOSE = <path d="M6 6l12 12M18 6L6 18" />

function computeLayout(map: ProceduralMap) {
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: 'TB', nodesep: 30, ranksep: 55, marginx: 20, marginy: 20 })
  for (const n of map.nodes) {
    const style = NODE_STYLES[n.type] || NODE_STYLES.start
    const w = style.shape === 'diamond' ? 200 : 240
    const h = style.shape === 'diamond' ? 110 : 64
    g.setNode(n.id, { width: w, height: h })
  }
  for (const e of map.edges) g.setEdge(e.from, e.to, { label: e.label || '' })
  dagre.layout(g)
  const positions = new Map<string, { x: number; y: number }>()
  for (const n of map.nodes) {
    const p = g.node(n.id)
    if (p) positions.set(n.id, { x: p.x, y: p.y })
  }
  return positions
}

interface Props {
  mapId: string
  onClose?: () => void
  onOpenSection: (act: string, section: string) => void
  height?: string
  isMobile?: boolean
}

export default function MapView({ mapId, onClose, onOpenSection, height, isMobile }: Props) {
  const [map, setMap] = useState<ProceduralMap | null>(null)
  const [selected, setSelected] = useState<MapNode | null>(null)
  const [error, setError] = useState<string | null>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const [view, setView] = useState({ scale: 0.9, x: 0, y: 0 })
  const dragRef = useRef<{ startX: number; startY: number; viewX: number; viewY: number; moved: boolean } | null>(null)
  const pointersRef = useRef(new Map<number, { x: number; y: number }>())
  const pinchRef = useRef<{ dist: number; midX: number; midY: number; scale: number; x: number; y: number } | null>(null)

  // Mobile detection — falls back to self-detect when prop not passed (MapModal)
  const [isMobileState, setIsMobileState] = useState(false)
  useEffect(() => {
    const check = () => setIsMobileState(window.innerWidth < 768)
    check()
    window.addEventListener('resize', check)
    return () => window.removeEventListener('resize', check)
  }, [])
  const mobile = isMobile ?? isMobileState

  useEffect(() => {
    setMap(null)
    setSelected(null)
    setError(null)
    fetch(`${API}/api/maps/${mapId}`)
      .then(r => { if (!r.ok) throw new Error('map not found'); return r.json() })
      .then(setMap)
      .catch(e => setError(e.message))
  }, [mapId])

  // A node link opened in a new tab lands back on this map with #node-<id> —
  // select that node once the map has loaded (F-15).
  useEffect(() => {
    if (!map) return
    const m = window.location.hash.match(/^#node-(.+)$/)
    if (!m) return
    const n = map.nodes.find(x => x.id === decodeURIComponent(m[1]))
    if (n) setSelected(n)
  }, [map])

  const layout = useMemo(() => (map ? computeLayout(map) : null), [map])

  const fitView = useCallback(() => {
    if (!map || !layout) return
    const minX = Math.min(...map.nodes.map(n => layout.get(n.id)!.x))
    const maxX = Math.max(...map.nodes.map(n => layout.get(n.id)!.x))
    const minY = Math.min(...map.nodes.map(n => layout.get(n.id)!.y))
    const maxY = Math.max(...map.nodes.map(n => layout.get(n.id)!.y))
    const containerW = svgRef.current?.parentElement?.clientWidth || 800
    const containerH = svgRef.current?.parentElement?.clientHeight || 600
    const pad = 60
    const scale = Math.min((containerW - pad) / (maxX - minX + 240), (containerH - pad) / (maxY - minY + 130), 1.1)
    const s = Math.max(scale, 0.2)
    setView({
      scale: s,
      x: containerW / 2 - s * (minX + (maxX - minX) / 2),
      y: containerH / 2 - s * (minY + (maxY - minY) / 2),
    })
  }, [map, layout])

  useEffect(() => {
    if (map && layout) {
      const t = setTimeout(fitView, 60)
      return () => clearTimeout(t)
    }
  }, [map, layout, fitView])

  const onWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault()
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
    const mx = e.clientX - rect.left
    const my = e.clientY - rect.top
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15
    setView(v => {
      const ns = Math.min(Math.max(v.scale * factor, 0.15), 3)
      const k = ns / v.scale
      return {
        scale: ns,
        x: mx - (mx - v.x) * k,
        y: my - (my - v.y) * k,
      }
    })
  }, [])

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    // Don't capture when the press starts on an interactive element (node or
    // button) — capture would swallow the subsequent click event.
    const t = e.target as Element
    if (t.closest('button') || t.closest('[data-node]')) return
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (pointersRef.current.size === 2) {
      const pts = [...pointersRef.current.values()]
      const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y)
      pinchRef.current = { dist, midX: (pts[0].x + pts[1].x) / 2, midY: (pts[0].y + pts[1].y) / 2, scale: view.scale, x: view.x, y: view.y }
      dragRef.current = null
    } else {
      dragRef.current = { startX: e.clientX, startY: e.clientY, viewX: view.x, viewY: view.y, moved: false }
    }
  }, [view])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (!pointersRef.current.has(e.pointerId)) return
    pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    const pts = [...pointersRef.current.values()]
    // Two-finger pinch zoom
    if (pts.length === 2 && pinchRef.current) {
      const p = pinchRef.current
      const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y)
      const midX = (pts[0].x + pts[1].x) / 2
      const midY = (pts[0].y + pts[1].y) / 2
      const ns = Math.min(Math.max(p.scale * (dist / p.dist), 0.15), 3)
      const k = ns / p.scale
      setView({
        scale: ns,
        x: midX - (p.midX - p.x) * k,
        y: midY - (p.midY - p.y) * k,
      })
      if (dragRef.current) dragRef.current.moved = true
      return
    }
    if (!dragRef.current) return
    // Snapshot the drag state — the setView updater runs async (React batches
    // pointermove updates) and pointerup may have nulled dragRef by then.
    // Reading the ref inside the updater crashes the render phase (blank screen).
    const d = dragRef.current
    const dx = e.clientX - d.startX
    const dy = e.clientY - d.startY
    if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true
    setView(v => ({ ...v, x: d.viewX + dx, y: d.viewY + dy }))
  }, [])

  const onPointerUp = useCallback((e: React.PointerEvent) => {
    pointersRef.current.delete(e.pointerId)
    if (pointersRef.current.size < 2) pinchRef.current = null
    if (pointersRef.current.size === 0) dragRef.current = null
  }, [])

  if (error) {
    return (
      <div className="lk-map-state" role="alert">
        <h2 className="lk-empty-state__title">This map wandered off.</h2>
        <div>The cat looked under the couch. Nothing.</div>
        <div className="lk-map__row-cite">{error}</div>
        {onClose && (
          <button type="button" className="lk-reader-btn" onClick={onClose} style={{ marginTop: 8 }}>Close</button>
        )}
      </div>
    )
  }

  if (!map || !layout) {
    return (
      <div className="lk-map-loading" style={{ height: height || '100%' }} role="status">
        Unrolling the map...
      </div>
    )
  }

  const zoomIn = () => setView(v => ({ ...v, scale: Math.min(v.scale * 1.2, 3) }))
  const zoomOut = () => setView(v => ({ ...v, scale: Math.max(v.scale / 1.2, 0.15) }))

  const detailBody = selected ? (
    <>
      <p className="lk-map__detail-title">{selected.label}</p>
      {selected.body && (
        <p className="lk-map__detail-body">{selected.body}</p>
      )}
      {selected.statute && selected.statute.length > 0 && (
        <div className="lk-map__group">
          <span className="lk-map__group-label">Statute</span>
          {selected.statute.map((s, i) => (
            <button key={i} type="button" className="lk-map__row"
                    onClick={() => onOpenSection(s.act, s.section.split('(')[0].trim())}>
              <span className="lk-badge lk-badge--act">Act</span>
              <span className="lk-map__row-cite">{s.section}</span>
              {s.title ? <span className="lk-map__row-title">{s.title}</span> : null}
            </button>
          ))}
        </div>
      )}
      {selected.rulings && selected.rulings.length > 0 && (
        <div className="lk-map__group">
          <span className="lk-map__group-label">ATO rulings</span>
          {selected.rulings.map((r, i) => (
            <button key={i} type="button" className="lk-map__row"
                    onClick={() => onOpenSection('rulings', r.id)}>
              <span className="lk-badge lk-badge--ruling">Ruling</span>
              <span className="lk-map__row-cite">{r.id.replace(/_/g, ' ')}</span>
              {r.title ? <span className="lk-map__row-title">{r.title}</span> : null}
              {r.note ? <span className="lk-map__row-title">{r.note}</span> : null}
            </button>
          ))}
        </div>
      )}
      {selected.cases && selected.cases.length > 0 && (
        <div className="lk-map__group">
          <span className="lk-map__group-label">Cases</span>
          {selected.cases.map((c, i) => (
            <div key={i} className="lk-map__row">
              <span className="lk-badge lk-badge--case">Case</span>
              <span className="lk-map__row-cite">{c.citation}</span>
              {c.note ? <span className="lk-map__row-title">{c.note}</span> : null}
            </div>
          ))}
        </div>
      )}
      {selected.definitions && selected.definitions.length > 0 && (
        <div className="lk-map__group">
          <span className="lk-map__group-label">Definitions</span>
          <div className="lk-map__terms">
            {selected.definitions.map((d, i) => (
              <span key={i} className="lk-map__term">{d}</span>
            ))}
          </div>
        </div>
      )}
    </>
  ) : null

  const detailHead = selected ? (
    <div className="lk-map__detail-head">
      <span className={`lk-map__type lk-map__type--${nodeTypeClass(selected.type)}`}>{selected.type}</span>
      <button type="button" className="lk-map__iconbtn lk-map__iconbtn--bare" onClick={() => setSelected(null)} aria-label="Close details">
        <Icon d={ICON_CLOSE} size={18} />
      </button>
    </div>
  ) : null

  return (
    <div className={`lk-map${mobile ? ' lk-map--mobile' : ''}`} style={{ height: height || '100%', minHeight: height ? 480 : 0 }}>
      {/* Header */}
      <div className="lk-map__head">
        <div className="lk-map__titles">
          <div className="lk-map__title-row">
            <h1 className="lk-map__title">{map.short || map.title}</h1>
            {onClose && mobile && (
              <button type="button" className="lk-map__iconbtn" onClick={onClose} aria-label="Close map">
                <Icon d={ICON_CLOSE} size={18} />
              </button>
            )}
          </div>
          {map.refs && <span className="lk-map__refs">{map.refs}</span>}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
          <div className="lk-map__legend" aria-label="Legend">
            {LEGEND.map(l => (
              <span key={l.type} className="lk-map__legend-item">
                <span className={`lk-map__swatch lk-map__swatch--${l.type}`} aria-hidden="true" />
                {l.label}
              </span>
            ))}
          </div>
          {onClose && !mobile && (
            <button type="button" className="lk-map__iconbtn" onClick={onClose} aria-label="Close map">
              <Icon d={ICON_CLOSE} size={18} />
            </button>
          )}
        </div>
      </div>

      <div className="lk-map__body">
        {/* Flowchart */}
        <div
          className="lk-map__canvas"
          onWheel={onWheel}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          <svg ref={svgRef} width="100%" height="100%" className="lk-map__svg">
            <g transform={`translate(${view.x}, ${view.y}) scale(${view.scale})`}>
              {/* edges */}
              {map.edges.map((e, i) => {
                const a = layout.get(e.from)!
                const b = layout.get(e.to)!
                const dx = b.x - a.x
                const dy = b.y - a.y
                const len = Math.sqrt(dx * dx + dy * dy) || 1
                const ux = dx / len, uy = dy / len
                const sx = a.x + ux * 32, sy = a.y + uy * 32
                const tx = b.x - ux * 32, ty = b.y - uy * 32
                const mx = (sx + tx) / 2, my = (sy + ty) / 2
                return (
                  <g key={i}>
                    <line x1={sx} y1={sy} x2={tx} y2={ty} className="lk-map__edge" markerEnd="url(#map-arrow)" />
                    {e.label && (
                      <text x={mx} y={my - 6} textAnchor="middle" className="lk-map__edge-label">
                        {e.label}
                      </text>
                    )}
                  </g>
                )
              })}
              <defs>
                <marker id="map-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" className="lk-map__arrow" />
                </marker>
              </defs>
              {/* nodes */}
              {map.nodes.map(n => {
                const p = layout.get(n.id)!
                const style = NODE_STYLES[n.type] || NODE_STYLES.start
                const isDiamond = style.shape === 'diamond'
                const w = isDiamond ? 200 : 240
                const h = isDiamond ? 110 : 64
                const isSel = selected?.id === n.id
                const fontSize = isDiamond ? 11 : 12
                const lineH = isDiamond ? 14 : 15
                const words = n.label.split(' ')
                const lineLen = Math.max(20, Math.min(32, Math.floor(w / (isDiamond ? 8.4 : 7.4))))
                const lines: string[] = []
                let cur = ''
                for (const word of words) {
                  if ((cur + ' ' + word).trim().length > lineLen && cur) { lines.push(cur.trim()); cur = word }
                  else cur = (cur + ' ' + word).trim()
                }
                if (cur) lines.push(cur)
                const shown = lines.slice(0, 3)
                const truncated = lines.length > 3
                const totalLines = shown.length + (truncated ? 1 : 0)
                return (
                  // Native SVG <a> (not react-router Link, which renders an
                  // HTML <a> and can't nest inside <svg>) so right-click /
                  // middle-click / ctrl-click "open in new tab" work (F-15).
                  // A plain left click still does the in-app node select.
                  <a key={n.id} href={`/maps/${mapId}#node-${encodeURIComponent(n.id)}`}
                     data-node
                     className={`lk-map__node lk-map__node--${nodeTypeClass(n.type)}${isSel ? ' lk-map__node--selected' : ''}`}
                     aria-label={`${n.type}: ${n.label}`}
                     onClick={(e) => {
                       if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return
                       e.preventDefault()
                       if (!dragRef.current?.moved) setSelected(n)
                     }}>
                    <g transform={`translate(${p.x - w / 2}, ${p.y - h / 2})`}>
                      {style.shape === 'diamond' ? (
                        <polygon className="lk-map__node-shape" points={`${w / 2},1 ${w - 1},${h / 2} ${w / 2},${h - 1} 1,${h / 2}`} strokeLinejoin="round" />
                      ) : style.shape === 'ellipse' ? (
                        <ellipse className="lk-map__node-shape" cx={w / 2} cy={h / 2} rx={w / 2 - 1} ry={h / 2 - 1} />
                      ) : (
                        <rect className="lk-map__node-shape" x={2} y={2} width={w - 4} height={h - 4} rx={14} />
                      )}
                      <text x={w / 2} y={h / 2 - ((totalLines - 1) * lineH) / 2 + fontSize * 0.35} textAnchor="middle" fontSize={fontSize} className="lk-map__node-text">
                        {shown.map((ln, li) => (
                          <tspan key={li} x={w / 2} dy={li === 0 ? 0 : lineH}>{ln}</tspan>
                        ))}
                        {truncated && <tspan x={w / 2} dy={lineH} className="lk-map__node-more">...</tspan>}
                      </text>
                    </g>
                  </a>
                )
              })}
            </g>
          </svg>
          {/* Zoom controls */}
          <div className="lk-map__controls">
            <button type="button" className="lk-map__iconbtn" onClick={zoomIn} aria-label="Zoom in">
              <Icon d={<path d="M12 5v14M5 12h14" />} />
            </button>
            <button type="button" className="lk-map__iconbtn" onClick={zoomOut} aria-label="Zoom out">
              <Icon d={<path d="M5 12h14" />} />
            </button>
            <button type="button" className="lk-map__iconbtn" onClick={fitView} aria-label="Fit to view" title="Fit to view">
              <Icon d={<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />} />
            </button>
          </div>
          <div className="lk-map__hint">
            {mobile ? 'Drag to pan · pinch to zoom · tap a node' : 'Scroll to zoom · drag to pan · click a node for details'}
          </div>
        </div>

        {/* Detail panel (desktop side panel) */}
        {!mobile && (
          <aside className="lk-map__detail" aria-label="Step details">
            {!selected ? (
              <div>
                <h2 className="lk-map__empty-title">Pick a node.</h2>
                <p className="lk-map__empty">The cat will fetch the statute, cases and definitions behind that step.</p>
              </div>
            ) : (
              <>
                {detailHead}
                {detailBody}
              </>
            )}
          </aside>
        )}

        {/* Detail sheet (mobile bottom sheet) */}
        {mobile && selected && (
          <aside className="lk-map__detail lk-map__detail--sheet" aria-label="Step details">
            {detailHead}
            {detailBody}
          </aside>
        )}
      </div>
    </div>
  )
}
