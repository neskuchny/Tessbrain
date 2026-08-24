'use client'

import { useState } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import { Plus, FolderOpen, Sparkles, Layout, Smartphone, ShoppingBag, Bot, Globe, FileText } from 'lucide-react'
import type { ProjectData, TaskKind } from '@/sima/types'

interface ProjectTemplate {
  id: string
  name: string
  icon: typeof Layout
  color: string
  description: string
  suggestedName: string
}

const buildProjectTemplates = (t: (k: string) => string): ProjectTemplate[] => [
  { id: 'blank', name: t('tpl_blank_name'), icon: Plus, color: '#6366F1', description: t('tpl_blank_desc'), suggestedName: '' },
  { id: 'saas', name: t('tpl_saas_name'), icon: Layout, color: '#22D3EE', description: t('tpl_saas_desc'), suggestedName: t('tpl_saas_default') },
  { id: 'mobile', name: t('tpl_mobile_name'), icon: Smartphone, color: '#34D399', description: t('tpl_mobile_desc'), suggestedName: t('tpl_mobile_name') },
  { id: 'marketplace', name: t('tpl_marketplace_name'), icon: ShoppingBag, color: '#FBBF24', description: t('tpl_marketplace_desc'), suggestedName: t('tpl_marketplace_name') },
  { id: 'ai-agent', name: t('tpl_ai_agent_name'), icon: Bot, color: '#A78BFA', description: t('tpl_ai_agent_desc'), suggestedName: t('tpl_ai_agent_name') },
  { id: 'landing', name: t('tpl_landing_name'), icon: Globe, color: '#F472B6', description: t('tpl_landing_desc'), suggestedName: t('tpl_landing_default') },
  { id: 'automation', name: t('tpl_automation_name'), icon: FileText, color: '#FB923C', description: t('tpl_automation_desc'), suggestedName: t('tpl_automation_default') },
]

interface ProjectSelectorProps {
  projects: ProjectData[]
  onSelect: (id: string) => void
  onCreate: (name: string, template?: string, taskKind?: TaskKind) => void
}

const TASK_KINDS: { id: TaskKind; icon: string }[] = [
  { id: 'product', icon: '🧩' },
  { id: 'book', icon: '📖' },
  { id: 'idea', icon: '💡' },
]

