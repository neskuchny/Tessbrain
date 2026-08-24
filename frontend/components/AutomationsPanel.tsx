'use client'

import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import AutomationWizard from './AutomationWizard'
import {
  Clock,
  Play,
  Pause,
  Trash2,
  RefreshCw,
  Plus,
  Calendar,
  MessageCircle,
  Mail,
  FileText,
  CheckSquare,
  Bot,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  CheckCircle,
  XCircle,
  Loader2,
  Settings,
  History,
  Repeat,
  Timer,
  Edit3,
  X,
  Zap,
  Send,
  Sun,
  BarChart3,
  Bell,
  Layout,
  Brain,
  Search,
  DollarSign,
  Coins,
  RotateCcw,
} from 'lucide-react'

interface Automation {
  id: string
  user_id: string
  name: string
  description?: string
  action_type: string
  action_data: Record<string, any>
  schedule_type: 'once' | 'recurring'
  execute_at?: string
  cron_expression?: string
  interval_seconds?: number
  status: 'active' | 'paused' | 'completed' | 'failed' | 'cancelled' | 'pending'
  last_run_at?: string
  last_run_status?: string
  last_error?: string
  run_count: number
  max_runs?: number
  expires_at?: string
  created_at?: string
  updated_at?: string
  is_legacy?: boolean
  // LLM Usage tracking
  total_tokens_used?: number
  total_cost_usd?: number
  last_run_tokens?: number
  last_run_cost_usd?: number
}

interface Meeting {
  id: string
  title: string
  created_at: string
  project_id?: string
  display: string
}

interface IntegrationContact {
  id: string
  type: string
  value: string
  display_name: string
  display: string
}

interface Integration {
  provider: string
  configured: boolean
  contacts: IntegrationContact[]
  keys: string[]
}

interface Folder {
  id: string
  name: string
  type: 'folder' | 'project'
}

interface ExecutionLog {
  id: string
  automation_id: string
  status: string
  result?: Record<string, any>
  error_message?: string
  started_at: string
  completed_at?: string
  duration_ms?: number
}

interface ActionType {
  id: string
  name: string
  description: string
}

interface WorkflowTemplate {
  id: string
  name: string
  description: string
  category: string
  action_type: string
  default_schedule: 'once' | 'recurring'
  default_cron?: string
  default_interval?: number
  parameters: Record<string, any>
}

const ACTION_ICONS: Record<string, React.ReactNode> = {
  send_telegram: <MessageCircle className="w-4 h-4 text-blue-400" />,
  create_calendar_event: <Calendar className="w-4 h-4 text-green-400" />,
  create_notion_page: <FileText className="w-4 h-4 text-orange-400" />,
  send_email: <Mail className="w-4 h-4 text-purple-400" />,
  create_yougile_task: <CheckSquare className="w-4 h-4 text-yellow-400" />,
  custom_agent_task: <Bot className="w-4 h-4 text-pink-400" />,
  // LLM Processing - NEW
  llm_process: <Brain className="w-4 h-4 text-emerald-400" />,
  llm_extract_and_send: <Search className="w-4 h-4 text-teal-400" />,
  // Meeting workflows
  meeting_summary_telegram: <Send className="w-4 h-4 text-blue-400" />,
  meeting_tasks_yougile: <CheckSquare className="w-4 h-4 text-yellow-400" />,
  meeting_report_email: <Mail className="w-4 h-4 text-purple-400" />,
  meeting_notes_notion: <FileText className="w-4 h-4 text-orange-400" />,
  daily_meetings_digest: <Calendar className="w-4 h-4 text-green-400" />,
  morning_briefing: <Sun className="w-4 h-4 text-amber-400" />,
  weekly_tasks_report: <BarChart3 className="w-4 h-4 text-cyan-400" />,
  weekly_meetings_summary: <Layout className="w-4 h-4 text-indigo-400" />,
  sync_overdue_tasks: <AlertCircle className="w-4 h-4 text-red-400" />,
  deadline_reminder: <Bell className="w-4 h-4 text-rose-400" />,
}

type Tr = (key: string, values?: Record<string, any>) => string

const buildCategoryLabels = (t: (k: string) => string): Record<string, string> => ({
  meeting_processing: t('category_meeting_processing'),
  daily_digest: t('category_daily_digest'),
  weekly_report: t('category_weekly_report'),
  task_sync: t('category_task_sync'),
})

const CATEGORY_COLORS: Record<string, string> = {
  meeting_processing: 'bg-blue-500/10 border-blue-500/30 text-blue-400',
  daily_digest: 'bg-green-500/10 border-green-500/30 text-green-400',
  weekly_report: 'bg-purple-500/10 border-purple-500/30 text-purple-400',
  task_sync: 'bg-yellow-500/10 border-yellow-500/30 text-yellow-400',
}

const STATUS_COLORS: Record<string, string> = {
  active: 'text-green-400 bg-green-400/10 border-green-400/30',
  paused: 'text-yellow-400 bg-yellow-400/10 border-yellow-400/30',
  completed: 'text-blue-400 bg-blue-400/10 border-blue-400/30',
  failed: 'text-red-400 bg-red-400/10 border-red-400/30',
  cancelled: 'text-gray-400 bg-gray-400/10 border-gray-400/30',
  pending: 'text-cyan-400 bg-cyan-400/10 border-cyan-400/30',
}

const STATUS_ICONS: Record<string, React.ReactNode> = {
  active: <Play className="w-3 h-3" />,
  paused: <Pause className="w-3 h-3" />,
  completed: <CheckCircle className="w-3 h-3" />,
  failed: <XCircle className="w-3 h-3" />,
  cancelled: <X className="w-3 h-3" />,
  pending: <Clock className="w-3 h-3" />,
}

interface AutomationsPanelProps {
  userId?: string
}

// Русские подписи булевых параметров шаблонов (вместо «Include Decisions»).
const buildParamLabels = (t: Tr): Record<string, string> => ({
  include_decisions: t('param_include_decisions'),
  include_action_items: t('param_include_action_items'),
  include_tasks: t('param_include_tasks'),
  include_participants: t('param_include_participants'),
  include_next_steps: t('param_include_next_steps'),
  include_summary: t('param_include_summary'),
  include_transcript: t('param_include_transcript'),
  include_kpis: t('param_include_kpis'),
  include_risks: t('param_include_risks'),
  include_completed: t('param_include_completed'),
  include_overdue: t('param_include_overdue'),
  include_upcoming: t('param_include_upcoming'),
  include_context: t('param_include_context'),
  include_meetings: t('param_include_meetings'),
  include_deadlines: t('param_include_deadlines'),
  include_transcript_summary: t('param_include_transcript_summary'),
  include_next_agenda: t('param_include_next_agenda'),
  include_upcoming_meetings: t('param_include_upcoming_meetings'),
  auto_assign: t('param_auto_assign'),
  extract_deadlines: t('param_extract_deadlines'),
})

// cron → человеческий текст. «0 18 * * 5» → «каждую пятницу в 18:00».
function cronToHuman(cron: string | undefined, t: Tr): string {
  if (!cron) return t('cron_on_schedule')
  const p = cron.trim().split(/\s+/)
  if (p.length < 5) return t('cron_on_schedule_expr', { cron })
  const [min, hour, dom, , dow] = p
  const time = (hour !== '*' && min !== '*') ? t('cron_at_time', { time: `${hour.padStart(2, '0')}:${min.padStart(2, '0')}` }) : ''
  // множественное с «по» — грамматически корректно для любого дня
  const days: Record<string, string> = { '0': t('cron_dow_sun'), '1': t('cron_dow_mon'), '2': t('cron_dow_tue'), '3': t('cron_dow_wed'), '4': t('cron_dow_thu'), '5': t('cron_dow_fri'), '6': t('cron_dow_sat'), '7': t('cron_dow_sun') }
  if (dow && dow !== '*') {
    if (dow === '1-5') return t('cron_weekdays_at', { time }).trim()
    const named = dow.split(',').map(d => days[d] || d).join(', ')
    return t('cron_on_named_days', { days: named, time }).trim()
  }
  if (dom && dom !== '*') return t('cron_monthly_day', { day: dom, time }).trim()
  return t('cron_every_day', { time }).trim()
}

// Пресеты расписания: cron собирается под капотом, руками его писать не нужно.
const SCHEDULE_PRESETS: { id: string; labelKey: string }[] = [
  { id: 'daily', labelKey: 'preset_daily' },
  { id: 'weekdays', labelKey: 'preset_weekdays' },
  { id: 'weekly', labelKey: 'preset_weekly' },
  { id: 'monthly', labelKey: 'preset_monthly' },
  { id: 'custom', labelKey: 'preset_custom' },
]

const DOW_OPTIONS: { value: string; labelKey: string }[] = [
  { value: '1', labelKey: 'dow_monday' },
  { value: '2', labelKey: 'dow_tuesday' },
  { value: '3', labelKey: 'dow_wednesday' },
  { value: '4', labelKey: 'dow_thursday' },
  { value: '5', labelKey: 'dow_friday' },
  { value: '6', labelKey: 'dow_saturday' },
  { value: '0', labelKey: 'dow_sunday' },
]

function presetToCron(preset: string, time: string, dow: string): string {
  const [h, m] = (time || '09:00').split(':')
  // не «|| 9»: полночь 00:00 — валидное время, а parseInt('00') falsy
  const hn = parseInt(h, 10)
  const mn = parseInt(m, 10)
  const hh = String(Number.isNaN(hn) ? 9 : hn)
  const mm = String(Number.isNaN(mn) ? 0 : mn)
  if (preset === 'daily') return `${mm} ${hh} * * *`
  if (preset === 'weekdays') return `${mm} ${hh} * * 1-5`
  if (preset === 'weekly') return `${mm} ${hh} * * ${dow || '1'}`
  if (preset === 'monthly') return `${mm} ${hh} 1 * *`
  return ''
}

// «Куда отправить»: каналы = разные action_type. Переключение сохраняет
// набранный текст (message/body/content/text — у каждого канала своё поле).
const MESSAGE_CHANNELS: { action: string; labelKey: string; textField: string }[] = [
  { action: 'send_telegram', labelKey: 'channel_telegram', textField: 'message' },
  { action: 'send_email', labelKey: 'channel_email', textField: 'body' },
  { action: 'send_slack', labelKey: 'channel_slack', textField: 'message' },
  { action: 'create_notion_page', labelKey: 'channel_notion', textField: 'content' },
]

function extractMessageText(data: Record<string, any>): string {
  return data.message || data.body || data.content || data.text || ''
}

// Технические ошибки → понятные пользователю + подсказка что делать.
function humanizeError(err: string, t: Tr): string {
  const e = (err || '').toLowerCase()
  if (e.includes('no recipients') || e.includes('chat_id not provided') || e.includes('recipients specified'))
    return t('error_no_recipient')
  if (e.includes('bot token') || e.includes('token not configured'))
    return t('error_bot_token')
  if (e.includes('gmail not configured') || e.includes('credentials'))
    return t('error_gmail_not_configured')
  if (e.includes('not configured') || e.includes('не настроен'))
    return t('error_integration_not_configured')
  if (e.includes('quota') || e.includes('429'))
    return t('error_quota_exceeded')
  return err
}

// action_data → человекочитаемые строки (без сырого JSON и chat_id).
function describeAction(actionType: string, data: Record<string, any>, t: Tr): { label: string; value: string }[] {
  const d = data || {}
  const lines: { label: string; value: string }[] = []
  const recipient = (v: any) => (v && String(v).trim() ? String(v) : t('describe_default_telegram'))
  switch (actionType) {
    case 'send_telegram':
      if (d.message) lines.push({ label: t('describe_message_label'), value: String(d.message) })
      lines.push({ label: t('describe_where_label'), value: (d.chat_id || d.chat_ids?.length) ? recipient(d.chat_id || (d.chat_ids || []).join(', ')) : t('describe_default_telegram') })
      break
    case 'send_email':
      if (d.subject) lines.push({ label: t('describe_subject_label'), value: String(d.subject) })
      if (d.body) lines.push({ label: t('describe_text_label'), value: String(d.body).slice(0, 200) })
      lines.push({ label: t('describe_to_label'), value: recipient(d.to || (d.recipients || []).join(', ')) })
      break
    case 'send_slack':
      if (d.message || d.text) lines.push({ label: t('describe_message_label'), value: String(d.message || d.text) })
      lines.push({ label: t('describe_where_label'), value: d.channel ? t('describe_slack_channel', { channel: d.channel }) : t('describe_slack_default') })
      break
    case 'create_notion_page':
      if (d.title) lines.push({ label: t('describe_page_label'), value: String(d.title) })
      if (d.content) lines.push({ label: t('content_label'), value: String(d.content).slice(0, 200) })
      lines.push({ label: t('describe_where_label'), value: 'Notion' })
      break
    case 'daily_meetings_digest':
    case 'morning_briefing':
    case 'weekly_meetings_summary':
      lines.push({ label: t('describe_will_send_label'), value: t('describe_digest_to_telegram') })
      if (d.chat_id) lines.push({ label: t('describe_where_label'), value: String(d.chat_id) })
      break
    case 'run_meeting_pipeline': {
      const modeMap: Record<string, string> = {
        deliver: t('mode_deliver'), gate: t('mode_gate'),
        web: t('mode_web'), auto: t('mode_auto'),
      }
      lines.push({ label: t('describe_what_i_do_label'), value: t('describe_pipeline_value') })
      lines.push({ label: t('describe_mode_label'), value: modeMap[d.dispatch_mode || 'deliver'] || t('mode_deliver') })
      if (d.dispatch_mode === 'web') lines.push({ label: t('describe_tool_label'), value: String(d.web_target || t('describe_auto_by_type')) })
      if ((d.dispatch_mode === 'gate' || d.dispatch_mode === 'auto')) lines.push({ label: t('describe_cli_agent_label'), value: String(d.handoff_agent || 'claude') })
      if (Array.isArray(d.task_titles) && d.task_titles.length) lines.push({ label: t('describe_tasks_label'), value: t('describe_tasks_selected', { count: d.task_titles.length }) })
      else lines.push({ label: t('describe_tasks_label'), value: t('describe_tasks_all_up_to', { max: d.max_tasks || 5 }) })
      if (d.assignee_filter) lines.push({ label: t('describe_assignee_label'), value: String(d.assignee_filter) })
      if (d.create_tracker_task && d.tracker) lines.push({ label: t('describe_tracker_label'), value: d.tracker_mark_done ? t('describe_tracker_mark_done', { tracker: d.tracker }) : String(d.tracker) })
      const ch = d.channel || 'telegram'
      const to = ch === 'email' ? d.to_email : (ch === 'slack' ? d.slack_channel : d.chat_id)
      lines.push({ label: t('describe_delivery_label'), value: to ? `${ch}: ${to}` : t('describe_channel_default', { channel: ch }) })
      break
    }
    case 'scheduled_research': {
      const s = d.sources || {}
      const src: string[] = []
      if (s.search_queries?.length) src.push(t('describe_search_count', { count: s.search_queries.length }))
      if (s.urls?.length) src.push(t('describe_sites_count', { count: s.urls.length }))
      if (s.telegram_channels?.length) src.push(t('describe_channels_count', { count: s.telegram_channels.length }))
      lines.push({ label: t('describe_sources_label'), value: src.join(', ') || t('describe_sources_none') })
      if (d.lens) lines.push({ label: t('describe_lens_label'), value: String(d.lens).slice(0, 120) })
      const ch = d.channel || 'telegram'
      const to = ch === 'email' ? d.to_email : (ch === 'slack' ? d.slack_channel : d.chat_id)
      lines.push({ label: t('describe_report_to_label'), value: to ? `${ch}: ${to}` : t('describe_channel_default', { channel: ch }) })
      break
    }
    case 'chat_digest': {
      lines.push({ label: t('describe_source_label'), value: `${d.source || 'slack'}${d.channel ? `: ${d.channel}` : ''}` })
      lines.push({ label: t('describe_period_label'), value: t(d.mode === 'daily_analytics' ? 'describe_period_analytics' : 'describe_period_retrospective', { days: d.window_days || 7 }) })
      break
    }
    default: {
      // fallback: показать понятные пары, скрыть пустые/технические
      for (const [k, v] of Object.entries(d)) {
        if (v === '' || v == null || k === 'model_tier') continue
        lines.push({ label: k, value: typeof v === 'object' ? JSON.stringify(v) : String(v) })
      }
      if (lines.length === 0) lines.push({ label: t('describe_action_label'), value: actionType })
    }
  }
  return lines
}

// Общий блок «модель + доставка» для research/digest-автоматизаций.
function researchDelivery(na: any, setNa: (v: any) => void, t: Tr) {
  const ad = na.action_data || {}
  const ch = ad.channel || 'telegram'
  const setAd = (patch: Record<string, any>) => setNa({ ...na, action_data: { ...ad, ...patch } })
  const recVal = ch === 'email' ? (ad.to_email || '') : (ch === 'slack' ? (ad.slack_channel || '') : (ad.chat_id || ''))
  const recKey = ch === 'email' ? 'to_email' : (ch === 'slack' ? 'slack_channel' : 'chat_id')
  const recPh = ch === 'email' ? t('recipient_email_placeholder') : (ch === 'slack' ? t('recipient_slack_placeholder') : t('recipient_chat_id_placeholder'))
  return (
    <div className="space-y-2">
      <div>
        <label className="block text-xs text-brain-500 mb-1">{t('model_label')}</label>
        <select value={ad.model_tier || 'standard'} onChange={(e) => setAd({ model_tier: e.target.value })}
          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
          <option value="standard">{t('model_fast_option')}</option>
          <option value="premium">{t('model_premium_option')}</option>
        </select>
      </div>
      <div>
        <label className="block text-xs text-brain-500 mb-1">{t('report_delivery_label')}</label>
        <div className="flex gap-2">
          <select value={ch} onChange={(e) => setAd({ channel: e.target.value })}
            className="px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
            <option value="telegram">Telegram</option>
            <option value="email">Email</option>
            <option value="slack">Slack</option>
          </select>
          <input type="text" placeholder={recPh} value={recVal}
            onChange={(e) => setAd({ [recKey]: e.target.value })}
            className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
        </div>
        <p className="text-xs text-brain-500 mt-1">{t('research_delivery_hint')}</p>
      </div>
    </div>
  )
}

