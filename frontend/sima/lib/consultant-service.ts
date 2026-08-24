/**
 * ConsultantService — «линтер» схемы продукта
 * 
 * Работает на фронтенде, анализирует блоки и связи из store.
 * Выдаёт предупреждения и рекомендации в реальном времени.
 */

import type { BlockData, ConnectionData } from '@/sima/types'
import { calculateBlockProgress } from '@/sima/types'

// ═══════════════════════════════════════════════════
// Типы
// ═══════════════════════════════════════════════════

export type IssueSeverity = 'error' | 'warning' | 'info' | 'tip'

export type IssueCategory =
  | 'completeness'   // незаполненные поля
  | 'structure'      // проблемы структуры (изолированные блоки, циклы)
  | 'quality'        // качество описаний (слишком коротко, нечётко)
  | 'architecture'   // архитектурные проблемы (нет ядра, нет данных)
  | 'connections'    // проблемы связей (нет описания, односторонние)
  | 'layers'         // проблемы слоёв (mvp пуст, перекос)
  | 'best_practice'  // рекомендации лучших практик

export interface ConsultantIssue {
  id: string
  severity: IssueSeverity
  category: IssueCategory
  title: string
  description: string
  blockId?: string       // если привязана к блоку
  connectionId?: string  // если привязана к связи
  suggestion?: string    // что сделать
  autoFixable?: boolean  // можно автоматически исправить
}

export interface ConsultantReport {
  score: number           // 0-100 общий балл качества
  emoji: '🟢' | '🟡' | '🔴'
  oneLiner: string
  issues: ConsultantIssue[]
  stats: {
    totalBlocks: number
    totalConnections: number
    avgBlockProgress: number
    issuesByCategory: Record<IssueCategory, number>
    issuesBySeverity: Record<IssueSeverity, number>
  }
}

// ═══════════════════════════════════════════════════
// Анализаторы
// ═══════════════════════════════════════════════════

let issueCounter = 0
function makeId() { return `issue_${++issueCounter}` }

/** 1. Проверка полноты заполнения блоков */
type T = (key: string, vars?: Record<string, string | number>) => string

function checkCompleteness(blocks: BlockData[], t: T): ConsultantIssue[] {
  const issues: ConsultantIssue[] = []

  for (const block of blocks) {
    const progress = calculateBlockProgress(block)

    if (!block.description) {
      issues.push({
        id: makeId(),
        severity: 'error',
        category: 'completeness',
        title: t('no_description_title', { label: block.label }),
        description: t('no_description_desc', { name: block.name }),
        blockId: block.id,
        suggestion: t('no_description_suggestion'),
        autoFixable: true,
      })
    }

    if (!block.goal) {
      issues.push({
        id: makeId(),
        severity: 'warning',
        category: 'completeness',
        title: t('no_goal_title', { label: block.label }),
        description: t('no_goal_desc', { name: block.name }),
        blockId: block.id,
        suggestion: t('no_goal_suggestion'),
        autoFixable: true,
      })
    }

    if (!block.businessValue) {
      issues.push({
        id: makeId(),
        severity: 'warning',
        category: 'completeness',
        title: t('no_business_value_title', { label: block.label }),
        description: t('no_business_value_desc', { name: block.name }),
        blockId: block.id,
        suggestion: t('no_business_value_suggestion'),
        autoFixable: true,
      })
    }

    if (!block.inputData && !block.outputData) {
      issues.push({
        id: makeId(),
        severity: 'warning',
        category: 'completeness',
        title: t('no_io_data_title', { label: block.label }),
        description: t('no_io_data_desc', { name: block.name }),
        blockId: block.id,
        suggestion: t('no_io_data_suggestion'),
        autoFixable: true,
      })
    }

    if (progress < 20 && block.description) {
      issues.push({
        id: makeId(),
        severity: 'info',
        category: 'completeness',
        title: t('low_progress_title', { label: block.label, progress }),
        description: t('low_progress_desc', { name: block.name }),
        blockId: block.id,
        suggestion: t('low_progress_suggestion'),
        autoFixable: true,
      })
    }
  }

  return issues
}

