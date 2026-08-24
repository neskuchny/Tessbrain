'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { buildGraph3D, ensureThree, G3DNode, G3DEdge, G3DHandle } from '@/lib/graph3d'

// Ключи переводов (namespace graph3d_view) для типов узлов и типов связей.
const TYPE_LABEL_KEYS: Record<string, string> = {
  person: 'type_person', team: 'type_team', unit: 'type_unit', project: 'type_project',
  task: 'type_task', client: 'type_client', meeting: 'type_meeting', decision: 'type_decision', other: 'type_other',
}
const REL_LABEL_KEYS: Record<string, string> = {
  rep: 'rel_rep', reports_to: 'rel_reports_to', member: 'rel_member', member_of: 'rel_member_of',
  owns: 'rel_owns', leads: 'rel_leads', partof: 'rel_part_of', part_of: 'rel_part_of', for: 'rel_for',
  assignee: 'rel_assignee', assigned_to: 'rel_assignee',
}

// Живой 3D-граф поверх реальных данных:
//   mode='org'     → радиальная оргсхема (кто кому подчиняется, по отделам)
//   mode='company' → силовая сеть компании (люди/команды/проекты/задачи/клиенты)
// Three.js грузится с CDN; если не загрузился — показываем мягкий фолбэк.

interface Props {
  userId?: string | null
  mode: 'org' | 'company'
  onFallback?: () => void
  fallbackLabel?: string
  /** Открыть карточку сущности (человек/проект/…) из инфо-панели графа. */
  onOpenEntity?: (type: string, id: string, label: string) => void
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''
function authHeaders(): Record<string, string> {
  const t =
    typeof window !== 'undefined'
      ? localStorage.getItem('tessent_access_token') || localStorage.getItem('access_token')
      : null
  return t ? { Authorization: `Bearer ${t}` } : {}
}

const DEPT_HUES = [250, 205, 150, 30, 332, 265, 45, 190, 110, 12]
const hsl = (h: number, s: number, l: number) => `hsl(${h}, ${s}%, ${l}%)`

// Канонизируем метку узла графа в наш тип/форму (nameKey — ключ перевода в graph3d_view).
function classify(group: string): { type: string; shape: G3DNode['shape']; nameKey: string } {
  const g = (group || '').toLowerCase()
  if (g.includes('person') || g === 'role') return { type: 'person', shape: 'avatar', nameKey: 'group_people' }
  if (g.includes('team') || g.includes('department')) return { type: 'team', shape: 'hex', nameKey: 'group_teams' }
  if (g.includes('project') || g.includes('product')) return { type: 'project', shape: 'card', nameKey: 'group_projects' }
  if (g.includes('task') || g.includes('milestone')) return { type: 'task', shape: 'diamond', nameKey: 'group_tasks' }
  if (g.includes('client') || g.includes('customer') || g.includes('organization') || g === 'entity')
    return { type: 'client', shape: 'badge', nameKey: 'group_clients' }
  if (g.includes('meeting')) return { type: 'meeting', shape: 'badge', nameKey: 'group_meetings' }
  if (g.includes('decision')) return { type: 'decision', shape: 'diamond', nameKey: 'group_decisions' }
  return { type: 'other', shape: 'badge', nameKey: 'group_other' }
}

const STATUS_HUE: Record<string, number> = {
  todo: 220, plan: 220, planned: 220,
  doing: 38, in_progress: 38, active: 38,
  done: 150, completed: 150, выполнено: 150,
  blocked: 0, overdue: 0,
}

// Пресеты «настроения» сцены (из дизайн-прототипов: неон / космос / чертёж / атлас)
type ThemeId = 'neon' | 'space' | 'blueprint' | 'atlas'
interface Theme {
  id: ThemeId
  label: string
  bg: string
  fog: string
  glow: boolean
  starfield: boolean
  grid: boolean
  gridColor?: number
  edge: string
  dash: boolean
}
const THEMES: Theme[] = [
  { id: 'neon', label: 'Неон', fog: '#100b1f', glow: true, starfield: true, grid: false, edge: 'hsl(265,55%,66%)', dash: false,
    bg: 'radial-gradient(circle at 50% 40%, #1a1430 0%, #100b1f 62%, #08060f 100%)' },
  { id: 'space', label: 'Космос', fog: '#0a1226', glow: true, starfield: true, grid: false, edge: 'hsl(220,72%,66%)', dash: false,
    bg: 'radial-gradient(circle at 50% 36%, #141d3a 0%, #0a0f22 58%, #05060f 100%)' },
  { id: 'blueprint', label: 'Чертёж', fog: '#08161f', glow: false, starfield: false, grid: true, gridColor: 0x2c6f88, edge: 'hsl(190,72%,60%)', dash: true,
    bg: 'radial-gradient(circle at 50% 40%, #0c2433 0%, #08161f 62%, #050d14 100%)' },
  { id: 'atlas', label: 'Атлас', fog: '#11151f', glow: true, starfield: false, grid: false, edge: 'hsl(210,24%,58%)', dash: false,
    bg: 'radial-gradient(circle at 50% 32%, #1c2334 0%, #141a28 64%, #0e131d 100%)' },
]

export default function Graph3DView({ userId, mode, onFallback, fallbackLabel, onOpenEntity }: Props) {
  const t = useTranslations('graph3d_view')
  const rootRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLDivElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)
  const filterRef = useRef<HTMLDivElement>(null)
  const handleRef = useRef<G3DHandle | null>(null)
  const dataRef = useRef<{ byId: Record<string, G3DNode>; conns: Record<string, Array<{ id: string; rel: string }>> }>({ byId: {}, conns: {} })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [legend, setLegend] = useState<Array<{ type?: string; name: string; col: string }>>([])
  const [count, setCount] = useState<{ n: number; e: number }>({ n: 0, e: 0 })
  const [hiddenIso, setHiddenIso] = useState(0)
  const [isFs, setIsFs] = useState(false)
  const [selected, setSelected] = useState<{ node: G3DNode; conns: Array<{ node: G3DNode; rel: string }> } | null>(null)
  // Одна фиксированная 3D-тема (пресеты убраны — «одна понятная 3D-схема»).
  const [themeId] = useState<ThemeId>('neon')
  const theme = THEMES.find((t) => t.id === themeId) || THEMES[0]

