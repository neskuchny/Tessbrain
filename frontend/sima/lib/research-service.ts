/**
 * ResearchService — типы для исследования рынка
 * Серверная логика в Python бэкенде, здесь только типы для фронтенда.
 */

export interface Competitor {
  name: string
  description: string
  strengths: string[]
  weaknesses: string[]
  pricing: string
  url?: string
}

export interface MarketTrend {
  trend: string
  relevance: 'high' | 'medium' | 'low'
  description: string
  opportunity: string
}

export interface ResearchReport {
  marketOverview: string
  marketSize: string
  targetSegments: string[]
  competitors: Competitor[]
  trends: MarketTrend[]
  opportunities: string[]
  threats: string[]
  positioning: string
  differentiators: string[]
  goToMarket: string
  analysisTime: number
}
