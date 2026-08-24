// Типы для СИМА

export type TaskKind = 'product' | 'book' | 'idea'

export interface ProjectData {
  id: string
  name: string
  taskKind?: TaskKind
  owner?: string | null
  description?: string | null
  goal?: string | null
  targetAudience?: string | null
  mvpDescription?: string | null
  idealProduct?: string | null
  narrativeProblem?: string | null
  narrativeSolution?: string | null
  narrativeHowItWorks?: string | null
  narrativeProductMap?: string | null
  narrativeDecisions?: string | null
  generatedTZ?: string | null
  createdAt: string
  updatedAt: string
  blocks: BlockData[]
  connections: ConnectionData[]
}

export interface BlockData {
  id: string
  projectId: string
  parentBlockId?: string | null  // если вложен в подсхему
  name: string
  label: string
  type: BlockType
  layer: BlockLayer
  color: string
  positionX: number
  positionY: number
  status?: BlockStatus  // жизненный статус (Sima Remix): done/wip/q/idea
  isMvp?: boolean       // войдёт ли в первую версию (фильтр «только MVP»)
  hasChildren?: boolean // вычисляемое: есть ли вложенные блоки
  // Зачем
  businessValue?: string | null
  userBenefit?: string | null
  problemSolved?: string | null
  impactIfRemoved?: string | null
  metrics?: string | null
  // Что
  description?: string | null
  goal?: string | null
  sourceTake?: string | null  // «что берём из этого источника» (Sima Remix)
  inputData?: string | null
  outputData?: string | null
  acceptanceCriteria?: string | null
  userFlow?: string | null
  edgeCases?: string | null
  // Как
  implementation?: string | null
  blockTechStack?: string | null
  apiEndpoints?: string | null
  dataModel?: string | null
  filesToCreate?: string | null
  technicalDeps?: string | null
  estimatedComplexity: string
  // Контекст
  analogues?: string | null
  alternatives?: string | null
  risks?: string | null
  futureEvolution?: string | null
  problems?: string | null
  // Нарратив
  blockNarrative?: string | null
  createdAt: string
  updatedAt: string
}

export interface ConnectionData {
  id: string
  projectId: string
  parentBlockId?: string | null  // если связь внутри подсхемы
  fromBlockId: string
  toBlockId: string
  type: ConnectionType
  label?: string | null
  description?: string | null
  dataDescription?: string | null
  dataFormat?: string | null
  dataExample?: string | null
  dataVolume?: string | null
  condition?: string | null
  errorHandling?: string | null
  businessMeaning?: string | null
  color: string
  style: string
  animated: boolean
  direction?: ConnectionDirection
  createdAt: string
  updatedAt: string
}

// Направление связи между кубиками (Sima Remix):
// one_way — от источника к задаче, two_way — двусторонняя, to_task — в задачу.
export type ConnectionDirection = 'one_way' | 'two_way' | 'to_task'

export type BlockType = 'core' | 'feature' | 'integration' | 'data' | 'agent'
export type BlockLayer = 'problem' | 'solution' | 'mvp' | 'medium' | 'ideal'
// Жизненный статус блока (Sima Remix): готово / в работе / под вопросом / идея.
export type BlockStatus = 'done' | 'wip' | 'q' | 'idea'
export const BLOCK_STATUS_COLORS: Record<BlockStatus, string> = {
  done: '#34c759',
  wip:  '#f5a623',
  q:    '#8e8e93',
  idea: '#5b8def',
}
export type ConnectionType = 'data_flow' | 'trigger' | 'dependency' | 'condition'

export interface MessageData {
  id: string
  projectId: string
  role: 'user' | 'assistant' | 'system'
  content: string
  actions?: string | null
  createdAt: string
}

export interface DecisionData {
  id: string
  projectId: string
  title: string
  description: string
  reasoning?: string | null
  alternatives?: string | null
  sessionId?: string | null
  createdAt: string
}

// AI Actions — действия, которые AI выполняет в ответ на сообщения пользователя
export type AIAction =
  | { type: 'CREATE_BLOCK'; data: Partial<BlockData> & { name: string; label: string } }
  | { type: 'UPDATE_BLOCK'; blockId: string; data: Partial<BlockData> }
  | { type: 'DELETE_BLOCK'; blockId: string }
  | { type: 'CREATE_CONNECTION'; data: { fromLabel: string; toLabel: string; label?: string; type?: ConnectionType } }
  | { type: 'DELETE_CONNECTION'; connectionId: string }
  | { type: 'UPDATE_PROJECT'; data: Partial<ProjectData> }
  | { type: 'ADD_DECISION'; data: { title: string; description: string; reasoning?: string } }

export interface AIResponse {
  message: string
  actions: AIAction[]
}

// Чеклист блока
export interface BlockChecklist {
  business: {
    businessValue: boolean
    userBenefit: boolean
    problemSolved: boolean
    impactIfRemoved: boolean
    metrics: boolean
  }
  functional: {
    description: boolean
    goal: boolean
    inputData: boolean
    outputData: boolean
    acceptanceCriteria: boolean
    userFlow: boolean
    edgeCases: boolean
  }
  technical: {
    implementation: boolean
    blockTechStack: boolean
    apiEndpoints: boolean
    dataModel: boolean
    filesToCreate: boolean
    estimatedComplexity: boolean
  }
  context: {
    analogues: boolean
    alternatives: boolean
    risks: boolean
    futureEvolution: boolean
  }
}

