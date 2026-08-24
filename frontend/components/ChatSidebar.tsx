'use client'

import { useState, useEffect } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import {
  MessageSquare, Plus, Search, Trash2, Calendar, FolderKanban,
  Building2, ChevronDown, ChevronRight, Filter, X, FileText,
  Folder, FolderOpen
} from 'lucide-react'

export interface ChatSession {
  id: string
  title: string
  preview: string
  created_at: string
  updated_at: string
  message_count: number
  folder?: string | null
  agent_mode?: string | null   // режим чата → цветовая пометка в списке
}

// Цвет и подпись режима чата (совпадает с AGENT_MODES в page.tsx). Неизвестный
// режим/старые чаты без пометки → нейтральный серый (честно «не размечено»).
export const CHAT_MODE_META: Record<string, { dot: string; labelKey: string }> = {
  brain: { dot: 'bg-purple-500', labelKey: 'mode_brain' },
  mark: { dot: 'bg-orange-500', labelKey: 'mode_mark' },
  transcripts: { dot: 'bg-green-500', labelKey: 'mode_transcripts' },
  automation: { dot: 'bg-blue-500', labelKey: 'mode_automation' },
}

export function chatModeMeta(mode?: string | null) {
  return (mode && CHAT_MODE_META[mode]) || { dot: 'bg-brain-600', labelKey: 'mode_none' }
}

export interface FilterState {
  meetings: string[]
  projects: string[]
  folders: string[]
  documents: string[]
  // подключаемый контекст: переписки (chat_ingest) + CRM + задачи
  chatSources?: string[]
  includeCrm?: boolean
  includeTasks?: boolean
}

interface ChatSidebarProps {
  sessions: ChatSession[]
  currentSessionId: string | null
  onSelectSession: (id: string) => void
  onNewChat: () => void
  onDeleteSession: (id: string) => void
  onSetSessionFolder?: (id: string, folder: string) => void
  chatFolders?: string[]                       // реестр (включая пустые)
  onCreateFolder?: (name: string) => void
  onDeleteFolder?: (name: string) => void
  filters: FilterState
  onFiltersChange: (filters: FilterState) => void
  availableFilters: {
    meetings: Array<{ id: string; title: string; project_id?: string; folder_id?: string }>
    projects: Array<{ id: string; name: string }>
    folders: Array<{ id: string; name: string; project_id?: string }>
    documents: Array<{ id: string; title: string; doc_type?: string; sync_status?: string; folder?: string }>
  }
}

