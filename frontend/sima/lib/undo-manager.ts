'use client'

import type { BlockData, ConnectionData } from '@/sima/types'

// ═══════════════════════════════════════════════════════
// Undo/Redo менеджер для СИМА
// Хранит снапшоты состояния блоков и связей
// ═══════════════════════════════════════════════════════

export interface Snapshot {
  allBlocks: BlockData[]
  allConnections: ConnectionData[]
  timestamp: number
  label?: string  // описание действия (опционально)
}

const MAX_HISTORY = 50

class UndoManager {
  private undoStack: Snapshot[] = []
  private redoStack: Snapshot[] = []
  private isApplying = false  // предотвращает рекурсию

  /** Очищает историю (при смене проекта) */
  clear() {
    this.undoStack = []
    this.redoStack = []
  }

  /** Сохраняет текущее состояние как снапшот перед изменением */
  push(snapshot: Snapshot) {
    if (this.isApplying) return  // не записываем при undo/redo
    this.undoStack.push(snapshot)
    if (this.undoStack.length > MAX_HISTORY) {
      this.undoStack.shift()
    }
    // При новом действии redo очищается
    this.redoStack = []
  }

  /** Есть ли что отменять */
  get canUndo() { return this.undoStack.length > 0 }

  /** Есть ли что повторять */
  get canRedo() { return this.redoStack.length > 0 }

  get undoCount() { return this.undoStack.length }
  get redoCount() { return this.redoStack.length }

  /**
   * Отменяет последнее действие.
   * Принимает текущее состояние (чтобы положить в redo) и возвращает предыдущее.
   */
  undo(currentSnapshot: Snapshot): Snapshot | null {
    if (this.undoStack.length === 0) return null
    const prev = this.undoStack.pop()!
    this.redoStack.push(currentSnapshot)
    return prev
  }

  /**
   * Повторяет отменённое действие.
   * Принимает текущее состояние (чтобы положить в undo) и возвращает следующее.
   */
  redo(currentSnapshot: Snapshot): Snapshot | null {
    if (this.redoStack.length === 0) return null
    const next = this.redoStack.pop()!
    this.undoStack.push(currentSnapshot)
    return next
  }

  /** Устанавливает флаг "применяется" — push будет игнорироваться */
  setApplying(v: boolean) { this.isApplying = v }
}

// Singleton
export const undoManager = new UndoManager()
