'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch, getAccessToken } from '@/lib/authFetch'
import { BookOpenCheck, MessageSquareText, RefreshCw } from 'lucide-react'

// Переписки как источник мозга: TG-группы (бот + /brain_on в группе) и
// Slack-каналы (pull по bot_token из «Интеграций»). Backend: /chat-sources/*.
// Функция за флагом ENABLE_CHAT_INGEST — карточка честно показывает состояние.

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

type Source = {
  key: string; platform: string; chat_id: string; title: string
  enabled: boolean; message_count?: number; mode?: string
  collect_files?: boolean; files_ingested?: number
  last_message_at?: string; last_digest_at?: string
  analysis?: { agreements?: any[]; tasks?: any[]; client_facts?: any[]; decisions?: any[]; dropped_unverified?: number }
}
type Participant = {
  author: string; name: string; count: number
  confirmed?: { person_id: string; person_name: string }
  candidates?: { person_id: string; person_name: string; role: string; score: number }[]
}

const btnCls = 'px-3 py-1 rounded-md bg-brain-700 hover:bg-brain-600 text-white text-xs disabled:opacity-50 inline-flex items-center gap-1'

export default function ChatSourcesCard({ userId }: { userId?: string | null }) {
  const t = useTranslations('chat_sources_card')
  const [sources, setSources] = useState<Source[]>([])
  const [flagOn, setFlagOn] = useState(true)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [slackChannels, setSlackChannels] = useState('')
  const [openKey, setOpenKey] = useState('')
  const [participants, setParticipants] = useState<Participant[]>([])
  const [preview, setPreview] = useState<{ author: string; text: string; ts: string }[]>([])
  const [importTitle, setImportTitle] = useState('')
  const [importText, setImportText] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [webhookInfo, setWebhookInfo] = useState<any>(null)
  const [webhookBase, setWebhookBase] = useState('')

  const load = useCallback(async () => {
    if (!userId || !getAccessToken()) return
    try {
      const r = await authFetch(`${API}/api/v1/chat-sources`)
      const d = await r.json().catch(() => ({}))
      setSources(d.sources || [])
      setFlagOn(Boolean(d.enabled_flag))
    } catch { /* необязательно */ }
  }, [userId])

  useEffect(() => { load() }, [load])

  const toggle = useCallback(async (s: Source) => {
    setBusy(true)
    try {
      await authFetch(`${API}/api/v1/chat-sources/toggle`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: s.key, enabled: !s.enabled }),
      })
      load()
    } catch { /* ignore */ }
    setBusy(false)
  }, [load])

  const digest = useCallback(async (s: Source) => {
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/chat-sources/digest`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: s.key }),
      })
      const d = await r.json().catch(() => ({}))
      setNote(d.status === 'success'
        ? t('note_digest_success', { title: d.title, count: d.messages ?? 0 })
        : (d.message || t('note_failed')))
      load()
    } catch (e: any) { setNote(e?.message || t('note_network')) }
    setBusy(false)
  }, [load, t])

  const openSource = useCallback(async (s: Source) => {
    if (openKey === s.key) { setOpenKey(''); return }
    setOpenKey(s.key); setParticipants([]); setPreview([])
    try {
      const [rp, rm] = await Promise.all([
        authFetch(`${API}/api/v1/chat-sources/participants?key=${encodeURIComponent(s.key)}`),
        authFetch(`${API}/api/v1/chat-sources/messages?key=${encodeURIComponent(s.key)}&limit=15&offset=${Math.max(0, (s.message_count || 0) - 15)}`),
      ])
      const dp = await rp.json().catch(() => ({}))
      const dm = await rm.json().catch(() => ({}))
      setParticipants(dp.participants || [])
      setPreview(dm.messages || [])
    } catch { /* необязательно */ }
  }, [openKey])

  const confirmIdentity = useCallback(async (key: string, author: string,
    personId: string, personName: string) => {
    setBusy(true)
    try {
      await authFetch(`${API}/api/v1/chat-sources/confirm-identity`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, author, person_id: personId, person_name: personName }),
      })
      const r = await authFetch(`${API}/api/v1/chat-sources/participants?key=${encodeURIComponent(key)}`)
      const d = await r.json().catch(() => ({}))
      setParticipants(d.participants || [])
    } catch { /* ignore */ }
    setBusy(false)
  }, [])

  const analyze = useCallback(async (s: Source) => {
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/chat-sources/analyze`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: s.key }),
      })
      const d = await r.json().catch(() => ({}))
      setNote(d.status === 'success'
        ? t('note_analyze_success', {
            agreements: d.agreements?.length || 0,
            tasks: d.tasks?.length || 0,
            facts: d.client_facts?.length || 0,
            decisions: d.decisions?.length || 0,
            dropped: d.dropped_unverified || 0,
          })
        : (d.message || t('note_failed')))
      await load()
      // результат должен быть виден сразу — раскрываем источник, не
      // заставляя догадываться, что его название кликабельно
      if (d.status === 'success') {
        setOpenKey(s.key)
        try {
          const [rp, rm] = await Promise.all([
            authFetch(`${API}/api/v1/chat-sources/participants?key=${encodeURIComponent(s.key)}`),
            authFetch(`${API}/api/v1/chat-sources/messages?key=${encodeURIComponent(s.key)}&limit=15&offset=${Math.max(0, (s.message_count || 0) - 15)}`),
          ])
          const dp = await rp.json().catch(() => ({}))
          const dm = await rm.json().catch(() => ({}))
          setParticipants(dp.participants || [])
          setPreview(dm.messages || [])
        } catch { /* раскрытие не критично */ }
      }
    } catch (e: any) { setNote(e?.message || t('note_network')) }
    setBusy(false)
  }, [load, t])

  const doImport = useCallback(async () => {
    if (!importText.trim()) return
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/chat-sources/import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: importTitle, content: importText }),
      })
      const d = await r.json().catch(() => ({}))
      setNote(d.status === 'success' ? d.note : (d.message || t('note_failed')))
      if (d.status === 'success') { setImportText(''); setImportTitle(''); setShowImport(false) }
      load()
    } catch (e: any) { setNote(e?.message || t('note_network')) }
    setBusy(false)
  }, [importTitle, importText, load, t])

  const checkWebhook = useCallback(async () => {
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/chat-sources/telegram/webhook-info`)
      const d = await r.json().catch(() => ({}))
      setWebhookInfo(d)
    } catch (e: any) { setNote(e?.message || t('note_network')) }
    setBusy(false)
  }, [t])

  const setupWebhook = useCallback(async () => {
    if (!webhookBase.trim()) return
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/chat-sources/telegram/setup-webhook`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: webhookBase.trim() }),
      })
      const d = await r.json().catch(() => ({}))
      setNote(d.status === 'success' ? d.message : (d.message || t('note_failed')))
      checkWebhook()
    } catch (e: any) { setNote(e?.message || t('note_network')) }
    setBusy(false)
  }, [webhookBase, checkWebhook, t])

  const syncSlack = useCallback(async () => {
    const channels = slackChannels.split(',').map((c) => c.trim()).filter(Boolean)
    if (!channels.length) return
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/chat-sources/slack/sync`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channels }),
      })
      const d = await r.json().catch(() => ({}))
      setNote(d.status === 'success'
        ? d.synced.map((s: any) => `#${s.channel}: +${s.added}`).join(' · ')
        : (d.message || t('note_failed')))
      load()
    } catch (e: any) { setNote(e?.message || t('note_network')) }
    setBusy(false)
  }, [slackChannels, load, t])

  return (
    <div className="bg-brain-900/60 border border-brain-700 rounded-lg p-4 mt-4 text-brain-100">
      <div className="flex items-center gap-2 mb-2">
        <MessageSquareText size={16} className="text-brain-400" />
        <span className="text-sm font-medium">{t('title')}</span>
        <button className={btnCls} onClick={load} disabled={busy}><RefreshCw size={12} /></button>
      </div>
      {!flagOn && (
        <div className="text-xs text-amber-400/90 mb-2">
          {t('feature_disabled')}
        </div>
      )}
      <div className="text-xs text-brain-400 mb-3">
        {t.rich('telegram_hint', {
          b: (chunks) => <b>{chunks}</b>,
          code: (chunks) => <code>{chunks}</code>,
        })}
        {' '}{t.rich('slack_hint', { b: (chunks) => <b>{chunks}</b> })}
      </div>

      {sources.length === 0 ? (
        <div className="text-xs text-brain-500 mb-3">{t('no_sources')}</div>
      ) : (
        <div className="flex flex-col gap-1 mb-3">
          {sources.map((s) => (
            <div key={s.key} className="border-t border-brain-800 pt-1">
              <div className="flex items-center gap-2 text-xs">
                <span className="text-brain-500">{s.platform.startsWith('telegram') ? 'TG' : s.platform.startsWith('whatsapp') ? 'WA' : s.platform === 'slack' ? 'Slack' : t('platform_file')}</span>
                <button className="text-brain-100 hover:text-white" onClick={() => openSource(s)}>{s.title}</button>
                <span className="text-brain-500">{t('messages_count', { count: s.message_count || 0 })}</span>
                {s.last_digest_at && <span className="text-brain-600">{t('in_brain_at', { date: String(s.last_digest_at).slice(0, 10) })}</span>}
                <label className="ml-auto flex items-center gap-1 cursor-pointer">
                  <input type="checkbox" checked={s.enabled} onChange={() => toggle(s)} disabled={busy} />
                  <span className="text-brain-400">{t('collect')}</span>
                </label>
                <label className="flex items-center gap-1 cursor-pointer"
                  title={t('files_title')}>
                  <input type="checkbox" checked={s.collect_files !== false} disabled={busy}
                    onChange={async (e) => {
                      await authFetch(`${API}/api/v1/chat-sources/files`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ key: s.key, enabled: e.target.checked }),
                      }).catch(() => null)
                      load()
                    }} />
                  <span className="text-brain-400">{t('files')}{(s.files_ingested || 0) > 0 ? ` (${s.files_ingested})` : ''}</span>
                </label>
                <select
                  className="px-1 py-0.5 rounded bg-brain-900 border border-brain-700 text-brain-300 text-[10px]"
                  value={s.mode === 'context_only' ? 'context_only' : 'brain'}
                  title={t('mode_title')}
                  onChange={async (e) => {
                    await authFetch(`${API}/api/v1/chat-sources/mode`, {
                      method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ key: s.key, mode: e.target.value }),
                    }).catch(() => null)
                    load()
                  }}>
                  <option value="brain">{t('mode_brain')}</option>
                  <option value="context_only">{t('mode_context_only')}</option>
                </select>
                <button className={btnCls} onClick={() => analyze(s)} disabled={busy || !(s.message_count || 0)}>{t('btn_analyze')}</button>
                <button className={btnCls} onClick={() => digest(s)}
                  disabled={busy || !(s.message_count || 0) || s.mode === 'context_only'}>
                  <BookOpenCheck size={12} /> {t('btn_to_brain')}
                </button>
              </div>
              {openKey === s.key && (
                <div className="ml-4 mt-1 mb-2 flex flex-col gap-2">
                  {participants.length > 0 && (
                    <div className="text-xs">
                      <div className="text-brain-400 mb-1">{t('participants_heading')}</div>
                      {participants.map((p) => (
                        <div key={p.author} className="flex items-center gap-2 flex-wrap py-0.5">
                          <span className="text-brain-100">{p.name}</span>
                          <span className="text-brain-600">{t('participant_messages', { count: p.count })}</span>
                          {p.confirmed ? (
                            p.confirmed.person_id
                              ? <span className="text-emerald-400">= {p.confirmed.person_name} ✓</span>
                              : <span className="text-brain-500">{t('not_from_team')}</span>
                          ) : (p.candidates || []).length > 0 ? (
                            <>
                              <span className="text-amber-400/80">{t('looks_like')}</span>
                              {p.candidates!.map((c) => (
                                <button key={c.person_id} className="px-1.5 py-0.5 rounded bg-brain-800 hover:bg-brain-700 text-brain-200"
                                  onClick={() => confirmIdentity(s.key, p.author, c.person_id, c.person_name)}>
                                  {t('confirm_person', { name: `${c.person_name}${c.role ? ` (${c.role})` : ''}` })}
                                </button>
                              ))}
                              <button className="px-1.5 py-0.5 rounded bg-brain-800/60 hover:bg-brain-700 text-brain-400"
                                onClick={() => confirmIdentity(s.key, p.author, '', '')}>
                                {t('another_person')}
                              </button>
                            </>
                          ) : (
                            <span className="text-brain-600">{t('not_found_in_meetings')}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {s.analysis && (
                    <div className="text-xs border border-brain-800/70 rounded p-2">
                      <div className="text-brain-400 mb-1">{t('analysis_heading')}</div>
                      {([['agreements', t('cat_agreements')], ['tasks', t('cat_tasks')],
                        ['client_facts', t('cat_client_facts')], ['decisions', t('cat_decisions')]] as const)
                        .map(([cat, label]) => {
                          const rows: any[] = (s.analysis as any)?.[cat] || []
                          if (!rows.length) return null
                          return (
                            <div key={cat} className="mb-1">
                              <div className="text-brain-300">{label}:</div>
                              {rows.map((r: any, i: number) => (
                                <div key={i} className="text-brain-400 ml-2">
                                  • {r.text || r.title || `${r.client}: ${r.fact}`}{r.assignee ? ` — ${r.assignee}` : ''}
                                  <span className="text-brain-600"> · «{String(r.quote || '').slice(0, 90)}»</span>
                                </div>
                              ))}
                            </div>
                          )
                        })}
                      {Boolean((s.analysis as any)?.dropped_unverified) && (
                        <div className="text-brain-600">{t('dropped_unverified', { count: (s.analysis as any).dropped_unverified })}</div>
                      )}
                    </div>
                  )}
                  {preview.length > 0 && (
                    <div className="text-xs">
                      <div className="text-brain-400 mb-1">{t('recent_messages')}</div>
                      {preview.map((m, i) => (
                        <div key={i} className="text-brain-300"><span className="text-brain-600">{String(m.ts).slice(5, 16)}</span> <b>{m.author}</b>: {String(m.text).slice(0, 160)}</div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="mb-3 flex gap-2 flex-wrap">
        <button className={btnCls} onClick={checkWebhook} disabled={busy}>
          {t('btn_bot_diagnostics')}
        </button>
        <button className={btnCls} onClick={() => setShowImport(!showImport)}>
          {t('btn_load_old_chat')}
        </button>
      </div>
      {webhookInfo && (
        <div className="mb-3 text-xs border border-brain-800 rounded-md p-2">
          {webhookInfo.status === 'success' ? (
            <>
              <div className="text-brain-300">
                {t('webhook_label')} {webhookInfo.url
                  ? <code className="text-brain-200">{webhookInfo.url}</code>
                  : <span className="text-red-400">{t('webhook_not_set')}</span>}
                {webhookInfo.pending_update_count > 0 && <span className="text-brain-500"> {t('webhook_pending', { count: webhookInfo.pending_update_count })}</span>}
              </div>
              {(webhookInfo.hints || []).map((h: string, i: number) => (
                <div key={i} className="text-amber-400/80 mt-1">• {h}</div>
              ))}
              <div className="flex gap-2 mt-2">
                <input
                  className="px-2 py-1 rounded bg-brain-900 border border-brain-700 text-brain-100 text-xs flex-1"
                  placeholder={t('webhook_base_placeholder')}
                  value={webhookBase} onChange={(e) => setWebhookBase(e.target.value)} />
                <button className={btnCls} onClick={setupWebhook} disabled={busy || !webhookBase.trim()}>
                  {t('btn_setup_webhook')}
                </button>
              </div>
            </>
          ) : (
            <div className="text-red-400">{webhookInfo.message}</div>
          )}
        </div>
      )}
      <div className="mb-3">
        {showImport && (
          <div className="mt-2 flex flex-col gap-2">
            <div className="text-xs text-brain-400">
              {t('import_hint')}
            </div>
            <input className="px-2 py-1 rounded bg-brain-900 border border-brain-700 text-brain-100 text-xs"
              placeholder={t('import_title_placeholder')}
              value={importTitle} onChange={(e) => setImportTitle(e.target.value)} />
            <textarea className="px-2 py-1 rounded bg-brain-900 border border-brain-700 text-brain-100 text-xs h-28"
              placeholder={t('import_text_placeholder')}
              value={importText} onChange={(e) => setImportText(e.target.value)} />
            <button className={btnCls} onClick={doImport} disabled={busy || !importText.trim() || !flagOn}>
              {t('btn_import')}
            </button>
          </div>
        )}
      </div>

      <div className="flex gap-2 items-center">
        <input
          className="px-2 py-1 rounded bg-brain-900 border border-brain-700 text-brain-100 text-xs flex-1"
          placeholder={t('slack_channels_placeholder')}
          value={slackChannels}
          onChange={(e) => setSlackChannels(e.target.value)}
        />
        <button className={btnCls} onClick={syncSlack} disabled={busy || !slackChannels.trim() || !flagOn}>
          {t('btn_slack_sync')}
        </button>
      </div>
      {note && <div className="text-xs text-brain-300 mt-2">{note}</div>}
      <div className="text-[10px] text-brain-600 mt-2">
        {t('footer_note')}
      </div>
    </div>
  )
}
