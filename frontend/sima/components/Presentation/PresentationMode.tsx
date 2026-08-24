'use client'

import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import {
  X,
  ChevronLeft,
  ChevronRight,
  Layers,
  ArrowRight,
  Target,
  Zap,
  BarChart3,
} from 'lucide-react'
import type { BlockData, ConnectionData } from '@/sima/types'
import { BLOCK_TYPE_COLORS } from '@/sima/types'
import ReactMarkdown from 'react-markdown'

interface Props {
  onClose: () => void
}

type Slide = {
  type: 'title' | 'block' | 'connections' | 'summary'
  title: string
  block?: BlockData
  connections?: ConnectionData[]
  allBlocks?: BlockData[]
}

export default function PresentationMode({ onClose }: Props) {
  const t = useTranslations('sima_presentation')
  const { project, allBlocks, allConnections } = useSimaStore()
  const [currentSlide, setCurrentSlide] = useState(0)

  // Генерируем слайды
  const slides: Slide[] = (() => {
    if (!project) return []
    const s: Slide[] = []

    // 1. Титульный слайд
    s.push({ type: 'title', title: project.name })

    // 2. По блоку на слайд (корневые)
    const rootBlocks = allBlocks.filter(b => !b.parentBlockId)
    for (const block of rootBlocks) {
      const relatedConns = allConnections.filter(
        c => c.fromBlockId === block.id || c.toBlockId === block.id
      )
      s.push({ type: 'block', title: block.name, block, connections: relatedConns, allBlocks: rootBlocks })
    }

    // 3. Сводка связей
    if (allConnections.length > 0) {
      s.push({ type: 'connections', title: t('slide_connections_title'), connections: allConnections, allBlocks: rootBlocks })
    }

    // 4. Итоговый слайд
    s.push({ type: 'summary', title: t('slide_summary_title'), allBlocks: rootBlocks })

    return s
  })()

  const totalSlides = slides.length
  const slide = slides[currentSlide]

  const goNext = useCallback(() => {
    setCurrentSlide(p => Math.min(p + 1, totalSlides - 1))
  }, [totalSlides])

  const goPrev = useCallback(() => {
    setCurrentSlide(p => Math.max(p - 1, 0))
  }, [])

  // Горячие клавиши
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); goNext() }
      if (e.key === 'ArrowLeft') { e.preventDefault(); goPrev() }
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [goNext, goPrev, onClose])

  if (!slide) return null

  const getBlockName = (id: string) => {
    const b = allBlocks.find(b => b.id === id)
    return b ? `${b.label}: ${b.name}` : id
  }

  return (
    <div className="fixed inset-0 z-[100] bg-sima-bg flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-3 bg-sima-surface/50 backdrop-blur-sm border-b border-sima-border">
        <div className="flex items-center gap-3">
          <Layers className="w-5 h-5 text-sima-primary" />
          <span className="text-sm font-semibold text-sima-text">{project?.name}{t('header_suffix')}</span>
        </div>
        <div className="flex items-center gap-4">
          <span className="text-xs text-sima-textDim">{currentSlide + 1} / {totalSlides}</span>
          <button
            onClick={onClose}
            className="p-2 hover:bg-sima-surfaceLight rounded-lg transition-colors text-sima-textMuted"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 flex items-center justify-center p-8 overflow-y-auto">
        <div className="max-w-4xl w-full">
          {slide.type === 'title' && (
            <div className="text-center space-y-6 animate-fadeIn">
              <div className="w-20 h-20 rounded-2xl bg-gradient-to-br from-sima-primary to-purple-500 mx-auto flex items-center justify-center">
                <Layers className="w-10 h-10 text-white" />
              </div>
              <h1 className="text-4xl font-bold text-sima-text">{project?.name}</h1>
              {project?.description && (
                <p className="text-lg text-sima-textMuted max-w-2xl mx-auto">{project.description}</p>
              )}
              {project?.goal && (
                <div className="inline-flex items-center gap-2 px-4 py-2 bg-sima-primary/10 border border-sima-primary/30 rounded-xl text-sima-primary text-sm">
                  <Target className="w-4 h-4" />
                  {project.goal}
                </div>
              )}
              <div className="flex items-center justify-center gap-6 text-sm text-sima-textDim">
                <span>{t('blocks_count', { count: allBlocks.filter(b => !b.parentBlockId).length })}</span>
                <span>{t('connections_count', { count: allConnections.length })}</span>
              </div>
            </div>
          )}

          {slide.type === 'block' && slide.block && (
            <div className="space-y-6 animate-fadeIn">
              <div className="flex items-center gap-4">
                <div
                  className="w-14 h-14 rounded-xl flex items-center justify-center text-white text-lg font-bold shadow-lg"
                  style={{ backgroundColor: slide.block.color || '#6366F1' }}
                >
                  {slide.block.label}
                </div>
                <div>
                  <h2 className="text-2xl font-bold text-sima-text">{slide.block.name}</h2>
                  <div className="flex items-center gap-2 mt-1">
                    <span
                      className="px-2 py-0.5 text-[10px] rounded-full text-white"
                      style={{ backgroundColor: BLOCK_TYPE_COLORS[slide.block.type as keyof typeof BLOCK_TYPE_COLORS] || '#6366F1' }}
                    >
                      {slide.block.type}
                    </span>
                    <span className="text-xs text-sima-textDim">{t('layer_label', { value: slide.block.layer })}</span>
                  </div>
                </div>
              </div>

              {slide.block.description && (
                <div className="bg-sima-surface rounded-xl border border-sima-border p-5">
                  <h3 className="text-xs font-semibold text-sima-textDim uppercase tracking-wider mb-2">{t('description_heading')}</h3>
                  <div className="prose prose-invert prose-sm max-w-none">
                    <ReactMarkdown>{slide.block.description}</ReactMarkdown>
                  </div>
                </div>
              )}

              <div className="grid grid-cols-2 gap-4">
                {slide.block.businessValue && (
                  <div className="bg-sima-surface rounded-xl border border-sima-border p-4">
                    <div className="flex items-center gap-2 mb-2">
                      <BarChart3 className="w-4 h-4 text-green-400" />
                      <h3 className="text-xs font-semibold text-green-400 uppercase">{t('business_value_heading')}</h3>
                    </div>
                    <p className="text-sm text-sima-textMuted">{slide.block.businessValue}</p>
                  </div>
                )}
                {slide.block.userBenefit && (
                  <div className="bg-sima-surface rounded-xl border border-sima-border p-4">
                    <div className="flex items-center gap-2 mb-2">
                      <Target className="w-4 h-4 text-blue-400" />
                      <h3 className="text-xs font-semibold text-blue-400 uppercase">{t('user_benefit_heading')}</h3>
                    </div>
                    <p className="text-sm text-sima-textMuted">{slide.block.userBenefit}</p>
                  </div>
                )}
                {slide.block.problemSolved && (
                  <div className="bg-sima-surface rounded-xl border border-sima-border p-4">
                    <div className="flex items-center gap-2 mb-2">
                      <Zap className="w-4 h-4 text-amber-400" />
                      <h3 className="text-xs font-semibold text-amber-400 uppercase">{t('problem_solved_heading')}</h3>
                    </div>
                    <p className="text-sm text-sima-textMuted">{slide.block.problemSolved}</p>
                  </div>
                )}
                {slide.block.goal && (
                  <div className="bg-sima-surface rounded-xl border border-sima-border p-4">
                    <div className="flex items-center gap-2 mb-2">
                      <Target className="w-4 h-4 text-purple-400" />
                      <h3 className="text-xs font-semibold text-purple-400 uppercase">{t('goal_heading')}</h3>
                    </div>
                    <p className="text-sm text-sima-textMuted">{slide.block.goal}</p>
                  </div>
                )}
              </div>

              {/* Связи блока */}
              {slide.connections && slide.connections.length > 0 && (
                <div className="bg-sima-surface rounded-xl border border-sima-border p-4">
                  <h3 className="text-xs font-semibold text-sima-textDim uppercase tracking-wider mb-3">{t('connections_heading')}</h3>
                  <div className="space-y-2">
                    {slide.connections.map(c => (
                      <div key={c.id} className="flex items-center gap-2 text-sm text-sima-textMuted">
                        <span className="text-sima-text font-medium">{getBlockName(c.fromBlockId)}</span>
                        <ArrowRight className="w-4 h-4 text-sima-primary shrink-0" />
                        <span className="text-sima-text font-medium">{getBlockName(c.toBlockId)}</span>
                        {c.label && <span className="text-xs text-sima-textDim ml-2">({c.label})</span>}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {slide.type === 'connections' && (
            <div className="space-y-6 animate-fadeIn">
              <h2 className="text-2xl font-bold text-sima-text text-center">{slide.title}</h2>
              <div className="grid grid-cols-1 gap-2 max-h-[60vh] overflow-y-auto">
                {(slide.connections || []).map(c => (
                  <div key={c.id} className="flex items-center gap-3 px-4 py-3 bg-sima-surface rounded-xl border border-sima-border">
                    <span className="text-sm font-medium text-sima-text">{getBlockName(c.fromBlockId)}</span>
                    <ArrowRight className="w-4 h-4 text-sima-primary shrink-0" />
                    <span className="text-sm font-medium text-sima-text">{getBlockName(c.toBlockId)}</span>
                    <span className="text-[10px] px-2 py-0.5 bg-sima-surfaceLight rounded text-sima-textDim ml-auto">{c.type}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {slide.type === 'summary' && (
            <div className="text-center space-y-6 animate-fadeIn">
              <h2 className="text-3xl font-bold text-sima-text">{t('summary_heading')}</h2>
              <div className="grid grid-cols-3 gap-6 max-w-lg mx-auto">
                <div className="bg-sima-surface rounded-xl border border-sima-border p-5">
                  <div className="text-3xl font-bold text-sima-primary">{(slide.allBlocks || []).length}</div>
                  <div className="text-xs text-sima-textDim mt-1">{t('stat_blocks')}</div>
                </div>
                <div className="bg-sima-surface rounded-xl border border-sima-border p-5">
                  <div className="text-3xl font-bold text-green-400">{allConnections.length}</div>
                  <div className="text-xs text-sima-textDim mt-1">{t('stat_connections')}</div>
                </div>
                <div className="bg-sima-surface rounded-xl border border-sima-border p-5">
                  <div className="text-3xl font-bold text-amber-400">
                    {new Set((slide.allBlocks || []).map(b => b.type)).size}
                  </div>
                  <div className="text-xs text-sima-textDim mt-1">{t('stat_types')}</div>
                </div>
              </div>
              {/* Распределение по типам */}
              <div className="flex flex-wrap justify-center gap-3">
                {Object.entries(
                  (slide.allBlocks || []).reduce((acc, b) => {
                    acc[b.type] = (acc[b.type] || 0) + 1
                    return acc
                  }, {} as Record<string, number>)
                ).map(([type, count]) => (
                  <div key={type} className="flex items-center gap-2 px-3 py-1.5 bg-sima-surface rounded-lg border border-sima-border">
                    <div
                      className="w-3 h-3 rounded-full"
                      style={{ backgroundColor: BLOCK_TYPE_COLORS[type as keyof typeof BLOCK_TYPE_COLORS] || '#6366F1' }}
                    />
                    <span className="text-xs text-sima-textMuted">{type}: {count}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Navigation */}
      <div className="flex items-center justify-between px-6 py-3 bg-sima-surface/50 backdrop-blur-sm border-t border-sima-border">
        <div className="flex items-center gap-1">
          {/* Явная кнопка выхода в нижней панели: верхний ✕ перекрывается
              шапкой приложения, поэтому здесь — гарантированный выход. */}
          <button
            onClick={onClose}
            className="flex items-center gap-2 px-4 py-2 text-sm text-sima-textMuted hover:text-sima-text transition-colors"
          >
            <X className="w-4 h-4" />
            {t('exit_button')}
          </button>
          <button
            onClick={goPrev}
            disabled={currentSlide === 0}
            className="flex items-center gap-2 px-4 py-2 text-sm text-sima-textMuted hover:text-sima-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            <ChevronLeft className="w-4 h-4" />
            {t('back_button')}
          </button>
        </div>

        {/* Progress dots */}
        <div className="flex items-center gap-1.5">
          {slides.map((_, i) => (
            <button
              key={i}
              onClick={() => setCurrentSlide(i)}
              className={`w-2 h-2 rounded-full transition-all ${
                i === currentSlide
                  ? 'bg-sima-primary w-6'
                  : 'bg-sima-border hover:bg-sima-textDim'
              }`}
            />
          ))}
        </div>

        <button
          onClick={goNext}
          disabled={currentSlide === totalSlides - 1}
          className="flex items-center gap-2 px-4 py-2 text-sm text-sima-textMuted hover:text-sima-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        >
          {t('next_button')}
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  )
}
