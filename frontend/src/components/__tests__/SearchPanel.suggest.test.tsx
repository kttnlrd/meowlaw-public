/**
 * CDN-0099 — live autocomplete while typing.
 *
 * Commit 7066299a5 deliberately removed the debounced suggest effect; this test
 * pins the restored behaviour: a 2-character minimum, a 250 ms debounce, one
 * `api.suggest(q, 5)` call per typing burst, top-5 rendered inline in the main
 * results area (not a dropdown), and no out-of-order overwrite. It fails
 * against the removed-effect state (the first assertion sees zero calls).
 */
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

vi.mock('../../api', () => ({
  api: {
    suggest: vi.fn(),
    search: vi.fn(async () => ({ results: [] })),
    searchHybrid: vi.fn(async () => ({ results: [] })),
  },
}))

import { api } from '../../api'
import SearchPanel from '../SearchPanel'

const SUGGEST_LIMIT = 5

function suggestion(title: string) {
  return { act: 'itaa-1997', section: '6-5', title, type: 'section' }
}

function renderPanel() {
  return render(
    <MemoryRouter>
      <SearchPanel
        acts={[{ id: 'itaa-1997', name: 'Income Tax Assessment Act 1997' }]}
        onNavigate={() => {}}
        isMobile={false}
      />
    </MemoryRouter>,
  )
}

const input = () => screen.getByPlaceholderText(/Search legislation/)

async function type(text: string, advanceMs: number) {
  await act(async () => {
    fireEvent.change(input(), { target: { value: text } })
  })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(advanceMs)
  })
}

describe('SearchPanel live suggestions (CDN-0099)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    ;(api.suggest as Mock).mockReset()
    ;(api.suggest as Mock).mockResolvedValue({ suggestions: [suggestion('Gene technology')] })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('does not call suggest below the 2-character minimum', async () => {
    renderPanel()
    await type('g', 250)
    expect(api.suggest).not.toHaveBeenCalled()
    expect(screen.queryByText('Gene technology')).not.toBeInTheDocument()
  })

  it('debounces a typing burst into one call and renders the dropdown', async () => {
    renderPanel()
    // four keystrokes, each inside the debounce window of the previous one
    for (const text of ['g', 'ge', 'gen', 'gene']) {
      await act(async () => {
        fireEvent.change(input(), { target: { value: text } })
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50)
      })
    }
    expect(api.suggest).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250)
    })

    expect(api.suggest).toHaveBeenCalledTimes(1)
    expect((api.suggest as Mock).mock.calls[0]).toEqual(['gene', SUGGEST_LIMIT])
    expect(screen.getByText('Gene technology')).toBeInTheDocument()
  })

  it('ignores an out-of-order response — "ge" must not replace "gene"', async () => {
    const pending: Record<string, (v: unknown) => void> = {}
    ;(api.suggest as Mock).mockImplementation(
      (q: string) => new Promise(resolve => { pending[q] = resolve }),
    )
    renderPanel()

    // "ge" is issued ...
    await type('ge', 250)
    expect(api.suggest).toHaveBeenCalledTimes(1)

    // ... then "gene" is typed before the "ge" response lands
    await type('gene', 250)
    expect(api.suggest).toHaveBeenCalledTimes(2)

    // the newer response arrives first
    await act(async () => {
      pending['gene']({ suggestions: [suggestion('Gene technology for the newer query')] })
      await Promise.resolve()
    })
    expect(screen.getByText('Gene technology for the newer query')).toBeInTheDocument()

    // the stale "ge" response arrives late and must be dropped
    await act(async () => {
      pending['ge']?.({ suggestions: [suggestion('Stale result for ge')] })
      await Promise.resolve()
    })
    expect(screen.queryByText('Stale result for ge')).not.toBeInTheDocument()
    expect(screen.getByText('Gene technology for the newer query')).toBeInTheDocument()
  })

  it('clears the dropdown when the query drops below two characters', async () => {
    renderPanel()
    await type('gene', 250)
    expect(screen.getByText('Gene technology')).toBeInTheDocument()

    await type('g', 250)
    expect(screen.queryByText('Gene technology')).not.toBeInTheDocument()
  })
})
