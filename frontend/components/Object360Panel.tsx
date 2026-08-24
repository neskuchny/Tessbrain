'use client'

// Объектный 360-браузер («палантир», docs/ru/DATA_ONTOLOGY.md):
// ввёл имя сущности → всё про неё на одном экране: факты с версиями,
// упоминания по источникам, события, связи графа, датасеты.
// Клик по связанной сущности → её 360 (навигация по онтологии).
import { useState, useCallback, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import {
  CircleDot, Search, RefreshCw, Database, GitBranch, MessageSquare,
  Activity, BookOpen, ChevronRight, ListTodo, Users,
} from 'lucide-react'
import Cinema360View from './Cinema360View'

interface Object360PanelProps {
  userId?: string
  /** Открыть карточку сразу для этого имени (переход из графа/оргсхемы). */
  initialName?: string
}

export default function Object360Panel({ userId, initialName }: Object360PanelProps) {
  const t = useTranslations('object360_panel')
  const [name, setName] = useState('')
  const [card, setCard] = useState<any | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [trail, setTrail] = useState<string[]>([])
  const [cinema, setCinema] = useState(false)

  const headers = useCallback((): Record<string, string> => {
    const h: Record<string, string> = {}
    if (typeof window !== 'undefined') {
      const token = localStorage.getItem('tessent_access_token')
      if (token) h['Authorization'] = `Bearer ${token}`
    }
    return h
  }, [])

  // Бэкенд на 500 отдаёт plain-text «Internal Server Error», а не JSON.
  // res.json() на нём падал с «Unexpected token 'I'…» — ловим тело как текст
  // и пытаемся распарсить; если не JSON, отдаём понятную ошибку по статусу.
  async function safeJson(res: Response): Promise<any> {
    const text = await res.text()
    try {
      return JSON.parse(text)
    } catch {
      return {
        success: false,
        error: res.ok ? t('invalid_server_response') : t('server_error', { status: res.status }),
      }
    }
  }

  async function load(entity: string, pushTrail = true) {
    const q = entity.trim()
    if (!q) return
    setLoading(true)
    setError('')
    try {
      const userParam = userId ? `&user_id=${userId}` : ''
      const res = await fetch(
        `/api/v1/ontology/object?name=${encodeURIComponent(q)}${userParam}`,
        { headers: headers() })
      const data = await safeJson(res)
      if (res.status === 403) {
        setError(data.detail || t('disabled_hint'))
      } else if (data.success === false) {
        setError(data.error || 'error')
      } else {
        setCard(data)
        setName(q)
        if (pushTrail) setTrail(prev => [...prev.filter(x => x !== q), q].slice(-6))
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  // Переход из графа/оргсхемы: сразу открываем карточку по имени.
  useEffect(() => {
    if (initialName && initialName.trim()) load(initialName)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialName])

  const fmtKey = (k: string) => k.split('::').slice(1).join(' · ') || k

  // Expert finding (идея Glean): «кто в компании знает про тему»
  const [expertTopic, setExpertTopic] = useState('')
  const [experts, setExperts] = useState<any | null>(null)
  const [expertsLoading, setExpertsLoading] = useState(false)

  async function findExperts() {
    const q = expertTopic.trim()
    if (!q) return
    setExpertsLoading(true)
    setExperts(null)
    try {
      const userParam = userId ? `&user_id=${userId}` : ''
      const res = await fetch(
        `/api/v1/ontology/experts?topic=${encodeURIComponent(q)}${userParam}`,
        { headers: headers() })
      setExperts(await safeJson(res))
    } catch (e) {
      setExperts({ success: false, error: (e as Error).message })
    } finally {
      setExpertsLoading(false)
    }
  }

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center gap-2">
        <CircleDot className="w-5 h-5 text-indigo-400" />
        <h2 className="text-lg font-semibold">{t('title')}</h2>
        <span className="text-xs text-slate-400">{t('subtitle')}</span>
      </div>

      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-md">
          <Search className="w-4 h-4 absolute left-3 top-2.5 text-slate-500" />
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && load(name)}
            placeholder={t('search_placeholder')}
            className="w-full pl-9 pr-3 py-2 rounded-lg bg-brain-900/40 border border-brain-600/30 text-sm"
          />
        </div>
        <button
          onClick={() => load(name)}
          disabled={loading || !name.trim()}
          className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-sm font-medium"
        >
          {loading ? <RefreshCw className="w-4 h-4 animate-spin" /> : t('open_button')}
        </button>
        {/* Кино-режим: витринный 3D-вид того же объекта (список остаётся
            рабочим видом по умолчанию) */}
        {card?.found && (
          <button
            onClick={() => setCinema((v) => !v)}
            title={t('cinema_toggle_title')}
            className={`px-3 py-2 rounded-lg border text-sm font-medium transition-colors ${
              cinema
                ? 'border-amber-400/60 bg-amber-500/15 text-amber-300'
                : 'border-brain-600/40 bg-brain-800/40 text-brain-300 hover:text-amber-300'
            }`}
          >
            🎞
          </button>
        )}
      </div>

      {/* хлебные крошки навигации по онтологии */}
      {trail.length > 1 && (
        <div className="flex items-center gap-1 flex-wrap text-[11px] text-slate-500">
          {trail.map((x, i) => (
            <span key={x} className="flex items-center gap-1">
              {i > 0 && <ChevronRight className="w-3 h-3" />}
              <button onClick={() => load(x, false)} className="hover:text-gray-200">{x}</button>
            </span>
          ))}
        </div>
      )}

      {/* Кто знает про тему (expert finding) */}
      <div className="p-3 rounded-xl border border-brain-600/30 bg-brain-800/20 space-y-2">
        <div className="flex items-center gap-2">
          <Users className="w-4 h-4 text-emerald-400" />
          <span className="text-sm font-medium">{t('experts_title')}</span>
          <input
            value={expertTopic}
            onChange={e => setExpertTopic(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && findExperts()}
            placeholder={t('experts_placeholder')}
            className="flex-1 max-w-sm px-3 py-1.5 rounded-lg bg-brain-900/40 border border-brain-600/30 text-xs"
          />
          <button
            onClick={findExperts}
            disabled={expertsLoading || !expertTopic.trim()}
            className="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-xs font-medium"
          >
            {expertsLoading ? '…' : t('experts_button')}
          </button>
        </div>
        {experts && (
          (experts.experts || []).length > 0 ? (
            <div className="space-y-1.5">
              {experts.experts.map((ex: any) => (
                <div key={ex.name} className="flex items-start gap-2 text-xs">
                  <button
                    onClick={() => load(ex.name)}
                    className="px-1.5 py-0.5 rounded border border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10"
                  >
                    {ex.name}
                  </button>
                  <span className="text-slate-400">{(ex.evidence || []).join('; ')}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-[11px] text-slate-500">{experts.honest_note || experts.error}</p>
          )
        )}
      </div>

      {error && (
        <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-300 text-sm">{error}</div>
      )}

      {card && !card.found && (
        <p className="text-sm text-slate-500">{t('not_found', { name: card.name })}</p>
      )}

      {/* Кино-режим: витринный 3D-вид (кольцо связей вокруг фокуса). Рабочие
          секции ниже остаются — это второй взгляд, не замена. */}
      {card && card.found && cinema && (
        <Cinema360View
          card={card}
          onNavigate={(n) => load(n)}
          fetchCard={async (n: string) => {
            try {
              const userParam = userId ? `&user_id=${userId}` : ''
              const res = await fetch(
                `/api/v1/ontology/object?name=${encodeURIComponent(n)}${userParam}`,
                { headers: headers() })
              const data = await res.json()
              return data?.found ? data : null
            } catch { return null }
          }}
        />
      )}

      {/* Вердикт из PersonSnapshot — первым, на всю ширину: кто это и что
          важно, до фактов/связей (единый источник с карточкой «Сотрудник») */}
      {card && card.found && card.verdict && (card.verdict.summary || card.verdict.role) && (
        <div className="p-4 rounded-xl border border-cyan-500/25 bg-gradient-to-br from-brain-800/50 to-cyan-900/15 mb-3">
          <div className="flex items-center gap-2 text-cyan-300 text-xs font-semibold mb-1.5">
            🧠 {t('verdict_title')}
            {card.verdict.role && <span className="text-slate-400 font-normal">· {card.verdict.role}</span>}
            {card.verdict.department && <span className="text-slate-500 font-normal">· {card.verdict.department}</span>}
          </div>
          {card.verdict.summary && (
            <p className="text-slate-200 text-sm whitespace-pre-wrap mb-2">{card.verdict.summary}</p>
          )}
          {card.verdict.evolution && (
            <p className="text-purple-200/90 text-xs whitespace-pre-wrap mb-2 border-l-2 border-purple-400/40 pl-2">
              🧬 {t('verdict_evolution', { text: card.verdict.evolution })}
            </p>
          )}
          <div className="flex flex-wrap gap-3 text-xs text-slate-400">
            {card.verdict.tasks_in_progress > 0 && <span>⏳ {t('verdict_in_progress', { n: card.verdict.tasks_in_progress })}</span>}
            {card.verdict.tasks_completed_week > 0 && <span>✅ {t('verdict_completed_week', { n: card.verdict.tasks_completed_week })}</span>}
            {card.verdict.meetings > 0 && <span>💬 {t('verdict_meetings', { n: card.verdict.meetings })}</span>}
            {(card.verdict.achievements || []).map((a: any, i: number) => (
              <span key={i} className="text-emerald-300/80">🏆 {typeof a === 'string' ? a : a?.title || ''}</span>
            ))}
          </div>
        </div>
      )}

      {card && card.found && (
        <div className="grid md:grid-cols-2 gap-3">
          {/* Граф: кто это + связи */}
          {card.graph && (
            <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-2">
              <div className="flex items-center gap-2 text-sm font-medium">
                <GitBranch className="w-4 h-4 text-indigo-400" />{t('section_graph')}
              </div>
              {Object.entries(card.graph.node || {}).map(([k, v]) => (
                <p key={k} className="text-xs text-gray-300"><span className="text-slate-500">{k}:</span> {String(v)}</p>
              ))}
              {Object.entries(card.graph.neighbors || {}).map(([label, items]: [string, any]) => {
                // Человекочитаемые подписи типов связей (атрибуция/эволюция/оргсвязи)
                const relLabel = (it: any): string => {
                  const m: Record<string, string> = {
                    DECIDED: 'rel_decided', SUPERSEDES: 'rel_supersedes', REPORTS_TO: 'rel_reports_to',
                    MEMBER_OF: 'rel_member_of', LEADS: 'rel_leads', ASSIGNED_TO: 'rel_assigned_to',
                    PROPOSED: 'rel_proposed', EXPRESSED: 'rel_expressed', HAS_PROFILE: 'rel_has_profile',
                    INVOLVED_IN: 'rel_involved_in', HAS_CONTRADICTION: 'rel_has_contradiction',
                  };
                  if (!it.rel_type) return "";
                  const base = m[it.rel_type] ? t(m[it.rel_type]) : it.rel_type;
                  // для входящих DECIDED/REPORTS_TO смысл инвертируется
                  return it.direction === "in" && it.rel_type === "DECIDED" ? t('rel_decided_incoming') : base;
                };
                return (
                <div key={label}>
                  <p className="text-[11px] text-slate-500 uppercase mt-1">{label} · {items.length}</p>
                  <div className="flex flex-col gap-1 mt-0.5">
                    {items.map((it: any, i: number) => (
                      <div key={i} className="flex items-center gap-1.5 flex-wrap">
                        <button
                          onClick={() => (it.name || it.title) && load(it.name || it.title)}
                          className="text-[11px] px-1.5 py-0.5 rounded border border-brain-600/40 text-gray-300 hover:border-indigo-400 hover:text-indigo-300 text-left"
                        >
                          {it.name || it.title}
                        </button>
                        {it.rel_type && relLabel(it) && (
                          <span className="text-[10px] text-indigo-300/70">· {relLabel(it)}</span>
                        )}
                        {it.decision_maker && (
                          <span className="text-[10px] text-emerald-300/80">👤 {it.decision_maker}</span>
                        )}
                        {it.needs_human_review && (
                          <span className="text-[10px] text-amber-300 bg-amber-500/10 px-1 rounded" title={t('needs_review_hint')}>⚠ {t('needs_review')}</span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              );})}
            </div>
          )}

          {/* Психологический профиль (богатые поля, раньше был виден только узел) */}
          {(() => {
            const psych = Object.values(card.graph?.neighbors || {})
              .flat()
              .filter((it: any) => it && it.details);
            if (!psych.length) return null;
            return (
              <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-2 md:col-span-2">
                <div className="flex items-center gap-2 text-sm font-medium">🧠 {t('section_psych_profile')}</div>
                {psych.map((it: any, i: number) => {
                  const d = it.details || {};
                  const Row = ({ label, value }: { label: string; value: any }) => {
                    const v = Array.isArray(value) ? value.filter(Boolean).join(", ") : value;
                    if (!v) return null;
                    return <p className="text-xs text-gray-300"><span className="text-slate-500">{label}:</span> {v}</p>;
                  };
                  return (
                    <div key={i} className="space-y-1 border-t border-brain-600/20 pt-2 first:border-0 first:pt-0">
                      {it.name && <p className="text-xs text-indigo-300 font-medium">{it.name}</p>}
                      <Row label={t('psych_personality_type')} value={d.personality_type} />
                      <Row label={t('psych_team_role')} value={d.team_role} />
                      <Row label={t('psych_leadership_style')} value={d.leadership_style} />
                      <Row label={t('psych_communication_style')} value={d.communication_style} />
                      <Row label={t('psych_dominant_traits')} value={d.dominant_traits} />
                      <Row label={t('psych_strengths')} value={d.strengths} />
                      <Row label={t('psych_motivators')} value={d.motivation_drivers} />
                    </div>
                  );
                })}
              </div>
            );
          })()}

          {/* Факты с версиями */}
          {card.facts && (
            <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-1.5">
              <div className="flex items-center gap-2 text-sm font-medium">
                <BookOpen className="w-4 h-4 text-green-400" />{t('section_facts')}
              </div>
              {card.facts.map((f: any) => (
                <p key={f.key} className="text-xs text-gray-300">
                  <span className="text-slate-500">{fmtKey(f.key)}:</span> {f.current}
                  {f.versions > 1 && (
                    <span className="ml-1 text-[10px] text-amber-400" title={t('versions_hint')}>
                      v{f.versions}
                    </span>
                  )}
                </p>
              ))}
            </div>
          )}

          {/* Задачи (со статусами) */}
          {card.tasks && (
            <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-1.5">
              <div className="flex items-center gap-2 text-sm font-medium">
                <ListTodo className="w-4 h-4 text-violet-400" />{t('section_tasks')}
              </div>
              {card.tasks.map((tk: any, i: number) => (
                <p key={i} className="text-xs text-gray-300 flex items-center gap-2 flex-wrap">
                  <span className={
                    'text-[10px] px-1.5 py-0.5 rounded ' +
                    (String(tk.status).toLowerCase().includes('done') || String(tk.status).toLowerCase().includes('complet')
                      ? 'bg-green-500/15 text-green-300'
                      : String(tk.status).toLowerCase().includes('block')
                        ? 'bg-red-500/15 text-red-300'
                        : 'bg-violet-500/15 text-violet-300')
                  }>{tk.status}</span>
                  {tk.title}
                  {tk.source && tk.source !== 'graph' && (
                    <span className="text-[9px] px-1 py-0.5 rounded bg-brain-700/40 text-gray-300 uppercase" title={t('task_source_hint')}>
                      {tk.source}
                    </span>
                  )}
                  {tk.deadline && <span className="text-[10px] text-slate-500">{t('task_deadline', { date: String(tk.deadline).slice(0, 10) })}</span>}
                </p>
              ))}
            </div>
          )}

          {/* Упоминания */}
          {card.mentions && (
            <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-1.5">
              <div className="flex items-center gap-2 text-sm font-medium">
                <MessageSquare className="w-4 h-4 text-cyan-400" />{t('section_mentions')}
              </div>
              <div className="flex gap-2 flex-wrap">
                {Object.entries(card.mentions.by_source || {}).map(([src, n]) => (
                  <span key={src} className="text-[11px] px-2 py-0.5 rounded bg-cyan-500/10 text-cyan-300 border border-cyan-500/20">
                    {src}: {String(n)}
                  </span>
                ))}
              </div>
              {(card.mentions.recent || []).slice(-5).map((m: any, i: number) => (
                <p key={i} className="text-[11px] text-slate-500">
                  {m.source_type} · {m.source_id} {m.at ? `· ${String(m.at).slice(0, 10)}` : ''}
                </p>
              ))}
            </div>
          )}

          {/* События */}
          {card.events && (
            <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-1.5">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Activity className="w-4 h-4 text-amber-400" />{t('section_events')}
              </div>
              <div className="flex gap-2 flex-wrap">
                {Object.entries(card.events.kinds || {}).map(([k, n]) => (
                  <span key={k} className="text-[11px] px-2 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/20">
                    {k}: {String(n)}
                  </span>
                ))}
              </div>
              {(card.events.recent || []).slice(-5).map((ev: any, i: number) => (
                <p key={i} className="text-[11px] text-slate-500">
                  {ev.kind} {ev.at ? `· ${String(ev.at).slice(0, 10)}` : ''}
                  {ev.payload?.t ? ` — ${ev.payload.t}` : ''}
                </p>
              ))}
            </div>
          )}

          {/* Датасеты */}
          {card.datasets && (
            <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-1.5">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Database className="w-4 h-4 text-pink-400" />{t('section_datasets')}
              </div>
              {card.datasets.map((d: any, i: number) => (
                <div key={i} className="text-xs text-gray-300">
                  <p>
                    {d.title} <span className="text-slate-500">· {t('dataset_column', { column: d.column })}</span>
                    {typeof d.rows === 'number' && (
                      <span className="ml-1 text-[10px] text-slate-500">· {t('dataset_rows', { count: d.rows })}</span>
                    )}
                  </p>
                  {d.totals && (
                    <div className="flex gap-2 flex-wrap mt-0.5">
                      {Object.entries(d.totals).map(([col, v]) => (
                        <span key={col} className="text-[11px] px-1.5 py-0.5 rounded bg-pink-500/10 text-pink-300 border border-pink-500/20">
                          {col}: {Number(v).toLocaleString('ru-RU')}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
              <p className="text-[10px] text-slate-500">{t('dataset_hint')}</p>
            </div>
          )}

          {/* Daily Pulse: операционка (стендапы, блокеры, сигналы) */}
          {card.daily_pulse && (
            <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-1.5 md:col-span-2">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Activity className="w-4 h-4 text-orange-400" />{t('section_daily_pulse')}
              </div>
              {(card.daily_pulse.blockers || []).length > 0 && (
                <div>
                  <p className="text-[11px] text-slate-500 uppercase">{t('dp_blockers')}</p>
                  {card.daily_pulse.blockers.map((b: any, i: number) => (
                    <p key={i} className="text-xs text-gray-300 flex items-center gap-2 flex-wrap">
                      <span className={
                        'text-[10px] px-1.5 py-0.5 rounded ' +
                        (String(b.severity).toLowerCase().includes('high') || String(b.severity).toLowerCase().includes('crit')
                          ? 'bg-red-500/15 text-red-300' : 'bg-amber-500/15 text-amber-300')
                      }>{b.severity || 'medium'}</span>
                      {b.task} <span className="text-slate-500">— {b.reason}</span>
                      {b.days_open != null && <span className="text-[10px] text-slate-500">{t('dp_days_open', { days: b.days_open })}</span>}
                    </p>
                  ))}
                </div>
              )}
              {(card.daily_pulse.signals || []).length > 0 && (
                <div>
                  <p className="text-[11px] text-slate-500 uppercase">{t('dp_signals')}</p>
                  <div className="flex gap-1.5 flex-wrap">
                    {card.daily_pulse.signals.map((s: any, i: number) => (
                      <span key={i} className={
                        'text-[11px] px-1.5 py-0.5 rounded border ' +
                        (s.signal === 'done' ? 'border-green-500/30 text-green-300'
                          : s.signal === 'blocked' ? 'border-red-500/30 text-red-300'
                          : 'border-brain-600/40 text-gray-300')
                      } title={s.evidence || ''}>{s.signal}: {s.task}</span>
                    ))}
                  </div>
                </div>
              )}
              {card.daily_pulse.weekly_report?.metrics && Object.keys(card.daily_pulse.weekly_report.metrics).length > 0 && (
                <div>
                  <p className="text-[11px] text-slate-500 uppercase">{t('dp_weekly')}</p>
                  <div className="flex gap-2 flex-wrap">
                    {Object.entries(card.daily_pulse.weekly_report.metrics).map(([k, v]) => (
                      <span key={k} className="text-[11px] px-1.5 py-0.5 rounded bg-orange-500/10 text-orange-300 border border-orange-500/20">
                        {k}: {String(v)}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              <p className="text-[10px] text-slate-500">{t('dp_note')}</p>
            </div>
          )}

          {/* CallInsight: работа с клиентами (жалобы, сделки к спасению, coach) */}
          {card.callcenter && (
            <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-1.5 md:col-span-2">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Activity className="w-4 h-4 text-cyan-400" />{t('section_callcenter')}
              </div>
              {(card.callcenter.complaints || []).length > 0 && (
                <div>
                  <p className="text-[11px] text-slate-500 uppercase">{t('cc_complaints')}</p>
                  {card.callcenter.complaints.map((c: any, i: number) => (
                    <p key={i} className="text-xs text-gray-300 flex items-center gap-2 flex-wrap">
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/15 text-red-300">{String(c.at || '').slice(0, 10)}</span>
                      <span className="text-slate-400">«{c.excerpt || c.marker}»</span>
                      {c.deep_link && (
                        <a href={c.deep_link} target="_blank" rel="noreferrer" className="text-cyan-400 hover:text-cyan-300 text-[10px]">{t('cc_open_call')}</a>
                      )}
                    </p>
                  ))}
                </div>
              )}
              {(card.callcenter.at_risk || []).length > 0 && (
                <div>
                  <p className="text-[11px] text-slate-500 uppercase">{t('cc_at_risk')}</p>
                  {card.callcenter.at_risk.map((d: any, i: number) => (
                    <p key={i} className="text-xs text-gray-300">
                      {d.customer_phone || '?'} <span className="text-slate-500">— {d.key_insight}</span>
                    </p>
                  ))}
                </div>
              )}
              {(card.callcenter.coach_advice || []).length > 0 && (
                <div>
                  <p className="text-[11px] text-slate-500 uppercase">{t('cc_coach')}</p>
                  <div className="flex gap-1.5 flex-wrap">
                    {card.callcenter.coach_advice.map((a: any, i: number) => (
                      <span key={i} className="text-[11px] px-1.5 py-0.5 rounded border border-cyan-500/30 text-cyan-300" title={a.focus_metric || ''}>{a.title}</span>
                    ))}
                  </div>
                </div>
              )}
              {card.callcenter.coach_outcomes && (card.callcenter.coach_outcomes.worked + card.callcenter.coach_outcomes.worsened) > 0 && (
                <p className="text-xs text-gray-300">
                  {t('cc_outcomes', { worked: card.callcenter.coach_outcomes.worked, worsened: card.callcenter.coach_outcomes.worsened })}
                </p>
              )}
              <p className="text-[10px] text-slate-500">{t('cc_note')}</p>
            </div>
          )}
        </div>
      )}

      {card && card.found && (
        <p className="text-[11px] text-slate-500">
          {t('sources_note', { sources: (card.sources_present || []).join(', ') })}
        </p>
      )}
    </div>
  )
}
