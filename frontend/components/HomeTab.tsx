'use client'

import { useState, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch as fetch } from '@/lib/authFetch'
import ProactiveNudges from '@/components/ProactiveNudges'
import ObserverWidget from '@/components/ObserverWidget'
import { BroadcastInbox } from '@/components/StrategyBroadcast'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BookOpen,
  Calendar,
  CheckSquare,
  Database,
  FolderKanban,
  Lightbulb,
  Network,
  Radio,
  Sparkles,
  TrendingUp,
  Users,
  Zap,
} from 'lucide-react'
import { useLiveSignals } from '@/lib/useLiveSignals'
import { AnimatedNumber, MiniSparkline, TrendIndicator } from '@/components/wow/AnimatedNumber'

/**
 * HomeTab — первый экран после логина (issue: wow-screen).
 *
 * Foundry-style editorial briefing: «вот что система для тебя нашла»,
 * а не «вот все данные, разбирайся». Структура:
 *   1) Hero card — главная находка недели (top insight по priority/confidence)
 *   2) Live row — 3 карточки с числами «свежих сигналов» (пульсируют при ingest)
 *   3) Big numbers — накопленное понимание (растущие во времени)
 *   4) CTA links — быстрый переход в граф / инсайты / встречи
 *
 * Real-time pulse сейчас имитирован через CSS-анимацию + polling/30s; в
 * следующем шаге заменим на WebSocket подписку (/ws/signals).
 *
 * Принципы:
 *   - НЕ пытается заменить существующий граф/инсайты — отправляет в них через onNavigate
 *   - Использует существующие API (/api/v1/meetflow/insights, /anomalies,
 *     /api/v1/chat/graph-contents) — backend-правок ноль
 *   - Все строки в Русском inline (i18n keys добавим следующим шагом)
 */

interface HomeTabProps {
  userId: string | null
  /**
   * Колбэк для перехода в существующие вкладки. Пример:
   *   onNavigate({ tab: 'knowledge', subTab: 'graph' })
   *   onNavigate({ tab: 'knowledge', subTab: 'insights', insightId: 'X' })
   *     → откроет insights subtab и проскроллит к карточке insightId
   *     (page.tsx сохраняет insightFocusRequest, InsightsPanel подсвечивает)
   */
  onNavigate: (target: {
    tab: string
    subTab?: string
    insightId?: string
    entityIds?: string[]
  }) => void
  onOpenGuide?: () => void
}

interface Insight {
  id: string
  type?: string
  title: string
  description?: string
  priority?: 'critical' | 'high' | 'medium' | 'low'
  confidence?: number
  entities?: Array<{ name?: string; id?: string }>
  actions?: string[]
  source?: string
}

interface Anomaly {
  id: string
  severity?: 'critical' | 'high' | 'medium' | 'low'
  title?: string
  description?: string
}

interface GraphStats {
  total_nodes: number
  total_edges: number
  meetings: number
  people: number
  tasks: number
  decisions: number
  entities: number
  kpis: number
}

// issue #112 T-7 + T-9: Memory Quality summary с backend.
interface MemoryQualitySummary {
  tasks: {
    total: number
    high: number
    medium: number
    low: number
    needs_review: number
  }
  decisions: {
    total: number
    high: number
    medium: number
    low: number
    needs_review: number
  }
  overall: {
    total: number
    honest_attribution_pct: number | null
    needs_review: number
  }
}

const PRIORITY_RANK: Record<string, number> = {
  critical: 4, high: 3, medium: 2, low: 1,
}

function rankInsight(i: Insight): number {
  // Сортируем по priority, потом по confidence. Hero card — самый «жирный» сигнал.
  const p = PRIORITY_RANK[i.priority ?? 'low'] ?? 0
  const c = i.confidence ?? 0
  return p * 1000 + c * 100
}

// Фокус дня: цвет/иконка по типу (тип чередует ночная консолидация)
// label переводится в компоненте через t(labelKey) — хук нельзя звать здесь.
const FOCUS_STYLE: Record<string, { icon: string; cls: string; labelKey: string }> = {
  warning:    { icon: '⚠️', cls: 'from-amber-500/15 border-amber-500/40 text-amber-200', labelKey: 'focus_label_warning' },
  risk:       { icon: '🔥', cls: 'from-red-500/15 border-red-500/40 text-red-200', labelKey: 'focus_label_risk' },
  client:     { icon: '💼', cls: 'from-sky-500/15 border-sky-500/40 text-sky-200', labelKey: 'focus_label_client' },
  prediction: { icon: '🔮', cls: 'from-purple-500/15 border-purple-500/40 text-purple-200', labelKey: 'focus_label_prediction' },
  insight:    { icon: '💡', cls: 'from-emerald-500/15 border-emerald-500/40 text-emerald-200', labelKey: 'focus_label_insight' },
}

