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

const STATUS_LABELS: Record<string, { label: string; cls: string }> = {
  awaiting_human: { label: 'ждёт вашего решения', cls: 'bg-amber-500/15 text-amber-300' },
  returned_exhausted: { label: 'доработки исчерпаны — решайте', cls: 'bg-orange-500/15 text-orange-300' },
  failed: { label: 'исполнитель не справился', cls: 'bg-red-500/15 text-red-300' },
  closed: { label: 'принято вами', cls: 'bg-green-500/15 text-green-300' },
  cancelled: { label: 'отклонено вами', cls: 'bg-brain-700/40 text-brain-400' },
}

const VERDICT_LABELS: Record<string, string> = {
  pass: '✓ проверки прошли',
  fail: '✗ проверки провалены',
  inconclusive: '? проверить нечем — читайте сами',
}

function Md({ text }: { text: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none leading-relaxed break-words text-brain-200">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  )
}

export default function OfficeTasksPanel({ userId }: { userId?: string }) {
  const [runs, setRuns] = useState<OfficeRun[]>([])
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [taskText, setTaskText] = useState('')
  const [checks, setChecks] = useState<CheckRow[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [closing, setClosing] = useState<string | null>(null)

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
      setError('Опишите задачу подробнее — минимум 20 символов.')
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
        setError(d?.detail || 'Исполнитель недоступен. Проверьте, что openworker-server поднят и адрес задан в настройках.')
      } else {
        setTaskText('')
        setChecks([])
        if (d?.run?.id) setExpanded(d.run.id)
        await load()
      }
    } catch {
      setError('Сбой запроса — исполнитель не ответил.')
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
        setError(d?.detail || 'Не удалось закрыть прогон.')
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
            Офисная задача исполнителю
          </h3>
          <span className="text-[11px] text-brain-500">
            бриф · письмо · сводка · исследование — не код
          </span>
        </div>
        <textarea
          value={taskText}
          onChange={(e) => setTaskText(e.target.value)}
          placeholder="Например: собери бриф по клиенту N перед завтрашним звонком — история отношений, суммы, открытые вопросы, риски."
          rows={3}
          className="w-full rounded-lg bg-brain-950/70 border border-brain-700/40 px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-brain-500"
        />

        {/* Проверки приёмки */}
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-xs text-brain-400">Проверки приёмки</span>
            <span className="text-[11px] text-brain-600">
              без них результат честно помечается «не доказано»
            </span>
            <button
              onClick={() => setChecks((c) => [...c, { kind: 'contains', value: '' }])}
              className="ml-auto flex items-center gap-1 text-xs text-brain-400 hover:text-brain-200"
            >
              <Plus className="w-3.5 h-3.5" /> добавить
            </button>
          </div>
          {checks.map((c, i) => (
            <div key={i} className="flex items-center gap-2">
              <select
                value={c.kind}
                onChange={(e) => setChecks((arr) => arr.map((x, j) => (j === i ? { ...x, kind: e.target.value as CheckRow['kind'] } : x)))}
                className="rounded-md bg-brain-950/70 border border-brain-700/40 px-2 py-1 text-xs text-brain-200"
              >
                <option value="contains">содержит слово</option>
                <option value="min_len">минимум символов</option>
                <option value="regex">по шаблону (regex)</option>
              </select>
              <input
                value={c.value}
                onChange={(e) => setChecks((arr) => arr.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                placeholder={c.kind === 'min_len' ? '400' : c.kind === 'regex' ? '\\d{2,}' : 'риски'}
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
            Провал приёмки вернётся исполнителю с замечаниями (до 2 доработок).
            Финальное «принято» — только за вами.
          </span>
          <button
            onClick={run}
            disabled={running}
            className="flex items-center gap-1.5 rounded-lg bg-brain-600 hover:bg-brain-500 disabled:opacity-50 px-4 py-1.5 text-sm text-white"
          >
            {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            {running ? 'Исполнитель работает…' : 'Запустить'}
          </button>
        </div>
      </div>

      {/* ── Прогоны ──────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-brain-100">Прогоны</h3>
        <button onClick={load} className="text-brain-400 hover:text-brain-200">
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {runs.length === 0 && !loading && (
        <div className="text-xs text-brain-500 py-6 text-center">
          Прогонов пока нет. Первая задача появится здесь со всей историей
          попыток и вердиктами приёмки.
        </div>
      )}

      {runs.map((r) => {
        const st = STATUS_LABELS[r.status] || { label: r.status, cls: 'bg-brain-700/40 text-brain-300' }
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
              <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] ${st.cls}`}>{st.label}</span>
            </button>

            {open && (
              <div className="px-4 pb-4 space-y-3 border-t border-brain-700/30 pt-3">
                {/* попытки */}
                <div className="space-y-1">
                  {r.attempts.map((a) => (
                    <div key={a.attempt} className="text-xs text-brain-400">
                      Попытка {a.attempt}: {a.error
                        ? <span className="text-red-400">{a.error}</span>
                        : <span>{VERDICT_LABELS[a.verdict] || a.verdict}</span>}
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
                      <Check className="w-3.5 h-3.5" /> Принять
                    </button>
                    <button
                      onClick={() => close(r.id, false)}
                      disabled={closing === r.id}
                      className="flex items-center gap-1 rounded-lg bg-brain-700 hover:bg-brain-600 disabled:opacity-50 px-3 py-1.5 text-xs text-brain-200"
                    >
                      <X className="w-3.5 h-3.5" /> Отклонить
                    </button>
                    <span className="text-[11px] text-brain-600">
                      машина уже отбраковала что могла — финал за вами
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
