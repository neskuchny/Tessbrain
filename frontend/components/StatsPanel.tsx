'use client'

import { useState, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import { Users, FolderKanban, Calendar, CheckSquare, Lightbulb, AlertTriangle, Database, ChevronDown, ChevronUp } from 'lucide-react'

interface GraphContents {
  stats: {
    total_nodes: number
    total_edges: number
    meetings: number
    people: number
    tasks: number
    decisions: number
    entities: number
    kpis: number
  }
  access_groups: Record<string, number>
  entity_types: Record<string, number>
  meetings: Array<{ id: string; title: string; date: string }>
  people: Array<{ id: string; name: string; role: string }>
  recent_tasks: Array<{ id: string; title: string; status: string; assignee: string }>
  recent_decisions: Array<{ id: string; summary: string }>
  // optional fields from backend
  error?: string
}

interface StatsPanelProps {
  userId?: string | null
}

export default function StatsPanel({ userId }: StatsPanelProps) {
  const t = useTranslations('stats_panel')
  const [data, setData] = useState<GraphContents | null>(null)
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    fetchGraphContents()
  }, [userId])

  const normalizeGraphContents = (raw: any): GraphContents | null => {
    if (!raw || typeof raw !== 'object') return null
    if (raw.error) return null

    return {
      stats: raw.stats || {
        total_nodes: 0,
        total_edges: 0,
        meetings: 0,
        people: 0,
        tasks: 0,
        decisions: 0,
        entities: 0,
        kpis: 0,
      },
      access_groups: raw.access_groups || {},
      entity_types: raw.entity_types || {},
      meetings: Array.isArray(raw.meetings) ? raw.meetings : [],
      people: Array.isArray(raw.people) ? raw.people : [],
      recent_tasks: Array.isArray(raw.recent_tasks) ? raw.recent_tasks : [],
      recent_decisions: Array.isArray(raw.recent_decisions) ? raw.recent_decisions : [],
    }
  }

  const fetchGraphContents = async () => {
    try {
      // Без userId этот эндпоинт вернёт "Authentication required" (в виде JSON),
      // и UI не должен падать на data.meetings.length
      if (!userId) {
        setData(null)
        return
      }

      const userParam = `?user_id=${userId}`
      const res = await fetch(`/api/v1/chat/graph-contents${userParam}`)
      if (res.ok) {
        const contents = await res.json()
        setData(normalizeGraphContents(contents))
      }
    } catch (e) {
      console.error('Failed to fetch graph contents:', e)
    } finally {
      setLoading(false)
    }
  }

  const stats = data?.stats || {
    total_nodes: 0,
    total_edges: 0,
    meetings: 0,
    people: 0,
    tasks: 0,
    decisions: 0,
    entities: 0,
    kpis: 0,
  }

  const statCards = [
    { label: t('label_people'), value: stats.people, icon: Users, color: 'text-green-400' },
    { label: t('label_meetings'), value: stats.meetings, icon: Calendar, color: 'text-orange-400' },
    { label: t('label_tasks'), value: stats.tasks, icon: CheckSquare, color: 'text-purple-400' },
    { label: t('label_decisions'), value: stats.decisions, icon: Lightbulb, color: 'text-red-400' },
    { label: t('label_entities'), value: stats.entities, icon: FolderKanban, color: 'text-blue-400' },
    { label: t('label_total_nodes'), value: stats.total_nodes, icon: Database, color: 'text-brain-400' },
  ]

  return (
    <div className="mb-6">
      {/* Stats Row */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-4">
        {statCards.map((stat) => {
          const Icon = stat.icon
          return (
            <div key={stat.label} className="card card-glow text-center">
              <Icon className={`w-6 h-6 ${stat.color} mx-auto mb-2`} />
              <div className="text-2xl font-bold">
                {loading ? '...' : stat.value}
              </div>
              <div className="text-sm text-brain-400">{stat.label}</div>
            </div>
          )
        })}
      </div>

      {/* Expand/Collapse Button */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-center gap-2 py-2 text-sm text-brain-400 hover:text-brain-300 transition-colors"
      >
        {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        {expanded ? t('hide_details') : t('show_kb_content')}
      </button>

      {/* Expanded Details */}
      {expanded && data && (
        <div className="mt-4 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {/* Meetings */}
          <div className="card bg-brain-900/60">
            <h3 className="text-sm font-semibold text-brain-300 mb-3 flex items-center gap-2">
              <Calendar className="w-4 h-4 text-orange-400" />
              {t('meetings_count_heading', { count: data.meetings?.length || 0 })}
            </h3>
            <div className="space-y-2 max-h-48 overflow-y-auto">
              {(data.meetings || []).map((m) => (
                <div key={m.id} className="text-sm p-2 bg-brain-800/40 rounded">
                  <div className="font-medium truncate">{m.title}</div>
                  {m.date && <div className="text-xs text-brain-500">{m.date}</div>}
                </div>
              ))}
              {(data.meetings?.length || 0) === 0 && (
                <div className="text-sm text-brain-500">{t('no_meetings')}</div>
              )}
            </div>
          </div>

          {/* People */}
          <div className="card bg-brain-900/60">
            <h3 className="text-sm font-semibold text-brain-300 mb-3 flex items-center gap-2">
              <Users className="w-4 h-4 text-green-400" />
              {t('people_count_heading', { count: data.people?.length || 0 })}
            </h3>
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {(data.people || []).slice(0, 15).map((p) => (
                <div key={p.id} className="text-sm flex items-center gap-2">
                  <span className="w-2 h-2 bg-green-500 rounded-full flex-shrink-0" />
                  <span className="truncate">{p.name}</span>
                  {p.role && <span className="text-xs text-brain-500 truncate">({p.role.slice(0, 20)})</span>}
                </div>
              ))}
              {(data.people?.length || 0) > 15 && (
                <div className="text-xs text-brain-500">{t('more_people', { count: (data.people?.length || 0) - 15 })}</div>
              )}
            </div>
          </div>

          {/* Tasks */}
          <div className="card bg-brain-900/60">
            <h3 className="text-sm font-semibold text-brain-300 mb-3 flex items-center gap-2">
              <CheckSquare className="w-4 h-4 text-purple-400" />
              {t('tasks_count_heading', { count: stats.tasks })}
            </h3>
            <div className="space-y-2 max-h-48 overflow-y-auto">
              {data.recent_tasks.slice(0, 10).map((t) => (
                <div key={t.id} className="text-sm p-2 bg-brain-800/40 rounded">
                  <div className="flex items-center gap-2">
                    <span className={`text-xs px-1.5 py-0.5 rounded ${
                      t.status === 'done' ? 'bg-green-500/20 text-green-400' :
                      t.status === 'in_progress' ? 'bg-blue-500/20 text-blue-400' :
                      'bg-brain-600/40 text-brain-400'
                    }`}>
                      {t.status}
                    </span>
                    <span className="truncate">{t.title}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Entity Types */}
          <div className="card bg-brain-900/60">
            <h3 className="text-sm font-semibold text-brain-300 mb-3 flex items-center gap-2">
              <FolderKanban className="w-4 h-4 text-blue-400" />
              {t('entity_types')}
            </h3>
            <div className="space-y-1">
              {Object.entries(data.entity_types).map(([type, count]) => (
                <div key={type} className="flex items-center justify-between text-sm">
                  <span className="text-brain-400">{type}</span>
                  <span className="text-brain-300 font-medium">{count}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Decisions */}
          <div className="card bg-brain-900/60">
            <h3 className="text-sm font-semibold text-brain-300 mb-3 flex items-center gap-2">
              <Lightbulb className="w-4 h-4 text-red-400" />
              {t('decisions_count_heading', { count: stats.decisions })}
            </h3>
            <div className="space-y-2 max-h-48 overflow-y-auto">
              {data.recent_decisions.map((d) => (
                <div key={d.id} className="text-sm p-2 bg-brain-800/40 rounded">
                  {d.summary}
                </div>
              ))}
              {data.recent_decisions.length === 0 && (
                <div className="text-sm text-brain-500">{t('no_decisions')}</div>
              )}
            </div>
          </div>

          {/* Access Groups */}
          <div className="card bg-brain-900/60">
            <h3 className="text-sm font-semibold text-brain-300 mb-3 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 text-yellow-400" />
              {t('access_groups')}
            </h3>
            <div className="space-y-1">
              {Object.entries(data.access_groups).map(([group, count]) => (
                <div key={group} className="flex items-center justify-between text-sm">
                  <span className="text-brain-400">{group}</span>
                  <span className="text-brain-300 font-medium">{t('nodes_count', { count })}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
