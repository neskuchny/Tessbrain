'use client'

import { create } from 'zustand'
import type { ProjectData, BlockData, ConnectionData, MessageData, AIAction, StrategistReport, PersonaData, SimulationReport } from '@/sima/types'
import type { EconomicsReport } from './economics-service'
import { undoManager, type Snapshot } from './undo-manager'

export type BottomPanelTab = 'chat' | 'search' | 'consultant' | 'narrative' | 'progress' | 'strategist' | 'simulator' | 'export' | 'economics' | 'research' | 'diff' | 'data' | 'artifacts'
// Слои рабочей области SIMA (Sima Remix): канвас источников → карта продукта
// → ТЗ → реализация → артефакты. Слой = полная смена вида центра.
export type SimaLayer = 'canvas' | 'map' | 'tz' | 'impl' | 'gallery'

export interface FrameData {
  id: string
  label: string
  color: string
  x: number
  y: number
  width: number
  height: number
  blockIds: string[]  // блоки внутри фрейма
  parentBlockId: string | null  // уровень подсхемы
}
export type RightPanelTab = 'why' | 'what' | 'how' | 'context' | 'files' | 'connection'

interface StrategistChatMessage {
  role: 'user' | 'assistant'
  content: string
}

interface SimaState {
  // Текущий проект
  project: ProjectData | null
  setProject: (project: ProjectData | null) => void

  // Сброс всего состояния, связанного с проектом (при переключении)
  resetProjectState: () => void

  // Все блоки проекта (плоский список, включая вложенные)
  allBlocks: BlockData[]
  setAllBlocks: (blocks: BlockData[]) => void

  // Блоки текущего уровня (фильтрованные по currentParentBlockId)
  blocks: BlockData[]
  setBlocks: (blocks: BlockData[]) => void
  addBlock: (block: BlockData) => void
  updateBlock: (id: string, data: Partial<BlockData>) => void
  removeBlock: (id: string) => void

  // Все связи проекта
  allConnections: ConnectionData[]
  setAllConnections: (connections: ConnectionData[]) => void

  // Связи текущего уровня
  connections: ConnectionData[]
  setConnections: (connections: ConnectionData[]) => void
  addConnection: (connection: ConnectionData) => void
  updateConnection: (id: string, data: Partial<ConnectionData>) => void
  removeConnection: (id: string) => void

  // Навигация по подсхемам
  currentParentBlockId: string | null  // null = корневой уровень
  breadcrumbs: { id: string | null; name: string }[]  // навигационная цепочка
  drillDown: (blockId: string) => void
  drillUp: () => void
  drillToRoot: () => void

  // Выбранные элементы
  selectedBlockId: string | null
  setSelectedBlockId: (id: string | null) => void
  selectedConnectionId: string | null
  setSelectedConnectionId: (id: string | null) => void

  // Мультивыбор
  selectedBlockIds: Set<string>
  toggleBlockSelection: (id: string) => void
  selectAllBlocks: () => void
  clearMultiSelection: () => void
  removeSelectedBlocks: () => void

  // Сообщения и сессии
  messages: MessageData[]
  setMessages: (messages: MessageData[]) => void
  addMessage: (message: MessageData) => void
  currentSessionId: string | null
  setCurrentSessionId: (id: string | null) => void

  // UI состояние
  rightPanelOpen: boolean
  setRightPanelOpen: (open: boolean) => void
  bottomPanelOpen: boolean
  setBottomPanelOpen: (open: boolean) => void
  bottomPanelTab: BottomPanelTab
  setBottomPanelTab: (tab: BottomPanelTab) => void
  // Палитра «Добавить источник» (правая панель канваса) просит DataPanel
  // открыть конкретный режим ввода; DataPanel потребляет и сбрасывает.
  dataPanelMode: 'file' | 'text' | 'meetings' | 'documents' | null
  setDataPanelMode: (m: 'file' | 'text' | 'meetings' | 'documents' | null) => void
  // Активный СЛОЙ рабочей области (Sima Remix): полная смена вида центра, а
  // не вкладка нижней панели. Нижняя панель (чат+инструменты) — независима.
  activeLayer: SimaLayer
  setActiveLayer: (layer: SimaLayer) => void
  rightPanelTab: RightPanelTab
  setRightPanelTab: (tab: RightPanelTab) => void

