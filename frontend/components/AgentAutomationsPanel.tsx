'use client'

import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import {
  Bot,
  Play,
  Pause,
  Trash2,
  RefreshCw,
  Plus,
  Clock,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  CheckCircle,
  XCircle,
  Loader2,
  History,
  Zap,
  X,
  Calendar,
  Repeat,
  Timer,
  Eye,
  Send,
  Sparkles,
  Settings2,
  ChevronRight,
} from 'lucide-react'

interface AgentAutomation {
  id: string
  user_id: string
  name: string
  instruction: string
  description?: string
  schedule_type: 'once' | 'recurring' | 'event'
  execute_at?: string
  cron_expression?: string
  interval_seconds?: number
  trigger_type: string
  event_type?: string
  model_tier: string
  max_react_steps: number
  status: 'active' | 'paused' | 'completed' | 'failed' | 'cancelled'
  last_run_at?: string
  last_run_status?: string
  last_error?: string
  run_count: number
  max_runs?: number
  expires_at?: string
  last_run_trace?: TraceStep[]
  total_tokens_used: number
  total_cost_usd: number
  last_run_tokens: number
  last_run_cost_usd: number
  created_at?: string
  updated_at?: string
}

interface TraceStep {
  step: number
  thought: string
  tool: string
  args: Record<string, any>
  observation: string
  is_final: boolean
}

interface ExecutionLog {
  id: string
  automation_id: string
  status: string
  started_at: string
  completed_at?: string
  duration_ms?: number
  trace?: TraceStep[]
  final_answer?: string
  error_message?: string
  input_tokens: number
  output_tokens: number
  cost_usd: number
  model_tier: string
}

const STATUS_COLORS: Record<string, string> = {
  active: 'text-green-400 bg-green-400/10 border-green-400/30',
  paused: 'text-yellow-400 bg-yellow-400/10 border-yellow-400/30',
  completed: 'text-blue-400 bg-blue-400/10 border-blue-400/30',
  failed: 'text-red-400 bg-red-400/10 border-red-400/30',
  cancelled: 'text-gray-400 bg-gray-400/10 border-gray-400/30',
}

const STATUS_ICONS: Record<string, React.ReactNode> = {
  active: <Play className="w-3 h-3" />,
  paused: <Pause className="w-3 h-3" />,
  completed: <CheckCircle className="w-3 h-3" />,
  failed: <XCircle className="w-3 h-3" />,
  cancelled: <X className="w-3 h-3" />,
}

const RUN_STATUS_COLORS: Record<string, string> = {
  success: 'text-green-400',
  failed: 'text-red-400',
  skipped: 'text-yellow-400',
}

interface AgentAutomationsPanelProps {
  userId?: string
}

