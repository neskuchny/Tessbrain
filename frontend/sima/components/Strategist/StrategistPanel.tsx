'use client'

import { useState, useRef, useEffect } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { cn } from '@/sima/lib/utils'
import {
  Brain,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  Loader2,
  Send,
  Sparkles,
  AlertTriangle,
  Lightbulb,
  Shield,
  Eye,
  ArrowRightLeft,
  Zap,
  Download,
  Copy,
  CheckCircle,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import type { StrategistReport, StrategistSection, StrategistInsight } from '@/sima/types'
import { simaFetch } from '@/sima/lib/api'

const insightIcons: Record<string, typeof Zap> = {
  strength: Zap,
  weakness: AlertTriangle,
  opportunity: Lightbulb,
  threat: Shield,
  blindspot: Eye,
  alternative: ArrowRightLeft,
}

const insightColors: Record<string, string> = {
  strength: 'text-green-400 bg-green-500/10',
  weakness: 'text-amber-400 bg-amber-500/10',
  opportunity: 'text-cyan-400 bg-cyan-500/10',
  threat: 'text-red-400 bg-red-500/10',
  blindspot: 'text-purple-400 bg-purple-500/10',
  alternative: 'text-blue-400 bg-blue-500/10',
}

export default function StrategistPanel() {
  const t = useTranslations('sima_strategist')
  const locale = useLocale()
  const {
    project,
    blocks,
    strategistReport,
    setStrategistReport,
    strategistLoading,
    setStrategistLoading,
    strategistChat,
    addStrategistChatMessage,
    strategistChatLoading,
    setStrategistChatLoading,
    setSelectedBlockId,
    setRightPanelOpen,
    addMessage,
    setAllBlocks,
    setAllConnections,
  } = useSimaStore()

  const [activeView, setActiveView] = useState<'report' | 'chat'>('report')
  const [chatInput, setChatInput] = useState('')
  const [expandedSections, setExpandedSections] = useState<Set<string>>(new Set())
  const chatEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [strategistChat])

  // Загрузить последний отчёт при первом открытии
  useEffect(() => {
    if (project && !strategistReport && !strategistLoading) {
      loadLastReport()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id])

  async function loadLastReport() {
    if (!project) return
    try {
      const res = await simaFetch('/ai/strategist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, action: 'last-report' }),
      })
      const data = await res.json()
      if (data && data.id) {
        setStrategistReport(data)
      }
    } catch (e) {
      console.error('Failed to load strategist report:', e)
    }
  }

  async function runAnalysis() {
    if (!project || strategistLoading) return
    setStrategistLoading(true)
    try {
      const res = await simaFetch('/ai/strategist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, action: 'analyze' }),
      })
      const report = await res.json()
      if (!res.ok || report.error) {
        throw new Error(report.error || t('err_analysis'))
      }
      setStrategistReport(report)
      setActiveView('report')
    } catch (e) {
      console.error('Strategist analysis error:', e)
      alert(e instanceof Error ? e.message : t('err_analysis'))
    } finally {
      setStrategistLoading(false)
    }
  }

  async function sendChatMessage() {
    if (!project || !chatInput.trim() || strategistChatLoading) return
    const msg = chatInput.trim()
    setChatInput('')
    addStrategistChatMessage({ role: 'user', content: msg })
    setStrategistChatLoading(true)

    try {
      const res = await simaFetch('/ai/strategist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          action: 'chat',
          question: msg,
          history: strategistChat,
        }),
      })
      const data = await res.json()
      addStrategistChatMessage({ role: 'assistant', content: data.answer || data.message || data.error || t('no_answer') })
    } catch (e) {
      addStrategistChatMessage({
        role: 'assistant',
        content: t('err_strategist'),
      })
    } finally {
      setStrategistChatLoading(false)
    }
  }

  function exportReportAsMarkdown() {
    if (!strategistReport) return
    const lines: string[] = []
    lines.push(t('report_heading') + '\n')
    lines.push(t('verdict_label') + ` ${strategistReport.verdict.emoji} ${strategistReport.verdict.oneLiner}\n`)
    lines.push(t('confidence_label', { percent: strategistReport.verdict.confidence }) + '\n')
    lines.push('\n' + t('top_actions_heading') + '\n')
    for (const a of strategistReport.topActions) {
      lines.push(t('action_line', { priority: a.priority, action: a.action, why: a.why, impact: a.impact }))
    }
    for (const section of strategistReport.sections) {
      lines.push(`\n## ${section.title}\n`)
      for (const ins of section.insights) {
        lines.push(`### ${ins.icon} ${ins.title} (${ins.type})`)
        lines.push(ins.body)
        if (ins.action) lines.push(t('action_prefix', { action: ins.action }))
        lines.push('')
      }
    }
    const text = lines.join('\n')
    const blob = new Blob([text], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `strategist_report.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  function toggleSection(title: string) {
    setExpandedSections((prev) => {
      const next = new Set(prev)
      if (next.has(title)) next.delete(title)
      else next.add(title)
      return next
    })
  }

  function highlightBlock(blockId: string) {
    setSelectedBlockId(blockId)
    setRightPanelOpen(true)
  }

  const hasBlocks = blocks.length > 0

  return (
    <div className="flex flex-col h-full bg-sima-surface">
      {/* Верхняя панель */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-sima-border">
        <Brain className="w-4 h-4 text-sima-primary" />
        <span className="text-xs font-semibold text-sima-text">{t('panel_title')}</span>

        <div className="flex ml-auto gap-1">
          <button
            onClick={() => setActiveView('report')}
            className={cn(
              'px-2 py-1 text-[10px] rounded transition-colors',
              activeView === 'report'
                ? 'bg-sima-primary/20 text-sima-primary'
                : 'text-sima-textDim hover:text-sima-textMuted'
            )}
          >
            {t('tab_report')}
          </button>
          <button
            onClick={() => setActiveView('chat')}
            className={cn(
              'px-2 py-1 text-[10px] rounded transition-colors',
              activeView === 'chat'
                ? 'bg-sima-primary/20 text-sima-primary'
                : 'text-sima-textDim hover:text-sima-textMuted'
            )}
          >
            {t('tab_dialog')}
          </button>
        </div>

        <button
          onClick={runAnalysis}
          disabled={strategistLoading || !hasBlocks}
          className={cn(
            'flex items-center gap-1 px-2.5 py-1 rounded text-[10px] font-medium transition-colors',
            strategistLoading || !hasBlocks
              ? 'bg-sima-bg text-sima-textDim cursor-not-allowed'
              : 'bg-sima-primary/20 text-sima-primary hover:bg-sima-primary/30'
          )}
        >
          {strategistLoading ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : (
            <RefreshCw className="w-3 h-3" />
          )}
          {strategistLoading ? t('analyzing') : t('analyze_button')}
        </button>

        {strategistReport && (
          <button
            onClick={exportReportAsMarkdown}
            className="flex items-center gap-1 px-2 py-1 rounded text-[10px] text-sima-textDim hover:text-sima-textMuted transition-colors"
          >
            <Download className="w-3 h-3" />
          </button>
        )}
      </div>

      {/* Контент */}
      <div className="flex-1 overflow-y-auto">
        {activeView === 'report' ? (
          <ReportView
            report={strategistReport}
            loading={strategistLoading}
            hasBlocks={hasBlocks}
            onRunAnalysis={runAnalysis}
            expandedSections={expandedSections}
            toggleSection={toggleSection}
            highlightBlock={highlightBlock}
            blocks={blocks}
          />
        ) : (
          <ChatView
            chat={strategistChat}
            chatInput={chatInput}
            setChatInput={setChatInput}
            sendMessage={sendChatMessage}
            loading={strategistChatLoading}
            chatEndRef={chatEndRef}
          />
        )}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════
// REPORT VIEW
// ═══════════════════════════════════════════

function ReportView({
  report,
  loading,
  hasBlocks,
  onRunAnalysis,
  expandedSections,
  toggleSection,
  highlightBlock,
  blocks,
}: {
  report: StrategistReport | null
  loading: boolean
  hasBlocks: boolean
  onRunAnalysis: () => void
  expandedSections: Set<string>
  toggleSection: (title: string) => void
  highlightBlock: (blockId: string) => void
  blocks: { id: string; label: string; name: string; color: string }[]
}) {
  const t = useTranslations('sima_strategist')
  const locale = useLocale()
  const { project, addMessage, setAllBlocks, setAllConnections } = useSimaStore()
  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 p-6">
        <Loader2 className="w-8 h-8 animate-spin text-sima-primary" />
        <p className="text-sm text-sima-textMuted">{t('analyzing_progress')}</p>
        <p className="text-[10px] text-sima-textDim">
          {t('analyzing_hint')}
        </p>
      </div>
    )
  }

  if (!report) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 p-6">
        <Brain className="w-10 h-10 text-sima-textDim" />
        <p className="text-sm text-sima-textMuted text-center">
          {hasBlocks
            ? t('no_analysis_yet')
            : t('need_blocks_first')}
        </p>
        {hasBlocks && (
          <button
            onClick={onRunAnalysis}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-sima-primary/20 text-sima-primary text-sm hover:bg-sima-primary/30 transition-colors"
          >
            <Sparkles className="w-4 h-4" />
            {t('run_analysis')}
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="p-3 space-y-3">
      {/* Вердикт */}
      <div
        className={cn(
          'rounded-lg p-3 border',
          report.verdict.emoji === '🟢'
            ? 'bg-green-500/5 border-green-500/20'
            : report.verdict.emoji === '🟡'
            ? 'bg-amber-500/5 border-amber-500/20'
            : 'bg-red-500/5 border-red-500/20'
        )}
      >
        <div className="flex items-start gap-2">
          <span className="text-lg">{report.verdict.emoji}</span>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-sima-text">{report.verdict.oneLiner}</p>
            <p className="text-[10px] text-sima-textDim mt-1">
              {t('confidence_analysis_line', { percent: report.verdict.confidence, seconds: report.analysisTime })}{' '}
              {new Date(report.createdAt).toLocaleString(locale)}
            </p>
          </div>
        </div>
      </div>

      {/* {t('top_3_actions')} */}
      <div>
        <h4 className="text-[10px] font-semibold text-sima-textDim uppercase tracking-wider mb-2">
          {t('top_3_actions')}
        </h4>
        <div className="space-y-2">
          {report.topActions.map((action, i) => (
            <div
              key={i}
              className="rounded-lg bg-sima-bg border border-sima-border p-2.5"
            >
              <div className="flex items-start gap-2">
                <span className="text-xs font-bold text-sima-primary bg-sima-primary/10 w-5 h-5 rounded-full flex items-center justify-center shrink-0">
                  {action.priority}
                </span>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-sima-text">{action.action}</p>
                  <p className="text-[10px] text-sima-textDim mt-0.5">{action.why}</p>
                  <p className="text-[10px] text-sima-accent mt-0.5">→ {action.impact}</p>
                  {action.relatedBlockIds && action.relatedBlockIds.length > 0 && (
                    <div className="flex gap-1 mt-1.5 flex-wrap">
                      {action.relatedBlockIds.map((bid) => {
                        const block = blocks.find((b) => b.id === bid)
                        if (!block) return null
                        return (
                          <button
                            key={bid}
                            onClick={() => highlightBlock(bid)}
                            className="text-[9px] font-bold px-1.5 py-0.5 rounded cursor-pointer hover:opacity-80 transition-opacity"
                            style={{
                              backgroundColor: `${block.color}20`,
                              color: block.color,
                            }}
                          >
                            {block.label}
                          </button>
                        )
                      })}
                    </div>
                  )}
                  {/* Кнопка «Применить» */}
                  <button
                    onClick={async () => {
                      if (!project) return
                      // Отправляем в AI-чат запрос на применение рекомендации
                      const blocksInfo = action.relatedBlockIds?.length ? action.relatedBlockIds.map((bid: string) => { const b = blocks.find(bl => bl.id === bid); return b ? b.label + ' ' + b.name : bid }).join(', ') : ''
                      const prompt = t('apply_prompt_template', { action: action.action, why: action.why, impact: action.impact }) + (blocksInfo ? ` (${blocksInfo})` : '')
                      try {
                        const res = await simaFetch('/ai/chat', {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({
                            projectId: project.id,
                            message: prompt,
                            mode: 'chat',
                          }),
                        })
                        if (res.ok) {
                          const data = await res.json()
                          addMessage({
                            id: crypto.randomUUID(),
                            projectId: project.id,
                            role: 'user',
                            content: t('applying_action', { action: action.action }),
                            createdAt: new Date().toISOString(),
                          })
                          addMessage({
                            id: crypto.randomUUID(),
                            projectId: project.id,
                            role: 'assistant',
                            content: data.message || t('default_apply_response'),
                            actions: JSON.stringify(data.actions || []),
                            createdAt: new Date().toISOString(),
                          })
                          if (data.project) {
                            setAllBlocks(data.project.blocks || [])
                            setAllConnections(data.project.connections || [])
                          }
                        }
                      } catch (e) {
                        console.error('Apply recommendation error:', e)
                      }
                    }}
                    className="mt-1.5 text-[10px] px-2 py-1 bg-sima-primary/10 text-sima-primary rounded hover:bg-sima-primary/20 transition-colors flex items-center gap-1"
                  >
                    <Zap className="w-2.5 h-2.5" />
                    {t('apply_button')}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* {t('detailed_breakdown')} по секциям */}
      <div>
        <h4 className="text-[10px] font-semibold text-sima-textDim uppercase tracking-wider mb-2">
          {t('detailed_breakdown')}
        </h4>
        <div className="space-y-1">
          {report.sections.map((section) => (
            <SectionBlock
              key={section.title}
              section={section}
              expanded={expandedSections.has(section.title)}
              onToggle={() => toggleSection(section.title)}
              highlightBlock={highlightBlock}
              blocks={blocks}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function SectionBlock({
  section,
  expanded,
  onToggle,
  highlightBlock,
  blocks,
}: {
  section: StrategistSection
  expanded: boolean
  onToggle: () => void
  highlightBlock: (blockId: string) => void
  blocks: { id: string; label: string; name: string; color: string }[]
}) {
  const t = useTranslations('sima_strategist')
  return (
    <div className="rounded-lg bg-sima-bg border border-sima-border overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-sima-surfaceLight transition-colors"
      >
        {expanded ? (
          <ChevronDown className="w-3 h-3 text-sima-textDim" />
        ) : (
          <ChevronRight className="w-3 h-3 text-sima-textDim" />
        )}
        <span className="text-xs font-medium text-sima-text">{section.title}</span>
        <span className="text-[10px] text-sima-textDim ml-auto">
          {t('insights_count_plural', { count: section.insights.length })}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-sima-border px-3 py-2 space-y-2">
          {section.insights.map((insight, i) => (
            <InsightCard
              key={i}
              insight={insight}
              highlightBlock={highlightBlock}
              blocks={blocks}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function InsightCard({
  insight,
  highlightBlock,
  blocks,
}: {
  insight: StrategistInsight
  highlightBlock: (blockId: string) => void
  blocks: { id: string; label: string; name: string; color: string }[]
}) {
  const Icon = insightIcons[insight.type] || Lightbulb
  const colorClass = insightColors[insight.type] || 'text-gray-400 bg-gray-500/10'

  return (
    <div className="flex gap-2 text-[11px]">
      <div className={cn('w-5 h-5 rounded flex items-center justify-center shrink-0 mt-0.5', colorClass)}>
        <Icon className="w-3 h-3" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="font-medium text-sima-text">{insight.title}</p>
        <p className="text-sima-textDim mt-0.5 leading-relaxed">{insight.body}</p>
        {insight.actionable && insight.action && (
          <p className="text-sima-accent mt-1">→ {insight.action}</p>
        )}
        {insight.relatedBlockIds.length > 0 && (
          <div className="flex gap-1 mt-1 flex-wrap">
            {insight.relatedBlockIds.map((bid) => {
              const block = blocks.find((b) => b.id === bid)
              if (!block) return null
              return (
                <button
                  key={bid}
                  onClick={() => highlightBlock(bid)}
                  className="text-[9px] font-bold px-1.5 py-0.5 rounded cursor-pointer hover:opacity-80 transition-opacity"
                  style={{
                    backgroundColor: `${block.color}20`,
                    color: block.color,
                  }}
                >
                  {block.label}
                </button>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════
// CHAT VIEW
// ═══════════════════════════════════════════

function ChatView({
  chat,
  chatInput,
  setChatInput,
  sendMessage,
  loading,
  chatEndRef,
}: {
  chat: { role: 'user' | 'assistant'; content: string }[]
  chatInput: string
  setChatInput: (val: string) => void
  sendMessage: () => void
  loading: boolean
  chatEndRef: React.RefObject<HTMLDivElement>
}) {
  const t = useTranslations('sima_strategist')
  const suggestions = [
    t('example_question_1'),
    t('example_question_2'),
    t('example_question_3'),
    t('example_question_4'),
    t('example_question_5'),
  ]

  return (
    <div className="flex flex-col h-full">
      {/* Сообщения */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {chat.length === 0 && (
          <div className="space-y-3">
            <div className="text-center py-4">
              <Brain className="w-8 h-8 text-sima-textDim mx-auto mb-2" />
              <p className="text-xs text-sima-textMuted">
                {t('ask_strategist_hint')}
              </p>
            </div>
            <div className="flex flex-wrap gap-1.5 justify-center">
              {suggestions.map((s) => (
                <button
                  key={s}
                  onClick={() => {
                    setChatInput(s)
                  }}
                  className="text-[10px] px-2.5 py-1.5 rounded-lg bg-sima-bg border border-sima-border text-sima-textMuted hover:text-sima-text hover:border-sima-primary/30 transition-colors"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {chat.map((msg, i) => (
          <div
            key={i}
            className={cn(
              'text-xs leading-relaxed',
              msg.role === 'user'
                ? 'text-sima-text bg-sima-bg rounded-lg px-3 py-2 ml-8'
                : 'text-sima-textMuted'
            )}
          >
            {msg.role === 'assistant' && (
              <div className="flex items-center gap-1 mb-1">
                <Brain className="w-3 h-3 text-sima-primary" />
                <span className="text-[10px] font-semibold text-sima-primary">{t('strategist_label')}</span>
              </div>
            )}
            {msg.role === 'assistant' ? (
              <div className="prose prose-invert prose-xs max-w-none [&_p]:my-1 [&_ul]:my-1 [&_ol]:my-1 [&_li]:my-0.5 [&_h1]:text-sm [&_h2]:text-xs [&_h3]:text-xs [&_h4]:text-[11px] [&_code]:text-[10px] [&_code]:bg-sima-bg [&_code]:px-1 [&_code]:rounded [&_strong]:text-sima-text [&_blockquote]:border-sima-primary/30 [&_blockquote]:text-sima-textDim">
                <ReactMarkdown>{msg.content}</ReactMarkdown>
              </div>
            ) : (
              <div className="whitespace-pre-wrap">{msg.content}</div>
            )}
          </div>
        ))}

        {loading && (
          <div className="flex items-center gap-2 text-xs text-sima-textDim">
            <Loader2 className="w-3 h-3 animate-spin" />
            {t('strategist_thinking')}
          </div>
        )}

        <div ref={chatEndRef} />
      </div>

      {/* Ввод */}
      <div className="border-t border-sima-border p-2">
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                sendMessage()
              }
            }}
            placeholder={t("ask_placeholder")}
            className="flex-1 bg-sima-bg border border-sima-border rounded-lg px-3 py-2 text-xs text-sima-text placeholder:text-sima-textDim focus:outline-none focus:border-sima-primary/50"
            disabled={loading}
          />
          <button
            onClick={sendMessage}
            disabled={loading || !chatInput.trim()}
            className={cn(
              'p-2 rounded-lg transition-colors',
              loading || !chatInput.trim()
                ? 'bg-sima-bg text-sima-textDim'
                : 'bg-sima-primary/20 text-sima-primary hover:bg-sima-primary/30'
            )}
          >
            <Send className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>
  )
}
