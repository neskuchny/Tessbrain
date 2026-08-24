'use client'

import { useState, useRef, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { Send, Loader2, FileText, Sparkles, MessageSquare, Zap, HelpCircle, Plus, History, Trash2, ChevronDown, RotateCw } from 'lucide-react'
import VoiceInput from '@/sima/components/VoiceInput/VoiceInput'
import ReactMarkdown from 'react-markdown'
import { cn } from '@/sima/lib/utils'
import { simaFetch } from '@/sima/lib/api'

type ChatMode = 'chat' | 'parse-tz' | 'brainstorm' | 'deep-interview'

export default function ChatPanel() {
  const t = useTranslations('sima_chat')
  const {
    project,
    messages,
    setMessages,
    addMessage,
    setAllBlocks,
    setAllConnections,
    isSending,
    setIsSending,
    setProject,
    currentParentBlockId,
    breadcrumbs,
    currentSessionId,
    setCurrentSessionId,
  } = useSimaStore()

  const [input, setInput] = useState('')
  const [chatMode, setChatMode] = useState<ChatMode>('chat')
  const [brainstormApproved, setBrainstormApproved] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // Сессии
  interface SessionInfo { id: string; name: string | null; createdAt: string }
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [showSessions, setShowSessions] = useState(false)
  const [reapplyingId, setReapplyingId] = useState<string | null>(null)

  // Повторно применить actions сохранённого сообщения к проекту. Безопасно:
  // apply_schema_actions идемпотентна по label/name блока (не дублирует).
  // Сценарий: AI вернул actions, что-то упало → блоки в проекте не появились.
  async function handleReapply(messageId: string) {
    if (!project || reapplyingId) return
    setReapplyingId(messageId)
    try {
      const res = await simaFetch('/ai/reapply-actions', {
        method: 'POST',
        body: JSON.stringify({ projectId: project.id, messageId }),
      })
      const data = await res.json()
      if (data?.applied && data?.project) {
        setProject(data.project)
        setAllBlocks(data.project.blocks || [])
        setAllConnections(data.project.connections || [])
      }
    } catch (err) {
      console.error('reapply failed', err)
    } finally {
      setReapplyingId(null)
    }
  }

  // Загрузка сессий
  useEffect(() => {
    if (!project) return
    simaFetch(`/sessions?projectId=${project.id}`)
      .then(r => r.json())
      .then(data => setSessions(data.sessions || []))
      .catch(() => {})
  }, [project])

  // Создать новую сессию
  async function createSession() {
    if (!project) return
    try {
      const res = await simaFetch('/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id }),
      })
      const data = await res.json()
      if (data.session) {
        setSessions(prev => [data.session, ...prev])
        setCurrentSessionId(data.session.id)
        setMessages([]) // Чистая сессия
      }
    } catch {}
  }

  // Переключить сессию
  async function switchSession(sessionId: string | null) {
    if (!project) return
    setCurrentSessionId(sessionId)
    setShowSessions(false)
    // Загрузить сообщения этой сессии или все
    try {
      const url = sessionId
        ? `/ai/chat?projectId=${project.id}&sessionId=${sessionId}&messagesOnly=true`
        : `/ai/chat?projectId=${project.id}&messagesOnly=true`
      const res = await simaFetch(url)
      const data = await res.json()
      setMessages(data.messages || [])
    } catch {
      setMessages([])
    }
  }

  // Удалить сессию
  async function deleteSession(sessionId: string) {
    try {
      await simaFetch(`/sessions?id=${sessionId}`, { method: 'DELETE' })
      setSessions(prev => prev.filter(s => s.id !== sessionId))
      if (currentSessionId === sessionId) {
        setCurrentSessionId(null)
        if (project) switchSession(null)
      }
    } catch {}
  }

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function sendMessage(text: string, modeOverride?: string) {
    if (!text.trim() || !project || isSending) return

    const effectiveMode = modeOverride || chatMode
    const userMessage = {
      id: crypto.randomUUID(),
      projectId: project.id,
      role: 'user' as const,
      content: text.trim(),
      createdAt: new Date().toISOString(),
    }
    addMessage(userMessage)
    setInput('')
    setIsSending(true)

    try {
      const res = await simaFetch('/ai/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          message: text.trim(),
          mode: effectiveMode,
          parentBlockId: currentParentBlockId || null,
          subschemaContext: currentParentBlockId ? breadcrumbs.map(b => b.name).join(' > ') : null,
          sessionId: currentSessionId || undefined,
        }),
      })

      const data = await res.json()

      const assistantMessage = {
        id: crypto.randomUUID(),
        projectId: project.id,
        role: 'assistant' as const,
        content: data.message || t('default_message'),
        actions: JSON.stringify(data.actions || []),
        createdAt: new Date().toISOString(),
      }
      addMessage(assistantMessage)

      // Обновляем блоки и связи из обновлённого проекта
      if (data.project) {
        console.log('[SIMA] ChatPanel received project:', {
          blocks: data.project.blocks?.length || 0,
          connections: data.project.connections?.length || 0,
          projectId: data.project.id,
        })
        setAllBlocks(data.project.blocks || [])
        setAllConnections(data.project.connections || [])
        // Обновляем проект в сторе если были изменения
        setProject(data.project)
      } else {
        console.warn('[SIMA] ChatPanel: no project in response, status:', res.status, 'data keys:', Object.keys(data))
      }

      // Если brainstorm создал блоки — отмечаем
      if (effectiveMode === 'brainstorm-build' && data.actions?.length > 0) {
        setBrainstormApproved(true)
      }
    } catch (e) {
      console.error('Failed to send message:', e)
      addMessage({
        id: crypto.randomUUID(),
        projectId: project.id,
        role: 'assistant',
        content: t('request_error'),
        createdAt: new Date().toISOString(),
      })
    } finally {
      setIsSending(false)
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  function handleVoiceResult(text: string) {
    setInput((prev) => (prev ? prev + ' ' + text : text))
    inputRef.current?.focus()
  }

  // Подтверждение brainstorm — "Создай схему"
  function handleBuildFromBrainstorm() {
    sendMessage(t('build_command'), 'brainstorm-build')
  }

  const modeConfig = {
    chat: { icon: MessageSquare, label: t('mode_chat_label'), placeholder: t('mode_chat_placeholder') },
    'parse-tz': { icon: FileText, label: t('mode_parsetz_label'), placeholder: t('mode_parsetz_placeholder') },
    brainstorm: { icon: Sparkles, label: t('mode_brainstorm_label'), placeholder: t('mode_brainstorm_placeholder') },
    'deep-interview': { icon: HelpCircle, label: t('mode_interview_label'), placeholder: t('mode_interview_placeholder') },
  }

  return (
    <div className="flex flex-col h-full">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
        {messages.length === 0 && (
          <div className="text-center py-6 space-y-4">
            <p className="text-sm text-sima-textDim">
              {t('welcome')}
            </p>

            {/* Mode cards */}
            <div className="grid grid-cols-4 gap-2 max-w-2xl mx-auto">
              <button
                onClick={() => setChatMode('chat')}
                className={cn(
                  'flex flex-col items-center gap-2 p-3 rounded-lg border transition-all',
                  chatMode === 'chat'
                    ? 'border-sima-primary bg-sima-primary/5'
                    : 'border-sima-border hover:border-sima-primary/50'
                )}
              >
                <MessageSquare className="w-5 h-5 text-sima-primary" />
                <span className="text-xs font-medium text-sima-text">{t('card_chat_title')}</span>
                <span className="text-[10px] text-sima-textDim leading-tight text-center">
                  {t('card_chat_subtitle')}
                </span>
              </button>
              <button
                onClick={() => setChatMode('deep-interview')}
                className={cn(
                  'flex flex-col items-center gap-2 p-3 rounded-lg border transition-all',
                  chatMode === 'deep-interview'
                    ? 'border-green-400 bg-green-400/5'
                    : 'border-sima-border hover:border-green-400/50'
                )}
              >
                <HelpCircle className="w-5 h-5 text-green-400" />
                <span className="text-xs font-medium text-sima-text">{t('card_interview_title')}</span>
                <span className="text-[10px] text-sima-textDim leading-tight text-center">
                  {t('card_interview_subtitle')}
                </span>
              </button>
              <button
                onClick={() => setChatMode('parse-tz')}
                className={cn(
                  'flex flex-col items-center gap-2 p-3 rounded-lg border transition-all',
                  chatMode === 'parse-tz'
                    ? 'border-cyan-400 bg-cyan-400/5'
                    : 'border-sima-border hover:border-cyan-400/50'
                )}
              >
                <FileText className="w-5 h-5 text-cyan-400" />
                <span className="text-xs font-medium text-sima-text">{t('card_parsetz_title')}</span>
                <span className="text-[10px] text-sima-textDim leading-tight text-center">
                  {t('card_parsetz_subtitle')}
                </span>
              </button>
              <button
                onClick={() => setChatMode('brainstorm')}
                className={cn(
                  'flex flex-col items-center gap-2 p-3 rounded-lg border transition-all',
                  chatMode === 'brainstorm'
                    ? 'border-amber-400 bg-amber-400/5'
                    : 'border-sima-border hover:border-amber-400/50'
                )}
              >
                <Sparkles className="w-5 h-5 text-amber-400" />
                <span className="text-xs font-medium text-sima-text">{t('card_brainstorm_title')}</span>
                <span className="text-[10px] text-sima-textDim leading-tight text-center">
                  {t('card_brainstorm_subtitle')}
                </span>
              </button>
            </div>

            {/* Suggestions based on mode */}
            <div className="flex flex-wrap gap-2 justify-center">
              {chatMode === 'chat' && [
                t('example_chat_1'),
                t('example_chat_2'),
                t('example_chat_3'),
              ].map((s) => (
                <button key={s} onClick={() => sendMessage(s)} className="px-3 py-1.5 bg-sima-surfaceLight border border-sima-border rounded-lg text-xs text-sima-textMuted hover:text-sima-text hover:border-sima-primary transition-colors">
                  {s}
                </button>
              ))}
              {chatMode === 'brainstorm' && [
                t('example_interview_1'),
                t('example_interview_2'),
                t('example_interview_3'),
              ].map((s) => (
                <button key={s} onClick={() => sendMessage(s)} className="px-3 py-1.5 bg-sima-surfaceLight border border-sima-border rounded-lg text-xs text-sima-textMuted hover:text-sima-text hover:border-amber-400/50 transition-colors">
                  {s}
                </button>
              ))}
              {chatMode === 'deep-interview' && [
                t('example_brainstorm_1'),
                t('example_brainstorm_2'),
                t('example_brainstorm_3'),
              ].map((s) => (
                <button key={s} onClick={() => sendMessage(s)} className="px-3 py-1.5 bg-sima-surfaceLight border border-sima-border rounded-lg text-xs text-sima-textMuted hover:text-sima-text hover:border-green-400/50 transition-colors">
                  {s}
                </button>
              ))}
              {chatMode === 'parse-tz' && (
                <p className="text-[11px] text-sima-textDim">{t('parsetz_hint')}</p>
              )}
            </div>
          </div>
        )}

        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className={`max-w-[80%] rounded-xl px-4 py-2.5 text-sm ${
                msg.role === 'user'
                  ? 'bg-sima-primary/20 text-sima-text border border-sima-primary/30'
                  : 'bg-sima-surfaceLight text-sima-text border border-sima-border'
              }`}
            >
              {msg.role === 'assistant' ? (
                <div className="prose prose-invert prose-sm max-w-none">
                  <ReactMarkdown>{msg.content}</ReactMarkdown>
                </div>
              ) : (
                <p className="whitespace-pre-wrap">{msg.content}</p>
              )}
              
              {/* Show actions if any */}
              {msg.actions && msg.role === 'assistant' && (() => {
                try {
                  const actions = JSON.parse(msg.actions)
                  if (actions && actions.length > 0) {
                    // Recovery: кнопка появляется, только если в проекте по-факту
                    // нет блока, который actions должны были создать (значит,
                    // apply упал — иначе блок есть и кнопка лишняя).
                    const expectedNames = actions
                      .filter((a: { type: string; data?: Record<string, unknown> }) => a.type === 'CREATE_BLOCK')
                      .map((a: { data?: Record<string, unknown> }) => String((a.data as Record<string, string>)?.label || (a.data as Record<string, string>)?.name || (a.data as Record<string, string>)?.title || '').toLowerCase())
                      .filter(Boolean)
                    const existingLabels = new Set(
                      (project?.blocks || []).flatMap((b: { label?: string; name?: string }) =>
                        [b.label, b.name].filter(Boolean).map((s) => String(s).toLowerCase()))
                    )
                    const missing = expectedNames.length > 0 && expectedNames.some((n: string) => !existingLabels.has(n))
                    const isReapplying = reapplyingId === msg.id
                    return (
                      <div className="mt-2 pt-2 border-t border-sima-border/30 space-y-1">
                        {actions.map((action: { type: string; data?: Record<string, unknown> }, i: number) => (
                          <div key={i} className="flex items-center gap-1.5 text-[10px] text-sima-textDim">
                            <span className="w-1.5 h-1.5 rounded-full bg-sima-accentGreen" />
                            {action.type === 'CREATE_BLOCK' && t('action_create_block', { name: (action.data as Record<string, string>)?.name || (action.data as Record<string, string>)?.title || (action.data as Record<string, string>)?.label || '' })}
                            {action.type === 'CREATE_CONNECTION' && t('action_create_connection', { from: (action.data as Record<string, string>)?.fromLabel || (action.data as Record<string, string>)?.source || (action.data as Record<string, string>)?.from || '?', to: (action.data as Record<string, string>)?.toLabel || (action.data as Record<string, string>)?.target || (action.data as Record<string, string>)?.to || '?' })}
                            {action.type === 'UPDATE_BLOCK' && t('action_update_block')}
                            {action.type === 'UPDATE_PROJECT' && t('action_update_project')}
                            {action.type === 'ADD_DECISION' && t('action_add_decision', { title: (action.data as Record<string, string>)?.title || '' })}
                          </div>
                        ))}
                        {missing && msg.id && (
                          <button
                            onClick={() => handleReapply(msg.id)}
                            disabled={isReapplying}
                            className="mt-1 inline-flex items-center gap-1 text-[10px] text-sima-textDim hover:text-sima-text disabled:opacity-50"
                            title={t('reapply_hint')}
                          >
                            {isReapplying
                              ? <Loader2 className="w-3 h-3 animate-spin" />
                              : <RotateCw className="w-3 h-3" />}
                            {t('reapply_actions')}
                          </button>
                        )}
                      </div>
                    )
                  }
                } catch { return null }
                return null
              })()}
            </div>
          </div>
        ))}

        {/* Brainstorm: кнопка подтверждения */}
        {chatMode === 'brainstorm' && messages.length > 0 && !brainstormApproved && !isSending && (() => {
          const lastAssistant = [...messages].reverse().find(m => m.role === 'assistant')
          if (!lastAssistant) return null
          // Проверяем, что AI не создал блоки (т.е. ещё в режиме обсуждения)
          try {
            const actions = JSON.parse(lastAssistant.actions || '[]')
            if (actions.length > 0) return null // Блоки уже созданы
          } catch { /* ok */ }
          return (
            <div className="flex justify-center">
              <button
                onClick={handleBuildFromBrainstorm}
                className="flex items-center gap-2 px-4 py-2 bg-amber-500/20 text-amber-400 border border-amber-500/30 rounded-xl text-sm font-medium hover:bg-amber-500/30 transition-colors"
              >
                <Zap className="w-4 h-4" />
                {t('build_from_proposed')}
              </button>
            </div>
          )
        })()}

        {isSending && (
          <div className="flex justify-start">
            <div className="bg-sima-surfaceLight rounded-xl px-4 py-3 border border-sima-border">
              <div className="flex items-center gap-2 text-sima-textDim text-sm">
                <Loader2 className="w-4 h-4 animate-spin" />
                {chatMode === 'parse-tz' ? t('analyzing_parsetz') : chatMode === 'brainstorm' ? t('thinking_brainstorm') : chatMode === 'deep-interview' ? t('thinking_interview') : t('thinking_default')}
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Mode selector (compact) + Input */}
      <div className="border-t border-sima-border p-3 space-y-2">
        {/* Session bar */}
        <div className="flex items-center gap-1.5">
          <div className="relative flex-1">
            <button
              onClick={() => setShowSessions(!showSessions)}
              className="flex items-center gap-1.5 px-2 py-1 text-[10px] text-sima-textDim hover:text-sima-text bg-sima-surfaceLight rounded-lg transition-colors w-full"
            >
              <History className="w-3 h-3" />
              <span className="truncate">
                {currentSessionId
                  ? sessions.find(s => s.id === currentSessionId)?.name || t('session_fallback')
                  : t('common_chat')}
              </span>
              <ChevronDown className="w-3 h-3 ml-auto shrink-0" />
            </button>

            {showSessions && (
              <div className="absolute bottom-full mb-1 left-0 w-64 bg-sima-surface border border-sima-border rounded-xl shadow-2xl z-50 p-2 max-h-60 overflow-y-auto">
                <button
                  onClick={() => switchSession(null)}
                  className={cn(
                    'w-full flex items-center gap-2 px-2 py-1.5 text-xs rounded-lg transition-colors text-left',
                    !currentSessionId ? 'bg-sima-primary/10 text-sima-primary' : 'text-sima-textMuted hover:bg-sima-surfaceLight'
                  )}
                >
                  <MessageSquare className="w-3 h-3 shrink-0" />
                  <span>{t('common_chat')}</span>
                </button>
                {sessions.map(s => (
                  <div key={s.id} className="flex items-center group">
                    <button
                      onClick={() => switchSession(s.id)}
                      className={cn(
                        'flex-1 flex items-center gap-2 px-2 py-1.5 text-xs rounded-lg transition-colors text-left truncate',
                        currentSessionId === s.id ? 'bg-sima-primary/10 text-sima-primary' : 'text-sima-textMuted hover:bg-sima-surfaceLight'
                      )}
                    >
                      <History className="w-3 h-3 shrink-0" />
                      <span className="truncate">{s.name || t('session_fallback')}</span>
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); deleteSession(s.id) }}
                      className="p-1 opacity-0 group-hover:opacity-100 text-red-400 hover:bg-red-400/10 rounded transition-all"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
          <button
            onClick={createSession}
            className="p-1.5 text-sima-textDim hover:text-sima-primary hover:bg-sima-primary/10 rounded-lg transition-colors"
            title={t("new_session_title")}
          >
            <Plus className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Compact mode tabs */}
        {messages.length > 0 && (
          <div className="flex gap-1">
            {(Object.entries(modeConfig) as [ChatMode, typeof modeConfig['chat']][]).map(([mode, cfg]) => {
              const Icon = cfg.icon
              return (
                <button
                  key={mode}
                  onClick={() => setChatMode(mode)}
                  className={cn(
                    'flex items-center gap-1 px-2 py-1 text-[10px] rounded transition-colors',
                    chatMode === mode
                      ? mode === 'parse-tz' ? 'bg-cyan-400/20 text-cyan-400'
                        : mode === 'brainstorm' ? 'bg-amber-400/20 text-amber-400'
                        : mode === 'deep-interview' ? 'bg-green-400/20 text-green-400'
                        : 'bg-sima-primary/20 text-sima-primary'
                      : 'text-sima-textDim hover:text-sima-textMuted'
                  )}
                >
                  <Icon className="w-3 h-3" />
                  {cfg.label}
                </button>
              )
            })}
          </div>
        )}

        <div className="flex items-end gap-2">
          <VoiceInput onResult={handleVoiceResult} />
          
          <div className="flex-1 relative">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={modeConfig[chatMode].placeholder}
              className={cn(
                'w-full bg-sima-bg border rounded-xl px-4 py-2.5 pr-10 text-sm text-sima-text placeholder-sima-textDim focus:outline-none resize-none transition-colors',
                chatMode === 'parse-tz'
                  ? 'border-cyan-400/30 focus:border-cyan-400/50 max-h-[200px]'
                  : chatMode === 'brainstorm'
                  ? 'border-amber-400/30 focus:border-amber-400/50 max-h-[150px]'
                  : chatMode === 'deep-interview'
                  ? 'border-green-400/30 focus:border-green-400/50 max-h-[150px]'
                  : 'border-sima-border focus:border-sima-primary max-h-[100px]'
              )}
              rows={chatMode === 'parse-tz' ? 3 : 1}
              disabled={isSending}
            />
          </div>

          <button
            onClick={() => sendMessage(input)}
            disabled={!input.trim() || isSending}
            className={cn(
              'p-2.5 text-white rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed shrink-0',
              chatMode === 'parse-tz'
                ? 'bg-cyan-500 hover:bg-cyan-600'
                : chatMode === 'brainstorm'
                ? 'bg-amber-500 hover:bg-amber-600'
                : chatMode === 'deep-interview'
                ? 'bg-green-500 hover:bg-green-600'
                : 'bg-sima-primary hover:bg-sima-primaryHover'
            )}
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  )
}
