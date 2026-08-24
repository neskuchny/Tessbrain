'use client'

/**
 * ImplementationPanel — слой «Реализация» (эталон Sima Remix, экран 5).
 *
 * Статус реализации проекта: прогресс «N из M · %», список блоков с
 * контрактом Kanon (готово / в работе / не готово — из ok/partial/missing),
 * последние вердикты верификаций, действия: «Проверить тестами» (kanon
 * verify), «Сверить с ТЗ», handoff кодинг-агенту, экспорт.
 *
 * Честность: всё из /sima/kanon/status — реальные контракты и вердикты, не
 * выдуманный прогресс. Kanon выключен на сервере → говорим об этом прямо.
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { simaFetch } from '@/sima/lib/api'
import {
  CheckCircle2, CircleDashed, CircleAlert, Loader2, RefreshCw,
  PlayCircle, FileDown, Bot,
} from 'lucide-react'
import ExportPanel from '@/sima/components/Export/ExportPanel'

interface BlockStatus {
  block_id: string
  name: string
  rows: Record<string, 'ok' | 'partial' | 'missing'>
  ready_for_handoff?: boolean
  last_verdict?: string
  cascade_state?: string
  checked_at?: string
}
interface KanonStatus {
  success?: boolean
  error?: string
  blocks_total: number
  blocks_ready_for_handoff: number
  blocks: BlockStatus[]
}

// Готовность блока по контракту: все ключевые аспекты ok → готов;
// есть хоть что-то → в работе; пусто → не начат.
function blockState(b: BlockStatus): 'done' | 'wip' | 'todo' {
  const vals = Object.values(b.rows || {})
  if (!vals.length) return 'todo'
  if (b.last_verdict === 'pass') return 'done'
  const oks = vals.filter(v => v === 'ok').length
  if (oks === vals.length) return 'done'
  if (oks > 0 || vals.some(v => v === 'partial')) return 'wip'
  return 'todo'
}

export default function ImplementationPanel() {
  const t = useTranslations('sima_impl')
  const project = useSimaStore(s => s.project)
  const [status, setStatus] = useState<KanonStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [verifying, setVerifying] = useState(false)
  const [note, setNote] = useState('')
  const [showExport, setShowExport] = useState(false)

  const load = useCallback(async () => {
    if (!project?.id) return
    setLoading(true); setNote('')
    try {
      const r = await simaFetch(`/kanon/status?projectId=${encodeURIComponent(String(project.id))}`)
      const d = await r.json()
      if (d?.success) setStatus(d)
      else setNote(d?.error || t('load_failed'))
    } catch {
      setNote(t('load_failed'))
    } finally { setLoading(false) }
  }, [project?.id, t])

  useEffect(() => { load() }, [load])

  const verify = useCallback(async () => {
    if (!project?.id || verifying) return
    setVerifying(true); setNote('')
    try {
      const r = await simaFetch('/kanon/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: String(project.id) }),
      })
      const d = await r.json()
      if (!d?.success && d?.error) {
        setNote(d.error)
      } else if (d?.counts) {
        // Проверка всего проекта: показываем сводку, а не молчим.
        const c = d.counts as Record<string, number>
        const parts = [
          c.pass ? `${c.pass} ✓` : '',
          c.fail ? `${c.fail} ✗` : '',
          c.inconclusive ? `${c.inconclusive} — нет доказательств` : '',
        ].filter(Boolean)
        setNote(parts.join(' · ') || t('verify_failed'))
      }
      await load()
    } catch {
      setNote(t('verify_failed'))
    } finally { setVerifying(false) }
  }, [project?.id, verifying, load, t])

  if (!project) return null

  const blocks = status?.blocks || []
  const done = blocks.filter(b => blockState(b) === 'done').length
  const pct = blocks.length ? Math.round((100 * done) / blocks.length) : 0
  const verdicts = blocks
    .filter(b => b.checked_at)
    .sort((a, b) => String(b.checked_at).localeCompare(String(a.checked_at)))
    .slice(0, 8)

  const stateBadge = (s: 'done' | 'wip' | 'todo') =>
    s === 'done'
      ? <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-sima-accentGreen/15 text-sima-accentGreen uppercase">{t('state_done')}</span>
      : s === 'wip'
        ? <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-sima-accentYellow/15 text-sima-accentYellow uppercase">{t('state_wip')}</span>
        : <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-sima-textDim/15 text-sima-textDim uppercase">{t('state_todo')}</span>

  const stateIcon = (s: 'done' | 'wip' | 'todo') =>
    s === 'done'
      ? <CheckCircle2 className="w-4 h-4 text-sima-accentGreen shrink-0" />
      : s === 'wip'
        ? <CircleDashed className="w-4 h-4 text-sima-accentYellow shrink-0" />
        : <CircleAlert className="w-4 h-4 text-sima-textDim shrink-0" />

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-4">
      {/* Шапка статуса: прогресс + инструмент */}
      <div className="rounded-2xl border border-sima-border bg-sima-surface p-5">
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div>
            <div className="text-[10px] uppercase tracking-wide text-sima-textDim">
              {t('header_kicker')}
            </div>
            <h2 className="text-xl font-semibold text-sima-text mt-0.5">
              {project.name || t('project_fallback')}
            </h2>
          </div>
          <div className="flex items-center gap-4">
            <div className="text-right">
              <div className="text-[12px] text-sima-textMuted">
                {t('progress_line', { done, total: blocks.length, pct })}
              </div>
              <div className="mt-1 h-1.5 w-44 rounded-full bg-sima-border overflow-hidden">
                <div className="h-full bg-sima-accentGreen transition-all"
                  style={{ width: `${pct}%` }} />
              </div>
            </div>
            <span className="px-3 py-1.5 rounded-full bg-sima-primary/15 text-sima-primary text-[12px] font-semibold flex items-center gap-1.5">
              <Bot className="w-3.5 h-3.5" /> Claude Code
            </span>
          </div>
        </div>
      </div>

      <div className="grid md:grid-cols-[1fr,320px] gap-4 items-start">
        {/* Список блоков со статусами */}
        <div className="rounded-2xl border border-sima-border bg-sima-surface p-3 space-y-1.5">
          {loading && (
            <div className="flex items-center gap-2 text-[12px] text-sima-textMuted p-2">
              <Loader2 className="w-4 h-4 animate-spin" /> {t('loading')}
            </div>
          )}
          {!loading && blocks.length === 0 && (
            <p className="text-[12px] text-sima-textMuted p-2">{t('empty_hint')}</p>
          )}
          {blocks.map((b) => {
            const s = blockState(b)
            const missing = Object.entries(b.rows || {})
              .filter(([, v]) => v !== 'ok').map(([k]) => t(`aspect_${k}`))
            return (
              <div key={b.block_id}
                className="flex items-center gap-2.5 px-3 py-2 rounded-xl border border-sima-border/60 bg-sima-bg/40">
                {stateIcon(s)}
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] font-medium text-sima-text truncate">{b.name}</div>
                  <div className="text-[11px] text-sima-textMuted truncate">
                    {s === 'done'
                      ? (b.last_verdict === 'pass' ? t('checked_ok') : t('contract_full'))
                      : missing.length
                        ? t('missing_line', { aspects: missing.slice(0, 3).join(', ') })
                        : t('contract_empty')}
                  </div>
                </div>
                {b.cascade_state === 'desync' && (
                  <span className="px-1.5 rounded bg-sima-accentRed/15 text-sima-accentRed text-[9px] uppercase font-semibold">desync</span>
                )}
                {stateBadge(s)}
              </div>
            )
          })}
        </div>

        {/* Лог верификаций + действия */}
        <div className="space-y-4">
          <div className="rounded-2xl border border-sima-border bg-sima-surface p-4 space-y-2">
            <div className="text-[10px] uppercase tracking-wide text-sima-textDim">
              {t('log_title')}
            </div>
            {verdicts.length === 0 && (
              <p className="text-[11px] text-sima-textMuted">{t('log_empty')}</p>
            )}
            {verdicts.map((v) => (
              <div key={v.block_id} className="flex items-start gap-2 text-[11px]">
                <span className="font-mono text-sima-textDim shrink-0">
                  {String(v.checked_at).slice(11, 16) || '—'}
                </span>
                <span className="text-sima-text flex-1 truncate">{v.name}</span>
                <span className={
                  v.last_verdict === 'pass' ? 'text-sima-accentGreen'
                    : v.last_verdict === 'fail' ? 'text-sima-accentRed'
                      : 'text-sima-accentYellow'
                }>{v.last_verdict}</span>
              </div>
            ))}
            <button
              onClick={verify}
              disabled={verifying || loading}
              className="w-full mt-1 py-2 rounded-lg bg-sima-primary hover:bg-sima-primaryHover text-white text-[12px] font-medium flex items-center justify-center gap-2 disabled:opacity-50 transition-colors"
            >
              {verifying ? <Loader2 className="w-4 h-4 animate-spin" /> : <PlayCircle className="w-4 h-4" />}
              {t('verify_button')}
            </button>
            <button
              onClick={load}
              disabled={loading}
              className="w-full py-1.5 rounded-lg border border-sima-border text-sima-textMuted hover:text-sima-text text-[11px] flex items-center justify-center gap-1.5 disabled:opacity-50 transition-colors"
            >
              <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} /> {t('refresh')}
            </button>
          </div>

          <div className="rounded-2xl border border-sima-border bg-sima-surface p-4 space-y-1.5">
            <div className="text-[10px] uppercase tracking-wide text-sima-textDim">
              {t('actions_title')}
            </div>
            <p className="text-[11px] text-sima-textMuted leading-relaxed">
              {t('handoff_explainer')}
            </p>
            <button
              onClick={() => setShowExport(v => !v)}
              className="w-full py-2 rounded-lg border border-sima-border text-sima-text hover:bg-sima-surfaceLight text-[12px] flex items-center justify-center gap-2 transition-colors"
            >
              <FileDown className="w-4 h-4" /> {t('export_button')}
            </button>
          </div>
          {note && <p className="text-[11px] text-sima-accentRed px-1">{note}</p>}
        </div>
      </div>

      {/* Экспорт — прежняя панель, теперь секцией внутри «Реализации» */}
      {showExport && (
        <div className="rounded-2xl border border-sima-border bg-sima-surface p-4">
          <ExportPanel />
        </div>
      )}
    </div>
  )
}
