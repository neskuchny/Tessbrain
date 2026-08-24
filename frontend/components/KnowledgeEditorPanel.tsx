'use client'

import { useState, useRef, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import {
  Edit3, 
  Check, 
  X, 
  AlertCircle, 
  HelpCircle, 
  ChevronRight,
  Search,
  Trash2,
  Link,
  Unlink,
  Merge,
  Tag,
  Send,
  History,
  RefreshCw
} from 'lucide-react'

interface EditOperation {
  operation_id: string
  status: string
  clarification_needed?: string
  found_entities?: Array<{
    id: string
    name: string
    type: string
    description?: string
  }>
  preview?: {
    action: string
    entity: { id: string; name: string; type: string }
    changes: Array<{ field: string; old_value: any; new_value: any }>
    human_readable: string
  }
  result?: any
  error?: string
  message: string
}

interface KnowledgeEditorPanelProps {
  userId: string | null
  onClose?: () => void
}

export default function KnowledgeEditorPanel({ userId, onClose }: KnowledgeEditorPanelProps) {
  const t = useTranslations('knowledge_editor')
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [currentOperation, setCurrentOperation] = useState<EditOperation | null>(null)
  const [history, setHistory] = useState<EditOperation[]>([])
  const [showHistory, setShowHistory] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const exampleCommands = [
    { text: t('example_change_position'), icon: Edit3 },
    { text: t('example_update_project'), icon: Edit3 },
    { text: t('example_link_team'), icon: Link },
    { text: t('example_add_product'), icon: Tag },
    { text: t('example_rename_dept'), icon: Tag },
    { text: t('example_merge'), icon: Merge },
    { text: t('example_delete_kpi'), icon: Trash2 },
  ]

  const handleSubmit = async (text?: string) => {
    const commandText = text || input
    if (!commandText.trim() || !userId) return

    setLoading(true)
    setInput('')

    try {
      const response = await fetch(
        `/api/v1/knowledge/edit?user_id=${userId}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: commandText }),
        }
      )

      const data: EditOperation = await response.json()
      setCurrentOperation(data)
      
      // Добавляем в историю
      setHistory(prev => [data, ...prev].slice(0, 20))
    } catch (error) {
      console.error('Edit error:', error)
      setCurrentOperation({
        operation_id: '',
        status: 'failed',
        error: String(error),
        message: t('error_prefix', { message: String(error) }),
      })
    } finally {
      setLoading(false)
    }
  }

  const handleClarify = async (clarification: string) => {
    if (!currentOperation?.operation_id || !userId) return

    setLoading(true)

    try {
      const response = await fetch(
        `/api/v1/knowledge/edit/${currentOperation.operation_id}/clarify?user_id=${userId}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: clarification }),
        }
      )

      const data: EditOperation = await response.json()
      setCurrentOperation(data)
    } catch (error) {
      console.error('Clarify error:', error)
    } finally {
      setLoading(false)
    }
  }

  const handleConfirm = async (confirmed: boolean) => {
    if (!currentOperation?.operation_id || !userId) return

    setLoading(true)

    try {
      const response = await fetch(
        `/api/v1/knowledge/edit/${currentOperation.operation_id}/confirm?user_id=${userId}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirmed }),
        }
      )

      const data: EditOperation = await response.json()
      setCurrentOperation(data)
      
      // Обновляем в истории
      setHistory(prev => 
        prev.map(op => op.operation_id === data.operation_id ? data : op)
      )
    } catch (error) {
      console.error('Confirm error:', error)
    } finally {
      setLoading(false)
    }
  }

  const handleSelectEntity = (index: number) => {
    handleClarify(String(index))
  }

  const resetOperation = () => {
    setCurrentOperation(null)
    inputRef.current?.focus()
  }

  // Определяем цвет статуса
  const getStatusColor = (status: string) => {
    switch (status) {
      case 'applied': return 'text-green-400'
      case 'pending_confirmation': return 'text-yellow-400'
      case 'pending_clarification': return 'text-blue-400'
      case 'cancelled': return 'text-slate-400'
      case 'failed': return 'text-red-400'
      default: return 'text-slate-400'
    }
  }

  const getStatusText = (status: string) => {
    switch (status) {
      case 'applied': return t('status_applied')
      case 'pending_confirmation': return t('status_pending_confirmation')
      case 'pending_clarification': return t('status_pending_clarification')
      case 'cancelled': return t('status_cancelled')
      case 'failed': return t('status_failed')
      default: return status
    }
  }

  return (
    <div className="flex flex-col h-full bg-brain-900/40 text-white">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-brain-600/30">
        <div className="flex items-center gap-2">
          <Edit3 className="w-5 h-5 text-purple-400" />
          <h2 className="text-lg font-semibold">{t('title')}</h2>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowHistory(!showHistory)}
            className={`p-2 rounded-lg transition-colors ${
              showHistory ? 'bg-purple-600' : 'hover:bg-brain-700/50'
            }`}
            title={t('history_title')}
          >
            <History className="w-4 h-4" />
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="p-2 hover:bg-brain-700/50 rounded-lg transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {showHistory ? (
          <div className="space-y-3">
            <h3 className="text-sm font-medium text-slate-400">{t('history_title')}</h3>
            {history.length === 0 ? (
              <p className="text-slate-500 text-sm">{t('no_operations')}</p>
            ) : (
              history.map((op, idx) => (
                <div
                  key={op.operation_id || idx}
                  className="p-3 bg-brain-800/40 rounded-lg border border-brain-600/30"
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className={`text-xs font-medium ${getStatusColor(op.status)}`}>
                      {getStatusText(op.status)}
                    </span>
                    <span className="text-xs text-slate-500">
                      {op.operation_id?.slice(-8)}
                    </span>
                  </div>
                  <p className="text-sm text-gray-300">{op.message}</p>
                </div>
              ))
            )}
          </div>
        ) : currentOperation ? (
          /* Текущая операция */
          <div className="space-y-4">
            {/* Статус */}
            <div className="flex items-center gap-2">
              <span className={`text-sm font-medium ${getStatusColor(currentOperation.status)}`}>
                {getStatusText(currentOperation.status)}
              </span>
              <button
                onClick={resetOperation}
                className="ml-auto text-slate-400 hover:text-white"
                title={t('new_command_tooltip')}
              >
                <RefreshCw className="w-4 h-4" />
              </button>
            </div>

            {/* Сообщение */}
            <div className="p-4 bg-brain-800/40 rounded-lg border border-brain-600/30">
              <p className="text-gray-200 whitespace-pre-wrap">{currentOperation.message}</p>
            </div>

            {/* Выбор из нескольких сущностей */}
            {currentOperation.status === 'pending_clarification' && 
             currentOperation.found_entities && 
             currentOperation.found_entities.length > 1 && (
              <div className="space-y-2">
                <p className="text-sm text-slate-400">{t('select_entity_label')}</p>
                {currentOperation.found_entities.map((entity, idx) => (
                  <button
                    key={entity.id}
                    onClick={() => handleSelectEntity(idx + 1)}
                    className="w-full p-3 bg-brain-800/40 hover:bg-brain-700/50 rounded-lg border border-brain-600/40 text-left transition-colors"
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-purple-400 font-mono">{idx + 1}.</span>
                      <span className="font-medium">{entity.name}</span>
                      <span className="text-xs text-slate-500 px-2 py-0.5 bg-brain-700/40 rounded">
                        {entity.type}
                      </span>
                    </div>
                    {entity.description && (
                      <p className="text-sm text-slate-400 mt-1 ml-6">
                        {entity.description.slice(0, 100)}...
                      </p>
                    )}
                  </button>
                ))}
              </div>
            )}

            {/* Предпросмотр изменений */}
            {currentOperation.status === 'pending_confirmation' && currentOperation.preview && (
              <div className="space-y-3">
                <div className="p-4 bg-yellow-900/20 border border-yellow-600/30 rounded-lg">
                  <div className="flex items-start gap-2">
                    <AlertCircle className="w-5 h-5 text-yellow-400 mt-0.5" />
                    <div>
                      <p className="font-medium text-yellow-400">{t('confirm_change_label')}</p>
                      <p className="text-gray-300 mt-1">
                        {currentOperation.preview.human_readable}
                      </p>
                    </div>
                  </div>
                </div>

                {/* Детали изменений */}
                {currentOperation.preview.changes && currentOperation.preview.changes.length > 0 && (
                  <div className="p-3 bg-brain-800/40 rounded-lg">
                    <p className="text-xs text-slate-400 mb-2">{t('details_label')}</p>
                    {currentOperation.preview.changes.map((change, idx) => (
                      <div key={idx} className="flex items-center gap-2 text-sm">
                        <span className="text-slate-500">{change.field}:</span>
                        <span className="text-red-400 line-through">{String(change.old_value)}</span>
                        <ChevronRight className="w-4 h-4 text-slate-500" />
                        <span className="text-green-400">{String(change.new_value)}</span>
                      </div>
                    ))}
                  </div>
                )}

                {/* Кнопки подтверждения */}
                <div className="flex gap-3">
                  <button
                    onClick={() => handleConfirm(true)}
                    disabled={loading}
                    className="flex-1 flex items-center justify-center gap-2 p-3 bg-green-600 hover:bg-green-700 disabled:opacity-50 rounded-lg transition-colors"
                  >
                    <Check className="w-4 h-4" />
                    {t('apply_button')}
                  </button>
                  <button
                    onClick={() => handleConfirm(false)}
                    disabled={loading}
                    className="flex-1 flex items-center justify-center gap-2 p-3 bg-brain-700/40 hover:bg-brain-600/60 disabled:opacity-50 rounded-lg transition-colors"
                  >
                    <X className="w-4 h-4" />
                    {t('cancel_button')}
                  </button>
                </div>
              </div>
            )}

            {/* Результат применения */}
            {currentOperation.status === 'applied' && (
              <div className="p-4 bg-green-900/20 border border-green-600/30 rounded-lg">
                <div className="flex items-center gap-2">
                  <Check className="w-5 h-5 text-green-400" />
                  <p className="text-green-400">{t('applied_success')}</p>
                </div>
              </div>
            )}

            {/* Ошибка */}
            {currentOperation.status === 'failed' && (
              <div className="p-4 bg-red-900/20 border border-red-600/30 rounded-lg">
                <div className="flex items-center gap-2">
                  <AlertCircle className="w-5 h-5 text-red-400" />
                  <p className="text-red-400">{currentOperation.error}</p>
                </div>
              </div>
            )}

            {/* Поле для уточнения */}
            {currentOperation.status === 'pending_clarification' && (
              <div className="flex gap-2">
                <input
                  ref={inputRef}
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      handleClarify(input)
                    }
                  }}
                  placeholder={t('clarify_placeholder')}
                  className="flex-1 p-3 bg-brain-800/40 border border-brain-600/40 rounded-lg focus:border-purple-500 focus:outline-none"
                />
                <button
                  onClick={() => handleClarify(input)}
                  disabled={loading || !input.trim()}
                  className="p-3 bg-purple-600 hover:bg-purple-700 disabled:opacity-50 rounded-lg transition-colors"
                >
                  <Send className="w-4 h-4" />
                </button>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-6">
            <div className="text-center py-6">
              <Edit3 className="w-12 h-12 text-purple-400 mx-auto mb-3" />
              <h3 className="text-lg font-medium mb-2">{t('intro_title')}</h3>
              <p className="text-slate-400 text-sm">
                {t('intro_description')}
              </p>
            </div>

            <div className="space-y-2">
              <p className="text-sm text-slate-400 flex items-center gap-1">
                <HelpCircle className="w-4 h-4" />
                {t('examples_label')}
              </p>
              {exampleCommands.map((cmd, idx) => (
                <button
                  key={idx}
                  onClick={() => handleSubmit(cmd.text)}
                  className="w-full flex items-center gap-3 p-3 bg-brain-800/40 hover:bg-brain-700/50 rounded-lg border border-brain-600/30 text-left transition-colors group"
                >
                  <cmd.icon className="w-4 h-4 text-purple-400" />
                  <span className="text-gray-300 group-hover:text-white">{cmd.text}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Input */}
      {!currentOperation && (
        <div className="p-4 border-t border-brain-600/30">
          <div className="flex gap-2">
            <input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  handleSubmit()
                }
              }}
              placeholder={t('main_input_placeholder')}
              className="flex-1 p-3 bg-brain-800/40 border border-brain-600/40 rounded-lg focus:border-purple-500 focus:outline-none"
              disabled={loading}
            />
            <button
              onClick={() => handleSubmit()}
              disabled={loading || !input.trim()}
              className="p-3 bg-purple-600 hover:bg-purple-700 disabled:opacity-50 rounded-lg transition-colors"
            >
              {loading ? (
                <RefreshCw className="w-5 h-5 animate-spin" />
              ) : (
                <Send className="w-5 h-5" />
              )}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}


