'use client'

import { useCallback, useState, useEffect, useRef, useMemo } from 'react'
import { useTranslations } from 'next-intl'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Node,
  type Edge,
  type OnConnect,
  type OnNodesChange,
  type OnEdgesChange,
  type Connection,
  applyNodeChanges,
  applyEdgeChanges,
  MarkerType,
  BackgroundVariant,
  ConnectionMode,
  type OnEdgesDelete,
  ReactFlowProvider,
  useReactFlow,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useSimaStore } from '@/sima/lib/store'
import SimaBlockNode from './SimaBlockNode'
import SimaEditableEdge from './SimaEditableEdge'
import { CONNECTION_TYPE_COLORS, BLOCK_TYPE_COLORS } from '@/sima/types'
import type { ConnectionType, BlockType } from '@/sima/types'
import { Plus, X, Search, Filter, ArrowLeft, Home, Layers, GripVertical, Group } from 'lucide-react'
import { cn } from '@/sima/lib/utils'
import { simaFetch } from '@/sima/lib/api'

const nodeTypes = { simaBlock: SimaBlockNode }
const edgeTypes = { simaEditable: SimaEditableEdge }

function DiagramCanvasInner() {
  const t = useTranslations('sima_canvas')
  const tRoot = useTranslations('sima')
  // Координаты холста с учётом зума/панорамы. Раньше считали clientX-bounds.left
  // напрямую → при любом зуме/сдвиге блок создавался не там, где кликнули.
  const { screenToFlowPosition } = useReactFlow()
  const {
    blocks,
    connections,
    updateBlock,
    setSelectedBlockId,
    setSelectedConnectionId,
    setRightPanelOpen,
    project,
    addConnection: addConnectionToStore,
    removeConnection,
    currentParentBlockId,
    breadcrumbs,
    drillDown,
    drillUp,
    drillToRoot,
    selectedBlockIds,
    toggleBlockSelection,
    clearMultiSelection,
    removeSelectedBlocks,
    selectAllBlocks,
    frames,
    addFrame,
    removeFrame,
  } = useSimaStore()

  // Локальное состояние нод для ReactFlow
  const [rfNodes, setRfNodes] = useState<Node[]>([])
  const [rfEdges, setRfEdges] = useState<Edge[]>([])

  // Поиск и фильтрация
  const [searchQuery, setSearchQuery] = useState('')
  const [showFilters, setShowFilters] = useState(false)
  const [filterType, setFilterType] = useState<BlockType | 'all'>('all')
  const [filterLayer, setFilterLayer] = useState<string>('all')
  const searchRef = useRef<HTMLInputElement>(null)

  // Вычисляем какие блоки подсвечены
  const highlightedBlockIds = useMemo(() => {
    const ids = new Set<string>()
    const query = searchQuery.toLowerCase().trim()
    const hasFilter = filterType !== 'all' || filterLayer !== 'all'

    if (!query && !hasFilter) return null // null = всё видно

    for (const block of blocks) {
      let matchesSearch = true
      let matchesFilter = true

      if (query) {
        matchesSearch = (
          block.name.toLowerCase().includes(query) ||
          block.label.toLowerCase().includes(query) ||
          (block.description || '').toLowerCase().includes(query) ||
          block.type.toLowerCase().includes(query)
        )
      }

      if (filterType !== 'all') matchesFilter = matchesFilter && block.type === filterType
      if (filterLayer !== 'all') matchesFilter = matchesFilter && block.layer === filterLayer

      if (matchesSearch && matchesFilter) ids.add(block.id)
    }
    return ids
  }, [searchQuery, filterType, filterLayer, blocks])

  // Фильтруем фреймы по текущему уровню
  const currentFrames = useMemo(
    () => frames.filter(f => (f.parentBlockId || null) === (currentParentBlockId || null)),
    [frames, currentParentBlockId]
  )

  // Синхронизация блоков из стора → ReactFlow ноды (с opacity для фильтрации + мультивыбор + фреймы)
  useEffect(() => {
    // Фрейм-ноды (фоновые группы)
    const frameNodes: Node[] = currentFrames.map(frame => ({
      id: `frame-${frame.id}`,
      type: 'group',
      position: { x: frame.x, y: frame.y },
      data: { label: frame.label },
      style: {
        width: frame.width,
        height: frame.height,
        backgroundColor: `${frame.color}08`,
        border: `2px dashed ${frame.color}40`,
        borderRadius: 12,
        zIndex: -1,
      },
      selectable: false,
      draggable: true,
    }))

    // Блок-ноды
    const blockNodes: Node[] = blocks.map((block) => {
      const isMultiSelected = selectedBlockIds.has(block.id)
      const isFiltered = highlightedBlockIds && !highlightedBlockIds.has(block.id)
      return {
        id: block.id,
        type: 'simaBlock',
        position: { x: block.positionX, y: block.positionY },
        selected: isMultiSelected,
        data: {
          label: block.label,
          name: block.name,
          type: block.type,
          color: block.color,
          description: block.description,
          layer: block.layer,
          estimatedComplexity: block.estimatedComplexity,
          hasChildren: block.hasChildren,
          isMultiSelected,
        },
        style: isFiltered
          ? { opacity: 0.2, transition: 'opacity 0.2s' }
          : isMultiSelected
          ? { opacity: 1, transition: 'opacity 0.2s', outline: '2px solid #6366F1', outlineOffset: 2, borderRadius: 12 }
          : { opacity: 1, transition: 'opacity 0.2s' },
      }
    })

    setRfNodes([...frameNodes, ...blockNodes])
  }, [blocks, highlightedBlockIds, selectedBlockIds, currentFrames])

  // Синхронизация связей из стора → ReactFlow эджи
  useEffect(() => {
    setRfEdges(
      connections.map((conn) => ({
        id: conn.id,
        source: conn.fromBlockId,
        target: conn.toBlockId,
        type: 'simaEditable',
        animated: conn.animated,
        label: conn.label || undefined,
        style: {
          stroke: conn.color || CONNECTION_TYPE_COLORS[conn.type as ConnectionType] || '#6366F1',
          strokeWidth: 2,
          strokeDasharray: conn.style === 'dashed' ? '8 4' : conn.style === 'dotted' ? '2 4' : undefined,
        },
        // Цвета подписи связи НЕ хардкодим инлайном — иначе они перебивают
        // тему (на светлой кремовой канве оставались тёмные чипы). Отдаём в
        // CSS-классы .react-flow__edge-text/-textbg (var-токены, обе темы).
        labelStyle: {
          fontSize: 11,
          fontWeight: 500,
        },
        labelBgStyle: {
          fillOpacity: 0.9,
        },
        labelBgPadding: [6, 4] as [number, number],
        labelBgBorderRadius: 4,
        // Направление задаёт стрелки: one_way/to_task — стрелка в конце,
        // two_way — стрелки с обоих концов (⇌). Цвет маркера = цвет связи.
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 16,
          height: 16,
          color: conn.color || CONNECTION_TYPE_COLORS[conn.type as ConnectionType] || '#6366F1',
        },
        ...(conn.direction === 'two_way' ? {
          markerStart: {
            type: MarkerType.ArrowClosed,
            width: 16,
            height: 16,
            color: conn.color || CONNECTION_TYPE_COLORS[conn.type as ConnectionType] || '#6366F1',
          },
        } : {}),
      }))
    )
  }, [connections])

  // Применяем изменения нод (позиция, выбор, перетаскивание)
  const onNodesChange: OnNodesChange = useCallback(
    (changes) => {
      setRfNodes((nds) => applyNodeChanges(changes, nds))
    },
    []
  )

  // Применяем изменения эджей (выбор, удаление)
  const onEdgesChange: OnEdgesChange = useCallback(
    (changes) => {
      setRfEdges((eds) => applyEdgeChanges(changes, eds))
    },
    []
  )

  // Удаление связей (клавиша Delete/Backspace)
  const onEdgesDelete: OnEdgesDelete = useCallback(
    async (deletedEdges) => {
      for (const edge of deletedEdges) {
        try {
          await simaFetch(`/connections?id=${edge.id}`, { method: 'DELETE' })
          removeConnection(edge.id)
        } catch (e) {
          console.error('Failed to delete connection:', e)
        }
      }
    },
    [removeConnection]
  )

  // Создание новой связи перетаскиванием
  const onConnect: OnConnect = useCallback(
    async (params: Connection) => {
      if (!project || !params.source || !params.target) return

      try {
        const res = await simaFetch('/connections', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            projectId: project.id,
            parentBlockId: currentParentBlockId,
            fromBlockId: params.source,
            toBlockId: params.target,
            autoDescribe: false, // Не ждём AI для быстрого соединения
          }),
        })
        const newConn = await res.json()
        addConnectionToStore(newConn)
      } catch (e) {
        console.error('Failed to create connection:', e)
      }
    },
    [project, addConnectionToStore, currentParentBlockId]
  )

  const onNodeClick = useCallback(
    (event: React.MouseEvent, node: Node) => {
      if (event.shiftKey || event.ctrlKey || event.metaKey) {
        // Мультивыбор
        toggleBlockSelection(node.id)
      } else {
        clearMultiSelection()
        setSelectedBlockId(node.id)
        setRightPanelOpen(true)
      }
    },
    [setSelectedBlockId, setRightPanelOpen, toggleBlockSelection, clearMultiSelection]
  )

  // Двойной клик — drill-down в подсхему (всегда, даже если пустая)
  const onNodeDoubleClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      drillDown(node.id)
    },
    [drillDown]
  )

  const onEdgeClick = useCallback(
    (_: React.MouseEvent, edge: Edge) => {
      setSelectedConnectionId(edge.id)
      setRightPanelOpen(true)
    },
    [setSelectedConnectionId, setRightPanelOpen]
  )

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const onPaneClick = useCallback((_event: any) => {
    setSelectedBlockId(null)
    setSelectedConnectionId(null)
    clearMultiSelection()
    setContextMenu(null)
  }, [setSelectedBlockId, setSelectedConnectionId, clearMultiSelection])

  // === Палитра блоков (drag-n-drop) ===
  const addBlockToStore = useSimaStore((s) => s.addBlock)
  const [showPalette, setShowPalette] = useState(false)

  // Создать фрейм из выбранных блоков
  const createFrameFromSelection = useCallback(() => {
    if (selectedBlockIds.size < 2) return
    const selectedBlocks = blocks.filter(b => selectedBlockIds.has(b.id))
    if (selectedBlocks.length === 0) return

    const minX = Math.min(...selectedBlocks.map(b => b.positionX)) - 30
    const minY = Math.min(...selectedBlocks.map(b => b.positionY)) - 40
    const maxX = Math.max(...selectedBlocks.map(b => b.positionX)) + 220
    const maxY = Math.max(...selectedBlocks.map(b => b.positionY)) + 130

    const colors = ['#6366F1', '#22D3EE', '#F59E0B', '#10B981', '#EF4444', '#8B5CF6']
    const frameColor = colors[frames.length % colors.length]

    addFrame({
      id: crypto.randomUUID(),
      label: t('group_default_name', { n: frames.length + 1 }),
      color: frameColor,
      x: minX,
      y: minY,
      width: maxX - minX,
      height: maxY - minY,
      blockIds: Array.from(selectedBlockIds),
      parentBlockId: currentParentBlockId,
    })
    clearMultiSelection()
  }, [selectedBlockIds, blocks, frames, addFrame, clearMultiSelection, currentParentBlockId])

  // (currentFrames перемещён выше, перед useEffect синхронизации)
  const reactFlowWrapper = useRef<HTMLDivElement>(null)

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
  }, [])

  const onDrop = useCallback(
    async (event: React.DragEvent) => {
      event.preventDefault()
      const type = event.dataTransfer.getData('application/sima-block-type')
      if (!type || !project) return

      const { x, y } = screenToFlowPosition({ x: event.clientX, y: event.clientY })

      const existingLabels = blocks.map(b => b.label)
      const prefix = type === 'core' ? 'M' : type === 'data' ? 'D' : type === 'integration' ? 'I' : type === 'agent' ? 'A' : 'F'
      let num = 1
      while (existingLabels.includes(`${prefix}${num}`)) num++
      const label = `${prefix}${num}`
      const color = BLOCK_TYPE_COLORS[type as BlockType] || '#6366F1'

      try {
        const res = await simaFetch('/blocks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            projectId: project.id,
            parentBlockId: currentParentBlockId || null,
            name: type === 'core' ? t('new_block_core') : type === 'feature' ? t('new_block_feature') : type === 'integration' ? t('new_block_integration') : type === 'data' ? t('new_block_data') : t('new_block_agent'),
            label, type, layer: 'mvp', color,
            positionX: x, positionY: y,
          }),
        })
        const newBlock = await res.json()
        addBlockToStore(newBlock)
        setSelectedBlockId(newBlock.id)
        setRightPanelOpen(true)
      } catch (e) {
        console.error('Failed to add block via drag:', e)
      }
    },
    [project, blocks, currentParentBlockId, addBlockToStore, setSelectedBlockId, setRightPanelOpen, screenToFlowPosition]
  )

  // === Контекстное меню для создания блока ===
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; flowX: number; flowY: number } | null>(null)
  const [quickAddName, setQuickAddName] = useState('')
  const [quickAddType, setQuickAddType] = useState<BlockType>('feature')
  const quickAddRef = useRef<HTMLInputElement>(null)

  const onPaneContextMenu = useCallback(
    (event: MouseEvent | React.MouseEvent<Element, MouseEvent>) => {
      event.preventDefault()
      // Координаты на холсте с учётом зума/панорамы (было: clientX-bounds.left)
      const { x: flowX, y: flowY } = screenToFlowPosition({ x: event.clientX, y: event.clientY })
      setContextMenu({ x: event.clientX, y: event.clientY, flowX, flowY })
      setQuickAddName('')
      setQuickAddType('feature')
      setTimeout(() => quickAddRef.current?.focus(), 100)
    },
    [screenToFlowPosition]
  )

  const handleQuickAddBlock = useCallback(
    async () => {
      if (!quickAddName.trim() || !project) return
      const existingLabels = blocks.map(b => b.label)
      const prefix = quickAddType === 'core' ? 'M' : quickAddType === 'data' ? 'D' : quickAddType === 'integration' ? 'I' : quickAddType === 'agent' ? 'A' : 'F'
      let num = 1
      while (existingLabels.includes(`${prefix}${num}`)) num++
      const label = `${prefix}${num}`

      const color = BLOCK_TYPE_COLORS[quickAddType] || '#6366F1'

      try {
        const res = await simaFetch('/blocks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            projectId: project.id,
            parentBlockId: currentParentBlockId || null,
            name: quickAddName.trim(),
            label,
            type: quickAddType,
            layer: 'mvp',
            color,
            positionX: contextMenu?.flowX || 200,
            positionY: contextMenu?.flowY || 200,
          }),
        })
        const newBlock = await res.json()
        addBlockToStore(newBlock)
      } catch (e) {
        console.error('Failed to add block:', e)
      }
      setContextMenu(null)
      setQuickAddName('')
    },
    [quickAddName, quickAddType, project, blocks, currentParentBlockId, contextMenu, addBlockToStore]
  )

  // Сохраняем позицию в БД и стор после перетаскивания
  const onNodeDragStop = useCallback(
    async (_: MouseEvent | TouchEvent, node: Node) => {
      updateBlock(node.id, {
        positionX: node.position.x,
        positionY: node.position.y,
      })
      try {
        await simaFetch('/blocks', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: node.id,
            positionX: node.position.x,
            positionY: node.position.y,
          }),
        })
      } catch (e) {
        console.error('Failed to save block position:', e)
      }
    },
    [updateBlock]
  )

  return (
    <div className="absolute inset-0">
      {/* Breadcrumbs + навигация по подсхемам */}
      {breadcrumbs.length > 1 && (
        <div className="absolute top-3 left-3 z-10 flex items-center gap-1.5 bg-sima-surface/90 backdrop-blur-sm border border-sima-border rounded-lg px-2 py-1.5 shadow-lg">
          {/* Кнопка «Назад» */}
          <button
            onClick={drillUp}
            className="p-1 rounded hover:bg-sima-bg transition-colors text-sima-textMuted hover:text-sima-text"
            title={t("back_level_up_title")}
          >
            <ArrowLeft className="w-3.5 h-3.5" />
          </button>

          {/* Кнопка «Корень» */}
          {breadcrumbs.length > 2 && (
            <button
              onClick={drillToRoot}
              className="p-1 rounded hover:bg-sima-bg transition-colors text-sima-textMuted hover:text-sima-text"
              title={t("root_level_title")}
            >
              <Home className="w-3.5 h-3.5" />
            </button>
          )}

          <div className="w-px h-4 bg-sima-border" />

          {/* Breadcrumbs */}
          {breadcrumbs.map((crumb, i) => (
            <div key={i} className="flex items-center gap-1">
              {i > 0 && <span className="text-sima-textDim text-[10px]">/</span>}
              <button
                onClick={() => {
                  if (crumb.id === null) drillToRoot()
                  else {
                    const stepsBack = breadcrumbs.length - 1 - i
                    for (let s = 0; s < stepsBack; s++) drillUp()
                  }
                }}
                className={cn(
                  'text-xs px-1.5 py-0.5 rounded transition-colors flex items-center gap-1',
                  i === breadcrumbs.length - 1
                    ? 'text-sima-primary font-semibold bg-sima-primary/10'
                    : 'text-sima-textMuted hover:text-sima-text hover:bg-sima-bg'
                )}
              >
                {i === 0 && <Home className="w-2.5 h-2.5" />}
                {i > 0 && <Layers className="w-2.5 h-2.5" />}
                {crumb.id === null ? tRoot('breadcrumb_root') : crumb.name}
              </button>
            </div>
          ))}
        </div>
      )}

      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onEdgesDelete={onEdgesDelete}
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        onNodeDoubleClick={onNodeDoubleClick}
        onEdgeClick={onEdgeClick}
        onPaneClick={onPaneClick}
        onPaneContextMenu={onPaneContextMenu}
        onNodeDragStop={onNodeDragStop}
        onDragOver={onDragOver}
        onDrop={onDrop}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        nodesDraggable
        panOnDrag
        panOnScroll={false}
        zoomOnScroll
        zoomOnPinch
        elementsSelectable
        connectionMode={ConnectionMode.Loose}
        deleteKeyCode={['Backspace', 'Delete']}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        defaultEdgeOptions={{
          type: 'simaEditable',
          animated: false,
        }}
        connectionLineStyle={{ stroke: '#6366F1', strokeWidth: 2 }}
        className="bg-sima-bg"
        proOptions={{ hideAttribution: true }}
        snapToGrid
        snapGrid={[20, 20]}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={20}
          size={1}
          color="#1A1F35"
        />
        <Controls
          showInteractive={false}
          className="!bg-sima-surface !border-sima-border !rounded-lg"
        />
        <MiniMap
          nodeStrokeWidth={3}
          className="!bg-sima-surface !border-sima-border !rounded-lg"
          maskColor="rgba(10, 14, 26, 0.7)"
          nodeColor={(node) => (node.data?.color as string) || '#6366F1'}
        />
      </ReactFlow>

      {/* Поиск + фильтрация */}
      {blocks.length > 0 && (
        <div className="absolute top-3 right-3 z-10 flex items-center gap-1.5">
          {/* Search input */}
          <div className="flex items-center bg-sima-surface/90 backdrop-blur-sm border border-sima-border rounded-lg overflow-hidden shadow-lg">
            <Search className="w-3.5 h-3.5 text-sima-textDim ml-2.5 shrink-0" />
            <input
              ref={searchRef}
              data-sima-search
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t("search_placeholder")}
              className="bg-transparent px-2 py-1.5 text-xs text-sima-text placeholder-sima-textDim focus:outline-none w-32"
            />
            {searchQuery && (
              <button onClick={() => setSearchQuery('')} className="pr-2 text-sima-textDim hover:text-sima-text">
                <X className="w-3 h-3" />
              </button>
            )}
          </div>

          {/* Filter toggle */}
          <button
            onClick={() => setShowFilters(!showFilters)}
            className={cn(
              'p-1.5 rounded-lg border shadow-lg transition-colors backdrop-blur-sm',
              showFilters || filterType !== 'all' || filterLayer !== 'all'
                ? 'bg-sima-primary/20 border-sima-primary/50 text-sima-primary'
                : 'bg-sima-surface/90 border-sima-border text-sima-textDim hover:text-sima-text'
            )}
          >
            <Filter className="w-3.5 h-3.5" />
          </button>

          {/* Filter dropdown */}
          {showFilters && (
            <div className="absolute top-full right-0 mt-1.5 bg-sima-surface/95 backdrop-blur-sm border border-sima-border rounded-lg shadow-2xl p-3 w-56">
              <div className="mb-2">
                <span className="text-[10px] font-medium text-sima-textDim uppercase tracking-wide">{t('filter_type_label')}</span>
                <div className="flex flex-wrap gap-1 mt-1">
                  <FilterChip active={filterType === 'all'} onClick={() => setFilterType('all')} label={t("filter_all")} />
                  {(['core', 'feature', 'integration', 'data', 'agent'] as BlockType[]).map(t => (
                    <FilterChip key={t} active={filterType === t} onClick={() => setFilterType(t)} label={t}
                      color={BLOCK_TYPE_COLORS[t]} />
                  ))}
                </div>
              </div>
              <div>
                <span className="text-[10px] font-medium text-sima-textDim uppercase tracking-wide">{t('filter_layer_label')}</span>
                <div className="flex flex-wrap gap-1 mt-1">
                  <FilterChip active={filterLayer === 'all'} onClick={() => setFilterLayer('all')} label={t("filter_all")} />
                  {['mvp', 'medium', 'ideal', 'problem', 'solution'].map(l => (
                    <FilterChip key={l} active={filterLayer === l} onClick={() => setFilterLayer(l)} label={l} />
                  ))}
                </div>
              </div>
              {(filterType !== 'all' || filterLayer !== 'all') && (
                <button
                  onClick={() => { setFilterType('all'); setFilterLayer('all') }}
                  className="mt-2 text-[10px] text-sima-primary hover:underline"
                >
                  {t('reset_filters')}
                </button>
              )}
            </div>
          )}

          {/* Matched count */}
          {highlightedBlockIds && (
            <span className="text-[10px] text-sima-textDim bg-sima-surface/90 backdrop-blur-sm border border-sima-border rounded px-1.5 py-0.5 shadow-lg">
              {highlightedBlockIds.size}/{blocks.length}
            </span>
          )}
        </div>
      )}

      {/* Контекстное меню — быстрое добавление блока (ПКМ по холсту) */}
      {contextMenu && (
        <div
          className="fixed z-50 bg-sima-surface border border-sima-border rounded-xl shadow-2xl p-3 w-64"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-sima-text">{t('add_block_heading')}</span>
            <button onClick={() => setContextMenu(null)} className="text-sima-textDim hover:text-sima-text">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
          <input
            ref={quickAddRef}
            value={quickAddName}
            onChange={(e) => setQuickAddName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleQuickAddBlock()
              if (e.key === 'Escape') setContextMenu(null)
            }}
            placeholder={t("block_name_placeholder")}
            className="w-full bg-sima-bg border border-sima-border rounded-lg px-3 py-1.5 text-xs text-sima-text placeholder-sima-textDim focus:outline-none focus:border-sima-primary mb-2"
          />
          <div className="flex flex-wrap gap-1 mb-2">
            {(['core', 'feature', 'integration', 'data', 'agent'] as BlockType[]).map((t) => (
              <button
                key={t}
                onClick={() => setQuickAddType(t)}
                className={`px-2 py-0.5 text-[10px] rounded-full border transition-colors ${
                  quickAddType === t
                    ? 'border-sima-primary bg-sima-primary/20 text-sima-primary'
                    : 'border-sima-border text-sima-textDim hover:text-sima-textMuted'
                }`}
              >
                {t}
              </button>
            ))}
          </div>
          <button
            onClick={handleQuickAddBlock}
            disabled={!quickAddName.trim()}
            className="w-full py-1.5 bg-sima-primary hover:bg-sima-primaryHover text-white rounded-lg text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-1"
          >
            <Plus className="w-3 h-3" />
            {t('create_button')}
          </button>
        </div>
      )}

      {/* Палитра блоков (drag-n-drop) */}
      <div className="absolute bottom-3 left-3 z-10">
        <button
          onClick={() => setShowPalette(!showPalette)}
          className={cn(
            'p-2 rounded-lg border shadow-lg backdrop-blur-sm transition-all',
            showPalette
              ? 'bg-sima-primary/20 border-sima-primary/50 text-sima-primary'
              : 'bg-sima-surface/90 border-sima-border text-sima-textMuted hover:text-sima-text'
          )}
          title={t("palette_title")}
        >
          <Plus className="w-4 h-4" />
        </button>
        {showPalette && (
          <div className="absolute bottom-12 left-0 bg-sima-surface/95 backdrop-blur-sm border border-sima-border rounded-xl shadow-2xl p-2.5 w-44 space-y-1">
            <p className="text-[10px] text-sima-textDim font-medium uppercase tracking-wider px-1 mb-1.5">{t('palette_hint')}</p>
            {([
              { type: 'core', label: t('palette_core'), emoji: '🔷' },
              { type: 'feature', label: t('palette_feature'), emoji: '💎' },
              { type: 'integration', label: t('palette_integration'), emoji: '🔗' },
              { type: 'data', label: t('palette_data'), emoji: '📊' },
              { type: 'agent', label: t('palette_agent'), emoji: '🤖' },
            ] as const).map((item) => (
              <div
                key={item.type}
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.setData('application/sima-block-type', item.type)
                  e.dataTransfer.effectAllowed = 'move'
                }}
                className="flex items-center gap-2 px-2.5 py-2 rounded-lg cursor-grab active:cursor-grabbing hover:bg-sima-surfaceLight transition-colors border border-transparent hover:border-sima-border"
                style={{ borderLeftColor: BLOCK_TYPE_COLORS[item.type], borderLeftWidth: 3 }}
              >
                <span className="text-xs">{item.emoji}</span>
                <span className="text-xs font-medium text-sima-text">{item.label}</span>
                <GripVertical className="w-3 h-3 text-sima-textDim ml-auto opacity-50" />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Мультивыбор-тулбар */}
      {selectedBlockIds.size > 0 && (
        <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 flex items-center gap-2 bg-sima-surface/95 backdrop-blur-sm border border-sima-primary/30 rounded-xl shadow-2xl px-4 py-2">
          <span className="text-xs font-medium text-sima-primary">{t('selected_count', { count: selectedBlockIds.size })}</span>
          <div className="w-px h-4 bg-sima-border" />
          {selectedBlockIds.size >= 2 && (
            <button
              onClick={createFrameFromSelection}
              className="flex items-center gap-1 px-2 py-1 text-[10px] text-sima-primary hover:bg-sima-primary/10 rounded transition-colors"
            >
              <Group className="w-3 h-3" />
              {t('group_button')}
            </button>
          )}
          <button
            onClick={removeSelectedBlocks}
            className="px-2 py-1 text-[10px] text-red-400 hover:bg-red-400/10 rounded transition-colors"
          >
            {t('delete_button')}
          </button>
          <button
            onClick={clearMultiSelection}
            className="px-2 py-1 text-[10px] text-sima-textDim hover:bg-sima-surfaceLight rounded transition-colors"
          >
            {t('deselect_button')}
          </button>
        </div>
      )}

      {/* Фреймы рендерятся как ReactFlow Group Nodes — добавлены в rfNodes */}

      {/* Подсказка для пустой подсхемы */}
      {currentParentBlockId && blocks.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none z-5">
          <div className="text-center space-y-3 pointer-events-auto bg-sima-surface/80 backdrop-blur-sm border border-sima-border rounded-xl px-6 py-5 shadow-xl">
            <Layers className="w-8 h-8 text-sima-textDim mx-auto opacity-40" />
            <p className="text-sm font-medium text-sima-text">{t('empty_subschema_title')}</p>
            <div className="space-y-1">
              <p className="text-xs text-sima-textDim">{t('right_click_hint')}</p>
              <p className="text-xs text-sima-textDim">{t('describe_in_chat_hint')}</p>
            </div>
            <button
              onClick={drillUp}
              className="mt-2 px-3 py-1.5 text-xs bg-sima-primary/20 text-sima-primary rounded-lg hover:bg-sima-primary/30 transition-colors flex items-center gap-1.5 mx-auto"
            >
              <ArrowLeft className="w-3 h-3" />
              {t('back_button')}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// Обёртка провайдером: useReactFlow (screenToFlowPosition) требует контекста
// ReactFlow. Раньше DiagramCanvas рендерил <ReactFlow> внутри себя, а сам
// был вне провайдера — координаты приходилось считать вручную (баг зума).
export default function DiagramCanvas() {
  return (
    <ReactFlowProvider>
      <DiagramCanvasInner />
    </ReactFlowProvider>
  )
}

function FilterChip({ active, onClick, label, color }: { active: boolean; onClick: () => void; label: string; color?: string }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'px-2 py-0.5 text-[10px] rounded-full border transition-colors',
        active
          ? 'border-sima-primary bg-sima-primary/20 text-sima-primary font-medium'
          : 'border-sima-border text-sima-textDim hover:text-sima-textMuted'
      )}
      style={active && color ? { borderColor: color, backgroundColor: `${color}20`, color } : undefined}
    >
      {label}
    </button>
  )
}