  const buildOrg = (raw: any): { nodes: G3DNode[]; edges: G3DEdge[]; legend: any[] } => {
    const apiNodes: any[] = raw.nodes || []
    const apiEdges: any[] = raw.edges || []
    const byId: Record<string, any> = {}
    apiNodes.forEach((n) => (byId[n.id] = n))

    // отдел/команда → стабильный hue
    const deptHue: Record<string, number> = {}
    let hi = 0
    const hueFor = (key: string) => {
      if (!(key in deptHue)) deptHue[key] = DEPT_HUES[hi++ % DEPT_HUES.length]
      return deptHue[key]
    }

    // связи: REPORTS_TO (person→manager), MEMBER_OF (person→team/dept), LEADS (head→unit)
    const reportsTo: Record<string, string> = {}
    const memberOf: Record<string, string> = {}
    apiEdges.forEach((e) => {
      const t = (e.label || '').toUpperCase()
      if (t === 'REPORTS_TO') reportsTo[e.from] = e.to
      else if (t === 'MEMBER_OF') memberOf[e.from] = e.to
    })

    const ROOT = '__root__'
    const nodes: G3DNode[] = []
    const units = apiNodes.filter((n) => /team|department/i.test(n.group || ''))
    const people = apiNodes.filter((n) => /person/i.test(n.group || ''))

    // root компания
    nodes.push({ id: ROOT, shape: 'hex', label: raw.company_name || t('company_fallback'), hue: 218, col: hsl(218, 12, 78), parent: null })

    // отдел по ИМЕНИ (department у Person — строка, а не id узла)
    const unitByName: Record<string, string> = {}
    units.forEach((u) => {
      const nm = (u.label || u.title || '').trim().toLowerCase()
      if (nm) unitByName[nm] = u.id
    })

    const personNodes: G3DNode[] = []
    people.forEach((p) => {
      // parent: менеджер (REPORTS_TO) → отдел (MEMBER_OF) → отдел по имени → root
      let parent = ROOT
      let hueKey = 'misc'
      const mgr = reportsTo[p.id]
      const unit = memberOf[p.id]
      const deptName = (p.properties?.department || '').trim().toLowerCase()
      if (mgr && byId[mgr] && /person/i.test(byId[mgr].group || '')) {
        parent = mgr
        hueKey = memberOf[mgr] || (byId[mgr]?.properties?.department) || p.properties?.department || 'misc'
      } else if (unit && byId[unit]) {
        parent = unit
        hueKey = unit
      } else if (deptName && unitByName[deptName]) {
        parent = unitByName[deptName]  // ночная раскладка пишет ИМЯ отдела
        hueKey = unitByName[deptName]
      } else if (p.properties?.department) {
        hueKey = p.properties.department
      }
      const hue = parent === ROOT && hueKey === 'misc' ? 218 : hueFor(hueKey)
      personNodes.push({
        id: p.id,
        shape: 'avatar',
        label: p.label || p.title || t('employee_fallback'),
        sub: p.properties?.role || '',
        dept: p.properties?.department || '',
        parent,
        hue,
        col: hsl(hue, 55, 62),
        type: 'person',
      })
    })

    // ТОЛЬКО отделы с людьми: экстракция плодит сотни Department-узлов
    // (комитеты клиентских компаний, «Не указан»…) — пустые гексы и стена
    // чипов в легенде делали схему нечитаемой. Считаем людей на отдел
    // (прямых + через менеджеров) и выкидываем пустые.
    const childCount: Record<string, number> = {}
    personNodes.forEach((p) => {
      let anchor = p.parent as string
      // подняться по цепочке менеджеров до отдела
      const seenIds = new Set<string>()
      while (anchor && anchor !== ROOT && !seenIds.has(anchor)) {
        seenIds.add(anchor)
        const parentNode = personNodes.find((x) => x.id === anchor)
        if (!parentNode) break
        anchor = parentNode.parent as string
      }
      if (anchor && anchor !== ROOT) childCount[anchor] = (childCount[anchor] || 0) + 1
    })
    const liveUnits = units.filter((u) => (childCount[u.id] || 0) > 0)
    liveUnits.forEach((u) => {
      const hue = hueFor(u.id)
      nodes.push({ id: u.id, shape: 'hex', label: u.label || u.title || t('unit_fallback'), hue, col: hsl(hue, 50, 60), parent: ROOT, type: 'unit' })
    })
    // люди, чей parent-отдел выпал как пустой (не должен случиться, но
    // страхуемся) — к корню
    const liveIds = new Set([ROOT, ...liveUnits.map((u) => u.id), ...personNodes.map((p) => p.id)])
    personNodes.forEach((p) => {
      if (!liveIds.has(p.parent as string)) p.parent = ROOT
      nodes.push(p)
    })

    // линии = связи родитель→потомок (иначе купол без рёбер подчинения)
    const edges: G3DEdge[] = nodes
      .filter((n) => n.parent)
      .map((n) => ({ a: n.parent as string, b: n.id, type: 'rep' }))

    // легенда: топ-20 отделов по числу людей, не стена из 150 чипов
    const leg = liveUnits
      .sort((a, b) => (childCount[b.id] || 0) - (childCount[a.id] || 0))
      .slice(0, 20)
      .map((u) => ({ name: `${u.label || t('unit_fallback')} · ${childCount[u.id]}`, col: hsl(deptHue[u.id] ?? 218, 55, 62) }))
    return { nodes, edges, legend: leg }
  }

