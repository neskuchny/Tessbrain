'use client'

import { useState, useEffect, useMemo, useCallback } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { RefreshCw, Download, Copy, FileText, Sparkles, CheckCircle, Scale, Trash2, AlertTriangle, ChevronDown, ChevronRight, Search, Folder, FileCheck, LayoutGrid } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import type { DecisionData } from '@/sima/types'
import { simaFetch } from '@/sima/lib/api'

interface TessentMeeting {
  id: string; title: string; date: string; hasTranscript: boolean;
  projectId?: string; projectName?: string; folderId?: string; folderName?: string;
}
interface TessentDocument {
  id: string; title: string; docType?: string; preview?: string; createdAt?: string;
}

export default function NarrativePanel() {
  const t = useTranslations('sima_narrative')
  const locale = useLocale()
  const { project, setProject, blocks, allBlocks, setAllBlocks, setAllConnections, setActiveLayer } = useSimaStore()
  const [isGeneratingNarrative, setIsGeneratingNarrative] = useState(false)
  const [isGeneratingTZ, setIsGeneratingTZ] = useState(false)
  const [copied, setCopied] = useState(false)
  const [activeSection, setActiveSection] = useState<'narrative' | 'tz' | 'decisions' | 'promo'>('narrative')
  // Промо-материалы из ТЗ/описаний (скелет+LLM): преимущества/лендинг/питч/реклама/пост
  const [promoKind, setPromoKind] = useState('benefits')
  const [promoResult, setPromoResult] = useState<{ title: string; markdown: string; llm_used?: boolean } | null>(null)
  const [promoLoading, setPromoLoading] = useState(false)
  const [promoError, setPromoError] = useState('')

  async function generatePromo() {
    if (!project) return
    setPromoLoading(true)
    setPromoError('')
    try {
      const res = await simaFetch('/ai/marketing', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, kind: promoKind }),
      })
      const data = await res.json()
      if (data.success === false) setPromoError(data.error || 'error')
      else setPromoResult(data)
    } catch (e) {
      setPromoError((e as Error).message)
    } finally {
      setPromoLoading(false)
    }
  }
  const [decisions, setDecisions] = useState<DecisionData[]>([])
  const [loadingDecisions, setLoadingDecisions] = useState(false)
  const [tzGeneratedAt, setTzGeneratedAt] = useState<string | null>(null)
  const [dataSourcesCount, setDataSourcesCount] = useState(0)

  // === Селектор встреч и документов для ТЗ ===
  const [showSourcePicker, setShowSourcePicker] = useState(false)
  const [tessentMeetings, setTessentMeetings] = useState<TessentMeeting[]>([])
  const [tessentDocuments, setTessentDocuments] = useState<TessentDocument[]>([])
  const [loadingSources, setLoadingSources] = useState(false)
  const [selectedMeetingIds, setSelectedMeetingIds] = useState<Set<string>>(new Set())
  const [selectedDocumentIds, setSelectedDocumentIds] = useState<Set<string>>(new Set())
  const [meetingSearch, setMeetingSearch] = useState('')
  const [docSearch, setDocSearch] = useState('')
  const [isCreatingSchema, setIsCreatingSchema] = useState(false)
  const [schemaMessage, setSchemaMessage] = useState<string | null>(null)
  // Флаг «сообщение об ошибке» — раньше тип определялся по префиксу текста
  // (startsWith('Ошибка')/'Error'), что ломалось при смене локали.
  const [schemaIsError, setSchemaIsError] = useState(false)

  // Загрузка количества источников данных (встречи, документы)
  useEffect(() => {
    if (project) {
      simaFetch(`/data-sources?projectId=${project.id}`)
        .then(r => r.json())
        .then(data => {
          const sources = data.sources || data || []
          setDataSourcesCount(Array.isArray(sources) ? sources.length : 0)
        })
        .catch(() => setDataSourcesCount(0))
    }
  }, [project?.id])

  // Загрузка списка встреч и документов для пикера
  const loadTessentSources = useCallback(async () => {
    if (loadingSources) return
    setLoadingSources(true)
    try {
      const [meetingsRes, docsRes] = await Promise.all([
        simaFetch('/data-sources/tessent-meetings'),
        simaFetch('/data-sources/tessent-documents'),
      ])
      const meetingsData = await meetingsRes.json()
      const docsData = await docsRes.json()
      setTessentMeetings(meetingsData.meetings || [])
      setTessentDocuments(Array.isArray(docsData) ? docsData : docsData.documents || [])
    } catch (e) {
      console.error('Failed to load tessent sources:', e)
    } finally {
      setLoadingSources(false)
    }
  }, [loadingSources])

  const toggleMeeting = (id: string) => {
    setSelectedMeetingIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }
  const toggleDocument = (id: string) => {
    setSelectedDocumentIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const totalSelected = selectedMeetingIds.size + selectedDocumentIds.size

  // Загрузка timestamp генерации ТЗ
  useEffect(() => {
    if (project) {
      const stored = localStorage.getItem(`sima_tz_generated_${project.id}`)
      setTzGeneratedAt(stored)
    }
  }, [project?.id])

  // Проверяем устарело ли ТЗ: если блоки обновились после генерации
  const isTzStale = useMemo(() => {
    if (!project?.generatedTZ || !tzGeneratedAt) return false
    const tzTime = new Date(tzGeneratedAt).getTime()
    return allBlocks.some(b => new Date(b.updatedAt).getTime() > tzTime)
  }, [project?.generatedTZ, tzGeneratedAt, allBlocks])

  // Загрузка решений при переключении на вкладку
  useEffect(() => {
    if (activeSection === 'decisions' && project && decisions.length === 0 && !loadingDecisions) {
      loadDecisions()
    }
  }, [activeSection, project])

  async function loadDecisions() {
    if (!project) return
    setLoadingDecisions(true)
    try {
      const res = await simaFetch(`/decisions?projectId=${project.id}`)
      const data = await res.json()
      setDecisions(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Failed to load decisions:', e)
    } finally {
      setLoadingDecisions(false)
    }
  }

  async function deleteDecision(id: string) {
    try {
      await simaFetch(`/decisions?id=${id}`, { method: 'DELETE' })
      setDecisions(prev => prev.filter(d => d.id !== id))
    } catch (e) {
      console.error('Failed to delete decision:', e)
    }
  }

  if (!project) return null

  const hasNarrative = !!(
    project.narrativeProblem ||
    project.narrativeSolution ||
    project.narrativeHowItWorks
  )

  async function generateNarrative() {
    setIsGeneratingNarrative(true)
    try {
      const res = await simaFetch('/ai/generate-narrative', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project!.id }),
      })
      const data = await res.json()
      
      if (data.error) {
        alert(data.error)
        return
      }
      
      setProject({
        ...project!,
        narrativeProblem: data.problem,
        narrativeSolution: data.solution,
        narrativeHowItWorks: data.howItWorks,
        narrativeProductMap: data.productMap,
        narrativeDecisions: data.decisions,
      })
    } catch (e) {
      console.error('Failed to generate narrative:', e)
    } finally {
      setIsGeneratingNarrative(false)
    }
  }

  async function generateTZ() {
    setIsGeneratingTZ(true)
    try {
      const body: Record<string, unknown> = { projectId: project!.id }
      if (selectedMeetingIds.size > 0) body.meetingIds = Array.from(selectedMeetingIds)
      if (selectedDocumentIds.size > 0) body.documentIds = Array.from(selectedDocumentIds)

      const res = await simaFetch('/ai/generate-tz', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await res.json()
      setProject({ ...project!, generatedTZ: data.tz })
      // Запоминаем время генерации для отслеживания устаревания
      const now = new Date().toISOString()
      localStorage.setItem(`sima_tz_generated_${project!.id}`, now)
      setTzGeneratedAt(now)
    } catch (e) {
      console.error('Failed to generate TZ:', e)
    } finally {
      setIsGeneratingTZ(false)
    }
  }

  async function createSchemaFromTZ() {
    if (!project) return
    setIsCreatingSchema(true)
    setSchemaMessage(null)
    setSchemaIsError(false)
    try {
      const body: Record<string, unknown> = { projectId: project.id }
      if (selectedMeetingIds.size > 0) body.meetingIds = Array.from(selectedMeetingIds)
      if (selectedDocumentIds.size > 0) body.documentIds = Array.from(selectedDocumentIds)

      const res = await simaFetch('/ai/tz-to-schema', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await res.json()

      if (data.error) {
        setSchemaIsError(true)
        setSchemaMessage(t('err_prefix', { error: data.error }))
        return
      }

      // Обновляем проект, блоки и связи в сторе
      if (data.project) {
        console.log('[SIMA] NarrativePanel received project:', {
          blocks: data.project.blocks?.length || 0,
          connections: data.project.connections?.length || 0,
          projectId: data.project.id,
        })
        setAllBlocks(data.project.blocks || [])
        setAllConnections(data.project.connections || [])
        setProject(data.project)
      } else {
        console.warn('[SIMA] NarrativePanel: no project in response, status:', res.status, 'data keys:', Object.keys(data))
      }

      const blockCount = (data.actions || []).filter((a: { type: string }) => a.type === 'CREATE_BLOCK').length
      const connCount = (data.actions || []).filter((a: { type: string }) => a.type === 'CREATE_CONNECTION').length
      setSchemaIsError(false)
      setSchemaMessage(t('schema_created', { blocks: blockCount, connections: connCount }))
    } catch (e) {
      console.error('Failed to create schema:', e)
      setSchemaIsError(true)
      setSchemaMessage(t('err_create_schema'))
    } finally {
      setIsCreatingSchema(false)
    }
  }

  function copyToClipboard(text: string) {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  function downloadAsMarkdown(text: string, filename: string) {
    const blob = new Blob([text], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  const narrativeText = [
    project.narrativeProblem && `${t('narrative_problem_heading')}\n\n${project.narrativeProblem}`,
    project.narrativeSolution && `${t('narrative_solution_heading')}\n\n${project.narrativeSolution}`,
    project.narrativeHowItWorks && `${t('narrative_how_heading')}\n\n${project.narrativeHowItWorks}`,
    project.narrativeProductMap && `${t('narrative_map_heading')}\n\n${project.narrativeProductMap}`,
    project.narrativeDecisions && `${t('narrative_decisions_heading')}\n\n${project.narrativeDecisions}`,
  ]
    .filter(Boolean)
    .join('\n\n---\n\n')

  return (
    <div className="flex flex-col h-full">
      {/* Sub-tabs */}
      <div className="flex gap-1 px-4 pt-3">
        <button
          onClick={() => setActiveSection('narrative')}
          className={`px-3 py-1.5 text-xs rounded-lg transition-colors ${
            activeSection === 'narrative'
              ? 'bg-sima-primary/20 text-sima-primary'
              : 'text-sima-textDim hover:text-sima-textMuted'
          }`}
        >
          {t('tab_narrative')}
        </button>
        <button
          onClick={() => setActiveSection('tz')}
          className={`px-3 py-1.5 text-xs rounded-lg transition-colors ${
            activeSection === 'tz'
              ? 'bg-sima-primary/20 text-sima-primary'
              : 'text-sima-textDim hover:text-sima-textMuted'
          }`}
        >
          {t('tab_tz')}
        </button>
        <button
          onClick={() => setActiveSection('decisions')}
          className={`px-3 py-1.5 text-xs rounded-lg transition-colors flex items-center gap-1 ${
            activeSection === 'decisions'
              ? 'bg-amber-400/20 text-amber-400'
              : 'text-sima-textDim hover:text-sima-textMuted'
          }`}
        >
          <Scale className="w-3 h-3" />
          {t('tab_decisions')}
          {decisions.length > 0 && (
            <span className="text-[9px] bg-amber-400/20 text-amber-400 px-1 rounded">{decisions.length}</span>
          )}
        </button>
        <button
          onClick={() => setActiveSection('promo')}
          className={`px-3 py-1.5 text-xs rounded-lg transition-colors flex items-center gap-1 ${
            activeSection === 'promo'
              ? 'bg-pink-400/20 text-pink-400'
              : 'text-sima-textDim hover:text-sima-textMuted'
          }`}
        >
          <Sparkles className="w-3 h-3" />
          {t('tab_promo')}
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4">
        {activeSection === 'promo' && (
          <div className="space-y-3">
            <p className="text-[11px] text-sima-textDim">{t('promo_hint')}</p>
            <div className="flex items-center gap-2">
              <select
                value={promoKind}
                onChange={(e) => { setPromoKind(e.target.value); setPromoResult(null) }}
                className="px-2 py-1.5 rounded-lg bg-sima-bg border border-sima-border text-[11px] text-sima-text"
              >
                <option value="benefits">{t('promo_kind_benefits')}</option>
                <option value="landing">{t('promo_kind_landing')}</option>
                <option value="pitch">{t('promo_kind_pitch')}</option>
                <option value="ads">{t('promo_kind_ads')}</option>
                <option value="social">{t('promo_kind_social')}</option>
              </select>
              <button
                onClick={generatePromo}
                disabled={promoLoading || !project}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-pink-400/40 bg-pink-400/10 hover:bg-pink-400/20 text-[11px] font-medium text-sima-text transition-all"
              >
                {promoLoading ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5 text-pink-400" />}
                {t('promo_generate')}
              </button>
              {promoResult && (
                <button
                  onClick={() => navigator.clipboard.writeText(promoResult.markdown)}
                  className="flex items-center gap-1 px-2 py-1.5 rounded-lg border border-sima-border text-[11px] text-sima-textMuted hover:text-sima-text transition-colors"
                >
                  <Copy className="w-3 h-3" />
                  {t('promo_copy')}
                </button>
              )}
            </div>
            {promoError && <p className="text-[11px] text-amber-400">{promoError}</p>}
            {promoResult && (
              <div className="p-3 rounded-lg border border-sima-border bg-sima-bg">
                {promoResult.llm_used === false && (
                  <p className="text-[10px] text-amber-400 mb-2">{t('promo_fallback_note')}</p>
                )}
                <div className="prose prose-invert prose-sm max-w-none text-sima-text">
                  <ReactMarkdown>{promoResult.markdown}</ReactMarkdown>
                </div>
              </div>
            )}
          </div>
        )}
        {activeSection === 'narrative' && (
          <>
            {blocks.length === 0 ? (
              <div className="text-center py-8 text-sima-textDim text-sm">
                <FileText className="w-10 h-10 mx-auto mb-3 opacity-30" />
                <p>{t('describe_first_hint')}</p>
              </div>
            ) : !hasNarrative ? (
              <div className="text-center py-8">
                <p className="text-sm text-sima-textDim mb-4">
                  {t('ready_to_generate_narrative', { count: blocks.length })}
                </p>
                <button
                  onClick={generateNarrative}
                  disabled={isGeneratingNarrative}
                  className="px-4 py-2 bg-sima-primary hover:bg-sima-primaryHover text-white rounded-lg text-sm font-medium flex items-center gap-2 mx-auto transition-colors disabled:opacity-50"
                >
                  {isGeneratingNarrative ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <Sparkles className="w-4 h-4" />
                  )}
                  {isGeneratingNarrative ? t('generating') : t('generate_narrative')}
                </button>
              </div>
            ) : (
              <>
                <div className="flex items-center gap-2 mb-4">
                  <button
                    onClick={generateNarrative}
                    disabled={isGeneratingNarrative}
                    className="px-3 py-1.5 bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted rounded-lg text-xs flex items-center gap-1.5 transition-colors"
                  >
                    <RefreshCw className={`w-3 h-3 ${isGeneratingNarrative ? 'animate-spin' : ''}`} />
                    {t('refresh')}
                  </button>
                  <button
                    onClick={() => copyToClipboard(narrativeText)}
                    className="px-3 py-1.5 bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted rounded-lg text-xs flex items-center gap-1.5 transition-colors"
                  >
                    {copied ? <CheckCircle className="w-3 h-3 text-green-400" /> : <Copy className="w-3 h-3" />}
                    {copied ? t('copied') : t('copy')}
                  </button>
                  <button
                    onClick={() => downloadAsMarkdown(narrativeText, `${project.name}_narrative.md`)}
                    className="px-3 py-1.5 bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted rounded-lg text-xs flex items-center gap-1.5 transition-colors"
                  >
                    <Download className="w-3 h-3" />
                    {t('download')}
                  </button>
                </div>

                <div className="prose prose-invert prose-sm max-w-none">
                  <ReactMarkdown>{narrativeText}</ReactMarkdown>
                </div>
              </>
            )}
          </>
        )}

        {activeSection === 'tz' && (
          <>
            {/* Индикатор устаревания */}
            {isTzStale && project.generatedTZ && (
              <div className="mb-3 flex items-center gap-2 p-2.5 bg-amber-400/10 border border-amber-400/20 rounded-lg">
                <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
                <div className="flex-1">
                  <span className="text-xs font-medium text-amber-400">{t('tz_outdated_label')}</span>
                  <p className="text-[10px] text-amber-400/70">{t('tz_outdated_hint')}</p>
                </div>
                <button
                  onClick={generateTZ}
                  disabled={isGeneratingTZ}
                  className="px-2 py-1 bg-amber-400/20 text-amber-400 rounded text-[10px] font-medium hover:bg-amber-400/30 transition-colors flex items-center gap-1"
                >
                  <RefreshCw className={`w-2.5 h-2.5 ${isGeneratingTZ ? 'animate-spin' : ''}`} />
                  {t('refresh')}
                </button>
              </div>
            )}
            {!project.generatedTZ ? (
              <div className="py-6 px-2">
                <p className="text-sm text-sima-textDim mb-4 text-center">
                  {blocks.length === 0
                    ? t('describe_in_chat')
                    : t('ready_to_generate_tz')}
                </p>
                {blocks.length > 0 && (
                  <div className="space-y-4">
                    {/* Источники данных — пикер */}
                    <div className="border border-sima-border rounded-lg overflow-hidden">
                      <button
                        onClick={() => {
                          setShowSourcePicker(!showSourcePicker)
                          if (!showSourcePicker && tessentMeetings.length === 0 && tessentDocuments.length === 0) {
                            loadTessentSources()
                          }
                        }}
                        className="w-full flex items-center justify-between px-3 py-2.5 bg-sima-surface hover:bg-sima-surfaceLight transition-colors text-left"
                      >
                        <div className="flex items-center gap-2 text-sm">
                          {showSourcePicker ? <ChevronDown className="w-4 h-4 text-sima-textMuted" /> : <ChevronRight className="w-4 h-4 text-sima-textMuted" />}
                          <Folder className="w-4 h-4 text-sima-primary" />
                          <span className="text-sima-text">{t('select_meetings_docs')}</span>
                        </div>
                        {totalSelected > 0 && (
                          <span className="text-xs bg-sima-primary/20 text-sima-primary px-2 py-0.5 rounded-full">
                            {t('selected_count', { count: totalSelected })}
                          </span>
                        )}
                      </button>

                      {showSourcePicker && (
                        <div className="border-t border-sima-border bg-sima-bg">
                          {loadingSources ? (
                            <div className="flex items-center justify-center py-4 gap-2 text-xs text-sima-textMuted">
                              <RefreshCw className="w-3 h-3 animate-spin" /> {t('loading')}
                            </div>
                          ) : (
                            <div className="max-h-72 overflow-y-auto">
                              {/* Встречи */}
                              <div className="px-3 pt-3 pb-1">
                                <p className="text-xs font-semibold text-sima-textMuted uppercase tracking-wider mb-2">{t('meetings_heading', { count: tessentMeetings.filter(m => m.hasTranscript).length })}</p>
                                <div className="relative mb-2">
                                  <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-sima-textMuted" />
                                  <input
                                    value={meetingSearch}
                                    onChange={e => setMeetingSearch(e.target.value)}
                                    placeholder={t("search_meeting")}
                                    className="w-full pl-7 pr-2 py-1 bg-sima-surface border border-sima-border rounded text-xs text-sima-text placeholder:text-sima-textMuted/50 outline-none focus:border-sima-primary/50"
                                  />
                                </div>
                                {tessentMeetings.length === 0 ? (
                                  <p className="text-xs text-sima-textMuted py-1">{t('no_meetings')}</p>
                                ) : (
                                  <div className="space-y-0.5 max-h-32 overflow-y-auto">
                                    {tessentMeetings
                                      .filter(m => m.hasTranscript)
                                      .filter(m => !meetingSearch || m.title.toLowerCase().includes(meetingSearch.toLowerCase()))
                                      .map(m => (
                                        <label key={m.id} className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-sima-surfaceLight cursor-pointer group transition-colors">
                                          <input
                                            type="checkbox"
                                            checked={selectedMeetingIds.has(m.id)}
                                            onChange={() => toggleMeeting(m.id)}
                                            className="accent-sima-primary w-3.5 h-3.5 rounded"
                                          />
                                          <div className="flex-1 min-w-0">
                                            <p className="text-xs text-sima-text truncate">{m.title}</p>
                                            <p className="text-[10px] text-sima-textMuted">
                                              {m.date?.split('T')[0]}
                                              {m.projectName && ` · ${m.projectName}`}
                                              {m.folderName && ` / ${m.folderName}`}
                                            </p>
                                          </div>
                                        </label>
                                      ))}
                                  </div>
                                )}
                              </div>

                              {/* Документы */}
                              <div className="px-3 pt-2 pb-3 border-t border-sima-border/50">
                                <p className="text-xs font-semibold text-sima-textMuted uppercase tracking-wider mb-2">{t('documents_heading', { count: tessentDocuments.length })}</p>
                                <div className="relative mb-2">
                                  <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-sima-textMuted" />
                                  <input
                                    value={docSearch}
                                    onChange={e => setDocSearch(e.target.value)}
                                    placeholder={t("search_document")}
                                    className="w-full pl-7 pr-2 py-1 bg-sima-surface border border-sima-border rounded text-xs text-sima-text placeholder:text-sima-textMuted/50 outline-none focus:border-sima-primary/50"
                                  />
                                </div>
                                {tessentDocuments.length === 0 ? (
                                  <p className="text-xs text-sima-textMuted py-1">{t('no_documents')}</p>
                                ) : (
                                  <div className="space-y-0.5 max-h-32 overflow-y-auto">
                                    {tessentDocuments
                                      .filter(d => !docSearch || d.title.toLowerCase().includes(docSearch.toLowerCase()))
                                      .map(d => (
                                        <label key={d.id} className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-sima-surfaceLight cursor-pointer group transition-colors">
                                          <input
                                            type="checkbox"
                                            checked={selectedDocumentIds.has(d.id)}
                                            onChange={() => toggleDocument(d.id)}
                                            className="accent-sima-primary w-3.5 h-3.5 rounded"
                                          />
                                          <div className="flex-1 min-w-0">
                                            <p className="text-xs text-sima-text truncate">{d.title}</p>
                                            {d.docType && (
                                              <p className="text-[10px] text-sima-textMuted">{d.docType}{d.createdAt ? ` · ${d.createdAt.split('T')[0]}` : ''}</p>
                                            )}
                                          </div>
                                        </label>
                                      ))}
                                  </div>
                                )}
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                    </div>

                    {/* Индикаторы */}
                    {(dataSourcesCount > 0 || totalSelected > 0) && (
                      <div className="text-center space-y-1">
                        {dataSourcesCount > 0 && (
                          <p className="text-xs text-sima-primary/70">
                            {t('imported_sources_count', { count: dataSourcesCount })}
                          </p>
                        )}
                        {totalSelected > 0 && (
                          <p className="text-xs text-sima-accent/80 flex items-center gap-1 justify-center">
                            <FileCheck className="w-3 h-3" />
                            + {selectedMeetingIds.size > 0 && t('meetings_count', { count: selectedMeetingIds.size })}
                            {selectedMeetingIds.size > 0 && selectedDocumentIds.size > 0 && ', '}
                            {selectedDocumentIds.size > 0 && t('documents_count', { count: selectedDocumentIds.size })}
                          </p>
                        )}
                      </div>
                    )}

                    {/* Кнопки генерации */}
                    <div className="flex items-center justify-center gap-3 flex-wrap">
                      <button
                        onClick={generateTZ}
                        disabled={isGeneratingTZ || isCreatingSchema}
                        className="px-4 py-2 bg-sima-accent hover:bg-sima-accent/80 text-sima-bg rounded-lg text-sm font-medium flex items-center gap-2 transition-colors disabled:opacity-50"
                      >
                        {isGeneratingTZ ? (
                          <RefreshCw className="w-4 h-4 animate-spin" />
                        ) : (
                          <FileText className="w-4 h-4" />
                        )}
                        {isGeneratingTZ ? t('generating_tz') : t('generate_tz')}
                      </button>
                      <button
                        onClick={createSchemaFromTZ}
                        disabled={isCreatingSchema || isGeneratingTZ}
                        className="px-4 py-2 bg-sima-primary/20 hover:bg-sima-primary/30 text-sima-primary border border-sima-primary/30 rounded-lg text-sm font-medium flex items-center gap-2 transition-colors disabled:opacity-50"
                        title={t("generate_tz_and_schema_title")}
                      >
                        {isCreatingSchema ? (
                          <RefreshCw className="w-4 h-4 animate-spin" />
                        ) : (
                          <LayoutGrid className="w-4 h-4" />
                        )}
                        {isCreatingSchema ? t('creating') : t('create_schema_now')}
                      </button>
                    </div>
                    {schemaMessage && (
                      <div className={`text-center text-xs mt-2 px-3 py-2 rounded-lg ${
                        schemaIsError
                          ? 'bg-red-500/10 text-red-400'
                          : 'bg-sima-accentGreen/10 text-sima-accentGreen'
                      }`}>
                        {schemaMessage}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <>
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <button
                    onClick={generateTZ}
                    disabled={isGeneratingTZ}
                    className="px-3 py-1.5 bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted rounded-lg text-xs flex items-center gap-1.5 transition-colors"
                  >
                    <RefreshCw className={`w-3 h-3 ${isGeneratingTZ ? 'animate-spin' : ''}`} />
                    {t('refresh')}
                  </button>
                  <button
                    onClick={() => {
                      setShowSourcePicker(!showSourcePicker)
                      if (!showSourcePicker && tessentMeetings.length === 0 && tessentDocuments.length === 0) {
                        loadTessentSources()
                      }
                    }}
                    className={`px-3 py-1.5 rounded-lg text-xs flex items-center gap-1.5 transition-colors ${
                      totalSelected > 0
                        ? 'bg-sima-primary/20 text-sima-primary hover:bg-sima-primary/30'
                        : 'bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted'
                    }`}
                  >
                    <Folder className="w-3 h-3" />
                    {t('sources_label')}{totalSelected > 0 ? ` (${totalSelected})` : ''}
                  </button>
                  <button
                    onClick={createSchemaFromTZ}
                    disabled={isCreatingSchema}
                    className="px-3 py-1.5 bg-sima-accent/20 hover:bg-sima-accent/30 text-sima-accent rounded-lg text-xs flex items-center gap-1.5 transition-colors disabled:opacity-50"
                    title={t("create_schema_from_tz_title")}
                  >
                    {isCreatingSchema ? (
                      <RefreshCw className="w-3 h-3 animate-spin" />
                    ) : (
                      <LayoutGrid className="w-3 h-3" />
                    )}
                    {isCreatingSchema ? t('creating') : t('create_schema')}
                  </button>
                  <button
                    onClick={() => copyToClipboard(project.generatedTZ || '')}
                    className="px-3 py-1.5 bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted rounded-lg text-xs flex items-center gap-1.5 transition-colors"
                  >
                    <Copy className="w-3 h-3" />
                    {t('copy')}
                  </button>
                  <button
                    onClick={() => downloadAsMarkdown(project.generatedTZ || '', `${project.name}_TZ.md`)}
                    className="px-3 py-1.5 bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted rounded-lg text-xs flex items-center gap-1.5 transition-colors"
                  >
                    <Download className="w-3 h-3" />
                    {t('download')}
                  </button>
                  {/* «в Claude Code → реализация»: ведёт на слой «Реализация»
                      (ExportPanel), где запускается защищённый kanon-handoff
                      подписочным CLI. Кнопка на самой странице результата —
                      как в прототипе Sima Remix. */}
                  <button
                    onClick={() => setActiveLayer('impl')}
                    className="px-3 py-1.5 bg-sima-primary/15 border border-sima-primary/40 hover:bg-sima-primary/25 text-sima-primary rounded-lg text-xs flex items-center gap-1.5 transition-colors font-medium"
                  >
                    <Sparkles className="w-3 h-3" />
                    {t('to_claude_code')}
                  </button>
                </div>

                {/* Трассировка: из чего собрано ТЗ */}
                <p className="text-[11px] text-sima-textDim mt-2 mb-2">
                  {t('tz_assembled_from', { sources: dataSourcesCount, blocks: blocks.length })}
                </p>

                {/* Результат создания схемы */}
                {schemaMessage && (
                  <div className={`px-3 py-2 rounded-lg text-xs mb-3 flex items-center gap-2 ${
                    schemaIsError
                      ? 'bg-red-500/10 text-red-400 border border-red-500/20'
                      : 'bg-sima-accentGreen/10 text-sima-accentGreen border border-sima-accentGreen/20'
                  }`}>
                    {schemaIsError ? (
                      <AlertTriangle className="w-3 h-3" />
                    ) : (
                      <CheckCircle className="w-3 h-3" />
                    )}
                    {schemaMessage}
                    <button onClick={() => { setSchemaMessage(null); setSchemaIsError(false) }} className="ml-auto text-current opacity-50 hover:opacity-100">&times;</button>
                  </div>
                )}

                {/* Мини-пикер источников при обновлении */}
                {showSourcePicker && (
                  <div className="border border-sima-border rounded-lg mb-4 overflow-hidden bg-sima-bg">
                    {loadingSources ? (
                      <div className="flex items-center justify-center py-3 gap-2 text-xs text-sima-textMuted">
                        <RefreshCw className="w-3 h-3 animate-spin" /> {t('loading')}
                      </div>
                    ) : (
                      <div className="max-h-60 overflow-y-auto">
                        {/* Встречи */}
                        <div className="px-3 pt-2 pb-1">
                          <p className="text-[10px] font-semibold text-sima-textMuted uppercase tracking-wider mb-1">{t('meetings_heading', { count: tessentMeetings.length })}</p>
                          <div className="relative mb-1">
                            <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-sima-textMuted" />
                            <input value={meetingSearch} onChange={e => setMeetingSearch(e.target.value)} placeholder={t("search_short")} className="w-full pl-7 pr-2 py-1 bg-sima-surface border border-sima-border rounded text-xs text-sima-text placeholder:text-sima-textMuted/50 outline-none focus:border-sima-primary/50" />
                          </div>
                          <div className="space-y-0.5 max-h-28 overflow-y-auto">
                            {tessentMeetings.filter(m => m.hasTranscript).filter(m => !meetingSearch || m.title.toLowerCase().includes(meetingSearch.toLowerCase())).map(m => (
                              <label key={m.id} className="flex items-center gap-2 px-1.5 py-1 rounded hover:bg-sima-surfaceLight cursor-pointer text-xs">
                                <input type="checkbox" checked={selectedMeetingIds.has(m.id)} onChange={() => toggleMeeting(m.id)} className="accent-sima-primary w-3 h-3" />
                                <span className="truncate text-sima-text">{m.title}</span>
                                <span className="text-[10px] text-sima-textMuted ml-auto shrink-0">{m.date?.split('T')[0]}</span>
                              </label>
                            ))}
                          </div>
                        </div>
                        {/* Документы */}
                        <div className="px-3 pt-1 pb-2 border-t border-sima-border/50">
                          <p className="text-[10px] font-semibold text-sima-textMuted uppercase tracking-wider mb-1">{t('documents_heading', { count: tessentDocuments.length })}</p>
                          <div className="relative mb-1">
                            <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-sima-textMuted" />
                            <input value={docSearch} onChange={e => setDocSearch(e.target.value)} placeholder={t("search_short")} className="w-full pl-7 pr-2 py-1 bg-sima-surface border border-sima-border rounded text-xs text-sima-text placeholder:text-sima-textMuted/50 outline-none focus:border-sima-primary/50" />
                          </div>
                          <div className="space-y-0.5 max-h-28 overflow-y-auto">
                            {tessentDocuments.filter(d => !docSearch || d.title.toLowerCase().includes(docSearch.toLowerCase())).map(d => (
                              <label key={d.id} className="flex items-center gap-2 px-1.5 py-1 rounded hover:bg-sima-surfaceLight cursor-pointer text-xs">
                                <input type="checkbox" checked={selectedDocumentIds.has(d.id)} onChange={() => toggleDocument(d.id)} className="accent-sima-primary w-3 h-3" />
                                <span className="truncate text-sima-text">{d.title}</span>
                              </label>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                    {totalSelected > 0 && (
                      <div className="px-3 py-1.5 border-t border-sima-border bg-sima-surface text-[10px] text-sima-accent/80 flex items-center gap-1">
                        <FileCheck className="w-3 h-3" />
                        {selectedMeetingIds.size > 0 && t('meetings_count', { count: selectedMeetingIds.size })}
                        {selectedMeetingIds.size > 0 && selectedDocumentIds.size > 0 && ', '}
                        {selectedDocumentIds.size > 0 && t('documents_count', { count: selectedDocumentIds.size })}
                        <span className="text-sima-textMuted ml-1">{t('will_be_included')}</span>
                      </div>
                    )}
                  </div>
                )}

                <div className="prose prose-invert prose-sm max-w-none">
                  <ReactMarkdown>{project.generatedTZ}</ReactMarkdown>
                </div>
              </>
            )}
          </>
        )}

        {activeSection === 'decisions' && (
          <>
            {loadingDecisions ? (
              <div className="flex items-center justify-center py-8">
                <RefreshCw className="w-5 h-5 animate-spin text-sima-textDim" />
              </div>
            ) : decisions.length === 0 ? (
              <div className="text-center py-8 text-sima-textDim text-sm">
                <Scale className="w-10 h-10 mx-auto mb-3 opacity-30" />
                <p>{t("no_decisions")}</p>
                <p className="text-[11px] mt-1">{t('decisions_hint')}</p>
              </div>
            ) : (
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-sima-textDim">{t('decisions_count', { count: decisions.length })}</span>
                  <button
                    onClick={loadDecisions}
                    className="px-2 py-1 bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted rounded text-[10px] flex items-center gap-1 transition-colors"
                  >
                    <RefreshCw className="w-2.5 h-2.5" />
                    {t('refresh')}
                  </button>
                </div>

                {decisions.map((decision) => (
                  <div key={decision.id} className="bg-sima-bg border border-sima-border rounded-lg p-3">
                    <div className="flex items-start justify-between gap-2">
                      <h4 className="text-xs font-semibold text-sima-text flex items-center gap-1.5">
                        <Scale className="w-3 h-3 text-amber-400 shrink-0" />
                        {decision.title}
                      </h4>
                      <button
                        onClick={() => deleteDecision(decision.id)}
                        className="text-sima-textDim hover:text-red-400 transition-colors shrink-0"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </div>
                    <p className="text-[11px] text-sima-textMuted mt-1.5 leading-relaxed">{decision.description}</p>
                    {decision.reasoning && (
                      <div className="mt-2 pt-2 border-t border-sima-border/50">
                        <span className="text-[10px] text-sima-textDim font-medium">{t('why_label')}</span>
                        <p className="text-[10px] text-sima-textDim mt-0.5">{decision.reasoning}</p>
                      </div>
                    )}
                    {decision.alternatives && (
                      <div className="mt-1">
                        <span className="text-[10px] text-sima-textDim font-medium">{t('alternatives_label')}</span>
                        <p className="text-[10px] text-sima-textDim mt-0.5">{decision.alternatives}</p>
                      </div>
                    )}
                    <span className="text-[9px] text-sima-textDim mt-2 block">
                      {new Date(decision.createdAt).toLocaleString(locale)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
