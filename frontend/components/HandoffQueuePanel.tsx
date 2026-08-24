'use client'

/**
 * HandoffQueuePanel — очередь задач на исполнение (handoffs).
 *
 * Закрывает UI-пробел vibe-tasking: режимы «гейт» (CLI-исполнитель по
 * подписке) и «web» (Lovable/v0/Claude-web/…) кладут готовое ТЗ в очередь
 * подтверждения. Здесь пользователь:
 *  - CLI-гейт: «Подтвердить» (запуск) / «Отклонить»;
 *  - web: «Открыть в инструменте» + «Зафиксировать результат» (URL).
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { authFetch } from '@/lib/authFetch'
import { RefreshCw, ExternalLink, Check, X, FileText, Loader2, RotateCcw, ChevronDown, HelpCircle, Trash2, Download } from 'lucide-react'
import SetupGuide from './SetupGuide'

/** Отчёт агента/готовый документ — это markdown; сырой <pre> показывал «**» и
 *  решётки как текст (нечитабельно). Рендерим как в чате: жирный/заголовки/
 *  списки/таблицы. Один компонент на оба блока (результат и документ). */
function MdBlock({ text, tone = 'text-brain-300' }: { text: string; tone?: string }) {
  return (
    <div className={`px-3 pb-3 max-h-96 overflow-y-auto leading-relaxed break-words prose prose-invert prose-sm max-w-none ${tone}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (p) => <a {...p} target="_blank" rel="noopener noreferrer" className="text-brain-400 hover:underline" />,
          ul: (p) => <ul {...p} className="list-disc pl-4 space-y-0.5" />,
          ol: (p) => <ol {...p} className="list-decimal pl-4 space-y-0.5" />,
          h1: (p) => <h1 {...p} className="text-sm font-bold mt-3 mb-1.5 text-brain-100" />,
          h2: (p) => <h2 {...p} className="text-sm font-bold mt-3 mb-1.5 text-brain-100" />,
          h3: (p) => <h3 {...p} className="text-xs font-bold mt-2 mb-1 text-brain-100" />,
          code: (p) => <code {...p} className="px-1 py-0.5 rounded bg-brain-800/70 text-brain-200 text-[11px]" />,
          table: (p) => <div className="overflow-x-auto my-2"><table {...p} className="min-w-full divide-y divide-brain-700/50 text-xs" /></div>,
          thead: (p) => <thead {...p} className="bg-brain-800/50" />,
          th: (p) => <th {...p} className="px-2 py-1.5 text-left font-medium text-brain-300" />,
          tbody: (p) => <tbody {...p} className="divide-y divide-brain-700/30" />,
          td: (p) => <td {...p} className="px-2 py-1.5 text-brain-100" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}

interface Handoff {
  id: string
  status: string
  kind?: string          // "web" | undefined (CLI)
  agent?: string
  tool?: string
  task_title?: string
  // происхождение задачи (метка «откуда пришло»): meeting/manual/…
  source?: { kind?: string; meeting_id?: string; meeting_title?: string }
  launch_url?: string
  prefilled?: boolean
  note?: string
  command?: string
  created_at?: string
  result_url?: string
  // авто-проверка результата (LLM-судья против ТЗ) — замыкание vibe tasking
  verify_verdict?: string   // "done" | "needs_work"
  verify_summary?: string
  verify_missing?: string[]
  // контентное исполнение: сгенерированный ГОТОВЫЙ документ (КП/статья)
  result_document?: string
  // artifact_mode: агент собрал файлы-результаты в scratch-папке (без репо)
  artifact_mode?: boolean
  artifacts?: Array<{ name: string; rel?: string; size?: number }>
  // сырой хвост вывода исполнителя + код возврата — «что реально сделал агент»
  output_tail?: string
  rc?: number
}

interface Props { userId?: string }

const STATUS_CLS: Record<string, string> = {
  pending_confirmation: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  running: 'bg-blue-500/15 text-blue-300 border-blue-500/30',
  done: 'bg-green-500/15 text-green-300 border-green-500/30',
  failed: 'bg-red-500/15 text-red-300 border-red-500/30',
  rejected: 'bg-brain-600/30 text-brain-400 border-brain-600',
}

export default function HandoffQueuePanel({ userId }: Props) {
  const t = useTranslations('handoff_queue')
  const statusText: Record<string, string> = {
    pending_confirmation: t('status_pending_confirmation'),
    running: t('status_running'),
    done: t('status_done'),
    failed: t('status_failed'),
    rejected: t('status_rejected'),
  }
  const [items, setItems] = useState<Handoff[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<'pending' | 'all'>('pending')
  // id записи, для которой документ только что сгенерирован — её блок
  // «Готовый документ» раскрываем сразу (иначе результат легко не заметить)
  const [justGenerated, setJustGenerated] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)     // id действия в процессе
  const [msg, setMsg] = useState<Record<string, string>>({})
  const [resultUrl, setResultUrl] = useState<Record<string, string>>({})
  const [reworkNote, setReworkNote] = useState<Record<string, string>>({})
  // «Отправить результат»: цель (yougile/trello/jira/crm:*) и id задачи/
  // колонки/сущности CRM — per-карточка.
  const [deliverSel, setDeliverSel] = useState<Record<string, string>>({})
  const [deliverId, setDeliverId] = useState<Record<string, string>>({})
  const [deliverMode, setDeliverMode] = useState<Record<string, string>>({})
  const [repoPath, setRepoPath] = useState<Record<string, string>>({})
  const [specOpen, setSpecOpen] = useState<Record<string, string>>({})
  // L4: какой исполнитель запустит подтверждённую задачу.
  const [execInfo, setExecInfo] = useState<{ backends: string[]; active: string } | null>(null)
  const [showGuide, setShowGuide] = useState(false)   // модалка «Как подключить»
  // Панели «исполнитель/как работает» — по умолчанию свёрнуты (было слишком
  // много текста на странице); в свёрнутом виде видна лишь строка статуса.
  const [showSettings, setShowSettings] = useState(false)
  // Исполнитель Claude Code: статус CLI/ключа + ввод своего API-ключа. Раньше
  // авторизоваться было негде — панели не было, хотя бэкенд-эндпоинты есть.
  const [cliHealth, setCliHealth] = useState<any | null>(null)
  // Форма «Новая задача»: завести ТЗ в очередь прямо отсюда (раньше
  // задачи попадали только из чата/SIMA — прямого входа не было)
  const [showNew, setShowNew] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newSpec, setNewSpec] = useState('')
  const [newAgent, setNewAgent] = useState('claude')
  const [newRepo, setNewRepo] = useState('')
  // Рабочая папка по умолчанию — чтобы не вбивать E:\…\repo каждый раз.
  // Хранится в браузере (localStorage), подставляется в поля репозитория.
  const [defaultRepo, setDefaultRepo] = useState('')
  useEffect(() => {
    try {
      const v = localStorage.getItem('tessent_handoff_repo') || ''
      if (v) { setDefaultRepo(v); setNewRepo((p) => p || v) }
    } catch { /* ignore */ }
  }, [])
  const saveDefaultRepo = (v: string) => {
    setDefaultRepo(v)
    try {
      if (v.trim()) localStorage.setItem('tessent_handoff_repo', v.trim())
      else localStorage.removeItem('tessent_handoff_repo')
    } catch { /* ignore */ }
  }
  const [newArtifact, setNewArtifact] = useState(false)
  const [newBusy, setNewBusy] = useState(false)
  const [newMsg, setNewMsg] = useState<string | null>(null)
  // «Из встречи»: выбрать встречу → отметить задачи → пачкой в очередь
  // (сценарий «кофе»: встреча прошла → задачи выпали → ТЗ → исполнение)
  const [showMeeting, setShowMeeting] = useState(false)
  const [meetings, setMeetings] = useState<Array<{ id: string; title: string }>>([])
  const [meetingSearch, setMeetingSearch] = useState('')
  const [selMeeting, setSelMeeting] = useState<{ id: string; title: string } | null>(null)
  const [mTasks, setMTasks] = useState<Array<{ id: string; title: string; description: string; status: string; assignee: string }>>([])
  const [mTasksNote, setMTasksNote] = useState('')
  const [mChecked, setMChecked] = useState<string[]>([])
  const [mBusy, setMBusy] = useState(false)
  const [mExtracting, setMExtracting] = useState(false)
  const [mMsg, setMMsg] = useState<string | null>(null)

  const uidQuery = userId ? `user_id=${encodeURIComponent(userId)}` : ''
  const [deliverManual, setDeliverManual] = useState<Record<string, boolean>>({})
  // Справочники задачника (колонки/задачи) — выбор списком вместо ID.
  const [trackerRefs, setTrackerRefs] = useState<Record<string, { columns: any[]; tasks: any[] }>>({})
  const loadRefs = useCallback(async (system: string) => {
    if (trackerRefs[system]) return
    try {
      const r = await authFetch(`/api/v1/task-analysis/tracker-refs?system=${system}${uidQuery ? `&${uidQuery}` : ''}`)
      const d = await r.json()
      setTrackerRefs((s) => ({ ...s, [system]: { columns: d.columns || [], tasks: d.tasks || [] } }))
    } catch { /* оставим ручной ввод */ }
  }, [trackerRefs, uidQuery])
  // Маршрут доставки по умолчанию (авто-отправка результатов без трекера).
  const [route, setRoute] = useState<any>(null)
  const [routeMsg, setRouteMsg] = useState('')
  useEffect(() => {
    (async () => {
      try {
        const r = await authFetch(`/api/v1/task-analysis/delivery-route${uidQuery ? `?${uidQuery}` : ''}`)
        const d = await r.json()
        setRoute(d.target || null)
      } catch { /* нет маршрута */ }
    })()
  }, [uidQuery])
  const saveRoute = async (target: any) => {
    try {
      const r = await authFetch('/api/v1/task-analysis/delivery-route', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...(userId ? { user_id: userId } : {}), target }),
      })
      const d = await r.json()
      if (d.status === 'success') { setRoute(d.target || null); setRouteMsg('✅') }
      else setRouteMsg('⚠️ ' + (d.message || ''))
    } catch (e: any) { setRouteMsg('⚠️ ' + (e?.message || '')) }
    setTimeout(() => setRouteMsg(''), 2500)
  }

  const submitNewTask = async () => {
    if (newBusy || newTitle.trim().length < 3) return
    setNewBusy(true); setNewMsg(null)
    try {
      const r = await authFetch('/api/v1/task-analysis/handoff', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: userId || undefined,
          task: { title: newTitle.trim(), description: newSpec.trim() },
          spec_text: newSpec.trim() || undefined,
          agent: newAgent,
          repo_path: newArtifact ? undefined : (newRepo.trim() || undefined),
          artifact_mode: newArtifact,
          source: { kind: 'manual' },
        }),
      })
      const d = await r.json()
      if (!r.ok || d?.status === 'error') throw new Error(d?.message || d?.detail || `HTTP ${r.status}`)
      setNewMsg(t('new_task_queued'))
      setNewTitle(''); setNewSpec(''); setShowNew(false)
      load()
    } catch (e) {
      setNewMsg('❌ ' + (e as Error).message)
    } finally {
      setNewBusy(false)
    }
  }

  const openMeetingPicker = async () => {
    setShowMeeting((v) => !v)
    if (meetings.length) return
    try {
      const r = await authFetch(`/api/v1/meetflow/meetings?limit=200${userId ? `&user_id=${userId}` : ''}`)
      const d = await r.json()
      const list = (d?.meetings || []).map((m: any) => ({ id: String(m.id), title: m.title || t('untitled') }))
      setMeetings(list)
      // Ошибка/пусто раньше глотались молча — пользователь видел пустой
      // список и думал «не грузит контекст из встреч». Показываем причину.
      if (d?.status === 'error') setMTasksNote(t('meetings_load_failed', { message: d?.message || t('error_generic') }))
      else if (!list.length) setMTasksNote(t('no_meetings_found'))
      else setMTasksNote('')
    } catch (e) {
      setMTasksNote(t('meetings_load_failed', { message: (e as Error).message }))
    }
  }

  const pickMeeting = async (m: { id: string; title: string }) => {
    setSelMeeting(m); setMTasks([]); setMChecked([]); setMMsg(null); setMTasksNote('')
    try {
      const r = await authFetch(`/api/v1/task-analysis/meeting-tasks?meeting_id=${encodeURIComponent(m.id)}&${uidQuery}`)
      const d = await r.json()
      const tasks = d?.tasks || []
      setMTasks(tasks)
      setMTasksNote(d?.note || '')
      // по умолчанию отмечены НЕзакрытые — их и отправляют в работу
      setMChecked(tasks.filter((t: any) => !['done', 'completed', 'выполнено'].includes((t.status || '').toLowerCase())).map((t: any) => t.id))
    } catch (e) {
      setMTasksNote((e as Error).message)
    }
  }

  // Встреча не обработана синхронизацией → извлечь задачи из транскрипта
  // по запросу (один LLM-вызов), затем перечитать список.
  const extractMeetingTasks = async () => {
    if (!selMeeting || mExtracting) return
    setMExtracting(true); setMTasksNote(t('extracting_tasks_note'))
    try {
      const r = await authFetch(`/api/v1/task-analysis/meeting-tasks/extract?${uidQuery}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ meeting_id: selMeeting.id }),
      })
      const d = await r.json()
      const tasks = d?.tasks || []
      setMTasks(tasks)
      setMTasksNote(d?.note || (tasks.length ? '' : t('no_tasks_extracted')))
      setMChecked(tasks.map((t: any) => t.id))
    } catch (e) {
      setMTasksNote(t('extract_failed', { message: (e as Error).message }))
    } finally {
      setMExtracting(false)
    }
  }

  const sendMeetingTasks = async () => {
    if (mBusy || !mChecked.length) return
    setMBusy(true); setMMsg(null)
    let ok = 0, fail = 0
    for (const t of mTasks.filter((x) => mChecked.includes(x.id))) {
      try {
        const r = await authFetch('/api/v1/task-analysis/handoff', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            user_id: userId || undefined,
            task: { title: t.title, description: t.description || `Из встречи «${selMeeting?.title}». Исполнитель: ${t.assignee || '—'}` },
            agent: newAgent,
            repo_path: newRepo.trim() || undefined,
            source: { kind: 'meeting', meeting_id: selMeeting?.id, meeting_title: selMeeting?.title },
          }),
        })
        const d = await r.json()
        if (!r.ok || d?.status === 'error') fail++; else ok++
      } catch { fail++ }
    }
    setMMsg(fail
      ? t('meeting_tasks_queued_with_fail', { ok, fail })
      : t('meeting_tasks_queued', { ok }))
    setMBusy(false)
    load()
  }

  useEffect(() => {
    authFetch('/api/v1/executor/backends')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setExecInfo({ backends: d.backends || [], active: d.active || 'noop' }))
      .catch(() => {})
  }, [])

  const loadCliHealth = useCallback(async () => {
    try {
      const r = await authFetch(`/api/v1/task-analysis/cli-health?${uidQuery}`)
      if (r.ok) setCliHealth(await r.json())
    } catch { /* ignore */ }
  }, [uidQuery])
  useEffect(() => { loadCliHealth() }, [loadCliHealth])

  const load = useCallback(async () => {
    // без токена не стреляем: task-analysis строгий (нет Bearer → 403
    // «authentication required») — до логина это только спамило лог бэка
    try {
      const { getAccessToken } = await import('@/lib/authFetch')
      if (!getAccessToken()) { setItems([]); return }
    } catch {}
    setLoading(true)
    try {
      const q = filter === 'pending' ? 'status=pending_confirmation' : ''
      const url = `/api/v1/task-analysis/handoffs?${[uidQuery, q].filter(Boolean).join('&')}`
      const r = await authFetch(url)
      if (r.ok) {
        const d = await r.json()
        setItems(d.handoffs || [])
      }
    } catch { /* ignore */ } finally { setLoading(false) }
  }, [filter, uidQuery])

  useEffect(() => { load() }, [load])

  // Асинхронное исполнение: пока есть карточки в статусе «выполняется», тихо
  // перечитываем очередь раз в 4с — чтобы результат/дифф появился сам, без
  // ручного «Обновить». Интервал живёт только пока реально что-то бежит.
  const hasRunning = items.some((i) => i.status === 'running')
  useEffect(() => {
    if (!hasRunning) return
    const h = setInterval(() => { load() }, 4000)
    return () => clearInterval(h)
  }, [hasRunning, load])

  const act = async (id: string, path: string, body: Record<string, any>) => {
    setBusy(id)
    setMsg((m) => ({ ...m, [id]: '' }))
    try {
      const r = await authFetch(`/api/v1/task-analysis/handoff/${id}/${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...(userId ? { user_id: userId } : {}), ...body }),
      })
      const d = await r.json().catch(() => ({}))
      const ok = d.status === 'success' || d.success === true
      // Подтверждение теперь АСИНХРОННО: бэкенд сразу отдаёт status:running и
      // гонит исполнитель в фоне (иначе запрос висел бы до 30 мин). Это не
      // ошибка — карточка остаётся со статусом «выполняется», список сам
      // обновится по завершении (поллинг ниже + WS-сигнал).
      const running = d.status === 'running'
      // «Сгенерировать документ»: результат живёт в записи со статусом DONE,
      // а дефолтный фильтр «Ждёт» её прячет — казалось, что задача просто
      // исчезла. Переключаем на «Все», чтобы блок «Готовый документ» был виден.
      if (ok && path === 'execute-content') {
        setFilter('all')
        setJustGenerated(id)
        setMsg((m) => ({ ...m, [id]: '✅ ' + t('document_ready_note') }))
      } else if (running) {
        // чтобы running-карточка не пропала из фильтра «Ждёт»
        setFilter('all')
        setMsg((m) => ({ ...m, [id]: '⏳ ' + (d.message || t('status_running')) }))
      } else {
        setMsg((m) => ({ ...m, [id]: (ok ? '✅ ' : '⚠️ ') + (d.message || (ok ? t('done_generic') : t('failed_generic'))) }))
      }
      await load()
    } catch (e: any) {
      setMsg((m) => ({ ...m, [id]: '⚠️ ' + (e?.message || t('network_error')) }))
    } finally { setBusy(null) }
  }

  // Очистить завершённые (done/failed/rejected) — чтобы очередь не захламлялась.
  // Идущие и ожидающие подтверждения не трогаются.
  const clearFinished = async () => {
    if (busy) return
    setBusy('__clear__')
    try {
      const r = await authFetch('/api/v1/task-analysis/handoffs/clear-finished', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(userId ? { user_id: userId } : {}),
      })
      await r.json().catch(() => ({}))
      await load()
    } catch { /* ignore */ } finally { setBusy(null) }
  }

  // Скачать документ в формате PDF/DOCX/XLSX/PPTX — рендерится на сервере тем
  // же модулем, что и Доска (реальные файлы). Auth — через authFetch (Bearer),
  // поэтому качаем как blob, а не прямой ссылкой.
  const safeName = (t0: string) => (t0 || 'документ').slice(0, 40).replace(/[\\/:*?"<>|]+/g, '_')
  const downloadRender = async (id: string, fmt: string, title: string) => {
    setBusy(`${id}:${fmt}`)
    setMsg((m) => ({ ...m, [id]: '' }))
    try {
      const r = await authFetch(`/api/v1/task-analysis/handoff/${id}/render/${fmt}${uidQuery ? `?${uidQuery}` : ''}`)
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        setMsg((m) => ({ ...m, [id]: '⚠️ ' + (d.detail || d.message || `HTTP ${r.status}`) }))
        return
      }
      const blob = await r.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = `${safeName(title)}.${fmt}`
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1500)
    } catch (e: any) {
      setMsg((m) => ({ ...m, [id]: '⚠️ ' + (e?.message || t('network_error')) }))
    } finally { setBusy(null) }
  }

  // Итоги встречи: агрегат по задачам исполнителей этой встречи (что сделано,
  // кем, зачем, какие файлы) — HTML-документом с сервера.
  const downloadMeetingSummary = async (meetingId: string, meetingTitle: string) => {
    if (!meetingId && !meetingTitle) return
    setBusy(`ms:${meetingId}`)
    try {
      const q = new URLSearchParams()
      if (meetingId) q.set('meeting_id', meetingId)
      else q.set('meeting_title', meetingTitle)
      q.set('fmt', 'html')
      if (uidQuery) q.set('user_id', uidQuery.split('=')[1] || '')
      const r = await authFetch(`/api/v1/task-analysis/meeting-summary?${q.toString()}`)
      if (!r.ok) return
      const blob = await r.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = `${safeName('Итоги ' + (meetingTitle || 'встречи'))}.html`
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1500)
    } finally { setBusy(null) }
  }

  // Скачать сгенерированный документ файлом (.md) — чтобы «взять и работать».
  const downloadText = (name: string, content: string) => {
    try {
      const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = name
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch { /* ignore */ }
  }

  const toggleSpec = async (id: string) => {
    if (specOpen[id] !== undefined) {
      setSpecOpen((s) => { const n = { ...s }; delete n[id]; return n })
      return
    }
    try {
      const r = await authFetch(`/api/v1/task-analysis/handoff/${id}?${uidQuery}`)
      if (r.ok) {
        const d = await r.json()
        setSpecOpen((s) => ({ ...s, [id]: d.handoff?.spec_text || t('spec_unavailable') }))
      }
    } catch { setSpecOpen((s) => ({ ...s, [id]: t('spec_load_failed') })) }
  }

  const pendingCount = items.filter((i) => i.status === 'pending_confirmation').length

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-lg font-semibold text-white">{t('queue_title')}</h2>
          <p className="text-xs text-brain-500">{t('queue_subtitle')}</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => setShowGuide(true)}
            title={t('setup_guide_title')}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-brain-700 text-brain-300 hover:bg-brain-800 text-sm font-medium">
            <HelpCircle className="w-4 h-4" />
            {t('setup_guide_button')}
          </button>
          <button onClick={() => setShowNew((v) => !v)}
            className="px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium">
            {t('new_task_button')}
          </button>
          <button onClick={openMeetingPicker}
            title={t('from_meeting_title')}
            className="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium">
            {t('from_meeting_button')}
          </button>
          <div className="flex rounded-lg border border-brain-700 overflow-hidden">
            {(['pending', 'all'] as const).map((f) => (
              <button key={f} onClick={() => setFilter(f)}
                className={`px-3 py-1.5 text-sm transition-colors ${filter === f ? 'bg-brain-700 text-white' : 'text-brain-400 hover:bg-brain-800'}`}>
                {f === 'pending' ? t('pending_filter', { count: pendingCount }) : t('all_filter')}
              </button>
            ))}
          </div>
          <button onClick={load} disabled={loading}
            className="p-2 rounded-lg border border-brain-700 text-brain-300 hover:bg-brain-800 disabled:opacity-50">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          {/* Очистить завершённые — чтобы очередь не захламлялась */}
          {items.some((i) => ['done', 'failed', 'rejected'].includes(i.status)) && (
            <button onClick={clearFinished} disabled={busy === '__clear__'}
              title={t('clear_finished_title')}
              className="p-2 rounded-lg border border-brain-700 text-brain-400 hover:text-red-300 hover:bg-brain-800 disabled:opacity-50">
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* Маршрут доставки по умолчанию: результаты задач БЕЗ привязки к
          трекеру уезжают туда автоматически после завершения. */}
      <details className="rounded-lg border border-brain-700/60 bg-brain-900/30 text-xs">
        <summary className="px-3 py-2 cursor-pointer text-brain-300 select-none">
          🧭 {t('route_summary')}{route
            ? <span className="ml-1 text-emerald-300">
                {route.kind === 'crm' ? `→ CRM ${route.provider}` : `→ ${route.system}${route.task_id ? ' #' + String(route.task_id).slice(0, 8) : route.column_id ? ' (новая задача)' : ''}`}
              </span>
            : <span className="ml-1 text-brain-500">{t('route_not_set')}</span>}
          {routeMsg && <span className="ml-2">{routeMsg}</span>}
        </summary>
        <div className="px-3 pb-2.5 space-y-2">
          <RouteEditor route={route} onSave={saveRoute} loadRefs={loadRefs}
            trackerRefs={trackerRefs} t={t} />
        </div>
      </details>

      {/* Форма прямого заведения задачи в очередь */}
      {showNew && (
        <div className="rounded-lg border border-blue-500/30 bg-blue-500/5 p-3 space-y-2">
          <input value={newTitle} onChange={(e) => setNewTitle(e.target.value)}
            placeholder={t('new_task_title_placeholder')}
            className="w-full px-3 py-2 rounded bg-brain-900/60 border border-brain-700 text-sm text-white" />
          <textarea value={newSpec} onChange={(e) => setNewSpec(e.target.value)} rows={4}
            placeholder={t('new_task_spec_placeholder')}
            className="w-full px-3 py-2 rounded bg-brain-900/60 border border-brain-700 text-sm text-white resize-y" />
          <div className="flex items-center gap-2 flex-wrap">
            <select value={newAgent} onChange={(e) => setNewAgent(e.target.value)}
              className="px-2 py-1.5 rounded bg-brain-900/60 border border-brain-700 text-xs text-white">
              <option value="claude">Claude Code</option>
              <option value="cursor">Cursor</option>
              <option value="codex">Codex</option>
              <option value="grok">Grok CLI</option>
              <option value="qwen">Qwen Code</option>
            </select>
            <input value={newRepo} onChange={(e) => setNewRepo(e.target.value)}
              placeholder={t('new_task_repo_placeholder')} disabled={newArtifact}
              className="flex-1 min-w-[220px] px-2 py-1.5 rounded bg-brain-900/60 border border-brain-700 text-xs text-white disabled:opacity-40" />
            <button onClick={submitNewTask} disabled={newBusy || newTitle.trim().length < 3}
              className="px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white text-sm font-medium">
              {newBusy ? '…' : t('queue_it_button')}
            </button>
          </div>
          <label className="flex items-center gap-2 text-[11px] text-brain-300 cursor-pointer">
            <input type="checkbox" checked={newArtifact} onChange={(e) => setNewArtifact(e.target.checked)}
              className="w-3.5 h-3.5 rounded bg-brain-700 border-brain-600 text-blue-500" />
            {t('artifact_mode_label')}
          </label>
          <p className="text-[11px] text-brain-500">{newArtifact ? t('artifact_mode_hint') : t('new_task_hint')}</p>
        </div>
      )}
      {newMsg && <div className="text-xs text-brain-300">{newMsg}</div>}

      {/* «Из встречи»: встреча → задачи (чекбоксы) → пачкой в очередь */}
      {showMeeting && (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3 space-y-2">
          {!selMeeting ? (
            <>
              <input value={meetingSearch} onChange={(e) => setMeetingSearch(e.target.value)}
                placeholder={t('meeting_search_placeholder')}
                className="w-full px-3 py-2 rounded bg-brain-900/60 border border-brain-700 text-sm text-white" />
              <div className="max-h-52 overflow-y-auto divide-y divide-brain-700/30 rounded border border-brain-700/40">
                {meetings.filter((m) => m.title.toLowerCase().includes(meetingSearch.toLowerCase())).slice(0, 50).map((m) => (
                  <button key={m.id} onClick={() => pickMeeting(m)}
                    className="w-full text-left px-3 py-2 hover:bg-brain-800/50 text-sm text-brain-200 truncate">
                    {m.title}
                  </button>
                ))}
                {meetings.length === 0 && (
                  <div className="p-3 text-xs text-brain-500 text-center">{t('meetings_empty')}</div>
                )}
              </div>
            </>
          ) : (
            <>
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm text-white truncate">📋 {selMeeting.title}</div>
                <button onClick={() => setSelMeeting(null)} className="text-xs text-brain-400 hover:text-brain-200">{t('pick_another_meeting')}</button>
              </div>
              {mTasks.length === 0 ? (
                <div className="space-y-2">
                  <div className="text-xs text-brain-500">{mTasksNote || t('tasks_not_found')}</div>
                  <button
                    onClick={extractMeetingTasks}
                    disabled={mExtracting}
                    className="px-3 py-1.5 rounded bg-brain-700 hover:bg-brain-600 disabled:opacity-40 text-white text-xs font-medium"
                    title={t('extract_tasks_title')}
                  >
                    {mExtracting ? t('extracting_button') : t('extract_tasks_button')}
                  </button>
                </div>
              ) : (
                <div className="space-y-1">
                  {mTasks.map((t) => (
                    <label key={t.id} className="flex items-start gap-2 text-xs text-brain-300 cursor-pointer">
                      <input type="checkbox" checked={mChecked.includes(t.id)}
                        onChange={() => setMChecked((c) => c.includes(t.id) ? c.filter((x) => x !== t.id) : [...c, t.id])}
                        className="mt-0.5 rounded border-brain-600 bg-brain-800" />
                      <span>
                        <span className={['done', 'completed', 'выполнено'].includes((t.status || '').toLowerCase()) ? 'line-through text-brain-500' : ''}>{t.title}</span>
                        {t.assignee && <span className="text-brain-500"> · {t.assignee}</span>}
                        {t.status && <span className="text-brain-600"> · {t.status}</span>}
                      </span>
                    </label>
                  ))}
                  <div className="flex items-center gap-2 pt-1 flex-wrap">
                    <select value={newAgent} onChange={(e) => setNewAgent(e.target.value)}
                      className="px-2 py-1.5 rounded bg-brain-900/60 border border-brain-700 text-xs text-white">
                      <option value="claude">Claude Code</option>
                      <option value="cursor">Cursor</option>
                      <option value="codex">Codex</option>
                      <option value="grok">Grok CLI</option>
                      <option value="qwen">Qwen Code</option>
                    </select>
                    <input value={newRepo} onChange={(e) => setNewRepo(e.target.value)}
                      placeholder={t('repo_placeholder_short')}
                      className="flex-1 min-w-[160px] px-2 py-1.5 rounded bg-brain-900/60 border border-brain-700 text-xs text-white" />
                    <button onClick={sendMeetingTasks} disabled={mBusy || !mChecked.length}
                      className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-sm font-medium">
                      {mBusy ? t('creating_specs_button') : t('create_specs_button', { count: mChecked.length })}
                    </button>
                  </div>
                  <p className="text-[11px] text-brain-500">{t('meeting_tasks_hint')}</p>
                </div>
              )}
              {mMsg && <div className="text-xs text-brain-300">{mMsg}</div>}
            </>
          )}
        </div>
      )}

      {/* Компактная строка статуса исполнителя + тумблер. Раньше две плотные
          панели («Исполнитель Claude Code» + «Как работает») висели раскрытыми
          всегда — из-за этого «слишком много текста». Теперь свёрнуты; видны
          только ключевые чипы, детали — по клику. */}
      {(cliHealth || execInfo) && (
        <button onClick={() => setShowSettings((v) => !v)}
          title={t('executor_settings_hint')}
          className="w-full flex items-center justify-between gap-2 rounded-lg border border-brain-700 bg-brain-900/50 px-3 py-2 text-xs text-brain-300 hover:bg-brain-800/50">
          <span className="flex items-center gap-2 flex-wrap min-w-0">
            <span className="text-brain-400">{t('executor_settings')}</span>
            {cliHealth && (
              <span className={`px-2 py-0.5 rounded border ${cliHealth.agents?.claude?.installed ? 'border-emerald-500/50 text-emerald-300' : 'border-amber-500/50 text-amber-300'}`}>
                CLI: {cliHealth.agents?.claude?.installed ? t('cli_installed') : t('cli_not_found')}
              </span>
            )}
            {cliHealth && (
              <span className={`px-2 py-0.5 rounded border ${cliHealth.my_key?.has_key ? 'border-emerald-500/50 text-emerald-300' : 'border-brain-700 text-brain-400'}`}>
                {t('key_label')} {cliHealth.my_key?.has_key ? t('key_linked') : t('key_none')}
              </span>
            )}
          </span>
          <ChevronDown className={`w-4 h-4 shrink-0 transition-transform ${showSettings ? 'rotate-180' : ''}`} />
        </button>
      )}

      {/* Исполнитель Claude Code: авторизация (статус CLI/ключа + ввод ключа).
          Раньше авторизоваться было негде — панели не было. */}
      {showSettings && cliHealth && (
        <div className="rounded-lg border border-brain-700 bg-brain-900/50 p-3 text-xs space-y-2">
          <div className="text-brain-300 font-medium">{t('cli_executor_title')}</div>
          <div className="flex flex-wrap gap-2">
            <span className={`px-2 py-0.5 rounded border ${cliHealth.agents?.claude?.installed ? 'border-emerald-500/50 text-emerald-300' : 'border-amber-500/50 text-amber-300'}`}>
              CLI: {cliHealth.agents?.claude?.installed ? t('cli_installed') : t('cli_not_found')}
            </span>
            <span className={`px-2 py-0.5 rounded border ${cliHealth.my_key?.has_key ? 'border-emerald-500/50 text-emerald-300' : 'border-brain-700 text-brain-400'}`}>
              {t('key_label')} {cliHealth.my_key?.has_key ? (cliHealth.my_key.masked || t('key_linked')) : t('key_none')}
            </span>
            <span className={`px-2 py-0.5 rounded border ${cliHealth.exec_enabled ? 'border-emerald-500/50 text-emerald-300' : 'border-amber-500/50 text-amber-300'}`}>
              {t('execution_label')} {cliHealth.exec_enabled ? t('execution_enabled') : t('execution_disabled')}
            </span>
          </div>
          {!cliHealth.my_key?.has_key && (
            <p className="text-[11px] text-amber-300/90">
              {t.rich('no_key_hint', { b: (chunks) => <b>{chunks}</b> })}
              {!cliHealth.agents?.claude?.installed && ' ' + t('no_key_cli_hint')}
            </p>
          )}
          <p className="text-[11px] text-brain-500">
            {t('repo_field_hint')}
          </p>
          {/* Рабочая папка по умолчанию — задаётся один раз, подставляется во
              все задачи (хранится в браузере). Чтобы не вбивать путь каждый раз. */}
          <div className="pt-1">
            <label className="block text-[11px] text-brain-400 mb-1">{t('default_repo_label')}</label>
            <input type="text" placeholder={t('default_repo_placeholder')}
              value={defaultRepo}
              onChange={(e) => saveDefaultRepo(e.target.value)}
              className="w-full px-3 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-xs" />
          </div>
        </div>
      )}

      {/* L4 — исполнитель, которому уходит подтверждённая задача */}
      {showSettings && execInfo && (
        <div className="rounded-lg border border-brain-700 bg-brain-900/50 p-3 text-xs space-y-1.5">
          <div className="text-brain-300">
            <span className="text-brain-400">{t('how_it_works_label')} </span>
            {t('how_it_works_text')}
          </div>
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-brain-400">{t('executor_label')}</span>
            {(() => {
              const label: Record<string, string> = {
                openhands: t('backend_openhands'),
                claude_code_cli: t('backend_claude_code_cli'),
                cursor_cli: t('backend_cursor_cli'),
                codex_cli: t('backend_codex_cli'),
                noop: t('backend_noop'),
              }
              return (execInfo.backends.length ? execInfo.backends : [execInfo.active]).map((b) => (
                <span key={b}
                  className={`px-2 py-0.5 rounded border ${b === execInfo.active
                    ? 'bg-purple-500/20 border-purple-500/50 text-purple-200'
                    : 'border-brain-700 text-brain-400'}`}>
                  {b === execInfo.active ? '● ' : ''}{label[b] || b}
                </span>
              ))
            })()}
          </div>
          {execInfo.active === 'noop' && (
            <div className="text-amber-300/90">
              {t('noop_active_warning')}
            </div>
          )}
        </div>
      )}

      {loading && items.length === 0 ? (
        <div className="text-sm text-brain-500 py-8 text-center">{t('loading')}</div>
      ) : items.length === 0 ? (
        <div className="text-sm text-brain-500 py-8 text-center">{t('queue_empty')}</div>
      ) : (
        <div className="space-y-3">
          {items.map((h) => {
            const st = {
              text: statusText[h.status] || h.status,
              cls: STATUS_CLS[h.status] || 'bg-brain-700/30 text-brain-300 border-brain-600',
            }
            const isWeb = h.kind === 'web'
            const pending = h.status === 'pending_confirmation'
            // Эффективная рабочая папка: что вписано в карточке, иначе — дефолт
            // (из настроек). Так путь не приходится вбивать в каждую задачу.
            const effRepo = (repoPath[h.id] ?? defaultRepo)
            return (
              <div key={h.id} className="rounded-xl border border-brain-700 bg-brain-800/40 p-3 space-y-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-brain-100 truncate">{h.task_title || t('task_fallback_title')}</div>
                    <div className="text-xs text-brain-500 mt-0.5">
                      {isWeb ? `🌐 ${h.tool || 'web'}` : t('cli_by_subscription', { agent: h.agent || 'claude' })}
                      {h.created_at ? ` · ${String(h.created_at).slice(0, 16).replace('T', ' ')}` : ''}
                    </div>
                    {/* Источник задачи — «откуда пришло»: из встречи (с названием)
                        или заведено вручную. Решает «непонятно, из какой встречи». */}
                    {h.source?.kind === 'meeting' && (
                      <div className="mt-1 inline-flex items-center gap-1.5 text-[11px] text-emerald-300/90 bg-emerald-500/10 border border-emerald-500/25 rounded px-1.5 py-0.5 max-w-full">
                        <span className="truncate">📋 {t('source_meeting', { title: h.source.meeting_title || '—' })}</span>
                        {/* Итоги встречи: какие задачи выполнены и зачем —
                            агрегат по всем handoff'ам этой встречи, HTML файлом */}
                        <button
                          onClick={() => downloadMeetingSummary(h.source?.meeting_id || '', h.source?.meeting_title || '')}
                          disabled={busy === `ms:${h.source?.meeting_id}`}
                          className="shrink-0 underline decoration-dotted hover:text-emerald-200"
                          title="Итоги встречи: задачи, статусы, файлы, зачем">
                          {busy === `ms:${h.source?.meeting_id}` ? '…' : 'итоги'}
                        </button>
                      </div>
                    )}
                    {h.source?.kind === 'manual' && (
                      <div className="mt-1 inline-flex items-center gap-1 text-[11px] text-brain-400 bg-brain-700/30 border border-brain-600/40 rounded px-1.5 py-0.5">
                        ✍️ {t('source_manual')}
                      </div>
                    )}
                  </div>
                  <div className="shrink-0 flex items-center gap-1.5">
                    <span className={`text-[11px] px-2 py-0.5 rounded-full border ${st.cls}`}>{st.text}</span>
                    {/* Убрать из очереди — доступно для не-идущих (не рвём прогон) */}
                    {h.status !== 'running' && (
                      <button disabled={busy === h.id}
                        onClick={() => act(h.id, 'delete', {})}
                        title={t('delete_from_queue_title')}
                        className="p-1 rounded text-brain-500 hover:text-red-300 hover:bg-brain-700/50 disabled:opacity-40">
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    )}
                  </div>
                </div>

                {/* Жизненный цикл одним взглядом: ТЗ → исполнение → проверка → доставка */}
                <div className="flex flex-wrap items-center gap-1 text-[10px] text-brain-500">
                  <span className="text-brain-300">📋 {t('lc_spec')}</span>
                  <span>→</span>
                  <span className={['done', 'failed', 'running'].includes(h.status) ? 'text-brain-300' : ''}>
                    🤖 {h.status === 'running' ? t('lc_running') : ['done', 'failed'].includes(h.status) ? t('lc_executed', { agent: h.agent || 'agent' }) : t('lc_waiting')}
                  </span>
                  <span>→</span>
                  <span className={h.verify_verdict ? (h.verify_verdict === 'done' ? 'text-emerald-300' : 'text-amber-300') : ''}>
                    🧪 {h.verify_verdict ? (h.verify_verdict === 'done' ? t('lc_verified') : t('lc_needs_work')) : '—'}
                  </span>
                  <span>→</span>
                  <span className={((h as any).deliveries?.length ?? 0) > 0 || ((h as any).tracker && (h as any).tracker_task_id) ? 'text-emerald-300' : ''}>
                    📤 {((h as any).deliveries?.length ?? 0) > 0
                      ? t('lc_delivered', { count: (h as any).deliveries.length })
                      : ((h as any).tracker && (h as any).tracker_task_id) ? `${(h as any).tracker}` : '—'}
                  </span>
                </div>

                {/* Вердикт авто-проверки: LLM-судья сверил результат с ТЗ */}
                {h.verify_verdict && (
                  <div className={`text-xs rounded-lg px-2.5 py-2 border ${
                    h.verify_verdict === 'done'
                      ? 'border-emerald-600/40 bg-emerald-500/10 text-emerald-200'
                      : 'border-amber-600/40 bg-amber-500/10 text-amber-200'
                  }`}>
                    <span className="font-medium">
                      {h.verify_verdict === 'done' ? t('verified_done') : t('verified_needs_work')}
                    </span>
                    {h.verify_summary && <div className="mt-1 text-brain-200">{h.verify_summary}</div>}
                    {(h.verify_missing?.length ?? 0) > 0 && (
                      <ul className="mt-1 list-disc list-inside text-amber-200/90">
                        {h.verify_missing!.slice(0, 5).map((m, i) => <li key={i}>{m}</li>)}
                      </ul>
                    )}
                  </div>
                )}

                {/* «Вернуть в доработку» (OpenOPC rework): новая попытка с
                    замечаниями ревью + комментарием заказчика */}
                {(h.status === 'done' || h.status === 'failed') && !(h as any).rework_child_id && (
                  <div className="flex gap-2">
                    <input type="text"
                      placeholder={t('rework_note_placeholder')}
                      value={reworkNote[h.id] || ''}
                      onChange={(e) => setReworkNote((r) => ({ ...r, [h.id]: e.target.value }))}
                      className="flex-1 px-3 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-sm" />
                    <button disabled={busy === h.id}
                      onClick={() => act(h.id, 'rework', { note: reworkNote[h.id] || '' })}
                      title={t('rework_hint')}
                      className="px-3 py-1.5 rounded-lg bg-amber-600/80 hover:bg-amber-600 text-white text-sm disabled:opacity-40 inline-flex items-center gap-1">
                      {busy === h.id ? <Loader2 className="w-4 h-4 animate-spin" /> : <RotateCcw className="w-4 h-4" />}
                      {t('rework_button')}
                    </button>
                  </div>
                )}
                {(h as any).rework_child_id && (
                  <p className="text-[11px] text-brain-500">{t('rework_created_note')}</p>
                )}

                {/* Куда уже доставлен результат — чипы (задачник/CRM) */}
                {((h as any).deliveries?.length ?? 0) > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {((h as any).deliveries as any[]).map((d, i) => (
                      <span key={i}
                        className="text-[11px] px-2 py-0.5 rounded-full border border-emerald-600/40 bg-emerald-500/10 text-emerald-200 inline-flex items-center gap-1">
                        ✓ {d.kind === 'crm' ? `${d.provider}${d.op === 'note' ? ' · 💬' : ' · 🤝'}`
                          : `${d.system} #${String(d.task_id || '').slice(0, 10)}${d.created ? ' (новая)' : ''}`}
                      </span>
                    ))}
                  </div>
                )}

                {/* Отправить результат в задачник/CRM — по кнопке, после done.
                    (Задачи, ПРИШЕДШИЕ из трекера, доставляются туда автоматически;
                    этот блок — для задач из встречи/руками.) */}
                {h.status === 'done' && (
                  <details className="text-xs rounded-lg border border-brain-600/50 bg-brain-900/40"
                    onToggle={(e) => {
                      const sel = deliverSel[h.id] || 'yougile'
                      if ((e.target as HTMLDetailsElement).open && !sel.startsWith('crm:')) loadRefs(sel)
                    }}>
                    <summary className="px-2.5 py-2 cursor-pointer text-brain-200 font-medium select-none">
                      📤 {t('deliver_summary')}
                    </summary>
                    <div className="px-2.5 pb-2.5 space-y-2">
                      {(() => {
                        const sel = deliverSel[h.id] || 'yougile'
                        const isCrm = sel.startsWith('crm:')
                        const mode = deliverMode[h.id] || (isCrm ? 'create' : 'attach')
                        const idv = (deliverId[h.id] || '').trim()
                        const placeholder = isCrm
                          ? t('deliver_crm_id_placeholder')
                          : mode === 'attach'
                            ? t('deliver_task_id_placeholder')
                            : t('deliver_column_id_placeholder')
                        return (
                          <>
                            <div className="flex flex-wrap gap-2">
                              <select value={sel}
                                onChange={(e) => {
                                  const v = e.target.value
                                  setDeliverSel((s) => ({ ...s, [h.id]: v }))
                                  setDeliverId((s) => ({ ...s, [h.id]: '' }))
                                  if (!v.startsWith('crm:')) loadRefs(v)
                                }}
                                className="px-2 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-xs">
                                <option value="yougile">YouGile</option>
                                <option value="trello">Trello</option>
                                <option value="jira">Jira</option>
                                <option value="crm:amocrm">CRM · amoCRM</option>
                                <option value="crm:bitrix24">CRM · Bitrix24</option>
                                <option value="crm:hubspot">CRM · HubSpot</option>
                                <option value="crm:pipedrive">CRM · Pipedrive</option>
                              </select>
                              <select
                                value={mode}
                                onChange={(e) => setDeliverMode((s) => ({ ...s, [h.id]: e.target.value }))}
                                className="px-2 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-xs">
                                {isCrm ? (
                                  <>
                                    <option value="create">{t('deliver_mode_crm_create')}</option>
                                    <option value="note">{t('deliver_mode_crm_note')}</option>
                                  </>
                                ) : (
                                  <>
                                    <option value="attach">{t('deliver_mode_attach')}</option>
                                    <option value="create">{t('deliver_mode_create')}</option>
                                  </>
                                )}
                              </select>
                              {(isCrm ? mode === 'note' : true) && (() => {
                                // Люди не любят ID: колонки/задачи — выбором из
                                // списка; ручной ввод — только фолбэк.
                                const refs = trackerRefs[sel]
                                const opts = !isCrm && refs
                                  ? (mode === 'attach' ? refs.tasks : refs.columns)
                                  : []
                                const manual = deliverManual[h.id] || isCrm || opts.length === 0
                                if (!manual) {
                                  return (
                                    <select value={deliverId[h.id] || ''}
                                      onChange={(e) => {
                                        if (e.target.value === '__manual__') {
                                          setDeliverManual((s) => ({ ...s, [h.id]: true }))
                                          setDeliverId((s) => ({ ...s, [h.id]: '' }))
                                        } else setDeliverId((s) => ({ ...s, [h.id]: e.target.value }))
                                      }}
                                      className="flex-1 min-w-[150px] px-2 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-xs">
                                      <option value="">{mode === 'attach' ? t('deliver_pick_task') : t('deliver_pick_column')}</option>
                                      {opts.map((o: any) => (
                                        <option key={o.id} value={o.id}>
                                          {mode === 'attach' ? o.title : `${o.board ? o.board + ' / ' : ''}${o.name}`}
                                        </option>
                                      ))}
                                      <option value="__manual__">{t('deliver_manual_option')}</option>
                                    </select>
                                  )
                                }
                                return (
                                  <input type="text" placeholder={placeholder}
                                    value={deliverId[h.id] || ''}
                                    onChange={(e) => setDeliverId((s) => ({ ...s, [h.id]: e.target.value }))}
                                    className="flex-1 min-w-[150px] px-2 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-xs" />
                                )
                              })()}
                              <button disabled={busy === h.id}
                                onClick={() => {
                                  const target = isCrm
                                    ? { kind: 'crm', provider: sel.slice(4), op: mode,
                                        ...(mode === 'note' ? { entity_id: idv } : {}) }
                                    : mode === 'attach'
                                      ? { kind: 'tracker', system: sel, task_id: idv }
                                      : { kind: 'tracker', system: sel, column_id: idv }
                                  act(h.id, 'deliver', { target })
                                }}
                                className="px-3 py-1.5 rounded-lg bg-blue-600/80 hover:bg-blue-600 text-white text-xs disabled:opacity-40 inline-flex items-center gap-1">
                                {busy === h.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                                {t('deliver_button')}
                              </button>
                            </div>
                            <p className="text-[10px] text-brain-500">{t('deliver_hint')}</p>
                          </>
                        )
                      })()}
                    </div>
                  </details>
                )}

                {/* Готовый документ (контентное исполнение): результат, не ТЗ */}
                {h.result_document && (
                  <details open={justGenerated === h.id}
                    className="text-xs rounded-lg border border-purple-600/40 bg-purple-500/5">
                    <summary className="px-2.5 py-2 cursor-pointer text-purple-200 font-medium select-none">
                      {t('result_document_summary', { size: Math.round(h.result_document.length / 1000) })}
                    </summary>
                    <div className="px-3 pb-1 flex flex-wrap items-center gap-1.5">
                      <span className="text-[11px] text-brain-400 inline-flex items-center gap-1">
                        <Download className="w-3.5 h-3.5" /> {t('download_as')}
                      </span>
                      <button
                        onClick={() => downloadText(`${safeName(h.task_title || 'документ')}.md`, h.result_document || '')}
                        className="text-[11px] px-2 py-0.5 rounded border border-brain-600/60 text-brain-200 hover:bg-brain-700/50">
                        MD
                      </button>
                      {(['html', 'pdf', 'docx', 'xlsx', 'pptx'] as const).map((f) => (
                        <button key={f} disabled={busy === `${h.id}:${f}`}
                          onClick={() => downloadRender(h.id, f, h.task_title || 'документ')}
                          className="text-[11px] px-2 py-0.5 rounded border border-brain-600/60 text-brain-200 hover:bg-brain-700/50 disabled:opacity-40 inline-flex items-center gap-1">
                          {busy === `${h.id}:${f}` ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
                          {f.toUpperCase()}
                        </button>
                      ))}
                    </div>
                    <MdBlock text={h.result_document} tone="text-brain-200" />
                  </details>
                )}

                {/* Сырой вывод исполнителя — «что реально сделал агент». Раньше
                    после прогона в карточке было пусто и непонятно, случилось ли
                    что-нибудь. Теперь виден хвост вывода + код возврата. */}
                {h.output_tail && (h.status === 'done' || h.status === 'failed') && (
                  <details className="text-xs rounded-lg border border-brain-600/50 bg-brain-900/40">
                    <summary className="px-2.5 py-2 cursor-pointer text-brain-200 font-medium select-none inline-flex items-center gap-1.5">
                      <FileText className="w-3.5 h-3.5" />
                      {t('show_result')}{typeof h.rc === 'number' ? ` · rc ${h.rc}` : ''}
                    </summary>
                    <MdBlock text={h.output_tail} />
                  </details>
                )}

                {h.result_url && (
                  <a href={h.result_url} target="_blank" rel="noopener noreferrer" className="text-xs text-blue-400 hover:underline inline-flex items-center gap-1">
                    <ExternalLink className="w-3 h-3" /> {t('result_link')}
                  </a>
                )}

                {!!h.artifacts?.length && (
                  <div className="rounded-lg border border-emerald-600/40 bg-emerald-500/5 p-2 space-y-1">
                    <div className="text-[11px] text-emerald-200 font-medium">{t('artifacts_ready')}</div>
                    {h.artifacts.map((a, i) => (
                      <a key={i}
                        href={`/api/v1/task-analysis/handoff/${h.id}/artifact/${i}${uidQuery ? `?${uidQuery}` : ''}`}
                        target="_blank" rel="noopener noreferrer" download
                        className="text-xs text-emerald-300 hover:underline inline-flex items-center gap-1 mr-3">
                        <ExternalLink className="w-3 h-3" /> {a.name}
                        {a.size ? <span className="text-brain-500"> · {Math.round((a.size || 0) / 1024)} КБ</span> : null}
                      </a>
                    ))}
                  </div>
                )}

                {pending && isWeb && (
                  <div className="space-y-2">
                    {h.launch_url && (
                      <a href={h.launch_url} target="_blank" rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg bg-purple-600/80 hover:bg-purple-600 text-white">
                        <ExternalLink className="w-4 h-4" /> {t('open_in_tool', { tool: h.tool || t('tool_fallback') })}
                        {h.prefilled ? ` ${t('prompt_prefilled')}` : ''}
                      </a>
                    )}
                    <div className="flex gap-2">
                      <input type="url" placeholder={t('result_url_placeholder')}
                        value={resultUrl[h.id] || ''}
                        onChange={(e) => setResultUrl((r) => ({ ...r, [h.id]: e.target.value }))}
                        className="flex-1 px-3 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-sm" />
                      <button disabled={busy === h.id || !(resultUrl[h.id] || '').trim()}
                        onClick={() => act(h.id, 'web-result', { result_url: resultUrl[h.id] })}
                        className="px-3 py-1.5 rounded-lg bg-green-600/80 hover:bg-green-600 text-white text-sm disabled:opacity-40 inline-flex items-center gap-1">
                        {busy === h.id ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />} {t('record_result_button')}
                      </button>
                      <button disabled={busy === h.id}
                        onClick={() => act(h.id, 'reject', { reason: 'отклонено из очереди' })}
                        className="px-3 py-1.5 rounded-lg border border-brain-700 text-brain-300 hover:bg-brain-800 text-sm disabled:opacity-40 inline-flex items-center gap-1">
                        <X className="w-4 h-4" /> {t('reject_button')}
                      </button>
                    </div>
                  </div>
                )}

                {pending && !isWeb && (
                  <div className="space-y-2">
                    <input type="text" placeholder={defaultRepo || t('repo_path_placeholder')}
                      value={effRepo}
                      onChange={(e) => setRepoPath((r) => ({ ...r, [h.id]: e.target.value }))}
                      className="w-full px-3 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-sm" />
                    <div className="flex gap-2 flex-wrap">
                      {/* Контентное ТЗ (без репозитория): раньше висело в «Ждёт»
                          вечно. Два пути: сгенерировать ГОТОВЫЙ документ LLM
                          (КП с ценами, статья) или принять само ТЗ как результат.
                          Эти кнопки — ПЕРВЫЕ, когда репозиторий не указан:
                          «Подтвердить» без repo_path запускать нечего, и клик
                          по нему только выдавал объяснение-отказ. */}
                      {!effRepo.trim() && (
                        <>
                          <button disabled={busy === h.id}
                            onClick={() => act(h.id, 'execute-content', {})}
                            title={t('generate_document_title')}
                            className="px-3 py-1.5 rounded-lg bg-purple-600/80 hover:bg-purple-600 text-white text-sm disabled:opacity-40 inline-flex items-center gap-1">
                            {busy === h.id ? <Loader2 className="w-4 h-4 animate-spin" /> : '🪄'} {t('generate_document_button')}
                          </button>
                          <button disabled={busy === h.id}
                            onClick={() => act(h.id, 'accept', {})}
                            title={t('accept_result_title')}
                            className="px-3 py-1.5 rounded-lg bg-sky-600/70 hover:bg-sky-600 text-white text-sm disabled:opacity-40 inline-flex items-center gap-1">
                            <Check className="w-4 h-4" /> {t('accept_result_button')}
                          </button>
                        </>
                      )}
                      <button disabled={busy === h.id}
                        onClick={() => act(h.id, 'confirm', effRepo.trim() ? { repo_path: effRepo.trim() } : {})}
                        title={effRepo.trim() ? undefined : t('confirm_code_hint')}
                        className={`px-3 py-1.5 rounded-lg text-sm disabled:opacity-40 inline-flex items-center gap-1 ${
                          effRepo.trim()
                            ? 'bg-green-600/80 hover:bg-green-600 text-white'
                            : 'border border-brain-700 text-brain-300 hover:bg-brain-800'
                        }`}>
                        {busy === h.id ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />} {t('confirm_button')}
                      </button>
                      <button disabled={busy === h.id}
                        onClick={() => act(h.id, 'reject', { reason: 'отклонено из очереди' })}
                        className="px-3 py-1.5 rounded-lg border border-brain-700 text-brain-300 hover:bg-brain-800 text-sm disabled:opacity-40 inline-flex items-center gap-1">
                        <X className="w-4 h-4" /> {t('reject_button')}
                      </button>
                    </div>
                  </div>
                )}

                <div>
                  <button onClick={() => toggleSpec(h.id)}
                    className="text-xs text-brain-400 hover:text-brain-200 inline-flex items-center gap-1">
                    <FileText className="w-3 h-3" /> {specOpen[h.id] !== undefined ? t('hide_spec') : t('show_spec')}
                  </button>
                  {specOpen[h.id] !== undefined && (
                    <div className="mt-1 space-y-1">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => { try { navigator.clipboard?.writeText(specOpen[h.id] || '') } catch { /* ignore */ } }}
                          className="text-[11px] text-brain-400 hover:text-brain-200">
                          {t('copy_spec')}
                        </button>
                      </div>
                      <pre className="max-h-[60vh] overflow-auto text-xs text-brain-300 bg-brain-900 rounded-lg p-3 whitespace-pre-wrap break-words leading-relaxed">{specOpen[h.id]}</pre>
                    </div>
                  )}
                </div>

                {msg[h.id] && <div className="text-xs text-brain-300">{msg[h.id]}</div>}
                {!isWeb && pending && h.command && (
                  <p className="text-[11px] text-brain-600">{t('manual_run_hint')}</p>
                )}
              </div>
            )
          })}
        </div>
      )}

      {showGuide && <SetupGuide initialSection="claude" onClose={() => setShowGuide(false)} />}
    </div>
  )
}

