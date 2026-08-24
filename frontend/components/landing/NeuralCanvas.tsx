'use client'

/**
 * NeuralCanvas — живой «мозг» лендинга (wow-слой первой страницы).
 *
 * Рукописный canvas-2D вместо Three.js: ноль зависимостей в бандле, SSR-safe.
 * Облако нейронов в 3D-объёме с перспективной проекцией, медленное вращение,
 * параллакс за курсором, синапсы между ближними узлами и ИМПУЛЬСЫ — яркие
 * искры, бегущие по связям (нейрон «загорается» при получении).
 *
 * Честная производительность: DPR-кап, число частиц по ширине экрана, пауза
 * при скрытой вкладке/вне вьюпорта, prefers-reduced-motion → один статичный
 * кадр без анимации.
 */
import { useEffect, useRef } from 'react'

const TEAL = { r: 77, g: 193, b: 199 }    // #4dc1c7 — фирменный
const HOT = { r: 233, g: 69, b: 96 }      // #e94560 — акцент
const IMPULSE_EVERY_MS = 260
const IMPULSE_LIFE_MS = 700

interface Node3 {
  x: number; y: number; z: number
  seed: number; hot: boolean
  glow: number            // 0..1 — вспышка при приходе импульса
}
interface Impulse { from: number; to: number; born: number }

