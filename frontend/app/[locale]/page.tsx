'use client'

import { useState, useEffect, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { Brain, MessageSquare, Network, Activity, AlertTriangle, Menu, X, TrendingUp, Zap, MessageCircle, Clock, RefreshCw, FileText, Layout, Edit3, BookOpen, Lightbulb, Sparkles, HelpCircle, GitBranch } from 'lucide-react'
import GraphVisualization from '@/components/GraphVisualization'
import NeuralCanvas from '@/components/landing/NeuralCanvas'
import ChatPanel, { SourceNavigationTarget } from '@/components/ChatPanel'
import StatsPanel from '@/components/StatsPanel'
import HomeTab from '@/components/HomeTab'
import { authFetch } from '@/lib/authFetch'
import ChatSidebar, { ChatSession, FilterState } from '@/components/ChatSidebar'
import AutomationsPanel from '@/components/AutomationsPanel'
import AgentAutomationsPanel from '@/components/AgentAutomationsPanel'
import AgentSkillsCard from '@/components/AgentSkillsCard'
import HandoffQueuePanel from '@/components/HandoffQueuePanel'
import OfficeTasksPanel from '@/components/OfficeTasksPanel'
import GuideChatPanel from '@/components/GuideChatPanel'
import SkillsPanel from '@/components/SkillsPanel'
import LLMProfilesPanel from '@/components/LLMProfilesPanel'
import OrgPanel from '@/components/OrgPanel'
import KnowledgeSyncPanel from '@/components/KnowledgeSyncPanel'
import DocumentsPanel from '@/components/DocumentsPanel'
import DatasetsPanel from '@/components/DatasetsPanel'
import DocResearchPanel from '@/components/DocResearchPanel'
import MeetingDocsPanel from '@/components/MeetingDocsPanel'
import DocumentsHubPanel from '@/components/DocumentsHubPanel'
import Object360Panel from '@/components/Object360Panel'
import AssistantCatalogPanel from '@/components/AssistantCatalogPanel'
import TemplatesPanel from '@/components/TemplatesPanel'
import BoardPanel from '@/components/BoardPanel'
import IntegrationsPanel from '@/components/IntegrationsPanel'
import DailyPulseAdminPanel from '@/components/DailyPulseAdminPanel'
import CustomersPanel from '@/components/CustomersPanel'
import DataBusPanel from '@/components/DataBusPanel'
import KnowledgeEditorPanel from '@/components/KnowledgeEditorPanel'
import SnapshotsPanel from '@/components/SnapshotsPanel'
import LineageTracePanel from '@/components/LineageTracePanel'
import MemoryReviewPanel from '@/components/MemoryReviewPanel'
import ReportsPanel from '@/components/ReportsPanel'
import VirtualBoardPanel from '@/components/VirtualBoardPanel'
import PulsePanel from '@/components/PulsePanel'
import ClientSimPanel from '@/components/ClientSimPanel'
import ChatSourcesCard from '@/components/ChatSourcesCard'
import CommunityLensPanel from '@/components/CommunityLensPanel'
import Team360Panel from '@/components/Team360Panel'
import SimaApp from '@/sima/SimaApp'

// Основные вкладки: Home (wow-screen) + Знания, Синхронизация, Автоматизации, Доска, SIMA
type Tab = 'home' | 'knowledge' | 'sync' | 'automations' | 'board' | 'sima'
// Подвкладки
type KnowledgeSubTab = 'graph' | 'documents' | 'datasets' | 'object360' | 'customers' | 'insights' | 'snapshots' | 'lineage' | 'review'
type SyncSubTab = 'sync' | 'editor'
type AutomationsSubTab = 'classic' | 'agent' | 'skills' | 'catalog' | 'queue' | 'office'
type InsightsSubTab = 'insights' | 'reports' | 'team360' | 'board' | 'pulse' | 'clients'
type AgentMode = 'brain' | 'mark' | 'automation' | 'transcripts'

interface AvailableFilters {
  meetings: Array<{ id: string; title: string; project_id?: string; folder_id?: string }>
  projects: Array<{ id: string; name: string }>
  folders: Array<{ id: string; name: string; project_id?: string }>
  documents: Array<{ id: string; title: string; doc_type?: string; sync_status?: string; folder?: string }>
}

interface AgentModeConfig {
  id: AgentMode
  name: string
  description: string
  icon: React.ReactNode
  color: string
}

interface DocumentFocusRequest {
  documentId: string
  requestId: number
}

interface SnapshotFocusRequest {
  tab: 'company' | 'person' | 'project'
  entityId?: string
  requestId: number
}

interface GraphFocusRequest {
  entityId: string
  label?: string
  requestId: number
}

interface GraphTraceRequest {
  targets: Array<{
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
  edges?: Array<{
    fromKey: string
    toKey: string
    label?: string
    kind?: string
    pathOrder?: number
    retrievalStage?: string
    segmentStage?: string
  }>
  segments?: Array<{
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
  missingLinks?: Array<{
    fromLabel?: string
    toLabel?: string
    reason?: string
  }>
  reasoningSteps?: string[]
  title?: string
  requestId: number
}

const buildAgentModes = (t: (k: string) => string): AgentModeConfig[] => [
  {
    id: 'brain',
    name: 'Brain',
    description: t('main_page.agent_brain_desc'),
    icon: <Brain className="w-4 h-4" />,
    color: 'purple'
  },
  {
    id: 'transcripts',
    name: t('main_page.agent_local_name'),
    description: t('main_page.agent_local_desc'),
    icon: <MessageCircle className="w-4 h-4" />,
    color: 'green'
  },
  {
    id: 'mark',
    name: 'Mark',
    description: t('main_page.agent_mark_desc'),
    icon: <TrendingUp className="w-4 h-4" />,
    color: 'orange'
  },
  {
    id: 'automation',
    name: 'Auto',
    description: t('main_page.agent_automation_desc'),
    icon: <Zap className="w-4 h-4" />,
    color: 'blue'
  },
]

import AccountMenu from '@/components/AccountMenu'
import BillingPanel from '@/components/BillingPanel'
import EvolutionPanel from '@/components/EvolutionPanel'
import HoldingConsolePanel from '@/components/HoldingConsolePanel'
import { LanguageSwitcher } from '@/components/LanguageSwitcher'
import { useTranslations } from 'next-intl'

export default function Home() {
  // W44: i18n hook — экспонируем `t` для use в JSX.
  // Дальнейшая миграция: заменяй hardcoded строки на t('group.key').
  const t = useTranslations()
  const AGENT_MODES = buildAgentModes(t)
  const router = useRouter()
  // wow-экран первого контакта: пользователь приземляется на Home, а не сразу
  // в граф (он слишком плотный для первого впечатления). Home — это
  // editorial briefing «что система для тебя нашла», граф — drill-down.
  const [activeTab, setActiveTab] = useState<Tab>('home')
  const [knowledgeSubTab, setKnowledgeSubTab] = useState<KnowledgeSubTab>('graph')
  // Имя для перехода «граф → Объект 360» (клик по узлу в 3D)
  const [object360Name, setObject360Name] = useState('')
  const [syncSubTab, setSyncSubTab] = useState<SyncSubTab>('sync')
  const [automationsSubTab, setAutomationsSubTab] = useState<AutomationsSubTab>('classic')
  const [insightsSubTab, setInsightsSubTab] = useState<InsightsSubTab>('insights')
  const [showChat, setShowChat] = useState(false)
  const [showGuide, setShowGuide] = useState(false)
  const [stats, setStats] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [userId, setUserId] = useState<string | null>(null)
  const [userEmail, setUserEmail] = useState<string | null>(null)
  const [integrationsOpen, setIntegrationsOpen] = useState(false)
  const [dailyPulseAdminOpen, setDailyPulseAdminOpen] = useState(false)
  const [llmProfilesOpen, setLlmProfilesOpen] = useState(false)
  const [orgPanelOpen, setOrgPanelOpen] = useState(false)
  const [billingOpen, setBillingOpen] = useState(false)
  const [evolutionOpen, setEvolutionOpen] = useState(false)
  const [holdingOpen, setHoldingOpen] = useState(false)
  // Режим мозга: компания (org) или личный. Индикатор в шапке — чтобы человек
  // видел, КУДА пишет. Членство определяется один раз (инвайт), тут только читаем.
  const [myOrg, setMyOrg] = useState<{ id: string; name?: string } | null>(null)
  const [dataBusOpen, setDataBusOpen] = useState(false)
  
  // Agent mode
  const [agentMode, setAgentModeInternal] = useState<AgentMode>('brain')
  
  const setAgentMode = (mode: AgentMode) => {
    console.log('[Page] Setting agentMode to:', mode)
    setAgentModeInternal(mode)
    // Клик по кнопке агента показывает чат-панель
    setShowChat(true)
  }
  
  // Клик по вкладке скрывает чат и показывает содержимое вкладки
  const handleTabClick = (tab: Tab) => {
    setActiveTab(tab)
    setShowChat(false)
  }

  // Гид: клик по источнику-разделу → перейти в соответствующую вкладку/панель.
  // slug = "section/subsection"; маппим section → верхнюю вкладку.
  const handleGuideOpenSection = (slug: string) => {
    const section = (slug || '').split('/')[0]
    const toTab: Record<string, Tab> = {
      'getting-started': 'home', knowledge: 'knowledge', sync: 'sync',
      automations: 'automations', sima: 'sima', board: 'board', concepts: 'home',
    }
    if (section === 'settings') { setIntegrationsOpen(true); setShowGuide(false); return }
    if (section === 'chats') { setShowGuide(false); setShowChat(true); return }
    const tab = toTab[section]
    if (tab) { handleTabClick(tab); setShowGuide(false) }
  }
  
  // Chat sessions state
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const [currentMessages, setCurrentMessages] = useState<any[]>([])
  
  // Filters state
  const [filters, setFilters] = useState<FilterState>({
    meetings: [],
    projects: [],
    folders: [],
    documents: [],
  })
  const [availableFilters, setAvailableFilters] = useState<AvailableFilters>({
    meetings: [],
    projects: [],
    folders: [],
    documents: [],
  })
  const [documentFocusRequest, setDocumentFocusRequest] = useState<DocumentFocusRequest | null>(null)
  const [snapshotFocusRequest, setSnapshotFocusRequest] = useState<SnapshotFocusRequest | null>(null)
  const [graphFocusRequest, setGraphFocusRequest] = useState<GraphFocusRequest | null>(null)
  const [graphTraceRequest, setGraphTraceRequest] = useState<GraphTraceRequest | null>(null)
  // wow-screen: при клике «Изучить» в hero card передаём insightId.
  // InsightsPanel (inline функция в этом файле, :949) должен это прочесть
  // и проскроллить + подсветить нужную карточку. Сейчас передадим как prop
  // и оставим TODO в InsightsPanel — расширение InsightsPanel со scroll-to
  // делать отдельным коммитом (это просто requestId, реакция UI после).
  const [insightFocusRequest, setInsightFocusRequest] = useState<
    { insightId: string; requestId: number } | null
  >(null)

  // Load userId on mount. ЕДИНЫЙ источник правды — uid из ТОКЕНА (JWT sub):
  // отдельный ключ tessent_user_id протухал при смене аккаунта, и запросы
  // уходили с чужим id при валидном токене → «данные из другого аккаунта».
  // При расхождении токен побеждает и localStorage самолечится.
  useEffect(() => {
    const storedUserId = localStorage.getItem('tessent_user_id')
    const storedUser = localStorage.getItem('tessent_user')
    let tokenUid: string | null = null
    try {
      const { getUserIdFromToken, getAccessToken, ensureFreshToken } = require('@/lib/authFetch')
      // Истёкший токен освежаем СРАЗУ на маунте: иначе до первого authFetch
      // страница живёт с протухшей сессией, запросы уходят «безтокенными» и
      // легаси-путь бэкенда может отдать данные по стейлному user_id.
      // Провал refresh → ensureFreshToken сам чистит сессию и уводит на /login.
      if (getAccessToken()) { void ensureFreshToken() }
      tokenUid = getUserIdFromToken()
    } catch {}
    const effectiveId = tokenUid || storedUserId
    if (tokenUid && storedUserId && tokenUid !== storedUserId) {
      console.warn('🔑 userId из localStorage не совпадает с токеном — самолечение:',
        storedUserId, '→', tokenUid)
      localStorage.setItem('tessent_user_id', tokenUid)
    }
    console.log('🔑 Loaded userId:', effectiveId, tokenUid ? '(из токена)' : '(из localStorage)')
    if (effectiveId) {
      setUserId(effectiveId)
      if (storedUser) {
        try {
          const user = JSON.parse(storedUser)
          setUserEmail(user.email || null)
        } catch {}
      }
    } else {
      // Раньше здесь подставлялся хардкод тестового UUID: он уходил в
      // ?user_id= вместе с валидным токеном реального юзера → бэкенд отвечал
      // «запрошен чужой user_id — отказ» (спам в логе + пустые списки).
      // Теперь без user_id оставляем null: все fetch'и за гейтом `if (userId)`.
      console.warn('⚠️ No userId in localStorage.')
    }
  }, [router])

  const handleLogout = useCallback(() => {
    localStorage.removeItem('tessent_access_token')
    localStorage.removeItem('tessent_refresh_token')
    localStorage.removeItem('tessent_user')
    localStorage.removeItem('tessent_user_id')
    router.push('/login')
  }, [router])

  const fetchStats = async () => {
    try {
      const res = await fetch('/api/v1/health')
      if (res.ok) {
        const data = await res.json()
        setStats(data)
      }
    } catch (e) {
      console.error('Failed to fetch stats:', e)
    } finally {
      setLoading(false)
    }
  }

  const [chatFolders, setChatFolders] = useState<string[]>([])

  const fetchChatFolders = useCallback(async (uid?: string) => {
    const userIdParam = uid || userId
    if (!userIdParam) return
    try {
      const res = await authFetch(`/api/v1/sessions/folders?user_id=${userIdParam}`)
      if (res.ok) setChatFolders((await res.json()).folders || [])
    } catch (e) {
      console.error('Failed to fetch chat folders:', e)
    }
  }, [userId])

  const createChatFolder = useCallback(async (name: string) => {
    if (!userId || !name.trim()) return
    try {
      const res = await authFetch(`/api/v1/sessions/folders?user_id=${userId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim() }),
      })
      if (res.ok) fetchChatFolders()
    } catch (e) {
      console.error('Failed to create chat folder:', e)
    }
  }, [userId, fetchChatFolders])

  const deleteChatFolder = useCallback(async (name: string) => {
    if (!userId) return
    try {
      const res = await authFetch(`/api/v1/sessions/folders/delete?user_id=${userId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      if (res.ok) {
        fetchChatFolders()
        fetchSessions(userId) // у чатов снялась метка папки
      }
    } catch (e) {
      console.error('Failed to delete chat folder:', e)
    }
  }, [userId, fetchChatFolders])

  const fetchSessions = useCallback(async (uid?: string) => {
    const userIdParam = uid || userId
    if (!userIdParam) return
    
    try {
      const res = await authFetch(`/api/v1/sessions/?user_id=${userIdParam}`)
      if (res.ok) {
        const data = await res.json()
        setSessions(data)
      }
    } catch (e) {
      console.error('Failed to fetch sessions:', e)
    }
  }, [userId])

  useEffect(() => {
    fetchStats()
  }, [])

  // Fetch filters and sessions when userId is available
  useEffect(() => {
    if (userId) {
      fetchAvailableFilters(userId)
      fetchSessions(userId)
      fetchChatFolders(userId)
    }
  }, [userId, fetchSessions])

  // Режим мозга (компания vs личный) — один запрос, идентичность из Bearer.
  useEffect(() => {
    if (!userId) return
    let alive = true
    // /orgs/my-org трактовал «my-org» как литеральный org_id → 404 со
    // стектрейсом в логе; верный эндпоинт /my-org (форма та же, solo → org:null)
    authFetch('/api/v1/my-org')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive) setMyOrg(d?.org?.id ? { id: d.org.id, name: d.org.name } : null) })
      .catch(() => {})
    return () => { alive = false }
  }, [userId])

  const fetchAvailableFilters = async (uid: string) => {
    try {
      // ВАЖНО: все четыре запроса — с Bearer (authFetch). При включённом
      // strict-auth голый fetch получал отказ → documents: [] → в сайдбаре
      // «нет документов», и выбрать их в чат было невозможно.
      // Fetch projects
      const projectsRes = await authFetch(`/api/v1/meetflow/projects?user_id=${uid}`)
      const projectsData = projectsRes.ok ? await projectsRes.json() : { projects: [] }

      // Fetch folders
      const foldersRes = await authFetch(`/api/v1/meetflow/folders?user_id=${uid}`)
      const foldersData = foldersRes.ok ? await foldersRes.json() : { folders: [] }

      // Fetch meetings - увеличиваем лимит до 1000, чтобы фильтрация на клиенте работала корректно
      const meetingsRes = await authFetch(`/api/v1/meetflow/meetings?user_id=${uid}&limit=1000`)
      const meetingsData = meetingsRes.ok ? await meetingsRes.json() : { meetings: [] }

      // Fetch documents
      const documentsRes = await authFetch(`/api/v1/documents/?user_id=${uid}`)
      const documentsData = documentsRes.ok ? await documentsRes.json() : { documents: [] }
      
      setAvailableFilters({
        meetings: (meetingsData.meetings || []).map((m: any) => ({ 
          id: m.id, 
          title: m.title || 'Untitled',
          project_id: m.project_id,
          folder_id: m.folder_id
        })),
        projects: (projectsData.projects || []).map((p: any) => ({ id: p.project_id || p.id, name: p.name })),
        folders: (foldersData.folders || []).map((f: any) => ({ id: f.id, name: f.name, project_id: f.project_id })),
        documents: (documentsData.documents || []).map((d: any) => ({
          id: d.id,
          title: d.title || 'Untitled',
          doc_type: d.doc_type,
          sync_status: d.sync_status,
          folder: (d.metadata?.folder || '') // ярлык-папка документа → группировка в контексте чата
        })),
      })
      
      console.log('📋 Filters loaded:', {
        meetings: meetingsData.meetings?.length || 0,
        projects: projectsData.projects?.length || 0,
        folders: foldersData.folders?.length || 0,
        documents: documentsData.documents?.length || 0
      })
    } catch (e) {
      console.error('Failed to fetch filters:', e)
    }
  }

  const createSession = useCallback(async () => {
    try {
      if (!userId) {
        console.warn('⚠️ Cannot create session: userId is missing')
        return null
      }
      const res = await authFetch('/api/v1/sessions/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: t('main_page.new_chat_title'), messages: [], filters, user_id: userId, agent_mode: agentMode }),
      })
      if (res.ok) {
        const newSession = await res.json()
        setSessions(prev => [newSession, ...prev])
        setCurrentSessionId(newSession.id)
        setCurrentMessages([])
        return newSession.id as string
      }
    } catch (e) {
      console.error('Failed to create session:', e)
    }
    return null
  }, [filters, userId, agentMode])

  const handleNewChat = useCallback(async () => {
    await createSession()
  }, [createSession])

  const handleSelectSession = useCallback(async (id: string) => {
    setCurrentSessionId(id)
    try {
      if (!userId) return
      const res = await authFetch(`/api/v1/sessions/${id}?user_id=${userId}`)
      if (res.ok) {
        const session = await res.json()
        setCurrentMessages(session.messages || [])
        if (session.filters) {
          // Ensure all fields are present to avoid runtime errors with old sessions
          setFilters({
            meetings: session.filters.meetings || [],
            projects: session.filters.projects || [],
            folders: session.filters.folders || [],
            documents: session.filters.documents || [],
          })
        }
      }
    } catch (e) {
      console.error('Failed to load session:', e)
    }
  }, [userId])

  const handleDeleteSession = useCallback(async (id: string) => {
    try {
      if (!userId) return
      const res = await authFetch(`/api/v1/sessions/${id}?user_id=${userId}`, { method: 'DELETE' })
      if (res.ok) {
        setSessions(prev => prev.filter(s => s.id !== id))
        if (currentSessionId === id) {
          setCurrentSessionId(null)
          setCurrentMessages([])
        }
      }
    } catch (e) {
      console.error('Failed to delete session:', e)
    }
  }, [currentSessionId, userId])

  const handleSetSessionFolder = useCallback(async (id: string, folder: string) => {
    if (!userId) return
    // оптимистично: папка видна сразу, откат при ошибке сервера
    const prev = sessions
    setSessions(list => list.map(s => (s.id === id ? { ...s, folder } : s)))
    try {
      const res = await authFetch(`/api/v1/sessions/${id}?user_id=${userId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder }),
      })
      if (!res.ok) setSessions(prev)
      else fetchChatFolders()
    } catch (e) {
      console.error('Failed to set session folder:', e)
      setSessions(prev)
    }
  }, [userId, sessions, fetchChatFolders])

  const handleMessagesUpdate = useCallback(async (messages: any[], sessionIdOverride?: string | null) => {
    // Обновляем видимые сообщения ТОЛЬКО если они принадлежат открытой
    // сессии: долгое исследование завершается, когда пользователь уже в
    // другой переписке — раньше его сообщения затирали текущий чат, а при
    // возврате казалось, что «отчёт не сохранился».
    if (!sessionIdOverride || sessionIdOverride === currentSessionId) {
      setCurrentMessages(messages)
    }

    const sid = sessionIdOverride || currentSessionId
    // Save to session if we have one
    if (sid) {
      try {
        if (!userId) return
        await authFetch(`/api/v1/sessions/${sid}?user_id=${userId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages, filters }),
        })
        // Refresh sessions list to update preview
        fetchSessions(userId || undefined)
      } catch (e) {
        console.error('Failed to save messages:', e)
      }
    }
  }, [currentSessionId, filters, userId, fetchSessions])

  const handleSourceNavigation = useCallback((target: SourceNavigationTarget) => {
    if (target.kind === 'meeting' && target.meetingId) {
      setShowChat(true)
      setGraphFocusRequest(null)
      setGraphTraceRequest(null)
      setFilters({
        meetings: [target.meetingId],
        projects: [],
        folders: [],
        documents: [],
      })
      return
    }

    if (target.kind === 'document' && target.documentId) {
      setActiveTab('knowledge')
      setKnowledgeSubTab('documents')
      setShowChat(false)
      setGraphFocusRequest(null)
      setGraphTraceRequest(null)
      setFilters({
        meetings: [],
        projects: [],
        folders: [],
        documents: [target.documentId],
      })
      setDocumentFocusRequest({
        documentId: target.documentId,
        requestId: Date.now(),
      })
      return
    }

    if (target.kind === 'snapshot' && target.snapshotTab) {
      setActiveTab('knowledge')
      setKnowledgeSubTab('snapshots')
      setShowChat(false)
      setGraphFocusRequest(null)
      setGraphTraceRequest(null)
      setSnapshotFocusRequest({
        tab: target.snapshotTab,
        entityId: target.entityId,
        requestId: Date.now(),
      })
      return
    }

    if (target.kind === 'graph' && target.entityId) {
      setActiveTab('knowledge')
      setKnowledgeSubTab('graph')
      setShowChat(false)
      setGraphTraceRequest(null)
      setGraphFocusRequest({
        entityId: target.entityId,
        label: target.label,
        requestId: Date.now(),
      })
      return
    }

    if (target.kind === 'graph_trace' && target.traceTargets && target.traceTargets.length > 0) {
      setActiveTab('knowledge')
      setKnowledgeSubTab('graph')
      setShowChat(false)
      setGraphFocusRequest(null)
      setGraphTraceRequest({
        targets: target.traceTargets,
        edges: target.traceEdges,
        segments: target.traceSegments,
        missingLinks: target.traceMissingLinks,
        reasoningSteps: target.reasoningSteps,
        title: target.title,
        requestId: Date.now(),
      })
    }
  }, [])

  const tabs = [
    // wow-screen: ставится первым специально (issue: editorial briefing).
    { id: 'home' as Tab, label: t('main_page.tab_home'), icon: Sparkles },
    { id: 'knowledge' as Tab, label: t('main_page.tab_knowledge'), icon: BookOpen },
    { id: 'sync' as Tab, label: t('main_page.tab_sync'), icon: RefreshCw },
    { id: 'automations' as Tab, label: t('main_page.tab_automations'), icon: Clock },
    { id: 'board' as Tab, label: t('main_page.tab_board'), icon: Layout },
    { id: 'sima' as Tab, label: 'SIMA', icon: Lightbulb },
  ]

  const knowledgeSubTabs = [
    { id: 'graph' as KnowledgeSubTab, label: t('main_page.subtab_graph') },
    // Единый раздел «Документы» (#3): по встрече + шаблоны/регламенты +
    // загруженные + анализ — внутренними вкладками (DocumentsHubPanel).
    { id: 'documents' as KnowledgeSubTab, label: `📄 ${t('main_page.subtab_documents')}` },
    { id: 'datasets' as KnowledgeSubTab, label: t('main_page.subtab_datasets') },
    { id: 'object360' as KnowledgeSubTab, label: t('main_page.subtab_object360') },
    { id: 'customers' as KnowledgeSubTab, label: t('main_page.subtab_customers') },
    { id: 'insights' as KnowledgeSubTab, label: t('main_page.subtab_insights') },
    { id: 'snapshots' as KnowledgeSubTab, label: t('main_page.subtab_snapshots') },
    { id: 'lineage' as KnowledgeSubTab, label: 'Lineage' },
    // issue #112 T-10: review queue для memory correctness
    { id: 'review' as KnowledgeSubTab, label: t('main_page.subtab_memory_review') },
  ]

  const syncSubTabs = [
    { id: 'sync' as SyncSubTab, label: t('main_page.subtab_sync') },
    { id: 'editor' as SyncSubTab, label: t('main_page.subtab_editor') },
  ]

  return (
    <div className="h-screen flex flex-col neural-grid relative">
      {/* Живой нейро-фон главного экрана (wow): приглушён, за контентом,
          указатели не перехватывает; reduced-motion → статичный кадр */}
      <NeuralCanvas className="fixed inset-0 w-full h-full z-0 pointer-events-none opacity-40" />
      {/* Header */}
      <header className="border-b border-brain-600/20 bg-brain-950/80 backdrop-blur-sm flex-shrink-0 z-50">
        <div className="px-4 py-3">
          <div className="flex items-center justify-between">
            {/* Left: Menu + Logo */}
            <div className="flex items-center gap-3">
              <button
                onClick={() => setSidebarOpen(!sidebarOpen)}
                className="p-2 hover:bg-brain-800 rounded-lg transition-colors lg:hidden"
              >
                {sidebarOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
              </button>
              <div className="flex items-center gap-3">
                <div className="relative">
                  <img src="/bot-avatar.svg" alt="Brain" className="w-8 h-8 brain-pulse" />
                  <div className="absolute inset-0 bg-brain-500/20 blur-xl rounded-full" />
                </div>
                <div>
                  <h1 className="text-lg font-bold text-gradient">Tessent Brain</h1>
                  <p className="text-xs text-brain-400 hidden sm:block">{t('metadata.description')}</p>
                </div>
              </div>
            </div>

            {/* Center: Tabs */}
            <div className="hidden md:flex gap-1">
              {tabs.map((tab) => {
                const Icon = tab.icon
                return (
                  <button
                    key={tab.id}
                    onClick={() => handleTabClick(tab.id)}
                    className={`flex items-center gap-2 px-3 py-1.5 rounded-lg transition-all text-sm ${
                      activeTab === tab.id && !showChat
                        ? 'bg-brain-600 text-white'
                        : 'text-brain-400 hover:bg-brain-800/50 hover:text-brain-300'
                    }`}
                  >
                    <Icon className="w-4 h-4" />
                    <span className="hidden lg:inline">{tab.label}</span>
                  </button>
                )
              })}
            </div>

            {/* Right: Agent Mode Selector + Status */}
            <div className="flex items-center gap-3">
              {/* Agent Mode Selector */}
              <div className="hidden sm:flex items-center gap-1 bg-brain-900/50 rounded-lg p-1">
                {AGENT_MODES.map((mode) => (
                  <button
                    key={mode.id}
                    onClick={() => setAgentMode(mode.id)}
                    className={`flex items-center gap-1.5 px-2 py-1 rounded-md transition-all text-xs font-medium ${
                      agentMode === mode.id
                        ? mode.color === 'purple' ? 'bg-purple-600 text-white' :
                          mode.color === 'orange' ? 'bg-orange-600 text-white' :
                          mode.color === 'green' ? 'bg-green-600 text-white' :
                          'bg-blue-600 text-white'
                        : 'text-brain-400 hover:text-brain-200 hover:bg-brain-800/50'
                    }`}
                    title={mode.description}
                  >
                    {mode.icon}
                    <span className="hidden lg:inline">{mode.name}</span>
                  </button>
                ))}
              </div>
              
              {/* Индикатор режима мозга: компания vs личный — видно, куда пишешь.
                  Клик → панель организации (управление/приглашения). */}
              <button
                onClick={() => setOrgPanelOpen(true)}
                title={myOrg ? t('main_page.org_mode_title') : t('main_page.personal_mode_title')}
                className={`hidden sm:inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs border transition-colors max-w-[220px] ${
                  myOrg
                    ? 'bg-brain-600/20 border-brain-500/40 text-brain-200 hover:bg-brain-600/30'
                    : 'border-brain-700 text-brain-400 hover:bg-brain-800/50'
                }`}
              >
                {myOrg
                  ? <><span>🏢</span><span className="truncate">{t('main_page.org_brain_label')}{myOrg.name ? ` ${myOrg.name}` : ''}</span></>
                  : <><span>👤</span><span>{t('main_page.personal_mode_label')}</span></>}
              </button>

              {/* W44: Language switcher — компактный (иконка + флаг) */}
              <LanguageSwitcher />

              {/* Единое меню аккаунта: расход токенов, утилитарные иконки,
                  почта/статус и выход — свёрнуты в один список */}
              <AccountMenu
                userEmail={userEmail}
                online={!!stats}
                onOpenDataBus={() => setDataBusOpen(true)}
                onOpenIntegrations={() => setIntegrationsOpen(true)}
                onOpenDailyPulse={() => setDailyPulseAdminOpen(true)}
                onOpenLlmProfiles={() => setLlmProfilesOpen(true)}
                onOpenOrg={() => setOrgPanelOpen(true)}
                onOpenBilling={() => setBillingOpen(true)}
                onOpenHolding={() => setHoldingOpen(true)}
                onLogout={handleLogout}
              />
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden relative z-10">
        {/* Chat Sidebar — показывается когда showChat === true */}
        {showChat && (
          <div className={`${sidebarOpen ? 'block' : 'hidden'} flex-shrink-0`}>
            <ChatSidebar
              sessions={sessions}
              currentSessionId={currentSessionId}
              onSelectSession={handleSelectSession}
              onNewChat={handleNewChat}
              onDeleteSession={handleDeleteSession}
              onSetSessionFolder={handleSetSessionFolder}
              chatFolders={chatFolders}
              onCreateFolder={createChatFolder}
              onDeleteFolder={deleteChatFolder}
              filters={filters}
              onFiltersChange={setFilters}
              availableFilters={availableFilters}
            />
          </div>
        )}

        {/* Main Panel */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Sub-tabs for Knowledge, Sync and Automations (скрываем когда показан чат) */}
          {!showChat && (activeTab === 'knowledge' || activeTab === 'sync' || activeTab === 'automations') && (
            <div className="flex gap-1 px-4 py-2 border-b border-brain-700/30 bg-brain-950/50">
              {activeTab === 'knowledge' && knowledgeSubTabs.map((sub) => (
                <button
                  key={sub.id}
                  onClick={() => setKnowledgeSubTab(sub.id)}
                  className={`px-3 py-1 rounded-md transition-all text-sm ${
                    knowledgeSubTab === sub.id
                      ? 'bg-brain-700 text-white'
                      : 'text-brain-400 hover:bg-brain-800/50 hover:text-brain-300'
                  }`}
                >
                  {sub.label}
                </button>
              ))}
              {activeTab === 'sync' && syncSubTabs.map((sub) => (
                <button
                  key={sub.id}
                  onClick={() => setSyncSubTab(sub.id)}
                  className={`px-3 py-1 rounded-md transition-all text-sm ${
                    syncSubTab === sub.id
                      ? 'bg-brain-700 text-white'
                      : 'text-brain-400 hover:bg-brain-800/50 hover:text-brain-300'
                  }`}
                >
                  {sub.label}
                </button>
              ))}
              {activeTab === 'automations' && [
                { id: 'classic' as AutomationsSubTab, label: t('main_page.automations_classic') },
                { id: 'agent' as AutomationsSubTab, label: t('main_page.automations_agent') },
                { id: 'queue' as AutomationsSubTab, label: '⚡ Vibe Tasking' },
                { id: 'office' as AutomationsSubTab, label: '🤝 Исполнители' },
                { id: 'skills' as AutomationsSubTab, label: t('main_page.automations_skills') },
    { id: 'catalog' as AutomationsSubTab, label: t('main_page.subtab_catalog') },
              ].map((sub) => (
                <button
                  key={sub.id}
                  onClick={() => setAutomationsSubTab(sub.id)}
                  className={`px-3 py-1 rounded-md transition-all text-sm ${
                    automationsSubTab === sub.id
                      ? 'bg-brain-700 text-white'
                      : 'text-brain-400 hover:bg-brain-800/50 hover:text-brain-300'
                  }`}
                >
                  {sub.label}
                </button>
              ))}
            </div>
          )}

          {/* Chat header — показывается когда чат открыт */}
          {showChat && (
            <div className="flex items-center justify-between px-4 py-2 border-b border-brain-700/30 bg-brain-950/50">
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setSidebarOpen(!sidebarOpen)}
                  className="p-1.5 hover:bg-brain-800 rounded-lg transition-colors"
                >
                  <Menu className="w-4 h-4 text-brain-400" />
                </button>
                <span className="text-sm font-medium text-brain-300">
                  {AGENT_MODES.find(m => m.id === agentMode)?.name || t('main_page.chat_default_name')}
                </span>
              </div>
              <button
                onClick={() => setShowChat(false)}
                className="p-1.5 hover:bg-brain-800 rounded-lg transition-colors"
                title={t('main_page.close_chat_title')}
              >
                <X className="w-4 h-4 text-brain-400" />
              </button>
            </div>
          )}

          {/* Mobile Tabs (скрываем когда показан чат) */}
          {!showChat && (
            <div className="md:hidden flex gap-1 p-2 border-b border-brain-700/30 overflow-x-auto">
              {tabs.map((tab) => {
                const Icon = tab.icon
                return (
                  <button
                    key={tab.id}
                    onClick={() => handleTabClick(tab.id)}
                    className={`flex items-center gap-2 px-3 py-1.5 rounded-lg transition-all text-sm whitespace-nowrap ${
                      activeTab === tab.id
                        ? 'bg-brain-600 text-white'
                        : 'text-brain-400 hover:bg-brain-800/50'
                    }`}
                  >
                    <Icon className="w-4 h-4" />
                    {tab.label}
                  </button>
                )
              })}
            </div>
          )}

          {/* Content */}
          <div className="flex-1 min-h-0 overflow-hidden">
            {/* === ЧАТ (полноценная панель) === */}
            {showChat && (
              <ChatPanel
                sessionId={currentSessionId}
                initialMessages={currentMessages}
                onMessagesUpdate={handleMessagesUpdate}
                filters={filters}
                ensureSession={createSession}
                agentMode={agentMode}
                userId={userId}
                onNavigateToSource={handleSourceNavigation}
              />
            )}

            {/* === HOME (wow-screen, editorial briefing) ===
                Первый экран после логина. Редакторская подача:
                «вот что система для тебя нашла», вместо плотного графа.
                Все кнопки внутри отправляют в существующие вкладки через
                onNavigate — никакого дублирования логики. */}
            {!showChat && activeTab === 'home' && (
              <HomeTab
                userId={userId}
                onNavigate={(target) => {
                  // Mark — это чат-agent-mode, а не вкладка: открываем чат Mark.
                  if (target.tab === 'mark') { setAgentMode('mark'); return }
                  setActiveTab(target.tab as Tab)
                  if (target.subTab) {
                    setKnowledgeSubTab(target.subTab as KnowledgeSubTab)
                  }
                  // wow-screen drill-down: при клике «Изучить» в hero
                  // card передаём insightId, при «Посмотреть в графе» —
                  // entityIds. page.tsx прокидывает эти focus-requests в
                  // существующие state-поля; InsightsPanel и
                  // GraphVisualization уже умеют scroll-to + highlight.
                  if (target.insightId) {
                    setInsightFocusRequest({
                      insightId: target.insightId,
                      requestId: Date.now(),
                    })
                  }
                  if (target.entityIds && target.entityIds.length > 0) {
                    setGraphTraceRequest({
                      targets: target.entityIds.map((id) => ({
                        entityId: id,
                      })),
                      requestId: Date.now(),
                    })
                  }
                }}
                onOpenGuide={() => setShowGuide(true)}
              />
            )}

            {/* === ЗНАНИЯ (Knowledge) === */}
            {!showChat && activeTab === 'knowledge' && knowledgeSubTab === 'graph' && (
              <div className="h-full p-4 overflow-auto">
                <GraphVisualization
                  userId={userId}
                  focusRequest={graphFocusRequest}
                  traceRequest={graphTraceRequest}
                  onOpenEntity={(_type, _id, label) => {
                    // клик «Открыть карточку» в 3D-графе → Объект 360 по имени
                    setObject360Name(label)
                    setKnowledgeSubTab('object360')
                  }}
                />
              </div>
            )}
            {!showChat && activeTab === 'knowledge' && knowledgeSubTab === 'object360' && (
              <div className="h-full overflow-auto">
                <Object360Panel userId={userId || undefined} initialName={object360Name} />
              </div>
            )}
            {!showChat && activeTab === 'knowledge' && knowledgeSubTab === 'customers' && (
              <CustomersPanel userId={userId || undefined} />
            )}
            {!showChat && activeTab === 'knowledge' && knowledgeSubTab === 'datasets' && (
              <DatasetsPanel userId={userId || undefined} />
            )}
            {!showChat && activeTab === 'knowledge' && knowledgeSubTab === 'documents' && (
              <DocumentsHubPanel
                userId={userId || undefined}
                onDocumentsUpdate={() => userId && fetchAvailableFilters(userId)}
                documentFocusRequest={documentFocusRequest}
              />
            )}
            {!showChat && activeTab === 'knowledge' && knowledgeSubTab === 'insights' && (
              <div className="h-full p-4 overflow-auto">
                {/* Подвкладки инсайтов: Инсайты | Отчёты (по аналогии с automations classic|agent|skills) */}
                <div className="flex gap-1 mb-4">
                  {[
                    { id: 'insights' as InsightsSubTab, label: t('main_page.insights_subtab_insights') },
                    { id: 'reports' as InsightsSubTab, label: t('main_page.insights_subtab_reports') },
                    { id: 'team360' as InsightsSubTab, label: t('main_page.insights_subtab_team360') },
                    { id: 'board' as InsightsSubTab, label: t('main_page.insights_subtab_board') },
                    { id: 'pulse' as InsightsSubTab, label: t('main_page.insights_subtab_pulse') },
                    { id: 'clients' as InsightsSubTab, label: t('main_page.insights_subtab_clients') },
                  ].map((sub) => (
                    <button
                      key={sub.id}
                      onClick={() => setInsightsSubTab(sub.id)}
                      className={`px-3 py-1 rounded-md transition-all text-sm ${
                        insightsSubTab === sub.id
                          ? 'bg-brain-700 text-white'
                          : 'text-brain-400 hover:bg-brain-800/50 hover:text-brain-300'
                      }`}
                    >
                      {sub.label}
                    </button>
                  ))}
                </div>
                {insightsSubTab === 'insights' ? (
                  <>
                    <StatsPanel userId={userId} />
                    <InsightsPanel userId={userId} />
                    <CommunityLensPanel userId={userId} />
                    <AnomaliesPanel userId={userId} />
                  </>
                ) : insightsSubTab === 'team360' ? (
                  <Team360Panel userId={userId} />
                ) : insightsSubTab === 'board' ? (
                  <VirtualBoardPanel userId={userId} />
                ) : insightsSubTab === 'pulse' ? (
                  <PulsePanel userId={userId} />
                ) : insightsSubTab === 'clients' ? (
                  <ClientSimPanel userId={userId} />
                ) : (
                  <ReportsPanel userId={userId} />
                )}
              </div>
            )}

            {!showChat && activeTab === 'knowledge' && knowledgeSubTab === 'snapshots' && (
              <div className="h-full p-4 overflow-auto">
                <div className="mb-3 flex justify-end">
                  <button
                    onClick={() => setEvolutionOpen(true)}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-purple-600/80 hover:bg-purple-600 text-white text-sm"
                  >
                    <GitBranch className="w-4 h-4" /> {t('main_page.evolution_button')}
                  </button>
                </div>
                <SnapshotsPanel userId={userId || ''} focusRequest={snapshotFocusRequest} />
              </div>
            )}

            {!showChat && activeTab === 'knowledge' && knowledgeSubTab === 'lineage' && (
              <div className="h-full p-4 overflow-auto">
                <LineageTracePanel
                  userId={userId || ''}
                  onShowOnGraph={(req) => {
                    setKnowledgeSubTab('graph')
                    setShowChat(false)
                    setGraphFocusRequest(null)
                    setGraphTraceRequest({
                      targets: req.targets,
                      edges: req.edges,
                      reasoningSteps: req.reasoningSteps,
                      title: req.title,
                      requestId: req.requestId,
                    })
                  }}
                />
              </div>
            )}

            {/* === ПРОВЕРКА ПАМЯТИ (Memory Review, issue #112 T-10) === */}
            {!showChat && activeTab === 'knowledge' && knowledgeSubTab === 'review' && (
              <MemoryReviewPanel userId={userId} />
            )}

            {/* === СИНХРОНИЗАЦИЯ (Sync) === */}
            {!showChat && activeTab === 'sync' && syncSubTab === 'sync' && (
              <div className="h-full p-4 overflow-auto">
                <KnowledgeSyncPanel userId={userId || undefined} />
                <ChatSourcesCard userId={userId} />
              </div>
            )}
            {!showChat && activeTab === 'sync' && syncSubTab === 'editor' && (
              <div className="h-full">
                <KnowledgeEditorPanel userId={userId} />
              </div>
            )}

            {/* === АВТОМАТИЗАЦИИ === */}
            {!showChat && activeTab === 'automations' && automationsSubTab === 'classic' && (
              <AutomationsPanel userId={userId || undefined} />
            )}
            {!showChat && activeTab === 'automations' && automationsSubTab === 'agent' && (
              /* обёртка со скроллом: панель занимает высоту экрана, карточка
                 навыков агента доступна прокруткой ниже, layout панели не трогаем */
              <div className="h-full overflow-y-auto">
                <AgentAutomationsPanel userId={userId || undefined} />
                <AgentSkillsCard userId={userId} />
              </div>
            )}
            {!showChat && activeTab === 'automations' && automationsSubTab === 'queue' && (
              /* обёртка со скроллом: родитель overflow-hidden, без неё очередь
                 с длинными ТЗ/документами не пролистывалась вниз */
              <div className="h-full overflow-y-auto">
                <HandoffQueuePanel userId={userId || undefined} />
              </div>
            )}
            {!showChat && activeTab === 'automations' && automationsSubTab === 'office' && (
              /* зона офисных задач: задача словами → исполнитель → приёмка →
                 финал человека. Скролл как у очереди — прогоны длинные */
              <div className="h-full overflow-y-auto">
                <OfficeTasksPanel userId={userId || undefined} />
              </div>
            )}
            {!showChat && activeTab === 'automations' && automationsSubTab === 'catalog' && (
              <AssistantCatalogPanel userId={userId || undefined} />
            )}
            {!showChat && activeTab === 'automations' && automationsSubTab === 'skills' && (
              <SkillsPanel userId={userId || undefined} />
            )}

            {/* === ДОСКА === */}
            {!showChat && activeTab === 'board' && (
              <div className="h-full overflow-hidden">
                <BoardPanel />
              </div>
            )}

            {/* === SIMA === */}
            {!showChat && activeTab === 'sima' && (
              <SimaApp userId={userId} />
            )}
          </div>
        </div>
      </div>
      
      {/* Integrations Panel Modal */}
      <IntegrationsPanel
        userId={userId}
        isOpen={integrationsOpen}
        onClose={() => setIntegrationsOpen(false)}
      />

      {/* Daily Pulse Admin Panel Modal */}
      <DailyPulseAdminPanel
        userId={userId}
        isOpen={dailyPulseAdminOpen}
        onClose={() => setDailyPulseAdminOpen(false)}
      />

      {/* LLM Profiles Panel Modal */}
      <LLMProfilesPanel
        userId={userId}
        isOpen={llmProfilesOpen}
        onClose={() => setLlmProfilesOpen(false)}
      />

      {/* Компания (мульти-аккаунт): участники/инвайты/мозг целиком */}
      <BillingPanel
        isOpen={billingOpen}
        onClose={() => setBillingOpen(false)}
        userId={userId || undefined}
      />

      <EvolutionPanel
        isOpen={evolutionOpen}
        onClose={() => setEvolutionOpen(false)}
        userId={userId || undefined}
      />

      <HoldingConsolePanel
        isOpen={holdingOpen}
        onClose={() => setHoldingOpen(false)}
      />

      <OrgPanel
        userId={userId}
        isOpen={orgPanelOpen}
        onClose={() => setOrgPanelOpen(false)}
      />

      {/* Data Bus / Federation Panel Modal */}
      <DataBusPanel
        userId={userId}
        isOpen={dataBusOpen}
        onClose={() => setDataBusOpen(false)}
      />

      {/* Гид по Tessent: плавающая «?» + панель. Прячем кнопку, когда открыт
          сам гид или что-то поверх (чат/модалки), чтобы не мешалась. */}
      {!showGuide && !showChat && !integrationsOpen && !dataBusOpen
        && !dailyPulseAdminOpen && !llmProfilesOpen && !orgPanelOpen && (
        <button
          onClick={() => setShowGuide(true)}
          title={t('main_page.guide_button_title')}
          className="fixed bottom-6 right-6 z-40 w-12 h-12 rounded-full bg-purple-600 hover:bg-purple-500 text-white shadow-xl flex items-center justify-center transition-colors"
        >
          <HelpCircle className="w-6 h-6" />
        </button>
      )}
      {showGuide && (
        <GuideChatPanel
          userId={userId || undefined}
          currentTab={activeTab}
          onClose={() => setShowGuide(false)}
          onOpenSection={handleGuideOpenSection}
        />
      )}
    </div>
  )
}