/** 2. Проверка качества описаний */
function checkQuality(blocks: BlockData[], t: T): ConsultantIssue[] {
  const issues: ConsultantIssue[] = []

  for (const block of blocks) {
    if (block.description && block.description.length < 30) {
      issues.push({
        id: makeId(),
        severity: 'info',
        category: 'quality',
        title: t('short_description_title', { label: block.label }),
        description: t('short_description_desc', { name: block.name }),
        blockId: block.id,
        suggestion: t('short_description_suggestion'),
      })
    }

    if (block.type === 'core' && !block.implementation) {
      issues.push({
        id: makeId(),
        severity: 'warning',
        category: 'quality',
        title: t('core_no_implementation_title', { label: block.label }),
        description: t('core_no_implementation_desc', { name: block.name }),
        blockId: block.id,
        suggestion: t('core_no_implementation_suggestion'),
        autoFixable: true,
      })
    }

    if (block.type === 'core' && !block.risks) {
      issues.push({
        id: makeId(),
        severity: 'info',
        category: 'quality',
        title: t('core_no_risks_title', { label: block.label }),
        description: t('core_no_risks_desc', { name: block.name }),
        blockId: block.id,
        suggestion: t('core_no_risks_suggestion'),
        autoFixable: true,
      })
    }
  }

  return issues
}

/** 3. Проверка структуры (изолированные блоки, циклы) */
function checkStructure(blocks: BlockData[], connections: ConnectionData[], t: T): ConsultantIssue[] {
  const issues: ConsultantIssue[] = []

  if (blocks.length === 0) return issues

  const connectedBlockIds = new Set<string>()
  for (const conn of connections) {
    connectedBlockIds.add(conn.fromBlockId)
    connectedBlockIds.add(conn.toBlockId)
  }

  for (const block of blocks) {
    if (!connectedBlockIds.has(block.id) && blocks.length > 1) {
      issues.push({
        id: makeId(),
        severity: 'warning',
        category: 'structure',
        title: t('isolated_block_title', { label: block.label }),
        description: t('isolated_block_desc', { name: block.name }),
        blockId: block.id,
        suggestion: t('isolated_block_suggestion'),
      })
    }
  }

  const adjacency = new Map<string, string[]>()
  for (const conn of connections) {
    if (!adjacency.has(conn.fromBlockId)) adjacency.set(conn.fromBlockId, [])
    adjacency.get(conn.fromBlockId)!.push(conn.toBlockId)
  }

  const visited = new Set<string>()
  const inStack = new Set<string>()
  let hasCycle = false

  function dfs(nodeId: string) {
    if (inStack.has(nodeId)) { hasCycle = true; return }
    if (visited.has(nodeId)) return
    visited.add(nodeId)
    inStack.add(nodeId)
    for (const neighbor of (adjacency.get(nodeId) || [])) {
      dfs(neighbor)
      if (hasCycle) return
    }
    inStack.delete(nodeId)
  }

  for (const block of blocks) {
    if (!visited.has(block.id)) dfs(block.id)
    if (hasCycle) break
  }

  if (hasCycle) {
    issues.push({
      id: makeId(),
      severity: 'warning',
      category: 'structure',
      title: t('cycle_title'),
      description: t('cycle_desc'),
      suggestion: t('cycle_suggestion'),
    })
  }

  for (const block of blocks) {
    const outCount = connections.filter(c => c.fromBlockId === block.id).length
    if (outCount > 5) {
      issues.push({
        id: makeId(),
        severity: 'info',
        category: 'structure',
        title: t('high_fanout_title', { label: block.label, count: outCount }),
        description: t('high_fanout_desc', { name: block.name, count: outCount }),
        blockId: block.id,
        suggestion: t('high_fanout_suggestion'),
      })
    }
  }

  return issues
}

