'use client'

/**
 * HoldingConsolePanel — консоль холдинга, Слой 1 (docs/HOLDING_CONSOLE.md).
 *
 * Директор владеет N корпорациями: видит карточку по каждой (сводка её мозга),
 * добавляет/убирает корпорации. Каждая корпорация остаётся изолированной org —
 * консоль лишь группирует. Вопросы к корпорации и roll-up — Слой 2/3.
 *
 * Эндпоинты: GET /holdings/mine, POST /holdings, POST/DELETE
 * /holdings/{id}/orgs, GET /holdings/{id}/overview.
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { Landmark, X, Plus, Building2, Loader2, Trash2 } from 'lucide-react'

interface Props { isOpen: boolean; onClose: () => void }

interface Corp { org_id: string; name: string; brain?: { nodes_total: number; by_type: Array<{ type: string; count: number }> } }
interface Holding { id: string; name: string; orgs: Array<{ org_id: string; name: string }> }

export default function HoldingConsolePanel({ isOpen, onClose }: Props) {
  const t = useTranslations('holding_console')
  const [holdings, setHoldings] = useState<Holding[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [corps, setCorps] = useState<Corp[]>([])
  const [loading, setLoading] = useState(false)
  const [loadingOv, setLoadingOv] = useState(false)
  const [newName, setNewName] = useState('')
  const [orgToAdd, setOrgToAdd] = useState('')
  const [addPolicy, setAddPolicy] = useState<'full' | 'aggregate'>('full')
  const [msg, setMsg] = useState<string | null>(null)
  // пикер доступных корпораций (где директор — основатель, ещё не добавлены)
  const [available, setAvailable] = useState<Array<{ org_id: string; name: string }>>([])
  // per-corp: развёрнутый отчёт и «спросить»
  const [openCorp, setOpenCorp] = useState<string | null>(null)
  const [report, setReport] = useState<any>(null)
  const [reportBusy, setReportBusy] = useState(false)
  const [askQ, setAskQ] = useState('')
  const [answer, setAnswer] = useState<{ text?: string; err?: string } | null>(null)
  const [askBusy, setAskBusy] = useState(false)
  const [rollup, setRollup] = useState<any>(null)

  const loadMine = useCallback(async () => {
    setLoading(true)
    try {
      const r = await authFetch('/api/v1/holdings/mine')
      const d = await r.json()
      const hs: Holding[] = d?.holdings || []
      setHoldings(hs)
      setSel((cur) => cur && hs.some((h) => h.id === cur) ? cur : (hs[0]?.id || null))
    } catch { /* ignore */ } finally { setLoading(false) }
  }, [])

  const loadOverview = useCallback(async (id: string) => {
    setLoadingOv(true); setCorps([]); setRollup(null)
    try {
      const r = await authFetch(`/api/v1/holdings/${id}/overview`)
      const d = await r.json()
      setCorps(d?.corporations || [])
    } catch { /* ignore */ } finally { setLoadingOv(false) }
    try {
      const rr = await authFetch(`/api/v1/holdings/${id}/rollup`)
      setRollup(await rr.json())
    } catch { setRollup(null) }
  }, [])

  const loadAvailable = useCallback(async () => {
    try {
      const r = await authFetch('/api/v1/holdings/available-orgs')
      const d = await r.json()
      setAvailable(d?.orgs || [])
    } catch { setAvailable([]) }
  }, [])

  useEffect(() => { if (isOpen) { loadMine(); loadAvailable() } }, [isOpen, loadMine, loadAvailable])
  useEffect(() => { if (sel) { loadOverview(sel); setOpenCorp(null) } }, [sel, loadOverview])

  const openReport = async (org_id: string) => {
    if (openCorp === org_id) { setOpenCorp(null); return }
    setOpenCorp(org_id); setReport(null); setAnswer(null); setAskQ(''); setReportBusy(true)
    try {
      const r = await authFetch(`/api/v1/holdings/${sel}/orgs/${encodeURIComponent(org_id)}/report`)
      setReport(await r.json())
    } catch { setReport(null) } finally { setReportBusy(false) }
  }

  const askCorp = async (org_id: string) => {
    const question = askQ.trim()
    if (!question || askBusy) return
    setAskBusy(true); setAnswer(null)
    try {
      const r = await authFetch(`/api/v1/holdings/${sel}/orgs/${encodeURIComponent(org_id)}/ask`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      })
      const d = await r.json()
      setAnswer(d?.answer ? { text: d.answer } : { err: d?.error || t('no_answer') })
    } catch { setAnswer({ err: t('network_error') }) } finally { setAskBusy(false) }
  }

  const createHolding = async () => {
    const name = newName.trim()
    if (!name) return
    try {
      const r = await authFetch('/api/v1/holdings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      const d = await r.json()
      setNewName('')
      await loadMine()
      if (d?.holding?.id) setSel(d.holding.id)
    } catch { setMsg(t('create_holding_failed')) }
  }

  const attachOrg = async () => {
    const org_id = orgToAdd.trim()
    if (!org_id || !sel) return
    setMsg(null)
    try {
      const r = await authFetch(`/api/v1/holdings/${sel}/orgs`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ org_id, data_policy: addPolicy }),
      })
      if (!r.ok) { const e = await r.json().catch(() => ({})); setMsg(e?.detail || t('corporation_not_found')); return }
      setOrgToAdd('')
      await loadMine(); await loadOverview(sel); await loadAvailable()
    } catch { setMsg(t('network_error')) }
  }

  const detachOrg = async (org_id: string) => {
    if (!sel) return
    try {
      await authFetch(`/api/v1/holdings/${sel}/orgs/${encodeURIComponent(org_id)}`, { method: 'DELETE' })
      await loadMine(); await loadOverview(sel)
    } catch { /* ignore */ }
  }

  if (!isOpen) return null
  const selHolding = holdings.find((h) => h.id === sel)

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
      <div className="bg-brain-900 border border-brain-700 rounded-xl w-full max-w-4xl h-[85vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-5 py-3 border-b border-brain-800">
          <div className="flex items-center gap-2">
            <Landmark className="w-5 h-5 text-purple-400" />
            <div>
              <h2 className="text-white font-semibold">{t('title')}</h2>
              <p className="text-xs text-brain-500">{t('subtitle')}</p>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-brain-800 text-brain-400 hover:text-white"><X className="w-5 h-5" /></button>
        </div>

        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          {loading ? (
            <div className="text-brain-500 text-sm"><Loader2 className="w-4 h-4 animate-spin inline" /> {t('loading')}</div>
          ) : holdings.length === 0 ? (
            <div className="max-w-md mx-auto text-center space-y-3 py-10">
              <Landmark className="w-10 h-10 mx-auto text-brain-600" />
              <p className="text-brain-300">{t('no_holdings_yet')}</p>
              <div className="flex gap-2">
                <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder={t('holding_name_placeholder')}
                  className="flex-1 px-3 py-2 rounded-lg bg-brain-950 border border-brain-700 text-sm text-white" />
                <button onClick={createHolding} disabled={newName.trim().length < 2}
                  className="px-3 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-40 text-white text-sm inline-flex items-center gap-1">
                  <Plus className="w-4 h-4" /> {t('create_button')}
                </button>
              </div>
            </div>
          ) : (
            <>
              {/* Выбор холдинга + создать ещё */}
              <div className="flex items-center gap-2 flex-wrap">
                {holdings.map((h) => (
                  <button key={h.id} onClick={() => setSel(h.id)}
                    className={`px-3 py-1.5 rounded-lg text-sm border ${sel === h.id ? 'bg-purple-600/20 border-purple-500/40 text-purple-200' : 'border-brain-700 text-brain-400 hover:bg-brain-800/50'}`}>
                    🏛 {h.name} <span className="text-brain-500">· {h.orgs.length}</span>
                  </button>
                ))}
                <div className="flex items-center gap-1 ml-auto">
                  <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder={t('new_holding_placeholder')}
                    className="px-2 py-1.5 rounded-lg bg-brain-950 border border-brain-700 text-xs text-white w-36" />
                  <button onClick={createHolding} disabled={newName.trim().length < 2}
                    className="px-2 py-1.5 rounded-lg border border-brain-700 text-brain-300 hover:bg-brain-800 text-xs disabled:opacity-40">＋</button>
                </div>
              </div>

              {/* Добавить корпорацию — выбор из ваших организаций */}
              {selHolding && (
                <div className="flex items-center gap-2">
                  {available.length > 0 ? (
                    <select value={orgToAdd} onChange={(e) => setOrgToAdd(e.target.value)}
                      className="flex-1 px-3 py-2 rounded-lg bg-brain-950 border border-brain-700 text-sm text-white">
                      <option value="">{t('select_corporation_option')}</option>
                      {available.map((o) => (
                        <option key={o.org_id} value={o.org_id}>{o.name}</option>
                      ))}
                    </select>
                  ) : (
                    <input value={orgToAdd} onChange={(e) => setOrgToAdd(e.target.value)} placeholder={t('org_id_placeholder')}
                      className="flex-1 px-3 py-2 rounded-lg bg-brain-950 border border-brain-700 text-sm text-white" />
                  )}
                  <select value={addPolicy} onChange={(e) => setAddPolicy(e.target.value as 'full' | 'aggregate')}
                    title={t('policy_hint')}
                    className="px-2 py-2 rounded-lg bg-brain-950 border border-brain-700 text-sm text-white">
                    <option value="full">{t('policy_full')}</option>
                    <option value="aggregate">{t('policy_aggregate')}</option>
                  </select>
                  <button onClick={attachOrg} disabled={!orgToAdd.trim()}
                    className="px-3 py-2 rounded-lg bg-brain-700 hover:bg-brain-600 disabled:opacity-40 text-white text-sm inline-flex items-center gap-1">
                    <Plus className="w-4 h-4" /> {t('add_button')}
                  </button>
                </div>
              )}
              <p className="text-[10.5px] text-brain-500">{t('policy_hint')}</p>
              {msg && <div className="text-xs text-amber-300">{msg}</div>}

              {/* Сводка по холдингу (кросс-корп roll-up) */}
              {rollup?.totals?.corporations > 0 && (
                <div className="rounded-xl border border-purple-500/25 bg-purple-500/5 p-3">
                  <div className="text-xs font-semibold uppercase tracking-wider text-purple-300 mb-2">{t('holding_summary')}</div>
                  <div className="grid grid-cols-4 gap-2 text-center">
                    {[
                      { l: t('stat_corporations'), v: rollup.totals.corporations },
                      { l: t('stat_nodes'), v: rollup.totals.nodes },
                      { l: t('stat_people'), v: rollup.totals.people },
                      { l: t('stat_projects_in_progress'), v: rollup.totals.projects_in_progress },
                    ].map((x) => (
                      <div key={x.l} className="bg-brain-900/50 rounded-lg py-2">
                        <div className="text-lg font-bold text-brain-100 tabular-nums">{x.v}</div>
                        <div className="text-[10px] text-brain-500">{x.l}</div>
                      </div>
                    ))}
                  </div>
                  {(rollup.signals || []).length > 0 && (
                    <ul className="mt-2 space-y-0.5">
                      {rollup.signals.map((s: any, i: number) => (
                        <li key={i} className="text-xs text-brain-300"><span className="text-purple-400 mr-1">•</span>{s.text}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {/* Карточки корпораций */}
              {loadingOv ? (
                <div className="text-brain-500 text-sm"><Loader2 className="w-4 h-4 animate-spin inline" /> {t('overview_loading')}</div>
              ) : corps.length === 0 ? (
                <div className="text-brain-500 text-sm py-6 text-center">{t('no_corporations_in_holding')}</div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {corps.map((c) => (
                    <div key={c.org_id} className="rounded-xl border border-brain-700 bg-brain-800/40 p-4">
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex items-center gap-2 min-w-0">
                          <Building2 className="w-4 h-4 text-brain-400 shrink-0" />
                          <span className="text-white font-medium truncate">{c.name}</span>
                        </div>
                        <button onClick={() => detachOrg(c.org_id)} title={t('remove_from_holding')}
                          className="text-brain-600 hover:text-red-400"><Trash2 className="w-3.5 h-3.5" /></button>
                      </div>
                      <div className="mt-2 text-2xl font-bold text-brain-100">{c.brain?.nodes_total ?? 0}
                        <span className="text-xs font-normal text-brain-500 ml-1">{t('nodes_in_brain')}</span></div>
                      <div className="mt-2 flex flex-wrap gap-1">
                        {(c.brain?.by_type || []).map((b) => (
                          <span key={b.type} className="text-[11px] px-1.5 py-0.5 rounded bg-brain-900 text-brain-300">{b.type}: {b.count}</span>
                        ))}
                      </div>
                      <button onClick={() => openReport(c.org_id)}
                        className="mt-3 text-xs text-purple-300 hover:text-purple-200">
                        {openCorp === c.org_id ? t('collapse') : t('report_and_questions')}
                      </button>

                      {openCorp === c.org_id && (
                        <div className="mt-2 pt-2 border-t border-brain-700 space-y-2">
                          {reportBusy ? (
                            <div className="text-brain-500 text-xs"><Loader2 className="w-3 h-3 animate-spin inline" /> {t('report_loading')}</div>
                          ) : report && (
                            <div className="text-xs text-brain-300 space-y-1">
                              <div>{t('report_people_count', { count: report.people_count ?? 0 })}</div>
                              {(report.recent_decisions || []).length > 0 && (
                                <div>{t('report_decisions', { list: report.recent_decisions.map((d: any) => `${d.name}${d.date ? ` (${d.date})` : ''}`).slice(0, 3).join('; ') })}</div>
                              )}
                              {(report.projects || []).length > 0 && (
                                <div>{t('report_projects', { list: report.projects.map((p: any) => `${p.name}${p.status ? ` [${p.status}]` : ''}`).slice(0, 4).join('; ') })}</div>
                              )}
                            </div>
                          )}
                          {/* Спросить корпорацию */}
                          <div className="flex gap-1.5">
                            <input value={askQ} onChange={(e) => setAskQ(e.target.value)}
                              onKeyDown={(e) => { if (e.key === 'Enter') askCorp(c.org_id) }}
                              placeholder={t('ask_corporation_placeholder')}
                              className="flex-1 px-2 py-1.5 rounded bg-brain-950 border border-brain-700 text-xs text-white" />
                            <button onClick={() => askCorp(c.org_id)} disabled={askBusy || !askQ.trim()}
                              className="px-2 py-1.5 rounded bg-purple-600 hover:bg-purple-500 disabled:opacity-40 text-white text-xs">
                              {askBusy ? '…' : t('ask_button')}
                            </button>
                          </div>
                          {answer?.text && <div className="text-xs text-brain-200 bg-brain-950 rounded p-2 whitespace-pre-wrap max-h-48 overflow-auto">{answer.text}</div>}
                          {answer?.err && <div className="text-xs text-amber-300">{answer.err}</div>}
                          <div className="text-[10px] text-brain-600">{t('read_only_audited')}</div>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
