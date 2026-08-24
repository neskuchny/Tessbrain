'use client'

import { useEffect, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import MethodologyEditor from './MethodologyEditor'

// Панель «Анализ документов»: загрузить 1+ документов → выбрать методику
// (или свободный разбор) → задать вопрос → исследовательский движок
// (extract→compute→analyze→assemble) вернёт отчёт с текстом, таблицами,
// числами и графиком по числовым метрикам.

interface Props { userId?: string | null }
interface UploadedDoc { id: string; title: string }
interface ChatMsg {
  role: 'user' | 'assistant'
  text?: string
  markdown?: string
  verdict?: string
  severity?: { counts?: Record<string, number>; overall?: string }
  chart?: Array<{ label: string; value: number }>
  status?: string
  runId?: string          // для скачивания Word и переоткрытия
  methodLabel?: string    // какая методика применялась (воспроизводимость)
}
interface RunHistoryItem {
  id: string; status: string; documents: number
  created_at?: string; playbook_id?: string
}

function authHeader(): Record<string, string> {
  if (typeof window === 'undefined') return {}
  const token = localStorage.getItem('tessent_access_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

interface Method { id: string; label: string; kind: 'freeform' | 'template' | 'playbook' }

// Достать массив числовых метрик {label,value} из отчёта (best-effort).
function extractChart(report: any): Array<{ label: string; value: number }> {
  const out: Array<{ label: string; value: number }> = []
  const scan = (arr: any[]) => {
    for (const it of arr || []) {
      if (!it || typeof it !== 'object') continue
      const label = it.label || it.name || it.key
      const raw = it.value ?? it.metric ?? it.computed ?? it.result
      const num = typeof raw === 'number' ? raw : parseFloat(raw)
      if (label && Number.isFinite(num)) out.push({ label: String(label).slice(0, 24), value: num })
    }
  }
  if (report?.metrics) scan(report.metrics)
  if (!out.length && report?.dimensions) scan(report.dimensions)
  if (!out.length && report?.sections) scan(report.sections)
  return out.slice(0, 12)
}

export default function DocResearchPanel({ userId }: Props) {
  const t = useTranslations('doc_research')
  const FREEFORM: Method = { id: 'freeform', label: t('freeform_method'), kind: 'freeform' }
  const [docs, setDocs] = useState<UploadedDoc[]>([])
  const [methods, setMethods] = useState<Method[]>([FREEFORM])
  const [methodology, setMethodology] = useState('freeform')
  const [request, setRequest] = useState('')
  const [modelTier, setModelTier] = useState<'standard' | 'premium'>('standard')
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [showEditor, setShowEditor] = useState(false)
  const [editorMode, setEditorMode] = useState<'manual' | 'import'>('manual')
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [error, setError] = useState<string | null>(null)
  const [history, setHistory] = useState<RunHistoryItem[]>([])
  const [showHistory, setShowHistory] = useState(false)
  const [showKb, setShowKb] = useState(false)
  const [kbDocs, setKbDocs] = useState<UploadedDoc[]>([])
  const fileRef = useRef<HTMLInputElement>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  // Методики: freeform + готовые шаблоны + сохранённые методологии клиента.
  useEffect(() => {
    if (!userId) return
    ;(async () => {
      const list: Method[] = [FREEFORM]
      try {
        const tplResp = await fetch('/api/v1/analysis/templates', { headers: authHeader() })
        const td = await tplResp.json()
        // БАГ был здесь: бэкенд отдаёт template_id, а не id → готовые методики
        // (Due Diligence, финразбор, оценка КП) никогда не появлялись в списке.
        for (const tpl of (td?.templates || td?.items || [])) {
          const tid = tpl?.template_id || tpl?.id
          if (tid) list.push({ id: String(tid), label: `📋 ${tpl.name || tid}`, kind: 'template' })
        }
      } catch { /* ignore */ }
      try {
        const p = await fetch('/api/v1/analysis/playbooks', { headers: authHeader() })
        const pd = await p.json()
        for (const pb of (pd?.playbooks || [])) {
          if (pb?.id) list.push({ id: pb.id, label: `⭐ ${pb.name || pb.id}`, kind: 'playbook' })
        }
      } catch { /* ignore */ }
      setMethods(list)
    })()
  }, [userId])

  // История прошлых разборов (раньше отчёты жили только в памяти и исчезали
  // при переключении вкладки — теперь подгружаются с бэка).
  const loadHistory = async () => {
    if (!userId) return
    try {
      const r = await fetch('/api/v1/analysis/runs?limit=50', { headers: authHeader() })
      const d = await r.json()
      setHistory(d?.runs || [])
    } catch { /* ignore */ }
  }
  useEffect(() => { loadHistory() }, [userId])

  // Открыть готовый разбор из истории в ленту.
  const openRun = async (runId: string) => {
    setShowHistory(false)
    setMessages((m) => [...m, { role: 'assistant', status: t('loading_report'), runId }])
    try {
      const rep = await fetch(`/api/v1/analysis/runs/${runId}/report?format=json`, { headers: authHeader() })
      if (!rep.ok) throw new Error(t('report_unavailable', { status: rep.status }))
      const repd = await rep.json()
      const report = repd?.report || repd
      setMessages((m) => {
        const c = [...m]
        c[c.length - 1] = {
          role: 'assistant', runId,
          markdown: report?.markdown || t('report_empty'),
          verdict: report?.verdict, severity: report?.severity_summary,
          chart: extractChart(report),
        }
        return c
      })
    } catch (e) {
      setMessages((m) => { const c = [...m]; c[c.length - 1] = { role: 'assistant', text: '❌ ' + (e as Error).message }; return c })
    }
  }

  const deleteMethodology = async () => {
    const cur = methods.find((m) => m.id === methodology)
    if (!cur || cur.kind !== 'playbook') return
    if (!confirm(t('confirm_delete_methodology', { name: cur.label.replace(/^⭐ /, '') }))) return
    try {
      const r = await fetch(`/api/v1/analysis/playbooks/${methodology}`, { method: 'DELETE', headers: authHeader() })
      if (r.ok || r.status === 204) {
        setMethods((m) => m.filter((x) => x.id !== methodology))
        setMethodology('freeform')
      } else {
        setError(t('delete_methodology_failed', { status: r.status }))
      }
    } catch (e) { setError((e as Error).message) }
  }

  const downloadWord = async (runId: string) => {
    // report?format=docx уже умеет бэкенд. Тянем blob с Authorization-заголовком
    // (навигация window.open заголовок не отправляет → был бы 401).
    try {
      const r = await fetch(`/api/v1/analysis/runs/${runId}/report?format=docx`, { headers: authHeader() })
      if (!r.ok) { setError(t('download_word_failed', { status: r.status })); return }
      const blob = await r.blob()
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = `analysis_${runId}.docx`
      document.body.appendChild(a); a.click()
      window.URL.revokeObjectURL(url); document.body.removeChild(a)
    } catch (e) { setError((e as Error).message) }
  }

  // Документы из базы знаний (уже проиндексированные) — чтобы не грузить
  // файл заново. run_engine грузит по id из той же таблицы documents.
  const loadKbDocs = async () => {
    if (!userId) return
    try {
      const r = await fetch(`/api/v1/documents/?user_id=${userId}&limit=100`, { headers: authHeader() })
      const d = await r.json()
      setKbDocs((d?.documents || []).map((x: any) => ({ id: String(x.id), title: x.title || x.file_name || x.id })))
    } catch { /* ignore */ }
  }
  const addKbDoc = (doc: UploadedDoc) => {
    setDocs((d) => d.some((x) => x.id === doc.id) ? d : [...d, doc])
  }

  const uploadFiles = async (files: FileList | null) => {
    if (!files || !files.length || !userId) return
    setUploading(true); setError(null)
    try {
      for (const file of Array.from(files)) {
        const content: string = await new Promise((res, rej) => {
          const r = new FileReader()
          r.onload = () => res(r.result as string)
          r.onerror = rej
          r.readAsDataURL(file)
        })
        const resp = await fetch(`/api/v1/documents/?user_id=${userId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeader() },
          body: JSON.stringify({
            title: file.name, content, doc_type: 'file',
            metadata: { user_id: userId, file_name: file.name, file_size: file.size },
          }),
        })
        const data = await resp.json()
        const id = data?.document?.id || data?.document_id
        if (data?.status === 'success' && id) {
          setDocs((d) => [...d, { id: String(id), title: file.name }])
        } else {
          setError(data?.message || t('upload_file_failed', { name: file.name }))
        }
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  // МОРМ-lite: вывести методологию компании из выбранных примеров.
  // Числовые правила считает код (гейт на отложенных против базлайна),
  // структуру извлекает LLM; результат — в Документы (папка «Методологии»).
  const induceMethodology = async () => {
    if (!userId || busy) return
    // «Из примеров» выводит методику из ОБЩЕГО в 2+ документах-примерах.
    // Раньше при <2 кнопка молча ничего не делала — теперь честно объясняем.
    if (docs.length < 2) { setError(t('induce_need_two')); return }
    const genre = window.prompt(t('genre_prompt'), t('genre_default'))?.trim()
    if (!genre) return
    setError(null); setBusy(true)
    setMessages((m) => [...m,
      { role: 'user', text: t('induce_request', { genre, count: docs.length }) },
      { role: 'assistant', status: t('induce_status') }])
    try {
      const r = await fetch('/api/v1/analysis/methodology/induce', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify({ document_ids: docs.map((d) => d.id), genre }),
      })
      const d = await r.json()
      if (!d?.success) throw new Error(d?.error || `HTTP ${r.status}`)
      const saved = d.document_id
        ? '\n\n> ' + t('induce_saved_note')
        : ''
      setMessages((m) => {
        const c = [...m]
        c[c.length - 1] = { role: 'assistant', markdown: (d.markdown || '') + saved,
                            methodLabel: t('induce_method_label', { status: d.status }) }
        return c
      })
    } catch (e) {
      setMessages((m) => { const c = [...m]; c[c.length - 1] = { role: 'assistant', text: '❌ ' + (e as Error).message }; return c })
    } finally {
      setBusy(false)
    }
  }

  // Пере-гейт по дрейфу: правила методологии перепроверяются на свежих
  // примерах; дрейфанувшие помечаются 🔴, trust и версия обновляются.
  const regateMethodology = async () => {
    if (!userId || docs.length < 1 || busy) return
    setError(null)
    let mid = ''
    let mtitle = ''
    try {
      const r = await fetch(`/api/v1/generated-documents?type=methodology&user_id=${userId}`, { headers: authHeader() })
      const d = await r.json()
      const list = (d?.documents || []) as Array<{ document_id: string; title: string }>
      if (!list.length) { setError(t('no_methodologies_yet')); return }
      if (list.length === 1) { mid = list[0].document_id; mtitle = list[0].title }
      else {
        const pick = window.prompt(
          t('regate_pick_prompt') + '\n' +
          list.map((m, i) => `${i + 1}. ${m.title}`).join('\n'), '1')
        const idx = parseInt(pick || '', 10) - 1
        if (!(idx >= 0 && idx < list.length)) return
        mid = list[idx].document_id; mtitle = list[idx].title
      }
    } catch (e) { setError((e as Error).message); return }
    setBusy(true)
    setMessages((m) => [...m,
      { role: 'user', text: t('regate_request', { title: mtitle, count: docs.length }) },
      { role: 'assistant', status: t('regate_status') }])
    try {
      const r = await fetch('/api/v1/analysis/methodology/regate', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify({ methodology_document_id: mid, document_ids: docs.map((d) => d.id) }),
      })
      const d = await r.json()
      if (!d?.success) throw new Error(d?.error || `HTTP ${r.status}`)
      setMessages((m) => {
        const c = [...m]
        c[c.length - 1] = { role: 'assistant', markdown: d.markdown || t('done'),
                            methodLabel: t('regate_method_label', { drifted: d.drifted }) }
        return c
      })
    } catch (e) {
      setMessages((m) => { const c = [...m]; c[c.length - 1] = { role: 'assistant', text: '❌ ' + (e as Error).message }; return c })
    } finally {
      setBusy(false)
    }
  }

  const run = async () => {
    if (!userId || !docs.length || busy) return
    if (!request.trim() && methodology === 'freeform') {
      setError(t('describe_request_error')); return
    }
    setError(null); setBusy(true)
    const docIds = docs.map((d) => d.id)
    const methodLabel = (methods.find((m) => m.id === methodology) || FREEFORM).label
    setMessages((m) => [...m, { role: 'user', text: request || t('method_label', { label: methodLabel }) },
      { role: 'assistant', status: t('starting_analysis'), methodLabel }])
    try {
      let runId = ''
      const kind = (methods.find((m) => m.id === methodology) || FREEFORM).kind
      if (kind === 'freeform') {
        const r = await fetch('/api/v1/analysis/runs/freeform', {
          method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
          body: JSON.stringify({ document_ids: docIds, request, model_tier: modelTier }),
        })
        runId = (await r.json())?.id
      } else {
        let pbId = methodology
        if (kind === 'template') {
          const pbResp = await fetch('/api/v1/analysis/playbooks/from-template', {
            method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
            body: JSON.stringify({ template_id: methodology }),
          })
          pbId = (await pbResp.json())?.id
        }
        const r = await fetch('/api/v1/analysis/runs', {
          method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
          body: JSON.stringify({ playbook_id: pbId, document_ids: docIds, client_context: request }),
        })
        runId = (await r.json())?.id
      }
      if (!runId) throw new Error(t('create_run_failed'))

      // Поллинг статуса
      const STAGES: Record<string, string> = {
        pending: t('stage_pending'), extracting: t('stage_extracting'),
        computing: t('stage_computing'), analyzing: t('stage_analyzing'),
        assembling: t('stage_assembling'),
      }
      let done = false
      for (let i = 0; i < 120 && !done; i++) {
        await new Promise((r) => setTimeout(r, 3000))
        const s = await fetch(`/api/v1/analysis/runs/${runId}`, { headers: authHeader() })
        const sd = await s.json()
        const st = sd?.status || 'pending'
        setMessages((m) => { const c = [...m]; c[c.length - 1] = { role: 'assistant', status: STAGES[st] || st }; return c })
        if (st === 'done') { done = true }
        else if (st === 'failed' || st === 'error') throw new Error(sd?.error || t('run_failed'))
      }
      if (!done) throw new Error(t('run_timeout', { runId }))

      // Отчёт
      const rep = await fetch(`/api/v1/analysis/runs/${runId}/report?format=json`, { headers: authHeader() })
      const repd = await rep.json()
      const report = repd?.report || repd
      setMessages((m) => {
        const c = [...m]
        c[c.length - 1] = {
          role: 'assistant', runId, methodLabel,
          markdown: report?.markdown || t('report_empty'),
          verdict: report?.verdict,
          severity: report?.severity_summary,
          chart: extractChart(report),
        }
        return c
      })
      setRequest('')
      loadHistory()  // обновить список прошлых разборов
    } catch (e) {
      setMessages((m) => { const c = [...m]; c[c.length - 1] = { role: 'assistant', text: '❌ ' + (e as Error).message }; return c })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Шапка: загрузка + методика */}
      <div className="border-b border-brain-600/20 p-3 space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            className="text-sm px-3 py-1.5 bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/40 rounded text-white disabled:opacity-50"
          >
            {uploading ? t('uploading') : t('upload_documents')}
          </button>
          <input ref={fileRef} type="file" multiple accept=".pdf,.docx,.txt,.md,.csv,.xlsx,.xls,.pptx"
                 className="hidden" onChange={(e) => uploadFiles(e.target.files)} />
          <button onClick={() => { setShowKb((v) => !v); if (!showKb) loadKbDocs() }}
                  className="text-sm px-3 py-1.5 bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/40 rounded text-white"
                  title={t('kb_button_title')}>{t('from_knowledge_base')}</button>
          <select value={methodology} onChange={(e) => setMethodology(e.target.value)}
                  className="text-sm px-2 py-1.5 bg-brain-800/40 border border-brain-600/40 rounded text-white">
            {methods.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          </select>
          <button onClick={() => { setEditorMode('manual'); setShowEditor(true) }}
                  className="text-sm px-2 py-1.5 bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/40 rounded text-cyan-300"
                  title={t('create_methodology_title')}>{t('add_methodology')}</button>
          <button onClick={() => { setEditorMode('import'); setShowEditor(true) }}
                  className="text-sm px-2 py-1.5 bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/40 rounded text-cyan-300/90"
                  title={t('import_methodology_title')}>{t('import_methodology')}</button>
          <button onClick={induceMethodology}
                  disabled={busy}
                  className={`text-sm px-2 py-1.5 bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/40 rounded text-emerald-300 disabled:opacity-40 ${docs.length < 2 ? 'opacity-60' : ''}`}
                  title={t('induce_button_title')}>{t('from_examples')}</button>
          <button onClick={regateMethodology}
                  disabled={busy || docs.length < 1}
                  className="text-sm px-2 py-1.5 bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/40 rounded text-emerald-300/80 disabled:opacity-40"
                  title={t('regate_button_title')}>{t('regate_button')}</button>
          {(methods.find((m) => m.id === methodology)?.kind === 'playbook') && (
            <button onClick={deleteMethodology}
                    className="text-sm px-2 py-1.5 bg-brain-800/40 hover:bg-red-700/40 border border-brain-600/40 rounded text-red-300"
                    title={t('delete_methodology_title')}>🗑</button>
          )}
          <button onClick={() => { setShowHistory((v) => !v); if (!showHistory) loadHistory() }}
                  className="text-sm px-2 py-1.5 bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/40 rounded text-slate-300"
                  title={t('history_title')}>{t('history_button')}{history.length ? ` (${history.length})` : ''}</button>
          <span className="text-xs text-slate-500">{t('docs_count', { count: docs.length })}</span>
        </div>
        {showKb && (
          <div className="max-h-52 overflow-y-auto rounded border border-brain-600/40 bg-brain-900/40 divide-y divide-brain-700/30">
            {kbDocs.length === 0 ? (
              <div className="text-xs text-slate-500 p-3 text-center">{t('kb_empty')}</div>
            ) : kbDocs.map((d) => {
              const added = docs.some((x) => x.id === d.id)
              return (
                <button key={d.id} onClick={() => addKbDoc(d)} disabled={added}
                        className="w-full text-left px-3 py-2 hover:bg-brain-800/50 flex items-center justify-between gap-2 disabled:opacity-50">
                  <span className="text-xs text-slate-300 truncate">📄 {d.title}</span>
                  <span className="text-[10px] text-slate-500 shrink-0">{added ? t('added') : t('add_action')}</span>
                </button>
              )
            })}
          </div>
        )}
        {showHistory && (
          <div className="max-h-52 overflow-y-auto rounded border border-brain-600/40 bg-brain-900/40 divide-y divide-brain-700/30">
            {history.length === 0 ? (
              <div className="text-xs text-slate-500 p-3 text-center">{t('no_history')}</div>
            ) : history.map((h) => (
              <button key={h.id} onClick={() => openRun(h.id)}
                      className="w-full text-left px-3 py-2 hover:bg-brain-800/50 flex items-center justify-between gap-2">
                <span className="text-xs text-slate-300 truncate">
                  {h.status === 'done' ? '✅' : h.status === 'failed' ? '❌' : '⏳'} {t('docs_short', { count: h.documents })} ·{' '}
                  {h.created_at ? new Date(h.created_at).toLocaleString() : h.id}
                </span>
                <span className="text-[10px] text-slate-500 shrink-0">{h.status}</span>
              </button>
            ))}
          </div>
        )}
        {docs.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {docs.map((d, i) => (
              <span key={d.id} className="inline-flex items-center gap-1 text-[11px] text-slate-300 bg-brain-800/40 px-2 py-0.5 rounded">
                📄 {d.title}
                <button onClick={() => setDocs((x) => x.filter((_, j) => j !== i))} className="text-slate-500 hover:text-red-300">✕</button>
              </span>
            ))}
          </div>
        )}
        {error && <div className="text-xs text-red-400">{error}</div>}
      </div>

      {/* Лента отчётов */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {messages.length === 0 && (
          <div className="text-center text-slate-500 text-sm mt-10">
            <div className="text-3xl mb-2">🔬</div>
            {t('empty_state_line1')}<br />
            {t('empty_state_line2')}
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'text-right' : ''}>
            {m.role === 'user' ? (
              <div className="inline-block bg-blue-600/30 text-white text-sm rounded-lg px-3 py-2 max-w-[80%]">{m.text}</div>
            ) : (
              <div className="bg-brain-800/30 border border-brain-600/20 rounded-lg p-3">
                {m.status && <div className="text-sm text-cyan-300 animate-pulse">⏳ {m.status}</div>}
                {m.text && <div className="text-sm text-slate-200">{m.text}</div>}
                {m.methodLabel && (m.markdown || m.verdict) && (
                  <div className="text-[11px] text-slate-500 mb-1.5">{t('method_label', { label: m.methodLabel })}</div>
                )}
                {(m.verdict || m.severity) && (
                  <div className="flex items-center gap-2 mb-2 flex-wrap">
                    {m.verdict && <span className="text-sm font-semibold text-white">{m.verdict}</span>}
                    {(m.severity?.counts?.red_flag ?? 0) > 0 && (
                      <span className="text-[11px] text-red-300 bg-red-500/10 px-2 py-0.5 rounded">🚩 {m.severity!.counts!.red_flag} red flags</span>
                    )}
                    {(m.severity?.counts?.warning ?? 0) > 0 && (
                      <span className="text-[11px] text-amber-300 bg-amber-500/10 px-2 py-0.5 rounded">⚠ {m.severity!.counts!.warning} warnings</span>
                    )}
                  </div>
                )}
                {m.chart && m.chart.length > 0 && (
                  <div className="h-56 my-2 bg-brain-900/30 rounded p-2">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={m.chart}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                        <XAxis dataKey="label" tick={{ fill: '#94a3b8', fontSize: 10 }} interval={0} angle={-20} textAnchor="end" height={60} />
                        <YAxis tick={{ fill: '#94a3b8', fontSize: 10 }} />
                        <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #475569', fontSize: 12 }} />
                        <Bar dataKey="value" fill="#22d3ee" radius={[3, 3, 0, 0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}
                {m.markdown && (
                  <div className="prose prose-invert prose-sm max-w-none text-slate-200">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.markdown}</ReactMarkdown>
                  </div>
                )}
                {m.runId && m.markdown && (
                  <div className="mt-2 pt-2 border-t border-brain-700/30">
                    <button onClick={() => downloadWord(m.runId!)}
                            className="text-[11px] px-2 py-1 bg-brain-800/50 hover:bg-brain-700/60 border border-brain-600/40 rounded text-slate-300">
                      {t('download_word')}
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {showEditor && (
        <MethodologyEditor
          userId={userId}
          initialMode={editorMode}
          uploadedDocs={docs.map((d) => ({ id: d.id, title: d.title }))}
          onClose={() => setShowEditor(false)}
          onSaved={(pb) => {
            setMethods((m) => [...m, { id: pb.id, label: `⭐ ${pb.name}`, kind: 'playbook' }])
            setMethodology(pb.id)
          }}
        />
      )}

      {/* Ввод запроса */}
      <div className="border-t border-brain-600/20 p-3">
        <div className="flex gap-2 items-end">
          <textarea
            value={request}
            onChange={(e) => setRequest(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) run() }}
            placeholder={t('request_placeholder')}
            rows={2}
            className="flex-1 px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm resize-none"
          />
          <select
            value={modelTier}
            onChange={(e) => setModelTier(e.target.value as 'standard' | 'premium')}
            title={t('model_tier_hint')}
            className="px-2 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-brain-200 text-xs"
          >
            <option value="standard">{t('model_tier_standard')}</option>
            <option value="premium">{t('model_tier_premium')}</option>
          </select>
          <button
            onClick={run}
            disabled={busy || !docs.length}
            className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white rounded text-sm font-medium"
          >
            {busy ? '…' : t('analyze_button')}
          </button>
        </div>
        <div className="text-[10px] text-slate-500 mt-1">{t('footer_hint')}</div>
      </div>
    </div>
  )
}
