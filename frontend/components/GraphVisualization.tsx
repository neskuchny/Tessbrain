'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'
import { RefreshCw, ZoomIn, ZoomOut, Maximize2, Info } from 'lucide-react'
import Graph3DView from '@/components/Graph3DView'
import OrgGraphView from '@/components/OrgGraphView'
import { authFetch } from '@/lib/authFetch'

interface GraphNode {
  id: string
  label: string
  title: string
  group: string
  color: string
  properties?: Record<string, any>
}

interface GraphEdge {
  id?: string
  from: string
  to: string
  label: string
  arrows: string
}

interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  stats: {
    node_count: number
    edge_count: number
  }
  color_legend?: Record<string, string>
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

interface GraphVisualizationProps {
  userId?: string | null
  focusRequest?: GraphFocusRequest | null
  traceRequest?: GraphTraceRequest | null
  /** Переход на карточку сущности (Object 360) из 3D-графа. */
  onOpenEntity?: (type: string, id: string, label: string) => void
}

interface GraphNeighborhoodItem {
  node: GraphNode
  relationLabels: string[]
}

interface GraphNeighborhoodState {
  centerId: string
  neighborIds: string[]
  edgeIds: string[]
  neighbors: GraphNeighborhoodItem[]
}

interface GraphTraceRenderState {
  matchedTargets: Array<{
    traceNodeKey?: string
    entityId: string
    label?: string
    sourceType?: string
    role?: string
    isAnchor?: boolean
    pathOrder?: number
    retrievalStage?: string
    retrievalSources?: string[]
    node: GraphNode
  }>
  unmatchedTargets: Array<{
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
  edgeIds: string[]
  reasoningSteps: string[]
  title?: string
  segments: Array<{
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
  missingLinks: Array<{
    fromLabel?: string
    toLabel?: string
    reason?: string
  }>
}

function normalizeGraphText(value: unknown): string {
  return String(value ?? '').trim().toLowerCase()
}

function getTraceStageLabel(stage?: string): string {
  const normalized = normalizeGraphText(stage)
  switch (normalized) {
    case 'graph':
      return 'graph'
    case 'vector':
      return 'vector'
    case 'bm25':
      return 'bm25'
    case 'snapshot':
      return 'snapshot'
    case 'document':
      return 'document'
    case 'reasoning_step':
      return 'reasoning'
    default:
      return normalized || 'unknown'
  }
}

function getTraceStageClasses(stage?: string): string {
  const normalized = normalizeGraphText(stage)
  switch (normalized) {
    case 'graph':
      return 'bg-cyan-500/15 text-cyan-300 border border-cyan-400/30'
    case 'vector':
      return 'bg-violet-500/15 text-violet-300 border border-violet-400/30'
    case 'bm25':
      return 'bg-blue-500/15 text-blue-300 border border-blue-400/30'
    case 'snapshot':
      return 'bg-emerald-500/15 text-emerald-300 border border-emerald-400/30'
    case 'document':
      return 'bg-amber-500/15 text-amber-300 border border-amber-400/30'
    case 'reasoning_step':
      return 'bg-slate-500/15 text-slate-300 border border-slate-400/30'
    default:
      return 'bg-brain-800/70 text-brain-300 border border-brain-700/60'
  }
}

function findMatchingGraphNode(
  nodes: GraphNode[],
  focusTarget: { entityId: string; label?: string; traceNodeKey?: string }
): GraphNode | undefined {
  const targetId = normalizeGraphText(focusTarget.entityId)
  const targetLabel = normalizeGraphText(focusTarget.label)
  const traceNodeKey = normalizeGraphText(focusTarget.traceNodeKey)

  return nodes.find((node) => {
    const properties = node.properties || {}
    const aliasesRaw = properties.aliases
    const aliases = Array.isArray(aliasesRaw)
      ? aliasesRaw
      : typeof aliasesRaw === 'string' && aliasesRaw.trim()
        ? aliasesRaw.split(',').map((item) => item.trim())
        : []

    const candidates = [
      node.id,
      node.label,
      node.title,
      properties.id,
      properties.entity_id,
      properties.person_id,
      properties.project_id,
      properties.name,
      properties.normalized_name,
      ...aliases,
    ]
      .map(normalizeGraphText)
      .filter(Boolean)

    if (traceNodeKey && candidates.includes(traceNodeKey)) return true
    if (candidates.includes(targetId)) return true
    if (targetLabel && candidates.includes(targetLabel)) return true
    if (traceNodeKey && candidates.some((candidate) => candidate.includes(traceNodeKey) || traceNodeKey.includes(candidate))) {
      return true
    }
    if (targetId && candidates.some((candidate) => candidate.includes(targetId) || targetId.includes(candidate))) {
      return true
    }
    if (targetLabel && candidates.some((candidate) => candidate.includes(targetLabel) || targetLabel.includes(candidate))) {
      return true
    }
    return false
  })
}

function buildTraceRenderState(
  nodes: GraphNode[],
  edges: GraphEdge[],
  traceRequest: GraphTraceRequest
): GraphTraceRenderState {
  const matchedTargets: GraphTraceRenderState['matchedTargets'] = []
  const unmatchedTargets: GraphTraceRenderState['unmatchedTargets'] = []

  for (const target of traceRequest.targets) {
    const node = findMatchingGraphNode(nodes, target)
    if (node) {
      matchedTargets.push({ ...target, node })
    } else {
      unmatchedTargets.push(target)
    }
  }

  const traceKeyToGraphNodeId = new Map<string, string>()
  for (const target of matchedTargets) {
    const traceNodeKey = target.traceNodeKey || target.entityId
    if (traceNodeKey) {
      traceKeyToGraphNodeId.set(traceNodeKey, target.node.id)
    }
  }

  let edgeIds: string[] = []
  if (traceRequest.edges) {
    edgeIds = traceRequest.edges
      .flatMap((traceEdge) => {
        const fromGraphId = traceKeyToGraphNodeId.get(traceEdge.fromKey)
        const toGraphId = traceKeyToGraphNodeId.get(traceEdge.toKey)
        if (!fromGraphId || !toGraphId) return []

        return edges
          .filter((edge) => (
            edge.from === fromGraphId &&
            edge.to === toGraphId &&
            (!traceEdge.label || edge.label === traceEdge.label)
          ))
          .map((edge) => edge.id || '')
          .filter(Boolean)
      })
  } else {
    const matchedNodeIds = new Set(matchedTargets.map((item) => item.node.id))
    edgeIds = edges
      .filter((edge) => matchedNodeIds.has(edge.from) && matchedNodeIds.has(edge.to))
      .map((edge) => edge.id || '')
      .filter(Boolean)
  }

  edgeIds = Array.from(new Set(edgeIds))

  return {
    matchedTargets,
    unmatchedTargets,
    edgeIds,
    reasoningSteps: traceRequest.reasoningSteps || [],
    title: traceRequest.title,
    segments: traceRequest.segments || [],
    missingLinks: traceRequest.missingLinks || [],
  }
}

export default function GraphVisualization({ userId, focusRequest, traceRequest, onOpenEntity }: GraphVisualizationProps) {
  const t = useTranslations('graph_visualization')
  const containerRef = useRef<HTMLDivElement>(null)
  const networkRef = useRef<any>(null)
  const nodesDataRef = useRef<any>(null)
  const edgesDataRef = useRef<any>(null)
  const graphNodesRef = useRef<GraphNode[]>([])
  const graphEdgesRef = useRef<GraphEdge[]>([])
  const autoExpandedRequestRef = useRef<number | null>(null)
  const autoExpandedTraceRequestRef = useRef<number | null>(null)
  const [graphData, setGraphData] = useState<GraphData | null>(null)
  const [filteredData, setFilteredData] = useState<GraphData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null)
  const [typeFilter, setTypeFilter] = useState<string>('all')
  const [nodeLimit, setNodeLimit] = useState<number>(1500)
  // Оргрежим: показать только людей/команды/отделы и связи подчинения
  // (REPORTS_TO/MEMBER_OF/LEADS) — чистая оргсхема вместо всего графа.
  const [orgMode, setOrgMode] = useState<boolean>(false)
  // 3D-вид компании — ОПЦИЯ по кнопке, не дефолт: пользователи начинали
  // с непонятной 3D-сцены вместо читаемого 2D-графа/оргсхемы.
  const [view3D, setView3D] = useState<boolean>(false)
  const [networkReady, setNetworkReady] = useState(false)
  const [focusStatus, setFocusStatus] = useState<string | null>(null)
  const [neighborhoodState, setNeighborhoodState] = useState<GraphNeighborhoodState | null>(null)
  const [traceState, setTraceState] = useState<GraphTraceRenderState | null>(null)
  
  // Get unique node types for filter
  const nodeTypes = graphData?.nodes ? [...new Set(graphData.nodes.map(n => n.group))].sort() : []
  
  // Apply filter
  useEffect(() => {
    if (!graphData) { setFilteredData(null); return }
    if (typeFilter === 'all') { setFilteredData(graphData); return }
    const filteredNodes = graphData.nodes.filter(n => n.group === typeFilter)
    const nodeIds = new Set(filteredNodes.map(n => n.id))
    const filteredEdges = graphData.edges.filter(e => nodeIds.has(e.from) || nodeIds.has(e.to))
    setFilteredData({
      nodes: filteredNodes,
      edges: filteredEdges,
      stats: { node_count: filteredNodes.length, edge_count: filteredEdges.length },
      color_legend: graphData.color_legend,
    })
  }, [graphData, typeFilter])

  const focusNodeInNetwork = useCallback((nodeId: string, scale: number = 1.4) => {
    if (!networkRef.current) return
    networkRef.current.selectNodes([nodeId])
    networkRef.current.focus(nodeId, {
      scale,
      animation: {
        duration: 700,
        easingFunction: 'easeInOutQuad',
      },
    })
  }, [])

  const applyNeighborhoodHighlight = useCallback((centerNode: GraphNode | null) => {
    const nodesDataset = nodesDataRef.current
    const edgesDataset = edgesDataRef.current
    const nodes = graphNodesRef.current
    const edges = graphEdgesRef.current

    if (!nodesDataset || !edgesDataset) return

    if (!centerNode) {
      nodesDataset.update(
        nodes.map((node) => ({
          id: node.id,
          color: {
            background: node.color,
            border: node.color,
            highlight: {
              background: node.color,
              border: '#ffffff',
            },
          },
          font: { color: '#ffffff', size: 12 },
          size: 20,
          borderWidth: 2,
        }))
      )
      edgesDataset.update(
        edges.map((edge) => ({
          id: edge.id,
          color: { color: 'rgba(255,255,255,0.3)', highlight: '#0c8ce7' },
          font: { color: 'rgba(255,255,255,0.5)', size: 10 },
          width: 1,
        }))
      )
      setNeighborhoodState(null)
      return
    }

    const centerNodeInGraph = nodes.find((node) => node.id === centerNode.id)
    if (!centerNodeInGraph) {
      nodesDataset.update(
        nodes.map((node) => ({
          id: node.id,
          color: {
            background: node.color,
            border: node.color,
            highlight: {
              background: node.color,
              border: '#ffffff',
            },
          },
          font: { color: '#ffffff', size: 12 },
          size: 20,
          borderWidth: 2,
        }))
      )
      edgesDataset.update(
        edges.map((edge) => ({
          id: edge.id,
          color: { color: 'rgba(255,255,255,0.3)', highlight: '#0c8ce7' },
          font: { color: 'rgba(255,255,255,0.5)', size: 10 },
          width: 1,
        }))
      )
      setNeighborhoodState(null)
      return
    }

    const nodeMap = new Map(nodes.map((node) => [node.id, node]))
    const neighborMap = new Map<string, GraphNeighborhoodItem>()
    const highlightedEdgeIds = new Set<string>()

    for (const edge of edges) {
      const isConnected = edge.from === centerNode.id || edge.to === centerNode.id
      if (!isConnected) continue

      if (edge.id) {
        highlightedEdgeIds.add(edge.id)
      }
      const neighborId = edge.from === centerNode.id ? edge.to : edge.from
      const neighborNode = nodeMap.get(neighborId)
      if (!neighborNode) continue

      const existing = neighborMap.get(neighborId)
      if (existing) {
        if (!existing.relationLabels.includes(edge.label)) {
          existing.relationLabels.push(edge.label)
        }
      } else {
        neighborMap.set(neighborId, {
          node: neighborNode,
          relationLabels: edge.label ? [edge.label] : [],
        })
      }
    }

    const neighborIds = Array.from(neighborMap.keys())
    const neighborIdSet = new Set(neighborIds)

    nodesDataset.update(
      nodes.map((node) => {
        const isCenter = node.id === centerNode.id
        const isNeighbor = neighborIdSet.has(node.id)

        if (isCenter) {
          return {
            id: node.id,
            color: {
              background: node.color,
              border: '#ffffff',
              highlight: {
                background: node.color,
                border: '#ffffff',
              },
            },
            font: { color: '#ffffff', size: 14 },
            size: 32,
            borderWidth: 4,
          }
        }

        if (isNeighbor) {
          return {
            id: node.id,
            color: {
              background: node.color,
              border: '#a855f7',
              highlight: {
                background: node.color,
                border: '#ffffff',
              },
            },
            font: { color: '#f8fafc', size: 12 },
            size: 24,
            borderWidth: 3,
          }
        }

        return {
          id: node.id,
          color: {
            background: 'rgba(96,125,139,0.18)',
            border: 'rgba(148,163,184,0.25)',
            highlight: {
              background: node.color,
              border: '#ffffff',
            },
          },
          font: { color: 'rgba(255,255,255,0.35)', size: 11 },
          size: 12,
          borderWidth: 1,
        }
      })
    )

    edgesDataset.update(
      edges.map((edge) => {
        const isHighlighted = edge.id ? highlightedEdgeIds.has(edge.id) : false
        return {
          id: edge.id,
          color: isHighlighted
            ? { color: '#a855f7', highlight: '#c084fc' }
            : { color: 'rgba(148,163,184,0.08)', highlight: '#0c8ce7' },
          font: { color: isHighlighted ? 'rgba(216,180,254,0.95)' : 'rgba(255,255,255,0.18)', size: 10 },
          width: isHighlighted ? 3 : 1,
        }
      })
    )

    setNeighborhoodState({
      centerId: centerNode.id,
      neighborIds,
      edgeIds: Array.from(highlightedEdgeIds),
      neighbors: Array.from(neighborMap.values()).sort((a, b) => a.node.label.localeCompare(b.node.label)),
    })
  }, [])

  const applyTraceHighlight = useCallback((
    currentTrace: GraphTraceRenderState | null,
    activeNodeId?: string | null
  ) => {
    const nodesDataset = nodesDataRef.current
    const edgesDataset = edgesDataRef.current
    const nodes = graphNodesRef.current
    const edges = graphEdgesRef.current

    if (!nodesDataset || !edgesDataset) return

    if (!currentTrace || currentTrace.matchedTargets.length === 0) {
      applyNeighborhoodHighlight(selectedNode)
      return
    }

    const matchedMap = new Map(
      currentTrace.matchedTargets.map((item) => [
        item.node.id,
        { role: item.role || 'supporting', sourceType: item.sourceType || '' },
      ])
    )
    const matchedNodeIds = new Set(currentTrace.matchedTargets.map((item) => item.node.id))
    const highlightedEdgeIds = new Set(currentTrace.edgeIds)
    const fallbackActiveNodeId = currentTrace.matchedTargets[0]?.node.id || ''
    const selectedTraceNodeId = activeNodeId && matchedNodeIds.has(activeNodeId)
      ? activeNodeId
      : fallbackActiveNodeId

    nodesDataset.update(
      nodes.map((node) => {
        const traceMeta = matchedMap.get(node.id)
        const isSelectedTraceNode = node.id === selectedTraceNodeId
        const isMatched = Boolean(traceMeta)

        if (isSelectedTraceNode) {
          return {
            id: node.id,
            color: {
              background: node.color,
              border: '#ffffff',
              highlight: {
                background: node.color,
                border: '#ffffff',
              },
            },
            font: { color: '#ffffff', size: 14 },
            size: 34,
            borderWidth: 4,
          }
        }

        if (isMatched) {
          const borderColor = traceMeta?.role === 'direct'
            ? '#f59e0b'
            : traceMeta?.role === 'anchor'
              ? '#22c55e'
              : '#60a5fa'
          return {
            id: node.id,
            color: {
              background: node.color,
              border: borderColor,
              highlight: {
                background: node.color,
                border: '#ffffff',
              },
            },
            font: { color: '#f8fafc', size: 12 },
            size: 24,
            borderWidth: 3,
          }
        }

        return {
          id: node.id,
          color: {
            background: 'rgba(96,125,139,0.18)',
            border: 'rgba(148,163,184,0.20)',
            highlight: {
              background: node.color,
              border: '#ffffff',
            },
          },
          font: { color: 'rgba(255,255,255,0.28)', size: 11 },
          size: 11,
          borderWidth: 1,
        }
      })
    )

    edgesDataset.update(
      edges.map((edge) => {
        const isHighlighted = edge.id ? highlightedEdgeIds.has(edge.id) : false
        return {
          id: edge.id,
          color: isHighlighted
            ? { color: '#c084fc', highlight: '#e9d5ff' }
            : { color: 'rgba(148,163,184,0.07)', highlight: '#0c8ce7' },
          font: { color: isHighlighted ? 'rgba(233,213,255,0.95)' : 'rgba(255,255,255,0.16)', size: 10 },
          width: isHighlighted ? 3 : 1,
        }
      })
    )

    setNeighborhoodState(null)
  }, [applyNeighborhoodHighlight, selectedNode])

  const fetchGraph = async () => {
    setLoading(true)
    setError(null)
    
    try {
      if (!userId) {
        setGraphData({ nodes: [], edges: [], stats: { node_count: 0, edge_count: 0 } })
        return
      }
      const userParam = userId ? `&user_id=${userId}` : ''
      const endpoint = orgMode
        ? `/api/v1/entities/org-graph?limit=${nodeLimit}${userParam}`
        : `/api/v1/entities/graph?limit=${nodeLimit}${userParam}`
      // authFetch: с Bearer анти-IDOR на бэке реально сверяет user_id с токеном
      const res = await authFetch(endpoint)
      if (res.ok) {
        const data = await res.json()
        // нормализуем: ответ-отказ/ошибка может прийти без nodes/edges —
        // без этого graphData.nodes.map падал «Cannot read properties of undefined»
        setGraphData({
          ...data,
          nodes: Array.isArray(data?.nodes) ? data.nodes : [],
          edges: Array.isArray(data?.edges) ? data.edges : [],
        })
        if (data.error) {
          setError(data.error)
        }
      } else {
        throw new Error('Failed to fetch graph')
      }
    } catch (e: any) {
      console.error('Graph fetch error:', e)
      setError(e.message || t('err_load_graph'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchGraph()
  }, [userId, nodeLimit, orgMode])

  useEffect(() => {
    if (!filteredData || !containerRef.current || filteredData.nodes.length === 0) return
    setNetworkReady(false)

    // Динамический импорт vis-network
    import('vis-network/standalone').then(({ Network, DataSet }) => {
      const enrichedEdges = filteredData.edges.map((e, i) => ({
        ...e,
        id: e.id || `edge_${i}`,
      }))

      const nodes = new DataSet(
        filteredData.nodes.map((n) => ({
          id: n.id,
          label: n.label,
          title: n.title,
          group: n.group,
          color: {
            background: n.color,
            border: n.color,
            highlight: {
              background: n.color,
              border: '#ffffff',
            },
          },
          font: { color: '#ffffff', size: 12 },
          shape: 'dot',
          size: 20,
        }))
      )

      const edges = new DataSet(
        enrichedEdges.map((e) => ({
          id: e.id,
          from: e.from,
          to: e.to,
          label: e.label,
          arrows: e.arrows,
          color: { color: 'rgba(255,255,255,0.3)', highlight: '#0c8ce7' },
          font: { color: 'rgba(255,255,255,0.5)', size: 10 },
        }))
      )

      graphNodesRef.current = filteredData.nodes
      graphEdgesRef.current = enrichedEdges
      nodesDataRef.current = nodes
      edgesDataRef.current = edges

      const options = {
        nodes: {
          borderWidth: 2,
          shadow: true,
        },
        edges: {
          width: 1,
          smooth: {
            enabled: true,
            type: 'continuous',
            roundness: 0.5,
          },
        },
        physics: {
          enabled: true,
          solver: 'forceAtlas2Based',
          forceAtlas2Based: {
            gravitationalConstant: -50,
            centralGravity: 0.01,
            springLength: 100,
            springConstant: 0.08,
          },
          stabilization: {
            enabled: true,
            iterations: 100,
            updateInterval: 25,
          },
        },
        interaction: {
          hover: true,
          tooltipDelay: 200,
          zoomView: true,
          dragView: true,
        },
        layout: {
          improvedLayout: true,
        },
      }

      // import() асинхронный: к этому моменту компонент мог перерисоваться в
      // пустое состояние (граф 0 узлов) и контейнер исчез — крашило
      // «Cannot read properties of null (hasChildNodes)» и оверлей ошибки
      // блокировал ВСЕ клики на странице
      if (!containerRef.current) return
      const network = new Network(containerRef.current, { nodes, edges }, options)
      networkRef.current = network

      // Обработка клика на узел
      network.on('click', (params: any) => {
        if (params.nodes.length > 0) {
          const nodeId = params.nodes[0]
          const node = filteredData?.nodes.find((n) => n.id === nodeId)
          setSelectedNode(node || null)
          if (node) {
            setFocusStatus(t('status_node_selected', { label: node.label }))
          }
        } else {
          setSelectedNode(null)
          setFocusStatus(t('status_highlight_reset'))
        }
      })

      // Стабилизация
      network.once('stabilizationIterationsDone', () => {
        network.setOptions({ physics: { enabled: false } })
        setNetworkReady(true)
      })
    })

    return () => {
      setNetworkReady(false)
      nodesDataRef.current = null
      edgesDataRef.current = null
      graphNodesRef.current = []
      graphEdgesRef.current = []
      if (networkRef.current) {
        networkRef.current.destroy()
      }
    }
  }, [filteredData])

  // 2D vis-network инициализируется, пока его накрывает 3D-оверлей (скрытый
  // размер) → при переключении на «2D-граф» холст оставался пустым/съехавшим.
  // При показе 2D форсим перерисовку и подгонку вида.
  useEffect(() => {
    if (view3D) return
    const net = networkRef.current
    if (!net) return
    const raf = requestAnimationFrame(() => {
      try {
        net.redraw()
        net.fit({ animation: false })
      } catch {
        /* vis-network ещё не готов — безопасно игнорируем */
      }
    })
    return () => cancelAnimationFrame(raf)
  }, [view3D])

  useEffect(() => {
    if (!focusRequest) return
    autoExpandedRequestRef.current = null
    setTraceState(null)
    setSelectedNode(null)
    setFocusStatus(t('status_searching_node', { value: focusRequest.label || focusRequest.entityId }))
    if (typeFilter !== 'all') {
      setTypeFilter('all')
    }
  }, [focusRequest?.requestId])

  useEffect(() => {
    if (!traceRequest) return
    autoExpandedTraceRequestRef.current = null
    setTraceState(null)
    setSelectedNode(null)
    setFocusStatus(t('status_collecting_trace', { count: traceRequest.targets.length }))
    if (typeFilter !== 'all') {
      setTypeFilter('all')
    }
  }, [traceRequest?.requestId])

  useEffect(() => {
    if (!focusRequest || !graphData || !filteredData || !networkReady || !networkRef.current) {
      return
    }

    const currentNode = findMatchingGraphNode(filteredData.nodes, focusRequest)
    if (currentNode) {
      focusNodeInNetwork(currentNode.id, 1.4)
      setSelectedNode(currentNode)
      setFocusStatus(t('status_node_highlighted', { label: currentNode.label }))
      return
    }

    const nodeInFullGraph = findMatchingGraphNode(graphData.nodes, focusRequest)
    if (!nodeInFullGraph && nodeLimit < 3000 && autoExpandedRequestRef.current !== focusRequest.requestId) {
      autoExpandedRequestRef.current = focusRequest.requestId
      setFocusStatus(t('status_node_not_found_loading'))
      setNodeLimit(3000)
      return
    }

    if (!nodeInFullGraph) {
      setFocusStatus(t('status_node_not_found', { value: focusRequest.label || focusRequest.entityId }))
    }
  }, [focusRequest, graphData, filteredData, networkReady, nodeLimit, focusNodeInNetwork])

  useEffect(() => {
    if (!traceRequest || !graphData || !filteredData || !networkReady || !networkRef.current) {
      return
    }

    const nextTraceState = buildTraceRenderState(filteredData.nodes, graphEdgesRef.current, traceRequest)
    const hasUnmatched = nextTraceState.unmatchedTargets.length > 0

    if (hasUnmatched && nodeLimit < 3000 && autoExpandedTraceRequestRef.current !== traceRequest.requestId) {
      autoExpandedTraceRequestRef.current = traceRequest.requestId
      setFocusStatus(t('status_trace_partial_loading'))
      setNodeLimit(3000)
      return
    }

    if (nextTraceState.matchedTargets.length === 0) {
      setTraceState(nextTraceState)
      setFocusStatus(t('status_trace_build_failed'))
      return
    }

    setTraceState(nextTraceState)
    const primaryNode = nextTraceState.matchedTargets[0].node
    setSelectedNode(primaryNode)
    focusNodeInNetwork(primaryNode.id, 1.25)
    setFocusStatus(
      t('status_trace_built', { nodes: nextTraceState.matchedTargets.length, edges: nextTraceState.edgeIds.length })
    )
  }, [traceRequest, graphData, filteredData, networkReady, nodeLimit, focusNodeInNetwork])

  useEffect(() => {
    if (!networkReady) return
    if (traceState) {
      applyTraceHighlight(traceState, selectedNode?.id)
      return
    }
    applyNeighborhoodHighlight(selectedNode)
  }, [selectedNode, networkReady, traceState, applyNeighborhoodHighlight, applyTraceHighlight])

  const handleZoomIn = () => {
    if (networkRef.current) {
      const scale = networkRef.current.getScale()
      networkRef.current.moveTo({ scale: scale * 1.3 })
    }
  }

  const handleZoomOut = () => {
    if (networkRef.current) {
      const scale = networkRef.current.getScale()
      networkRef.current.moveTo({ scale: scale / 1.3 })
    }
  }

  const handleFit = () => {
    if (networkRef.current) {
      networkRef.current.fit()
    }
  }

  const handleClearHighlight = () => {
    setTraceState(null)
    setSelectedNode(null)
    setFocusStatus(t('status_highlight_reset'))
    if (networkRef.current) {
      networkRef.current.unselectAll()
      networkRef.current.fit()
    }
  }

  const handleFocusNeighbor = (node: GraphNode) => {
    setSelectedNode(node)
    focusNodeInNetwork(node.id, 1.3)
    setFocusStatus(t('status_jump_neighbor', { label: node.label }))
  }

  const handleFocusTraceNode = (node: GraphNode) => {
    setSelectedNode(node)
    focusNodeInNetwork(node.id, 1.25)
    setFocusStatus(t('status_jump_trace', { label: node.label }))
  }

  return (
    <div className="card h-[600px] flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between p-4 border-b border-brain-600/20">
        <h2 className="text-lg font-semibold">{t('title')}</h2>
        
        <div className="flex items-center gap-2">
          {/* Node limit slider */}
          <div className="flex items-center gap-1">
            <span className="text-xs text-brain-500">{t('limit_label')}</span>
            <input
              type="range"
              min={100}
              max={3000}
              step={100}
              value={nodeLimit}
              onChange={(e) => setNodeLimit(Number(e.target.value))}
              className="w-20 h-1 accent-brain-500"
              title={t('limit_button_title', { count: nodeLimit })}
            />
            <span className="text-xs text-brain-400 w-10">{nodeLimit}</span>
          </div>
          
          {/* Type filter */}
          {nodeTypes.length > 0 && (
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="px-2 py-1 bg-brain-800 border border-brain-600 text-brain-200 rounded text-xs"
            >
              <option value="all">{t('filter_all_types', { count: graphData?.stats.node_count ?? 0 })}</option>
              {nodeTypes.map(t => (
                <option key={t} value={t}>{t} ({graphData?.nodes.filter(n => n.group === t).length})</option>
              ))}
            </select>
          )}
          
          {filteredData && (
            <span className="text-sm text-brain-400 mr-2">
              {t('graph_summary', { nodes: filteredData.stats.node_count, edges: filteredData.stats.edge_count })}
            </span>
          )}
          {focusStatus && (
            <span className="text-xs text-purple-300 max-w-[280px] truncate" title={focusStatus}>
              {focusStatus}
            </span>
          )}
          {traceState && (
            <span
              className="text-xs text-fuchsia-300 max-w-[260px] truncate"
              title={t('trace_summary_title', { nodes: traceState.matchedTargets.length, edges: traceState.edgeIds.length, unmatched: traceState.unmatchedTargets.length })}
            >
              {t('trace_summary', { nodes: traceState.matchedTargets.length, edges: traceState.edgeIds.length })}
            </span>
          )}
          {neighborhoodState && (
            <span className="text-xs text-amber-300 max-w-[220px] truncate" title={t('neighborhood_summary_title', { neighbors: neighborhoodState.neighborIds.length, edges: neighborhoodState.edgeIds.length })}>
              {t('neighborhood_summary', { neighbors: neighborhoodState.neighborIds.length, edges: neighborhoodState.edgeIds.length })}
            </span>
          )}
          
          <button
            onClick={handleZoomIn}
            className="btn btn-secondary p-2"
            title={t("zoom_in_title")}
          >
            <ZoomIn className="w-4 h-4" />
          </button>
          <button
            onClick={handleZoomOut}
            className="btn btn-secondary p-2"
            title={t("zoom_out_title")}
          >
            <ZoomOut className="w-4 h-4" />
          </button>
          <button
            onClick={handleFit}
            className="btn btn-secondary p-2"
            title={t("fit_title")}
          >
            <Maximize2 className="w-4 h-4" />
          </button>
          <button
            onClick={fetchGraph}
            disabled={loading}
            className="btn btn-secondary p-2"
            title={t("refresh_title")}
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={() => setOrgMode((v) => !v)}
            className={`btn px-3 py-2 text-xs ${orgMode ? 'btn-primary' : 'btn-secondary'}`}
            title={t('org_mode_title')}
          >
            🏢 {orgMode ? `${t('org_mode_button')} ✓` : t('org_mode_button')}
          </button>
          <button
            onClick={() => setView3D((v) => !v)}
            className={`btn px-3 py-2 text-xs ${view3D ? 'btn-primary' : 'btn-secondary'}`}
            title={t('view3d_title')}
          >
            ✨ {view3D ? '3D ✓' : '3D'}
          </button>
          {(selectedNode || traceState) && (
            <button
              onClick={handleClearHighlight}
              className="btn btn-secondary px-3 py-2 text-xs"
              title={t("reset_highlight_title")}
            >
              {t('reset_button')}
            </button>
          )}
        </div>
      </div>

      {/* Graph Container */}
      <div className="flex-1 relative">
        {/* Структурная оргсхема: компания → отделы колонками → люди.
            Кнопка «Оргсхема» открывает СХЕМУ, а не force-режим графа. */}
        {orgMode && (
          <div className="absolute inset-0 z-20 bg-brain-950 p-4">
            <OrgGraphView
              userId={userId}
              onOpenEntity={onOpenEntity}
            />
          </div>
        )}
        {/* Живой 3D-вид компании поверх 2D-холста (фолбэк — кнопка 2D внутри) */}
        {view3D && !orgMode && (
          <div className="absolute inset-0 z-20 bg-brain-950/60">
            <Graph3DView
              userId={userId}
              mode="company"
              onFallback={() => setView3D(false)}
              fallbackLabel={t('fallback_2d_label')}
              onOpenEntity={onOpenEntity}
            />
          </div>
        )}
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-brain-900/50 z-10">
            <div className="flex flex-col items-center gap-3">
              <RefreshCw className="w-8 h-8 text-brain-400 animate-spin" />
              <span className="text-brain-400">{t('loading_graph')}</span>
            </div>
          </div>
        )}

        {error && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="text-center">
              <p className="text-red-400 mb-4">{error}</p>
              <button onClick={fetchGraph} className="btn btn-primary">
                {t('retry_button')}
              </button>
            </div>
          </div>
        )}

        {!loading && !error && (!filteredData || filteredData.nodes.length === 0) && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="text-center max-w-md">
              <Info className="w-12 h-12 text-brain-400 mx-auto mb-4" />
              {orgMode ? (
                <>
                  <h3 className="text-lg font-medium mb-2">{t('org_empty_title')}</h3>
                  <p className="text-brain-400 mb-4">
                    {t('org_empty_hint')}
                  </p>
                </>
              ) : (
                <>
                  <h3 className="text-lg font-medium mb-2">{t('empty_graph_title')}</h3>
                  <p className="text-brain-400 mb-4">
                    {t('empty_graph_hint')}
                  </p>
                  <code className="text-sm bg-brain-800 px-3 py-1 rounded">
                    POST /api/v1/analyze/ingest/meeting
                  </code>
                </>
              )}
            </div>
          </div>
        )}

