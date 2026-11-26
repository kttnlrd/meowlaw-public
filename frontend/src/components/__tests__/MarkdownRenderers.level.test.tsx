import { describe, it, expect } from 'vitest'
import { subparagraphLevel } from '../MarkdownRenderers'

describe('subparagraphLevel', () => {
  it('indents inserted multi-letter paragraphs like single-letter ones', () => {
    expect(subparagraphLevel('(1) text')).toBe(0)
    expect(subparagraphLevel('(b) text')).toBe(1)
    expect(subparagraphLevel('(ba) text')).toBe(1)
    expect(subparagraphLevel('(ii) text')).toBe(2)
    expect(subparagraphLevel('(A) text')).toBe(3)
  })
})
