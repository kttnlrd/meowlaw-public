/**
 * Withdrawn authorities: a ruling row with withdrawal_status 'withdrawn' shows a
 * visible Withdrawn badge and leads with the document's own citation
 * (TD 93/12W, ATO ID 2001/1 — never a paragraph pinpoint). Current rulings get
 * no badge. Order is the server's. Both sort paths (hybrid / per-act) are covered.
 * Rows are the live /api/search/hybrid response fields, 10 Oct 2026.
 */
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { render, act, fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const ROWS = [
  { act: 'rulings', section: 'TD 1993/12', source_type: 'ruling', withdrawal_status: 'withdrawn', own_citation: 'TD 93/12W',
    title: 'Income tax: substantiation: what are the consequences if a taxpayer fails to respond to a notice to produce documentary evidence' },
  { act: 'rulings', section: 'TR 2024/1', source_type: 'ruling', withdrawal_status: 'current',
    title: 'Income tax: composite items - identifying the relevant depreciating asset for capital allowance purposes' },
  { act: 'rulings', section: 'AID 2001/1', source_type: 'ruling', withdrawal_status: 'withdrawn', own_citation: 'ATO ID 2001/1',
    title: 'AID 2001/1 — Eligible termination payment: Extension of Time to Roll Over' },
]
// Per-act (/api/search?act=rulings) rows carry the stem as `section`.
const ACT_ROWS = ROWS.map(r => ({ ...r, section: r.section.replace(/[ /]/g, '_'), source_type: undefined, type: 'ruling' }))

vi.mock('../../api', () => ({
  api: {
    searchWarm: vi.fn(async () => ({})),
    searchScopes: vi.fn(async () => ({ 'au-tax': ['rulings'] })),
    suggest: vi.fn(async () => ({ suggestions: [] })),
    search: vi.fn(async () => ({ results: ACT_ROWS })),
    searchHybrid: vi.fn(async () => ({ results: ROWS, total: ROWS.length })),
  },
}))

import SearchPanel from '../SearchPanel'

async function runSearch() {
  render(
    <MemoryRouter>
      <SearchPanel acts={[{ id: 'rulings', name: 'ATO rulings' }]} onNavigate={() => {}} isMobile={false} />
    </MemoryRouter>,
  )
  const box = screen.getByLabelText('Search legislation')
  fireEvent.change(box, { target: { value: 'TD 93/12' } })
  await act(async () => { fireEvent.keyDown(box, { key: 'Enter' }) })
}

function cards() {
  return [...document.querySelectorAll('.lk-result-card')].map(c => ({
    title: c.querySelector('.lk-result-card__title')?.textContent,
    withdrawn: !!c.querySelector('.lk-badge--withdrawn'),
    href: c.getAttribute('href'),
  }))
}

describe('SearchPanel withdrawn rulings', () => {
  it('best match: badge + own citation, current unbadged, order kept', async () => {
    await runSearch()
    const got = cards()
    expect(got.map(c => c.withdrawn)).toEqual([true, false, true])
    expect(got[0].title).toMatch(/^TD 93\/12W — Income tax: substantiation/)
    expect(got[1].title).toMatch(/^TR 2024\/1 — Income tax: composite items/)
    expect(got[2].title).toBe('ATO ID 2001/1 — Eligible termination payment: Extension of Time to Roll Over')
    expect(got[0].href).toBe('/rulings/TD_1993_12') // link still built from the corpus reference
    expect(screen.getAllByText('Withdrawn')).toHaveLength(2)
  })

  it('per-act sort: same badge and citation', async () => {
    await runSearch()
    fireEvent.click(screen.getByRole('button', { name: /options/i }))
    fireEvent.change(document.getElementById('lk-fsort')!, { target: { value: 'bysection' } })
    const box = screen.getByLabelText('Search legislation')
    await act(async () => { fireEvent.keyDown(box, { key: 'Enter' }) })
    const got = cards()
    expect(got.map(c => c.withdrawn)).toEqual([true, false, true])
    expect(got[0].title).toMatch(/^TD 93\/12W — /)
    expect(got[2].title).toMatch(/^ATO ID 2001\/1 — /)
    expect(got[0].href).toBe('/rulings/TD_1993_12')
  })
})
