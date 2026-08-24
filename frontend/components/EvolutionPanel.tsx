'use client'

/**
 * EvolutionPanel — «Эволюция объекта» во времени.
 *
 * Показывает, КАК трансформировался объект (человек/решение/задача/проект):
 * таймлайн версий + field-level дифф между соседними («роль: dev → lead»,
 * «+ дедлайн», «− блокер»). Данные — SQLite TemporalTracker (без Neo4j):
 *   GET /temporal/entities            — кого показывать (versions>1 = менялся)
 *   GET /temporal/entity/{id}/versions — полная история версий
 * Диффы считаем на клиенте из data каждой версии (устойчиво к коллизиям дат).
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { X, GitBranch, Search, User, CheckSquare, Lightbulb, FolderKanban, Clock, Loader2 } from 'lucide-react'

interface Props { isOpen: boolean; onClose: () => void; userId?: string }

interface EntityRow {
  entity_id: string
  entity_type: string
  name: string
  versions: number
  first_seen?: string
  last_seen?: string
}
interface Version {
  version: number
  data: Record<string, any>
  timestamp: string
  change_type?: string
}

const TYPE_META: Record<string, { labelKey: string; icon: any }> = {
  person: { labelKey: 'type_person', icon: User },
  decision: { labelKey: 'type_decision', icon: Lightbulb },
  task: { labelKey: 'type_task', icon: CheckSquare },
  project: { labelKey: 'type_project', icon: FolderKanban },
}

function fmtVal(v: any): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') { try { return JSON.stringify(v) } catch { return String(v) } }
  const s = String(v)
  return s.length > 80 ? s.slice(0, 80) + '…' : s
}

function diffData(before: Record<string, any>, after: Record<string, any>) {
  const b = before || {}, a = after || {}
  const kb = new Set(Object.keys(b)), ka = new Set(Object.keys(a))
  const added: string[] = [...ka].filter((k) => !kb.has(k))
  const removed: string[] = [...kb].filter((k) => !ka.has(k))
  const changed: Array<{ k: string; from: any; to: any }> = []
  for (const k of [...ka].filter((x) => kb.has(x))) {
    if (JSON.stringify(b[k]) !== JSON.stringify(a[k])) changed.push({ k, from: b[k], to: a[k] })
  }
  return { added, removed, changed }
}

function fmtDate(s?: string): string {
  if (!s) return ''
  const d = new Date(s)
  return isNaN(d.getTime()) ? String(s).slice(0, 10) : d.toLocaleDateString('ru-RU', { year: 'numeric', month: 'short', day: 'numeric' })
}

export default function EvolutionPanel({ isOpen, onClose, userId }: Props) {
  const t = useTranslations('evolution_panel')
  const [entities, setEntities] = useState<EntityRow[]>([])
  const [loadingList, setLoadingList] = useState(false)
  const [query, setQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState<string>('all')
  const [selected, setSelected] = useState<EntityRow | null>(null)
  const [versions, setVersions] = useState<Version[]>([])
  const [loadingV, setLoadingV] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const uq = userId ? `?user_id=${encodeURIComponent(userId)}` : ''

  const loadEntities = useCallback(async () => {
    setLoadingList(true); setErr(null)
    try {
      const r = await authFetch(`/api/v1/temporal/entities${uq}`)
      const d = await r.json()
      if (d?.status === 'success') setEntities(d.entities || [])
      else setErr(d?.message || t('load_error'))
    } catch (e) { setErr((e as Error).message) } finally { setLoadingList(false) }
  }, [uq, t])

  useEffect(() => { if (isOpen) loadEntities() }, [isOpen, loadEntities])

  const openEntity = useCallback(async (e: EntityRow) => {
    setSelected(e); setVersions([]); setLoadingV(true)
    try {
      const r = await authFetch(`/api/v1/temporal/entity/${encodeURIComponent(e.entity_id)}/versions${uq}`)
      const d = await r.json()
      // versions приходят DESC — разворачиваем в хронологию (старое → новое)
      const vs: Version[] = (d?.versions || []).slice().sort((x: Version, y: Version) => x.version - y.version)
      setVersions(vs)
    } catch { setVersions([]) } finally { setLoadingV(false) }
  }, [uq])

  const filtered = useMemo(() => entities.filter((e) => {
    if (typeFilter !== 'all' && e.entity_type !== typeFilter) return false
    if (query && !String(e.name).toLowerCase().includes(query.toLowerCase())) return false
    return true
  }), [entities, typeFilter, query])

  const typesPresent = useMemo(() => Array.from(new Set(entities.map((e) => e.entity_type))), [entities])

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
      <div className="bg-brain-900 border border-brain-700 rounded-xl w-full max-w-5xl h-[85vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-brain-800">
          <div className="flex items-center gap-2">
            <GitBranch className="w-5 h-5 text-purple-400" />
            <div>
              <h2 className="text-white font-semibold">{t('title')}</h2>
              <p className="text-xs text-brain-500">{t('subtitle')}</p>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-brain-800 text-brain-400 hover:text-white">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 flex min-h-0">
          {/* Список объектов */}
          <div className="w-72 border-r border-brain-800 flex flex-col min-h-0">
            <div className="p-3 space-y-2 border-b border-brain-800">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-brain-500" />
                <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t('search_placeholder')}
                  className="w-full pl-8 pr-2 py-1.5 rounded-lg bg-brain-950 border border-brain-700 text-sm text-white placeholder-brain-500" />
              </div>
              <div className="flex flex-wrap gap-1">
                <button onClick={() => setTypeFilter('all')}
                  className={`px-2 py-0.5 rounded text-xs border ${typeFilter === 'all' ? 'bg-brain-700 border-brain-600 text-white' : 'border-brain-700 text-brain-400'}`}>{t('filter_all')}</button>
                {typesPresent.map((ty) => (
                  <button key={ty} onClick={() => setTypeFilter(ty)}
                    className={`px-2 py-0.5 rounded text-xs border ${typeFilter === ty ? 'bg-brain-700 border-brain-600 text-white' : 'border-brain-700 text-brain-400'}`}>
                    {TYPE_META[ty] ? t(TYPE_META[ty].labelKey) : ty}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex-1 overflow-y-auto">
              {loadingList ? (
                <div className="p-4 text-center text-brain-500 text-sm"><Loader2 className="w-4 h-4 animate-spin inline" /> {t('loading')}</div>
              ) : filtered.length === 0 ? (
                <div className="p-4 text-center text-brain-500 text-xs">
                  {err || t('empty_state')}
                </div>
              ) : filtered.map((e) => {
                const Icon = TYPE_META[e.entity_type]?.icon || GitBranch
                const evolved = e.versions > 1
                return (
                  <button key={`${e.entity_type}:${e.entity_id}`} onClick={() => openEntity(e)}
                    className={`w-full text-left px-3 py-2 flex items-center gap-2 border-b border-brain-800/50 hover:bg-brain-800/50 ${selected?.entity_id === e.entity_id ? 'bg-brain-800' : ''}`}>
                    <Icon className={`w-4 h-4 shrink-0 ${evolved ? 'text-purple-400' : 'text-brain-500'}`} />
                    <span className="flex-1 min-w-0 truncate text-sm text-brain-200">{e.name}</span>
                    <span className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded-full ${evolved ? 'bg-purple-500/20 text-purple-300' : 'bg-brain-800 text-brain-500'}`}>
                      {e.versions} v
                    </span>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Таймлайн эволюции */}
          <div className="flex-1 overflow-y-auto p-5">
            {!selected ? (
              <div className="h-full flex flex-col items-center justify-center text-center text-brain-500">
                <GitBranch className="w-10 h-10 mb-3 opacity-40" />
                <p>{t('select_hint')}</p>
                <p className="text-xs mt-1">{t('legend_prefix')} <span className="text-purple-400">v&gt;1</span> {t('legend_suffix')}</p>
              </div>
            ) : loadingV ? (
              <div className="text-brain-500 text-sm"><Loader2 className="w-4 h-4 animate-spin inline" /> {t('building_timeline')}</div>
            ) : (
              <div>
                <div className="mb-4">
                  <h3 className="text-lg font-semibold text-white">{selected.name}</h3>
                  <p className="text-xs text-brain-500">
                    {TYPE_META[selected.entity_type] ? t(TYPE_META[selected.entity_type].labelKey) : selected.entity_type} · {t('versions_count', { n: versions.length })} · {fmtDate(selected.first_seen)} → {fmtDate(selected.last_seen)}
                  </p>
                </div>

                {versions.length === 0 ? (
                  <p className="text-brain-500 text-sm">{t('empty_history')}</p>
                ) : (
                  <ol className="relative border-l border-brain-700 ml-2 space-y-5">
                    {versions.map((v, i) => {
                      const prev = i > 0 ? versions[i - 1] : null
                      const d = prev ? diffData(prev.data, v.data) : null
                      const nothing = d && d.added.length === 0 && d.removed.length === 0 && d.changed.length === 0
                      return (
                        <li key={v.version} className="ml-5">
                          <span className="absolute -left-[7px] w-3.5 h-3.5 rounded-full bg-purple-500 border-2 border-brain-900" />
                          <div className="flex items-center gap-2 text-xs text-brain-400">
                            <Clock className="w-3 h-3" />{fmtDate(v.timestamp)}
                            <span className="px-1.5 py-0.5 rounded bg-brain-800 text-brain-300">v{v.version}</span>
                            {v.change_type && <span className="text-brain-500">{v.change_type}</span>}
                          </div>

                          {!prev ? (
                            <div className="mt-1.5 text-sm text-brain-300">
                              {t('initial_state')}
                              <div className="mt-1 flex flex-wrap gap-1">
                                {Object.entries(v.data).slice(0, 8).map(([k, val]) => (
                                  <span key={k} className="text-[11px] px-1.5 py-0.5 rounded bg-brain-800 text-brain-300">{k}: {fmtVal(val)}</span>
                                ))}
                              </div>
                            </div>
                          ) : nothing ? (
                            <div className="mt-1.5 text-xs text-brain-500">{t('no_field_changes')}</div>
                          ) : (
                            <div className="mt-1.5 flex flex-wrap gap-1.5">
                              {d!.changed.map((c) => (
                                <span key={c.k} className="text-[11px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-200 border border-amber-500/25">
                                  {c.k}: <span className="text-brain-400 line-through">{fmtVal(c.from)}</span> → {fmtVal(c.to)}
                                </span>
                              ))}
                              {d!.added.map((k) => (
                                <span key={k} className="text-[11px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-200 border border-emerald-500/25">+ {k}: {fmtVal(v.data[k])}</span>
                              ))}
                              {d!.removed.map((k) => (
                                <span key={k} className="text-[11px] px-1.5 py-0.5 rounded bg-red-500/15 text-red-200 border border-red-500/25">− {k}</span>
                              ))}
                            </div>
                          )}
                        </li>
                      )
                    })}
                  </ol>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