export function calculateBlockChecklist(block: BlockData): BlockChecklist {
  return {
    business: {
      businessValue: !!block.businessValue,
      userBenefit: !!block.userBenefit,
      problemSolved: !!block.problemSolved,
      impactIfRemoved: !!block.impactIfRemoved,
      metrics: !!block.metrics,
    },
    functional: {
      description: !!block.description,
      goal: !!block.goal,
      inputData: !!block.inputData,
      outputData: !!block.outputData,
      acceptanceCriteria: !!block.acceptanceCriteria,
      userFlow: !!block.userFlow,
      edgeCases: !!block.edgeCases,
    },
    technical: {
      implementation: !!block.implementation,
      blockTechStack: !!block.blockTechStack,
      apiEndpoints: !!block.apiEndpoints,
      dataModel: !!block.dataModel,
      filesToCreate: !!block.filesToCreate,
      estimatedComplexity: !!block.estimatedComplexity,
    },
    context: {
      analogues: !!block.analogues,
      alternatives: !!block.alternatives,
      risks: !!block.risks,
      futureEvolution: !!block.futureEvolution,
    },
  }
}

export function calculateBlockProgress(block: BlockData): number {
  const checklist = calculateBlockChecklist(block)
  const allValues = [
    ...Object.values(checklist.business),
    ...Object.values(checklist.functional),
    ...Object.values(checklist.technical),
    ...Object.values(checklist.context),
  ]
  const filled = allValues.filter(Boolean).length
  return Math.round((filled / allValues.length) * 100)
}

export function calculateSectionProgress(section: Record<string, boolean>): number {
  const values = Object.values(section)
  const filled = values.filter(Boolean).length
  return Math.round((filled / values.length) * 100)
}

// ═══════════════════════════════════════════════════
// Типы для StrategistService
// ═══════════════════════════════════════════════════

export interface StrategistVerdict {
  emoji: '🟢' | '🟡' | '🔴'
  oneLiner: string
  confidence: number // 0-100
}

export interface StrategistTopAction {
  priority: 1 | 2 | 3
  action: string
  why: string
  impact: string
  relatedBlockIds?: string[]
  applyAction?: {
    type: 'detach_block' | 'create_block' | 'modify_description' | 'add_connection' | 'change_layer'
    params: Record<string, unknown>
  }
}

export interface StrategistInsight {
  type: 'strength' | 'weakness' | 'opportunity' | 'threat' | 'blindspot' | 'alternative'
  icon: string
  title: string
  body: string
  relatedBlockIds: string[]
  actionable: boolean
  action?: string
}

export interface StrategistSection {
  title: string
  insights: StrategistInsight[]
}

export interface StrategistReport {
  id: string
  projectId: string
  scope: 'full' | 'layer' | 'block'
  scopeId?: string | null
  verdict: StrategistVerdict
  topActions: StrategistTopAction[]
  sections: StrategistSection[]
  analysisTime: number
  createdAt: string
}

// ═══════════════════════════════════════════════════
// Типы для SimulatorService
// ═══════════════════════════════════════════════════

export interface PersonaData {
  id: string
  projectId: string
  name: string
  role: string
  techLevel: 'none' | 'basic' | 'intermediate' | 'advanced' | 'expert'
  domainKnowledge: 'none' | 'basic' | 'deep'
  goal: string
  timeBudget: string
  motivation: 'high' | 'medium' | 'low'
  patienceLevel: 'low' | 'medium' | 'high'
  traits: string[]
  fears: string[]
  previousExperience: string[]
  constraints: string[]
  archetype: 'best_case' | 'average' | 'worst_case' | 'custom'
  createdAt: string
}

export type FrictionType =
  | 'confusion'
  | 'complexity'
  | 'missing_info'
  | 'technical_barrier'
  | 'time_cost'
  | 'trust_barrier'
  | 'dead_end'
  | 'irreversible_action'
  | 'context_switch'
  | 'cognitive_overload'

export type FrictionSeverity = 'minor' | 'moderate' | 'major' | 'wall'

export interface FrictionPoint {
  stepNumber: number
  blockId?: string
  type: FrictionType
  severity: FrictionSeverity
  description: string
  userThinking: string
  userEmotion: string
  estimatedDropoff: number
  recommendation: {
    action: string
    effort: 'low' | 'medium' | 'high'
    impact: string
  }
}

export interface SimulationStep {
  stepNumber: number
  blockId?: string
  blockName: string
  action: string
  userSees: string
  userUnderstands: string
  userDoesNotUnderstand: string
  userDoes: string
  userFeels: string
  cumulativeDropoff: number
  stepDropoff: number
  timeSpent: string
  friction?: FrictionPoint
  status: '🟢' | '🟡' | '🔴'
}

export interface SimulationSummary {
  totalSteps: number
  timeToValue: string
  completionRate: number
  overallVerdict: '🟢' | '🟡' | '🔴'
  oneLiner: string
}

export interface SimulationRecommendation {
  priority: 1 | 2 | 3
  action: string
  why: string
  impact: string
  relatedBlockIds: string[]
}

export interface SimulationReport {
  id: string
  projectId: string
  personaId: string
  personaName: string
  scope: string
  summary: SimulationSummary
  steps: SimulationStep[]
  frictionPoints: FrictionPoint[]
  recommendations: SimulationRecommendation[]
  analysisTime: number
  createdAt: string
}

// Цвета для типов блоков
export const BLOCK_TYPE_COLORS: Record<BlockType, string> = {
  core: '#6366F1',
  feature: '#22D3EE',
  integration: '#34D399',
  data: '#FBBF24',
  agent: '#A78BFA',
}

// Цвета для типов связей
export const CONNECTION_TYPE_COLORS: Record<ConnectionType, string> = {
  data_flow: '#6366F1',
  trigger: '#22D3EE',
  dependency: '#FBBF24',
  condition: '#F87171',
}