export default function ProjectSelector({ projects, onSelect, onCreate }: ProjectSelectorProps) {
  const t = useTranslations('sima')
  const locale = useLocale()
  const PROJECT_TEMPLATES = buildProjectTemplates(t)
  const [newName, setNewName] = useState('')
  const [isCreating, setIsCreating] = useState(false)
  const [selectedTemplate, setSelectedTemplate] = useState<string>('blank')
  const [newKind, setNewKind] = useState<TaskKind>('product')

  function handleCreate() {
    if (newName.trim()) {
      onCreate(newName.trim(), selectedTemplate !== 'blank' ? selectedTemplate : undefined, newKind)
      setNewName('')
      setIsCreating(false)
      setSelectedTemplate('blank')
      setNewKind('product')
    }
  }

  function handleTemplateSelect(template: ProjectTemplate) {
    setSelectedTemplate(template.id)
    if (template.suggestedName && !newName) {
      setNewName(template.suggestedName)
    }
  }

  return (
    <div className="h-screen flex items-center justify-center bg-sima-bg">
      <div className="max-w-2xl w-full mx-4">
        {/* Logo */}
        <div className="text-center mb-12">
          <div className="inline-flex items-center justify-center w-20 h-20 rounded-2xl bg-gradient-to-br from-sima-primary to-sima-accent mb-6">
            <Sparkles className="w-10 h-10 text-white" />
          </div>
          <h1 className="text-4xl font-bold bg-gradient-to-r from-sima-primary to-sima-accent bg-clip-text text-transparent">
            {t('logo_title')}
          </h1>
          <p className="text-sima-textMuted mt-2 text-lg">
            {t('logo_subtitle')}
          </p>
          <p className="text-sima-textDim mt-1">
            {t('tagline')}
          </p>
        </div>

        {/* Create new */}
        {isCreating ? (
          <div className="bg-sima-surface border border-sima-border rounded-xl p-6 mb-6">
            <h3 className="text-lg font-medium mb-4">{t("new_project_heading")}</h3>

            {/* Шаблоны */}
            <div className="grid grid-cols-4 gap-2 mb-4">
              {PROJECT_TEMPLATES.map((t) => {
                const Icon = t.icon
                const isSelected = selectedTemplate === t.id
                return (
                  <button
                    key={t.id}
                    onClick={() => handleTemplateSelect(t)}
                    className={`flex flex-col items-center gap-1.5 p-3 rounded-lg border transition-all text-center ${
                      isSelected
                        ? 'border-sima-primary bg-sima-primary/5'
                        : 'border-sima-border hover:border-sima-borderLight'
                    }`}
                  >
                    <Icon className="w-5 h-5" style={{ color: t.color }} />
                    <span className="text-[11px] font-medium text-sima-text leading-tight">{t.name}</span>
                    <span className="text-[9px] text-sima-textDim leading-tight">{t.description}</span>
                  </button>
                )
              })}
            </div>

            {/* Тип проекта: конструктор ЛЮБЫХ идей, не только продуктов */}
            <div className="flex gap-2 mb-3">
              {TASK_KINDS.map((k) => (
                <button
                  key={k.id}
                  onClick={() => setNewKind(k.id)}
                  className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg border text-sm transition-colors ${
                    newKind === k.id
                      ? 'border-sima-primary bg-sima-primary/15 text-sima-text font-medium'
                      : 'border-sima-border text-sima-textDim hover:text-sima-textMuted'
                  }`}
                >
                  <span>{k.icon}</span> {t(`task_kind_${k.id}`)}
                </button>
              ))}
            </div>

            <input
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              placeholder={t("project_name_placeholder")}
              className="w-full bg-sima-bg border border-sima-border rounded-lg px-4 py-3 text-sima-text placeholder-sima-textDim focus:outline-none focus:border-sima-primary transition-colors"
              autoFocus
            />
            <div className="flex gap-3 mt-4">
              <button
                onClick={handleCreate}
                disabled={!newName.trim()}
                className="px-6 py-2 bg-sima-primary hover:bg-sima-primaryHover text-white rounded-lg font-medium transition-colors disabled:opacity-50"
              >
                {t('create_button')}
              </button>
              <button
                onClick={() => { setIsCreating(false); setSelectedTemplate('blank') }}
                className="px-6 py-2 bg-sima-surfaceLight hover:bg-sima-border text-sima-textMuted rounded-lg transition-colors"
              >
                {t('cancel_button')}
              </button>
            </div>
          </div>
        ) : (
          <button
            onClick={() => setIsCreating(true)}
            className="w-full bg-sima-surface border border-dashed border-sima-border hover:border-sima-primary rounded-xl p-6 mb-6 flex items-center gap-4 transition-colors group"
          >
            <div className="w-12 h-12 rounded-xl bg-sima-primary/10 flex items-center justify-center group-hover:bg-sima-primary/20 transition-colors">
              <Plus className="w-6 h-6 text-sima-primary" />
            </div>
            <div className="text-left">
              <p className="font-medium text-sima-text">{t("create_new_project")}</p>
              <p className="text-sm text-sima-textDim">{t("create_new_subtitle")}</p>
            </div>
          </button>
        )}

        {/* Project list */}
        {projects.length > 0 && (
          <div>
            <h3 className="text-sm font-medium text-sima-textDim uppercase tracking-wider mb-3 px-1">
              {t('existing_projects')}
            </h3>
            <div className="space-y-2">
              {projects.map((p) => (
                <button
                  key={p.id}
                  onClick={() => onSelect(p.id)}
                  className="w-full bg-sima-surface border border-sima-border hover:border-sima-borderLight rounded-xl p-4 flex items-center gap-4 transition-colors text-left"
                >
                  <div className="w-10 h-10 rounded-lg bg-sima-surfaceLight flex items-center justify-center">
                    <FolderOpen className="w-5 h-5 text-sima-primary" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-sima-text truncate">
                      <span className="text-[9px] uppercase tracking-wide px-1.5 py-0.5 rounded border border-sima-border text-sima-textDim align-middle mr-2">
                        {t(`task_kind_${p.taskKind || 'product'}`)}
                      </span>
                      {p.name}
                      {(p as any).sharedWithMe && (
                        <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-sima-accent/10 text-sima-accent align-middle">
                          {t('shared_with_me')} · {(p as any).accessLevel === 'write' ? t('access_edit') : t('access_view')}
                        </span>
                      )}
                    </p>
                    <p className="text-sm text-sima-textDim">
                      {t('blocks_connections_summary', { blocks: (p as any).blocksCount ?? p.blocks?.length ?? 0, connections: (p as any).connectionsCount ?? p.connections?.length ?? 0 })}{' '}
                      {new Date(p.updatedAt).toLocaleDateString(locale)}
                    </p>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