export default function HomeTab({ userId, onNavigate, onOpenGuide }: HomeTabProps) {
  const t = useTranslations('home_tab')
  const [dailyFocus, setDailyFocus] = useState<any>(null)
  const [dailyReport, setDailyReport] = useState<any>(null)
  const [reportOpen, setReportOpen] = useState(false)
  useEffect(() => {
    if (!userId) return
    fetch(`/api/v1/insights/focus/today?user_id=${userId}`)
      .then((r) => r.json())
      .then((d) => setDailyFocus(d.focus || null))
      .catch(() => {})
    fetch(`/api/v1/insights/report/today?user_id=${userId}`)
      .then((r) => r.json())
      .then((d) => setDailyReport(d.report || null))
      .catch(() => {})
  }, [userId])

  const [insights, setInsights] = useState<Insight[]>([])
  const [anomalies, setAnomalies] = useState<Anomaly[]>([])
  const [stats, setStats] = useState<GraphStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null)
  const [pulseKey, setPulseKey] = useState(0)  // bump для CSS-анимации при refresh
  // Sparkline в hero card: cost по дням за неделю (для самой дорогой
  // встречи). Источник — /api/v1/usage/attribution; если нет данных —
  // sparkline просто не рисуется.
  const [heroSparkline, setHeroSparkline] = useState<number[]>([])
  // Seeding демо-данных: для свежего тенанта empty state предлагает
  // «попробовать на демо», что запускает scripts/seed_demo_tenant.py.
  const [seedingDemo, setSeedingDemo] = useState(false)
  // issue #112 T-9: Memory Quality виджет — Foundry-style 'honest
  // attribution'. Если null — backend ещё не отдал данные (loading)
  // или эндпоинт недоступен; виджет в этом случае не рендерится.
  const [memQuality, setMemQuality] = useState<MemoryQualitySummary | null>(null)

  // Маркетинг «Голос рынка»: топ болей из последнего завершённого Mark-
  // исследования (боли/язык клиентов с ПРОВЕРЕННЫМИ цитатами, origin='field').
  // Задел готов (Mark research их генерит) — выводим на главную.
  const [marketing, setMarketing] = useState<
    { product: string; runId: string; pains: Array<{ text: string; quote?: string; source?: string; origin?: string }> } | null
  >(null)

  useEffect(() => {
    if (!userId) return
    let alive = true
    ;(async () => {
      try {
        const lr = await fetch(`/api/v1/mark/research?user_id=${userId}`)
        const ld = await lr.json()
        // list_runs отдаёт reversed → первый со status 'done' самый свежий
        const done = (ld?.runs || []).find((r: any) => r?.status === 'done')
        if (!done?.id) { if (alive) setMarketing(null); return }
        const dr = await fetch(`/api/v1/mark/research/${done.id}?user_id=${userId}`)
        const run = await dr.json()
        const pains: any[] = run?.insights?.pains || []
        // приоритет — подтверждённым полевой цитатой (origin='field')
        const ranked = [...pains].sort((a, b) =>
          (b?.origin === 'field' ? 1 : 0) - (a?.origin === 'field' ? 1 : 0))
        if (alive && ranked.length) {
          setMarketing({ product: run?.product || done.product || '', runId: done.id, pains: ranked.slice(0, 3) })
        } else if (alive) setMarketing(null)
      } catch { if (alive) setMarketing(null) }
    })()
    return () => { alive = false }
  }, [userId])

  // Real-time: подписываемся на /ws/signals. Каждое событие (usage_tracked,
  // insight_added, anomaly_detected) → pulseKey++ для перерисовки CSS-
  // анимации иконок. Тяжёлые события (usage_tracked, insight_added) — ещё
  // и refetch данных, чтобы числа тикали без 30s polling.
  const { lastSignal, signalCount, connected } = useLiveSignals({
    userId,
    subscribeTo: ['usage_tracked', 'insight_added', 'anomaly_detected',
                  'graph_node_added'],
  })

  // Seed demo data: вызывает backend endpoint, который генерирует
  // ~15 синтетических LLM-вызовов с реалистичным распределением расходов.
  // После успешного seed — refetch, hero card сразу заполняется.
  const handleSeedDemo = async () => {
    if (seedingDemo || !userId) return
    setSeedingDemo(true)
    try {
      const res = await fetch(`/api/v1/usage/seed-demo?user_id=${userId}`, {
        method: 'POST',
      })
      if (res.ok) {
        // Подождём чуть-чуть чтобы backend дописал в БД, потом refetch
        await new Promise((r) => setTimeout(r, 500))
        await fetchAll()
      } else {
        console.error('seed-demo failed:', res.status)
      }
    } catch (e) {
      console.error('seed-demo error:', e)
    } finally {
      setSeedingDemo(false)
    }
  }

  const fetchAll = async () => {
    if (!userId) {
      setLoading(false)
      return
    }
    try {
      // meetflow insights — продуктовые (ночной анализ встреч/сущностей);
      // weekly cost insights (шаг 3) — финансовые (поверх llm_usage,
      // дорогие встречи, surface-spike, дни-аномалии). Объединяем обе ленты —
      // у них один формат, HomeTab их не различает кроме source.
      const [insRes, weeklyRes, anomRes, statsRes, mqRes] = await Promise.all([
        fetch(`/api/v1/meetflow/insights?user_id=${userId}`),
        fetch(`/api/v1/insights/weekly?user_id=${userId}`),
        fetch(`/api/v1/meetflow/anomalies?user_id=${userId}`),
        fetch(`/api/v1/chat/graph-contents?user_id=${userId}`),
        // issue #112 T-9: Memory Quality. Прямой fetch без tenant_id —
        // backend подставляет из tenant_context (через middleware).
        fetch(`/api/v1/insights/memory-quality`),
      ])
      const allInsights: Insight[] = []
      if (insRes.ok) {
        const data = await insRes.json()
        if (Array.isArray(data.insights)) allInsights.push(...data.insights)
      }
      if (weeklyRes.ok) {
        const data = await weeklyRes.json()
        if (Array.isArray(data.insights)) allInsights.push(...data.insights)
      }
      // Дедупликация: meetflow и weekly могут описать одну и ту же сущность
      // (например meeting:mtg-X). При коллизии оставляем insight с большей
      // confidence. Ключ — type + первая entity.id (если нет — id).
      const dedupMap = new Map<string, Insight>()
      for (const ins of allInsights) {
        const entityKey = ins.entities?.[0]?.id ?? ins.id
        const key = `${ins.type ?? 'unknown'}:${entityKey}`
        const existing = dedupMap.get(key)
        const insScore = ins.confidence ?? 0
        const existingScore = existing?.confidence ?? -1
        if (!existing || insScore > existingScore) {
          dedupMap.set(key, ins)
        }
      }
      setInsights(Array.from(dedupMap.values()))
      if (anomRes.ok) {
        const data = await anomRes.json()
        setAnomalies(Array.isArray(data.anomalies) ? data.anomalies : [])
      }
      if (statsRes.ok) {
        const data = await statsRes.json()
        setStats(data?.stats ?? null)
      }
      if (mqRes.ok) {
        const data = await mqRes.json()
        // Если total=0, виджет показывает empty-state. Если error от
        // backend (например graph_builder ещё не поднят) — null.
        if (data && !data.error && data.overall) {
          setMemQuality(data as MemoryQualitySummary)
        }
      }
      setLastUpdatedAt(new Date())
      setPulseKey((k) => k + 1)
    } catch (e) {
      console.error('HomeTab: fetchAll failed:', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAll()
    // Polling fallback (60s — реже, потому что WS даёт push'и).
    // Если WS зашёл и работает — лишний fetch скоро уберём, пока оставим
    // как safety-net на случай WebSocket-проблем.
    const id = setInterval(fetchAll, 60_000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId])

  // Sparkline для hero card: дёргаем /api/v1/usage/attribution?group_by=meeting_id
  // → получаем top-1 meeting → fetch его /usage/by-meeting/{id} с разбивкой по
  // recent (нет дневной агрегации в endpoint'е сейчас — пока подсчитаем
  // буквы из recent timestamps клиентски). Если нет данных — просто не рисуем.
  useEffect(() => {
    if (!userId) return
    const heroInsight = [...insights].sort(
      (a, b) => rankInsight(b) - rankInsight(a),
    )[0]
    const meetingId = heroInsight?.entities?.[0]?.id
    if (!meetingId || !heroInsight?.type?.includes('expensive_meeting')) {
      setHeroSparkline([])
      return
    }
    ;(async () => {
      try {
        const res = await fetch(`/api/v1/usage/by-meeting/${meetingId}`)
        if (!res.ok) return
        const data = await res.json()
        const recent: Array<{ timestamp?: string; total_cost?: number }> =
          data.recent ?? []
        // Группируем cost по дням за последние 7 дней
        const byDay: Record<string, number> = {}
        const today = new Date()
        for (let i = 6; i >= 0; i--) {
          const d = new Date(today)
          d.setDate(today.getDate() - i)
          byDay[d.toISOString().slice(0, 10)] = 0
        }
        for (const r of recent) {
          const day = (r.timestamp ?? '').slice(0, 10)
          if (day in byDay) byDay[day] += r.total_cost ?? 0
        }
        const arr = Object.values(byDay)
        if (arr.some((v) => v > 0)) {
          setHeroSparkline(arr)
        }
      } catch (e) {
        // sparkline опциональна — не падаем
      }
    })()
  }, [userId, insights])

  // Real-time: при WS-событии обновляем pulseKey (для CSS-анимации) и,
  // если событие «весомое» (новый insight / аномалия), делаем refetch.
  // usage_tracked-события тикают часто (каждый LLM-вызов), поэтому
  // только pulseKey++, без refetch (числа обновим следующим polling'ом).
  useEffect(() => {
    if (!lastSignal) return
    setPulseKey((k) => k + 1)
    if (lastSignal.type === 'insight_added'
        || lastSignal.type === 'anomaly_detected') {
      fetchAll()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signalCount])

  const heroInsight = [...insights].sort((a, b) => rankInsight(b) - rankInsight(a))[0]
  const criticalAnomalies = anomalies.filter(
    (a) => a.severity === 'critical' || a.severity === 'high',
  )

  // Свежесть подписи. Без секундомера; OSIRIS-урок про «движение = жизнь»
  // здесь даёт CSS-анимация pulse на иконке.
  const updatedLabel = lastUpdatedAt
    ? t('updated_at_time', { time: lastUpdatedAt.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) })
    : t('loading_ellipsis')

  return (
    <div className="h-full overflow-auto p-6 space-y-6">
      {/* === Header полоска: бренд + статус === */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-gradient flex items-center gap-3">
            <Sparkles className="w-6 h-6 text-brain-400 brain-pulse" />
            {t('what_system_found_title')}
          </h2>
          <p className="text-sm text-brain-400 mt-1 flex items-center gap-2">
            <span>{t('weekly_signals_subtitle', { updated: updatedLabel })}</span>
            {/* Live-индикатор: зелёная точка-пульс когда WS подключён,
                серая при reconnect. OSIRIS-урок про «видимая жизнь» — даже
                если данных нет, индикатор «онлайн» создаёт ощущение
                работающей системы. */}
            <LiveIndicator connected={connected} signalCount={signalCount} />
          </p>
        </div>
        <button
          onClick={fetchAll}
          className="text-xs text-brain-400 hover:text-brain-300 px-3 py-1.5 rounded-md hover:bg-brain-800/40 transition-colors"
          title={t('refresh_title')}
        >
          {t('refresh_button')}
        </button>
      </div>

      {/* === ФОКУС ДНЯ: система сама выбрала самое важное сейчас === */}
      {dailyFocus && (() => {
        const st = FOCUS_STYLE[dailyFocus.type] || FOCUS_STYLE.insight
        return (
          <div
            className={`bg-gradient-to-r ${st.cls} to-transparent border rounded-xl p-4 cursor-pointer hover:brightness-110 transition-all`}
            onClick={() => onNavigate({ tab: 'knowledge', subTab: 'insights' } as any)}
            title={t('open_insights_feed_title')}
          >
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider mb-1 opacity-90">
              <span className="text-base">{st.icon}</span> {t('daily_focus_label')} · {t(st.labelKey)}
            </div>
            <div className="text-white font-semibold">{dailyFocus.title}</div>
            {dailyFocus.description && (
              <p className="text-sm opacity-80 mt-1">{dailyFocus.description}</p>
            )}
          </div>
        )
      })()}

      {/* Наблюдатель: агент, который смотрит на компанию с разных фронтов */}
      <ObserverWidget />

      {/* Проактивная подсказка: система сама рассказывает, чем полезна */}
      <ProactiveNudges onNavigate={onNavigate} onOpenGuide={onOpenGuide} userId={userId || undefined} />

      {/* Послание руководителя — персональная версия под фрейм сотрудника */}
      <BroadcastInbox />

      {/* === МАРКЕТИНГ: голос рынка (боли клиентов с полевыми цитатами) === */}
      {marketing && marketing.pains.length > 0 && (
        <div
          onClick={() => onNavigate({ tab: 'mark' } as any)}
          className="cursor-pointer bg-gradient-to-br from-pink-500/10 to-purple-500/5 border border-pink-500/30 rounded-xl p-4 hover:border-pink-500/50 transition-colors"
        >
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-pink-300">
              <span>📣</span> {t('market_voice_title')}
              {marketing.product && <span className="text-slate-500 normal-case font-normal">· {marketing.product.slice(0, 40)}</span>}
            </div>
            <span className="text-pink-300/70 text-xs">Mark →</span>
          </div>
          <ul className="space-y-1.5">
            {marketing.pains.map((p, i) => (
              <li key={i} className="text-sm text-slate-200">
                <span className="text-pink-400 mr-1">•</span>{p.text}
                {p.origin === 'field' && p.quote && (
                  <span className="block ml-4 mt-0.5 text-xs text-slate-400 italic">{t('field_quote', { quote: String(p.quote).slice(0, 140) })}</span>
                )}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[11px] text-slate-500">{t('field_quote_footnote')}</p>
        </div>
      )}

      {/* === ОТЧЁТ ДНЯ: формат чередуется (дайджест/люди/клиенты/риски/проекты) === */}
      {dailyReport && (
        <div className="bg-brain-800/30 border border-brain-600/30 rounded-xl overflow-hidden">
          <button
            onClick={() => setReportOpen(!reportOpen)}
            className="w-full flex items-center justify-between px-4 py-2.5 text-left hover:bg-brain-700/20 transition-colors"
          >
            <span className="text-white text-sm font-semibold">{dailyReport.title}</span>
            <span className="text-slate-500 text-xs">{reportOpen ? t('collapse_label') : t('expand_label')}</span>
          </button>
          {reportOpen && (
            <pre className="px-4 pb-3 text-slate-300 text-xs whitespace-pre-wrap font-sans">
              {dailyReport.text}
            </pre>
          )}
        </div>
      )}

      {/* === HERO: главная находка недели ===
          Если инсайтов нет — показываем продакт-сэлл empty state с
          двумя CTA: «попробовать на демо» (запускает seed) и «открыть
          граф». */}
      {loading ? (
        <HeroSkeleton />
      ) : heroInsight ? (
        <HeroInsightCard
          key={`hero-${pulseKey}`}
          insight={heroInsight}
          sparklineData={heroSparkline}
          onOpenDetails={() => onNavigate({
            tab: 'knowledge',
            subTab: 'insights',
            insightId: heroInsight.id,  // фокус на конкретный insight в InsightsPanel
          })}
          onOpenGraph={() => onNavigate({
            tab: 'knowledge',
            subTab: 'graph',
            entityIds: (heroInsight.entities ?? []).map((e) => e.id ?? e.name).filter(Boolean) as string[],
          })}
        />
      ) : (
        <HeroEmptyState
          seeding={seedingDemo}
          onSeedDemo={handleSeedDemo}
          onOpenGraph={() => onNavigate({ tab: 'knowledge', subTab: 'graph' })}
        />
      )}

      {/* === LIVE ROW: 3 карточки «свежих сигналов» === */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <LiveSignalCard
          icon={AlertTriangle}
          color="text-orange-400"
          glowColor="shadow-orange-500/10"
          title={t('anomalies_title')}
          count={criticalAnomalies.length}
          subtitle={
            criticalAnomalies.length === 0
              ? t('all_calm')
              : t('need_attention_count', { count: criticalAnomalies.length })
          }
          pulseKey={pulseKey}
          onClick={() => onNavigate({ tab: 'knowledge', subTab: 'insights' })}
        />
        <LiveSignalCard
          icon={Lightbulb}
          color="text-yellow-400"
          glowColor="shadow-yellow-500/10"
          title={t('insights_title')}
          count={insights.length}
          subtitle={
            insights.length === 0 ? t('nothing_accumulated_yet') : t('fresh_findings')
          }
          pulseKey={pulseKey}
          onClick={() => onNavigate({ tab: 'knowledge', subTab: 'insights' })}
        />
        <LiveSignalCard
          icon={Activity}
          color="text-green-400"
          glowColor="shadow-green-500/10"
          title={t('accumulated_title')}
          count={stats?.total_nodes ?? 0}
          subtitle={t('edges_count', { count: stats?.total_edges ?? 0 })}
          pulseKey={pulseKey}
          onClick={() => onNavigate({ tab: 'knowledge', subTab: 'graph' })}
        />
      </div>

      {/* === MEMORY QUALITY: «честная атрибуция» (issue #112 T-9) ===
          Foundry-style: показываем не «100% точность» (вранье), а
          распределение по уверенности. Система говорит, когда не знает.
          Виджет рендерится только если есть данные (memQuality !== null
          и overall.total > 0). */}
      {memQuality && memQuality.overall.total > 0 && (
        <MemoryQualityBlock
          summary={memQuality}
          onOpenReview={() => onNavigate({
            tab: 'knowledge', subTab: 'review',
          })}
        />
      )}

      {/* === BIG NUMBERS: накопленное понимание ===
          Это и есть «density per pixel» урок от OSIRIS — большие числа
          сразу показывают масштаб того, что система уже знает. */}
      <div>
        <h3 className="text-sm font-semibold text-brain-300 mb-3 flex items-center gap-2">
          <Database className="w-4 h-4 text-brain-400" />
          {t('what_we_know_title')}
        </h3>
        <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
          <BigNumberCard
            icon={Users} color="text-green-400"
            label={t('people_label')} value={stats?.people ?? 0} loading={loading}
          />
          <BigNumberCard
            icon={Calendar} color="text-orange-400"
            label={t('meetings_label')} value={stats?.meetings ?? 0} loading={loading}
          />
          <BigNumberCard
            icon={CheckSquare} color="text-purple-400"
            label={t('tasks_label')} value={stats?.tasks ?? 0} loading={loading}
          />
          <BigNumberCard
            icon={Lightbulb} color="text-red-400"
            label={t('decisions_label')} value={stats?.decisions ?? 0} loading={loading}
          />
          <BigNumberCard
            icon={FolderKanban} color="text-blue-400"
            label={t('entities_label')} value={stats?.entities ?? 0} loading={loading}
          />
          <BigNumberCard
            icon={Network} color="text-brain-300"
            label={t('nodes_label')} value={stats?.total_nodes ?? 0} loading={loading}
          />
        </div>
      </div>

      {/* === CTA ROW === */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 pt-2">
        <NavigateCard
          icon={Network} title={t('open_knowledge_graph')}
          subtitle={t('open_knowledge_graph_subtitle')}
          onClick={() => onNavigate({ tab: 'knowledge', subTab: 'graph' })}
        />
        <NavigateCard
          icon={Lightbulb} title={t('all_insights_anomalies')}
          subtitle={t('all_insights_anomalies_subtitle')}
          onClick={() => onNavigate({ tab: 'knowledge', subTab: 'insights' })}
        />
        <NavigateCard
          icon={BookOpen} title={t('documents_meetings')}
          subtitle={t('documents_meetings_subtitle')}
          onClick={() => onNavigate({ tab: 'knowledge', subTab: 'documents' })}
        />
      </div>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────
// Subcomponents
// ────────────────────────────────────────────────────────────────────────

// Извлечь $-сумму из title/description weekly cost insights — для
// большого AnimatedNumber справа. Backend пока её не отдаёт отдельным
// полем; pull request на добавление insight.payload.cost — TODO.
function extractCostFromInsight(insight: Insight): number | null {
  const text = `${insight.title ?? ''} ${insight.description ?? ''}`
  const m = text.match(/\$\s*([0-9]+(?:[.,][0-9]+)?)/)
  if (!m) return null
  return parseFloat(m[1].replace(',', '.'))
}

function HeroInsightCard({
  insight, onOpenDetails, onOpenGraph, sparklineData,
}: {
  insight: Insight
  onOpenDetails: () => void
  onOpenGraph: () => void
  sparklineData?: number[]
}) {
  const t = useTranslations('home_tab')
  const isCritical = insight.priority === 'critical' || insight.priority === 'high'
  const cost = extractCostFromInsight(insight)
  return (
    <div
      className={`
        relative card card-glow overflow-hidden
        ${isCritical
          ? 'bg-gradient-to-br from-brain-900/70 via-brain-900/60 to-orange-900/30 border-orange-500/30'
          : 'bg-gradient-to-br from-brain-900/70 via-brain-900/60 to-brain-800/40 border-brain-500/30'}
      `}
    >
      {/* Декоративный glow в углу, увеличен и более насыщенный (#7 hero visual mass) */}
      <div
        className={`
          absolute -top-32 -right-32 w-96 h-96 rounded-full blur-3xl opacity-30
          ${isCritical ? 'bg-orange-500' : 'bg-brain-500'}
        `}
      />
      {/* Второй декоративный elements — диагональный градиент сетки */}
      <div className="absolute inset-0 opacity-[0.04] pointer-events-none"
           style={{
             backgroundImage: 'radial-gradient(circle at 1px 1px, white 1px, transparent 0)',
             backgroundSize: '24px 24px',
           }} />
      <div className="relative z-10 flex flex-col md:flex-row gap-6 md:gap-8">
        {/* Левый столбец: иконка + badge */}
        <div className="flex-shrink-0 flex flex-col items-center md:items-start gap-2">
          <div
            className={`
              w-16 h-16 rounded-2xl flex items-center justify-center
              ${isCritical
                ? 'bg-orange-500/20 ring-1 ring-orange-500/30'
                : 'bg-brain-500/20 ring-1 ring-brain-500/30'}
            `}
          >
            <Zap
              className={`
                w-9 h-9 ${isCritical ? 'text-orange-300' : 'text-brain-300'}
                brain-pulse
              `}
            />
          </div>
          <div className={`badge ${isCritical ? 'badge-warning' : 'badge-info'}`}>
            {insight.type ?? t('finding_fallback')}
          </div>
        </div>

        {/* Центральный столбец: title + description + entities + actions */}
        <div className="flex-1 min-w-0">
          <div className="text-xs text-brain-400 uppercase tracking-wider mb-1">
            {t('weekly_highlight')}
          </div>
          <h3 className="text-2xl md:text-3xl font-bold text-brain-50 leading-tight">
            {insight.title}
          </h3>
          {insight.description && (
            <p className="text-sm text-brain-300 mt-2 leading-relaxed max-w-prose">
              {insight.description}
            </p>
          )}
          {insight.entities && insight.entities.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-3">
              {insight.entities.slice(0, 8).map((e, idx) => (
                <span
                  key={idx}
                  className="text-xs bg-brain-800/60 text-brain-200 px-2 py-1 rounded-md border border-brain-700/40"
                >
                  {e.name ?? e.id}
                </span>
              ))}
              {insight.entities.length > 8 && (
                <span className="text-xs text-brain-500 px-1 py-1">
                  +{insight.entities.length - 8}
                </span>
              )}
            </div>
          )}
          {insight.actions && insight.actions.length > 0 && (
            <div className="mt-3 text-sm text-brain-300">
              💡 <span className="text-brain-200">{insight.actions[0]}</span>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-3 mt-5">
            <button
              onClick={onOpenDetails}
              className="btn btn-primary text-sm inline-flex items-center gap-2 px-5 py-2.5 rounded-lg bg-brain-600 hover:bg-brain-500 text-white transition-colors shadow-lg shadow-brain-500/20"
            >
              {t('explore_button')} <ArrowRight className="w-4 h-4" />
            </button>
            <button
              onClick={onOpenGraph}
              className="text-sm text-brain-300 hover:text-brain-100 px-3 py-2.5 rounded-lg hover:bg-brain-800/40 transition-colors inline-flex items-center gap-2"
            >
              <Network className="w-4 h-4" /> {t('view_in_graph')}
            </button>
            {insight.confidence !== undefined && (
              <span className="text-xs text-brain-500 ml-auto">
                {t('confidence_pct', { pct: Math.round((insight.confidence ?? 0) * 100) })}
              </span>
            )}
          </div>
        </div>

        {/* Правый столбец: большой cost-display + sparkline (только для
            cost-related insights, где удалось вытащить число из title).
            Создаёт «вот тебе факт на $X в лицо» эффект. */}
        {cost !== null && (
          <div className="flex-shrink-0 hidden md:flex flex-col items-end justify-center gap-2 pl-4 border-l border-brain-700/40">
            <div className="text-xs text-brain-400 uppercase tracking-wider">
              {t('cost_label')}
            </div>
            <div className={`
              text-4xl lg:text-5xl font-bold leading-none
              ${isCritical ? 'text-orange-300' : 'text-brain-200'}
            `}>
              $<AnimatedNumber value={cost} decimals={2} />
            </div>
            {sparklineData && sparklineData.length >= 2 && (
              <div className="opacity-70">
                <MiniSparkline
                  data={sparklineData}
                  color={isCritical ? '#fb923c' : '#36a7f6'}
                  width={120}
                  height={40}
                />
              </div>
            )}
            <div className="text-xs text-brain-500">{t('per_week_label')}</div>
          </div>
        )}
      </div>
    </div>
  )
}

function HeroEmptyState({
  onOpenGraph, onSeedDemo, seeding,
}: {
  onOpenGraph: () => void
  onSeedDemo: () => void
  seeding: boolean
}) {
  const t = useTranslations('home_tab')
  // Empty state с продактом: вместо «Пока нечего» — три причины,
  // почему это нормально, и две CTA (одна для demo, вторая для graph).
  // Главная — «Попробовать на демо-данных» — закрывает «новый тенант
  // видит пустой wow-screen» проблему.
  return (
    <div className="relative card card-glow overflow-hidden py-10 bg-gradient-to-br from-brain-900/60 via-brain-900/50 to-brain-800/30">
      <div className="absolute -top-32 -right-32 w-96 h-96 rounded-full blur-3xl opacity-15 bg-brain-500" />
      <div className="relative z-10 flex flex-col md:flex-row gap-8 items-center md:items-start">
        {/* Иллюстрация: пульсирующий мозг как намёк на «думает» */}
        <div className="flex-shrink-0">
          <div className="w-24 h-24 rounded-full bg-gradient-to-br from-brain-500/20 to-brain-700/40 flex items-center justify-center brain-pulse">
            <Sparkles className="w-12 h-12 text-brain-300" />
          </div>
        </div>

        <div className="flex-1 text-center md:text-left">
          <h3 className="text-xl md:text-2xl font-bold text-brain-50">
            {t('system_ready_title')}
          </h3>
          <p className="text-sm text-brain-300 mt-2 max-w-prose">
            {t('empty_state_intro')}
          </p>
          <ul className="mt-3 space-y-1.5 text-sm text-brain-300">
            <li className="flex items-start gap-2">
              <Zap className="w-4 h-4 text-brain-400 mt-0.5 flex-shrink-0" />
              <span>{t('empty_bullet_top_insight')}</span>
            </li>
            <li className="flex items-start gap-2">
              <Activity className="w-4 h-4 text-green-400 mt-0.5 flex-shrink-0" />
              <span>{t('empty_bullet_live_signals')}</span>
            </li>
            <li className="flex items-start gap-2">
              <Network className="w-4 h-4 text-brain-400 mt-0.5 flex-shrink-0" />
              <span>{t('empty_bullet_graph')}</span>
            </li>
          </ul>

          <div className="flex flex-wrap gap-3 mt-5 justify-center md:justify-start">
            <button
              onClick={onSeedDemo}
              disabled={seeding}
              className={`
                inline-flex items-center gap-2 px-5 py-2.5 rounded-lg
                bg-brain-600 hover:bg-brain-500 text-white text-sm
                transition-colors shadow-lg shadow-brain-500/20
                ${seeding ? 'opacity-60 cursor-wait' : ''}
              `}
            >
              <Sparkles className="w-4 h-4" />
              {seeding ? t('seeding_demo_progress') : t('try_demo_data')}
            </button>
            <button
              onClick={onOpenGraph}
              className="text-sm text-brain-300 hover:text-brain-100 inline-flex items-center gap-2 px-3 py-2.5"
            >
              {t('open_empty_graph')} <ArrowRight className="w-4 h-4" />
            </button>
          </div>
          <p className="text-xs text-brain-500 mt-3">
            {t('demo_seed_note')}
          </p>
        </div>
      </div>
    </div>
  )
}

function HeroSkeleton() {
  return (
    <div className="card card-glow bg-brain-900/40">
      <div className="flex gap-6">
        <div className="w-14 h-14 rounded-2xl bg-brain-800/60 animate-pulse" />
        <div className="flex-1 space-y-3">
          <div className="h-3 w-32 bg-brain-800/60 rounded animate-pulse" />
          <div className="h-7 w-3/4 bg-brain-800/60 rounded animate-pulse" />
          <div className="h-4 w-full bg-brain-800/40 rounded animate-pulse" />
          <div className="h-4 w-1/2 bg-brain-800/40 rounded animate-pulse" />
        </div>
      </div>
    </div>
  )
}

function LiveSignalCard({
  icon: Icon, color, glowColor, title, count, subtitle, pulseKey, onClick,
}: {
  icon: React.ComponentType<{ className?: string }>
  color: string
  glowColor: string
  title: string
  count: number
  subtitle: string
  pulseKey: number
  onClick: () => void
}) {
  // pulseKey используется как key для перерисовки иконки — простое
  // «мигание» при обновлении (заглушка до WebSocket-real-time).
  return (
    <button
      onClick={onClick}
      className={`
        card card-glow ${glowColor} text-left w-full
        hover:border-brain-400/60 hover:translate-y-[-1px] transition-all
        flex items-center gap-4
      `}
    >
      <div className="flex-shrink-0">
        <Icon key={pulseKey} className={`w-8 h-8 ${color} brain-pulse`} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-xs text-brain-400 uppercase tracking-wider">
          {title}
        </div>
        <div className="text-2xl font-bold text-brain-50 leading-tight">
          <AnimatedNumber value={count} />
        </div>
        <div className="text-xs text-brain-500 truncate">{subtitle}</div>
      </div>
      <ArrowRight className="w-4 h-4 text-brain-600 flex-shrink-0" />
    </button>
  )
}

function BigNumberCard({
  icon: Icon, color, label, value, loading,
}: {
  icon: React.ComponentType<{ className?: string }>
  color: string
  label: string
  value: number
  loading: boolean
}) {
  return (
    <div className="card text-center">
      <Icon className={`w-5 h-5 ${color} mx-auto mb-1.5`} />
      <div className="text-xl font-bold text-brain-50">
        {loading ? (
          '…'
        ) : (
          // Tweening 600ms ease-out — даже если число не меняется,
          // первый рендер мгновенный (см. AnimatedNumber.firstRenderRef)
          <AnimatedNumber value={value} />
        )}
      </div>
      <div className="text-xs text-brain-400">{label}</div>
    </div>
  )
}

function MemoryQualityBlock({
  summary, onOpenReview,
}: {
  summary: MemoryQualitySummary
  onOpenReview: () => void
}) {
  const t = useTranslations('home_tab')
  const { overall, tasks, decisions } = summary
  const highCount = tasks.high + decisions.high
  const mediumCount = tasks.medium + decisions.medium
  const lowCount = tasks.low + decisions.low
  const reviewCount = overall.needs_review
  const total = overall.total
  const pct = overall.honest_attribution_pct ?? 0

  // Bar-chart segments — пропорциональная ширина. Минимум 2% чтобы
  // видеть даже одиночные сегменты.
  const seg = (n: number) => {
    if (total === 0) return 0
    const raw = (n / total) * 100
    return n > 0 ? Math.max(raw, 2) : 0
  }

  // Цветовая логика: если honest_attribution_pct >= 80% — состояние OK
  // (зелёный акцент), если 50-80 — нейтральный синий, < 50 — оранжевый
  // (нужно вмешательство).
  const stateAccent =
    pct >= 80 ? 'text-green-400'
      : pct >= 50 ? 'text-brain-300'
      : 'text-orange-400'

  return (
    <div className="card card-glow bg-gradient-to-br from-brain-900/60 to-brain-800/30">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-brain-300 flex items-center gap-2">
          <Activity className="w-4 h-4 text-brain-400" />
          {t('memory_quality_title')}
        </h3>
        <div className={`text-2xl font-bold ${stateAccent}`}>
          <AnimatedNumber value={pct} decimals={1} suffix="%" />
        </div>
      </div>

      {/* Stacked bar: high / medium / low / review */}
      <div className="flex h-2 rounded-full overflow-hidden bg-brain-900/60 mb-3">
        {seg(highCount) > 0 && (
          <div
            className="bg-green-400/80 transition-all duration-500"
            style={{ width: `${seg(highCount)}%` }}
            title={`${highCount} high confidence`}
          />
        )}
        {seg(mediumCount) > 0 && (
          <div
            className="bg-brain-400/80 transition-all duration-500"
            style={{ width: `${seg(mediumCount)}%` }}
            title={`${mediumCount} medium confidence`}
          />
        )}
        {seg(lowCount) > 0 && (
          <div
            className="bg-orange-400/80 transition-all duration-500"
            style={{ width: `${seg(lowCount)}%` }}
            title={t('low_confidence_tooltip', { count: lowCount })}
          />
        )}
      </div>

      {/* Числа под bar'ом */}
      <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-green-400" />
          <AnimatedNumber value={highCount} className="font-bold text-brain-100" />
          <span className="text-brain-500">{t('high_confidence_label')}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-brain-400" />
          <AnimatedNumber value={mediumCount} className="font-bold text-brain-100" />
          <span className="text-brain-500">{t('medium_confidence_label')}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-orange-400" />
          <AnimatedNumber value={reviewCount} className="font-bold text-brain-100" />
          <span className="text-brain-500">{t('needs_review_label')}</span>
        </div>
        <button
          onClick={onOpenReview}
          className="ml-auto text-xs text-brain-300 hover:text-brain-100 inline-flex items-center gap-1 transition-colors"
        >
          {t('open_review_queue')} <ArrowRight className="w-3 h-3" />
        </button>
      </div>

      {/* Под-секции с разбиением по типу (compact mode) */}
      <div className="mt-3 pt-3 border-t border-brain-800/40 grid grid-cols-2 gap-3 text-xs">
        <div>
          <span className="text-brain-500">{t('tasks_count_label')} </span>
          <AnimatedNumber value={tasks.total} className="text-brain-200 font-bold" />
          <span className="text-brain-500">
            {' '}{t('confidence_breakdown', { high: tasks.high, review: tasks.needs_review })}
          </span>
        </div>
        <div>
          <span className="text-brain-500">{t('decisions_count_label')} </span>
          <AnimatedNumber value={decisions.total} className="text-brain-200 font-bold" />
          <span className="text-brain-500">
            {' '}{t('confidence_breakdown', { high: decisions.high, review: decisions.needs_review })}
          </span>
        </div>
      </div>

      {/* Foundry-style honesty footer */}
      <div className="mt-3 text-xs text-brain-600 italic">
        {t('honest_memory_footer')}
      </div>
    </div>
  )
}

function NavigateCard({
  icon: Icon, title, subtitle, onClick,
}: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  subtitle: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className="card text-left w-full hover:border-brain-400/60 transition-all group flex items-center gap-3"
    >
      <Icon className="w-5 h-5 text-brain-400 flex-shrink-0" />
      <div className="flex-1">
        <div className="text-sm font-medium text-brain-100 group-hover:text-white">
          {title}
        </div>
        <div className="text-xs text-brain-500 mt-0.5">{subtitle}</div>
      </div>
      <ArrowRight className="w-4 h-4 text-brain-600 group-hover:text-brain-400 transition-colors" />
    </button>
  )
}

function LiveIndicator({
  connected, signalCount,
}: { connected: boolean; signalCount: number }) {
  const t = useTranslations('home_tab')
  return (
    <span
      className={`
        inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full
        ${connected
          ? 'bg-green-500/10 text-green-400 border border-green-500/30'
          : 'bg-brain-800/40 text-brain-500 border border-brain-700/30'}
      `}
      title={connected
        ? t('realtime_connected_tooltip', { count: signalCount })
        : t('realtime_disconnected_tooltip')}
    >
      <Radio
        key={signalCount}  // bump key для перерисовки CSS-pulse на новых событиях
        className={`w-3 h-3 ${connected ? 'brain-pulse' : ''}`}
      />
      {connected ? 'live' : 'offline'}
    </span>
  )
}

// Дополнительный тонкий тренд-индикатор — пригодится в шаге 3 когда
// backend начнёт отдавать дельты (например «+12 встреч за неделю»).
export function TrendBadge({ value }: { value: number }) {
  const positive = value >= 0
  return (
    <span
      className={`inline-flex items-center text-xs ${
        positive ? 'text-green-400' : 'text-orange-400'
      }`}
    >
      <TrendingUp className={`w-3 h-3 mr-0.5 ${positive ? '' : 'rotate-180'}`} />
      {positive ? '+' : ''}{value}
    </span>
  )
}
