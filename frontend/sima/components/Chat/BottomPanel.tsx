'use client'

import { useTranslations, useLocale } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import type { BottomPanelTab } from '@/sima/lib/store'
import { cn } from '@/sima/lib/utils'
import {
  MessageSquare,
  BookOpen,
  BarChart3,
  Brain,
  User,
  ExternalLink,
  ShieldCheck,
  TrendingUp,
  Globe,
  GitCompare,
  Database,
  Package,
} from 'lucide-react'
import React, { useMemo } from 'react'
import { calculateBlockProgress } from '@/sima/types'
import { analyzeSchema } from '@/sima/lib/consultant-service'
import ChatPanel from './ChatPanel'
import NarrativePanel from '@/sima/components/Narrative/NarrativePanel'
import StrategistPanel from '@/sima/components/Strategist/StrategistPanel'
import SimulatorPanel from '@/sima/components/Simulator/SimulatorPanel'
import ExportPanel from '@/sima/components/Export/ExportPanel'
import ConsultantPanel from '@/sima/components/Consultant/ConsultantPanel'
import EconomicsPanel from '@/sima/components/Economics/EconomicsPanel'
import ResearchPanel from '@/sima/components/Research/ResearchPanel'
import SnapshotDiff from '@/sima/components/Snapshots/SnapshotDiff'
import DataPanel from '@/sima/components/DataIngestion/DataPanel'
import { simaFetch } from '@/sima/lib/api'
import ArtifactsPanel from '@/sima/components/Artifacts/ArtifactsPanel'

const buildTabs = (t: (k: string) => string): { id: BottomPanelTab; label: string; icon: typeof MessageSquare }[] => [
  { id: 'chat', label: t('tab_chat'), icon: MessageSquare },
  { id: 'consultant', label: t('tab_consultant'), icon: ShieldCheck },
  { id: 'narrative', label: t('tab_narrative'), icon: BookOpen },
  { id: 'strategist', label: t('tab_strategist'), icon: Brain },
  { id: 'simulator', label: t('tab_simulator'), icon: User },
  { id: 'economics', label: t('tab_economics'), icon: TrendingUp },
  { id: 'research', label: t('tab_research'), icon: Globe },
  { id: 'data', label: t('tab_data'), icon: Database },
  { id: 'artifacts', label: t('tab_artifacts'), icon: Package },
  { id: 'export', label: t('tab_export'), icon: ExternalLink },
  { id: 'progress', label: t('tab_progress'), icon: BarChart3 },
  { id: 'diff', label: t('tab_diff'), icon: GitCompare },
]

