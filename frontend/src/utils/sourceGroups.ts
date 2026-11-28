// Sidebar source groups. One classifier for both instances so the panel reads the same way
// wherever MeowLaw is served: the Acts people actually open first, then by jurisdiction, then
// everything else, and the non-legislation resources last.
import { STATE_TAX_ACTS } from './display'

export type SourceItem = { id: string; label: string; to: string }
export type SourceGroup = { label: string; items: SourceItem[] }

// Pinned, in this order: the five Acts that carry most of the work.
export const CORE_ACTS = ['itaa-1997', 'itaa-1936', 'corporations-act-2001', 'gst-1999', 'taa-1953']

// Commonwealth tax Acts. Anything Commonwealth and not here is "Other".
const FEDERAL_TAX = new Set([
  'itaa-1997', 'itaa-1936', 'gst-1999', 'fbt-1986', 'taa-1953', 'sis-1993',
  'aml-ctf-2006', 'aml-ctf-rules-2007', 'aml-ctf-rules-2025', 'aml-ctf-transitional-rules-2026',
])

const STATE_IDS = new Set(STATE_TAX_ACTS.map(a => a.id))
const STATE_PREFIXES = ['nsw-', 'qld-', 'tas-', 'sa-', 'wa-', 'vic-', 'nt-', 'act-']

// /api/acts also lists the browse-only surfaces (rulings, maps, spec). Those have their own
// groups below, so they must not appear as legislation.
const NON_ACT_IDS = new Set([
  'private-rulings', 'rulings', 'nz-rulings', 'treaties', 'definitions',
  'maps', 'afsa-guides', 'regulatory-guides', 'proposed-law', 'spec',
  'commentary', 'insolvency', 'nz-mtg',
])

const isState = (id: string) => STATE_IDS.has(id) || STATE_PREFIXES.some(p => id.startsWith(p))
const isNz = (id: string) => id.startsWith('nz-')

// Display order for server-supplied groups; any other group follows in the order it appears.
const GROUP_ORDER = ['Tax resources', 'Professional', 'Tools']
const LEAD_GROUPS = ['Case law']

export type GroupOptions = {
  /** /proposed-law exists (private-instance carries the proposed-law corpus and browser). */
  proposedLaw?: boolean
  /** AFSA guides and ASIC regulatory guides have their own pages. */
  guides?: boolean
}

// If a corpus ever serves a shorthand or the raw slug as its name, the panel still reads properly.
const KNOWN_NAMES: Record<string, string> = {
  'itaa-1997': 'Income Tax Assessment Act 1997',
  'itaa-1936': 'Income Tax Assessment Act 1936',
  'fbt-1986': 'Fringe Benefits Tax Assessment Act 1986',
  'sis-1993': 'Superannuation Industry (Supervision) Act 1993',
  'taa-1953': 'Taxation Administration Act 1953',
  'gst-1999': 'A New Tax System (Goods and Services Tax) Act 1999',
  'corporations-act-2001': 'Corporations Act 2001',
}

const labelFor = (a: any): string => {
  const id = String(a.id)
  const served = String(a.name || a.short_name || '').trim()
  if (served && served.toLowerCase() !== id.toLowerCase() && served.length > 2) return served
  return KNOWN_NAMES[id] || id
}

const byLabel = (a: SourceItem, b: SourceItem) =>
  a.label.localeCompare(b.label, undefined, { sensitivity: 'base' })

export function buildSourceGroups(acts: any[], opts: GroupOptions = {}): SourceGroup[] {
  // Collections name their own sidebar group (collection.yaml); they skip the classifiers below.
  const collectionGroups = new Map<string, SourceItem[]>(
    GROUP_ORDER.map(g => [g, [] as SourceItem[]]))
  for (const a of acts || []) {
    if (!a || !a.id || !a.group) continue
    const g = String(a.group)
    if (!collectionGroups.has(g)) collectionGroups.set(g, [])
    collectionGroups.get(g)!.push({ id: String(a.id), label: labelFor(a), to: `/${a.id}` })
  }
  const items: SourceItem[] = (acts || [])
    .filter(a => a && a.id && !a.group && !NON_ACT_IDS.has(String(a.id)))
    .map(a => ({ id: String(a.id), label: labelFor(a), to: `/${a.id}` }))

  const took = new Set<string>()
  const take = (test: (i: SourceItem) => boolean) =>
    items.filter(i => {
      if (took.has(i.id) || !test(i)) return false
      took.add(i.id)
      return true
    })

  const core = CORE_ACTS
    .map(id => items.find(i => i.id === id))
    .filter((i): i is SourceItem => Boolean(i))
  core.forEach(i => took.add(i.id))

  const federal = take(i => FEDERAL_TAX.has(i.id)).sort(byLabel)
  const state = take(i => isState(i.id)).sort(byLabel)
  const nz = take(i => isNz(i.id)).sort(byLabel)
  // Tax cases browse from the legislation list, pinned to the head of the remaining-legislation group.
  const taxCases = items.find(i => i.id === 'tax-cases')
  if (taxCases) took.add(taxCases.id)
  const other = take(() => true).sort(byLabel)

  // Server groups that sit straight under the core Acts rather than after all the legislation.
  const lead = [...collectionGroups].filter(([label]) => LEAD_GROUPS.includes(label))
  lead.forEach(([label]) => collectionGroups.delete(label))

  return [
    ...(core.length ? [{ label: 'Core legislation', items: core }] : []),
    ...lead.map(([label, gi]) => ({ label, items: gi.sort(byLabel) })),
    { label: 'Federal tax', items: federal },
    { label: 'State tax', items: state },
    { label: 'New Zealand legislation', items: nz },
    { label: 'Other legislation', items: [...(taxCases ? [taxCases] : []), ...other] },
    // Every other group comes from the server (/api/acts "group"): browse-only sources and
    // collections alike. No links are listed here.
    ...[...collectionGroups].map(([label, gi]) => ({ label, items: gi.sort(byLabel) })),
  ].filter((g): g is SourceGroup => g.items.length > 0)
}
