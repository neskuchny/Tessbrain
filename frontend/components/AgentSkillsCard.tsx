'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch, getAccessToken } from '@/lib/authFetch'
import { RefreshCw, Sparkles, Users } from 'lucide-react'

// Навыки процедурной памяти агента: личные (агент выучивает их из успешно
// решённых задач) и организационные (одобренный обмен между сотрудниками).
// Backend: /api/v1/agent-skills/*.

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

type PersonalSkill = { name: string; description: string; category: string; version: string }
type OrgSkill = PersonalSkill & { shared_by?: string; approved_by?: string }
type PendingSkill = { name: string; description: string; category: string; shared_by?: string }

const btnCls = 'px-3 py-1 rounded-md bg-brain-700 hover:bg-brain-600 text-white text-xs disabled:opacity-50 inline-flex items-center gap-1'
const preCls = 'text-[11px] text-brain-300 bg-brain-950/60 border border-brain-800 rounded p-2 mt-1 mb-1 max-h-64 overflow-auto whitespace-pre-wrap'

export default function AgentSkillsCard({ userId }: { userId?: string | null }) {
  const t = useTranslations('agent_skills_card')
  const [personal, setPersonal] = useState<PersonalSkill[]>([])
  const [org, setOrg] = useState<OrgSkill[]>([])
  const [pending, setPending] = useState<PendingSkill[]>([])
  const [canApprove, setCanApprove] = useState(false)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [openKey, setOpenKey] = useState('')
  const [content, setContent] = useState('')

  const load = useCallback(async () => {
    if (!userId || !getAccessToken()) return
    try {
      const r = await authFetch(`${API}/api/v1/agent-skills`)
      const d = await r.json().catch(() => ({}))
      setPersonal(d.personal || [])
      setOrg(d.org || [])
      setPending(d.pending || [])
      setCanApprove(Boolean(d.can_approve))
    } catch { /* необязательно */ }
  }, [userId])

  useEffect(() => { load() }, [load])

  const view = useCallback(async (name: string, scope: 'personal' | 'org') => {
    const key = `${scope}:${name}`
    if (openKey === key) { setOpenKey(''); setContent(''); return }
    setOpenKey(key); setContent('')
    try {
      const r = await authFetch(`${API}/api/v1/agent-skills/view?name=${encodeURIComponent(name)}&scope=${scope}`)
      const d = await r.json().catch(() => ({}))
      setContent(d.status === 'success' ? (d.content || '') : (d.message || t('note_failed')))
    } catch (e: any) { setContent(e?.message || t('note_network')) }
  }, [openKey, t])

  const propose = useCallback(async (name: string) => {
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/agent-skills/propose`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      const d = await r.json().catch(() => ({}))
      setNote(d.status === 'success' ? t('note_proposed', { name }) : (d.message || t('note_failed')))
      load()
    } catch (e: any) { setNote(e?.message || t('note_network')) }
    setBusy(false)
  }, [load, t])

  const decide = useCallback(async (action: 'approve' | 'reject', category: string, name: string) => {
    setBusy(true); setNote('')
    try {
      const r = await authFetch(`${API}/api/v1/agent-skills/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category, name }),
      })
      const d = await r.json().catch(() => ({}))
      setNote(d.status === 'success'
        ? t(action === 'approve' ? 'note_approved' : 'note_rejected', { name })
        : (d.message || t('note_failed')))
      load()
    } catch (e: any) { setNote(e?.message || t('note_network')) }
    setBusy(false)
  }, [load, t])

  return (
    <div className="bg-brain-900/60 border border-brain-700 rounded-lg p-4 mt-4 text-brain-100">
      <div className="flex items-center gap-2 mb-2">
        <Sparkles size={16} className="text-brain-400" />
        <span className="text-sm font-medium">{t('title')}</span>
        <button className={btnCls} onClick={load} disabled={busy}><RefreshCw size={12} /></button>
      </div>
      <div className="text-xs text-brain-400 mb-3">{t('intro')}</div>

      {/* Личные навыки */}
      <div className="text-xs text-brain-300 font-medium mb-1">{t('personal_heading')}</div>
      {personal.length === 0 ? (
        <div className="text-xs text-brain-500 mb-3">{t('empty_personal')}</div>
      ) : (
        <div className="flex flex-col gap-1 mb-3">
          {personal.map((s) => (
            <div key={`${s.category}:${s.name}`} className="border-t border-brain-800 pt-1">
              <div className="flex items-center gap-2 text-xs flex-wrap">
                <span className="text-brain-100">{s.name}</span>
                {s.version && <span className="text-brain-600">v{s.version}</span>}
                <span className="text-brain-500">{s.description}</span>
                <div className="ml-auto flex items-center gap-2">
                  <button className={btnCls} onClick={() => view(s.name, 'personal')}>
                    {openKey === `personal:${s.name}` ? t('btn_close') : t('btn_open')}
                  </button>
                  <button className={btnCls} onClick={() => propose(s.name)} disabled={busy}>
                    {t('btn_propose')}
                  </button>
                </div>
              </div>
              {openKey === `personal:${s.name}` && (
                <pre className={preCls}>{content || t('loading_content')}</pre>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Организационные навыки */}
      <div className="text-xs text-brain-300 font-medium mb-1">{t('org_heading')}</div>
      {org.length === 0 ? (
        <div className="text-xs text-brain-500 mb-3">{t('empty_org')}</div>
      ) : (
        <div className="flex flex-col gap-1 mb-3">
          {org.map((s) => (
            <div key={`${s.category}:${s.name}`} className="border-t border-brain-800 pt-1">
              <div className="flex items-center gap-2 text-xs flex-wrap">
                <span className="text-brain-100">{s.name}</span>
                {s.version && <span className="text-brain-600">v{s.version}</span>}
                <span className="text-brain-500">{s.description}</span>
                {s.shared_by && (
                  <span className="inline-flex items-center gap-1 text-brain-400"
                    title={s.approved_by ? t('approved_by', { name: s.approved_by }) : undefined}>
                    <Users size={12} /> {t('shared_by', { name: s.shared_by })}
                  </span>
                )}
                <div className="ml-auto flex items-center gap-2">
                  <button className={btnCls} onClick={() => view(s.name, 'org')}>
                    {openKey === `org:${s.name}` ? t('btn_close') : t('btn_open')}
                  </button>
                </div>
              </div>
              {openKey === `org:${s.name}` && (
                <pre className={preCls}>{content || t('loading_content')}</pre>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Ожидают одобрения — только если есть что одобрять */}
      {pending.length > 0 && (
        <>
          <div className="text-xs text-brain-300 font-medium mb-1">{t('pending_heading')}</div>
          <div className="flex flex-col gap-1 mb-1">
            {pending.map((s) => (
              <div key={`${s.category}:${s.name}`} className="border-t border-brain-800 pt-1">
                <div className="flex items-center gap-2 text-xs flex-wrap">
                  <span className="text-brain-100">{s.name}</span>
                  <span className="text-brain-500">{s.description}</span>
                  {s.shared_by && (
                    <span className="inline-flex items-center gap-1 text-brain-400">
                      <Users size={12} /> {t('shared_by', { name: s.shared_by })}
                    </span>
                  )}
                  {canApprove && (
                    <div className="ml-auto flex items-center gap-2">
                      <button className={btnCls} disabled={busy}
                        onClick={() => decide('approve', s.category, s.name)}>
                        {t('btn_approve')}
                      </button>
                      <button className={btnCls} disabled={busy}
                        onClick={() => decide('reject', s.category, s.name)}>
                        {t('btn_reject')}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {note && <div className="text-xs text-brain-300 mt-2">{note}</div>}
    </div>
  )
}
