'use client'

/**
 * IdentityLinkCard — сшивка «это я» (CogniLayer Ф0, UI).
 *
 * Показывает статус связи аккаунта с Person-сущностью графа и даёт выбрать
 * себя из кандидатов. Источник истины — явное подтверждение пользователя
 * (никакой авто-сшивки). Живёт компактным блоком в меню аккаунта.
 */
import { useState, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { UserCheck, Loader2, X } from 'lucide-react'

interface Candidate { person_id: string; name: string; score: number; confidence: string }

export default function IdentityLinkCard() {
  const t = useTranslations('identity')
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [linkedName, setLinkedName] = useState<string | null>(null)
  const [candidates, setCandidates] = useState<Candidate[] | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState<string | null>(null)

  const loadCandidates = useCallback(async () => {
    setLoading(true); setNote('')
    try {
      const r = await authFetch('/api/v1/identity/candidates')
      const d = await r.json()
      const cands: Candidate[] = Array.isArray(d.candidates) ? d.candidates : []
      setCandidates(cands)
      if (d.already_linked) {
        const self = cands.find((c) => c.person_id === d.already_linked)
        setLinkedName(self?.name || t('linked_generic'))
      } else {
        setLinkedName(null)
      }
      if (d.note) setNote(d.note)
      else if (cands.length === 0) setNote(t('no_candidates'))
    } catch {
      setNote(t('load_failed'))
    } finally {
      setLoading(false)
    }
  }, [t])

  const toggle = useCallback(() => {
    const next = !open
    setOpen(next)
    if (next && candidates === null) loadCandidates()
  }, [open, candidates, loadCandidates])

  const link = useCallback(async (c: Candidate) => {
    setBusy(c.person_id)
    try {
      const r = await authFetch('/api/v1/identity/link', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ person_entity_id: c.person_id }),
      })
      const d = await r.json()
      if (d.ok) { setLinkedName(c.name); setNote(t('linked_ok', { name: c.name })) }
      else setNote(d.error || t('link_failed'))
    } catch {
      setNote(t('link_failed'))
    } finally {
      setBusy(null)
    }
  }, [t])

  const unlink = useCallback(async () => {
    setBusy('unlink')
    try {
      await authFetch('/api/v1/identity/unlink', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      })
      setLinkedName(null); setNote(t('unlinked'))
    } catch { /* no-op */ } finally { setBusy(null) }
  }, [t])

  return (
    <div className="px-3 py-2 border-b border-brain-700/50">
      <button
        onClick={toggle}
        className="w-full flex items-center gap-2 text-xs text-brain-300 hover:text-white transition-colors"
      >
        <UserCheck className="w-3.5 h-3.5 text-brain-400" />
        <span className="flex-1 text-left">
          {linkedName ? t('linked_as', { name: linkedName }) : t('who_are_you')}
        </span>
        <span className="text-brain-500">{open ? '▴' : '▾'}</span>
      </button>

      {open && (
        <div className="mt-2 space-y-1">
          {loading && (
            <div className="flex items-center gap-2 text-[11px] text-brain-400">
              <Loader2 className="w-3 h-3 animate-spin" /> {t('loading')}
            </div>
          )}
          {!loading && (candidates || []).map((c) => {
            const isLinked = linkedName === c.name
            return (
              <div key={c.person_id} className="flex items-center gap-2">
                <button
                  onClick={() => link(c)}
                  disabled={busy === c.person_id || isLinked}
                  className={
                    'flex-1 flex items-center gap-2 px-2 py-1.5 rounded-lg text-[11px] transition-colors ' +
                    (isLinked
                      ? 'bg-green-500/10 text-green-400 border border-green-500/30'
                      : 'text-brain-200 hover:bg-brain-800/60 border border-brain-700/40')
                  }
                >
                  {busy === c.person_id ? <Loader2 className="w-3 h-3 animate-spin" /> : <UserCheck className="w-3 h-3" />}
                  <span className="flex-1 text-left truncate">{c.name}</span>
                  <span className={
                    'text-[9px] uppercase px-1 rounded ' +
                    (c.confidence === 'high' ? 'text-green-400' : c.confidence === 'medium' ? 'text-amber-400' : 'text-brain-500')
                  }>
                    {t(`conf_${c.confidence}`)}
                  </span>
                </button>
                {isLinked && (
                  <button onClick={unlink} title={t('unlink')} className="p-1 text-brain-500 hover:text-red-400">
                    <X className="w-3 h-3" />
                  </button>
                )}
              </div>
            )
          })}
          {note && <p className="text-[10px] text-brain-500 mt-1">{note}</p>}
        </div>
      )}
    </div>
  )
}
