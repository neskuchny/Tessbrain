'use client'

/**
 * Reveal — плавное появление блока при входе во вьюпорт (wow-слой лендинга).
 * IntersectionObserver + CSS-переход, без GSAP. Уважает reduced-motion
 * (класс сразу видимый — см. globals.css).
 */
import { useEffect, useRef, type ReactNode } from 'react'

export default function Reveal({
  children, delay = 0, className = '',
}: { children: ReactNode; delay?: number; className?: string }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            el.classList.add('is-revealed')
            io.disconnect()
          }
        }
      },
      { threshold: 0.15 },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [])

  return (
    <div ref={ref} className={`landing-reveal ${className}`}
      style={delay ? { transitionDelay: `${delay}ms` } : undefined}>
      {children}
    </div>
  )
}
