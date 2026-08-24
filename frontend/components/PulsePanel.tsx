'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { Bell, RefreshCw, Send, UserPlus, Users } from 'lucide-react'
import ReactMarkdown from 'react-markdown'

// «Пульс исполнения»: сводка сигналов по задачам (просрочено/без срока/висит),
// реестр людей (кому напоминать и куда) и предпросмотр пушей.
// Backend: /pulse/*. Работает и без Telegram-бота: анализ и отчёт — всегда,
// бот нужен только как канал доставки напоминаний.

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

type PersonRec = {
  person_key: string; names: string[]; email: string
  telegram_chat_id: string; reports_to: string; muted: boolean
}

const inputCls = 'px-2 py-1 rounded bg-brain-900 border border-brain-700 text-brain-100 text-xs'

export default function PulsePanel({ userId }: { userId: string | null }) {
  const t = useTranslations('pulse_panel')
  const [md, setMd] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [people, setPeople] = useState<PersonRec[]>([])
  const [showPeople, setShowPeople] = useState(false)
  const [preview, setPreview] = useState<any>(null)

  const [fullView, setFullView] = useState(false)

  const loadReport = useCallback(async (full?: boolean) => {
    if (!userId) return
    setBusy(true); setError('')
    try {
      const r = await authFetch(
        `${API}/api/v1/pulse/report?user_id=${userId}${full ? '&full=true' : ''}`)
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') setMd(d.markdown || '')
      else setError(d.message || d.detail || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network_unavailable')) }
    setBusy(false)
  }, [userId, t])

  const loadPeople = useCallback(async () => {
    if (!userId) return
    try {
      const r = await authFetch(`${API}/api/v1/pulse/people?user_id=${userId}`)
      const d = await r.json().catch(() => ({}))
      setPeople(d.people || [])
    } catch { /* необязательно */ }
  }, [userId])

  useEffect(() => { loadReport(); loadPeople() }, [loadReport, loadPeople])

  const autofill = useCallback(async () => {
    if (!userId) return
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/pulse/people/autofill?user_id=${userId}`,
        { method: 'POST' })
      const d = await r.json().catch(() => ({}))
      setNote(t('autofill_result', {
        total: d.total ?? '?',
        created: d.created ?? 0,
        withoutChannel: d.without_channel ?? '?',
      }))
      loadPeople()
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, loadPeople, t])

  const savePerson = useCallback(async (p: PersonRec, patch: Partial<PersonRec>) => {
    if (!userId) return
    try {
      await authFetch(`${API}/api/v1/pulse/people`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, name: p.names[0] || p.person_key, ...patch }),
      })
      loadPeople()
    } catch { /* показывается при перезагрузке */ }
  }, [userId, loadPeople])

  const dryRun = useCallback(async () => {
    if (!userId) return
    setBusy(true); setPreview(null); setError('')
    try {
      const r = await authFetch(`${API}/api/v1/pulse/push?user_id=${userId}&dry_run=true`,
        { method: 'POST' })
      setPreview(await r.json().catch(() => ({})))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, t])

  const sendToMe = useCallback(async () => {
    if (!userId) return
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/pulse/send?user_id=${userId}`, { method: 'POST' })
      const d = await r.json().catch(() => ({}))
      const dv = d.delivered || {}
      setNote(dv.telegram || dv.email ? t('sent_to_you') : t('collected_no_channel'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, t])

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <button onClick={() => loadReport(fullView)} disabled={busy}
          className="px-3 py-1.5 rounded-lg bg-purple-600/80 hover:bg-purple-600 text-white text-xs flex items-center gap-1.5 disabled:opacity-40">
          <RefreshCw className="w-3.5 h-3.5" /> {busy ? t('computing') : t('refresh_pulse')}
        </button>
        <label className="flex items-center gap-1.5 text-xs text-brain-300 cursor-pointer"
          title={t('show_all_hint')}>
          <input type="checkbox" checked={fullView}
            onChange={(e) => { setFullView(e.target.checked); loadReport(e.target.checked) }} />
          {t('show_all')}
        </label>
        <button onClick={sendToMe} disabled={busy}
          className="px-3 py-1.5 rounded-lg border border-brain-600 text-brain-300 hover:text-white text-xs flex items-center gap-1.5">
          <Send className="w-3.5 h-3.5" /> {t('send_to_myself')}
        </button>
        <button onClick={() => setShowPeople((v) => !v)}
          className="px-3 py-1.5 rounded-lg border border-brain-600 text-brain-300 hover:text-white text-xs flex items-center gap-1.5">
          <Users className="w-3.5 h-3.5" /> {t('people_and_channels', { count: people.length })}
        </button>
        <button onClick={dryRun} disabled={busy}
          title={t('preview_reminders_hint')}
          className="px-3 py-1.5 rounded-lg border border-brain-600 text-brain-300 hover:text-white text-xs flex items-center gap-1.5">
          <Bell className="w-3.5 h-3.5" /> {t('preview_reminders')}
        </button>
      </div>

      {error && <div className="p-2 rounded-lg bg-red-500/10 border border-red-500/30 text-red-300 text-xs">{error}</div>}
      {note && <div className="text-[11px] text-emerald-300">{note}</div>}

      {showPeople && (
        <div className="rounded-xl border border-brain-700/60 p-3 space-y-2">
          <div className="flex items-center gap-2">
            <div className="text-sm font-medium text-brain-100">{t('who_to_remind')}</div>
            <button onClick={autofill}
              className="ml-auto px-2 py-1 rounded border border-brain-600 text-brain-300 hover:text-white text-[11px] flex items-center gap-1">
              <UserPlus className="w-3 h-3" /> {t('collect_people')}
            </button>
          </div>
          <div className="text-[11px] text-brain-500">
            {t('channels_hint')}
          </div>
          {people.map((p) => (
            <div key={p.person_key} className="flex flex-wrap items-center gap-2 text-xs text-brain-300">
              <span className="min-w-[140px] font-medium text-brain-200">{p.names[0] || p.person_key}</span>
              <input defaultValue={p.email} placeholder={t('placeholder_email')} className={inputCls}
                onBlur={(e) => e.target.value !== p.email && savePerson(p, { email: e.target.value })} />
              <input defaultValue={p.telegram_chat_id} placeholder={t('placeholder_tg_chat_id')} className={inputCls}
                onBlur={(e) => e.target.value !== p.telegram_chat_id && savePerson(p, { telegram_chat_id: e.target.value })} />
              <input defaultValue={p.reports_to} placeholder={t('placeholder_manager')} className={inputCls}
                onBlur={(e) => e.target.value !== p.reports_to && savePerson(p, { reports_to: e.target.value })} />
              <label className="flex items-center gap-1 text-[11px] text-brain-500">
                <input type="checkbox" checked={p.muted}
                  onChange={(e) => savePerson(p, { muted: e.target.checked } as any)} />
                {t('do_not_disturb')}
              </label>
            </div>
          ))}
          {people.length === 0 && (
            <div className="text-xs text-brain-500">{t('people_empty')}</div>
          )}
        </div>
      )}

      {preview && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-3 space-y-2">
          <div className="text-xs font-medium text-amber-300">
            {preview.status === 'dry_run'
              ? t('preview_nothing_sent')
              : t('preview_nothing_sent_status', { status: String(preview.status ?? '') })}
          </div>
          {(preview.pushes || []).map((p: any, i: number) => (
            <div key={i} className="rounded bg-brain-900/60 p-2 text-xs text-brain-300">
              <div className="font-medium text-brain-200">→ {p.name} ({p.channel})</div>
              <pre className="whitespace-pre-wrap text-[11px] mt-1">{p.text}</pre>
            </div>
          ))}
          {(preview.pushes || []).length === 0 && (
            <div className="text-xs text-brain-500">{t('no_reminders')}</div>
          )}
          {(preview.unmapped || []).length > 0 && (
            <div className="text-[11px] text-brain-400">
              {t('without_channel_list', {
                names: (preview.unmapped || []).map((u: any) => u.assignee).join(', '),
              })}
            </div>
          )}
        </div>
      )}

      <div className="rounded-xl border border-brain-700/60 p-4">
        {md ? (
          <div className="prose prose-invert prose-sm max-w-none text-brain-200">
            <ReactMarkdown>{md}</ReactMarkdown>
          </div>
        ) : (
          <div className="text-sm text-brain-500">{busy ? t('collecting_pulse') : t('press_refresh')}</div>
        )}
      </div>
    </div>
  )
}