export default function AgentAutomationsPanel({ userId }: AgentAutomationsPanelProps) {
  const t = useTranslations('agent_automations')
  const [automations, setAutomations] = useState<AgentAutomation[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [logs, setLogs] = useState<Record<string, ExecutionLog[]>>({})
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [filter, setFilter] = useState<'all' | 'active' | 'recurring'>('all')
  const [runningIds, setRunningIds] = useState<Set<string>>(new Set())

  // Create form state
  const [newName, setNewName] = useState('')
  const [newInstruction, setNewInstruction] = useState('')
  const [newDescription, setNewDescription] = useState('')
  const [newScheduleType, setNewScheduleType] = useState<'once' | 'recurring' | 'event'>('once')
  const [newScheduleText, setNewScheduleText] = useState('')
  const [newCronExpression, setNewCronExpression] = useState('')
  const [newExecuteAt, setNewExecuteAt] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)
  const [newModelTier, setNewModelTier] = useState<'standard' | 'premium'>('standard')
  const [newMaxSteps, setNewMaxSteps] = useState(5)
  const [newEventType, setNewEventType] = useState('')
  const [creating, setCreating] = useState(false)

  const fetchAutomations = useCallback(async () => {
    try {
      setLoading(true)
      const params = new URLSearchParams()
      if (userId) params.append('user_id', userId)
      if (filter === 'active') params.append('status', 'active')
      if (filter === 'recurring') params.append('schedule_type', 'recurring')

      const res = await authFetch(`/api/v1/agent-automations?${params}`)
      if (res.ok) {
        const data = await res.json()
        setAutomations(data.automations || [])
      } else {
        throw new Error('Failed to fetch agent automations')
      }
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [userId, filter])

  useEffect(() => {
    fetchAutomations()
  }, [fetchAutomations])

  // Быстрый поллинг, пока есть идущий прогон (прогресс-стриминг), иначе 30с
  const anyRunning = automations.some((a) => (a.last_run_status || '').startsWith('running'))
  useEffect(() => {
    const interval = setInterval(fetchAutomations, anyRunning ? 4000 : 30000)
    return () => clearInterval(interval)
  }, [fetchAutomations, anyRunning])

  const fetchLogs = async (automationId: string) => {
    try {
      const res = await authFetch(`/api/v1/agent-automations/${automationId}/logs`)
      if (res.ok) {
        const data = await res.json()
        setLogs((prev) => ({ ...prev, [automationId]: data }))
      }
    } catch (e) {
      console.error('Failed to fetch logs:', e)
    }
  }

  const handleAction = async (
    automationId: string,
    action: 'pause' | 'resume' | 'cancel' | 'run' | 'delete'
  ) => {
    try {
      if (action === 'run') {
        setRunningIds((prev) => new Set(prev).add(automationId))
      }

      let url = `/api/v1/agent-automations/${automationId}`
      let method = 'POST'

      if (action === 'delete') {
        method = 'DELETE'
      } else {
        url += `/${action}`
      }

      const res = await authFetch(url, { method })
      if (res.ok) {
        fetchAutomations()
        if (action === 'run' && expandedId === automationId) {
          fetchLogs(automationId)
        }
      }
    } catch (e) {
      console.error(`Failed to ${action}:`, e)
    } finally {
      if (action === 'run') {
        setRunningIds((prev) => {
          const next = new Set(prev)
          next.delete(automationId)
          return next
        })
      }
    }
  }

  const handleCreate = async () => {
    setCreateError(null)
    if (!newInstruction.trim()) { setCreateError(t('error_no_instruction')); return }
    // Клиентская валидация расписания — чтобы не ловить 400 с сервера
    if (newScheduleType === 'once' && !newExecuteAt.trim()) {
      setCreateError(t('error_no_execute_at')); return
    }
    if (newScheduleType === 'recurring' && !newScheduleText.trim() && !newCronExpression.trim()) {
      setCreateError(t('error_no_schedule')); return
    }
    if (newScheduleType === 'event' && !newEventType) {
      setCreateError(t('error_no_event')); return
    }
    setCreating(true)
    try {
      const payload: Record<string, any> = {
        name: newName || newInstruction.slice(0, 60),
        instruction: newInstruction,
        description: newDescription || undefined,
        schedule_type: newScheduleType,
        model_tier: newModelTier,
        max_react_steps: newMaxSteps,
        user_id: userId || 'default_user',
      }

      if (newExecuteAt) {
        payload.schedule_text = newExecuteAt
      }
      if (newScheduleText) {
        payload.schedule_text = newScheduleText
      }
      if (newCronExpression) {
        payload.cron_expression = newCronExpression
      }
      if (newScheduleType === 'event') {
        payload.trigger_type = 'event'
        payload.event_type = newEventType || 'custom'
      }

      const res = await authFetch('/api/v1/agent-automations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      if (res.ok) {
        resetForm()
        setShowCreateModal(false)
        fetchAutomations()
      } else {
        const data = await res.json().catch(() => ({}))
        setCreateError(data?.detail || data?.message || t('error_create_failed', { status: res.status }))
      }
    } catch (e) {
      setCreateError(t('error_network'))
    } finally {
      setCreating(false)
    }
  }

  const resetForm = () => {
    setNewName('')
    setNewInstruction('')
    setNewDescription('')
    setNewScheduleType('once')
    setNewScheduleText('')
    setNewCronExpression('')
    setNewExecuteAt('')
    setCreateError(null)
    setNewModelTier('standard')
    setNewMaxSteps(5)
    setNewEventType('')
  }

  const toggleExpand = (id: string) => {
    if (expandedId === id) {
      setExpandedId(null)
    } else {
      setExpandedId(id)
      if (!logs[id]) {
        fetchLogs(id)
      }
    }
  }

  const formatDate = (dateStr?: string) => {
    if (!dateStr) return '—'
    const date = new Date(dateStr)
    return date.toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      year: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  const formatDuration = (ms?: number) => {
    if (!ms) return '—'
    if (ms < 1000) return `${ms}ms`
    return `${(ms / 1000).toFixed(1)}s`
  }

  const stats = {
    total: automations.length,
    active: automations.filter((a) => a.status === 'active').length,
    recurring: automations.filter((a) => a.schedule_type === 'recurring').length,
    completed: automations.filter((a) => a.status === 'completed').length,
  }

  return (
    <div className="h-full flex flex-col bg-brain-950/50">
      {/* Header */}
      <div className="p-4 border-b border-brain-700/30">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Bot className="w-5 h-5 text-purple-400" />
            <h2 className="text-lg font-semibold text-brain-100">{t('title')}</h2>
            <span className="text-xs text-brain-500 bg-purple-500/10 border border-purple-500/30 px-2 py-0.5 rounded-full">
              OpenClaw-style
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={fetchAutomations}
              className="p-2 rounded-lg bg-brain-800/50 hover:bg-brain-700/50 text-brain-400 hover:text-brain-200 transition-colors"
              title={t('refresh_button_title')}
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={() => setShowCreateModal(true)}
              className="flex items-center gap-2 px-3 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white transition-colors"
            >
              <Plus className="w-4 h-4" />
              <span className="text-sm">{t('create_button')}</span>
            </button>
          </div>
        </div>

        {/* Info */}
        <div className="text-xs text-brain-500 mb-3 bg-brain-900/40 rounded-lg px-3 py-2 border border-brain-700/20">
          <Sparkles className="w-3.5 h-3.5 inline mr-1 text-purple-400" />
          {t('info_banner')}
        </div>

        {/* Stats & Filters */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4 text-xs text-brain-500">
            <span>{t('stats_total', { count: stats.total })}</span>
            <span className="text-green-400">{t('stats_active', { count: stats.active })}</span>
            <span className="text-purple-400">{t('stats_recurring', { count: stats.recurring })}</span>
            <span className="text-blue-400">{t('stats_completed', { count: stats.completed })}</span>
          </div>
          <div className="flex items-center gap-1">
            {(['all', 'active', 'recurring'] as const).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-2.5 py-1 rounded text-xs transition-colors ${
                  filter === f
                    ? 'bg-purple-600 text-white'
                    : 'text-brain-400 hover:bg-brain-800/50'
                }`}
              >
                {f === 'all' ? t('filter_all') : f === 'active' ? t('filter_active') : t('filter_recurring')}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* List */}
      <div className="flex-1 overflow-auto p-4 space-y-3">
        {loading && automations.length === 0 ? (
          <div className="flex items-center justify-center py-12 text-brain-500">
            <Loader2 className="w-5 h-5 animate-spin mr-2" />
            {t('loading')}
          </div>
        ) : error ? (
          <div className="flex items-center justify-center py-12 text-red-400">
            <AlertCircle className="w-5 h-5 mr-2" />
            {error}
          </div>
        ) : automations.length === 0 ? (
          <div className="max-w-lg mx-auto text-center py-10 px-6">
            <div className="w-14 h-14 rounded-2xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center mb-4 mx-auto">
              <Bot className="w-7 h-7 text-purple-400" />
            </div>
            <p className="text-brain-100 font-medium text-base">{t('empty_state_title')}</p>
            <p className="text-sm text-brain-400 mt-2 leading-relaxed">
              {t('empty_state_description')}
            </p>
            <div className="mt-4 text-left bg-brain-800/40 border border-brain-700/30 rounded-xl p-3">
              <p className="text-xs text-brain-500 mb-2">{t('empty_examples_title')}</p>
              <ul className="space-y-1.5 text-sm text-brain-300">
                <li>• {t('empty_example_1')}</li>
                <li>• {t('empty_example_2')}</li>
                <li>• {t('empty_example_3')}</li>
              </ul>
            </div>
            <button
              onClick={() => setShowCreateModal(true)}
              className="mt-5 inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium transition-colors"
            >
              <Plus className="w-4 h-4" />
              {t('empty_create_button')}
            </button>
          </div>
        ) : (
          automations.map((a) => (
            <div
              key={a.id}
              className="bg-brain-900/60 border border-brain-700/30 rounded-xl overflow-hidden"
            >
              {/* Main row */}
              <div
                className="flex items-center gap-3 p-3 cursor-pointer hover:bg-brain-800/30 transition-colors"
                onClick={() => toggleExpand(a.id)}
              >
                {/* Status */}
                <div
                  className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs border ${
                    STATUS_COLORS[a.status] || ''
                  }`}
                >
                  {STATUS_ICONS[a.status]}
                  <span className="capitalize">{a.status}</span>
                </div>

                {/* Name & instruction */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-brain-100 truncate">
                      {a.name}
                    </span>
                    <span className="text-xs text-brain-600 bg-brain-800/50 px-1.5 py-0.5 rounded">
                      {a.schedule_type === 'once'
                        ? t('schedule_once_short')
                        : a.schedule_type === 'recurring'
                        ? t('schedule_recurring_short')
                        : t('schedule_event_short')}
                    </span>
                    <span
                      className={`text-xs px-1.5 py-0.5 rounded ${
                        a.model_tier === 'premium'
                          ? 'bg-amber-500/10 text-amber-400 border border-amber-500/30'
                          : 'bg-brain-800/50 text-brain-500'
                      }`}
                    >
                      {a.model_tier === 'premium' ? 'Premium' : 'Standard'}
                    </span>
                  </div>
                  <p className="text-xs text-brain-500 truncate mt-0.5">
                    {a.instruction}
                  </p>
                </div>

                {/* Run info */}
                <div className="flex items-center gap-3 text-xs text-brain-500 shrink-0">
                  {(a.last_run_status || '').startsWith('running') ? (
                    <span className="text-cyan-400 animate-pulse whitespace-nowrap">
                      {(() => {
                        // "running:3/5:send_telegram" → «⏳ шаг 3/5 · send_telegram»
                        const m = /^running:(\d+)\/(\d+):?(.*)$/.exec(a.last_run_status || '')
                        return m ? `${t('running_step', { step: m[1], total: m[2] })}${m[3] ? ' · ' + m[3] : ''}` : t('running_generic')
                      })()}
                    </span>
                  ) : a.last_run_status ? (
                    <span className={RUN_STATUS_COLORS[a.last_run_status] || ''}>
                      {a.last_run_status === 'success' ? '✓' : a.last_run_status === 'failed' ? '✗' : '~'}
                    </span>
                  ) : null}
                  <span>x{a.run_count}</span>
                  <span>{formatDate(a.last_run_at)}</span>
                </div>

                {/* Actions */}
                <div className="flex items-center gap-1 shrink-0">
                  {a.status === 'active' && (
                    <>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          handleAction(a.id, 'run')
                        }}
                        disabled={runningIds.has(a.id)}
                        className="p-1.5 rounded-lg hover:bg-purple-600/20 text-purple-400 hover:text-purple-300 transition-colors disabled:opacity-50"
                        title={t('run_now_title')}
                      >
                        {runningIds.has(a.id) ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                          <Play className="w-4 h-4" />
                        )}
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          handleAction(a.id, 'pause')
                        }}
                        className="p-1.5 rounded-lg hover:bg-yellow-600/20 text-yellow-400 hover:text-yellow-300 transition-colors"
                        title={t('pause_title')}
                      >
                        <Pause className="w-4 h-4" />
                      </button>
                    </>
                  )}
                  {a.status === 'paused' && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        handleAction(a.id, 'resume')
                      }}
                      className="p-1.5 rounded-lg hover:bg-green-600/20 text-green-400 hover:text-green-300 transition-colors"
                      title={t('resume_title')}
                    >
                      <Play className="w-4 h-4" />
                    </button>
                  )}
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      handleAction(a.id, 'delete')
                    }}
                    className="p-1.5 rounded-lg hover:bg-red-600/20 text-red-400 hover:text-red-300 transition-colors"
                    title={t('delete_title')}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                  {expandedId === a.id ? (
                    <ChevronUp className="w-4 h-4 text-brain-500" />
                  ) : (
                    <ChevronDown className="w-4 h-4 text-brain-500" />
                  )}
                </div>
              </div>

              {/* Expanded details */}
              {expandedId === a.id && (
                <div className="border-t border-brain-700/30 p-4 space-y-4">
                  {/* Instruction */}
                  <div>
                    <div className="text-xs text-brain-500 mb-1">{t('agent_instruction_label')}</div>
                    <div className="text-sm text-brain-200 bg-brain-800/40 rounded-lg p-3 border border-brain-700/20">
                      {a.instruction}
                    </div>
                  </div>

                  {/* Schedule info */}
                  <div className="grid grid-cols-2 gap-4 text-xs">
                    <div>
                      <span className="text-brain-500">{t('schedule_type_label')}</span>{' '}
                      <span className="text-brain-300">
                        {a.schedule_type === 'once'
                          ? t('schedule_once_full')
                          : a.schedule_type === 'recurring'
                          ? t('schedule_recurring_full')
                          : t('schedule_event_full')}
                      </span>
                    </div>
                    {a.cron_expression && (
                      <div>
                        <span className="text-brain-500">Cron:</span>{' '}
                        <span className="text-brain-300 font-mono">{a.cron_expression}</span>
                      </div>
                    )}
                    {a.execute_at && (
                      <div>
                        <span className="text-brain-500">{t('execute_at_label')}</span>{' '}
                        <span className="text-brain-300">{formatDate(a.execute_at)}</span>
                      </div>
                    )}
                    {a.interval_seconds && (
                      <div>
                        <span className="text-brain-500">{t('interval_label')}</span>{' '}
                        <span className="text-brain-300">{a.interval_seconds}s</span>
                      </div>
                    )}
                    <div>
                      <span className="text-brain-500">{t('model_label')}</span>{' '}
                      <span className="text-brain-300">{a.model_tier}</span>
                    </div>
                    <div>
                      <span className="text-brain-500">{t('max_steps_label')}</span>{' '}
                      <span className="text-brain-300">{a.max_react_steps}</span>
                    </div>
                    <div>
                      <span className="text-brain-500">{t('total_tokens_label')}</span>{' '}
                      <span className="text-brain-300">{a.total_tokens_used.toLocaleString()}</span>
                    </div>
                    <div>
                      <span className="text-brain-500">{t('created_label')}</span>{' '}
                      <span className="text-brain-300">{formatDate(a.created_at)}</span>
                    </div>
                  </div>

                  {/* Last run trace */}
                  {a.last_run_trace && a.last_run_trace.length > 0 && (
                    <div>
                      <div className="text-xs text-brain-500 mb-2 flex items-center gap-1">
                        <Eye className="w-3.5 h-3.5" />
                        {t('trace_title', { count: a.last_run_trace.length })}
                      </div>
                      <div className="space-y-2">
                        {a.last_run_trace.map((step) => (
                          <div
                            key={step.step}
                            className="bg-brain-800/40 rounded-lg p-3 border border-brain-700/20"
                          >
                            <div className="flex items-center gap-2 mb-1">
                              <span className="text-xs bg-purple-500/20 text-purple-400 px-1.5 py-0.5 rounded font-mono">
                                #{step.step}
                              </span>
                              <span className="text-xs font-medium text-brain-200">
                                {step.tool}
                              </span>
                              {step.args && Object.keys(step.args).length > 0 && (
                                <span className="text-xs text-brain-600">
                                  ({Object.entries(step.args)
                                    .slice(0, 2)
                                    .map(([k, v]) => `${k}=${String(v).slice(0, 30)}`)
                                    .join(', ')}
                                  )
                                </span>
                              )}
                            </div>
                            {step.thought && (
                              <div className="text-xs text-brain-500 italic mb-1">
                                {step.thought.slice(0, 200)}
                              </div>
                            )}
                            {step.observation && (
                              <div className="text-xs text-brain-400 bg-brain-900/50 rounded p-2 mt-1 max-h-24 overflow-auto font-mono">
                                {step.observation.slice(0, 500)}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Error */}
                  {a.last_error && (
                    <div className="text-xs text-red-400 bg-red-400/5 rounded-lg p-3 border border-red-400/20">
                      <AlertCircle className="w-3.5 h-3.5 inline mr-1" />
                      {a.last_error}
                    </div>
                  )}

                  {/* Execution logs */}
                  {logs[a.id] && logs[a.id].length > 0 && (
                    <div>
                      <div className="text-xs text-brain-500 mb-2 flex items-center gap-1">
                        <History className="w-3.5 h-3.5" />
                        {t('execution_history')}
                      </div>
                      <div className="space-y-1.5">
                        {logs[a.id].slice(0, 5).map((log) => (
                          <div
                            key={log.id}
                            className="flex items-center gap-3 text-xs bg-brain-800/30 rounded-lg px-3 py-2"
                          >
                            <span
                              className={
                                log.status === 'success'
                                  ? 'text-green-400'
                                  : log.status === 'failed'
                                  ? 'text-red-400'
                                  : 'text-yellow-400'
                              }
                            >
                              {log.status === 'success' ? '✓' : log.status === 'failed' ? '✗' : '~'}
                            </span>
                            <span className="text-brain-400">{formatDate(log.started_at)}</span>
                            <span className="text-brain-600">{formatDuration(log.duration_ms)}</span>
                            <span className="text-brain-600">
                              {(log.input_tokens + log.output_tokens).toLocaleString()} tok
                            </span>
                            {log.trace && (
                              <span className="text-brain-600">{t('steps_count', { count: log.trace.length })}</span>
                            )}
                            {log.error_message && (
                              <span className="text-red-400 truncate flex-1">
                                {log.error_message.slice(0, 80)}
                              </span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      {/* Create Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-brain-900 border border-brain-700/50 rounded-2xl shadow-2xl w-full max-w-lg mx-4 max-h-[85vh] overflow-auto">
            {/* Modal header */}
            <div className="flex items-center justify-between p-4 border-b border-brain-700/30">
              <div className="flex items-center gap-2">
                <Bot className="w-5 h-5 text-purple-400" />
                <h3 className="text-lg font-semibold text-brain-100">
                  {t('modal_title')}
                </h3>
              </div>
              <button
                onClick={() => {
                  setShowCreateModal(false)
                  resetForm()
                }}
                className="p-1.5 rounded-lg hover:bg-brain-800/50 text-brain-400"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Modal body */}
            <div className="p-4 space-y-4">
              {/* Instruction (main field) */}
              <div>
                <label className="block text-sm text-brain-300 mb-1.5">
                  {t('field_instruction_label')}
                </label>
                <textarea
                  value={newInstruction}
                  onChange={(e) => setNewInstruction(e.target.value)}
                  placeholder={t('field_instruction_placeholder')}
                  rows={4}
                  className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50 resize-none"
                />
              </div>

              {/* Name */}
              <div>
                <label className="block text-sm text-brain-300 mb-1.5">
                  {t('field_name_label')} <span className="text-brain-600">{t('field_name_optional')}</span>
                </label>
                <input
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder={t('field_name_placeholder')}
                  className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50"
                />
              </div>

              {/* Schedule type */}
              <div>
                <label className="block text-sm text-brain-300 mb-1.5">{t('field_schedule_type')}</label>
                <div className="flex gap-2">
                  {[
                    { id: 'once' as const, label: t('schedule_option_once'), icon: <Zap className="w-3.5 h-3.5" /> },
                    {
                      id: 'recurring' as const,
                      label: t('schedule_option_recurring'),
                      icon: <Repeat className="w-3.5 h-3.5" />,
                    },
                    {
                      id: 'event' as const,
                      label: t('schedule_option_event'),
                      icon: <Zap className="w-3.5 h-3.5" />,
                    },
                  ].map((opt) => (
                    <button
                      key={opt.id}
                      onClick={() => setNewScheduleType(opt.id)}
                      className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs border transition-colors ${
                        newScheduleType === opt.id
                          ? 'bg-purple-600/20 border-purple-500/50 text-purple-300'
                          : 'bg-brain-800/50 border-brain-700/30 text-brain-400 hover:bg-brain-800'
                      }`}
                    >
                      {opt.icon}
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Schedule details */}
              {newScheduleType === 'once' && (
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">
                    {t('field_execute_at_label')} <span className="text-red-400">*</span>
                  </label>
                  <input
                    type="text"
                    value={newExecuteAt}
                    onChange={(e) => setNewExecuteAt(e.target.value)}
                    placeholder={t('field_execute_at_placeholder')}
                    className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50"
                  />
                  <p className="text-xs text-brain-600 mt-1">{t('execute_at_hint')}</p>
                </div>
              )}
              {newScheduleType === 'recurring' && (
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">
                    {t('field_schedule_label')}
                  </label>
                  <input
                    type="text"
                    value={newScheduleText}
                    onChange={(e) => setNewScheduleText(e.target.value)}
                    placeholder={t('field_schedule_placeholder')}
                    className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50"
                  />
                  <p className="text-xs text-brain-600 mt-1">
                    {t('or_cron')}{' '}
                    <input
                      type="text"
                      value={newCronExpression}
                      onChange={(e) => setNewCronExpression(e.target.value)}
                      placeholder="0 9 * * *"
                      className="bg-brain-800/70 border border-brain-700/20 rounded px-2 py-0.5 text-brain-400 font-mono w-32 focus:outline-none"
                    />
                  </p>
                </div>
              )}

              {newScheduleType === 'event' && (
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">{t('field_event_type')}</label>
                  <select
                    value={newEventType}
                    onChange={(e) => setNewEventType(e.target.value)}
                    className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 focus:outline-none focus:border-purple-500/50"
                  >
                    <option value="">{t('event_placeholder')}</option>
                    <option value="meeting_ended">{t('event_meeting_ended')}</option>
                    <option value="task_overdue">{t('event_task_overdue')}</option>
                    <option value="task_completed">{t('event_task_completed')}</option>
                    <option value="new_meeting_scheduled">{t('event_new_meeting_scheduled')}</option>
                    <option value="document_updated">{t('event_document_updated')}</option>
                    <option value="daily_summary">{t('event_daily_summary')}</option>
                  </select>
                </div>
              )}

              {/* Advanced settings */}
              <div className="border-t border-brain-700/30 pt-3">
                <div className="flex items-center gap-2 text-xs text-brain-500 mb-3">
                  <Settings2 className="w-3.5 h-3.5" />
                  {t('agent_settings_title')}
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-brain-500 mb-1">{t('model_field_label')}</label>
                    <select
                      value={newModelTier}
                      onChange={(e) => setNewModelTier(e.target.value as 'standard' | 'premium')}
                      className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 focus:outline-none focus:border-purple-500/50"
                    >
                      <option value="standard">{t('model_standard')}</option>
                      <option value="premium">{t('model_premium')}</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-brain-500 mb-1">{t('max_steps_field_label')}</label>
                    <input
                      type="number"
                      value={newMaxSteps}
                      onChange={(e) => setNewMaxSteps(Number(e.target.value))}
                      min={1}
                      max={15}
                      className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 focus:outline-none focus:border-purple-500/50"
                    />
                  </div>
                </div>
              </div>
            </div>

            {/* Modal footer */}
            {createError && (
              <div className="mx-4 mb-2 flex items-start gap-2 text-sm text-red-300 bg-red-900/30 border border-red-700/40 rounded-lg px-3 py-2">
                <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                <span>{createError}</span>
              </div>
            )}
            <div className="flex items-center justify-end gap-2 p-4 border-t border-brain-700/30">
              <button
                onClick={() => {
                  setShowCreateModal(false)
                  resetForm()
                }}
                className="px-4 py-2 rounded-lg text-sm text-brain-400 hover:bg-brain-800/50 transition-colors"
              >
                {t('cancel_button')}
              </button>
              <button
                onClick={handleCreate}
                disabled={!newInstruction.trim() || creating}
                className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm bg-purple-600 hover:bg-purple-500 text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {creating ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Send className="w-4 h-4" />
                )}
                {t('submit_button')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