  const buildCompany = (raw: any): { nodes: G3DNode[]; edges: G3DEdge[]; legend: any[]; hiddenIsolated?: number } => {
    const apiNodes: any[] = raw.nodes || []
    const apiEdges: any[] = raw.edges || []
    const present: Record<string, { name: string; col: string }> = {}
    const NAMEKEY: Record<string, string> = {
      person: 'group_people', team: 'group_teams', project: 'group_projects',
      task: 'group_tasks', client: 'group_clients', meeting: 'group_meetings',
      decision: 'group_decisions', other: 'group_other',
    }
    // Мапим ВСЕ узлы (в т.ч. 'other'/чанки). Чистую бизнес-схему покажем ниже,
    // только если есть связная бизнес-структура; иначе — фолбэк на всё, чтобы 3D
    // показывал то же, что 2D (а не «нет данных», когда граф из чанков/одиночек).
    const mapNode = (n: any): G3DNode => {
      const cls = classify(n.group || '')
      let col = n.color || '#7b8aa8'
      const status = (n.properties?.status || '').toString().toLowerCase()
      if (cls.type === 'task' && STATUS_HUE[status] != null) col = hsl(STATUS_HUE[status], 60, 55)
      const sub = cls.type === 'person' ? (n.properties?.role || '')
        : cls.type === 'project' ? (n.properties?.status || '') : ''
      return { id: n.id, shape: cls.shape, label: n.label || n.title || '—', sub, type: cls.type, status, col }
    }
    const allNodes: G3DNode[] = apiNodes.filter((n) => n && n.id).map(mapNode)
    const business = allNodes.filter((n) => n.type !== 'other')
    const allIds = new Set(allNodes.map((n) => n.id))
    const edges: G3DEdge[] = apiEdges
      .filter((e) => allIds.has(e.from) && allIds.has(e.to))
      .map((e) => ({ a: e.from, b: e.to, type: (e.label || '').toLowerCase() }))
    const linked = new Set<string>()
    edges.forEach((e) => { linked.add(e.a); linked.add(e.b) })
    const bizIds = new Set(business.map((n) => n.id))
    const bizEdges = edges.filter((e) => bizIds.has(e.a) && bizIds.has(e.b))
    const connectedBiz = business.filter((n) => linked.has(n.id))
    // Цель — СВЯЗНЫЙ граф, а не облако точек. Если бизнес-узлы связаны между
    // собой (есть рёбра) — показываем чистую бизнес-схему; иначе (рёбра идут
    // через чанки/упоминания) показываем ВЕСЬ граф, чтобы были связи и смысл.
    const finalNodes = bizEdges.length >= 3
      ? (connectedBiz.length >= 2 ? connectedBiz : business)
      : allNodes
    const finalIds = new Set(finalNodes.map((n) => n.id))
    const finalEdges = edges.filter((e) => finalIds.has(e.a) && finalIds.has(e.b))
    finalNodes.forEach((n) => { const ty = n.type || 'other'; if (!present[ty]) present[ty] = { name: t(NAMEKEY[ty] || 'group_other'), col: n.col || '#7b8aa8' } })
    const leg = Object.entries(present).map(([type, v]) => ({ type, name: v.name, col: v.col }))
    const hiddenIsolated = connectedBiz.length >= 2 ? (allNodes.length - finalNodes.length) : 0
    return { nodes: finalNodes, edges: finalEdges, legend: leg, hiddenIsolated }
  }

