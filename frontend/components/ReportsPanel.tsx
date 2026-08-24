'use client'

import { useState, useEffect, useCallback } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  FileText,
  RefreshCw,
  AlertCircle,
  Loader2,
  Sparkles,
  ArrowLeft,
  Clock,
  Users,
} from 'lucide-react'

interface MethodologyType {
  id: string
  name: string
  icon?: string
  description?: string
}

interface MethodologyReport {
  id: string
  report_type?: string
  title: string
  icon?: string
  content_text?: string
  summary?: string
  meetings_count?: number
  created_at?: string
}

// API functions
const API_BASE = process.env.NEXT_PUBLIC_API_URL || '' // '' → Next.js rewrites proxy /api → backend

function getAuthHeaders(): Record<string, string> {
  const token =
    typeof window !== 'undefined'
      ? localStorage.getItem('tessent_access_token') || localStorage.getItem('access_token')
      : null
  if (token) {
    return { Authorization: `Bearer ${token}` }
  }
  return {}
}

function ReportMarkdown({ content }: { content: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node, ...props }) => (
            <a {...props} target="_blank" rel="noopener noreferrer" className="text-brain-400 hover:underline" />
          ),
          ul: ({ node, ...props }) => <ul {...props} className="list-disc pl-4 space-y-1" />,
          ol: ({ node, ...props }) => <ol {...props} className="list-decimal pl-4 space-y-1" />,
          li: ({ node, ...props }) => <li {...props} className="pl-1" />,
          table: ({ node, ...props }) => (
            <div className="overflow-x-auto my-4">
              <table {...props} className="min-w-full divide-y divide-brain-700/50" />
            </div>
          ),
          thead: ({ node, ...props }) => <thead {...props} className="bg-brain-800/50" />,
          th: ({ node, ...props }) => (
            <th {...props} className="px-3 py-2 text-left text-xs font-medium text-brain-300 uppercase tracking-wider" />
          ),
          tbody: ({ node, ...props }) => <tbody {...props} className="divide-y divide-brain-700/30 bg-brain-900/20" />,
          td: ({ node, ...props }) => <td {...props} className="px-3 py-2 text-sm text-brain-100" />,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}

interface ReportsPanelProps {
  userId?: string | null
}

