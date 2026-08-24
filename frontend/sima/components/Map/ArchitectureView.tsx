'use client'

/**
 * ArchitectureView — вид «Архитектура» карты продукта (эталон Sima Remix,
 * экран 3): блоки раскладываются по горизонтальным дорожкам-слоям, связи
 * рисуются между дорожками. Клик по блоку открывает тот же правый инспектор.
 *
 * Группировка: «по слоям» (problem→solution→mvp→medium→ideal — родная модель
 * SIMA), «по статусу» (готово/в работе/под вопросом/идея), «по пути»
 * (топологический порядок связей). Фильтр «только MVP». Раскладка
 * вычисляется — позиции блоков на канвасе НЕ трогаются (read-only вид).
 */
import { useMemo, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import {
  ReactFlow, Background, Controls, BackgroundVariant,
  type Node, type Edge, type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useSimaStore } from '@/sima/lib/store'
import SimaBlockNode from '@/sima/components/Canvas/SimaBlockNode'
import { BLOCK_STATUS_COLORS } from '@/sima/types'

type GroupMode = 'layers' | 'status' | 'path'

const CARD_W = 260
const CARD_GAP = 28
const LANE_H = 190
const LANE_PAD_X = 200 // левая рейка с подписью дорожки

const LAYER_ORDER = ['problem', 'solution', 'mvp', 'medium', 'ideal'] as const
const STATUS_ORDER = ['done', 'wip', 'q', 'idea'] as const

// Фон-дорожка: неинтерактивный узел под блоками
function LaneNode({ data }: NodeProps) {
  const d = data as { label: string; count: number; width: number; tint?: string }
  return (
    <div
      className="rounded-2xl border border-sima-border/60"
      style={{
        width: d.width, height: LANE_H - 16,
        background: d.tint || 'rgb(var(--sima-surface) / 0.45)',
      }}
    >
      <div className="absolute left-3 top-3 w-[170px]">
        <div className="text-[11px] font-semibold text-sima-textMuted leading-tight">
          {d.label}
        </div>
        <div className="text-[10px] text-sima-textDim mt-0.5">{d.count}</div>
      </div>
    </div>
  )
}

const nodeTypes = { simaBlock: SimaBlockNode, archLane: LaneNode }

export default function ArchitectureView({
  groupMode, mvpOnly,
}: { groupMode: GroupMode; mvpOnly: boolean }) {
  const t = useTranslations('sima_arch')
  const blocks = useSimaStore(s => s.blocks)
  const connections = useSimaStore(s => s.connections)
  const setSelectedBlockId = useSimaStore(s => s.setSelectedBlockId)
  const setSelectedConnectionId = useSimaStore(s => s.setSelectedConnectionId)
  const setRightPanelOpen = useSimaStore(s => s.setRightPanelOpen)

  const visible = useMemo(
    () => blocks.filter(b => !mvpOnly || (b as any).isMvp),
    [blocks, mvpOnly])

  // топологический порядок для «по пути» (Кан; циклы дообходятся хвостом)
  const topoRank = useMemo(() => {
    const ids = new Set(visible.map(b => b.id))
    const indeg: Record<string, number> = {}
    const out: Record<string, string[]> = {}
    visible.forEach(b => { indeg[b.id] = 0 })
    connections.forEach(c => {
      if (ids.has(c.fromBlockId) && ids.has(c.toBlockId)) {
        out[c.fromBlockId] = out[c.fromBlockId] || []
        out[c.fromBlockId].push(c.toBlockId)
        indeg[c.toBlockId] = (indeg[c.toBlockId] || 0) + 1
      }
    })
    const rank: Record<string, number> = {}
    let frontier = visible.filter(b => !indeg[b.id]).map(b => b.id)
    let level = 0
    const seen = new Set<string>()
    while (frontier.length) {
      const next: string[] = []
      for (const id of frontier) {
        if (seen.has(id)) continue
        seen.add(id); rank[id] = level
        for (const to of out[id] || []) {
          indeg[to] -= 1
          if (indeg[to] <= 0) next.push(to)
        }
      }
      frontier = next; level += 1
    }
    visible.forEach(b => { if (!(b.id in rank)) rank[b.id] = level })
    return rank
  }, [visible, connections])

  const lanes = useMemo(() => {
    if (groupMode === 'status') {
      return STATUS_ORDER.map(st => ({
        key: st,
        label: t(`status_${st}`),
        tint: `${BLOCK_STATUS_COLORS[st] || '#888'}0d`,
        items: visible.filter(b => ((b as any).status || 'idea') === st),
      }))
    }
    if (groupMode === 'path') {
      const maxRank = Math.max(0, ...visible.map(b => topoRank[b.id] || 0))
      return Array.from({ length: maxRank + 1 }, (_, lv) => ({
        key: `lv${lv}`,
        label: t('path_step', { n: lv + 1 }),
        tint: undefined as string | undefined,
        items: visible.filter(b => (topoRank[b.id] || 0) === lv),
      }))
    }
    return LAYER_ORDER.map(l => ({
      key: l,
      label: t(`layer_${l}`),
      tint: undefined as string | undefined,
      items: visible.filter(b => b.layer === l),
    }))
  }, [groupMode, visible, topoRank, t])

  const { nodes, edges } = useMemo(() => {
    const shown = lanes.filter(l => l.items.length > 0)
    const maxItems = Math.max(1, ...shown.map(l => l.items.length))
    const laneWidth = LANE_PAD_X + maxItems * (CARD_W + CARD_GAP) + 24

    const ns: Node[] = []
    shown.forEach((lane, li) => {
      ns.push({
        id: `lane-${lane.key}`,
        type: 'archLane',
        position: { x: 0, y: li * LANE_H },
        data: { label: lane.label, count: t('blocks_count', { n: lane.items.length }), width: laneWidth, tint: lane.tint },
        draggable: false, selectable: false, focusable: false,
        zIndex: -1,
      })
      lane.items.forEach((b, bi) => {
        ns.push({
          id: b.id,
          type: 'simaBlock',
          position: {
            x: LANE_PAD_X + bi * (CARD_W + CARD_GAP),
            y: li * LANE_H + 22,
          },
          draggable: false,
          data: {
            label: b.label, name: b.name, type: b.type, color: b.color,
            description: b.description, layer: b.layer,
            status: (b as any).status, isMvp: (b as any).isMvp,
            sourceTake: (b as any).sourceTake,
            estimatedComplexity: b.estimatedComplexity,
            hasChildren: b.hasChildren,
          },
        })
      })
    })

    const shownIds = new Set(visible.map(b => b.id))
    const es: Edge[] = connections
      .filter(c => shownIds.has(c.fromBlockId) && shownIds.has(c.toBlockId))
      .map(c => ({
        id: c.id, source: c.fromBlockId, target: c.toBlockId,
        type: 'smoothstep',
        label: c.label || undefined,
        style: { stroke: 'rgb(var(--sima-text-dim) / 0.55)', strokeWidth: 1.2 },
        labelStyle: { fill: 'rgb(var(--sima-text-muted))', fontSize: 10 },
        labelBgStyle: { fill: 'rgb(var(--sima-surface))', fillOpacity: 0.9 },
      }))
    return { nodes: ns, edges: es }
  }, [lanes, connections, visible, t])

  const onNodeClick = useCallback((_: unknown, node: Node) => {
    if (String(node.id).startsWith('lane-')) return
    setSelectedConnectionId(null)
    setSelectedBlockId(node.id)
    setRightPanelOpen(true)
  }, [setSelectedBlockId, setSelectedConnectionId, setRightPanelOpen])

  if (visible.length === 0) {
    return (
      <div className="h-full flex items-center justify-center p-8 text-center">
        <p className="text-[13px] text-sima-textMuted max-w-md">
          {mvpOnly ? t('empty_mvp') : t('empty_hint')}
        </p>
      </div>
    )
  }

  return (
    <div className="h-full sima-workspace">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={onNodeClick}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        fitView
        fitViewOptions={{ padding: 0.15, maxZoom: 0.9 }}
        minZoom={0.2}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
