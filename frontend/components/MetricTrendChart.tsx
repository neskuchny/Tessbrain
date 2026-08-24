'use client'

/**
 * MetricTrendChart — тренд метрики (B3): факт из данных / факт из встреч /
 * план по оси периодов. Чистый SVG по спекам dataviz:
 *  - линии 2px, точки-маркеры ≥8px на hover;
 *  - crosshair прилипает к ближайшему X, один тултип со ВСЕМИ сериями
 *    (значение — главное, имя серии — вторично, ключ — штрих цвета серии);
 *  - легенда всегда при ≥2 сериях + значения в тултипе (relief-правило:
 *    контраст синего на тёмной поверхности ниже 3:1);
 *  - план — пунктирная линия (не категориальная серия);
 *  - одна ось Y (никаких dual-axis), сетка рецессивная.
 * Палитра — провалидированные dark-слоты (surface brain-900): данные #3987e5,
 * встречи #199e70, план #9085e9. CVD worst ΔE 61.6 — запас большой.
 *
 * MetricSparkline — мини-тренд для чипа метрики (без осей/тултипа).
 */
import { useMemo, useRef, useState } from 'react'
import { useLocale, useTranslations } from 'next-intl'

export interface TrendPoint {
  x: string
  fact_dataset?: number
  fact_meeting?: number
  plan?: number
}

const SERIES: { key: keyof TrendPoint; color: string; dash?: string }[] = [
  { key: 'fact_dataset', color: '#3987e5' },
  { key: 'fact_meeting', color: '#199e70' },
  { key: 'plan', color: '#9085e9', dash: '5 4' },
]

/**
 * Компактный формат числа под локаль интерфейса: сама цифра — через
 * Intl.NumberFormat(locale), а суффикс порядка (тыс/млн/млрд) — ключ словаря
 * с ICU-плейсхолдером {value}. Так и число, и подпись следуют языку UI.
 */
function useValueFormatter() {
  const t = useTranslations('metric_trend_chart')
  const locale = useLocale()

  return useMemo(() => {
    const nf3 = new Intl.NumberFormat(locale, { maximumSignificantDigits: 3 })
    const nf4 = new Intl.NumberFormat(locale, { maximumSignificantDigits: 4 })
    return (v: number | undefined | null): string => {
      if (v === undefined || v === null || !isFinite(v)) return t('value_empty')
      const a = Math.abs(v)
      if (a >= 1e9) return t('value_billions', { value: nf3.format(v / 1e9) })
      if (a >= 1e6) return t('value_millions', { value: nf3.format(v / 1e6) })
      if (a >= 1e4) return t('value_thousands', { value: nf3.format(v / 1e3) })
      return nf4.format(v)
    }
  }, [t, locale])
}

