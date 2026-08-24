'use client'

/**
 * OrgPanel — «Компания»: продуктовая поверхность мульти-аккаунта.
 *
 * - solo-пользователь: создать компанию (self-serve, он становится
 *   генеральным/founder);
 * - участник: карточка компании, «Мозг компании целиком» (сколько узлов и
 *   что видно именно вам — уровни/отделы честно), участники (роль/отдел,
 *   правка для админов), приглашения (создать → токен-ссылка один раз,
 *   отозвать).
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { Building2, X, Loader2, Users, Brain, Link2, Trash2, Copy, Check, Shield, Network, Plus } from 'lucide-react'
import { StrategyBroadcastSection } from '@/components/StrategyBroadcast'
import { SyncDashboardSection } from '@/components/SyncDashboard'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

function hdrs(): Record<string, string> {
  const t = typeof window !== 'undefined' ? localStorage.getItem('tessent_access_token') : null
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  if (t) h['Authorization'] = `Bearer ${t}`
  return h
}

interface Member { user_id: string; role?: string; department?: string | null }
interface Invite { invite_id: string; role?: string; invited_email?: string | null; expires_at?: string }
interface CustomRole { role: string; base: string; title?: string; max_access_level?: number }
interface OrgTreeNode { org_id: string; name: string; parent_org_id?: string | null; children: OrgTreeNode[] }
interface BrainStats {
  total_nodes: number; visible_nodes: number; hidden_nodes: number
  by_type: Record<string, number>; by_department: Record<string, number>; by_level: Record<string, number>
}

const ROLE_LABEL_KEYS: Record<string, string> = {
  founder: 'role_founder', admin: 'role_admin', manager: 'role_manager',
  employee: 'role_employee', contractor: 'role_contractor',
}

export default function OrgPanel({ userId, isOpen, onClose }: {
  userId: string | null
  isOpen: boolean
  onClose: () => void
}) {
  const t = useTranslations('org_panel')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [org, setOrg] = useState<{ id: string; name?: string } | null>(null)
  const [me, setMe] = useState<{ role?: string; department?: string } | null>(null)
  const [members, setMembers] = useState<Member[]>([])
  const [stats, setStats] = useState<BrainStats | null>(null)
  const [invites, setInvites] = useState<Invite[]>([])
  const [newInviteToken, setNewInviteToken] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [newCompanyName, setNewCompanyName] = useState('')
  const [deptDraft, setDeptDraft] = useState<Record<string, string>>({})
  const [inviteTokenInput, setInviteTokenInput] = useState('')
  const [myOrgs, setMyOrgs] = useState<Array<{ org_id: string; name?: string; role?: string; primary?: boolean }>>([])
  const [customRoles, setCustomRoles] = useState<CustomRole[]>([])
  const [orgTree, setOrgTree] = useState<OrgTreeNode | null>(null)
  const [newRole, setNewRole] = useState<{ role: string; base: string; title: string; cap: string }>(
    { role: '', base: 'employee', title: '', cap: '' })

  // canManage — по base-роли (кастомная роль с base=admin тоже управляет)
  const baseRole = (role?: string): string => {
    if (!role) return ''
    if (['founder', 'admin', 'manager', 'employee', 'contractor'].includes(role)) return role
    return customRoles.find((c) => c.role === role)?.base || ''
  }
  const canManage = ['founder', 'admin'].includes(baseRole(me?.role))

  const roleLabel = (role?: string | null) => {
    const key = ROLE_LABEL_KEYS[role || '']
    if (key) return t(key)
    const custom = customRoles.find((c) => c.role === role)
    return custom?.title || role
  }

  const load = useCallback(async () => {
    if (!userId) return
    setLoading(true); setError(null)
    try {
      const res = await fetch(`${API}/api/v1/my-org`, { headers: hdrs() })
      const d = await res.json()
      // Все членства (мульти-орг): показываем переключатель, если их >1.
      try {
        const mo = await fetch(`${API}/api/v1/my-orgs`, { headers: hdrs() })
        if (mo.ok) setMyOrgs((await mo.json())?.orgs || [])
      } catch { /* ignore */ }
      if (!d?.org) { setOrg(null); setMembers([]); setStats(null); return }
      setOrg(d.org); setMe(d.me || null); setMembers(d.members || [])
      // ролевая модель организации (встроенные + кастомные)
      let custom: CustomRole[] = []
      try {
        const rr = await fetch(`${API}/api/v1/orgs/${d.org.id}/roles`, { headers: hdrs() })
        if (rr.ok) { custom = (await rr.json())?.custom || []; setCustomRoles(custom) }
      } catch { /* ignore */ }
      // дерево организаций (холдинг → корпорация → филиал)
      try {
        const tr = await fetch(`${API}/api/v1/orgs/${d.org.id}/tree`, { headers: hdrs() })
        if (tr.ok) setOrgTree((await tr.json())?.tree || null)
      } catch { /* ignore */ }
      // управляющий — по base-роли (кастомная роль с base=admin тоже)
      const myBase = ['founder', 'admin', 'manager', 'employee', 'contractor']
        .includes(d.me?.role) ? d.me?.role
        : custom.find((c) => c.role === d.me?.role)?.base || ''
      const manages = ['founder', 'admin'].includes(myBase)
      // мозг компании — через призму МОИХ прав
      try {
        const sr = await fetch(`${API}/api/v1/orgs/${d.org.id}/brain-stats`, { headers: hdrs() })
        if (sr.ok) setStats(await sr.json())
      } catch { /* ignore */ }
      // приглашения — только для управляющих
      if (manages) {
        try {
          const ir = await fetch(`${API}/api/v1/orgs/${d.org.id}/invites`, { headers: hdrs() })
          if (ir.ok) setInvites((await ir.json())?.invites || [])
        } catch { /* ignore */ }
      }
    } catch (e) {
      setError((e as Error).message)
    } finally { setLoading(false) }
  }, [userId])

  useEffect(() => { if (isOpen) load() }, [isOpen, load])

  const createCompany = async () => {
    if (!newCompanyName.trim() || busy) return
    setBusy('create'); setError(null)
    try {
      const res = await fetch(`${API}/api/v1/orgs`, {
        method: 'POST', headers: hdrs(),
        body: JSON.stringify({ name: newCompanyName.trim() }),
      })
      const d = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(d?.detail || t('create_company_error'))
      setNewCompanyName('')
      await load()
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const acceptInvite = async () => {
    const token = inviteTokenInput.trim()
    if (!token || busy) return
    setBusy('accept'); setError(null)
    try {
      const res = await fetch(`${API}/api/v1/invites/${encodeURIComponent(token)}/accept`, {
        method: 'POST', headers: hdrs(), body: JSON.stringify({}),
      })
      const d = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(d?.detail || t('accept_invite_error'))
      setInviteTokenInput('')
      await load()
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const createInvite = async (role: string) => {
    if (!org || busy) return
    setBusy('invite'); setError(null); setNewInviteToken(null)
    try {
      const res = await fetch(`${API}/api/v1/orgs/${org.id}/invites`, {
        method: 'POST', headers: hdrs(), body: JSON.stringify({ role }),
      })
      const d = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(d?.detail || t('create_invite_error'))
      setNewInviteToken(d?.token || null) // показывается ОДИН раз
      await load()
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const revokeInvite = async (inviteId: string) => {
    if (!org || busy) return
    setBusy(inviteId)
    try {
      await fetch(`${API}/api/v1/orgs/${org.id}/invites/${inviteId}`, {
        method: 'DELETE', headers: hdrs(),
      })
      await load()
    } catch { /* ignore */ } finally { setBusy(null) }
  }

  const saveMember = async (m: Member, patch: { role?: string; department?: string }) => {
    if (!org || busy) return
    setBusy(m.user_id)
    try {
      const res = await fetch(`${API}/api/v1/orgs/${org.id}/members/${m.user_id}`, {
        method: 'PATCH', headers: hdrs(), body: JSON.stringify(patch),
      })
      const d = await res.json().catch(() => ({}))
      if (!res.ok) setError(d?.detail || t('save_error'))
      await load()
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const switchOrg = async (orgId: string) => {
    if (busy || orgId === org?.id) return
    setBusy('switch'); setError(null)
    try {
      const res = await fetch(`${API}/api/v1/my-org/switch`, {
        method: 'POST', headers: hdrs(), body: JSON.stringify({ org_id: orgId }),
      })
      const d = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(d?.detail || t('switch_error'))
      await load()
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const saveRoles = async (roles: CustomRole[]) => {
    if (!org || busy) return
    setBusy('roles'); setError(null)
    const payload: Record<string, { base: string; title?: string; max_access_level?: number }> = {}
    for (const r of roles) {
      const name = r.role.trim().toLowerCase()
      if (!name) continue
      payload[name] = { base: r.base }
      if (r.title?.trim()) payload[name].title = r.title.trim()
      if (r.max_access_level !== undefined && r.max_access_level !== null) {
        payload[name].max_access_level = r.max_access_level
      }
    }
    try {
      const res = await fetch(`${API}/api/v1/orgs/${org.id}/roles`, {
        method: 'PUT', headers: hdrs(), body: JSON.stringify({ roles: payload }),
      })
      const d = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(d?.detail || t('roles_save_error'))
      await load()
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const addCustomRole = () => {
    const name = newRole.role.trim().toLowerCase()
    if (!name || customRoles.some((c) => c.role === name)) return
    const next: CustomRole = { role: name, base: newRole.base }
    if (newRole.title.trim()) next.title = newRole.title.trim()
    if (newRole.cap !== '') next.max_access_level = Math.max(0, Math.min(5, Number(newRole.cap)))
    setNewRole({ role: '', base: 'employee', title: '', cap: '' })
    saveRoles([...customRoles, next])
  }

  const removeCustomRole = (role: string) => saveRoles(customRoles.filter((c) => c.role !== role))

  const copyInvite = async () => {
    if (!newInviteToken) return
    try {
      await navigator.clipboard.writeText(newInviteToken)
      setCopied(true); setTimeout(() => setCopied(false), 2000)
    } catch { /* ignore */ }
  }

  if (!isOpen) return null

  const renderTree = (node: OrgTreeNode, depth = 0): JSX.Element => (
    <div key={node.org_id} style={{ marginLeft: depth * 14 }}>
      <div className="flex items-center gap-1.5 text-xs py-0.5">
        {depth > 0 && <span className="text-brain-600">└</span>}
        <span className={node.org_id === org?.id ? 'text-purple-300 font-medium' : 'text-brain-300'}>
          {node.name}
        </span>
        {node.org_id === org?.id && <span className="text-[10px] text-purple-400">• {t('you_here')}</span>}
      </div>
      {node.children?.map((c) => renderTree(c, depth + 1))}
    </div>
  )

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="max-w-2xl w-full max-h-[85vh] overflow-y-auto rounded-2xl border border-brain-700/40 bg-brain-900 p-5 space-y-4 shadow-2xl">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Building2 className="w-5 h-5 text-purple-400" />
            <h2 className="text-white font-semibold">{t('title')}</h2>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg text-brain-500 hover:bg-brain-800 hover:text-brain-300">
            <X className="w-4 h-4" />
          </button>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 text-brain-400 text-sm py-6">
            <Loader2 className="w-4 h-4 animate-spin" /> {t('loading')}
          </div>
        ) : !org ? (
          /* solo: создать компанию */
          <div className="space-y-3">
            <p className="text-sm text-brain-300">
              {t('no_company_text')}
            </p>
            <div className="flex items-center gap-2">
              <input value={newCompanyName} onChange={(e) => setNewCompanyName(e.target.value)}
                placeholder={t('company_name_placeholder')}
                className="flex-1 px-3 py-2 rounded-lg bg-brain-950 border border-brain-700/40 text-white text-sm" />
              <button onClick={createCompany} disabled={!newCompanyName.trim() || busy !== null}
                className="px-3 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm disabled:opacity-50">
                {busy === 'create' ? <Loader2 className="w-4 h-4 animate-spin" /> : t('create_button')}
              </button>
            </div>

            {/* Новый сотрудник: вступить по приглашению */}
            <div className="pt-2 border-t border-brain-800/60 space-y-1.5">
              <p className="text-xs text-brain-400">{t('invite_prompt')}</p>
              <div className="flex items-center gap-2">
                <input value={inviteTokenInput} onChange={(e) => setInviteTokenInput(e.target.value)}
                  placeholder={t('invite_token_placeholder')}
                  className="flex-1 px-3 py-2 rounded-lg bg-brain-950 border border-brain-700/40 text-white text-sm font-mono" />
                <button onClick={acceptInvite} disabled={!inviteTokenInput.trim() || busy !== null}
                  className="px-3 py-2 rounded-lg bg-brain-700 hover:bg-brain-600 text-white text-sm disabled:opacity-50">
                  {busy === 'accept' ? <Loader2 className="w-4 h-4 animate-spin" /> : t('join_button')}
                </button>
              </div>
            </div>
          </div>
        ) : (
          <>
            {/* Переключатель организаций (мульти-орг: директор портфеля) */}
            {myOrgs.length > 1 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] uppercase tracking-wide text-brain-500 mr-1">{t('my_orgs')}</span>
                {myOrgs.map((o) => (
                  <button key={o.org_id} onClick={() => switchOrg(o.org_id)}
                    disabled={busy !== null}
                    title={roleLabel(o.role) || ''}
                    className={`px-2.5 py-1 rounded-lg text-xs border transition-colors disabled:opacity-50 ${
                      o.primary
                        ? 'bg-purple-600 border-purple-500 text-white'
                        : 'bg-brain-950 border-brain-700/40 text-brain-300 hover:bg-brain-800'
                    }`}>
                    {o.name || o.org_id}
                  </button>
                ))}
              </div>
            )}

            {/* Карточка компании */}
            <div className="text-sm text-brain-300">
              <span className="text-white font-medium">{org.name || org.id}</span>
              {me && <> · {t('you_are', { role: roleLabel(me.role) || '' })}{me.department ? ` · ${t('department_label', { department: me.department })}` : ''}</>}
            </div>

            {/* Дерево организаций (холдинг → корпорация → филиал) */}
            {orgTree && (orgTree.children.length > 0 || orgTree.parent_org_id) && (
              <div className="rounded-xl border border-brain-700/30 bg-brain-950/50 p-3 space-y-1.5">
                <div className="flex items-center gap-2 text-[12px] uppercase tracking-wide text-brain-400">
                  <Network className="w-4 h-4 text-purple-400" /> {t('org_tree')}
                </div>
                {renderTree(orgTree)}
              </div>
            )}

            {/* Мозг компании целиком */}
            <div className="rounded-xl border border-brain-700/30 bg-brain-950/50 p-3 space-y-2">
              <div className="flex items-center gap-2 text-[12px] uppercase tracking-wide text-brain-400">
                <Brain className="w-4 h-4 text-purple-400" /> {t('company_brain')}
              </div>
              {!stats ? (
                <p className="text-xs text-brain-500">{t('stats_unavailable')}</p>
              ) : stats.total_nodes === 0 ? (
                <p className="text-xs text-brain-400">
                  {t('brain_empty')}
                </p>
              ) : (
                <>
                  <p className="text-sm text-brain-200">
                    {t.rich('brain_totals', {
                      total: stats.total_nodes,
                      visible: stats.visible_nodes,
                      b: (chunks) => <b>{chunks}</b>,
                    })}
                    {stats.hidden_nodes > 0 && (
                      <span className="text-brain-500"> {t('hidden_nodes', { n: stats.hidden_nodes })}</span>
                    )}
                  </p>
                  <div className="flex flex-wrap gap-1.5 text-[11px]">
                    {Object.entries(stats.by_type).map(([k, v]) => (
                      <span key={k} className="px-2 py-0.5 rounded-full bg-brain-800/70 text-brain-300">{k}: {v}</span>
                    ))}
                  </div>
                  {Object.keys(stats.by_department).length > 1 && (
                    <div className="flex flex-wrap gap-1.5 text-[11px]">
                      {Object.entries(stats.by_department).map(([k, v]) => (
                        <span key={k} className="px-2 py-0.5 rounded-full bg-purple-500/10 text-purple-300">🏢 {k}: {v}</span>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>

            {/* Участники */}
            <div className="rounded-xl border border-brain-700/30 bg-brain-950/50 p-3 space-y-2">
              <div className="flex items-center gap-2 text-[12px] uppercase tracking-wide text-brain-400">
                <Users className="w-4 h-4 text-purple-400" /> {t('members_count', { n: members.length })}
              </div>
              <div className="space-y-1.5">
                {members.map((m) => (
                  <div key={m.user_id} className="flex items-center gap-2 text-xs bg-brain-900/60 rounded-lg px-2.5 py-1.5 border border-brain-800/50">
                    <span className="font-mono text-brain-400 w-20 truncate" title={m.user_id}>{m.user_id.slice(0, 8)}</span>
                    {canManage && m.user_id !== userId ? (
                      <select value={m.role || 'employee'} disabled={busy !== null}
                        onChange={(e) => saveMember(m, { role: e.target.value })}
                        className="px-1.5 py-1 rounded bg-brain-950 border border-brain-700/40 text-brain-200">
                        {Object.entries(ROLE_LABEL_KEYS).map(([r, key]) => (
                          <option key={r} value={r}>{t(key)}</option>
                        ))}
                        {customRoles.length > 0 && (
                          <optgroup label={t('custom_roles')}>
                            {customRoles.map((c) => (
                              <option key={c.role} value={c.role}>{c.title || c.role}</option>
                            ))}
                          </optgroup>
                        )}
                      </select>
                    ) : (
                      <span className="text-brain-200">{roleLabel(m.role)}</span>
                    )}
                    {canManage ? (
                      <input
                        value={deptDraft[m.user_id] ?? (m.department || '')}
                        onChange={(e) => setDeptDraft((p) => ({ ...p, [m.user_id]: e.target.value }))}
                        onBlur={(e) => {
                          const v = e.target.value.trim()
                          if (v !== (m.department || '')) saveMember(m, { department: v })
                        }}
                        placeholder={t('department_placeholder')}
                        className="flex-1 px-2 py-1 rounded bg-brain-950 border border-brain-700/40 text-brain-200" />
                    ) : (
                      <span className="flex-1 text-brain-400">{m.department || '—'}</span>
                    )}
                    {busy === m.user_id && <Loader2 className="w-3 h-3 animate-spin text-brain-500" />}
                  </div>
                ))}
              </div>
              <p className="text-[10.5px] text-brain-600">
                {t('department_visibility_note')}
              </p>
            </div>

            {/* Кастомные роли организации (enterprise, только управляющим) */}
            {canManage && (
              <div className="rounded-xl border border-brain-700/30 bg-brain-950/50 p-3 space-y-2">
                <div className="flex items-center gap-2 text-[12px] uppercase tracking-wide text-brain-400">
                  <Shield className="w-4 h-4 text-purple-400" /> {t('custom_roles')}
                </div>
                <p className="text-[10.5px] text-brain-600">{t('custom_roles_hint')}</p>
                {customRoles.length > 0 && (
                  <div className="space-y-1">
                    {customRoles.map((c) => (
                      <div key={c.role} className="flex items-center gap-2 text-[11px] text-brain-300 bg-brain-900/60 rounded px-2 py-1 border border-brain-800/50">
                        <span className="font-medium text-brain-200">{c.title || c.role}</span>
                        <span className="text-brain-500">{t('role_base', { base: roleLabel(c.base) || c.base })}</span>
                        {c.max_access_level !== undefined && (
                          <span className="text-amber-300/80">{t('role_cap', { level: c.max_access_level })}</span>
                        )}
                        <button onClick={() => removeCustomRole(c.role)} disabled={busy !== null}
                          title={t('revoke')} className="ml-auto text-brain-500 hover:text-red-400">
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                <div className="flex flex-wrap items-center gap-1.5">
                  <input value={newRole.role} onChange={(e) => setNewRole((p) => ({ ...p, role: e.target.value }))}
                    placeholder={t('role_name_placeholder')}
                    className="w-28 px-2 py-1 rounded bg-brain-950 border border-brain-700/40 text-brain-200 text-xs" />
                  <input value={newRole.title} onChange={(e) => setNewRole((p) => ({ ...p, title: e.target.value }))}
                    placeholder={t('role_title_placeholder')}
                    className="w-32 px-2 py-1 rounded bg-brain-950 border border-brain-700/40 text-brain-200 text-xs" />
                  <select value={newRole.base} onChange={(e) => setNewRole((p) => ({ ...p, base: e.target.value }))}
                    className="px-1.5 py-1 rounded bg-brain-950 border border-brain-700/40 text-brain-200 text-xs">
                    {(['admin', 'manager', 'employee', 'contractor'] as const).map((b) => (
                      <option key={b} value={b}>{t(ROLE_LABEL_KEYS[b])}</option>
                    ))}
                  </select>
                  <input value={newRole.cap} onChange={(e) => setNewRole((p) => ({ ...p, cap: e.target.value }))}
                    placeholder={t('role_cap_placeholder')} type="number" min={0} max={5}
                    className="w-16 px-2 py-1 rounded bg-brain-950 border border-brain-700/40 text-brain-200 text-xs" />
                  <button onClick={addCustomRole} disabled={!newRole.role.trim() || busy !== null}
                    className="px-2 py-1 rounded-lg bg-purple-600/80 hover:bg-purple-500 text-white text-xs disabled:opacity-50 flex items-center gap-1">
                    {busy === 'roles' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                    {t('add')}
                  </button>
                </div>
              </div>
            )}

            {/* Синхронизация компании: 4 уровня + S(t) (CogniLayer Ф2) */}
            {canManage && <SyncDashboardSection />}

            {/* Трансляция стратегии: послание → персональные версии (Persona) */}
            {canManage && members.length > 1 && (
              <StrategyBroadcastSection members={members} />
            )}

            {/* Приглашения (только управляющим) */}
            {canManage && (
              <div className="rounded-xl border border-brain-700/30 bg-brain-950/50 p-3 space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 text-[12px] uppercase tracking-wide text-brain-400">
                    <Link2 className="w-4 h-4 text-purple-400" /> {t('invites_title')}
                  </div>
                  <div className="flex gap-1.5">
                    {(['employee', 'manager'] as const).map((r) => (
                      <button key={r} onClick={() => createInvite(r)} disabled={busy !== null}
                        className="px-2 py-1 rounded-lg bg-purple-600/80 hover:bg-purple-500 text-white text-[11px] disabled:opacity-50">
                        + {t(ROLE_LABEL_KEYS[r])}
                      </button>
                    ))}
                  </div>
                </div>
                {newInviteToken && (
                  <div className="rounded-lg bg-amber-500/10 border border-amber-500/30 p-2 text-[11px] text-amber-200 space-y-1">
                    <p>{t('invite_token_notice')}</p>
                    <div className="flex items-center gap-1.5">
                      <code className="flex-1 font-mono break-all select-all text-amber-100">{newInviteToken}</code>
                      <button onClick={copyInvite} className="p-1 rounded hover:bg-amber-500/20">
                        {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                      </button>
                    </div>
                  </div>
                )}
                {invites.length > 0 && (
                  <div className="space-y-1">
                    {invites.map((inv) => (
                      <div key={inv.invite_id} className="flex items-center justify-between gap-2 text-[11px] text-brain-300 bg-brain-900/60 rounded px-2 py-1 border border-brain-800/50">
                        <span>{roleLabel(inv.role || 'employee')}{inv.invited_email ? ` · ${inv.invited_email}` : ''}</span>
                        <button onClick={() => revokeInvite(inv.invite_id)} disabled={busy !== null}
                          title={t('revoke')} className="text-brain-500 hover:text-red-400">
                          {busy === inv.invite_id ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </>
        )}
        {error && <p className="text-xs text-red-400">{error}</p>}
      </div>
    </div>
  )
}
