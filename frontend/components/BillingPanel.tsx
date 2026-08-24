'use client'

// Тарифы и расход (шаг 1 биллинга): свой план + расход месяца vs включённый
// пул + сетка тарифов. Только показ — применение плана идёт через платёжный
// webhook (ЮKassa, шаг 2). Данные: GET /billing/me, GET /billing/plans.

import { useEffect, useState } from 'react'
import { X, Zap, Check } from 'lucide-react'
import { useTranslations } from 'next-intl'

interface Props {
  isOpen: boolean
  onClose: () => void
  userId?: string
}

function authHeaders(): Record<string, string> {
  if (typeof window === 'undefined') return {}
  const token = localStorage.getItem('tessent_access_token') || localStorage.getItem('access_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

const PLAN_LABEL_KEY: Record<string, string> = {
  solo: 'plan_solo', small_byok: 'plan_small_byok', business: 'plan_business', mid: 'plan_mid',
  mid_byok: 'plan_mid_byok', enterprise_managed: 'plan_enterprise_managed',
  enterprise_onprem: 'plan_enterprise_onprem', free: 'plan_free',
}

const rub = (n?: number) => (typeof n === 'number' ? n.toLocaleString('ru-RU') + ' ₽' : '—')

export default function BillingPanel({ isOpen, onClose, userId }: Props) {
  const t = useTranslations('billing_panel')
  const [me, setMe] = useState<any | null>(null)
  const [plans, setPlans] = useState<Record<string, any>>({})
  const [loading, setLoading] = useState(false)
  const [checkoutBusy, setCheckoutBusy] = useState<string | null>(null)
  const [checkoutMsg, setCheckoutMsg] = useState<string | null>(null)
  const uq = userId ? `?user_id=${encodeURIComponent(userId)}` : ''

  const startCheckout = async (planKey: string) => {
    if (checkoutBusy) return
    setCheckoutBusy(planKey); setCheckoutMsg(null)
    try {
      const r = await fetch(`/api/v1/billing/checkout${uq}`, {
        method: 'POST', headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: planKey, return_url: window.location.origin }),
      })
      const d = await r.json()
      if (d?.success && d.confirmation_url) window.location.href = d.confirmation_url
      else setCheckoutMsg(`⚠️ ${d?.error || t('checkout_create_failed')}`)
    } catch (e) { setCheckoutMsg(`⚠️ ${(e as Error).message}`) }
    finally { setCheckoutBusy(null) }
  }

  useEffect(() => {
    if (!isOpen) return
    let alive = true
    setLoading(true)
    Promise.all([
      fetch(`/api/v1/billing/me${uq}`, { headers: authHeaders() }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
      fetch('/api/v1/billing/plans', { headers: authHeaders() }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]).then(([m, p]) => {
      if (!alive) return
      setMe(m); setPlans((p?.plans as Record<string, any>) || {}); setLoading(false)
    })
    return () => { alive = false }
  }, [isOpen, uq])

  if (!isOpen) return null

  const pool = me?.included_monthly_usd
  const used = me?.month_used_usd ?? 0
  const pct = pool ? Math.min(100, Math.round((used / pool) * 100)) : null
  const currentPlan = me?.plan || 'free'

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-3xl max-h-[85vh] overflow-hidden shadow-2xl flex flex-col">
        <div className="flex items-center justify-between p-4 border-b border-brain-700/50">
          <div className="flex items-center gap-2">
            <Zap className="w-5 h-5 text-amber-400" />
            <h2 className="text-lg font-semibold text-white">{t('title')}</h2>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-brain-800 rounded-lg text-brain-400 hover:text-white">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-4 custom-scrollbar">
          {loading && <div className="text-center py-8 text-brain-400 text-sm">{t('loading')}</div>}

          {/* Мой план + расход */}
          {!loading && me && (
            <div className="rounded-xl border border-brain-700 bg-brain-800/30 p-4 space-y-2">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div>
                  <div className="text-xs text-brain-400">{t('current_plan')}</div>
                  <div className="text-lg font-semibold text-white">{PLAN_LABEL_KEY[currentPlan] ? t(PLAN_LABEL_KEY[currentPlan]) : currentPlan}</div>
                </div>
                <div className="text-right">
                  <div className="text-xs text-brain-400">{t('month_spend')}</div>
                  <div className="text-lg font-mono font-semibold text-green-400">${used.toFixed(2)}</div>
                </div>
              </div>
              {typeof pool === 'number' && pool > 0 ? (
                <div className="space-y-1">
                  <div className="h-2 rounded-full bg-brain-800 overflow-hidden">
                    <div className={`h-full ${pct! >= 90 ? 'bg-red-500' : pct! >= 70 ? 'bg-amber-500' : 'bg-emerald-500'}`}
                      style={{ width: `${pct}%` }} />
                  </div>
                  <div className="flex justify-between text-[11px] text-brain-500">
                    <span>{t('included_pool', { amount: pool.toFixed(0) })}</span>
                    <span>{t('pool_remaining', { pct: pct!, amount: me.pool_remaining_usd?.toFixed?.(2) ?? '—' })}</span>
                  </div>
                </div>
              ) : (
                <div className="text-[11px] text-brain-500">
                  {me.byok ? t('byok_note') : t('no_pool')}
                </div>
              )}
            </div>
          )}

          {/* Сетка тарифов */}
          <div>
            <div className="text-sm font-medium text-brain-300 mb-2">{t('plans_title')}</div>
            <div className="grid sm:grid-cols-2 gap-2">
              {Object.entries(plans).map(([key, p]) => {
                const isCurrent = key === currentPlan
                return (
                  <div key={key}
                    className={`rounded-xl border p-3 space-y-1.5 ${isCurrent ? 'border-emerald-500/50 bg-emerald-500/5' : 'border-brain-700 bg-brain-800/20'}`}>
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-white">{PLAN_LABEL_KEY[key] ? t(PLAN_LABEL_KEY[key]) : key}</span>
                      {isCurrent && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">{t('current_badge')}</span>}
                    </div>
                    <div className="text-xl font-bold text-white">{rub(p.price_rub_month)}<span className="text-xs font-normal text-brain-400">{t('per_month')}</span></div>
                    <ul className="text-[11px] text-brain-400 space-y-0.5">
                      {p.byok && <li className="flex items-center gap-1"><Check className="w-3 h-3 text-emerald-400" />{t('feature_byok')}</li>}
                      {typeof p.included_monthly_usd === 'number' && p.included_monthly_usd > 0 &&
                        <li className="flex items-center gap-1"><Check className="w-3 h-3 text-emerald-400" />{t('feature_pool', { amount: p.included_monthly_usd })}</li>}
                      {typeof p.meetings_per_month === 'number' && p.meetings_per_month > 0 &&
                        <li className="flex items-center gap-1"><Check className="w-3 h-3 text-emerald-400" />{t('feature_meetings', { n: p.meetings_per_month })}</li>}
                      {typeof p.seats === 'number' &&
                        <li className="flex items-center gap-1"><Check className="w-3 h-3 text-emerald-400" />{t('feature_seats', { n: p.seats })}</li>}
                      {p.can_use_premium_tier &&
                        <li className="flex items-center gap-1"><Check className="w-3 h-3 text-emerald-400" />{t('feature_premium')}</li>}
                    </ul>
                    {!isCurrent && p.price_rub_month && (
                      <button
                        onClick={() => startCheckout(key)}
                        disabled={checkoutBusy === key}
                        className="w-full mt-1 px-2 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-500 disabled:opacity-40 text-white text-xs font-medium">
                        {checkoutBusy === key ? t('checkout_opening') : t('subscribe')}
                      </button>
                    )}
                    {!isCurrent && !p.price_rub_month && (
                      <div className="text-[11px] text-brain-500 mt-1">{t('by_contract')}</div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          {checkoutMsg && <div className="text-xs text-amber-300">{checkoutMsg}</div>}
          <p className="text-[11px] text-brain-500">
            {t('checkout_flow_note')}
          </p>
          <p className="text-[11px] text-brain-500">
            {t('agreement_prefix')}{' '}
            <a href="/offer" target="_blank" rel="noopener noreferrer" className="text-brain-300 hover:text-white underline">{t('agreement_offer_link')}</a>
            {' '}{t('agreement_and')}{' '}
            <a href="/terms" target="_blank" rel="noopener noreferrer" className="text-brain-300 hover:text-white underline">{t('agreement_terms_link')}</a>.
          </p>
        </div>
      </div>
    </div>
  )
}
