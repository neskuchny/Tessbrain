'use client'

/**
 * OrgGraphView — интерактивная оргсхема-граф (по мотивам tessent-org-graph).
 *
 * РЕАЛЬНЫЕ данные: Компания → Отделы → Люди (из /entities/org-graph + снапшот
 * компании; тот же источник, что у OrgSchemeView). Без выдуманных метрик —
 * карточка человека показывает только настоящие поля (роль, отдел, руководитель,
 * связей в графе).
 *
 * Взаимодействие: тяни фон (панорама), колесо — зум, перетаскивай узлы, дерево ↔
 * радиал, поиск, «оставить только отдел», раскрыть/свернуть отдел (показать
 * больше/свернуть), наведение — цепочка (человек → отдел → компания), клик —
 * карточка. Всё DOM+SVG, без WebGL/CDN.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { Loader2, Plus, Minus, Maximize2, X, Search, Trees, Radar, Trash2, Pencil } from 'lucide-react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const HUES = [265, 210, 170, 30, 300, 140, 200, 45, 330, 110]

interface Person { id: string; name: string; role: string; isHead: boolean; links: number }
interface Unit { id: string; name: string; hue: number; head: string; people: Person[] }
type Kind = 'company' | 'dept' | 'person'
interface GNode { id: string; kind: Kind; name: string; role?: string; hue?: number; deptId?: string; count?: number; links?: number; isHead?: boolean }

/** Собирает оргсхему ИЗ СНАПШОТА КОМПАНИИ (тот же источник, что вкладка
 *  «🏢 Компания»): Компания → CEO → Отделы (head+members) → Люди. Не зависит
 *  от графа знаний (который может быть не синхронизирован) — поэтому данные
 *  совпадают с карточкой компании и всегда есть руководитель. */
async function loadOrg(userId: string, fallbackDept: string, fallbackCompany: string):
  Promise<{ company: string; ceo: Person | null; leadership: Person[]; units: Unit[]; deptsMeta: any[] }> {
  const fetchJson = (url: string) => {
    const ac = new AbortController(); const timer = setTimeout(() => ac.abort(), 60000)
    return authFetch(url, { signal: ac.signal }).then((r) => r.json()).catch(() => ({})).finally(() => clearTimeout(timer))
  }
  // Компания (структура отделов + CEO) + ПОЛНЫЙ ростер людей (тот же
  // федеративный источник, что снепшоты). Полные списки сотрудников — в /people,
  // а не в усечённом company.departments.
  const [snapRes, peopleRes] = await Promise.all([
    fetchJson(`${API}/api/v1/snapshots/enhanced/company?user_id=${userId}`),
    fetchJson(`${API}/api/v1/snapshots/people?user_id=${userId}`),
  ])
  const snap = snapRes?.snapshot || {}
  const company = snap.name || fallbackCompany
  const snapDepts: any[] = Array.isArray(snap.departments) ? snap.departments : []
  const keyPeople: any[] = Array.isArray(snap.key_people) ? snap.key_people : []
  const founder = snap.founder || {}
  const roster: any[] = Array.isArray(peopleRes?.profiles) ? peopleRes.profiles : []
  const usedNames = new Set<string>()

  // CEO / основатель — вершина
  const ceoRe = /ceo|founder|основател|генеральн|глава|управляющ/i
  let ceo: Person | null = null
  if (founder?.name) ceo = { id: founder.person_id || 'p:ceo', name: founder.name, role: founder.role || 'CEO', isHead: true, links: 0 }
  else { const kp = keyPeople.find((p) => ceoRe.test(p.role || '')); if (kp) ceo = { id: kp.person_id || kp.id || 'p:ceo', name: kp.name, role: kp.role || 'CEO', isHead: true, links: 0 } }
  if (ceo) usedNames.add(ceo.name.trim().toLowerCase())

  const norm = (s: string) => (s || '').trim().toLowerCase()
  const units: Unit[] = snapDepts.map((d, i) => {
    const dpeople: Person[] = [], seen = new Set<string>()
    const add = (name: string, id: string, role: string, head: boolean) => {
      const k = norm(name); if (!name || seen.has(k)) return
      dpeople.push({ id, name, role, isHead: head, links: 0 }); seen.add(k); usedNames.add(k)
    }
    if (d.head) add(d.head, `p:${d.name}:head`, '', true)
    ;(Array.isArray(d.members) ? d.members : []).forEach((m: string) => add(m, `p:${d.name}:${m}`, '', false))
    // ПОЛНЫЙ ростер: все люди из /people с этим отделом (реальные id → правка в БД)
    roster.forEach((p: any) => { if (p?.name && norm(p.department) === norm(d.name)) add(p.name, p.id || p.person_id || `p:${d.name}:${p.name}`, p.role || '', ceoRe.test(p.role || '')) })
    return { id: `dept:${d.name}`, name: d.name || fallbackDept, hue: HUES[i % HUES.length], head: d.head || '', people: dpeople }
  })

  // Люди из /people без совпавшего отдела → «Без отдела»
  const rest: Person[] = roster
    .filter((p: any) => p?.name && !usedNames.has(norm(p.name)))
    .map((p: any) => { usedNames.add(norm(p.name)); return { id: p.id || p.person_id || `p:free:${p.name}`, name: p.name, role: p.role || '', isHead: false, links: 0 } })
  let finalUnits = units.filter((u) => u.people.length > 0)
  if (rest.length) finalUnits = [...finalUnits, { id: 'dept:__free__', name: fallbackDept, hue: 220, head: '', people: rest }]

  // Прочее руководство (key_people, не попавшие никуда) — под CEO
  const leadership: Person[] = keyPeople
    .filter((p) => p.name && !usedNames.has(norm(p.name)))
    .map((p) => ({ id: p.person_id || p.id || `p:lead:${p.name}`, name: p.name, role: p.role || '', isHead: true, links: 0 }))

  return { company, ceo, leadership, units: finalUnits, deptsMeta: snapDepts }
}

