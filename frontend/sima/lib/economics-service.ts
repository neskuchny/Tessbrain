/**
 * EconomicsService — типы для экономики решений
 * Серверная логика в Python бэкенде, здесь только типы для фронтенда.
 */

export interface BlockEconomics {
  blockId: string
  blockLabel: string
  blockName: string
  value: number
  complexity: number
  quadrant: 'quick_win' | 'strategic' | 'fill_in' | 'avoid'
  valueReasoning: string
  complexityReasoning: string
  priority: number
  recommendation: string
}

export interface EconomicsReport {
  blocks: BlockEconomics[]
  summary: {
    mvpRecommendation: string[]
    totalComplexity: string
    quickWins: string[]
    strategicInvestments: string[]
    avoidOrDefer: string[]
  }
  overallAdvice: string
  analysisTime: number
}
