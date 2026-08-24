'use client'

import { useState, useRef, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { authHeaders } from '@/lib/authFetch'
import { Send, Bot, User, Sparkles, ChevronRight, Plus, Brain, TrendingUp, Zap, MessageCircle, Crown, ClipboardList, Copy, Trash2, Pencil, Check, X, Paperclip } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { FilterState } from './ChatSidebar'
import MarkResearchPanel from './MarkResearchPanel'

type ModelTier = 'standard' | 'premium'

type AgentMode = 'brain' | 'mark' | 'automation' | 'transcripts'

export interface SourceNavigationTarget {
  kind: 'meeting' | 'document' | 'snapshot' | 'graph' | 'graph_trace'
  meetingId?: string
  documentId?: string
  snapshotTab?: 'company' | 'person' | 'project'
  entityId?: string
  label?: string
  traceTargets?: Array<{
    traceNodeKey?: string
    entityId: string
    label?: string
    sourceType?: string
    role?: string
    isAnchor?: boolean
    pathOrder?: number
    retrievalStage?: string
    retrievalSources?: string[]
  }>
  traceEdges?: Array<{
    fromKey: string
    toKey: string
    label?: string
    kind?: string
    pathOrder?: number
    retrievalStage?: string
    segmentStage?: string
  }>
  traceSegments?: Array<{
    fromKey: string
    toKey: string
    fromLabel?: string
    toLabel?: string
    fromRetrievalStage?: string
    toRetrievalStage?: string
    segmentStage?: string
    segmentSources?: string[]
    bridgeKind?: string
    pathFound?: boolean
    pathEdgeCount?: number
    relationLabels?: string[]
    reason?: string
    pathOrder?: number
  }>
  traceMissingLinks?: Array<{
    fromLabel?: string
    toLabel?: string
    reason?: string
  }>
  reasoningSteps?: string[]
  title?: string
}

interface EvidenceItem {
  id?: string
  type?: string
  normalized_type?: string
  title?: string
  snippet?: string
  score?: number | null
  evidence_role?: string
  meeting_id?: string
  meeting_title?: string
  document_id?: string
  entity_id?: string
  snapshot_type?: string
  retrieval_stage?: string
  retrieval_sources?: string[]
}

interface EvidenceConfidenceEnvelope {
  score?: number
  label?: 'high' | 'medium' | 'low' | string
  search_depth?: string
  source_count?: number
  structured_source_count?: number
  direct_evidence_count?: number
  source_breakdown?: Record<string, number>
}

interface EvidenceGraphTraceNode {
  node_key?: string
  entity_id?: string
  label?: string
  graph_label?: string
  source_type?: string
  role?: string
  is_anchor?: boolean
  path_order?: number
  retrieval_stage?: string
  retrieval_sources?: string[]
}

interface EvidenceGraphTraceEdge {
  from_key?: string
  to_key?: string
  label?: string
  kind?: string
  path_order?: number
  retrieval_stage?: string
  segment_stage?: string
}

interface EvidenceGraphTraceSegment {
  from_key?: string
  to_key?: string
  from_label?: string
  to_label?: string
  from_retrieval_stage?: string
  to_retrieval_stage?: string
  segment_stage?: string
  segment_sources?: string[]
  bridge_kind?: string
  path_found?: boolean
  path_edge_count?: number
  relation_labels?: string[]
  reason?: string
  path_order?: number
}

interface EvidenceGraphTraceMissingLink {
  from_label?: string
  to_label?: string
  reason?: string
}

interface EvidenceGraphTrace {
  title?: string
  mode?: string
  nodes?: EvidenceGraphTraceNode[]
  edges?: EvidenceGraphTraceEdge[]
  segments?: EvidenceGraphTraceSegment[]
  reasoning_steps?: string[]
  missing_links?: EvidenceGraphTraceMissingLink[]
}

interface EvidenceContract {
  direct_evidence?: EvidenceItem[]
  supporting_evidence?: EvidenceItem[]
  inferred_path?: string[]
  ambiguities?: string[]
  confidence_envelope?: EvidenceConfidenceEnvelope
  graph_trace?: EvidenceGraphTrace
}

interface MessageResponseMetadata {
  evidence_contract?: EvidenceContract
  graph_trace?: EvidenceGraphTrace
  confidence?: number
  search_depth?: string
  attempts?: number
  strategy?: string
  strategy_used?: string
  processing_time_ms?: number
  duration_ms?: number
  [key: string]: any
}

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  reasoning_steps?: string[]
  sources?: any[]
  response_metadata?: MessageResponseMetadata
  agents_involved?: string[]
  timestamp: Date | string
}

interface ChatPanelProps {
  sessionId: string | null
  initialMessages: any[]
  onMessagesUpdate: (messages: any[], sessionIdOverride?: string | null) => void
  filters: FilterState
  ensureSession: () => Promise<string | null>
  agentMode: AgentMode
  userId: string | null
  onNavigateToSource?: (target: SourceNavigationTarget) => void
}

// W44a: welcome тексты живут в i18n catalog (messages/<locale>.json).
// Builder принимает translator и возвращает map strategy → text.
type TranslatorFn = (key: string) => string;
function buildBrainStrategyWelcomes(t: TranslatorFn): Record<string, string> {
  return {
    auto: t('welcome_auto'),
    instant: t('welcome_instant'),
    quick: t('welcome_quick'),
    standard: t('welcome_standard'),
    deep: t('welcome_deep'),
    tasker: t('welcome_tasker'),
    tz: t('welcome_tz'),
    agentic: t('welcome_agentic'),
  }
}

// W44a-1: все welcome-сообщения живут в i18n catalogs (messages/<locale>.json).
// Builders создают Message объекты с актуальным переведённым контентом.
function buildWelcomeMessages(t: TranslatorFn): Record<AgentMode, Message> {
  return {
    brain: {
      id: '0',
      role: 'assistant',
      content: t('welcome_auto'),   // default brain strategy = auto
      timestamp: new Date(),
    },
    mark: {
      id: '0',
      role: 'assistant',
      content: t('welcome_mark'),
      timestamp: new Date(),
    },
    automation: {
      id: '0',
      role: 'assistant',
      content: t('welcome_automation'),
      timestamp: new Date(),
    },
    transcripts: {
      id: '0',
      role: 'assistant',
      content: t('welcome_transcripts'),
      timestamp: new Date(),
    },
  }
}

function buildAutomationWelcomes(t: TranslatorFn): Record<string, Message> {
  return {
    tasks: { id: '0', role: 'assistant', content: t('automation_tasks'), timestamp: new Date() },
    web: { id: '0', role: 'assistant', content: t('automation_web'), timestamp: new Date() },
    meetflow: { id: '0', role: 'assistant', content: t('automation_meetflow'), timestamp: new Date() },
    calls: { id: '0', role: 'assistant', content: t('automation_calls'), timestamp: new Date() },
    tess: { id: '0', role: 'assistant', content: t('automation_tess'), timestamp: new Date() },
    hybrid: { id: '0', role: 'assistant', content: t('automation_hybrid'), timestamp: new Date() },
    openclaw: { id: '0', role: 'assistant', content: t('automation_openclaw'), timestamp: new Date() },
    agent_oc: { id: '0', role: 'assistant', content: t('automation_agent_oc'), timestamp: new Date() },
  }
}

// W44a-2: lookup table сохраняем для known type IDs; reverse map → i18n key.
// `t('source_type_<kind>')` отдаёт переведённую label (с emoji).
const SOURCE_TYPE_KIND_MAP: Record<string, string> = {
  meeting: 'meeting',
  Meeting: 'meeting',
  task: 'task',
  Task: 'task',
  decision: 'decision',
  Decision: 'decision',
  person: 'person',
  Person: 'person',
  entity: 'entity',
  Entity: 'entity',
  document: 'document',
  Document: 'document',
  chunk: 'chunk',
  Chunk: 'chunk',
  report: 'report',
  product: 'product',
  department: 'department',
  team: 'team',
  presentation: 'presentation',
}

// Type-only labels не привязанные к локали (technical / emoji-prefixed).
const SOURCE_TYPE_RAW_LABELS: Record<string, string> = {
  raw_chunk: '📝 Raw chunk',
  snapshot: '🧠 Snapshot',
  proposition: '🧩 Proposition',
  topic: '🗂️ Topic',
  kpi: '📈 KPI',
  pdf: '📄 PDF',
}

function getSourceTypeLabel(
  sourceType: string | null | undefined,
  t: TranslatorFn,
): string {
  if (!sourceType) return t('source_type_default')
  const normalized = String(sourceType)
  const kind = SOURCE_TYPE_KIND_MAP[normalized] || SOURCE_TYPE_KIND_MAP[normalized.toLowerCase()]
  if (kind) return t(`source_type_${kind}`)
  return SOURCE_TYPE_RAW_LABELS[normalized] || SOURCE_TYPE_RAW_LABELS[normalized.toLowerCase()] || `📌 ${normalized}`
}

function formatConfidencePercent(value?: number | null) {
  if (typeof value !== 'number' || Number.isNaN(value)) return null
  return `${Math.round(value * 100)}%`
}

function getConfidenceLabel(label: string | null | undefined, t: TranslatorFn): string | null {
  const normalized = String(label || '').toLowerCase()
  if (normalized === 'high') return t('confidence_high')
  if (normalized === 'medium') return t('confidence_medium')
  if (normalized === 'low') return t('confidence_low')
  return normalized || null
}

function formatEvidenceScore(score?: number | null) {
  if (typeof score !== 'number' || Number.isNaN(score)) return null
  return score <= 1 ? `${Math.round(score * 100)}%` : score.toFixed(2)
}

