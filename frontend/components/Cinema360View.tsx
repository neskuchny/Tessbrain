'use client'

// «Кино-режим» Объекта 360 — порт эталонного прототипа Company_Snapshot.dc
// (золотой FUI): canvas-слой рисует светящиеся изогнутые связи с бегущими
// импульсами, узлы — DOM-карточки по радиальной раскладке (2D) или на
// вращающейся 3D-орбите; hover подсвечивает соседей и гасит остальное;
// клик раскрывает карточку в боковой панели — с фактами и связями ЕЁ 360
// (докачиваются тем же /ontology/object), клик по связи — перецентровка.
// Данные только реальные: граф-соседи + источники-упоминания карточки.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'

interface CinemaProps {
  card: any
  onNavigate: (name: string) => void
  /** Догрузить мини-карточку сущности (факты+связи) для раскрытия на месте. */
  fetchCard?: (name: string) => Promise<any | null>
}

const ACC = '#f5b942'
const ACC_HI = '#ffe3a0'
const INKD = 'rgba(224,208,178,0.85)'

function hexA(hex: string, a: number): string {
  let h = hex.replace('#', '')
  if (h.length === 3) h = h.split('').map((c) => c + c).join('')
  const n = parseInt(h, 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
}

interface Node {
  id: string
  kind: 'focus' | 'group' | 'item'
  label: string
  sub?: string
  group?: string
  nx: number; ny: number          // 2D (доли контейнера)
  x: number; y: number; z: number // 3D px
}

export default function Cinema360View({ card, onNavigate, fetchCard }: CinemaProps) {
  const t = useTranslations('object360_panel')
  const rootRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const rafRef = useRef<number>(0)
  const [hoverId, setHoverId] = useState<string | null>(null)
  const [selId, setSelId] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [view, setView] = useState<'2d' | '3d'>('3d')
  const [detail, setDetail] = useState<any | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const hoverRef = useRef<string | null>(null)
  hoverRef.current = hoverId

  // ── Данные → узлы: фокус в центре, группы связей секторами, элементы вокруг ──
  const { nodes, edges } = useMemo(() => {
    const ns: Node[] = [{
      id: '__focus', kind: 'focus', label: String(card?.name || ''),
      nx: 0.5, ny: 0.5, x: 0, y: 0, z: 0,
    }]
    const es: { from: string; to: string; strong: boolean }[] = []
    const groups: { label: string; items: { name: string; sub?: string }[] }[] = []
    for (const [label, arr] of Object.entries(card?.graph?.neighbors || {})) {
      const items = ((arr as any[]) || [])
        .map((it) => ({ name: String(it?.name || it?.title || '').trim(),
                        sub: String(it?.rel || it?.type || '').trim() }))
        .filter((x) => x.name).slice(0, 6)
      if (items.length) groups.push({ label: String(label), items })
    }
    const srcItems = ((card?.mentions || []) as any[])
      .map((m) => ({ name: String(m?.source_title || m?.title || '').trim(),
                     sub: t('cinema_kind_source') }))
      .filter((x, i, a) => x.name && a.findIndex((y) => y.name === x.name) === i)
      .slice(0, 5)
    if (srcItems.length) groups.push({ label: t('cinema_sources'), items: srcItems })

    const nG = Math.max(groups.length, 1)
    const S = 225
    groups.slice(0, 6).forEach((g, gi) => {
      const gAng = -Math.PI / 2 + (gi / nG) * Math.PI * 2
      const gid = `g:${g.label}`
      ns.push({
        id: gid, kind: 'group', label: g.label, group: g.label,
        nx: 0.5 + Math.cos(gAng) * 0.24, ny: 0.5 + Math.sin(gAng) * 0.27,
        x: Math.cos(gAng) * S, z: Math.sin(gAng) * S, y: Math.sin(gAng * 1.7) * 46,
      })
      es.push({ from: '__focus', to: gid, strong: true })
      g.items.forEach((it, k) => {
        const spread = (k - (g.items.length - 1) / 2) * 0.34
        const ang = gAng + spread
        const iid = `i:${g.label}:${it.name}`
        ns.push({
          id: iid, kind: 'item', label: it.name, sub: it.sub, group: g.label,
          nx: 0.5 + Math.cos(ang) * 0.395, ny: 0.5 + Math.sin(ang) * 0.435,
          x: Math.cos(ang) * S * 1.46, z: Math.sin(ang) * S * 1.46,
          y: Math.sin(ang * 1.7) * 46 + ((k % 2) ? 38 : -38),
        })
        es.push({ from: gid, to: iid, strong: false })
      })
    })
    return { nodes: ns, edges: es }
  }, [card, t])

  const neighborSet = useCallback((id: string | null): Set<string> => {
    const s = new Set<string>()
    if (!id) return s
    for (const e of edges) {
      if (e.from === id) s.add(e.to)
      if (e.to === id) s.add(e.from)
    }
    return s
  }, [edges])

  const matches = useCallback((n: Node): boolean => {
    const q = query.trim().toLowerCase()
    if (!q || n.kind === 'focus') return true
    return `${n.label} ${n.sub || ''} ${n.group || ''}`.toLowerCase().includes(q)
  }, [query])

  // ── Canvas: связи со свечением, импульсы, аура, скан (порт _drawFrame) ──
  useEffect(() => {
    const cv = canvasRef.current
    const root = rootRef.current
    if (!cv || !root) return
    const ctx = cv.getContext('2d')
    if (!ctx) return
    let scan = 0
    let last = performance.now() / 1000

    const draw = () => {
      rafRef.current = requestAnimationFrame(draw)
      const r = root.getBoundingClientRect()
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      if (cv.width !== Math.floor(r.width * dpr)) {
        cv.width = Math.floor(r.width * dpr)
        cv.height = Math.floor(r.height * dpr)
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      const W = r.width, H = r.height
      const tNow = performance.now() / 1000
      const dt = Math.min(tNow - last, 0.05); last = tNow

      const bg = ctx.createRadialGradient(W * 0.5, H * 0.48, 0, W * 0.5, H * 0.48, Math.max(W, H) * 0.85)
      bg.addColorStop(0, '#0b0906'); bg.addColorStop(0.55, '#0a0704'); bg.addColorStop(1, '#180f09')
      ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H)

      const pos: Record<string, { x: number; y: number }> = {}
      root.querySelectorAll<HTMLElement>('[data-cnode]').forEach((el) => {
        const b = el.getBoundingClientRect()
        pos[el.dataset.cnode!] = { x: b.left + b.width / 2 - r.left, y: b.top + b.height / 2 - r.top }
      })
      const comp = pos['__focus']
      if (comp) {
        const pulse = Math.sin(tNow * 1.4) * 0.5 + 0.5
        ctx.save(); ctx.globalCompositeOperation = 'lighter'
        const aura = ctx.createRadialGradient(comp.x, comp.y, 0, comp.x, comp.y, 180 + pulse * 22)
        aura.addColorStop(0, hexA(ACC, 0.18)); aura.addColorStop(0.4, hexA(ACC, 0.05)); aura.addColorStop(1, 'rgba(0,0,0,0)')
        ctx.fillStyle = aura; ctx.beginPath(); ctx.arc(comp.x, comp.y, 180 + pulse * 22, 0, Math.PI * 2); ctx.fill(); ctx.restore()
      }

      const focus = hoverRef.current
      const near = focus ? neighborSet(focus) : null
      for (const e of edges) {
        const a = pos[e.from], b = pos[e.to]
        if (!a || !b) continue
        const isFocus = !!focus && (e.from === focus || e.to === focus)
        const dim = !!focus && !isFocus
        let alpha = e.strong ? 0.5 : 0.3
        let w = e.strong ? 1.2 : 1
        let blur = 6
        if (isFocus) { alpha = Math.min(1, alpha * 1.9); w += 0.8; blur = 10 }
        if (dim) alpha *= 0.15
        const mx = (a.x + b.x) / 2 + (a.y - b.y) * 0.08
        const my = (a.y + b.y) / 2 + (b.x - a.x) * 0.08
        ctx.save(); ctx.globalCompositeOperation = 'lighter'
        ctx.strokeStyle = hexA(ACC_HI, alpha); ctx.lineWidth = w; ctx.lineCap = 'round'
        ctx.shadowColor = hexA(ACC_HI, alpha); ctx.shadowBlur = blur
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.quadraticCurveTo(mx, my, b.x, b.y); ctx.stroke()
        ctx.restore()
        if (isFocus) {
          const ph = (tNow * 0.7) % 1
          const px = (1 - ph) * (1 - ph) * a.x + 2 * (1 - ph) * ph * mx + ph * ph * b.x
          const py = (1 - ph) * (1 - ph) * a.y + 2 * (1 - ph) * ph * my + ph * ph * b.y
          ctx.save(); ctx.globalCompositeOperation = 'lighter'
          const g = ctx.createRadialGradient(px, py, 0, px, py, 6)
          g.addColorStop(0, hexA(ACC_HI, 0.9)); g.addColorStop(1, 'rgba(0,0,0,0)')
          ctx.globalAlpha = Math.sin(ph * Math.PI) * 0.8
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(px, py, 6, 0, Math.PI * 2); ctx.fill(); ctx.restore()
        }
      }
      // маркеры узлов
      for (const n of nodes) {
        if (n.kind === 'focus') continue
        const p = pos[n.id]; if (!p) continue
        const focused = !!focus && (focus === n.id || (near && near.has(n.id)))
        ctx.save(); ctx.globalCompositeOperation = 'lighter'
        const rad = focused ? 6 : 3
        const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, rad)
        g.addColorStop(0, hexA(ACC_HI, 0.6)); g.addColorStop(1, 'rgba(0,0,0,0)')
        ctx.globalAlpha = focused ? 0.5 : 0.2
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, rad, 0, Math.PI * 2); ctx.fill(); ctx.restore()
      }
      // скан-полоса
      scan = (scan + dt * 0.05) % 1.3
      const sy = scan * H
      const sg = ctx.createLinearGradient(0, sy - 40, 0, sy + 40)
      sg.addColorStop(0, hexA(ACC, 0)); sg.addColorStop(0.5, hexA(ACC, 0.03)); sg.addColorStop(1, hexA(ACC, 0))
      ctx.fillStyle = sg; ctx.fillRect(0, sy - 40, W, 80)
    }
    draw()
    return () => cancelAnimationFrame(rafRef.current)
  }, [edges, nodes, neighborSet])

  // ── Раскрытие карточки: клик → мини-360 в панели ──
  const openDetail = useCallback(async (n: Node) => {
    setSelId(n.id)
    if (n.kind === 'focus') {
      setDetail({ kicker: t('cinema_focus'), title: card?.name,
                  facts: (card?.facts || []).slice(0, 8),
                  links: [], name: card?.name })
      return
    }
    setDetail({ kicker: n.group || n.sub || '', title: n.label, loading: true, name: n.label })
    if (!fetchCard) return
    setDetailLoading(true)
    try {
      const mini = await fetchCard(n.label)
      const links: { name: string; kind: string }[] = []
      for (const [label, arr] of Object.entries(mini?.graph?.neighbors || {})) {
        for (const it of (arr as any[]) || []) {
          const nm = String(it?.name || '').trim()
          if (nm && nm !== n.label) links.push({ name: nm, kind: String(label) })
        }
      }
      setDetail({
        kicker: n.group || '', title: n.label, name: n.label,
        facts: (mini?.facts || []).slice(0, 6),
        links: links.slice(0, 8),
        verdict: mini?.verdict?.summary || '',
      })
    } finally {
      setDetailLoading(false)
    }
  }, [card, fetchCard, t])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { setSelId(null); setDetail(null) } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const focus = hoverId
  const near = neighborSet(focus)
  const is3d = view === '3d'

  return (
    <div ref={rootRef} className="relative overflow-hidden rounded-xl"
      style={{ background: '#060504', minHeight: 560, fontFamily: "'JetBrains Mono', ui-monospace, monospace", cursor: 'crosshair' }}>
      <canvas ref={canvasRef} className="absolute inset-0 w-full h-full" style={{ zIndex: 0 }} />

      {/* Сцена узлов */}
      <div className="absolute inset-0" style={is3d
        ? { zIndex: 5, perspective: '1500px', perspectiveOrigin: '50% 48%' }
        : { zIndex: 5 }}>
        <div style={is3d
          ? { position: 'absolute', left: '50%', top: '48%', transformStyle: 'preserve-3d',
              animation: 'tsOrbit 30s ease-in-out infinite' }
          : { position: 'absolute', inset: 0 }}>
          {nodes.map((n) => {
            const isSel = selId === n.id, isHov = hoverId === n.id
            const related = !!focus && (near.has(n.id) || n.id === focus)
            const active = isSel || isHov || related
            const dim = (!!focus && !active) || !matches(n)
            const base: React.CSSProperties = is3d
              ? { position: 'absolute', left: 0, top: 0, transformStyle: 'preserve-3d',
                  transform: `translate(-50%,-50%) translate3d(${n.x}px, ${n.y}px, ${n.z}px) scale(${isSel || isHov ? 1.08 : 1})`,
                  zIndex: n.kind === 'focus' ? 20 : n.kind === 'group' ? 14 : 10,
                  opacity: dim ? 0.14 : 1, transition: 'opacity .4s ease' }
              : { position: 'absolute', left: `${n.nx * 100}%`, top: `${n.ny * 100}%`,
                  transform: `translate(-50%,-50%) scale(${isSel || isHov ? 1.08 : 1})`,
                  zIndex: n.kind === 'focus' ? 20 : n.kind === 'group' ? 14 : 10,
                  opacity: dim ? 0.12 : 1,
                  transition: 'left .6s cubic-bezier(.4,0,.2,1), top .6s cubic-bezier(.4,0,.2,1), transform .3s ease, opacity .4s ease' }
            const ring: React.CSSProperties = {
              position: 'absolute', inset: -2, pointerEvents: 'none',
              borderRadius: n.kind === 'item' ? 20 : 7,
              border: `1px solid ${isSel ? ACC_HI : ACC}`, opacity: active ? 1 : 0,
              boxShadow: isSel ? `0 0 26px ${hexA(ACC, 0.5)}` : `0 0 16px ${hexA(ACC, 0.3)}`,
              transition: 'opacity .3s ease',
            }
            return (
              <div key={n.id} data-cnode={n.id} style={base}
                onClick={() => openDetail(n)}
                onMouseEnter={() => setHoverId(n.id)}
                onMouseLeave={() => setHoverId(null)}>
                {n.kind === 'focus' && (
                  <div className="relative flex flex-col items-center justify-center cursor-pointer"
                    style={{ width: 128, height: 96, borderRadius: 8,
                             background: 'linear-gradient(180deg, rgba(60,42,16,0.94), rgba(28,18,7,0.96))' }}>
                    <div style={ring} />
                    {(['-3px -3px auto auto', '-3px auto auto -3px', 'auto -3px -3px auto', 'auto auto -3px -3px'] as const).map((c, i) => {
                      const [top, right, bottom, left] = c.split(' ')
                      return <div key={i} style={{ position: 'absolute', top, right, bottom, left, width: 10, height: 10,
                        borderTop: top !== 'auto' ? `1px solid #ffce6e` : undefined,
                        borderBottom: bottom !== 'auto' ? `1px solid #ffce6e` : undefined,
                        borderLeft: left !== 'auto' ? `1px solid #ffce6e` : undefined,
                        borderRight: right !== 'auto' ? `1px solid #ffce6e` : undefined }} />
                    })}
                    <div className="px-2 text-center" style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.12em', color: ACC_HI }}>
                      {n.label}
                    </div>
                    <div style={{ fontSize: 7, letterSpacing: '0.2em', color: 'rgba(255,220,150,0.55)', marginTop: 4, textTransform: 'uppercase' }}>
                      {t('cinema_focus')}
                    </div>
                  </div>
                )}
                {n.kind === 'group' && (
                  <div className="relative cursor-pointer" style={{ minWidth: 96, padding: '8px 12px', borderRadius: 5,
                    background: 'linear-gradient(160deg, rgba(60,42,16,0.5), rgba(30,20,8,0.5))', backdropFilter: 'blur(3px)' }}>
                    <div style={ring} />
                    <div style={{ fontSize: 7, letterSpacing: '0.18em', color: 'rgba(255,220,150,0.5)', textTransform: 'uppercase' }}>
                      {t('cinema_group')}
                    </div>
                    <div style={{ fontSize: 11.5, fontWeight: 500, color: '#f4ecda', marginTop: 3, whiteSpace: 'nowrap' }}>{n.label}</div>
                  </div>
                )}
                {n.kind === 'item' && (
                  <div className="relative flex items-center cursor-pointer" style={{ gap: 7, padding: '6px 10px', borderRadius: 20,
                    background: 'rgba(18,13,6,0.62)', border: '1px solid rgba(210,210,220,0.12)', backdropFilter: 'blur(3px)', whiteSpace: 'nowrap' }}>
                    <div style={ring} />
                    <div className="flex flex-col">
                      <span style={{ fontSize: 10, fontWeight: 500, color: 'rgba(240,232,214,0.92)', maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {n.label}
                      </span>
                      {n.sub && <span style={{ fontSize: 7.5, letterSpacing: '0.06em', color: 'rgba(224,208,178,0.5)' }}>{n.sub}</span>}
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {/* Верхняя панель: поиск + 2D/3D */}
      <div className="absolute top-0 left-0 right-0 flex items-center gap-4 px-4 py-3" style={{ zIndex: 35, pointerEvents: 'none' }}>
        <div style={{ pointerEvents: 'auto' }}>
          <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.3em', color: ACC }}>TESSENT</div>
          <div style={{ fontSize: 7.5, letterSpacing: '0.22em', color: 'rgba(224,208,178,0.4)', textTransform: 'uppercase' }}>{t('cinema_subtitle')}</div>
        </div>
        <div className="flex-1 flex justify-center">
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t('cinema_search')}
            style={{ pointerEvents: 'auto', width: 'min(340px, 40vw)', padding: '7px 12px', background: 'rgba(20,14,6,0.5)',
                     border: `1px solid ${hexA(ACC, 0.2)}`, borderRadius: 3, color: '#e8dcc0', fontFamily: 'inherit',
                     fontSize: 11, letterSpacing: '0.06em', backdropFilter: 'blur(6px)' }} />
        </div>
        <div className="flex gap-1.5" style={{ pointerEvents: 'auto' }}>
          {(['2d', '3d'] as const).map((v) => (
            <button key={v} onClick={() => setView(v)}
              style={{ background: view === v ? hexA(ACC, 0.18) : 'rgba(20,14,6,0.5)',
                       border: `1px solid ${view === v ? hexA(ACC, 0.5) : hexA(ACC, 0.18)}`,
                       color: view === v ? ACC_HI : 'rgba(224,208,178,0.6)', borderRadius: 3,
                       padding: '6px 12px', fontSize: 9, letterSpacing: '0.18em', cursor: 'pointer', fontFamily: 'inherit' }}>
              {v.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {/* Панель деталей: карточка раскрывается со своими фактами и связями */}
      {detail && (
        <div className="absolute flex flex-col" style={{ zIndex: 45, top: 64, right: 14, bottom: 14, width: 340,
          padding: '18px 20px', overflowY: 'auto',
          background: 'linear-gradient(180deg, rgba(24,17,8,0.94), rgba(12,8,4,0.96))',
          borderLeft: `1px solid ${hexA(ACC, 0.28)}`, borderRadius: 6, boxShadow: '-24px 0 60px rgba(0,0,0,0.5)',
          backdropFilter: 'blur(10px)' }}>
          <div className="flex justify-between items-start">
            <div style={{ fontSize: 8, letterSpacing: '0.2em', color: 'rgba(255,220,150,0.55)', textTransform: 'uppercase' }}>{detail.kicker}</div>
            <button onClick={() => { setSelId(null); setDetail(null) }}
              style={{ background: 'transparent', border: `1px solid ${hexA(ACC, 0.25)}`, color: hexA(ACC, 0.7),
                       width: 22, height: 22, borderRadius: 3, cursor: 'pointer', fontSize: 11, lineHeight: 1 }}>✕</button>
          </div>
          <div style={{ marginTop: 12, fontSize: 18, fontWeight: 500, color: '#f4ecda', lineHeight: 1.15 }}>{detail.title}</div>
          {detail.verdict && <div style={{ fontSize: 10, color: 'rgba(224,208,178,0.6)', marginTop: 5 }}>{String(detail.verdict).slice(0, 160)}</div>}

          {detailLoading && <div style={{ marginTop: 16, fontSize: 10, color: hexA(ACC, 0.6) }}>{t('cinema_loading')}</div>}

          {!!(detail.facts?.length) && (
            <div style={{ marginTop: 18 }}>
              <div style={{ fontSize: 8, letterSpacing: '0.2em', color: 'rgba(224,208,178,0.4)', marginBottom: 8 }}>{t('cinema_facts')}</div>
              {detail.facts.map((f: any, i: number) => (
                <div key={i} style={{ fontSize: 10.5, color: INKD, padding: '6px 0', lineHeight: 1.45,
                                      borderBottom: `1px solid ${hexA(ACC, 0.1)}` }}>
                  {String(f?.text || f?.fact || f?.value || '').slice(0, 160)}
                </div>
              ))}
            </div>
          )}

          {!!(detail.links?.length) && (
            <div style={{ marginTop: 18 }}>
              <div style={{ fontSize: 8, letterSpacing: '0.2em', color: 'rgba(224,208,178,0.4)', marginBottom: 8 }}>{t('cinema_links')}</div>
              <div className="flex flex-col" style={{ gap: 2 }}>
                {detail.links.map((l: any, i: number) => (
                  <div key={i} onClick={() => onNavigate(l.name)}
                    className="flex items-center cursor-pointer"
                    style={{ gap: 8, padding: '6px 8px', borderRadius: 3,
                             background: hexA(ACC, 0.05), border: `1px solid ${hexA(ACC, 0.12)}` }}>
                    <span style={{ fontSize: 8, color: ACC }}>›</span>
                    <span style={{ flex: 1, fontSize: 10.5, color: 'rgba(240,232,214,0.85)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{l.name}</span>
                    <span style={{ fontSize: 8, letterSpacing: '0.06em', color: 'rgba(224,208,178,0.45)' }}>{l.kind}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {detail.name && detail.name !== card?.name && (
            <button onClick={() => onNavigate(detail.name)}
              style={{ marginTop: 18, padding: '8px 12px', background: hexA(ACC, 0.14), color: ACC_HI,
                       border: `1px solid ${hexA(ACC, 0.4)}`, borderRadius: 3, cursor: 'pointer',
                       fontSize: 10, letterSpacing: '0.1em', fontFamily: 'inherit' }}>
              {t('cinema_open_360')} →
            </button>
          )}
        </div>
      )}

      {/* Виньетка + легенда */}
      <div className="absolute inset-0 pointer-events-none" style={{ zIndex: 38,
        background: 'radial-gradient(120% 100% at 48% 48%, transparent 44%, rgba(0,0,0,0.3) 78%, rgba(0,0,0,0.68) 100%)',
        mixBlendMode: 'multiply' }} />
      <div className="absolute left-4 bottom-3 pointer-events-none" style={{ zIndex: 39, fontSize: 8.5,
        letterSpacing: '0.14em', textTransform: 'uppercase', color: 'rgba(224,208,178,0.45)' }}>
        {t('cinema_hint')}
      </div>

      <style jsx>{`
        @keyframes tsOrbit {
          0% { transform: rotateY(-42deg) rotateX(8deg); }
          50% { transform: rotateY(42deg) rotateX(-6deg); }
          100% { transform: rotateY(-42deg) rotateX(8deg); }
        }
      `}</style>
    </div>
  )
}
