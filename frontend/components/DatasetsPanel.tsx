'use client'

// Онтология данных («палантир-модуль», docs/ru/DATA_ONTOLOGY.md):
// загрузка таблицы файлом/текстом/по URL → профиль + заземление колонок
// в контексте компании → вопросы цифрами с честной оценкой.
import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import {
  Database, Upload, Link2, RefreshCw, Trash2, ChevronDown, ChevronRight,
  MessageSquare, CheckCircle, AlertTriangle, Clock,
} from 'lucide-react'
import ResourcesPanel from '@/components/ResourcesPanel'
import { MetricTrendChart, MetricSparkline, TrendPoint } from '@/components/MetricTrendChart'

interface SyncSchedule { kind: 'interval' | 'daily' | 'weekly'; minutes?: number; time?: string; weekday?: number }
interface SyncState { last_at?: string; last_status?: string; last_rows?: number | null; last_error?: string | null; last_trigger?: string }

interface DatasetSlim {
  dataset_id: string
  title: string
  source: { kind: string; location?: string; provider?: string; deck?: string }
  columns: string[]
  ontology?: { is_known?: boolean; confidence?: number }
  relations?: { title: string }[]
  rows_total: number
  findings_count: number
  refreshed_at?: string
  refresh_schedule?: SyncSchedule | null
  sync?: SyncState
}

interface DatasetsPanelProps {
  userId?: string
}

