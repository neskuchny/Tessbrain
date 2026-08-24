'use client'

import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'

// Настройка «Модели»: Стандарт/Премиум = свой провайдер + модель + ключ,
// «Сбросить к нашим» — возврат моделей и ключей платформы. Бэкенд:
// GET/POST /api/v1/task-analysis/llm-tiers (tier_overrides).
const PROVIDERS: { id: string; label: string; labelKey?: string; models: string[] }[] = [
  // deepseek-chat/-reasoner сняты с обслуживания (проверено 14.08.2026)
  { id: 'deepseek', label: 'DeepSeek', models: ['deepseek-v4-pro', 'deepseek-v4-flash'] },
  { id: 'xai', label: 'xAI (Grok)', models: ['grok-4', 'grok-4-mini'] },
  { id: 'openai', label: 'OpenAI', models: ['gpt-5.2', 'gpt-5-mini', 'gpt-4o', 'gpt-4o-mini'] },
  { id: 'anthropic', label: 'Anthropic (Claude)', models: ['claude-sonnet-5', 'claude-haiku-4-5'] },
  { id: 'gemini', label: 'Google Gemini', models: ['gemini-flash-latest', 'gemini-flash-lite-latest', 'gemini-3-pro'] },
  { id: 'qwen', label: 'Qwen', models: ['qwen-max', 'qwen-plus', 'qwen-turbo'] },
  { id: 'glm', label: 'GLM (Zhipu)', models: ['glm-4-plus', 'glm-4-flash'] },
  { id: 'kimi', label: 'Kimi (Moonshot)', models: ['kimi-k3', 'kimi-k2'] },
  { id: 'openrouter', label: 'OpenRouter', labelKey: 'provider_openrouter', models: [] },
  { id: 'mistral', label: 'Mistral', models: ['mistral-large-latest'] },
  { id: 'groq', label: 'Groq', models: ['llama-3.3-70b-versatile'] },
  { id: 'subscription_cli', label: 'Subscription', labelKey: 'provider_subscription_cli', models: ['claude', 'codex', 'gemini', 'qwen', 'kimi'] },
]
const inputCls = 'px-2 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-xs'

function TierSlot({ level, title, cur, onSaved }: {
  level: 'standard' | 'premium'; title: string; cur: any
  onSaved: (levels: any) => void
}) {
  const t = useTranslations('model_settings_panel')
  const [prov, setProv] = useState(cur?.provider || '')
  const [model, setModel] = useState(cur?.model || '')
  const [key, setKey] = useState('')
  const [msg, setMsg] = useState('')
  useEffect(() => { setProv(cur?.provider || ''); setModel(cur?.model || '') }, [cur])
  const models = PROVIDERS.find((p) => p.id === prov)?.models || []
  const post = async (body: any) => {
    try {
      const r = await authFetch('/api/v1/task-analysis/llm-tiers', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      const d = await r.json()
      if (d.status === 'success') { onSaved(d.levels || {}); setMsg(t('saved_ok')); setKey('') }
      else setMsg('⚠️ ' + (d.message || t('error_generic')))
    } catch (e: any) { setMsg('⚠️ ' + (e?.message || t('error_network'))) }
    setTimeout(() => setMsg(''), 3000)
  }
  return (
    <div className="rounded-lg border border-brain-700/60 bg-brain-900/40 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-brain-100">{title}</span>
        {cur ? <span className="text-[11px] text-emerald-300">{t('custom_model_label', { provider: cur.provider, model: cur.model })}{cur.has_key ? ' · 🔑' : ''}</span>
             : <span className="text-[11px] text-brain-500">{t('platform_model')}</span>}
      </div>
      <div className="flex flex-wrap gap-2">
        <select value={prov} onChange={(e) => { setProv(e.target.value); setModel('') }} className={inputCls}>
          <option value="">{t('provider_placeholder')}</option>
          {PROVIDERS.map((p) => <option key={p.id} value={p.id}>{p.labelKey ? t(p.labelKey) : p.label}</option>)}
        </select>
        <input list={`models-${level}`} value={model} onChange={(e) => setModel(e.target.value)}
          placeholder={t('model_placeholder')} className={`${inputCls} min-w-[170px]`} />
        <datalist id={`models-${level}`}>{models.map((m) => <option key={m} value={m} />)}</datalist>
        <input type="password" value={key} onChange={(e) => setKey(e.target.value)}
          placeholder={cur?.has_key ? t('key_saved_replace') : t('api_key_placeholder')}
          className={`${inputCls} min-w-[170px]`} />
        <button disabled={!prov || !model}
          onClick={() => post({ level, config: { provider: prov, model, ...(key ? { api_key: key } : {}) } })}
          className="px-3 py-1.5 rounded-lg bg-blue-600/80 hover:bg-blue-600 text-white text-xs disabled:opacity-40">
          {t('save')}
        </button>
        {cur && (
          <button onClick={() => post({ level, config: null })}
            className="px-3 py-1.5 rounded-lg border border-brain-600 text-brain-300 hover:text-red-300 text-xs">
            {t('reset_level')}
          </button>
        )}
        {msg && <span className="text-[11px] self-center">{msg}</span>}
      </div>
    </div>
  )
}

export default function ModelSettingsPanel() {
  const t = useTranslations('model_settings_panel')
  const [levels, setLevels] = useState<any>({})
  useEffect(() => {
    (async () => {
      try {
        const r = await authFetch('/api/v1/task-analysis/llm-tiers')
        const d = await r.json()
        setLevels(d.levels || {})
      } catch { /* платформа */ }
    })()
  }, [])
  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-brain-100 font-medium">{t('heading')}</h3>
        <p className="text-xs text-brain-400 mt-1">
          {t('description')}
        </p>
      </div>
      <TierSlot level="standard" title={t('tier_standard_title')} cur={levels.standard} onSaved={setLevels} />
      <TierSlot level="premium" title={t('tier_premium_title')} cur={levels.premium} onSaved={setLevels} />
      {(levels.standard || levels.premium) && (
        <button onClick={async () => {
          const r = await authFetch('/api/v1/task-analysis/llm-tiers', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reset_all: true }) })
          const d = await r.json().catch(() => ({}))
          if (d.status === 'success') setLevels({})
        }}
          className="px-3 py-1.5 rounded-lg bg-amber-600/80 hover:bg-amber-600 text-white text-xs">
          {t('reset_all')}
        </button>
      )}
    </div>
  )
}
