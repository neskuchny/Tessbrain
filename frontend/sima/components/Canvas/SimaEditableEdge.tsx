'use client'

/**
 * SimaEditableEdge — ребро канваса SIMA с inline-редактированием подписи.
 *
 * Двойной клик по подписи (или по ребру без подписи) → инпут прямо на
 * канвасе: пишем «что взять из этого кубика в тот», Enter/blur сохраняет
 * (store + PATCH /connections), Esc отменяет. Инспектор связи остаётся
 * альтернативным путём — здесь быстрый путь без ухода в панель.
 */
import { useState, useCallback } from 'react'
import {
  BaseEdge,
  EdgeLabelRenderer,
  getSmoothStepPath,
  type EdgeProps,
} from '@xyflow/react'
import { useSimaStore } from '@/sima/lib/store'
import { simaFetch } from '@/sima/lib/api'

export default function SimaEditableEdge({
  id, sourceX, sourceY, targetX, targetY,
  sourcePosition, targetPosition, style, markerEnd, markerStart, label,
}: EdgeProps) {
  const updateConnection = useSimaStore((s) => s.updateConnection)
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState<string>(typeof label === 'string' ? label : '')

  const [edgePath, labelX, labelY] = getSmoothStepPath({
    sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition,
  })

  const save = useCallback(async () => {
    setEditing(false)
    const v = value.trim()
    updateConnection(id, { label: v || null } as any)
    try {
      await simaFetch('/connections', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, label: v || null }),
      })
    } catch (e) {
      console.error('Failed to save edge label:', e)
    }
  }, [id, value, updateConnection])

  const startEdit = useCallback(() => {
    setValue(typeof label === 'string' ? label : '')
    setEditing(true)
  }, [label])

  return (
    <>
      <BaseEdge id={id} path={edgePath} style={style} markerEnd={markerEnd} markerStart={markerStart} />
      <EdgeLabelRenderer>
        <div
          className="nodrag nopan"
          style={{
            position: 'absolute',
            transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            pointerEvents: 'all',
          }}
        >
          {editing ? (
            <input
              autoFocus
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onBlur={save}
              onKeyDown={(e) => {
                if (e.key === 'Enter') save()
                if (e.key === 'Escape') setEditing(false)
              }}
              placeholder=""
              className="text-[11px] px-1.5 py-0.5 rounded border border-sima-primary/60 bg-sima-surface text-sima-text outline-none min-w-[80px]"
            />
          ) : label ? (
            <button
              onDoubleClick={startEdit}
              title=""
              className="text-[11px] px-1.5 py-0.5 rounded bg-sima-surface/90 border border-sima-border text-sima-textMuted hover:border-sima-primary/50 cursor-text"
            >
              {String(label)}
            </button>
          ) : (
            <button
              onDoubleClick={startEdit}
              className="text-[10px] w-4 h-4 rounded-full bg-sima-surface/80 border border-dashed border-sima-border text-sima-textDim hover:border-sima-primary/50 leading-none"
            >
              +
            </button>
          )}
        </div>
      </EdgeLabelRenderer>
    </>
  )
}
