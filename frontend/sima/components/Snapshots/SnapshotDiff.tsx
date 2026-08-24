'use client'

import { useState, useEffect } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { X, GitCompare, Plus, Minus, RefreshCw, History } from 'lucide-react'
import type { BlockData, ConnectionData } from '@/sima/types'
import { cn } from '@/sima/lib/utils'
import { simaFetch } from '@/sima/lib/api'

interface Snapshot {
  id: string
  label: string | null
  type: string
  blocksData: string
  connectionsData: string
  createdAt: string
}

interface DiffResult {
  addedBlocks: BlockData[]
  removedBlocks: BlockData[]
  modifiedBlocks: { before: BlockData; after: BlockData; changes: string[] }[]
  addedConnections: ConnectionData[]
  removedConnections: ConnectionData[]
}

interface Props {
  onClose: () => void
}

function computeDiff(
  blocksA: BlockData[],
  connsA: ConnectionData[],
  blocksB: BlockData[],
  connsB: ConnectionData[],
  t: (k: string, vars?: Record<string, string | number>) => string
): DiffResult {
  const mapA = new Map(blocksA.map(b => [b.id, b]))
  const mapB = new Map(blocksB.map(b => [b.id, b]))

  const addedBlocks = blocksB.filter(b => !mapA.has(b.id))
  const removedBlocks = blocksA.filter(b => !mapB.has(b.id))

  const modifiedBlocks: DiffResult['modifiedBlocks'] = []
  for (const [id, after] of mapB) {
    const before = mapA.get(id)
    if (!before) continue
    const changes: string[] = []
    if (before.name !== after.name) changes.push(t('name_changed', { before: before.name, after: after.name }))
    if (before.description !== after.description) changes.push(t('description_changed'))
    if (before.type !== after.type) changes.push(t('type_changed', { before: before.type, after: after.type }))
    if (before.layer !== after.layer) changes.push(t('layer_changed', { before: before.layer, after: after.layer }))
    if (before.businessValue !== after.businessValue) changes.push(t('business_value_changed'))
    if (before.goal !== after.goal) changes.push(t('goal_changed'))
    if (changes.length > 0) modifiedBlocks.push({ before, after, changes })
  }

  const connSetA = new Set(connsA.map(c => c.id))
  const connSetB = new Set(connsB.map(c => c.id))
  const addedConnections = connsB.filter(c => !connSetA.has(c.id))
  const removedConnections = connsA.filter(c => !connSetB.has(c.id))

  return { addedBlocks, removedBlocks, modifiedBlocks, addedConnections, removedConnections }
}

