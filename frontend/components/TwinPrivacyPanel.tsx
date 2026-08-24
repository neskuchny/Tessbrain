'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { Eye, RefreshCw, ShieldCheck, UserCheck, Users } from 'lucide-react'

// Приватность слепков: «кто обращался к моему слепку» и — для руководителя —
// слепки подчинённых со сводкой обращений. Backend: /twin/access-log,
// /twin/my-team. Текста вопросов здесь нет и быть не может: сервер его не
// хранит, чтобы журнал не раскрывал приватное самого спрашивающего.

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

type Entry = {
  ts: number
  asker_uid: string
  person_name?: string
  question_chars?: number
  granted?: boolean
  reason?: string
}

type Summary = {
  total: number
  last_7d: number
  denied: number
  askers: { asker_uid: string; count: number }[]
  last_ts: number | null
}

type TeamRow = {
  user_id: string
  person_id: string | null
  role?: string
  department?: string
  depth: number
  has_twin: boolean
  access_summary: Summary | null
}

type View = 'mine' | 'team'

function fmtTs(ts: number, locale?: string) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString(locale, {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

const card = 'rounded-xl border border-brain-700 bg-brain-900/40 p-4'

export default function TwinPrivacyPanel({ userId }: { userId: string | null }) {
  const t = useTranslations('twin_privacy')
  const [view, setView] = useState<View>('mine')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // мой журнал
  const [entries, setEntries] = useState<Entry[]>([])
  const [summary, setSummary] = useState<Summary | null>(null)
  const [notLinked, setNotLinked] = useState('')

  // команда
  const [team, setTeam] = useState<TeamRow[]>([])
  const [includeIndirect, setIncludeIndirect] = useState(false)
  const [teamNote, setTeamNote] = useState('')
  const [noOrg, setNoOrg] = useState(false)
  // Слепок подчинённого, чей журнал руководитель раскрыл. Грузим по клику,
  // а не сразу для всех: список команды может быть длинным.
  const [openPerson, setOpenPerson] = useState<string | null>(null)
  const [openEntries, setOpenEntries] = useState<Entry[]>([])

  const loadMine = useCallback(async () => {
    if (!userId) return
    setBusy(true); setError(''); setNotLinked('')
    try {
      const r = await authFetch(`${API}/api/v1/twin/access-log`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, limit: 100 }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'not_linked') {
        setNotLinked(d.message || t('not_linked'))
        setEntries([]); setSummary(null)
      } else if (d.status === 'success') {
        setEntries(d.entries || [])
        setSummary(d.summary || null)
      } else setError(d.detail || d.message || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, t])

  const loadTeam = useCallback(async () => {
    if (!userId) return
    setBusy(true); setError(''); setNoOrg(false)
    try {
      const r = await authFetch(`${API}/api/v1/twin/my-team`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, include_indirect: includeIndirect }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'no_org') { setNoOrg(true); setTeam([]) }
      else if (d.status === 'success') {
        setTeam(d.team || []); setTeamNote(d.note || '')
      } else setError(d.detail || d.message || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, includeIndirect, t])

  const openMemberLog = useCallback(async (personId: string) => {
    if (!userId) return
    if (openPerson === personId) { setOpenPerson(null); setOpenEntries([]); return }
    setBusy(true); setError('')
    try {
      const r = await authFetch(`${API}/api/v1/twin/access-log`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, person_id: personId, limit: 50 }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') {
        setOpenPerson(personId); setOpenEntries(d.entries || [])
      } else setError(d.detail || d.message || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, openPerson, t])

  useEffect(() => { if (view === 'mine') loadMine(); else loadTeam() },
    [view, loadMine, loadTeam])

  const tab = (on: boolean) =>
    `px-3 py-1.5 rounded-lg border text-xs transition-colors flex items-center gap-1.5 ${
      on ? 'bg-purple-600/30 border-purple-500 text-white'
         : 'border-brain-700 text-brain-300 hover:bg-brain-800'}`

  const renderEntries = (rows: Entry[]) => (
    <div className="space-y-1.5">
      {rows.map((e, i) => (
        <div key={`${e.ts}-${i}`}
             className="flex items-center justify-between gap-3 rounded-lg border border-brain-800 bg-brain-900/60 px-3 py-2 text-xs">
          <div className="flex items-center gap-2 min-w-0">
            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
              e.granted === false ? 'bg-red-400' : 'bg-emerald-400'}`} />
            <span className="text-brain-200 truncate">{e.asker_uid}</span>
            {e.granted === false && (
              <span className="shrink-0 rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] text-red-300">
                {t('denied_badge')}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3 shrink-0 text-brain-500">
            {!!e.question_chars && (
              <span title={t('chars_hint')}>{t('chars', { n: e.question_chars })}</span>
            )}
            <span className="tabular-nums">{fmtTs(e.ts)}</span>
          </div>
        </div>
      ))}
    </div>
  )

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <button onClick={() => setView('mine')} className={tab(view === 'mine')}>
          <Eye className="w-3.5 h-3.5" /> {t('tab_mine')}
        </button>
        <button onClick={() => setView('team')} className={tab(view === 'team')}>
          <Users className="w-3.5 h-3.5" /> {t('tab_team')}
        </button>
        <button
          onClick={() => (view === 'mine' ? loadMine() : loadTeam())}
          disabled={busy}
          className="ml-auto rounded-lg border border-brain-700 px-3 py-1.5 text-xs text-brain-300 hover:bg-brain-800 disabled:opacity-50 flex items-center gap-1.5">
          <RefreshCw className={`w-3.5 h-3.5 ${busy ? 'animate-spin' : ''}`} />
          {t('refresh')}
        </button>
      </div>

      <p className="text-xs text-brain-400 leading-relaxed flex items-start gap-2">
        <ShieldCheck className="w-4 h-4 shrink-0 mt-0.5 text-brain-500" />
        {view === 'mine' ? t('intro_mine') : t('intro_team')}
      </p>

      {error && (
        <div className="rounded-lg border border-red-800 bg-red-900/20 px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {view === 'mine' && (
        <>
          {notLinked && (
            <div className="rounded-lg border border-amber-800 bg-amber-900/15 px-3 py-2.5 text-xs text-amber-200">
              {notLinked}
            </div>
          )}

          {summary && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {[
                { k: t('stat_total'), v: summary.total },
                { k: t('stat_7d'), v: summary.last_7d },
                { k: t('stat_denied'), v: summary.denied },
                { k: t('stat_askers'), v: summary.askers.length },
              ].map((s) => (
                <div key={s.k} className={card}>
                  <div className="text-[11px] uppercase tracking-wide text-brain-500">{s.k}</div>
                  <div className="mt-1 text-xl font-semibold tabular-nums text-brain-100">{s.v}</div>
                </div>
              ))}
            </div>
          )}

          {!busy && !notLinked && !entries.length && (
            <div className="rounded-xl border border-brain-800 bg-brain-900/30 px-4 py-6 text-center text-xs text-brain-400">
              {t('empty_mine')}
            </div>
          )}

          {!!entries.length && renderEntries(entries)}

          {!!entries.length && (
            <p className="text-[11px] text-brain-500 leading-relaxed">{t('note_no_text')}</p>
          )}
        </>
      )}

      {view === 'team' && (
        <>
          <label className="flex items-center gap-2 text-xs text-brain-300">
            <input type="checkbox" checked={includeIndirect}
                   onChange={(e) => setIncludeIndirect(e.target.checked)}
                   className="accent-purple-500" />
            {t('include_indirect')}
          </label>

          {noOrg && (
            <div className="rounded-lg border border-brain-800 bg-brain-900/30 px-4 py-6 text-center text-xs text-brain-400">
              {t('no_org')}
            </div>
          )}

          {!busy && !noOrg && !team.length && (
            <div className="rounded-xl border border-brain-800 bg-brain-900/30 px-4 py-6 text-center text-xs text-brain-400">
              {t('empty_team')}
            </div>
          )}

          <div className="space-y-2">
            {team.map((m) => (
              <div key={m.user_id} className={card}>
                <div className="flex items-start justify-between gap-3 flex-wrap">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <UserCheck className="w-3.5 h-3.5 text-brain-500 shrink-0" />
                      <span className="text-sm text-brain-100 truncate">{m.user_id}</span>
                      {m.depth > 1 && (
                        <span className="rounded bg-brain-800 px-1.5 py-0.5 text-[10px] text-brain-400">
                          {t('indirect_badge')}
                        </span>
                      )}
                    </div>
                    <div className="mt-1 text-[11px] text-brain-500">
                      {[m.role, m.department].filter(Boolean).join(' · ') || t('no_role')}
                    </div>
                  </div>

                  {m.has_twin && m.access_summary ? (
                    <div className="flex items-center gap-4 text-xs">
                      <div className="text-right">
                        <div className="text-[10px] uppercase tracking-wide text-brain-500">
                          {t('stat_total')}
                        </div>
                        <div className="tabular-nums text-brain-200">
                          {m.access_summary.total}
                        </div>
                      </div>
                      <div className="text-right">
                        <div className="text-[10px] uppercase tracking-wide text-brain-500">
                          {t('stat_7d')}
                        </div>
                        <div className="tabular-nums text-brain-200">
                          {m.access_summary.last_7d}
                        </div>
                      </div>
                      <button
                        onClick={() => m.person_id && openMemberLog(m.person_id)}
                        className="rounded-lg border border-brain-700 px-2.5 py-1 text-[11px] text-brain-300 hover:bg-brain-800">
                        {openPerson === m.person_id ? t('hide_log') : t('show_log')}
                      </button>
                    </div>
                  ) : (
                    <span className="rounded bg-amber-500/10 px-2 py-1 text-[11px] text-amber-300">
                      {t('no_twin')}
                    </span>
                  )}
                </div>

                {openPerson && openPerson === m.person_id && (
                  <div className="mt-3 border-t border-brain-800 pt-3">
                    {openEntries.length
                      ? renderEntries(openEntries)
                      : <div className="text-xs text-brain-500">{t('empty_member')}</div>}
                  </div>
                )}
              </div>
            ))}
          </div>

          {teamNote && (
            <p className="text-[11px] text-brain-500 leading-relaxed">{teamNote}</p>
          )}
        </>
      )}
    </div>
  )
}