function buildSourceNavigationTarget(source: any): SourceNavigationTarget | null {
  if (!source || typeof source !== 'object') return null

  const metadata = source.metadata || {}
  const normalizedType = String(
    source.normalized_type ||
    source._normalized_type ||
    source.type ||
    metadata.type ||
    ''
  ).toLowerCase()
  const snapshotType = String(source.snapshot_type || metadata.snapshot_type || '').toLowerCase()
  const meetingId = String(source.meeting_id || metadata.meeting_id || '').trim()
  const documentId = String(source.document_id || metadata.document_id || '').trim()
  const entityId = String(source.entity_id || metadata.entity_id || source.id || '').trim()
  const label = String(source.title || source.name || source.meeting_title || source.id || '').trim()

  if (documentId) {
    return { kind: 'document', documentId, label }
  }

  if (normalizedType === 'document') {
    const fallbackDocumentId = String(source.id || '').trim()
    if (fallbackDocumentId) {
      return { kind: 'document', documentId: fallbackDocumentId, label }
    }
  }

  if (snapshotType === 'company' || entityId === 'company') {
    return { kind: 'snapshot', snapshotTab: 'company', entityId: 'company', label }
  }

  if (snapshotType === 'person' || normalizedType === 'person') {
    if (entityId) {
      return { kind: 'snapshot', snapshotTab: 'person', entityId, label }
    }
  }

  if (snapshotType === 'project' || normalizedType === 'project') {
    if (entityId) {
      return { kind: 'snapshot', snapshotTab: 'project', entityId, label }
    }
  }

  if (normalizedType === 'raw_chunk' && meetingId) {
    return { kind: 'meeting', meetingId, label }
  }

  if (meetingId) {
    return { kind: 'meeting', meetingId, label }
  }

  if (normalizedType === 'meeting') {
    const fallbackMeetingId = String(source.id || '').trim()
    if (fallbackMeetingId) {
      return { kind: 'meeting', meetingId: fallbackMeetingId, label }
    }
  }

  return null
}

function buildGraphNavigationTarget(source: any): SourceNavigationTarget | null {
  if (!source || typeof source !== 'object') return null

  const metadata = source.metadata || {}
  const normalizedType = String(
    source.normalized_type ||
    source._normalized_type ||
    source.type ||
    metadata.type ||
    ''
  ).toLowerCase()
  const snapshotType = String(source.snapshot_type || metadata.snapshot_type || '').toLowerCase()
  const entityId = String(source.entity_id || metadata.entity_id || source.id || '').trim()
  const documentId = String(source.document_id || metadata.document_id || '').trim()
  const label = String(source.title || source.name || source.meeting_title || source.id || '').trim()

  if (!entityId || entityId === 'company') return null
  if (documentId) return null
  if (normalizedType === 'document') return null
  if (snapshotType === 'company') return null

  const graphFriendlyTypes = new Set([
    'snapshot',
    'person',
    'project',
    'product',
    'department',
    'team',
    'entity',
    'task',
    'decision',
    'meeting',
    'topic',
    'proposition',
    'report',
    'kpi',
    'risk',
    'question',
    'pattern',
  ])

  if (!graphFriendlyTypes.has(normalizedType) && !snapshotType && !String(source.meeting_id || metadata.meeting_id || '').trim()) {
    return null
  }

  return {
    kind: 'graph',
    entityId,
    label,
  }
}

function buildGraphTraceNavigationTarget(
  responseMetadata?: MessageResponseMetadata | null,
  evidenceContract?: EvidenceContract | null,
  tRaw?: (key: string) => string
): SourceNavigationTarget | null {
  const defaultTitle = tRaw ? tRaw('trace_title') : 'Reasoning trace'
  const backendTrace = responseMetadata?.graph_trace || evidenceContract?.graph_trace

  if (backendTrace?.nodes && backendTrace.nodes.length > 0) {
    const traceTargets = backendTrace.nodes
      .map((item) => ({
        traceNodeKey: item.node_key || item.entity_id,
        entityId: item.entity_id || item.node_key || '',
        label: item.label,
        sourceType: item.source_type || item.graph_label,
        role: item.role,
        isAnchor: item.is_anchor,
        pathOrder: item.path_order,
        retrievalStage: item.retrieval_stage,
        retrievalSources: item.retrieval_sources || [],
      }))
      .filter((item) => item.entityId)

    if (traceTargets.length >= 2) {
      return {
        kind: 'graph_trace',
        traceTargets,
        traceEdges: (backendTrace.edges || [])
          .map((edge) => ({
            fromKey: edge.from_key || '',
            toKey: edge.to_key || '',
            label: edge.label,
            kind: edge.kind,
            pathOrder: edge.path_order,
            retrievalStage: edge.retrieval_stage,
            segmentStage: edge.segment_stage,
          }))
          .filter((edge) => edge.fromKey && edge.toKey),
        traceSegments: (backendTrace.segments || [])
          .map((segment) => ({
            fromKey: segment.from_key || '',
            toKey: segment.to_key || '',
            fromLabel: segment.from_label,
            toLabel: segment.to_label,
            fromRetrievalStage: segment.from_retrieval_stage,
            toRetrievalStage: segment.to_retrieval_stage,
            segmentStage: segment.segment_stage,
            segmentSources: segment.segment_sources || [],
            bridgeKind: segment.bridge_kind,
            pathFound: segment.path_found,
            pathEdgeCount: segment.path_edge_count,
            relationLabels: segment.relation_labels || [],
            reason: segment.reason,
            pathOrder: segment.path_order,
          }))
          .filter((segment) => segment.fromKey && segment.toKey),
        traceMissingLinks: (backendTrace.missing_links || []).map((item) => ({
          fromLabel: item.from_label,
          toLabel: item.to_label,
          reason: item.reason,
        })),
        reasoningSteps: backendTrace.reasoning_steps || evidenceContract?.inferred_path || [],
        title: backendTrace.title || defaultTitle,
      }
    }
  }

  if (!evidenceContract) return null

  const candidates = [
    ...(evidenceContract.direct_evidence || []).map((item) => ({ ...item, role: 'direct' })),
    ...(evidenceContract.supporting_evidence || []).map((item) => ({ ...item, role: 'supporting' })),
  ]

  const traceTargets: Array<{
    traceNodeKey?: string
    entityId: string
    label?: string
    sourceType?: string
    role?: string
    retrievalStage?: string
    retrievalSources?: string[]
  }> = []
  const seen = new Set<string>()

  for (const candidate of candidates) {
    const graphTarget = buildGraphNavigationTarget(candidate)
    if (!graphTarget?.entityId) continue

    const key = graphTarget.entityId.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)

    traceTargets.push({
      traceNodeKey: graphTarget.entityId,
      entityId: graphTarget.entityId,
      label: graphTarget.label,
      sourceType: String(candidate.normalized_type || candidate.type || ''),
      role: candidate.role || candidate.evidence_role,
        retrievalStage: candidate.retrieval_stage,
        retrievalSources: candidate.retrieval_sources || [],
    })
  }

  if (traceTargets.length < 2) {
    return null
  }

  return {
    kind: 'graph_trace',
    traceTargets,
    reasoningSteps: evidenceContract.inferred_path || [],
    title: defaultTitle,
  }
}

