/// Shared display utilities for legislation explorer.

/** State tax Acts (practice area "State taxes"); mirrors backend/doc_ids.py STATE_TAX_ACTS. */
export const STATE_TAX_ACTS: { id: string; title: string; jur: 'NSW' | 'Qld' | 'Tas' | 'SA' | 'WA' }[] = [
  { id: 'qld-duties-2001', title: 'Duties Act 2001', jur: 'Qld' },
  { id: 'qld-landtax-2010', title: 'Land Tax Act 2010', jur: 'Qld' },
  { id: 'qld-payrolltax-1971', title: 'Payroll Tax Act 1971', jur: 'Qld' },
  { id: 'tas-duties-2001', title: 'Duties Act 2001', jur: 'Tas' },
  { id: 'tas-landtax-2000', title: 'Land Tax Act 2000', jur: 'Tas' },
  { id: 'tas-payrolltax-2008', title: 'Payroll Tax Act 2008', jur: 'Tas' },
  { id: 'sa-stampduties-1923', title: 'Stamp Duties Act 1923', jur: 'SA' },
  { id: 'sa-landtax-1936', title: 'Land Tax Act 1936', jur: 'SA' },
  { id: 'sa-payrolltax-2009', title: 'Payroll Tax Act 2009', jur: 'SA' },
  { id: 'wa-duties-2008', title: 'Duties Act 2008', jur: 'WA' },
  { id: 'wa-landtaxassessment-2002', title: 'Land Tax Assessment Act 2002', jur: 'WA' },
  { id: 'wa-payrolltaxassessment-2002', title: 'Pay-roll Tax Assessment Act 2002', jur: 'WA' },
  { id: 'nsw-duties-1997', title: 'Duties Act 1997', jur: 'NSW' },
  { id: 'nsw-landtaxmgmt-1956', title: 'Land Tax Management Act 1956', jur: 'NSW' },
  { id: 'nsw-landtax-1956', title: 'Land Tax Act 1956', jur: 'NSW' },
  { id: 'nsw-payrolltax-2007', title: 'Payroll Tax Act 2007', jur: 'NSW' },
]
export const JURISDICTION_NAME: Record<string, string> = { NSW: 'New South Wales', Qld: 'Queensland', Tas: 'Tasmania', SA: 'South Australia', WA: 'Western Australia' }
const STATE_TAX_NAMES = Object.fromEntries(STATE_TAX_ACTS.map(a => [a.id, `${a.title} (${a.jur})`]))

const ACT_SHORT: Record<string, string> = {
  'itaa-1997': 'ITAA97',
  'itaa-1936': 'ITAA36',
  'gst-1999': 'GST99',
  'taa-1953': 'TAA53',
  'fbt-1986': 'FBTAA 1986',
  'sis-1993': 'SIS 1993',
  'nz-it-2007': 'NZ IT07',
  'tax-cases': 'Tax Cases',
  'rulings': 'ATO Public Rulings',
  'nz-rulings': 'NZ tax rulings',
  'corporations-act-2001': 'Corps Act',
  'regulatory-guides': 'ASIC RGs',
  'afsa-guides': 'AFSA Practice',
  'bankruptcy-act-1966': 'Bankruptcy Act',
  'aml-ctf-2006': 'AML/CTF Act',
  'aml-ctf-rules-2007': 'AML/CTF Rules',
  'spec': 'Display Spec',
  'treaties': 'Tax Treaties',
  'argentina': 'Argentina',
  'austria': 'Austria',
  'belgium': 'Belgium',
  'canada': 'Canada',
  'chile': 'Chile',
  'china': 'China',
  'czech-republic': 'Czech Republic',
  'denmark': 'Denmark',
  'fiji': 'Fiji',
  'finland': 'Finland',
  'france': 'France',
  'hungary': 'Hungary',
  'iceland': 'Iceland',
  'india': 'India',
  'indonesia': 'Indonesia',
  'ireland': 'Ireland',
  'israel': 'Israel',
  'italy': 'Italy',
  'kiribati': 'Kiribati',
  'korea': 'Korea',
  'malaysia': 'Malaysia',
  'malta': 'Malta',
  'mexico': 'Mexico',
  'netherlands': 'Netherlands',
  'new-zealand': 'New Zealand',
  'norway': 'Norway',
  'papua-new-guinea': 'Papua New Guinea',
  'philippines': 'Philippines',
  'poland': 'Poland',
  'romania': 'Romania',
  'russia': 'Russia',
  'singapore': 'Singapore',
  'slovakia': 'Slovakia',
  'south-africa': 'South Africa',
  'spain': 'Spain',
  'sri-lanka': 'Sri Lanka',
  'sweden': 'Sweden',
  'taipei': 'Taipei',
  'thailand': 'Thailand',
  'turkey': 'Turkey',
  'usa': 'USA',
  'vietnam': 'Vietnam',
  ...STATE_TAX_NAMES,
}

/** Short display name for an act ID. Falls back to the original name. */
export function shortActName(actId: string): string {
  return ACT_SHORT[actId] || actId.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

/** Format a section reference like "ITAA97 s8-1". */
export function formatSectionRef(actId: string, section: string): string {
  return `${shortActName(actId)} s${section}`
}

/** Fuller act name for result-card titles — abbreviation + year, not the
 *  compact ACT_SHORT token ("ITAA36"). Falls back to shortActName. */
const ACT_TITLE: Record<string, string> = {
  'itaa-1997': 'ITAA 1997',
  'itaa-1936': 'ITAA 1936',
  'gst-1999': 'GST 1999',
  'taa-1953': 'TAA 1953',
  'fbt-1986': 'FBTAA 1986',
  'sis-1993': 'SIS 1993',
  'nz-it-2007': 'NZ IT 2007',
  'corporations-act-2001': 'Corporations Act 2001',
  'bankruptcy-act-1966': 'Bankruptcy Act 1966',
  'aml-ctf-2006': 'AML/CTF Act 2006',
  'aml-ctf-rules-2007': 'AML/CTF Rules 2007',
  'regulatory-guides': 'ASIC RG',
  'afsa-guides': 'AFSA',
  ...STATE_TAX_NAMES,
}

export function actTitleName(actId: string): string {
  return ACT_TITLE[actId] || shortActName(actId)
}

/** Convert a filename-style citation slug ("1970_HCA_23") to a medium-neutral
 *  citation ("[1970] HCA 23"). Idempotent for already-formatted citations. */
export function normalizeCaseCitation(s: string | undefined | null): string {
  if (!s) return ''
  const m = /^(\d{4})_([A-Z]+)_(\d+)$/.exec(s)
  if (m) return `[${m[1]}] ${m[2]} ${m[3]}`
  return s
}

/**
 * Slugify a ruling citation for use as a URL path segment, matching the
 * backend's normalization (`re.sub(r'[\s/]+', '_', citation)`).
 * "TR 2014/5" -> "TR_2014_5", "TD 2020/1" -> "TD_2020_1".
 */
export function rulingSlug(citation: string): string {
  return citation.replace(/[\s/]+/g, '_')
}

/** Format a search result's act+snippet into a short label. */
export function formatSearchResult(result: { act: string; section?: string; title?: string }): string {
  const ref = result.section ? formatSectionRef(result.act, result.section) : shortActName(result.act)
  return result.title ? `${ref} — ${result.title}` : ref
}