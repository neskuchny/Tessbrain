'use client'

import { useState, useEffect, useCallback, Fragment } from 'react'
import { useTranslations } from 'next-intl'
import { RefreshCw, Loader2, AlertCircle, Users, Award, TrendingUp, Copy } from 'lucide-react'

// 360-обзор команды: сравнительная сводка по сотрудникам в зоне отчётов
// (инсайты → 360). Данные — те же PersonSnapshot, что в карточке сотрудника
// (вклад/решения/задачи/встречи/психопрофиль), агрегированные в один экран.

interface Person360 {
  person_id: string
  name: string
  role?: string
  department?: string
  collaboration_score: number
  decisions_made: number
  tasks_completed: number
  tasks_in_progress: number
  meetings: number
  achievements_count: number
  challenges_count: number
  personality_type?: string
  team_role?: string
  strengths?: string[]
  areas_for_improvement?: string[]
}

interface Summary {
  people_count?: number
  avg_collaboration?: number
  total_decisions?: number
  total_tasks_completed?: number
  with_psych_profile?: number
  top_contributors?: string[]
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''

function getAuthHeaders(): Record<string, string> {
  const token =
    typeof window !== 'undefined'
      ? localStorage.getItem('tessent_access_token') || localStorage.getItem('access_token')
      : null
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function scoreColor(v: number): string {
  if (v >= 70) return 'text-emerald-300'
  if (v >= 40) return 'text-cyan-300'
  if (v >= 15) return 'text-amber-300'
  return 'text-slate-400'
}

export default function Team360Panel({ userId }: { userId?: string | null }) {
  const t = useTranslations('team360_panel')
  const [people, setPeople] = useState<Person360[]>([])
  const [summary, setSummary] = useState<Summary>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [sortBy, setSortBy] = useState<keyof Person360>('collaboration_score')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [dupes, setDupes] = useState<Array<{ members: string[]; canonical: string; confidence: number; reason: string; tier: string }> | null>(null)
  const [dupesLoading, setDupesLoading] = useState(false)

  // Превью дублей людей — content-aware кластеры (LLM группирует по имени+
  // активности). Только показ, граф не меняется.
  const loadDupes = useCallback(async () => {
    if (!userId) return
    setDupesLoading(true)
    try {
      const res = await fetch(`${API_BASE}/api/v1/snapshots/duplicate-candidates?user_id=${userId}`, {
        headers: getAuthHeaders(),
      })
      const data = await res.json().catch(() => ({}))
      setDupes(Array.isArray(data.clusters) ? data.clusters : [])
    } catch {
      setDupes([])
    } finally {
      setDupesLoading(false)
    }
  }, [userId])

  const load = useCallback(async () => {
    if (!userId) return
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/api/v1/snapshots/team360?user_id=${userId}&limit=40`, {
        headers: getAuthHeaders(),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok || data.status === 'error') {
        throw new Error(data?.message || t('error_status', { status: res.status }))
      }
      setPeople(Array.isArray(data.people) ? data.people : [])
      setSummary(data.summary || {})
    } catch (e: any) {
      setError(e.message || t('load_error'))
    } finally {
      setLoading(false)
    }
  }, [userId, t])

  useEffect(() => {
    if (!userId) { setLoading(false); return }
    load()
  }, [userId, load])

  const sorted = [...people].sort((a, b) => {
    const av = a[sortBy], bv = b[sortBy]
    if (typeof av === 'number' && typeof bv === 'number') return bv - av
    return String(bv ?? '').localeCompare(String(av ?? ''))
  })

  return (
    <div className="space-y-4">
      {/* Шапка + сводка */}
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <Users className="w-5 h-5 text-brain-400" />
            {t('title')}
          </h2>
          <div className="flex items-center gap-2">
            <button
              onClick={loadDupes}
              disabled={dupesLoading}
              className="px-3 py-1.5 rounded-lg bg-amber-600/20 hover:bg-amber-600/30 text-amber-300 text-xs inline-flex items-center gap-1.5 disabled:opacity-50"
              title={t('show_dupes_title')}
            >
              <Copy className="w-3.5 h-3.5" />
              {dupesLoading ? t('searching') : t('show_dupes')}
            </button>
            <button
              onClick={load}
              className="p-2 rounded-lg bg-brain-800/50 hover:bg-brain-700/50 text-brain-400 hover:text-brain-200 transition-colors"
              title={t('refresh')}
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>
        <p className="text-xs text-brain-500 mb-3">
          {t('description')}
        </p>
        {summary.people_count != null && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <div className="bg-brain-900/40 rounded p-2 text-center">
              <div className="text-lg font-bold text-white">{summary.people_count}</div>
              <div className="text-[11px] text-brain-500">{t('stat_people')}</div>
            </div>
            <div className="bg-brain-900/40 rounded p-2 text-center">
              <div className="text-lg font-bold text-white">{summary.avg_collaboration}</div>
              <div className="text-[11px] text-brain-500">{t('stat_avg_contribution')}</div>
            </div>
            <div className="bg-brain-900/40 rounded p-2 text-center">
              <div className="text-lg font-bold text-white">{summary.total_decisions}</div>
              <div className="text-[11px] text-brain-500">{t('stat_total_decisions')}</div>
            </div>
            <div className="bg-brain-900/40 rounded p-2 text-center">
              <div className="text-lg font-bold text-white">{summary.with_psych_profile}</div>
              <div className="text-[11px] text-brain-500">{t('stat_with_psych')}</div>
            </div>
          </div>
        )}
        {summary.top_contributors && summary.top_contributors.length > 0 && (
          <div className="mt-3 flex items-center gap-2 text-sm text-brain-300">
            <Award className="w-4 h-4 text-amber-400" />
            <span className="text-brain-500">{t('top_contributors')}</span>
            {summary.top_contributors.join(', ')}
          </div>
        )}
      </div>

      {/* Превью дублей (только показ, слияния нет) */}
      {dupes !== null && (
        <div className="card">
          <div className="flex items-center gap-2 mb-2">
            <Copy className="w-4 h-4 text-amber-400" />
            <h3 className="text-sm font-semibold text-brain-100">
              {t('dupes_groups_title', { n: dupes.length })}
            </h3>
          </div>
          {dupes.length === 0 ? (
            <p className="text-xs text-brain-500">
              {t('dupes_empty')}
            </p>
          ) : (
            <>
              <p className="text-[11px] text-brain-500 mb-2">
                {t('dupes_note')} <span className="text-emerald-300">auto</span> — {t('dupes_auto_hint')}; <span className="text-amber-300">review</span> — {t('dupes_review_hint')}.
              </p>
              <div className="space-y-1.5">
                {dupes.map((d, i) => (
                  <div key={i} className="text-xs bg-brain-900/40 rounded px-2 py-1.5">
                    <div className="flex items-center justify-between">
                      <span className="text-brain-100 font-medium">{d.canonical}</span>
                      <span className="flex items-center gap-2">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                          d.tier === 'auto' ? 'bg-emerald-500/20 text-emerald-300'
                          : d.tier === 'review' ? 'bg-amber-500/20 text-amber-300'
                          : 'bg-slate-500/20 text-slate-400'
                        }`}>{d.tier}</span>
                        <span className="font-mono text-brain-400">{d.confidence.toFixed(2)}</span>
                      </span>
                    </div>
                    <div className="text-brain-300 mt-0.5">
                      {d.members.join(' + ')}
                    </div>
                    {d.reason && <div className="text-brain-500 mt-0.5">{d.reason}</div>}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {/* Таблица сравнения */}
      <div className="card">
        {loading ? (
          <div className="flex items-center justify-center py-10 text-brain-400">
            <Loader2 className="w-5 h-5 animate-spin mr-2" />
            {t('loading')}
          </div>
        ) : error ? (
          <div className="flex items-center justify-center py-10 text-red-400">
            <AlertCircle className="w-5 h-5 mr-2" />
            {error}
          </div>
        ) : sorted.length === 0 ? (
          <div className="text-center py-10 text-brain-400">
            {t('empty_state')}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="text-left text-brain-400 border-b border-brain-700/40">
                  <th className="px-2 py-2">{t('col_employee')}</th>
                  {([
                    ['collaboration_score', t('col_contribution')],
                    ['decisions_made', t('col_decisions')],
                    ['tasks_completed', t('col_tasks_done')],
                    ['tasks_in_progress', t('col_in_progress')],
                    ['meetings', t('col_meetings')],
                    ['achievements_count', '🏆'],
                    ['challenges_count', '⚠️'],
                  ] as [keyof Person360, string][]).map(([key, label]) => (
                    <th
                      key={key}
                      onClick={() => setSortBy(key)}
                      className={`px-2 py-2 text-center cursor-pointer hover:text-brain-200 ${sortBy === key ? 'text-cyan-300' : ''}`}
                      title={t('sort_title')}
                    >
                      {label}{sortBy === key ? ' ↓' : ''}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-brain-700/20">
                {sorted.map((p) => (
                  <Fragment key={p.person_id}>
                    <tr
                      onClick={() => setExpanded(expanded === p.person_id ? null : p.person_id)}
                      className="hover:bg-brain-800/30 cursor-pointer"
                    >
                      <td className="px-2 py-2">
                        <div className="text-brain-100 font-medium">{p.name}</div>
                        <div className="text-[11px] text-brain-500">
                          {[p.role, p.department].filter(Boolean).join(' · ')}
                          {p.team_role && <span className="text-brain-600"> · {p.team_role}</span>}
                        </div>
                      </td>
                      <td className={`px-2 py-2 text-center font-mono font-bold ${scoreColor(p.collaboration_score)}`}>
                        {p.collaboration_score}
                      </td>
                      <td className="px-2 py-2 text-center text-brain-200">{p.decisions_made}</td>
                      <td className="px-2 py-2 text-center text-brain-200">{p.tasks_completed}</td>
                      <td className="px-2 py-2 text-center text-brain-200">{p.tasks_in_progress}</td>
                      <td className="px-2 py-2 text-center text-brain-200">{p.meetings}</td>
                      <td className="px-2 py-2 text-center text-emerald-300">{p.achievements_count || ''}</td>
                      <td className="px-2 py-2 text-center text-amber-300">{p.challenges_count || ''}</td>
                    </tr>
                    {expanded === p.person_id && (
                      <tr className="bg-brain-900/40">
                        <td colSpan={8} className="px-4 py-3">
                          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                            {p.personality_type && (
                              <div>
                                <span className="text-brain-500">{t('personality_type')} </span>
                                <span className="text-brain-200">{p.personality_type}</span>
                              </div>
                            )}
                            {(p.strengths?.length ?? 0) > 0 && (
                              <div>
                                <span className="text-brain-500">{t('strengths')} </span>
                                <span className="text-emerald-200">{p.strengths!.join(', ')}</span>
                              </div>
                            )}
                            {(p.areas_for_improvement?.length ?? 0) > 0 && (
                              <div className="sm:col-span-2 flex items-start gap-1">
                                <TrendingUp className="w-3.5 h-3.5 text-blue-400 mt-0.5 shrink-0" />
                                <span className="text-blue-200">{p.areas_for_improvement!.join(' · ')}</span>
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
