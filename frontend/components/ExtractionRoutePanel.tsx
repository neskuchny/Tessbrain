'use client'

// Выбор МОДЕЛИ обработки встреч (backend model_router): провайдер × уровень
// (стандарт|премиум) + тумблер «использовать мой ключ». Без выбора — дефолты
// платформы. Формат запросов меняется под тир модели (frontier=1, lite≈9).
import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { Cpu, Loader2, KeyRound, Info, Zap, Gauge } from 'lucide-react'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''

interface TierInfo { model: string; tier: string; calls: number }
interface RouteOption { provider: string; needs_key: boolean; fast: TierInfo; standard: TierInfo; premium: TierInfo }
interface Resolved {
  model: string; tier: string; level: string; key_source: string
  planned_calls: number; native?: string | null; reason?: string | null; caller_ready?: boolean
}
interface Pref { provider: string; level: string; use_own_key: boolean }

const PROVIDER_LABEL: Record<string, string> = {
  anthropic: 'Anthropic (Claude)',
  openai: 'OpenAI (ChatGPT)',
  xai: 'xAI (Grok)',
  qwen: 'Qwen (Alibaba)',
  deepseek: 'DeepSeek',
  google: 'Google (Gemini)',
}

export default function ExtractionRoutePanel({ userId }: { userId: string }) {
  const t = useTranslations('extraction_route')
  const [pref, setPref] = useState<Pref | null>(null)
  const [resolved, setResolved] = useState<Resolved | null>(null)
  const [options, setOptions] = useState<RouteOption[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    if (!userId) return
    setLoading(true)
    try {
      const r = await fetch(`${API_BASE}/api/v1/chat/extraction-route?user_id=${encodeURIComponent(userId)}`)
      const d = await r.json()
      setPref(d.pref); setResolved(d.resolved); setOptions(d.options || [])
    } catch { /* тихо: панель необязательная */ } finally { setLoading(false) }
  }, [userId])

  useEffect(() => { load() }, [load])

  const save = async (patch: Partial<Pref>) => {
    if (!userId || !pref) return
    setSaving(true)
    const next = { ...pref, ...patch }
    setPref(next)
    try {
      await fetch(`${API_BASE}/api/v1/chat/extraction-route?user_id=${encodeURIComponent(userId)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
      })
      await load()
    } catch { /* тихо */ } finally { setSaving(false) }
  }

  if (loading && !pref) {
    return (
      <div className="border border-brain-700/50 rounded-xl p-4 flex items-center gap-2 text-brain-400">
        <Loader2 className="w-4 h-4 animate-spin" /> {t('loading')}
      </div>
    )
  }
  if (!pref || !resolved) return null

  const opt = options.find(o => o.provider === pref.provider)
  const needsKey = pref.provider !== 'platform'

  return (
    <div className="border border-purple-500/30 bg-purple-500/[0.04] rounded-xl p-4 space-y-4">
      {/* header */}
      <div className="flex items-start gap-3">
        <div className="w-8 h-8 rounded-lg bg-purple-500/15 flex items-center justify-center shrink-0">
          <Cpu className="w-4 h-4 text-purple-400" />
        </div>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-brain-100">{t('title')}</h3>
          <p className="text-xs text-brain-400 mt-0.5">{t('subtitle')}</p>
        </div>
      </div>

      {/* provider */}
      <div className="space-y-1.5">
        <label className="text-xs font-medium text-brain-300">{t('provider')}</label>
        <select
          value={pref.provider}
          onChange={e => save({ provider: e.target.value })}
          className="w-full bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-brain-100 focus:border-purple-500 outline-none"
        >
          {options.map(o => (
            <option key={o.provider} value={o.provider}>
              {o.provider === 'platform' ? t('provider_platform') : (PROVIDER_LABEL[o.provider] || o.provider)}{o.needs_key ? ` — ${t('needs_key')}` : ''}
            </option>
          ))}
        </select>
      </div>

      {/* level */}
      <div className="space-y-1.5">
        <label className="text-xs font-medium text-brain-300">{t('level')}</label>
        <div className="grid grid-cols-3 gap-2">
          {(['fast', 'standard', 'premium'] as const).map(lv => {
            const info = opt ? opt[lv] : null
            const active = pref.level === lv
            return (
              <button
                key={lv}
                onClick={() => save({ level: lv })}
                className={`flex flex-col items-start gap-1 rounded-lg border px-3 py-2 text-left transition-colors ${
                  active ? 'border-purple-500 bg-purple-500/10' : 'border-brain-700 hover:border-brain-600'
                }`}
              >
                <span className="flex items-center gap-1.5 text-sm font-medium text-brain-100">
                  {lv === 'premium' ? <Zap className="w-3.5 h-3.5 text-amber-400" />
                    : lv === 'fast' ? <Zap className="w-3.5 h-3.5 text-sky-400" />
                    : <Gauge className="w-3.5 h-3.5 text-brain-400" />}
                  {t(lv)}
                </span>
                {/* Имена базовых моделей платформы пользователю не показываем —
                    это внутренняя кухня. Для СВОЕГО провайдера модель видна. */}
                {pref.provider === 'platform' ? (
                  <span className="text-[11px] text-brain-400">
                    {t(`level_hint_${lv}`)}
                  </span>
                ) : info && (
                  <span className="text-[11px] text-brain-400 font-mono">
                    {info.model}
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </div>

      {/* use own key */}
      {needsKey && (
        <label className="flex items-center justify-between gap-3 cursor-pointer">
          <span className="flex items-center gap-2 text-sm text-brain-200">
            <KeyRound className="w-4 h-4 text-brain-400" /> {t('use_own_key')}
          </span>
          <button
            onClick={() => save({ use_own_key: !pref.use_own_key })}
            className={`relative w-10 h-6 rounded-full transition-colors ${pref.use_own_key ? 'bg-purple-500' : 'bg-brain-700'}`}
            aria-pressed={pref.use_own_key}
          >
            <span className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform ${pref.use_own_key ? 'translate-x-4' : 'translate-x-0.5'}`} />
          </button>
        </label>
      )}

      {/* resolved preview */}
      <div className="flex items-start gap-2 rounded-lg bg-brain-800/60 border border-brain-700/50 px-3 py-2">
        <Info className="w-3.5 h-3.5 text-brain-400 mt-0.5 shrink-0" />
        <div className="text-[11px] text-brain-300 leading-relaxed">
          <span className="text-brain-100 font-medium">{t('will_run')}: </span>
          {resolved.key_source === 'user' ? (
            <>
              <span className="font-mono">{resolved.model}</span>
              {' · '}{t('your_key')}
            </>
          ) : (
            <>{t(pref.level)}{' · '}{t('platform_key')}</>
          )}
          {saving && <Loader2 className="inline w-3 h-3 animate-spin ml-1.5" />}
          {resolved.reason && <div className="text-amber-400/80 mt-0.5">⚠ {resolved.reason}</div>}
        </div>
      </div>
    </div>
  )
}
