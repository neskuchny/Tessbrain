'use client'

/**
 * ShareProjectPanel — «Поделиться проектом» (P1 мульти-аккаунта).
 *
 * Владелец выдаёт коллеге из своей организации доступ read/write к проекту
 * SIMA (гранты resource_permissions). У получателя проект появляется в
 * списке с пометкой «доступен мне». Отзыв — крестиком. Коллеги берутся из
 * GET /my-org; гранты — /api/v1/permissions/project/{id}.
 */
import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { X, Share2, Loader2, Check } from 'lucide-react'

interface Member { user_id: string; role?: string; department?: string | null }
interface Grant { grantee_type: string; grantee_id: string; permission_type: string }

function authHdrs(): Record<string, string> {
  const token = typeof window !== 'undefined' ? localStorage.getItem('tessent_access_token') : null
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) h['Authorization'] = `Bearer ${token}`
  return h
}

export default function ShareProjectPanel({
  projectId, projectName, ownerId, currentUserId, onClose,
}: {
  projectId: string
  projectName: string
  ownerId: string
  currentUserId: string
  onClose: () => void
}) {
  const t = useTranslations('sima_share')
  const [members, setMembers] = useState<Member[]>([])
  const [grants, setGrants] = useState<Grant[]>([])
  const [orgName, setOrgName] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pickId, setPickId] = useState('')
  const [pickLevel, setPickLevel] = useState<'read' | 'write'>('read')

  const load = async () => {
    setLoading(true); setError(null)
    try {
      const [orgRes, grantsRes] = await Promise.all([
        fetch('/api/v1/my-org', { headers: authHdrs() }),
        fetch(`/api/v1/permissions/project/${projectId}`, { headers: authHdrs() }),
      ])
      const org = await orgRes.json().catch(() => ({}))
      setOrgName(org?.org?.name || null)
      setMembers((org?.members || []).filter((m: Member) => m.user_id !== currentUserId))
      const g = await grantsRes.json().catch(() => ({}))
      if (grantsRes.status === 403) setError(t('sharing_disabled'))
      setGrants(g?.grants || [])
    } catch (e) {
      setError((e as Error).message)
    } finally { setLoading(false) }
  }

  useEffect(() => { load() /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [projectId])

  const doGrant = async () => {
    if (!pickId || busy) return
    setBusy('grant'); setError(null)
    try {
      const res = await fetch(`/api/v1/permissions/project/${projectId}`, {
        method: 'POST', headers: authHdrs(),
        body: JSON.stringify({ grantee_type: 'user', grantee_id: pickId,
                               permission_type: pickLevel, owner_id: ownerId }),
      })
      const d = await res.json().catch(() => ({}))
      if (d?.success === false || d?.error) setError(d.error || t('grant_failed'))
      else { setPickId(''); await load() }
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const doRevoke = async (g: Grant) => {
    if (busy) return
    setBusy(g.grantee_id); setError(null)
    try {
      await fetch(`/api/v1/permissions/project/${projectId}`, {
        method: 'DELETE', headers: authHdrs(),
        body: JSON.stringify({ grantee_type: g.grantee_type, grantee_id: g.grantee_id,
                               owner_id: ownerId }),
      })
      await load()
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const memberLabel = (uid: string) => {
    const m = members.find((x) => x.user_id === uid)
    const short = uid.slice(0, 8)
    if (!m) return short
    return `${short} · ${m.role || t('member_role_fallback')}${m.department ? ` · ${m.department}` : ''}`
  }

  return (
    <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4">
      <div className="max-w-md w-full rounded-2xl border border-sima-border bg-sima-surface p-5 space-y-3 shadow-2xl">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Share2 className="w-4 h-4 text-sima-primary shrink-0" />
            <h2 className="text-sima-text font-semibold text-sm truncate">{t('title', { name: projectName })}</h2>
          </div>
          <button onClick={onClose} className="p-1 hover:bg-sima-surfaceLight rounded">
            <X className="w-4 h-4 text-sima-textDim" />
          </button>
        </div>
        <p className="text-[11px] text-sima-textMuted">
          {t('description')}
          {orgName && <> {t('org_line', { name: orgName })}</>}
        </p>

        {loading ? (
          <div className="flex items-center gap-2 text-sima-textDim text-xs py-3">
            <Loader2 className="w-4 h-4 animate-spin" /> {t('loading')}
          </div>
        ) : (
          <>
            {/* Выдать доступ */}
            <div className="flex items-center gap-1.5">
              <select value={pickId} onChange={(e) => setPickId(e.target.value)}
                className="flex-1 px-2 py-1.5 rounded bg-sima-bg border border-sima-border text-[11px] text-sima-text">
                <option value="">{t('pick_colleague')}</option>
                {members.map((m) => (
                  <option key={m.user_id} value={m.user_id}>{memberLabel(m.user_id)}</option>
                ))}
              </select>
              <select value={pickLevel} onChange={(e) => setPickLevel(e.target.value as any)}
                className="px-1.5 py-1.5 rounded bg-sima-bg border border-sima-border text-[11px] text-sima-text">
                <option value="read">{t('level_read')}</option>
                <option value="write">{t('level_write')}</option>
              </select>
              <button onClick={doGrant} disabled={!pickId || busy !== null}
                className="px-2.5 py-1.5 rounded bg-sima-primary/80 hover:bg-sima-primary text-white text-[11px] disabled:opacity-50">
                {busy === 'grant' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : t('grant_button')}
              </button>
            </div>
            {members.length === 0 && (
              <p className="text-[10.5px] text-sima-textDim">
                {t('no_members')}
              </p>
            )}

            {/* Текущие доступы */}
            <div className="space-y-1">
              <div className="text-[10px] uppercase tracking-wide text-sima-textDim">{t('grants_heading')}</div>
              {grants.length === 0 ? (
                <p className="text-[11px] text-sima-textDim">{t('no_grants')}</p>
              ) : grants.map((g) => (
                <div key={`${g.grantee_type}:${g.grantee_id}`}
                     className="flex items-center justify-between gap-2 text-[11px] bg-sima-bg rounded px-2 py-1.5 border border-sima-border">
                  <span className="text-sima-text truncate">
                    <Check className="w-3 h-3 inline mr-1 text-green-400" />
                    {memberLabel(g.grantee_id)} — {g.permission_type === 'write' ? t('level_write') : t('level_read')}
                  </span>
                  <button onClick={() => doRevoke(g)} disabled={busy !== null}
                    title={t('revoke_title')}
                    className="text-sima-textDim hover:text-red-400 disabled:opacity-50">
                    {busy === g.grantee_id ? <Loader2 className="w-3 h-3 animate-spin" /> : <X className="w-3.5 h-3.5" />}
                  </button>
                </div>
              ))}
            </div>
          </>
        )}
        {error && <p className="text-[11px] text-red-400">{error}</p>}
      </div>
    </div>
  )
}
