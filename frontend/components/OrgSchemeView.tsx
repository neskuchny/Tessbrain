'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'

/**
 * OrgSchemeView — структурная оргсхема (задача №1 бэклога).
 *
 * НЕ force-граф: компания сверху, отделы колонками (руководитель первым),
 * люди карточками внутри. Визуально в стиле графа (цвета/свечение по
 * отделам), но раскладка фиксированная — сразу видно «кто где и с кем».
 * Клик по человеку → карточка (Объект 360 / сотрудник).
 *
 * Источник структуры — снапшот КОМПАНИИ (cs.departments: head/members),
 * который пользователь правит вручную и который наполняет ночная
 * консолидация. Раньше схема строилась ТОЛЬКО по рёбрам графа
 * (MEMBER_OF/department-проп) — если их ещё нет, все падали в «Без отдела»
 * и вместо схемы показывались одни карточки. Теперь колонки берём из
 * departments, а людей из /org-graph раскладываем по ним по имени.
 *
 * Перетаскивание человека между колонками сохраняется как company-override
 * "departments" (тот же механизм, что в редакторе оргструктуры на вкладке
 * «Компания») — переживает ре-генерацию и влияет на категоризацию людей.
 */

const API_ORG = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const HUES = [250, 205, 150, 30, 332, 265, 45, 190, 110, 12]
const FREE_HUE = 218

interface OrgPerson {
  id: string
  name: string
  role: string
  isHead: boolean
  /** число связей в графе знаний (центральность) — «кто держит контекст» */
  links?: number
}

interface OrgUnit {
  id: string
  name: string
  hue: number
  description: string
  head: string
  people: OrgPerson[]
}

interface SnapDept {
  name: string
  head?: string
  description?: string
  members?: string[]
  employees_count?: number
}

// Толерантное сравнение имён (как _org_membership на бэке): по вхождению и
// по первому слову — «Саша» ↔ «Саша Иванов».
function nameMatch(a: string, b: string): boolean {
  const x = (a || '').trim().toLowerCase()
  const y = (b || '').trim().toLowerCase()
  if (!x || !y) return false
  if (x === y || x.includes(y) || y.includes(x)) return true
  return (x.split(/\s+/)[0] || x) === (y.split(/\s+/)[0] || y)
}

