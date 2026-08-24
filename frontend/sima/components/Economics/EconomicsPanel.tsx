'use client'

import { useState } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import type { EconomicsReport, BlockEconomics } from '@/sima/lib/economics-service'
import { Loader2, TrendingUp, Zap, Target, AlertTriangle, RefreshCw, Copy, CheckCircle } from 'lucide-react'
import { cn } from '@/sima/lib/utils'
import ReactMarkdown from 'react-markdown'
import { simaFetch } from '@/sima/lib/api'

const buildQuadrantConfig = (t: (k: string) => string) => ({
  quick_win: { label: 'Quick Win', color: 'text-emerald-400', bg: 'bg-emerald-400/10', border: 'border-emerald-400/30', icon: Zap },
  strategic: { label: t('quadrant_strategic'), color: 'text-blue-400', bg: 'bg-blue-400/10', border: 'border-blue-400/30', icon: Target },
  fill_in: { label: t('quadrant_fill_in'), color: 'text-sima-textDim', bg: 'bg-sima-bg', border: 'border-sima-border', icon: TrendingUp },
  avoid: { label: t('quadrant_avoid'), color: 'text-red-400', bg: 'bg-red-400/10', border: 'border-red-400/30', icon: AlertTriangle },
})

export default function EconomicsPanel() {
  const t = useTranslations('sima_economics')
  const quadrantConfig = buildQuadrantConfig(t)
  const { project, allBlocks, setSelectedBlockId, setRightPanelOpen, economicsReport: report, setEconomicsReport: setReport } = useSimaStore()
  const [loading, setLoading] = useState(false)
  const [view, setView] = useState<'matrix' | 'list' | 'summary'>('matrix')
  const [copied, setCopied] = useState(false)

  async function runAnalysis() {
    if (!project) return
    setLoading(true)
    try {
      const res = await simaFetch('/ai/economics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id }),
      })
      const data = await res.json()
      if (data.error) {
        alert(data.error)
      } else {
        setReport(data)
      }
    } catch (e) {
      console.error('Economics error:', e)
    } finally {
      setLoading(false)
    }
  }

  function handleBlockClick(blockId: string) {
    setSelectedBlockId(blockId)
    setRightPanelOpen(true)
  }

  if (!project) return null

  // Ещё нет отчёта
  if (!report && !loading) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 p-4">
        <TrendingUp className="w-10 h-10 text-sima-textDim opacity-30" />
        <p className="text-sm text-sima-textDim text-center">
          {t('subtitle')}
        </p>
        <button
          onClick={runAnalysis}
          disabled={allBlocks.length === 0}
          className="px-4 py-2 bg-sima-primary hover:bg-sima-primaryHover text-white rounded-lg text-sm font-medium flex items-center gap-2 transition-colors disabled:opacity-50"
        >
          <TrendingUp className="w-4 h-4" />
          {t('analyze_button')}
        </button>
        {allBlocks.length === 0 && (
          <p className="text-[10px] text-sima-textDim">{t('need_blocks_hint')}</p>
        )}
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full gap-2">
        <Loader2 className="w-5 h-5 animate-spin text-sima-primary" />
        <span className="text-sm text-sima-textDim">{t('analyzing')}</span>
      </div>
    )
  }

  if (!report) return null

  const matrixBlocks = {
    quick_win: report.blocks.filter(b => b.quadrant === 'quick_win'),
    strategic: report.blocks.filter(b => b.quadrant === 'strategic'),
    fill_in: report.blocks.filter(b => b.quadrant === 'fill_in'),
    avoid: report.blocks.filter(b => b.quadrant === 'avoid'),
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="shrink-0 px-4 py-2 border-b border-sima-border flex items-center gap-2">
        <div className="flex gap-1">
          {(['matrix', 'list', 'summary'] as const).map(v => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={cn(
                'px-2 py-1 text-[10px] rounded transition-colors',
                view === v ? 'bg-sima-primary/20 text-sima-primary font-medium' : 'text-sima-textDim hover:text-sima-textMuted'
              )}
            >
              {v === 'matrix' ? t('view_matrix') : v === 'list' ? t('view_list') : t('view_summary')}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        <span className="text-[9px] text-sima-textDim">{t('analysis_seconds', { seconds: report.analysisTime })}</span>
        <button
          onClick={runAnalysis}
          className="px-2 py-1 bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted rounded text-[10px] flex items-center gap-1 transition-colors"
        >
          <RefreshCw className="w-2.5 h-2.5" />
          {t('refresh_button')}
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-3">
        {view === 'matrix' && (
          <div className="grid grid-cols-2 gap-2 h-full">
            {/* Quick Wins */}
            <MatrixQuadrant
              title="Quick Wins"
              subtitle={t("subtitle_high_low")}
              blocks={matrixBlocks.quick_win}
              quadrant="quick_win"
              onBlockClick={handleBlockClick}
            />
            {/* Strategic */}
            <MatrixQuadrant
              title={t("card_strategic_title")}
              subtitle={t("subtitle_high_high")}
              blocks={matrixBlocks.strategic}
              quadrant="strategic"
              onBlockClick={handleBlockClick}
            />
            {/* Fill-in */}
            <MatrixQuadrant
              title={t("card_fillin_title")}
              subtitle={t("subtitle_low_low")}
              blocks={matrixBlocks.fill_in}
              quadrant="fill_in"
              onBlockClick={handleBlockClick}
            />
            {/* Avoid */}
            <MatrixQuadrant
              title={t("card_avoid_title")}
              subtitle={t("subtitle_low_high")}
              blocks={matrixBlocks.avoid}
              quadrant="avoid"
              onBlockClick={handleBlockClick}
            />
          </div>
        )}

        {view === 'list' && (
          <div className="space-y-2">
            {report.blocks.map((block) => {
              const qConf = quadrantConfig[block.quadrant]
              const QIcon = qConf.icon
              return (
                <div
                  key={block.blockId}
                  onClick={() => handleBlockClick(block.blockId)}
                  className="flex items-center gap-3 p-2.5 bg-sima-bg border border-sima-border rounded-lg cursor-pointer hover:border-sima-primary/40 transition-colors"
                >
                  <span className="text-sm font-bold text-sima-primary w-6 text-center">#{block.priority}</span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold text-sima-text">{block.blockLabel}: {block.blockName}</span>
                      <span className={cn('text-[9px] px-1.5 py-0.5 rounded-full flex items-center gap-0.5', qConf.bg, qConf.color, 'border', qConf.border)}>
                        <QIcon className="w-2.5 h-2.5" />
                        {qConf.label}
                      </span>
                    </div>
                    <p className="text-[10px] text-sima-textDim mt-0.5 truncate">{block.recommendation}</p>
                  </div>
                  <div className="flex gap-3 shrink-0">
                    <div className="text-center">
                      <div className="text-[9px] text-sima-textDim">{t('value_label')}</div>
                      <div className={cn('text-sm font-bold', block.value >= 7 ? 'text-emerald-400' : block.value >= 4 ? 'text-amber-400' : 'text-red-400')}>
                        {block.value}
                      </div>
                    </div>
                    <div className="text-center">
                      <div className="text-[9px] text-sima-textDim">{t('complexity_label')}</div>
                      <div className={cn('text-sm font-bold', block.complexity <= 3 ? 'text-emerald-400' : block.complexity <= 6 ? 'text-amber-400' : 'text-red-400')}>
                        {block.complexity}
                      </div>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {view === 'summary' && (
          <div className="space-y-4">
            {/* Overall advice */}
            <div className="bg-sima-bg border border-sima-border rounded-lg p-3">
              <h4 className="text-xs font-semibold text-sima-text mb-1">{t('general_advice')}</h4>
              <div className="prose prose-invert prose-xs max-w-none">
                <ReactMarkdown>{report.overallAdvice}</ReactMarkdown>
              </div>
            </div>

            {/* Total complexity */}
            <div className="bg-sima-bg border border-sima-border rounded-lg p-3">
              <h4 className="text-xs font-semibold text-sima-text mb-1">{t('total_complexity')}</h4>
              <p className="text-[11px] text-sima-textMuted">{report.summary.totalComplexity}</p>
            </div>

            {/* MVP recommendation */}
            {report.summary.mvpRecommendation.length > 0 && (
              <div className="bg-emerald-400/5 border border-emerald-400/20 rounded-lg p-3">
                <h4 className="text-xs font-semibold text-emerald-400 mb-1">{t('mvp_recommendation')}</h4>
                <div className="flex flex-wrap gap-1">
                  {report.summary.mvpRecommendation.map(label => (
                    <span key={label} className="text-[10px] px-2 py-0.5 bg-emerald-400/20 text-emerald-400 rounded-full">{label}</span>
                  ))}
                </div>
              </div>
            )}

            {/* Quick Wins */}
            {report.summary.quickWins.length > 0 && (
              <div className="bg-cyan-400/5 border border-cyan-400/20 rounded-lg p-3">
                <h4 className="text-xs font-semibold text-cyan-400 mb-1 flex items-center gap-1"><Zap className="w-3 h-3" /> Quick Wins</h4>
                <div className="flex flex-wrap gap-1">
                  {report.summary.quickWins.map(label => (
                    <span key={label} className="text-[10px] px-2 py-0.5 bg-cyan-400/20 text-cyan-400 rounded-full">{label}</span>
                  ))}
                </div>
              </div>
            )}

            {/* Avoid */}
            {report.summary.avoidOrDefer.length > 0 && (
              <div className="bg-red-400/5 border border-red-400/20 rounded-lg p-3">
                <h4 className="text-xs font-semibold text-red-400 mb-1">{t('avoid_recommendation')}</h4>
                <div className="flex flex-wrap gap-1">
                  {report.summary.avoidOrDefer.map(label => (
                    <span key={label} className="text-[10px] px-2 py-0.5 bg-red-400/20 text-red-400 rounded-full">{label}</span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function MatrixQuadrant({ title, subtitle, blocks, quadrant, onBlockClick }: {
  title: string
  subtitle: string
  blocks: BlockEconomics[]
  quadrant: BlockEconomics['quadrant']
  onBlockClick: (id: string) => void
}) {
  const t = useTranslations('sima_economics')
  const quadrantConfig = buildQuadrantConfig(t)
  const conf = quadrantConfig[quadrant]
  return (
    <div className={cn('rounded-lg border p-2 flex flex-col', conf.bg, conf.border)}>
      <div className="mb-1.5">
        <h4 className={cn('text-[11px] font-semibold', conf.color)}>{title}</h4>
        <p className="text-[9px] text-sima-textDim">{subtitle}</p>
      </div>
      <div className="flex-1 space-y-1 overflow-y-auto">
        {blocks.length === 0 && (
          <p className="text-[9px] text-sima-textDim italic">{t('no_blocks')}</p>
        )}
        {blocks.map(b => (
          <button
            key={b.blockId}
            onClick={() => onBlockClick(b.blockId)}
            className="w-full text-left px-2 py-1 bg-sima-surface/50 rounded hover:bg-sima-surface transition-colors"
          >
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-medium text-sima-text">{b.blockLabel}: {b.blockName}</span>
              <span className="text-[9px] text-sima-textDim">{b.value}/{b.complexity}</span>
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}
