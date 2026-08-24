'use client'

import { memo, useMemo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { cn } from '@/sima/lib/utils'
import { BLOCK_STATUS_COLORS, type BlockStatus } from '@/sima/types'

interface SimaBlockData {
  label: string
  name: string
  type: string
  color: string
  description?: string
  sourceTake?: string
  status?: string
  isMvp?: boolean
  layer: string
  estimatedComplexity: string
  hasChildren?: boolean
  [key: string]: unknown
}

const complexityColors: Record<string, string> = {
  low: 'bg-green-500/20 text-green-400',
  medium: 'bg-yellow-500/20 text-yellow-400',
  high: 'bg-red-500/20 text-red-400',
}

const buildTypeLabels = (t: (k: string) => string): Record<string, string> => ({
  core: t('type_core'),
  feature: t('type_feature'),
  integration: t('type_integration'),
  data: t('type_data'),
  agent: t('type_agent'),
})

function SimaBlockNode({ id, data, selected }: NodeProps) {
  const t = useTranslations('sima_canvas')
  const typeLabels = buildTypeLabels(t)
  const blockData = data as SimaBlockData
  const selectedBlockId = useSimaStore(s => s.selectedBlockId)
  const strategistReport = useSimaStore(s => s.strategistReport)
  const kanonVerdict = useSimaStore(s => s.kanonVerdicts[id])
  const isSelected = selected || selectedBlockId === id

  // Kanon-маркер: вердикт последней верификации (как канвас в atlas:
  // pass→green, fail/desync→red, inconclusive→amber, готов к handoff→violet)
  const kanonMarker = useMemo(() => {
    if (!kanonVerdict) return null
    if (kanonVerdict.cascadeState === 'desync' || kanonVerdict.verdict === 'fail')
      return { cls: 'bg-red-500', title: `Kanon: ${kanonVerdict.cascadeState === 'desync' ? 'desync' : 'fail'}` }
    if (kanonVerdict.verdict === 'pass')
      return { cls: 'bg-green-500', title: 'Kanon: pass' }
    if (kanonVerdict.verdict === 'inconclusive')
      return { cls: 'bg-amber-500', title: 'Kanon: inconclusive' }
    if (kanonVerdict.ready)
      return { cls: 'bg-violet-500', title: t('kanon_ready_handoff') }
    return null
  }, [kanonVerdict, t])

  const setBottomPanelTab = useSimaStore(s => s.setBottomPanelTab)
  const setBottomPanelOpen = useSimaStore(s => s.setBottomPanelOpen)

  // Найти инсайт стратега для этого блока
  const strategistMarker = useMemo(() => {
    if (!strategistReport) return null
    const insights: { type: string; icon: string; text: string }[] = []
    // Ищем во всех секциях инсайты, связанные с этим блоком
    const sections = Array.isArray(strategistReport.sections) ? strategistReport.sections : []
    for (const section of sections) {
      const sectionInsights = Array.isArray(section?.insights) ? section.insights : []
      for (const insight of sectionInsights) {
        if (insight?.relatedBlockIds?.includes(id)) {
          insights.push({ type: insight.type, icon: insight.icon, text: insight.title })
        }
      }
    }
    // Ищем в topActions
    const topActions = Array.isArray(strategistReport.topActions) ? strategistReport.topActions : []
    for (const action of topActions) {
      if (action.relatedBlockIds?.includes(id)) {
        insights.push({ type: 'action', icon: '🎯', text: action.action })
      }
    }
    if (insights.length === 0) return null
    return { icon: insights[0].icon, count: insights.length, title: insights.map(i => i.text).join('\n') }
  }, [strategistReport, id])

  return (
    <div
      className={cn(
        'group relative min-w-[180px] max-w-[240px] rounded-xl border-2 transition-all duration-200',
        'bg-sima-surface shadow-lg',
        isSelected
          ? 'border-opacity-100 shadow-xl scale-105'
          : 'border-opacity-40 hover:border-opacity-70'
      )}
      style={{
        borderColor: blockData.color,
        boxShadow: isSelected
          ? `0 0 20px ${blockData.color}33, 0 4px 20px rgba(0,0,0,0.4)`
          : '0 4px 12px rgba(0,0,0,0.3)',
      }}
    >
      {/* Kanon-вердикт: точка слева сверху */}
      {kanonMarker && (
        <span
          className={cn('absolute -top-1.5 -left-1.5 w-3 h-3 rounded-full z-10 shadow ring-2 ring-sima-surface', kanonMarker.cls)}
          title={kanonMarker.title}
        />
      )}

      {/* Маркер стратега — кликабельный */}
      {strategistMarker && (
        <button
          className="absolute -top-2.5 -right-2.5 w-6 h-6 rounded-full flex items-center justify-center text-[10px] z-10 shadow-lg border border-sima-border bg-sima-surface hover:scale-125 hover:shadow-xl transition-all cursor-pointer"
          title={strategistMarker.title}
          onClick={(e) => {
            e.stopPropagation()
            setBottomPanelTab('strategist')
            setBottomPanelOpen(true)
          }}
        >
          {strategistMarker.icon}
          {strategistMarker.count > 1 && (
            <span className="absolute -bottom-1 -right-1 w-3.5 h-3.5 rounded-full bg-red-500 text-white text-[8px] flex items-center justify-center font-bold">
              {strategistMarker.count}
            </span>
          )}
        </button>
      )}

      {/* Header */}
      <div
        className="px-3 py-1.5 rounded-t-[10px] flex items-center gap-2"
        style={{ backgroundColor: `${blockData.color}20` }}
      >
        {/* Статус-точка (Sima Remix): done/wip/q/idea */}
        {blockData.status && (
          <span
            className="w-2 h-2 rounded-full shrink-0"
            style={{ backgroundColor: BLOCK_STATUS_COLORS[blockData.status as BlockStatus] || BLOCK_STATUS_COLORS.idea }}
            title={String(blockData.status)}
          />
        )}
        <span
          className="text-xs font-bold px-1.5 py-0.5 rounded"
          style={{ backgroundColor: `${blockData.color}30`, color: blockData.color }}
        >
          {blockData.label}
        </span>
        <span className="text-[10px] text-sima-textDim">{typeLabels[blockData.type] || blockData.type}</span>
        {blockData.isMvp && (
          <span className="text-[9px] px-1 py-0.5 rounded bg-sima-primary/15 text-sima-primary font-semibold">MVP</span>
        )}
        {blockData.estimatedComplexity && (
          <span className={cn(
            'text-[10px] px-1.5 py-0.5 rounded ml-auto',
            complexityColors[blockData.estimatedComplexity] || complexityColors.medium
          )}>
            {blockData.estimatedComplexity === 'low' ? 'L' : blockData.estimatedComplexity === 'high' ? 'H' : 'M'}
          </span>
        )}
      </div>

      {/* Body */}
      <div className="px-3 py-2">
        <p className="text-sm font-medium text-sima-text leading-tight">{blockData.name}</p>
        {blockData.description && (
          <p className="text-[11px] text-sima-textDim mt-1 line-clamp-2 leading-relaxed">
            {blockData.description}
          </p>
        )}
        {/* «Что берём из источника» (Sima Remix .take): отдельная строка на
            теле кубика — что именно из него идёт в синтез. */}
        {blockData.sourceTake && (
          <p className="text-[11px] mt-1.5 pl-2 border-l-2 border-sima-primary/40 text-sima-primary/90 line-clamp-2 leading-relaxed">
            {blockData.sourceTake}
          </p>
        )}
      </div>

      {/* Layer badge + subschema indicator */}
      <div className="px-3 pb-2 flex items-center gap-2">
        <span className="text-[10px] text-sima-textDim bg-sima-bg px-2 py-0.5 rounded">
          {blockData.layer.toUpperCase()}
        </span>
        {blockData.hasChildren ? (
          <span
            className="text-[10px] px-2 py-0.5 rounded flex items-center gap-1 cursor-pointer hover:opacity-80 transition-opacity"
            style={{ backgroundColor: `${blockData.color}20`, color: blockData.color }}
            title={t("has_subschema_title")}
          >
            {t('has_subschema_label')}
          </span>
        ) : (
          <span
            className="text-[10px] px-2 py-0.5 rounded flex items-center gap-1 opacity-0 group-hover:opacity-60 transition-opacity cursor-pointer"
            style={{ color: blockData.color }}
            title={t("create_subschema_title")}
          >
            {t('create_subschema_label')}
          </span>
        )}
      </div>

      {/* Handles — входы слева, выходы справа */}
      <Handle
        type="target"
        position={Position.Left}
        id="target"
        style={{
          width: 12,
          height: 12,
          background: blockData.color,
          border: `2px solid ${blockData.color}`,
          borderRadius: '50%',
          left: -6,
        }}
      />
      <Handle
        type="source"
        position={Position.Right}
        id="source"
        style={{
          width: 12,
          height: 12,
          background: blockData.color,
          border: `2px solid ${blockData.color}`,
          borderRadius: '50%',
          right: -6,
        }}
      />

      {/* Дополнительные хэндлы сверху и снизу для гибкости */}
      <Handle
        type="target"
        position={Position.Top}
        id="target-top"
        style={{
          width: 10,
          height: 10,
          background: `${blockData.color}80`,
          border: `2px solid ${blockData.color}`,
          borderRadius: '50%',
          top: -5,
        }}
      />
      <Handle
        type="source"
        position={Position.Bottom}
        id="source-bottom"
        style={{
          width: 10,
          height: 10,
          background: `${blockData.color}80`,
          border: `2px solid ${blockData.color}`,
          borderRadius: '50%',
          bottom: -5,
        }}
      />
    </div>
  )
}

export default memo(SimaBlockNode)
