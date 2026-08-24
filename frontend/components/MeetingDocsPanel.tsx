'use client'

import { useEffect, useState, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'

/**
 * «Документы по встрече»: заполненный договор/КП/карточка из встречи.
 * Режим A (жёсткий): загрузить DOCX-шаблон {{поля}} → заполнить по встрече
 * с пометками уверенности 🟢/🟡/🔴. Режим B (гибкий): собрать содержимое по
 * встрече в стиле примера → превью → скачать Word.
 */
interface Props { userId?: string | null }
interface Meeting { id: string; title: string; created_at?: string; has_transcript?: boolean; duplicate_count?: number }

const DOC_KIND_IDS = ['kp', 'contract', 'card', 'free'] as const


export default function MeetingDocsPanel({ userId }: Props) {
  const t = useTranslations('meeting_docs')
  const docKinds = DOC_KIND_IDS.map((id) => ({ id, label: t(`doc_kind_${id}`) }))
  // Дедуп (#3 шаг 2): панель делает только «Собрать по встрече». Заполнение
  // своего шаблона (бывший mode 'A') — единый вход во вкладке «Шаблоны и
  // регламенты» (FillDocumentsPanel). Ветка 'A' и её состояние удалены.
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Сборка документа по встрече
  const [docKind, setDocKind] = useState('kp')
  const [styleExample, setStyleExample] = useState('')
  const [customPrompt, setCustomPrompt] = useState('')
  const [markdown, setMarkdown] = useState('')
  // Смета (позиции считаются кодом на бэке)
  const [lineItems, setLineItems] = useState<Array<{ name: string; qty: string; price: string; unit: string }>>([])

  const [extraContext, setExtraContext] = useState('')
  const [snippets, setSnippets] = useState<Array<{ id: string; name: string; text: string }>>([])

  const loadMeetings = useCallback(async (q?: string) => {
    if (!userId) return
    try {
      const params = new URLSearchParams({ user_id: userId, limit: q ? '50' : '10' })
      if (q) params.set('search', q)
      const r = await authFetch(`/api/v1/meetings-for-extraction?${params.toString()}`)
      const d = await r.json()
      setMeetings(d?.meetings || [])
    } catch { /* ignore */ }
  }, [userId])

  useEffect(() => { loadMeetings() }, [loadMeetings])
  useEffect(() => {
    const t = setTimeout(() => loadMeetings(search), 300)
    return () => clearTimeout(t)
  }, [search, loadMeetings])

  const loadSnippets = useCallback(async () => {
    if (!userId) return
    try {
      const r = await authFetch(`/api/v1/meeting-docs/snippets?user_id=${userId}`)
      const d = await r.json()
      setSnippets(d?.snippets || [])
    } catch { /* ignore */ }
  }, [userId])
  useEffect(() => { loadSnippets() }, [loadSnippets])

  const insertSnippet = (text: string) =>
    setExtraContext((c) => c ? `${c}\n${text}` : text)
  const saveSnippet = async () => {
    if (!extraContext.trim()) return
    const name = prompt(t('snippet_name_prompt'))
    if (!name) return
    try {
      await authFetch(`/api/v1/meeting-docs/snippets?user_id=${userId}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, text: extraContext }),
      })
      await loadSnippets()
    } catch (e) { setError((e as Error).message) }
  }
  const deleteSnippet = async (id: string) => {
    try {
      await authFetch(`/api/v1/meeting-docs/snippets/${id}?user_id=${userId}`, { method: 'DELETE' })
      await loadSnippets()
    } catch { /* ignore */ }
  }

  // Живой пересчёт сметы (число считает бэк — но для превью считаем и в JS)
  const money = (() => {
    let subtotal = 0
    for (const it of lineItems) {
      const q = parseFloat((it.qty || '').replace(',', '.')) || 0
      const p = parseFloat((it.price || '').replace(/\s/g, '').replace(',', '.')) || 0
      subtotal += q * p
    }
    const vat = subtotal * 0.2
    return { subtotal, vat, total: subtotal + vat }
  })()

  const toggle = (id: string) =>
    setSelected((s) => s.includes(id) ? s.filter((x) => x !== id) : [...s, id])

  // Доставка артефактов встречи (саммари/задачи/решения/участники) в Telegram
  // по кнопке. Статус — по строке встречи (idle|sending|ok|empty|err).
  const [deliverState, setDeliverState] = useState<Record<string, string>>({})
  const deliverMeeting = async (id: string) => {
    if (!userId) return
    setDeliverState((s) => ({ ...s, [id]: 'sending' }))
    try {
      const r = await authFetch(`/api/v1/meetflow/meetings/deliver`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, meeting_id: id }),
      })
      const d = await r.json()
      const next = d?.status === 'success' ? 'ok' : (d?.status === 'empty' ? 'empty' : 'err')
      setDeliverState((s) => ({ ...s, [id]: next }))
      if (next === 'err' && d?.message) setError(d.message)
    } catch (e) {
      setDeliverState((s) => ({ ...s, [id]: 'err' }))
      setError((e as Error).message)
    }
  }

  // Поделиться встречей через MeetFlow: публичная ссылка ИЛИ доступ по email.
  const [sharePanel, setSharePanel] = useState<{ id: string; title: string; kind: string; access: string; expires: string; password: string; emails: string; perm: string; result: string; loading: boolean } | null>(null)
  const createShare = async () => {
    if (!sharePanel || !userId) return
    setSharePanel({ ...sharePanel, loading: true, result: '' })
    try {
      const isGrant = sharePanel.kind === 'grant'
      const path = isGrant ? '/api/v1/meetflow/meetings/grant' : '/api/v1/meetflow/meetings/share'
      const body: Record<string, unknown> = isGrant
        ? { user_id: userId, meeting_id: sharePanel.id, grantee: sharePanel.emails.split(',')[0]?.trim(), permission_type: sharePanel.perm }
        : { user_id: userId, meeting_id: sharePanel.id, access_level: sharePanel.access, password: sharePanel.password || undefined, expires_in_days: sharePanel.expires ? parseInt(sharePanel.expires, 10) : null }
      const r = await authFetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      const d = await r.json()
      if (d?.status === 'success') {
        const res = isGrant ? (d.meeting_link || '') : (d.share_url || '')
        setSharePanel((s) => s ? { ...s, result: res, loading: false } : s)
      } else {
        setSharePanel((s) => s ? { ...s, loading: false } : s)
        if (d?.message) setError(d.message)
      }
    } catch (e) {
      setSharePanel((s) => s ? { ...s, loading: false } : s); setError((e as Error).message)
    }
  }

  // Тянуть из встречи конкретный артефакт (саммари/задачи/транскрипт/повестка/
  // отчёт) — показываем текст в панели-просмотрщике, можно скопировать/отправить.
  const ARTIFACT_KINDS = ['summary', 'tasks', 'decisions', 'participants', 'transcript', 'agenda', 'report'] as const
  const [viewer, setViewer] = useState<{ id: string; kind: string; title: string; text: string; loading: boolean; sent?: string } | null>(null)
  const pullArtifact = async (id: string, kind: string) => {
    if (!userId || !kind) return
    setViewer({ id, kind, title: '', text: '', loading: true })
    try {
      const r = await authFetch(`/api/v1/meetflow/meetings/artifact`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, meeting_id: id, kind }),
      })
      const d = await r.json()
      if (d?.status === 'success') {
        setViewer({ id, kind, title: d.title || '', text: d.text || '', loading: false })
      } else {
        setViewer({ id, kind, title: d?.title || '', text: '', loading: false })
        if (d?.message) setError(d.message)
      }
    } catch (e) {
      setViewer(null); setError((e as Error).message)
    }
  }
  const sendArtifact = async () => {
    if (!viewer || !userId) return
    setViewer({ ...viewer, sent: 'sending' })
    try {
      const r = await authFetch(`/api/v1/meetflow/meetings/artifact`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, meeting_id: viewer.id, kind: viewer.kind, deliver: true }),
      })
      const d = await r.json()
      setViewer((v) => v ? { ...v, sent: d?.delivered ? 'ok' : 'err' } : v)
      if (!d?.delivered && d?.message) setError(d.message)
    } catch (e) {
      setViewer((v) => v ? { ...v, sent: 'err' } : v); setError((e as Error).message)
    }
  }

  const runCompose = async () => {
    if (!selected.length) { setError(t('select_meeting_error')); return }
    setBusy(true); setError(null); setMarkdown('')
    try {
      const r = await authFetch(`/api/v1/meeting-docs/compose?user_id=${userId}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          meeting_ids: selected, doc_kind: docKind, style_example: styleExample,
          custom_prompt: customPrompt, extra_context: extraContext,
          line_items: lineItems.filter((it) => it.name.trim()),
        }),
      })
      const d = await r.json()
      if (!r.ok) { setError(d?.detail || t('compose_error')); return }
      setMarkdown(d.markdown || '')
    } catch (e) { setError((e as Error).message) }
    finally { setBusy(false) }
  }

  const downloadComposed = async (format: 'docx' | 'pdf' = 'docx') => {
    try {
      const r = await authFetch(`/api/v1/meeting-docs/render?user_id=${userId}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ markdown, format, title: docKinds.find((k) => k.id === docKind)?.label || t('document_fallback_title') }),
      })
      if (!r.ok) { setError(t('word_render_error')); return }
      // Бэк не смог отдать PDF (нет fpdf2/LibreOffice) → прислал DOCX и заголовок.
      // Не молчим: качаем DOCX, но честно предупреждаем и как починить.
      if (format === 'pdf' && r.headers.get('X-PDF-Fallback') === '1') {
        setError(t('pdf_engine_missing'))
      }
      const blob = await r.blob()
      const isPdf = (blob.type || '').includes('pdf')
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url; a.download = isPdf ? 'document.pdf' : 'document.docx'
      document.body.appendChild(a); a.click(); URL.revokeObjectURL(url); a.remove()
    } catch (e) { setError((e as Error).message) }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="border-b border-brain-600/20 p-3 space-y-3">
        <div className="flex items-center gap-2">
          <span className="text-lg">📄</span>
          <h2 className="text-white font-semibold">{t('panel_title')}</h2>
        </div>
        {/* Дедуп (#3 шаг 2): «Заполнить мой шаблон» жил и здесь, и во вкладке
            «Шаблоны и регламенты» (FillDocumentsPanel: библиотека шаблонов +
            загрузка DOCX + заполнение + пометки полей). Оставили ЕДИНЫЙ вход
            там; здесь — только уникальная «Собрать по встрече» (генерация в
            стиле примера, а не заполнение шаблона). */}
        <p className="text-xs text-slate-500">
          {t('panel_hint_compose')} {t('panel_hint_fill_prefix')}{' '}
          <code>{t('template_field_example')}</code> {t('panel_hint_fill_suffix')}
        </p>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        {/* Выбор встреч */}
        <div>
          <div className="text-xs text-slate-400 mb-1.5">{t('meeting_select_label')}</div>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t('search_meeting_placeholder')}
            className="w-full mb-2 px-3 py-1.5 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm" />
          <div className="max-h-40 overflow-y-auto rounded border border-brain-600/40 divide-y divide-brain-700/30">
            {meetings.length === 0 ? (
              <div className="text-xs text-slate-500 p-3 text-center">{t('no_meetings_found')}</div>
            ) : meetings.map((m) => (
              <label key={m.id} className="flex items-center gap-2 px-3 py-2 hover:bg-brain-800/50 cursor-pointer">
                <input type="checkbox" checked={selected.includes(m.id)} onChange={() => toggle(m.id)}
                  className="w-4 h-4 rounded bg-brain-700 border-brain-600 text-cyan-500" />
                <span className="text-sm text-slate-200 truncate flex-1">{m.title}</span>
                {(m.duplicate_count || 1) > 1 && (
                  <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-brain-700/50 text-slate-400"
                    title={t('duplicate_copies_title')}>×{m.duplicate_count}</span>
                )}
                {!m.has_transcript && <span className="text-[10px] text-amber-400/70">{t('no_transcript')}</span>}
                <button type="button"
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); deliverMeeting(m.id) }}
                  disabled={deliverState[m.id] === 'sending'}
                  title={t('deliver_meeting_title')}
                  className="shrink-0 text-[11px] px-2 py-0.5 rounded border border-brain-600/40 text-slate-300 hover:border-cyan-500/50 hover:text-cyan-200 disabled:opacity-50">
                  {deliverState[m.id] === 'sending' ? '…'
                    : deliverState[m.id] === 'ok' ? t('deliver_meeting_ok')
                    : deliverState[m.id] === 'empty' ? t('deliver_meeting_empty')
                    : deliverState[m.id] === 'err' ? t('deliver_meeting_err')
                    : t('deliver_meeting_btn')}
                </button>
                <select value=""
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => { const k = e.target.value; e.target.value = ''; if (k) pullArtifact(m.id, k) }}
                  title={t('pull_artifact_title')}
                  className="shrink-0 text-[11px] px-1 py-0.5 rounded border border-brain-600/40 bg-brain-800/40 text-slate-300 hover:border-cyan-500/50 cursor-pointer">
                  <option value="">{t('pull_artifact_btn')}</option>
                  {ARTIFACT_KINDS.map((k) => (
                    <option key={k} value={k}>{t(`art_${k}`)}</option>
                  ))}
                </select>
                <button type="button"
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); setSharePanel({ id: m.id, title: m.title, kind: 'link', access: 'view', expires: '7', password: '', emails: '', perm: 'read', result: '', loading: false }) }}
                  title={t('share_meeting_title')}
                  className="shrink-0 text-[11px] px-2 py-0.5 rounded border border-brain-600/40 text-slate-300 hover:border-cyan-500/50 hover:text-cyan-200">
                  {t('share_meeting_btn')}
                </button>
              </label>
            ))}
          </div>
        </div>

        {/* Панель расшаривания встречи (публичная ссылка MeetFlow) */}
        {sharePanel && (
          <div className="rounded border border-cyan-600/30 bg-brain-800/30 p-3 space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-cyan-200 flex-1 truncate">🔗 {t('share_meeting_btn')} — {sharePanel.title}</span>
              <button onClick={() => setSharePanel(null)} className="text-[11px] px-2 py-0.5 rounded border border-brain-600/40 text-slate-400 hover:text-red-300">✕</button>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <select value={sharePanel.kind} onChange={(e) => setSharePanel({ ...sharePanel, kind: e.target.value, result: '' })}
                className="text-xs px-2 py-1 rounded bg-brain-900/60 border border-brain-600/40 text-slate-200">
                <option value="link">{t('share_kind_link')}</option>
                <option value="grant">{t('share_kind_grant')}</option>
              </select>
              {sharePanel.kind === 'grant' ? (
                <>
                  <input value={sharePanel.emails} onChange={(e) => setSharePanel({ ...sharePanel, emails: e.target.value })}
                    placeholder={t('share_email_placeholder')}
                    className="flex-1 min-w-[140px] text-xs px-2 py-1 rounded bg-brain-900/60 border border-brain-600/40 text-slate-200" />
                  <select value={sharePanel.perm} onChange={(e) => setSharePanel({ ...sharePanel, perm: e.target.value })}
                    className="text-xs px-2 py-1 rounded bg-brain-900/60 border border-brain-600/40 text-slate-200">
                    <option value="read">{t('perm_read')}</option>
                    <option value="write">{t('perm_write')}</option>
                    <option value="admin">{t('perm_admin')}</option>
                  </select>
                </>
              ) : (
                <>
                  <select value={sharePanel.access} onChange={(e) => setSharePanel({ ...sharePanel, access: e.target.value })}
                    className="text-xs px-2 py-1 rounded bg-brain-900/60 border border-brain-600/40 text-slate-200">
                    <option value="view">{t('share_access_view')}</option>
                    <option value="comment">{t('share_access_comment')}</option>
                  </select>
                  <input type="number" min={0} value={sharePanel.expires} onChange={(e) => setSharePanel({ ...sharePanel, expires: e.target.value })}
                    title={t('share_expires_title')} placeholder={t('share_expires_placeholder')}
                    className="w-20 text-xs px-2 py-1 rounded bg-brain-900/60 border border-brain-600/40 text-slate-200" />
                  <input value={sharePanel.password} onChange={(e) => setSharePanel({ ...sharePanel, password: e.target.value })}
                    placeholder={t('share_password_placeholder')}
                    className="flex-1 min-w-[100px] text-xs px-2 py-1 rounded bg-brain-900/60 border border-brain-600/40 text-slate-200" />
                </>
              )}
              <button onClick={createShare} disabled={sharePanel.loading}
                className="text-[11px] px-2 py-1 rounded border border-cyan-500/50 text-cyan-200 hover:bg-cyan-500/10 disabled:opacity-40">
                {sharePanel.loading ? '…' : (sharePanel.kind === 'grant' ? t('share_grant') : t('share_create'))}
              </button>
            </div>
            {sharePanel.result && (
              <div className="flex items-center gap-2">
                {sharePanel.kind === 'grant' && <span className="text-[11px] text-emerald-300">✓</span>}
                <input readOnly value={sharePanel.result}
                  className="flex-1 text-xs px-2 py-1 rounded bg-brain-900/60 border border-brain-600/40 text-cyan-200" />
                <button onClick={() => navigator.clipboard?.writeText(sharePanel.result)}
                  className="text-[11px] px-2 py-1 rounded border border-brain-600/40 text-slate-300 hover:border-cyan-500/50">
                  {t('artifact_copy')}
                </button>
              </div>
            )}
          </div>
        )}

        {/* Просмотрщик вытянутого артефакта встречи */}
        {viewer && (
          <div className="rounded border border-cyan-600/30 bg-brain-800/30 p-3 space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-cyan-200 flex-1 truncate">
                {t(`art_${viewer.kind}`)}{viewer.title ? ` — ${viewer.title}` : ''}
              </span>
              <button onClick={() => { if (viewer.text) navigator.clipboard?.writeText(viewer.text) }}
                disabled={!viewer.text}
                className="text-[11px] px-2 py-0.5 rounded border border-brain-600/40 text-slate-300 hover:border-cyan-500/50 disabled:opacity-40">
                {t('artifact_copy')}
              </button>
              <button onClick={sendArtifact}
                disabled={!viewer.text || viewer.sent === 'sending'}
                className="text-[11px] px-2 py-0.5 rounded border border-brain-600/40 text-slate-300 hover:border-cyan-500/50 disabled:opacity-40">
                {viewer.sent === 'sending' ? '…' : viewer.sent === 'ok' ? t('deliver_meeting_ok')
                  : viewer.sent === 'err' ? t('deliver_meeting_err') : t('artifact_to_telegram')}
              </button>
              <button onClick={() => setViewer(null)}
                className="text-[11px] px-2 py-0.5 rounded border border-brain-600/40 text-slate-400 hover:text-red-300">
                ✕
              </button>
            </div>
            {viewer.loading ? (
              <div className="text-xs text-slate-500">{t('artifact_loading')}</div>
            ) : viewer.text ? (
              <pre className="text-xs text-slate-200 whitespace-pre-wrap max-h-56 overflow-y-auto font-sans">{viewer.text}</pre>
            ) : (
              <div className="text-xs text-amber-400/80">{t('artifact_empty')}</div>
            )}
          </div>
        )}

        {/* Заготовки контекста — реюз-блоки для обоих режимов */}
        {snippets.length > 0 && (
          <div>
            <div className="text-xs text-slate-400 mb-1">{t('snippets_label')}</div>
            <div className="flex flex-wrap gap-1.5">
              {snippets.map((s) => (
                <span key={s.id} className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border border-brain-600/40 text-slate-300 cursor-pointer hover:border-cyan-500/40"
                  onClick={() => insertSnippet(s.text)} title={s.text.slice(0, 120)}>
                  ＋ {s.name}
                  <button onClick={(e) => { e.stopPropagation(); deleteSnippet(s.id) }} className="text-slate-500 hover:text-red-300">✕</button>
                </span>
              ))}
            </div>
          </div>
        )}

          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {docKinds.map((k) => (
                <button key={k.id} onClick={() => setDocKind(k.id)}
                  className={`px-3 py-1.5 rounded text-sm border ${docKind === k.id ? 'bg-cyan-600 border-cyan-600 text-white' : 'border-brain-600/40 text-brain-300'}`}>
                  {k.label}
                </button>
              ))}
            </div>
            <textarea value={styleExample} onChange={(e) => setStyleExample(e.target.value)} rows={3}
              placeholder={t('style_example_placeholder')}
              className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm resize-none" />
            <input value={customPrompt} onChange={(e) => setCustomPrompt(e.target.value)}
              placeholder={t('custom_prompt_placeholder')}
              className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm" />
            <div className="relative">
              <textarea value={extraContext} onChange={(e) => setExtraContext(e.target.value)} rows={2}
                placeholder={t('extra_context_placeholder')}
                className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm resize-none" />
              {extraContext.trim() && (
                <button onClick={saveSnippet} className="absolute right-2 bottom-2 text-[11px] text-cyan-300 hover:text-cyan-200">{t('save_snippet_button')}</button>
              )}
            </div>

            {/* Смета: позиции — число считает КОД, не модель */}
            {(docKind === 'kp') && (
              <div className="border border-brain-600/30 rounded p-2 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-slate-400">{t('line_items_label')}</span>
                  <button onClick={() => setLineItems((l) => [...l, { name: '', qty: '1', price: '', unit: '' }])}
                    className="text-xs px-2 py-0.5 bg-brain-800/50 border border-brain-600/40 rounded text-cyan-300">{t('add_line_item')}</button>
                </div>
                {lineItems.map((it, i) => (
                  <div key={i} className="flex gap-1.5 items-center">
                    <input value={it.name} onChange={(e) => setLineItems((l) => l.map((x, j) => j === i ? { ...x, name: e.target.value } : x))}
                      placeholder={t('item_name_placeholder')} className="flex-1 px-2 py-1 bg-brain-800/40 border border-brain-600/40 rounded text-white text-xs" />
                    <input value={it.qty} onChange={(e) => setLineItems((l) => l.map((x, j) => j === i ? { ...x, qty: e.target.value } : x))}
                      placeholder={t('item_qty_placeholder')} className="w-16 px-2 py-1 bg-brain-800/40 border border-brain-600/40 rounded text-white text-xs" />
                    <input value={it.price} onChange={(e) => setLineItems((l) => l.map((x, j) => j === i ? { ...x, price: e.target.value } : x))}
                      placeholder={t('item_price_placeholder')} className="w-24 px-2 py-1 bg-brain-800/40 border border-brain-600/40 rounded text-white text-xs" />
                    <button onClick={() => setLineItems((l) => l.filter((_, j) => j !== i))} className="text-slate-500 hover:text-red-300 text-xs">✕</button>
                  </div>
                ))}
                {lineItems.length > 0 && (
                  <div className="text-xs text-slate-300 text-right">
                    {t('subtotal_label', { amount: money.subtotal.toLocaleString('ru-RU', { minimumFractionDigits: 2 }) })} ·{' '}
                    {t('vat_label', { amount: money.vat.toLocaleString('ru-RU', { minimumFractionDigits: 2 }) })} ·
                    <b> {t('total_label', { amount: money.total.toLocaleString('ru-RU', { minimumFractionDigits: 2 }) })}</b>
                  </div>
                )}
              </div>
            )}
            <button onClick={runCompose} disabled={busy || !selected.length}
              className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white rounded text-sm font-medium">
              {busy ? t('composing') : t('compose_button')}
            </button>

            {markdown && (
              <div className="space-y-2">
                <div className="prose prose-invert prose-sm max-w-none bg-brain-900/30 rounded p-3 text-slate-200">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => downloadComposed('docx')}
                    className="px-3 py-1.5 bg-brain-800/50 hover:bg-brain-700/60 border border-brain-600/40 rounded text-slate-200 text-sm">
                    {t('download_word')}
                  </button>
                  <button onClick={() => downloadComposed('pdf')}
                    className="px-3 py-1.5 bg-brain-800/50 hover:bg-brain-700/60 border border-brain-600/40 rounded text-slate-200 text-sm">
                    {t('download_pdf')}
                  </button>
                </div>
              </div>
            )}
          </div>

        {error && <div className="text-xs text-red-400">{error}</div>}
      </div>
    </div>
  )
}
