'use client'

import { useCallback, useEffect, useState } from 'react'
import { useLocale, useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { Brain, Download, MessageCircle, Play, ShieldCheck, Users } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import TwinPrivacyPanel from './TwinPrivacyPanel'

// «Совет директоров»: (1) планёрка нескольких ролей и/или слепков живых
// сотрудников — раунды, решение, протокол разногласий, сверка с данными;
// (2) диалог с одним директором; (3) чат со слепком сотрудника + выгрузка
// датасета для дообучения ИИ-копии. Backend: /boardroom/*, /twin/*.

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

type Director = { id: string; name: string; icon: string }
type Person = { name?: string; person_id?: string; role?: string }
type Mode = 'board' | 'director' | 'twin' | 'privacy'

// На чём стоит ответ слепка: встречи и разделы профиля. Приходит от
// бэкенда в grounded_on — без этого «похоже на правду» остаётся
// единственным критерием, а слепок говорит от лица человека.
type Grounding = {
  meetings?: { meeting_id?: string; title?: string; date?: string; role?: string }[]
  meetings_total?: number
  sections?: { section: string; items: number }[]
  note?: string
}

const chip = (on: boolean) =>
  `px-3 py-1.5 rounded-lg border text-xs transition-colors ${
    on ? 'bg-purple-600/30 border-purple-500 text-white'
       : 'border-brain-700 text-brain-300 hover:bg-brain-800'}`

function Markdown({ text }: { text: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none text-brain-200">
      <ReactMarkdown>{text}</ReactMarkdown>
    </div>
  )
}

// Свёрнутая опора ответа: раскрывается по клику, чтобы не забивать диалог,
// но была под рукой — ответ от лица человека нужно уметь перепроверить.
function Grounded({ g, label }: { g: Grounding; label: string }) {
  const meetings = g.meetings || []
  const sections = g.sections || []
  if (!meetings.length && !sections.length) return null
  return (
    <details className="mt-2 border-t border-brain-800 pt-2">
      <summary className="cursor-pointer text-[11px] text-brain-500 hover:text-brain-300">
        {label}
      </summary>
      <div className="mt-2 space-y-2">
        {!!sections.length && (
          <div className="flex flex-wrap gap-1.5">
            {sections.map((s) => (
              <span key={s.section}
                    className="rounded bg-brain-800/70 px-2 py-0.5 text-[10px] text-brain-300">
                {s.section} · <span className="tabular-nums">{s.items}</span>
              </span>
            ))}
          </div>
        )}
        {!!meetings.length && (
          <ul className="space-y-1">
            {meetings.map((m, i) => (
              <li key={m.meeting_id || i} className="text-[11px] text-brain-400 truncate">
                · {m.title || m.meeting_id}
                {m.date ? <span className="text-brain-600"> — {m.date}</span> : null}
              </li>
            ))}
          </ul>
        )}
        {g.note && <div className="text-[10px] text-brain-600">{g.note}</div>}
      </div>
    </details>
  )
}

export default function VirtualBoardPanel({ userId }: { userId: string | null }) {
  const t = useTranslations('virtual_board')
  // язык интерфейса едет на бэкенд: промпты ролей русские, и без
  // этого директора отвечали по-русски в английском интерфейсе
  const locale = useLocale()
  // Имена ролей приходят с бэкенда по-русски: переводим по id, а для
  // неизвестного id честно показываем то, что прислал бэкенд.
  const dirName = useCallback(
    (d: { id?: string; name?: string }) => {
      const key = `director_${d.id || ''}`
      return d.id && t.has(key) ? t(key) : (d.name || d.id || '')
    },
    [t],
  )
  const [mode, setMode] = useState<Mode>('board')
  const [directors, setDirectors] = useState<Director[]>([])
  const [people, setPeople] = useState<Person[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // планёрка
  const [question, setQuestion] = useState('')
  const [cast, setCast] = useState<string[]>(['ceo', 'cmo', 'sales', 'cto'])
  const [twinCast, setTwinCast] = useState<string[]>([])
  const [rounds, setRounds] = useState(2)
  const [useBrain, setUseBrain] = useState(true)
  const [board, setBoard] = useState<any>(null)

  // диалоги
  const [dirIds, setDirIds] = useState<string[]>(['ceo'])
  const [dirLog, setDirLog] = useState<{ who: string; text: string }[]>([])
  const [dirQ, setDirQ] = useState('')
  const [twinId, setTwinId] = useState('')
  const [twinLog, setTwinLog] = useState<{ who: string; text: string;
                                           grounding?: Grounding }[]>([])
  const [twinQ, setTwinQ] = useState('')
  const [exportNote, setExportNote] = useState('')

  useEffect(() => {
    (async () => {
      try {
        const r = await authFetch(`${API}/api/v1/boardroom/directors`)
        if (r.ok) setDirectors(((await r.json()) || {}).directors || [])
      } catch { /* каталог не критичен */ }
      if (!userId) return
      try {
        const r = await authFetch(`${API}/api/v1/snapshots/people?user_id=${userId}`)
        if (r.ok) setPeople(((await r.json()) || {}).profiles || [])
      } catch { /* людей может не быть */ }
    })()
  }, [userId])

  const runBoard = useCallback(async () => {
    if (!userId || !question.trim()) return
    setBusy(true); setError(''); setBoard(null)
    try {
      const r = await authFetch(`${API}/api/v1/boardroom/run`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, question, directors: cast,
                               persons: twinCast, rounds, use_brain: useBrain,
                               lang: locale }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') setBoard(d)
      else setError(d.message || d.detail || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, question, cast, twinCast, rounds, useBrain, t])

  const askDirector = useCallback(async () => {
    if (!userId || !dirQ.trim() || dirIds.length === 0) return
    const q = dirQ.trim(); setDirQ(''); setBusy(true); setError('')
    setDirLog((l) => [...l, { who: 'you', text: q }])
    try {
      const r = await authFetch(`${API}/api/v1/boardroom/chat`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, director_ids: dirIds, question: q,
                               history: dirLog, use_brain: useBrain,
                               lang: locale }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success' && d.mode === 'panel') {
        // мини-обсуждение: каждая роль отдельной репликой + итог секретаря
        setDirLog((l) => [
          ...l,
          ...(d.voices || []).map((v: any) => ({
            who: 'director', text: `**${v.icon} ${dirName(v)}:**\n\n${v.answer}` })),
          ...(d.summary ? [{ who: 'director', text: `**📋 ${t('summary_title')}**\n\n${d.summary}` }] : []),
        ])
      } else if (d.status === 'success') {
        setDirLog((l) => [...l, { who: 'director', text: d.answer }])
      } else setError(d.message || d.detail || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, dirIds, dirQ, dirLog, useBrain, t, dirName])

  const askTwin = useCallback(async () => {
    if (!userId || !twinId || !twinQ.trim()) return
    const q = twinQ.trim(); setTwinQ(''); setBusy(true); setError('')
    setTwinLog((l) => [...l, { who: 'you', text: q }])
    try {
      const r = await authFetch(`${API}/api/v1/twin/ask`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, person_id: twinId, question: q,
                               lang: locale }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') {
        setTwinLog((l) => [...l, { who: 'twin', text: d.answer,
                                   grounding: d.grounded_on }])
      } else setError(d.message || d.detail || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, twinId, twinQ, t])

  const exportDataset = useCallback(async () => {
    if (!userId || !twinId) return
    setBusy(true); setError(''); setExportNote('')
    try {
      const r = await authFetch(`${API}/api/v1/twin/training-export`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, person_id: twinId }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success' && d.jsonl) {
        const blob = new Blob([d.jsonl], { type: 'application/jsonl' })
        const a = document.createElement('a')
        a.href = URL.createObjectURL(blob)
        a.download = `twin_${(d.person || 'person').replace(/\s+/g, '_')}.jsonl`
        a.click()
        URL.revokeObjectURL(a.href)
        setExportNote(d.note || t('export_examples', { count: d.examples_count }))
      } else setError(d.message || t('error_export_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, twinId, t])

  const personKey = (p: Person) => p.person_id || p.name || ''

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        {[
          { id: 'board' as Mode, label: `🏛️ ${t('tab_board')}`, icon: <Users className="w-3.5 h-3.5" /> },
          { id: 'director' as Mode, label: `💬 ${t('tab_director')}`, icon: <MessageCircle className="w-3.5 h-3.5" /> },
          { id: 'twin' as Mode, label: `🧬 ${t('tab_twin')}`, icon: <Brain className="w-3.5 h-3.5" /> },
          { id: 'privacy' as Mode, label: `🛡️ ${t('tab_privacy')}`, icon: <ShieldCheck className="w-3.5 h-3.5" /> },
        ].map((m) => (
          <button key={m.id} onClick={() => setMode(m.id)} className={chip(mode === m.id)}>
            {m.label}
          </button>
        ))}
        <label className="ml-auto flex items-center gap-1.5 text-xs text-brain-400">
          <input type="checkbox" checked={useBrain} onChange={(e) => setUseBrain(e.target.checked)} />
          {t('use_brain_label')}
        </label>
      </div>

      {error && <div className="p-2 rounded-lg bg-red-500/10 border border-red-500/30 text-red-300 text-xs">{error}</div>}

      {mode === 'board' && (
        <div className="space-y-3">
          <div className="text-xs text-brain-400">
            {t('intro_board')}
          </div>
          <div className="flex flex-wrap gap-2">
            {directors.map((d) => (
              <button key={d.id}
                onClick={() => setCast((c) => c.includes(d.id) ? c.filter((x) => x !== d.id) : [...c, d.id])}
                className={chip(cast.includes(d.id))}>
                {d.icon} {dirName(d)}
              </button>
            ))}
          </div>
          {people.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {people.slice(0, 20).map((p) => {
                const k = personKey(p)
                return (
                  <button key={k}
                    onClick={() => setTwinCast((c) => c.includes(k) ? c.filter((x) => x !== k) : [...c, k])}
                    className={chip(twinCast.includes(k))}>
                    🧬 {p.name || k}{p.role ? ` · ${p.role}` : ''}
                  </button>
                )
              })}
            </div>
          )}
          <div className="flex gap-2">
            <textarea value={question} onChange={(e) => setQuestion(e.target.value)}
              placeholder={t('placeholder_board_question')}
              rows={2}
              className="flex-1 px-3 py-2 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-sm" />
            <select value={rounds} onChange={(e) => setRounds(Number(e.target.value))}
              className="px-2 rounded-lg bg-brain-900 border border-brain-700 text-brain-200 text-xs">
              <option value={1}>{t('rounds_1')}</option>
              <option value={2}>{t('rounds_2')}</option>
              <option value={3}>{t('rounds_3')}</option>
            </select>
            <button onClick={runBoard} disabled={busy || !question.trim()}
              className="px-4 py-2 rounded-lg bg-purple-600/80 hover:bg-purple-600 text-white text-sm disabled:opacity-40 flex items-center gap-1.5">
              <Play className="w-4 h-4" /> {busy ? t('running_board') : t('btn_run_board')}
            </button>
          </div>

          {board && (
            <div className="space-y-3">
              <div className="rounded-xl border border-purple-500/40 bg-purple-500/5 p-4">
                <Markdown text={board.decision || ''} />
              </div>
              {board.verification && (
                <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-4">
                  <div className="text-xs font-medium text-amber-300 mb-2">🔍 {t('verification_title')}</div>
                  <Markdown text={board.verification} />
                </div>
              )}
              {(board.rounds || []).map((r: any) => (
                <details key={r.round} className="rounded-xl border border-brain-700/60 p-3">
                  <summary className="text-sm text-brain-300 cursor-pointer">{t('round_replies', { round: r.round })}</summary>
                  <div className="mt-2 space-y-3">
                    {Object.entries(r.replies || {}).map(([pid, text]) => {
                      const meta = (board.cast || []).find((c: any) => c.id === pid) || { id: pid }
                      return (
                        <div key={pid} className="rounded-lg bg-brain-900/50 p-3">
                          <div className="text-xs font-medium text-brain-200 mb-1">{meta.icon} {dirName(meta)}</div>
                          <Markdown text={String(text)} />
                        </div>
                      )
                    })}
                  </div>
                </details>
              ))}
              <div className="text-[11px] text-brain-500">{t('disclaimer_board')}</div>
            </div>
          )}
        </div>
      )}

      {mode === 'director' && (
        <div className="space-y-3">
          <div className="text-xs text-brain-400">
            {t('intro_director')}
          </div>
          <div className="flex flex-wrap gap-2">
            {directors.map((d) => (
              <button key={d.id}
                onClick={() => { setDirIds((c) => c.includes(d.id) ? c.filter((x) => x !== d.id) : [...c, d.id]); setDirLog([]) }}
                className={chip(dirIds.includes(d.id))}>
                {d.icon} {dirName(d)}
              </button>
            ))}
          </div>
          <div className="space-y-2 max-h-[45vh] overflow-y-auto">
            {dirLog.map((m, i) => (
              <div key={i} className={`rounded-lg p-3 ${m.who === 'you' ? 'bg-brain-800/60 ml-8' : 'bg-brain-900/60 mr-8 border border-brain-700/50'}`}>
                <Markdown text={m.text} />
              </div>
            ))}
          </div>
          <div className="flex gap-2">
            <input value={dirQ} onChange={(e) => setDirQ(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && !busy && askDirector()}
              placeholder={dirIds.length > 1 ? t('placeholder_director_multi') : t('placeholder_director_single')}
              className="flex-1 px-3 py-2 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-sm" />
            <button onClick={askDirector} disabled={busy || !dirQ.trim()}
              className="px-4 py-2 rounded-lg bg-blue-600/80 hover:bg-blue-600 text-white text-sm disabled:opacity-40">
              {busy ? '…' : t('btn_ask')}
            </button>
          </div>
          <div className="text-[11px] text-brain-500">{t('disclaimer_director')}</div>
        </div>
      )}

      {mode === 'twin' && (
        <div className="space-y-3">
          <div className="text-xs text-brain-400">
            {t('intro_twin')}
          </div>
          <div className="flex flex-wrap gap-2">
            {people.slice(0, 30).map((p) => {
              const k = personKey(p)
              return (
                <button key={k} onClick={() => { setTwinId(k); setTwinLog([]); setExportNote('') }}
                  className={chip(twinId === k)}>
                  🧬 {p.name || k}{p.role ? ` · ${p.role}` : ''}
                </button>
              )
            })}
            {people.length === 0 && (
              <div className="text-xs text-brain-500">{t('twin_empty')}</div>
            )}
          </div>
          {twinId && (
            <>
              <div className="space-y-2 max-h-[40vh] overflow-y-auto">
                {twinLog.map((m, i) => (
                  <div key={i} className={`rounded-lg p-3 ${m.who === 'you' ? 'bg-brain-800/60 ml-8' : 'bg-brain-900/60 mr-8 border border-brain-700/50'}`}>
                    <Markdown text={m.text} />
                    {m.grounding && <Grounded g={m.grounding} label={t('grounded_on')} />}
                  </div>
                ))}
              </div>
              <div className="flex gap-2">
                <input value={twinQ} onChange={(e) => setTwinQ(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && !busy && askTwin()}
                  placeholder={t('placeholder_twin_question')}
                  className="flex-1 px-3 py-2 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-sm" />
                <button onClick={askTwin} disabled={busy || !twinQ.trim()}
                  className="px-4 py-2 rounded-lg bg-blue-600/80 hover:bg-blue-600 text-white text-sm disabled:opacity-40">
                  {busy ? '…' : t('btn_ask')}
                </button>
                <button onClick={exportDataset} disabled={busy}
                  title={t('title_export_dataset')}
                  className="px-3 py-2 rounded-lg border border-brain-600 text-brain-300 hover:text-white text-sm flex items-center gap-1.5">
                  <Download className="w-4 h-4" /> {t('btn_dataset')}
                </button>
              </div>
              {exportNote && <div className="text-[11px] text-emerald-300">{exportNote}</div>}
              <div className="text-[11px] text-brain-500">
                {t('disclaimer_twin')}
              </div>
            </>
          )}
        </div>
      )}

      {mode === 'privacy' && <TwinPrivacyPanel userId={userId} />}
    </div>
  )
}
