'use client'

/**
 * Наблюдатель — присутствие агента на главной.
 *
 * Показывает, на какой фронт агент смотрит сейчас (ротация видна глазами),
 * последние наблюдения с реакциями и кнопку «осмотреться сейчас» (ручной
 * цикл — работает даже до включения фонового флага, если есть снапшоты).
 * Честность: выключен → говорим, как включить; мало снапшотов → говорим.
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import { Eye, Loader2 } from 'lucide-react'
import { authFetch, getUserIdFromToken } from '@/lib/authFetch'

interface Observation {
  id: string
  front_id: string
  format: string
  hook: string
  signal?: string
  ts: number
  reaction?: string | null
  outcome_status?: string
  outcome_note?: string
  report?: string
  board_id?: string
  board_name?: string
}

interface WatchItem { id: string; text: string; ts: number }

interface ObserverStatus {
  enabled: boolean
  snapshots_enabled: boolean
  snapshots_count: number
  current_front?: { front_id: string; title: string; ts: number } | null
  last_cycle_at?: number | null
  observations: Observation[]
  watch?: WatchItem[]
}

export default function ObserverWidget() {
  const t = useTranslations('observer')
  const [st, setSt] = useState<ObserverStatus | null>(null)
  const [running, setRunning] = useState(false)
  const [note, setNote] = useState('')

  const uid = typeof window !== 'undefined' ? getUserIdFromToken() : null

  const load = useCallback(async () => {
    if (!uid) return
    try {
      const r = await authFetch(`/api/v1/observer/status?user_id=${encodeURIComponent(uid)}`)
      if (r.ok) setSt(await r.json())
    } catch { /* виджет молча не показывается */ }
  }, [uid])

  useEffect(() => { load() }, [load])

  const runNow = async () => {
    if (!uid || running) return
    setRunning(true)
    setNote('')
    try {
      const r = await authFetch(`/api/v1/observer/run?user_id=${encodeURIComponent(uid)}`, { method: 'POST' })
      const d = await r.json()
      if (d.status === 'observed') setNote(t('run_observed'))
      else if (d.status === 'silent') setNote(t('run_silent'))
      else if (d.status === 'few_snapshots') setNote(t('run_few_snapshots'))
      else setNote(t('run_failed'))
      await load()
    } catch { setNote(t('run_failed')) } finally { setRunning(false) }
  }

  const react = async (oid: string, reaction: 'accepted' | 'declined') => {
    if (!uid) return
    // мгновенная подсветка выбора; сервер догоняет следом (load())
    setSt((prev) => prev ? {
      ...prev,
      observations: (prev.observations || []).map((o) =>
        o.id === oid ? { ...o, reaction } : o),
    } : prev)
    try {
      await authFetch(`/api/v1/observer/reaction?user_id=${encodeURIComponent(uid)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ observation_id: oid, reaction }),
      })
      await load()
    } catch { /* оставим как есть */ }
  }

  // «Разобрать подробнее»: премиум-разбор, кэшируется на наблюдении.
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [expanding, setExpanding] = useState<string | null>(null)
  const [reports, setReports] = useState<Record<string, string>>({})

  const expand = async (o: Observation) => {
    if (expandedId === o.id) { setExpandedId(null); return }
    const known = o.report || reports[o.id]
    if (known) { setReports((r) => ({ ...r, [o.id]: known })); setExpandedId(o.id); return }
    if (!uid || expanding) return
    setExpanding(o.id)
    try {
      const r = await authFetch(`/api/v1/observer/expand?user_id=${encodeURIComponent(uid)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ observation_id: o.id }),
      })
      const d = await r.json()
      if (d.status === 'ok' && d.report) {
        setReports((rr) => ({ ...rr, [o.id]: d.report }))
        setExpandedId(o.id)
      } else {
        setNote(t('expand_failed'))
      }
    } catch { setNote(t('expand_failed')) } finally { setExpanding(null) }
  }

  // «Собрать доску»: NL-планировщик строит автоматизацию-черновик.
  const [building, setBuilding] = useState<string | null>(null)
  const makeBoard = async (o: Observation) => {
    if (!uid || building) return
    setBuilding(o.id)
    setNote('')
    try {
      const r = await authFetch(`/api/v1/observer/make-board?user_id=${encodeURIComponent(uid)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ observation_id: o.id }),
      })
      const d = await r.json()
      if (d.status === 'ok') setNote(t('board_created', { name: d.board_name }))
      else setNote(t('board_failed'))
      await load()
    } catch { setNote(t('board_failed')) } finally { setBuilding(null) }
  }

  const [watchText, setWatchText] = useState('')
  const addWatch = async () => {
    if (!uid || !watchText.trim()) return
    try {
      await authFetch(`/api/v1/observer/watch?user_id=${encodeURIComponent(uid)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: watchText.trim() }),
      })
      setWatchText('')
      await load()
    } catch { /* оставим как есть */ }
  }
  const removeWatch = async (wid: string) => {
    if (!uid) return
    try {
      await authFetch(`/api/v1/observer/watch/remove?user_id=${encodeURIComponent(uid)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ watch_id: wid }),
      })
      await load()
    } catch { /* оставим как есть */ }
  }

  if (!st) return null
  // Совсем нет субстрата и всё выключено — не занимаем место на главной.
  if (!st.snapshots_enabled && !st.enabled && st.snapshots_count === 0
      && (st.observations || []).length === 0) return null

  const fmtTs = (ts?: number | null) =>
    ts ? new Date(ts * 1000).toLocaleDateString() : '—'

  return (
    <div className="card bg-gradient-to-br from-brain-900/70 to-indigo-900/20 border-indigo-500/20 p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Eye className="w-5 h-5 text-indigo-300 brain-pulse shrink-0" />
          <div className="min-w-0">
            <div className="text-sm font-semibold text-white truncate">{t('title')}</div>
            <div className="text-xs text-brain-400 truncate">
              {st.current_front?.title
                ? t('watching_now', { front: st.current_front.title })
                : st.snapshots_count > 0
                  ? t('idle_with_snapshots', { count: st.snapshots_count })
                  : t('idle_no_snapshots')}
            </div>
          </div>
        </div>
        <button
          onClick={runNow}
          disabled={running || st.snapshots_count === 0}
          className="text-xs px-3 py-1.5 rounded-md bg-indigo-600/70 hover:bg-indigo-500/70 text-white disabled:opacity-40 shrink-0 flex items-center gap-1.5"
          title={st.snapshots_count === 0 ? t('run_disabled_title') : t('run_title')}
        >
          {running && <Loader2 className="w-3 h-3 animate-spin" />}
          {t('run_button')}
        </button>
      </div>

      {note && <div className="text-xs text-brain-300 mt-2">{note}</div>}
      {!st.enabled && (
        <div className="text-[11px] text-amber-300/80 mt-2">{t('disabled_hint')}</div>
      )}

      {(st.observations || []).length > 0 && (
        <div className="mt-3 space-y-2">
          {st.observations.slice(0, 3).map((o) => (
            <div key={o.id} className="rounded-lg bg-brain-950/40 border border-brain-700/40 px-3 py-2">
              <div className="text-[10px] uppercase tracking-wide text-indigo-300/80 mb-0.5">
                {fmtTs(o.ts)} · {o.front_id}
                {o.format === 'follow_up' && <span className="ml-1 text-emerald-300/90">· {t('follow_up_badge')}</span>}
              </div>
              <div className="text-sm text-brain-100">{o.hook}</div>
              {o.outcome_note && (
                <div className="text-[11px] text-brain-400 mt-1">
                  ↳ {t('outcome_label', { status: o.outcome_status || '?' })}: {o.outcome_note}
                </div>
              )}
              <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                <button onClick={() => expand(o)}
                  disabled={expanding === o.id}
                  className="text-[11px] px-2 py-0.5 rounded bg-indigo-600/30 text-indigo-200 hover:bg-indigo-600/50 disabled:opacity-50 flex items-center gap-1">
                  {expanding === o.id && <Loader2 className="w-3 h-3 animate-spin" />}
                  {expandedId === o.id ? t('collapse') : t('expand_button')}
                </button>
                {o.board_id ? (
                  <span className="text-[10px] text-indigo-300/80" title={o.board_name || ''}>
                    {t('board_badge')}
                  </span>
                ) : (
                  <button onClick={() => makeBoard(o)}
                    disabled={building === o.id}
                    className="text-[11px] px-2 py-0.5 rounded bg-brain-800/70 text-brain-300 hover:text-white disabled:opacity-50 flex items-center gap-1">
                    {building === o.id && <Loader2 className="w-3 h-3 animate-spin" />}
                    {t('board_button')}
                  </button>
                )}
                {/* Кнопки остаются на месте, выбранная подсвечена явно:
                    раньше «✓» была в лейбле НЕвыбранной кнопки, а после
                    клика обе заменялись серым текстом — выглядело так,
                    будто выбор не сработал */}
                <button onClick={() => react(o.id, 'accepted')}
                  aria-pressed={o.reaction === 'accepted'}
                  className={`text-[11px] px-2 py-0.5 rounded ${o.reaction === 'accepted'
                    ? 'bg-emerald-500/80 text-white ring-1 ring-emerald-300'
                    : 'bg-emerald-600/20 text-emerald-200/80 hover:bg-emerald-600/40'}`}>
                  {o.reaction === 'accepted' ? '✓ ' : ''}{t('react_interesting')}
                </button>
                <button onClick={() => react(o.id, 'declined')}
                  aria-pressed={o.reaction === 'declined'}
                  className={`text-[11px] px-2 py-0.5 rounded ${o.reaction === 'declined'
                    ? 'bg-brain-600 text-white ring-1 ring-brain-400'
                    : 'bg-brain-800/70 text-brain-400 hover:text-brain-200'}`}>
                  {o.reaction === 'declined' ? '✓ ' : ''}{t('react_not_interesting')}
                </button>
                {o.reaction != null && (
                  <span className="text-[10px] text-brain-500">
                    {o.reaction === 'accepted' ? t('reacted_interesting') : t('reacted_not_interesting')}
                  </span>
                )}
              </div>
              {expandedId === o.id && (o.report || reports[o.id]) && (
                <div className="mt-2 border-t border-brain-700/40 pt-2 max-h-72 overflow-y-auto">
                  {/* разбор — markdown от модели: сырые ## и ** нечитаемы */}
                  <div className="prose prose-invert prose-sm max-w-none text-[12px] text-brain-200 prose-headings:text-brain-100 prose-headings:text-[13px] prose-headings:mt-2 prose-headings:mb-1 prose-p:my-1 prose-li:my-0.5 prose-strong:text-brain-50">
                    <ReactMarkdown>{o.report || reports[o.id] || ''}</ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Поручения: «проследи за …» — персональный фронт в ротации агента */}
      <div className="mt-3 pt-2 border-t border-brain-700/40">
        <div className="flex gap-1.5">
          <input
            value={watchText}
            onChange={(e) => setWatchText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') addWatch() }}
            placeholder={t('watch_placeholder')}
            className="flex-1 text-xs px-2 py-1.5 rounded-md bg-brain-950/50 border border-brain-700/50 text-brain-100 placeholder-brain-500 focus:outline-none focus:border-indigo-500/50"
          />
          <button onClick={addWatch} disabled={!watchText.trim()}
            className="text-xs px-2.5 py-1.5 rounded-md bg-brain-800/70 text-brain-300 hover:text-white disabled:opacity-40">
            {t('watch_add')}
          </button>
        </div>
        {(st.watch || []).length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2">
            {(st.watch || []).map((w) => (
              <span key={w.id}
                className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-indigo-500/10 border border-indigo-500/30 text-indigo-200">
                👁 {w.text}
                <button onClick={() => removeWatch(w.id)}
                  className="text-indigo-300/60 hover:text-white ml-0.5">✕</button>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
