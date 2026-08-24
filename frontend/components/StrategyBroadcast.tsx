'use client'

/**
 * Трансляция стратегии.
 *
 * StrategyBroadcastSection (в OrgPanel, для руководителя):
 *   послание → персональные версии для каждого сотрудника (фрейм из
 *   Persona/Mini Tess) → правка руками → «Отправить».
 *
 * BroadcastInbox (на главной, для сотрудника):
 *   СВОЯ версия послания + кнопка «Понял, принял» (+ вопрос руководителю).
 */

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { Loader2, Megaphone, Send, Check } from 'lucide-react'

// Все broadcast-роуты требуют Authorization; без токена не ходим вовсе
// (неавторизованный поллинг давал 401-спам в логах бэка).
function authHeaders(json = false): Record<string, string> | null {
  const token = typeof window !== 'undefined'
    ? localStorage.getItem('tessent_access_token') : null
  if (!token) return null
  const h: Record<string, string> = { Authorization: `Bearer ${token}` }
  if (json) h['Content-Type'] = 'application/json'
  return h
}

interface Version { text: string; role: string; department: string; status: string; question?: string }
interface Broadcast {
  id: string
  original: string
  created_at: string
  status: 'draft' | 'sent'
  versions: Record<string, Version>
}

export function StrategyBroadcastSection({ members }: {
  members: { user_id: string; role?: string | null; department?: string | null }[]
}) {
  const t = useTranslations('strategy_broadcast')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState<Broadcast | null>(null)
  const [history, setHistory] = useState<Broadcast[]>([])
  const [note, setNote] = useState('')

  const loadHistory = useCallback(async () => {
    const h = authHeaders()
    if (!h) return
    try {
      const r = await fetch('/api/v1/broadcast/list', { headers: h })
      const d = await r.json()
      setHistory(Array.isArray(d.broadcasts) ? d.broadcasts : [])
    } catch { /* best-effort */ }
  }, [])

  useEffect(() => { loadHistory() }, [loadHistory])

  const generate = async () => {
    if (busy || message.trim().length < 20) return
    setBusy(true); setNote('')
    try {
      const r = await fetch('/api/v1/broadcast/strategy', {
        method: 'POST',
        headers: authHeaders(true) || { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
      })
      const d = await r.json()
      if (d.status === 'success' && d.broadcast) {
        setDraft(d.broadcast)
        setNote('')
      } else {
        setNote('⚠️ ' + (d.message || t('generate_failed')))
      }
    } catch { setNote('⚠️ ' + t('generate_failed')) } finally { setBusy(false) }
  }

  const saveVersion = async (memberUid: string, text: string) => {
    if (!draft) return
    setDraft({ ...draft, versions: { ...draft.versions, [memberUid]: { ...draft.versions[memberUid], text } } })
    try {
      await fetch(`/api/v1/broadcast/${draft.id}/version`, {
        method: 'POST',
        headers: authHeaders(true) || { 'Content-Type': 'application/json' },
        body: JSON.stringify({ member_uid: memberUid, text }),
      })
    } catch { /* правка останется локально до отправки */ }
  }

  const sendAll = async () => {
    if (!draft || busy) return
    setBusy(true)
    try {
      const r = await fetch(`/api/v1/broadcast/${draft.id}/send`, {
        method: 'POST',
        headers: authHeaders(true) || { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      const d = await r.json()
      if (d.status === 'success') {
        setNote('✅ ' + t('sent_note', { n: d.recipients, tg: d.telegram_delivered }))
        setDraft(null); setMessage('')
        loadHistory()
      } else {
        setNote('⚠️ ' + (d.message || t('send_failed')))
      }
    } catch { setNote('⚠️ ' + t('send_failed')) } finally { setBusy(false) }
  }

  const ackCount = (b: Broadcast) =>
    Object.values(b.versions || {}).filter((v) => v.status === 'acked').length

  return (
    <div className="rounded-xl border border-brain-700/30 bg-brain-950/50 p-3 space-y-2">
      <div className="flex items-center gap-2 text-[12px] uppercase tracking-wide text-brain-400">
        <Megaphone className="w-4 h-4 text-purple-400" /> {t('section_title')}
      </div>
      <p className="text-[10.5px] text-brain-500">{t('section_hint')}</p>

      {!draft && (
        <>
          <textarea value={message} onChange={(e) => setMessage(e.target.value)}
            placeholder={t('message_placeholder')} rows={4}
            className="w-full px-2.5 py-2 rounded-lg bg-brain-900/60 border border-brain-700/40 text-xs text-brain-100 resize-y" />
          <button onClick={generate} disabled={busy || message.trim().length < 20}
            className="px-3 py-1.5 rounded-lg bg-purple-600/80 hover:bg-purple-500 text-white text-xs disabled:opacity-40 inline-flex items-center gap-1.5">
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : '🪄'} {t('generate_button')}
          </button>
        </>
      )}

      {draft && (
        <div className="space-y-2">
          <p className="text-[11px] text-brain-400">{t('preview_hint')}</p>
          {Object.entries(draft.versions).map(([uid, v]) => {
            const m = members.find((x) => x.user_id === uid)
            return (
              <div key={uid} className="rounded-lg border border-brain-800/60 bg-brain-900/50 p-2 space-y-1">
                <div className="text-[11px] text-purple-300 font-medium">
                  {uid.slice(0, 8)} · {v.role || m?.role || ''}{(v.department || m?.department) ? ` · ${v.department || m?.department}` : ''}
                </div>
                <textarea defaultValue={v.text} rows={6}
                  onBlur={(e) => { if (e.target.value !== v.text) saveVersion(uid, e.target.value) }}
                  className="w-full px-2 py-1.5 rounded bg-brain-950 border border-brain-800/50 text-[11px] text-brain-200 resize-y" />
              </div>
            )
          })}
          <div className="flex gap-2">
            <button onClick={sendAll} disabled={busy}
              className="px-3 py-1.5 rounded-lg bg-emerald-600/80 hover:bg-emerald-500 text-white text-xs disabled:opacity-40 inline-flex items-center gap-1.5">
              {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />} {t('send_button')}
            </button>
            <button onClick={() => setDraft(null)} disabled={busy}
              className="px-3 py-1.5 rounded-lg border border-brain-700 text-brain-300 hover:bg-brain-800 text-xs">
              {t('discard_button')}
            </button>
          </div>
        </div>
      )}

      {note && <p className="text-[11px] text-brain-300">{note}</p>}

      {history.length > 0 && (
        <div className="space-y-1 pt-1 border-t border-brain-800/50">
          {history.slice(0, 5).map((b) => (
            <div key={b.id} className="flex items-center gap-2 text-[11px] text-brain-400">
              <span className={b.status === 'sent' ? 'text-emerald-400' : 'text-amber-400'}>
                {b.status === 'sent' ? '✓' : '…'}
              </span>
              <span className="flex-1 truncate" title={b.original}>{b.original}</span>
              {b.status === 'sent' && (
                <span className="text-brain-500">
                  {t('ack_stat', { acked: ackCount(b), total: Object.keys(b.versions || {}).length })}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function BroadcastInbox() {
  const t = useTranslations('strategy_broadcast')
  const [items, setItems] = useState<{ broadcast_id: string; text: string; status: string; created_at: string }[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [question, setQuestion] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    const h = authHeaders()
    if (!h) return
    try {
      const r = await fetch('/api/v1/broadcast/inbox', { headers: h })
      const d = await r.json()
      setItems(Array.isArray(d.items) ? d.items : [])
    } catch { /* блок просто не показывается */ }
  }, [])

  useEffect(() => { load() }, [load])

  const doAck = async (bid: string) => {
    const h = authHeaders(true)
    if (!h) return
    setBusy(bid)
    try {
      await fetch(`/api/v1/broadcast/${bid}/ack`, {
        method: 'POST',
        headers: h,
        body: JSON.stringify({ question: question[bid] || '' }),
      })
      await load()
    } catch { /* повторит позже */ } finally { setBusy(null) }
  }

  const fresh = items.filter((i) => i.status !== 'acked')
  if (fresh.length === 0) return null

  return (
    <div className="rounded-xl border border-purple-500/30 bg-purple-500/5 p-3 space-y-2">
      <div className="flex items-center gap-2 text-[12px] uppercase tracking-wide text-purple-300">
        <Megaphone className="w-4 h-4" /> {t('inbox_title')}
      </div>
      {fresh.slice(0, 2).map((it) => (
        <div key={it.broadcast_id} className="space-y-1.5">
          <div className="text-xs text-brain-100 whitespace-pre-wrap">{it.text}</div>
          <div className="flex gap-2 items-center flex-wrap">
            <input value={question[it.broadcast_id] || ''}
              onChange={(e) => setQuestion((q) => ({ ...q, [it.broadcast_id]: e.target.value }))}
              placeholder={t('question_placeholder')}
              className="flex-1 min-w-[180px] px-2 py-1 rounded bg-brain-950 border border-brain-800/50 text-[11px] text-brain-200" />
            <button onClick={() => doAck(it.broadcast_id)} disabled={busy === it.broadcast_id}
              className="px-2.5 py-1 rounded-lg bg-emerald-600/80 hover:bg-emerald-500 text-white text-[11px] disabled:opacity-40 inline-flex items-center gap-1">
              {busy === it.broadcast_id ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />} {t('ack_button')}
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
