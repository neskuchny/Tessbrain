'use client'

/**
 * AddSourcePalette — «Добавить источник» на правой панели канваса
 * (эталон Sima Remix, экран 1). Кнопки ИСПОЛНЯЕМЫЕ: открывают нижнюю
 * панель «Данные» сразу в нужном режиме ввода (store.dataPanelMode),
 * «Артефакт из галереи» переключает на слой галереи со вставкой.
 */
import { useTranslations } from 'next-intl'
import { Video, FileText, Mic, Type, Package } from 'lucide-react'
import { useSimaStore } from '@/sima/lib/store'

export default function AddSourcePalette() {
  const t = useTranslations('sima_sources')
  const setBottomPanelTab = useSimaStore(s => s.setBottomPanelTab)
  const setBottomPanelOpen = useSimaStore(s => s.setBottomPanelOpen)
  const setDataPanelMode = useSimaStore(s => s.setDataPanelMode)
  const setActiveLayer = useSimaStore(s => s.setActiveLayer)

  const openData = (mode: 'file' | 'text' | 'meetings' | 'documents') => {
    setDataPanelMode(mode)
    setBottomPanelTab('data')
    setBottomPanelOpen(true)
  }

  const ITEMS = [
    { icon: <Video className="w-4 h-4" />, title: t('meetflow_title'),
      sub: t('meetflow_sub'), onClick: () => openData('meetings') },
    { icon: <FileText className="w-4 h-4" />, title: t('document_title'),
      sub: t('document_sub'), onClick: () => openData('documents') },
    { icon: <Mic className="w-4 h-4" />, title: t('audio_title'),
      sub: t('audio_sub'), onClick: () => openData('file') },
    { icon: <Type className="w-4 h-4" />, title: t('text_title'),
      sub: t('text_sub'), onClick: () => openData('text') },
    { icon: <Package className="w-4 h-4" />, title: t('artifact_title'),
      sub: t('artifact_sub'), onClick: () => setActiveLayer('gallery') },
  ]

  return (
    <div className="p-4 border-b border-sima-border">
      <div className="text-[10px] uppercase tracking-wide text-sima-textDim mb-2">
        {t('palette_title')}
      </div>
      <div className="space-y-1.5">
        {ITEMS.map((it) => (
          <button
            key={it.title}
            onClick={it.onClick}
            className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl border border-sima-border bg-sima-surfaceLight/60 hover:bg-sima-surfaceLight hover:border-sima-primary/40 text-left transition-colors group"
          >
            <span className="w-8 h-8 rounded-lg bg-sima-bg flex items-center justify-center text-sima-textMuted group-hover:text-sima-primary transition-colors shrink-0">
              {it.icon}
            </span>
            <span className="min-w-0">
              <span className="block text-[12px] font-medium text-sima-text">{it.title}</span>
              <span className="block text-[10px] text-sima-textDim truncate">{it.sub}</span>
            </span>
          </button>
        ))}
      </div>
      <p className="text-[10px] text-sima-textDim italic mt-2 leading-snug">
        {t('palette_hint')}
      </p>
    </div>
  )
}