export default function ChatPanel({
  sessionId,
  initialMessages,
  onMessagesUpdate,
  filters,
  ensureSession,
  agentMode,
  userId,
  onNavigateToSource,
}: ChatPanelProps) {
  // W44a/W44a-1: scoped translator для chat_panel namespace.
  const t = useTranslations('chat_panel')
  // t.raw возвращает строку как есть (с \n) — нужно для multiline markdown.
  const tRaw = (key: string) => t.raw(key) as string
  const BRAIN_STRATEGY_WELCOME = buildBrainStrategyWelcomes(tRaw)
  const WELCOME_MESSAGES = buildWelcomeMessages(tRaw)
  const AUTOMATION_WELCOME = buildAutomationWelcomes(tRaw)
  console.log('[ChatPanel] Rendering with agentMode:', agentMode)
  // W44a-1: WELCOME_MESSAGES.brain.content уже = t('welcome_auto') из builder'а;
  // если strategy != auto, useEffect ниже обновит content через BRAIN_STRATEGY_WELCOME[strategy].
  const [messages, setMessages] = useState<Message[]>([WELCOME_MESSAGES[agentMode]])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [strategy, setStrategy] = useState<string>('auto')
  const [deepResearch, setDeepResearch] = useState(false)
  const [automationMode, setAutomationMode] = useState<string>('tasks')  // tasks | web | meetflow | calls
  // Mark: обычный чат или панель «Исследования» (вывод продукта на рынок)
  const [markView, setMarkView] = useState<'chat' | 'research'>('chat')
  const [tessAllowedTools, setTessAllowedTools] = useState<string[]>([
    'get_all_tasks', 
    'get_person_info', 
    'get_recent_meetings',
    'search_knowledge',
    'analyze_with_llm',
  ])
  const [modelTier, setModelTier] = useState<ModelTier>('standard')
  // Mark: «простой» = один специалист-скилл со всеми инструментами (быстро,
  // дёшево), «команда» = многошаговый GroupChat из агентов (для сложных
  // многопрофильных задач). Уходит на бэкенд как context.mark_mode.
  const [markMode, setMarkMode] = useState<'solo' | 'swarm'>('solo')
  // Временные файлы чата (скрепка): живут только в этом диалоге, в базу
  // знаний НЕ попадают (ретеншн 7 дней на бэке). context.temp_files.
  const [attachedFiles, setAttachedFiles] = useState<Array<{ id: string; name: string; chars: number }>>([])
  const [uploadingFile, setUploadingFile] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const handleFileAttach = async (file: File) => {
    if (!file) return
    setUploadingFile(true)
    try {
      const dataUrl: string = await new Promise((resolve, reject) => {
        const r = new FileReader()
        r.onload = () => resolve(String(r.result))
        r.onerror = reject
        r.readAsDataURL(file)
      })
      const res = await fetch('/api/v1/agents/chat-upload', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ filename: file.name, content: dataUrl, session_id: sessionId }),
      })
      const data = await res.json()
      if (data.success) {
        setAttachedFiles(prev => [...prev, { id: data.file_id, name: data.name, chars: data.chars }])
      } else {
        alert(t('file_attach_error', { error: data.error || t('error_fallback') }))
      }
    } catch (e) {
      alert(t('file_upload_failed'))
    } finally {
      setUploadingFile(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }
  const messagesEndRef = useRef<HTMLDivElement>(null)
  
  // Message actions state
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null)
  const [editingContent, setEditingContent] = useState<string>('')
  const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null)
  const [deepDivingId, setDeepDivingId] = useState<string | null>(null)

  // «Отправить в работу»: ТЗ из чата → handoff-очередь CLI-исполнителей
  // (тот же конвейер, что у SIMA/vibe-tasking: подтверждение в
  // Автоматизации → Очередь задач, потом реальный прогон claude/codex).
  const [handoffFor, setHandoffFor] = useState<Message | null>(null)
  const [handoffAgent, setHandoffAgent] = useState('claude')
  const [handoffRepo, setHandoffRepo] = useState('')
  const [handoffBusy, setHandoffBusy] = useState(false)
  const [handoffNote, setHandoffNote] = useState('')

  const submitHandoff = async () => {
    // repo_path ОПЦИОНАЛЕН (бэк принимает без него; папку можно указать на
    // шаге «Подтвердить» в очереди). Раньше молча return'или без папки —
    // кнопка выглядела «не работает».
    if (!handoffFor || !userId) {
      setHandoffNote(t('handoff_missing_data'))
      return
    }
    setHandoffBusy(true)
    setHandoffNote('')
    try {
      const res = await fetch(`/api/v1/task-analysis/handoff?user_id=${userId}`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          user_id: userId,
          agent: handoffAgent,
          ...(handoffRepo.trim() ? { repo_path: handoffRepo.trim() } : {}),
          spec_text: handoffFor.content,
        }),
      })
      const data = await res.json()
      if (data.success === false || data.detail) {
        setHandoffNote(`⚠️ ${data.error || data.detail}`)
      } else {
        setHandoffNote(t('handoff_queued'))
        setHandoffFor(null)
      }
    } catch (e) {
      setHandoffNote(`⚠️ ${(e as Error).message}`)
    } finally {
      setHandoffBusy(false)
    }
  }

  // Update welcome message when agent mode changes
  useEffect(() => {
    if (messages.length === 1 && messages[0].id === '0') {
      const base = WELCOME_MESSAGES[agentMode]
      const next = agentMode === 'brain'
        ? { ...base, content: BRAIN_STRATEGY_WELCOME[strategy] || BRAIN_STRATEGY_WELCOME.auto }
        : base
      setMessages([next])
    }
  }, [agentMode])

  // Update welcome message when automation sub-mode changes
  useEffect(() => {
    if (agentMode === 'automation' && messages.length === 1 && messages[0].id === '0') {
      setMessages([AUTOMATION_WELCOME[automationMode] || WELCOME_MESSAGES.automation])
    }
  }, [automationMode, agentMode])

  // Update welcome message when Brain strategy changes
  useEffect(() => {
    if (agentMode === 'brain' && messages.length === 1 && messages[0].id === '0') {
      const welcomeText = BRAIN_STRATEGY_WELCOME[strategy] || BRAIN_STRATEGY_WELCOME.auto
      setMessages([{
        id: '0',
        role: 'assistant',
        content: welcomeText,
        timestamp: new Date(),
      }])
    }
  }, [strategy, agentMode])

  // Sync messages from props
  useEffect(() => {
    if (initialMessages && initialMessages.length > 0) {
      // Track seen IDs to ensure uniqueness
      const seenIds = new Set<string>()
      const parsed = initialMessages.map((m: any, index: number) => {
        let id = m.id
        // Generate unique ID if missing or duplicate
        if (!id || seenIds.has(id)) {
          id = `msg_${index}_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
        }
        seenIds.add(id)
        return {
          ...m,
          id,
          timestamp: new Date(m.timestamp),
        }
      })
      setMessages(parsed)
    } else {
      const base = WELCOME_MESSAGES[agentMode]
      const next = agentMode === 'brain'
        ? { ...base, content: BRAIN_STRATEGY_WELCOME[strategy] || BRAIN_STRATEGY_WELCOME.auto }
        : base
      setMessages([next])
    }
  }, [initialMessages, sessionId, agentMode])

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    scrollToBottom()
  }, [messages])

  const sendMessage = async () => {
    if (!input.trim() || loading) return

    // Ensure session exists (for reliable persistence)
    const activeSessionId = sessionId || (await ensureSession())

    const userMessage: Message = {
      id: Date.now().toString() + '-' + Math.random().toString(36).substr(2, 9),
      role: 'user',
      content: input,
      timestamp: new Date(),
    }

    const newMessages = [...messages, userMessage]
    setMessages(newMessages)
    setInput('')
    setLoading(true)

    try {
      // Build context from filters
      const context: any = {
        user_id: userId, // CRITICAL: Pass user_id to agents for integration lookups
      }
      if (filters.meetings.length > 0) {
        context.meeting_filter = filters.meetings
      }
      if (filters.projects.length > 0) {
        context.project_filter = filters.projects
      }
      if (filters.folders.length > 0) {
        context.folder_filter = filters.folders
      }
      if (filters.documents && filters.documents.length > 0) {
        context.document_filter = filters.documents
      }
      if (filters.chatSources && filters.chatSources.length > 0) {
        context.chat_source_keys = filters.chatSources
      }
      if (filters.includeCrm) context.include_crm = true
      if (filters.includeTasks) context.include_tasks = true
      if (agentMode === 'mark') {
        context.mark_mode = markMode
      }
      if (agentMode === 'transcripts' && attachedFiles.length > 0) {
        context.temp_files = attachedFiles.map(f => ({ id: f.id, name: f.name }))
      }

      // Add automation sub-mode context
      if (agentMode === 'automation') {
        context.automation_mode = automationMode
        if (automationMode === 'tess') {
          context.tess_allowed_tools = tessAllowedTools
        }
        // Set start_url for web mode based on sub-mode
        if (automationMode === 'web') {
          context.headless = true
        } else if (automationMode === 'meetflow') {
          context.start_url = 'https://meetflow.synlabs.pro'
          context.headless = true
        } else if (automationMode === 'calls') {
          context.start_url = 'https://calls.synlabs.pro'
          context.headless = true
        }
      }

      let assistantMessage: Message = {
        id: Date.now().toString() + '_error',
        role: 'assistant',
        content: tRaw('generic_error'),
        timestamp: new Date(),
      }

      console.log('[ChatPanel] Sending message with agentMode:', agentMode, 'automationMode:', automationMode)

      if (agentMode === 'brain' && (strategy === 'research' || strategy === 'advisor')) {
        // Batch Research: разведка → план → полное чтение встреч → отчёт.
        // Долгий (минуты) — фоновая задача + поллинг с живым прогрессом.
        if (!userId) throw new Error('Missing userId')
        const startRes = await fetch(`/api/v1/research/start?user_id=${userId}`, {
          method: 'POST',
          headers: authHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({ question: input, mode: strategy === 'advisor' ? 'advisor' : 'research', depth: deepResearch ? 'deep' : 'standard' }),
        })
        const startData = await startRes.json()
        if (!startData.ok) throw new Error(startData.error || 'research start failed')
        const rid = startData.research_id

        // временное сообщение с прогрессом
        const progressId = Date.now().toString() + '_research'
        setMessages((prev) => [...prev, {
          id: progressId, role: 'assistant',
          content: t('research_started'),
          timestamp: new Date(),
        }])

        let finalState: any = null
        for (let i = 0; i < (deepResearch ? 900 : 400); i++) { // до ~20/45 минут
          await new Promise((r) => setTimeout(r, 3000))
          const st = await fetch(`/api/v1/research/${rid}?user_id=${userId}`)
            .then((r) => r.json()).catch(() => null)
          if (!st) continue
          if (st.status === 'done' || st.status === 'failed') { finalState = st; break }
          setMessages((prev) => prev.map((m) => m.id === progressId
            ? { ...m, content: `🔬 ${st.progress || t('researching_fallback')}` } : m))
        }
        const reportText = finalState?.status === 'done'
          ? finalState.report + (finalState.report_file
              ? '\n\n' + t('research_open_report_link', { url: `/api/v1/research/${rid}/report?user_id=${userId}` })
              : '')
          : t('research_failed', { error: finalState?.error || t('research_polling_timeout') })
        // Финальный список собираем явно и СОХРАНЯЕМ в сессию: раньше ветка
        // выходила без onMessagesUpdate — отчёт пропадал при переключении
        // перепис (терялась и ссылка на HTML-документ).
        const finalList = [...newMessages, {
          id: progressId, role: 'assistant' as const,
          content: reportText, timestamp: new Date(),
        }]
        setMessages(finalList)
        onMessagesUpdate(finalList.map((m) => ({
          ...m,
          timestamp: m.timestamp instanceof Date ? m.timestamp.toISOString() : m.timestamp,
        })), activeSessionId)
        setLoading(false)
        return
      }

      if (agentMode === 'brain' && strategy === 'tz') {
        // ТЗ — через Task Specification API (DataDrivenTaskSystem, лучший результат)
        if (!userId) throw new Error('Missing userId')
        const controller = new AbortController()
        const timeoutId = setTimeout(() => controller.abort(), 300000) // 5 min timeout

        const endpoint = '/api/v1/agents/tasks/process';

        const res = await fetch(endpoint, {
          method: 'POST',
          headers: authHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({
            task_description: input,
            additional_context: Object.keys(context).length > 0 ? context : undefined,
            execution_mode: 'plan_only',
            skip_execution: false,
          }),
          signal: controller.signal,
        })

        clearTimeout(timeoutId)

        if (res.ok) {
          const data = await res.json()
          if (data.success) {
            let responseContent = `${tRaw('tz_generated_header')}\n\n`;
            if (data.markdown_spec) {
              responseContent += data.markdown_spec;
            } else {
              // Fallback: собираем из stages
              const stages = data.stages || {};
              const result = stages.result || {};
              responseContent += result.markdown || JSON.stringify(data, null, 2);
            }
            
            if (data.file_paths && Object.keys(data.file_paths).length > 0) {
              responseContent += `\n\n---\n${tRaw('tz_files_saved_header')}\n`;
              for (const [type, path] of Object.entries(data.file_paths)) {
                responseContent += `- ${type}: \`${path}\`\n`;
              }
            }

            assistantMessage = {
              id: Date.now().toString() + '_assistant',
              role: 'assistant',
              content: responseContent,
              reasoning_steps: data.stages_completed ? [tRaw('tz_stages_label').replace('{stages}', data.stages_completed.join(' → '))] : [],
              timestamp: new Date(),
              // без is_tz кнопка ⚡ «Отправить в работу» не появлялась именно
              // для ТЗ из TaskSpec-режима — а это его основной сценарий
              response_metadata: { is_tz: true },
            }
          } else {
            assistantMessage = {
              id: Date.now().toString() + '_error',
              role: 'assistant',
              content: tRaw('tz_generation_error').replace('{errors}', data.errors?.join('\n') || tRaw('tz_unknown_error')),
              timestamp: new Date(),
            }
          }
        } else {
          throw new Error('Failed to generate TZ')
        }
      } else if (agentMode === 'brain') {
        // Tessent Brain - стандартный поиск (chat completions).
        // AbortController: глубокие multi-query запросы идут 60-120с; без
        // таймаута зависший запрос ждёт бесконечно, а на разрыв прокси раньше
        // показывалось невнятное «Попробуйте позже» без причины.
        if (!userId) throw new Error('Missing userId')
        const controller = new AbortController()
        const timeoutId = setTimeout(() => controller.abort(), 420000) // 7 мин (DEEP на слабом железе доходит до ~4-5 мин)
        let res: Response | null = null
        try {
          res = await fetch(`/api/v1/chat/completions?user_id=${userId}`, {
            method: 'POST',
            headers: authHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({
              // кап истории (аудит P1 №10): вся сессия в каждом запросе
              // раздувала payload/токены; бэкенд берёт последние 10 — шлём 30
              messages: newMessages.slice(-30).map((m) => ({ role: m.role, content: m.content })),
              session_id: activeSessionId,
              strategy: strategy,
              context: Object.keys(context).length > 0 ? context : undefined,
              model_tier: modelTier,
            }),
            signal: controller.signal,
          })
        } catch (err: any) {
          if (err?.name === 'AbortError') {
            assistantMessage = {
              id: Date.now().toString() + '_error',
              role: 'assistant',
              content: t('request_timeout'),
              timestamp: new Date(),
            }
          } else {
            throw err
          }
        } finally {
          clearTimeout(timeoutId)
        }

        if (res) {
          if (res.ok) {
            const data = await res.json()
            assistantMessage = {
              id: Date.now().toString() + '_assistant',
              role: 'assistant',
              content: data.message.content,
              reasoning_steps: data.reasoning_steps,
              sources: data.sources,
              response_metadata: data.response_metadata,
              timestamp: new Date(),
            }
          } else {
            // Показываем реальную причину вместо немого «Попробуйте позже».
            let detail = `HTTP ${res.status}`
            try {
              const body = await res.json()
              detail = body?.error?.message || body?.detail || body?.message || detail
            } catch {}
            assistantMessage = {
              id: Date.now().toString() + '_error',
              role: 'assistant',
              content: res.status === 429 ? t('limit_exhausted', { detail }) : `❌ ${detail}`,
              timestamp: new Date(),
            }
          }
        }
      } else {
        // Mark, Automation, or Transcripts - use agents API
        const controller = new AbortController()
        // Auto/Mark многошаговые прогоны (MeetFlow groupchat, ReAct-петли,
        // рой Mark) дольше 3 мин — на 180с фронт обрубал готовый результат и
        // сжигал токены (аудит). 420с как у deep-исследования.
        const timeoutId = setTimeout(() => controller.abort(), 420000)

        // Use direct backend URL for ALL agent modes to avoid Next.js proxy timeout (typically 60s)
        // This is crucial for long-running agent tasks like research or complex queries
        const endpoint = '/api/v1/agents/chat';

        // Подготавливаем историю чата для контекста агентов
        const chatHistory = newMessages.slice(-31, -1).map((m) => ({
          role: m.role,
          content: m.content,
        }));

        const res = await fetch(endpoint, {
          method: 'POST',
          headers: authHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({
            message: input,
            session_id: sessionId,
            context: context,
            agent_mode: agentMode,
            model_tier: modelTier,
            chat_history: chatHistory,
          }),
          signal: controller.signal,
        })
        
        clearTimeout(timeoutId)

        if (res.ok) {
          const data = await res.json()
          if (data.success) {
            assistantMessage = {
              id: Date.now().toString() + '_assistant',
              role: 'assistant',
              content: data.message,
              agents_involved: data.agents_involved,
              sources: data.sources,
              response_metadata: data.response_metadata,
              timestamp: new Date(),
            }
          } else {
            assistantMessage = {
              id: Date.now().toString() + '_error',
              role: 'assistant',
              content: data.message || tRaw('request_error'),
              timestamp: new Date(),
            }
          }
        } else if (res.status === 429) {
          // Квота/бюджет исчерпаны — показываем честное сообщение вместо
          // generic-ошибки, чтобы было понятно ЧТО произошло и когда сбросится.
          let detail = ''
          try {
            const body = await res.json()
            const err = body?.error || {}
            detail = err.message || ''
            if (err.reset_at) detail += ' ' + t('reset_at_suffix', { time: err.reset_at })
          } catch {}
          assistantMessage = {
            id: Date.now().toString() + '_error',
            role: 'assistant',
            content: detail
              ? t('daily_token_limit_with_detail', { detail })
              : t('daily_token_limit_exhausted'),
            timestamp: new Date(),
          }
        } else {
          throw new Error('Failed to get response')
        }
      }

      const updatedMessages = [...newMessages, assistantMessage]
      setMessages(updatedMessages)
        
      // Notify parent to save
      onMessagesUpdate(updatedMessages.map(m => ({
        ...m,
        timestamp: m.timestamp instanceof Date ? m.timestamp.toISOString() : m.timestamp,
      })), activeSessionId)
    } catch (e) {
      console.error('Chat error:', e)
      const errorMessage: Message = {
        id: Date.now().toString() + '_error',
        role: 'assistant',
        content: tRaw('generic_error'),
        timestamp: new Date(),
      }
      setMessages(prev => [...prev, errorMessage])
    } finally {
      setLoading(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  // === MESSAGE ACTIONS ===
  
  // Markdown → чистый ТЕКСТ с сохранением структуры (отступы, списки,
  // нумерация, переносы) — без символов разметки #, *, `, |.
  const stripMarkdown = (md: string): string => {
    if (!md) return ''
    let t = md.replace(/\r\n/g, '\n')
    // блоки кода ```...``` → содержимое с сохранением переносов
    t = t.replace(/```[\w-]*\n?([\s\S]*?)```/g, '$1')
    t = t.replace(/`([^`]+)`/g, '$1')                       // `inline` → inline
    t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')          // картинки → alt
    t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')           // ссылки → текст
    // инлайн-эмфазис убираем, НЕ трогая ведущие пробелы
    t = t.replace(/(\*\*|__)(.*?)\1/g, '$2')                // **жирный**
    t = t.replace(/(\*|_)(.*?)\1/g, '$2')                   // *курсив*
    t = t.replace(/~~(.*?)~~/g, '$1')                       // ~~зачёркнутый~~

    const lines = t.split('\n').map((line) => {
      // заголовки → текст на своей строке (структура сохраняется пустыми строками)
      if (/^\s{0,3}#{1,6}\s+/.test(line)) return line.replace(/^\s{0,3}#{1,6}\s+/, '')
      // горизонтальные разделители --- *** ___ → пустая строка
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) return ''
      // строки-разделители таблиц |---|---| → удаляем
      if (/^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes('|')) return null
      // ячейки таблицы | a | b | → колонки через таб (сохраняем как строку)
      if (/^\s*\|(.+)\|\s*$/.test(line)) {
        return line.replace(/^\s*\|(.+)\|\s*$/, (_m, row: string) =>
          row.split('|').map((c) => c.trim()).filter(Boolean).join('\t'))
      }
      // цитаты: убираем > , СОХРАНЯЯ ведущий отступ
      line = line.replace(/^(\s*)>\s?/, '$1')
      // маркеры списков -, *, + → • , СОХРАНЯЯ вложенный отступ
      line = line.replace(/^(\s*)[-*+]\s+/, '$1• ')
      // нумерованные списки 1. 2. — оставляем как есть (уже читаемо)
      return line
    }).filter((l) => l !== null) as string[]

    t = lines.join('\n')
    t = t.replace(/\n{3,}/g, '\n\n')                        // не более одной пустой строки подряд
    return t.replace(/[ \t]+\n/g, '\n').trim()              // обрезаем хвостовые пробелы строк
  }

  // Copy message content to clipboard (как чистый текст, без Markdown-разметки)
  const handleCopyMessage = async (message: Message) => {
    try {
      const plain = message.role === 'assistant' ? stripMarkdown(message.content) : message.content
      await navigator.clipboard.writeText(plain)
      setCopiedMessageId(message.id)
      setTimeout(() => setCopiedMessageId(null), 2000) // Reset after 2 seconds
    } catch (err) {
      console.error('Failed to copy:', err)
    }
  }

  // Deep dive: пересобрать ответ, прочитав ПОЛНЫЕ транскрипты топ-встреч.
  // Берём вопрос пользователя, предшествующий этому ответу ассистента, и
  // повторяем запрос с deep_dive=true. Результат заменяет текущий ответ.
  const handleDeepDive = async (message: Message) => {
    if (!userId) return
    const idx = messages.findIndex(m => m.id === message.id)
    if (idx < 0) return
    // Ищем ближайшее пользовательское сообщение выше.
    let userIdx = -1
    for (let i = idx; i >= 0; i--) {
      if (messages[i].role === 'user') { userIdx = i; break }
    }
    if (userIdx < 0) return

    setDeepDivingId(message.id)
    try {
      const convo = messages.slice(0, userIdx + 1).map(m => ({ role: m.role, content: m.content }))
      // Тот же контекст-фильтр, что и в обычной отправке (см. sendMessage).
      const ddContext: any = { user_id: userId }
      if (filters.meetings.length > 0) ddContext.meeting_filter = filters.meetings
      if (filters.projects.length > 0) ddContext.project_filter = filters.projects
      if (filters.folders.length > 0) ddContext.folder_filter = filters.folders
      if (filters.documents && filters.documents.length > 0) ddContext.document_filter = filters.documents
      if (filters.chatSources && filters.chatSources.length > 0) ddContext.chat_source_keys = filters.chatSources
      if (filters.includeCrm) ddContext.include_crm = true
      if (filters.includeTasks) ddContext.include_tasks = true
      const res = await fetch(`/api/v1/chat/completions?user_id=${userId}`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          messages: convo,
          session_id: sessionId,
          strategy: strategy,
          context: ddContext,
          model_tier: modelTier,
          deep_dive: true,
        }),
      })
      if (!res.ok) throw new Error('deep dive failed')
      const data = await res.json()
      const updated = messages.map(m => m.id === message.id ? {
        ...m,
        content: data.message?.content ?? m.content,
        reasoning_steps: data.reasoning_steps,
        sources: data.sources,
        response_metadata: data.response_metadata,
      } : m)
      setMessages(updated)
      onMessagesUpdate(updated.map(m => ({
        ...m,
        timestamp: m.timestamp instanceof Date ? m.timestamp.toISOString() : m.timestamp,
      })), sessionId)
    } catch (e) {
      console.error('Deep dive error:', e)
    } finally {
      setDeepDivingId(null)
    }
  }

  // Delete message
  const handleDeleteMessage = (messageId: string) => {
    // Don't allow deleting the welcome message
    if (messageId === '0') return
    
    const updatedMessages = messages.filter(m => m.id !== messageId)
    setMessages(updatedMessages)
    
    // Notify parent to save
    onMessagesUpdate(updatedMessages.map(m => ({
      ...m,
      timestamp: m.timestamp instanceof Date ? m.timestamp.toISOString() : m.timestamp,
    })), sessionId)
  }

  // Start editing message
  const handleStartEdit = (message: Message) => {
    // Only allow editing user messages
    if (message.role !== 'user' || message.id === '0') return
    
    setEditingMessageId(message.id)
    setEditingContent(message.content)
  }

  // Save edited message and regenerate response (like ChatGPT/Claude)
  const handleSaveEdit = async () => {
    if (!editingMessageId || !editingContent.trim()) {
      setEditingMessageId(null)
      setEditingContent('')
      return
    }

    // Find the index of the edited message
    const editedIndex = messages.findIndex(m => m.id === editingMessageId)
    if (editedIndex === -1) {
      setEditingMessageId(null)
      setEditingContent('')
      return
    }

    // Keep messages up to and including the edited one, remove all after
    const messagesUpToEdited = messages.slice(0, editedIndex)
    
    // Create updated user message with new content
    const editedMessage: Message = {
      ...messages[editedIndex],
      content: editingContent.trim(),
      timestamp: new Date(), // Update timestamp
    }
    
    // Set messages to everything before + edited message
    const truncatedMessages = [...messagesUpToEdited, editedMessage]
    setMessages(truncatedMessages)
    
    // Clear editing state
    setEditingMessageId(null)
    setEditingContent('')
    
    // Now send the edited message to get a new response
    setLoading(true)
    
    try {
      const activeSessionId = sessionId || (await ensureSession())
      
      // Build context from filters
      const context: any = {
        user_id: userId,
      }
      if (filters.meetings.length > 0) {
        context.meeting_filter = filters.meetings
      }
      if (filters.projects.length > 0) {
        context.project_filter = filters.projects
      }
      if (filters.folders.length > 0) {
        context.folder_filter = filters.folders
      }
      if (filters.documents && filters.documents.length > 0) {
        context.document_filter = filters.documents
      }
      if (filters.chatSources && filters.chatSources.length > 0) {
        context.chat_source_keys = filters.chatSources
      }
      if (filters.includeCrm) context.include_crm = true
      if (filters.includeTasks) context.include_tasks = true
      if (agentMode === 'mark') {
        context.mark_mode = markMode
      }
      if (agentMode === 'transcripts' && attachedFiles.length > 0) {
        context.temp_files = attachedFiles.map(f => ({ id: f.id, name: f.name }))
      }

      let assistantMessage: Message = {
        id: Date.now().toString() + '_error',
        role: 'assistant',
        content: tRaw('generic_error'),
        timestamp: new Date(),
      }

      if (agentMode === 'brain') {
        // Tessent Brain - use chat completions
        if (!userId) throw new Error('Missing userId')
        const res = await fetch(`/api/v1/chat/completions?user_id=${userId}`, {
          method: 'POST',
          headers: authHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({
            messages: truncatedMessages.map((m) => ({
              role: m.role,
              content: m.content,
            })),
            session_id: activeSessionId,
            strategy: strategy,
            context: Object.keys(context).length > 0 ? context : undefined,
            model_tier: modelTier,
          }),
        })

        if (res.ok) {
          const data = await res.json()
          assistantMessage = {
            id: Date.now().toString() + '_assistant_regen',
            role: 'assistant',
            content: data.message.content,
            timestamp: new Date(),
            reasoning_steps: data.reasoning_steps,
            sources: data.sources,
            response_metadata: data.response_metadata,
          }
        } else {
          throw new Error('Failed to get response')
        }
      } else {
        // Agent modes (mark, automation, transcripts)
        // Use direct backend URL for agent modes to avoid Next.js proxy timeout
        const res = await fetch('/api/v1/agents/chat', {
          method: 'POST',
          headers: authHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({
            message: editedMessage.content,
            session_id: activeSessionId,
            agent_mode: agentMode,
            context,
            model_tier: modelTier,
          }),
        })

        const data = await res.json()
        if (data.success) {
          assistantMessage = {
            id: Date.now().toString() + '_assistant_regen',
            role: 'assistant',
            content: data.message,
            timestamp: new Date(),
            agents_involved: data.agents_involved,
            response_metadata: data.response_metadata,
          }
        } else {
          assistantMessage = {
            id: Date.now().toString() + '_error',
            role: 'assistant',
            content: data.message || tRaw('request_error'),
            timestamp: new Date(),
          }
        }
      }

      const finalMessages = [...truncatedMessages, assistantMessage]
      setMessages(finalMessages)
      
      // Save to session
      onMessagesUpdate(finalMessages.map(m => ({
        ...m,
        timestamp: m.timestamp instanceof Date ? m.timestamp.toISOString() : m.timestamp,
      })), activeSessionId)
      
    } catch (e) {
      console.error('Edit regeneration error:', e)
      const errorMessage: Message = {
        id: Date.now().toString() + '_error',
        role: 'assistant',
        content: tRaw('generic_error'),
        timestamp: new Date(),
      }
      const finalMessages = [...truncatedMessages, errorMessage]
      setMessages(finalMessages)
      onMessagesUpdate(finalMessages.map(m => ({
        ...m,
        timestamp: m.timestamp instanceof Date ? m.timestamp.toISOString() : m.timestamp,
      })), sessionId)
    } finally {
      setLoading(false)
    }
  }

  // Cancel editing
  const handleCancelEdit = () => {
    setEditingMessageId(null)
    setEditingContent('')
  }

  // Active filters display
  const activeFilterCount = filters.meetings.length + filters.projects.length + filters.folders.length + (filters.documents?.length || 0)

  return (
    <div className="h-full flex flex-col bg-brain-950/50">
      {/* Active Filters Bar */}
      {activeFilterCount > 0 && (
        <div className="px-4 py-2 bg-brain-900/50 border-b border-brain-700/30 flex items-center gap-2 text-xs flex-wrap">
          <span className="text-brain-500">{tRaw('context_label')}</span>
          {filters.documents && filters.documents.length > 0 && (
            <span className="px-2 py-0.5 bg-purple-600/20 text-purple-400 rounded-full">
              {tRaw('context_documents').replace('{count}', String(filters.documents.length))}
            </span>
          )}
          {filters.meetings.length > 0 && (
            <span className="px-2 py-0.5 bg-orange-600/20 text-orange-400 rounded-full">
              {tRaw('context_meetings').replace('{count}', String(filters.meetings.length))}
            </span>
          )}
          {filters.projects.length > 0 && (
            <span className="px-2 py-0.5 bg-blue-600/20 text-blue-400 rounded-full">
              {tRaw('context_projects').replace('{count}', String(filters.projects.length))}
            </span>
          )}
          {filters.folders.length > 0 && (
            <span className="px-2 py-0.5 bg-green-600/20 text-green-400 rounded-full">
              {tRaw('context_folders').replace('{count}', String(filters.folders.length))}
            </span>
          )}
        </div>
      )}

      {/* Mark: панель «Исследования» вместо ленты сообщений */}
      {agentMode === 'mark' && markView === 'research' ? (
        <div className="flex-1 overflow-hidden">
          <MarkResearchPanel userId={userId} />
        </div>
      ) : (
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.map((message) => (
          <div
            key={message.id}
            className={`flex gap-3 ${message.role === 'user' ? 'justify-end' : ''} group`}
          >
            {message.role === 'assistant' && (
              <div className="flex-shrink-0 w-8 h-8 rounded-full overflow-hidden bg-brain-600 flex items-center justify-center">
                <img src="/bot-avatar.svg" alt="AI" className="w-full h-full object-cover" />
              </div>
            )}
            
            <div
              className={`max-w-[80%] ${
                message.role === 'user'
                  ? 'chat-message user'
                  : 'chat-message assistant'
              }`}
            >
              {/* Content - Show editor if editing, otherwise show markdown */}
              {editingMessageId === message.id ? (
                <div className="space-y-2">
                  <textarea
                    value={editingContent}
                    onChange={(e) => setEditingContent(e.target.value)}
                    className="w-full min-h-[80px] bg-brain-900 border border-brain-600 rounded-lg p-2 text-sm text-white resize-none focus:outline-none focus:border-brain-500"
                    autoFocus
                  />
                  <div className="flex gap-2 justify-end">
                    <button
                      onClick={handleCancelEdit}
                      className="px-3 py-1 text-xs bg-brain-700 hover:bg-brain-600 rounded transition-colors flex items-center gap-1"
                    >
                      <X className="w-3 h-3" />
                      {tRaw('edit_cancel')}
                    </button>
                    <button
                      onClick={handleSaveEdit}
                      className="px-3 py-1 text-xs bg-brain-600 hover:bg-brain-500 rounded transition-colors flex items-center gap-1"
                    >
                      <Check className="w-3 h-3" />
                      {tRaw('edit_save')}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="prose prose-invert prose-sm max-w-none">
                  <ReactMarkdown 
                    remarkPlugins={[remarkGfm]}
                    components={{
                      a: ({node, ...props}) => <a {...props} target="_blank" rel="noopener noreferrer" className="text-brain-400 hover:underline" />,
                      ul: ({node, ...props}) => <ul {...props} className="list-disc pl-4 space-y-1" />,
                      ol: ({node, ...props}) => <ol {...props} className="list-decimal pl-4 space-y-1" />,
                      li: ({node, ...props}) => <li {...props} className="pl-1" />,
                      table: ({node, ...props}) => <div className="overflow-x-auto my-4"><table {...props} className="min-w-full divide-y divide-brain-700/50" /></div>,
                      thead: ({node, ...props}) => <thead {...props} className="bg-brain-800/50" />,
                      th: ({node, ...props}) => <th {...props} className="px-3 py-2 text-left text-xs font-medium text-brain-300 uppercase tracking-wider" />,
                      tbody: ({node, ...props}) => <tbody {...props} className="divide-y divide-brain-700/30 bg-brain-900/20" />,
                      td: ({node, ...props}) => <td {...props} className="px-3 py-2 whitespace-nowrap text-sm text-brain-100" />,
                    }}
                  >
                    {message.content}
                  </ReactMarkdown>
                </div>
              )}

              {/* Reasoning steps */}
              {message.reasoning_steps && message.reasoning_steps.length > 0 && (
                <details className="mt-3 text-xs">
                  <summary className="cursor-pointer text-brain-400 hover:text-brain-300 flex items-center gap-1">
                    <Sparkles className="w-3 h-3" />
                    {tRaw('reasoning_steps').replace('{count}', String(message.reasoning_steps.length))}
                  </summary>
                  <ul className="mt-2 space-y-1 text-brain-400 pl-4 border-l border-brain-600/30">
                    {message.reasoning_steps.map((step, i) => (
                      <li key={i}>{step}</li>
                    ))}
                  </ul>
                </details>
              )}

              {message.response_metadata?.evidence_contract && (() => {
                const evidenceContract = message.response_metadata?.evidence_contract
                const directEvidence = evidenceContract?.direct_evidence || []
                const supportingEvidence = evidenceContract?.supporting_evidence || []
                const inferredPath = evidenceContract?.inferred_path || []
                const ambiguities = evidenceContract?.ambiguities || []
                const confidenceEnvelope = evidenceContract?.confidence_envelope
                const traceNavigationTarget = buildGraphTraceNavigationTarget(message.response_metadata, evidenceContract, tRaw)
                const confidenceText =
                  formatConfidencePercent(confidenceEnvelope?.score ?? message.response_metadata?.confidence)
                const confidenceLabel = getConfidenceLabel(confidenceEnvelope?.label, tRaw)

                return (
                  <details className="mt-2 text-xs">
                    <summary className="cursor-pointer text-brain-400 hover:text-brain-300 flex items-center gap-1">
                      <ClipboardList className="w-3 h-3" />
                      {tRaw('evidence_base')}
                      {confidenceText && (
                        <span className="text-brain-500">
                          ({confidenceText}{confidenceLabel ? `, ${confidenceLabel}` : ''})
                        </span>
                      )}
                    </summary>

                    <div className="mt-2 space-y-3 pl-4 border-l border-amber-600/30">
                      <div className="flex flex-wrap gap-2 text-[11px]">
                        {confidenceText && (
                          <span className="px-2 py-1 rounded bg-brain-800/60 text-brain-300">
                            {tRaw('confidence_label').replace('{value}', String(confidenceText))}
                          </span>
                        )}
                        {confidenceEnvelope?.search_depth && (
                          <span className="px-2 py-1 rounded bg-brain-800/60 text-brain-300">
                            {tRaw('depth_label').replace('{value}', String(confidenceEnvelope.search_depth))}
                          </span>
                        )}
                        {typeof confidenceEnvelope?.source_count === 'number' && (
                          <span className="px-2 py-1 rounded bg-brain-800/60 text-brain-300">
                            {tRaw('sources_label').replace('{value}', String(confidenceEnvelope.source_count))}
                          </span>
                        )}
                        {typeof confidenceEnvelope?.structured_source_count === 'number' && (
                          <span className="px-2 py-1 rounded bg-brain-800/60 text-brain-300">
                            {tRaw('structured_label').replace('{value}', String(confidenceEnvelope.structured_source_count))}
                          </span>
                        )}
                        {traceNavigationTarget && onNavigateToSource && (
                          <button
                            onClick={() => onNavigateToSource(traceNavigationTarget)}
                            className="px-2 py-1 rounded bg-purple-500/10 text-purple-300 hover:bg-purple-500/20 transition-colors"
                          >
                            {tRaw('trace_in_graph')}
                          </button>
                        )}
                      </div>

                      {directEvidence.length > 0 && (
                        <div>
                          <div className="text-amber-300 font-medium mb-1">{tRaw('direct_evidence')}</div>
                          <ul className="space-y-2">
                            {directEvidence.map((item, i) => {
                              const navigationTarget = buildSourceNavigationTarget(item)
                              const graphNavigationTarget = buildGraphNavigationTarget(item)
                              return (
                                <li key={`${item.id || item.title || 'direct'}_${i}`} className="text-brain-300">
                                  <div className="flex items-start justify-between gap-2">
                                    <div className="text-amber-200">
                                      {getSourceTypeLabel(item.normalized_type || item.type, tRaw)}: {item.title || item.id || `${tRaw('source_type_default')} ${i + 1}`}
                                      {formatEvidenceScore(item.score) && (
                                        <span className="text-brain-500 ml-2">{t('relevance_label', { score: formatEvidenceScore(item.score) ?? '' })}</span>
                                      )}
                                    </div>
                                    {(navigationTarget || graphNavigationTarget) && onNavigateToSource && (
                                      <div className="flex items-center gap-1">
                                        {navigationTarget && (
                                          <button
                                            onClick={() => onNavigateToSource(navigationTarget)}
                                            className="text-[11px] px-2 py-1 rounded bg-amber-500/10 text-amber-300 hover:bg-amber-500/20 transition-colors whitespace-nowrap"
                                          >
                                            {t('open_button')}
                                          </button>
                                        )}
                                        {graphNavigationTarget && (
                                          <button
                                            onClick={() => onNavigateToSource(graphNavigationTarget)}
                                            className="text-[11px] px-2 py-1 rounded bg-purple-500/10 text-purple-300 hover:bg-purple-500/20 transition-colors whitespace-nowrap"
                                          >
                                            {t('to_graph_button')}
                                          </button>
                                        )}
                                      </div>
                                    )}
                                  </div>
                                  {item.snippet && (
                                    <div className="text-brain-500 mt-0.5">
                                      {item.snippet}
                                    </div>
                                  )}
                                </li>
                              )
                            })}
                          </ul>
                        </div>
                      )}

                      {supportingEvidence.length > 0 && (
                        <div>
                          <div className="text-blue-300 font-medium mb-1">Supporting evidence</div>
                          <ul className="space-y-2">
                            {supportingEvidence.map((item, i) => {
                              const navigationTarget = buildSourceNavigationTarget(item)
                              const graphNavigationTarget = buildGraphNavigationTarget(item)
                              return (
                                <li key={`${item.id || item.title || 'support'}_${i}`} className="text-brain-300">
                                  <div className="flex items-start justify-between gap-2">
                                    <div className="text-blue-200">
                                      {getSourceTypeLabel(item.normalized_type || item.type, tRaw)}: {item.title || item.id || `${tRaw('source_type_default')} ${i + 1}`}
                                    </div>
                                    {(navigationTarget || graphNavigationTarget) && onNavigateToSource && (
                                      <div className="flex items-center gap-1">
                                        {navigationTarget && (
                                          <button
                                            onClick={() => onNavigateToSource(navigationTarget)}
                                            className="text-[11px] px-2 py-1 rounded bg-blue-500/10 text-blue-300 hover:bg-blue-500/20 transition-colors whitespace-nowrap"
                                          >
                                            {t('open_button')}
                                          </button>
                                        )}
                                        {graphNavigationTarget && (
                                          <button
                                            onClick={() => onNavigateToSource(graphNavigationTarget)}
                                            className="text-[11px] px-2 py-1 rounded bg-purple-500/10 text-purple-300 hover:bg-purple-500/20 transition-colors whitespace-nowrap"
                                          >
                                            {t('to_graph_button')}
                                          </button>
                                        )}
                                      </div>
                                    )}
                                  </div>
                                  {item.snippet && (
                                    <div className="text-brain-500 mt-0.5">
                                      {item.snippet}
                                    </div>
                                  )}
                                </li>
                              )
                            })}
                          </ul>
                        </div>
                      )}

                      {inferredPath.length > 0 && (
                        <div>
                          <div className="text-purple-300 font-medium mb-1">{t('reasoning_path')}</div>
                          <ul className="space-y-1 text-brain-400">
                            {inferredPath.map((step, i) => (
                              <li key={`${step}_${i}`}>{step}</li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {ambiguities.length > 0 && (
                        <div>
                          <div className="text-rose-300 font-medium mb-1">{t('uncertainties_heading')}</div>
                          <ul className="space-y-1 text-brain-400">
                            {ambiguities.map((item, i) => (
                              <li key={`${item}_${i}`}>{item}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  </details>
                )
              })()}

              {/* Sources */}
              {message.sources && message.sources.length > 0 && (
                <details className="mt-2 text-xs">
                  <summary className="cursor-pointer text-brain-400 hover:text-brain-300 flex items-center gap-1">
                    {t('sources_heading', { count: message.sources.length })}
                  </summary>
                  <ul className="mt-2 space-y-2 text-brain-400 pl-4 border-l border-green-600/30">
                    {message.sources.map((source: any, i: number) => {
                      const navigationTarget = buildSourceNavigationTarget(source)
                      const graphNavigationTarget = buildGraphNavigationTarget(source)
                      // Извлекаем понятное название источника
                      const sourceType = source.type || source._label || source.metadata?.type || tRaw('source_doc_fallback');
                      const sourceTypeKey = String(sourceType);
                      const sourceName = 
                        source.title || 
                        source.name || 
                        source.metadata?.title ||
                        source.metadata?.name ||
                        source.metadata?.document_title ||
                        source.metadata?.meeting_title ||
                        (source.text && source.text.length > 50 ? source.text.slice(0, 50) + '...' : source.text) ||
                        (source.id && !source.id.includes('-') ? source.id : null) ||
                        tRaw('source_prefix') + ' ' + (i + 1);
                      
                      // Форматируем тип источника
                      const typeMap: Record<string, string> = {
                        'Meeting': tRaw('src_meeting'),
                        'Task': tRaw('src_task'),
                        'Decision': tRaw('src_decision'),
                        'Person': tRaw('src_person'),
                        'Entity': tRaw('src_entity'),
                        'Document': tRaw('src_document'),
                        'Chunk': tRaw('src_chunk'),
                        'chunk': tRaw('src_chunk'),
                        'document': tRaw('src_document'),
                        'presentation': tRaw('src_presentation'),
                        'pdf': '📄 PDF',
                      };
                      const typeLabel = typeMap[sourceTypeKey] || getSourceTypeLabel(sourceTypeKey, tRaw);
                      
                      return (
                        <li key={i} className="text-green-400/80">
                          <div className="flex items-start justify-between gap-2">
                            <div>
                              <span className="text-brain-500">{typeLabel}:</span>{' '}
                              <span className="text-green-300">{sourceName}</span>
                              {source.metadata?.meeting_id && (
                                <span className="text-brain-600 ml-1">{t('meeting_suffix')}</span>
                              )}
                            </div>
                            {(navigationTarget || graphNavigationTarget) && onNavigateToSource && (
                              <div className="flex items-center gap-1">
                                {navigationTarget && (
                                  <button
                                    onClick={() => onNavigateToSource(navigationTarget)}
                                    className="text-[11px] px-2 py-1 rounded bg-green-500/10 text-green-300 hover:bg-green-500/20 transition-colors whitespace-nowrap"
                                  >
                                    {t('open_button')}
                                  </button>
                                )}
                                {graphNavigationTarget && (
                                  <button
                                    onClick={() => onNavigateToSource(graphNavigationTarget)}
                                    className="text-[11px] px-2 py-1 rounded bg-purple-500/10 text-purple-300 hover:bg-purple-500/20 transition-colors whitespace-nowrap"
                                  >
                                    {t('to_graph_button')}
                                  </button>
                                )}
                              </div>
                            )}
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </details>
              )}

              {/* Agents involved */}
              {message.agents_involved && message.agents_involved.length > 0 && (
                <div className="mt-2 text-xs">
                  <span className="text-brain-500">{t('agents_label')}</span>
                  <span className="text-blue-400">
                    {message.agents_involved.join(' → ')}
                  </span>
                </div>
              )}

              {/* Timestamp and Actions Row - ChatGPT/Claude style */}
              <div className="flex items-center gap-2 mt-2">
                <span className="text-xs text-brain-500" suppressHydrationWarning>
                  {(message.timestamp instanceof Date ? message.timestamp : new Date(message.timestamp)).toLocaleTimeString('ru-RU', {
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </span>
                
                {/* Action buttons - appear on hover, like ChatGPT/Claude */}
                {message.id !== '0' && editingMessageId !== message.id && (
                  <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    {/* Copy button */}
                    <button
                      onClick={() => handleCopyMessage(message)}
                      className="p-1 rounded hover:bg-brain-700/50 transition-colors text-brain-500 hover:text-brain-300"
                      title={t("copy_title")}
                    >
                      {copiedMessageId === message.id ? (
                        <Check className="w-3.5 h-3.5 text-green-400" />
                      ) : (
                        <Copy className="w-3.5 h-3.5" />
                      )}
                    </button>
                    
                    {/* Edit button - only for user messages */}
                    {message.role === 'user' && (
                      <button
                        onClick={() => handleStartEdit(message)}
                        className="p-1 rounded hover:bg-brain-700/50 transition-colors text-brain-500 hover:text-brain-300"
                        title={t("edit_title")}
                      >
                        <Pencil className="w-3.5 h-3.5" />
                      </button>
                    )}
                    
                    {/* Delete button */}
                    <button
                      onClick={() => handleDeleteMessage(message.id)}
                      className="p-1 rounded hover:bg-brain-700/50 transition-colors text-brain-500 hover:text-red-400"
                      title={t("delete_title")}
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>

                    {/* ТЗ → в работу: handoff в очередь CLI-исполнителей */}
                    {message.role === 'assistant' && message.response_metadata?.is_tz && (
                      <button
                        onClick={() => { setHandoffFor(handoffFor?.id === message.id ? null : message); setHandoffNote('') }}
                        className="p-1 rounded hover:bg-brain-700/50 transition-colors text-brain-500 hover:text-emerald-300"
                        title={t('handoff_button_title')}
                      >
                        <Zap className="w-3.5 h-3.5" />
                      </button>
                    )}

                    {/* Deep dive — только для ответов ассистента с флагом */}
                    {message.role === 'assistant' && message.response_metadata?.deep_dive_available && (
                      <button
                        onClick={() => handleDeepDive(message)}
                        disabled={deepDivingId === message.id}
                        className="p-1 rounded hover:bg-brain-700/50 transition-colors text-brain-500 hover:text-purple-300 disabled:opacity-50"
                        title={t('deep_dive_title')}
                      >
                        <Sparkles className={`w-3.5 h-3.5 ${deepDivingId === message.id ? 'animate-pulse' : ''}`} />
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>

            {message.role === 'user' && (
              <div className="flex-shrink-0 w-8 h-8 rounded-full bg-brain-700 flex items-center justify-center">
                <User className="w-5 h-5" />
              </div>
            )}
          </div>
        ))}

        {loading && (
          <div className="flex gap-3">
            <div className="flex-shrink-0 w-8 h-8 rounded-full overflow-hidden bg-brain-600 flex items-center justify-center">
              <img src="/bot-avatar.svg" alt="AI" className="w-full h-full object-cover" />
            </div>
            <div className="chat-message assistant">
              <div className="flex items-center gap-2">
                <div className="w-2 h-2 bg-brain-400 rounded-full animate-bounce" />
                <div className="w-2 h-2 bg-brain-400 rounded-full animate-bounce [animation-delay:0.1s]" />
                <div className="w-2 h-2 bg-brain-400 rounded-full animate-bounce [animation-delay:0.2s]" />
                <span className="text-sm text-brain-400 ml-2">{t('thinking')}</span>
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>
      )}

      {/* Input */}
      <div className="border-t border-brain-600/20 p-4 bg-brain-950/80">
        {/* Handoff-форма: ТЗ из чата → очередь CLI-исполнителей */}
        {(handoffFor || handoffNote) && (
          <div className="mb-3 p-3 rounded-lg border border-emerald-500/30 bg-emerald-500/5 space-y-2">
            {handoffFor && (
              <>
                <p className="text-xs font-medium text-emerald-200">{t('handoff_title')}</p>
                <div className="flex items-center gap-2 flex-wrap">
                  <select
                    value={handoffAgent}
                    onChange={e => setHandoffAgent(e.target.value)}
                    className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs"
                  >
                    <option value="claude">Claude Code</option>
                    <option value="codex">Codex</option>
                    <option value="cursor">Cursor</option>
                  </select>
                  <input
                    value={handoffRepo}
                    onChange={e => setHandoffRepo(e.target.value)}
                    placeholder={t('handoff_repo_placeholder')}
                    className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs flex-1 min-w-[220px] font-mono"
                  />
                  <button
                    onClick={submitHandoff}
                    disabled={handoffBusy}
                    className="px-3 py-1 rounded bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-xs font-medium"
                  >
                    {handoffBusy ? '…' : t('handoff_submit')}
                  </button>
                  <button
                    onClick={() => { setHandoffFor(null); setHandoffNote('') }}
                    className="p-1 text-brain-500 hover:text-gray-300"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
                <p className="text-[10px] text-slate-500">{t('handoff_hint')}</p>
              </>
            )}
            {handoffNote && <p className="text-xs text-emerald-300">{handoffNote}</p>}
          </div>
        )}

        {/* Strategy Selector - only for Brain mode */}
        {agentMode === 'brain' && (
          <div className="flex gap-2 mb-3 flex-wrap items-center justify-between">
            <div className="flex gap-2 flex-wrap">
            {[
              { id: 'auto',     icon: '🤖', label: 'Auto',     tip: tRaw('strategy_auto_tip') },
              { id: 'quick',    icon: '⚡', label: 'Quick',    tip: tRaw('strategy_quick_answer_tip') },
              { id: 'standard', icon: '📊', label: 'Standard', tip: tRaw('strategy_standard_tip') },
              { id: 'deep',     icon: '🧠', label: 'Deep',     tip: tRaw('strategy_deep_tip') },
              { id: 'tasker',   icon: '📋', label: 'Tasker',   tip: tRaw('strategy_tasker_tip') },
              { id: 'tz',       icon: '📄', label: tRaw('strategy_tz_label'), tip: tRaw('strategy_tz_tip') },
              { id: 'agentic',  icon: '🤝', label: 'Agentic',  tip: 'Multi-Agent Pipeline' },
              { id: 'research', icon: '🔬', label: 'Research', tip: tRaw('strategy_research_tip') },
              { id: 'advisor',  icon: '🧭', label: tRaw('strategy_advisor_label'), tip: tRaw('strategy_advisor_tip') },
            ].map((s) => (
              <button
                key={s.id}
                onClick={() => setStrategy(s.id)}
                className={`text-xs px-2 py-1 rounded transition-colors uppercase font-bold tracking-wider ${
                  strategy === s.id
                    ? 'bg-brain-600 text-brain-50 shadow-sm'
                    : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
                }`}
                title={s.tip}
              >
                {s.icon} {s.label}
              </button>
            ))}
            {(strategy === 'research' || strategy === 'advisor') && (
              <label
                className={`text-xs px-2 py-1 rounded cursor-pointer transition-colors flex items-center gap-1 border ${
                  deepResearch
                    ? 'bg-purple-600/60 text-purple-100 border-purple-400/60 shadow-sm'
                    : 'bg-brain-800/50 text-purple-300 border-purple-500/30 hover:bg-brain-700/50'
                }`}
                title={t('deep_research_tip')}
              >
                <input
                  type="checkbox"
                  className="hidden"
                  checked={deepResearch}
                  onChange={(e) => setDeepResearch(e.target.checked)}
                />
                {t('deep_research_label')}
              </label>
            )}
            </div>
            
            {/* Model Tier Selector */}
            <div className="flex gap-1 items-center">
              <button
                onClick={() => setModelTier('standard')}
                className={`text-xs px-2 py-1 rounded transition-all flex items-center gap-1 ${
                  modelTier === 'standard'
                    ? 'bg-brain-600 text-brain-50 shadow-sm'
                    : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
                }`}
                title={t("model_standard_title")}
              >
                {t('model_standard_label')}
              </button>
              <button
                onClick={() => setModelTier('premium')}
                className={`text-xs px-2 py-1 rounded transition-all flex items-center gap-1 ${
                  modelTier === 'premium'
                    ? 'bg-gradient-to-r from-amber-500 to-yellow-500 text-black shadow-lg shadow-amber-500/30'
                    : 'bg-brain-800/50 text-amber-400 hover:bg-brain-700/50 border border-amber-500/30'
                }`}
                title={t("model_premium_title")}
              >
                <Crown className="w-3 h-3" />
                {t('model_premium_label')}
              </button>
            </div>
          </div>
        )}

        {/* Automation Sub-Mode Selector */}
        {agentMode === 'automation' && (
          <div className="flex gap-2 mb-3 flex-wrap items-center justify-between">
            <div className="flex gap-2 flex-wrap">
              {[
                { id: 'tasks', icon: '✅', label: tRaw('function_tasks_label'), title: tRaw('function_tasks_title') },
                { id: 'web', icon: '🌐', label: 'Web', title: tRaw('automation_web_unavailable_title'), disabled: true },
                { id: 'meetflow', icon: '🎙️', label: 'MeetFlow', title: 'meetflow.synlabs.pro' },
                { id: 'calls', icon: '📞', label: 'Calls', title: tRaw('automation_calls_title') },
                { id: 'tess', icon: '🧩', label: 'Tess', title: tRaw('automation_tess_title') },
                { id: 'hybrid', icon: '⚡', label: tRaw('automation_hybrid_label'), title: tRaw('automation_hybrid_title') },
                { id: 'agent_oc', icon: '🤖', label: tRaw('automation_agent_label'), title: tRaw('automation_agent_title') },
              ].map((mode) => (
                <button
                  key={mode.id}
                  onClick={() => !mode.disabled && setAutomationMode(mode.id)}
                  disabled={!!mode.disabled}
                  className={`text-xs px-2 py-1 rounded transition-colors font-medium ${
                    mode.disabled
                      ? 'bg-brain-900/40 text-brain-600 opacity-50 cursor-not-allowed'
                      : automationMode === mode.id
                      ? 'bg-blue-600 text-white shadow-sm'
                      : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
                  }`}
                  title={mode.title}
                >
                  {mode.icon} {mode.label}
                </button>
              ))}
            </div>
            
            {/* Model Tier Selector */}
            <div className="flex gap-1 items-center">
              <button
                onClick={() => setModelTier('standard')}
                className={`text-xs px-2 py-1 rounded transition-all flex items-center gap-1 ${
                  modelTier === 'standard'
                    ? 'bg-brain-600 text-brain-50 shadow-sm'
                    : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
                }`}
                title={t("model_standard_title")}
              >
                {t('model_standard_label')}
              </button>
              <button
                onClick={() => setModelTier('premium')}
                className={`text-xs px-2 py-1 rounded transition-all flex items-center gap-1 ${
                  modelTier === 'premium'
                    ? 'bg-gradient-to-r from-amber-500 to-yellow-500 text-black shadow-lg shadow-amber-500/30'
                    : 'bg-brain-800/50 text-amber-400 hover:bg-brain-700/50 border border-amber-500/30'
                }`}
                title={t("model_premium_title")}
              >
                <Crown className="w-3 h-3" />
                {t('model_premium_label')}
              </button>
            </div>
          </div>
        )}

        {/* Tess tool selector (local router with ReAct loop) */}
        {agentMode === 'automation' && automationMode === 'tess' && (
          <div className="mb-3 p-3 rounded bg-brain-900/40 border border-brain-600/20">
            <div className="text-xs text-brain-300 mb-2 font-semibold">
              {t('tess_panel_heading')}
              <span className="ml-2 text-brain-500 font-normal">
                {t('tess_panel_subheading')}
              </span>
            </div>
            <div className="flex gap-2 flex-wrap">
              {[
                { id: 'get_all_tasks', label: tRaw('tess_fn_tasks_label'), desc: tRaw('tess_fn_tasks_desc') },
                { id: 'get_person_info', label: tRaw('tess_fn_person_label'), desc: tRaw('tess_fn_person_desc') },
                { id: 'get_project_status', label: tRaw('tess_fn_project_label'), desc: tRaw('tess_fn_project_desc') },
                { id: 'get_recent_meetings', label: tRaw('tess_fn_meetings_label'), desc: tRaw('tess_fn_meetings_desc') },
                { id: 'get_meeting_context', label: tRaw('tess_fn_meeting_ctx_label'), desc: tRaw('tess_fn_meeting_ctx_desc') },
                { id: 'search_knowledge', label: tRaw('tess_fn_search_label'), desc: tRaw('tess_fn_search_desc') },
                { id: 'get_recent_decisions', label: tRaw('tess_fn_decisions_label'), desc: tRaw('tess_fn_decisions_desc') },
                { id: 'analyze_with_llm', label: tRaw('tess_fn_analyze_label'), desc: tRaw('tess_fn_analyze_desc') },
                { id: 'send_telegram', label: tRaw('tess_fn_telegram_label'), desc: tRaw('tess_fn_telegram_desc') },
                { id: 'create_task', label: tRaw('tess_fn_create_task_label'), desc: tRaw('tess_fn_create_task_desc') },
              ].map((t) => {
                const checked = tessAllowedTools.includes(t.id)
                return (
                  <label 
                    key={t.id} 
                    className={`text-xs flex items-center gap-2 px-2 py-1 rounded cursor-pointer transition-colors ${
                      checked 
                        ? 'bg-emerald-600/30 text-emerald-300 border border-emerald-500/30' 
                        : 'bg-brain-800/30 text-brain-400 hover:bg-brain-700/30'
                    }`}
                    title={t.desc}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => {
                        const next = e.target.checked
                          ? Array.from(new Set([...tessAllowedTools, t.id]))
                          : tessAllowedTools.filter((x) => x !== t.id)
                        setTessAllowedTools(next)
                      }}
                      className="sr-only"
                    />
                    {t.label}
                  </label>
                )
              })}
            </div>
            <div className="mt-2 text-xs text-brain-500">
              {t('react_loop_hint')}
            </div>
          </div>
        )}

        {/* Mode indicator and Model Selector for non-brain modes (except automation which has its own) */}
        {agentMode !== 'brain' && agentMode !== 'automation' && (
          <div className="mb-3 flex items-center justify-between">
            <div className="text-xs text-brain-400">
            {agentMode === 'mark' && (
              <span className="flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-orange-400" />
                {t('mode_mark_label')}
                {/* Простой (соло-скилл) vs Команда (многошаговый GroupChat) */}
                <span className="flex gap-1 ml-2">
                  <button
                    onClick={() => setMarkMode('solo')}
                    className={`text-xs px-2 py-0.5 rounded transition-all ${
                      markMode === 'solo'
                        ? 'bg-orange-600/80 text-white shadow-sm'
                        : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
                    }`}
                    title={t('mark_mode_solo_title')}
                  >
                    ⚡ {t('mark_mode_solo_label')}
                  </button>
                  <button
                    onClick={() => setMarkMode('swarm')}
                    className={`text-xs px-2 py-0.5 rounded transition-all ${
                      markMode === 'swarm'
                        ? 'bg-orange-600/80 text-white shadow-sm'
                        : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
                    }`}
                    title={t('mark_mode_swarm_title')}
                  >
                    👥 {t('mark_mode_swarm_label')}
                  </button>
                  <button
                    onClick={() => setMarkView(markView === 'research' ? 'chat' : 'research')}
                    className={`text-xs px-2 py-0.5 rounded transition-all ${
                      markView === 'research'
                        ? 'bg-orange-600 text-white shadow-sm'
                        : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
                    }`}
                    title={t('mark_research_view_title')}
                  >
                    {t('mark_research_view_label')}
                  </button>
                </span>
              </span>
            )}
            {agentMode === 'transcripts' && (
              <span className="flex items-center gap-1">
                <MessageCircle className="w-4 h-4 text-green-400" />
                {t('mode_local_label')}
              </span>
            )}
            </div>
            
            {/* Model Tier Selector for non-brain modes */}
            <div className="flex gap-1 items-center">
              <button
                onClick={() => setModelTier('standard')}
                className={`text-xs px-2 py-1 rounded transition-all flex items-center gap-1 ${
                  modelTier === 'standard'
                    ? 'bg-brain-600 text-brain-50 shadow-sm'
                    : 'bg-brain-800/50 text-brain-400 hover:bg-brain-700/50'
                }`}
                title={t("model_standard_title")}
              >
                {t('model_standard_label')}
              </button>
              <button
                onClick={() => setModelTier('premium')}
                className={`text-xs px-2 py-1 rounded transition-all flex items-center gap-1 ${
                  modelTier === 'premium'
                    ? 'bg-gradient-to-r from-amber-500 to-yellow-500 text-black shadow-lg shadow-amber-500/30'
                    : 'bg-brain-800/50 text-amber-400 hover:bg-brain-700/50 border border-amber-500/30'
                }`}
                title={t("model_premium_title")}
              >
                <Crown className="w-3 h-3" />
                {t('model_premium_label')}
              </button>
            </div>
          </div>
        )}

        {/* Временные файлы чата (скрепка) — только режим встреч/документов */}
        {agentMode === 'transcripts' && attachedFiles.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {attachedFiles.map(f => (
              <span key={f.id}
                className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-full bg-green-900/40 text-green-300 border border-green-700/40"
                title={t('temp_file_tooltip', { chars: f.chars.toLocaleString() })}>
                <Paperclip className="w-3 h-3" />
                {f.name}
                <button
                  onClick={() => setAttachedFiles(prev => prev.filter(x => x.id !== f.id))}
                  className="ml-1 hover:text-red-400" aria-label="remove">
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        {/* Input Area */}
        <div className="flex gap-3 items-end">
          {agentMode === 'transcripts' && (
            <>
              <input ref={fileInputRef} type="file" className="hidden"
                accept=".pdf,.docx,.doc,.txt,.md,.csv,.xlsx,.json"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFileAttach(f) }} />
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={uploadingFile}
                className="p-3 rounded-lg bg-brain-800/60 text-brain-300 hover:bg-brain-700/60 hover:text-green-300 transition-all disabled:opacity-50"
                title={t('attach_temp_file_title')}>
                <Paperclip className={`w-5 h-5 ${uploadingFile ? 'animate-pulse' : ''}`} />
              </button>
            </>
          )}
          <textarea
            value={input}
            onChange={(e) => {
              setInput(e.target.value)
              // Авто-расширение textarea
              e.target.style.height = 'auto'
              e.target.style.height = Math.min(e.target.scrollHeight, 200) + 'px'
            }}
            onKeyDown={handleKeyDown}
            placeholder={
              agentMode === 'brain' ? (
                strategy === 'tz' ? tRaw("placeholder_tz") :
                strategy === 'tasker' ? tRaw("placeholder_tasker") :
                tRaw("placeholder_brain_company")
              ) :
              agentMode === 'mark' ? tRaw("placeholder_mark") :
              agentMode === 'automation' ? tRaw("placeholder_automation") :
              tRaw("placeholder_default")
            }
            className="input flex-1 resize-none min-h-[44px] max-h-[200px] overflow-y-auto"
            rows={1}
            disabled={loading}
            style={{ height: 'auto' }}
          />
          <button
            onClick={sendMessage}
            disabled={loading || !input.trim()}
            className="btn btn-primary px-4 h-[44px] flex-shrink-0"
          >
            <Send className="w-5 h-5" />
          </button>
        </div>
        <p className="text-xs text-brain-500 mt-2">
          {t('send_hint')}
        </p>
      </div>
    </div>
  )
}
