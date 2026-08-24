'use client'

import React, { useState, useEffect, useRef, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import {
  Upload,
  FileText,
  Trash2,
  Sparkles,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  CheckCircle,
  Clock,
  Link2,
  Unlink,
  Database,
  FileSpreadsheet,
  FileJson,
  FileType,
  Loader2,
  Plus,
  Eye,
  X,
  Search,
  Folder,
} from 'lucide-react'
import { simaFetch } from '@/sima/lib/api'

// ВНИМАНИЕ: бэкенд SIMA сериализует все ответы в camelCase (_to_camel_dict).
// Раньше эти интерфейсы были в snake_case → все поля читались как undefined
// (инсайты «0», иконки типа, дедуп встреч — ничего не работало). Держим
// camelCase как единый контракт с API.
interface DataSourceItem {
  id: string
  name: string
  sourceType: string
  mimeType: string | null
  content: string
  status: string
  rawMetadata: any | null
  createdAt: string
  // Insights join from backend
  insights: InsightItem[]
  insightsCount?: number
}

interface InsightItem {
  id: string
  category: string
  title: string
  content: string
  evidence: string | null
  confidence: number
  priority: string
  linkedBlockId: string | null
  applied: boolean
}

const buildCategoryLabels = (t: (k: string) => string): Record<string, string> => ({
  user_need: t('insight_user_need'),
  pain_point: t('insight_pain_point'),
  feature_idea: t('insight_feature_idea'),
  market_trend: t('insight_market_trend'),
  tech_requirement: t('insight_tech_requirement'),
  business_metric: t('insight_business_metric'),
  quote: t('insight_quote'),
  persona_trait: t('insight_persona_trait'),
})

const CATEGORY_COLORS: Record<string, string> = {
  user_need: 'bg-blue-500/20 text-blue-300',
  pain_point: 'bg-red-500/20 text-red-300',
  feature_idea: 'bg-green-500/20 text-green-300',
  market_trend: 'bg-purple-500/20 text-purple-300',
  tech_requirement: 'bg-yellow-500/20 text-yellow-300',
  business_metric: 'bg-cyan-500/20 text-cyan-300',
  quote: 'bg-orange-500/20 text-orange-300',
  persona_trait: 'bg-pink-500/20 text-pink-300',
}

const PRIORITY_ICONS: Record<string, string> = {
  high: '🔴',
  medium: '🟡',
  low: '🟢',
}

const buildStatusConfig = (t: (k: string) => string): Record<string, { icon: React.ElementType; label: string; className: string }> => ({
  uploaded: { icon: Clock, label: t('status_uploaded'), className: 'text-gray-400' },
  processing: { icon: Loader2, label: t('status_processing'), className: 'text-yellow-400 animate-spin' },
  analyzed: { icon: CheckCircle, label: t('status_analyzed'), className: 'text-green-400' },
  error: { icon: AlertCircle, label: t('status_error'), className: 'text-red-400' },
})

function getMimeIcon(mime_type: string | null) {
  if (!mime_type) return FileText
  if (mime_type.includes('csv') || mime_type.includes('spreadsheet')) return FileSpreadsheet
  if (mime_type.includes('json')) return FileJson
  if (mime_type.includes('markdown')) return FileType
  return FileText
}

export default function DataPanel() {
  const t = useTranslations('sima_data')
  const CATEGORY_LABELS = buildCategoryLabels(t)
  const STATUS_CONFIG = buildStatusConfig(t)
  const { project, blocks, setAllBlocks, setAllConnections } = useSimaStore()
  const [sources, setSources] = useState<DataSourceItem[]>([])
  const [loading, setLoading] = useState(false)
  const [analyzing, setAnalyzing] = useState<string | null>(null)
  const [applyingInsights, setApplyingInsights] = useState(false)
  const [expandedSource, setExpandedSource] = useState<string | null>(null)
  const [expandedInsight, setExpandedInsight] = useState<string | null>(null)
  const [showUpload, setShowUpload] = useState(false)
  const [textInput, setTextInput] = useState('')
  const [textName, setTextName] = useState('')
  const [uploadMode, setUploadMode] = useState<'file' | 'text' | 'meetings' | 'documents'>('file')
  // Палитра «Добавить источник» на правой панели просит открыть режим
  const dataPanelMode = useSimaStore(s => s.dataPanelMode)
  const setDataPanelMode = useSimaStore(s => s.setDataPanelMode)
  useEffect(() => {
    if (dataPanelMode) {
      setShowUpload(true)
      setUploadMode(dataPanelMode)
      setDataPanelMode(null)
    }
  }, [dataPanelMode, setDataPanelMode])
  const [linkingInsight, setLinkingInsight] = useState<string | null>(null)
  const [previewSource, setPreviewSource] = useState<string | null>(null)
  const [tessentMeetings, setTessentMeetings] = useState<any[]>([])
  const [tessentFolders, setTessentFolders] = useState<any[]>([])
  const [tessentProjects, setTessentProjects] = useState<any[]>([])
  const [tessentDocuments, setTessentDocuments] = useState<any[]>([])
  const [loadingTessent, setLoadingTessent] = useState(false)
  const [importingIds, setImportingIds] = useState<Set<string>>(new Set())
  const [meetingSearch, setMeetingSearch] = useState('')
  const [meetingFolderFilter, setMeetingFolderFilter] = useState<string | null>(null)
  const [meetingProjectFilter, setMeetingProjectFilter] = useState<string | null>(null)
  const [docSearch, setDocSearch] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const loadSources = useCallback(async () => {
    if (!project?.id) return
    setLoading(true)
    try {
      const res = await simaFetch(`/data-sources?projectId=${project.id}`)
      const data = await res.json()
      setSources(data.sources || [])
    } catch (err) {
      console.error('Load sources error:', err)
    } finally {
      setLoading(false)
    }
  }, [project?.id])

  useEffect(() => {
    loadSources()
  }, [loadSources])

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files || !project?.id) return

    const fileSources = []
    for (const file of Array.from(files)) {
      const content = await file.text()
      fileSources.push({
        name: file.name,
        content,
        sourceType: 'file',
        mimeType: file.type || detectMimeType(file.name),
      })
    }

    setLoading(true)
    try {
      await simaFetch('/data-sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          action: 'upload',
          sources: fileSources,
          autoAnalyze: true, // автоматически извлекаем инсайты
        }),
      })
      await loadSources()
      setShowUpload(false)
    } catch (err) {
      console.error('Upload error:', err)
    } finally {
      setLoading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const handleTextUpload = async () => {
    if (!textInput.trim() || !project?.id) return

    setLoading(true)
    try {
      await simaFetch('/data-sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          action: 'upload',
          sources: [
            {
              name: textName.trim() || t('default_text_name'),
              content: textInput,
              sourceType: 'text',
              mimeType: 'text/plain',
            },
          ],
          autoAnalyze: true,
        }),
      })
      await loadSources()
      setTextInput('')
      setTextName('')
      setShowUpload(false)
    } catch (err) {
      console.error('Text upload error:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleAnalyze = async (dataSourceId: string) => {
    if (!project?.id) return
    setAnalyzing(dataSourceId)
    try {
      await simaFetch('/data-sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          action: 'analyze',
          dataSourceId,
        }),
      })
      await loadSources()
    } catch (err) {
      console.error('Analyze error:', err)
    } finally {
      setAnalyzing(null)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await simaFetch(`/data-sources?id=${id}`, { method: 'DELETE' })
      setSources((prev) => prev.filter((s) => s.id !== id))
    } catch (err) {
      console.error('Delete error:', err)
    }
  }

  const handleLinkInsight = async (insightId: string, blockId: string) => {
    if (!project?.id) return
    try {
      await simaFetch('/data-sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          action: 'link',
          insightId,
          blockId,
        }),
      })
      await loadSources()
      setLinkingInsight(null)
    } catch (err) {
      console.error('Link error:', err)
    }
  }

  const handleUnlinkInsight = async (insightId: string) => {
    if (!project?.id) return
    try {
      await simaFetch('/data-sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          action: 'unlink',
          insightId,
        }),
      })
      await loadSources()
    } catch (err) {
      console.error('Unlink error:', err)
    }
  }

  const handleApplyInsights = async () => {
    if (!project?.id || applyingInsights) return
    setApplyingInsights(true)
    try {
      const res = await simaFetch('/data-sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          action: 'apply-insights',
        }),
      })
      const data = await res.json()
      if (data.error) {
        alert(data.error)
      } else {
        // Перезагрузить проект чтобы обновить блоки
        const projRes = await simaFetch(`/projects/${project.id}`)
        if (projRes.ok) {
          const proj = await projRes.json()
          if (proj.blocks) setAllBlocks(proj.blocks)
          if (proj.connections) setAllConnections(proj.connections)
        }
        alert(t('insights_applied', { count: data.updatedBlocks?.length || 0 }))
      }
    } catch (err) {
      console.error('Apply insights error:', err)
    } finally {
      setApplyingInsights(false)
    }
  }

  // === Tessent Integration ===

  const [suggestions, setSuggestions] = useState<Array<{
    meeting: { id: string; title?: string; created_at?: string }
    score: number
    matched_words: string[]
  }>>([])
  const [suggesting, setSuggesting] = useState(false)

  // Автоподбор: система предлагает встречи под тему проекта, с совпавшими
  // словами — видно, ПОЧЕМУ предложено. Пустой список честен: значит,
  // пересечений с темой проекта не нашлось.
  const loadSuggestions = useCallback(async () => {
    if (!project?.id) return
    setSuggesting(true)
    try {
      const res = await simaFetch(
        `/data-sources/tessent-meetings/suggest?projectId=${encodeURIComponent(String(project.id))}`)
      const data = await res.json()
      setSuggestions(Array.isArray(data.suggestions) ? data.suggestions : [])
    } catch (err) {
      console.error('Suggest meetings error:', err)
      setSuggestions([])
    } finally {
      setSuggesting(false)
    }
  }, [project?.id])

  const loadTessentMeetings = useCallback(async () => {
    setLoadingTessent(true)
    try {
      const res = await simaFetch('/data-sources/tessent-meetings')
      const data = await res.json()
      setTessentMeetings(data.meetings || [])
      setTessentFolders(data.folders || [])
      setTessentProjects(data.projects || [])
    } catch (err) {
      console.error('Load tessent meetings error:', err)
      setTessentMeetings([])
      setTessentFolders([])
      setTessentProjects([])
    } finally {
      setLoadingTessent(false)
    }
  }, [])

  const loadTessentDocuments = useCallback(async () => {
    setLoadingTessent(true)
    try {
      const res = await simaFetch('/data-sources/tessent-documents')
      const data = await res.json()
      setTessentDocuments(Array.isArray(data) ? data : [])
    } catch (err) {
      console.error('Load tessent documents error:', err)
      setTessentDocuments([])
    } finally {
      setLoadingTessent(false)
    }
  }, [])

  useEffect(() => {
    if (uploadMode === 'meetings') { loadTessentMeetings(); loadSuggestions() }
    if (uploadMode === 'documents') loadTessentDocuments()
  }, [uploadMode, loadTessentMeetings, loadTessentDocuments, loadSuggestions])

  const handleImportMeeting = async (meetingId: string) => {
    if (!project?.id) return
    setImportingIds((prev) => new Set(prev).add(meetingId))
    try {
      await simaFetch('/data-sources/tessent-meetings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          meetingId,
          autoAnalyze: true,
        }),
      })
      await loadSources()
    } catch (err) {
      console.error('Import meeting error:', err)
    } finally {
      setImportingIds((prev) => {
        const next = new Set(prev)
        next.delete(meetingId)
        return next
      })
    }
  }

  const handleImportDocument = async (documentId: string) => {
    if (!project?.id) return
    setImportingIds((prev) => new Set(prev).add(documentId))
    try {
      await simaFetch('/data-sources/tessent-documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          documentId,
          autoAnalyze: true,
        }),
      })
      await loadSources()
    } catch (err) {
      console.error('Import document error:', err)
    } finally {
      setImportingIds((prev) => {
        const next = new Set(prev)
        next.delete(documentId)
        return next
      })
    }
  }

  // Подсчёт статистики
  const totalInsights = sources.reduce((sum, s) => sum + s.insights.length, 0)
  const highPriorityInsights = sources.reduce(
    (sum, s) => sum + s.insights.filter((i) => i.priority === 'high').length,
    0
  )
  const linkedInsights = sources.reduce(
    (sum, s) => sum + s.insights.filter((i) => i.linkedBlockId).length,
    0
  )

  if (!project) {
    return (
      <div className="p-4 text-sima-textDim text-sm">
        {t('no_project')}
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="p-3 border-b border-sima-border">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-sima-text flex items-center gap-1.5">
            <Database className="w-4 h-4 text-sima-primary" />
            {t('panel_title')}
          </h3>
          <button
            onClick={() => setShowUpload(!showUpload)}
            className="flex items-center gap-1 text-xs px-2.5 py-1.5 bg-sima-primary/20 text-sima-primary rounded-lg hover:bg-sima-primary/30 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            {t('add_button')}
          </button>
        </div>

        {/* Статистика */}
        {sources.length > 0 && (
          <div className="flex items-center gap-3 text-xs text-sima-textDim">
            <span>{t('sources_count', { count: sources.length })}</span>
            <span>{t('insights_count', { count: totalInsights })}</span>
            {highPriorityInsights > 0 && (
              <span className="text-red-400">{t('critical_count', { count: highPriorityInsights })}</span>
            )}
            {linkedInsights > 0 && (
              <>
                <span className="text-green-400">{t('linked_count', { count: linkedInsights })}</span>
                <button
                  onClick={handleApplyInsights}
                  disabled={applyingInsights}
                  className="ml-auto px-2 py-1 bg-sima-primary/20 text-sima-primary rounded text-[10px] hover:bg-sima-primary/30 transition-colors disabled:opacity-50"
                >
                  {applyingInsights ? t('applying') : t('apply_to_blocks')}
                </button>
              </>
            )}
          </div>
        )}
      </div>

      {/* Upload panel */}
      {showUpload && (
        <div className="p-3 border-b border-sima-border bg-sima-bg/50 space-y-2">
          <div className="flex gap-1 flex-wrap">
            {([
              { key: 'file' as const, icon: Upload, label: t('tab_file') },
              { key: 'text' as const, icon: FileText, label: t('tab_text') },
              { key: 'meetings' as const, icon: Database, label: t('tab_meetings') },
              { key: 'documents' as const, icon: FileType, label: t('tab_documents') },
            ]).map(({ key, icon: Icon, label }) => (
              <button
                key={key}
                onClick={() => setUploadMode(key)}
                className={`flex-1 text-xs py-1.5 rounded-lg transition-colors min-w-[70px] ${
                  uploadMode === key
                    ? 'bg-sima-primary/20 text-sima-primary'
                    : 'bg-sima-surface text-sima-textDim hover:text-sima-text'
                }`}
              >
                <Icon className="w-3 h-3 inline mr-1" />
                {label}
              </button>
            ))}
          </div>

          {uploadMode === 'file' && (
            <div>
              <label className="flex items-center justify-center gap-2 p-4 border-2 border-dashed border-sima-border rounded-lg cursor-pointer hover:border-sima-primary/50 transition-colors">
                <Upload className="w-5 h-5 text-sima-textDim" />
                <span className="text-xs text-sima-textDim">
                  {t('drop_files_hint')}
                </span>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept=".txt,.csv,.json,.md,.markdown,.tsv"
                  onChange={handleFileUpload}
                  className="hidden"
                />
              </label>
              <p className="text-[10px] text-sima-textDim mt-1">
                {t('data_examples')}
              </p>
            </div>
          )}

          {uploadMode === 'text' && (
            <div className="space-y-2">
              <input
                type="text"
                placeholder={t("text_name_placeholder")}
                value={textName}
                onChange={(e) => setTextName(e.target.value)}
                className="w-full text-xs px-3 py-1.5 bg-sima-bg border border-sima-border rounded-lg text-sima-text placeholder:text-sima-textDim focus:outline-none focus:border-sima-primary/50"
              />
              <textarea
                placeholder={t("text_content_placeholder")}
                value={textInput}
                onChange={(e) => setTextInput(e.target.value)}
                rows={5}
                className="w-full text-xs px-3 py-2 bg-sima-bg border border-sima-border rounded-lg text-sima-text placeholder:text-sima-textDim resize-none focus:outline-none focus:border-sima-primary/50"
              />
              <button
                onClick={handleTextUpload}
                disabled={!textInput.trim() || loading}
                className="w-full text-xs py-2 bg-sima-primary/20 text-sima-primary rounded-lg hover:bg-sima-primary/30 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-1"
              >
                {loading ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Sparkles className="w-3.5 h-3.5" />
                )}
                {t('upload_and_analyze')}
              </button>
            </div>
          )}

          {uploadMode === 'meetings' && (
            <div className="space-y-2">
              {/* Автоподбор: предложенные под тему проекта встречи */}
              {suggestions.length > 0 && (
                <div className="rounded-lg border border-sima-primary/30 bg-sima-primary/5 p-2 space-y-1.5">
                  <div className="text-[11px] font-medium text-sima-text">
                    Предложено под тему проекта{suggesting ? '…' : ''}
                  </div>
                  {suggestions.slice(0, 5).map((sug) => (
                    <div key={sug.meeting.id}
                         className="flex items-center justify-between gap-2 text-xs">
                      <div className="min-w-0">
                        <div className="truncate text-sima-text">
                          {sug.meeting.title || sug.meeting.id}
                        </div>
                        <div className="text-[10px] text-sima-textDim truncate">
                          совпало: {sug.matched_words.slice(0, 4).join(', ')}
                        </div>
                      </div>
                      <button
                        onClick={() => handleImportMeeting(sug.meeting.id)}
                        disabled={importingIds.has(sug.meeting.id)}
                        className="shrink-0 text-[10px] px-2 py-1 rounded bg-sima-primary/20 text-sima-primary hover:bg-sima-primary/30 disabled:opacity-50"
                      >
                        {importingIds.has(sug.meeting.id) ? '…' : 'Подключить'}
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {/* Поиск */}
              <div className="relative">
                <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-sima-textDim" />
                <input
                  type="text"
                  placeholder={t("search_placeholder")}
                  value={meetingSearch}
                  onChange={(e) => setMeetingSearch(e.target.value)}
                  className="w-full text-xs pl-8 pr-3 py-1.5 bg-sima-bg border border-sima-border rounded-lg text-sima-text placeholder:text-sima-textDim focus:outline-none focus:border-sima-primary/50"
                />
              </div>

              {/* Фильтры по проекту / папке (каскадные) */}
              {(tessentFolders.length > 0 || tessentProjects.length > 0) && (() => {
                // Папки, доступные для выбранного проекта
                const availableFolders = meetingProjectFilter
                  ? (() => {
                      // Собираем folder_id всех встреч выбранного проекта
                      const folderIdsInProject = new Set(
                        tessentMeetings
                          .filter((m) => m.projectId === meetingProjectFilter && m.folderId)
                          .map((m) => m.folderId)
                      )
                      return tessentFolders.filter((f) => folderIdsInProject.has(f.id))
                    })()
                  : tessentFolders

                return (
                  <div className="flex gap-1.5 flex-wrap">
                    {tessentProjects.length > 0 && (
                      <select
                        value={meetingProjectFilter || ''}
                        onChange={(e) => {
                          setMeetingProjectFilter(e.target.value || null)
                          setMeetingFolderFilter(null) // сброс папки при смене проекта
                        }}
                        className="text-[10px] px-2 py-1 bg-sima-bg border border-sima-border rounded-lg text-sima-text focus:outline-none focus:border-sima-primary/50 max-w-[140px]"
                      >
                        <option value="">{t('all_projects', { count: tessentProjects.length })}</option>
                        {tessentProjects.map((p) => (
                          <option key={p.id} value={p.id}>{p.name}</option>
                        ))}
                      </select>
                    )}
                    {availableFolders.length > 0 && (
                      <select
                        value={meetingFolderFilter || ''}
                        onChange={(e) => setMeetingFolderFilter(e.target.value || null)}
                        className="text-[10px] px-2 py-1 bg-sima-bg border border-sima-border rounded-lg text-sima-text focus:outline-none focus:border-sima-primary/50 max-w-[140px]"
                      >
                        <option value="">{t('all_folders', { count: availableFolders.length })}</option>
                        {availableFolders.map((f) => (
                          <option key={f.id} value={f.id}>{f.name}</option>
                        ))}
                      </select>
                    )}
                    {(meetingProjectFilter || meetingFolderFilter) && (
                      <button
                        onClick={() => { setMeetingProjectFilter(null); setMeetingFolderFilter(null) }}
                        className="text-[10px] px-2 py-1 text-sima-textDim hover:text-sima-text transition-colors"
                      >
                        {t('reset_button')}
                      </button>
                    )}
                  </div>
                )
              })()}

              {/* Список встреч */}
              <div className="space-y-1.5 max-h-52 overflow-y-auto">
                {loadingTessent && (
                  <div className="flex items-center justify-center py-4">
                    <Loader2 className="w-4 h-4 animate-spin text-sima-textDim" />
                    <span className="text-xs text-sima-textDim ml-2">{t('loading_meetings')}</span>
                  </div>
                )}
                {!loadingTessent && tessentMeetings.length === 0 && (
                  <div className="text-center py-4 text-sima-textDim">
                    <p className="text-xs">{t("no_meetings_available")}</p>
                    <p className="text-[10px] mt-1">{t('meetings_hint')}</p>
                  </div>
                )}
                {(() => {
                  const searchLower = meetingSearch.toLowerCase()
                  const filtered = tessentMeetings.filter((m) => {
                    if (searchLower && !(m.title || '').toLowerCase().includes(searchLower)) return false
                    if (meetingProjectFilter && m.projectId !== meetingProjectFilter) return false
                    if (meetingFolderFilter && m.folderId !== meetingFolderFilter) return false
                    return true
                  })

                  if (!loadingTessent && tessentMeetings.length > 0 && filtered.length === 0) {
                    return (
                      <div className="text-center py-3 text-sima-textDim">
                        <p className="text-xs">{t("no_results")}</p>
                        <p className="text-[10px] mt-0.5">{t('try_filters_hint')}</p>
                      </div>
                    )
                  }

                  return filtered.map((meeting) => {
                    const isImporting = importingIds.has(meeting.id)
                    const alreadyImported = sources.some((s) => {
                      try {
                        const meta = typeof s.rawMetadata === 'string' ? JSON.parse(s.rawMetadata) : s.rawMetadata
                        return meta && meta.meeting_id === meeting.id
                      } catch { return false }
                    })
                    return (
                      <div
                        key={meeting.id}
                        className="flex items-center gap-2 p-2 bg-sima-surface/50 rounded-lg border border-sima-border"
                      >
                        <div className="flex-1 min-w-0">
                          <div className="text-xs font-medium text-sima-text truncate">
                            {meeting.title}
                          </div>
                          <div className="text-[10px] text-sima-textDim flex items-center gap-2 flex-wrap">
                            {meeting.date && <span>{String(meeting.date).split('T')[0]}</span>}
                            {meeting.folderName && (
                              <span className="flex items-center gap-0.5 text-blue-400">
                                <Folder className="w-2.5 h-2.5" />
                                {meeting.folderName}
                              </span>
                            )}
                            {meeting.projectName && (
                              <span className="text-purple-400">{meeting.projectName}</span>
                            )}
                            {meeting.hasTranscript ? (
                              <span className="text-green-400">{t('has_transcript')}</span>
                            ) : (
                              <span className="text-yellow-400">{t('no_transcript')}</span>
                            )}
                          </div>
                        </div>
                        {alreadyImported ? (
                          <span className="text-[10px] text-green-400 px-2 py-0.5 bg-green-500/10 rounded shrink-0">
                            <CheckCircle className="w-3 h-3 inline mr-0.5" />
                            {t('added')}
                          </span>
                        ) : (
                          <button
                            onClick={() => handleImportMeeting(meeting.id)}
                            disabled={isImporting || !meeting.hasTranscript}
                            className="text-[10px] px-2.5 py-1 bg-sima-primary/20 text-sima-primary rounded-lg hover:bg-sima-primary/30 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center gap-1 shrink-0"
                          >
                            {isImporting ? (
                              <Loader2 className="w-3 h-3 animate-spin" />
                            ) : (
                              <Plus className="w-3 h-3" />
                            )}
                            {t('import')}
                          </button>
                        )}
                      </div>
                    )
                  })
                })()}
              </div>
              {!loadingTessent && tessentMeetings.length > 0 && (
                <div className="text-[10px] text-sima-textDim text-center">
                  {t('total_meetings', { count: tessentMeetings.length })}
                </div>
              )}
            </div>
          )}

          {uploadMode === 'documents' && (
            <div className="space-y-2">
              {/* Поиск документов */}
              <div className="relative">
                <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-sima-textDim" />
                <input
                  type="text"
                  placeholder={t("search_placeholder")}
                  value={docSearch}
                  onChange={(e) => setDocSearch(e.target.value)}
                  className="w-full text-xs pl-8 pr-3 py-1.5 bg-sima-bg border border-sima-border rounded-lg text-sima-text placeholder:text-sima-textDim focus:outline-none focus:border-sima-primary/50"
                />
              </div>

              <div className="space-y-1.5 max-h-52 overflow-y-auto">
                {loadingTessent && (
                  <div className="flex items-center justify-center py-4">
                    <Loader2 className="w-4 h-4 animate-spin text-sima-textDim" />
                    <span className="text-xs text-sima-textDim ml-2">{t('loading_documents')}</span>
                  </div>
                )}
                {!loadingTessent && tessentDocuments.length === 0 && (
                  <div className="text-center py-4 text-sima-textDim">
                    <p className="text-xs">{t('no_documents_available')}</p>
                    <p className="text-[10px] mt-1">{t('documents_hint')}</p>
                  </div>
                )}
                {(() => {
                  const searchLower = docSearch.toLowerCase()
                  const filtered = tessentDocuments.filter((d) => {
                    if (searchLower && !(d.title || '').toLowerCase().includes(searchLower)) return false
                    return true
                  })

                  if (!loadingTessent && tessentDocuments.length > 0 && filtered.length === 0) {
                    return (
                      <div className="text-center py-3 text-sima-textDim">
                        <p className="text-xs">{t("no_results")}</p>
                      </div>
                    )
                  }

                  return filtered.map((doc) => {
                    const isImporting = importingIds.has(doc.id)
                    const alreadyImported = sources.some((s) => {
                      try {
                        const meta = typeof s.rawMetadata === 'string' ? JSON.parse(s.rawMetadata) : s.rawMetadata
                        return meta && meta.document_id === doc.id
                      } catch { return false }
                    })
                    return (
                      <div
                        key={doc.id}
                        className="flex items-center gap-2 p-2 bg-sima-surface/50 rounded-lg border border-sima-border"
                      >
                        <FileText className="w-4 h-4 text-sima-textDim shrink-0" />
                        <div className="flex-1 min-w-0">
                          <div className="text-xs font-medium text-sima-text truncate">
                            {doc.title}
                          </div>
                          <div className="text-[10px] text-sima-textDim flex items-center gap-2">
                            {doc.docType && <span>{doc.docType}</span>}
                            {doc.preview && (
                              <span className="truncate max-w-[150px]">{doc.preview}</span>
                            )}
                          </div>
                        </div>
                        {alreadyImported ? (
                          <span className="text-[10px] text-green-400 px-2 py-0.5 bg-green-500/10 rounded shrink-0">
                            <CheckCircle className="w-3 h-3 inline mr-0.5" />
                            {t('added')}
                          </span>
                        ) : (
                          <button
                            onClick={() => handleImportDocument(doc.id)}
                            disabled={isImporting}
                            className="text-[10px] px-2.5 py-1 bg-sima-primary/20 text-sima-primary rounded-lg hover:bg-sima-primary/30 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center gap-1 shrink-0"
                          >
                            {isImporting ? (
                              <Loader2 className="w-3 h-3 animate-spin" />
                            ) : (
                              <Plus className="w-3 h-3" />
                            )}
                            {t('import')}
                          </button>
                        )}
                      </div>
                    )
                  })
                })()}
              </div>
              {!loadingTessent && tessentDocuments.length > 0 && (
                <div className="text-[10px] text-sima-textDim text-center">
                  {t('total_documents', { count: tessentDocuments.length })}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Sources list */}
      <div className="flex-1 overflow-y-auto p-2 space-y-2">
        {loading && sources.length === 0 && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 animate-spin text-sima-textDim" />
          </div>
        )}

        {!loading && sources.length === 0 && (
          <div className="text-center py-8 text-sima-textDim">
            <Database className="w-8 h-8 mx-auto mb-2 opacity-30" />
            <p className="text-xs">{t('no_uploaded_data')}</p>
            <p className="text-[10px] mt-1">
              {t('no_uploaded_data_hint')}
            </p>
          </div>
        )}

        {sources.map((source) => {
          const isExpanded = expandedSource === source.id
          const StatusIcon = STATUS_CONFIG[source.status]?.icon || Clock
          const statusConf = STATUS_CONFIG[source.status]
          const MimeIcon = getMimeIcon(source.mimeType)

          return (
            <div
              key={source.id}
              className="bg-sima-bg rounded-lg border border-sima-border overflow-hidden"
            >
              {/* Source header */}
              <div
                className="flex items-center gap-2 p-2.5 cursor-pointer hover:bg-sima-surface/50 transition-colors"
                onClick={() => setExpandedSource(isExpanded ? null : source.id)}
              >
                <MimeIcon className="w-4 h-4 text-sima-textDim shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs font-medium text-sima-text truncate">
                    {source.name}
                  </div>
                  <div className="text-[10px] text-sima-textDim flex items-center gap-2">
                    <span className={statusConf?.className}>
                      <StatusIcon className="w-3 h-3 inline mr-0.5" />
                      {statusConf?.label}
                    </span>
                    {source.insights.length > 0 && (
                      <span>{t('insights_count_in_source', { count: source.insights.length })}</span>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-1">
                  {source.status === 'uploaded' && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        handleAnalyze(source.id)
                      }}
                      disabled={analyzing === source.id}
                      className="p-1 text-sima-primary hover:bg-sima-primary/10 rounded transition-colors"
                      title={t("analyze_title")}
                    >
                      {analyzing === source.id ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <Sparkles className="w-3.5 h-3.5" />
                      )}
                    </button>
                  )}
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      setPreviewSource(previewSource === source.id ? null : source.id)
                    }}
                    className="p-1 text-sima-textDim hover:text-sima-text rounded transition-colors"
                    title={t("view_title")}
                  >
                    <Eye className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      handleDelete(source.id)
                    }}
                    className="p-1 text-sima-textDim hover:text-red-400 rounded transition-colors"
                    title={t("delete_title")}
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                  {isExpanded ? (
                    <ChevronUp className="w-3.5 h-3.5 text-sima-textDim" />
                  ) : (
                    <ChevronDown className="w-3.5 h-3.5 text-sima-textDim" />
                  )}
                </div>
              </div>

              {/* Source preview */}
              {previewSource === source.id && (
                <div className="border-t border-sima-border p-2.5 bg-sima-surface/30">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] text-sima-textDim">{t('content_label_with_mime', { mime: source.mimeType ?? '' })}</span>
                    <button
                      onClick={() => setPreviewSource(null)}
                      className="p-0.5 text-sima-textDim hover:text-sima-text"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </div>
                  <pre className="text-[10px] text-sima-textMuted max-h-40 overflow-y-auto whitespace-pre-wrap font-mono bg-sima-bg p-2 rounded">
                    {source.content.slice(0, 3000)}
                    {source.content.length > 3000 && t('truncated_suffix')}
                  </pre>
                </div>
              )}

              {/* Insights */}
              {isExpanded && source.insights.length > 0 && (
                <div className="border-t border-sima-border p-2 space-y-1.5">
                  {source.insights.map((insight) => {
                    const isInsightExpanded = expandedInsight === insight.id
                    return (
                      <div
                        key={insight.id}
                        className={`rounded-lg border p-2 transition-colors ${
                          insight.applied
                            ? 'border-green-500/30 bg-green-500/5'
                            : 'border-sima-border bg-sima-surface/30'
                        }`}
                      >
                        <div
                          className="flex items-start gap-1.5 cursor-pointer"
                          onClick={() =>
                            setExpandedInsight(isInsightExpanded ? null : insight.id)
                          }
                        >
                          <span className="text-[10px] shrink-0 mt-0.5">
                            {PRIORITY_ICONS[insight.priority] || '⚪'}
                          </span>
                          <div className="flex-1 min-w-0">
                            <div className="text-xs font-medium text-sima-text">
                              {insight.title}
                            </div>
                            <div className="flex items-center gap-1.5 mt-0.5">
                              <span
                                className={`text-[9px] px-1.5 py-0.5 rounded-full ${
                                  CATEGORY_COLORS[insight.category] || 'bg-gray-500/20 text-gray-300'
                                }`}
                              >
                                {CATEGORY_LABELS[insight.category] || insight.category}
                              </span>
                              <span className="text-[9px] text-sima-textDim">
                                {Math.round(insight.confidence * 100)}%
                              </span>
                              {insight.linkedBlockId && (
                                <span className="text-[9px] text-green-400 flex items-center gap-0.5">
                                  <Link2 className="w-2.5 h-2.5" />
                                  {t('linked')}
                                </span>
                              )}
                            </div>
                          </div>
                        </div>

                        {isInsightExpanded && (
                          <div className="mt-2 space-y-1.5">
                            <p className="text-[11px] text-sima-textMuted">{insight.content}</p>
                            {insight.evidence && (
                              <div className="text-[10px] text-sima-textDim bg-sima-bg rounded p-1.5 border-l-2 border-sima-primary/30">
                                &ldquo;{insight.evidence}&rdquo;
                              </div>
                            )}

                            {/* Link / Unlink */}
                            <div className="flex items-center gap-1 pt-1">
                              {insight.linkedBlockId ? (
                                <button
                                  onClick={() => handleUnlinkInsight(insight.id)}
                                  className="text-[10px] flex items-center gap-1 px-2 py-0.5 bg-red-500/10 text-red-400 rounded hover:bg-red-500/20 transition-colors"
                                >
                                  <Unlink className="w-3 h-3" />
                                  {t('unlink')}
                                </button>
                              ) : (
                                <>
                                  {linkingInsight === insight.id ? (
                                    <div className="flex flex-wrap gap-1">
                                      {blocks.map((b) => (
                                        <button
                                          key={b.id}
                                          onClick={() => handleLinkInsight(insight.id, b.id)}
                                          className="text-[10px] px-2 py-0.5 bg-sima-primary/10 text-sima-primary rounded hover:bg-sima-primary/20 transition-colors"
                                        >
                                          {b.label}: {b.name}
                                        </button>
                                      ))}
                                      <button
                                        onClick={() => setLinkingInsight(null)}
                                        className="text-[10px] px-2 py-0.5 text-sima-textDim hover:text-sima-text"
                                      >
                                        {t('cancel')}
                                      </button>
                                    </div>
                                  ) : (
                                    <button
                                      onClick={() => setLinkingInsight(insight.id)}
                                      className="text-[10px] flex items-center gap-1 px-2 py-0.5 bg-sima-primary/10 text-sima-primary rounded hover:bg-sima-primary/20 transition-colors"
                                    >
                                      <Link2 className="w-3 h-3" />
                                      {t('link_to_block')}
                                    </button>
                                  )}
                                </>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}

              {isExpanded && source.insights.length === 0 && source.status === 'analyzed' && (
                <div className="border-t border-sima-border p-3 text-center text-[11px] text-sima-textDim">
                  {t('no_insights_found')}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Footer info */}
      {sources.length > 0 && (
        <div className="p-2 border-t border-sima-border">
          <p className="text-[10px] text-sima-textDim text-center">
            {t('insights_auto_used')}
          </p>
        </div>
      )}
    </div>
  )
}

function detectMimeType(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase()
  switch (ext) {
    case 'csv':
    case 'tsv':
      return 'text/csv'
    case 'json':
      return 'application/json'
    case 'md':
    case 'markdown':
      return 'text/markdown'
    default:
      return 'text/plain'
  }
}