export default function BottomPanel() {
  const t = useTranslations('sima_bottom_panel')
  const locale = useLocale()
  const tSvc = useTranslations('sima_consultant_svc')
  const tabs = buildTabs(t)
  const { bottomPanelTab, setBottomPanelTab, strategistReport, simulationReport, allBlocks, allConnections } = useSimaStore()

  // Мемоизированный анализ для badge на вкладке
  const consultantReport = useMemo(
    () => allBlocks.length > 0 ? analyzeSchema(allBlocks, allConnections, tSvc as any) : null,
    [allBlocks, allConnections, tSvc]
  )

  return (
    <div className="flex flex-col h-full bg-sima-surface">
      {/* Tabs */}
      <div className="flex border-b border-sima-border px-2 overflow-x-auto">
        {tabs.map((tab) => {
          const Icon = tab.icon
          const hasReport = tab.id === 'strategist' && strategistReport
          const hasSimReport = tab.id === 'simulator' && simulationReport
          const hasConsultant = tab.id === 'consultant' && consultantReport
          return (
            <button
              key={tab.id}
              onClick={() => setBottomPanelTab(tab.id)}
              className={cn(
                'px-3 py-2 text-xs font-medium flex items-center gap-1.5 transition-colors border-b-2 whitespace-nowrap shrink-0',
                bottomPanelTab === tab.id
                  ? 'text-sima-primary border-sima-primary'
                  : 'text-sima-textDim border-transparent hover:text-sima-textMuted'
              )}
            >
              <Icon className="w-3.5 h-3.5" />
              {tab.label}
              {hasReport && (
                <span className="text-[9px] ml-0.5">{strategistReport!.verdict.emoji}</span>
              )}
              {hasSimReport && (
                <span className="text-[9px] ml-0.5">{simulationReport!.summary.overallVerdict}</span>
              )}
              {hasConsultant && (
                <span className="text-[9px] ml-0.5">{consultantReport!.emoji}</span>
              )}
            </button>
          )
        })}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        {bottomPanelTab === 'chat' && <ChatPanel />}
        {bottomPanelTab === 'consultant' && <ConsultantPanel />}
        {bottomPanelTab === 'narrative' && <NarrativePanel />}
        {bottomPanelTab === 'strategist' && <StrategistPanel />}
        {bottomPanelTab === 'simulator' && <SimulatorPanel />}
        {bottomPanelTab === 'economics' && <EconomicsPanel />}
        {bottomPanelTab === 'research' && <ResearchPanel />}
        {bottomPanelTab === 'data' && <DataPanel />}
        {bottomPanelTab === 'artifacts' && <ArtifactsPanel />}
        {bottomPanelTab === 'export' && <ExportPanel />}
        {bottomPanelTab === 'progress' && <ProgressPanel />}
        {bottomPanelTab === 'diff' && <SnapshotDiff onClose={() => setBottomPanelTab('chat')} />}
      </div>
    </div>
  )
}

type BlockStatus = 'todo' | 'in_progress' | 'review' | 'done'
const buildStatusConfig = (t: (k: string) => string): Record<BlockStatus, { label: string; color: string; bg: string }> => ({
  todo: { label: t('status_todo'), color: 'text-sima-textDim', bg: 'bg-sima-bg' },
  in_progress: { label: t('status_in_progress'), color: 'text-blue-400', bg: 'bg-blue-400/10' },
  review: { label: t('status_review'), color: 'text-amber-400', bg: 'bg-amber-400/10' },
  done: { label: t('status_done'), color: 'text-green-400', bg: 'bg-green-400/10' },
})