  // Загрузка
  isLoading: boolean
  setIsLoading: (loading: boolean) => void
  isSending: boolean
  setIsSending: (sending: boolean) => void

  // Kanon: вердикты верификаций по блокам (подсветка на канвасе)
  kanonVerdicts: Record<string, { verdict?: string; cascadeState?: string; ready?: boolean }>
  setKanonVerdicts: (v: Record<string, { verdict?: string; cascadeState?: string; ready?: boolean }>) => void

  // Стратег
  strategistReport: StrategistReport | null
  setStrategistReport: (report: StrategistReport | null) => void
  strategistLoading: boolean
  setStrategistLoading: (loading: boolean) => void
  strategistChat: StrategistChatMessage[]
  addStrategistChatMessage: (msg: StrategistChatMessage) => void
  clearStrategistChat: () => void
  strategistChatLoading: boolean
  setStrategistChatLoading: (loading: boolean) => void

  // Экономика
  economicsReport: EconomicsReport | null
  setEconomicsReport: (report: EconomicsReport | null) => void

  // Симулятор
  personas: PersonaData[]
  setPersonas: (personas: PersonaData[]) => void
  addPersona: (persona: PersonaData) => void
  removePersona: (id: string) => void
  simulationReport: SimulationReport | null
  setSimulationReport: (report: SimulationReport | null) => void
  simulationReports: SimulationReport[]
  setSimulationReports: (reports: SimulationReport[]) => void
  simulatorLoading: boolean
  setSimulatorLoading: (loading: boolean) => void
  selectedPersonaId: string | null
  setSelectedPersonaId: (id: string | null) => void

  // Фреймы (группировка)
  frames: FrameData[]
  addFrame: (frame: FrameData) => void
  updateFrame: (id: string, data: Partial<FrameData>) => void
  removeFrame: (id: string) => void

  // Undo/Redo
  pushUndo: (label?: string) => void
  undo: () => void
  redo: () => void
  canUndo: boolean
  canRedo: boolean
}

// Хелпер для вычисления hasChildren
function enrichBlocksWithChildren(allBlocks: BlockData[]): BlockData[] {
  const parentIds = new Set(allBlocks.filter(b => b.parentBlockId).map(b => b.parentBlockId!))
  return allBlocks.map(b => ({ ...b, hasChildren: parentIds.has(b.id) }))
}

// Хелпер для фильтрации блоков/связей по текущему уровню
function filterByParent(allBlocks: BlockData[], allConnections: ConnectionData[], parentBlockId: string | null) {
  const blocks = allBlocks.filter(b => (b.parentBlockId || null) === parentBlockId)
  const blockIds = new Set(blocks.map(b => b.id))
  const connections = allConnections.filter(c => {
    // Связь принадлежит уровню, если оба конца на этом уровне
    return blockIds.has(c.fromBlockId) && blockIds.has(c.toBlockId)
  })
  return { blocks, connections }
}