        <div
          ref={containerRef}
          className="w-full h-full graph-container"
          style={{ minHeight: '400px' }}
        />

        {/* Legend */}
        {graphData?.color_legend && (
          <div className="absolute bottom-4 left-4 bg-brain-900/80 backdrop-blur-sm rounded-lg p-3 border border-brain-600/30">
            <p className="text-xs text-brain-400 mb-2">{t('legend_label')}</p>
            <div className="flex flex-wrap gap-2">
              {Object.entries(graphData.color_legend).map(([type, color]) => (
                <div key={type} className="flex items-center gap-1">
                  <div
                    className="w-3 h-3 rounded-full"
                    style={{ backgroundColor: color }}
                  />
                  <span className="text-xs">{type}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Selected Node Detail Panel */}
        {selectedNode && (
          <div className="absolute top-4 right-4 w-80 max-h-[500px] overflow-y-auto bg-brain-900/95 backdrop-blur-sm rounded-lg p-4 border border-brain-600/30 shadow-xl">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <div className="w-4 h-4 rounded-full" style={{ backgroundColor: selectedNode.color }} />
                <span className="font-medium text-sm">{selectedNode.group}</span>
              </div>
              <button onClick={handleClearHighlight} className="text-brain-400 hover:text-white text-lg">x</button>
            </div>
            <h3 className="text-lg font-semibold mb-1">{selectedNode.label}</h3>
            <p className="text-brain-500 text-xs mb-3">ID: {selectedNode.id}</p>
            
            {selectedNode.properties && Object.keys(selectedNode.properties).length > 0 && (
              <div className="space-y-2 text-sm">
                {/* Key properties first */}
                {selectedNode.properties.description && (
                  <div className="bg-brain-800/50 rounded p-2">
                    <span className="text-brain-400 text-xs block mb-1">{t('description_label')}</span>
                    <span className="text-white text-xs">{String(selectedNode.properties.description).slice(0, 200)}</span>
                  </div>
                )}
                {selectedNode.properties.confidence !== undefined && (
                  <div className="flex justify-between">
                    <span className="text-brain-400">{t('confidence_label')}</span>
                    <span className={`font-medium ${Number(selectedNode.properties.confidence) > 0.7 ? 'text-green-400' : 'text-yellow-400'}`}>
                      {(Number(selectedNode.properties.confidence) * 100).toFixed(0)}%
                    </span>
                  </div>
                )}
                {selectedNode.properties.access_level !== undefined && (
                  <div className="flex justify-between">
                    <span className="text-brain-400">{t('access_level_label')}</span>
                    <span className="text-slate-300">{selectedNode.properties.access_level}</span>
                  </div>
                )}
                {/* All other properties */}
                {Object.entries(selectedNode.properties)
                  .filter(([k]) => !['description', 'confidence', 'access_level', '_label', 'id'].includes(k))
                  .map(([key, value]) => (
                  <div key={key} className="flex justify-between gap-2">
                    <span className="text-brain-400 text-xs shrink-0">{key}:</span>
                    <span className="text-xs text-right truncate max-w-[180px]" title={String(value)}>
                      {Array.isArray(value) ? value.join(', ') : String(value).slice(0, 60)}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {traceState && traceState.matchedTargets.length > 0 && (
              <div className="mt-4 pt-3 border-t border-brain-700/40">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-medium text-fuchsia-300">
                    {traceState.title || 'Reasoning trace'}
                  </span>
                  <span className="text-[11px] text-brain-500">
                    {t('trace_nodes_edges', { nodes: traceState.matchedTargets.length, edges: traceState.edgeIds.length })}
                  </span>
                </div>

                <div className="space-y-2">
                  {traceState.matchedTargets.map((item, index) => {
                    const isSelected = item.node.id === selectedNode.id
                    return (
                      <button
                        key={`${item.node.id}-${index}`}
                        onClick={() => handleFocusTraceNode(item.node)}
                        className={`w-full text-left rounded px-2 py-2 transition-colors ${
                          isSelected
                            ? 'bg-fuchsia-500/20 border border-fuchsia-400/40'
                            : 'bg-brain-800/50 hover:bg-brain-800/80 border border-transparent'
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs font-medium text-white truncate">{item.node.label}</span>
                          <span className="text-[10px] text-brain-500 shrink-0">
                            {item.role === 'direct' ? 'direct' : item.role === 'anchor' ? 'anchor' : 'support'}
                          </span>
                        </div>
                        <div className="mt-1 flex items-center justify-between gap-2">
                          <span className="text-[11px] text-fuchsia-300 truncate">{item.sourceType || item.node.group}</span>
                          <span className="text-[10px] text-brain-500 shrink-0">#{(item.pathOrder ?? index) + 1}</span>
                        </div>
                        <div className="mt-1 flex items-center justify-between gap-2">
                          <span className={`text-[10px] px-1.5 py-0.5 rounded ${getTraceStageClasses(item.retrievalStage)}`}>
                            {getTraceStageLabel(item.retrievalStage)}
                          </span>
                          {item.retrievalSources && item.retrievalSources.length > 0 && (
                            <span className="text-[10px] text-brain-500 truncate">
                              via {item.retrievalSources.join(', ')}
                            </span>
                          )}
                        </div>
                      </button>
                    )
                  })}
                </div>

                {traceState.segments.length > 0 && (
                  <div className="mt-3 space-y-1">
                    <span className="text-[11px] font-medium text-brain-400">{t('provenance_segments')}</span>
                    {traceState.segments.slice(0, 6).map((segment, index) => (
                      <div
                        key={`${segment.fromKey}-${segment.toKey}-${index}`}
                        className="bg-brain-800/40 rounded px-2 py-2 space-y-1"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-[11px] text-white truncate">
                            {segment.fromLabel || segment.fromKey} → {segment.toLabel || segment.toKey}
                          </span>
                          <span className={`text-[10px] px-1.5 py-0.5 rounded ${getTraceStageClasses(segment.segmentStage)}`}>
                            {getTraceStageLabel(segment.segmentStage)}
                          </span>
                        </div>
                        <div className="flex items-center justify-between gap-2 text-[10px] text-brain-400">
                          <span>
                            {segment.bridgeKind === 'graph_path'
                              ? `graph bridge: ${segment.pathEdgeCount || 0} edges`
                              : 'gap in graph path'}
                          </span>
                          {segment.segmentSources && segment.segmentSources.length > 0 && (
                            <span className="truncate">via {segment.segmentSources.join(', ')}</span>
                          )}
                        </div>
                        {segment.relationLabels && segment.relationLabels.length > 0 && (
                          <div className="text-[10px] text-fuchsia-300 truncate">
                            {segment.relationLabels.join(' -> ')}
                          </div>
                        )}
                        {!segment.pathFound && segment.reason && (
                          <div className="text-[10px] text-amber-300">
                            {segment.reason}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {traceState.reasoningSteps.length > 0 && (
                  <div className="mt-3 space-y-1">
                    <span className="text-[11px] font-medium text-brain-400">{t('reasoning_steps')}</span>
                    {traceState.reasoningSteps.slice(0, 5).map((step, index) => (
                      <div key={`${index}-${step}`} className="text-[11px] text-brain-300 bg-brain-800/40 rounded px-2 py-1">
                        {index + 1}. {step}
                      </div>
                    ))}
                  </div>
                )}

                {traceState.unmatchedTargets.length > 0 && (
                  <div className="mt-3 text-[11px] text-amber-300">
                    {t('unmatched_targets', { labels: traceState.unmatchedTargets.map((item) => item.label || item.entityId).join(', ') })}
                  </div>
                )}

                {traceState.missingLinks.length > 0 && (
                  <div className="mt-3 space-y-1">
                    <span className="text-[11px] font-medium text-amber-300">{t('provenance_breaks')}</span>
                    {traceState.missingLinks.slice(0, 4).map((item, index) => (
                      <div key={`${index}-${item.fromLabel}-${item.toLabel}`} className="text-[11px] text-amber-200 bg-amber-500/10 rounded px-2 py-1">
                        {item.fromLabel || 'source'} → {item.toLabel || 'target'} ({item.reason || 'path_not_found'})
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {neighborhoodState && neighborhoodState.centerId === selectedNode.id && (
              <div className="mt-4 pt-3 border-t border-brain-700/40">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-medium text-purple-300">{t('local_subgraph')}</span>
                  <span className="text-[11px] text-brain-500">
                    {t('neighborhood_summary_short', { neighbors: neighborhoodState.neighborIds.length, edges: neighborhoodState.edgeIds.length })}
                  </span>
                </div>
                {neighborhoodState.neighbors.length > 0 ? (
                  <div className="space-y-2">
                    {neighborhoodState.neighbors.slice(0, 8).map((item) => (
                      <button
                        key={item.node.id}
                        onClick={() => handleFocusNeighbor(item.node)}
                        className="w-full text-left rounded bg-brain-800/50 hover:bg-brain-800/80 transition-colors px-2 py-2"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs font-medium text-white truncate">{item.node.label}</span>
                          <span className="text-[10px] text-brain-500 shrink-0">{item.node.group}</span>
                        </div>
                        {item.relationLabels.length > 0 && (
                          <div className="text-[11px] text-purple-300 mt-1 truncate">
                            {item.relationLabels.join(', ')}
                          </div>
                        )}
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-brain-500">{t('no_neighbors_hint')}</p>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}


