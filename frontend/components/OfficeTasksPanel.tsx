'use client'

/**
 * OfficeTasksPanel — зона выполнения офисных задач под приёмкой.
 *
 * Дополняет очередь Vibe Tasking (кодинг-хэндоффы) второй половиной:
 * задачи НЕ про код — «собери бриф», «письмо с цифрами», «сведи в
 * сравнение». Задача словами + проверки приёмки → исполнитель →
 * трёхисходная приёмка (провал возвращается с замечаниями на доработку)
 * → результат ждёт финального решения ЧЕЛОВЕКА здесь. Машина «принято»
 * не ставит никогда — только отбраковывает и дорабатывает.
 */
import { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import {
  RefreshCw, Check, X, Loader2, Plus, Trash2, ChevronDown, Send,
} from 'lucide-react'

interface Attempt {
  attempt: number
  verdict: string
  remarks?: string
  error?: string
}

interface OfficeRun {
  id: string
  task_text: string
  backend: string
  status: string
  final_text: string
  final_verdict: { verdict?: string; note?: string; checks?: any[] }
  attempts: Attempt[]
  created_at: string
  closed_at?: string
  close_note?: string
}

interface CheckRow {
  kind: 'contains' | 'min_len' | 'regex'
  value: string
}

// Тексты статусов/вердиктов — в словаре i18n (office_tasks.status_* /
// verdict_*); здесь только стили, чтобы неизвестный статус не ронял t().
const STATUS_CLS: Record<string, string> = {
  awaiting_human: 'bg-amber-500/15 text-amber-300',
  returned_exhausted: 'bg-orange-500/15 text-orange-300',
  failed: 'bg-red-500/15 text-red-300',
  closed: 'bg-green-500/15 text-green-300',
  cancelled: 'bg-brain-700/40 text-brain-400',
}

const KNOWN_VERDICTS = new Set(['pass', 'fail', 'inconclusive'])

function Md({ text }: { text: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none leading-relaxed break-words text-brain-200">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  )
}

export default function OfficeTasksPanel({ userId }: { userId?: string }) {
  const t = useTranslations('office_tasks')
  const [runs, setRuns] = useState<OfficeRun[]>([])
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [taskText, setTaskText] = useState('')
  const [checks, setChecks] = useState<CheckRow[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [closing, setClosing] = useState<string | null>(null)

  const statusLabel = (s: string) => (STATUS_CLS[s] ? t(`status_${s}`) : s)
  const verdictLabel = (v: string) => (KNOWN_VERDICTS.has(v) ? t(`verdict_${v}`) : v)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await authFetch('/api/v1/executor/office/runs?limit=50')
      if (r.ok) {
        const d = await r.json()
        setRuns(d.runs || [])
      }
    } catch { /* список просто не обновится */ }
    setLoading(false)
  }, [])

  useEffect(() => { load() }, [load])

  const buildAcceptance = () =>
    checks
      .filter((c) => c.value.trim())
      .map((c) =>
        c.kind === 'min_len'
          ? { kind: 'min_len', n: parseInt(c.value, 10) || 0 }
          : c.kind === 'regex'
            ? { kind: 'regex', pattern: c.value }
            : { kind: 'contains', target: c.value },
      )

  const run = async () => {
    if (taskText.trim().length < 20) {
      setError(t('err_too_short'))
      return
    }
    setError('')
    setRunning(true)
    try {
      const r = await authFetch('/api/v1/executor/office', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_text: taskText, acceptance: buildAcceptance() }),
      })
      const d = await r.json().catch(() => ({}))
      if (!r.ok) {
        setError(d?.detail || t('err_unavailable'))
      } else {
        setTaskText('')
        setChecks([])
        if (d?.run?.id) setExpanded(d.run.id)
        await load()
      }
    } catch {
      setError(t('err_no_response'))
    }
    setRunning(false)
  }

  const close = async (id: string, approve: boolean) => {
    setClosing(id)
    try {
      const r = await authFetch(`/api/v1/executor/office/runs/${id}/close`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve }),
      })
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        setError(d?.detail || t('err_close'))
      }
      await load()
    } catch { /* список покажет фактическое состояние */ }
    setClosing(null)
  }

  return (
    <div className="p-4 space-y-4 max-w-4xl mx-auto">
      {/* ── Новая задача ─────────────────────────────────────────────── */}
      <div className="rounded-xl border border-brain-700/40 bg-brain-900/60 p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-brain-100">
            {t('title')}
          </h3>
          <span className="text-[11px] text-brain-500">
            {t('subtitle')}
          </span>
        </div>
        <textarea
          value={taskText}
          onChange={(e) => setTaskText(e.target.value)}
          placeholder={t('task_placeholder')}
          rows={3}
          className="w-full rounded-lg bg-brain-950/70 border border-brain-700/40 px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-brain-500"
        />

        {/* Проверки приёмки */}
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-xs text-brain-400">{t('checks_heading')}</span>
            <span className="text-[11px] text-brain-600">
              {t('checks_hint')}
            </span>
            <button
              onClick={() => setChecks((c) => [...c, { kind: 'contains', value: '' }])}
              className="ml-auto flex items-center gap-1 text-xs text-brain-400 hover:text-brain-200"
            >
              <Plus className="w-3.5 h-3.5" /> {t('add_check')}
            </button>
          </div>
          {checks.map((c, i) => (
            <div key={i} className="flex items-center gap-2">
              <select
                value={c.kind}
                onChange={(e) => setChecks((arr) => arr.map((x, j) => (j === i ? { ...x, kind: e.target.value as CheckRow['kind'] } : x)))}
                className="rounded-md bg-brain-950/70 border border-brain-700/40 px-2 py-1 text-xs text-brain-200"
              >
                <option value="contains">{t('check_contains')}</option>
                <option value="min_len">{t('check_min_len')}</option>
                <option value="regex">{t('check_regex')}</option>
              </select>
              <input
                value={c.value}
                onChange={(e) => setChecks((arr) => arr.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                placeholder={c.kind === 'min_len' ? '400' : c.kind === 'regex' ? '\\d{2,}' : t('check_placeholder_contains')}
                className="flex-1 rounded-md bg-brain-950/70 border border-brain-700/40 px-2 py-1 text-xs text-brain-100"
              />
              <button onClick={() => setChecks((arr) => arr.filter((_, j) => j !== i))}
                      className="text-brain-500 hover:text-red-400">
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>

        {error && <div className="text-xs text-red-400">{error}</div>}

        <div className="flex items-center justify-between">
          <span className="text-[11px] text-brain-600">
            {t('footer_hint')}
          </span>
          <button
            onClick={run}
            disabled={running}
            className="flex items-center gap-1.5 rounded-lg bg-brain-600 hover:bg-brain-500 disabled:opacity-50 px-4 py-1.5 text-sm text-white"
          >
            {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            {running ? t('running') : t('run')}
          </button>
        </div>
      </div>

      {/* ── Прогоны ──────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-brain-100">{t('runs_heading')}</h3>
        <button onClick={load} className="text-brain-400 hover:text-brain-200">
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {runs.length === 0 && !loading && (
        <div className="text-xs text-brain-500 py-6 text-center">
          {t('empty')}
        </div>
      )}

      {runs.map((r) => {
        const stCls = STATUS_CLS[r.status] || 'bg-brain-700/40 text-brain-300'
        const open = expanded === r.id
        const needsDecision = r.status === 'awaiting_human' || r.status === 'returned_exhausted'
        return (
          <div key={r.id} className="rounded-xl border border-brain-700/40 bg-brain-900/50">
            <button
              onClick={() => setExpanded(open ? null : r.id)}
              className="w-full flex items-center gap-3 px-4 py-3 text-left"
            >
              <ChevronDown className={`w-4 h-4 text-brain-500 transition-transform ${open ? '' : '-rotate-90'}`} />
              <span className="flex-1 text-sm text-brain-100 truncate">{r.task_text}</span>
              <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] ${stCls}`}>{statusLabel(r.status)}</span>
            </button>

            {open && (
              <div className="px-4 pb-4 space-y-3 border-t border-brain-700/30 pt-3">
                {/* попытки */}
                <div className="space-y-1">
                  {r.attempts.map((a) => (
                    <div key={a.attempt} className="text-xs text-brain-400">
                      {t('attempt')} {a.attempt}: {a.error
                        ? <span className="text-red-400">{a.error}</span>
                        : <span>{verdictLabel(a.verdict)}</span>}
                      {a.remarks ? (
                        <div className="mt-0.5 pl-3 text-brain-500 whitespace-pre-wrap">{a.remarks}</div>
                      ) : null}
                    </div>
                  ))}
                </div>

                {/* результат */}
                {r.final_text && (
                  <div className="rounded-lg bg-brain-950/60 border border-brain-700/30 p-3 max-h-80 overflow-y-auto">
                    <Md text={r.final_text} />
                  </div>
                )}
                {r.final_verdict?.note && (
                  <div className="text-[11px] text-brain-500">{r.final_verdict.note}</div>
                )}

                {/* финал человека */}
                {needsDecision && (
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => close(r.id, true)}
                      disabled={closing === r.id}
                      className="flex items-center gap-1 rounded-lg bg-green-600/80 hover:bg-green-600 disabled:opacity-50 px-3 py-1.5 text-xs text-white"
                    >
                      <Check className="w-3.5 h-3.5" /> {t('accept')}
                    </button>
                    <button
                      onClick={() => close(r.id, false)}
                      disabled={closing === r.id}
                      className="flex items-center gap-1 rounded-lg bg-brain-700 hover:bg-brain-600 disabled:opacity-50 px-3 py-1.5 text-xs text-brain-200"
                    >
                      <X className="w-3.5 h-3.5" /> {t('reject')}
                    </button>
                    <span className="text-[11px] text-brain-600">
                      {t('final_hint')}
                    </span>
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