/** 4. Проверка архитектуры */
function checkArchitecture(blocks: BlockData[], t: T): ConsultantIssue[] {
  const issues: ConsultantIssue[] = []

  if (blocks.length === 0) return issues

  const types = blocks.map(b => b.type)

  if (!types.includes('core') && blocks.length >= 3) {
    issues.push({
      id: makeId(),
      severity: 'warning',
      category: 'architecture',
      title: t('no_core_title'),
      description: t('no_core_desc'),
      suggestion: t('no_core_suggestion'),
    })
  }

  if (!types.includes('data') && blocks.length >= 5) {
    issues.push({
      id: makeId(),
      severity: 'info',
      category: 'architecture',
      title: t('no_data_title'),
      description: t('no_data_desc'),
      suggestion: t('no_data_suggestion'),
    })
  }

  const uniqueTypes = new Set(types)
  if (uniqueTypes.size === 1 && blocks.length >= 4) {
    issues.push({
      id: makeId(),
      severity: 'info',
      category: 'architecture',
      title: t('same_type_title'),
      description: t('same_type_desc', { count: blocks.length, type: types[0] }),
      suggestion: t('same_type_suggestion'),
    })
  }

  return issues
}

/** 5. Проверка связей */
function checkConnections(blocks: BlockData[], connections: ConnectionData[], t: T): ConsultantIssue[] {
  const issues: ConsultantIssue[] = []

  for (const conn of connections) {
    const fromBlock = blocks.find(b => b.id === conn.fromBlockId)
    const toBlock = blocks.find(b => b.id === conn.toBlockId)
    const label = `${fromBlock?.label || '?'} → ${toBlock?.label || '?'}`

    if (!conn.label && !conn.description) {
      issues.push({
        id: makeId(),
        severity: 'info',
        category: 'connections',
        title: t('conn_no_desc_title', { label }),
        description: t('conn_no_desc_desc', { from: fromBlock?.name ?? '', to: toBlock?.name ?? '' }),
        connectionId: conn.id,
        suggestion: t('conn_no_desc_suggestion'),
        autoFixable: true,
      })
    }

    if (!conn.dataDescription && !conn.dataFormat) {
      issues.push({
        id: makeId(),
        severity: 'info',
        category: 'connections',
        title: t('conn_no_format_title', { label }),
        description: t('conn_no_format_desc', { label }),
        connectionId: conn.id,
        suggestion: t('conn_no_format_suggestion'),
        autoFixable: true,
      })
    }
  }

  const connPairs = new Map<string, number>()
  for (const conn of connections) {
    const key = `${conn.fromBlockId}-${conn.toBlockId}`
    connPairs.set(key, (connPairs.get(key) || 0) + 1)
  }
  for (const [pair, count] of connPairs) {
    if (count > 1) {
      const [fromId, toId] = pair.split('-')
      const fromBlock = blocks.find(b => b.id === fromId)
      const toBlock = blocks.find(b => b.id === toId)
      issues.push({
        id: makeId(),
        severity: 'warning',
        category: 'connections',
        title: t('conn_duplicate_title', { fromLabel: fromBlock?.label ?? '', toLabel: toBlock?.label ?? '' }),
        description: t('conn_duplicate_desc', { from: fromBlock?.name ?? '', to: toBlock?.name ?? '', count }),
        suggestion: t('conn_duplicate_suggestion'),
      })
    }
  }

  return issues
}

/** 6. Проверка слоёв */
function checkLayers(blocks: BlockData[], t: T): ConsultantIssue[] {
  const issues: ConsultantIssue[] = []

  if (blocks.length < 3) return issues

  const layers = blocks.map(b => b.layer)
  const mvpCount = layers.filter(l => l === 'mvp').length

  if (mvpCount === 0) {
    issues.push({
      id: makeId(),
      severity: 'error',
      category: 'layers',
      title: t('no_mvp_title'),
      description: t('no_mvp_desc'),
      suggestion: t('no_mvp_suggestion'),
    })
  }

  if (mvpCount === blocks.length && blocks.length > 3) {
    issues.push({
      id: makeId(),
      severity: 'warning',
      category: 'layers',
      title: t('all_mvp_title'),
      description: t('all_mvp_desc', { count: blocks.length }),
      suggestion: t('all_mvp_suggestion'),
    })
  }

  const idealCount = layers.filter(l => l === 'ideal').length
  if (idealCount === 0 && blocks.length >= 5) {
    issues.push({
      id: makeId(),
      severity: 'tip',
      category: 'layers',
      title: t('no_ideal_title'),
      description: t('no_ideal_desc'),
      suggestion: t('no_ideal_suggestion'),
    })
  }

  return issues
}

