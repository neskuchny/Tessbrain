'use client'

import { useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { analyzeSchema, type ConsultantIssue, type IssueSeverity, type IssueCategory } from '@/sima/lib/consultant-service'
import {
  AlertTriangle,
  AlertCircle,
  Info,
  Lightbulb,
  ChevronDown,
  ChevronRight,
  Crosshair,
  Layers,
  Link2,
  Puzzle,
  CheckCircle2,
  Sparkles,
  FileWarning,
  Gauge,
} from 'lucide-react'
import { cn } from '@/sima/lib/utils'

const buildSeverityConfig = (t: (k: string) => string): Record<IssueSeverity, { icon: typeof AlertCircle; color: string; label: string }> => ({
  error: { icon: AlertCircle, color: 'text-red-400', label: t('severity_error') },
  warning: { icon: AlertTriangle, color: 'text-amber-400', label: t('severity_warning') },
  info: { icon: Info, color: 'text-blue-400', label: t('severity_info') },
  tip: { icon: Lightbulb, color: 'text-emerald-400', label: t('severity_tip') },
})

const buildCategoryConfig = (t: (k: string) => string): Record<IssueCategory, { icon: typeof Puzzle; label: string }> => ({
  completeness: { icon: FileWarning, label: t('category_completeness') },
  structure: { icon: Puzzle, label: t('category_structure') },
  quality: { icon: Sparkles, label: t('category_quality') },
  architecture: { icon: Layers, label: t('category_architecture') },
  connections: { icon: Link2, label: t('category_connections') },
  layers: { icon: Layers, label: t('category_layers') },
  best_practice: { icon: Lightbulb, label: t('category_best_practice') },
})

type FilterMode = 'all' | IssueSeverity

export default function ConsultantPanel() {
  const t = useTranslations('sima_consultant')
  const tSvc = useTranslations('sima_consultant_svc')
  const severityConfig = buildSeverityConfig(t)
  const categoryConfig = buildCategoryConfig(t)
  const { allBlocks, allConnections, setSelectedBlockId, setSelectedConnectionId, setRightPanelOpen, setBottomPanelTab } = useSimaStore()
  const [filter, setFilter] = useState<FilterMode>('all')
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set(['completeness', 'structure', 'architecture', 'layers', 'best_practice']))

  // Анализ в реальном времени (useMemo — пересчёт при изменении блоков/связей)
  const report = useMemo(
    () => analyzeSchema(allBlocks, allConnections, tSvc as any),
    [allBlocks, allConnections]
  )

  // Фильтрация
  const filteredIssues = filter === 'all' ? report.issues : report.issues.filter(i => i.severity === filter)

  // Группировка по категории
  const groupedIssues = useMemo(() => {
    const groups = new Map<IssueCategory, ConsultantIssue[]>()
    for (const issue of filteredIssues) {
      if (!groups.has(issue.category)) groups.set(issue.category, [])
      groups.get(issue.category)!.push(issue)
    }
    return groups
  }, [filteredIssues])

  const toggleCategory = (cat: string) => {
    setExpandedCategories(prev => {
      const next = new Set(prev)
      if (next.has(cat)) next.delete(cat)
      else next.add(cat)
      return next
    })
  }

  const handleIssueClick = (issue: ConsultantIssue) => {
    if (issue.blockId) {
      setSelectedBlockId(issue.blockId)
      setRightPanelOpen(true)
    } else if (issue.connectionId) {
      setSelectedConnectionId(issue.connectionId)
      setRightPanelOpen(true)
    }
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header: Score + Stats */}
      <div className="shrink-0 px-4 py-3 border-b border-sima-border">
        <div className="flex items-center gap-3">
          {/* Score circle */}
          <div className="relative w-14 h-14 shrink-0">
            <svg viewBox="0 0 36 36" className="w-14 h-14 -rotate-90">
              <path
                d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                fill="none"
                stroke="rgb(var(--sima-surface-light))"
                strokeWidth="3"
              />
              <path
                d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                fill="none"
                stroke={report.score >= 70 ? '#34D399' : report.score >= 40 ? '#FBBF24' : '#F87171'}
                strokeWidth="3"
                strokeDasharray={`${report.score}, 100`}
                strokeLinecap="round"
              />
            </svg>
            <span className="absolute inset-0 flex items-center justify-center text-sm font-bold text-sima-text">
              {report.score}
            </span>
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="text-sm font-semibold text-sima-text">{report.emoji} {t('quality_heading')}</span>
            </div>
            <p className="text-[11px] text-sima-textDim mt-0.5 truncate">{report.oneLiner}</p>
            <div className="flex gap-3 mt-1.5">
              <span className="text-[10px] text-sima-textDim flex items-center gap-1">
                <Puzzle className="w-3 h-3" /> {t('blocks_count_label', { count: report.stats.totalBlocks })}
              </span>
              <span className="text-[10px] text-sima-textDim flex items-center gap-1">
                <Link2 className="w-3 h-3" /> {t('connections_count', { count: report.stats.totalConnections })}
              </span>
              <span className="text-[10px] text-sima-textDim flex items-center gap-1">
                <Gauge className="w-3 h-3" /> {t('fill_percent', { percent: report.stats.avgBlockProgress })}
              </span>
            </div>
          </div>
        </div>

        {/* Filter tabs */}
        <div className="flex gap-1 mt-3">
          <FilterButton active={filter === 'all'} onClick={() => setFilter('all')} label={t('filter_all', { count: report.issues.length })} />
          {report.stats.issuesBySeverity.error > 0 && (
            <FilterButton active={filter === 'error'} onClick={() => setFilter('error')} label={`🔴 ${report.stats.issuesBySeverity.error}`} color="text-red-400" />
          )}
          {report.stats.issuesBySeverity.warning > 0 && (
            <FilterButton active={filter === 'warning'} onClick={() => setFilter('warning')} label={`🟡 ${report.stats.issuesBySeverity.warning}`} color="text-amber-400" />
          )}
          {report.stats.issuesBySeverity.info > 0 && (
            <FilterButton active={filter === 'info'} onClick={() => setFilter('info')} label={`🔵 ${report.stats.issuesBySeverity.info}`} color="text-blue-400" />
          )}
          {report.stats.issuesBySeverity.tip > 0 && (
            <FilterButton active={filter === 'tip'} onClick={() => setFilter('tip')} label={`💡 ${report.stats.issuesBySeverity.tip}`} color="text-emerald-400" />
          )}
        </div>
      </div>

      {/* Issues list */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-1">
        {report.issues.length === 0 && allBlocks.length > 0 && (
          <div className="flex flex-col items-center justify-center py-8 gap-2">
            <CheckCircle2 className="w-8 h-8 text-emerald-400" />
            <p className="text-sm text-emerald-400 font-medium">{t('all_good')}</p>
            <p className="text-[11px] text-sima-textDim">{t('no_issues_hint')}</p>
          </div>
        )}

        {allBlocks.length === 0 && (
          <div className="flex flex-col items-center justify-center py-8 gap-2">
            <Crosshair className="w-8 h-8 text-sima-textDim" />
            <p className="text-sm text-sima-textDim">{t('empty_schema')}</p>
            <p className="text-[11px] text-sima-textDim">{t('add_blocks_hint')}</p>
          </div>
        )}

        {filteredIssues.length === 0 && report.issues.length > 0 && allBlocks.length > 0 && (
          <p className="text-xs text-sima-textDim text-center py-4">
            {t('no_issues_for_filter')}
          </p>
        )}

        {/* Grouped by category */}
        {Array.from(groupedIssues.entries()).map(([category, issues]) => {
          const catConf = categoryConfig[category]
          const CatIcon = catConf.icon
          const isExpanded = expandedCategories.has(category)

          return (
            <div key={category} className="rounded-lg border border-sima-border/50 overflow-hidden">
              <button
                onClick={() => toggleCategory(category)}
                className="w-full flex items-center gap-2 px-3 py-2 bg-sima-bg/50 hover:bg-sima-bg transition-colors"
              >
                {isExpanded ? <ChevronDown className="w-3.5 h-3.5 text-sima-textDim" /> : <ChevronRight className="w-3.5 h-3.5 text-sima-textDim" />}
                <CatIcon className="w-3.5 h-3.5 text-sima-textMuted" />
                <span className="text-xs font-medium text-sima-textMuted flex-1 text-left">{catConf.label}</span>
                <span className="text-[10px] text-sima-textDim bg-sima-surface px-1.5 py-0.5 rounded">{issues.length}</span>
              </button>

              {isExpanded && (
                <div className="divide-y divide-sima-border/30">
                  {issues.map(issue => {
                    const sevConf = severityConfig[issue.severity]
                    const SevIcon = sevConf.icon

                    return (
                      <div
                        key={issue.id}
                        onClick={() => handleIssueClick(issue)}
                        className={cn(
                          'px-3 py-2 transition-colors',
                          (issue.blockId || issue.connectionId) ? 'cursor-pointer hover:bg-sima-surfaceLight' : ''
                        )}
                      >
                        <div className="flex items-start gap-2">
                          <SevIcon className={cn('w-3.5 h-3.5 mt-0.5 shrink-0', sevConf.color)} />
                          <div className="flex-1 min-w-0">
                            <p className="text-xs font-medium text-sima-text">{issue.title}</p>
                            <p className="text-[10px] text-sima-textDim mt-0.5 leading-relaxed">{issue.description}</p>
                            {issue.suggestion && (
                              <p className="text-[10px] text-sima-primary/80 mt-1 flex items-center gap-1">
                                <Lightbulb className="w-2.5 h-2.5 shrink-0" />
                                {issue.suggestion}
                              </p>
                            )}
                          </div>
                          {(issue.blockId || issue.connectionId) && (
                            <Crosshair className="w-3 h-3 text-sima-textDim shrink-0 mt-0.5" />
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function FilterButton({ active, onClick, label, color }: { active: boolean; onClick: () => void; label: string; color?: string }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'px-2 py-1 text-[10px] rounded transition-colors',
        active
          ? 'bg-sima-primary/20 text-sima-primary font-medium'
          : cn('text-sima-textDim hover:text-sima-textMuted', color)
      )}
    >
      {label}
    </button>
  )
}
