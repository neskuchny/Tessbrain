'use client'

import { useEffect, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import FillDocumentsPanel from './FillDocumentsPanel'

// Mark → «Исследования»: вывод продукта на рынок по полевым данным.
// Бриф → сегменты-гипотезы → сбор корпуса (веб, t.me/s, VK, отзовики) →
// лексикон/боли с проверенными цитатами → отчёт (📍 поле / 💡 гипотеза).

interface Props { userId: string | null }

interface ResearchRun {
  id: string
  product: string
  market?: string
  status: string
  stage?: string
  created_at?: string
  corpus_count?: number
  document_id?: string | null
  report_markdown?: string
  error?: string
  stages_log?: Array<{ stage: string; detail: string; at: string }>
}

interface PersonaPain { text: string; quote?: string; origin?: string }
interface Persona {
  id: string
  run_id?: string
  product?: string
  name: string
  segment?: string
  portrait?: string
  goals?: string[]
  pains?: PersonaPain[]
  lexicon?: string[]
  objections?: string[]
  media?: string[]
  tone?: string
  field_pains?: number
}

interface Reaction {
  persona: string
  first_impression: string
  hooks: string[]
  repels: string[]
  objections: string[]
  trust_1_5: number
  would_respond: boolean
  say_it_better: string
}

interface SimResult {
  success: boolean
  error?: string
  reactions?: Reaction[]
  skeptic?: {
    attacks: string[]
    panel_flattery: string[]
    reality_check: string
    fix_first: string
  } | null
  avg_trust?: number | null
  would_respond?: number
  panel_size?: number
  disclaimer?: string
}

function authHeader(): Record<string, string> {
  if (typeof window === 'undefined') return {}
  const token = localStorage.getItem('tessent_access_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

const STAGE_LABEL_KEYS: Record<string, string> = {
  queued: 'stage_queued',
  segments: 'stage_segments',
  collect: 'stage_collect',
  language: 'stage_language',
  insights: 'stage_insights',
  report: 'stage_report',
  done: 'stage_done',
}

export default function MarkResearchPanel({ userId }: Props) {
  const t = useTranslations('mark_research')
  const [product, setProduct] = useState('')
  const [market, setMarket] = useState('')
  const [extra, setExtra] = useState('')
  const [tgChannels, setTgChannels] = useState('')
  const [vkGroups, setVkGroups] = useState('')
  const [urls, setUrls] = useState('')
  const [showSources, setShowSources] = useState(false)
  const [runs, setRuns] = useState<ResearchRun[]>([])
  const [openRun, setOpenRun] = useState<ResearchRun | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Фаза 2: персоны из корпуса + панель реакций
  const [personas, setPersonas] = useState<Persona[]>([])
  const [selectedPersonas, setSelectedPersonas] = useState<string[]>([])
  const [offerText, setOfferText] = useState('')
  const [personasBusy, setPersonasBusy] = useState(false)
  const [simBusy, setSimBusy] = useState(false)
  const [simResult, setSimResult] = useState<SimResult | null>(null)
  const [simHistory, setSimHistory] = useState<SimResult[]>([])
  const [showSimHistory, setShowSimHistory] = useState(false)
  const [toChatMsg, setToChatMsg] = useState<string | null>(null)
  const [toChatBusy, setToChatBusy] = useState(false)

  // Фаза 3: размер сегментов (Wordstat) + факт-гейт кампании
  const [sizingBusy, setSizingBusy] = useState(false)
  const [showFacts, setShowFacts] = useState(false)
  const [factsBusy, setFactsBusy] = useState(false)
  const emptyFactRow = { label: '', impressions: '', clicks: '', leads: '', spend: '' }
  const [factRows, setFactRows] = useState([{ ...emptyFactRow }, { ...emptyFactRow }])

  const runSizing = async (runId: string) => {
    setSizingBusy(true); setError(null)
    try {
      const r = await fetch(`/api/v1/mark/research/${runId}/sizing`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
      })
      const d = await r.json()
      if (!d?.success) throw new Error(d?.error || `HTTP ${r.status}`)
      await openById(runId) // отчёт дополнен секцией размеров
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSizingBusy(false)
    }
  }

  const submitFacts = async (runId: string) => {
    const facts = factRows
      .filter((r) => r.label.trim() && (r.impressions || r.clicks))
      .map((r) => ({
        label: r.label.trim(),
        impressions: Number(r.impressions) || 0,
        clicks: Number(r.clicks) || 0,
        leads: Number(r.leads) || 0,
        spend: Number(r.spend) || 0,
      }))
    if (!facts.length) { setError(t('facts_fill_at_least_one_row')); return }
    setFactsBusy(true); setError(null)
    try {
      const r = await fetch(`/api/v1/mark/research/${runId}/facts`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify({ facts }),
      })
      const d = await r.json()
      if (!d?.success) throw new Error(d?.error || `HTTP ${r.status}`)
      setShowFacts(false)
      setFactRows([{ ...emptyFactRow }, { ...emptyFactRow }])
      await openById(runId) // отчёт дополнен факт-гейтом
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setFactsBusy(false)
    }
  }

  const loadRuns = async () => {
    if (!userId) return
    try {
      const r = await fetch('/api/v1/mark/research', { headers: authHeader() })
      const d = await r.json()
      setRuns(d?.runs || [])
    } catch { /* ignore */ }
  }
  const loadPersonas = async () => {
    if (!userId) return
    try {
      const r = await fetch('/api/v1/mark/personas', { headers: authHeader() })
      const d = await r.json()
      setPersonas(d?.personas || [])
    } catch { /* ignore */ }
  }
  const loadSimulations = async () => {
    if (!userId) return
    try {
      const r = await fetch('/api/v1/mark/personas/simulations', { headers: authHeader() })
      const d = await r.json()
      setSimHistory(d?.simulations || [])
    } catch { /* ignore */ }
  }
  useEffect(() => { loadRuns(); loadPersonas(); loadSimulations() }, [userId])

  const exportToChat = async () => {
    if (!userId || toChatBusy) return
    setToChatBusy(true); setToChatMsg(null); setError(null)
    try {
      const r = await fetch('/api/v1/mark/personas/to-chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify({ run_id: openRun?.id || null }),
      })
      const d = await r.json()
      if (!d?.success) throw new Error(d?.error || `HTTP ${r.status}`)
      setToChatMsg(d.updated
        ? t('to_chat_doc_updated', { title: d.title, hint: d.hint })
        : t('to_chat_doc_created', { title: d.title, hint: d.hint }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setToChatBusy(false)
    }
  }

  const buildPersonas = async (runId: string) => {
    setPersonasBusy(true); setError(null)
    try {
      const r = await fetch(`/api/v1/mark/research/${runId}/personas`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
      })
      const d = await r.json()
      if (!d?.success) throw new Error(d?.error || `HTTP ${r.status}`)
      await loadPersonas()
      setSelectedPersonas((d.personas || []).map((p: Persona) => p.id))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setPersonasBusy(false)
    }
  }

  const removePersona = async (id: string) => {
    try {
      await fetch('/api/v1/mark/personas/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify({ persona_id: id }),
      })
      setPersonas((p) => p.filter((x) => x.id !== id))
      setSelectedPersonas((s) => s.filter((x) => x !== id))
    } catch { /* ignore */ }
  }

  const runSimulation = async () => {
    if (simBusy || selectedPersonas.length === 0 || offerText.trim().length < 10) return
    setSimBusy(true); setError(null); setSimResult(null)
    try {
      const r = await fetch('/api/v1/mark/personas/simulate', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify({ persona_ids: selectedPersonas, message: offerText.trim() }),
      })
      const d = await r.json()
      if (!d?.success) throw new Error(d?.error || `HTTP ${r.status}`)
      setSimResult(d)
      loadSimulations()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSimBusy(false)
    }
  }

  // Поллинг открытого рана, пока он running
  useEffect(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    if (!openRun || openRun.status !== 'running') return
    pollRef.current = setInterval(async () => {
      try {
        const r = await fetch(`/api/v1/mark/research/${openRun.id}`, { headers: authHeader() })
        if (!r.ok) return
        const d = await r.json()
        setOpenRun(d)
        if (d.status !== 'running') { loadRuns() }
      } catch { /* ignore */ }
    }, 4000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [openRun?.id, openRun?.status])

  const start = async () => {
    if (!userId || busy || product.trim().length < 3) return
    setBusy(true); setError(null)
    try {
      const body = {
        product: product.trim(),
        market: market.trim(),
        extra: extra.trim(),
        tg_channels: tgChannels.split(/[,\s]+/).filter(Boolean),
        vk_groups: vkGroups.split(/[,\s]+/).filter(Boolean),
        urls: urls.split(/[\s,]+/).filter((u) => u.startsWith('http')),
      }
      const r = await fetch('/api/v1/mark/research', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify(body),
      })
      const d = await r.json()
      if (!d?.success) throw new Error(d?.detail || d?.error || `HTTP ${r.status}`)
      setOpenRun(d.run)
      loadRuns()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const openById = async (id: string) => {
    try {
      const r = await fetch(`/api/v1/mark/research/${id}`, { headers: authHeader() })
      if (r.ok) setOpenRun(await r.json())
    } catch { /* ignore */ }
  }

  return (
    <div className="h-full overflow-y-auto p-4 space-y-4">
      {/* Бриф */}
      <div className="rounded-xl border border-brain-600/30 bg-brain-900/40 p-4 space-y-2">
        <div className="text-sm font-semibold text-white">{t('audience_research_title')}</div>
        <div className="text-[11px] text-slate-500">
          {t('brief_description')}
        </div>
        <input
          value={product}
          onChange={(e) => setProduct(e.target.value)}
          placeholder={t('product_placeholder')}
          className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm"
        />
        <div className="flex gap-2">
          <input
            value={market}
            onChange={(e) => setMarket(e.target.value)}
            placeholder={t('market_placeholder')}
            className="flex-1 px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm"
          />
          <button
            onClick={() => setShowSources((v) => !v)}
            className="text-xs px-3 py-2 bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/40 rounded text-slate-300"
          >
            {showSources ? t('hide_sources') : t('show_sources')}
          </button>
        </div>
        {showSources && (
          <div className="space-y-2">
            <input value={tgChannels} onChange={(e) => setTgChannels(e.target.value)}
              placeholder={t('tg_channels_placeholder')}
              className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm" />
            <input value={vkGroups} onChange={(e) => setVkGroups(e.target.value)}
              placeholder={t('vk_groups_placeholder')}
              className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm" />
            <input value={urls} onChange={(e) => setUrls(e.target.value)}
              placeholder={t('urls_placeholder')}
              className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm" />
          </div>
        )}
        <textarea
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
          rows={2}
          placeholder={t('extra_placeholder')}
          className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm resize-none"
        />
        <div className="flex items-center gap-3">
          <button
            onClick={start}
            disabled={busy || product.trim().length < 3}
            className="px-4 py-2 bg-orange-600 hover:bg-orange-500 disabled:opacity-40 text-white rounded text-sm font-medium"
          >
            {busy ? '…' : t('start_research')}
          </button>
          <span className="text-[11px] text-slate-500">{t('background_hint')}</span>
        </div>
        {error && <div className="text-xs text-red-400">{error}</div>}
      </div>

      {/* Открытый ран: прогресс/отчёт */}
      {openRun && (
        <div className="rounded-xl border border-brain-600/30 bg-brain-900/40 p-4">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm font-medium text-white truncate">
              {openRun.status === 'running' ? '⏳' : openRun.status === 'done' ? '✅' : '❌'} {openRun.product}
            </div>
            <button onClick={() => setOpenRun(null)} className="text-slate-500 hover:text-slate-300 text-sm">✕</button>
          </div>
          {openRun.status === 'running' && (
            <div className="space-y-1">
              <div className="text-sm text-orange-300 animate-pulse">
                {STAGE_LABEL_KEYS[openRun.stage || 'queued'] ? t(STAGE_LABEL_KEYS[openRun.stage || 'queued']) : openRun.stage}
              </div>
              {(openRun.stages_log || []).slice(-5).map((s, i) => (
                <div key={i} className="text-[11px] text-slate-500">· {s.detail}</div>
              ))}
            </div>
          )}
          {openRun.status === 'failed' && (
            <div className="text-sm text-red-400">{t('run_failed', { error: openRun.error ?? '' })}</div>
          )}
          {openRun.status === 'done' && (
            <div className="mb-2 flex gap-2 flex-wrap">
              <button
                onClick={() => buildPersonas(openRun.id)}
                disabled={personasBusy}
                className="text-xs px-3 py-1.5 rounded bg-orange-600/80 hover:bg-orange-500 disabled:opacity-40 text-white font-medium"
                title={t('build_personas_hint')}
              >
                {personasBusy ? t('building_personas') : t('build_personas')}
              </button>
              <button
                onClick={() => runSizing(openRun.id)}
                disabled={sizingBusy}
                className="text-xs px-3 py-1.5 rounded bg-brain-800/60 hover:bg-brain-700/60 border border-brain-600/40 disabled:opacity-40 text-slate-200"
                title={t('sizing_hint')}
              >
                {sizingBusy ? t('sizing_busy') : t('sizing_button')}
              </button>
              <button
                onClick={() => setShowFacts((v) => !v)}
                className="text-xs px-3 py-1.5 rounded bg-brain-800/60 hover:bg-brain-700/60 border border-brain-600/40 text-slate-200"
                title={t('facts_hint')}
              >
                {t('facts_button')}
              </button>
            </div>
          )}
          {openRun.status === 'done' && showFacts && (
            <div className="mb-3 p-3 rounded-lg border border-brain-700/40 bg-brain-900/40 space-y-2">
              <div className="text-[11px] text-slate-500">
                {t('facts_description')}
              </div>
              {factRows.map((row, i) => (
                <div key={i} className="flex gap-1.5 flex-wrap items-center">
                  <input value={row.label}
                    onChange={(e) => setFactRows((rs) => rs.map((r, j) => j === i ? { ...r, label: e.target.value } : r))}
                    placeholder={t('fact_variant_placeholder', { num: i + 1 })}
                    className="flex-1 min-w-[180px] px-2 py-1.5 bg-brain-800/40 border border-brain-600/40 rounded text-white text-xs" />
                  {(['impressions', 'clicks', 'leads', 'spend'] as const).map((k) => (
                    <input key={k} value={row[k]} inputMode="numeric"
                      onChange={(e) => setFactRows((rs) => rs.map((r, j) => j === i ? { ...r, [k]: e.target.value } : r))}
                      placeholder={{ impressions: t('fact_impressions'), clicks: t('fact_clicks'), leads: t('fact_leads'), spend: t('fact_spend') }[k]}
                      className="w-20 px-2 py-1.5 bg-brain-800/40 border border-brain-600/40 rounded text-white text-xs" />
                  ))}
                </div>
              ))}
              <div className="flex gap-2">
                <button onClick={() => setFactRows((rs) => [...rs, { ...emptyFactRow }])}
                  className="text-xs px-2 py-1 rounded bg-brain-800/50 text-slate-400 hover:bg-brain-700/50">{t('add_variant')}</button>
                <button onClick={() => submitFacts(openRun.id)} disabled={factsBusy}
                  className="text-xs px-3 py-1 rounded bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white font-medium">
                  {factsBusy ? '…' : t('compare_with_panel')}
                </button>
              </div>
            </div>
          )}
          {openRun.status === 'done' && openRun.report_markdown && (
            <div className="prose prose-invert prose-sm max-w-none text-slate-200">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{openRun.report_markdown}</ReactMarkdown>
            </div>
          )}
        </div>
      )}

      {/* Персоны (переиспользуемые аудитории) + панель реакций */}
      {personas.length > 0 && (
        <div className="rounded-xl border border-brain-600/30 bg-brain-900/40 p-4 space-y-3">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <div className="text-sm font-semibold text-white">{t('personas_title')}</div>
            <div className="flex gap-2">
              <button
                onClick={() => setShowSimHistory((v) => !v)}
                className="text-xs px-2 py-1 rounded bg-brain-800/60 hover:bg-brain-700/60 border border-brain-600/40 text-slate-300"
              >{simHistory.length ? t('history_with_count', { count: simHistory.length }) : t('history')}</button>
              <button
                onClick={exportToChat}
                disabled={toChatBusy}
                title={t('to_chat_hint')}
                className="text-xs px-2 py-1 rounded bg-purple-600/80 hover:bg-purple-500 disabled:opacity-40 text-white"
              >{toChatBusy ? '…' : t('to_chat_button')}</button>
            </div>
          </div>
          {toChatMsg && <div className="text-[11px] text-emerald-300">{toChatMsg}</div>}
          {showSimHistory && (
            <div className="max-h-44 overflow-y-auto rounded border border-brain-700/40 divide-y divide-brain-700/30">
              {simHistory.length === 0 ? (
                <div className="text-xs text-slate-500 p-2 text-center">{t('no_panels_yet')}</div>
              ) : simHistory.map((h, i) => (
                <button key={i}
                  onClick={() => { setSimResult(h); setShowSimHistory(false) }}
                  className="w-full text-left px-2 py-1.5 hover:bg-brain-800/40 flex items-center justify-between gap-2">
                  <span className="text-xs text-slate-300 truncate">«{(h as any).message}»</span>
                  <span className="text-[10px] text-slate-500 shrink-0">
                    {h.avg_trust != null ? `★${h.avg_trust}` : ''} · {h.would_respond}/{h.panel_size}
                  </span>
                </button>
              ))}
            </div>
          )}
          <div className="text-[11px] text-slate-500">
            {t('panel_description')}
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {personas.map((p) => (
              <div
                key={p.id}
                className={`relative p-3 rounded-lg border cursor-pointer transition-colors ${
                  selectedPersonas.includes(p.id)
                    ? 'border-orange-500/60 bg-orange-500/5'
                    : 'border-brain-700/40 bg-brain-900/30 hover:border-brain-600/60'
                }`}
                onClick={() => setSelectedPersonas((s) =>
                  s.includes(p.id) ? s.filter((x) => x !== p.id) : [...s, p.id])}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="text-sm font-medium text-white">{p.name}</div>
                  <button
                    onClick={(e) => { e.stopPropagation(); removePersona(p.id) }}
                    className="text-slate-600 hover:text-red-400 text-xs"
                  >✕</button>
                </div>
                <div className="text-[11px] text-orange-300/90 mb-1">{p.segment}</div>
                <div className="text-xs text-slate-400 line-clamp-2">{p.portrait}</div>
                {(p.pains || []).slice(0, 2).map((pain, i) => (
                  <div key={i} className="text-[11px] text-slate-400 mt-1">
                    {pain.origin === 'field' ? '📍' : '💡'} {pain.text}
                  </div>
                ))}
                {(p.lexicon || []).length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {(p.lexicon || []).slice(0, 5).map((w, i) => (
                      <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-brain-800/60 text-slate-400">{w}</span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>

          <textarea
            value={offerText}
            onChange={(e) => setOfferText(e.target.value)}
            rows={3}
            placeholder={t('offer_placeholder')}
            className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm resize-none"
          />
          <div className="flex items-center gap-3">
            <button
              onClick={runSimulation}
              disabled={simBusy || selectedPersonas.length === 0 || offerText.trim().length < 10}
              className="px-4 py-2 bg-orange-600 hover:bg-orange-500 disabled:opacity-40 text-white rounded text-sm font-medium"
            >
              {simBusy ? '…' : t('run_panel_button', { count: selectedPersonas.length })}
            </button>
            {simResult?.avg_trust != null && (
              <span className="text-xs text-slate-400">
                {t('trust_label')} <b className="text-white">{simResult.avg_trust}/5</b> ·{' '}
                {t('would_respond_label')} <b className="text-white">{simResult.would_respond}/{simResult.panel_size}</b>
              </span>
            )}
          </div>

          {simResult?.reactions && (
            <div className="space-y-2">
              {simResult.reactions.map((r, i) => (
                <div key={i} className="p-3 rounded-lg border border-brain-700/40 bg-brain-900/30">
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="text-sm font-medium text-white">{r.persona}</span>
                    <span className={`text-xs ${r.trust_1_5 >= 4 ? 'text-emerald-400' : r.trust_1_5 <= 2 ? 'text-red-400' : 'text-amber-400'}`}>
                      {'★'.repeat(r.trust_1_5)}{'☆'.repeat(5 - r.trust_1_5)} {r.would_respond ? t('will_respond') : t('will_pass')}
                    </span>
                  </div>
                  <div className="text-xs text-slate-300 italic mb-1">«{r.first_impression}»</div>
                  {r.hooks.length > 0 && (
                    <div className="text-[11px] text-emerald-300/90">{t('hooks_line', { items: r.hooks.join(' · ') })}</div>
                  )}
                  {r.repels.length > 0 && (
                    <div className="text-[11px] text-red-300/90">{t('repels_line', { items: r.repels.join(' · ') })}</div>
                  )}
                  {r.objections.length > 0 && (
                    <div className="text-[11px] text-amber-300/90">{t('objections_line', { items: r.objections.join(' · ') })}</div>
                  )}
                  {r.say_it_better && (
                    <div className="text-[11px] text-slate-400 mt-1">{t('say_it_better_line', { text: r.say_it_better })}</div>
                  )}
                </div>
              ))}
              {simResult.skeptic && (
                <div className="p-3 rounded-lg border border-red-500/30 bg-red-500/5">
                  <div className="text-sm font-medium text-red-300 mb-1">{t('skeptic_title')}</div>
                  {simResult.skeptic.attacks.map((a, i) => (
                    <div key={i} className="text-[11px] text-slate-300">• {a}</div>
                  ))}
                  {simResult.skeptic.panel_flattery.length > 0 && (
                    <div className="text-[11px] text-amber-300/90 mt-1">
                      {t('panel_flattery_line', { items: simResult.skeptic.panel_flattery.join(' · ') })}
                    </div>
                  )}
                  {simResult.skeptic.reality_check && (
                    <div className="text-[11px] text-slate-400 mt-1">{t('reality_check_line', { text: simResult.skeptic.reality_check })}</div>
                  )}
                  {simResult.skeptic.fix_first && (
                    <div className="text-xs text-white mt-1.5">{t('fix_first_line', { text: simResult.skeptic.fix_first })}</div>
                  )}
                </div>
              )}
              {simResult.disclaimer && (
                <div className="text-[10px] text-slate-600">{simResult.disclaimer}</div>
              )}
            </div>
          )}
        </div>
      )}

      {/* История */}
      {runs.length > 0 && (
        <div className="rounded-xl border border-brain-600/30 bg-brain-900/30 divide-y divide-brain-700/30">
          {runs.map((r) => (
            <button
              key={r.id}
              onClick={() => openById(r.id)}
              className="w-full text-left px-3 py-2 hover:bg-brain-800/40 flex items-center justify-between gap-2"
            >
              <span className="text-xs text-slate-300 truncate">
                {r.status === 'done' ? '✅' : r.status === 'failed' ? '❌' : '⏳'} {r.product}
                {typeof r.corpus_count === 'number' ? ` · ${t('corpus_count', { count: r.corpus_count })}` : ''}
              </span>
              <span className="text-[10px] text-slate-500 shrink-0" suppressHydrationWarning>
                {r.created_at ? new Date(r.created_at + 'Z').toLocaleString('ru-RU') : ''}
              </span>
            </button>
          ))}
        </div>
      )}

      {/* Медиаплан клиента — рядом с исследованием аудитории и каналов, где
          ему по смыслу и место (раньше был спрятан в «Документы»). */}
      {userId && (
        <div className="rounded-xl border border-sky-500/25 bg-sky-500/5 p-4 space-y-2">
          <div className="text-sm font-semibold text-white">{t('mediaplan_title')}</div>
          <div className="text-[11px] text-slate-500">
            {t('mediaplan_description')}
          </div>
          <FillDocumentsPanel userId={userId} variant="mediaplan" />
        </div>
      )}
    </div>
  )
}