export function MetricTrendChart({ series, unit, labels }: {
  series: TrendPoint[]
  unit?: string | null
  labels: { fact_dataset: string; fact_meeting: string; plan: string }
}) {
  const [hoverI, setHoverI] = useState<number | null>(null)
  const svgRef = useRef<SVGSVGElement | null>(null)
  const fmt = useValueFormatter()

  const W = 560, H = 180
  const PAD = { l: 58, r: 28, t: 10, b: 22 }
  const plotW = W - PAD.l - PAD.r
  const plotH = H - PAD.t - PAD.b

  const model = useMemo(() => {
    const pts = (series || []).filter(p => p && p.x)
    const used = SERIES.filter(s => pts.some(p => typeof p[s.key] === 'number'))
    const vals: number[] = []
    for (const p of pts) for (const s of used) {
      const v = p[s.key]
      if (typeof v === 'number' && isFinite(v)) vals.push(v)
    }
    if (!pts.length || !vals.length) return null
    let lo = Math.min(...vals), hi = Math.max(...vals)
    if (lo === hi) { lo -= Math.abs(lo) * 0.1 || 1; hi += Math.abs(hi) * 0.1 || 1 }
    const span = hi - lo
    lo -= span * 0.08; hi += span * 0.08
    const xAt = (i: number) => pts.length === 1
      ? PAD.l + plotW / 2
      : PAD.l + (i / (pts.length - 1)) * plotW
    const yAt = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * plotH
    // рецессивная сетка: 3 горизонтали с подписями
    const ticks = [0, 0.5, 1].map(f => ({ y: PAD.t + f * plotH, v: hi - f * (hi - lo) }))
    return { pts, used, lo, hi, xAt, yAt, ticks }
  }, [series])

  if (!model) return null
  const { pts, used, xAt, yAt, ticks } = model

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect) return
    const px = ((e.clientX - rect.left) / rect.width) * W
    let best = 0, bd = Infinity
    for (let i = 0; i < pts.length; i++) {
      const d = Math.abs(xAt(i) - px)
      if (d < bd) { bd = d; best = i }
    }
    setHoverI(best)
  }

  const hover = hoverI !== null ? pts[hoverI] : null
  const hx = hoverI !== null ? xAt(hoverI) : 0
  // тултип не вылезает за край
  const tipLeftPct = hoverI !== null ? Math.min(78, Math.max(2, (hx / W) * 100)) : 0

  return (
    <div className="relative">
      <svg
        ref={svgRef} viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto select-none"
        onPointerMove={onMove} onPointerLeave={() => setHoverI(null)}
      >
        {/* сетка + подписи Y — рецессивно */}
        {ticks.map((tk, i) => (
          <g key={i}>
            <line x1={PAD.l} x2={W - PAD.r} y1={tk.y} y2={tk.y}
              stroke="#ffffff" strokeOpacity={0.07} strokeWidth={1} />
            <text x={PAD.l - 6} y={tk.y + 3} textAnchor="end"
              fontSize={9} fill="#94a3b8">{fmt(tk.v)}</text>
          </g>
        ))}
        {/* подписи X: первая/последняя (+средняя при ширине); крайние
            прижаты внутрь, чтобы не резались о край */}
        {[0, pts.length > 4 ? Math.floor((pts.length - 1) / 2) : -1, pts.length - 1]
          .filter((i, k, a) => i >= 0 && a.indexOf(i) === k)
          .map(i => (
            <text key={i} x={xAt(i)} y={H - 6}
              textAnchor={i === 0 ? 'start' : i === pts.length - 1 ? 'end' : 'middle'}
              fontSize={9} fill="#94a3b8">{pts[i].x}</text>
          ))}
        {/* линии серий: 2px; план — пунктир. Разреженная серия (пропуски по X)
            соединяется через пропуски + несёт постоянные маркеры — иначе
            одиночные точки были бы невидимы. */}
        {used.map(s => {
          const present = pts
            .map((p, i) => ({ i, v: p[s.key] }))
            .filter((q): q is { i: number; v: number } =>
              typeof q.v === 'number' && isFinite(q.v))
          const d = present
            .map((q, k) => `${k === 0 ? 'M' : 'L'}${xAt(q.i).toFixed(1)},${yAt(q.v).toFixed(1)}`)
            .join(' ')
          const sparse = present.length < pts.length
          return (
            <g key={String(s.key)}>
              <path d={d} fill="none" stroke={s.color}
                strokeWidth={2} strokeDasharray={s.dash}
                strokeLinejoin="round" strokeLinecap="round" />
              {sparse && present.map(q => (
                <circle key={q.i} cx={xAt(q.i)} cy={yAt(q.v)} r={3}
                  fill={s.color} stroke="#0a3f6e" strokeWidth={1.5} />
              ))}
            </g>
          )
        })}
        {/* crosshair + маркеры ≥8px на активном X */}
        {hoverI !== null && (
          <g>
            <line x1={hx} x2={hx} y1={PAD.t} y2={PAD.t + plotH}
              stroke="#ffffff" strokeOpacity={0.25} strokeWidth={1} />
            {used.map(s => {
              const v = hover?.[s.key]
              return typeof v === 'number' && isFinite(v) ? (
                <circle key={String(s.key)} cx={hx} cy={yAt(v)} r={4}
                  fill={s.color} stroke="#0a3f6e" strokeWidth={2} />
              ) : null
            })}
          </g>
        )}
      </svg>

      {/* тултип: значение — главное, серия — вторично, ключ — штрих цвета */}
      {hover && (
        <div
          className="absolute top-1 pointer-events-none rounded-lg border border-brain-600/50 bg-brain-950/95 px-2.5 py-1.5 space-y-0.5 shadow-lg"
          style={{ left: `${tipLeftPct}%` }}
        >
          <p className="text-[10px] text-slate-400">{hover.x}</p>
          {used.map(s => {
            const v = hover[s.key]
            if (typeof v !== 'number' || !isFinite(v)) return null
            return (
              <p key={String(s.key)} className="text-[11px] flex items-center gap-1.5">
                <span className="inline-block w-3 border-t-2"
                  style={{ borderColor: s.color, borderStyle: s.dash ? 'dashed' : 'solid' }} />
                <span className="text-white font-medium tabular-nums">
                  {fmt(v)}{unit ? ` ${unit}` : ''}
                </span>
                <span className="text-slate-400">{labels[s.key as keyof typeof labels]}</span>
              </p>
            )
          })}
        </div>
      )}

      {/* легенда: всегда при ≥2 сериях (relief-правило для синего) */}
      {used.length >= 2 && (
        <div className="flex flex-wrap gap-3 mt-1">
          {used.map(s => (
            <span key={String(s.key)} className="flex items-center gap-1.5 text-[10.5px] text-slate-300">
              <span className="inline-block w-4 border-t-2"
                style={{ borderColor: s.color, borderStyle: s.dash ? 'dashed' : 'solid' }} />
              {labels[s.key as keyof typeof labels]}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

export function MetricSparkline({ series }: { series: TrendPoint[] }) {
  const model = useMemo(() => {
    const pts = (series || [])
      .map(p => (typeof p.fact_dataset === 'number' ? p.fact_dataset
        : typeof p.fact_meeting === 'number' ? p.fact_meeting : null))
      .filter((v): v is number => v !== null && isFinite(v))
    if (pts.length < 2) return null
    let lo = Math.min(...pts), hi = Math.max(...pts)
    if (lo === hi) { lo -= 1; hi += 1 }
    const W = 52, H = 14
    const d = pts.map((v, i) =>
      `${i === 0 ? 'M' : 'L'}${(2 + (i / (pts.length - 1)) * (W - 4)).toFixed(1)},${(2 + (1 - (v - lo) / (hi - lo)) * (H - 4)).toFixed(1)}`,
    ).join(' ')
    return { d, W, H, up: pts[pts.length - 1] >= pts[0] }
  }, [series])
  if (!model) return null
  return (
    <svg viewBox={`0 0 ${model.W} ${model.H}`} className="inline-block w-[52px] h-[14px] align-middle">
      <path d={model.d} fill="none" stroke={model.up ? '#199e70' : '#e66767'}
        strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}
