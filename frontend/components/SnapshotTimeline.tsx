'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'

/**
 * SnapshotTimeline — история сущности (человек/проект/компания) как
 * ЧИТАЕМЫЙ график, а не нитка точек.
 *
 * Дизайн (dataviz-правила):
 * - одна метрика (здоровье/прогресс/задачи — что есть в данных) кодируется
 *   линией+областью: видно РОСТ/ПАДЕНИЕ, а не только «были версии»;
 * - x = реальное время (не равные интервалы), точки = версии снапшота;
 * - «СЕЙЧАС» — изумрудная точка с подписью и значением метрики;
 * - события (встречи/решения) — отдельная дорожка тиков ПОД осью,
 *   не ломают геометрию линии;
 * - прогнозы — призрачные чипы справа (у них нет даты на оси);
 * - hover-тултип на каждой точке; клик — карточка «что изменилось»;
 *   режим «Сравнить» (две точки) сохранён. 3D убран.
 */

const API_TL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

interface TLPoint {
  version: number
  date: string
  delta_summary: string
  status?: string
  health_score?: number | null
  progress?: number | null
  tasks_completed?: number | null
  name?: string
  is_current?: boolean
}

interface TLFuture {
  title: string
  description: string
  probability?: number | null
  horizon?: string
  kind: string
}

interface TLEvent {
  kind: 'meeting' | 'decision'
  date: string
  title: string
}

const W_PER_POINT = 92
const CHART_H = 132
const PAD_L = 40
const PAD_R = 56
const PAD_T = 16
const PAD_B = 8