export const useSimaStore = create<SimaState>((set, get) => ({
  project: null,
  setProject: (project) => set({ project }),

  resetProjectState: () => {
    undoManager.clear()
    set({
    allBlocks: [],
    blocks: [],
    allConnections: [],
    connections: [],
    messages: [],
    currentParentBlockId: null,
    breadcrumbs: [{ id: null, name: '' }],
    selectedBlockId: null,
    selectedBlockIds: new Set<string>(),
    selectedConnectionId: null,
    currentSessionId: null,
    // Kanon
    kanonVerdicts: {},
    // Стратег
    strategistReport: null,
    strategistLoading: false,
    strategistChat: [],
    strategistChatLoading: false,
    // Экономика
    economicsReport: null,
    // Симулятор
    personas: [],
    simulationReport: null,
    simulationReports: [],
    simulatorLoading: false,
    selectedPersonaId: null,
    canUndo: false,
    canRedo: false,
  })
  },

  allBlocks: [],
  setAllBlocks: (blocks) => {
    const enriched = enrichBlocksWithChildren(blocks)
    const { currentParentBlockId, allConnections } = get()
    const filtered = filterByParent(enriched, allConnections, currentParentBlockId)
    set({ allBlocks: enriched, blocks: filtered.blocks, connections: filtered.connections })
  },

  blocks: [],
  setBlocks: (blocks) => set({ blocks }),
  addBlock: (block) => {
    get().pushUndo('Add block')
    set((state) => {
      const newAllBlocks = enrichBlocksWithChildren([...state.allBlocks, block])
      const filtered = filterByParent(newAllBlocks, state.allConnections, state.currentParentBlockId)
      return { allBlocks: newAllBlocks, blocks: filtered.blocks, connections: filtered.connections }
    })
  },
  updateBlock: (id, data) => {
    get().pushUndo('Update block')
    set((state) => {
      const newAllBlocks = enrichBlocksWithChildren(
        state.allBlocks.map((b) => (b.id === id ? { ...b, ...data } : b))
      )
      const filtered = filterByParent(newAllBlocks, state.allConnections, state.currentParentBlockId)
      return { allBlocks: newAllBlocks, blocks: filtered.blocks, connections: filtered.connections }
    })
  },
  removeBlock: (id) => {
    get().pushUndo('Remove block')
    set((state) => {
      // Удаляем блок и все вложенные рекурсивно
      const idsToRemove = new Set<string>()
      function collectChildren(parentId: string) {
        idsToRemove.add(parentId)
        state.allBlocks.filter(b => b.parentBlockId === parentId).forEach(b => collectChildren(b.id))
      }
      collectChildren(id)
      const newAllBlocks = enrichBlocksWithChildren(state.allBlocks.filter(b => !idsToRemove.has(b.id)))
      const newAllConns = state.allConnections.filter(c => !idsToRemove.has(c.fromBlockId) && !idsToRemove.has(c.toBlockId))
      const filtered = filterByParent(newAllBlocks, newAllConns, state.currentParentBlockId)
      return {
        allBlocks: newAllBlocks,
        allConnections: newAllConns,
        blocks: filtered.blocks,
        connections: filtered.connections,
        selectedBlockId: state.selectedBlockId === id ? null : state.selectedBlockId,
      }
    })
  },

  allConnections: [],
  setAllConnections: (connections) => {
    const { currentParentBlockId, allBlocks } = get()
    const filtered = filterByParent(allBlocks, connections, currentParentBlockId)
    set({ allConnections: connections, blocks: filtered.blocks, connections: filtered.connections })
  },

  connections: [],
  setConnections: (connections) => set({ connections }),
  addConnection: (connection) => {
    get().pushUndo('Add connection')
    set((state) => {
      const newAllConns = [...state.allConnections, connection]
      const filtered = filterByParent(state.allBlocks, newAllConns, state.currentParentBlockId)
      return { allConnections: newAllConns, blocks: filtered.blocks, connections: filtered.connections }
    })
  },
  updateConnection: (id, data) =>
    set((state) => {
      const newAllConns = state.allConnections.map((c) => c.id === id ? { ...c, ...data } : c)
      const filtered = filterByParent(state.allBlocks, newAllConns, state.currentParentBlockId)
      return { allConnections: newAllConns, connections: filtered.connections }
    }),
  removeConnection: (id) => {
    get().pushUndo('Remove connection')
    set((state) => {
      const newAllConns = state.allConnections.filter((c) => c.id !== id)
      const filtered = filterByParent(state.allBlocks, newAllConns, state.currentParentBlockId)
      return {
        allConnections: newAllConns,
        connections: filtered.connections,
        selectedConnectionId: state.selectedConnectionId === id ? null : state.selectedConnectionId,
      }
    })
  },

  // Навигация подсхем
  currentParentBlockId: null,
  breadcrumbs: [{ id: null, name: '' }],
  drillDown: (blockId) =>
    set((state) => {
      const block = state.allBlocks.find(b => b.id === blockId)
      if (!block) return state
      const newParent = blockId
      const filtered = filterByParent(state.allBlocks, state.allConnections, newParent)
      return {
        currentParentBlockId: newParent,
        breadcrumbs: [...state.breadcrumbs, { id: blockId, name: block.label + ': ' + block.name }],
        blocks: filtered.blocks,
        connections: filtered.connections,
        selectedBlockId: null,
        selectedConnectionId: null,
      }
    }),
  drillUp: () =>
    set((state) => {
      if (state.breadcrumbs.length <= 1) return state
      const newBreadcrumbs = state.breadcrumbs.slice(0, -1)
      const newParent = newBreadcrumbs[newBreadcrumbs.length - 1].id
      const filtered = filterByParent(state.allBlocks, state.allConnections, newParent)
      return {
        currentParentBlockId: newParent,
        breadcrumbs: newBreadcrumbs,
        blocks: filtered.blocks,
        connections: filtered.connections,
        selectedBlockId: null,
        selectedConnectionId: null,
      }
    }),
  drillToRoot: () =>
    set((state) => {
      const filtered = filterByParent(state.allBlocks, state.allConnections, null)
      return {
        currentParentBlockId: null,
        breadcrumbs: [{ id: null, name: '' }],
        blocks: filtered.blocks,
        connections: filtered.connections,
        selectedBlockId: null,
        selectedConnectionId: null,
      }
    }),

  selectedBlockId: null,
  setSelectedBlockId: (id) =>
    set({ selectedBlockId: id, selectedConnectionId: null, rightPanelOpen: !!id, selectedBlockIds: new Set() }),
  selectedConnectionId: null,
  setSelectedConnectionId: (id) =>
    set({
      selectedConnectionId: id,
      selectedBlockId: null,
      rightPanelOpen: !!id,
      rightPanelTab: 'connection',
      selectedBlockIds: new Set(),
    }),

  // Мультивыбор
  selectedBlockIds: new Set<string>(),
  toggleBlockSelection: (id) =>
    set((state) => {
      const next = new Set(state.selectedBlockIds)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return { selectedBlockIds: next, selectedBlockId: null, selectedConnectionId: null }
    }),
  selectAllBlocks: () =>
    set((state) => ({
      selectedBlockIds: new Set(state.blocks.map(b => b.id)),
      selectedBlockId: null,
      selectedConnectionId: null,
    })),
  clearMultiSelection: () => set({ selectedBlockIds: new Set() }),
  removeSelectedBlocks: () => {
    const { selectedBlockIds, pushUndo } = get()
    if (selectedBlockIds.size === 0) return
    pushUndo('Remove selected blocks')
    set((state) => {
      const idsToRemove = new Set<string>()
      function collectChildren(parentId: string) {
        idsToRemove.add(parentId)
        state.allBlocks.filter(b => b.parentBlockId === parentId).forEach(b => collectChildren(b.id))
      }
      for (const id of selectedBlockIds) collectChildren(id)
      const newAllBlocks = enrichBlocksWithChildren(state.allBlocks.filter(b => !idsToRemove.has(b.id)))
      const newAllConns = state.allConnections.filter(c => !idsToRemove.has(c.fromBlockId) && !idsToRemove.has(c.toBlockId))
      const filtered = filterByParent(newAllBlocks, newAllConns, state.currentParentBlockId)
      return {
        allBlocks: newAllBlocks,
        allConnections: newAllConns,
        blocks: filtered.blocks,
        connections: filtered.connections,
        selectedBlockId: null,
        selectedBlockIds: new Set(),
      }
    })
  },

  messages: [],
  setMessages: (messages) => set({ messages }),
  addMessage: (message) =>
    set((state) => ({ messages: [...state.messages, message] })),
  currentSessionId: null,
  setCurrentSessionId: (id) => set({ currentSessionId: id }),

  rightPanelOpen: false,
  setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
  bottomPanelOpen: true,
  setBottomPanelOpen: (open) => set({ bottomPanelOpen: open }),
  bottomPanelTab: 'chat',
  setBottomPanelTab: (tab) => set({ bottomPanelTab: tab }),
  dataPanelMode: null,
  setDataPanelMode: (m) => set({ dataPanelMode: m }),
  activeLayer: 'canvas',
  setActiveLayer: (layer) => set({ activeLayer: layer }),
  rightPanelTab: 'why',
  setRightPanelTab: (tab) => set({ rightPanelTab: tab }),

  isLoading: false,
  setIsLoading: (loading) => set({ isLoading: loading }),
  isSending: false,
  setIsSending: (sending) => set({ isSending: sending }),

  // Kanon
  kanonVerdicts: {},
  setKanonVerdicts: (v) => set({ kanonVerdicts: v }),

  // Стратег
  strategistReport: null,
  setStrategistReport: (report) => set({ strategistReport: report }),
  strategistLoading: false,
  setStrategistLoading: (loading) => set({ strategistLoading: loading }),
  strategistChat: [],
  addStrategistChatMessage: (msg) =>
    set((state) => ({ strategistChat: [...state.strategistChat, msg] })),
  clearStrategistChat: () => set({ strategistChat: [] }),
  strategistChatLoading: false,
  setStrategistChatLoading: (loading) => set({ strategistChatLoading: loading }),

  // Экономика
  economicsReport: null,
  setEconomicsReport: (report) => set({ economicsReport: report }),

  // Симулятор
  personas: [],
  setPersonas: (personas) => set({ personas }),
  addPersona: (persona) =>
    set((state) => ({ personas: [...state.personas, persona] })),
  removePersona: (id) =>
    set((state) => ({
      personas: state.personas.filter((p) => p.id !== id),
      selectedPersonaId: state.selectedPersonaId === id ? null : state.selectedPersonaId,
    })),
  simulationReport: null,
  setSimulationReport: (report) => set({ simulationReport: report }),
  simulationReports: [],
  setSimulationReports: (reports) => set({ simulationReports: reports }),
  simulatorLoading: false,
  setSimulatorLoading: (loading) => set({ simulatorLoading: loading }),
  selectedPersonaId: null,
  setSelectedPersonaId: (id) => set({ selectedPersonaId: id }),

  // Фреймы
  frames: [],
  addFrame: (frame) => set((state) => ({ frames: [...state.frames, frame] })),
  updateFrame: (id, data) => set((state) => ({
    frames: state.frames.map(f => f.id === id ? { ...f, ...data } : f),
  })),
  removeFrame: (id) => set((state) => ({
    frames: state.frames.filter(f => f.id !== id),
  })),

  // Undo/Redo
  canUndo: false,
  canRedo: false,
  pushUndo: (label?: string) => {
    const { allBlocks, allConnections } = get()
    undoManager.push({
      allBlocks: JSON.parse(JSON.stringify(allBlocks)),
      allConnections: JSON.parse(JSON.stringify(allConnections)),
      timestamp: Date.now(),
      label,
    })
    set({ canUndo: undoManager.canUndo, canRedo: undoManager.canRedo })
  },
  undo: () => {
    const { allBlocks, allConnections, currentParentBlockId } = get()
    const currentSnapshot: Snapshot = {
      allBlocks: JSON.parse(JSON.stringify(allBlocks)),
      allConnections: JSON.parse(JSON.stringify(allConnections)),
      timestamp: Date.now(),
    }
    undoManager.setApplying(true)
    const prev = undoManager.undo(currentSnapshot)
    if (prev) {
      const enriched = enrichBlocksWithChildren(prev.allBlocks)
      const filtered = filterByParent(enriched, prev.allConnections, currentParentBlockId)
      set({
        allBlocks: enriched,
        allConnections: prev.allConnections,
        blocks: filtered.blocks,
        connections: filtered.connections,
        canUndo: undoManager.canUndo,
        canRedo: undoManager.canRedo,
      })
    }
    undoManager.setApplying(false)
  },
  redo: () => {
    const { allBlocks, allConnections, currentParentBlockId } = get()
    const currentSnapshot: Snapshot = {
      allBlocks: JSON.parse(JSON.stringify(allBlocks)),
      allConnections: JSON.parse(JSON.stringify(allConnections)),
      timestamp: Date.now(),
    }
    undoManager.setApplying(true)
    const next = undoManager.redo(currentSnapshot)
    if (next) {
      const enriched = enrichBlocksWithChildren(next.allBlocks)
      const filtered = filterByParent(enriched, next.allConnections, currentParentBlockId)
      set({
        allBlocks: enriched,
        allConnections: next.allConnections,
        blocks: filtered.blocks,
        connections: filtered.connections,
        canUndo: undoManager.canUndo,
        canRedo: undoManager.canRedo,
      })
    }
    undoManager.setApplying(false)
  },
}))