export default function ReportsPanel({ userId }: ReportsPanelProps) {
  const t = useTranslations('reports_panel')
  const locale = useLocale()

  // Catalog of methodologies
  const [types, setTypes] = useState<MethodologyType[]>([])
  const [typesLoading, setTypesLoading] = useState(true)
  const [typesError, setTypesError] = useState<string | null>(null)

  // Generation form
  const [selectedType, setSelectedType] = useState<string | null>(null)
  const [daysBack, setDaysBack] = useState<number>(30)
  const [modelTier, setModelTier] = useState<'standard' | 'premium'>('standard')
  const [customPrompt, setCustomPrompt] = useState('')
  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState<string | null>(null)

  // Current report view
  const [report, setReport] = useState<MethodologyReport | null>(null)

  // History
  const [history, setHistory] = useState<MethodologyReport[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [openingReportId, setOpeningReportId] = useState<string | null>(null)

  const formatDate = useCallback(
    (iso?: string) => {
      if (!iso) return ''
      try {
        return new Date(iso).toLocaleString(locale === 'ru' ? 'ru-RU' : 'en-US', {
          day: '2-digit',
          month: 'short',
          year: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        })
      } catch {
        return iso
      }
    },
    [locale]
  )

  const fetchTypes = useCallback(async () => {
    if (!userId) return
    try {
      setTypesLoading(true)
      setTypesError(null)
      const res = await fetch(`${API_BASE}/api/v1/reports/methodology/types?user_id=${userId}`, {
        headers: getAuthHeaders(),
      })
      if (res.ok) {
        const data = await res.json()
        setTypes(data.types || data.methodologies || (Array.isArray(data) ? data : []))
      } else if (res.status === 401) {
        throw new Error(t('err_unauthorized'))
      } else {
        throw new Error(t('err_types'))
      }
    } catch (e: any) {
      setTypesError(e.message || t('err_types'))
    } finally {
      setTypesLoading(false)
    }
  }, [userId, t])

  const fetchHistory = useCallback(async () => {
    if (!userId) return
    try {
      setHistoryLoading(true)
      setHistoryError(null)
      const res = await fetch(`${API_BASE}/api/v1/reports/methodology/?user_id=${userId}`, {
        headers: getAuthHeaders(),
      })
      if (res.ok) {
        const data = await res.json()
        setHistory(data.reports || [])
      } else if (res.status === 401) {
        throw new Error(t('err_unauthorized'))
      } else {
        throw new Error(t('err_history'))
      }
    } catch (e: any) {
      setHistoryError(e.message || t('err_history'))
    } finally {
      setHistoryLoading(false)
    }
  }, [userId, t])

  useEffect(() => {
    if (!userId) {
      setTypesLoading(false)
      setHistoryLoading(false)
      return
    }
    fetchTypes()
    fetchHistory()
  }, [userId, fetchTypes, fetchHistory])

  const handleGenerate = async () => {
    if (!userId || !selectedType || generating) return
    setGenerating(true)
    setGenerateError(null)
    try {
      const body: Record<string, any> = {
        report_type: selectedType,
        user_id: userId,
        days_back: daysBack,
        model_tier: modelTier,
      }
      if (selectedType === 'custom' && customPrompt.trim()) {
        body.custom_prompt = customPrompt.trim()
      }
      const res = await fetch(`${API_BASE}/api/v1/reports/methodology/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify(body),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        throw new Error(data?.message || data?.detail || t('err_generate', { status: res.status }))
      }
      if (data.status === 'success' && data.report) {
        setReport(data.report)
        fetchHistory()
      } else if (data.status === 'no_data') {
        setGenerateError(data.message || t('no_data_message'))
      } else {
        throw new Error(data?.message || t('err_generate_generic'))
      }
    } catch (e: any) {
      setGenerateError(e.message || t('err_generate_generic'))
    } finally {
      setGenerating(false)
    }
  }

  const handleOpenReport = async (reportId: string) => {
    if (!userId || openingReportId) return
    try {
      setOpeningReportId(reportId)
      setHistoryError(null)
      const res = await fetch(`${API_BASE}/api/v1/reports/methodology/${reportId}?user_id=${userId}`, {
        headers: getAuthHeaders(),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        throw new Error(data?.message || data?.detail || t('err_report'))
      }
      setReport(data.report || data)
    } catch (e: any) {
      setHistoryError(e.message || t('err_report'))
    } finally {
      setOpeningReportId(null)
    }
  }

  // === Report view ===
  if (report) {
    return (
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <button
            onClick={() => setReport(null)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm bg-brain-800/50 hover:bg-brain-700/50 text-brain-300 hover:text-brain-100 transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            {t('back_button')}
          </button>
          <div className="flex items-center gap-3 text-xs text-brain-500">
            {typeof report.meetings_count === 'number' && (
              <span className="flex items-center gap-1">
                <Users className="w-3.5 h-3.5" />
                {t('meetings_count', { count: report.meetings_count })}
              </span>
            )}
            {report.created_at && (
              <span className="flex items-center gap-1">
                <Clock className="w-3.5 h-3.5" />
                {formatDate(report.created_at)}
              </span>
            )}
          </div>
        </div>

        <h2 className="text-lg font-semibold mb-1 flex items-center gap-2">
          {report.icon && <span>{report.icon}</span>}
          {report.title}
        </h2>
        {report.summary && <p className="text-sm text-brain-400 mb-4">{report.summary}</p>}

        <div className="border-t border-brain-700/30 pt-4">
          {report.content_text ? (
            <ReportMarkdown content={report.content_text} />
          ) : (
            <div className="text-center py-8 text-brain-400">{t('report_empty')}</div>
          )}
        </div>
      </div>
    )
  }

  // === Catalog + form + history ===
  return (
    <div className="space-y-4">
      {/* Methodology catalog */}
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <FileText className="w-5 h-5 text-brain-400" />
            {t('title')}
          </h2>
          <button
            onClick={() => {
              fetchTypes()
              fetchHistory()
            }}
            className="p-2 rounded-lg bg-brain-800/50 hover:bg-brain-700/50 text-brain-400 hover:text-brain-200 transition-colors"
            title={t('refresh_button_title')}
          >
            <RefreshCw className={`w-4 h-4 ${typesLoading || historyLoading ? 'animate-spin' : ''}`} />
          </button>
        </div>

        {typesLoading ? (
          <div className="flex items-center justify-center py-8 text-brain-400">
            <Loader2 className="w-5 h-5 animate-spin mr-2" />
            {t('loading_types')}
          </div>
        ) : typesError ? (
          <div className="flex items-center justify-center py-8 text-red-400">
            <AlertCircle className="w-5 h-5 mr-2" />
            {typesError}
          </div>
        ) : types.length === 0 ? (
          <div className="text-center py-8 text-brain-400">{t('types_empty')}</div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {types.map((type) => (
              <button
                key={type.id}
                onClick={() => setSelectedType(type.id)}
                className={`text-left p-3 rounded-xl border transition-all ${
                  selectedType === type.id
                    ? 'bg-brain-700/50 border-purple-500/50'
                    : 'bg-brain-900/60 border-brain-700/30 hover:bg-brain-800/40 hover:border-brain-600/50'
                }`}
              >
                <div className="flex items-center gap-2 mb-1">
                  {type.icon && <span className="text-lg">{type.icon}</span>}
                  <span className="text-sm font-medium text-brain-100">{type.name}</span>
                </div>
                {type.description && <p className="text-xs text-brain-500">{type.description}</p>}
              </button>
            ))}
          </div>
        )}

        {/* Generation form */}
        {selectedType && (
          <div className="mt-4 border-t border-brain-700/30 pt-4 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="block text-sm text-brain-300 mb-1.5">{t('days_back_label')}</label>
                <input
                  type="number"
                  min={1}
                  max={365}
                  value={daysBack}
                  onChange={(e) => setDaysBack(Math.max(1, Number(e.target.value) || 30))}
                  className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 focus:outline-none focus:border-purple-500/50"
                />
              </div>
              <div>
                <label className="block text-sm text-brain-300 mb-1.5">{t('model_tier_label')}</label>
                <select
                  value={modelTier}
                  onChange={(e) => setModelTier(e.target.value as 'standard' | 'premium')}
                  className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 focus:outline-none focus:border-purple-500/50"
                >
                  <option value="standard">{t('tier_standard')}</option>
                  <option value="premium">{t('tier_premium')}</option>
                </select>
              </div>
            </div>

            {selectedType === 'custom' && (
              <div>
                <label className="block text-sm text-brain-300 mb-1.5">{t('custom_prompt_label')}</label>
                <textarea
                  value={customPrompt}
                  onChange={(e) => setCustomPrompt(e.target.value)}
                  rows={4}
                  placeholder={t('custom_prompt_placeholder')}
                  className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50 resize-y"
                />
              </div>
            )}

            <div className="flex items-center justify-end">
              <button
                onClick={handleGenerate}
                disabled={generating || !userId || (selectedType === 'custom' && !customPrompt.trim())}
                className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm bg-purple-600 hover:bg-purple-500 text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                {generating ? t('generating_button') : t('generate_button')}
              </button>
            </div>

            {generating && (
              <div className="flex items-center gap-2 text-sm text-brain-400 bg-brain-900/40 rounded-lg px-3 py-2 border border-brain-700/20">
                <Loader2 className="w-4 h-4 animate-spin text-purple-400" />
                {t('generating_hint')}
              </div>
            )}

            {generateError && (
              <div className="flex items-center gap-2 text-sm text-red-400 bg-red-400/5 border border-red-400/20 rounded-lg px-3 py-2">
                <AlertCircle className="w-4 h-4 shrink-0" />
                {generateError}
              </div>
            )}
          </div>
        )}
      </div>

      {/* History */}
      <div className="card">
        <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
          <Clock className="w-5 h-5 text-brain-400" />
          {t('history_title')}
        </h2>

        {historyLoading ? (
          <div className="flex items-center justify-center py-8 text-brain-400">
            <Loader2 className="w-5 h-5 animate-spin mr-2" />
            {t('history_loading')}
          </div>
        ) : historyError ? (
          <div className="flex items-center justify-center py-8 text-red-400">
            <AlertCircle className="w-5 h-5 mr-2" />
            {historyError}
          </div>
        ) : history.length === 0 ? (
          <div className="text-center py-8 text-brain-400">{t('history_empty')}</div>
        ) : (
          <div className="space-y-2">
            {history.map((item) => (
              <button
                key={item.id}
                onClick={() => handleOpenReport(item.id)}
                disabled={openingReportId !== null}
                className={`w-full text-left p-3 rounded-xl border bg-brain-900/60 border-brain-700/30 hover:bg-brain-800/40 hover:border-brain-600/50 transition-all ${
                  openingReportId && openingReportId !== item.id ? 'opacity-50' : ''
                }`}
              >
                <div className="flex items-center gap-2">
                  {item.icon && <span>{item.icon}</span>}
                  <span className="text-sm font-medium text-brain-100 flex-1 truncate">{item.title}</span>
                  {openingReportId === item.id && <Loader2 className="w-4 h-4 animate-spin text-brain-400 shrink-0" />}
                  <span className="text-xs text-brain-500 shrink-0">{formatDate(item.created_at)}</span>
                </div>
                {item.summary && <p className="text-xs text-brain-500 mt-1 line-clamp-2">{item.summary}</p>}
                {typeof item.meetings_count === 'number' && (
                  <div className="text-xs text-brain-600 mt-1">{t('meetings_count', { count: item.meetings_count })}</div>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