export default function SnapshotTimeline({
  entityType,
  entityId,
  userId,
}: {
  entityType: 'person' | 'project' | 'company' | 'department' | 'product' | 'team'
  entityId: string
  userId: string
}) {
  const t = useTranslations('snapshot_timeline')
  const [points, setPoints] = useState<TLPoint[]>([])
  const [future, setFuture] = useState<TLFuture[]>([])
  const [events, setEvents] = useState<TLEvent[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [selEvent, setSelEvent] = useState<TLEvent | null>(null)
  const [hover, setHover] = useState<number | null>(null)
  const [story, setStory] = useState<string>('')
  const [storyLoading, setStoryLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [cmpMode, setCmpMode] = useState(false)
  const [cmpSel, setCmpSel] = useState<number[]>([])

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        // authFetch, не fetch: URL абсолютный (localhost:8000), глобальный
        // патч токена ловит только same-origin /api/* — без Bearer strict-
        // middleware отвечал 401 и таймлайн молча оставался пустым.
        const res = await authFetch(
          `${API_TL}/api/v1/snapshots/timeline/${entityType}/${encodeURIComponent(entityId)}?user_id=${userId}`
        )
        const data = await res.json()
        if (!alive) return
        setPoints(Array.isArray(data.points) ? data.points : [])
        setFuture(Array.isArray(data.future) ? data.future : [])
        setEvents(Array.isArray(data.events) ? data.events : [])
        setSelected(data.points?.length ? data.points.length - 1 : null)
      } catch {
        /* таймлайн — best-effort украшение карточки */
      } finally {
        if (alive) setLoaded(true)
      }
    })()
    return () => { alive = false }
  }, [entityType, entityId, userId])

  const loadStory = useCallback(async () => {
    if (storyLoading) return
    setStoryLoading(true)
    try {
      const res = await authFetch(
        `${API_TL}/api/v1/snapshots/timeline/${entityType}/${encodeURIComponent(entityId)}?user_id=${userId}&storyline=true`
      )
      const data = await res.json()
      setStory(data.storyline || t('story_empty'))
    } catch {
      setStory(t('story_error'))
    } finally {
      setStoryLoading(false)
    }
  }, [entityType, entityId, userId, storyLoading, t])

  // ── геометрия графика ──────────────────────────────────────────────
  const geo = useMemo(() => {
    if (points.length === 0) return null
    // какая метрика есть хотя бы у двух точек
    const metricOf = (p: TLPoint, key: 'health_score' | 'progress' | 'tasks_completed') =>
      typeof p[key] === 'number' && p[key] !== null ? (p[key] as number) : null
    let metricKey: 'health_score' | 'progress' | 'tasks_completed' | null = null
    for (const k of ['health_score', 'progress', 'tasks_completed'] as const) {
      if (points.filter((p) => metricOf(p, k) !== null).length >= 2) { metricKey = k; break }
    }
    const metricLabel = metricKey === 'health_score' ? t('metric_health')
      : metricKey === 'progress' ? t('metric_progress')
      : metricKey === 'tasks_completed' ? t('metric_tasks') : ''
    const isPct = metricKey === 'health_score' || metricKey === 'progress'

    // время → x: реальная шкала по датам версий (события клэмпятся в неё)
    const times = points.map((p) => new Date(p.date || 0).getTime() || 0)
    const tMin = Math.min(...times)
    const tMax = Math.max(...times)
    const span = Math.max(1, tMax - tMin)
    const width = Math.max(420, points.length * W_PER_POINT) + PAD_L + PAD_R
    const plotW = width - PAD_L - PAD_R
    const xOf = (t: number) => PAD_L + ((Math.min(Math.max(t, tMin), tMax) - tMin) / span) * plotW

    // значения → y
    const vals = metricKey ? points.map((p) => metricOf(p, metricKey!)) : []
    const known = vals.filter((v): v is number => v !== null)
    let vMin = known.length ? Math.min(...known) : 0
    let vMax = known.length ? Math.max(...known) : 1
    if (isPct) { vMin = 0; vMax = 100 }
    if (vMax === vMin) vMax = vMin + 1
    const plotH = CHART_H - PAD_T - PAD_B
    const yOf = (v: number) => PAD_T + (1 - (v - vMin) / (vMax - vMin)) * plotH

    // последняя известная метрика тянется на точки без значения — линия
    // непрерывна, а «дырявые» точки рисуются полыми
    let last: number | null = null
    const pts = points.map((p, i) => {
      const raw = metricKey ? metricOf(p, metricKey) : null
      if (raw !== null) last = raw
      const v = raw !== null ? raw : last
      return {
        p, i,
        x: points.length === 1 ? PAD_L + plotW / 2 : xOf(times[i]),
        y: v !== null && metricKey ? yOf(v) : PAD_T + plotH / 2,
        value: raw, carried: raw === null,
      }
    })
    const linePts = pts.filter((d) => !(d.carried && d.i === 0))
    const line = linePts.map((d) => `${d.x},${d.y}`).join(' ')
    const area = linePts.length >= 2
      ? `M${linePts[0].x},${CHART_H - PAD_B} L${line.split(' ').map(s => 'L' + s).join(' ').slice(1)} L${linePts[linePts.length - 1].x},${CHART_H - PAD_B} Z`
      : ''
    const evs = events
      .map((e) => ({ e, t: new Date(e.date || 0).getTime() || 0 }))
      .filter((d) => d.t > 0)
      .map((d) => ({ ...d, x: xOf(d.t) }))
    return { width, pts, line, area, evs, metricKey, metricLabel, isPct, vMin, vMax }
  }, [points, events, t])

  if (!loaded) return null
  if (points.length < 2 && future.length === 0 && events.length === 0) return null

  const sel = selected !== null ? points[selected] : null
  const fmtV = (v: number) => (geo?.isPct ? `${Math.round(v)}%` : `${Math.round(v)}`)

  const clickPoint = (idx: number) => {
    if (cmpMode) {
      setCmpSel((prev) => prev.includes(idx) ? prev.filter((x) => x !== idx) : [...prev, idx].slice(-2))
      return
    }
    setSelected(idx); setSelEvent(null)
  }

  return (
    <div className="bg-brain-900/40 border border-brain-700/30 rounded-xl p-4 mb-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-white text-sm font-semibold flex items-center gap-2">
          <span>🕰️</span> {t('title')}
          {geo?.metricLabel && (
            <span className="text-slate-400 font-normal text-xs">· {t('line_metric', { metric: geo.metricLabel })}</span>
          )}
          <span className="text-slate-500 font-normal text-xs">
            {t('versions_count', { n: points.length })}{events.length > 0 && ` · ${t('events_count', { n: events.length })}`}
          </span>
        </h3>
        <div className="flex items-center gap-2">
          {points.length >= 3 && (
            <button
              onClick={() => { setCmpMode(!cmpMode); setCmpSel([]) }}
              className={`text-xs px-2.5 py-1 rounded-md border transition-colors ${
                cmpMode
                  ? 'border-amber-400/60 text-amber-200 bg-amber-500/15'
                  : 'border-brain-600/40 text-brain-200 hover:text-white hover:border-brain-500/60'
              }`}
              title={t('compare_hint')}
            >
              ⇄ {t('compare')}
            </button>
          )}
          <button
            onClick={loadStory}
            disabled={storyLoading}
            className="text-xs px-2.5 py-1 rounded-md border border-brain-600/40 text-brain-200 hover:text-white hover:border-brain-500/60 transition-colors"
          >
            {storyLoading ? t('story_writing') : `📖 ${t('storyline')}`}
          </button>
        </div>
      </div>

      {/* График: область+линия метрики, точки-версии, дорожка событий */}
      {geo && (
        <div className="relative overflow-x-auto pb-1">
          <svg width={geo.width} height={CHART_H + 40} className="block">
            <defs>
              <linearGradient id="tlArea" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#34d399" stopOpacity="0.22" />
                <stop offset="100%" stopColor="#34d399" stopOpacity="0" />
              </linearGradient>
            </defs>
            {/* сетка: 3 тихие линии + подписи диапазона */}
            {geo.metricKey && [0, 0.5, 1].map((f) => {
              const y = PAD_T + f * (CHART_H - PAD_T - PAD_B)
              const v = geo.vMax - f * (geo.vMax - geo.vMin)
              return (
                <g key={f}>
                  <line x1={PAD_L} x2={geo.width - PAD_R} y1={y} y2={y}
                        stroke="#334155" strokeOpacity="0.35" strokeWidth="1" />
                  <text x={PAD_L - 6} y={y + 3} textAnchor="end"
                        className="fill-slate-600" fontSize="9">{fmtV(v)}</text>
                </g>
              )
            })}
            {/* область + линия */}
            {geo.metricKey && geo.area && <path d={geo.area} fill="url(#tlArea)" />}
            {geo.metricKey && (
              <polyline points={geo.line} fill="none" stroke="#34d399"
                        strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
            )}
            {!geo.metricKey && (
              <line x1={PAD_L} x2={geo.width - PAD_R} y1={CHART_H / 2} y2={CHART_H / 2}
                    stroke="#475569" strokeWidth="1.5" strokeDasharray="4 4" />
            )}
            {/* точки-версии */}
            {geo.pts.map((d) => {
              const isCur = !!d.p.is_current
              const isSel = selected === d.i
              const isCmp = cmpSel.includes(d.i)
              return (
                <g key={d.i}>
                  {/* невидимая крупная цель клика/ховера */}
                  <circle cx={d.x} cy={d.y} r={14} fill="transparent"
                          style={{ cursor: 'pointer' }}
                          onMouseEnter={() => setHover(d.i)}
                          onMouseLeave={() => setHover((h) => (h === d.i ? null : h))}
                          onClick={() => clickPoint(d.i)} />
                  <circle cx={d.x} cy={d.y}
                          r={isCur ? 6 : isSel || isCmp ? 5 : 4}
                          fill={isCmp ? '#fbbf24' : isCur ? '#34d399' : d.carried ? '#0f172a' : '#64748b'}
                          stroke={isCmp ? '#fde68a' : isCur ? '#a7f3d0' : isSel ? '#e2e8f0' : '#94a3b8'}
                          strokeWidth={isCur || isSel || isCmp ? 2 : 1.5}
                          pointerEvents="none" />
                  {/* подпись даты под точкой */}
                  <text x={d.x} y={CHART_H + 12} textAnchor="middle" fontSize="9"
                        className={isCur ? 'fill-emerald-300 font-semibold' : 'fill-slate-500'}>
                    {isCur ? t('now') : (d.p.date || '').slice(5, 10).split('-').reverse().join('.')}
                  </text>
                </g>
              )
            })}
            {/* значение у последней точки */}
            {geo.metricKey && (() => {
              const lastPt = [...geo.pts].reverse().find((d) => d.value !== null)
              if (!lastPt) return null
              return (
                <text x={lastPt.x + 10} y={lastPt.y + 3} fontSize="11"
                      className="fill-emerald-300 font-semibold">
                  {fmtV(lastPt.value as number)}
                </text>
              )
            })()}
            {/* дорожка событий под осью */}
            {geo.evs.map((d, i) => (
              <g key={`e${i}`} style={{ cursor: 'pointer' }}
                 onClick={() => { setSelEvent(d.e); setSelected(null) }}>
                <rect x={d.x - 6} y={CHART_H + 18} width={12} height={16} fill="transparent" />
                <line x1={d.x} x2={d.x} y1={CHART_H + 20} y2={CHART_H + 30}
                      stroke={d.e.kind === 'meeting' ? '#38bdf8' : '#f59e0b'}
                      strokeWidth="2" strokeLinecap="round"
                      opacity={selEvent === d.e ? 1 : 0.65} />
              </g>
            ))}
            {geo.evs.length > 0 && (
              <text x={PAD_L - 6} y={CHART_H + 30} textAnchor="end" fontSize="9"
                    className="fill-slate-600">{t('events_label')}</text>
            )}
          </svg>

          {/* hover-тултип */}
          {hover !== null && geo.pts[hover] && (
            <div
              className="absolute z-20 pointer-events-none bg-brain-950/95 border border-brain-600/50 rounded-lg px-2.5 py-1.5 shadow-xl max-w-[240px]"
              style={{
                left: Math.min(geo.pts[hover].x + 12, geo.width - 250),
                top: Math.max(geo.pts[hover].y - 8, 0),
              }}
            >
              <div className="text-[10px] text-slate-400">
                v{geo.pts[hover].p.version} · {(geo.pts[hover].p.date || '').slice(0, 10)}
                {geo.pts[hover].value !== null && geo.metricLabel &&
                  ` · ${geo.metricLabel} ${fmtV(geo.pts[hover].value as number)}`}
              </div>
              <div className="text-[11px] text-slate-200 line-clamp-2">
                {geo.pts[hover].p.delta_summary || t('no_changes')}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Прогнозы — призрачные чипы (у них нет даты на оси) */}
      {future.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5 items-center">
          <span className="text-[10px] text-purple-300/70 uppercase tracking-wide">{t('forecasts')}</span>
          {future.map((f, i) => (
            <span key={i}
                  title={f.description || f.title}
                  className="text-[11px] px-2 py-0.5 rounded-full border border-dashed border-purple-400/40 text-purple-200/80 bg-purple-500/10">
              {f.kind === 'scenario' ? '🔮' : '📈'} {f.title.slice(0, 48)}
              {f.probability ? ` · ${Math.round(f.probability <= 1 ? f.probability * 100 : f.probability)}%` : ''}
              {f.horizon ? ` · ${f.horizon}` : ''}
            </span>
          ))}
        </div>
      )}

      {/* Сравнение двух дат: все изменения между выбранными версиями */}
      {cmpMode && (
        <div className="mt-2 bg-amber-500/5 border border-amber-500/25 rounded-lg p-3">
          {cmpSel.length < 2 ? (
            <div className="text-amber-200/80 text-xs">
              ⇄ {2 - cmpSel.length === 2 ? t('compare_select_two') : t('compare_select_more')}
            </div>
          ) : (() => {
            const [a, b] = [...cmpSel].sort((x, y) => x - y)
            const from = points[a], to = points[b]
            const changes = points
              .slice(a + 1, b + 1)
              .map((p) => ({ v: p.version, d: (p.date || '').slice(0, 10), s: p.delta_summary }))
              .filter((c) => c.s && c.s !== 'Минорные обновления' && c.s !== 'Без существенных изменений')
            return (
              <>
                <div className="text-amber-200 text-xs font-semibold mb-1.5">
                  ⇄ {`${(from?.date || '').slice(0, 10)} → ${(to?.date || '').slice(0, 10)}`}
                  <span className="text-amber-200/60 font-normal"> · v{from?.version} → v{to?.version}</span>
                </div>
                {changes.length === 0 ? (
                  <div className="text-slate-400 text-xs">{t('no_significant_changes')}</div>
                ) : (
                  <ul className="space-y-1">
                    {changes.map((c, i) => (
                      <li key={i} className="text-slate-200 text-xs">
                        <span className="text-slate-500 font-mono">{c.d}</span> — {c.s}
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )
          })()}
        </div>
      )}

      {/* Карточка выбранного события */}
      {selEvent && (
        <div className="mt-2 bg-brain-800/40 border border-brain-600/25 rounded-lg p-3">
          <div className="flex items-center gap-2 text-xs text-slate-400 mb-1">
            <span>{selEvent.kind === 'meeting' ? `💬 ${t('meeting')}` : `⚖️ ${t('decision')}`}</span>
            {selEvent.date && <span>{selEvent.date.slice(0, 10)}</span>}
            <button onClick={() => setSelEvent(null)} className="ml-auto text-slate-500 hover:text-white">✕</button>
          </div>
          <div className="text-slate-200 text-sm">{selEvent.title}</div>
        </div>
      )}

      {/* Карточка выбранной точки */}
      {sel && (
        <div className="mt-2 bg-brain-800/40 border border-brain-600/25 rounded-lg p-3">
          <div className="flex items-center gap-2 text-xs text-slate-400 mb-1 flex-wrap">
            <span className="text-white font-medium">v{sel.version}</span>
            {sel.date && <span>{sel.date.slice(0, 16).replace('T', ' ')}</span>}
            {sel.status && <span className="px-1.5 py-0.5 rounded bg-brain-700/50">{sel.status}</span>}
            {typeof sel.health_score === 'number' && <span>{t('health_pct', { n: Math.round(sel.health_score) })}</span>}
            {typeof sel.progress === 'number' && sel.progress > 0 && <span>{t('progress_pct', { n: Math.round(sel.progress) })}</span>}
            {typeof sel.tasks_completed === 'number' && <span>{t('tasks_done', { n: sel.tasks_completed })}</span>}
          </div>
          <div className="text-slate-200 text-sm">
            {sel.delta_summary || t('no_changes_in_version')}
          </div>
        </div>
      )}

      {/* Сторилайн */}
      {story && (
        <div className="mt-2 bg-gradient-to-br from-brain-800/50 to-purple-900/20 border border-purple-500/20 rounded-lg p-3">
          <div className="text-purple-300 text-xs font-semibold mb-1">📖 {t('story_title')}</div>
          <div className="text-slate-200 text-sm whitespace-pre-wrap">{story}</div>
        </div>
      )}
    </div>
  )
}