const GAPX = 176, GAPY = 150, RING = 200, PAD = 70

export default function OrgGraphView({ userId, onOpenEntity }: {
  userId?: string | null
  onOpenEntity?: (type: string, id: string, label: string) => void
}) {
  const t = useTranslations('org_scheme_view')
  const [company, setCompany] = useState('')
  const [ceo, setCeo] = useState<Person | null>(null)
  const [leadership, setLeadership] = useState<Person[]>([])
  const [units, setUnits] = useState<Unit[]>([])
  const [loading, setLoading] = useState(true)
  const [layout, setLayout] = useState<'tree' | 'radial'>('tree')
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [isolate, setIsolate] = useState<string | null>(null)   // dept id или null
  const [query, setQuery] = useState('')
  const [hoverId, setHoverId] = useState<string | null>(null)
  const [card, setCard] = useState<GNode | null>(null)
  const [drag, setDrag] = useState<Record<string, { x: number; y: number }>>({})

  const viewportRef = useRef<HTMLDivElement | null>(null)
  const view = useRef({ scale: 1, x: 0, y: 0 })
  const deptsMetaRef = useRef<any[]>([])
  const [editName, setEditName] = useState('')            // карточка: правка имени
  const [editingDept, setEditingDept] = useState<{ id: string; name: string } | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const flash = (m: string) => { setToast(m); setTimeout(() => setToast(null), 2200) }

  useEffect(() => {
    if (!userId) return
    let alive = true; setLoading(true)
    loadOrg(userId, t('department_fallback'), t('company_fallback'))
      .then((d) => { if (alive) { setCompany(d.company); setCeo(d.ceo); setLeadership(d.leadership); setUnits(d.units); deptsMetaRef.current = d.deptsMeta } })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [userId, t])

  const reload = useCallback(async () => {
    if (!userId) return
    const d = await loadOrg(userId, t('department_fallback'), t('company_fallback'))
    setCompany(d.company); setCeo(d.ceo); setLeadership(d.leadership); setUnits(d.units); deptsMetaRef.current = d.deptsMeta
  }, [userId, t])

  // Сохранить структуру отделов как company-override "departments" (в БД,
  // переживает регенерацию снапшота). Тот же механизм, что в редакторе оргструктуры.
  const persistDepartments = useCallback(async (nextUnits: Unit[]) => {
    if (!userId) return
    const byName: Record<string, Unit> = {}
    nextUnits.forEach((u) => (byName[u.name.trim().toLowerCase()] = u))
    const knownNames = deptsMetaRef.current.length
      ? deptsMetaRef.current.map((d: any) => d.name).filter(Boolean)
      : nextUnits.filter((u) => u.id !== 'dept:__free__').map((u) => u.name)
    // добавим новые отделы, которых нет в meta
    nextUnits.forEach((u) => { if (u.id !== 'dept:__free__' && !knownNames.some((n: string) => n.trim().toLowerCase() === u.name.trim().toLowerCase())) knownNames.push(u.name) })
    const payload = knownNames.map((nm: string) => {
      const u = byName[nm.trim().toLowerCase()]
      const meta = deptsMetaRef.current.find((d: any) => (d.name || '').trim().toLowerCase() === nm.trim().toLowerCase())
      const members = u ? u.people.map((p) => p.name) : (meta?.members || [])
      return { name: nm, description: u?.head ? (meta?.description || '') : (meta?.description || ''), head: u?.head || meta?.head || '', members, employees_count: members.length }
    })
    const res = await authFetch(`${API}/api/v1/snapshots/enhanced/company/override?user_id=${userId}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ field: 'departments', value: payload }) })
    const data = await res.json().catch(() => ({}))
    if (!res.ok || data?.status === 'error') throw new Error(data?.message || 'save failed')
  }, [userId])

  const isRealId = (id: string) => !!id && !id.startsWith('d:') && !id.startsWith('dept:')

  // Перенести человека в другой отдел (в БД).
  const movePerson = useCallback(async (personId: string, targetUnitId: string) => {
    let moved: Person | null = null
    const next = units.map((u) => ({ ...u, people: u.people.filter((p) => { if (p.id === personId) { moved = p; return false } return true }) }))
    if (!moved) return
    const tu = next.find((u) => u.id === targetUnitId); if (!tu) return
    tu.people = [...tu.people, moved]
    setBusy(true)
    try { setUnits(next); await persistDepartments(next); flash(t('saved_moved')); await reload() }
    catch { flash(t('save_error')) } finally { setBusy(false) }
  }, [units, persistDepartments, reload, t])

  // Переименовать человека (граф-узел в БД + синхронизация отделов).
  const renamePerson = useCallback(async (person: GNode, newName: string) => {
    const nm = newName.trim(); if (!nm || nm === person.name) return
    setBusy(true)
    try {
      if (isRealId(person.id)) {
        const res = await authFetch(`${API}/api/v1/entities/rename?user_id=${userId}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ node_id: person.id, name: nm }) })
        const d = await res.json().catch(() => ({})); if (!res.ok || d?.status === 'error') throw new Error(d?.message)
      }
      const next = units.map((u) => ({ ...u, people: u.people.map((p) => p.id === person.id ? { ...p, name: nm } : p) }))
      setUnits(next); await persistDepartments(next); flash(t('saved_renamed')); await reload()
    } catch { flash(t('save_error')) } finally { setBusy(false); setCard(null) }
  }, [units, userId, persistDepartments, reload, t])

  // Удалить человека (из графа + tombstone, в БД).
  const deletePerson = useCallback(async (person: GNode) => {
    if (!window.confirm(t('confirm_delete', { name: person.name }))) return
    setBusy(true)
    try {
      const res = await authFetch(`${API}/api/v1/snapshots/enhanced/entity/delete?user_id=${userId}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: person.name, entity_id: isRealId(person.id) ? person.id : null, label: 'Person' }) })
      const d = await res.json().catch(() => ({})); if (!res.ok || d?.status === 'error') throw new Error(d?.message)
      const next = units.map((u) => ({ ...u, people: u.people.filter((p) => p.id !== person.id) }))
      setUnits(next); await persistDepartments(next); flash(t('saved_deleted')); await reload()
    } catch { flash(t('save_error')) } finally { setBusy(false); setCard(null) }
  }, [units, userId, persistDepartments, reload, t])

  // Переименовать отдел (в БД).
  const renameDept = useCallback(async (unitId: string, newName: string) => {
    const nm = newName.trim(); const oldName = units.find((u) => u.id === unitId)?.name || ''
    if (!nm || nm === oldName) { setEditingDept(null); return }
    // Переименовываем и в meta, иначе payload создаст «отдел-призрак» под старым именем.
    deptsMetaRef.current = deptsMetaRef.current.map((d: any) => (d.name || '').trim().toLowerCase() === oldName.trim().toLowerCase() ? { ...d, name: nm } : d)
    const next = units.map((u) => u.id === unitId ? { ...u, name: nm } : u)
    setBusy(true)
    try { setUnits(next); await persistDepartments(next); flash(t('saved_renamed')); await reload() }
    catch { flash(t('save_error')) } finally { setBusy(false); setEditingDept(null) }
  }, [units, persistDepartments, reload, t])

  useEffect(() => { setEditName(card?.name || '') }, [card])

  // ── строим узлы/рёбра по текущему состоянию (collapse/isolate) ──
  const { nodes, edges, positions, bounds } = useMemo(() => {
    const shownUnits = isolate ? units.filter((u) => u.id === isolate) : units
    const nodes: GNode[] = [{ id: '__root__', kind: 'company', name: company || t('company_fallback') }]
    const edges: [string, string, number][] = []   // from,to,hue
    const kids: Record<string, string[]> = { __root__: [] }
    // CEO — вершина под компанией; отделы и прочее руководство висят под ним
    const CEO_HUE = 265
    let deptParent = '__root__'
    if (ceo) {
      nodes.push({ id: ceo.id, kind: 'person', name: ceo.name, role: ceo.role, hue: CEO_HUE, links: ceo.links, isHead: true })
      kids.__root__.push(ceo.id); kids[ceo.id] = []; edges.push(['__root__', ceo.id, CEO_HUE])
      deptParent = ceo.id
      leadership.forEach((p) => {
        nodes.push({ id: p.id, kind: 'person', name: p.name, role: p.role, hue: CEO_HUE, links: p.links, isHead: true })
        kids[ceo.id].push(p.id); edges.push([ceo.id, p.id, CEO_HUE])
      })
    }
    shownUnits.forEach((u) => {
      nodes.push({ id: u.id, kind: 'dept', name: u.name, hue: u.hue, head: u.head, count: u.people.length } as GNode)
      ;(kids[deptParent] = kids[deptParent] || []).push(u.id); kids[u.id] = []; edges.push([deptParent, u.id, u.hue])
      if (!collapsed.has(u.id)) {
        u.people.forEach((p) => {
          nodes.push({ id: p.id, kind: 'person', name: p.name, role: p.role, hue: u.hue, deptId: u.id, links: p.links, isHead: p.isHead })
          kids[u.id].push(p.id); edges.push([u.id, p.id, u.hue])
        })
      }
    })
    // раскладка (tidy tree): назначаем x листам по порядку, внутренним — среднее
    const depthOf: Record<string, number> = { __root__: 0 }
    const pos: Record<string, { x: number; y: number }> = {}
    let leaf = 0
    const order: string[] = []
    const walk = (id: string, depth: number) => {
      depthOf[id] = depth
      const ch = kids[id] || []
      if (!ch.length) { pos[id] = { x: leaf * GAPX, y: depth * GAPY }; leaf++; order.push(id) }
      else { ch.forEach((c) => walk(c, depth + 1)); pos[id] = { x: (pos[ch[0]].x + pos[ch[ch.length - 1]].x) / 2, y: depth * GAPY } }
    }
    walk('__root__', 0)
    const leafCount = Math.max(1, leaf)
    // радиал: угол по порядку листа, радиус по глубине
    if (layout === 'radial') {
      const leafAng: Record<string, number> = {}
      order.forEach((id, i) => { leafAng[id] = (i / leafCount) * Math.PI * 2 })
      const ang: Record<string, number> = {}
      const setAng = (id: string): number => {
        const ch = kids[id] || []
        if (!ch.length) return (ang[id] = leafAng[id])
        const a = ch.map(setAng); ang[id] = (a[0] + a[a.length - 1]) / 2; return ang[id]
      }
      setAng('__root__')
      Object.keys(pos).forEach((id) => { const r = depthOf[id] * RING; pos[id] = { x: Math.cos(ang[id] - Math.PI / 2) * r, y: Math.sin(ang[id] - Math.PI / 2) * r } })
    }
    // нормализуем базовую раскладку к (PAD,PAD)
    let minx = 1e9, miny = 1e9
    Object.keys(pos).forEach((id) => { minx = Math.min(minx, pos[id].x); miny = Math.min(miny, pos[id].y) })
    Object.keys(pos).forEach((id) => { pos[id] = { x: pos[id].x - minx + PAD, y: pos[id].y - miny + PAD } })
    // ручной drag — уже в координатах холста, накладываем ПОСЛЕ нормализации
    Object.keys(pos).forEach((id) => { if (drag[id]) pos[id] = drag[id] })
    // границы по финальным позициям
    let maxx = -1e9, maxy = -1e9
    Object.keys(pos).forEach((id) => { maxx = Math.max(maxx, pos[id].x); maxy = Math.max(maxy, pos[id].y) })
    return { nodes, edges, positions: pos, bounds: { w: maxx + PAD, h: maxy + PAD } }
  }, [units, company, ceo, leadership, collapsed, isolate, layout, drag, t])

  // цепочка подсветки: hover человек → его отдел+компания; hover отдел → его люди
  const chain = useMemo(() => {
    if (!hoverId) return null
    const s = new Set<string>([hoverId])
    const n = nodes.find((x) => x.id === hoverId)
    if (n?.kind === 'person') { s.add('__root__'); if (n.deptId) s.add(n.deptId) }
    if (n?.kind === 'dept') { s.add('__root__'); nodes.forEach((x) => { if (x.deptId === n.id) s.add(x.id) }) }
    return s
  }, [hoverId, nodes])

  // ── pan / zoom ──
  const apply = () => { const el = viewportRef.current?.querySelector('.org-canvas') as HTMLElement | null; if (el) el.style.transform = `translate(${view.current.x}px,${view.current.y}px) scale(${view.current.scale})` }
  const fit = useCallback(() => {
    const vp = viewportRef.current; if (!vp) return
    const s = Math.min(vp.clientWidth / bounds.w, vp.clientHeight / bounds.h) * 0.9
    view.current.scale = Math.max(0.2, Math.min(1.4, s))
    view.current.x = (vp.clientWidth - bounds.w * view.current.scale) / 2
    view.current.y = (vp.clientHeight - bounds.h * view.current.scale) / 2
    apply()
  }, [bounds])
  useEffect(() => { const id = setTimeout(fit, 60); return () => clearTimeout(id) }, [fit, loading])

  const panning = useRef<{ x: number; y: number; px: number; py: number } | null>(null)
  const dragNode = useRef<string | null>(null)
  const moved = useRef(false)
  const onBgDown = (e: React.PointerEvent) => { if ((e.target as HTMLElement).closest('.org-node')) return; panning.current = { x: e.clientX, y: e.clientY, px: view.current.x, py: view.current.y }; moved.current = false }
  const onNodeDown = (e: React.PointerEvent, id: string) => { if (id === '__root__') return; e.stopPropagation(); dragNode.current = id; moved.current = false }
  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (dragNode.current) {
        const vp = viewportRef.current; if (!vp) return
        const r = vp.getBoundingClientRect()
        const wx = (e.clientX - r.left - view.current.x) / view.current.scale
        const wy = (e.clientY - r.top - view.current.y) / view.current.scale
        moved.current = true
        setDrag((d) => ({ ...d, [dragNode.current!]: { x: wx, y: wy } }))
        return
      }
      if (panning.current) { view.current.x = panning.current.px + (e.clientX - panning.current.x); view.current.y = panning.current.py + (e.clientY - panning.current.y); if (Math.hypot(e.clientX - panning.current.x, e.clientY - panning.current.y) > 4) moved.current = true; apply() }
    }
    const up = () => { panning.current = null; dragNode.current = null }
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up)
    return () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up) }
  }, [])
  const onWheel = (e: React.WheelEvent) => {
    const vp = viewportRef.current; if (!vp) return; const r = vp.getBoundingClientRect()
    const cx = e.clientX - r.left, cy = e.clientY - r.top
    const f = e.deltaY < 0 ? 1.12 : 0.89
    const wx = (cx - view.current.x) / view.current.scale, wy = (cy - view.current.y) / view.current.scale
    view.current.scale = Math.max(0.2, Math.min(2.2, view.current.scale * f))
    view.current.x = cx - wx * view.current.scale; view.current.y = cy - wy * view.current.scale; apply()
  }
  const zoom = (f: number) => { const vp = viewportRef.current; if (!vp) return; const cx = vp.clientWidth / 2, cy = vp.clientHeight / 2; const wx = (cx - view.current.x) / view.current.scale, wy = (cy - view.current.y) / view.current.scale; view.current.scale = Math.max(0.2, Math.min(2.2, view.current.scale * f)); view.current.x = cx - wx * view.current.scale; view.current.y = cy - wy * view.current.scale; apply() }

  const q = query.trim().toLowerCase()

  if (loading) return <div className="h-full flex items-center justify-center text-brain-400"><Loader2 className="w-5 h-5 animate-spin mr-2" />{t('loading')}</div>
  if (!units.length && !ceo) return <div className="h-full flex items-center justify-center text-center text-brain-500 p-8">{t('empty_hint')}</div>

  return (
    <div className="h-full flex flex-col">
      {/* Панель управления */}
      <div className="flex items-center gap-2 flex-wrap mb-3">
        <div className="inline-flex rounded-lg border border-brain-600/40 overflow-hidden">
          <button onClick={() => setLayout('tree')} className={`px-3 py-1.5 text-xs inline-flex items-center gap-1.5 ${layout === 'tree' ? 'bg-brain-600 text-white' : 'text-brain-400 hover:text-brain-200'}`}><Trees className="w-3.5 h-3.5" />{t('layout_tree')}</button>
          <button onClick={() => setLayout('radial')} className={`px-3 py-1.5 text-xs inline-flex items-center gap-1.5 ${layout === 'radial' ? 'bg-brain-600 text-white' : 'text-brain-400 hover:text-brain-200'}`}><Radar className="w-3.5 h-3.5" />{t('layout_radial')}</button>
        </div>
        <div className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-brain-900/50 border border-brain-700/40">
          <Search className="w-3.5 h-3.5 text-brain-500" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t('search_placeholder')}
            className="bg-transparent outline-none text-xs text-brain-100 placeholder:text-brain-600 w-40" />
        </div>
        {isolate && (
          <button onClick={() => setIsolate(null)} className="px-2.5 py-1.5 rounded-lg bg-amber-600/80 text-white text-xs inline-flex items-center gap-1">
            {t('show_all_depts')} <X className="w-3 h-3" />
          </button>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          <button onClick={() => zoom(1.2)} className="w-8 h-8 grid place-items-center rounded-lg border border-brain-700/40 text-brain-400 hover:text-brain-200"><Plus className="w-4 h-4" /></button>
          <button onClick={() => zoom(0.83)} className="w-8 h-8 grid place-items-center rounded-lg border border-brain-700/40 text-brain-400 hover:text-brain-200"><Minus className="w-4 h-4" /></button>
          <button onClick={fit} className="w-8 h-8 grid place-items-center rounded-lg border border-brain-700/40 text-brain-400 hover:text-brain-200"><Maximize2 className="w-4 h-4" /></button>
        </div>
      </div>

      {/* Легенда отделов — клик «оставить только отдел» */}
      <div className="flex items-center gap-1.5 flex-wrap mb-2">
        {units.map((u) => (
          <button key={u.id} onClick={() => setIsolate(isolate === u.id ? null : u.id)}
            title={t('keep_only_dept')}
            className={`text-[11px] px-2 py-0.5 rounded-full border inline-flex items-center gap-1.5 transition-colors ${isolate === u.id ? 'border-white/40 bg-white/10 text-white' : 'border-brain-700/40 text-brain-400 hover:text-brain-200'}`}>
            <span className="w-2 h-2 rounded-full" style={{ background: `hsl(${u.hue} 70% 60%)` }} />
            {u.name} <span className="text-brain-600 tabular-nums">{u.people.length}</span>
          </button>
        ))}
      </div>

      {/* Полотно */}
      <div ref={viewportRef} onPointerDown={onBgDown} onWheel={onWheel}
        className="relative flex-1 min-h-0 rounded-xl overflow-hidden border border-brain-700/40 cursor-grab"
        style={{ background: 'radial-gradient(80% 70% at 50% 30%, #0b1430 0%, #070b18 78%)' }}>
        <div className="absolute top-3 left-3 z-10 text-[10px] text-brain-500 bg-black/40 border border-brain-700/40 rounded px-2 py-1 pointer-events-none">
          {t('org_hint_edit')}
        </div>
        {toast && (
          <div className="absolute top-3 right-3 z-20 text-xs text-white bg-brain-700/90 border border-brain-500/50 rounded-lg px-3 py-1.5 pointer-events-none">
            {busy ? '…' : '✓'} {toast}
          </div>
        )}
        <div className="org-canvas absolute top-0 left-0" style={{ transformOrigin: '0 0', width: bounds.w, height: bounds.h }}>
          {/* связи */}
          <svg width={bounds.w} height={bounds.h} className="absolute top-0 left-0 overflow-visible pointer-events-none">
            {edges.map(([a, b, hue], i) => {
              const pa = positions[a], pb = positions[b]; if (!pa || !pb) return null
              const on = !chain || (chain.has(a) && chain.has(b))
              const my = (pa.y + pb.y) / 2
              return <path key={i} d={`M ${pa.x} ${pa.y} C ${pa.x} ${my}, ${pb.x} ${my}, ${pb.x} ${pb.y}`}
                fill="none" stroke={`hsl(${hue} 70% 60%)`} strokeWidth={on && chain ? 2 : 1.3}
                opacity={chain ? (on ? 0.9 : 0.05) : 0.3} strokeLinecap="round" />
            })}
          </svg>
          {/* узлы */}
          {nodes.map((n) => {
            const p = positions[n.id]; if (!p) return null
            const dim = (chain && !chain.has(n.id)) || (q && n.kind === 'person' && !(n.name.toLowerCase().includes(q) || (n.role || '').toLowerCase().includes(q)))
            const hue = n.hue ?? 265
            const isCompany = n.kind === 'company', isDept = n.kind === 'dept'
            const w = isCompany ? 200 : isDept ? 190 : 168
            return (
              <div key={n.id} className="org-node absolute -translate-x-1/2 -translate-y-1/2 group"
                style={{ left: p.x, top: p.y, width: w, opacity: dim ? 0.16 : 1, transition: 'opacity .25s' }}
                onPointerDown={(e) => onNodeDown(e, n.id)}
                onPointerEnter={() => setHoverId(n.id)} onPointerLeave={() => setHoverId(null)}
                onDoubleClick={() => { if (isDept) setEditingDept({ id: n.id, name: n.name }) }}
                onClick={() => { if (moved.current) return; if (n.kind === 'person') { setCard(n) } else if (isDept && editingDept?.id !== n.id) { setCollapsed((c) => { const s = new Set(c); s.has(n.id) ? s.delete(n.id) : s.add(n.id); return s }) } }}>
                <div className="flex items-center gap-2.5 px-3 py-2 rounded-xl border cursor-pointer"
                  style={{
                    background: 'linear-gradient(180deg, rgba(20,32,66,.95), rgba(12,20,44,.95))',
                    borderColor: hoverId === n.id ? 'hsl(200 80% 65%)' : 'rgba(38,56,106,.9)',
                    boxShadow: isCompany ? '0 8px 30px rgba(120,90,255,.25)' : '0 6px 20px rgba(0,0,0,.28)',
                  }}>
                  <div className="grid place-items-center rounded-full flex-none font-bold text-[11px] text-[#0a1020]"
                    style={{ width: isCompany ? 40 : 32, height: isCompany ? 40 : 32, background: `linear-gradient(135deg, hsl(${hue} 70% 62%), hsl(${hue} 70% 40%))` }}>
                    {isDept ? '▦' : isCompany ? '◈' : n.name.split(' ').slice(0, 2).map((s) => s[0]).join('').toUpperCase()}
                  </div>
                  <div className="min-w-0 flex-1">
                    {isDept && editingDept?.id === n.id ? (
                      <input autoFocus value={editingDept.name} onPointerDown={(e) => e.stopPropagation()}
                        onChange={(e) => setEditingDept({ id: n.id, name: e.target.value })}
                        onKeyDown={(e) => { if (e.key === 'Enter') renameDept(n.id, editingDept.name); if (e.key === 'Escape') setEditingDept(null) }}
                        onBlur={() => renameDept(n.id, editingDept.name)}
                        className="w-full bg-brain-950 border border-brain-500 rounded px-1.5 py-0.5 text-[13px] text-brain-50 outline-none" />
                    ) : (
                      <div className="text-[13px] font-semibold text-brain-50 truncate flex items-center gap-1.5">
                        {n.name}
                        {n.isHead && <span className="text-[8px] px-1 rounded bg-amber-500/20 text-amber-300 uppercase">{t('head_badge')}</span>}
                        {isDept && <button onPointerDown={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); setEditingDept({ id: n.id, name: n.name }) }} className="opacity-0 group-hover:opacity-100 text-brain-500 hover:text-brain-200" title={t('rename_dept')}><Pencil className="w-3 h-3" /></button>}
                      </div>
                    )}
                    <div className="text-[11px] text-brain-400 truncate">
                      {isCompany ? t('company_label') : isDept ? `${t('dept_label')} · ${n.count} ${collapsed.has(n.id) ? `· ${t('collapsed')}` : ''}` : (n.role || t('employee_fallback'))}
                    </div>
                  </div>
                  {isDept && n.count! > 0 && (
                    <span className="ml-auto text-[10px] text-brain-500 flex-none">{collapsed.has(n.id) ? '▸' : '▾'}</span>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Карточка человека — только реальные поля */}
      {card && (
        <div className="fixed inset-0 z-[100] grid place-items-center bg-black/60 backdrop-blur-sm p-4" onClick={() => setCard(null)}>
          <div className="w-[min(520px,94vw)] rounded-2xl border border-brain-600/50 bg-gradient-to-b from-brain-900 to-brain-950 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="p-6 border-b border-brain-700/40 flex items-center gap-4">
              <div className="grid place-items-center rounded-2xl flex-none font-bold text-lg text-[#0a1020]" style={{ width: 60, height: 60, background: `linear-gradient(135deg, hsl(${card.hue ?? 265} 70% 62%), hsl(${card.hue ?? 265} 70% 40%))` }}>
                {card.name.split(' ').slice(0, 2).map((s) => s[0]).join('').toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                {/* Правка имени — сохраняется в БД */}
                <input value={editName} onChange={(e) => setEditName(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') renamePerson(card, editName) }}
                  className="w-full bg-brain-800/60 border border-brain-700/50 focus:border-brain-500 rounded-lg px-2.5 py-1.5 text-lg font-bold text-brain-50 outline-none" />
                <div className="text-sm text-brain-400 mt-1">{card.role || t('employee_fallback')}</div>
              </div>
              <button onClick={() => setCard(null)} className="w-8 h-8 grid place-items-center rounded-lg border border-brain-700/40 text-brain-400 hover:text-white"><X className="w-4 h-4" /></button>
            </div>
            <div className="p-6 grid grid-cols-2 gap-4 text-sm">
              {/* Перенос в другой отдел — сохраняется в БД */}
              <div className="col-span-2">
                <div className="text-[10px] uppercase tracking-wider text-brain-500 mb-1">{t('move_to_dept')}</div>
                <select value={card.deptId || ''} disabled={busy}
                  onChange={(e) => { if (e.target.value && e.target.value !== card.deptId) movePerson(card.id, e.target.value) }}
                  className="w-full bg-brain-800/60 border border-brain-700/50 rounded-lg px-2.5 py-2 text-brain-100 outline-none">
                  {units.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
                </select>
              </div>
              <div><div className="text-[10px] uppercase tracking-wider text-brain-500 mb-1">{t('head_role')}</div><div className="text-brain-100">{units.find((u) => u.id === card.deptId)?.head || '—'}</div></div>
              <div><div className="text-[10px] uppercase tracking-wider text-brain-500 mb-1">{t('links_label')}</div><div className="text-brain-100 tabular-nums">{card.links ?? 0}</div></div>
            </div>
            <div className="px-6 pb-6 flex items-center gap-2">
              <button disabled={busy || !editName.trim() || editName === card.name} onClick={() => renamePerson(card, editName)}
                className="flex-1 py-2.5 rounded-lg bg-brain-600 hover:bg-brain-500 disabled:opacity-40 text-white text-sm font-medium">
                {t('save_name')}
              </button>
              {onOpenEntity && (
                <button onClick={() => { onOpenEntity('person', card.id, card.name); setCard(null) }}
                  className="px-4 py-2.5 rounded-lg border border-brain-700/50 text-brain-200 hover:text-white text-sm">
                  {t('open_in_graph')}
                </button>
              )}
              <button disabled={busy} onClick={() => deletePerson(card)}
                className="px-4 py-2.5 rounded-lg border border-red-500/40 text-red-300 hover:bg-red-500/15 text-sm inline-flex items-center gap-1.5">
                <Trash2 className="w-4 h-4" /> {t('delete_person')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