export default function DatasetsPanel({ userId }: DatasetsPanelProps) {
  const t = useTranslations('datasets_panel')
  const [items, setItems] = useState<DatasetSlim[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [detail, setDetail] = useState<any | null>(null)

  // форма загрузки
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [contentB64, setContentB64] = useState('')  // .xlsx — бинарно
  const [xlsxName, setXlsxName] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)

  // вопрос
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState<any | null>(null)
  const [asking, setAsking] = useState(false)

  // CRM/BI pull: провайдеры и роуты уже готовы на бэке
  // (GET /ontology/crm/providers, POST /ontology/crm/import).
  type CrmProvider = { provider: string; ready: boolean; env_hint?: string; kinds: string[] }
  const [crmOpen, setCrmOpen] = useState(false)
  const [crmProvider, setCrmProvider] = useState('')
  const [crmKind, setCrmKind] = useState('')
  const [crmProviders, setCrmProviders] = useState<CrmProvider[]>([])

  // единые метрики (мост «цифры из встреч ↔ цифры из таблиц»)
  type MetricSlim = {
    name: string; unit?: string; points: number
    source_types: number; last_value?: number; last_at?: string
  }
  const [metrics, setMetrics] = useState<MetricSlim[]>([])
  const [metricCard, setMetricCard] = useState<any | null>(null)
  const [trends, setTrends] = useState<Record<string, TrendPoint[]>>({})

  // корректировка колонки («система поняла не так — я поправил»)
  const [editCol, setEditCol] = useState<string | null>(null)
  const [editEntity, setEditEntity] = useState('')
  const [editUnit, setEditUnit] = useState('')
  const [editScale, setEditScale] = useState('')
  const [savingEdit, setSavingEdit] = useState(false)

  // Версии датасета: история refresh'ей + дифф + обратимый откат
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [versions, setVersions] = useState<any[]>([])
  const [lastDiff, setLastDiff] = useState<any | null>(null)
  const [rollingBack, setRollingBack] = useState<number | null>(null)

  const headers = useCallback((): Record<string, string> => {
    const h: Record<string, string> = { 'Content-Type': 'application/json' }
    if (typeof window !== 'undefined') {
      const token = localStorage.getItem('tessent_access_token')
      if (token) h['Authorization'] = `Bearer ${token}`
    }
    return h
  }, [])

  const userParam = userId ? `?user_id=${userId}` : ''

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`/api/v1/ontology/datasets${userParam}`, { headers: headers() })
      const data = await res.json()
      if (res.status === 403) {
        setError(data.detail || t('disabled_hint'))
      } else {
        setItems(data.datasets || [])
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [headers, userParam, t])

  useEffect(() => { load() }, [load])

  const loadMetrics = useCallback(async () => {
    try {
      // B3: дашборд одним запросом — метрики + компактные ряды для трендов
      const res = await fetch(`/api/v1/ontology/metrics-dashboard${userParam}`, { headers: headers() })
      const data = await res.json()
      if (data?.success) {
        const items = (data.metrics || []).filter((m: MetricSlim) => m.points > 0)
        setMetrics(items)
        const tr: Record<string, TrendPoint[]> = {}
        for (const m of data.metrics || []) tr[m.name] = m.series || []
        setTrends(tr)
        return
      }
      // фолбэк на прежний list-эндпоинт (старый бэкенд)
      const res2 = await fetch(`/api/v1/ontology/metrics${userParam}`, { headers: headers() })
      const data2 = await res2.json()
      setMetrics((data2.metrics || []).filter((m: MetricSlim) => m.points > 0))
    } catch { /* метрик пока нет */ }
  }, [headers, userParam])

  useEffect(() => { loadMetrics() }, [loadMetrics])

  async function openMetric(name: string) {
    if (metricCard?.metric?.name === name) { setMetricCard(null); return }
    try {
      const res = await fetch(
        `/api/v1/ontology/metrics/${encodeURIComponent(name)}${userParam}`,
        { headers: headers() })
      const data = await res.json()
      if (data.success) setMetricCard(data)
    } catch { /* карточка не открылась — не страшно */ }
  }

  const loadCrmProviders = useCallback(async () => {
    try {
      const res = await fetch('/api/v1/ontology/crm/providers', { headers: headers() })
      const data = await res.json()
      setCrmProviders(data.providers || [])
    } catch { /* нет провайдеров — блок просто пуст */ }
  }, [headers])

  async function toggleCrm() {
    const next = !crmOpen
    setCrmOpen(next)
    if (next && crmProviders.length === 0) loadCrmProviders()
  }

  async function crmImport() {
    if (!crmProvider || !crmKind) return
    setBusy(true)
    setError('')
    try {
      const res = await fetch(`/api/v1/ontology/crm/import${userParam}`, {
        method: 'POST', headers: headers(),
        body: JSON.stringify({ provider: crmProvider, kind: crmKind }),
      })
      const data = await res.json()
      if (data.success === false || data.detail) {
        setError(data.error || data.detail)
      } else {
        setCrmKind('')
        load()
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  function openEdit(col: string, detail: any) {
    if (editCol === col) { setEditCol(null); return }
    const prof = (detail?.profile?.columns || []).find((c: any) => c.name === col) || {}
    const g = detail?.ontology?.grounding?.[col] || {}
    setEditCol(col)
    setEditEntity(g.entity_type || '')
    setEditUnit(prof.unit_symbol || prof.unit || '')
    setEditScale(prof.scale_label || '')
  }

  async function saveEdit(datasetId: string) {
    if (!editCol) return
    setSavingEdit(true)
    try {
      const res = await fetch(`/api/v1/ontology/datasets/${datasetId}/correct${userParam}`, {
        method: 'POST', headers: headers(),
        body: JSON.stringify({
          column: editCol,
          entity_type: editEntity || undefined,
          unit: editUnit,
          scale: editScale,
        }),
      })
      const data = await res.json()
      if (data.success === false) {
        setError(data.error || 'correction failed')
      } else {
        setEditCol(null)
        // перечитать деталь — чипсы и грундинг обновятся
        const r = await fetch(`/api/v1/ontology/datasets/${datasetId}${userParam}`, { headers: headers() })
        const d = await r.json()
        if (d.success) setDetail(d.dataset)
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSavingEdit(false)
    }
  }

  function onFile(f: File | null) {
    if (!f) return
    if (!title) setTitle(f.name.replace(/\.(csv|tsv|json|txt|xlsx)$/i, ''))
    const reader = new FileReader()
    if (/\.xlsx$/i.test(f.name)) {
      // Excel — бинарный: читаем в base64, бэкенд разберёт OOXML сам
      reader.onload = () => {
        const bytes = new Uint8Array(reader.result as ArrayBuffer)
        let bin = ''
        for (let i = 0; i < bytes.length; i += 0x8000) {
          bin += String.fromCharCode(...Array.from(bytes.subarray(i, i + 0x8000)))
        }
        setContentB64(btoa(bin))
        setXlsxName(f.name)
        setContent('')
      }
      reader.readAsArrayBuffer(f)
    } else {
      reader.onload = () => { setContent(String(reader.result || '')); setContentB64(''); setXlsxName('') }
      reader.readAsText(f)
    }
  }

  async function register() {
    if (!content.trim() && !url.trim() && !contentB64) return
    setBusy(true)
    setError('')
    try {
      const res = await fetch(`/api/v1/ontology/datasets${userParam}`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({
          title: title.trim(),
          content: content.trim() || undefined,
          url: url.trim() || undefined,
          content_b64: contentB64 || undefined,
          format: contentB64 ? 'xlsx' : undefined,
        }),
      })
      const data = await res.json()
      if (data.success === false || data.detail) {
        setError(data.error || data.detail)
      } else {
        setTitle(''); setContent(''); setUrl(''); setContentB64(''); setXlsxName('')
        load()
        loadMetrics()
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function toggle(id: string) {
    if (expanded === id) { setExpanded(null); setDetail(null); setAnswer(null); return }
    setExpanded(id)
    setDetail(null)
    setAnswer(null)
    setVersionsOpen(false)
    setVersions([])
    setLastDiff(null)
    const res = await fetch(`/api/v1/ontology/datasets/${id}${userParam}`, { headers: headers() })
    const data = await res.json()
    if (data.success) setDetail(data.dataset)
  }

  async function ask(id: string) {
    if (!question.trim()) return
    setAsking(true)
    setAnswer(null)
    try {
      const res = await fetch(`/api/v1/ontology/datasets/${id}/ask${userParam}`, {
        method: 'POST', headers: headers(),
        body: JSON.stringify({ question: question.trim() }),
      })
      setAnswer(await res.json())
    } catch (e) {
      setAnswer({ success: false, error: (e as Error).message })
    } finally {
      setAsking(false)
    }
  }

  async function refresh(id: string) {
    setBusy(true)
    try {
      await fetch(`/api/v1/ontology/datasets/${id}/refresh${userParam}`, {
        method: 'POST', headers: headers(),
      })
      load()
      if (versionsOpen) loadVersions(id)
    } finally { setBusy(false) }
  }

  // ── A1: авто-синк по расписанию (только для crm/url-источников) ──
  const [schedKind, setSchedKind] = useState<'interval' | 'daily' | 'weekly'>('daily')
  const [schedMinutes, setSchedMinutes] = useState('60')
  const [schedTime, setSchedTime] = useState('09:00')
  const [schedWeekday, setSchedWeekday] = useState('0')

  async function setSchedule(id: string) {
    setBusy(true)
    try {
      const spec: SyncSchedule = schedKind === 'interval'
        ? { kind: 'interval', minutes: Math.max(5, parseInt(schedMinutes) || 60) }
        : schedKind === 'daily'
          ? { kind: 'daily', time: schedTime }
          : { kind: 'weekly', time: schedTime, weekday: parseInt(schedWeekday) || 0 }
      const res = await fetch(`/api/v1/ontology/datasets/${id}/schedule${userParam}`, {
        method: 'POST', headers: headers(), body: JSON.stringify(spec),
      })
      const d = await res.json().catch(() => ({}))
      if (!d?.success) setError(d?.error || t('sync_schedule_error'))
      load()
    } finally { setBusy(false) }
  }

  async function clearSchedule(id: string) {
    setBusy(true)
    try {
      await fetch(`/api/v1/ontology/datasets/${id}/schedule${userParam}`, {
        method: 'DELETE', headers: headers(),
      })
      load()
    } finally { setBusy(false) }
  }

  async function syncNow(id: string) {
    setBusy(true)
    try {
      await fetch(`/api/v1/ontology/datasets/${id}/sync-now${userParam}`, {
        method: 'POST', headers: headers(),
      })
      load()
    } finally { setBusy(false) }
  }

  async function loadVersions(id: string) {
    try {
      const res = await fetch(`/api/v1/ontology/datasets/${id}/versions${userParam}`, { headers: headers() })
      const data = await res.json()
      if (data.success) {
        setVersions(data.versions || [])
        setLastDiff(data.last_diff || null)
      }
    } catch { /* история недоступна — не страшно */ }
  }

  function toggleVersions(id: string) {
    const next = !versionsOpen
    setVersionsOpen(next)
    if (next) loadVersions(id)
  }

  async function rollbackTo(id: string, version: number) {
    if (!confirm(t('rollback_confirm', { version }))) return
    setRollingBack(version)
    try {
      const res = await fetch(`/api/v1/ontology/datasets/${id}/rollback${userParam}`, {
        method: 'POST', headers: headers(),
        body: JSON.stringify({ version }),
      })
      const data = await res.json()
      if (data.success) {
        setVersions(data.versions || [])
        setAnswer(null)
        load()
        // перечитать деталь (профиль/строки поменялись)
        const r = await fetch(`/api/v1/ontology/datasets/${id}${userParam}`, { headers: headers() })
        const dd = await r.json()
        if (dd.success) setDetail(dd.dataset)
        loadVersions(id)
      } else {
        alert(data.error || t('rollback_failed'))
      }
    } finally { setRollingBack(null) }
  }

  async function remove(id: string) {
    if (!confirm(t('delete_confirm'))) return
    await fetch(`/api/v1/ontology/datasets/${id}${userParam}`, {
      method: 'DELETE', headers: headers(),
    })
    if (expanded === id) { setExpanded(null); setDetail(null) }
    load()
  }

  const fmtAnswer = (a: any): string =>
    typeof a === 'object' ? JSON.stringify(a, null, 1) : String(a)

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center gap-2">
        <Database className="w-5 h-5 text-indigo-400" />
        <h2 className="text-lg font-semibold">{t('title')}</h2>
        <span className="text-xs text-slate-400">{t('subtitle')}</span>
      </div>

      {/* Ресурсы компании: ссылки (сайт/прайс/шаблоны) → контекст ТЗ/документов */}
      <ResourcesPanel userId={userId} />

      {/* Загрузка */}
      <div className="p-4 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <input
            value={title}
            onChange={e => setTitle(e.target.value)}
            placeholder={t('name_placeholder')}
            className="px-3 py-1.5 rounded-lg bg-brain-900/40 border border-brain-600/30 text-sm w-64"
          />
          <label className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-brain-600/40 text-sm cursor-pointer hover:bg-brain-700/50">
            <Upload className="w-4 h-4" />
            {t('upload_file')}
            <input
              type="file" accept=".csv,.tsv,.json,.txt,.xlsx" className="hidden"
              onChange={e => onFile(e.target.files?.[0] || null)}
            />
          </label>
          {xlsxName && (
            <span className="text-[11px] text-emerald-300">📗 {xlsxName}</span>
          )}
          <span className="text-xs text-slate-500">{t('or')}</span>
          <div className="flex items-center gap-1 flex-1 min-w-[220px]">
            <Link2 className="w-4 h-4 text-slate-500" />
            <input
              value={url}
              onChange={e => setUrl(e.target.value)}
              placeholder={t('url_placeholder')}
              className="px-3 py-1.5 rounded-lg bg-brain-900/40 border border-brain-600/30 text-sm flex-1"
            />
          </div>
          <button
            onClick={register}
            disabled={busy || (!content.trim() && !url.trim() && !contentB64)}
            className="px-4 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-sm font-medium"
          >
            {busy ? t('connecting') : t('connect_button')}
          </button>
          <button
            onClick={toggleCrm}
            className={
              'px-3 py-1.5 rounded-lg border text-sm ' +
              (crmOpen
                ? 'border-indigo-500/60 text-indigo-300 bg-indigo-500/10'
                : 'border-brain-600/40 text-slate-300 hover:bg-brain-700/50')
            }
          >
            {t('crm_button')}
          </button>
        </div>
        {crmOpen && (
          <div className="p-3 rounded-lg border border-indigo-500/30 bg-indigo-500/5 space-y-2">
            <p className="text-[11px] text-gray-300">{t('crm_hint')}</p>
            <div className="flex items-center gap-2 flex-wrap">
              <select
                value={crmProvider}
                onChange={e => { setCrmProvider(e.target.value); setCrmKind('') }}
                className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs"
              >
                <option value="">{t('crm_choose_provider')}</option>
                {crmProviders.map(p => (
                  <option key={p.provider} value={p.provider} disabled={!p.ready}>
                    {p.provider}{!p.ready ? ` (${p.env_hint})` : ''}
                  </option>
                ))}
              </select>
              {crmProvider && (() => {
                const kinds: string[] = crmProviders.find(p => p.provider === crmProvider)?.kinds || []
                // BI-провайдеры: дека параметризована (table:<имя>) → текстовое поле
                const isTemplate = kinds.length > 0 && kinds[0].includes('<')
                return isTemplate ? (
                  <input
                    value={crmKind}
                    onChange={e => setCrmKind(e.target.value)}
                    placeholder={kinds[0]}
                    className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs w-56 font-mono"
                  />
                ) : (
                  <select
                    value={crmKind}
                    onChange={e => setCrmKind(e.target.value)}
                    className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs"
                  >
                    <option value="">{t('crm_choose_kind')}</option>
                    {kinds.map((k: string) => (
                      <option key={k} value={k}>{k}</option>
                    ))}
                  </select>
                )
              })()}
              <button
                onClick={crmImport}
                disabled={busy || !crmProvider || !crmKind}
                className="px-3 py-1 rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-xs font-medium"
              >
                {busy ? t('connecting') : t('crm_pull')}
              </button>
            </div>
          </div>
        )}
        <textarea
          value={content}
          onChange={e => setContent(e.target.value)}
          placeholder={t('paste_placeholder')}
          rows={3}
          className="w-full px-3 py-2 rounded-lg bg-brain-900/40 border border-brain-600/30 text-xs font-mono"
        />
        <p className="text-[11px] text-slate-500">{t('pipeline_hint')}</p>
      </div>

      {error && (
        <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-300 text-sm">{error}</div>
      )}

      {/* Единые метрики: цифры из встреч + из таблиц в одном объекте */}
      {metrics.length > 0 && (
        <div className="p-3 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-2">
          <p className="text-xs font-medium text-slate-300">{t('metrics_title')}</p>
          <div className="flex flex-wrap gap-1.5">
            {metrics.map(m => (
              <button
                key={m.name}
                onClick={() => openMetric(m.name)}
                className={
                  'text-[11px] px-2 py-1 rounded-lg border ' +
                  (m.source_types > 1
                    ? 'border-emerald-500/50 text-emerald-300 bg-emerald-500/5'
                    : 'border-brain-600/40 text-slate-300 hover:border-slate-400')
                }
                title={m.source_types > 1 ? t('metric_both_sources') : t('metric_one_source')}
              >
                {m.name}
                {typeof m.last_value === 'number' ? ` · ${Number(m.last_value.toPrecision(4))}` : ''}
                {m.unit ? ` ${m.unit}` : ''}
                {m.source_types > 1 ? ' ⚖️' : ''}
                {(trends[m.name]?.length || 0) >= 2 && (
                  <span className="ml-1.5"><MetricSparkline series={trends[m.name]} /></span>
                )}
              </button>
            ))}
          </div>
          {metricCard && (
            <div className="p-3 rounded-lg border border-emerald-500/30 bg-emerald-500/5 space-y-1">
              <p className="text-xs font-medium text-emerald-200">{metricCard.metric?.name}</p>
              {(trends[metricCard.metric?.name]?.length || 0) >= 2 && (
                <MetricTrendChart
                  series={trends[metricCard.metric.name]}
                  unit={metricCard.metric?.unit}
                  labels={{
                    fact_dataset: t('trend_fact_dataset'),
                    fact_meeting: t('trend_fact_meeting'),
                    plan: t('trend_plan'),
                  }}
                />
              )}
              {Object.entries(metricCard.latest || {}).map(([src, p]: [string, any]) => (
                <p key={src} className="text-[11px] text-slate-300">
                  {src === 'meeting' ? t('metric_from_meetings') : src === 'dataset' ? t('metric_from_data') : src}
                  {': '}{Number(Number(p.value).toPrecision(6))}
                  {metricCard.metric?.unit ? ` ${metricCard.metric.unit}` : ''}
                  {p.period ? ` · ${p.period}` : ''}
                  <span className="text-slate-500"> ({String(p.at || '').slice(0, 10)})</span>
                </p>
              ))}
              {metricCard.plan?.note && (
                <p className="text-[11px] text-sky-300">🎯 {metricCard.plan.note}</p>
              )}
              {metricCard.note && (
                <p className="text-[11px] text-amber-300">⚖️ {metricCard.note}</p>
              )}
              <p className="text-[10px] text-slate-500">{t('metric_points_hint', { count: metricCard.points_total })}</p>
            </div>
          )}
        </div>
      )}

      {/* Список */}
      {loading ? (
        <div className="flex justify-center py-8"><RefreshCw className="w-5 h-5 animate-spin text-slate-500" /></div>
      ) : items.length === 0 && !error ? (
        <p className="text-sm text-slate-500 text-center py-6">{t('empty_hint')}</p>
      ) : (
        <div className="space-y-2">
          {items.map(d => (
            <div key={d.dataset_id} className="rounded-xl border border-brain-600/30 bg-brain-800/30">
              <button
                onClick={() => toggle(d.dataset_id)}
                className="w-full flex items-center gap-3 px-4 py-3 text-left"
              >
                {expanded === d.dataset_id
                  ? <ChevronDown className="w-4 h-4 text-slate-500" />
                  : <ChevronRight className="w-4 h-4 text-slate-500" />}
                <span className="font-medium text-sm">{d.title}</span>
                <span className="text-xs text-slate-500">
                  {t('row_summary', { rows: d.rows_total, cols: d.columns?.length || 0 })}
                </span>
                {d.ontology?.is_known
                  ? <span className="flex items-center gap-1 text-[11px] text-green-400"><CheckCircle className="w-3 h-3" />{t('grounded')}</span>
                  : <span className="flex items-center gap-1 text-[11px] text-amber-400"><AlertTriangle className="w-3 h-3" />{t('not_grounded')}</span>}
                {(d.relations?.length || 0) > 0 && (
                  <span className="text-[11px] text-indigo-300">{t('relations_count', { count: d.relations!.length })}</span>
                )}
                {d.refresh_schedule && (
                  <span
                    title={d.sync?.last_error || t('sync_scheduled')}
                    className={
                      'flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border ' +
                      (d.sync?.last_status === 'error'
                        ? 'border-red-500/40 text-red-300 bg-red-500/5'
                        : d.sync?.last_status === 'ok'
                          ? 'border-green-500/40 text-green-300 bg-green-500/5'
                          : 'border-slate-500/40 text-slate-400')
                    }
                  >
                    <Clock className="w-3 h-3" />
                    {d.sync?.last_status === 'error' ? t('sync_badge_error')
                      : d.sync?.last_status === 'ok' ? t('sync_badge_ok')
                        : t('sync_badge_pending')}
                  </span>
                )}
                <span className="ml-auto text-[11px] text-slate-500">{String(d.refreshed_at || '').slice(0, 10)}</span>
                {(d.source?.kind === 'url' || d.source?.kind === 'crm') && (
                  <RefreshCw
                    className="w-3.5 h-3.5 text-slate-500 hover:text-gray-300"
                    onClick={e => { e.stopPropagation(); refresh(d.dataset_id) }}
                  />
                )}
                <Trash2
                  className="w-3.5 h-3.5 text-slate-500 hover:text-red-400"
                  onClick={e => { e.stopPropagation(); remove(d.dataset_id) }}
                />
              </button>

              {expanded === d.dataset_id && (
                <div className="px-4 pb-4 space-y-3 border-t border-brain-600/30 pt-3">
                  {/* Онтология колонок: клик по чипсу — корректировка */}
                  {detail ? (
                    <div className="space-y-2">
                      <div className="flex flex-wrap gap-1.5">
                        {(detail.profile?.columns || []).map((c: any) => {
                          const g = detail.ontology?.grounding?.[c.name] || {}
                          const unit = [c.scale_label, c.unit_symbol].filter(Boolean).join(' ')
                          return (
                            <button
                              key={c.name}
                              onClick={() => openEdit(c.name, detail)}
                              title={g.known ? `${c.name} → ${g.entity_type || g.kpi}` : t('column_unknown_hint')}
                              className={
                                'text-[11px] px-2 py-0.5 rounded border cursor-pointer ' +
                                (editCol === c.name
                                  ? 'border-indigo-400 text-indigo-200 bg-indigo-500/10'
                                  : g.known
                                    ? 'border-green-500/40 text-green-300 bg-green-500/5 hover:border-green-400'
                                    : 'border-brain-600/40 text-slate-400 hover:border-slate-400')
                              }
                            >
                              {c.name} · {c.dtype}{unit ? ` (${unit})` : ''}
                              {g.known ? ` → ${g.entity_type || 'KPI'}` : ''}
                              {g.corrected_by_user ? ' ✓' : ''}
                            </button>
                          )
                        })}
                      </div>
                      <p className="text-[10px] text-slate-500">{t('correct_click_hint')}</p>
                      {editCol && (
                        <div className="p-3 rounded-lg border border-indigo-500/30 bg-indigo-500/5 space-y-2">
                          <p className="text-xs font-medium text-indigo-200">{t('correct_title', { name: editCol })}</p>
                          <p className="text-[11px] text-slate-400">{t('correct_hint')}</p>
                          <div className="flex items-center gap-2 flex-wrap">
                            <label className="text-[11px] text-slate-400">{t('correct_entity')}</label>
                            <select
                              value={editEntity}
                              onChange={e => setEditEntity(e.target.value)}
                              className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs"
                            >
                              <option value="">{t('correct_entity_none')}</option>
                              {['person', 'department', 'team', 'project', 'product', 'kpi'].map(x => (
                                <option key={x} value={x}>{x}</option>
                              ))}
                            </select>
                            <label className="text-[11px] text-slate-400">{t('correct_unit')}</label>
                            <input
                              value={editUnit}
                              onChange={e => setEditUnit(e.target.value)}
                              placeholder={t('correct_unit_placeholder')}
                              className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs w-24"
                            />
                            <label className="text-[11px] text-slate-400">{t('correct_scale')}</label>
                            <select
                              value={editScale}
                              onChange={e => setEditScale(e.target.value)}
                              className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs"
                            >
                              <option value="">{t('correct_scale_none')}</option>
                              <option value="тыс">{t('scale_thousand')}</option>
                              <option value="млн">{t('scale_million')}</option>
                              <option value="млрд">{t('scale_billion')}</option>
                            </select>
                            <button
                              onClick={() => saveEdit(d.dataset_id)}
                              disabled={savingEdit}
                              className="px-3 py-1 rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-xs font-medium"
                            >
                              {savingEdit ? '…' : t('correct_save')}
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  ) : (
                    <RefreshCw className="w-4 h-4 animate-spin text-slate-500" />
                  )}

                  {/* Вопрос */}
                  <div className="flex items-center gap-2">
                    <MessageSquare className="w-4 h-4 text-indigo-400 shrink-0" />
                    <input
                      value={question}
                      onChange={e => setQuestion(e.target.value)}
                      onKeyDown={e => e.key === 'Enter' && ask(d.dataset_id)}
                      placeholder={t('ask_placeholder')}
                      className="px-3 py-1.5 rounded-lg bg-brain-900/40 border border-brain-600/30 text-sm flex-1"
                    />
                    <button
                      onClick={() => ask(d.dataset_id)}
                      disabled={asking || !question.trim()}
                      className="px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-sm"
                    >
                      {asking ? '…' : t('ask_button')}
                    </button>
                  </div>

                  {answer && (
                    answer.success === false ? (
                      <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs">
                        {answer.error}{answer.hint ? ` — ${answer.hint}` : ''}
                      </div>
                    ) : (
                      <div className="p-3 rounded-lg bg-brain-900/40 border border-brain-600/30 space-y-2">
                        <pre className="text-sm text-gray-100 whitespace-pre-wrap max-h-48 overflow-y-auto">
                          {fmtAnswer(answer.answer)}
                          {answer.assessment?.unit ? ` ${answer.assessment.unit}` : ''}
                        </pre>
                        <div className="flex items-center gap-2 flex-wrap text-[11px]">
                          <span className={
                            'px-1.5 py-0.5 rounded ' +
                            (answer.assessment?.confidence === 'high'
                              ? 'bg-green-500/15 text-green-300'
                              : answer.assessment?.confidence === 'medium'
                                ? 'bg-amber-500/15 text-amber-300'
                                : 'bg-red-500/15 text-red-300')
                          }>
                            confidence: {answer.assessment?.confidence}
                          </span>
                          {answer.mode === 'workbook'
                            ? <span className="px-1.5 py-0.5 rounded bg-purple-500/15 text-purple-300">🧮 {t('computed_by_code')}</span>
                            : <>
                                <span className="text-slate-400">{t('rows_used', { count: answer.rows_used })}</span>
                                <span className="text-slate-500">{answer.llm_plan_used ? t('plan_llm') : t('plan_fallback')}</span>
                              </>}
                        </div>
                        {answer.explanation && (
                          <p className="text-xs text-slate-300">{answer.explanation}</p>
                        )}
                        {answer.code && (
                          <details className="text-[11px]">
                            <summary className="text-purple-300 cursor-pointer">{t('show_pandas_code')}</summary>
                            <pre className="mt-1 p-2 rounded bg-brain-950/60 text-purple-100/90 whitespace-pre-wrap max-h-40 overflow-y-auto">{answer.code}</pre>
                          </details>
                        )}
                        {(answer.assessment?.notes || []).map((n: string, i: number) => (
                          <p key={i} className="text-[11px] text-slate-400">• {n}</p>
                        ))}
                      </div>
                    )
                  )}

                  {/* A1: авто-синк по расписанию — только для crm/url-источников */}
                  {(d.source?.kind === 'crm' || d.source?.kind === 'url') && (
                    <div className="p-3 rounded-lg border border-brain-600/30 bg-brain-900/30 space-y-2">
                      <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-slate-400">
                        <Clock className="w-3.5 h-3.5 text-indigo-300" /> {t('sync_title')}
                      </div>
                      {d.refresh_schedule ? (
                        <div className="space-y-1.5">
                          <p className="text-[11px] text-slate-300">
                            {t('sync_active', {
                              schedule: d.refresh_schedule.kind === 'interval'
                                ? t('sync_every_min', { n: d.refresh_schedule.minutes || 0 })
                                : d.refresh_schedule.kind === 'daily'
                                  ? t('sync_daily_at', { time: d.refresh_schedule.time || '' })
                                  : t('sync_weekly_at', { time: d.refresh_schedule.time || '', wd: (['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][d.refresh_schedule.weekday || 0]) }),
                            })}
                          </p>
                          {d.sync?.last_at && (
                            <p className="text-[10.5px] text-slate-500">
                              {t('sync_last', {
                                status: d.sync.last_status || '—',
                                at: String(d.sync.last_at).slice(0, 16).replace('T', ' '),
                                rows: d.sync.last_rows ?? '—',
                              })}
                              {d.sync.last_error ? ` · ${d.sync.last_error}` : ''}
                            </p>
                          )}
                          <div className="flex gap-1.5">
                            <button onClick={() => syncNow(d.dataset_id)} disabled={busy}
                              className="text-[11px] px-2 py-1 rounded-md border border-indigo-500/40 text-indigo-200 hover:bg-indigo-500/10 disabled:opacity-50">
                              {t('sync_now')}
                            </button>
                            <button onClick={() => clearSchedule(d.dataset_id)} disabled={busy}
                              className="text-[11px] px-2 py-1 rounded-md border border-brain-600/40 text-slate-400 hover:text-red-300 disabled:opacity-50">
                              {t('sync_disable')}
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="flex flex-wrap items-center gap-1.5">
                          <select value={schedKind} onChange={e => setSchedKind(e.target.value as 'interval' | 'daily' | 'weekly')}
                            className="text-[11px] px-1.5 py-1 rounded bg-brain-900/40 border border-brain-600/30">
                            <option value="interval">{t('sync_kind_interval')}</option>
                            <option value="daily">{t('sync_kind_daily')}</option>
                            <option value="weekly">{t('sync_kind_weekly')}</option>
                          </select>
                          {schedKind === 'interval' && (
                            <input type="number" min={5} value={schedMinutes} onChange={e => setSchedMinutes(e.target.value)}
                              className="w-16 text-[11px] px-1.5 py-1 rounded bg-brain-900/40 border border-brain-600/30" title={t('sync_minutes')} />
                          )}
                          {schedKind !== 'interval' && (
                            <input type="time" value={schedTime} onChange={e => setSchedTime(e.target.value)}
                              className="text-[11px] px-1.5 py-1 rounded bg-brain-900/40 border border-brain-600/30" />
                          )}
                          {schedKind === 'weekly' && (
                            <select value={schedWeekday} onChange={e => setSchedWeekday(e.target.value)}
                              className="text-[11px] px-1.5 py-1 rounded bg-brain-900/40 border border-brain-600/30">
                              {['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'].map((w, i) => <option key={i} value={i}>{w}</option>)}
                            </select>
                          )}
                          <button onClick={() => setSchedule(d.dataset_id)} disabled={busy}
                            className="text-[11px] px-2 py-1 rounded-md border border-indigo-500/40 text-indigo-200 hover:bg-indigo-500/10 disabled:opacity-50">
                            {t('sync_enable')}
                          </button>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Версии: история refresh'ей, дифф, обратимый откат */}
                  <div>
                    <button
                      onClick={() => toggleVersions(d.dataset_id)}
                      className="text-[11px] text-slate-400 hover:text-white border border-brain-600/30 hover:border-brain-500/50 rounded-md px-2.5 py-1 transition-colors"
                    >
                      🕘 {t('versions_button')}{versions.length ? ` (${versions.length})` : ''}
                    </button>
                    {versionsOpen && (
                      <div className="mt-2 p-3 rounded-lg border border-brain-600/30 bg-brain-900/30 space-y-2">
                        {lastDiff && (
                          <div className="text-[11px] text-slate-300 space-y-0.5">
                            <p className="text-slate-400 font-medium">{t('last_change', { at: String(lastDiff.at || '').slice(0, 16).replace('T', ' ') })}</p>
                            {lastDiff.rows && lastDiff.rows.old !== lastDiff.rows.new && (
                              <p>• {t('diff_rows', { oldRows: lastDiff.rows.old, newRows: lastDiff.rows.new })}</p>
                            )}
                            {Object.entries(lastDiff.sums || {}).map(([col, v]: [string, any]) => (
                              <p key={col}>
                                • Σ {col}: {v.old ?? '—'} → {v.new ?? '—'}
                                {typeof v.delta_pct === 'number' && (
                                  <span className={v.delta_pct >= 0 ? ' text-emerald-300' : ' text-rose-300'}>
                                    {' '}({v.delta_pct >= 0 ? '+' : ''}{v.delta_pct}%)
                                  </span>
                                )}
                              </p>
                            ))}
                          </div>
                        )}
                        {versions.length === 0 ? (
                          <p className="text-[11px] text-slate-500">
                            {t('versions_empty')}
                          </p>
                        ) : (
                          [...versions].reverse().map((v: any) => (
                            <div key={v.version} className="flex items-center gap-2 text-[11px] text-slate-300 border-t border-brain-700/20 pt-1.5 first:border-t-0 first:pt-0">
                              <span className="font-mono text-slate-400">v{v.version}</span>
                              <span>{String(v.saved_at || '').slice(0, 16).replace('T', ' ')}</span>
                              <span className="text-slate-500">{t('rows_count', { count: v.rows_total })}</span>
                              {v.numeric_sums && Object.keys(v.numeric_sums).length > 0 && (
                                <span className="text-slate-500 truncate max-w-[240px]">
                                  Σ {Object.entries(v.numeric_sums).map(([c, s]) => `${c}=${s}`).join(', ')}
                                </span>
                              )}
                              <button
                                onClick={() => rollbackTo(d.dataset_id, v.version)}
                                disabled={rollingBack !== null}
                                className="ml-auto px-2 py-0.5 rounded border border-amber-500/30 text-amber-300 hover:bg-amber-500/10 disabled:opacity-40"
                              >
                                {rollingBack === v.version ? '…' : `↩ ${t('rollback_button')}`}
                              </button>
                            </div>
                          ))
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
