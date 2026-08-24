'use client'

import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import {
  AlertTriangle,
  Check,
  Edit3,
  Trash2,
  Clock,
  RefreshCw,
  CheckCircle2,
  User,
  Mic,
  Sparkles,
} from 'lucide-react'

/**
 * MemoryReviewPanel — review queue UI (issue #112 T-10).
 *
 * Показывает Task/Decision с needs_human_review=True — записи, где
 * система НЕ уверена в attribution (assignee не в attendees, нет speaker,
 * low confidence). Человек подтверждает / исправляет / удаляет.
 *
 * Это Foundry-style honest UX: вместо «100% accuracy» (вранье) система
 * честно выносит спорные случаи на проверку, и УЧИТСЯ на исправлениях.
 *
 * Workflow:
 *   - confirm: система была права
 *   - fix: дать правильное имя assignee/maker
 *   - delete: эта attribution неверна
 *   - skip: отложить
 *
 * Backend: GET /api/v1/insights/needs-review, POST /insights/review/{id}/apply
 */

interface DedupMember {
  id: string
  name: string
  is_canonical?: boolean
}

interface ReviewItem {
  id?: string                          // correction_id (если из corrections_store)
  entity_id?: string
  entity_type?: string                 // 'task' | 'decision' | 'dedup'
  members?: DedupMember[]              // dedup: участники кластера (чекбоксы)
  title?: string
  original_attribution?: string
  original_confidence?: string
  review_reason?: string
  assignee_name?: string
  assignee_speaker?: string
  decision_maker_resolved?: string
  decision_maker_speaker?: string
  confidence?: string
  created_at?: string
}

interface ReviewStats {
  pending: number
  by_action?: Record<string, number>
  system_accuracy_pct?: number | null
}

interface MemoryReviewPanelProps {
  userId: string | null
}