  const load = useCallback(async () => {
    if (!userId || !canvasRef.current) return
    setLoading(true)
    setError(null)
    setSelected(null)
    if (handleRef.current) {
      handleRef.current.dispose()
      handleRef.current = null
    }
    try {
      const ok = await ensureThree()
      if (!ok) throw new Error(t('engine_unavailable_error'))
      const url =
        mode === 'org'
          ? `${API_BASE}/api/v1/entities/org-graph?user_id=${userId}&limit=300`
          : `${API_BASE}/api/v1/entities/graph?user_id=${userId}&limit=260`
      const res = await fetch(url, { headers: authHeaders() })
      const raw = await res.json().catch(() => ({}))
      if (raw.error) throw new Error(raw.error)
      const mapped = mode === 'org' ? buildOrg(raw) : buildCompany(raw)
      setLegend(mapped.legend)
      setCount({ n: mapped.nodes.length, e: mapped.edges.length })
      setHiddenIso((mapped as any).hiddenIsolated || 0)
      if (!mapped.nodes.length || (mode === 'org' && mapped.nodes.length <= 1)) {
        setError(t('no_data_error'))
        setLoading(false)
        return
      }
      // индекс для инфо-панели (узлы + список связей по каждому id)
      const byId: Record<string, G3DNode> = {}
      mapped.nodes.forEach((n) => (byId[n.id] = n))
      const conns: Record<string, Array<{ id: string; rel: string }>> = {}
      mapped.edges.forEach((e) => {
        ;(conns[e.a] = conns[e.a] || []).push({ id: e.b, rel: e.type || '' })
        ;(conns[e.b] = conns[e.b] || []).push({ id: e.a, rel: e.type || '' })
      })
      dataRef.current = { byId, conns }
      // дать DOM кадр на установку размеров
      requestAnimationFrame(() => {
        if (!canvasRef.current || !tipRef.current) return
        const edgeStyle =
          mode === 'org'
            ? () => ({ color: theme.edge, op: 0.42, dash: theme.dash })
            : (t: string) => {
                // в нейтральных пресетах типы связей сохраняют семантику цвета;
                // в «Чертёж»/«Атлас» подчиняем всё единому тону темы.
                if (theme.id === 'blueprint' || theme.id === 'atlas')
                  return { color: theme.edge, op: theme.id === 'blueprint' ? 0.5 : 0.34, dash: theme.dash }
                const m: Record<string, { color: string; op: number; dash: boolean }> = {
                  member: { color: 'hsl(210,30%,62%)', op: 0.32, dash: false },
                  member_of: { color: 'hsl(210,30%,62%)', op: 0.32, dash: false },
                  owns: { color: 'hsl(210,42%,64%)', op: 0.5, dash: false },
                  leads: { color: 'hsl(150,42%,60%)', op: 0.5, dash: false },
                  reports_to: { color: 'hsl(250,42%,66%)', op: 0.45, dash: false },
                  partof: { color: 'hsl(265,45%,66%)', op: 0.42, dash: false },
                  part_of: { color: 'hsl(265,45%,66%)', op: 0.42, dash: false },
                  assignee: { color: 'hsl(0,0%,66%)', op: 0.3, dash: true },
                  assigned_to: { color: 'hsl(0,0%,66%)', op: 0.3, dash: true },
                  for: { color: 'hsl(45,72%,62%)', op: 0.45, dash: true },
                }
                return m[t] || { color: theme.edge, op: 0.28, dash: false }
              }
        handleRef.current = buildGraph3D({
          container: canvasRef.current,
          tip: tipRef.current,
          mode: mode === 'org' ? 'tree' : 'force',
          nodes: mapped.nodes,
          edges: mapped.edges,
          fog: theme.fog,
          phi: mode === 'org' ? 0.82 : 1.02,
          edgeStyle,
          filterEl: mode === 'company' ? filterRef.current : null,
          glow: theme.glow,
          starfield: theme.starfield,
          grid: theme.grid,
          gridColor: theme.gridColor,
          // «сигналы» по связям и в оргсхеме тоже — схема живёт (концепт-апгрейд)
          pulses: theme.id !== 'blueprint',
          onSelect: (id) => {
            if (!id) {
              setSelected(null)
              return
            }
            const node = dataRef.current.byId[id]
            if (!node) return
            const conns = (dataRef.current.conns[id] || [])
              .map((c) => ({ node: dataRef.current.byId[c.id], rel: c.rel }))
              .filter((c) => c.node)
            setSelected({ node, conns })
          },
        })
        setLoading(false)
      })
    } catch (e: any) {
      setError(e?.message || t('build_failed_error'))
      setLoading(false)
    }
  }, [userId, mode, theme, t])

