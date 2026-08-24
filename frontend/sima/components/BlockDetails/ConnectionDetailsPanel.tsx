'use client'

import { useState } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { X, RefreshCw, Sparkles, Loader2 } from 'lucide-react'
import type { ConnectionData } from '@/sima/types'
import { simaFetch } from '@/sima/lib/api'

const buildFields = (t: (k: string) => string): { key: keyof ConnectionData; label: string }[] => [
  { key: 'label', label: t('field_label_arrow') },
  { key: 'description', label: t('field_description') },
  { key: 'dataDescription', label: t('field_dataDescription') },
  { key: 'dataFormat', label: t('field_dataFormat') },
  { key: 'dataExample', label: t('field_dataExample') },
  { key: 'dataVolume', label: t('field_dataVolume') },
  { key: 'condition', label: t('field_condition') },
  { key: 'errorHandling', label: t('field_errorHandling') },
  { key: 'businessMeaning', label: t('field_businessMeaning') },
]

export default function ConnectionDetailsPanel() {
  const t = useTranslations('sima_connection_details')
  const fields = buildFields(t)
  const {
    selectedConnectionId,
    connections,
    blocks,
    project,
    updateConnection,
    setSelectedConnectionId,
    removeConnection,
  } = useSimaStore()

  const [editingField, setEditingField] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [generatingField, setGeneratingField] = useState<string | null>(null)

  const connection = connections.find((c) => c.id === selectedConnectionId)
  if (!connection) return null

  const fromBlock = blocks.find((b) => b.id === connection.fromBlockId)
  const toBlock = blocks.find((b) => b.id === connection.toBlockId)

  function startEdit(key: string, currentValue: string | null | undefined) {
    setEditingField(key)
    setEditValue(currentValue || '')
  }

  async function saveEdit(key: string) {
    setEditingField(null)
    updateConnection(connection!.id, { [key]: editValue } as Partial<ConnectionData>)
    
    try {
      await simaFetch('/connections', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: connection!.id, [key]: editValue }),
      })
    } catch (e) {
      console.error('Failed to save connection:', e)
    }
  }

  async function saveDirection(direction: 'one_way' | 'two_way' | 'to_task') {
    updateConnection(connection!.id, { direction } as Partial<ConnectionData>)
    try {
      await simaFetch('/connections', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: connection!.id, direction }),
      })
    } catch (e) {
      console.error('Failed to save connection direction:', e)
    }
  }

  async function handleAiFillAll() {
    if (!project || !connection || isGenerating) return
    setIsGenerating(true)
    try {
      const res = await simaFetch('/ai/generate-connection-description', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          connectionId: connection.id,
        }),
      })
      const data = await res.json()
      if (data.error) {
        console.error('AI fill error:', data.error)
        return
      }
      if (data.description && typeof data.description === 'object') {
        const updates = data.description as Partial<ConnectionData>
        updateConnection(connection.id, updates)
      }
    } catch (e) {
      console.error('AI fill failed:', e)
    } finally {
      setIsGenerating(false)
    }
  }

  async function handleAiFillField(fieldKey: string) {
    if (!project || !connection || generatingField) return
    setGeneratingField(fieldKey)
    try {
      const res = await simaFetch('/ai/generate-connection-description', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          connectionId: connection.id,
          sections: [fieldKey],
        }),
      })
      const data = await res.json()
      if (data.error) {
        console.error('AI fill field error:', data.error)
        return
      }
      if (data.description && typeof data.description === 'object') {
        const updates = data.description as Partial<ConnectionData>
        updateConnection(connection.id, updates)
      }
    } catch (e) {
      console.error('AI fill field failed:', e)
    } finally {
      setGeneratingField(null)
    }
  }

  async function handleDelete() {
    if (!connection) return
    try {
      await simaFetch(`/connections?id=${connection.id}`, { method: 'DELETE' })
      removeConnection(connection.id)
      setSelectedConnectionId(null)
    } catch (e) {
      console.error('Failed to delete connection:', e)
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-sima-border">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-sima-textDim uppercase tracking-wider">
            {t('connection_label')}
          </span>
          <button
            onClick={() => setSelectedConnectionId(null)}
            className="p-1 hover:bg-sima-surfaceLight rounded"
          >
            <X className="w-4 h-4 text-sima-textDim" />
          </button>
        </div>
        <div className="flex items-center gap-2">
          {fromBlock && (
            <span
              className="text-xs font-bold px-2 py-1 rounded"
              style={{ backgroundColor: `${fromBlock.color}30`, color: fromBlock.color }}
            >
              {fromBlock.label}
            </span>
          )}
          <span className="text-sima-textDim">→</span>
          {toBlock && (
            <span
              className="text-xs font-bold px-2 py-1 rounded"
              style={{ backgroundColor: `${toBlock.color}30`, color: toBlock.color }}
            >
              {toBlock.label}
            </span>
          )}
        </div>
        <p className="text-sm text-sima-text mt-1">
          {fromBlock?.name} → {toBlock?.name}
        </p>
        <div className="mt-2 flex items-center gap-2">
          <span className="text-[10px] px-2 py-0.5 rounded bg-sima-bg text-sima-textDim">
            {connection.type.replace('_', ' ').toUpperCase()}
          </span>
        </div>

        {/* Направление связи (Sima Remix): что и куда берётся */}
        <div className="mt-2 flex items-center gap-1">
          {([
            { id: 'one_way', icon: '→', label: t('direction_one_way') },
            { id: 'two_way', icon: '⇌', label: t('direction_two_way') },
            { id: 'to_task', icon: '⤳', label: t('direction_to_task') },
          ] as { id: 'one_way' | 'two_way' | 'to_task'; icon: string; label: string }[]).map((d) => {
            const active = (connection.direction || 'one_way') === d.id
            return (
              <button
                key={d.id}
                onClick={() => saveDirection(d.id)}
                title={d.label}
                className={
                  'flex items-center gap-1 px-2 py-1 rounded text-[11px] transition-colors ' +
                  (active
                    ? 'bg-sima-primary/15 text-sima-primary font-medium'
                    : 'text-sima-textDim hover:text-sima-textMuted hover:bg-sima-surfaceLight')
                }
              >
                <span>{d.icon}</span> {d.label}
              </button>
            )
          })}
        </div>

        {/* AI Fill All Button */}
        <button
          onClick={handleAiFillAll}
          disabled={isGenerating}
          className="mt-3 w-full flex items-center justify-center gap-2 py-2 px-3 bg-gradient-to-r from-sima-primary/20 to-purple-500/20 border border-sima-primary/30 text-sima-primary rounded-lg text-xs font-medium hover:from-sima-primary/30 hover:to-purple-500/30 transition-all disabled:opacity-50"
        >
          {isGenerating ? (
            <>
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              {t('generating')}
            </>
          ) : (
            <>
              <Sparkles className="w-3.5 h-3.5" />
              {t('fill_all_with_sima')}
            </>
          )}
        </button>
      </div>

      {/* Fields */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {fields.map((field) => {
          const value = connection[field.key] as string | null | undefined
          const isEditing = editingField === field.key
          const isFilled = !!value
          const isFieldGenerating = generatingField === field.key

          return (
            <div
              key={field.key}
              className={`rounded-lg border ${isFilled ? 'border-sima-border bg-sima-surfaceLight/50' : 'border-dashed border-sima-border/50 bg-sima-bg/50'}`}
            >
              <div className="px-3 py-2 flex items-center justify-between">
                <span className="text-xs font-medium text-sima-textMuted">{field.label}</span>
                <button
                  onClick={() => handleAiFillField(field.key)}
                  disabled={!!generatingField || isGenerating}
                  className="p-1 rounded hover:bg-sima-primary/10 transition-colors disabled:opacity-30"
                  title={t("fill_with_ai_title")}
                >
                  {isFieldGenerating ? (
                    <Loader2 className="w-3 h-3 text-sima-primary animate-spin" />
                  ) : (
                    <Sparkles className="w-3 h-3 text-sima-primary/60 hover:text-sima-primary" />
                  )}
                </button>
              </div>

              {isEditing ? (
                <div className="px-3 pb-3">
                  <textarea
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    className="w-full bg-sima-bg border border-sima-border rounded-lg px-3 py-2 text-sm text-sima-text resize-none min-h-[60px] focus:outline-none focus:border-sima-primary"
                    autoFocus
                  />
                  <div className="flex gap-2 mt-2">
                    <button
                      onClick={() => saveEdit(field.key)}
                      className="px-3 py-1 bg-sima-primary text-white text-xs rounded-lg"
                    >
                      {t('save_button')}
                    </button>
                    <button
                      onClick={() => setEditingField(null)}
                      className="px-3 py-1 bg-sima-border text-sima-textMuted text-xs rounded-lg"
                    >
                      {t('cancel_button')}
                    </button>
                  </div>
                </div>
              ) : isFilled ? (
                <div
                  className="px-3 pb-3 text-sm text-sima-text/90 whitespace-pre-wrap cursor-pointer hover:bg-sima-surfaceLight/30 transition-colors"
                  onClick={() => startEdit(field.key, value)}
                >
                  {value}
                </div>
              ) : (
                <div className="px-3 pb-3">
                  <button
                    onClick={() => startEdit(field.key, '')}
                    className="text-xs text-sima-textDim hover:text-sima-primary transition-colors"
                  >
                    {t('click_to_fill')}
                  </button>
                </div>
              )}
            </div>
          )
        })}

        {/* Delete button */}
        <button
          onClick={handleDelete}
          className="w-full mt-4 py-2 px-3 border border-red-500/30 text-red-400 rounded-lg text-xs font-medium hover:bg-red-500/10 transition-colors"
        >
          {t('delete_connection')}
        </button>
      </div>
    </div>
  )
}