export default function MemoryReviewPanel({ userId }: MemoryReviewPanelProps) {
  const t = useTranslations('memory_review_panel')
  const [items, setItems] = useState<ReviewItem[]>([])
  const [stats, setStats] = useState<ReviewStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [actingId, setActingId] = useState<string | null>(null)
  // dedup: какие участники кластера ОТЦЕПЛЕНЫ от слияния (cid -> set id)
  const [dedupUnchecked, setDedupUnchecked] = useState<Record<string, string[]>>({})
  const [editingId, setEditingId] = useState<string | null>(null)
  const [fixValue, setFixValue] = useState('')
  const [consolidating, setConsolidating] = useState(false)
  const [consolidateMsg, setConsolidateMsg] = useState('')

  const fetchQueue = useCallback(async () => {
    if (!userId) {
      setLoading(false)
      return
    }
    try {
      const res = await fetch(`/api/v1/insights/needs-review?user_id=${encodeURIComponent(userId)}`)
      if (res.ok) {
        const data = await res.json()
        setItems(Array.isArray(data.items) ? data.items : [])
        setStats(data.stats ?? null)
      }
    } catch (e) {
      console.error('review queue fetch failed:', e)
    } finally {
      setLoading(false)
    }
  }, [userId])

  useEffect(() => {
    fetchQueue()
  }, [fetchQueue])

  // Ночная консолидация: коррекции → temporal decay → консолидация памяти →
  // снапшоты → инсайты. Плановый прогон (2:00–6:00 UTC) на локальной машине
  // почти не срабатывает, поэтому даём ручной запуск. Работает в фоне —
  // опрашиваем /nightly-status пока is_running, потом обновляем очередь.
  const runConsolidation = useCallback(async () => {
    if (consolidating) return
    setConsolidating(true)
    setConsolidateMsg(t('consolidation_starting'))
    try {
      // Гоняем только текущий аккаунт (не всех тенантов).
      const uq = userId ? `?user_id=${encodeURIComponent(userId)}` : ''
      const res = await fetch(`/api/v1/nightly-run${uq}`, { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok || data.started === false) {
        setConsolidateMsg(data.detail || t('consolidation_already_running'))
      } else {
        setConsolidateMsg(t('consolidation_in_progress'))
      }
      // Poll до завершения (макс. ~10 мин).
      for (let i = 0; i < 200; i++) {
        await new Promise((r) => setTimeout(r, 3000))
        const st = await fetch(`/api/v1/nightly-status`).then((r) => r.json()).catch(() => null)
        if (st && st.is_running === false) {
          const err = st.last_result?.error
          setConsolidateMsg(err ? t('consolidation_error', { error: err }) : t('consolidation_done'))
          break
        }
      }
      fetchQueue()
    } catch (e) {
      setConsolidateMsg(t('consolidation_error', { error: (e as Error).message }))
    } finally {
      setConsolidating(false)
    }
  }, [consolidating, fetchQueue, t])

  const applyAction = async (
    item: ReviewItem,
    action: 'confirm' | 'fix' | 'delete' | 'skip',
    correction?: string,
    extras?: { selected_ids?: string[]; comment?: string },
  ) => {
    // correction_id — если запись из corrections_store, иначе используем
    // entity_id как fallback (graph_scan mode)
    const cid = item.id || item.entity_id
    if (!cid || actingId) return
    setActingId(cid)
    try {
      const res = await fetch(
        `/api/v1/insights/review/${cid}/apply?user_id=${userId ?? ''}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action, correction, ...(extras || {}) }),
        },
      )
      if (res.ok) {
        // Убираем из локального списка (кроме skip)
        if (action !== 'skip') {
          setItems((prev) => prev.filter((i) => (i.id || i.entity_id) !== cid))
        }
        setEditingId(null)
        setFixValue('')
        // Обновим stats
        fetchQueue()
      }
    } catch (e) {
      console.error('apply action failed:', e)
    } finally {
      setActingId(null)
    }
  }

  return (
    <div className="h-full overflow-auto p-4 space-y-4">
      {/* Header + stats */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <AlertTriangle className="w-5 h-5 text-orange-400" />
            {t('title')}
          </h2>
          <p className="text-sm text-brain-400 mt-1">
            {t('subtitle')}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {consolidateMsg && (
            <span className="text-xs text-brain-400">{consolidateMsg}</span>
          )}
          <button
            onClick={runConsolidation}
            disabled={consolidating}
            title={t('run_consolidation_hint')}
            className="text-xs text-indigo-300 hover:text-indigo-200 px-3 py-1.5 rounded-md bg-indigo-600/20 hover:bg-indigo-600/30 inline-flex items-center gap-1.5 disabled:opacity-50"
          >
            <Sparkles className={`w-3.5 h-3.5 ${consolidating ? 'animate-pulse' : ''}`} />
            {consolidating ? t('consolidating') : t('run_consolidation')}
          </button>
          <button
            onClick={fetchQueue}
            className="text-xs text-brain-400 hover:text-brain-300 px-3 py-1.5 rounded-md hover:bg-brain-800/40 inline-flex items-center gap-1.5"
          >
            <RefreshCw className="w-3.5 h-3.5" /> {t('refresh')}
          </button>
        </div>
      </div>

      {/* Stats bar */}
      {stats && (
        <div className="flex flex-wrap gap-4 text-sm card bg-brain-900/40">
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4 text-orange-400" />
            <span className="font-bold text-brain-100">{stats.pending}</span>
            <span className="text-brain-500">{t('stat_pending')}</span>
          </div>
          {stats.system_accuracy_pct !== null &&
            stats.system_accuracy_pct !== undefined && (
              <div className="flex items-center gap-2">
                <CheckCircle2 className="w-4 h-4 text-green-400" />
                <span className="font-bold text-brain-100">
                  {stats.system_accuracy_pct}%
                </span>
                <span className="text-brain-500">{t('stat_accuracy')}</span>
              </div>
            )}
          {stats.by_action?.fix !== undefined && (
            <div className="flex items-center gap-2">
              <Edit3 className="w-4 h-4 text-brain-400" />
              <span className="font-bold text-brain-100">
                {stats.by_action.fix}
              </span>
              <span className="text-brain-500">{t('stat_fixed')}</span>
            </div>
          )}
        </div>
      )}

      {/* Queue */}
      {loading ? (
        <div className="text-center py-12 text-brain-400">{t('loading')}</div>
      ) : items.length === 0 ? (
        <div className="text-center py-12">
          <CheckCircle2 className="w-12 h-12 text-green-400 mx-auto mb-3" />
          <h3 className="text-lg font-semibold text-brain-200">
            {t('empty_title')}
          </h3>
          <p className="text-sm text-brain-400 mt-1">
            {t('empty_subtitle')}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((item) => {
            const cid = item.id || item.entity_id || ''
            const attribution =
              item.original_attribution ||
              item.assignee_name ||
              item.decision_maker_resolved ||
              '—'
            const speaker =
              item.assignee_speaker || item.decision_maker_speaker
            const isEditing = editingId === cid
            return (
              <div key={cid} className="card bg-brain-900/40">
                <div className="flex items-start gap-3">
                  <div
                    className={`badge ${
                      item.entity_type === 'decision'
                        ? 'badge-info'
                        : 'badge-warning'
                    }`}
                  >
                    {item.entity_type === 'decision' ? t('type_decision')
                      : item.entity_type === 'dedup' ? t('type_dedup') : t('type_task')}
                  </div>
                  <div className="flex-1 min-w-0">
                    <h3 className="font-medium text-brain-100 truncate">
                      {item.title || item.entity_id}
                    </h3>
                    <div className="flex flex-wrap items-center gap-3 mt-1.5 text-xs">
                      <span className="inline-flex items-center gap-1 text-brain-300">
                        <User className="w-3 h-3" />
                        {t('attributed_to')} <span className="font-medium">{attribution}</span>
                      </span>
                      {speaker && (
                        <span className="inline-flex items-center gap-1 text-brain-500">
                          <Mic className="w-3 h-3" />
                          {speaker}
                        </span>
                      )}
                      {item.review_reason && (
                        <span className="text-orange-400/80">
                          ⚠ {item.review_reason}
                        </span>
                      )}
                    </div>

                    {/* dedup: выбор кого сливать — в кластере могут быть и
                        настоящие дубли, и чужие (две разные Кати). Отцепленные
                        при подтверждении уходят в негативную память. */}
                    {item.entity_type === 'dedup' && (item.members?.length || 0) > 0 && (
                      <div className="mt-2 space-y-1">
                        {item.members!.map((m) => {
                          const un = dedupUnchecked[cid] || []
                          const checked = m.is_canonical || !un.includes(m.id)
                          return (
                            <label key={m.id} className="flex items-center gap-2 text-xs text-brain-300 cursor-pointer">
                              <input
                                type="checkbox"
                                checked={checked}
                                disabled={m.is_canonical}
                                onChange={() => setDedupUnchecked((prev) => {
                                  const cur = prev[cid] || []
                                  return {
                                    ...prev,
                                    [cid]: cur.includes(m.id)
                                      ? cur.filter((x) => x !== m.id)
                                      : [...cur, m.id],
                                  }
                                })}
                                className="rounded border-brain-600 bg-brain-800"
                              />
                              <span className={m.is_canonical ? 'font-medium text-brain-100' : ''}>
                                {m.name}{m.is_canonical ? ` ${t('canonical_marker')}` : ''}
                              </span>
                            </label>
                          )
                        })}
                        <div className="text-[10px] text-brain-500">
                          {t('dedup_hint')}
                        </div>
                      </div>
                    )}

                    {/* Fix-input (когда редактируем) */}
                    {isEditing && (
                      <div className="mt-3 flex items-center gap-2">
                        <input
                          type="text"
                          value={fixValue}
                          onChange={(e) => setFixValue(e.target.value)}
                          placeholder={t('fix_placeholder')}
                          className="flex-1 px-3 py-1.5 rounded-md bg-brain-800/60 border border-brain-700/40 text-sm text-brain-100 focus:outline-none focus:border-brain-500"
                          autoFocus
                        />
                        <button
                          onClick={() => applyAction(item, 'fix', fixValue)}
                          disabled={!fixValue.trim() || actingId === cid}
                          className="px-3 py-1.5 rounded-md bg-brain-600 hover:bg-brain-500 text-white text-sm disabled:opacity-50"
                        >
                          {t('save')}
                        </button>
                        <button
                          onClick={() => {
                            setEditingId(null)
                            setFixValue('')
                          }}
                          className="px-3 py-1.5 rounded-md text-brain-400 hover:text-brain-200 text-sm"
                        >
                          {t('cancel')}
                        </button>
                      </div>
                    )}
                  </div>

                  {/* Action buttons (когда не редактируем) */}
                  {!isEditing && (
                    <div className="flex items-center gap-1 flex-shrink-0">
                      <button
                        onClick={() => {
                          if (item.entity_type === 'dedup' && item.members?.length) {
                            const un = dedupUnchecked[cid] || []
                            const selected = item.members
                              .filter((m) => m.is_canonical || !un.includes(m.id))
                              .map((m) => m.id)
                            let comment = ''
                            if (un.length > 0) {
                              comment = window.prompt(
                                t('confirm_dedup_prompt'),
                                '') || ''
                            }
                            applyAction(item, 'confirm', undefined,
                              { selected_ids: selected, comment })
                          } else {
                            applyAction(item, 'confirm')
                          }
                        }}
                        disabled={actingId === cid}
                        title={t('confirm_hint')}
                        className="p-2 rounded-md hover:bg-green-500/20 text-green-400 transition-colors disabled:opacity-50"
                      >
                        <Check className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => {
                          setEditingId(cid)
                          setFixValue(attribution !== '—' ? attribution : '')
                        }}
                        disabled={actingId === cid}
                        title={t('fix_hint')}
                        className="p-2 rounded-md hover:bg-brain-700/40 text-brain-300 transition-colors disabled:opacity-50"
                      >
                        <Edit3 className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => {
                          if (item.entity_type === 'dedup') {
                            const comment = window.prompt(
                              t('reject_dedup_prompt'),
                              '') || ''
                            applyAction(item, 'delete', undefined, { comment })
                          } else {
                            applyAction(item, 'delete')
                          }
                        }}
                        disabled={actingId === cid}
                        title={t('delete_hint')}
                        className="p-2 rounded-md hover:bg-red-500/20 text-red-400 transition-colors disabled:opacity-50"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
