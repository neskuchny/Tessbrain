'use client'

import { useState, useCallback, useRef, useEffect, useMemo } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import type { ProjectData } from '@/sima/types'
import {
  ArrowLeft,
  Sun,
  Moon,
  Sliders,
  PanelRightOpen,
  PanelRightClose,
  ChevronDown,
  ChevronUp,
  FileText,
  Sparkles,
  GripHorizontal,
  Undo2,
  Redo2,
  Save,
  Check,
  Play,
  Layers,
  Plus,
} from 'lucide-react'
import DiagramCanvas from '@/sima/components/Canvas/DiagramCanvas'
import BlockDetailsPanel from '@/sima/components/BlockDetails/BlockDetailsPanel'
import AddSourcePalette from '@/sima/components/Canvas/AddSourcePalette'
import ConnectionDetailsPanel from '@/sima/components/BlockDetails/ConnectionDetailsPanel'
import BottomPanel from '@/sima/components/Chat/BottomPanel'
import MapView from '@/sima/components/Map/MapView'
import SimaTweaks, { applyAccent, readAccent, type SimaAccent } from '@/sima/components/Layout/SimaTweaks'
import TzDocumentView from '@/sima/components/Narrative/TzDocumentView'
import ImplementationPanel from '@/sima/components/Implementation/ImplementationPanel'
import ArtifactsPanel from '@/sima/components/Artifacts/ArtifactsPanel'
import PresentationMode from '@/sima/components/Presentation/PresentationMode'
import ShareProjectPanel from '@/sima/components/Share/ShareProjectPanel'
import { simaFetch } from '@/sima/lib/api'

interface WorkspaceProps {
  onBack: () => void
  userId?: string | null
  projects?: ProjectData[]
  onSwitchProject?: (id: string) => void
}

const MIN_PANEL_HEIGHT = 150
const MAX_PANEL_HEIGHT_RATIO = 0.75 // max 75% of viewport
const DEFAULT_PANEL_HEIGHT = 320

