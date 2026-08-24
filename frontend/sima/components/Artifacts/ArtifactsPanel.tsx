'use client'

// Галерея артефактов (из дизайн-прототипа Sima Remix): блоки, подсхемы,
// карты, ТЗ и вики сохраняются как артефакты с тегами и переиспользуются
// между проектами — «вставить» кладёт готовый кубик на канвас.
import { useState, useEffect, useCallback, useMemo } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { cn } from '@/sima/lib/utils'
import { Package, Search, RefreshCw, Plus, Trash2, Copy, Eye } from 'lucide-react'
import { simaFetch } from '@/sima/lib/api'

interface Artifact {
  id: string
  artifactType: string
  title: string
  description?: string
  tags: string[]
  usedInCount: number
  createdAt?: string
  maturity?: 'draft' | 'verified' | 'canon'
}

// Зрелость артефакта (Sima Remix): черновик → проверен → эталон.
const MATURITY_ORDER: Array<'draft' | 'verified' | 'canon'> = ['draft', 'verified', 'canon']
const MATURITY_COLORS: Record<string, string> = {
  draft: 'bg-sima-bg text-sima-textDim',
  verified: 'bg-amber-400/15 text-amber-400',
  canon: 'bg-green-400/15 text-green-400',
}

const TYPE_FILTERS = ['all', 'block', 'subschema', 'map', 'tz', 'wiki'] as const

const TYPE_COLORS: Record<string, string> = {
  block: 'bg-indigo-400/15 text-indigo-400',
  subschema: 'bg-amber-400/15 text-amber-400',
  map: 'bg-pink-400/15 text-pink-400',
  tz: 'bg-green-400/15 text-green-400',
  wiki: 'bg-violet-400/15 text-violet-400',
}

