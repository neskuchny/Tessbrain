'use client'

/**
 * MapFieldsView — вид «Поля» карты продукта (Sima Remix, слой 2).
 *
 * Card-grid ключевых полей проекта (миссия/цель/аудитория/MVP/идеал +
 * важное). Каждое поле: заполнено (значение + правка по клику) ИЛИ пропуск
 * (красный «!» + «Сима, помоги заполнить» → /ai/fill-field). Сверху
 * advice-bar «N пропусков · заполнить все». Замыкает цикл источник →
 * заполнение карты. Тип проекта (продукт/книга/идея) — в шапке.
 */
import { useState, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { simaFetch } from '@/sima/lib/api'
import { Sparkles, Loader2, Check, AlertCircle } from 'lucide-react'

// Поля карты: key совпадает с полем проекта; fillable — умеет ли Сима
// заполнить (см. _FILLABLE_FIELDS на бэкенде). important — широкая карточка.
const FIELD_DEFS: { key: string; labelKey: string; fillable: boolean; wide?: boolean }[] = [
  { key: 'description', labelKey: 'field_mission', fillable: true },
  { key: 'goal', labelKey: 'field_goal', fillable: true },
  { key: 'targetAudience', labelKey: 'field_audience', fillable: true },
  { key: 'mvpDescription', labelKey: 'field_mvp', fillable: true },
  { key: 'idealProduct', labelKey: 'field_ideal', fillable: true },
  { key: 'narrativeProblem', labelKey: 'field_problem', fillable: true },
  { key: 'narrativeSolution', labelKey: 'field_solution', fillable: true, wide: true },
]

export default function MapFieldsView() {
  const t = useTranslations('sima_map')
  const { project, setProject } = useSimaStore()
  const [filling, setFilling] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const [err, setErr] = useState('')

  const p = (project || {}) as Record<string, any>
  const isGap = useCallback((key: string) => !String(p[key] || '').trim(), [p])
  const gaps = FIELD_DEFS.filter((f) => isGap(f.key))
  const fillableGaps = gaps.filter((f) => f.fillable)

  const patchProject = useCallback(async (patch: Record<string, string>) => {
    if (!project) return
    await simaFetch(`/projects/${project.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    setProject({ ...(project as any), ...patch })
  }, [project, setProject])

  const fillOne = useCallback(async (key: string) => {
    if (!project || filling) return
    setFilling(key); setErr('')
    try {
      const res = await simaFetch('/ai/fill-field', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, field: key }),
      })
      const data = await res.json()
      if (data.value) await patchProject({ [key]: data.value })
      else setErr(data.error || t('fill_failed'))
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setFilling(null)
    }
  }, [project, filling, patchProject, t])

  const fillAll = useCallback(async () => {
    for (const f of fillableGaps) {
      // eslint-disable-next-line no-await-in-loop
      await fillOne(f.key)
    }
  }, [fillableGaps, fillOne])

  const saveEdit = useCallback(async (key: string) => {
    setEditing(null)
    if (editValue !== String(p[key] || '')) await patchProject({ [key]: editValue })
  }, [editValue, p, patchProject])

  if (!project) return null

  return (
    <div className="max-w-[1000px] mx-auto p-5">
      {/* advice-bar */}
      <div className={
        'flex items-center gap-2 px-3 py-2 rounded-xl border mb-4 text-sm ' +
        (gaps.length === 0
          ? 'border-green-500/30 bg-green-500/5 text-green-400'
          : 'border-amber-500/30 bg-amber-500/5 text-amber-300')
      }>
        {gaps.length === 0 ? <Check className="w-4 h-4" /> : <AlertCircle className="w-4 h-4" />}
        <span>{gaps.length === 0 ? t('map_complete') : t('map_gaps', { count: gaps.length })}</span>
        {fillableGaps.length > 0 && (
          <button
            onClick={fillAll}
            disabled={!!filling}
            className="ml-auto inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-sima-primary/15 border border-sima-primary/40 text-sima-primary text-xs hover:bg-sima-primary/25 disabled:opacity-50"
          >
            {filling ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
            {t('fill_all')}
          </button>
        )}
      </div>

      {/* map-head */}
      <div className="mb-4">
        <div className="text-[10px] uppercase tracking-wide text-sima-textDim">
          {t('map_kind', { kind: t(`kind_${(p.taskKind || 'product')}`) })}
        </div>
        <h1 className="text-xl font-semibold text-sima-text mt-0.5">
          {String(p.description || '').trim() || project.name}
        </h1>
      </div>

      {/* card-grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {FIELD_DEFS.map((f) => {
          const gap = isGap(f.key)
          const val = String(p[f.key] || '').trim()
          return (
            <div
              key={f.key}
              className={
                'rounded-xl border p-3 ' + (f.wide ? 'sm:col-span-2 ' : '') +
                (gap ? 'border-red-500/30 bg-red-500/[0.03]' : 'border-sima-border bg-sima-surface')
              }
            >
              <div className="flex items-center gap-2 mb-1.5">
                {gap
                  ? <span className="w-4 h-4 rounded-full bg-red-500/15 text-red-400 text-[11px] flex items-center justify-center font-bold">!</span>
                  : <Check className="w-3.5 h-3.5 text-green-400" />}
                <span className="text-xs font-semibold text-sima-textMuted">{t(f.labelKey)}</span>
              </div>

              {editing === f.key ? (
                <textarea
                  autoFocus
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  onBlur={() => saveEdit(f.key)}
                  rows={3}
                  className="w-full text-sm bg-sima-bg border border-sima-primary/50 rounded px-2 py-1 text-sima-text outline-none resize-y"
                />
              ) : gap ? (
                <div>
                  <p className="text-[12px] text-sima-textDim mb-2">{t('gap_hint')}</p>
                  <div className="flex gap-2">
                    {f.fillable && (
                      <button
                        onClick={() => fillOne(f.key)}
                        disabled={!!filling}
                        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-sima-primary/15 border border-sima-primary/40 text-sima-primary text-xs hover:bg-sima-primary/25 disabled:opacity-50"
                      >
                        {filling === f.key ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
                        {t('fill_button')}
                      </button>
                    )}
                    <button
                      onClick={() => { setEditing(f.key); setEditValue('') }}
                      className="px-2.5 py-1 rounded-lg border border-sima-border text-sima-textDim text-xs hover:bg-sima-surfaceLight"
                    >
                      {t('fill_manual')}
                    </button>
                  </div>
                </div>
              ) : (
                <p
                  className="text-sm text-sima-text leading-relaxed cursor-text hover:bg-sima-surfaceLight/40 rounded px-1 -mx-1"
                  title={t('edit_hint')}
                  onClick={() => { setEditing(f.key); setEditValue(val) }}
                >
                  {val}
                </p>
              )}
            </div>
          )
        })}
      </div>

      {err && <p className="text-xs text-red-400 mt-3">{err}</p>}
    </div>
  )
}
