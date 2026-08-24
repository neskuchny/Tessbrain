'use client'

// Единый раздел «Документы» (#3, шаг 1): 4 доковые подвкладки, что были
// размазаны по «Знаниям» (📄 по встрече, Шаблоны, Загруженные, 🔬 Анализ),
// сведены в один раздел с внутренними вкладками. Панели переиспользуются как
// есть — ничего не переписано и не удалено, изменилась только группировка.

import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { FileSignature, BookOpen, Upload, Microscope } from 'lucide-react'
import MeetingDocsPanel from './MeetingDocsPanel'
import TemplatesPanel from './TemplatesPanel'
import DocumentsPanel from './DocumentsPanel'
import DocResearchPanel from './DocResearchPanel'

type DocTab = 'collect' | 'templates' | 'uploaded' | 'analysis'

interface Props {
  userId?: string
  onDocumentsUpdate?: () => void
  documentFocusRequest?: React.ComponentProps<typeof DocumentsPanel>['focusRequest']
}

const TABS: Array<{ id: DocTab; labelKey: string; icon: React.ReactNode }> = [
  { id: 'collect', labelKey: 'tab_collect', icon: <FileSignature className="w-4 h-4" /> },
  { id: 'templates', labelKey: 'tab_templates', icon: <BookOpen className="w-4 h-4" /> },
  { id: 'uploaded', labelKey: 'tab_uploaded', icon: <Upload className="w-4 h-4" /> },
  { id: 'analysis', labelKey: 'tab_analysis', icon: <Microscope className="w-4 h-4" /> },
]

export default function DocumentsHubPanel({ userId, onDocumentsUpdate, documentFocusRequest }: Props) {
  const t = useTranslations('documents_hub_panel')
  const [tab, setTab] = useState<DocTab>('collect')

  // Переход по ссылке на конкретный документ (documentFocusRequest) должен
  // открывать вкладку «Загруженные», где живёт DocumentsPanel с фокусом.
  useEffect(() => {
    if (documentFocusRequest) setTab('uploaded')
  }, [documentFocusRequest])

  return (
    <div className="h-full flex flex-col">
      <div className="flex gap-1 px-4 py-2 border-b border-brain-700/30 bg-brain-950/40 flex-wrap">
        {TABS.map((tb) => (
          <button
            key={tb.id}
            onClick={() => setTab(tb.id)}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-md transition-all text-sm ${
              tab === tb.id
                ? 'bg-brain-700 text-white'
                : 'text-brain-400 hover:bg-brain-800/50 hover:text-brain-300'
            }`}
          >
            {tb.icon}
            <span>{t(tb.labelKey)}</span>
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-auto">
        {tab === 'collect' && <MeetingDocsPanel userId={userId} />}
        {tab === 'templates' && <TemplatesPanel />}
        {tab === 'uploaded' && (
          <div className="h-full p-4 overflow-auto">
            <DocumentsPanel
              userId={userId}
              onDataUpdate={onDocumentsUpdate}
              focusRequest={documentFocusRequest}
            />
          </div>
        )}
        {tab === 'analysis' && <DocResearchPanel userId={userId} />}
      </div>
    </div>
  )
}