export default function OrgSchemeView({
  userId,
  onOpenEntity,
  onClose,
}: {
  userId?: string | null
  onOpenEntity?: (type: string, id: string, label: string) => void
  onClose?: () => void
}) {
  const t = useTranslations('org_scheme_view')
  const [units, setUnits] = useState<OrgUnit[]>([])
  const [unassigned, setUnassigned] = useState<OrgPerson[]>([])
  const [company, setCompany] = useState(t('company_fallback'))
  const [loading, setLoading] = useState(true)
  const [showAllFree, setShowAllFree] = useState(false)
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [dragId, setDragId] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null) // unit.id | '__free__'
  const [query, setQuery] = useState('')          // поиск сотрудника (dim мимо)
  const [hoverUnit, setHoverUnit] = useState<string | null>(null) // подсветка коннектора
  // структура отделов из снапшота (для сохранения override без потери
  // description/head пустых отделов)
  const deptsRef = useRef<SnapDept[]>([])
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!userId) return
    let alive = true
    ;(async () => {
      try {
        // Таймауты: снапшот может регенерироваться ~20с (TTL), а орг-граф на
        // большом аккаунте раньше выполнялся минуты (N+1 в бэке, уже починен).
        // Без таймаута любой из двух подвисших запросов держал «висит» вечно.
        const fetchJson = (url: string, ms: number) => {
          const ac = new AbortController()
          const timer = setTimeout(() => ac.abort(), ms)
          return authFetch(url, { signal: ac.signal })
            .then((r) => r.json())
            .catch(() => ({}))
            .finally(() => clearTimeout(timer))
        }
        const [orgRes, snapRes] = await Promise.all([
          fetchJson(`${API_ORG}/api/v1/entities/org-graph?user_id=${userId}&limit=400`, 60000),
          fetchJson(`${API_ORG}/api/v1/snapshots/enhanced/company?user_id=${userId}`, 60000),
        ])
        if (!alive) return

        const nodes: any[] = orgRes?.nodes || []
        const edges: any[] = orgRes?.edges || []
        const snap = snapRes?.snapshot || {}
        const snapDepts: SnapDept[] = Array.isArray(snap.departments) ? snap.departments : []
        deptsRef.current = snapDepts.map((d) => ({
          name: d.name || t('department_fallback'),
          head: d.head || '',
          description: d.description || '',
          members: Array.isArray(d.members) ? d.members : [],
          employees_count: d.employees_count || 0,
        }))
        setCompany(snap.name || orgRes?.company_name || t('company_fallback'))

        // Люди из графа (id/role/вовлечённость для богатых карточек)
        const people = nodes.filter((n) => /person/i.test(n.group || ''))
        const reportsCount: Record<string, number> = {}
        const memberOf: Record<string, string> = {}
        const unitNodeName: Record<string, string> = {}
        nodes.forEach((n) => {
          if (/team|department/i.test(n.group || '')) {
            unitNodeName[n.id] = (n.label || '').trim().toLowerCase()
          }
        })
        const degree: Record<string, number> = {}
        edges.forEach((e) => {
          const t = (e.label || '').toUpperCase()
          if (t === 'MEMBER_OF') memberOf[e.from] = e.to
          if (t === 'REPORTS_TO') reportsCount[e.to] = (reportsCount[e.to] || 0) + 1
          degree[e.from] = (degree[e.from] || 0) + 1
          degree[e.to] = (degree[e.to] || 0) + 1
        })

        const mgmtRe = /руковод|директор|ceo|founder|основател|управляющ|head|партн[её]р/i
        const graphPeople: (OrgPerson & { deptHint: string })[] = people.map((p) => ({
          id: p.id,
          name: p.label || t('employee_fallback'),
          role: p.properties?.role || '',
          links: degree[p.id] || 0,
          isHead: (reportsCount[p.id] || 0) >= 2 || mgmtRe.test(p.properties?.role || ''),
          // подсказка отдела: имя unit-узла по MEMBER_OF или проп department
          deptHint:
            (memberOf[p.id] && unitNodeName[memberOf[p.id]]) ||
            (p.properties?.department || '').trim().toLowerCase() ||
            '',
        }))

        let result: OrgUnit[] = []
        const taken = new Set<string>()

        if (snapDepts.length > 0) {
          // Структура из снапшота: колонки = отделы, люди раскладываются по
          // members/head (по имени) + подсказке отдела из графа.
          result = deptsRef.current.map((d, i) => {
            const headNm = d.head || ''
            const memberNames = d.members || []
            const dpeople: OrgPerson[] = []
            const seen = new Set<string>()

            const attach = (gp: (typeof graphPeople)[number], head: boolean) => {
              if (taken.has(gp.id)) return
              taken.add(gp.id)
              seen.add(gp.name.toLowerCase())
              dpeople.push({ ...gp, isHead: head || gp.isHead })
            }
            // руководитель
            if (headNm) {
              const g = graphPeople.find((gp) => !taken.has(gp.id) && nameMatch(gp.name, headNm))
              if (g) attach(g, true)
              else if (!seen.has(headNm.toLowerCase())) {
                dpeople.push({ id: `dept:${d.name}:head`, name: headNm, role: t('head_role'), isHead: true })
                seen.add(headNm.toLowerCase())
              }
            }
            // участники
            memberNames.forEach((m) => {
              if (!m || seen.has(m.toLowerCase())) return
              const g = graphPeople.find((gp) => !taken.has(gp.id) && nameMatch(gp.name, m))
              if (g) attach(g, false)
              else dpeople.push({ id: `dept:${d.name}:${m}`, name: m, role: '', isHead: false })
            })
            // люди с подсказкой этого отдела из графа (консолидация проставила)
            const dl = d.name.trim().toLowerCase()
            graphPeople.forEach((gp) => {
              if (!taken.has(gp.id) && gp.deptHint && nameMatch(gp.deptHint, dl)) attach(gp, false)
            })

            return {
              id: `dept:${d.name}`,
              name: d.name,
              hue: HUES[i % HUES.length],
              description: d.description || '',
              head: headNm,
              people: dpeople.sort((a, b) => Number(b.isHead) - Number(a.isHead)),
            }
          })
        } else {
          // Fallback: старая логика по рёбрам графа (нет снапшот-структуры).
          const unitNodes = nodes.filter((n) => /team|department/i.test(n.group || ''))
          const unitByName: Record<string, string> = {}
          unitNodes.forEach((u) => {
            const nm = (u.label || '').trim().toLowerCase()
            if (nm) unitByName[nm] = u.id
          })
          const byUnit: Record<string, OrgPerson[]> = {}
          graphPeople.forEach((p) => {
            const uid = memberOf[p.id] || (p.deptHint && unitByName[p.deptHint]) || ''
            if (uid) {
              taken.add(p.id)
              ;(byUnit[uid] = byUnit[uid] || []).push(p)
            }
          })
          let hi = 0
          result = unitNodes
            .filter((u) => (byUnit[u.id] || []).length > 0)
            .map((u) => ({
              id: u.id,
              name: u.label || t('department_fallback'),
              hue: HUES[hi++ % HUES.length],
              description: '',
              head: '',
              people: (byUnit[u.id] || []).sort((a, b) => Number(b.isHead) - Number(a.isHead)),
            }))
            .sort((a, b) => b.people.length - a.people.length)
        }

        // Все, кого никуда не разложили → «Без отдела»
        const free = graphPeople
          .filter((gp) => !taken.has(gp.id))
          .map(({ deptHint, ...p }) => p)
          .sort((a, b) => Number(b.isHead) - Number(a.isHead))

        setUnits(result)
        setUnassigned(free)
      } catch {
        /* best-effort */
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => {
      alive = false
      if (savedTimer.current) clearTimeout(savedTimer.current)
    }
  }, [userId])

  // Сохранить текущую раскладку как company-override "departments"
  // (переживает ре-генерацию, влияет на категоризацию — «изменения системы»).
  const persistDepartments = async (nextUnits: OrgUnit[]) => {
    if (!userId) return
    // Строим payload по ВСЕМ известным отделам (в т.ч. опустевшим — чтобы
    // не потерять их из структуры), накладывая актуальный состав из nextUnits.
    const byName: Record<string, OrgUnit> = {}
    nextUnits.forEach((u) => (byName[u.name.trim().toLowerCase()] = u))
    const known = deptsRef.current.length
      ? deptsRef.current.map((d) => d.name)
      : nextUnits.map((u) => u.name)
    const payload = known.map((nm) => {
      const u = byName[nm.trim().toLowerCase()]
      const meta = deptsRef.current.find((d) => d.name.trim().toLowerCase() === nm.trim().toLowerCase())
      const members = u ? u.people.map((p) => p.name) : meta?.members || []
      return {
        name: nm,
        description: (u?.description || meta?.description || ''),
        head: u?.head || meta?.head || '',
        members,
        employees_count: members.length,
      }
    })
    deptsRef.current = payload
    setSaveState('saving')
    try {
      const res = await fetch(
        `${API_ORG}/api/v1/snapshots/enhanced/company/override?user_id=${userId}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ field: 'departments', value: payload }),
        }
      )
      const data = await res.json().catch(() => ({}))
      if (!res.ok || data?.status === 'error') throw new Error(data?.message || 'save failed')
      setSaveState('saved')
      if (savedTimer.current) clearTimeout(savedTimer.current)
      savedTimer.current = setTimeout(() => setSaveState('idle'), 2200)
    } catch {
      setSaveState('error')
    }
  }

  // Перенос человека в отдел (targetUnitId) или в «Без отдела» ('__free__').
  const movePerson = (personId: string, targetUnitId: string) => {
    // найти человека в текущей раскладке
    let moved: OrgPerson | null = null
    const strippedUnits = units.map((u) => {
      const keep = u.people.filter((p) => {
        if (p.id === personId) {
          moved = p
          return false
        }
        return true
      })
      return { ...u, people: keep }
    })
    let freeList = unassigned
    if (!moved) {
      const f = unassigned.find((p) => p.id === personId)
      if (f) {
        moved = f
        freeList = unassigned.filter((p) => p.id !== personId)
      }
    }
    if (!moved) return
    const person: OrgPerson = moved

    if (targetUnitId === '__free__') {
      const nextUnits = strippedUnits
      const nextFree = [{ ...person, isHead: false }, ...freeList]
      setUnits(nextUnits)
      setUnassigned(nextFree)
      persistDepartments(nextUnits)
      return
    }
    // не сбрасываем: если целевой отдел тот же, ничего не делаем
    const target = strippedUnits.find((u) => u.id === targetUnitId)
    if (!target) return
    const already = target.people.some((p) => p.id === person.id)
    const nextUnits = strippedUnits.map((u) =>
      u.id === targetUnitId && !already
        ? { ...u, people: [...u.people, { ...person, isHead: nameMatch(person.name, u.head) }] }
        : u
    )
    setUnits(nextUnits)
    setUnassigned(freeList)
    persistDepartments(nextUnits)
  }

  const onDrop = (targetId: string) => {
    if (dragId) movePerson(dragId, targetId)
    setDragId(null)
    setDropTarget(null)
  }

  const matchesQuery = (p: OrgPerson) => {
    const v = query.trim().toLowerCase()
    if (!v) return true
    return p.name.toLowerCase().includes(v) || (p.role || '').toLowerCase().includes(v)
  }

  const personCard = (p: OrgPerson, hue: number) => {
    const hit = matchesQuery(p)
    return (
    <div
      key={p.id}
      draggable={!!userId}
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = 'move'
        setDragId(p.id)
      }}
      onDragEnd={() => {
        setDragId(null)
        setDropTarget(null)
      }}
      role="button"
      tabIndex={0}
      onClick={() => onOpenEntity?.('person', p.id, p.name)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onOpenEntity?.('person', p.id, p.name)
      }}
      className={`w-full text-left rounded-lg px-2.5 py-1.5 transition-all hover:translate-x-0.5 hover:scale-[1.02] active:scale-[0.99] ${
        userId ? 'cursor-grab active:cursor-grabbing' : 'cursor-pointer'
      } ${dragId === p.id ? 'opacity-40' : ''} ${!hit ? 'opacity-20 saturate-50' : ''}`}
      style={{
        background: `linear-gradient(180deg, hsla(${hue}, 45%, 55%, ${p.isHead ? 0.24 : 0.11}), hsla(${hue}, 45%, 45%, ${p.isHead ? 0.14 : 0.05}))`,
        border: `1px solid hsla(${hue}, 55%, 65%, ${p.isHead ? 0.55 : 0.25})`,
        boxShadow: p.isHead ? `0 0 14px hsla(${hue}, 60%, 60%, 0.28)` : undefined,
      }}
      title={userId ? t('drag_hint_title') : p.role || p.name}
    >
      <div className="flex items-center gap-2">
        <span
          className="flex-none w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-semibold text-white/90"
          style={{
            background: `linear-gradient(135deg, hsl(${hue}, 55%, ${p.isHead ? 52 : 42}%), hsl(${hue}, 55%, ${p.isHead ? 30 : 22}%))`,
            boxShadow: `inset 0 1px 0 hsla(${hue},70%,80%,.35)`,
          }}
        >
          {(p.name || '?').trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join('').toUpperCase()}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1">
            {p.isHead && <span className="text-[11px]">👑</span>}
            <span className="text-white text-[12.5px] font-medium truncate">{p.name}</span>
          </span>
          {p.role && <span className="block text-slate-400 text-[10.5px] truncate">{p.role}</span>}
        </span>
        {(p.links ?? 0) > 0 && (
          <span
            className="flex-none text-[9.5px] font-mono px-1 py-0.5 rounded"
            title={t('links_in_graph', { n: p.links ?? 0 })}
            style={{ color: `hsl(${hue}, 70%, 78%)`, background: `hsla(${hue}, 55%, 55%, 0.16)` }}
          >
            ◈{p.links}
          </span>
        )}
      </div>
    </div>
    )
  }

  const total = units.reduce((s, u) => s + u.people.length, 0) + unassigned.length
  const saveLabel = useMemo(
    () =>
      ({
        saving: `⏳ ${t('save_saving')}`,
        saved: `✓ ${t('save_saved')}`,
        error: `⚠ ${t('save_error')}`,
        idle: '',
      }[saveState]),
    [saveState, t]
  )

  return (
    <div className="h-full flex flex-col bg-gradient-to-b from-brain-950 via-[#0c1226] to-brain-950 overflow-hidden">
      {/* Компания — шапка */}
      <div className="flex items-center gap-3 px-5 py-3 border-b border-brain-700/30 bg-brain-900/40 backdrop-blur-sm">
        <span className="text-xl">🏢</span>
        <div className="flex-1 min-w-0">
          <div className="text-white font-semibold text-sm truncate flex items-center gap-2">
            {company}
            {/* ДИАГНОСТ-МЕТКА СБОРКИ: если ты её видишь — фронт обновился до
                свежего кода. Нет метки → браузер/сборка отдаёт старое (см.
                инструкцию: git reset --hard + rm .next + Ctrl+Shift+R). */}
            <span
              title={t('build_badge_title')}
              className="text-[9px] font-mono font-normal text-emerald-300 bg-emerald-500/10 border border-emerald-500/25 rounded px-1.5 py-0.5"
            >
              build ✓ v2
            </span>
          </div>
          <div className="text-slate-500 text-[11px]">
            {t('header_stats', { units: units.length, total })}
            {unassigned.length > 0 && ` · ${t('unassigned_count', { n: unassigned.length })}`}
          </div>
        </div>
        <div className="flex items-center gap-1.5 bg-brain-900/60 border border-brain-600/40 rounded-lg px-2.5 py-1">
          <span className="text-[11px] text-slate-500">🔎</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('search_placeholder')}
            className="bg-transparent outline-none text-[12px] text-white placeholder:text-slate-600 w-36"
          />
          {query && (
            <button onClick={() => setQuery('')} className="text-slate-500 hover:text-white text-[11px]">✕</button>
          )}
        </div>
        {saveLabel && (
          <span
            className={`text-[11px] px-2 py-0.5 rounded-md ${
              saveState === 'error'
                ? 'text-red-300 bg-red-500/10'
                : saveState === 'saved'
                ? 'text-emerald-300 bg-emerald-500/10'
                : 'text-brain-200 bg-brain-700/30'
            }`}
          >
            {saveLabel}
          </span>
        )}
        {onClose && (
          <button onClick={onClose} className="text-[11px] text-brain-300 hover:text-white border border-brain-600/30 hover:border-brain-500/50 rounded-md px-2.5 py-1 transition-colors">
            ✕ {t('close_scheme')}
          </button>
        )}
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center text-slate-400">
          <div className="w-7 h-7 border-2 border-brain-500/30 border-t-brain-400 rounded-full animate-spin" />
        </div>
      ) : units.length === 0 && unassigned.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-center px-6">
          <div className="text-3xl mb-2 opacity-60">🏗️</div>
          <div className="text-slate-300 text-sm max-w-sm">
            {t('empty_state')}
          </div>
        </div>
      ) : (
        <div className="flex-1 overflow-auto p-4">
          {userId && (
            <div className="text-slate-500 text-[11px] mb-2.5">
              {t('drag_hint')}
            </div>
          )}
          {/* Кривые коннекторы компания → отделы: градиент + свечение + «ток»
              (из дизайн-концепта). Hover отдела подсвечивает его ветку,
              остальные мягко гаснут. Геометрия: w-52=208px + gap-3=12px. */}
          {units.length > 0 && (() => {
            const COL = 208, GAP = 12, H = 34
            const W = units.length * (COL + GAP) - GAP
            const rootX = W / 2
            return (
              <div className="relative min-w-max mb-0.5" aria-hidden>
                <svg width={W} height={H} className="block" style={{ overflow: 'visible' }}>
                  {units.map((u, i) => {
                    const cx = i * (COL + GAP) + COL / 2
                    const on = hoverUnit === null || hoverUnit === u.id
                    const hot = hoverUnit === u.id
                    const d = `M ${rootX} 0 C ${rootX} ${H * 0.55}, ${cx} ${H * 0.45}, ${cx} ${H}`
                    return (
                      <g key={u.id}>
                        {/* свечение-подложка */}
                        <path d={d} fill="none" stroke={`hsla(${u.hue},70%,60%,${hot ? 0.35 : 0.10})`}
                              strokeWidth={hot ? 6 : 4} strokeLinecap="round" />
                        {/* основная ветка */}
                        <path d={d} fill="none" stroke={`hsla(${u.hue},60%,62%,${on ? (hot ? 0.95 : 0.55) : 0.12})`}
                              strokeWidth={hot ? 2.2 : 1.4} strokeLinecap="round"
                              style={{ transition: 'stroke .25s, stroke-width .25s' }} />
                        {/* «ток» по ветке */}
                        <path d={d} fill="none" stroke={`hsla(${u.hue},85%,78%,${on ? 0.9 : 0})`}
                              strokeWidth={hot ? 2.2 : 1.4} strokeLinecap="round"
                              strokeDasharray="3 15" className="org-flow" />
                      </g>
                    )
                  })}
                  {/* узел-компания на стволе */}
                  <circle cx={rootX} cy={2} r={3.5} fill="#8fb4ff" opacity={0.9}>
                    <animate attributeName="opacity" values="0.9;0.5;0.9" dur="2.4s" repeatCount="indefinite" />
                  </circle>
                </svg>
                <style>{`.org-flow{ animation: orgflow 1.4s linear infinite; } @keyframes orgflow{ to{ stroke-dashoffset:-18; } }
                  @media (prefers-reduced-motion: reduce){ .org-flow{ animation:none; opacity:0 !important; } }`}</style>
              </div>
            )
          })()}
          {/* Отделы — колонками (drop-цели) */}
          <div className="flex gap-3 items-start min-w-max pb-3">
            {units.map((u) => {
              const active = dropTarget === u.id
              return (
                <div
                  key={u.id}
                  onMouseEnter={() => setHoverUnit(u.id)}
                  onMouseLeave={() => setHoverUnit((h) => (h === u.id ? null : h))}
                  onDragOver={(e) => {
                    if (dragId) {
                      e.preventDefault()
                      if (dropTarget !== u.id) setDropTarget(u.id)
                    }
                  }}
                  onDragLeave={() => setDropTarget((t) => (t === u.id ? null : t))}
                  onDrop={(e) => {
                    e.preventDefault()
                    onDrop(u.id)
                  }}
                  className="w-52 flex-none rounded-xl overflow-hidden transition-all"
                  style={{
                    background: `linear-gradient(180deg, hsla(${u.hue}, 40%, 45%, 0.14), hsla(${u.hue}, 40%, 30%, 0.05))`,
                    border: `1px solid hsla(${u.hue}, 50%, 60%, ${active ? 0.85 : hoverUnit === u.id ? 0.55 : 0.3})`,
                    boxShadow: active
                      ? `0 0 0 2px hsla(${u.hue}, 70%, 60%, 0.5), 0 6px 24px hsla(${u.hue}, 55%, 45%, 0.2)`
                      : hoverUnit === u.id
                      ? `0 8px 30px hsla(${u.hue}, 55%, 45%, 0.22)`
                      : `0 6px 24px hsla(${u.hue}, 55%, 45%, 0.12)`,
                    transform: hoverUnit === u.id ? 'translateY(-1px)' : undefined,
                  }}
                >
                  {/* соединительная «линия» к компании */}
                  <div className="h-1" style={{ background: `linear-gradient(90deg, transparent, hsla(${u.hue}, 60%, 60%, 0.8), transparent)` }} />
                  <div className="px-3 py-2 flex items-center justify-between gap-2">
                    <span className="min-w-0">
                      <span className="block text-white text-[12.5px] font-semibold truncate" title={u.description || u.name}>
                        {u.name}
                      </span>
                      {u.description && (
                        <span className="block text-slate-500 text-[9.5px] truncate">{u.description}</span>
                      )}
                    </span>
                    <span className="flex-none flex items-center gap-1">
                      {(() => {
                        const avg = u.people.length
                          ? Math.round(u.people.reduce((s, p) => s + (p.links || 0), 0) / u.people.length)
                          : 0
                        return avg > 0 ? (
                          <span className="text-[9.5px] font-mono px-1 py-0.5 rounded"
                                title={t('avg_links', { n: avg })}
                                style={{ color: `hsl(${u.hue}, 70%, 78%)`, background: `hsla(${u.hue}, 50%, 55%, 0.14)` }}>
                            ◈{avg}
                          </span>
                        ) : null
                      })()}
                      <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-full"
                            style={{ background: `hsla(${u.hue}, 50%, 55%, 0.25)`, color: `hsl(${u.hue}, 70%, 80%)` }}>
                        {u.people.length}
                      </span>
                    </span>
                  </div>
                  <div className="px-2 pb-2 space-y-1.5 min-h-[36px]">
                    {u.people.length === 0 ? (
                      <div className="text-slate-500 text-[10.5px] italic px-1 py-2 text-center">
                        {active ? t('drop_here') : t('empty')}
                      </div>
                    ) : (
                      [...u.people].sort((a, b) => Number(b.isHead) - Number(a.isHead)).map((p) => personCard(p, u.hue))
                    )}
                  </div>
                </div>
              )
            })}
          </div>

          {/* Без отдела — отдельным рядом снизу (тоже drop-цель) */}
          {(unassigned.length > 0 || dragId) && (
            <div
              className="mt-2 rounded-lg transition-all p-1"
              onDragOver={(e) => {
                if (dragId) {
                  e.preventDefault()
                  if (dropTarget !== '__free__') setDropTarget('__free__')
                }
              }}
              onDragLeave={() => setDropTarget((t) => (t === '__free__' ? null : t))}
              onDrop={(e) => {
                e.preventDefault()
                onDrop('__free__')
              }}
              style={{
                outline: dropTarget === '__free__' ? `2px solid hsla(${FREE_HUE}, 70%, 60%, 0.6)` : 'none',
              }}
            >
              <div className="text-slate-500 text-[11px] uppercase tracking-wider mb-1.5">
                {t('no_department')} · {unassigned.length}
                {dragId && dropTarget === '__free__' && ` — ${t('drop_to_remove')}`}
              </div>
              {unassigned.length === 0 ? (
                <div className="text-slate-600 text-[10.5px] italic px-1 py-3">
                  {t('drag_here_to_remove')}
                </div>
              ) : (
                <div className="grid gap-1.5" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))' }}>
                  {(showAllFree ? unassigned : unassigned.slice(0, 18)).map((p) => personCard(p, FREE_HUE))}
                </div>
              )}
              {unassigned.length > 18 && (
                <button onClick={() => setShowAllFree(!showAllFree)}
                        className="mt-2 text-[11px] text-brain-300 hover:text-white">
                  {showAllFree ? t('collapse') : t('show_more', { n: unassigned.length - 18 })}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
