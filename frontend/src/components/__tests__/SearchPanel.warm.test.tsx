/**
 * Search warm-up: opening the search screen fires one fire-and-forget
 * `api.searchWarm()` per page session, and a failing warm-up never surfaces
 * (a rejected promise here would fail the run as an unhandled rejection).
 */
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { render, act } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../../api', () => ({
  api: {
    searchWarm: vi.fn(async () => { throw new Error('503: warm-up down') }),
    suggest: vi.fn(async () => ({ suggestions: [] })),
    search: vi.fn(async () => ({ results: [] })),
    searchHybrid: vi.fn(async () => ({ results: [] })),
  },
}))

import { api } from '../../api'
import SearchPanel, { warmSearch } from '../SearchPanel'

const panel = () => (
  <MemoryRouter>
    <SearchPanel acts={[{ id: 'itaa-1997', name: 'Income Tax Assessment Act 1997' }]} onNavigate={() => {}} isMobile={false} />
  </MemoryRouter>
)

describe('SearchPanel warm-up', () => {
  it('calls searchWarm once on mount, not again on remount or nav hover, and swallows failure', async () => {
    const first = render(panel())
    await act(async () => {})
    expect(api.searchWarm).toHaveBeenCalledTimes(1)

    first.unmount()
    render(panel())
    warmSearch()
    await act(async () => {})
    expect(api.searchWarm).toHaveBeenCalledTimes(1)
  })
})
