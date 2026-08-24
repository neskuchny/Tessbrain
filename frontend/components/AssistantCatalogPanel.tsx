'use client'

// Каталог ассистентов (Cowork-перенос C4): карточки агентов, методологий
// и скиллов — что умеет система и как это позвать. Скиллы тянутся живьём
// из /skills; агенты и методологии описаны статически (они и есть код).
import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { Bot, RefreshCw, Sparkles } from 'lucide-react'

interface CatalogProps { userId?: string }

const AGENTS: { id: string; name?: string; nameKey?: string; emoji: string; descKey: string; howKey: string }[] = [
  { id: 'brain', name: 'Brain', emoji: '🧠', descKey: 'agent_brain_desc', howKey: 'agent_brain_how' },
  { id: 'mark', name: 'Mark', emoji: '📈', descKey: 'agent_mark_desc', howKey: 'agent_mark_how' },
  { id: 'automation', name: 'Automation', emoji: '⚙️', descKey: 'agent_automation_desc', howKey: 'agent_automation_how' },
  { id: 'agent_oc', name: 'Hermes', emoji: '🤖', descKey: 'agent_hermes_desc', howKey: 'agent_hermes_how' },
  { id: 'composer', nameKey: 'agent_composer_name', emoji: '📄', descKey: 'agent_composer_desc', howKey: 'agent_composer_how' },
  { id: 'experts', nameKey: 'agent_experts_name', emoji: '👥', descKey: 'agent_experts_desc', howKey: 'agent_experts_how' },
  { id: 'datasets', nameKey: 'agent_datasets_name', emoji: '🔢', descKey: 'agent_datasets_desc', howKey: 'agent_datasets_how' },
]

export default function AssistantCatalogPanel({ userId }: CatalogProps) {
  const t = useTranslations('catalog_panel')
  const [skills, setSkills] = useState<any[]>([])
  const [loading, setLoading] = useState(false)

  const headers = useCallback((): Record<string, string> => {
    const h: Record<string, string> = {}
    if (typeof window !== 'undefined') {
      const token = localStorage.getItem('tessent_access_token')
      if (token) h['Authorization'] = `Bearer ${token}`
    }
    return h
  }, [])

  useEffect(() => {
    setLoading(true)
    const userParam = userId ? `?user_id=${userId}` : ''
    fetch(`/api/v1/skills/${userParam}`, { headers: headers() })
      .then(r => r.ok ? r.json() : null)
      .then(d => setSkills(d?.skills || []))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [headers, userId])

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center gap-2">
        <Bot className="w-5 h-5 text-indigo-400" />
        <h2 className="text-lg font-semibold">{t('title')}</h2>
        <span className="text-xs text-slate-400">{t('subtitle')}</span>
      </div>

      <div>
        <p className="text-xs text-slate-500 uppercase mb-2">{t('agents_heading')}</p>
        <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-2">
          {AGENTS.map(a => (
            <div key={a.id} className="p-3 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-1">
              <p className="text-sm font-medium">{a.emoji} {a.nameKey ? t(a.nameKey) : a.name}</p>
              <p className="text-xs text-slate-400">{t(a.descKey)}</p>
              <p className="text-[10px] text-slate-500 font-mono">{t(a.howKey)}</p>
            </div>
          ))}
        </div>
      </div>

      <div>
        <p className="text-xs text-slate-500 uppercase mb-2 flex items-center gap-2">
          <Sparkles className="w-3.5 h-3.5" />{t('skills_heading')}
          {loading && <RefreshCw className="w-3 h-3 animate-spin" />}
        </p>
        {skills.length === 0 && !loading ? (
          <p className="text-xs text-slate-500">{t('skills_empty')}</p>
        ) : (
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-2">
            {skills.map((s: any) => (
              <div key={s.id || s.skill_id} className="p-3 rounded-xl border border-brain-600/30 bg-brain-800/20 space-y-1">
                <p className="text-sm font-medium">{s.name || s.title}</p>
                {s.description && <p className="text-xs text-slate-400 line-clamp-2">{s.description}</p>}
                <p className="text-[10px] text-slate-500 font-mono">
                  {t('skill_how', { id: s.id || s.skill_id })}
                </p>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