export default function AutomationsPanel({ userId }: AutomationsPanelProps) {
  const t = useTranslations('automations')
  const CATEGORY_LABELS = buildCategoryLabels(t)
  const [automations, setAutomations] = useState<Automation[]>([])
  const [actionTypes, setActionTypes] = useState<ActionType[]>([])
  // Колонки/списки выбранного трекера — выбор списком вместо ID.
  const [trackerCols, setTrackerCols] = useState<Record<string, { id: string; name: string; board?: string }[]>>({})
  const loadTrackerCols = useCallback(async (system: string) => {
    if (!system || trackerCols[system]) return
    try {
      const r = await authFetch(`/api/v1/task-analysis/tracker-refs?system=${system}`)
      const d = await r.json()
      setTrackerCols((s) => ({ ...s, [system]: d.columns || [] }))
    } catch { /* ручной ввод остаётся */ }
  }, [trackerCols])
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([])
  const [createError, setCreateError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [logs, setLogs] = useState<Record<string, ExecutionLog[]>>({})
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [showWizard, setShowWizard] = useState(false)
  const [showTemplatesModal, setShowTemplatesModal] = useState(false)
  const [selectedTemplate, setSelectedTemplate] = useState<WorkflowTemplate | null>(null)
  const [filter, setFilter] = useState<'all' | 'active' | 'recurring'>('all')
  const [activeTab, setActiveTab] = useState<'list' | 'templates'>('list')

  // Данные для селекторов
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [integrations, setIntegrations] = useState<Integration[]>([])
  const [folders, setFolders] = useState<Folder[]>([])
  const [loadingMeetings, setLoadingMeetings] = useState(false)
  const [loadingIntegrations, setLoadingIntegrations] = useState(false)
  const [loadingFolders, setLoadingFolders] = useState(false)

  // Дефолтное состояние формы
  const defaultAutomation = {
    name: '',
    description: '',
    action_type: 'send_telegram',
    action_data: {} as Record<string, any>,
    schedule_type: 'once' as 'once' | 'recurring',
    execute_at: '',
    cron_expression: '',
    interval_seconds: 0,
  }

  // Форма создания
  const [newAutomation, setNewAutomation] = useState(defaultAutomation)

  // Для действия «Разбор документа» (run_analysis): методики и документы
  const [analysisPlaybooks, setAnalysisPlaybooks] = useState<Array<{ id: string; name: string }>>([])
  const [analysisDocs, setAnalysisDocs] = useState<Array<{ id: string; title: string }>>([])
  useEffect(() => {
    if (newAutomation.action_type !== 'run_analysis' || analysisPlaybooks.length) return
    ;(async () => {
      try {
        const [p, d] = await Promise.all([
          authFetch('/api/v1/analysis/playbooks'),
          authFetch(`/api/v1/documents/?user_id=${userId || ''}&limit=100`),
        ])
        if (p.ok) { const pd = await p.json(); setAnalysisPlaybooks(pd?.playbooks || []) }
        if (d.ok) { const dd = await d.json(); setAnalysisDocs((dd?.documents || []).map((x: any) => ({ id: String(x.id), title: x.title || x.file_name || x.id }))) }
      } catch { /* ignore */ }
    })()
  }, [newAutomation.action_type, analysisPlaybooks.length, userId])

  // Для действия «Задачи → готовые ТЗ» (run_meeting_pipeline, vibe-tasking):
  // задачи выбранной встречи для галочек. Пусто → отбор идёт критериями
  // (для событийного триггера «после каждой встречи»).
  const [pipelineTasks, setPipelineTasks] = useState<Array<{ task_id: string; title: string; assignee: string }>>([])
  const [loadingPipelineTasks, setLoadingPipelineTasks] = useState(false)
  const pipelineMeetingId = newAutomation.action_data.meeting_id || ''
  useEffect(() => {
    if (newAutomation.action_type !== 'run_meeting_pipeline' || !pipelineMeetingId || !userId) {
      setPipelineTasks([])
      return
    }
    setLoadingPipelineTasks(true)
    ;(async () => {
      try {
        const r = await authFetch(`/api/v1/automations/meeting-tasks?user_id=${userId}&meeting_id=${encodeURIComponent(pipelineMeetingId)}`)
        if (r.ok) {
          const d = await r.json()
          setPipelineTasks((d?.tasks || []).map((x: any) => ({ task_id: String(x.task_id || ''), title: x.title || t('task_fallback'), assignee: x.assignee || '' })))
        } else { setPipelineTasks([]) }
      } catch { setPipelineTasks([]) } finally { setLoadingPipelineTasks(false) }
    })()
  }, [newAutomation.action_type, pipelineMeetingId, userId])

  // Редактирование существующей автоматизации
  const [editingAuto, setEditingAuto] = useState<Automation | null>(null)

  // Человеческое расписание (cron собирается из пресета под капотом)
  const [schedPreset, setSchedPreset] = useState('daily')
  const [schedTime, setSchedTime] = useState('09:00')
  const [schedDow, setSchedDow] = useState('1')

  const applySchedulePreset = (preset: string, time: string, dow: string) => {
    setSchedPreset(preset)
    setSchedTime(time)
    setSchedDow(dow)
    if (preset !== 'custom') {
      setNewAutomation(prev => ({
        ...prev,
        cron_expression: presetToCron(preset, time, dow),
        interval_seconds: 0,
      }))
    }
  }

  // Переключение канала отправки: меняем action_type, текст переносим
  const switchMessageChannel = (targetAction: string) => {
    if (newAutomation.action_type === targetAction) return
    const text = extractMessageText(newAutomation.action_data)
    const target = MESSAGE_CHANNELS.find(c => c.action === targetAction)
    const data: Record<string, any> = text && target ? { [target.textField]: text } : {}
    if (targetAction === 'send_email' && newAutomation.action_data.subject) data.subject = newAutomation.action_data.subject
    if (targetAction === 'create_notion_page' && newAutomation.name) data.title = newAutomation.name
    setNewAutomation({ ...newAutomation, action_type: targetAction, action_data: data })
  }

  // Сброс формы при закрытии модального окна
  const resetAndCloseModal = useCallback(() => {
    setNewAutomation(defaultAutomation)
    setSchedPreset('daily')
    setSchedTime('09:00')
    setSchedDow('1')
    setShowCreateModal(false)
    setCreateError(null)
  }, [])

  const fetchAutomations = useCallback(async () => {
    try {
      setLoading(true)
      const params = new URLSearchParams()
      if (userId) params.append('user_id', userId)
      if (filter === 'active') params.append('status', 'active')
      if (filter === 'recurring') params.append('schedule_type', 'recurring')

      const res = await authFetch(`/api/v1/automations?${params}`)
      if (res.ok) {
        const data = await res.json()
        setAutomations(data.automations || [])
      } else {
        throw new Error('Failed to fetch automations')
      }
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [userId, filter])

  const fetchActionTypes = useCallback(async () => {
    try {
      const res = await authFetch('/api/v1/automations/action-types')
      if (res.ok) {
        const data = await res.json()
        setActionTypes(data.action_types || [])
      }
    } catch (e) {
      console.error('Failed to fetch action types:', e)
    }
  }, [])

  const fetchTemplates = useCallback(async () => {
    try {
      const res = await authFetch('/api/v1/automations/templates')
      if (res.ok) {
        const data = await res.json()
        setTemplates(data || [])
      }
    } catch (e) {
      console.error('Failed to fetch templates:', e)
    }
  }, [])

  const fetchMeetings = useCallback(async () => {
    if (!userId) return
    setLoadingMeetings(true)
    try {
      const res = await authFetch(`/api/v1/automations/meetings-list?user_id=${userId}&limit=100`)
      if (res.ok) {
        const data = await res.json()
        setMeetings(data.meetings || [])
      }
    } catch (e) {
      console.error('Failed to fetch meetings:', e)
    } finally {
      setLoadingMeetings(false)
    }
  }, [userId])

  const fetchIntegrations = useCallback(async () => {
    if (!userId) return
    setLoadingIntegrations(true)
    try {
      const res = await authFetch(`/api/v1/automations/integrations-list?user_id=${userId}`)
      if (res.ok) {
        const data = await res.json()
        setIntegrations(data.integrations || [])
      }
    } catch (e) {
      console.error('Failed to fetch integrations:', e)
    } finally {
      setLoadingIntegrations(false)
    }
  }, [userId])

  const fetchFolders = useCallback(async () => {
    if (!userId) return
    setLoadingFolders(true)
    try {
      // Загружаем папки и проекты параллельно
      const [foldersRes, projectsRes] = await Promise.all([
        fetch(`/api/v1/meetflow/folders?user_id=${userId}`),
        fetch(`/api/v1/meetflow/projects?user_id=${userId}`)
      ])
      
      const allFolders: Folder[] = []
      
      if (foldersRes.ok) {
        const foldersData = await foldersRes.json()
        allFolders.push(...(foldersData.folders || []).map((f: any) => ({ 
          id: f.id, 
          name: f.name, 
          type: 'folder' as const 
        })))
      }
      
      if (projectsRes.ok) {
        const projectsData = await projectsRes.json()
        allFolders.push(...(projectsData.projects || []).map((p: any) => ({ 
          id: p.id, 
          name: p.name, 
          type: 'project' as const 
        })))
      }
      
      setFolders(allFolders)
    } catch (e) {
      console.error('Failed to fetch folders:', e)
    } finally {
      setLoadingFolders(false)
    }
  }, [userId])

  const fetchLogs = async (automationId: string) => {
    try {
      const res = await authFetch(`/api/v1/automations/${automationId}/logs`)
      if (res.ok) {
        const data = await res.json()
        setLogs((prev) => ({ ...prev, [automationId]: data }))
      }
    } catch (e) {
      console.error('Failed to fetch logs:', e)
    }
  }

  useEffect(() => {
    fetchAutomations()
    fetchActionTypes()
    fetchTemplates()
    fetchMeetings()
    fetchIntegrations()
    fetchFolders()
  }, [fetchAutomations, fetchActionTypes, fetchTemplates, fetchMeetings, fetchIntegrations, fetchFolders])

  useEffect(() => {
    const interval = setInterval(fetchAutomations, 30000)
    return () => clearInterval(interval)
  }, [fetchAutomations])

  const handleAction = async (
    automationId: string,
    action: 'pause' | 'resume' | 'cancel' | 'run' | 'delete' | 'rerun' | 'reactivate'
  ) => {
    try {
      let url = `/api/v1/automations/${automationId}`
      let method = 'POST'

      if (action === 'delete') {
        method = 'DELETE'
      } else if (action === 'rerun') {
        // Запустить снова - сначала активируем, потом запускаем
        url += '/rerun'
      } else if (action === 'reactivate') {
        // Просто активировать без запуска
        url += '/reactivate'
      } else {
        url += `/${action}`
      }

      const res = await fetch(url, { method })
      if (res.ok) {
        fetchAutomations()
      }
    } catch (e) {
      console.error(`Failed to ${action} automation:`, e)
    }
  }

  const handleCreate = async () => {
    setCreateError(null)
    try {
      const payload = {
        ...newAutomation,
        user_id: userId || 'default_user',
        action_data: newAutomation.action_data,
      }

      const res = await authFetch('/api/v1/automations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      if (res.ok) {
        resetAndCloseModal()
        fetchAutomations()
      } else {
        const data = await res.json().catch(() => ({}))
        setCreateError(data?.detail || data?.message || t('create_failed_http', { status: res.status }))
      }
    } catch (e) {
      setCreateError(t('network_error'))
    }
  }

  const handleCreateFromTemplate = async (template: WorkflowTemplate, params: Record<string, any>) => {
    try {
      // apply_to / name / project_id / folder_id бэкенд ждёт на ВЕРХНЕМ
      // уровне (раньше клались внутрь parameters и терялись — «Ко всем
      // новым встречам» и своё название не доезжали)
      const { apply_to, name, project_id, folder_id, ...actionParams } = params
      const payload = {
        template_id: template.id,
        user_id: userId || 'default_user',
        parameters: actionParams,
        apply_to,
        project_id,
        folder_id,
        name: name || template.name,
        description: template.description,
        cron_expression: template.default_cron,
        interval_seconds: template.default_interval,
      }

      const res = await authFetch('/api/v1/automations/from-template', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      if (res.ok) {
        setShowTemplatesModal(false)
        setSelectedTemplate(null)
        fetchAutomations()
        setActiveTab('list')
      } else {
        const data = await res.json().catch(() => ({}))
        setCreateError(data?.detail || data?.message || t('create_from_template_failed_http', { status: res.status }))
      }
    } catch (e) {
      setCreateError(t('network_error'))
    }
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
            <Clock className="w-5 h-5 text-brain-400" />
            <h2 className="text-lg font-semibold text-brain-100">{t('title')}</h2>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={fetchAutomations}
              className="p-2 rounded-lg bg-brain-800/50 hover:bg-brain-700/50 text-brain-400 hover:text-brain-200 transition-colors"
              title={t('refresh_title')}
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={() => setShowWizard(true)}
              className="flex items-center gap-2 px-3 py-2 rounded-lg bg-brain-600 hover:bg-brain-500 text-white transition-colors"
            >
              <Plus className="w-4 h-4" />
              <span className="text-sm">{t('create_button')}</span>
            </button>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex gap-2 mb-4">
          <button
            onClick={() => setActiveTab('list')}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              activeTab === 'list'
                ? 'bg-brain-600 text-white'
                : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
            }`}
          >
            <History className="w-4 h-4" />
            {t('tab_my')}
          </button>
          <button
            onClick={() => setActiveTab('templates')}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              activeTab === 'templates'
                ? 'bg-gradient-to-r from-purple-600 to-pink-600 text-white'
                : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
            }`}
          >
            <Zap className="w-4 h-4" />
            {t('tab_templates')}
            <span className="px-1.5 py-0.5 rounded-full bg-white/20 text-[10px]">{templates.length}</span>
          </button>
        </div>

        {/* Stats - only for list tab */}
        {activeTab === 'list' && (
          <>
            <div className="grid grid-cols-4 gap-2 text-xs">
              <div className="p-2 rounded-lg bg-brain-800/30 text-center">
                <div className="text-brain-400">{t('stats_total')}</div>
                <div className="text-lg font-bold text-brain-100">{stats.total}</div>
              </div>
              <div className="p-2 rounded-lg bg-green-500/10 text-center">
                <div className="text-green-400">{t('stats_active')}</div>
                <div className="text-lg font-bold text-green-300">{stats.active}</div>
              </div>
              <div className="p-2 rounded-lg bg-purple-500/10 text-center">
                <div className="text-purple-400">{t('stats_recurring')}</div>
                <div className="text-lg font-bold text-purple-300">{stats.recurring}</div>
              </div>
              <div className="p-2 rounded-lg bg-blue-500/10 text-center">
                <div className="text-blue-400">{t('stats_completed')}</div>
                <div className="text-lg font-bold text-blue-300">{stats.completed}</div>
              </div>
            </div>

            {/* Filters */}
            <div className="flex gap-2 mt-4">
              {(['all', 'active', 'recurring'] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                    filter === f
                      ? 'bg-brain-600 text-white'
                      : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
                  }`}
                >
                  {f === 'all' ? t('filter_all') : f === 'active' ? t('filter_active') : t('filter_recurring')}
                </button>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {/* Templates Tab */}
        {activeTab === 'templates' && (
          <div className="space-y-6">
            {Object.entries(CATEGORY_LABELS).map(([category, label]) => {
              const categoryTemplates = templates.filter((t) => t.category === category)
              if (categoryTemplates.length === 0) return null
              
              return (
                <div key={category}>
                  <h3 className={`text-sm font-semibold mb-3 px-2 py-1 rounded-lg inline-block ${CATEGORY_COLORS[category] || 'text-brain-400'}`}>
                    {label}
                  </h3>
                  <div className="grid gap-3">
                    {categoryTemplates.map((template) => (
                      <div
                        key={template.id}
                        className="rounded-xl border border-brain-700/30 bg-brain-900/50 p-4 hover:bg-brain-800/50 transition-colors"
                      >
                        <div className="flex items-start justify-between">
                          <div className="flex items-start gap-3">
                            <div className="mt-1 p-2 rounded-lg bg-brain-800/50">
                              {ACTION_ICONS[template.action_type] || <Bot className="w-4 h-4" />}
                            </div>
                            <div>
                              <div className="flex items-center gap-2">
                                <span className="font-medium text-brain-100">{template.name}</span>
                                {template.default_schedule === 'recurring' && (
                                  <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-purple-500/10 text-purple-400 border border-purple-500/30 flex items-center gap-1">
                                    <Repeat className="w-3 h-3" />
                                    {t('recurring_badge')}
                                  </span>
                                )}
                              </div>
                              <p className="text-xs text-brain-400 mt-1">{template.description}</p>
                              {(template.default_cron || template.category === 'meeting_processing') && (
                                <p className="text-[10px] text-brain-500 mt-2 flex items-center gap-1">
                                  <Clock className="w-3 h-3" />
                                  {template.category === 'meeting_processing'
                                    ? t('after_each_new_meeting')
                                    : cronToHuman(template.default_cron, t)}
                                </p>
                              )}
                            </div>
                          </div>
                          <button
                            onClick={() => {
                              setSelectedTemplate(template)
                              setShowTemplatesModal(true)
                            }}
                            className="flex items-center gap-2 px-3 py-2 rounded-lg bg-gradient-to-r from-purple-600 to-pink-600 hover:from-purple-500 hover:to-pink-500 text-white text-xs font-medium transition-all"
                          >
                            <Zap className="w-3 h-3" />
                            {t('use_template')}
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {/* Automations List Tab */}
        {activeTab === 'list' && (loading && automations.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-brain-400">
            <Loader2 className="w-6 h-6 animate-spin mr-2" />
            {t('loading')}
          </div>
        ) : error ? (
          <div className="flex items-center justify-center h-32 text-red-400">
            <AlertCircle className="w-5 h-5 mr-2" />
            {error}
          </div>
        ) : automations.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-10 px-6 text-center max-w-md mx-auto">
            <div className="w-14 h-14 rounded-2xl bg-blue-500/10 border border-blue-500/20 flex items-center justify-center mb-4">
              <Clock className="w-7 h-7 text-blue-400" />
            </div>
            <p className="text-brain-100 font-medium text-base">{t('empty_what_is_title')}</p>
            <p className="text-sm text-brain-400 mt-2 leading-relaxed">
              {t('empty_what_is_description')}
            </p>
            <button
              onClick={() => setShowWizard(true)}
              className="mt-5 flex items-center gap-2 px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors"
            >
              <Zap className="w-4 h-4" />
              {t('create_first_automation')}
            </button>
            <button
              onClick={() => setActiveTab('templates')}
              className="mt-2 hidden text-xs text-brain-500"
            >
              <Zap className="w-4 h-4" />
              {t('view_templates')}
            </button>
          </div>
        ) : (
          automations.map((auto) => (
            <div
              key={auto.id}
              className="rounded-xl border border-brain-700/30 bg-brain-900/50 overflow-hidden"
            >
              {/* Main Row */}
              <div
                className="p-4 cursor-pointer hover:bg-brain-800/30 transition-colors"
                onClick={() => toggleExpand(auto.id)}
              >
                <div className="flex items-start justify-between">
                  <div className="flex items-start gap-3">
                    <div className="mt-1">{ACTION_ICONS[auto.action_type] || <Bot className="w-4 h-4" />}</div>
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-brain-100">{auto.name}</span>
                        {auto.is_legacy && (
                          <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-orange-500/10 text-orange-400 border border-orange-500/30">
                            Legacy
                          </span>
                        )}
                        <span
                          className={`px-2 py-0.5 rounded-full text-[10px] font-medium border flex items-center gap-1 ${
                            STATUS_COLORS[auto.status] || STATUS_COLORS.active
                          }`}
                        >
                          {STATUS_ICONS[auto.status] || STATUS_ICONS.active}
                          {auto.status === 'active'
                            ? t('status_active')
                            : auto.status === 'paused'
                            ? t('status_paused')
                            : auto.status === 'completed'
                            ? t('status_completed')
                            : auto.status === 'failed'
                            ? t('status_failed')
                            : auto.status === 'pending'
                            ? t('status_pending')
                            : t('status_cancelled')}
                        </span>
                        {auto.schedule_type === 'recurring' && (
                          <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-purple-500/10 text-purple-400 border border-purple-500/30 flex items-center gap-1">
                            <Repeat className="w-3 h-3" />
                            {t('recurring_badge')}
                          </span>
                        )}
                      </div>
                      {auto.description && (
                        <p className="text-xs text-brain-400 mt-1">{auto.description}</p>
                      )}
                      <div className="flex items-center gap-4 mt-2 text-xs text-brain-500">
                        {auto.execute_at && (
                          <span className="flex items-center gap-1">
                            <Timer className="w-3 h-3" />
                            {formatDate(auto.execute_at)}
                          </span>
                        )}
                        {auto.cron_expression && (
                          <span className="flex items-center gap-1">
                            <Repeat className="w-3 h-3" />
                            {auto.cron_expression}
                          </span>
                        )}
                        {auto.interval_seconds && (
                          <span className="flex items-center gap-1">
                            <RefreshCw className="w-3 h-3" />
                            {t('every_seconds', { seconds: auto.interval_seconds })}
                          </span>
                        )}
                        <span className="flex items-center gap-1">
                          <History className="w-3 h-3" />
                          {t('runs_count', { count: auto.run_count })}
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {expandedId === auto.id ? (
                      <ChevronUp className="w-4 h-4 text-brain-400" />
                    ) : (
                      <ChevronDown className="w-4 h-4 text-brain-400" />
                    )}
                  </div>
                </div>
              </div>

              {/* Expanded Details */}
              {expandedId === auto.id && (
                <div className="border-t border-brain-700/30 p-4 bg-brain-800/20">
                  {/* Actions */}
                  <div className="flex flex-wrap gap-2 mb-4">
                    {auto.status === 'active' && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          handleAction(auto.id, 'pause')
                        }}
                        className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-yellow-500/10 text-yellow-400 hover:bg-yellow-500/20 text-xs transition-colors"
                      >
                        <Pause className="w-3 h-3" />
                        {t('action_pause')}
                      </button>
                    )}
                    {auto.status === 'paused' && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          handleAction(auto.id, 'resume')
                        }}
                        className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-green-500/10 text-green-400 hover:bg-green-500/20 text-xs transition-colors"
                      >
                        <Play className="w-3 h-3" />
                        {t('action_resume')}
                      </button>
                    )}
                    {(auto.status === 'active' || auto.status === 'paused') && (
                      <>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            handleAction(auto.id, 'run')
                          }}
                          className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 text-xs transition-colors"
                        >
                          <Play className="w-3 h-3" />
                          {t('action_run_now')}
                        </button>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            handleAction(auto.id, 'cancel')
                          }}
                          className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-orange-500/10 text-orange-400 hover:bg-orange-500/20 text-xs transition-colors"
                        >
                          <XCircle className="w-3 h-3" />
                          {t('action_cancel')}
                        </button>
                      </>
                    )}
                    {(auto.status === 'completed' || auto.status === 'failed' || auto.status === 'cancelled') && (
                      <>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            handleAction(auto.id, 'rerun')
                          }}
                          className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-green-500/10 text-green-400 hover:bg-green-500/20 text-xs transition-colors"
                        >
                          <RefreshCw className="w-3 h-3" />
                          {t('action_rerun')}
                        </button>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            handleAction(auto.id, 'reactivate')
                          }}
                          className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 text-xs transition-colors"
                        >
                          <RotateCcw className="w-3 h-3" />
                          {t('action_activate')}
                        </button>
                      </>
                    )}
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        setEditingAuto(auto)
                      }}
                      className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-brain-700/40 text-brain-200 hover:bg-brain-700 text-xs transition-colors"
                    >
                      <Edit3 className="w-3 h-3" />
                      {t('action_edit')}
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        if (confirm(t('confirm_delete'))) {
                          handleAction(auto.id, 'delete')
                        }
                      }}
                      className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 hover:bg-red-500/20 text-xs transition-colors"
                    >
                      <Trash2 className="w-3 h-3" />
                      {t('action_delete')}
                    </button>
                  </div>

                  {/* LLM Usage Stats */}
                  {(auto.total_tokens_used || auto.total_cost_usd) && (
                    <div className="mb-4 p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30">
                      <div className="text-xs text-emerald-400 font-medium mb-2 flex items-center gap-2">
                        <Coins className="w-3 h-3" />
                        {t('llm_cost_title')}
                      </div>
                      <div className="grid grid-cols-2 gap-4 text-xs">
                        <div>
                          <span className="text-brain-400">{t('total_tokens')}</span>
                          <span className="ml-2 text-brain-200 font-mono">{auto.total_tokens_used?.toLocaleString() || 0}</span>
                        </div>
                        <div>
                          <span className="text-brain-400">{t('total_spent')}</span>
                          <span className="ml-2 text-emerald-300 font-mono">${auto.total_cost_usd?.toFixed(4) || '0.0000'}</span>
                        </div>
                        {auto.last_run_tokens ? (
                          <>
                            <div>
                              <span className="text-brain-500">{t('last_run_label')}</span>
                              <span className="ml-2 text-brain-300 font-mono">{t('last_run_tokens', { value: auto.last_run_tokens?.toLocaleString() ?? '0' })}</span>
                            </div>
                            <div>
                              <span className="text-brain-500">{t('cost_label')}</span>
                              <span className="ml-2 text-brain-300 font-mono">${auto.last_run_cost_usd?.toFixed(4)}</span>
                            </div>
                          </>
                        ) : null}
                      </div>
                    </div>
                  )}

                  {/* Action Data — по-человечески, без сырого JSON */}
                  <div className="mb-4">
                    <div className="text-xs text-brain-400 mb-2 font-medium">{t('what_it_does')}</div>
                    <div className="text-sm bg-brain-900/50 rounded-lg p-3 text-brain-200 space-y-1">
                      {describeAction(auto.action_type, auto.action_data, t).map((line, i) => (
                        <div key={i} className="flex gap-2">
                          <span className="text-brain-500 shrink-0">{line.label}:</span>
                          <span>{line.value}</span>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Last Error — по-человечески */}
                  {auto.last_error && (
                    <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30">
                      <div className="text-xs text-red-400 font-medium mb-1">{t('what_went_wrong')}</div>
                      <div className="text-xs text-red-300">{humanizeError(auto.last_error, t)}</div>
                    </div>
                  )}

                  {/* Execution Logs */}
                  <div>
                    <div className="text-xs text-brain-400 mb-2 font-medium flex items-center gap-2">
                      <History className="w-3 h-3" />
                      {t('execution_history')}
                    </div>
                    {logs[auto.id] && logs[auto.id].length > 0 ? (
                      <div className="space-y-2">
                        {logs[auto.id].slice(0, 5).map((log) => (
                          <div
                            key={log.id}
                            className={`flex items-center justify-between p-2 rounded-lg text-xs ${
                              log.status === 'success'
                                ? 'bg-green-500/10 border border-green-500/20'
                                : 'bg-red-500/10 border border-red-500/20'
                            }`}
                          >
                            <div className="flex items-center gap-2">
                              {log.status === 'success' ? (
                                <CheckCircle className="w-3 h-3 text-green-400" />
                              ) : (
                                <XCircle className="w-3 h-3 text-red-400" />
                              )}
                              <span className={log.status === 'success' ? 'text-green-300' : 'text-red-300'}>
                                {log.status === 'success' ? t('log_success') : t('status_failed')}
                              </span>
                              {log.error_message && (
                                <span className="text-red-400 truncate max-w-[240px]" title={humanizeError(log.error_message, t)}>
                                  {humanizeError(log.error_message, t)}
                                </span>
                              )}
                            </div>
                            <div className="flex items-center gap-4 text-brain-500">
                              <span>{formatDuration(log.duration_ms)}</span>
                              <span>{formatDate(log.started_at)}</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="text-xs text-brain-500 text-center py-4">
                        {t('no_execution_records')}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          ))
        ))}
      </div>

      {/* Template Config Modal */}
      {showTemplatesModal && selectedTemplate && (
        <TemplateConfigModal
          template={selectedTemplate}
          onClose={() => {
            setShowTemplatesModal(false)
            setSelectedTemplate(null)
          }}
          onSubmit={(params) => handleCreateFromTemplate(selectedTemplate, params)}
          meetings={meetings}
          integrations={integrations}
        />
      )}

      {/* Create Modal */}
      {showWizard && (
        <AutomationWizard
          userId={userId}
          onClose={() => setShowWizard(false)}
          onCreated={() => { setShowWizard(false); fetchAutomations() }}
          onAdvanced={() => { setShowWizard(false); setShowCreateModal(true) }}
        />
      )}

      {/* Edit Modal */}
      {editingAuto && (
        <EditAutomationModal
          automation={editingAuto}
          onClose={() => setEditingAuto(null)}
          onSaved={() => { setEditingAuto(null); fetchAutomations() }}
        />
      )}

      {showCreateModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-brain-900 rounded-xl border border-brain-700/50 w-full max-w-lg max-h-[90vh] overflow-y-auto m-4">
            <div className="p-4 border-b border-brain-700/30 flex items-center justify-between">
              <h3 className="text-lg font-semibold text-brain-100">{t('modal_title')}</h3>
              <button
                onClick={resetAndCloseModal}
                className="p-2 rounded-lg hover:bg-brain-800 text-brain-400"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-4 space-y-4">
              {/* Name */}
              <div>
                <label className="block text-xs text-brain-400 mb-1">{t('field_name')}</label>
                <input
                  type="text"
                  value={newAutomation.name}
                  onChange={(e) => setNewAutomation({ ...newAutomation, name: e.target.value })}
                  className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500"
                  placeholder={t('field_name_placeholder')}
                />
              </div>

              {/* Description */}
              <div>
                <label className="block text-xs text-brain-400 mb-1">{t('field_description_optional')}</label>
                <textarea
                  value={newAutomation.description}
                  onChange={(e) => setNewAutomation({ ...newAutomation, description: e.target.value })}
                  className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500 resize-none"
                  rows={2}
                  placeholder={t('field_description_placeholder')}
                />
              </div>

              {/* Action Type */}
              <div>
                <label className="block text-xs text-brain-400 mb-1">{t('field_action_type')}</label>
                <select
                  value={newAutomation.action_type}
                  onChange={(e) => setNewAutomation({ ...newAutomation, action_type: e.target.value, action_data: {} })}
                  className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500"
                >
                  {actionTypes.map((at) => (
                    <option key={at.id} value={at.id}>
                      {at.name} - {at.description}
                    </option>
                  ))}
                </select>
              </div>

              {/* Action Data - Dynamic based on type */}
              <div>
                <label className="block text-xs text-brain-400 mb-1">{t('field_action_params')}</label>

                {/* Куда отправить — переключение канала без потери текста */}
                {MESSAGE_CHANNELS.some(c => c.action === newAutomation.action_type) && (
                  <div className="mb-3">
                    <div className="text-xs text-brain-500 mb-1.5">{t('where_send_label')}</div>
                    <div className="flex flex-wrap gap-2">
                      {MESSAGE_CHANNELS.map(ch => (
                        <button
                          key={ch.action}
                          type="button"
                          onClick={() => switchMessageChannel(ch.action)}
                          className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                            newAutomation.action_type === ch.action
                              ? 'bg-brain-600 text-white'
                              : 'bg-brain-800 text-brain-400 hover:bg-brain-700'
                          }`}
                        >
                          {t(ch.labelKey)}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {newAutomation.action_type === 'send_telegram' && (
                  <div className="space-y-3">
                    <textarea
                      placeholder={t('msg_text_placeholder')}
                      value={newAutomation.action_data.message || ''}
                      onChange={(e) =>
                        setNewAutomation({
                          ...newAutomation,
                          action_data: { ...newAutomation.action_data, message: e.target.value },
                        })
                      }
                      className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      rows={3}
                    />
                    <div className="space-y-2">
                      <label className="block text-xs text-brain-500 mb-1">
                        {t('telegram_where_to_send')} {(newAutomation.action_data.chat_ids?.length || 0) > 0 && 
                          <span className="text-blue-400">{t('selected_count', { count: newAutomation.action_data.chat_ids?.length })}</span>
                        }
                      </label>
                      {integrations.find(i => i.provider === 'telegram')?.contacts.length ? (
                        <div className="space-y-2">
                          {/* Multi-select checkboxes */}
                          <div className="max-h-32 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2">
                            {integrations.find(i => i.provider === 'telegram')?.contacts.map(c => (
                              <label key={c.id} className="flex items-center gap-2 p-1.5 hover:bg-brain-700 rounded cursor-pointer">
                                <input
                                  type="checkbox"
                                  checked={(newAutomation.action_data.chat_ids || []).includes(c.value)}
                                  onChange={(e) => {
                                    const currentIds = newAutomation.action_data.chat_ids || []
                                    const newIds = e.target.checked
                                      ? [...currentIds, c.value]
                                      : currentIds.filter((id: string) => id !== c.value)
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        chat_ids: newIds,
                                        chat_id: newIds[0] || ''
                                      },
                                    })
                                  }}
                                  className="rounded border-brain-600 bg-brain-700 text-blue-500 focus:ring-blue-500"
                                />
                                <span className="text-sm text-brain-200">{c.display}</span>
                              </label>
                            ))}
                          </div>
                          {/* Manual input */}
                          <div className="flex gap-2">
                            <input
                              type="text"
                              placeholder={t('add_chat_id_manual')}
                              className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') {
                                  e.preventDefault()
                                  const input = e.currentTarget
                                  const value = input.value.trim()
                                  if (value && !(newAutomation.action_data.chat_ids || []).includes(value)) {
                                    const currentIds = newAutomation.action_data.chat_ids || []
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        chat_ids: [...currentIds, value],
                                        chat_id: currentIds[0] || value
                                      },
                                    })
                                    input.value = ''
                                  }
                                }
                              }}
                            />
                            <button
                              type="button"
                              onClick={(e) => {
                                const input = (e.currentTarget.previousElementSibling as HTMLInputElement)
                                const value = input.value.trim()
                                if (value && !(newAutomation.action_data.chat_ids || []).includes(value)) {
                                  const currentIds = newAutomation.action_data.chat_ids || []
                                  setNewAutomation({
                                    ...newAutomation,
                                    action_data: { 
                                      ...newAutomation.action_data, 
                                      chat_ids: [...currentIds, value],
                                      chat_id: currentIds[0] || value
                                    },
                                  })
                                  input.value = ''
                                }
                              }}
                              className="px-3 py-2 rounded-lg bg-blue-600 text-white text-sm hover:bg-blue-700"
                            >
                              <Plus className="w-4 h-4" />
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="space-y-2">
                          <input
                            type="text"
                            placeholder={t('chat_ids_comma')}
                            value={(newAutomation.action_data.chat_ids || []).join(', ') || newAutomation.action_data.chat_id || ''}
                            onChange={(e) => {
                              const ids = e.target.value.split(',').map(s => s.trim()).filter(Boolean)
                              setNewAutomation({
                                ...newAutomation,
                                action_data: { 
                                  ...newAutomation.action_data, 
                                  chat_ids: ids,
                                  chat_id: ids[0] || ''
                                },
                              })
                            }}
                            className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          />
                          <p className="text-[10px] text-brain-500">{t('chat_ids_hint')}</p>
                        </div>
                      )}
                      {/* Show selected as tags */}
                      {(newAutomation.action_data.chat_ids?.length || 0) > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {newAutomation.action_data.chat_ids.map((chatId: string, idx: number) => {
                            const contact = integrations.find(i => i.provider === 'telegram')?.contacts.find(c => c.value === chatId)
                            return (
                              <span key={idx} className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-blue-500/20 text-blue-300 text-xs">
                                {contact?.display || chatId}
                                <button
                                  type="button"
                                  onClick={() => {
                                    const newIds = newAutomation.action_data.chat_ids.filter((_: string, i: number) => i !== idx)
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        chat_ids: newIds,
                                        chat_id: newIds[0] || ''
                                      },
                                    })
                                  }}
                                  className="hover:text-red-400"
                                >
                                  <X className="w-3 h-3" />
                                </button>
                              </span>
                            )
                          })}
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {newAutomation.action_type === 'send_email' && (
                  <div className="space-y-3">
                    <div className="space-y-2">
                      <label className="block text-xs text-brain-500 mb-1">
                        {t('email_to_label')} {(newAutomation.action_data.to_emails?.length || 0) > 0 && 
                          <span className="text-purple-400">{t('selected_count', { count: newAutomation.action_data.to_emails?.length })}</span>
                        }
                      </label>
                      {integrations.find(i => i.provider === 'gmail')?.contacts.length ? (
                        <div className="space-y-2">
                          {/* Multi-select checkboxes */}
                          <div className="max-h-32 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2">
                            {integrations.find(i => i.provider === 'gmail')?.contacts.map(c => (
                              <label key={c.id} className="flex items-center gap-2 p-1.5 hover:bg-brain-700 rounded cursor-pointer">
                                <input
                                  type="checkbox"
                                  checked={(newAutomation.action_data.to_emails || []).includes(c.value)}
                                  onChange={(e) => {
                                    const currentEmails = newAutomation.action_data.to_emails || []
                                    const newEmails = e.target.checked
                                      ? [...currentEmails, c.value]
                                      : currentEmails.filter((email: string) => email !== c.value)
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        to_emails: newEmails,
                                        to: newEmails[0] || ''
                                      },
                                    })
                                  }}
                                  className="rounded border-brain-600 bg-brain-700 text-purple-500 focus:ring-purple-500"
                                />
                                <span className="text-sm text-brain-200">{c.display}</span>
                              </label>
                            ))}
                          </div>
                          {/* Manual input */}
                          <div className="flex gap-2">
                            <input
                              type="email"
                              placeholder={t('add_email_manual')}
                              className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') {
                                  e.preventDefault()
                                  const input = e.currentTarget
                                  const value = input.value.trim()
                                  if (value && value.includes('@') && !(newAutomation.action_data.to_emails || []).includes(value)) {
                                    const currentEmails = newAutomation.action_data.to_emails || []
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        to_emails: [...currentEmails, value],
                                        to: currentEmails[0] || value
                                      },
                                    })
                                    input.value = ''
                                  }
                                }
                              }}
                            />
                            <button
                              type="button"
                              onClick={(e) => {
                                const input = (e.currentTarget.previousElementSibling as HTMLInputElement)
                                const value = input.value.trim()
                                if (value && value.includes('@') && !(newAutomation.action_data.to_emails || []).includes(value)) {
                                  const currentEmails = newAutomation.action_data.to_emails || []
                                  setNewAutomation({
                                    ...newAutomation,
                                    action_data: { 
                                      ...newAutomation.action_data, 
                                      to_emails: [...currentEmails, value],
                                      to: currentEmails[0] || value
                                    },
                                  })
                                  input.value = ''
                                }
                              }}
                              className="px-3 py-2 rounded-lg bg-purple-600 text-white text-sm hover:bg-purple-700"
                            >
                              <Plus className="w-4 h-4" />
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="space-y-2">
                          <input
                            type="text"
                            placeholder={t('emails_comma')}
                            value={(newAutomation.action_data.to_emails || []).join(', ') || newAutomation.action_data.to || ''}
                            onChange={(e) => {
                              const emails = e.target.value.split(',').map(s => s.trim()).filter(Boolean)
                              setNewAutomation({
                                ...newAutomation,
                                action_data: { 
                                  ...newAutomation.action_data, 
                                  to_emails: emails,
                                  to: emails[0] || ''
                                },
                              })
                            }}
                            className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          />
                          <p className="text-[10px] text-brain-500">{t('emails_hint')}</p>
                        </div>
                      )}
                      {/* Show selected as tags */}
                      {(newAutomation.action_data.to_emails?.length || 0) > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {newAutomation.action_data.to_emails.map((email: string, idx: number) => (
                            <span key={idx} className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-purple-500/20 text-purple-300 text-xs">
                              {email}
                              <button
                                type="button"
                                onClick={() => {
                                  const newEmails = newAutomation.action_data.to_emails.filter((_: string, i: number) => i !== idx)
                                  setNewAutomation({
                                    ...newAutomation,
                                    action_data: { 
                                      ...newAutomation.action_data, 
                                      to_emails: newEmails,
                                      to: newEmails[0] || ''
                                    },
                                  })
                                }}
                                className="hover:text-red-400"
                              >
                                <X className="w-3 h-3" />
                              </button>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    <input
                      type="text"
                      placeholder={t('email_subject_placeholder')}
                      value={newAutomation.action_data.subject || ''}
                      onChange={(e) =>
                        setNewAutomation({
                          ...newAutomation,
                          action_data: { ...newAutomation.action_data, subject: e.target.value },
                        })
                      }
                      className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                    />
                    <textarea
                      placeholder={t('email_body_placeholder')}
                      value={newAutomation.action_data.body || ''}
                      onChange={(e) =>
                        setNewAutomation({
                          ...newAutomation,
                          action_data: { ...newAutomation.action_data, body: e.target.value },
                        })
                      }
                      className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      rows={4}
                    />
                  </div>
                )}

                {newAutomation.action_type === 'create_calendar_event' && (
                  <div className="space-y-2">
                    <input
                      type="text"
                      placeholder={t('event_title_placeholder')}
                      value={newAutomation.action_data.title || ''}
                      onChange={(e) =>
                        setNewAutomation({
                          ...newAutomation,
                          action_data: { ...newAutomation.action_data, title: e.target.value },
                        })
                      }
                      className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                    />
                    <input
                      type="text"
                      placeholder={t('event_start_placeholder')}
                      value={newAutomation.action_data.start_time || ''}
                      onChange={(e) =>
                        setNewAutomation({
                          ...newAutomation,
                          action_data: { ...newAutomation.action_data, start_time: e.target.value },
                        })
                      }
                      className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                    />
                  </div>
                )}

                {newAutomation.action_type === 'custom_agent_task' && (
                  <textarea
                    placeholder={t('agent_command_placeholder')}
                    value={newAutomation.action_data.message || ''}
                    onChange={(e) =>
                      setNewAutomation({
                        ...newAutomation,
                        action_data: { ...newAutomation.action_data, message: e.target.value },
                      })
                    }
                    className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm resize-none"
                    rows={3}
                  />
                )}

                {/* LLM Processing */}
                {newAutomation.action_type === 'llm_process' && (
                  <div className="space-y-3">
                    <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30">
                      <div className="flex items-center gap-2 text-emerald-400 text-sm font-medium mb-1">
                        <Brain className="w-4 h-4" />
                        {t('llm_process_title')}
                      </div>
                      <p className="text-xs text-brain-400">
                        {t('llm_process_description')}
                      </p>
                    </div>
                    
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('data_source_label')}</label>
                      <select
                        value={newAutomation.action_data.source_type || 'meeting'}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, source_type: e.target.value, source_id: '' },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      >
                        <option value="meeting">{t('data_source_meeting')}</option>
                        <option value="text">{t('data_source_text')}</option>
                      </select>
                    </div>

                    {newAutomation.action_data.source_type === 'meeting' && (
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('select_meeting_label')}</label>
                        <select
                          value={newAutomation.action_data.source_id || ''}
                          onChange={(e) =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, source_id: e.target.value },
                            })
                          }
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                        >
                          <option value="">{t('select_meeting_placeholder')}</option>
                          {meetings.map(m => (
                            <option key={m.id} value={m.id}>{m.display}</option>
                          ))}
                        </select>
                        {loadingMeetings && <p className="text-xs text-brain-500 mt-1">{t('loading_meetings')}</p>}
                      </div>
                    )}

                    {newAutomation.action_data.source_type === 'text' && (
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('process_text_label')}</label>
                        <textarea
                          placeholder={t('process_text_placeholder')}
                          value={newAutomation.action_data.source_text || ''}
                          onChange={(e) =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, source_text: e.target.value },
                            })
                          }
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          rows={4}
                        />
                      </div>
                    )}

                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('prompt_label')}</label>
                      <textarea
                        placeholder={t('prompt_placeholder')}
                        value={newAutomation.action_data.prompt || ''}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, prompt: e.target.value },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                        rows={3}
                      />
                    </div>

                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('where_send_result')}</label>
                      <select
                        value={newAutomation.action_data.output_type || 'telegram'}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, output_type: e.target.value, output_target: '' },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      >
                        <option value="telegram">Telegram</option>
                        <option value="email">Email</option>
                        <option value="notion">Notion</option>
                        <option value="yougile">YouGile</option>
                      </select>
                    </div>

                    {/* Recipients - Multiple selection for llm_process */}
                    <div className="space-y-2">
                      <label className="block text-xs text-brain-500 mb-1">
                        {t('recipients_label')} {(newAutomation.action_data.output_targets?.length || 0) > 0 && 
                          <span className="text-violet-400">{t('selected_count', { count: newAutomation.action_data.output_targets?.length })}</span>
                        }
                      </label>
                      
                      {/* Telegram recipients */}
                      {newAutomation.action_data.output_type === 'telegram' && (
                        integrations.find(i => i.provider === 'telegram')?.contacts.length ? (
                          <div className="space-y-2">
                            <div className="max-h-32 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2">
                              {integrations.find(i => i.provider === 'telegram')?.contacts.map(c => (
                                <label key={c.id} className="flex items-center gap-2 p-1.5 hover:bg-brain-700 rounded cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={(newAutomation.action_data.output_targets || []).includes(c.value)}
                                    onChange={(e) => {
                                      const current = newAutomation.action_data.output_targets || []
                                      const updated = e.target.checked
                                        ? [...current, c.value]
                                        : current.filter((id: string) => id !== c.value)
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          output_targets: updated,
                                          output_target: updated[0] || ''
                                        },
                                      })
                                    }}
                                    className="rounded border-brain-600 bg-brain-700 text-violet-500 focus:ring-violet-500"
                                  />
                                  <span className="text-sm text-brain-200">{c.display}</span>
                                </label>
                              ))}
                            </div>
                            <div className="flex gap-2">
                              <input
                                type="text"
                                placeholder={t('add_chat_id_manual')}
                                className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') {
                                    e.preventDefault()
                                    const input = e.currentTarget
                                    const value = input.value.trim()
                                    if (value && !(newAutomation.action_data.output_targets || []).includes(value)) {
                                      const current = newAutomation.action_data.output_targets || []
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          output_targets: [...current, value],
                                          output_target: current[0] || value
                                        },
                                      })
                                      input.value = ''
                                    }
                                  }
                                }}
                              />
                              <button
                                type="button"
                                onClick={(e) => {
                                  const input = (e.currentTarget.previousElementSibling as HTMLInputElement)
                                  const value = input.value.trim()
                                  if (value && !(newAutomation.action_data.output_targets || []).includes(value)) {
                                    const current = newAutomation.action_data.output_targets || []
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        output_targets: [...current, value],
                                        output_target: current[0] || value
                                      },
                                    })
                                    input.value = ''
                                  }
                                }}
                                className="px-3 py-2 rounded-lg bg-violet-600 text-white text-sm hover:bg-violet-700"
                              >
                                <Plus className="w-4 h-4" />
                              </button>
                            </div>
                          </div>
                        ) : (
                          <input
                            type="text"
                            placeholder={t('chat_ids_comma')}
                            value={(newAutomation.action_data.output_targets || []).join(', ') || newAutomation.action_data.output_target || ''}
                            onChange={(e) => {
                              const ids = e.target.value.split(',').map(s => s.trim()).filter(Boolean)
                              setNewAutomation({
                                ...newAutomation,
                                action_data: { 
                                  ...newAutomation.action_data, 
                                  output_targets: ids,
                                  output_target: ids[0] || ''
                                },
                              })
                            }}
                            className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          />
                        )
                      )}
                      
                      {/* Email recipients */}
                      {newAutomation.action_data.output_type === 'email' && (
                        integrations.find(i => i.provider === 'gmail')?.contacts.length ? (
                          <div className="space-y-2">
                            <div className="max-h-32 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2">
                              {integrations.find(i => i.provider === 'gmail')?.contacts.map(c => (
                                <label key={c.id} className="flex items-center gap-2 p-1.5 hover:bg-brain-700 rounded cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={(newAutomation.action_data.output_targets || []).includes(c.value)}
                                    onChange={(e) => {
                                      const current = newAutomation.action_data.output_targets || []
                                      const updated = e.target.checked
                                        ? [...current, c.value]
                                        : current.filter((id: string) => id !== c.value)
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          output_targets: updated,
                                          output_target: updated[0] || ''
                                        },
                                      })
                                    }}
                                    className="rounded border-brain-600 bg-brain-700 text-violet-500 focus:ring-violet-500"
                                  />
                                  <span className="text-sm text-brain-200">{c.display}</span>
                                </label>
                              ))}
                            </div>
                            <div className="flex gap-2">
                              <input
                                type="email"
                                placeholder={t('add_email_manual')}
                                className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') {
                                    e.preventDefault()
                                    const input = e.currentTarget
                                    const value = input.value.trim()
                                    if (value && value.includes('@') && !(newAutomation.action_data.output_targets || []).includes(value)) {
                                      const current = newAutomation.action_data.output_targets || []
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          output_targets: [...current, value],
                                          output_target: current[0] || value
                                        },
                                      })
                                      input.value = ''
                                    }
                                  }
                                }}
                              />
                              <button
                                type="button"
                                onClick={(e) => {
                                  const input = (e.currentTarget.previousElementSibling as HTMLInputElement)
                                  const value = input.value.trim()
                                  if (value && value.includes('@') && !(newAutomation.action_data.output_targets || []).includes(value)) {
                                    const current = newAutomation.action_data.output_targets || []
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        output_targets: [...current, value],
                                        output_target: current[0] || value
                                      },
                                    })
                                    input.value = ''
                                  }
                                }}
                                className="px-3 py-2 rounded-lg bg-violet-600 text-white text-sm hover:bg-violet-700"
                              >
                                <Plus className="w-4 h-4" />
                              </button>
                            </div>
                          </div>
                        ) : (
                          <input
                            type="text"
                            placeholder={t('emails_comma')}
                            value={(newAutomation.action_data.output_targets || []).join(', ') || newAutomation.action_data.output_target || ''}
                            onChange={(e) => {
                              const emails = e.target.value.split(',').map(s => s.trim()).filter(Boolean)
                              setNewAutomation({
                                ...newAutomation,
                                action_data: { 
                                  ...newAutomation.action_data, 
                                  output_targets: emails,
                                  output_target: emails[0] || ''
                                },
                              })
                            }}
                            className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          />
                        )
                      )}
                      
                      {/* Notion/YouGile - single input */}
                      {(newAutomation.action_data.output_type === 'notion' || newAutomation.action_data.output_type === 'yougile') && (
                        <input
                          type="text"
                          placeholder={newAutomation.action_data.output_type === 'notion' ? 'Page ID' : 'Column ID'}
                          value={newAutomation.action_data.output_target || ''}
                          onChange={(e) =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, output_target: e.target.value },
                            })
                          }
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                        />
                      )}
                      
                      {/* Show selected as tags */}
                      {(newAutomation.action_data.output_targets?.length || 0) > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {newAutomation.action_data.output_targets.map((target: string, idx: number) => {
                            const provider = newAutomation.action_data.output_type === 'telegram' ? 'telegram' : 'gmail'
                            const contact = integrations.find(i => i.provider === provider)?.contacts.find(c => c.value === target)
                            return (
                              <span key={idx} className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-violet-500/20 text-violet-300 text-xs">
                                {contact?.display || target}
                                <button
                                  type="button"
                                  onClick={() => {
                                    const updated = newAutomation.action_data.output_targets.filter((_: string, i: number) => i !== idx)
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        output_targets: updated,
                                        output_target: updated[0] || ''
                                      },
                                    })
                                  }}
                                  className="hover:text-red-400"
                                >
                                  <X className="w-3 h-3" />
                                </button>
                              </span>
                            )
                          })}
                        </div>
                      )}
                    </div>

                    {/* Model Tier Selector */}
                    <div>
                      <label className="block text-xs text-brain-500 mb-2">{t('llm_model_label')}</label>
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, model_tier: 'standard' },
                            })
                          }
                          className={`flex-1 px-3 py-2.5 rounded-lg border transition-all ${
                            (newAutomation.action_data.model_tier || 'standard') === 'standard'
                              ? 'bg-brain-600 border-brain-500 text-white shadow-md'
                              : 'bg-brain-800 border-brain-700 text-brain-400 hover:border-brain-600'
                          }`}
                        >
                          <div className="font-medium text-sm">{t('model_standard')}</div>
                          <div className="text-[10px] mt-0.5 opacity-70">{t('model_standard_hint')}</div>
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, model_tier: 'premium' },
                            })
                          }
                          className={`flex-1 px-3 py-2.5 rounded-lg border transition-all ${
                            newAutomation.action_data.model_tier === 'premium'
                              ? 'bg-gradient-to-r from-amber-600 to-yellow-600 border-amber-500 text-white shadow-lg shadow-amber-500/20'
                              : 'bg-brain-800 border-brain-700 text-amber-400 hover:border-amber-600/50'
                          }`}
                        >
                          <div className="font-medium text-sm">{t('model_premium')}</div>
                          <div className="text-[10px] mt-0.5 opacity-70">{t('model_premium_hint')}</div>
                        </button>
                      </div>
                    </div>
                  </div>
                )}

                {/* LLM Extract and Send */}
                {newAutomation.action_type === 'llm_extract_and_send' && (
                  <div className="space-y-3">
                    <div className="p-3 rounded-lg bg-teal-500/10 border border-teal-500/30">
                      <div className="flex items-center gap-2 text-teal-400 text-sm font-medium mb-1">
                        <Search className="w-4 h-4" />
                        {t('extract_send_title')}
                      </div>
                      <p className="text-xs text-brain-400">
                        {t('extract_send_description')}
                      </p>
                    </div>

                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('select_meeting_label')}</label>
                      <select
                        value={newAutomation.action_data.meeting_id || ''}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, meeting_id: e.target.value },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      >
                        <option value="">{t('select_meeting_placeholder')}</option>
                        {meetings.map(m => (
                          <option key={m.id} value={m.id}>{m.display}</option>
                        ))}
                      </select>
                    </div>

                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('what_to_extract_label')}</label>
                      <select
                        value={newAutomation.action_data.extract_type || 'summary'}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, extract_type: e.target.value },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      >
                        <option value="summary">{t('extract_summary')}</option>
                        <option value="tasks">{t('extract_tasks')}</option>
                        <option value="decisions">{t('extract_decisions')}</option>
                        <option value="action_items">📌 Action Items</option>
                        <option value="custom">{t('extract_custom')}</option>
                      </select>
                    </div>

                    {newAutomation.action_data.extract_type === 'custom' && (
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('custom_prompt_label')}</label>
                        <textarea
                          placeholder={t('custom_prompt_placeholder')}
                          value={newAutomation.action_data.custom_prompt || ''}
                          onChange={(e) =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, custom_prompt: e.target.value },
                            })
                          }
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          rows={3}
                        />
                      </div>
                    )}

                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('where_send_label')}</label>
                      <select
                        value={newAutomation.action_data.output_type || 'telegram'}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, output_type: e.target.value, output_target: '' },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      >
                        <option value="telegram">Telegram</option>
                        <option value="email">Email</option>
                        <option value="notion">Notion</option>
                        <option value="yougile">YouGile</option>
                      </select>
                    </div>

                    {/* Recipients - Multiple selection */}
                    <div className="space-y-2">
                      <label className="block text-xs text-brain-500 mb-1">
                        {t('recipients_label')} {(newAutomation.action_data.output_targets?.length || 0) > 0 && 
                          <span className="text-teal-400">{t('selected_count', { count: newAutomation.action_data.output_targets?.length })}</span>
                        }
                      </label>
                      
                      {/* Telegram recipients */}
                      {newAutomation.action_data.output_type === 'telegram' && (
                        integrations.find(i => i.provider === 'telegram')?.contacts.length ? (
                          <div className="space-y-2">
                            <div className="max-h-32 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2">
                              {integrations.find(i => i.provider === 'telegram')?.contacts.map(c => (
                                <label key={c.id} className="flex items-center gap-2 p-1.5 hover:bg-brain-700 rounded cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={(newAutomation.action_data.output_targets || []).includes(c.value)}
                                    onChange={(e) => {
                                      const current = newAutomation.action_data.output_targets || []
                                      const updated = e.target.checked
                                        ? [...current, c.value]
                                        : current.filter((id: string) => id !== c.value)
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          output_targets: updated,
                                          output_target: updated[0] || ''
                                        },
                                      })
                                    }}
                                    className="rounded border-brain-600 bg-brain-700 text-teal-500 focus:ring-teal-500"
                                  />
                                  <span className="text-sm text-brain-200">{c.display}</span>
                                </label>
                              ))}
                            </div>
                            <div className="flex gap-2">
                              <input
                                type="text"
                                placeholder={t('add_chat_id_manual')}
                                className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') {
                                    e.preventDefault()
                                    const input = e.currentTarget
                                    const value = input.value.trim()
                                    if (value && !(newAutomation.action_data.output_targets || []).includes(value)) {
                                      const current = newAutomation.action_data.output_targets || []
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          output_targets: [...current, value],
                                          output_target: current[0] || value
                                        },
                                      })
                                      input.value = ''
                                    }
                                  }
                                }}
                              />
                              <button
                                type="button"
                                onClick={(e) => {
                                  const input = (e.currentTarget.previousElementSibling as HTMLInputElement)
                                  const value = input.value.trim()
                                  if (value && !(newAutomation.action_data.output_targets || []).includes(value)) {
                                    const current = newAutomation.action_data.output_targets || []
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        output_targets: [...current, value],
                                        output_target: current[0] || value
                                      },
                                    })
                                    input.value = ''
                                  }
                                }}
                                className="px-3 py-2 rounded-lg bg-teal-600 text-white text-sm hover:bg-teal-700"
                              >
                                <Plus className="w-4 h-4" />
                              </button>
                            </div>
                          </div>
                        ) : (
                          <input
                            type="text"
                            placeholder={t('chat_ids_comma')}
                            value={(newAutomation.action_data.output_targets || []).join(', ') || newAutomation.action_data.output_target || ''}
                            onChange={(e) => {
                              const ids = e.target.value.split(',').map(s => s.trim()).filter(Boolean)
                              setNewAutomation({
                                ...newAutomation,
                                action_data: { 
                                  ...newAutomation.action_data, 
                                  output_targets: ids,
                                  output_target: ids[0] || ''
                                },
                              })
                            }}
                            className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          />
                        )
                      )}
                      
                      {/* Email recipients */}
                      {newAutomation.action_data.output_type === 'email' && (
                        integrations.find(i => i.provider === 'gmail')?.contacts.length ? (
                          <div className="space-y-2">
                            <div className="max-h-32 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2">
                              {integrations.find(i => i.provider === 'gmail')?.contacts.map(c => (
                                <label key={c.id} className="flex items-center gap-2 p-1.5 hover:bg-brain-700 rounded cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={(newAutomation.action_data.output_targets || []).includes(c.value)}
                                    onChange={(e) => {
                                      const current = newAutomation.action_data.output_targets || []
                                      const updated = e.target.checked
                                        ? [...current, c.value]
                                        : current.filter((id: string) => id !== c.value)
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          output_targets: updated,
                                          output_target: updated[0] || ''
                                        },
                                      })
                                    }}
                                    className="rounded border-brain-600 bg-brain-700 text-teal-500 focus:ring-teal-500"
                                  />
                                  <span className="text-sm text-brain-200">{c.display}</span>
                                </label>
                              ))}
                            </div>
                            <div className="flex gap-2">
                              <input
                                type="email"
                                placeholder={t('add_email_manual')}
                                className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') {
                                    e.preventDefault()
                                    const input = e.currentTarget
                                    const value = input.value.trim()
                                    if (value && value.includes('@') && !(newAutomation.action_data.output_targets || []).includes(value)) {
                                      const current = newAutomation.action_data.output_targets || []
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          output_targets: [...current, value],
                                          output_target: current[0] || value
                                        },
                                      })
                                      input.value = ''
                                    }
                                  }
                                }}
                              />
                              <button
                                type="button"
                                onClick={(e) => {
                                  const input = (e.currentTarget.previousElementSibling as HTMLInputElement)
                                  const value = input.value.trim()
                                  if (value && value.includes('@') && !(newAutomation.action_data.output_targets || []).includes(value)) {
                                    const current = newAutomation.action_data.output_targets || []
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        output_targets: [...current, value],
                                        output_target: current[0] || value
                                      },
                                    })
                                    input.value = ''
                                  }
                                }}
                                className="px-3 py-2 rounded-lg bg-teal-600 text-white text-sm hover:bg-teal-700"
                              >
                                <Plus className="w-4 h-4" />
                              </button>
                            </div>
                          </div>
                        ) : (
                          <input
                            type="text"
                            placeholder={t('emails_comma')}
                            value={(newAutomation.action_data.output_targets || []).join(', ') || newAutomation.action_data.output_target || ''}
                            onChange={(e) => {
                              const emails = e.target.value.split(',').map(s => s.trim()).filter(Boolean)
                              setNewAutomation({
                                ...newAutomation,
                                action_data: { 
                                  ...newAutomation.action_data, 
                                  output_targets: emails,
                                  output_target: emails[0] || ''
                                },
                              })
                            }}
                            className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          />
                        )
                      )}
                      
                      {/* Notion/YouGile - single input */}
                      {(newAutomation.action_data.output_type === 'notion' || newAutomation.action_data.output_type === 'yougile') && (
                        <input
                          type="text"
                          placeholder={newAutomation.action_data.output_type === 'notion' ? 'Page ID' : 'Column ID'}
                          value={newAutomation.action_data.output_target || ''}
                          onChange={(e) =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, output_target: e.target.value },
                            })
                          }
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                        />
                      )}
                      
                      {/* Show selected as tags */}
                      {(newAutomation.action_data.output_targets?.length || 0) > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {newAutomation.action_data.output_targets.map((target: string, idx: number) => {
                            const provider = newAutomation.action_data.output_type === 'telegram' ? 'telegram' : 'gmail'
                            const contact = integrations.find(i => i.provider === provider)?.contacts.find(c => c.value === target)
                            return (
                              <span key={idx} className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-teal-500/20 text-teal-300 text-xs">
                                {contact?.display || target}
                                <button
                                  type="button"
                                  onClick={() => {
                                    const updated = newAutomation.action_data.output_targets.filter((_: string, i: number) => i !== idx)
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        output_targets: updated,
                                        output_target: updated[0] || ''
                                      },
                                    })
                                  }}
                                  className="hover:text-red-400"
                                >
                                  <X className="w-3 h-3" />
                                </button>
                              </span>
                            )
                          })}
                        </div>
                      )}
                    </div>

                    {/* Model Tier Selector for Extract */}
                    <div>
                      <label className="block text-xs text-brain-500 mb-2">{t('llm_model_label')}</label>
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, model_tier: 'standard' },
                            })
                          }
                          className={`flex-1 px-3 py-2.5 rounded-lg border transition-all ${
                            (newAutomation.action_data.model_tier || 'standard') === 'standard'
                              ? 'bg-brain-600 border-brain-500 text-white shadow-md'
                              : 'bg-brain-800 border-brain-700 text-brain-400 hover:border-brain-600'
                          }`}
                        >
                          <div className="font-medium text-sm">{t('model_standard')}</div>
                          <div className="text-[10px] mt-0.5 opacity-70">{t('model_standard_hint')}</div>
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, model_tier: 'premium' },
                            })
                          }
                          className={`flex-1 px-3 py-2.5 rounded-lg border transition-all ${
                            newAutomation.action_data.model_tier === 'premium'
                              ? 'bg-gradient-to-r from-amber-600 to-yellow-600 border-amber-500 text-white shadow-lg shadow-amber-500/20'
                              : 'bg-brain-800 border-brain-700 text-amber-400 hover:border-amber-600/50'
                          }`}
                        >
                          <div className="font-medium text-sm">{t('model_premium')}</div>
                          <div className="text-[10px] mt-0.5 opacity-70">{t('model_premium_hint')}</div>
                        </button>
                      </div>
                    </div>
                  </div>
                )}

                {/* Meeting Workflow Types */}
                {['meeting_summary_telegram', 'meeting_tasks_yougile', 'meeting_report_email', 'meeting_notes_notion',
                  'meeting_document', 'client_followup', 'hr_candidate_report', 'commitments_tracker',
                  'client_risk_alert', 'sales_call_scorecard', 'voice_of_customer', 'one_on_one_pulse',
                  'call_qa_review'].includes(newAutomation.action_type) && (
                  <div className="space-y-3">
                    <div className="p-3 rounded-lg bg-blue-500/10 border border-blue-500/30">
                      <div className="flex items-center gap-2 text-blue-400 text-sm font-medium mb-1">
                        {ACTION_ICONS[newAutomation.action_type]}
                        <span>{t('meeting_processing_title')}</span>
                      </div>
                      <p className="text-xs text-brain-400">
                        {newAutomation.action_type === 'meeting_summary_telegram' && t('meeting_summary_telegram_desc')}
                        {newAutomation.action_type === 'meeting_tasks_yougile' && t('meeting_tasks_yougile_desc')}
                        {newAutomation.action_type === 'meeting_report_email' && t('meeting_report_email_desc')}
                        {newAutomation.action_type === 'meeting_notes_notion' && t('meeting_notes_notion_desc')}
                        {!['meeting_summary_telegram', 'meeting_tasks_yougile', 'meeting_report_email', 'meeting_notes_notion'].includes(newAutomation.action_type) &&
                          (actionTypes.find(a => a.id === newAutomation.action_type)?.description || '')}
                      </p>
                    </div>

                    {/* Trigger Type */}
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('when_run_label')}</label>
                      <div className="grid grid-cols-2 gap-2">
                        <button
                          type="button"
                          onClick={() => setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, trigger_type: 'manual' }
                          })}
                          className={`px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                            (newAutomation.action_data.trigger_type || 'manual') === 'manual'
                              ? 'bg-blue-600 text-white'
                              : 'bg-brain-800 text-brain-400 hover:bg-brain-700'
                          }`}
                        >
                          {t('choose_meeting_tab')}
                        </button>
                        <button
                          type="button"
                          onClick={() => setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, trigger_type: 'on_new_meeting' }
                          })}
                          className={`px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                            newAutomation.action_data.trigger_type === 'on_new_meeting'
                              ? 'bg-green-600 text-white'
                              : 'bg-brain-800 text-brain-400 hover:bg-brain-700'
                          }`}
                        >
                          {t('on_new_meeting_tab')}
                        </button>
                      </div>
                    </div>

                    {/* On New Meeting - folder/project filter */}
                    {newAutomation.action_data.trigger_type === 'on_new_meeting' && (
                      <div className="p-3 rounded-lg bg-green-500/10 border border-green-500/30 space-y-3">
                        <p className="text-xs text-green-400">
                          {t('on_new_meeting_hint')}
                        </p>
                        <div>
                          <label className="block text-xs text-brain-500 mb-1">{t('folder_filter_label')}</label>
                          {loadingFolders ? (
                            <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-400 text-sm">
                              <Loader2 className="w-4 h-4 animate-spin" />
                              {t('loading')}
                            </div>
                          ) : folders.length > 0 ? (
                            <select
                              value={newAutomation.action_data.folder_id || newAutomation.action_data.project_id || ''}
                              onChange={(e) => {
                                const selected = folders.find(f => f.id === e.target.value)
                                setNewAutomation({
                                  ...newAutomation,
                                  action_data: { 
                                    ...newAutomation.action_data, 
                                    folder_id: selected?.type === 'folder' ? e.target.value : '',
                                    project_id: selected?.type === 'project' ? e.target.value : ''
                                  },
                                })
                              }}
                              className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                            >
                              <option value="">{t('all_meetings_no_filter')}</option>
                              {folders.filter(f => f.type === 'folder').length > 0 && (
                                <optgroup label={t('folders_optgroup')}>
                                  {folders.filter(f => f.type === 'folder').map(f => (
                                    <option key={f.id} value={f.id}>📁 {f.name}</option>
                                  ))}
                                </optgroup>
                              )}
                              {folders.filter(f => f.type === 'project').length > 0 && (
                                <optgroup label={t('projects_optgroup')}>
                                  {folders.filter(f => f.type === 'project').map(f => (
                                    <option key={f.id} value={f.id}>📂 {f.name}</option>
                                  ))}
                                </optgroup>
                              )}
                            </select>
                          ) : (
                            <input
                              type="text"
                              placeholder={t('folder_id_placeholder')}
                              value={newAutomation.action_data.folder_id || newAutomation.action_data.project_id || ''}
                              onChange={(e) =>
                                setNewAutomation({
                                  ...newAutomation,
                                  action_data: { ...newAutomation.action_data, folder_id: e.target.value },
                                })
                              }
                              className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                            />
                          )}
                        </div>
                        <p className="text-[10px] text-brain-500">
                          {t('folder_filter_hint')}
                        </p>
                      </div>
                    )}

                    {/* Meeting Selection with Search */}
                    {(newAutomation.action_data.trigger_type || 'manual') === 'manual' && (
                      <div className="space-y-2">
                        <label className="block text-xs text-brain-500 mb-1">
                          {t('select_meeting_with_count')}
                          <span className="text-brain-600 ml-1">{t('available_count', { count: meetings.length })}</span>
                        </label>
                        {loadingMeetings ? (
                          <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-400 text-sm">
                            <Loader2 className="w-4 h-4 animate-spin" />
                            {t('loading_meetings')}
                          </div>
                        ) : meetings.length > 0 ? (
                          <div className="space-y-2">
                            {/* Search input */}
                            <div className="relative">
                              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-brain-500" />
                              <input
                                type="text"
                                placeholder={t('search_meeting_placeholder')}
                                value={newAutomation.action_data._meetingSearch || ''}
                                onChange={(e) =>
                                  setNewAutomation({
                                    ...newAutomation,
                                    action_data: { ...newAutomation.action_data, _meetingSearch: e.target.value },
                                  })
                                }
                                className="w-full pl-9 pr-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                              />
                            </div>
                            {/* Filtered meetings list */}
                            <div className="max-h-48 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700">
                              {meetings
                                .filter(m => 
                                  !newAutomation.action_data._meetingSearch || 
                                  m.display.toLowerCase().includes((newAutomation.action_data._meetingSearch || '').toLowerCase())
                                )
                                .slice(0, 50)
                                .map(m => (
                                  <button
                                    key={m.id}
                                    type="button"
                                    onClick={() =>
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { ...newAutomation.action_data, meeting_id: m.id, _selectedMeetingTitle: m.display },
                                      })
                                    }
                                    className={`w-full text-left px-3 py-2 text-sm hover:bg-brain-700 transition-colors border-b border-brain-700/50 last:border-0 ${
                                      newAutomation.action_data.meeting_id === m.id
                                        ? 'bg-blue-600/20 text-blue-300'
                                        : 'text-brain-200'
                                    }`}
                                  >
                                    <div className="font-medium truncate">{m.title}</div>
                                    <div className="text-xs text-brain-500">{m.created_at}</div>
                                  </button>
                                ))}
                              {meetings.filter(m => 
                                !newAutomation.action_data._meetingSearch || 
                                m.display.toLowerCase().includes((newAutomation.action_data._meetingSearch || '').toLowerCase())
                              ).length === 0 && (
                                <div className="px-3 py-4 text-center text-brain-500 text-sm">
                                  {t('meetings_not_found')}
                                </div>
                              )}
                            </div>
                            {/* Selected meeting display */}
                            {newAutomation.action_data.meeting_id && (
                              <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-blue-600/10 border border-blue-500/30">
                                <CheckCircle className="w-4 h-4 text-blue-400" />
                                <span className="text-sm text-blue-300 truncate flex-1">
                                  {newAutomation.action_data._selectedMeetingTitle || 
                                   meetings.find(m => m.id === newAutomation.action_data.meeting_id)?.display ||
                                   newAutomation.action_data.meeting_id}
                                </span>
                                <button
                                  type="button"
                                  onClick={() =>
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { ...newAutomation.action_data, meeting_id: '', _selectedMeetingTitle: '' },
                                    })
                                  }
                                  className="text-brain-500 hover:text-red-400"
                                >
                                  <X className="w-4 h-4" />
                                </button>
                              </div>
                            )}
                          </div>
                        ) : (
                          <div className="space-y-2">
                            <input
                              type="text"
                              placeholder={t('manual_meeting_id_placeholder')}
                              value={newAutomation.action_data.meeting_id || ''}
                              onChange={(e) =>
                                setNewAutomation({
                                  ...newAutomation,
                                  action_data: { ...newAutomation.action_data, meeting_id: e.target.value },
                                })
                              }
                              className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                            />
                            <p className="text-xs text-brain-500">{t('meetings_not_found_hint')}</p>
                          </div>
                        )}
                      </div>
                    )}

                    {/* Telegram destination - Multiple recipients */}
                    {newAutomation.action_type === 'meeting_summary_telegram' && (
                      <div className="space-y-2">
                        <label className="block text-xs text-brain-500 mb-1">
                          {t('telegram_where_to_send')} {(newAutomation.action_data.chat_ids?.length || 0) > 0 && 
                            <span className="text-blue-400">{t('selected_count', { count: newAutomation.action_data.chat_ids?.length })}</span>
                          }
                        </label>
                        {integrations.find(i => i.provider === 'telegram')?.contacts.length ? (
                          <div className="space-y-2">
                            {/* Multi-select checkboxes */}
                            <div className="max-h-40 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2">
                              {integrations.find(i => i.provider === 'telegram')?.contacts.map(c => (
                                <label key={c.id} className="flex items-center gap-2 p-1.5 hover:bg-brain-700 rounded cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={(newAutomation.action_data.chat_ids || []).includes(c.value)}
                                    onChange={(e) => {
                                      const currentIds = newAutomation.action_data.chat_ids || []
                                      const newIds = e.target.checked
                                        ? [...currentIds, c.value]
                                        : currentIds.filter((id: string) => id !== c.value)
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          chat_ids: newIds,
                                          chat_id: newIds[0] || '' // backward compatibility
                                        },
                                      })
                                    }}
                                    className="rounded border-brain-600 bg-brain-700 text-blue-500 focus:ring-blue-500"
                                  />
                                  <span className="text-sm text-brain-200">{c.display}</span>
                                </label>
                              ))}
                            </div>
                            {/* Manual input for additional chat */}
                            <div className="flex gap-2">
                              <input
                                type="text"
                                placeholder={t('add_chat_id_manual')}
                                className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') {
                                    const input = e.currentTarget
                                    const value = input.value.trim()
                                    if (value && !(newAutomation.action_data.chat_ids || []).includes(value)) {
                                      const currentIds = newAutomation.action_data.chat_ids || []
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          chat_ids: [...currentIds, value],
                                          chat_id: currentIds[0] || value
                                        },
                                      })
                                      input.value = ''
                                    }
                                  }
                                }}
                              />
                              <button
                                type="button"
                                onClick={(e) => {
                                  const input = (e.currentTarget.previousElementSibling as HTMLInputElement)
                                  const value = input.value.trim()
                                  if (value && !(newAutomation.action_data.chat_ids || []).includes(value)) {
                                    const currentIds = newAutomation.action_data.chat_ids || []
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        chat_ids: [...currentIds, value],
                                        chat_id: currentIds[0] || value
                                      },
                                    })
                                    input.value = ''
                                  }
                                }}
                                className="px-3 py-2 rounded-lg bg-blue-600 text-white text-sm hover:bg-blue-700"
                              >
                                <Plus className="w-4 h-4" />
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="space-y-2">
                            <input
                              type="text"
                              placeholder={t('chat_ids_comma')}
                              value={(newAutomation.action_data.chat_ids || []).join(', ') || newAutomation.action_data.chat_id || ''}
                              onChange={(e) => {
                                const ids = e.target.value.split(',').map(s => s.trim()).filter(Boolean)
                                setNewAutomation({
                                  ...newAutomation,
                                  action_data: { 
                                    ...newAutomation.action_data, 
                                    chat_ids: ids,
                                    chat_id: ids[0] || ''
                                  },
                                })
                              }}
                              className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                            />
                            <p className="text-[10px] text-brain-500">{t('chat_ids_hint')}</p>
                          </div>
                        )}
                        {/* Show selected chats as tags */}
                        {(newAutomation.action_data.chat_ids?.length || 0) > 0 && (
                          <div className="flex flex-wrap gap-1 mt-2">
                            {newAutomation.action_data.chat_ids.map((chatId: string, idx: number) => {
                              const contact = integrations.find(i => i.provider === 'telegram')?.contacts.find(c => c.value === chatId)
                              return (
                                <span key={idx} className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-blue-500/20 text-blue-300 text-xs">
                                  {contact?.display || chatId}
                                  <button
                                    type="button"
                                    onClick={() => {
                                      const newIds = newAutomation.action_data.chat_ids.filter((_: string, i: number) => i !== idx)
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          chat_ids: newIds,
                                          chat_id: newIds[0] || ''
                                        },
                                      })
                                    }}
                                    className="hover:text-red-400"
                                  >
                                    <X className="w-3 h-3" />
                                  </button>
                                </span>
                              )
                            })}
                          </div>
                        )}
                      </div>
                    )}

                    {/* YouGile column */}
                    {newAutomation.action_type === 'meeting_tasks_yougile' && (
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('yougile_column_id_label')}</label>
                        <input
                          type="text"
                          placeholder={t('yougile_column_id_placeholder')}
                          value={newAutomation.action_data.column_id || ''}
                          onChange={(e) =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, column_id: e.target.value },
                            })
                          }
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                        />
                      </div>
                    )}

                    {/* Email destination - Multiple recipients */}
                    {newAutomation.action_type === 'meeting_report_email' && (
                      <div className="space-y-2">
                        <label className="block text-xs text-brain-500 mb-1">
                          {t('email_recipients_label')} {(newAutomation.action_data.to_emails?.length || 0) > 0 && 
                            <span className="text-blue-400">{t('selected_count', { count: newAutomation.action_data.to_emails?.length })}</span>
                          }
                        </label>
                        {integrations.find(i => i.provider === 'gmail')?.contacts.length ? (
                          <div className="space-y-2">
                            {/* Multi-select checkboxes for emails */}
                            <div className="max-h-40 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2">
                              {integrations.find(i => i.provider === 'gmail')?.contacts.map(c => (
                                <label key={c.id} className="flex items-center gap-2 p-1.5 hover:bg-brain-700 rounded cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={(newAutomation.action_data.to_emails || []).includes(c.value)}
                                    onChange={(e) => {
                                      const currentEmails = newAutomation.action_data.to_emails || []
                                      const newEmails = e.target.checked
                                        ? [...currentEmails, c.value]
                                        : currentEmails.filter((email: string) => email !== c.value)
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          to_emails: newEmails,
                                          to_email: newEmails[0] || '' // backward compatibility
                                        },
                                      })
                                    }}
                                    className="rounded border-brain-600 bg-brain-700 text-blue-500 focus:ring-blue-500"
                                  />
                                  <span className="text-sm text-brain-200">{c.display}</span>
                                </label>
                              ))}
                            </div>
                            {/* Manual input for additional email */}
                            <div className="flex gap-2">
                              <input
                                type="email"
                                placeholder={t('add_email_manual')}
                                className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') {
                                    const input = e.currentTarget
                                    const value = input.value.trim()
                                    if (value && value.includes('@') && !(newAutomation.action_data.to_emails || []).includes(value)) {
                                      const currentEmails = newAutomation.action_data.to_emails || []
                                      setNewAutomation({
                                        ...newAutomation,
                                        action_data: { 
                                          ...newAutomation.action_data, 
                                          to_emails: [...currentEmails, value],
                                          to_email: currentEmails[0] || value
                                        },
                                      })
                                      input.value = ''
                                    }
                                  }
                                }}
                              />
                              <button
                                type="button"
                                onClick={(e) => {
                                  const input = (e.currentTarget.previousElementSibling as HTMLInputElement)
                                  const value = input.value.trim()
                                  if (value && value.includes('@') && !(newAutomation.action_data.to_emails || []).includes(value)) {
                                    const currentEmails = newAutomation.action_data.to_emails || []
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        to_emails: [...currentEmails, value],
                                        to_email: currentEmails[0] || value
                                      },
                                    })
                                    input.value = ''
                                  }
                                }}
                                className="px-3 py-2 rounded-lg bg-blue-600 text-white text-sm hover:bg-blue-700"
                              >
                                <Plus className="w-4 h-4" />
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="space-y-2">
                            <input
                              type="text"
                              placeholder={t('emails_comma')}
                              value={(newAutomation.action_data.to_emails || []).join(', ') || newAutomation.action_data.to_email || ''}
                              onChange={(e) => {
                                const emails = e.target.value.split(',').map(s => s.trim()).filter(Boolean)
                                setNewAutomation({
                                  ...newAutomation,
                                  action_data: { 
                                    ...newAutomation.action_data, 
                                    to_emails: emails,
                                    to_email: emails[0] || ''
                                  },
                                })
                              }}
                              className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                            />
                            <p className="text-[10px] text-brain-500">{t('emails_hint')}</p>
                          </div>
                        )}
                        {/* Show selected emails as tags */}
                        {(newAutomation.action_data.to_emails?.length || 0) > 0 && (
                          <div className="flex flex-wrap gap-1 mt-2">
                            {newAutomation.action_data.to_emails.map((email: string, idx: number) => (
                              <span key={idx} className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-blue-500/20 text-blue-300 text-xs">
                                {email}
                                <button
                                  type="button"
                                  onClick={() => {
                                    const newEmails = newAutomation.action_data.to_emails.filter((_: string, i: number) => i !== idx)
                                    setNewAutomation({
                                      ...newAutomation,
                                      action_data: { 
                                        ...newAutomation.action_data, 
                                        to_emails: newEmails,
                                        to_email: newEmails[0] || ''
                                      },
                                    })
                                  }}
                                  className="hover:text-red-400"
                                >
                                  <X className="w-3 h-3" />
                                </button>
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    )}

                    {/* Notion page */}
                    {newAutomation.action_type === 'meeting_notes_notion' && (
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('notion_parent_page_label')}</label>
                        <input
                          type="text"
                          placeholder={t('notion_parent_page_placeholder')}
                          value={newAutomation.action_data.parent_page_id || ''}
                          onChange={(e) =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, parent_page_id: e.target.value },
                            })
                          }
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                        />
                      </div>
                    )}

                    {/* Model Tier Selector */}
                    <div>
                      <label className="block text-xs text-brain-500 mb-2">{t('llm_model_label')}</label>
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, model_tier: 'standard' },
                            })
                          }
                          className={`flex-1 px-3 py-2.5 rounded-lg border transition-all ${
                            (newAutomation.action_data.model_tier || 'standard') === 'standard'
                              ? 'bg-brain-600 border-brain-500 text-white shadow-md'
                              : 'bg-brain-800 border-brain-700 text-brain-400 hover:border-brain-600'
                          }`}
                        >
                          <div className="font-medium text-sm">{t('model_standard')}</div>
                          <div className="text-[10px] mt-0.5 opacity-70">{t('model_standard_hint')}</div>
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            setNewAutomation({
                              ...newAutomation,
                              action_data: { ...newAutomation.action_data, model_tier: 'premium' },
                            })
                          }
                          className={`flex-1 px-3 py-2.5 rounded-lg border transition-all ${
                            newAutomation.action_data.model_tier === 'premium'
                              ? 'bg-gradient-to-r from-amber-600 to-yellow-600 border-amber-500 text-white shadow-lg shadow-amber-500/20'
                              : 'bg-brain-800 border-brain-700 text-amber-400 hover:border-amber-600/50'
                          }`}
                        >
                          <div className="font-medium text-sm">{t('model_premium')}</div>
                          <div className="text-[10px] mt-0.5 opacity-70">{t('model_premium_hint')}</div>
                        </button>
                      </div>
                    </div>

                    {/* Custom Prompt - for all meeting workflows */}
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">
                        {t('custom_prompt_optional_label')}
                      </label>
                      <textarea
                        placeholder={t('custom_prompt_meeting_placeholder')}
                        value={newAutomation.action_data.custom_prompt || ''}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, custom_prompt: e.target.value },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                        rows={3}
                      />
                      <p className="text-[10px] text-brain-500 mt-1">
                        {t('custom_prompt_optional_hint')}
                      </p>
                    </div>
                  </div>
                )}

                {newAutomation.action_type === 'send_slack' && (
                  <div className="space-y-3">
                    <textarea
                      placeholder={t('slack_message_placeholder')}
                      value={newAutomation.action_data.message || ''}
                      onChange={(e) =>
                        setNewAutomation({
                          ...newAutomation,
                          action_data: { ...newAutomation.action_data, message: e.target.value },
                        })
                      }
                      className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      rows={3}
                    />
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('slack_channel_label')}</label>
                      <input
                        type="text"
                        placeholder={t('slack_channel_placeholder')}
                        value={newAutomation.action_data.channel || ''}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, channel: e.target.value },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      />
                      {!integrations.find(i => i.provider === 'slack')?.configured && (
                        <p className="text-xs text-amber-400/70 mt-1">
                          {t('slack_not_connected_warning')}
                        </p>
                      )}
                    </div>
                  </div>
                )}

                {newAutomation.action_type === 'create_notion_page' && (
                  <div className="space-y-3">
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('notion_page_title_label')}</label>
                      <input
                        type="text"
                        placeholder={t('notion_page_title_placeholder')}
                        value={newAutomation.action_data.title || ''}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, title: e.target.value },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('notion_page_content_label')}</label>
                      <textarea
                        placeholder={t('notion_page_content_placeholder')}
                        value={newAutomation.action_data.content || ''}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, content: e.target.value },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                        rows={4}
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('notion_parent_optional_label')}</label>
                      <input
                        type="text"
                        placeholder={t('notion_parent_optional_placeholder')}
                        value={newAutomation.action_data.parent_page_id || ''}
                        onChange={(e) =>
                          setNewAutomation({
                            ...newAutomation,
                            action_data: { ...newAutomation.action_data, parent_page_id: e.target.value },
                          })
                        }
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                      />
                      {!integrations.find(i => i.provider === 'notion')?.configured && (
                        <p className="text-xs text-amber-400/70 mt-1">
                          {t('notion_not_connected_warning')}
                        </p>
                      )}
                    </div>
                  </div>
                )}

                {newAutomation.action_type === 'create_yougile_task' && (
                  <textarea
                    placeholder={t('json_params_placeholder')}
                    value={JSON.stringify(newAutomation.action_data, null, 2)}
                    onChange={(e) => {
                      try {
                        setNewAutomation({
                          ...newAutomation,
                          action_data: JSON.parse(e.target.value),
                        })
                      } catch {}
                    }}
                    className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm font-mono resize-none"
                    rows={4}
                  />
                )}

                {['create_jira_issue', 'create_trello_card', 'create_github_issue'].includes(newAutomation.action_type) && (() => {
                  const setD = (k: string, v: string) =>
                    setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, [k]: v } })
                  const d = newAutomation.action_data
                  const inputCls = 'w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm'
                  const provider = newAutomation.action_type === 'create_jira_issue' ? 'jira'
                    : newAutomation.action_type === 'create_trello_card' ? 'trello' : 'github'
                  return (
                    <div className="space-y-3">
                      {provider === 'jira' && (
                        <>
                          <div>
                            <label className="block text-xs text-brain-500 mb-1">{t('jira_project_label')}</label>
                            <input type="text" placeholder="ABC" value={d.project || ''}
                              onChange={(e) => setD('project', e.target.value)} className={inputCls} />
                          </div>
                          <input type="text" placeholder={t('tracker_title_placeholder')} value={d.summary || ''}
                            onChange={(e) => setD('summary', e.target.value)} className={inputCls} />
                          <textarea placeholder={t('tracker_description_placeholder')} value={d.description || ''}
                            onChange={(e) => setD('description', e.target.value)} className={inputCls} rows={3} />
                          <select value={d.issue_type || 'Task'} onChange={(e) => setD('issue_type', e.target.value)}
                            className={inputCls}>
                            {['Task', 'Bug', 'Story'].map(v => <option key={v} value={v}>{v}</option>)}
                          </select>
                        </>
                      )}
                      {provider === 'trello' && (
                        <>
                          <div>
                            <label className="block text-xs text-brain-500 mb-1">{t('trello_list_label')}</label>
                            <input type="text" placeholder="list_id" value={d.list_id || ''}
                              onChange={(e) => setD('list_id', e.target.value)} className={inputCls} />
                          </div>
                          <input type="text" placeholder={t('tracker_title_placeholder')} value={d.name || ''}
                            onChange={(e) => setD('name', e.target.value)} className={inputCls} />
                          <textarea placeholder={t('tracker_description_placeholder')} value={d.description || ''}
                            onChange={(e) => setD('description', e.target.value)} className={inputCls} rows={3} />
                        </>
                      )}
                      {provider === 'github' && (
                        <>
                          <div>
                            <label className="block text-xs text-brain-500 mb-1">{t('github_repo_label')}</label>
                            <input type="text" placeholder="owner/repo" value={d.repo || ''}
                              onChange={(e) => setD('repo', e.target.value)} className={inputCls} />
                          </div>
                          <input type="text" placeholder={t('tracker_title_placeholder')} value={d.title || ''}
                            onChange={(e) => setD('title', e.target.value)} className={inputCls} />
                          <textarea placeholder={t('tracker_description_placeholder')} value={d.body || ''}
                            onChange={(e) => setD('body', e.target.value)} className={inputCls} rows={3} />
                        </>
                      )}
                      {!integrations.find(i => i.provider === provider)?.configured && (
                        <p className="text-xs text-amber-400/70">{t('tracker_not_connected_warning', { provider })}</p>
                      )}
                    </div>
                  )
                })()}

                {newAutomation.action_type === 'crm_write' && (() => {
                  const setD = (k: string, v: string) =>
                    setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, [k]: v } })
                  const d = newAutomation.action_data
                  const inputCls = 'w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm'
                  return (
                    <div className="space-y-3">
                      <div className="grid grid-cols-2 gap-2">
                        <div>
                          <label className="block text-xs text-brain-500 mb-1">{t('crm_provider_label')}</label>
                          <select value={d.provider || 'amocrm'} onChange={(e) => setD('provider', e.target.value)}
                            className={inputCls}>
                            {['amocrm', 'bitrix24', 'hubspot', 'pipedrive'].map(p => <option key={p} value={p}>{p}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className="block text-xs text-brain-500 mb-1">{t('crm_op_label')}</label>
                          <select value={d.op || 'create'} onChange={(e) => setD('op', e.target.value)}
                            className={inputCls}>
                            <option value="create">{t('crm_op_create')}</option>
                            <option value="note">{t('crm_op_note')}</option>
                          </select>
                        </div>
                      </div>
                      {(d.op || 'create') === 'create' ? (
                        <>
                          <input type="text" placeholder={t('crm_deal_name_placeholder')} value={d.name || ''}
                            onChange={(e) => setD('name', e.target.value)} className={inputCls} />
                          <input type="text" placeholder={t('crm_deal_value_placeholder')} value={d.value || ''}
                            onChange={(e) => setD('value', e.target.value)} className={inputCls} />
                        </>
                      ) : (
                        <>
                          <input type="text" placeholder={t('crm_entity_id_placeholder')} value={d.entity_id || ''}
                            onChange={(e) => setD('entity_id', e.target.value)} className={inputCls} />
                          <textarea placeholder={t('crm_note_text_placeholder')} value={d.note_text || ''}
                            onChange={(e) => setD('note_text', e.target.value)} className={inputCls} rows={3} />
                        </>
                      )}
                      <p className="text-xs text-amber-400/70">{t('crm_writeback_env_hint')}</p>
                    </div>
                  )
                })()}

                {newAutomation.action_type === 'integration_tool' && (() => {
                  const d = newAutomation.action_data
                  const inputCls = 'w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm'
                  return (
                    <div className="space-y-3">
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('integration_tool_name_label')}</label>
                        <input type="text" placeholder="slack_send_message / hubspot_create_deal / …"
                          value={d.tool_name || ''}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...d, tool_name: e.target.value } })}
                          className={inputCls} />
                      </div>
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('integration_tool_args_label')}</label>
                        <textarea
                          placeholder='{"channel": "#general", "text": "..."}'
                          defaultValue={JSON.stringify(d.tool_args || {}, null, 2)}
                          onChange={(e) => {
                            try {
                              const parsed = JSON.parse(e.target.value)
                              setNewAutomation({ ...newAutomation, action_data: { ...d, tool_args: parsed } })
                            } catch { /* невалидный JSON — ждём, пока допечатают */ }
                          }}
                          className={`${inputCls} font-mono resize-none`} rows={4} />
                      </div>
                      <p className="text-[10px] text-brain-500">{t('integration_tool_hint')}</p>
                    </div>
                  )
                })()}

                {newAutomation.action_type === 'run_analysis' && (
                  <div className="space-y-3">
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('analysis_playbook_label')}</label>
                      <select
                        value={newAutomation.action_data.playbook_id || ''}
                        onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, playbook_id: e.target.value } })}
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
                        <option value="">{t('analysis_playbook_placeholder')}</option>
                        {analysisPlaybooks.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                      </select>
                      {analysisPlaybooks.length === 0 && (
                        <p className="text-xs text-amber-400/70 mt-1">{t('analysis_no_playbooks')}</p>
                      )}
                    </div>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('analysis_docs_label')}</label>
                      <div className="max-h-32 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2 space-y-1">
                        {analysisDocs.length === 0 ? (
                          <div className="text-xs text-brain-500">{t('analysis_no_docs')}</div>
                        ) : analysisDocs.map(doc => {
                          const ids: string[] = newAutomation.action_data.document_ids || []
                          return (
                            <label key={doc.id} className="flex items-center gap-2 cursor-pointer">
                              <input type="checkbox" checked={ids.includes(doc.id)}
                                onChange={(e) => {
                                  const next = e.target.checked ? [...ids, doc.id] : ids.filter((x) => x !== doc.id)
                                  setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, document_ids: next } })
                                }}
                                className="w-4 h-4 rounded bg-brain-700 border-brain-600 text-blue-500" />
                              <span className="text-sm text-brain-200 truncate">{doc.title}</span>
                            </label>
                          )
                        })}
                      </div>
                      <p className="text-xs text-brain-500 mt-1">{t('analysis_result_hint')}</p>
                    </div>
                  </div>
                )}

                {newAutomation.action_type === 'run_meeting_pipeline' && (
                  <div className="space-y-3">
                    <p className="text-xs text-brain-500">
                      {t('pipeline_intro')}
                    </p>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('pipeline_meeting_label')}</label>
                      <select
                        value={newAutomation.action_data.meeting_id || ''}
                        onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, meeting_id: e.target.value, task_titles: [] } })}
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
                        <option value="">{t('pipeline_meeting_placeholder')}</option>
                        {meetings.map(m => <option key={m.id} value={m.id}>{m.display}</option>)}
                      </select>
                      <p className="text-xs text-brain-500 mt-1">{t('pipeline_event_hint')}</p>
                    </div>
                    {newAutomation.action_data.meeting_id && (
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('pipeline_tasks_label')}</label>
                        <div className="max-h-32 overflow-y-auto rounded-lg bg-brain-800 border border-brain-700 p-2 space-y-1">
                          {loadingPipelineTasks ? (
                            <div className="text-xs text-brain-500">{t('pipeline_loading_tasks')}</div>
                          ) : pipelineTasks.length === 0 ? (
                            <div className="text-xs text-brain-500">{t('pipeline_no_tasks')}</div>
                          ) : pipelineTasks.map((tk, i) => {
                            // Выбор по task_id (стабильная сущность): бэкенд грузит
                            // ИМЕННО эти задачи и не пере-извлекает. task_titles
                            // шлём параллельно как точный фолбэк. Ключ — id, иначе
                            // (нет id) заголовок.
                            const selIds: string[] = newAutomation.action_data.task_ids || []
                            const selTitles: string[] = newAutomation.action_data.task_titles || []
                            const key = tk.task_id || tk.title
                            const checked = tk.task_id ? selIds.includes(tk.task_id) : selTitles.includes(tk.title)
                            return (
                              <label key={key || i} className="flex items-center gap-2 cursor-pointer">
                                <input type="checkbox" checked={checked}
                                  onChange={(e) => {
                                    const on = e.target.checked
                                    const nextIds = tk.task_id
                                      ? (on ? [...selIds, tk.task_id] : selIds.filter((x) => x !== tk.task_id))
                                      : selIds
                                    const nextTitles = on ? [...selTitles, tk.title] : selTitles.filter((x) => x !== tk.title)
                                    setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, task_ids: nextIds, task_titles: nextTitles } })
                                  }}
                                  className="w-4 h-4 rounded bg-brain-700 border-brain-600 text-blue-500" />
                                <span className="text-sm text-brain-200 truncate">{tk.title}{tk.assignee ? ` · ${tk.assignee}` : ''}</span>
                              </label>
                            )
                          })}
                        </div>
                      </div>
                    )}
                    <div className="flex gap-2">
                      <div className="flex-1">
                        <label className="block text-xs text-brain-500 mb-1">{t('pipeline_max_tasks_label')}</label>
                        <input type="number" min={1} max={20}
                          value={newAutomation.action_data.max_tasks || 5}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, max_tasks: parseInt(e.target.value) || 5 } })}
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                      </div>
                      <div className="flex-1">
                        <label className="block text-xs text-brain-500 mb-1">{t('pipeline_assignee_filter_label')}</label>
                        <input type="text" placeholder={t('pipeline_assignee_placeholder')}
                          value={newAutomation.action_data.assignee_filter || ''}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, assignee_filter: e.target.value } })}
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('pipeline_dispatch_label')}</label>
                      <select
                        value={newAutomation.action_data.dispatch_mode || 'deliver'}
                        onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, dispatch_mode: e.target.value } })}
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
                        <option value="deliver">{t('dispatch_deliver_option')}</option>
                        <option value="gate">{t('dispatch_gate_option')}</option>
                        <option value="web">{t('dispatch_web_option')}</option>
                        <option value="auto">{t('dispatch_auto_option')}</option>
                      </select>
                      {newAutomation.action_data.dispatch_mode === 'auto' && (
                        <p className="text-xs text-amber-400/80 mt-1">{t('dispatch_auto_warning')}</p>
                      )}
                      {newAutomation.action_data.dispatch_mode === 'gate' && (
                        <p className="text-xs text-brain-500 mt-1">{t('dispatch_gate_hint')}</p>
                      )}
                      {newAutomation.action_data.dispatch_mode === 'web' && (
                        <p className="text-xs text-brain-500 mt-1">{t('dispatch_web_hint')}</p>
                      )}
                    </div>
                    {(newAutomation.action_data.dispatch_mode === 'gate' || newAutomation.action_data.dispatch_mode === 'auto') && (
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('cli_agent_label')}</label>
                        <select
                          value={newAutomation.action_data.handoff_agent || 'claude'}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, handoff_agent: e.target.value } })}
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
                          <option value="claude">Claude Code</option>
                          <option value="cursor">Cursor</option>
                          <option value="codex">Codex</option>
                        </select>
                      </div>
                    )}
                    {newAutomation.action_data.dispatch_mode === 'web' && (
                      <div>
                        <label className="block text-xs text-brain-500 mb-1">{t('web_tool_label')}</label>
                        <select
                          value={newAutomation.action_data.web_target || ''}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, web_target: e.target.value } })}
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
                          <option value="">{t('web_tool_auto_option')}</option>
                          <option value="lovable">{t('web_tool_lovable_option')}</option>
                          <option value="v0">{t('web_tool_v0_option')}</option>
                          <option value="bolt">{t('web_tool_bolt_option')}</option>
                          <option value="replit">Replit Agent</option>
                          <option value="claude">{t('web_tool_claude_option')}</option>
                          <option value="chatgpt">{t('web_tool_chatgpt_option')}</option>
                        </select>
                        <p className="text-xs text-brain-500 mt-1">{t('web_tool_prefill_hint')}</p>
                      </div>
                    )}
                    <div className="rounded-lg bg-brain-800/50 border border-brain-700 p-2 space-y-2">
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input type="checkbox" checked={!!newAutomation.action_data.create_tracker_task}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, create_tracker_task: e.target.checked } })}
                          className="w-4 h-4 rounded bg-brain-700 border-brain-600 text-blue-500" />
                        <span className="text-sm text-brain-200">{t('tracker_create_task_label')}</span>
                      </label>
                      {newAutomation.action_data.create_tracker_task && (
                        <div className="space-y-2 pl-6">
                          <div className="flex gap-2">
                            <select
                              value={newAutomation.action_data.tracker || 'yougile'}
                              onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, tracker: e.target.value } })}
                              className="px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
                              <option value="yougile">YouGile</option>
                              <option value="trello">Trello</option>
                              <option value="jira">Jira</option>
                            </select>
                            <input type="text"
                              placeholder={newAutomation.action_data.tracker === 'jira' ? t('tracker_jira_key_placeholder') : (newAutomation.action_data.tracker === 'trello' ? 'list_id' : 'column id')}
                              value={newAutomation.action_data.tracker_column_id || ''}
                              onFocus={() => loadTrackerCols(newAutomation.action_data.tracker || 'yougile')}
                              onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, tracker_column_id: e.target.value } })}
                              list="tracker-cols-list"
                              className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                            <datalist id="tracker-cols-list">
                              {(trackerCols[newAutomation.action_data.tracker || 'yougile'] || []).map((c) => (
                                <option key={c.id} value={c.id}>{c.board ? `${c.board} / ${c.name}` : c.name}</option>
                              ))}
                            </datalist>
                          </div>
                          <label className="flex items-center gap-2 cursor-pointer">
                            <input type="checkbox" checked={!!newAutomation.action_data.tracker_mark_done}
                              onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, tracker_mark_done: e.target.checked } })}
                              className="w-4 h-4 rounded bg-brain-700 border-brain-600 text-blue-500" />
                            <span className="text-sm text-brain-200">{t('tracker_mark_done_label')}</span>
                          </label>
                          {newAutomation.action_data.tracker_mark_done && (
                            <input type="text"
                              placeholder={newAutomation.action_data.tracker === 'jira' ? t('tracker_jira_done_placeholder') : t('tracker_done_column_placeholder')}
                              value={newAutomation.action_data.tracker === 'jira' ? (newAutomation.action_data.jira_done_status || '') : (newAutomation.action_data.tracker_done_column_id || '')}
                              onChange={(e) => {
                                const key = newAutomation.action_data.tracker === 'jira' ? 'jira_done_status' : 'tracker_done_column_id'
                                setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, [key]: e.target.value } })
                              }}
                              className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                          )}
                          <p className="text-xs text-brain-500">{t('tracker_limitations_hint')}</p>
                        </div>
                      )}
                    </div>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('pipeline_delivery_label')}</label>
                      <div className="flex gap-2">
                        <select
                          value={newAutomation.action_data.channel || 'telegram'}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, channel: e.target.value } })}
                          className="px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
                          <option value="telegram">Telegram</option>
                          <option value="email">Email</option>
                          <option value="slack">Slack</option>
                        </select>
                        <input type="text"
                          placeholder={
                            (newAutomation.action_data.channel || 'telegram') === 'email' ? t('recipient_email_placeholder')
                            : (newAutomation.action_data.channel === 'slack' ? t('recipient_slack_placeholder') : t('recipient_chat_id_placeholder'))
                          }
                          value={
                            (newAutomation.action_data.channel || 'telegram') === 'email' ? (newAutomation.action_data.to_email || '')
                            : (newAutomation.action_data.channel === 'slack' ? (newAutomation.action_data.slack_channel || '') : (newAutomation.action_data.chat_id || ''))
                          }
                          onChange={(e) => {
                            const ch = newAutomation.action_data.channel || 'telegram'
                            const key = ch === 'email' ? 'to_email' : (ch === 'slack' ? 'slack_channel' : 'chat_id')
                            setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, [key]: e.target.value } })
                          }}
                          className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                      </div>
                      <p className="text-xs text-brain-500 mt-1">{t('pipeline_delivery_hint')}</p>
                    </div>
                  </div>
                )}

                {newAutomation.action_type === 'scheduled_research' && (
                  <div className="space-y-3">
                    <p className="text-xs text-brain-500">{t('research_intro')}</p>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('research_queries_label')}</label>
                      <textarea rows={2} placeholder={t('research_queries_placeholder')}
                        value={(newAutomation.action_data.sources?.search_queries || []).join('\n')}
                        onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, sources: { ...(newAutomation.action_data.sources || {}), search_queries: e.target.value.split('\n').map((s: string) => s.trim()).filter(Boolean) } } })}
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                    </div>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('research_urls_label')}</label>
                      <textarea rows={2} placeholder="https://vc.ru/marketing&#10;https://www.sostav.ru/"
                        value={(newAutomation.action_data.sources?.urls || []).join('\n')}
                        onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, sources: { ...(newAutomation.action_data.sources || {}), urls: e.target.value.split('\n').map((s: string) => s.trim()).filter(Boolean) } } })}
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                    </div>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('research_channels_label')}</label>
                      <textarea rows={2} placeholder="@cossa&#10;@adindex"
                        value={(newAutomation.action_data.sources?.telegram_channels || []).join('\n')}
                        onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, sources: { ...(newAutomation.action_data.sources || {}), telegram_channels: e.target.value.split('\n').map((s: string) => s.trim()).filter(Boolean) } } })}
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                      <p className="text-xs text-brain-500 mt-1">{t('research_channels_hint')}</p>
                    </div>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('research_lens_label')}</label>
                      <textarea rows={3} placeholder={t('research_lens_placeholder')}
                        value={newAutomation.action_data.lens || ''}
                        onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, lens: e.target.value } })}
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                    </div>
                    {researchDelivery(newAutomation, setNewAutomation, t)}
                  </div>
                )}

                {newAutomation.action_type === 'chat_digest' && (
                  <div className="space-y-3">
                    <p className="text-xs text-brain-500">{t('digest_intro')}</p>
                    <div className="flex gap-2">
                      <div className="flex-1">
                        <label className="block text-xs text-brain-500 mb-1">{t('digest_source_label')}</label>
                        <select
                          value={newAutomation.action_data.source || 'slack'}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, source: e.target.value } })}
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
                          <option value="slack">Slack</option>
                          <option value="telegram_group">{t('digest_source_telegram_group')}</option>
                        </select>
                      </div>
                      <div className="flex-1">
                        <label className="block text-xs text-brain-500 mb-1">{newAutomation.action_data.source === 'telegram_group' ? t('digest_tg_chat_id_label') : t('digest_slack_channel_label')}</label>
                        <input type="text" placeholder={newAutomation.action_data.source === 'telegram_group' ? '-100…' : t('digest_slack_channel_placeholder')}
                          value={newAutomation.action_data.channel || ''}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, channel: e.target.value } })}
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                      </div>
                    </div>
                    {newAutomation.action_data.source === 'telegram_group' && (
                      <p className="text-xs text-amber-400/80">{t('digest_tg_userbot_warning')}</p>
                    )}
                    {newAutomation.action_data.source === 'slack' && (
                      <p className="text-xs text-brain-500">{t('digest_slack_token_hint')}</p>
                    )}
                    <div className="flex gap-2">
                      <div className="flex-1">
                        <label className="block text-xs text-brain-500 mb-1">{t('digest_window_label')}</label>
                        <input type="number" min={1} max={60}
                          value={newAutomation.action_data.window_days || 7}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, window_days: parseInt(e.target.value) || 7 } })}
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                      </div>
                      <div className="flex-1">
                        <label className="block text-xs text-brain-500 mb-1">{t('digest_mode_label')}</label>
                        <select
                          value={newAutomation.action_data.mode || 'retrospective'}
                          onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, mode: e.target.value } })}
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm">
                          <option value="retrospective">{t('digest_mode_retrospective')}</option>
                          <option value="daily_analytics">{t('digest_mode_daily_analytics')}</option>
                        </select>
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs text-brain-500 mb-1">{t('digest_lens_label')}</label>
                      <textarea rows={2} placeholder=""
                        value={newAutomation.action_data.lens || ''}
                        onChange={(e) => setNewAutomation({ ...newAutomation, action_data: { ...newAutomation.action_data, lens: e.target.value } })}
                        className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm" />
                    </div>
                    {researchDelivery(newAutomation, setNewAutomation, t)}
                  </div>
                )}
              </div>

              {/* Schedule Type */}
              <div>
                <label className="block text-xs text-brain-400 mb-1">{t('schedule_type_label')}</label>
                <div className="flex gap-2">
                  <button
                    onClick={() => setNewAutomation({ ...newAutomation, schedule_type: 'once' })}
                    className={`flex-1 px-3 py-2 rounded-lg text-sm transition-colors ${
                      newAutomation.schedule_type === 'once'
                        ? 'bg-brain-600 text-white'
                        : 'bg-brain-800 text-brain-400 hover:bg-brain-700'
                    }`}
                  >
                    <Timer className="w-4 h-4 inline mr-2" />
                    {t('schedule_once')}
                  </button>
                  <button
                    onClick={() => {
                      setNewAutomation({
                        ...newAutomation,
                        schedule_type: 'recurring',
                        // сразу валидное расписание из текущего пресета —
                        // cron-строку руками писать не нужно
                        cron_expression: schedPreset !== 'custom'
                          ? presetToCron(schedPreset, schedTime, schedDow)
                          : newAutomation.cron_expression,
                      })
                    }}
                    className={`flex-1 px-3 py-2 rounded-lg text-sm transition-colors ${
                      newAutomation.schedule_type === 'recurring'
                        ? 'bg-brain-600 text-white'
                        : 'bg-brain-800 text-brain-400 hover:bg-brain-700'
                    }`}
                  >
                    <Repeat className="w-4 h-4 inline mr-2" />
                    {t('schedule_recurring')}
                  </button>
                </div>
              </div>

              {/* Schedule Details */}
              {newAutomation.schedule_type === 'once' && (
                <div>
                  <label className="block text-xs text-brain-400 mb-1">{t('when_execute_label')}</label>
                  <input
                    type="datetime-local"
                    value={newAutomation.execute_at}
                    onChange={(e) => setNewAutomation({ ...newAutomation, execute_at: e.target.value })}
                    className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                  />
                  <p className="text-xs text-brain-500 mt-1">{t('once_datetime_hint')}</p>
                </div>
              )}

              {newAutomation.schedule_type === 'recurring' && (
                <div className="space-y-3">
                  {/* Как часто — пресеты вместо cron-строки */}
                  <div>
                    <label className="block text-xs text-brain-400 mb-1.5">{t('how_often_label')}</label>
                    <div className="flex flex-wrap gap-2">
                      {SCHEDULE_PRESETS.map(p => (
                        <button
                          key={p.id}
                          type="button"
                          onClick={() => applySchedulePreset(p.id, schedTime, schedDow)}
                          className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                            schedPreset === p.id
                              ? 'bg-brain-600 text-white'
                              : 'bg-brain-800 text-brain-400 hover:bg-brain-700'
                          }`}
                        >
                          {t(p.labelKey)}
                        </button>
                      ))}
                    </div>
                  </div>

                  {schedPreset !== 'custom' && (
                    <div className="flex gap-3 items-end">
                      {schedPreset === 'weekly' && (
                        <div>
                          <label className="block text-xs text-brain-400 mb-1">{t('day_of_week_label')}</label>
                          <select
                            value={schedDow}
                            onChange={(e) => applySchedulePreset(schedPreset, schedTime, e.target.value)}
                            className="px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500"
                          >
                            {DOW_OPTIONS.map(d => (
                              <option key={d.value} value={d.value}>{t(d.labelKey)}</option>
                            ))}
                          </select>
                        </div>
                      )}
                      <div>
                        <label className="block text-xs text-brain-400 mb-1">{t('time_label')}</label>
                        <input
                          type="time"
                          value={schedTime}
                          onChange={(e) => applySchedulePreset(schedPreset, e.target.value, schedDow)}
                          className="px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500"
                        />
                      </div>
                      <div className="text-sm text-brain-300 pb-2">
                        → {cronToHuman(newAutomation.cron_expression, t)}
                      </div>
                    </div>
                  )}

                  {schedPreset === 'custom' && (
                    <div className="space-y-3">
                      <div>
                        <label className="block text-xs text-brain-400 mb-1">{t('cron_expression_label')}</label>
                        <input
                          type="text"
                          value={newAutomation.cron_expression}
                          onChange={(e) => setNewAutomation({ ...newAutomation, cron_expression: e.target.value })}
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          placeholder={t('cron_placeholder')}
                        />
                        <p className="text-[10px] text-brain-500 mt-1">
                          {newAutomation.cron_expression
                            ? `→ ${cronToHuman(newAutomation.cron_expression, t)}`
                            : t('cron_format_hint')}
                        </p>
                      </div>
                      <div className="text-xs text-brain-500 text-center">{t('or_separator')}</div>
                      <div>
                        <label className="block text-xs text-brain-400 mb-1">{t('interval_seconds_label')}</label>
                        <input
                          type="number"
                          value={newAutomation.interval_seconds || ''}
                          onChange={(e) =>
                            setNewAutomation({ ...newAutomation, interval_seconds: parseInt(e.target.value) || 0, cron_expression: '' })
                          }
                          className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm"
                          placeholder={t('interval_placeholder')}
                        />
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>

            {createError && (
              <div className="mx-4 mb-1 flex items-start gap-2 text-sm text-red-300 bg-red-900/30 border border-red-700/40 rounded-lg px-3 py-2">
                <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                <span>{createError}</span>
              </div>
            )}
            <div className="p-4 border-t border-brain-700/30 flex justify-end gap-2">
              <button
                onClick={resetAndCloseModal}
                className="px-4 py-2 rounded-lg bg-brain-800 text-brain-400 hover:bg-brain-700 text-sm transition-colors"
              >
                {t('cancel_button')}
              </button>
              <button
                onClick={handleCreate}
                disabled={!newAutomation.name || !newAutomation.action_type}
                className="px-4 py-2 rounded-lg bg-brain-600 text-white hover:bg-brain-500 text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {t('submit_button')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function TemplateConfigModal({
  template,
  onClose,
  onSubmit,
  meetings = [],
  integrations = [],
}: {
  template: WorkflowTemplate
  onClose: () => void
  onSubmit: (params: Record<string, any>) => void
  meetings?: Meeting[]
  integrations?: Integration[]
}) {
  const t = useTranslations('automations')
  const PARAM_LABELS = buildParamLabels(t)
  const [params, setParams] = useState<Record<string, any>>(template.parameters || {})
  const [loading, setLoading] = useState(false)
  // Для meeting-шаблонов: «ко всем новым» (событие) / «из папки» (событие
  // с фильтром по проекту/папке) / «конкретной» (разово)
  const isMeeting = template.category === 'meeting_processing'
  const [applyTo, setApplyTo] = useState<'all_new' | 'folder' | 'meeting'>('all_new')
  const [customName, setCustomName] = useState('')
  const [projects, setProjects] = useState<Array<{ project_id: string; name: string }>>([])
  const [folders, setFolders] = useState<Array<{ id: string; name: string; project_id: string }>>([])
  const [selProject, setSelProject] = useState('')
  const [selFolder, setSelFolder] = useState('')

  // проекты и папки для фильтра «из папки»
  useEffect(() => {
    if (!isMeeting || applyTo !== 'folder' || projects.length > 0) return
    ;(async () => {
      try {
        const res = await authFetch('/api/v1/automations/projects-list')
        const data = await res.json()
        setProjects(data.projects || [])
        setFolders(data.folders || [])
      } catch { /* оставим пустым */ }
    })()
  }, [isMeeting, applyTo, projects.length])

  const handleSubmit = async () => {
    setLoading(true)
    await onSubmit({
      ...params,
      apply_to: isMeeting ? applyTo : undefined,
      project_id: applyTo === 'folder' ? (selProject || undefined) : undefined,
      folder_id: applyTo === 'folder' ? (selFolder || undefined) : undefined,
      name: customName || undefined,
    })
    setLoading(false)
  }

  // Get contacts for specific provider
  const getTelegramContacts = () => integrations.find(i => i.provider === 'telegram')?.contacts || []
  const getEmailContacts = () => integrations.find(i => i.provider === 'gmail')?.contacts || []

  const getFieldsForTemplate = () => {
    const fields: { key: string; label: string; type: string; placeholder?: string; hint?: string; options?: {value: string; label: string}[] }[] = []

    // Выбор конкретной встречи — ТОЛЬКО если пользователь выбрал «конкретной»
    if (isMeeting && applyTo === 'meeting') {
      fields.push({
        key: 'meeting_id',
        label: t('field_which_meeting_label'),
        type: 'select',
        placeholder: t('field_meeting_placeholder'),
        options: meetings.map(m => ({ value: m.id, label: m.display }))
      })
    }

    if (template.action_type.includes('telegram')) {
      const contacts = getTelegramContacts()
      if (contacts.length > 0) {
        fields.push({
          key: 'chat_id',
          label: t('field_telegram_dest_label'),
          type: 'select',
          placeholder: t('field_telegram_default_placeholder'),
          options: contacts.map(c => ({ value: c.value, label: c.display }))
        })
      } else {
        fields.push({ key: 'chat_id', label: t('field_telegram_dest_label'), type: 'text',
          placeholder: t('field_telegram_empty_placeholder'),
          hint: t('field_telegram_hint') })
      }
    }

    if (template.action_type === 'chat_digest') {
      const isTg = params.source === 'telegram_group'
      fields.push({
        key: 'channel',
        label: isTg ? t('field_tg_group_chat_id_label') : t('field_slack_channel_id_label'),
        type: 'text',
        placeholder: isTg ? '-100…' : t('field_slack_channel_id_placeholder'),
        hint: isTg
          ? t('field_userbot_hint')
          : t('field_slack_token_hint'),
      })
    }

    if (template.action_type === 'scheduled_research') {
      fields.push({
        key: 'lens', label: t('research_lens_label'),
        type: 'text',
        placeholder: t('field_lens_placeholder'),
        hint: t('field_lens_hint'),
      })
    }

    if (template.action_type.includes('yougile')) {
      // Универсальный «задачи → трекер»: YouGile / Trello / Jira
      fields.push({
        key: 'tracker', label: t('field_tracker_label'), type: 'select',
        placeholder: t('field_tracker_default_placeholder'),
        options: [
          { value: 'yougile', label: 'YouGile' },
          { value: 'trello', label: 'Trello' },
          { value: 'jira', label: 'Jira' },
          { value: 'linear', label: 'Linear' },
        ],
        hint: t('field_tracker_hint'),
      })
      const tracker = params.tracker || 'yougile'
      if (tracker === 'yougile') {
        fields.push({ key: 'column_id', label: t('yougile_column_label'), type: 'text', placeholder: t('field_yougile_column_placeholder'), hint: t('yougile_column_hint') })
      } else if (tracker === 'trello') {
        fields.push({ key: 'trello_list_id', label: t('trello_list_label'), type: 'text', placeholder: t('trello_list_placeholder'), hint: t('trello_list_hint') })
      } else if (tracker === 'jira') {
        fields.push({ key: 'jira_project', label: t('jira_project_label'), type: 'text', placeholder: t('jira_project_placeholder'), hint: t('jira_project_hint') })
      } else if (tracker === 'linear') {
        fields.push({ key: 'linear_team_id', label: t('linear_team_label'), type: 'text', placeholder: t('linear_team_placeholder'), hint: t('linear_team_hint') })
      }
    }

    if (template.action_type.includes('email')) {
      const contacts = getEmailContacts()
      if (contacts.length > 0) {
        fields.push({
          key: 'to_email',
          label: t('field_to_email_label'),
          type: 'select',
          placeholder: t('field_to_email_placeholder'),
          options: contacts.map(c => ({ value: c.value, label: c.display }))
        })
      } else {
        fields.push({ key: 'to_email', label: t('field_to_email_label'), type: 'email', placeholder: t('field_to_email_direct_placeholder'), hint: t('emails_multiple_hint') })
      }
    }

    // Ролевые разборы встреч и алерты: получатель в Telegram + опции
    const roleInsightIds = ['commitments_tracker', 'client_risk_alert',
      'sales_call_scorecard', 'voice_of_customer', 'one_on_one_pulse',
      'call_qa_review', 'exec_weekly_brief']
    if (roleInsightIds.includes(template.id)) {
      fields.push({
        key: 'chat_id', label: t('field_telegram_send_to_label'), type: 'text',
        placeholder: t('field_telegram_empty_default_placeholder'),
        hint: t('field_multiple_chats_hint'),
      })
      if (template.id === 'client_risk_alert') {
        fields.push({
          key: 'min_level', label: t('field_alert_when_label'), type: 'select',
          placeholder: t('field_alert_default_placeholder'),
          options: [
            { value: 'high', label: t('alert_high_only') },
            { value: 'medium', label: t('alert_medium_up') },
            { value: 'low', label: t('alert_any') },
          ],
        })
      }
      if (['sales_call_scorecard', 'voice_of_customer', 'one_on_one_pulse', 'call_qa_review'].includes(template.id)) {
        fields.push({
          key: 'custom_prompt', label: t('field_custom_wishes_label'), type: 'textarea',
          placeholder: t('field_custom_wishes_placeholder'),
        })
      }
    }

    // Бизнес-сценарии: клиентский фоллоу-ап и отчёт по кандидату
    if (template.id === 'client_followup' || template.id === 'hr_candidate_report') {
      fields.push({
        key: 'channel', label: t('where_send_label'), type: 'select',
        placeholder: t('field_email_default_placeholder'),
        options: [
          { value: 'email', label: t('channel_email') },
          { value: 'telegram', label: t('channel_telegram') },
        ],
      })
      if ((params.channel || 'email') === 'email') {
        fields.push({
          key: 'to_email',
          label: template.id === 'client_followup' ? t('field_client_email_label') : t('field_hiring_manager_email_label'),
          type: 'email',
          placeholder: 'client@company.ru',
          hint: t('emails_multiple_hint'),
        })
      } else {
        fields.push({
          key: 'chat_id', label: t('telegram_chat_label'), type: 'text',
          placeholder: t('field_telegram_empty_default_placeholder'),
          hint: t('field_multiple_chats_group_hint'),
        })
      }
      if (template.id === 'client_followup') {
        fields.push({
          key: 'custom_prompt', label: t('field_letter_style_label'), type: 'textarea',
          placeholder: t('field_letter_style_placeholder'),
          hint: t('field_letter_style_hint'),
        })
      } else {
        fields.push({
          key: 'role_description', label: t('field_role_description_label'), type: 'text',
          placeholder: t('field_role_description_placeholder'),
        })
        fields.push({
          key: 'recruiter_notes', label: t('field_recruiter_notes_label'), type: 'textarea',
          placeholder: t('field_recruiter_notes_placeholder'),
          hint: t('field_recruiter_notes_hint'),
        })
      }
    }

    if (template.action_type.includes('notion')) {
      fields.push({ key: 'parent_page_id', label: t('field_notion_page_label'), type: 'text', placeholder: t('field_notion_page_placeholder') })
    }

    return fields
  }

  const fields = getFieldsForTemplate()

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-brain-900 rounded-xl border border-brain-700/50 w-full max-w-lg max-h-[90vh] overflow-y-auto m-4">
        <div className="p-4 border-b border-brain-700/30">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-gradient-to-r from-purple-600/20 to-pink-600/20">
                {ACTION_ICONS[template.action_type] || <Zap className="w-5 h-5 text-purple-400" />}
              </div>
              <div>
                <h3 className="text-lg font-semibold text-brain-100">{template.name}</h3>
                <p className="text-xs text-brain-400">{template.description}</p>
              </div>
            </div>
            <button onClick={onClose} className="p-2 rounded-lg hover:bg-brain-800 text-brain-400">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="p-4 space-y-4">
          {/* Что делает — данные и адресат понятным языком */}
          <div className="p-3 rounded-lg bg-blue-950/20 border border-blue-800/30 text-sm text-brain-200 space-y-1">
            <div className="flex gap-2"><span className="text-brain-500 shrink-0">{t('template_what_does_label')}</span><span>{template.description}</span></div>
            <div className="flex gap-2"><span className="text-brain-500 shrink-0">{t('template_when_label')}</span><span>{
              isMeeting && applyTo === 'all_new' ? t('after_each_processed_meeting')
              : template.default_schedule === 'recurring' ? cronToHuman(template.default_cron, t)
              : t('by_selected_meeting')
            }</span></div>
          </div>

          {/* meeting_processing: ко всем новым / из папки / к конкретной */}
          {isMeeting && (
            <div>
              <label className="block text-xs text-brain-400 mb-1.5">{t('apply_to_label')}</label>
              <div className="flex flex-col sm:flex-row gap-2">
                <button onClick={() => setApplyTo('all_new')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm border transition-all text-left ${applyTo === 'all_new' ? 'bg-blue-600 border-blue-600 text-white' : 'border-brain-700/50 text-brain-300 hover:border-blue-500/40'}`}>
                  {t('apply_all_new')}
                </button>
                <button onClick={() => setApplyTo('folder')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm border transition-all text-left ${applyTo === 'folder' ? 'bg-blue-600 border-blue-600 text-white' : 'border-brain-700/50 text-brain-300 hover:border-blue-500/40'}`}>
                  {t('apply_from_folder')}
                </button>
                <button onClick={() => setApplyTo('meeting')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm border transition-all text-left ${applyTo === 'meeting' ? 'bg-blue-600 border-blue-600 text-white' : 'border-brain-700/50 text-brain-300 hover:border-blue-500/40'}`}>
                  {t('apply_one_meeting')} <span className="opacity-70">{t('apply_once_suffix')}</span>
                </button>
              </div>
              <p className="text-xs text-brain-500 mt-1">
                {applyTo === 'all_new'
                  ? t('apply_all_new_hint')
                  : applyTo === 'folder'
                  ? t('apply_folder_hint')
                  : t('apply_meeting_hint')}
              </p>

              {applyTo === 'folder' && (
                <div className="mt-2 space-y-2">
                  <select value={selProject}
                    onChange={(e) => { setSelProject(e.target.value); setSelFolder('') }}
                    className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500">
                    <option value="">{t('select_project_placeholder')}</option>
                    {projects.map(p => (
                      <option key={p.project_id} value={p.project_id}>{p.name}</option>
                    ))}
                  </select>
                  {selProject && folders.some(f => f.project_id === selProject) && (
                    <select value={selFolder}
                      onChange={(e) => setSelFolder(e.target.value)}
                      className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500">
                      <option value="">{t('whole_project_option')}</option>
                      {folders.filter(f => f.project_id === selProject).map(f => (
                        <option key={f.id} value={f.id}>{f.name}</option>
                      ))}
                    </select>
                  )}
                  {projects.length === 0 && (
                    <p className="text-xs text-amber-400/70">{t('projects_not_found_hint')}</p>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Название автоматизации */}
          <div>
            <label className="block text-xs text-brain-400 mb-1">{t('automation_name_label')}</label>
            <input type="text" value={customName} onChange={(e) => setCustomName(e.target.value)}
              placeholder={template.name}
              className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500" />
          </div>

          {/* Dynamic Fields */}
          {fields.map((field) => (
            <div key={field.key}>
              <label className="block text-xs text-brain-400 mb-1">{field.label}</label>
              {field.type === 'select' && field.options ? (
                <select
                  value={params[field.key] || ''}
                  onChange={(e) => setParams({ ...params, [field.key]: e.target.value })}
                  className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500"
                >
                  <option value="">{field.placeholder || t('select_placeholder')}</option>
                  {field.options.map(opt => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              ) : field.type === 'textarea' ? (
                <textarea
                  value={params[field.key] || ''}
                  onChange={(e) => setParams({ ...params, [field.key]: e.target.value })}
                  className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500 resize-none"
                  placeholder={field.placeholder}
                  rows={3}
                />
              ) : (
                <input
                  type={field.type}
                  value={params[field.key] || ''}
                  onChange={(e) => setParams({ ...params, [field.key]: e.target.value })}
                  className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500"
                  placeholder={field.placeholder}
                />
              )}
              {field.hint && <p className="text-xs text-amber-400/70 mt-1">{field.hint}</p>}
            </div>
          ))}

          {/* Библиотека готовых стилей для промпта (клик — заполняет поле) */}
          {template.id === 'client_followup' && (
            <div>
              <label className="block text-xs text-brain-400 mb-1.5">{t('letter_styles_label')}</label>
              <div className="flex flex-wrap gap-2">
                {[
                  { label: t('style_friendly_label'), p: t('style_friendly_prompt') },
                  { label: t('style_formal_label'), p: t('style_formal_prompt') },
                  { label: t('style_brief_label'), p: t('style_brief_prompt') },
                  { label: t('style_commercial_label'), p: t('style_commercial_prompt') },
                ].map((s) => (
                  <button key={s.label} type="button"
                    onClick={() => setParams({ ...params, custom_prompt: s.p })}
                    className={`px-3 py-1.5 rounded-lg text-xs border transition-all ${params.custom_prompt === s.p ? 'bg-blue-600 border-blue-600 text-white' : 'border-brain-700/50 text-brain-300 hover:border-blue-500/40'}`}>
                    {s.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Boolean Options — по-русски (что включить в отчёт) */}
          {Object.entries(template.parameters || {}).some(([, v]) => typeof v === 'boolean') && (
            <div>
              <label className="block text-xs text-brain-400 mb-1.5">{t('what_to_include_label')}</label>
              <div className="space-y-1.5">
                {Object.entries(template.parameters || {}).map(([key, value]) => {
                  if (typeof value !== 'boolean') return null
                  return (
                    <label key={key} className="flex items-center gap-3 cursor-pointer">
                      <input type="checkbox" checked={params[key] ?? value}
                        onChange={(e) => setParams({ ...params, [key]: e.target.checked })}
                        className="w-4 h-4 rounded bg-brain-800 border-brain-600 text-blue-500 focus:ring-blue-500" />
                      <span className="text-sm text-brain-300">{PARAM_LABELS[key] || key.replace(/_/g, ' ')}</span>
                    </label>
                  )
                })}
              </div>
            </div>
          )}
        </div>

        <div className="p-4 border-t border-brain-700/30 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg bg-brain-800 text-brain-400 hover:bg-brain-700 text-sm transition-colors"
          >
            {t('cancel_button')}
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-gradient-to-r from-purple-600 to-pink-600 hover:from-purple-500 hover:to-pink-500 text-white text-sm transition-all disabled:opacity-50"
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
            {t('submit_create_automation')}
          </button>
        </div>
      </div>
    </div>
  )
}

// Известные поля action_data с русскими подписями — редактируем их,
// остальные ключи сохраняются нетронутыми.
const buildEditFieldMeta = (t: Tr): Record<string, { label: string; textarea?: boolean }> => ({
  message: { label: t('edit_message_label'), textarea: true },
  chat_id: { label: t('edit_chat_id_label') },
  to_email: { label: t('edit_to_email_label') },
  to: { label: t('edit_to_label') },
  subject: { label: t('email_subject_placeholder') },
  body: { label: t('email_body_placeholder'), textarea: true },
  slack_channel: { label: t('edit_slack_channel_label') },
  custom_prompt: { label: t('edit_custom_prompt_label'), textarea: true },
  recruiter_notes: { label: t('edit_recruiter_notes_label'), textarea: true },
  role_description: { label: t('field_role_description_short_label') },
  min_level: { label: t('edit_min_level_label') },
  tracker: { label: t('edit_tracker_label') },
  column_id: { label: t('yougile_column_label') },
  trello_list_id: { label: t('trello_list_label') },
  jira_project: { label: t('jira_project_label') },
  linear_team_id: { label: t('edit_linear_team_label') },
  parent_page_id: { label: t('edit_parent_page_label') },
  title: { label: t('edit_title_label') },
  content: { label: t('content_label'), textarea: true },
})

function EditAutomationModal({
  automation,
  onClose,
  onSaved,
}: {
  automation: Automation
  onClose: () => void
  onSaved: () => void
}) {
  const t = useTranslations('automations')
  const PARAM_LABELS = buildParamLabels(t)
  const EDIT_FIELD_META = buildEditFieldMeta(t)
  const [name, setName] = useState(automation.name)
  const [description, setDescription] = useState(automation.description || '')
  const [data, setData] = useState<Record<string, any>>({ ...(automation.action_data || {}) })
  const isOnce = automation.schedule_type === 'once'
  const isEvent = (automation as any).trigger_type === 'event'
  const [executeAt, setExecuteAt] = useState((automation.execute_at || '').slice(0, 16))
  const [cron, setCron] = useState(automation.cron_expression || '')
  const [intervalSec, setIntervalSec] = useState(automation.interval_seconds || 0)
  const [schedTime, setSchedTime] = useState('09:00')
  const [schedDow, setSchedDow] = useState('1')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const textKeys = Object.keys(data).filter(k => EDIT_FIELD_META[k] && typeof data[k] !== 'boolean')
  const boolKeys = Object.keys(data).filter(k => typeof data[k] === 'boolean')

  const save = async () => {
    setSaving(true); setErr(null)
    try {
      const body: Record<string, any> = { name, description, action_data: data }
      if (isOnce && !isEvent && executeAt) body.execute_at = executeAt
      if (!isOnce && !isEvent) {
        if (cron) { body.cron_expression = cron; body.interval_seconds = 0 }
        else if (intervalSec) { body.interval_seconds = intervalSec }
      }
      const res = await authFetch(`/api/v1/automations/${automation.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (res.ok) { onSaved() }
      else {
        const d = await res.json().catch(() => ({}))
        setErr(d?.detail || d?.message || t('save_failed_http', { status: res.status }))
      }
    } catch {
      setErr(t('network_error'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-brain-900 rounded-xl border border-brain-700/50 w-full max-w-lg max-h-[90vh] overflow-y-auto m-4">
        <div className="p-4 border-b border-brain-700/30 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-brain-100">{t('edit_automation_title')}</h3>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-brain-800 text-brain-400">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <div>
            <label className="block text-xs text-brain-400 mb-1">{t('field_name')}</label>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500" />
          </div>
          <div>
            <label className="block text-xs text-brain-400 mb-1">{t('description_label')}</label>
            <input type="text" value={description} onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500" />
          </div>

          {textKeys.map((k) => (
            <div key={k}>
              <label className="block text-xs text-brain-400 mb-1">{EDIT_FIELD_META[k].label}</label>
              {EDIT_FIELD_META[k].textarea ? (
                <textarea value={String(data[k] ?? '')} rows={3}
                  onChange={(e) => setData({ ...data, [k]: e.target.value })}
                  className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500 resize-none" />
              ) : (
                <input type="text" value={String(data[k] ?? '')}
                  onChange={(e) => setData({ ...data, [k]: e.target.value })}
                  className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500" />
              )}
            </div>
          ))}

          {boolKeys.length > 0 && (
            <div>
              <label className="block text-xs text-brain-400 mb-1.5">{t('what_to_include_label')}</label>
              <div className="space-y-1.5">
                {boolKeys.map((k) => (
                  <label key={k} className="flex items-center gap-3 cursor-pointer">
                    <input type="checkbox" checked={!!data[k]}
                      onChange={(e) => setData({ ...data, [k]: e.target.checked })}
                      className="w-4 h-4 rounded bg-brain-800 border-brain-600 text-blue-500 focus:ring-blue-500" />
                    <span className="text-sm text-brain-300">{PARAM_LABELS[k] || k.replace(/_/g, ' ')}</span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {/* Расписание */}
          {isEvent ? (
            <p className="text-xs text-brain-500">{t('event_schedule_not_editable')}</p>
          ) : isOnce ? (
            <div>
              <label className="block text-xs text-brain-400 mb-1">{t('when_execute_label')}</label>
              <input type="datetime-local" value={executeAt}
                onChange={(e) => setExecuteAt(e.target.value)}
                className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500" />
            </div>
          ) : (
            <div className="space-y-2">
              <label className="block text-xs text-brain-400">{t('how_often_label')} <span className="text-brain-500">{t('currently_schedule', { schedule: cronToHuman(cron, t) })}</span></label>
              <div className="flex flex-wrap gap-2">
                {SCHEDULE_PRESETS.filter(p => p.id !== 'custom').map(p => (
                  <button key={p.id} type="button"
                    onClick={() => setCron(presetToCron(p.id, schedTime, schedDow))}
                    className="px-3 py-1.5 rounded-lg text-sm bg-brain-800 text-brain-400 hover:bg-brain-700 transition-colors">
                    {t(p.labelKey)}
                  </button>
                ))}
              </div>
              <div className="flex gap-3 items-center">
                <input type="time" value={schedTime}
                  onChange={(e) => setSchedTime(e.target.value)}
                  className="px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500" />
                <select value={schedDow} onChange={(e) => setSchedDow(e.target.value)}
                  className="px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500">
                  {DOW_OPTIONS.map(d => (
                    <option key={d.value} value={d.value}>{t(d.labelKey)}</option>
                  ))}
                </select>
                <span className="text-xs text-brain-500">{t('time_day_for_buttons_hint')}</span>
              </div>
              <input type="text" value={cron} onChange={(e) => setCron(e.target.value)}
                placeholder={t('or_cron_placeholder')}
                className="w-full px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-brain-500" />
            </div>
          )}

          {err && (
            <div className="flex items-start gap-2 text-sm text-red-300 bg-red-900/30 border border-red-700/40 rounded-lg px-3 py-2">
              <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" /><span>{err}</span>
            </div>
          )}
        </div>

        <div className="p-4 border-t border-brain-700/30 flex justify-end gap-2">
          <button onClick={onClose}
            className="px-4 py-2 rounded-lg bg-brain-800 text-brain-400 hover:bg-brain-700 text-sm transition-colors">
            {t('cancel_button')}
          </button>
          <button onClick={save} disabled={saving}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-brain-600 text-white hover:bg-brain-500 text-sm transition-colors disabled:opacity-50">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />}
            {t('save_button')}
          </button>
        </div>
      </div>
    </div>
  )
}