export default function ArtifactsPanel() {
  const t = useTranslations('sima_artifacts')
  const project = useSimaStore(s => s.project)
  const setAllBlocks = useSimaStore(s => s.setAllBlocks)
  const setAllConnections = useSimaStore(s => s.setAllConnections)

  const [items, setItems] = useState<Artifact[]>([])
  const [typeFilter, setTypeFilter] = useState<string>('all')
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [msg, setMsg] = useState('')
  const [insertedMd, setInsertedMd] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ id: string; text: string } | null>(null)

  // превью содержимого перед вставкой (GET /artifacts/{id} с payload)
  async function togglePreview(a: Artifact) {
    if (preview?.id === a.id) { setPreview(null); return }
    setBusyId(a.id)
    try {
      const res = await simaFetch(`/artifacts/${a.id}`)
      const data = await res.json()
      if (data.success === false) { setMsg(data.error); return }
      const pl = data.artifact?.payload || {}
      let text = ''
      if (pl.markdown) {
        text = String(pl.markdown).slice(0, 1500)
      } else if (pl.block) {
        const b = pl.block
        const parts = [
          `${b.name || ''} (${b.type || 'feature'}, ${b.layer || 'mvp'})`,
          b.description, b.goal, b.businessValue,
          (pl.children?.length ? t('preview_nested', { children: pl.children.length, connections: pl.connections?.length || 0 }) : ''),
        ].filter(Boolean)
        text = parts.join('\n')
      } else {
        text = JSON.stringify(pl, null, 1).slice(0, 1000)
      }
      setPreview({ id: a.id, text })
    } catch (e) {
      setMsg((e as Error).message)
    } finally {
      setBusyId(null)
    }
  }

  async function cycleMaturity(a: Artifact) {
    const cur = a.maturity || 'draft'
    const next = MATURITY_ORDER[(MATURITY_ORDER.indexOf(cur) + 1) % MATURITY_ORDER.length]
    // Оптимистично обновляем список; при ошибке откатываем.
    setItems((prev) => prev.map((x) => x.id === a.id ? { ...x, maturity: next } : x))
    try {
      await simaFetch(`/artifacts/${a.id}/maturity`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ maturity: next }),
      })
    } catch {
      setItems((prev) => prev.map((x) => x.id === a.id ? { ...x, maturity: cur } : x))
    }
  }

  const load = useCallback(async () => {
    setLoading(true)
    try {
      // тип фильтруем на клиенте: чипам нужны счётчики по ВСЕМ типам,
      // правой колонке — статистика по всей галерее (эталон Sima Remix)
      const params = new URLSearchParams()
      if (query.trim()) params.set('q', query.trim())
      const res = await simaFetch(`/artifacts?${params.toString()}`)
      const data = await res.json()
      setItems(data.artifacts || [])
    } catch (e) {
      setMsg((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [query])

  useEffect(() => {       // дебаунс: не дёргаем API на каждую букву поиска
    const id = setTimeout(load, 300)
    return () => clearTimeout(id)
  }, [load])

  const typeCounts = useMemo(() => {
    const c: Record<string, number> = {}
    items.forEach(a => { c[a.artifactType] = (c[a.artifactType] || 0) + 1 })
    return c
  }, [items])
  const visibleItems = useMemo(
    () => typeFilter === 'all' ? items : items.filter(a => a.artifactType === typeFilter),
    [items, typeFilter])
  const stats = useMemo(() => ({
    total: items.length,
    reused: items.filter(a => (a.usedInCount || 0) >= 2).length,
    blocks: items.filter(a => a.artifactType === 'block').length,
  }), [items])

  async function refreshProject() {
    if (!project) return
    const res = await simaFetch(`/projects/${project.id}`)
    const data = await res.json()
    if (data.blocks) {
      setAllBlocks(data.blocks)
      setAllConnections(data.connections || [])
    }
  }

  async function insert(a: Artifact) {
    if (!project) return
    setBusyId(a.id)
    setMsg('')
    setInsertedMd(null)
    try {
      const res = await simaFetch(`/artifacts/${a.id}/insert`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, positionX: 120, positionY: 120 }),
      })
      const data = await res.json()
      if (data.success === false) {
        setMsg(data.error || 'error')
      } else if (data.markdown) {
        setInsertedMd(data.markdown)
        setMsg(t('inserted_doc', { title: a.title }))
      } else {
        await refreshProject()
        setMsg(t('inserted_blocks', { count: data.blocksCreated || 1 }))
      }
      load()
    } catch (e) {
      setMsg((e as Error).message)
    } finally {
      setBusyId(null)
    }
  }

  async function saveCurrentTZ() {
    if (!project) return
    const tz = (project as any).generatedTZ || (project as any).generated_tz
    if (!tz) {
      setMsg(t('no_tz'))
      return
    }
    setBusyId('save-tz')
    try {
      const res = await simaFetch('/artifacts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          artifactType: 'tz',
          title: t('tz_artifact_title', { name: project.name }),
          description: (project as any).description || '',
          tags: ['#tz'],
          payload: { markdown: tz },
          sourceProjectId: project.id,
        }),
      })
      const data = await res.json()
      setMsg(data.success === false ? data.error : t('saved_tz'))
      load()
    } finally {
      setBusyId(null)
    }
  }

  async function remove(a: Artifact) {
    setBusyId(a.id)
    try {
      await simaFetch(`/artifacts?id=${a.id}`, { method: 'DELETE' })
      load()
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="flex flex-col h-full p-4 gap-3 overflow-y-auto">
      <div className="flex items-center gap-2 flex-wrap">
        <Package className="w-4 h-4 text-sima-primary" />
        <span className="text-sm font-semibold text-sima-text">{t('title')}</span>
        <span className="text-[10px] text-sima-textDim">{t('subtitle')}</span>
        <button
          onClick={saveCurrentTZ}
          disabled={busyId !== null || !project}
          className="ml-auto flex items-center gap-1 px-2 py-1 rounded-lg border border-sima-border text-[11px] text-sima-textMuted hover:text-sima-text transition-colors"
        >
          <Plus className="w-3 h-3" />
          {t('save_tz_button')}
        </button>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        {TYPE_FILTERS.map(f => (
          <button
            key={f}
            onClick={() => setTypeFilter(f)}
            className={cn(
              'px-2 py-1 rounded-full text-[11px] border transition-colors',
              typeFilter === f
                ? 'border-sima-primary text-sima-primary bg-sima-primary/10'
                : 'border-sima-border text-sima-textDim hover:text-sima-textMuted'
            )}
          >
            {t(`filter_${f}`)}
            {f !== 'all' && typeCounts[f] ? <span className="ml-1 opacity-60">· {typeCounts[f]}</span> : null}
          </button>
        ))}
        <div className="relative ml-auto">
          <Search className="w-3.5 h-3.5 absolute left-2 top-1.5 text-sima-textDim" />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder={t('search_placeholder')}
            className="pl-7 pr-2 py-1 rounded-lg bg-sima-bg border border-sima-border text-[11px] text-sima-text w-52"
          />
        </div>
      </div>

      {msg && <p className="text-[11px] text-sima-textMuted">{msg}</p>}
      {insertedMd && (
        <div className="p-2 rounded-lg border border-sima-border bg-sima-bg">
          <button
            onClick={() => navigator.clipboard.writeText(insertedMd)}
            className="flex items-center gap-1 text-[10px] text-sima-textDim hover:text-sima-text mb-1"
          >
            <Copy className="w-3 h-3" /> {t('copy_doc')}
          </button>
          <pre className="text-[10px] text-sima-textMuted whitespace-pre-wrap max-h-40 overflow-y-auto">{insertedMd.slice(0, 2000)}</pre>
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-10">
          <RefreshCw className="w-5 h-5 animate-spin text-sima-textDim" />
        </div>
      ) : visibleItems.length === 0 ? (
        <div className="text-center py-10 text-sima-textDim text-sm">
          <Package className="w-10 h-10 mx-auto mb-3 opacity-30" />
          <p>{t('empty_hint')}</p>
        </div>
      ) : (
        <div className="flex gap-4 items-start">
        <div className="flex-1 grid grid-cols-2 lg:grid-cols-3 gap-2">
          {visibleItems.map(a => (
            <div key={a.id} className="p-3 rounded-xl border border-sima-border bg-sima-surfaceLight flex flex-col gap-1.5">
              <div className="flex items-center gap-2">
                <span className={cn('text-[9px] px-1.5 py-0.5 rounded uppercase font-semibold',
                  TYPE_COLORS[a.artifactType] || 'bg-sima-bg text-sima-textDim')}>
                  {t(`filter_${a.artifactType}` as any) || a.artifactType}
                </span>
                {/* Зрелость: клик циклит draft→verified→canon (Sima Remix) */}
                <button
                  onClick={(e) => { e.stopPropagation(); cycleMaturity(a) }}
                  title={t('maturity_cycle_hint')}
                  className={cn('text-[9px] px-1.5 py-0.5 rounded font-medium hover:opacity-80',
                    MATURITY_COLORS[a.maturity || 'draft'])}
                >
                  {t(`maturity_${a.maturity || 'draft'}`)}
                </button>
                {a.createdAt && (
                  <span className="text-[9px] text-sima-textDim ml-auto">
                    {String(a.createdAt).slice(0, 10)}
                  </span>
                )}
              </div>
              <p className="text-xs font-medium text-sima-text leading-tight">{a.title}</p>
              {a.description && (
                <p className="text-[10px] text-sima-textDim line-clamp-2">{a.description}</p>
              )}
              {a.tags?.length > 0 && (
                <div className="flex gap-1 flex-wrap">
                  {a.tags.slice(0, 4).map(tag => (
                    <span key={tag} className="text-[9px] px-1 py-0.5 rounded bg-sima-bg text-sima-textDim border border-sima-border">{tag}</span>
                  ))}
                </div>
              )}
              {preview?.id === a.id && (
                <pre className="text-[9px] text-sima-textMuted whitespace-pre-wrap max-h-28 overflow-y-auto p-1.5 rounded bg-sima-bg border border-sima-border">{preview.text}</pre>
              )}
              <div className="flex items-center gap-1 mt-auto pt-1">
                <span className="text-[9px] text-sima-textDim">{t('used_in', { count: a.usedInCount })}</span>
                <button
                  onClick={() => togglePreview(a)}
                  disabled={busyId !== null}
                  className="p-1 text-sima-textDim hover:text-sima-text transition-colors"
                  title={t('preview_title')}
                >
                  <Eye className="w-3 h-3" />
                </button>
                <button
                  onClick={() => remove(a)}
                  disabled={busyId !== null}
                  className="ml-auto p-1 text-sima-textDim hover:text-red-400 transition-colors"
                  title={t('delete_title')}
                >
                  <Trash2 className="w-3 h-3" />
                </button>
                <button
                  onClick={() => insert(a)}
                  disabled={busyId !== null || !project}
                  className="flex items-center gap-1 px-2 py-1 rounded-lg border border-sima-primary/40 bg-sima-primary/10 hover:bg-sima-primary/20 text-[10px] text-sima-primary transition-colors"
                >
                  {busyId === a.id ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
                  {t('insert_button')}
                </button>
              </div>
            </div>
          ))}
        </div>

        {/* Правая колонка: как работают артефакты + честная статистика */}
        <aside className="hidden xl:block w-72 shrink-0 space-y-4">
          <div className="rounded-xl border border-sima-border bg-sima-surfaceLight p-4">
            <div className="text-[10px] uppercase tracking-wide text-sima-textDim mb-2">
              {t('how_title')}
            </div>
            <p className="text-[11px] text-sima-textMuted leading-relaxed">
              {t('how_text')}
            </p>
          </div>
          <div className="rounded-xl border border-sima-border bg-sima-surfaceLight p-4 space-y-1.5">
            <div className="text-[10px] uppercase tracking-wide text-sima-textDim mb-1">
              {t('stats_title')}
            </div>
            <div className="flex justify-between text-[11px]">
              <span className="text-sima-textMuted">{t('stats_total')}</span>
              <span className="text-sima-text font-medium">{stats.total}</span>
            </div>
            <div className="flex justify-between text-[11px]">
              <span className="text-sima-textMuted">{t('stats_reused')}</span>
              <span className="text-sima-text font-medium">{stats.reused}</span>
            </div>
            <div className="flex justify-between text-[11px]">
              <span className="text-sima-textMuted">{t('stats_blocks')}</span>
              <span className="text-sima-text font-medium">{stats.blocks}</span>
            </div>
          </div>
        </aside>
        </div>
      )}
    </div>
  )
}