  useEffect(() => {
    load()
    return () => {
      if (handleRef.current) {
        handleRef.current.dispose()
        handleRef.current = null
      }
    }
  }, [load])

  const closePanel = () => {
    setSelected(null)
    handleRef.current?.clearSelect()
  }

  const isCompany = mode === 'company'
  return (
    <div ref={rootRef} className="relative w-full h-full flex flex-col bg-brain-950">
      {/* header / legend / filters */}
      <div className="px-4 pt-3 pb-2 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-brain-700/30 bg-brain-900/30 backdrop-blur-sm">
        <div className="flex items-center gap-2">
          <span className="text-white font-semibold text-sm">
            {isCompany ? t('company_graph_title') : t('org_chart_title')}
          </span>
          <span className="text-[11px] text-brain-400 font-mono border border-brain-600/30 rounded-full px-2 py-0.5">
            {isCompany ? t('network_3d_badge') : t('hierarchy_3d_badge')}
          </span>
          <span className="text-[11px] text-slate-500 font-mono">
            {t('nodes_count', { count: count.n })}{isCompany ? ` · ${t('edges_count', { count: count.e })}` : ''}
            {hiddenIso > 0 && <span title={t('hidden_isolated_title')}> · {t('hidden_isolated_count', { count: hiddenIso })}</span>}
          </span>
        </div>
        <div ref={filterRef} className="flex flex-wrap gap-1.5 items-center">
          {legend.map((l, i) => (
            <span
              key={i}
              className={`gv-chip inline-flex items-center gap-1.5 text-[11px] font-mono rounded-full px-2.5 py-1 border ${
                isCompany ? 'cursor-pointer select-none' : ''
              }`}
              data-type={isCompany ? l.type : undefined}
              style={{
                color: '#cfd6e6',
                borderColor: 'rgba(255,255,255,0.16)',
                transition: 'opacity .18s',
              }}
            >
              <span style={{ width: 9, height: 9, borderRadius: 3, background: l.col }} />
              {l.name}
            </span>
          ))}
        </div>
        {/* Одна чистая 3D-схема (без зоопарка пресетов) + переключение на 2D */}
        <div className="flex items-center gap-1 ml-auto">
          <button
            onClick={() => {
              const el = rootRef.current
              if (!el) return
              if (document.fullscreenElement) { document.exitFullscreen(); setIsFs(false) }
              else { el.requestFullscreen?.(); setIsFs(true) }
            }}
            className="text-[11px] text-brain-300 hover:text-white border border-brain-600/30 hover:border-brain-500/50 rounded-md px-2.5 py-1 transition-colors"
            title={t('fullscreen_title')}
          >
            {isFs ? t('exit_fullscreen') : t('enter_fullscreen')}
          </button>
          {onFallback && (
            <button
              onClick={onFallback}
              className="text-[11px] text-brain-300 hover:text-white border border-brain-600/30 hover:border-brain-500/50 rounded-md px-2.5 py-1 transition-colors"
              title={t('switch_to_2d_title')}
            >
              {fallbackLabel || t('graph_2d_button')}
            </button>
          )}
        </div>
      </div>

      {/* canvas */}
      <div className="relative flex-1 min-h-[420px] overflow-hidden" style={{ background: theme.bg }}>
        <div ref={canvasRef} className="absolute inset-0" />

        {/* Инфо-панель сущности (клик по узлу) */}
        <div
          className="absolute top-0 right-0 h-full p-3 z-[8] pointer-events-none flex items-start justify-end"
          style={{ width: 320, maxWidth: '82%' }}
        >
          <div
            className="pointer-events-auto w-full rounded-2xl p-4 border"
            style={{
              background: 'rgba(16,20,44,0.62)',
              borderColor: 'rgba(255,255,255,0.10)',
              backdropFilter: 'blur(18px)',
              WebkitBackdropFilter: 'blur(18px)',
              boxShadow: '0 24px 60px rgba(0,0,0,0.5)',
              transform: selected ? 'translateX(0)' : 'translateX(118%)',
              opacity: selected ? 1 : 0,
              transition: 'transform .42s cubic-bezier(.22,1,.36,1), opacity .42s',
            }}
          >
            {selected && (
              <>
                <div className="flex items-start justify-between gap-3">
                  <div className="text-white font-semibold text-base leading-tight">{selected.node.label}</div>
                  <button onClick={closePanel} className="text-slate-400 hover:text-white text-xl leading-none -mt-1">
                    ×
                  </button>
                </div>
                <div className="flex items-center gap-2 mt-2 text-[11px] uppercase tracking-wider text-slate-400">
                  <span style={{ width: 8, height: 8, borderRadius: 99, background: selected.node.col, boxShadow: `0 0 8px ${selected.node.col}` }} />
                  {t(TYPE_LABEL_KEYS[selected.node.type || 'other'] || 'type_other')}
                  {selected.node.sub ? <span className="text-slate-500 normal-case tracking-normal">· {selected.node.sub}</span> : null}
                </div>
                {onOpenEntity && ['person', 'project', 'client', 'task', 'unit', 'team'].includes(selected.node.type || '') && (
                  <button
                    onClick={() => onOpenEntity(selected.node.type || '', selected.node.id, selected.node.label)}
                    className="mt-3 w-full text-center text-xs bg-brain-600/60 hover:bg-brain-500/70 text-white rounded-lg px-3 py-2 transition-colors"
                  >
                    {t('open_card')}
                  </button>
                )}
                <div className="text-[10.5px] uppercase tracking-[1.4px] text-slate-500 mt-4 mb-2">
                  {t('connections_count', { count: selected.conns.length })}
                </div>
                <div className="flex flex-col gap-0.5 max-h-[46vh] overflow-auto pr-1">
                  {selected.conns.length === 0 && <div className="text-slate-500 text-xs px-1 py-2">{t('no_connections')}</div>}
                  {selected.conns.map((c, i) => (
                    <div key={i} className="flex items-center gap-2.5 px-2 py-1.5 rounded-lg hover:bg-white/5 transition-colors">
                      <span style={{ width: 7, height: 7, borderRadius: 99, background: c.node.col, boxShadow: `0 0 7px ${c.node.col}`, flex: 'none' }} />
                      <span className="flex-1 min-w-0 truncate text-[13px] text-slate-200">{c.node.label}</span>
                      <span className="text-[10.5px] text-slate-500 flex-none">{REL_LABEL_KEYS[c.rel] ? t(REL_LABEL_KEYS[c.rel]) : c.rel}</span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>

        <div
          ref={tipRef}
          style={{
            position: 'absolute',
            display: 'none',
            pointerEvents: 'none',
            background: 'rgba(12,14,22,0.94)',
            border: '1px solid rgba(255,255,255,0.14)',
            borderRadius: 9,
            padding: '8px 11px',
            fontFamily: 'IBM Plex Mono, monospace',
            fontSize: 12,
            color: '#eef1f8',
            zIndex: 5,
            backdropFilter: 'blur(4px)',
          }}
        />
        {loading && (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-slate-400 pointer-events-none">
            <div className="w-8 h-8 border-2 border-brain-500/30 border-t-brain-400 rounded-full animate-spin mb-3" />
            <div className="text-sm">{t('building_scene')}</div>
          </div>
        )}
        {error && !loading && (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-center px-6">
            <div className="text-3xl mb-2 opacity-60">🧭</div>
            <div className="text-slate-300 text-sm max-w-sm mb-3">{error}</div>
            {onFallback && (
              <button
                onClick={onFallback}
                className="text-xs bg-brain-700/50 hover:bg-brain-600/60 text-white rounded px-3 py-1.5 transition-colors"
              >
                {fallbackLabel || t('open_2d_view')}
              </button>
            )}
          </div>
        )}
        <div className="absolute left-3 bottom-2 text-[11px] font-mono text-slate-500/70 pointer-events-none z-[4]">
          {isCompany ? t('controls_hint_company') : t('controls_hint_org')}
        </div>
      </div>
    </div>
  )
}