function ProductMapGaps() {
  // Карта продукта (Sima Remix, слой 2): заполненность полей ПРОЕКТА с
  // gap-индикаторами ✓/!. «Сима, помоги» реально заполняет нарративные
  // поля из схемы (/ai/generate-narrative) и сохраняет в проект.
  const t = useTranslations('sima_bottom_panel')
  const { project, setProject } = useSimaStore()
  const [filling, setFilling] = React.useState(false)
  const [fillMsg, setFillMsg] = React.useState('')
  if (!project) return null
  const p = project as any
  const fields: { key: string; label: string; fillable: boolean }[] = [
    { key: 'goal', label: t('map_goal'), fillable: false },
    { key: 'targetAudience', label: t('map_audience'), fillable: false },
    { key: 'mvpDescription', label: t('map_mvp'), fillable: false },
    { key: 'narrativeProblem', label: t('map_problem'), fillable: true },
    { key: 'narrativeSolution', label: t('map_solution'), fillable: true },
    { key: 'narrativeHowItWorks', label: t('map_how'), fillable: true },
    { key: 'generatedTZ', label: t('map_tz'), fillable: false },
  ]
  const gaps = fields.filter(f => !String(p[f.key] || '').trim())
  const fillableGaps = gaps.filter(f => f.fillable)

  async function simaFill() {
    setFilling(true)
    setFillMsg('')
    try {
      const res = await simaFetch('/ai/generate-narrative', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project!.id }),
      })
      const data = await res.json()
      const patch: Record<string, string> = {}
      if (data.problem) patch.narrativeProblem = data.problem
      if (data.solution) patch.narrativeSolution = data.solution
      if (data.howItWorks) patch.narrativeHowItWorks = data.howItWorks
      if (Object.keys(patch).length) {
        await simaFetch(`/projects/${project!.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch),
        })
        setProject({ ...(project as any), ...patch })
        setFillMsg(t('map_filled', { count: Object.keys(patch).length }))
      } else {
        setFillMsg(t('map_fill_empty'))
      }
    } catch (e) {
      setFillMsg((e as Error).message)
    } finally {
      setFilling(false)
    }
  }

  return (
    <div className="mb-4 p-3 rounded-xl border border-sima-border bg-sima-surfaceLight">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-xs font-semibold text-sima-text">{t('map_heading')}</span>
        <span className={gaps.length === 0 ? 'text-[10px] text-green-400' : 'text-[10px] text-amber-400'}>
          {gaps.length === 0 ? t('map_complete') : t('map_gaps', { count: gaps.length })}
        </span>
        {fillableGaps.length > 0 && (
          <button
            onClick={simaFill}
            disabled={filling}
            className="ml-auto px-2 py-1 rounded-lg border border-sima-primary/40 bg-sima-primary/10 hover:bg-sima-primary/20 text-[10px] text-sima-primary transition-colors"
          >
            {filling ? t('map_filling') : t('map_fill_button')}
          </button>
        )}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {fields.map(f => {
          const filled = !!String(p[f.key] || '').trim()
          return (
            <span
              key={f.key}
              className={
                'text-[10px] px-1.5 py-0.5 rounded border ' +
                (filled
                  ? 'border-green-400/30 text-green-400 bg-green-400/5'
                  : 'border-amber-400/40 text-amber-400 bg-amber-400/10')
              }
              title={filled ? '' : t('map_gap_hint')}
            >
              {filled ? '✓' : '!'} {f.label}
            </span>
          )
        })}
      </div>
      {fillMsg && <p className="text-[10px] text-sima-textMuted mt-2">{fillMsg}</p>}
    </div>
  )
}

export function ProgressPanel() {
  const t = useTranslations('sima_bottom_panel')
  const locale = useLocale()
  const statusConfig = buildStatusConfig(t)
  const { blocks, project } = useSimaStore()

  // Статусы и дедлайны из localStorage
  const storageKey = project ? `sima_progress_${project.id}` : ''

  const [tracker, setTracker] = React.useState<Record<string, { status: BlockStatus; deadline?: string }>>({})
  const [editingDeadline, setEditingDeadline] = React.useState<string | null>(null)

  React.useEffect(() => {
    if (!storageKey) return
    try {
      const saved = localStorage.getItem(storageKey)
      if (saved) setTracker(JSON.parse(saved))
    } catch {}
  }, [storageKey])

  const updateTracker = (blockId: string, data: { status?: BlockStatus; deadline?: string }) => {
    setTracker(prev => {
      const next = { ...prev, [blockId]: { ...prev[blockId], ...data } as { status: BlockStatus; deadline?: string } }
      if (storageKey) localStorage.setItem(storageKey, JSON.stringify(next))
      return next
    })
  }

  const totalBlocks = blocks.length
  const doneCount = blocks.filter(b => tracker[b.id]?.status === 'done').length
  const overallProgress = totalBlocks > 0 ? Math.round((doneCount / totalBlocks) * 100) : 0

  // Фильтрация по просроченным
  const now = new Date()
  const overdueBlocks = blocks.filter(b => {
    const entry = tracker[b.id]
    if (!entry?.deadline || entry.status === 'done') return false
    return new Date(entry.deadline) < now
  })

  return (
    <div className="p-4 overflow-y-auto h-full">
      {/* Сводка */}
      <div className="flex items-center gap-4 mb-4">
        <div className="flex-1">
          <div className="flex items-center justify-between mb-1.5">
            <h3 className="text-sm font-medium text-sima-text">{t('progress_heading')}</h3>
            <span className="text-xs font-bold text-sima-primary">{overallProgress}%</span>
          </div>
          <div className="w-full h-2 bg-sima-bg rounded-full overflow-hidden">
            <div className="h-full bg-sima-primary rounded-full transition-all" style={{ width: `${overallProgress}%` }} />
          </div>
          <div className="flex items-center gap-3 mt-2 text-[10px] text-sima-textDim">
            <span>{t('blocks_done', { done: doneCount, total: totalBlocks })}</span>
            {overdueBlocks.length > 0 && (
              <span className="text-red-400 font-medium">{t('overdue_count', { count: overdueBlocks.length })}</span>
            )}
          </div>
        </div>
      </div>

      <ProductMapGaps />

      {blocks.length === 0 ? (
        <p className="text-sm text-sima-textDim">
          {t('describe_first_hint')}
        </p>
      ) : (
        <div className="space-y-2">
          {blocks.map((block) => {
            const progress = calculateBlockProgress(block)
            const trackerEntry = tracker[block.id] || { status: 'todo' as BlockStatus }
            const statusCfg = statusConfig[trackerEntry.status || 'todo']
            const isOverdue = trackerEntry.deadline && trackerEntry.status !== 'done' && new Date(trackerEntry.deadline) < now

            return (
              <div key={block.id} className={cn(
                'flex items-center gap-2 px-3 py-2 rounded-lg border transition-colors',
                isOverdue ? 'border-red-400/30 bg-red-400/5' : 'border-sima-border bg-sima-bg/50'
              )}>
                <span
                  className="text-[10px] font-bold px-1.5 py-0.5 rounded shrink-0"
                  style={{ backgroundColor: `${block.color}30`, color: block.color }}
                >
                  {block.label}
                </span>
                <span className="text-xs text-sima-textMuted truncate flex-1 min-w-0">
                  {block.name}
                </span>

                {/* Status selector */}
                <select
                  value={trackerEntry.status || 'todo'}
                  onChange={(e) => updateTracker(block.id, { status: e.target.value as BlockStatus })}
                  className={cn('text-[10px] px-1.5 py-0.5 rounded border-none outline-none cursor-pointer', statusCfg.bg, statusCfg.color)}
                >
                  {Object.entries(statusConfig).map(([key, cfg]) => (
                    <option key={key} value={key}>{cfg.label}</option>
                  ))}
                </select>

                {/* Deadline */}
                {editingDeadline === block.id ? (
                  <input
                    type="date"
                    value={trackerEntry.deadline || ''}
                    onChange={(e) => { updateTracker(block.id, { deadline: e.target.value }); setEditingDeadline(null) }}
                    onBlur={() => setEditingDeadline(null)}
                    className="text-[10px] bg-sima-bg border border-sima-border rounded px-1 py-0.5 text-sima-text w-24"
                    autoFocus
                  />
                ) : (
                  <button
                    onClick={() => setEditingDeadline(block.id)}
                    className={cn(
                      'text-[10px] px-1 py-0.5 rounded transition-colors',
                      trackerEntry.deadline ? (isOverdue ? 'text-red-400' : 'text-sima-textMuted') : 'text-sima-textDim hover:text-sima-textMuted'
                    )}
                  >
                    {trackerEntry.deadline ? new Date(trackerEntry.deadline).toLocaleDateString(locale, { day: 'numeric', month: 'short' }) : t('deadline_fallback')}
                  </button>
                )}

                {/* Fill progress */}
                <div className="w-16 h-1 bg-sima-bg rounded-full overflow-hidden shrink-0">
                  <div
                    className="h-full rounded-full transition-all"
                    style={{ width: `${progress}%`, backgroundColor: block.color }}
                  />
                </div>
                <span className="text-[10px] text-sima-textDim w-6 text-right shrink-0">{progress}%</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