export default function ChatSidebar({
  sessions,
  currentSessionId,
  onSelectSession,
  onNewChat,
  onDeleteSession,
  onSetSessionFolder,
  chatFolders,
  onCreateFolder,
  onDeleteFolder,
  filters,
  onFiltersChange,
  availableFilters,
}: ChatSidebarProps) {
  const t = useTranslations('chat_sidebar')
  const locale = useLocale()
  const [searchQuery, setSearchQuery] = useState('')
  const [showFilters, setShowFilters] = useState(false)
  const [folderMenuFor, setFolderMenuFor] = useState<string | null>(null)
  const [collapsedChatFolders, setCollapsedChatFolders] = useState<Record<string, boolean>>({})
  const [expandedSections, setExpandedSections] = useState({
    meetings: true,
    projects: false,
    folders: false,
    documents: false,
    sources: false,
  })
  // Переписки (chat_ingest): грузим сами по Bearer — источники видит владелец
  const [chatSourceList, setChatSourceList] = useState<Array<{
    key: string; title: string; platform: string
    message_count?: number; mode?: string
  }>>([])
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const { authFetch, getAccessToken } = await import('@/lib/authFetch')
        // до логина токена нет — не шумим 401-ами в лог бэкенда
        if (!getAccessToken()) return
        const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
        const r = await authFetch(`${API}/api/v1/chat-sources`)
        const d = await r.json().catch(() => ({}))
        if (alive) setChatSourceList(d.sources || [])
      } catch { /* переписок может не быть — секция просто пустая */ }
    })()
    return () => { alive = false }
  }, [])

  const filteredSessions = sessions.filter(s =>
    s.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
    s.preview.toLowerCase().includes(searchQuery.toLowerCase())
  )

  // Папки чатов — плоские ярлыки на сессиях (не путать с filters.folders:
  // это папки ВСТРЕЧ MeetFlow для контекста чата)
  // Реестр (папки живут и пустыми) ∪ фактические метки сессий
  const chatFolderNames = Array.from(new Set([
    ...(chatFolders || []),
    ...sessions.map(s => (s.folder || '').trim()).filter(Boolean),
  ])).sort((a, b) => a.localeCompare(b, 'ru'))
  const sessionsInFolder = (name: string) =>
    filteredSessions.filter(s => (s.folder || '').trim() === name)
  const ungroupedSessions = filteredSessions.filter(s => !(s.folder || '').trim())

  const assignFolder = (sessionId: string, folder: string) => {
    setFolderMenuFor(null)
    onSetSessionFolder?.(sessionId, folder)
  }
  const promptNewFolder = (sessionId: string) => {
    const name = window.prompt(t('folder_prompt'))?.trim()
    if (name) assignFolder(sessionId, name.slice(0, 60))
    else setFolderMenuFor(null)
  }

  const toggleSection = (section: keyof typeof expandedSections) => {
    setExpandedSections(prev => ({ ...prev, [section]: !prev[section] }))
  }

  const toggleFilter = (
    type: 'meetings' | 'projects' | 'folders' | 'documents', id: string) => {
    const current = filters[type]
    const updated = current.includes(id)
      ? current.filter(x => x !== id)
      : [...current, id]
    onFiltersChange({ ...filters, [type]: updated })
  }

  const clearAllFilters = () => {
    onFiltersChange({ meetings: [], projects: [], folders: [], documents: [],
      chatSources: [], includeCrm: false, includeTasks: false })
  }

  const toggleChatSource = (key: string) => {
    const cur = filters.chatSources || []
    const updated = cur.includes(key) ? cur.filter(x => x !== key) : [...cur, key]
    onFiltersChange({ ...filters, chatSources: updated })
  }

  const activeFilterCount = (filters.meetings?.length || 0) +
                            (filters.projects?.length || 0) +
                            (filters.folders?.length || 0) +
                            (filters.documents?.length || 0) +
                            (filters.chatSources?.length || 0) +
                            (filters.includeCrm ? 1 : 0) +
                            (filters.includeTasks ? 1 : 0)

  // Filter folders based on selected projects
  const filteredFolders = availableFilters.folders.filter(f => {
    if (filters.projects.length === 0) return true;
    
    // Allow "No Project" folders if selected
    if (filters.projects.includes('no_project') && !f.project_id) {
      return true;
    }
    
    return f.project_id && filters.projects.includes(f.project_id);
  });

  // Filter meetings based on selected projects AND selected folders
  const filteredMeetings = availableFilters.meetings.filter(m => {
    // 1. Project filter
    let projectMatch = true;
    if (filters.projects.length > 0) {
      // Direct project match
      const directMatch = m.project_id && filters.projects.includes(m.project_id);
      
      // Indirect match via folder
      const meetingFolder = availableFilters.folders.find(f => f.id === m.folder_id);
      const folderIndirectMatch = meetingFolder && meetingFolder.project_id && filters.projects.includes(meetingFolder.project_id);
      
      // "No Project" match logic:
      // Meeting has NO direct project AND (NO folder OR folder has NO project)
      const isNoProject = !m.project_id && (!meetingFolder || !meetingFolder.project_id);
      const noProjectMatch = filters.projects.includes('no_project') && isNoProject;
      
      projectMatch = Boolean(directMatch || folderIndirectMatch || noProjectMatch);
    }
    
    // 2. Folder filter
    const folderMatch = filters.folders.length === 0 ||
      (m.folder_id && filters.folders.includes(m.folder_id));

    return projectMatch && folderMatch;
  });

  const renderSessionRow = (session: ChatSession) => {
    const mode = chatModeMeta(session.agent_mode)
    return (
    <div
      key={session.id}
      className={`group relative p-3 rounded-lg cursor-pointer transition-colors ${
        currentSessionId === session.id
          ? 'bg-brain-700/50 border border-brain-600/50'
          : 'hover:bg-brain-800/50'
      }`}
      onClick={() => onSelectSession(session.id)}
    >
      <div className="flex items-start gap-3">
        {/* иконка чата + цветная точка режима (brain/mark/…) */}
        <div className="relative mt-0.5 flex-shrink-0" title={t(mode.labelKey)}>
          <MessageSquare className="w-4 h-4 text-brain-500" />
          <span className={`absolute -bottom-0.5 -right-0.5 w-2 h-2 rounded-full ring-2 ring-brain-900 ${mode.dot}`} />
        </div>
        <div className="flex-1 min-w-0">
          <h4 className="text-sm font-medium text-brain-200 truncate">
            {session.title}
          </h4>
          <p className="text-xs text-brain-500 truncate mt-0.5">
            {session.preview}
          </p>
          <div className="flex items-center gap-2 mt-1 text-xs text-brain-600">
            <span className="inline-flex items-center gap-1">
              <span className={`w-1.5 h-1.5 rounded-full ${mode.dot}`} />
              {t(mode.labelKey)}
            </span>
            <span>•</span>
            <span suppressHydrationWarning>{new Date(session.updated_at).toLocaleDateString(locale)}</span>
            <span>•</span>
            <span>{session.message_count} {t('messages_short')}</span>
          </div>
        </div>
      </div>

      {/* Row actions: folder + delete */}
      <div className={`absolute top-2 right-2 flex gap-0.5 transition-all ${
        folderMenuFor === session.id ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
      }`}>
        {onSetSessionFolder && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              setFolderMenuFor(folderMenuFor === session.id ? null : session.id)
            }}
            className="p-1.5 rounded-md hover:bg-brain-700/60 text-brain-500 hover:text-amber-400 transition-all"
            title={t('folder_assign_title')}
          >
            <Folder className="w-3.5 h-3.5" />
          </button>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation()
            onDeleteSession(session.id)
          }}
          className="p-1.5 rounded-md hover:bg-red-500/20 text-brain-500 hover:text-red-400 transition-all"
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Folder menu */}
      {folderMenuFor === session.id && (
        <>
          <div
            className="fixed inset-0 z-20"
            onClick={(e) => { e.stopPropagation(); setFolderMenuFor(null) }}
          />
          <div
            className="absolute right-2 top-9 z-30 w-48 py-1 bg-brain-900 border border-brain-700 rounded-lg shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            {chatFolderNames.map(name => (
              <button
                key={name}
                onClick={() => assignFolder(session.id, name)}
                className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-brain-800 ${
                  (session.folder || '').trim() === name ? 'text-amber-400' : 'text-brain-300'
                }`}
              >
                <Folder className="w-3 h-3 flex-shrink-0" />
                <span className="truncate">{name}</span>
              </button>
            ))}
            <button
              onClick={() => promptNewFolder(session.id)}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-brain-300 hover:bg-brain-800"
            >
              <Plus className="w-3 h-3 flex-shrink-0" />
              <span>{t('folder_new')}</span>
            </button>
            {(session.folder || '').trim() && (
              <button
                onClick={() => assignFolder(session.id, '')}
                className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-brain-400 hover:bg-brain-800"
              >
                <X className="w-3 h-3 flex-shrink-0" />
                <span>{t('folder_remove')}</span>
              </button>
            )}
          </div>
        </>
      )}
    </div>
    )
  }

  // Показываем все документы, сортируем: синхронизированные сверху
  const allDocuments = [...(availableFilters.documents || [])].sort((a, b) => {
    if (a.sync_status === 'synced' && b.sync_status !== 'synced') return -1;
    if (a.sync_status !== 'synced' && b.sync_status === 'synced') return 1;
    return 0;
  });

  // Группировка документов по папке (metadata.folder). Папка в контексте чата =
  // все её документы разом — как встречи из папки. Документы без ярлыка идут
  // отдельным «свободным» списком (поведение как раньше).
  const docFolderNames = Array.from(
    new Set(allDocuments.map(d => String(d.folder || '').trim()).filter(Boolean))
  ).sort((a, b) => a.localeCompare(b));
  const looseDocuments = allDocuments.filter(d => !String(d.folder || '').trim());

  // Выбрать/снять все документы папки одним действием.
  const toggleFolderDocuments = (docIds: string[], allSelected: boolean) => {
    const set = new Set(filters.documents || []);
    if (allSelected) docIds.forEach(id => set.delete(id));
    else docIds.forEach(id => set.add(id));
    onFiltersChange({ ...filters, documents: Array.from(set) });
  };

  // Одна строка-документ (переиспользуется в папках и «свободном» списке).
  const renderDocRow = (d: { id: string; title: string; doc_type?: string; sync_status?: string }) => (
    <label key={d.id} className="flex items-center gap-2 text-xs text-brain-400 hover:text-brain-300 cursor-pointer">
      <input
        type="checkbox"
        checked={filters.documents.includes(d.id)}
        onChange={() => toggleFilter('documents', d.id)}
        className="rounded border-brain-600 bg-brain-800 text-purple-500 focus:ring-purple-500 flex-shrink-0"
      />
      <span className="truncate flex-1" title={d.title}>{d.title}</span>
      <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
        d.sync_status === 'synced' ? 'bg-green-500' :
        d.sync_status === 'syncing' ? 'bg-blue-500 animate-pulse' :
        d.sync_status === 'error' ? 'bg-red-500' : 'bg-brain-600'
      }`} title={
        d.sync_status === 'synced' ? t('doc_status_synced') :
        d.sync_status === 'syncing' ? t('doc_status_syncing') :
        d.sync_status === 'error' ? t('doc_status_error') : t('doc_status_chat_only')
      } />
      {d.doc_type && (
        <span className="text-[10px] text-brain-500 flex-shrink-0">({d.doc_type})</span>
      )}
    </label>
  );

  return (
    <div className="w-72 h-full bg-brain-950 border-r border-brain-700/30 flex flex-col">
      {/* Header */}
      <div className="p-4 border-b border-brain-700/30">
        <button
          onClick={onNewChat}
          className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-brain-600 hover:bg-brain-500 text-white rounded-lg transition-colors font-medium"
        >
          <Plus className="w-5 h-5" />
          {t('new_chat')}
        </button>
      </div>

      {/* Search */}
      <div className="p-3 border-b border-brain-700/30">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-brain-500" />
          <input
            type="text"
            placeholder={t('search_placeholder')}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-9 pr-3 py-2 bg-brain-900 border border-brain-700/50 rounded-lg text-sm text-brain-200 placeholder-brain-500 focus:outline-none focus:border-brain-500"
          />
        </div>
      </div>

      {/* Filters Toggle */}
      <div className="px-3 py-2 border-b border-brain-700/30">
        <button
          onClick={() => setShowFilters(!showFilters)}
          className={`w-full flex items-center justify-between px-3 py-2 rounded-lg transition-colors ${
            showFilters || activeFilterCount > 0
              ? 'bg-orange-600/20 text-orange-400'
              : 'bg-brain-900/50 text-brain-400 hover:bg-brain-800'
          }`}
        >
          <span className="flex items-center gap-2 text-sm font-medium">
            <Filter className="w-4 h-4" />
            {t('chat_context')}
            {activeFilterCount > 0 && (
              <span className="px-1.5 py-0.5 bg-orange-600 text-white text-xs rounded-full">
                {activeFilterCount}
              </span>
            )}
          </span>
          {showFilters ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
        </button>
      </div>

      {/* Filters Panel */}
      {showFilters && (
        <div className="px-3 py-2 border-b border-brain-700/30 max-h-80 overflow-y-auto bg-brain-900/30">
          {activeFilterCount > 0 && (
            <button
              onClick={clearAllFilters}
              className="w-full mb-2 flex items-center justify-center gap-1 px-2 py-1 text-xs text-orange-400 hover:text-orange-300"
            >
              <X className="w-3 h-3" />
              {t('clear_all_filters')}
            </button>
          )}

          {/* Documents Filter - FIRST */}
          <div className="mb-2">
            <button
              onClick={() => toggleSection('documents')}
              className="w-full flex items-center justify-between px-2 py-1.5 text-sm text-brain-300 hover:text-brain-200"
            >
              <span className="flex items-center gap-2">
                <FileText className="w-4 h-4 text-purple-400" />
                {t('documents_label', { count: allDocuments.length })}
              </span>
              {expandedSections.documents ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            </button>
            {expandedSections.documents && (
              <div className="ml-6 mt-1 space-y-1">
                {/* Папки документов: выбор папки = все её документы в контексте */}
                {docFolderNames.map(name => {
                  const docs = allDocuments.filter(d => String(d.folder || '').trim() === name);
                  const ids = docs.map(d => d.id);
                  const allSelected = ids.length > 0 && ids.every(id => filters.documents.includes(id));
                  return (
                    <div key={`folder-${name}`} className="mb-1">
                      <label className="flex items-center gap-2 text-xs font-medium text-brain-300 hover:text-brain-200 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={allSelected}
                          onChange={() => toggleFolderDocuments(ids, allSelected)}
                          className="rounded border-brain-600 bg-brain-800 text-purple-500 focus:ring-purple-500 flex-shrink-0"
                        />
                        <Folder className="w-3 h-3 text-purple-400 flex-shrink-0" />
                        <span className="truncate flex-1" title={name}>{name}</span>
                        <span className="text-[10px] text-brain-500 flex-shrink-0">{docs.length}</span>
                      </label>
                      <div className="ml-5 mt-1 space-y-1">
                        {docs.map(renderDocRow)}
                      </div>
                    </div>
                  );
                })}
                {/* Документы без папки */}
                {looseDocuments.map(renderDocRow)}
                {allDocuments.length === 0 && (
                  <span className="text-xs text-brain-500">{t('no_documents')}</span>
                )}
              </div>
            )}
          </div>

          {/* Переписки + данные (CRM, задачи) — подключаемый контекст */}
          <div className="mb-2">
            <button
              onClick={() => toggleSection('sources')}
              className="w-full flex items-center justify-between px-2 py-1.5 text-sm text-brain-300 hover:text-brain-200"
            >
              <span className="flex items-center gap-2">
                <MessageSquare className="w-4 h-4 text-teal-400" />
                {t('sources_label', { count: chatSourceList.length })}
              </span>
              {expandedSections.sources ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            </button>
            {expandedSections.sources && (
              <div className="ml-6 mt-1 space-y-1">
                {chatSourceList.map(s => (
                  <label key={s.key} className="flex items-center gap-2 text-xs text-brain-400 hover:text-brain-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={(filters.chatSources || []).includes(s.key)}
                      onChange={() => toggleChatSource(s.key)}
                      className="rounded border-brain-600 bg-brain-800 text-teal-500 focus:ring-teal-500 flex-shrink-0"
                    />
                    <span className="truncate flex-1" title={s.title}>{s.title}</span>
                    <span className="text-[10px] text-brain-500 flex-shrink-0">
                      {s.message_count || 0}{s.mode === 'context_only' ? ` · ${t('source_ctx_only')}` : ''}
                    </span>
                  </label>
                ))}
                {chatSourceList.length === 0 && (
                  <span className="text-xs text-brain-500">{t('no_sources')}</span>
                )}
                <label className="flex items-center gap-2 text-xs text-brain-400 hover:text-brain-300 cursor-pointer pt-1 border-t border-brain-800/60">
                  <input
                    type="checkbox"
                    checked={Boolean(filters.includeCrm)}
                    onChange={() => onFiltersChange({ ...filters, includeCrm: !filters.includeCrm })}
                    className="rounded border-brain-600 bg-brain-800 text-teal-500 focus:ring-teal-500"
                  />
                  <span>{t('source_crm')}</span>
                </label>
                <label className="flex items-center gap-2 text-xs text-brain-400 hover:text-brain-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={Boolean(filters.includeTasks)}
                    onChange={() => onFiltersChange({ ...filters, includeTasks: !filters.includeTasks })}
                    className="rounded border-brain-600 bg-brain-800 text-teal-500 focus:ring-teal-500"
                  />
                  <span>{t('source_tasks')}</span>
                </label>
              </div>
            )}
          </div>

          {/* Meetings Filter */}
          <div className="mb-2">
            <button
              onClick={() => toggleSection('meetings')}
              className="w-full flex items-center justify-between px-2 py-1.5 text-sm text-brain-300 hover:text-brain-200"
            >
              <span className="flex items-center gap-2">
                <Calendar className="w-4 h-4 text-orange-400" />
                {t('meetings_label', { count: filteredMeetings.length })}
              </span>
              {expandedSections.meetings ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            </button>
            {expandedSections.meetings && (
              <div className="ml-6 mt-1 space-y-1">
                {filteredMeetings.map(m => (
                  <label key={m.id} className="flex items-center gap-2 text-xs text-brain-400 hover:text-brain-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={filters.meetings.includes(m.id)}
                      onChange={() => toggleFilter('meetings', m.id)}
                      className="rounded border-brain-600 bg-brain-800 text-orange-500 focus:ring-orange-500"
                    />
                    <span className="truncate">{m.title}</span>
                  </label>
                ))}
                {filteredMeetings.length === 0 && (
                  <span className="text-xs text-brain-500">{t('no_meetings')}</span>
                )}
              </div>
            )}
          </div>

          {/* Projects Filter */}
          <div className="mb-2">
            <button
              onClick={() => toggleSection('projects')}
              className="w-full flex items-center justify-between px-2 py-1.5 text-sm text-brain-300 hover:text-brain-200"
            >
              <span className="flex items-center gap-2">
                <Building2 className="w-4 h-4 text-blue-400" />
                {t('projects_label', { count: availableFilters.projects.length })}
              </span>
              {expandedSections.projects ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            </button>
            {expandedSections.projects && (
              <div className="ml-6 mt-1 space-y-1">
                {/* No Project Option */}
                <label className="flex items-center gap-2 text-xs text-brain-400 hover:text-brain-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={filters.projects.includes('no_project')}
                    onChange={() => toggleFilter('projects', 'no_project')}
                    className="rounded border-brain-600 bg-brain-800 text-blue-500 focus:ring-blue-500 flex-shrink-0"
                  />
                  <span className="truncate italic">{t('no_project')}</span>
                </label>

                {availableFilters.projects.map(p => (
                  <label key={p.id} className="flex items-center gap-2 text-xs text-brain-400 hover:text-brain-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={filters.projects.includes(p.id)}
                      onChange={() => toggleFilter('projects', p.id)}
                      className="rounded border-brain-600 bg-brain-800 text-blue-500 focus:ring-blue-500"
                    />
                    <span className="truncate">{p.name}</span>
                  </label>
                ))}
                {availableFilters.projects.length === 0 && (
                  <span className="text-xs text-brain-500">{t('no_projects')}</span>
                )}
              </div>
            )}
          </div>

          {/* Folders Filter */}
          <div>
            <button
              onClick={() => toggleSection('folders')}
              className="w-full flex items-center justify-between px-2 py-1.5 text-sm text-brain-300 hover:text-brain-200"
            >
              <span className="flex items-center gap-2">
                <FolderKanban className="w-4 h-4 text-green-400" />
                {t('folders_label', { count: filteredFolders.length })}
              </span>
              {expandedSections.folders ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            </button>
            {expandedSections.folders && (
              <div className="ml-6 mt-1 space-y-1">
                {filteredFolders.map(f => (
                  <label key={f.id} className="flex items-center gap-2 text-xs text-brain-400 hover:text-brain-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={filters.folders.includes(f.id)}
                      onChange={() => toggleFilter('folders', f.id)}
                      className="rounded border-brain-600 bg-brain-800 text-green-500 focus:ring-green-500"
                    />
                    <span className="truncate">{f.name}</span>
                  </label>
                ))}
                {filteredFolders.length === 0 && (
                  <span className="text-xs text-brain-500">{t('no_folders')}</span>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Chat List — сначала папки (сворачиваемые), затем чаты без папки */}
      <div className="flex-1 overflow-y-auto">
        <div className="p-2 space-y-1">
          {filteredSessions.length === 0 ? (
            <div className="text-center py-8 text-brain-500 text-sm">
              {searchQuery ? t('no_chats_found') : t('no_chats')}
            </div>
          ) : (
            <>
              {onCreateFolder && (
                <button
                  onClick={() => {
                    const name = window.prompt(t('folder_prompt'))?.trim()
                    if (name) onCreateFolder(name.slice(0, 60))
                  }}
                  className="w-full flex items-center gap-2 px-2 py-1 rounded-md text-xs text-brain-500 hover:text-brain-300 hover:bg-brain-800/40 transition-colors"
                >
                  <Plus className="w-3 h-3" />{t('folder_new')}
                </button>
              )}
              {chatFolderNames.map(name => {
                const inFolder = sessionsInFolder(name)
                if (searchQuery && inFolder.length === 0) return null
                const collapsed = collapsedChatFolders[name]
                return (
                  <div key={`folder:${name}`}>
                    <button
                      onClick={() => setCollapsedChatFolders(prev => ({ ...prev, [name]: !prev[name] }))}
                      className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-xs font-medium text-brain-400 hover:text-brain-200 hover:bg-brain-800/40 transition-colors"
                    >
                      {collapsed
                        ? <Folder className="w-3.5 h-3.5 text-amber-500/80 flex-shrink-0" />
                        : <FolderOpen className="w-3.5 h-3.5 text-amber-500/80 flex-shrink-0" />}
                      <span className="truncate flex-1 text-left">{name}</span>
                      {inFolder.length === 0 && onDeleteFolder && (
                        <span
                          role="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            if (window.confirm(t('folder_delete_confirm', { name }))) onDeleteFolder(name)
                          }}
                          className="text-brain-600 hover:text-red-400 px-0.5"
                          title={t('folder_delete_title')}
                        ><X className="w-3 h-3" /></span>
                      )}
                      <span className="text-brain-600">{inFolder.length}</span>
                      {collapsed
                        ? <ChevronRight className="w-3 h-3 flex-shrink-0" />
                        : <ChevronDown className="w-3 h-3 flex-shrink-0" />}
                    </button>
                    {!collapsed && (
                      <div className="ml-2 pl-2 border-l border-brain-700/40 space-y-1 mt-0.5 mb-1">
                        {inFolder.map(renderSessionRow)}
                        {inFolder.length === 0 && (
                          <div className="px-2 py-1.5 text-xs text-brain-600">{t('folder_empty')}</div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
              {ungroupedSessions.map(renderSessionRow)}
            </>
          )}
        </div>
      </div>

      {/* Active Filters Summary */}
      {activeFilterCount > 0 && !showFilters && (
        <div className="p-3 border-t border-brain-700/30 bg-brain-900/50">
          <div className="flex flex-wrap gap-1">
            {filters.documents && filters.documents.map(id => {
              const doc = (availableFilters.documents || []).find(d => d.id === id)
              return doc && (
                <span key={id} className="inline-flex items-center gap-1 px-2 py-0.5 bg-purple-600/20 text-purple-400 text-xs rounded-full">
                  <FileText className="w-3 h-3" />
                  {doc.title.slice(0, 15)}
                  <button onClick={() => toggleFilter('documents', id)} className="hover:text-purple-200">
                    <X className="w-3 h-3" />
                  </button>
                </span>
              )
            })}
            {filters.meetings.map(id => {
              const meeting = availableFilters.meetings.find(m => m.id === id)
              return meeting && (
                <span key={id} className="inline-flex items-center gap-1 px-2 py-0.5 bg-orange-600/20 text-orange-400 text-xs rounded-full">
                  <Calendar className="w-3 h-3" />
                  {meeting.title.slice(0, 15)}
                  <button onClick={() => toggleFilter('meetings', id)} className="hover:text-orange-200">
                    <X className="w-3 h-3" />
                  </button>
                </span>
              )
            })}
            {filters.projects.map(id => {
              if (id === 'no_project') {
                return (
                  <span key={id} className="inline-flex items-center gap-1 px-2 py-0.5 bg-blue-600/20 text-blue-400 text-xs rounded-full">
                    <Building2 className="w-3 h-3" />
                    {t('no_project')}
                    <button onClick={() => toggleFilter('projects', id)} className="hover:text-blue-200">
                      <X className="w-3 h-3" />
                    </button>
                  </span>
                )
              }
              const project = availableFilters.projects.find(p => p.id === id)
              return project && (
                <span key={id} className="inline-flex items-center gap-1 px-2 py-0.5 bg-blue-600/20 text-blue-400 text-xs rounded-full">
                  <Building2 className="w-3 h-3" />
                  {project.name.slice(0, 15)}
                  <button onClick={() => toggleFilter('projects', id)} className="hover:text-blue-200">
                    <X className="w-3 h-3" />
                  </button>
                </span>
              )
            })}
            {filters.folders.map(id => {
              const folder = availableFilters.folders.find(f => f.id === id)
              return folder && (
                <span key={id} className="inline-flex items-center gap-1 px-2 py-0.5 bg-green-600/20 text-green-400 text-xs rounded-full">
                  <FolderKanban className="w-3 h-3" />
                  {folder.name.slice(0, 15)}
                  <button onClick={() => toggleFilter('folders', id)} className="hover:text-green-200">
                    <X className="w-3 h-3" />
                  </button>
                </span>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