export default function NeuralCanvas({ className }: { className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    let width = 0, height = 0
    let raf = 0
    let running = true
    let visible = true

    // ── мир ──
    const count = window.innerWidth < 768 ? 90 : 170
    const R = 260 // радиус облака (мировые единицы)
    const nodes: Node3[] = []
    for (let i = 0; i < count; i++) {
      // сплюснутый эллипсоид с двумя «полушариями» — силуэт мозга, не шар
      const t = Math.random() * Math.PI * 2
      const u = Math.random() * 2 - 1
      const r = R * (0.55 + 0.45 * Math.cbrt(Math.random()))
      const lobe = Math.random() < 0.5 ? -0.55 : 0.55
      nodes.push({
        x: r * Math.sqrt(1 - u * u) * Math.cos(t) * 1.15 + lobe * 60,
        y: r * u * 0.72,
        z: r * Math.sqrt(1 - u * u) * Math.sin(t) * 0.9,
        seed: Math.random() * 1000,
        hot: Math.random() < 0.08,
        glow: 0,
      })
    }
    // статическая топология: k ближайших соседей
    const links: Array<[number, number]> = []
    const K = 3
    for (let i = 0; i < count; i++) {
      const d: Array<[number, number]> = []
      for (let j = 0; j < count; j++) {
        if (i === j) continue
        const dx = nodes[i].x - nodes[j].x
        const dy = nodes[i].y - nodes[j].y
        const dz = nodes[i].z - nodes[j].z
        d.push([dx * dx + dy * dy + dz * dz, j])
      }
      d.sort((a, b) => a[0] - b[0])
      for (let k = 0; k < K; k++) {
        const j = d[k][1]
        if (i < j) links.push([i, j])
      }
    }

    const impulses: Impulse[] = []
    let lastImpulse = 0
    let rotY = 0
    let targetTiltX = 0, targetTiltY = 0
    let tiltX = 0, tiltY = 0

    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      width = rect.width; height = rect.height
      canvas.width = Math.round(width * dpr)
      canvas.height = Math.round(height * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()

    const onMouse = (e: MouseEvent) => {
      targetTiltY = (e.clientX / window.innerWidth - 0.5) * 0.35
      targetTiltX = (e.clientY / window.innerHeight - 0.5) * 0.25
    }

    // проекция точки мира на экран
    const project = (n: Node3, time: number) => {
      // лёгкое дыхание узла
      const bx = Math.sin(time * 0.0005 + n.seed) * 6
      const by = Math.cos(time * 0.0004 + n.seed * 1.7) * 6
      const cosY = Math.cos(rotY + tiltY), sinY = Math.sin(rotY + tiltY)
      const cosX = Math.cos(tiltX), sinX = Math.sin(tiltX)
      let x = n.x + bx, y = n.y + by, z = n.z
      // вращение вокруг Y, затем X
      const x1 = x * cosY - z * sinY
      const z1 = x * sinY + z * cosY
      const y1 = y * cosX - z1 * sinX
      const z2 = y * sinX + z1 * cosX
      const persp = 620 / (620 + z2 + R)
      return {
        sx: width / 2 + x1 * persp,
        sy: height / 2 + y1 * persp * 0.98,
        depth: persp, // ~0.45..1.2 — ближе = больше
      }
    }

    const draw = (time: number) => {
      ctx.clearRect(0, 0, width, height)
      rotY += reduced ? 0 : 0.0009
      tiltX += (targetTiltX - tiltX) * 0.04
      tiltY += (targetTiltY - tiltY) * 0.04

      const proj = nodes.map((n) => project(n, time))

      // связи
      ctx.lineWidth = 1
      for (const [i, j] of links) {
        const a = proj[i], b = proj[j]
        const depth = (a.depth + b.depth) / 2
        const alpha = Math.max(0, (depth - 0.55)) * 0.22
        if (alpha <= 0.01) continue
        ctx.strokeStyle = `rgba(${TEAL.r},${TEAL.g},${TEAL.b},${alpha.toFixed(3)})`
        ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke()
      }

      // импульсы по связям
      if (!reduced && time - lastImpulse > IMPULSE_EVERY_MS && links.length) {
        lastImpulse = time
        const [i, j] = links[Math.floor(Math.random() * links.length)]
        impulses.push(Math.random() < 0.5 ? { from: i, to: j, born: time }
                                          : { from: j, to: i, born: time })
        if (impulses.length > 14) impulses.shift()
      }
      for (let k = impulses.length - 1; k >= 0; k--) {
        const im = impulses[k]
        const p = (time - im.born) / IMPULSE_LIFE_MS
        if (p >= 1) {
          nodes[im.to].glow = 1
          impulses.splice(k, 1)
          continue
        }
        const a = proj[im.from], b = proj[im.to]
        const x = a.sx + (b.sx - a.sx) * p
        const y = a.sy + (b.sy - a.sy) * p
        const d = (a.depth + b.depth) / 2
        const rr = 2.4 * d
        const g = ctx.createRadialGradient(x, y, 0, x, y, rr * 4)
        g.addColorStop(0, `rgba(255,255,255,${0.9 * d})`)
        g.addColorStop(0.4, `rgba(${TEAL.r},${TEAL.g},${TEAL.b},${0.5 * d})`)
        g.addColorStop(1, 'rgba(0,0,0,0)')
        ctx.fillStyle = g
        ctx.beginPath(); ctx.arc(x, y, rr * 4, 0, Math.PI * 2); ctx.fill()
      }

      // нейроны
      for (let i = 0; i < count; i++) {
        const n = nodes[i], p = proj[i]
        const c = n.hot ? HOT : TEAL
        const base = n.hot ? 0.85 : 0.6
        const alpha = Math.max(0.06, (p.depth - 0.45)) * base + n.glow * 0.4
        const r = (n.hot ? 2.2 : 1.6) * p.depth * (1 + n.glow * 1.6)
        if (n.glow > 0.02) {
          const g = ctx.createRadialGradient(p.sx, p.sy, 0, p.sx, p.sy, r * 6)
          g.addColorStop(0, `rgba(${c.r},${c.g},${c.b},${(n.glow * 0.5).toFixed(3)})`)
          g.addColorStop(1, 'rgba(0,0,0,0)')
          ctx.fillStyle = g
          ctx.beginPath(); ctx.arc(p.sx, p.sy, r * 6, 0, Math.PI * 2); ctx.fill()
        }
        ctx.fillStyle = `rgba(${c.r},${c.g},${c.b},${Math.min(1, alpha).toFixed(3)})`
        ctx.beginPath(); ctx.arc(p.sx, p.sy, r, 0, Math.PI * 2); ctx.fill()
        n.glow = Math.max(0, n.glow - 0.02)
      }
    }

    const loop = (time: number) => {
      if (!running) return
      if (visible && !document.hidden) draw(time)
      raf = requestAnimationFrame(loop)
    }

    if (reduced) {
      draw(0) // один статичный кадр
    } else {
      raf = requestAnimationFrame(loop)
      window.addEventListener('mousemove', onMouse, { passive: true })
    }
    window.addEventListener('resize', resize)
    const io = new IntersectionObserver((es) => { visible = es[0]?.isIntersecting ?? true })
    io.observe(canvas)

    return () => {
      running = false
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', resize)
      window.removeEventListener('mousemove', onMouse)
      io.disconnect()
    }
  }, [])

  return <canvas ref={ref} className={className} aria-hidden="true" />
}
