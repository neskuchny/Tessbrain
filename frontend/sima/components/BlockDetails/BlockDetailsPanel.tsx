'use client'

import { useState, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { cn } from '@/sima/lib/utils'
import {
  Target,
  Lightbulb,
  Wrench,
  Globe,
  X,
  Package,
  RefreshCw,
  Sparkles,
  Brain,
  Loader2,
  FolderOpen,
  FolderPlus,
  StickyNote,
  Database,
  Link2,
  Unlink,
} from 'lucide-react'
import { calculateBlockProgress, calculateSectionProgress, calculateBlockChecklist, BLOCK_STATUS_COLORS } from '@/sima/types'
import type { BlockData } from '@/sima/types'
import { simaFetch } from '@/sima/lib/api'

const buildTabs = (t: (k: string) => string) => [
  { id: 'why', label: t('tab_why'), icon: Target },
  { id: 'what', label: t('tab_what'), icon: Lightbulb },
  { id: 'how', label: t('tab_how'), icon: Wrench },
  { id: 'context', label: t('tab_context'), icon: Globe },
] as const

type TabId = 'why' | 'what' | 'how' | 'context'

const buildTabFields = (t: (k: string) => string): Record<TabId, { key: keyof BlockData; label: string }[]> => ({
  why: [
    { key: 'businessValue', label: t('field_businessValue') },
    { key: 'userBenefit', label: t('field_userBenefit') },
    { key: 'problemSolved', label: t('field_problemSolved') },
    { key: 'impactIfRemoved', label: t('field_impactIfRemoved') },
    { key: 'metrics', label: t('field_metrics') },
  ],
  what: [
    { key: 'description', label: t('field_description') },
    { key: 'sourceTake', label: t('field_sourceTake') },
    { key: 'goal', label: t('field_goal') },
    { key: 'inputData', label: t('field_inputData') },
    { key: 'outputData', label: t('field_outputData') },
    { key: 'acceptanceCriteria', label: t('field_acceptanceCriteria') },
    { key: 'userFlow', label: t('field_userFlow') },
    { key: 'edgeCases', label: t('field_edgeCases') },
  ],
  how: [
    { key: 'implementation', label: t('field_implementation') },
    { key: 'blockTechStack', label: t('field_blockTechStack') },
    { key: 'apiEndpoints', label: t('field_apiEndpoints') },
    { key: 'dataModel', label: t('field_dataModel') },
    { key: 'filesToCreate', label: t('field_filesToCreate') },
    { key: 'technicalDeps', label: t('field_technicalDeps') },
    { key: 'estimatedComplexity', label: t('field_estimatedComplexity') },
  ],
  context: [
    { key: 'analogues', label: t('field_analogues') },
    { key: 'alternatives', label: t('field_alternatives') },
    { key: 'risks', label: t('field_risks') },
    { key: 'futureEvolution', label: t('field_futureEvolution') },
    { key: 'problems', label: t('field_problems') },
  ],
})

export default function BlockDetailsPanel() {
  const t = useTranslations('sima_block_details')
  const tabs = buildTabs(t)
  const tabFields = buildTabFields(t)
  const {
    selectedBlockId,
    blocks,
    updateBlock,
    setSelectedBlockId,
    project,
    drillDown,
    allBlocks,
  } = useSimaStore()

  const {
    setBottomPanelOpen,
    setBottomPanelTab,
    setStrategistReport,
    strategistReport,
  } = useSimaStore()

  const [activeTab, setActiveTab] = useState<TabId>('why')
  const [editingField, setEditingField] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [isAnalyzingBlock, setIsAnalyzingBlock] = useState(false)
  const [pmNote, setPmNote] = useState('')
  const [pmNoteEditing, setPmNoteEditing] = useState(false)
  const [blockInsights, setBlockInsights] = useState<{ id: string; category: string; title: string; content: string; evidence: string | null; confidence: number; priority: string; dataSource: { name: string } }[]>([])
  const [loadingInsights, setLoadingInsights] = useState(false)

  // PM-заметки из localStorage
  useEffect(() => {
    if (selectedBlockId) {
      const saved = localStorage.getItem(`sima_note_${selectedBlockId}`)
      setPmNote(saved || '')
      setPmNoteEditing(false)
    }
  }, [selectedBlockId])

  // Загрузка привязанных инсайтов
  useEffect(() => {
    if (!selectedBlockId || !project?.id) {
      setBlockInsights([])
      return
    }
    let cancelled = false
    const loadInsights = async () => {
      setLoadingInsights(true)
      try {
        const res = await simaFetch(`/data-sources?projectId=${project.id}`)
        const data = await res.json()
        const allInsights: typeof blockInsights = []
        for (const source of data.sources || []) {
          for (const insight of source.insights || []) {
            if (insight.linkedBlockId === selectedBlockId) {
              allInsights.push({ ...insight, dataSource: { name: source.name } })
            }
          }
        }
        if (!cancelled) setBlockInsights(allInsights)
      } catch {
        // ignore
      } finally {
        if (!cancelled) setLoadingInsights(false)
      }
    }
    loadInsights()
    return () => { cancelled = true }
  }, [selectedBlockId, project?.id])

  const handleUnlinkFromBlock = async (insightId: string) => {
    if (!project?.id) return
    try {
      await simaFetch('/data-sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, action: 'unlink', insightId }),
      })
      setBlockInsights(prev => prev.filter(i => i.id !== insightId))
    } catch { /* ignore */ }
  }

  const savePmNote = (text: string) => {
    setPmNote(text)
    if (selectedBlockId) {
      if (text.trim()) {
        localStorage.setItem(`sima_note_${selectedBlockId}`, text)
      } else {
        localStorage.removeItem(`sima_note_${selectedBlockId}`)
      }
    }
    setPmNoteEditing(false)
  }

  // сообщение «сохранено в библиотеку» — хук ДО раннего return
  const [libraryMsg, setLibraryMsg] = useState('')

  // Kanon-готовность блока («можно ли отдавать в разработку») — хуки ДО
  // раннего return. Панель для непрограммиста: что дозаполнить, чтобы SIMA
  // могла отдать блок агенту и сама проверить результат.
  const [kanonRow, setKanonRow] = useState<any | null>(null)
  const [kanonAvailable, setKanonAvailable] = useState(false)
  const [kanonRefresh, setKanonRefresh] = useState(0)
  const [accBusy, setAccBusy] = useState(false)
  const [accMsg, setAccMsg] = useState('')

  useEffect(() => {
    if (!project?.id || !selectedBlockId) { setKanonRow(null); return }
    let cancelled = false
    ;(async () => {
      try {
        const res = await simaFetch(`/kanon/status?projectId=${project.id}`)
        const data = await res.json()
        if (cancelled) return
        if (data?.success === false) { setKanonAvailable(false); setKanonRow(null); return }
        setKanonAvailable(true)
        setKanonRow((data.blocks || []).find((b: any) => String(b.block_id) === String(selectedBlockId)) || null)
      } catch { if (!cancelled) setKanonAvailable(false) }
    })()
    return () => { cancelled = true }
  }, [project?.id, selectedBlockId, kanonRefresh])

  const block = blocks.find((b) => b.id === selectedBlockId)
  if (!block) return null

  const progress = calculateBlockProgress(block)
  const checklist = calculateBlockChecklist(block)

  const sectionProgressMap: Record<TabId, number> = {
    why: calculateSectionProgress(checklist.business),
    what: calculateSectionProgress(checklist.functional),
    how: calculateSectionProgress(checklist.technical),
    context: calculateSectionProgress(checklist.context),
  }

  function startEdit(key: string, currentValue: string | null | undefined) {
    setEditingField(key)
    setEditValue(currentValue || '')
  }

  async function saveBlockField(key: string, value: string | boolean) {
    updateBlock(block!.id, { [key]: value } as Partial<BlockData>)
    try {
      await simaFetch('/blocks', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: block!.id, [key]: value }),
      })
    } catch (e) {
      console.error('Failed to save block field:', e)
    }
  }

  async function saveEdit(key: string) {
    setEditingField(null)
    updateBlock(block!.id, { [key]: editValue } as Partial<BlockData>)
    
    try {
      await simaFetch('/blocks', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: block!.id, [key]: editValue }),
      })
    } catch (e) {
      console.error('Failed to save block field:', e)
    }
  }

  // Автогенерация проверок приёмки («за ручку»: file:/contains:/cmd: пишет
  // SIMA из полей блока, сотруднику не нужно уметь их формулировать).
  async function generateAcceptance() {
    if (!project || !selectedBlockId || accBusy) return
    setAccBusy(true)
    setAccMsg('')
    try {
      const res = await simaFetch('/kanon/suggest-acceptance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, blockId: selectedBlockId, apply: true }),
      })
      const data = await res.json()
      if (data.success === false) {
        setAccMsg(data.error || t('acceptance_failed'))
      } else {
        const text = (data.criteria || []).map((c: string) => `- ${c}`).join('\n')
        updateBlock(selectedBlockId, { acceptanceCriteria: text } as Partial<BlockData>)
        setAccMsg(t('acceptance_generated', { count: (data.criteria || []).length, deterministic: data.deterministic || 0 }))
        setKanonRefresh((x) => x + 1)
      }
    } catch (e) {
      setAccMsg((e as Error).message)
    } finally {
      setAccBusy(false)
    }
  }

  // Сохранить блок (с подсхемой, если есть) как артефакт библиотеки —
  // переиспользование между проектами (галерея «Артефакты»)
  async function saveToLibrary() {
    if (!block || !project) return
    try {
      const res = await simaFetch('/artifacts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, blockId: block.id }),
      })
      const data = await res.json()
      setLibraryMsg(data.success === false ? (data.error || 'error') : t('saved_to_library'))
    } catch (e) {
      setLibraryMsg((e as Error).message)
    }
    setTimeout(() => setLibraryMsg(''), 4000)
  }

  async function generateSection(sectionName: string) {
    if (!project || !block) return
    setIsGenerating(true)
    
    try {
      const res = await simaFetch('/ai/generate-description', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          blockId: block.id,
          sections: [sectionName],
        }),
      })
      const data = await res.json()
      
      if (data.description && typeof data.description === 'object') {
        const updates = data.description as Partial<BlockData>
        // Обновляем локальный стор
        updateBlock(block.id, updates)
        // Сохраняем в БД
        try {
          await simaFetch('/blocks', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: block.id, ...updates }),
          })
        } catch (saveErr) {
          console.error('Failed to save generated section to DB:', saveErr)
        }
      } else {
        console.warn('[SIMA] generateSection: unexpected response format', data)
      }
    } catch (e) {
      console.error('Failed to generate description:', e)
    } finally {
      setIsGenerating(false)
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-sima-border">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <span
              className="text-xs font-bold px-2 py-1 rounded"
              style={{
                backgroundColor: `${block.color}30`,
                color: block.color,
              }}
            >
              {block.label}
            </span>
            <span className="text-xs text-sima-textDim">{block.type}</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={saveToLibrary}
              className="p-1 hover:bg-sima-surfaceLight rounded"
              title={t('save_to_library_title')}
            >
              <Package className="w-4 h-4 text-sima-textDim" />
            </button>
            <button
              onClick={() => setSelectedBlockId(null)}
              className="p-1 hover:bg-sima-surfaceLight rounded"
            >
              <X className="w-4 h-4 text-sima-textDim" />
            </button>
          </div>
        </div>
        {editingField === 'name' ? (
          <input
            autoFocus
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            onBlur={() => saveEdit('name')}
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveEdit('name')
              if (e.key === 'Escape') setEditingField(null)
            }}
            className="w-full font-semibold text-sima-text bg-sima-bg border border-sima-primary/50 rounded px-1.5 py-0.5 outline-none"
          />
        ) : (
          <h3
            className="font-semibold text-sima-text cursor-text hover:bg-sima-surfaceLight/50 rounded px-1 -mx-1"
            title={t('rename_hint')}
            onClick={() => startEdit('name', block.name)}
          >
            {block.name}
          </h3>
        )}
        {libraryMsg && (
          <p className="text-[10px] text-sima-textMuted mt-1">{libraryMsg}</p>
        )}

        {/* Статус блока + MVP-флаг (Sima Remix) */}
        <div className="mt-2 flex items-center gap-1 flex-wrap">
          {(['done', 'wip', 'q', 'idea'] as const).map((s) => {
            const active = (block.status || 'idea') === s
            return (
              <button
                key={s}
                onClick={() => saveBlockField('status', s)}
                title={t(`status_${s}`)}
                className={
                  'flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] transition-colors ' +
                  (active ? 'bg-sima-surfaceLight text-sima-text font-medium' : 'text-sima-textDim hover:bg-sima-surfaceLight/60')
                }
              >
                <span className="w-2 h-2 rounded-full" style={{ backgroundColor: BLOCK_STATUS_COLORS[s] }} />
                {t(`status_${s}`)}
              </button>
            )
          })}
          <button
            onClick={() => saveBlockField('isMvp', !block.isMvp)}
            className={
              'ml-auto px-1.5 py-0.5 rounded text-[10px] font-semibold transition-colors ' +
              (block.isMvp ? 'bg-sima-primary/15 text-sima-primary' : 'text-sima-textDim hover:bg-sima-surfaceLight/60')
            }
          >
            MVP
          </button>
        </div>

        {/* Progress bar */}
        <div className="mt-3 flex items-center gap-3">
          <div className="flex-1 h-2 bg-sima-bg rounded-full overflow-hidden">
            <div
              className="h-full bg-gradient-to-r from-sima-primary to-sima-accent rounded-full transition-all duration-500"
              style={{ width: `${progress}%` }}
            />
          </div>
          <span className="text-xs text-sima-textMuted font-medium">{progress}%</span>
        </div>

        {/* Кнопка стратегического анализа блока */}
        <button
          onClick={async () => {
            if (!project || !block || isAnalyzingBlock) return
            setIsAnalyzingBlock(true)
            try {
              const res = await simaFetch('/ai/strategist', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  projectId: project.id,
                  action: 'analyze-block',
                  blockId: block.id,
                }),
              })
              if (!res.ok) {
                const err = await res.json()
                throw new Error(err.error || t('err_analysis'))
              }
              const report = await res.json()
              setStrategistReport(report)
              setBottomPanelOpen(true)
              setBottomPanelTab('strategist')
            } catch (e) {
              console.error('Block analysis error:', e)
            } finally {
              setIsAnalyzingBlock(false)
            }
          }}
          disabled={isAnalyzingBlock}
          className="mt-2 w-full py-1.5 px-3 bg-purple-500/10 hover:bg-purple-500/20 text-purple-400 rounded-lg text-[11px] font-medium flex items-center justify-center gap-1.5 transition-colors disabled:opacity-50"
        >
          {isAnalyzingBlock ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : (
            <Brain className="w-3 h-3" />
          )}
          {isAnalyzingBlock ? t('analyzing_block') : t('strategic_analysis_button')}
        </button>

        {/* Кнопка подсхемы */}
        {(() => {
          const hasChildren = allBlocks.some(b => b.parentBlockId === block.id)
          return (
            <button
              onClick={() => drillDown(block.id)}
              className="mt-1.5 w-full py-1.5 px-3 bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-400 rounded-lg text-[11px] font-medium flex items-center justify-center gap-1.5 transition-colors"
            >
              {hasChildren ? (
                <>
                  <FolderOpen className="w-3 h-3" />
                  {t('enter_subschema', { count: allBlocks.filter(b => b.parentBlockId === block.id).length })}
                </>
              ) : (
                <>
                  <FolderPlus className="w-3 h-3" />
                  {t('create_subschema')}
                </>
              )}
            </button>
          )
        })()}

        {/* Kanon: готовность блока к реализации (понятным языком) */}
        {kanonAvailable && kanonRow && (
          <div className="mt-2 p-2 rounded-lg border border-sima-border bg-sima-bg/40 space-y-1.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] uppercase tracking-wide text-sima-textDim">{t('readiness_title')}</span>
              {kanonRow.ready_for_handoff ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-400/10 text-green-400">{t('ready_for_handoff')}</span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-400/10 text-amber-400">{t('not_ready')}</span>
              )}
            </div>
            <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[10.5px]">
              {([
                ['mission', t('kanon_row_mission')],
                ['acceptance', t('kanon_row_acceptance')],
                ['files', t('kanon_row_files')],
                ['tasks', t('kanon_row_tasks')],
              ] as const).map(([key, label]) => {
                const grade = kanonRow.rows?.[key] || 'missing'
                const mark = grade === 'ok' ? '✓' : grade === 'partial' ? '!' : '✗'
                const color = grade === 'ok' ? 'text-green-400' : grade === 'partial' ? 'text-amber-400' : 'text-red-400'
                return (
                  <span key={key} className="text-sima-textMuted">
                    <span className={cn('font-mono mr-1', color)}>{mark}</span>{label}
                  </span>
                )
              })}
            </div>
            <p className="text-[10px] text-sima-textDim">
              {t('deterministic_checks_count', { count: kanonRow.deterministic_checks || 0 })}
              {kanonRow.last_verdict && <> · {t('last_run', { verdict: kanonRow.last_verdict })}</>}
            </p>
            {!kanonRow.ready_for_handoff && (
              <p className="text-[10px] text-sima-textMuted">
                {kanonRow.rows?.mission !== 'ok'
                  ? t('fill_mission_hint')
                  : t('need_auto_check_hint')}
              </p>
            )}
            <button
              onClick={generateAcceptance}
              disabled={accBusy}
              className="w-full py-1.5 px-3 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 rounded-lg text-[11px] font-medium flex items-center justify-center gap-1.5 transition-colors disabled:opacity-50"
            >
              {accBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
              {accBusy ? t('formulating_checks') : t('generate_acceptance_button')}
            </button>
            {accMsg && <p className="text-[10px] text-sima-textMuted">{accMsg}</p>}
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="flex border-b border-sima-border">
        {tabs.map((tab) => {
          const Icon = tab.icon
          const sectionProg = sectionProgressMap[tab.id]
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={cn(
                'flex-1 py-2 px-1 text-xs font-medium transition-colors relative flex flex-col items-center gap-0.5',
                activeTab === tab.id
                  ? 'text-sima-primary border-b-2 border-sima-primary'
                  : 'text-sima-textDim hover:text-sima-textMuted'
              )}
            >
              <Icon className="w-3.5 h-3.5" />
              <span>{tab.label}</span>
              <span className="text-[10px] opacity-60">{sectionProg}%</span>
            </button>
          )
        })}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Generate all button */}
        <button
          onClick={() => generateSection(activeTab === 'why' ? 'business' : activeTab === 'what' ? 'functional' : activeTab === 'how' ? 'technical' : 'context')}
          disabled={isGenerating}
          className="w-full py-2 px-3 bg-sima-primary/10 hover:bg-sima-primary/20 text-sima-primary rounded-lg text-xs font-medium flex items-center justify-center gap-2 transition-colors disabled:opacity-50"
        >
          {isGenerating ? (
            <RefreshCw className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <Sparkles className="w-3.5 h-3.5" />
          )}
          {isGenerating ? t('generating') : t('fill_section_with_sima')}
        </button>

        {/* Fields */}
        {tabFields[activeTab].map((field) => {
          const value = block[field.key] as string | null | undefined
          const isFilled = !!value
          const isEditing = editingField === field.key

          return (
            <div
              key={field.key}
              className={cn(
                'rounded-lg border transition-colors',
                isFilled
                  ? 'border-sima-border bg-sima-surfaceLight/50'
                  : 'border-dashed border-sima-border/50 bg-sima-bg/50'
              )}
            >
              <div className="px-3 py-2 flex items-center justify-between">
                <span className="text-xs font-medium text-sima-textMuted">{field.label}</span>
                <div className="flex items-center gap-1">
                  {isFilled && (
                    <button
                      onClick={() => generateSection(activeTab === 'why' ? 'business' : activeTab)}
                      className="p-1 hover:bg-sima-border rounded transition-colors"
                      title={t("rewrite_with_sima_title")}
                    >
                      <RefreshCw className="w-3 h-3 text-sima-textDim" />
                    </button>
                  )}
                </div>
              </div>

              {isEditing ? (
                <div className="px-3 pb-3">
                  <textarea
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    className="w-full bg-sima-bg border border-sima-border rounded-lg px-3 py-2 text-sm text-sima-text resize-none min-h-[80px] focus:outline-none focus:border-sima-primary"
                    autoFocus
                  />
                  <div className="flex gap-2 mt-2">
                    <button
                      onClick={() => saveEdit(field.key)}
                      className="px-3 py-1 bg-sima-primary text-white text-xs rounded-lg"
                    >
                      {t('save_button')}
                    </button>
                    <button
                      onClick={() => setEditingField(null)}
                      className="px-3 py-1 bg-sima-border text-sima-textMuted text-xs rounded-lg"
                    >
                      {t('cancel_button')}
                    </button>
                  </div>
                </div>
              ) : isFilled ? (
                <div
                  className="px-3 pb-3 text-sm text-sima-text/90 whitespace-pre-wrap cursor-pointer hover:bg-sima-surfaceLight/30 rounded-b-lg transition-colors"
                  onClick={() => startEdit(field.key, value)}
                >
                  {value}
                </div>
              ) : (
                <div className="px-3 pb-3">
                  <button
                    onClick={() => startEdit(field.key, '')}
                    className="text-xs text-sima-textDim hover:text-sima-primary transition-colors"
                  >
                    {t('click_to_fill')}
                  </button>
                </div>
              )}
            </div>
          )
        })}

        {/* PM заметка */}
        <div className="rounded-lg border border-amber-400/30 bg-amber-400/5 p-3 mt-4">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-1.5">
              <StickyNote className="w-3 h-3 text-amber-400" />
              <span className="text-xs font-medium text-amber-400">{t('pm_note_label')}</span>
            </div>
            {pmNote && !pmNoteEditing && (
              <button
                onClick={() => setPmNoteEditing(true)}
                className="text-[10px] text-amber-400/60 hover:text-amber-400"
              >
                {t('edit_button')}
              </button>
            )}
          </div>
          {pmNoteEditing || !pmNote ? (
            <div>
              <textarea
                value={pmNote}
                onChange={(e) => setPmNote(e.target.value)}
                placeholder={t("pm_note_placeholder")}
                className="w-full bg-sima-bg border border-sima-border rounded-lg px-3 py-2 text-xs text-sima-text placeholder-sima-textDim resize-none min-h-[60px] focus:outline-none focus:border-amber-400/50"
                autoFocus={pmNoteEditing}
              />
              <div className="flex gap-2 mt-1.5">
                <button
                  onClick={() => savePmNote(pmNote)}
                  className="px-3 py-1 bg-amber-400/20 text-amber-400 text-[10px] rounded-lg hover:bg-amber-400/30"
                >
                  {t('save_button')}
                </button>
                {pmNote && (
                  <button
                    onClick={() => savePmNote('')}
                    className="px-3 py-1 text-sima-textDim text-[10px] hover:text-red-400"
                  >
                    {t('delete_button')}
                  </button>
                )}
              </div>
            </div>
          ) : (
            <p className="text-xs text-sima-text/80 whitespace-pre-wrap">{pmNote}</p>
          )}
        </div>

        {/* Привязанные инсайты из данных */}
        {blockInsights.length > 0 && (
          <div className="rounded-lg border border-blue-400/30 bg-blue-400/5 p-3 mt-4">
            <div className="flex items-center gap-1.5 mb-2">
              <Database className="w-3 h-3 text-blue-400" />
              <span className="text-xs font-medium text-blue-400">
                {t('insights_from_data', { count: blockInsights.length })}
              </span>
            </div>
            <div className="space-y-2">
              {blockInsights.map((insight) => (
                <div key={insight.id} className="bg-sima-bg/50 rounded p-2">
                  <div className="flex items-start justify-between gap-1">
                    <div className="flex-1 min-w-0">
                      <div className="text-[11px] font-medium text-sima-text">{insight.title}</div>
                      <div className="text-[10px] text-sima-textDim mt-0.5">{insight.content}</div>
                      {insight.evidence && (
                        <div className="text-[10px] text-sima-textDim/70 mt-1 border-l-2 border-blue-400/30 pl-1.5 italic">
                          &ldquo;{insight.evidence}&rdquo;
                        </div>
                      )}
                      <div className="text-[9px] text-sima-textDim mt-1 flex items-center gap-1.5">
                        <span className="flex items-center gap-0.5">
                          <Link2 className="w-2.5 h-2.5" />
                          {insight.dataSource.name}
                        </span>
                        <span>{Math.round(insight.confidence * 100)}%</span>
                      </div>
                    </div>
                    <button
                      onClick={() => handleUnlinkFromBlock(insight.id)}
                      className="p-0.5 text-sima-textDim hover:text-red-400 shrink-0"
                      title={t("unlink_title")}
                    >
                      <Unlink className="w-3 h-3" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Block narrative */}
        {block.blockNarrative && (
          <div className="rounded-lg border border-sima-primary/30 bg-sima-primary/5 p-3 mt-4">
            <div className="text-xs font-medium text-sima-primary mb-2">{t('role_in_product')}</div>
            <p className="text-sm text-sima-text/80">{block.blockNarrative}</p>
          </div>
        )}
      </div>
    </div>
  )
}