// Редактор маршрута доставки по умолчанию: система → режим → колонка/задача
// СПИСКОМ (ID — только ручной фолбэк). Сохраняется на бэкенде (delivery-route).
function RouteEditor({ route, onSave, loadRefs, trackerRefs, t }: {
  route: any
  onSave: (target: any) => void
  loadRefs: (system: string) => void
  trackerRefs: Record<string, { columns: any[]; tasks: any[] }>
  t: (k: string, v?: any) => string
}) {
  const [sel, setSel] = useState<string>(
    route?.kind === 'crm' ? `crm:${route.provider}` : (route?.system || 'yougile'))
  const [mode, setMode] = useState<string>(
    route?.kind === 'crm' ? (route.op || 'create') : (route?.task_id ? 'attach' : 'create'))
  const [val, setVal] = useState<string>(route?.task_id || route?.column_id || route?.entity_id || '')
  const [manual, setManual] = useState(false)
  const isCrm = sel.startsWith('crm:')
  useEffect(() => { if (!isCrm) loadRefs(sel) }, [sel, isCrm, loadRefs])
  const refs = trackerRefs[sel]
  const opts = !isCrm && refs ? (mode === 'attach' ? refs.tasks : refs.columns) : []
  const inputCls = 'px-2 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-xs'
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <select value={sel} onChange={(e) => { setSel(e.target.value); setVal(''); setManual(false) }} className={inputCls}>
          <option value="yougile">YouGile</option>
          <option value="trello">Trello</option>
          <option value="jira">Jira</option>
          <option value="crm:amocrm">CRM · amoCRM</option>
          <option value="crm:bitrix24">CRM · Bitrix24</option>
          <option value="crm:hubspot">CRM · HubSpot</option>
          <option value="crm:pipedrive">CRM · Pipedrive</option>
        </select>
        <select value={mode} onChange={(e) => { setMode(e.target.value); setVal('') }} className={inputCls}>
          {isCrm ? (
            <>
              <option value="create">{t('deliver_mode_crm_create')}</option>
              <option value="note">{t('deliver_mode_crm_note')}</option>
            </>
          ) : (
            <>
              <option value="create">{t('deliver_mode_create')}</option>
              <option value="attach">{t('deliver_mode_attach')}</option>
            </>
          )}
        </select>
        {(isCrm ? mode === 'note' : true) && (
          !isCrm && opts.length > 0 && !manual ? (
            <select value={val}
              onChange={(e) => e.target.value === '__manual__' ? (setManual(true), setVal('')) : setVal(e.target.value)}
              className={`${inputCls} flex-1 min-w-[150px]`}>
              <option value="">{mode === 'attach' ? t('deliver_pick_task') : t('deliver_pick_column')}</option>
              {opts.map((o: any) => (
                <option key={o.id} value={o.id}>
                  {mode === 'attach' ? o.title : `${o.board ? o.board + ' / ' : ''}${o.name}`}
                </option>
              ))}
              <option value="__manual__">{t('deliver_manual_option')}</option>
            </select>
          ) : (
            <input type="text" value={val} onChange={(e) => setVal(e.target.value)}
              placeholder={isCrm ? t('deliver_crm_id_placeholder')
                : mode === 'attach' ? t('deliver_task_id_placeholder') : t('deliver_column_id_placeholder')}
              className={`${inputCls} flex-1 min-w-[150px]`} />
          )
        )}
        <button onClick={() => onSave(isCrm
          ? { kind: 'crm', provider: sel.slice(4), op: mode, ...(mode === 'note' ? { entity_id: val } : {}) }
          : mode === 'attach'
            ? { kind: 'tracker', system: sel, task_id: val }
            : { kind: 'tracker', system: sel, column_id: val })}
          className="px-3 py-1.5 rounded-lg bg-emerald-600/80 hover:bg-emerald-600 text-white text-xs">
          {t('route_save')}
        </button>
        {route && (
          <button onClick={() => onSave(null)}
            className="px-3 py-1.5 rounded-lg border border-brain-600 text-brain-300 hover:text-red-300 text-xs">
            {t('route_clear')}
          </button>
        )}
      </div>
      <p className="text-[10px] text-brain-500">{t('route_hint')}</p>
    </div>
  )
}
