'use client'

/**
 * SyncDashboard — секция «Синхронизация компании» (CogniLayer Ф2, шаг 2).
 *
 * Показывает генеральному/руководителю: индекс S(t) с компонентами, тренд,
 * рассинхроны по 4 уровням (внутри отделов / отдел↔отдел / каскад /
 * руководители) и «непокрытые» цели. Кнопка «Просудить спорные» гоняет
 * LLM-судью по soft-парам (?judge=true). Данные — GET /sync/report|/sync/index.
 * Пустые уровни показываются честно («нет данных»), не как идеальная синхр-я.
 */
import { useCallback, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { Radar, Loader2, RefreshCw, Scale, ChevronDown, ChevronRight, GitBranch, Check } from 'lucide-react'

interface Conflict {
  kind?: string; a?: string; b?: string; goal_a?: string; goal_b?: string
  severity?: string; shared?: string[]; grounded?: boolean
  judge?: { verdict: string; reason?: string }
}
interface Report {
  levels: {
    within_departments: { conflicts: Conflict[]; uncovered_dept_goals: Array<{ department: string; goal: string }>; pairs: number }
    cross_department: { conflicts: Conflict[]; pairs: number }
    cascade: { conflicts: Conflict[]; covered: Array<{ goal: string; owners: string[] }>; uncovered: Array<{ goal: string; goal_id?: string }>; pairs: number }
    managers: { conflicts: Conflict[]; pairs: number; managers_detected?: number }
  }
  index: { s: number | null; components: Record<string, number | null>; pairs_total: number }
  judge?: { judged: number; confirmed: number; rejected: number } | null
  inputs?: Record<string, number>
}
interface Proposal { department: string; title: string; rationale?: string; draft_numbers?: boolean; accepted?: boolean }
interface Warning {
  goal_a?: string; goal_b?: string; b?: string; severity?: string
  shared?: string[]; detected_at?: string; meeting_id?: string
}

const LEVEL_KEYS = ['within_departments', 'cross_department', 'cascade', 'managers'] as const

function sColor(s: number | null): string {
  if (s === null) return 'text-brain-500 border-brain-700/40'
  if (s >= 80) return 'text-green-400 border-green-500/40'
  if (s >= 50) return 'text-amber-400 border-amber-500/40'
  return 'text-red-400 border-red-500/40'
}

function Sparkline({ points }: { points: number[] }) {
  if (points.length < 2) return null
  const w = 96; const h = 24
  const min = Math.min(...points); const max = Math.max(...points)
  const span = max - min || 1
  const pts = points.map((p, i) =>
    `${(i / (points.length - 1)) * w},${h - 2 - ((p - min) / span) * (h - 4)}`).join(' ')
  return (
    <svg width={w} height={h} className="opacity-80">
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5"
        className="text-purple-400" />
    </svg>
  )
}

export function SyncDashboardSection() {
  const t = useTranslations('sync_dashboard')
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [judging, setJudging] = useState(false)
  const [report, setReport] = useState<Report | null>(null)
  const [trend, setTrend] = useState<number[]>([])
  const [note, setNote] = useState('')
  // каскад (Ф3): goal_id → предложения отделам; busy — принимаемая цель
  const [proposals, setProposals] = useState<Record<string, Proposal[]>>({})
  const [proposing, setProposing] = useState<string | null>(null)
  const [accepting, setAccepting] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<Warning[]>([])

  const load = useCallback(async (judge: boolean) => {
    judge ? setJudging(true) : setLoading(true)
    setNote('')
    try {
      const r = await authFetch(`/api/v1/sync/report${judge ? '?judge=true' : ''}`)
      if (r.status === 403) { setNote(t('disabled')); return }
      const d = await r.json()
      if (d?.levels) setReport(d)
      try {
        const hr = await authFetch('/api/v1/sync/index?limit=30')
        const hd = await hr.json()
        setTrend(((hd?.history || []) as Array<{ s: number | null }>)
          .map((p) => p.s).filter((s): s is number => typeof s === 'number'))
      } catch { /* тренд опционален */ }
      try {
        const wr = await authFetch('/api/v1/sync/predictive?limit=10')
        const wd = await wr.json()
        setWarnings((wd?.warnings || []) as Warning[])
      } catch { /* ранние сигналы опциональны */ }
    } catch {
      setNote(t('load_failed'))
    } finally {
      setLoading(false); setJudging(false)
    }
  }, [t])

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next && !report) load(false)
  }

  // Ф3: черновики целей отделов для непокрытой цели компании (ничего не пишет)
  const propose = useCallback(async (goalId: string, goalText: string) => {
    setProposing(goalId)
    try {
      const r = await authFetch('/api/v1/sync/cascade/propose', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(goalId ? { goal_id: goalId } : { goal_text: goalText }),
      })
      const d = await r.json()
      setProposals((p) => ({ ...p, [goalId || goalText]: d?.proposals || [] }))
      if (!(d?.proposals || []).length && d?.note) setNote(d.note)
    } catch {
      setNote(t('load_failed'))
    } finally { setProposing(null) }
  }, [t])

  // human gate: цель создаётся ТОЛЬКО по явному «Принять»
  const accept = useCallback(async (key: string, goalId: string, pr: Proposal) => {
    setAccepting(`${key}:${pr.department}`)
    try {
      const r = await authFetch('/api/v1/sync/cascade/accept', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          department: pr.department, title: pr.title,
          company_goal_id: goalId, rationale: pr.rationale || '',
        }),
      })
      const d = await r.json()
      if (d?.ok) {
        setProposals((p) => ({
          ...p,
          [key]: (p[key] || []).map((x) =>
            x.department === pr.department ? { ...x, accepted: true } : x),
        }))
      } else if (d?.error) setNote(d.error)
    } catch {
      setNote(t('load_failed'))
    } finally { setAccepting(null) }
  }, [t])

  const conflicts = (key: typeof LEVEL_KEYS[number]): Conflict[] =>
    report?.levels?.[key]?.conflicts || []

  const pct = (v: number | null | undefined) =>
    v === null || v === undefined ? t('no_data') : `${Math.round(v * 100)}%`

  const s = report?.index?.s ?? null

  return (
    <div className="rounded-xl border border-brain-700/30 bg-brain-950/50 p-3 space-y-2">
      <button onClick={toggle} className="w-full flex items-center gap-2">
        <Radar className="w-4 h-4 text-purple-400" />
        <span className="text-[12px] uppercase tracking-wide text-brain-400 flex-1 text-left">
          {t('title')}
        </span>
        {report && (
          <span className={`px-2 py-0.5 rounded-full border text-xs font-semibold ${sColor(s)}`}>
            {s === null ? t('s_unknown') : `S ${s}`}
          </span>
        )}
        {open ? <ChevronDown className="w-4 h-4 text-brain-500" /> : <ChevronRight className="w-4 h-4 text-brain-500" />}
      </button>

      {open && (
        <div className="space-y-2.5">
          {loading && (
            <div className="flex items-center gap-2 text-xs text-brain-400">
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> {t('loading')}
            </div>
          )}

          {report && (
            <>
              {/* Компоненты индекса + тренд + действия */}
              <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                <span className="px-2 py-0.5 rounded-full bg-brain-800/70 text-brain-300"
                  title={t('alignment_hint')}>
                  {t('alignment')}: {pct(report.index?.components?.alignment)}
                </span>
                <span className="px-2 py-0.5 rounded-full bg-brain-800/70 text-brain-300"
                  title={t('cascade_hint')}>
                  {t('cascade')}: {pct(report.index?.components?.cascade_coverage)}
                </span>
                <span className="px-2 py-0.5 rounded-full bg-brain-800/70 text-brain-300"
                  title={t('pickup_hint')}>
                  {t('pickup')}: {pct(report.index?.components?.dept_pickup)}
                </span>
                {trend.length >= 2 && <Sparkline points={trend} />}
                <span className="flex-1" />
                <button onClick={() => load(false)} disabled={loading || judging}
                  title={t('refresh')}
                  className="p-1 rounded text-brain-500 hover:text-brain-300 disabled:opacity-50">
                  <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
                </button>
                <button onClick={() => load(true)} disabled={loading || judging}
                  title={t('judge_hint')}
                  className="flex items-center gap-1 px-2 py-1 rounded-lg bg-brain-800/70 hover:bg-brain-700 text-brain-200 text-[11px] disabled:opacity-50">
                  {judging ? <Loader2 className="w-3 h-3 animate-spin" /> : <Scale className="w-3 h-3" />}
                  {t('judge_button')}
                </button>
              </div>
              {report.judge && (
                <p className="text-[10.5px] text-brain-500">
                  {t('judge_stats', {
                    judged: report.judge.judged,
                    confirmed: report.judge.confirmed,
                    rejected: report.judge.rejected,
                  })}
                </p>
              )}

              {/* Уровни */}
              {LEVEL_KEYS.map((key) => {
                const cs = conflicts(key)
                const pairs = report.levels?.[key]?.pairs || 0
                return (
                  <div key={key} className="rounded-lg bg-brain-900/60 border border-brain-800/50 p-2 space-y-1">
                    <div className="flex items-center gap-2 text-[11px] text-brain-400">
                      <span className="flex-1">{t(`level_${key}`)}</span>
                      <span className="text-brain-600">
                        {pairs === 0 ? t('level_empty') : t('level_pairs', { pairs, conflicts: cs.length })}
                      </span>
                    </div>
                    {cs.map((c, i) => {
                      const rejected = c.judge?.verdict === 'rejected'
                      return (
                        <div key={i}
                          className={`text-[11px] rounded px-2 py-1 border ${rejected
                            ? 'border-brain-800/40 text-brain-600 line-through'
                            : c.severity === 'high'
                              ? 'border-red-500/30 bg-red-500/5 text-brain-200'
                              : 'border-amber-500/25 bg-amber-500/5 text-brain-300'}`}>
                          <span className={`mr-1.5 px-1 rounded text-[9px] uppercase ${
                            c.severity === 'high' ? 'bg-red-500/20 text-red-300' : 'bg-amber-500/20 text-amber-300'}`}>
                            {c.severity === 'high' ? t('sev_high') : t('sev_soft')}
                          </span>
                          <b>{c.a}</b> ↔ <b>{c.b}</b>: «{(c.goal_a || '').slice(0, 90)}» / «{(c.goal_b || '').slice(0, 90)}»
                          {c.judge && (
                            <span className="block text-[10px] mt-0.5 text-brain-500 no-underline">
                              {c.judge.verdict === 'confirmed' ? t('judge_confirmed') : t('judge_rejected')}
                              {c.judge.reason ? ` — ${c.judge.reason}` : ''}
                            </span>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )
              })}

              {/* Непокрытое: цели компании без владельца + цели отделов без людей */}
              {((report.levels?.cascade?.uncovered?.length || 0) > 0
                || (report.levels?.within_departments?.uncovered_dept_goals?.length || 0) > 0) && (
                <div className="rounded-lg bg-brain-900/60 border border-brain-800/50 p-2 space-y-1">
                  <div className="text-[11px] text-brain-400">{t('uncovered_title')}</div>
                  {(report.levels.cascade.uncovered || []).map((u, i) => {
                    const key = u.goal_id || u.goal
                    const prs = proposals[key]
                    return (
                      <div key={`c${i}`} className="space-y-1">
                        <div className="flex items-center gap-2">
                          <p className="flex-1 text-[11px] text-brain-300">
                            ⚠ {t('uncovered_company', { goal: u.goal })}
                          </p>
                          <button
                            onClick={() => propose(u.goal_id || '', u.goal)}
                            disabled={proposing !== null}
                            title={t('cascade_hint_btn')}
                            className="flex items-center gap-1 px-2 py-0.5 rounded-lg bg-purple-600/70 hover:bg-purple-500 text-white text-[10.5px] disabled:opacity-50">
                            {proposing === (u.goal_id || u.goal)
                              ? <Loader2 className="w-3 h-3 animate-spin" />
                              : <GitBranch className="w-3 h-3" />}
                            {t('cascade_button')}
                          </button>
                        </div>
                        {prs && prs.length === 0 && (
                          <p className="text-[10.5px] text-brain-600 pl-3">{t('cascade_empty')}</p>
                        )}
                        {(prs || []).map((pr) => (
                          <div key={pr.department}
                            className="flex items-start gap-2 pl-3 text-[11px] rounded px-2 py-1 border border-purple-500/20 bg-purple-500/5">
                            <div className="flex-1">
                              <b className="text-purple-300">{pr.department}:</b>{' '}
                              <span className="text-brain-200">{pr.title}</span>
                              {pr.draft_numbers && (
                                <span className="ml-1 px-1 rounded bg-amber-500/20 text-amber-300 text-[9px] uppercase">
                                  {t('draft_numbers')}
                                </span>
                              )}
                              {pr.rationale && (
                                <span className="block text-[10px] text-brain-500">{pr.rationale}</span>
                              )}
                            </div>
                            {pr.accepted ? (
                              <span className="flex items-center gap-1 text-green-400 text-[10.5px]">
                                <Check className="w-3 h-3" /> {t('cascade_accepted')}
                              </span>
                            ) : (
                              <button
                                onClick={() => accept(key, u.goal_id || '', pr)}
                                disabled={accepting !== null}
                                className="px-2 py-0.5 rounded bg-green-600/70 hover:bg-green-500 text-white text-[10.5px] disabled:opacity-50">
                                {accepting === `${key}:${pr.department}`
                                  ? <Loader2 className="w-3 h-3 animate-spin" />
                                  : t('cascade_accept')}
                              </button>
                            )}
                          </div>
                        ))}
                      </div>
                    )
                  })}
                  {(report.levels.within_departments.uncovered_dept_goals || []).map((u, i) => (
                    <p key={`d${i}`} className="text-[11px] text-brain-300">
                      ⚠ {t('uncovered_dept', { department: u.department, goal: u.goal })}
                    </p>
                  ))}
                </div>
              )}

              {/* Ранние сигналы: решения встреч, тянущие против целей (Ф4) */}
              {warnings.length > 0 && (
                <div className="rounded-lg bg-brain-900/60 border border-brain-800/50 p-2 space-y-1">
                  <div className="text-[11px] text-brain-400">{t('predictive_title')}</div>
                  {warnings.map((w, i) => (
                    <div key={i}
                      className={`text-[11px] rounded px-2 py-1 border ${
                        w.severity === 'high'
                          ? 'border-red-500/30 bg-red-500/5 text-brain-200'
                          : 'border-brain-800/40 text-brain-400'}`}>
                      <span className={`mr-1.5 px-1 rounded text-[9px] uppercase ${
                        w.severity === 'high' ? 'bg-red-500/20 text-red-300' : 'bg-brain-800 text-brain-400'}`}>
                        {w.severity === 'high' ? t('sev_high') : t('sev_soft')}
                      </span>
                      {t('predictive_row', { decision: (w.goal_a || '').slice(0, 110), goal: (w.goal_b || '').slice(0, 90) })}
                      {w.detected_at && (
                        <span className="ml-1 text-[10px] text-brain-600">
                          {new Date(w.detected_at).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              )}

              {(report.index?.pairs_total || 0) === 0 && (
                <p className="text-[11px] text-brain-500">{t('empty_hint')}</p>
              )}
            </>
          )}

          {note && <p className="text-[11px] text-brain-500">{note}</p>}
        </div>
      )}
    </div>
  )
}