export default function Workspace({ onBack, userId, projects = [], onSwitchProject }: WorkspaceProps) {
  // Тема SIMA (Sima Remix): СВЕТЛАЯ кремовая по умолчанию — это палитра
  // эталонного прототипа («визуальное конструирование — основа инструмента»);
  // тёмная остаётся выбором. Переключение живёт на html[data-sima-theme].
  const [simaTheme, setSimaTheme] = useState<'dark' | 'light'>(() =>
    (typeof window !== 'undefined' &&
      localStorage.getItem('sima_theme') === 'dark') ? 'dark' : 'light')
  useEffect(() => {
    // Живое переключение темы. Снятие атрибута при выходе из SIMA теперь
    // на SimaApp (владелец раздела) — иначе возврат к списку проектов
    // (Workspace unmount) сбрасывал тему на всём разделе.
    if (simaTheme === 'light') document.documentElement.setAttribute('data-sima-theme', 'light')
    else document.documentElement.removeAttribute('data-sima-theme')
    localStorage.setItem('sima_theme', simaTheme)
  }, [simaTheme])

  // Акцент (Sima Remix Tweaks): цвет --sima-primary, persist localStorage.
  const [accent, setAccentState] = useState<SimaAccent>(() => readAccent())
  const [tweaksOpen, setTweaksOpen] = useState(false)
  const setAccent = useCallback((a: SimaAccent) => {
    setAccentState(a)
    localStorage.setItem('sima_accent', a)
    applyAccent(a)
  }, [])
  useEffect(() => { applyAccent(accent) }, [accent])

  // «Поделиться проектом» (P1 мульти-аккаунта): гранты коллегам read/write
  const [showShare, setShowShare] = useState(false)

  // Онбординг первого входа: одна дружелюбная карточка «как этим пользоваться»
  // для человека, который никогда не работал с такой системой. Показывается
  // один раз (localStorage), всегда доступна заново по кнопке «?» в шапке.
  const [showOnboarding, setShowOnboarding] = useState<boolean>(() =>
    typeof window !== 'undefined' && !localStorage.getItem('sima_onboarded'))
  const dismissOnboarding = () => {
    setShowOnboarding(false)
    try { localStorage.setItem('sima_onboarded', '1') } catch { /* ignore */ }
  }

  const t = useTranslations('sima')
  const locale = useLocale()
  const {
    project,
    rightPanelOpen,
    setRightPanelOpen,
    bottomPanelOpen,
    bottomPanelTab,
    setBottomPanelOpen,
    selectedBlockId,
    selectedConnectionId,
    blocks,
    connections,
    activeLayer,
    setActiveLayer,
  } = useSimaStore()

  const [presentationMode, setPresentationMode] = useState(false)
  const [panelHeight, setPanelHeight] = useState(DEFAULT_PANEL_HEIGHT)
  const isDragging = useRef(false)
  const startY = useRef(0)
  const startHeight = useRef(0)

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    isDragging.current = true
    startY.current = e.clientY
    startHeight.current = panelHeight
    document.body.style.cursor = 'ns-resize'
    document.body.style.userSelect = 'none'
  }, [panelHeight])

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDragging.current) return
      const delta = startY.current - e.clientY
      const maxH = window.innerHeight * MAX_PANEL_HEIGHT_RATIO
      const newHeight = Math.min(maxH, Math.max(MIN_PANEL_HEIGHT, startHeight.current + delta))
      setPanelHeight(newHeight)
    }

    const handleMouseUp = () => {
      if (isDragging.current) {
        isDragging.current = false
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
      }
    }

    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)
    return () => {
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
    }
  }, [])

  // Автосохранение (снапшот каждые 90 секунд)
  const [lastSaved, setLastSaved] = useState<Date | null>(null)
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved'>('idle')

  const autoSave = useCallback(async () => {
    if (!project) return
    try {
      setSaveStatus('saving')
      await simaFetch('/snapshots', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, type: 'auto' }),
      })
      setLastSaved(new Date())
      setSaveStatus('saved')
      setTimeout(() => setSaveStatus('idle'), 2000)
    } catch (e) {
      console.error('Autosave failed:', e)
      setSaveStatus('idle')
    }
  }, [project])

  useEffect(() => {
    if (!project) return
    const interval = setInterval(autoSave, 90_000) // каждые 90 сек
    return () => clearInterval(interval)
  }, [project, autoSave])

  const manualSave = useCallback(async () => {
    if (!project) return
    try {
      setSaveStatus('saving')
      await simaFetch('/snapshots', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          type: 'manual',
          label: t('manual_save_label', { time: new Date().toLocaleString(locale) }),
        }),
      })
      setLastSaved(new Date())
      setSaveStatus('saved')
      setTimeout(() => setSaveStatus('idle'), 2000)
    } catch (e) {
      console.error('Manual save failed:', e)
      setSaveStatus('idle')
    }
  }, [project])

  // Горячие клавиши
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const isInput = ['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target as HTMLElement).tagName) ||
                      (e.target as HTMLElement).isContentEditable
      
      // Ctrl+Z / Cmd+Z — Undo
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key === 'z') {
        e.preventDefault()
        useSimaStore.getState().undo()
        return
      }
      // Ctrl+Shift+Z / Ctrl+Y / Cmd+Shift+Z — Redo
      if (((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'z') ||
          ((e.ctrlKey || e.metaKey) && e.key === 'y')) {
        e.preventDefault()
        useSimaStore.getState().redo()
        return
      }

      if (isInput) return  // Остальные хоткеи не работают в инпутах

      // Ctrl+K — поиск блока (фокус на поиск)
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault()
        const searchInput = document.querySelector<HTMLInputElement>('[data-sima-search]')
        if (searchInput) searchInput.focus()
        return
      }

      // Ctrl+B — toggle правой панели
      if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
        e.preventDefault()
        const s = useSimaStore.getState()
        s.setRightPanelOpen(!s.rightPanelOpen)
        return
      }

      // Ctrl+J — toggle нижней панели
      if ((e.ctrlKey || e.metaKey) && e.key === 'j') {
        e.preventDefault()
        const s = useSimaStore.getState()
        s.setBottomPanelOpen(!s.bottomPanelOpen)
        return
      }

      // Ctrl+S — ручное сохранение
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault()
        manualSave()
        return
      }

      // Ctrl+A — выбрать все блоки
      if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
        e.preventDefault()
        useSimaStore.getState().selectAllBlocks()
        return
      }

      // Delete — удалить выбранные блоки (при мультивыборе)
      if (e.key === 'Delete' || e.key === 'Backspace') {
        const s = useSimaStore.getState()
        if (s.selectedBlockIds.size > 0) {
          e.preventDefault()
          s.removeSelectedBlocks()
          return
        }
      }

      // Escape — снять выделение
      if (e.key === 'Escape') {
        const s = useSimaStore.getState()
        s.clearMultiSelection()
        if (s.selectedBlockId || s.selectedConnectionId) {
          s.setSelectedBlockId(null)
          s.setSelectedConnectionId(null)
        }
        return
      }
    }

    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [])

  if (!project) return null

  const blockCount = blocks.length
  const { canUndo, canRedo, undo, redo } = useSimaStore()

  // Слои рабочей области: канвас → карта → ТЗ → реализация → артефакты.
  // Слой = полная смена вида центра (не вкладка нижней панели).
  const LAYERS = [
    { id: 'canvas' as const, n: '1', label: t('step_canvas') },
    { id: 'map' as const, n: '2', label: t('step_map') },
    { id: 'tz' as const, n: '3', label: t('step_tz') },
    { id: 'impl' as const, n: '4', label: t('step_impl') },
    { id: 'gallery' as const, n: 'A', label: t('step_artifacts') },
  ]
  const currentStageLabel = (LAYERS.find(l => l.id === activeLayer) || LAYERS[0]).label

  if (presentationMode) {
    return <PresentationMode onClose={() => setPresentationMode(false)} />
  }

  return (
    <div className="sima-workspace h-full flex flex-col relative bg-sima-bg text-sima-text">
      {/* Поделиться проектом (гранты коллегам) */}
      {showShare && userId && project && (
        <ShareProjectPanel
          projectId={String(project.id)}
          projectName={project.name || t('project_fallback')}
          ownerId={String((project as any).userId || userId)}
          currentUserId={userId}
          onClose={() => setShowShare(false)}
        />
      )}

      {/* Онбординг первого входа — простыми словами, без терминов */}
      {showOnboarding && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4">
          <div className="max-w-lg w-full rounded-2xl border border-sima-border bg-sima-surface p-6 space-y-4 shadow-2xl">
            <div className="flex items-center gap-2">
              <Sparkles className="w-5 h-5 text-sima-primary" />
              <h2 className="text-sima-text font-semibold">{t('onboarding_title')}</h2>
            </div>
            <p className="text-[13px] text-sima-textMuted">
              {t('onboarding_intro')}
            </p>
            <ol className="space-y-2.5 text-[13px] text-sima-text">
              <li className="flex gap-2.5">
                <span className="flex-none w-5 h-5 rounded-full bg-sima-primary/15 text-sima-primary text-[11px] font-bold flex items-center justify-center">1</span>
                <span>{t.rich('onboarding_step1', { b: (chunks) => <b>{chunks}</b> })}</span>
              </li>
              <li className="flex gap-2.5">
                <span className="flex-none w-5 h-5 rounded-full bg-sima-primary/15 text-sima-primary text-[11px] font-bold flex items-center justify-center">2</span>
                <span>{t.rich('onboarding_step2', { b: (chunks) => <b>{chunks}</b> })}</span>
              </li>
              <li className="flex gap-2.5">
                <span className="flex-none w-5 h-5 rounded-full bg-sima-primary/15 text-sima-primary text-[11px] font-bold flex items-center justify-center">3</span>
                <span>{t.rich('onboarding_step3', { b: (chunks) => <b>{chunks}</b> })}</span>
              </li>
              <li className="flex gap-2.5">
                <span className="flex-none w-5 h-5 rounded-full bg-sima-primary/15 text-sima-primary text-[11px] font-bold flex items-center justify-center">4</span>
                <span>{t.rich('onboarding_step4', { b: (chunks) => <b>{chunks}</b> })}</span>
              </li>
            </ol>
            <p className="text-[12px] text-sima-textDim">
              {t('onboarding_footer')}
            </p>
            <button
              onClick={dismissOnboarding}
              className="w-full py-2 rounded-lg bg-sima-primary hover:bg-sima-primaryHover text-white text-sm font-medium transition-colors"
            >
              {t('onboarding_start')}
            </button>
          </div>
        </div>
      )}
      {/* Top bar */}
      <div className="h-12 bg-sima-surface border-b border-sima-border flex items-center px-4 gap-3 shrink-0">
        <button
          onClick={onBack}
          className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors"
          title={t("back_to_projects")}
        >
          <ArrowLeft className="w-4 h-4 text-sima-textMuted" />
        </button>

        <Sparkles className="w-4 h-4 text-sima-primary shrink-0" />

        {/* Табы проектов (Sima Remix): быстрое переключение недавних без
            ухода на полноэкранный селектор. Активный — текущий; «ещё» ведёт
            в полный список (ProjectSelector сохранён — там шаринг/доступы). */}
        <div className="flex items-center gap-1 min-w-0 overflow-x-auto">
          {(() => {
            const recent = [project, ...projects.filter(p => p.id !== project.id)].slice(0, 5)
            return recent.map(p => {
              const active = p.id === project.id
              return (
                <button
                  key={p.id}
                  onClick={() => { if (!active) onSwitchProject?.(p.id) }}
                  className={
                    'flex items-center gap-1.5 px-2 py-1 rounded-lg text-[12px] shrink-0 transition-colors ' +
                    (active
                      ? 'bg-sima-surfaceLight text-sima-text font-medium'
                      : 'text-sima-textDim hover:text-sima-textMuted hover:bg-sima-surfaceLight/60')
                  }
                  title={p.name}
                >
                  <span className="text-[9px] uppercase tracking-wide px-1 py-0.5 rounded border border-sima-border/70 text-sima-textDim">
                    {t(`task_kind_${p.taskKind || 'product'}`)}
                  </span>
                  <span className="max-w-[120px] truncate">{p.name}</span>
                </button>
              )
            })
          })()}
          <button
            onClick={onBack}
            className="flex items-center gap-1 px-2 py-1 rounded-lg text-[12px] text-sima-textDim hover:text-sima-textMuted hover:bg-sima-surfaceLight/60 shrink-0"
            title={t('all_projects')}
          >
            <Plus className="w-3 h-3" /> {t('project_more')}
          </button>
        </div>

        {/* Слои (Sima Remix): 1 Канвас → 2 Карта → 3 ТЗ → 4 Реализация →
            A Артефакты. Клик = ПОЛНАЯ смена вида центра (не вкладка нижней
            панели — та остаётся независимым чат+инструменты-ящиком). */}
        <div className="hidden md:flex items-center gap-1 ml-3">
          {LAYERS.map(L => {
            const active = L.id === activeLayer
            return (
              <button
                key={L.id}
                onClick={() => setActiveLayer(L.id)}
                className={
                  'flex items-center gap-1.5 px-2 py-1 rounded-lg text-[11px] transition-colors ' +
                  (active
                    ? 'bg-sima-primary/15 text-sima-primary'
                    : 'text-sima-textDim hover:text-sima-textMuted hover:bg-sima-surfaceLight')
                }
              >
                <span className={
                  'w-4 h-4 rounded-full flex items-center justify-center text-[9px] font-bold ' +
                  (active ? 'bg-sima-primary text-white' : 'bg-sima-surfaceLight text-sima-textDim')
                }>{L.n}</span>
                {L.label}
              </button>
            )
          })}
        </div>

        {/* Save indicator */}
        <div className="flex items-center gap-1 ml-2">
          <button
            onClick={manualSave}
            disabled={saveStatus === 'saving'}
            className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors disabled:opacity-50"
            title={t("save_button_title")}
          >
            {saveStatus === 'saving' ? (
              <Save className="w-3.5 h-3.5 text-sima-textDim animate-pulse" />
            ) : saveStatus === 'saved' ? (
              <Check className="w-3.5 h-3.5 text-green-400" />
            ) : (
              <Save className="w-3.5 h-3.5 text-sima-textDim" />
            )}
          </button>
          {lastSaved && saveStatus === 'idle' && (
            <span className="text-[10px] text-sima-textDim">
              {lastSaved.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
        </div>

        {/* Presentation */}
        <button
          onClick={() => setPresentationMode(true)}
          disabled={blocks.length === 0}
          className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors disabled:opacity-30 disabled:cursor-not-allowed ml-2"
          title={t("presentation_mode_title")}
        >
          <Play className="w-3.5 h-3.5 text-sima-textMuted" />
        </button>

        {/* Тема (Sima Remix: светлая кремовая / тёмная) */}
        <button
          onClick={() => setSimaTheme(simaTheme === 'light' ? 'dark' : 'light')}
          className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors ml-2"
          title={t('theme_toggle_title')}
        >
          {simaTheme === 'light'
            ? <Moon className="w-3.5 h-3.5 text-sima-textMuted" />
            : <Sun className="w-3.5 h-3.5 text-sima-textMuted" />}
        </button>

        {/* Настройки внешнего вида (Tweaks: акцент + тема) */}
        <button
          onClick={() => setTweaksOpen((v) => !v)}
          className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors"
          title={t('tweaks_title')}
        >
          <Sliders className="w-3.5 h-3.5 text-sima-textMuted" />
        </button>

        {/* «Как этим пользоваться» — вернуть онбординг-карточку */}
        <button
          onClick={() => setShowOnboarding(true)}
          className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors text-sima-textMuted text-[13px] font-semibold w-7"
          title={t('onboarding_help_title')}
        >
          ?
        </button>

        {/* Поделиться проектом с коллегой (только владелец) */}
        {userId && project && (project as any).userId === userId && (
          <button
            onClick={() => setShowShare(true)}
            className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors text-sima-textMuted text-[11px]"
            title={t('share_project_title')}
          >
            ↗
          </button>
        )}

        {/* Undo/Redo */}
        <div className="flex items-center gap-0.5 ml-2">
          <button
            onClick={undo}
            disabled={!canUndo}
            className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors disabled:opacity-20 disabled:cursor-not-allowed"
            title={t("undo_title")}
          >
            <Undo2 className="w-3.5 h-3.5 text-sima-textMuted" />
          </button>
          <button
            onClick={redo}
            disabled={!canRedo}
            className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors disabled:opacity-20 disabled:cursor-not-allowed"
            title={t("redo_title")}
          >
            <Redo2 className="w-3.5 h-3.5 text-sima-textMuted" />
          </button>
        </div>

        <div className="flex-1" />

        <button
          onClick={() => setBottomPanelOpen(!bottomPanelOpen)}
          className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors"
          title={bottomPanelOpen ? t('hide_panel') : t('show_chat')}
        >
          {bottomPanelOpen ? (
            <ChevronDown className="w-4 h-4 text-sima-textMuted" />
          ) : (
            <ChevronUp className="w-4 h-4 text-sima-textMuted" />
          )}
        </button>

        <button
          onClick={() => setRightPanelOpen(!rightPanelOpen)}
          className="p-1.5 hover:bg-sima-surfaceLight rounded-lg transition-colors"
          title={rightPanelOpen ? t('hide_details') : t('show_details')}
        >
          {rightPanelOpen ? (
            <PanelRightClose className="w-4 h-4 text-sima-textMuted" />
          ) : (
            <PanelRightOpen className="w-4 h-4 text-sima-textMuted" />
          )}
        </button>
      </div>

      {/* Суб-крошки (Sima Remix .l2-sub): путь проект→слой + счётчики + мета.
          Даёт контекст «где я и что вижу» без обращения к заголовкам панелей. */}
      <div className="h-7 bg-sima-surface/60 border-b border-sima-border flex items-center px-4 gap-2 shrink-0 text-[11px] text-sima-textDim overflow-hidden">
        <Layers className="w-3 h-3 text-sima-primary/70 shrink-0" />
        <span className="font-medium text-sima-textMuted truncate">{project.name}</span>
        <span className="text-sima-border">→</span>
        <span className="truncate">{currentStageLabel}</span>
        <span className="text-sima-textDim/70 truncate">
          · {t('sub_counts', { blocks: blockCount, connections: connections.length })}
        </span>
        <div className="flex-1" />
        {project.owner && (
          <span className="hidden sm:inline text-sima-textDim/80 shrink-0">{project.owner}</span>
        )}
        {project.createdAt && (
          <span className="hidden md:inline text-sima-textDim/70 shrink-0">
            {new Date(project.createdAt).toLocaleDateString(locale)}
          </span>
        )}
      </div>

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden min-h-0">
        {/* Canvas + Bottom panel */}
        <div className="flex-1 flex flex-col overflow-hidden min-h-0 min-w-0">
          {/* Центр = активный слой (полная смена вида). Канвас источников,
              карта продукта, ТЗ, реализация, галерея артефактов. Нижняя
              панель ниже — независимый чат+инструменты-ящик. */}
          <div className="flex-1 relative min-h-0 overflow-hidden">
            <div className={activeLayer === 'canvas' ? 'absolute inset-0' : 'hidden'}>
              <DiagramCanvas />
            </div>
            {activeLayer === 'map' && <div className="absolute inset-0"><MapView /></div>}
            {activeLayer === 'tz' && <div className="absolute inset-0"><TzDocumentView /></div>}
            {activeLayer === 'impl' && <div className="absolute inset-0 overflow-y-auto"><ImplementationPanel /></div>}
            {activeLayer === 'gallery' && <div className="absolute inset-0 overflow-y-auto"><ArtifactsPanel /></div>}
          </div>

          {/* Bottom panel with resize handle */}
          {bottomPanelOpen && (
            <div className="shrink-0 flex flex-col" style={{ height: panelHeight }}>
              {/* Drag handle */}
              <div
                onMouseDown={handleMouseDown}
                className="h-2 border-t border-sima-border bg-sima-surface cursor-ns-resize flex items-center justify-center hover:bg-sima-surfaceLight transition-colors group"
              >
                <GripHorizontal className="w-4 h-3 text-sima-textDim opacity-0 group-hover:opacity-100 transition-opacity" />
              </div>
              {/* Panel content */}
              <div className="flex-1 overflow-hidden">
                <BottomPanel />
              </div>
            </div>
          )}
        </div>

        {/* Right panel */}
        {rightPanelOpen && (
          <div className="w-[400px] border-l border-sima-border bg-sima-surface overflow-y-auto shrink-0">
            {/* Палитра «Добавить источник» (эталон, экран 1) — на канвасе,
                над инспектором; кнопки открывают реальный ввод в «Данных» */}
            {activeLayer === 'canvas' && !selectedBlockId && !selectedConnectionId && (
              <AddSourcePalette />
            )}
            {selectedConnectionId ? (
              <ConnectionDetailsPanel />
            ) : selectedBlockId ? (
              <BlockDetailsPanel />
            ) : activeLayer !== 'canvas' ? (
              <div className="p-6 text-center text-sima-textDim">
                <FileText className="w-12 h-12 mx-auto mb-3 opacity-30" />
                <p>{t('right_panel_empty')}</p>
              </div>
            ) : null}
          </div>
        )}
      </div>

      <SimaTweaks
        open={tweaksOpen}
        onClose={() => setTweaksOpen(false)}
        accent={accent}
        setAccent={setAccent}
        theme={simaTheme}
        setTheme={setSimaTheme}
      />
    </div>
  )
}
