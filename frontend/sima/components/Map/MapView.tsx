'use client'

/**
 * MapView — слой «Карта продукта» с двумя видами (эталон Sima Remix):
 * «Поля карты» (карточки миссии/аудитории/MVP с пропусками) и
 * «Архитектура» (блоки дорожками по слоям, связи, статусы).
 * Тулбар архитектуры: группировка «по слоям / по пути / по статусу»,
 * фильтр «только MVP», кнопка «править на канвасе».
 */
import { useState } from 'react'
import { useTranslations } from 'next-intl'
import { LayoutGrid, Network, PencilRuler } from 'lucide-react'
import { useSimaStore } from '@/sima/lib/store'
import MapFieldsView from '@/sima/components/Map/MapFieldsView'
import ArchitectureView from '@/sima/components/Map/ArchitectureView'

type MapTab = 'fields' | 'arch'
type GroupMode = 'layers' | 'status' | 'path'

export default function MapView() {
  const t = useTranslations('sima_arch')
  const setActiveLayer = useSimaStore(s => s.setActiveLayer)
  const [tab, setTab] = useState<MapTab>('fields')
  const [groupMode, setGroupMode] = useState<GroupMode>('layers')
  const [mvpOnly, setMvpOnly] = useState(false)

  const tabBtn = (id: MapTab, icon: React.ReactNode, label: string) => (
    <button
      onClick={() => setTab(id)}
      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors ${
        tab === id
          ? 'bg-sima-surface text-sima-text border border-sima-border shadow-sm'
          : 'text-sima-textMuted hover:text-sima-text'
      }`}
    >
      {icon}{label}
    </button>
  )

  const modeBtn = (id: GroupMode, label: string) => (
    <button
      onClick={() => setGroupMode(id)}
      className={`px-2.5 py-1 rounded-md text-[11px] transition-colors ${
        groupMode === id
          ? 'bg-sima-primary/15 text-sima-primary font-medium'
          : 'text-sima-textMuted hover:text-sima-text'
      }`}
    >
      {label}
    </button>
  )

  return (
    <div className="h-full flex flex-col">
      {/* Тулбар вида */}
      <div className="shrink-0 px-4 py-2 border-b border-sima-border bg-sima-bg/60 flex items-center gap-2 flex-wrap">
        <div className="flex items-center gap-1 rounded-xl bg-sima-surfaceLight/60 p-1">
          {tabBtn('fields', <LayoutGrid className="w-3.5 h-3.5" />, t('tab_fields'))}
          {tabBtn('arch', <Network className="w-3.5 h-3.5" />, t('tab_arch'))}
        </div>

        {tab === 'arch' && (
          <>
            <span className="text-[10px] uppercase tracking-wide text-sima-textDim ml-2">
              {t('view_label')}
            </span>
            <div className="flex items-center gap-0.5 rounded-lg bg-sima-surfaceLight/60 p-0.5">
              {modeBtn('layers', t('mode_layers'))}
              {modeBtn('path', t('mode_path'))}
              {modeBtn('status', t('mode_status'))}
            </div>
            <label className="flex items-center gap-1.5 text-[11px] text-sima-textMuted cursor-pointer select-none ml-1">
              <input type="checkbox" checked={mvpOnly}
                onChange={(e) => setMvpOnly(e.target.checked)}
                className="accent-sima-primary" />
              {t('mvp_only')}
            </label>
            <span className="flex-1" />
            <button
              onClick={() => setActiveLayer('canvas')}
              title={t('edit_on_canvas_hint')}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-sima-border text-[11px] text-sima-textMuted hover:text-sima-text hover:bg-sima-surfaceLight transition-colors"
            >
              <PencilRuler className="w-3.5 h-3.5" /> {t('edit_on_canvas')}
            </button>
          </>
        )}
      </div>

      {/* Контент вида */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {tab === 'fields' ? (
          <div className="h-full overflow-y-auto"><MapFieldsView /></div>
        ) : (
          <ArchitectureView groupMode={groupMode} mvpOnly={mvpOnly} />
        )}
      </div>
    </div>
  )
}