/** 7. Лучшие практики */
function checkBestPractices(blocks: BlockData[], connections: ConnectionData[], t: T): ConsultantIssue[] {
  const issues: ConsultantIssue[] = []

  if (blocks.length >= 1 && blocks.length < 3) {
    issues.push({
      id: makeId(),
      severity: 'tip',
      category: 'best_practice',
      title: t('few_modules_title'),
      description: t('few_modules_desc', { count: blocks.length }),
      suggestion: t('few_modules_suggestion'),
    })
  }

  if (blocks.length >= 3 && connections.length === 0) {
    issues.push({
      id: makeId(),
      severity: 'error',
      category: 'best_practice',
      title: t('no_connections_title'),
      description: t('no_connections_desc'),
      suggestion: t('no_connections_suggestion'),
    })
  }

  if (blocks.length >= 4 && connections.length > 0 && connections.length < blocks.length - 1) {
    issues.push({
      id: makeId(),
      severity: 'info',
      category: 'best_practice',
      title: t('few_connections_title'),
      description: t('few_connections_desc', { connections: connections.length, blocks: blocks.length }),
      suggestion: t('few_connections_suggestion'),
    })
  }

  return issues
}

// ═══════════════════════════════════════════════════
// Главная функция анализа
// ═══════════════════════════════════════════════════

export function analyzeSchema(blocks: BlockData[], connections: ConnectionData[], t: T): ConsultantReport {
  issueCounter = 0

  const allIssues: ConsultantIssue[] = [
    ...checkCompleteness(blocks, t),
    ...checkQuality(blocks, t),
    ...checkStructure(blocks, connections, t),
    ...checkArchitecture(blocks, t),
    ...checkConnections(blocks, connections, t),
    ...checkLayers(blocks, t),
    ...checkBestPractices(blocks, connections, t),
  ]

  const severityOrder: Record<IssueSeverity, number> = { error: 0, warning: 1, info: 2, tip: 3 }
  allIssues.sort((a, b) => severityOrder[a.severity] - severityOrder[b.severity])

  const issuesByCategory = {} as Record<IssueCategory, number>
  const issuesBySeverity = {} as Record<IssueSeverity, number>
  for (const issue of allIssues) {
    issuesByCategory[issue.category] = (issuesByCategory[issue.category] || 0) + 1
    issuesBySeverity[issue.severity] = (issuesBySeverity[issue.severity] || 0) + 1
  }

  const avgBlockProgress = blocks.length > 0
    ? Math.round(blocks.reduce((sum, b) => sum + calculateBlockProgress(b), 0) / blocks.length)
    : 0

  const errors = issuesBySeverity.error || 0
  const warnings = issuesBySeverity.warning || 0
  const infos = issuesBySeverity.info || 0
  let score = 100
  score -= errors * 10
  score -= warnings * 4
  score -= infos * 1
  score = Math.max(0, Math.min(100, score))

  if (blocks.length === 0) score = 0

  const emoji = score >= 70 ? '🟢' : score >= 40 ? '🟡' : '🔴'

  const oneLiner = blocks.length === 0
    ? t('empty_schema_oneliner')
    : score >= 80
    ? t('great_schema_oneliner', { count: allIssues.length })
    : score >= 50
    ? t('needs_work_oneliner', { errors, warnings })
    : t('critical_oneliner', { errors })

  return {
    score,
    emoji,
    oneLiner,
    issues: allIssues,
    stats: {
      totalBlocks: blocks.length,
      totalConnections: connections.length,
      avgBlockProgress,
      issuesByCategory,
      issuesBySeverity,
    },
  }
}
