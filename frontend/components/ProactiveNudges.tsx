'use client'

/**
 * ProactiveNudges — одна ненавязчивая карточка-подсказка на главном экране.
 *
 * Цель (по задумке владельца): система сама рассказывает, чем полезна, и
 * подталкивает пользоваться эффективнее. Держим ПРОСТО и не мешаем: одна
 * карточка за раз, можно скрыть (запоминается в localStorage), «ещё совет»
 * листает. Правила — курируемый список (high-precision онбординг), без
 * тяжёлого движка; персонализация профилем — позже.
 */
import { useEffect, useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { X, ArrowRight, Lightbulb, RotateCw, Sparkles, Wand2, Check } from 'lucide-react'
import { authHeaders } from '@/lib/authFetch'

type NavTarget = { tab: string; subTab?: string }
interface Nudge {
  id: string
  emoji: string
  title: string
  text: string
  cta: string
  nav?: NavTarget
  guide?: boolean
}

const LS_KEY = 'tessent_nudges_dismissed'

function loadDismissed(): Set<string> {
  try {
    if (typeof window === 'undefined') return new Set()
    return new Set(JSON.parse(localStorage.getItem(LS_KEY) || '[]'))
  } catch { return new Set() }
}

interface Advice { advice: string; personalized: boolean; tools: { slug: string; title: string }[] }
interface Props {
  onNavigate: (t: any) => void
  onOpenGuide?: () => void
  userId?: string
}

// section → верхняя вкладка (для чипов-инструментов персонального совета)
const SECTION_TAB: Record<string, string> = {
  'getting-started': 'home', knowledge: 'knowledge', sync: 'sync',
  automations: 'automations', sima: 'sima', board: 'board', concepts: 'home',
}

interface Proposal {
  id: string
  title: string
  problem?: string
  proposedAction?: string
  expectedBenefit?: string
  source?: string
}

export default function ProactiveNudges({ onNavigate, onOpenGuide, userId }: Props) {
  const t = useTranslations('proactive_nudges')
  const nudgesList = useMemo<Nudge[]>(() => [
    { id: 'ask-guide', emoji: '💡', title: t('nudge_ask_guide_title'),
      text: t('nudge_ask_guide_text'),
      cta: t('nudge_ask_guide_cta'), guide: true },
    { id: 'connect-meetings', emoji: '🔌', title: t('nudge_connect_meetings_title'),
      text: t('nudge_connect_meetings_text'),
      cta: t('nudge_connect_meetings_cta'), nav: { tab: 'sync' } },
    { id: 'market-digest', emoji: '📈', title: t('nudge_market_digest_title'),
      text: t('nudge_market_digest_text'),
      cta: t('nudge_market_digest_cta'), nav: { tab: 'automations', subTab: 'classic' } },
    { id: 'meeting-docs', emoji: '📄', title: t('nudge_meeting_docs_title'),
      text: t('nudge_meeting_docs_text'),
      cta: t('nudge_meeting_docs_cta'), nav: { tab: 'knowledge' } },
    { id: 'ask-brain', emoji: '🧠', title: t('nudge_ask_brain_title'),
      text: t('nudge_ask_brain_text'),
      cta: t('nudge_ask_brain_cta'), guide: true },
  ], [t])
  const [dismissed, setDismissed] = useState<Set<string>>(() => loadDismissed())
  const [idx, setIdx] = useState(0)
  const [advice, setAdvice] = useState<Advice | null>(null)
  const [adviceDismissed, setAdviceDismissed] = useState(false)
  const [proposals, setProposals] = useState<Proposal[]>([])
  const [busyId, setBusyId] = useState<string | null>(null)
  const [detecting, setDetecting] = useState(false)
  const [detectMsg, setDetectMsg] = useState<string | null>(null)

  // Предложения автоматизации (SIMA P2): Tessent сам предлагает
  // автоматизировать процесс. «Принять» → формирует ТЗ и заводит проект в SIMA.
  useEffect(() => {
    if (!userId) return
    let alive = true
    ;(async () => {
      try {
        const r = await fetch(`/api/v1/automations/proposals?user_id=${encodeURIComponent(userId)}&status=pending`, { headers: authHeaders() })
        if (!r.ok) return
        const d = await r.json()
        if (alive && Array.isArray(d?.proposals)) setProposals(d.proposals)
      } catch { /* ignore */ }
    })()
    return () => { alive = false }
  }, [userId])

  const acceptProposal = async (p: Proposal) => {
    if (!userId || busyId) return
    setBusyId(p.id)
    try {
      const r = await fetch(`/api/v1/automations/proposals/${p.id}/accept?user_id=${encodeURIComponent(userId)}`,
        { method: 'POST', headers: { ...authHeaders(), 'Content-Type': 'application/json' } })
      const d = await r.json().catch(() => ({}))
      if (r.ok && !d?.error) {
        setProposals((prev) => prev.filter((x) => x.id !== p.id))
        onNavigate({ tab: 'sima' }) // проект уже создан в SIMA — открываем вкладку
      }
    } catch { /* ignore */ } finally { setBusyId(null) }
  }

  const dismissProposal = async (p: Proposal) => {
    if (!userId || busyId) return
    setBusyId(p.id)
    try {
      await fetch(`/api/v1/automations/proposals/${p.id}/dismiss?user_id=${encodeURIComponent(userId)}`,
        { method: 'POST', headers: { ...authHeaders(), 'Content-Type': 'application/json' } })
      setProposals((prev) => prev.filter((x) => x.id !== p.id))
    } catch { /* ignore */ } finally { setBusyId(null) }
  }

  // Звено A по требованию: попросить Tessent найти, что стоит автоматизировать.
  const runDetect = async () => {
    if (!userId || detecting) return
    setDetecting(true); setDetectMsg(null)
    try {
      const r = await fetch(`/api/v1/automations/proposals/detect?user_id=${encodeURIComponent(userId)}`,
        { method: 'POST', headers: { ...authHeaders(), 'Content-Type': 'application/json' } })
      const d = await r.json().catch(() => ({}))
      const created: Proposal[] = Array.isArray(d?.created) ? d.created : []
      if (created.length) {
        setProposals((prev) => [...created, ...prev.filter((x) => !created.some((c) => c.id === x.id))])
      } else {
        setDetectMsg(t('detect_no_candidates'))
      }
    } catch { setDetectMsg(t('detect_failed')) }
    finally { setDetecting(false) }
  }

  // Персональный совет (по профилю+корпусу): грузим НЕ ЧАЩЕ раза в неделю
  // (кеш по возрасту), чтобы не спамить и не тратить токены.
  useEffect(() => {
    if (!userId) return
    const key = `tessent_advice_${userId}`
    const WEEK = 7 * 24 * 60 * 60 * 1000
    try {
      const cached = localStorage.getItem(key)
      if (cached) {
        const parsed = JSON.parse(cached)
        if (parsed?.ts && (Date.now() - parsed.ts) < WEEK && parsed?.data?.advice) {
          setAdvice(parsed.data); return
        }
      }
    } catch { /* ignore */ }
    ;(async () => {
      try {
        const r = await fetch(`/api/v1/guide/advice?user_id=${encodeURIComponent(userId)}`, { headers: authHeaders() })
        if (!r.ok) return
        const d: Advice = await r.json()
        if (d?.advice && d?.personalized) {
          setAdvice(d)
          try { localStorage.setItem(key, JSON.stringify({ ts: Date.now(), data: d })) } catch { /* ignore */ }
        }
      } catch { /* ignore */ }
    })()
  }, [userId])

  const active = useMemo(() => nudgesList.filter((n) => !dismissed.has(n.id)), [nudgesList, dismissed])
  const showAdvice = advice && advice.advice && !adviceDismissed

  if (!showAdvice && active.length === 0 && proposals.length === 0) return null
  const nudge = active.length ? active[idx % active.length] : null

  const persist = (next: Set<string>) => {
    setDismissed(new Set(next))
    try { localStorage.setItem(LS_KEY, JSON.stringify([...next])) } catch { /* ignore */ }
  }
  const dismiss = () => { if (!nudge) return; const n = new Set(dismissed); n.add(nudge.id); persist(n) }
  const next = () => setIdx((i) => (i + 1) % Math.max(1, active.length))
  const act = () => {
    if (!nudge) return
    if (nudge.guide) onOpenGuide?.()
    else if (nudge.nav) onNavigate(nudge.nav)
  }

  return (
    <div className="space-y-3">
      {/* Предложения автоматизации (SIMA P2) — самое действенное, сверху */}
      {proposals.map((p) => (
        <div key={p.id} className="rounded-2xl border border-teal-500/30 bg-gradient-to-br from-teal-600/15 to-brain-800/40 p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-start gap-3 min-w-0">
              <Wand2 className="w-5 h-5 text-teal-300 shrink-0 mt-0.5" />
              <div className="min-w-0">
                <span className="text-[11px] uppercase tracking-wide text-teal-300/80">{t('proposal_badge')}</span>
                <div className="text-sm font-semibold text-white mt-0.5">{p.title}</div>
                {p.problem && <p className="text-sm text-brain-300 mt-0.5">{p.problem}</p>}
                {p.proposedAction && <p className="text-[13px] text-brain-200 mt-1"><span className="text-teal-300/80">{t('proposal_action_label')}{' '}</span>{p.proposedAction}</p>}
                {p.expectedBenefit && <p className="text-[13px] text-brain-200 mt-0.5"><span className="text-teal-300/80">{t('proposal_benefit_label')}{' '}</span>{p.expectedBenefit}</p>}
                <div className="flex items-center gap-3 mt-2.5">
                  <button onClick={() => acceptProposal(p)} disabled={busyId === p.id}
                    className="inline-flex items-center gap-1 text-sm font-medium px-3 py-1 rounded-lg bg-teal-500/90 hover:bg-teal-400 text-white disabled:opacity-50">
                    <Check className="w-3.5 h-3.5" /> {busyId === p.id ? t('preparing_spec') : t('accept_to_sima')}
                  </button>
                  <button onClick={() => dismissProposal(p)} disabled={busyId === p.id}
                    className="text-xs text-brain-500 hover:text-brain-300 disabled:opacity-50">
                    {t('decline')}
                  </button>
                </div>
                <p className="text-[10.5px] text-brain-500 mt-1.5">{t('proposal_footnote')}</p>
              </div>
            </div>
            <button onClick={() => dismissProposal(p)} title={t('decline')} disabled={busyId === p.id}
              className="p-1 rounded-lg text-brain-500 hover:bg-brain-700 hover:text-brain-300 shrink-0 disabled:opacity-50">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      ))}

      {/* Звено A по требованию: «найди, что автоматизировать» */}
      <div className="flex items-center gap-2">
        <button onClick={runDetect} disabled={detecting}
          className="inline-flex items-center gap-1.5 text-xs text-teal-300/90 hover:text-teal-200 border border-teal-500/25 hover:border-teal-500/50 rounded-lg px-2.5 py-1 disabled:opacity-50">
          <Wand2 className="w-3.5 h-3.5" />
          {detecting ? t('detecting') : t('detect_cta')}
        </button>
        {detectMsg && <span className="text-[11px] text-brain-500">{detectMsg}</span>}
      </div>

      {showAdvice && (
        <div className="rounded-2xl border border-purple-500/30 bg-gradient-to-br from-purple-600/15 to-brain-800/40 p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-start gap-3 min-w-0">
              <Sparkles className="w-5 h-5 text-purple-300 shrink-0 mt-0.5" />
              <div className="min-w-0">
                <span className="text-[11px] uppercase tracking-wide text-purple-300/80">{t('advice_badge')}</span>
                <p className="text-sm text-brain-100 mt-1 whitespace-pre-line">{advice!.advice}</p>
                {advice!.tools?.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mt-2">
                    {advice!.tools.map((t) => (
                      <button key={t.slug}
                        onClick={() => {
                          const tab = SECTION_TAB[(t.slug || '').split('/')[0]]
                          if (tab) onNavigate({ tab }); else onOpenGuide?.()
                        }}
                        className="text-[11px] px-2 py-0.5 rounded-full bg-brain-700/70 text-brain-200 hover:bg-brain-700">
                        {t.title}
                      </button>
                    ))}
                  </div>
                )}
                <button onClick={() => onOpenGuide?.()}
                  className="inline-flex items-center gap-1 text-sm text-purple-300 hover:text-purple-200 font-medium mt-2">
                  {t('ask_guide_more')} <ArrowRight className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
            <button onClick={() => setAdviceDismissed(true)} title={t('hide')}
              className="p-1 rounded-lg text-brain-500 hover:bg-brain-700 hover:text-brain-300 shrink-0">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}

      {nudge && (
    <div className="rounded-2xl border border-purple-500/25 bg-gradient-to-br from-purple-600/10 to-brain-800/40 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <div className="text-2xl leading-none mt-0.5">{nudge.emoji}</div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Lightbulb className="w-3.5 h-3.5 text-purple-400 shrink-0" />
              <span className="text-[11px] uppercase tracking-wide text-purple-400/80">{t('hint_badge')}</span>
            </div>
            <div className="text-sm font-semibold text-white mt-0.5">{nudge.title}</div>
            <p className="text-sm text-brain-300 mt-0.5">{nudge.text}</p>
            <div className="flex items-center gap-3 mt-2">
              <button onClick={act}
                className="inline-flex items-center gap-1 text-sm text-purple-300 hover:text-purple-200 font-medium">
                {nudge.cta} <ArrowRight className="w-3.5 h-3.5" />
              </button>
              {active.length > 1 && (
                <button onClick={next}
                  className="inline-flex items-center gap-1 text-xs text-brain-500 hover:text-brain-300">
                  <RotateCw className="w-3 h-3" /> {t('another_tip')}
                </button>
              )}
            </div>
          </div>
        </div>
        <button onClick={dismiss} title={t('hide')}
          className="p-1 rounded-lg text-brain-500 hover:bg-brain-700 hover:text-brain-300 shrink-0">
          <X className="w-4 h-4" />
        </button>
      </div>
    </div>
      )}
    </div>
  )
}
