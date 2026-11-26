import { useEffect, useRef } from 'react'

export default function YarnBall() {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const lkYarn = (window as any).lkYarn
    if (typeof lkYarn !== 'function') return

    const yarn = lkYarn(cv, { showCat: false, catShadow: false })

    const local = (e: { clientX: number; clientY: number }) => {
      const r = cv.getBoundingClientRect()
      return [(e.clientX - r.left) * 1440 / r.width, (e.clientY - r.top) * 780 / r.height]
    }
    const onMove = (e: PointerEvent) => {
      const p = local(e)
      cv.style.cursor = yarn.hit(p[0], p[1]) ? 'pointer' : ''
    }
    const onClick = (e: MouseEvent) => {
      const p = local(e)
      yarn.click(p[0], p[1])
    }

    cv.addEventListener('pointermove', onMove)
    cv.addEventListener('click', onClick)

    return () => {
      cv.removeEventListener('pointermove', onMove)
      cv.removeEventListener('click', onClick)
      if (yarn.stop) yarn.stop()
    }
  }, [])

  return (
    <canvas
      ref={ref}
      width={1440}
      height={780}
      aria-hidden="true"
      style={{ width: '100%', maxWidth: 520, height: 'auto', display: 'block', margin: '4px auto 0', outline: 'none', WebkitTapHighlightColor: 'transparent', userSelect: 'none', WebkitUserSelect: 'none' }}
    />
  )
}
