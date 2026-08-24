'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch, getAccessToken } from '@/lib/authFetch'
import { Network, RefreshCw } from 'lucide-react'

// Линза кластеров: закономерности компании «снизу вверх». Состав кластеров —
// математика (Louvain по реальным связям графа), имена и значимость — LLM с
// пометкой «гипотеза». Backend: /communities.

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

type Member = { id: string; name: string; label: string }
type Community = {
  id: string; size: number; members: Member[]; members_total: number
  by_label: Record<string, number>; meetings_involved: number
  internal_edges: number; edge_types: Record<string, number>
  bridges: { name: string; label: string; degree: number }[]
  summary?: { name: string; pattern: string; significance: string; watch_out: string }
}

const LABEL_ICON: Record<string, string> = {
  Person: '👤', Client: '🏢', Product: '📦', Project: '🗂️', Task: '☑️',
  Department: '🏛️', Team: '👥', Process: '⚙️', Decision: '⚖️', Risk: '⚠️',
  Goal: '🎯', Resource: '🧰', Company: '🏙️',
}

export default function CommunityLensPanel({ userId }: { userId: string | null }) {
  const t = useTranslations('community_lens_panel')
  const [communities, setCommunities] = useState<Community[]>([])
  const [generatedAt, setGeneratedAt] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [open, setOpen] = useState<string>('')

  const load = useCallback(async () => {
    if (!userId || !getAccessToken()) return
    try {
      const r = await authFetch(`${API}/api/v1/communities`)
      const d = await r.json().catch(() => ({}))
      setCommunities(d.communities || [])
      setGeneratedAt(d.generated_at || '')
    } catch { /* необязательно */ }
  }, [userId])

  useEffect(() => { load() }, [load])

  const rebuild = useCallback(async () => {
    if (!userId) return
    setBusy(true); setError('')
    try {
      const r = await authFetch(`${API}/api/v1/communities/rebuild`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') {
        setCommunities(d.communities || []); setGeneratedAt(d.generated_at || '')
        if (d.unchanged) setError(t('unchanged'))
      } else setError(d.message || t('error_generic'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, t])

  return (
    <div className="bg-brain-900/60 border border-brain-700 rounded-lg p-4 mt-4 text-brain-100">
      <div className="flex items-center gap-2 mb-1">
        <Network size={16} className="text-brain-400" />
        <span className="text-sm font-medium">{t('title')}</span>
        {generatedAt && (
          <span className="text-[10px] text-brain-600">
            {t('updated_at', { date: String(generatedAt).slice(0, 10) })}
          </span>
        )}
        <button
          className="ml-auto px-3 py-1 rounded-md bg-brain-700 hover:bg-brain-600 text-white text-xs disabled:opacity-50 inline-flex items-center gap-1"
          onClick={rebuild} disabled={busy}
          title={t('rebuild_hint')}>
          <RefreshCw size={12} className={busy ? 'animate-spin' : ''} /> {t('rebuild')}
        </button>
      </div>
      <div className="text-[11px] text-brain-500 mb-3">
        {t('explanation')}
      </div>
      {error && <div className="text-xs text-amber-400/90 mb-2">{error}</div>}
      {communities.length === 0 ? (
        <div className="text-xs text-brain-500">
          {t('empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {communities.map((c) => (
            <div key={c.id} className="border border-brain-800 rounded-md p-2">
              <button className="w-full text-left" onClick={() => setOpen(open === c.id ? '' : c.id)}>
                <div className="flex items-center gap-2 text-sm">
                  <span className="text-brain-100">{c.summary?.name || t('cluster_fallback_name', { id: c.id })}</span>
                  <span className="text-xs text-brain-500" title={t('metrics_hint')}>
                    {t('members_count', { count: c.size })} · {t('edges_count', { count: c.internal_edges })}
                    {c.meetings_involved > 0 && <> · {t('meetings_involved', { count: c.meetings_involved })}</>}
                  </span>
                  <span className="ml-auto text-xs text-brain-500" title={t('by_label_hint')}>
                    {Object.entries(c.by_label).map(([l, n]) => `${LABEL_ICON[l] || l} ${n}`).join(' ')}
                  </span>
                </div>
                {c.summary?.significance && (
                  <div className="text-xs text-brain-300 mt-1">
                    💡 <span className="text-amber-300/80" title={t('hypothesis_hint')}>{t('hypothesis_label')}</span>{' '}
                    {c.summary.significance}
                  </div>
                )}
              </button>
              {open === c.id && (
                <div className="mt-2 text-xs flex flex-col gap-1">
                  {c.summary?.pattern && <div className="text-brain-400">{t('pattern_label')} {c.summary.pattern}</div>}
                  {c.summary?.watch_out && <div className="text-emerald-400/80">{t('watch_out_label')} {c.summary.watch_out}</div>}
                  {c.bridges?.length > 0 && (
                    <div className="text-brain-400" title={t('bridges_hint')}>
                      {t('bridges_label')} {c.bridges.map((b) => `${b.name} (${b.degree})`).join(', ')}
                    </div>
                  )}
                  <div className="text-brain-300 flex flex-wrap gap-1 mt-1">
                    {c.members.map((m) => (
                      <span key={m.id} className="px-1.5 py-0.5 rounded bg-brain-800/80" title={m.label}>
                        {LABEL_ICON[m.label] || ''} {m.name}
                      </span>
                    ))}
                    {c.members_total > c.members.length && (
                      <span className="text-brain-600">{t('more_members', { count: c.members_total - c.members.length })}</span>
                    )}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
