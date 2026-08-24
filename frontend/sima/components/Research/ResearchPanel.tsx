'use client'

import { useState } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import type { ResearchReport, Competitor, MarketTrend } from '@/sima/lib/research-service'
import { Loader2, Globe, TrendingUp, Users, Shield, Target, Zap, Download, RefreshCw, ChevronDown, ChevronRight, ExternalLink } from 'lucide-react'
import { cn } from '@/sima/lib/utils'
import ReactMarkdown from 'react-markdown'
import { simaFetch } from '@/sima/lib/api'

type ResearchView = 'overview' | 'competitors' | 'trends' | 'strategy'

export default function ResearchPanel() {
  const t = useTranslations('sima_research')
  const { project, allBlocks } = useSimaStore()
  const [report, setReport] = useState<ResearchReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [view, setView] = useState<ResearchView>('overview')

  async function runResearch() {
    if (!project) return
    setLoading(true)
    try {
      const res = await simaFetch('/ai/research', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id }),
      })
      const data = await res.json()
      setReport(data)
    } catch (e) {
      console.error('Research error:', e)
    } finally {
      setLoading(false)
    }
  }

  function exportAsMarkdown() {
    if (!report) return
    const lines = [
      t('report_title', { name: project?.name ?? '' }) + '\n',
      t('market_overview_heading', { text: report.marketOverview }) + '\n',
      t('market_size_label', { value: report.marketSize }) + '\n',
      t('target_segments_label', { value: report.targetSegments.join(', ') }) + '\n',
      t('competitors_heading') + '\n',
      ...report.competitors.map(c => [
        `### ${c.name}`,
        c.description,
        t('strengths_md', { value: c.strengths.join(', ') }),
        t('weaknesses_md', { value: c.weaknesses.join(', ') }),
        t('pricing_md', { value: c.pricing }),
        c.url ? t('website_link', { url: c.url }) : '',
        ''
      ].join('\n')),
      t('trends_heading') + '\n',
      ...report.trends.map(t => `- **${t.trend}** (${t.relevance}): ${t.description} → ${t.opportunity}`),
      '\n' + t('opportunities_md_heading') + '\n',
      ...report.opportunities.map(o => `- ${o}`),
      '\n' + t('threats_md_heading') + '\n',
      ...report.threats.map(t => `- ${t}`),
      '\n' + t('positioning_md_heading', { text: report.positioning }),
      '\n' + t('differentiators_md_heading') + '\n',
      ...report.differentiators.map(d => `- ${d}`),
      '\n' + t('go_to_market_heading', { text: report.goToMarket }),
    ]
    const blob = new Blob([lines.join('\n')], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `research_${project?.name}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  if (!project) return null

  if (!report && !loading) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 p-4">
        <Globe className="w-10 h-10 text-sima-textDim opacity-30" />
        <p className="text-sm text-sima-textDim text-center">
          {t('subtitle')}
        </p>
        <button
          onClick={runResearch}
          disabled={allBlocks.length === 0}
          className="px-4 py-2 bg-sima-primary hover:bg-sima-primaryHover text-white rounded-lg text-sm font-medium flex items-center gap-2 transition-colors disabled:opacity-50"
        >
          <Globe className="w-4 h-4" />
          {t('research_button')}
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
        <span className="text-sm text-sima-textDim">{t('researching')}</span>
      </div>
    )
  }

  if (!report) return null

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="shrink-0 px-4 py-2 border-b border-sima-border flex items-center gap-2">
        <div className="flex gap-1">
          {([
            { id: 'overview' as const, label: t('tab_overview'), icon: Globe },
            { id: 'competitors' as const, label: t('tab_competitors'), icon: Users },
            { id: 'trends' as const, label: t('tab_trends'), icon: TrendingUp },
            { id: 'strategy' as const, label: t('tab_strategy'), icon: Target },
          ]).map(v => (
            <button
              key={v.id}
              onClick={() => setView(v.id)}
              className={cn(
                'px-2 py-1 text-[10px] rounded transition-colors flex items-center gap-1',
                view === v.id ? 'bg-sima-primary/20 text-sima-primary font-medium' : 'text-sima-textDim hover:text-sima-textMuted'
              )}
            >
              <v.icon className="w-3 h-3" />
              {v.label}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        <span className="text-[9px] text-sima-textDim">{t('analysis_seconds', { seconds: (report.analysisTime / 1000).toFixed(1) })}</span>
        <button onClick={exportAsMarkdown} className="p-1 text-sima-textDim hover:text-sima-textMuted transition-colors">
          <Download className="w-3.5 h-3.5" />
        </button>
        <button onClick={runResearch} className="p-1 text-sima-textDim hover:text-sima-textMuted transition-colors">
          <RefreshCw className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-3">
        {view === 'overview' && (
          <div className="space-y-4">
            <div className="prose prose-invert prose-xs max-w-none">
              <ReactMarkdown>{report.marketOverview}</ReactMarkdown>
            </div>
            <div className="bg-sima-bg border border-sima-border rounded-lg p-3">
              <h4 className="text-xs font-semibold text-sima-text mb-1">{t('market_size_heading')}</h4>
              <p className="text-[11px] text-sima-textMuted">{report.marketSize}</p>
            </div>
            {report.targetSegments.length > 0 && (
              <div className="bg-sima-bg border border-sima-border rounded-lg p-3">
                <h4 className="text-xs font-semibold text-sima-text mb-2">{t('target_segments_heading')}</h4>
                <div className="flex flex-wrap gap-1.5">
                  {report.targetSegments.map((seg, i) => (
                    <span key={i} className="text-[10px] px-2 py-0.5 bg-sima-primary/10 text-sima-primary rounded-full">{seg}</span>
                  ))}
                </div>
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <div className="bg-emerald-400/5 border border-emerald-400/20 rounded-lg p-3">
                <h4 className="text-xs font-semibold text-emerald-400 mb-1.5 flex items-center gap-1"><Zap className="w-3 h-3" />{t('opportunities_heading')}</h4>
                <ul className="space-y-1">
                  {report.opportunities.map((o, i) => (
                    <li key={i} className="text-[10px] text-sima-textMuted flex items-start gap-1"><span className="text-emerald-400">+</span>{o}</li>
                  ))}
                </ul>
              </div>
              <div className="bg-red-400/5 border border-red-400/20 rounded-lg p-3">
                <h4 className="text-xs font-semibold text-red-400 mb-1.5 flex items-center gap-1"><Shield className="w-3 h-3" />{t('threats_heading')}</h4>
                <ul className="space-y-1">
                  {report.threats.map((t, i) => (
                    <li key={i} className="text-[10px] text-sima-textMuted flex items-start gap-1"><span className="text-red-400">−</span>{t}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        )}

        {view === 'competitors' && (
          <div className="space-y-3">
            {report.competitors.map((comp, i) => (
              <CompetitorCard key={i} competitor={comp} />
            ))}
          </div>
        )}

        {view === 'trends' && (
          <div className="space-y-2">
            {report.trends.map((trend, i) => (
              <TrendCard key={i} trend={trend} />
            ))}
          </div>
        )}

        {view === 'strategy' && (
          <div className="space-y-4">
            <div className="bg-sima-bg border border-sima-border rounded-lg p-3">
              <h4 className="text-xs font-semibold text-sima-text mb-2">{t('positioning_heading')}</h4>
              <div className="prose prose-invert prose-xs max-w-none">
                <ReactMarkdown>{report.positioning}</ReactMarkdown>
              </div>
            </div>
            {report.differentiators.length > 0 && (
              <div className="bg-sima-primary/5 border border-sima-primary/20 rounded-lg p-3">
                <h4 className="text-xs font-semibold text-sima-primary mb-2">{t('key_differentiators')}</h4>
                <ul className="space-y-1">
                  {report.differentiators.map((d, i) => (
                    <li key={i} className="text-[10px] text-sima-textMuted flex items-start gap-1.5">
                      <span className="text-sima-primary font-bold">•</span>{d}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="bg-sima-bg border border-sima-border rounded-lg p-3">
              <h4 className="text-xs font-semibold text-sima-text mb-2">{t('go_to_market_strategy')}</h4>
              <div className="prose prose-invert prose-xs max-w-none">
                <ReactMarkdown>{report.goToMarket}</ReactMarkdown>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function CompetitorCard({ competitor }: { competitor: Competitor }) {
  const t = useTranslations('sima_research')
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="bg-sima-bg border border-sima-border rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-sima-surfaceLight transition-colors"
      >
        {expanded ? <ChevronDown className="w-3.5 h-3.5 text-sima-textDim" /> : <ChevronRight className="w-3.5 h-3.5 text-sima-textDim" />}
        <span className="text-xs font-semibold text-sima-text flex-1 text-left">{competitor.name}</span>
        {competitor.url && (
          <a href={competitor.url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()} className="text-sima-textDim hover:text-sima-primary">
            <ExternalLink className="w-3 h-3" />
          </a>
        )}
      </button>
      {expanded && (
        <div className="px-3 pb-3 space-y-2">
          <p className="text-[10px] text-sima-textMuted">{competitor.description}</p>
          <div className="text-[10px]"><span className="text-sima-textDim font-medium">{t('pricing_label')}</span> <span className="text-sima-textMuted">{competitor.pricing}</span></div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <span className="text-[9px] text-emerald-400 font-medium">{t('strengths_label')}</span>
              <ul className="mt-0.5 space-y-0.5">
                {competitor.strengths.map((s, i) => <li key={i} className="text-[9px] text-sima-textDim">+ {s}</li>)}
              </ul>
            </div>
            <div>
              <span className="text-[9px] text-red-400 font-medium">{t('weaknesses_label')}</span>
              <ul className="mt-0.5 space-y-0.5">
                {competitor.weaknesses.map((w, i) => <li key={i} className="text-[9px] text-sima-textDim">− {w}</li>)}
              </ul>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function TrendCard({ trend }: { trend: MarketTrend }) {
  const relevanceColors = { high: 'text-red-400 bg-red-400/10', medium: 'text-amber-400 bg-amber-400/10', low: 'text-sima-textDim bg-sima-bg' }
  return (
    <div className="bg-sima-bg border border-sima-border rounded-lg p-3">
      <div className="flex items-center gap-2 mb-1">
        <TrendingUp className="w-3 h-3 text-sima-primary" />
        <span className="text-xs font-semibold text-sima-text flex-1">{trend.trend}</span>
        <span className={cn('text-[9px] px-1.5 py-0.5 rounded', relevanceColors[trend.relevance])}>{trend.relevance}</span>
      </div>
      <p className="text-[10px] text-sima-textMuted">{trend.description}</p>
      <p className="text-[10px] text-sima-primary/80 mt-1">→ {trend.opportunity}</p>
    </div>
  )
}