export default function SnapshotDiff({ onClose }: Props) {
  const t = useTranslations('sima_snapshot_diff')
  const locale = useLocale()
  const { project, allBlocks, allConnections } = useSimaStore()
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [selectedA, setSelectedA] = useState<string>('')
  const [selectedB, setSelectedB] = useState<string>('current')
  const [diff, setDiff] = useState<DiffResult | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!project) return
    simaFetch(`/snapshots?projectId=${project.id}`)
      .then(r => r.json())
      .then(data => {
        setSnapshots(data.snapshots || [])
        if (data.snapshots?.length > 0) setSelectedA(data.snapshots[0].id)
      })
      .catch(() => {})
  }, [project])

  async function compareSessions() {
    if (!selectedA) return
    setLoading(true)

    let blocksA: BlockData[] = []
    let connsA: ConnectionData[] = []
    let blocksB: BlockData[] = allBlocks
    let connsB: ConnectionData[] = allConnections

    // Parse snapshot A
    const snapA = snapshots.find(s => s.id === selectedA)
    if (snapA) {
      try {
        blocksA = JSON.parse(snapA.blocksData)
        connsA = JSON.parse(snapA.connectionsData)
      } catch { /* ok */ }
    }

    // Parse snapshot B (if not "current")
    if (selectedB !== 'current') {
      const snapB = snapshots.find(s => s.id === selectedB)
      if (snapB) {
        try {
          blocksB = JSON.parse(snapB.blocksData)
          connsB = JSON.parse(snapB.connectionsData)
        } catch { /* ok */ }
      }
    }

    setDiff(computeDiff(blocksA, connsA, blocksB, connsB, t))
    setLoading(false)
  }

  const totalChanges = diff
    ? diff.addedBlocks.length + diff.removedBlocks.length + diff.modifiedBlocks.length +
      diff.addedConnections.length + diff.removedConnections.length
    : 0

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-sima-border shrink-0">
        <div className="flex items-center gap-2">
          <GitCompare className="w-4 h-4 text-sima-primary" />
          <span className="text-sm font-semibold text-sima-text">{t('panel_title')}</span>
        </div>
        <button onClick={onClose} className="text-sima-textDim hover:text-sima-text">
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Selectors */}
      <div className="px-4 py-3 border-b border-sima-border space-y-2">
        <div className="grid grid-cols-[1fr_auto_1fr] gap-2 items-center">
          <div>
            <label className="text-[10px] text-sima-textDim uppercase">{t('version_a_label')}</label>
            <select
              value={selectedA}
              onChange={(e) => setSelectedA(e.target.value)}
              className="w-full bg-sima-bg border border-sima-border rounded-lg px-2 py-1.5 text-xs text-sima-text mt-0.5"
            >
              <option value="">{t("select_placeholder")}</option>
              {snapshots.map(s => (
                <option key={s.id} value={s.id}>
                  {s.label || (s.type === 'auto' ? t('type_auto') : t('type_manual'))} — {new Date(s.createdAt).toLocaleString(locale)}
                </option>
              ))}
            </select>
          </div>
          <div className="pt-4">
            <RefreshCw className="w-4 h-4 text-sima-textDim" />
          </div>
          <div>
            <label className="text-[10px] text-sima-textDim uppercase">{t('version_b_label')}</label>
            <select
              value={selectedB}
              onChange={(e) => setSelectedB(e.target.value)}
              className="w-full bg-sima-bg border border-sima-border rounded-lg px-2 py-1.5 text-xs text-sima-text mt-0.5"
            >
              <option value="current">{t("current_version")}</option>
              {snapshots.map(s => (
                <option key={s.id} value={s.id}>
                  {s.label || (s.type === 'auto' ? t('type_auto') : t('type_manual'))} — {new Date(s.createdAt).toLocaleString(locale)}
                </option>
              ))}
            </select>
          </div>
        </div>
        <button
          onClick={compareSessions}
          disabled={!selectedA || loading}
          className="w-full py-2 bg-sima-primary hover:bg-sima-primaryHover text-white rounded-lg text-xs font-medium disabled:opacity-50 transition-colors flex items-center justify-center gap-2"
        >
          <GitCompare className="w-3.5 h-3.5" />
          {t('compare_button')}
        </button>
      </div>

      {/* Results */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
        {!diff && snapshots.length === 0 && (
          <div className="text-center py-8">
            <History className="w-8 h-8 text-sima-textDim mx-auto mb-2 opacity-40" />
            <p className="text-xs text-sima-textDim">{t('no_snapshots')}</p>
            <p className="text-[10px] text-sima-textDim mt-1">{t('snapshots_hint')}</p>
          </div>
        )}

        {diff && (
          <>
            <div className="text-center text-xs text-sima-textDim">
              {t('found_changes', { count: totalChanges })}
            </div>

            {/* Added blocks */}
            {diff.addedBlocks.length > 0 && (
              <div>
                <h3 className="text-[10px] font-semibold text-green-400 uppercase tracking-wider mb-1.5 flex items-center gap-1">
                  <Plus className="w-3 h-3" />
                  {t('added_label', { count: diff.addedBlocks.length })}
                </h3>
                {diff.addedBlocks.map(b => (
                  <div key={b.id} className="px-3 py-2 bg-green-400/5 border border-green-400/20 rounded-lg mb-1">
                    <span className="text-xs text-green-400 font-medium">{b.label}: {b.name}</span>
                    <span className="text-[10px] text-sima-textDim ml-2">{b.type}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Removed blocks */}
            {diff.removedBlocks.length > 0 && (
              <div>
                <h3 className="text-[10px] font-semibold text-red-400 uppercase tracking-wider mb-1.5 flex items-center gap-1">
                  <Minus className="w-3 h-3" />
                  {t('removed_label', { count: diff.removedBlocks.length })}
                </h3>
                {diff.removedBlocks.map(b => (
                  <div key={b.id} className="px-3 py-2 bg-red-400/5 border border-red-400/20 rounded-lg mb-1">
                    <span className="text-xs text-red-400 font-medium">{b.label}: {b.name}</span>
                    <span className="text-[10px] text-sima-textDim ml-2">{b.type}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Modified blocks */}
            {diff.modifiedBlocks.length > 0 && (
              <div>
                <h3 className="text-[10px] font-semibold text-amber-400 uppercase tracking-wider mb-1.5 flex items-center gap-1">
                  <RefreshCw className="w-3 h-3" />
                  {t('modified_label', { count: diff.modifiedBlocks.length })}
                </h3>
                {diff.modifiedBlocks.map(m => (
                  <div key={m.after.id} className="px-3 py-2 bg-amber-400/5 border border-amber-400/20 rounded-lg mb-1">
                    <div className="text-xs text-amber-400 font-medium">{m.after.label}: {m.after.name}</div>
                    <div className="space-y-0.5 mt-1">
                      {m.changes.map((c, i) => (
                        <div key={i} className="text-[10px] text-sima-textMuted">{c}</div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Connection changes */}
            {(diff.addedConnections.length > 0 || diff.removedConnections.length > 0) && (
              <div>
                <h3 className="text-[10px] font-semibold text-sima-textDim uppercase tracking-wider mb-1.5">
                  {t('connections_label')}
                </h3>
                {diff.addedConnections.map(c => (
                  <div key={c.id} className="px-3 py-1.5 bg-green-400/5 border border-green-400/20 rounded-lg mb-1 text-xs text-green-400">
                    + {c.label || `${c.fromBlockId.slice(0,8)} → ${c.toBlockId.slice(0,8)}`}
                  </div>
                ))}
                {diff.removedConnections.map(c => (
                  <div key={c.id} className="px-3 py-1.5 bg-red-400/5 border border-red-400/20 rounded-lg mb-1 text-xs text-red-400">
                    − {c.label || `${c.fromBlockId.slice(0,8)} → ${c.toBlockId.slice(0,8)}`}
                  </div>
                ))}
              </div>
            )}

            {totalChanges === 0 && (
              <div className="text-center py-6">
                <p className="text-xs text-sima-textDim">{t('no_diffs')}</p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