// Insights Panel - загружает реальные инсайты из API
function InsightsPanel({ userId }: { userId: string | null }) {
  const tInsights = useTranslations('main_page')
  const [insights, setInsights] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reactingId, setReactingId] = useState<string | null>(null)
  const [usefulIds, setUsefulIds] = useState<string[]>([])

  const handleReact = async (insightId: string, reaction: 'useful' | 'declined') => {
    if (!userId || reactingId) return
    try {
      setReactingId(insightId)
      const res = await fetch(`/api/v1/meetflow/insights/${insightId}/react?user_id=${userId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reaction }),
      })
      if (res.ok) {
        if (reaction === 'declined') {
          // Убираем карточку из списка локально
          setInsights(prev => prev.filter(i => i.id !== insightId))
        } else {
          setUsefulIds(prev => (prev.includes(insightId) ? prev : [...prev, insightId]))
        }
      }
    } catch (e) {
      console.error('Error reacting to insight:', e)
    } finally {
      setReactingId(null)
    }
  }

  useEffect(() => {
    const fetchInsights = async () => {
      if (!userId) {
        setLoading(false)
        return
      }
      
      try {
        setLoading(true)
        setError(null)
        const res = await fetch(`/api/v1/meetflow/insights?user_id=${userId}`)
        if (res.ok) {
          const data = await res.json()
          setInsights(data.insights || [])
        } else {
          setError(tInsights('insights_load_failed'))
        }
      } catch (e) {
        console.error('Error fetching insights:', e)
        setError(tInsights('insights_load_error'))
      } finally {
        setLoading(false)
      }
    }
    
    fetchInsights()
  }, [userId])

  return (
    <div className="card mt-4">
      <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
        <Activity className="w-5 h-5 text-brain-400" />
        {tInsights('insights_title')}
      </h2>
      
      {loading ? (
        <div className="text-center py-8 text-brain-400">{tInsights('loading')}</div>
      ) : error ? (
        <div className="text-center py-8 text-red-400">
          {error}
        </div>
      ) : insights.length === 0 ? (
        <div className="text-center py-8 text-brain-400">
          {tInsights('insights_empty')}
        </div>
      ) : (
        <div className="space-y-4">
          {insights.map((insight) => (
            <div key={insight.id} className="card bg-brain-900/40">
              <div className="flex items-start gap-3">
                <div className={`badge ${
                  insight.priority === 'high' || insight.priority === 'critical' ? 'badge-error' :
                  insight.priority === 'medium' ? 'badge-warning' :
                  'badge-info'
                }`}>
                  {insight.type}
                </div>
                <div className="flex-1">
                  <h3 className="font-medium">{insight.title}</h3>
                  <p className="text-sm text-brain-400 mt-1">{insight.description}</p>
                  {insight.source && (
                    <div className="text-xs text-brain-500 mt-1">{insight.source}</div>
                  )}
                  {insight.entities && insight.entities.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {insight.entities.map((entity: any, idx: number) => (
                        <span key={idx} className="text-xs bg-brain-800 px-2 py-0.5 rounded">
                          {entity.name || entity.id}
                        </span>
                      ))}
                    </div>
                  )}
                  {insight.actions && insight.actions.length > 0 && (
                    <div className="mt-2 text-xs text-brain-500">
                      💡 {insight.actions[0]}
                    </div>
                  )}
                </div>
                <div className="flex flex-col items-end gap-2">
                  {insight.confidence && (
                    <div className="text-xs text-brain-500">
                      {Math.round(insight.confidence * 100)}%
                    </div>
                  )}
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => handleReact(insight.id, 'useful')}
                      disabled={reactingId === insight.id || usefulIds.includes(insight.id)}
                      className={`p-1 rounded-md text-sm transition-colors disabled:cursor-not-allowed ${
                        usefulIds.includes(insight.id)
                          ? 'bg-green-500/20'
                          : 'hover:bg-brain-800 opacity-60 hover:opacity-100'
                      }`}
                      title={tInsights('insight_react_useful')}
                    >
                      👍
                    </button>
                    <button
                      onClick={() => handleReact(insight.id, 'declined')}
                      disabled={reactingId === insight.id}
                      className="p-1 rounded-md text-sm transition-colors hover:bg-brain-800 opacity-60 hover:opacity-100 disabled:cursor-not-allowed"
                      title={tInsights('insight_react_declined')}
                    >
                      🚫
                    </button>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Anomalies Panel - загружает реальные аномалии из API
function AnomaliesPanel({ userId }: { userId: string | null }) {
  const tAnomalies = useTranslations('main_page')
  const [anomalies, setAnomalies] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const fetchAnomalies = async () => {
      if (!userId) {
        setLoading(false)
        return
      }
      
      try {
        setLoading(true)
        setError(null)
        const res = await fetch(`/api/v1/meetflow/anomalies?user_id=${userId}`)
        if (res.ok) {
          const data = await res.json()
          // Защита: backend возвращает {} (dict) при ошибке/пустом графе,
          // и `{} || []` → `{}` → anomalies.map крашит экран TypeError'ом.
          setAnomalies(Array.isArray(data.anomalies) ? data.anomalies : [])
        } else {
          setError(tAnomalies('anomalies_load_failed'))
        }
      } catch (e) {
        console.error('Error fetching anomalies:', e)
        setError(tAnomalies('anomalies_load_error'))
      } finally {
        setLoading(false)
      }
    }
    
    fetchAnomalies()
  }, [userId])

  return (
    <div className="card">
      <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
        <AlertTriangle className="w-5 h-5 text-yellow-500" />
        {tAnomalies('anomalies_title')}
        {anomalies.length > 0 && (
          <span className="text-sm font-normal text-brain-400">({anomalies.length})</span>
        )}
      </h2>

      {loading ? (
        <div className="text-center py-8 text-brain-400">{tAnomalies('loading')}</div>
      ) : error ? (
        <div className="text-center py-8 text-red-400">
          {error}
        </div>
      ) : anomalies.length === 0 ? (
        <div className="text-center py-8 text-green-400">
          {tAnomalies('anomalies_empty')}
        </div>
      ) : (
        <div className="space-y-4">
          {anomalies.map((anomaly) => (
            <div 
              key={anomaly.id} 
              className={`card bg-brain-900/40 border-l-4 ${
                anomaly.severity === 'critical' ? 'border-l-red-500/70' :
                anomaly.severity === 'high' ? 'border-l-yellow-500/50' :
                'border-l-blue-500/50'
              }`}
            >
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`badge ${
                      anomaly.severity === 'critical' ? 'badge-error' :
                      anomaly.severity === 'high' ? 'badge-warning' :
                      'badge-info'
                    }`}>
                      {anomaly.severity}
                    </span>
                    <span className="text-xs text-brain-400">{anomaly.type}</span>
                    {anomaly.entity_name && (
                      <span className="text-xs bg-brain-800 px-2 py-0.5 rounded">
                        {anomaly.entity_name}
                      </span>
                    )}
                  </div>
                  <h3 className="font-medium">{anomaly.title}</h3>
                  <p className="text-sm text-brain-400 mt-1">{anomaly.description}</p>
                  {anomaly.suggested_actions && anomaly.suggested_actions.length > 0 && (
                    <div className="mt-2 text-xs text-brain-500">
                      💡 {anomaly.suggested_actions[0]}
                    </div>
                  )}
                  {anomaly.details && Object.keys(anomaly.details).length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-2 text-xs text-brain-500">
                      {anomaly.details.days_stuck && (
                        <span>{tAnomalies('days_stuck', { days: anomaly.details.days_stuck })}</span>
                      )}
                      {anomaly.details.days_overdue && (
                        <span>{tAnomalies('days_overdue', { days: anomaly.details.days_overdue })}</span>
                      )}
                      {anomaly.details.assignee && (
                        <span>👤 {anomaly.details.assignee}</span>
                      )}
                    </div>
                  )}
                </div>
                <button className="btn btn-secondary text-sm">
                  {tAnomalies('resolve_button')}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
