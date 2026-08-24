'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { Handshake, MessageCircle, RefreshCw, Trash2, Users, Zap } from 'lucide-react'
import ReactMarkdown from 'react-markdown'

// Симуляция клиентов, сегментов и партнёров. Backend: /clients/sim/*.
// Реальные клиенты — из графа (встречи+CRM), группы рынка — явные гипотезы,
// партнёры — слепки из реальных встреч. Везде дисклеймер: это симуляция.

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

type Client = { id: string; name: string; industry?: string; segment?: string; status?: string; description?: string }
type Segment = { name: string; client_ids: string[]; count: number }
type Group = { id: string; name: string; who: string; pains: string[]; buying_trigger: string; objections: string[]; channel: string; validate_by: string; fit_1_5: number; market: string }
type Person = { id: string; name: string; internal: boolean; role?: string; category?: string }
type ChatMsg = { role: 'user' | 'sim'; text: string }

const box = 'bg-brain-900/60 border border-brain-700 rounded-lg p-3'
const inputCls = 'px-2 py-1 rounded bg-brain-900 border border-brain-700 text-brain-100 text-xs w-full'
const btnCls = 'px-3 py-1 rounded-md bg-brain-700 hover:bg-brain-600 text-white text-xs disabled:opacity-50 inline-flex items-center gap-1'

export default function ClientSimPanel({ userId }: { userId: string | null }) {
  const t = useTranslations('client_sim_panel')
  const [tab, setTab] = useState<'clients' | 'market' | 'offer' | 'chat' | 'partner'>('clients')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const [clients, setClients] = useState<Client[]>([])
  const [segments, setSegments] = useState<Segment[]>([])
  const [groups, setGroups] = useState<Group[]>([])
  const [selClients, setSelClients] = useState<Set<string>>(new Set())
  const [selGroups, setSelGroups] = useState<Set<string>>(new Set())

  const [market, setMarket] = useState('')
  const [productDesc, setProductDesc] = useState('')

  const [offer, setOffer] = useState('')
  const [panelResult, setPanelResult] = useState<any>(null)

  const [chatTarget, setChatTarget] = useState<{ kind: 'client' | 'group'; id: string; name: string } | null>(null)
  const [chatHistory, setChatHistory] = useState<ChatMsg[]>([])
  const [chatInput, setChatInput] = useState('')

  const [partners, setPartners] = useState<Person[]>([])
  const [partner, setPartner] = useState<Person | null>(null)
  const [pMode, setPMode] = useState<'negotiation' | 'co_create'>('co_create')
  const [pHistory, setPHistory] = useState<ChatMsg[]>([])
  const [pInput, setPInput] = useState('')
  const [packMd, setPackMd] = useState('')
  const [packFocus, setPackFocus] = useState('')

  const load = useCallback(async () => {
    if (!userId) return
    setBusy(true); setError('')
    try {
      const [rc, rg] = await Promise.all([
        authFetch(`${API}/api/v1/clients/sim/clients`),
        authFetch(`${API}/api/v1/clients/sim/market-groups`),
      ])
      const dc = await rc.json().catch(() => ({}))
      const dg = await rg.json().catch(() => ({}))
      setClients(dc.clients || []); setSegments(dc.segments || [])
      setGroups(dg.groups || [])
    } catch (e: any) { setError(e?.message || t('error_network_unavailable')) }
    setBusy(false)
  }, [userId, t])

  useEffect(() => { load() }, [load])

  const loadPartners = useCallback(async () => {
    if (!userId) return
    try {
      const r = await authFetch(`${API}/api/v1/clients/sim/partners`)
      const d = await r.json().catch(() => ({}))
      setPartners(d.people || [])
    } catch { /* необязательно */ }
  }, [userId])

  useEffect(() => { if (tab === 'partner') loadPartners() }, [tab, loadPartners])

  const buildGroups = useCallback(async () => {
    if (!userId || market.trim().length < 3) return
    setBusy(true); setError('')
    try {
      const r = await authFetch(`${API}/api/v1/clients/sim/market-groups`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ market, product: productDesc }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') { setGroups((g) => [...g, ...(d.groups || [])]) }
      else setError(d.message || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, market, productDesc, t])

  const deleteGroup = useCallback(async (gid: string) => {
    if (!userId) return
    try {
      await authFetch(`${API}/api/v1/clients/sim/market-groups/delete`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_id: gid }),
      })
      setGroups((g) => g.filter((x) => x.id !== gid))
    } catch { /* ignore */ }
  }, [userId])

  const runOffer = useCallback(async () => {
    if (!userId || offer.trim().length < 10) return
    setBusy(true); setError(''); setPanelResult(null)
    try {
      const r = await authFetch(`${API}/api/v1/clients/sim/offer`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ offer, client_ids: [...selClients], group_ids: [...selGroups] }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') setPanelResult(d)
      else setError(d.message || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, offer, selClients, selGroups, t])

  const sendChat = useCallback(async () => {
    if (!userId || !chatTarget || !chatInput.trim()) return
    const msg = chatInput.trim()
    setChatInput('')
    const hist = [...chatHistory, { role: 'user' as const, text: msg }]
    setChatHistory(hist)
    setBusy(true); setError('')
    try {
      const r = await authFetch(`${API}/api/v1/clients/sim/chat`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          client_id: chatTarget.kind === 'client' ? chatTarget.id : '',
          group_id: chatTarget.kind === 'group' ? chatTarget.id : '',
          message: msg,
          history: chatHistory.map((m) => ({ role: m.role === 'user' ? 'user' : 'sim', text: m.text })),
        }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') setChatHistory([...hist, { role: 'sim', text: d.reply }])
      else setError(d.message || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, chatTarget, chatInput, chatHistory, t])

  const sendPartner = useCallback(async () => {
    if (!userId || !partner || !pInput.trim()) return
    const msg = pInput.trim()
    setPInput('')
    const hist = [...pHistory, { role: 'user' as const, text: msg }]
    setPHistory(hist)
    setBusy(true); setError('')
    try {
      const r = await authFetch(`${API}/api/v1/clients/sim/partner/chat`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          person_id: partner.id, message: msg, mode: pMode,
          history: pHistory.map((m) => ({ role: m.role === 'user' ? 'user' : 'sim', text: m.text })),
        }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') setPHistory([...hist, { role: 'sim', text: d.reply }])
      else { setError(d.message || t('error_failed')); setPHistory(chatUndo(hist)) }
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, partner, pInput, pHistory, pMode, t])

  const buildPack = useCallback(async () => {
    if (!userId || !partner) return
    setBusy(true); setError(''); setPackMd('')
    try {
      const r = await authFetch(`${API}/api/v1/clients/sim/partner/pack`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          person_id: partner.id, focus: packFocus,
          history: pHistory.map((m) => ({ role: m.role === 'user' ? 'user' : 'sim', text: m.text })),
        }),
      })
      const d = await r.json().catch(() => ({}))
      if (d.status === 'success') setPackMd(d.markdown || '')
      else setError(d.message || t('error_failed'))
    } catch (e: any) { setError(e?.message || t('error_network')) }
    setBusy(false)
  }, [userId, partner, packFocus, t])

  const toggle = (set: Set<string>, id: string, fn: (s: Set<string>) => void) => {
    const next = new Set(set)
    if (next.has(id)) next.delete(id); else next.add(id)
    fn(next)
  }

  const chatWindow = (history: ChatMsg[], input: string, setInput: (v: string) => void,
    onSend: () => void, placeholder: string) => (
    <div className="flex flex-col gap-2">
      <div className="max-h-72 overflow-auto flex flex-col gap-2">
        {history.map((m, i) => (
          <div key={i} className={`text-xs rounded-lg p-2 whitespace-pre-wrap ${
            m.role === 'user' ? 'bg-brain-700/60 self-end max-w-[85%]' : 'bg-brain-800/80 self-start max-w-[85%]'}`}>
            {m.text}
          </div>
        ))}
      </div>
      <div className="flex gap-2 items-end">
        <textarea className={`${inputCls} min-h-[3.5rem] resize-y`} value={input}
          placeholder={placeholder}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSend() }
          }} />
        <button className={btnCls} onClick={onSend} disabled={busy || !input.trim()}>
          <MessageCircle size={12} /> {t('send')}
        </button>
      </div>
      <div className="text-[10px] text-brain-600">{t('chat_hint')}</div>
    </div>
  )

  return (
    <div className="flex flex-col gap-3 text-brain-100">
      <div className="flex items-center gap-2 flex-wrap">
        {([
          ['clients', 'tab_clients', Users],
          ['market', 'tab_market', Zap],
          ['offer', 'tab_offer', Zap],
          ['chat', 'tab_chat', MessageCircle],
          ['partner', 'tab_partner', Handshake],
        ] as const).map(([id, labelKey]) => (
          <button key={id} onClick={() => setTab(id)}
            className={`px-3 py-1 rounded-md text-sm ${tab === id ? 'bg-brain-700 text-white' : 'text-brain-400 hover:bg-brain-800/50'}`}>
            {t(labelKey)}
          </button>
        ))}
        <button className={btnCls} onClick={load} disabled={busy} title={t('refresh')} aria-label={t('refresh')}><RefreshCw size={12} /></button>
      </div>
      <div className="text-[11px] text-brain-500">
        {t('disclaimer')}
      </div>
      {error && <div className="text-xs text-red-400">{error}</div>}

      {tab === 'clients' && (
        <div className={box}>
          {clients.length === 0 ? (
            <div className="text-xs text-brain-400">
              {t('clients_empty')}
            </div>
          ) : (
            <>
              <div className="text-xs text-brain-400 mb-2">{t('segments_label')}{' '}
                {segments.map((s) => `${s.name} — ${s.count}`).join(' · ')}</div>
              <div className="grid gap-1">
                {clients.map((c) => (
                  <label key={c.id} className="flex items-center gap-2 text-xs cursor-pointer">
                    <input type="checkbox" checked={selClients.has(c.id)}
                      onChange={() => toggle(selClients, c.id, setSelClients)} />
                    <span className="text-brain-100">{c.name}</span>
                    {c.industry && <span className="text-brain-500">· {c.industry}</span>}
                    {c.status && <span className="text-brain-500">· {c.status}</span>}
                    <button className="text-brain-400 hover:text-brain-200 ml-auto"
                      onClick={(e) => { e.preventDefault(); setChatTarget({ kind: 'client', id: c.id, name: c.name }); setChatHistory([]); setTab('chat') }}>
                      {t('talk_to')}
                    </button>
                  </label>
                ))}
              </div>
              <div className="text-[11px] text-brain-500 mt-2">{t('selected_go_to_offer')}</div>
            </>
          )}
        </div>
      )}

      {tab === 'market' && (
        <div className="flex flex-col gap-3">
          <div className={box}>
            <div className="text-xs text-brain-300 mb-2">{t('market_intro')}</div>
            <div className="flex flex-col gap-2">
              <input className={inputCls} placeholder={t('market_placeholder')}
                value={market} onChange={(e) => setMarket(e.target.value)} />
              <input className={inputCls} placeholder={t('product_placeholder')}
                value={productDesc} onChange={(e) => setProductDesc(e.target.value)} />
              <button className={btnCls} onClick={buildGroups} disabled={busy || market.trim().length < 3}>
                <Zap size={12} /> {t('build_groups')}
              </button>
            </div>
          </div>
          {groups.map((g) => (
            <div key={g.id} className={box}>
              <div className="flex items-center gap-2">
                <input type="checkbox" checked={selGroups.has(g.id)}
                  onChange={() => toggle(selGroups, g.id, setSelGroups)} />
                <span className="text-sm text-brain-100">{g.name}</span>
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300">{t('badge_hypothesis')}</span>
                <span className="text-xs text-brain-500">{t('group_fit', { fit: g.fit_1_5, market: g.market })}</span>
                <button className="ml-auto text-brain-500 hover:text-red-400" onClick={() => deleteGroup(g.id)}
                  title={t('delete_group')} aria-label={t('delete_group')}><Trash2 size={12} /></button>
                <button className="text-brain-400 hover:text-brain-200 text-xs"
                  onClick={() => { setChatTarget({ kind: 'group', id: g.id, name: g.name }); setChatHistory([]); setTab('chat') }}>
                  {t('talk_to')}
                </button>
              </div>
              <div className="text-xs text-brain-300 mt-1">{g.who}</div>
              {g.pains?.length > 0 && <div className="text-xs text-brain-400 mt-1">{t('label_pains', { items: g.pains.join('; ') })}</div>}
              {g.objections?.length > 0 && <div className="text-xs text-brain-400">{t('label_objections', { items: g.objections.join('; ') })}</div>}
              {g.channel && <div className="text-xs text-brain-400">{t('label_channel', { value: g.channel })}</div>}
              {g.validate_by && <div className="text-xs text-emerald-400/80 mt-1">{t('label_validate_by', { value: g.validate_by })}</div>}
            </div>
          ))}
        </div>
      )}

      {tab === 'offer' && (
        <div className="flex flex-col gap-3">
          <div className={box}>
            <div className="text-xs text-brain-300 mb-2">
              {t('panel_summary', { clients: selClients.size, groups: selGroups.size })}
            </div>
            <textarea className={`${inputCls} h-24`} placeholder={t('offer_placeholder')}
              value={offer} onChange={(e) => setOffer(e.target.value)} />
            <button className={`${btnCls} mt-2`} onClick={runOffer}
              disabled={busy || offer.trim().length < 10 || (selClients.size + selGroups.size === 0)}>
              <Zap size={12} /> {t('run_panel')}
            </button>
          </div>
          {panelResult && (
            <div className={box}>
              <div className="text-sm mb-2">{t('avg_interest', { value: panelResult.avg_interest ?? '—' })}</div>
              {(panelResult.reactions || []).map((r: any, i: number) => (
                <div key={i} className="border-t border-brain-800 pt-2 mt-2 text-xs">
                  <div className="text-brain-100">{t('reaction_interest', { name: r.name, value: r.interest_1_5 })}</div>
                  <div className="text-brain-300 mt-1">«{r.first_reaction}»</div>
                  {r.objections?.length > 0 && <div className="text-brain-400 mt-1">{t('label_objections', { items: r.objections.join('; ') })}</div>}
                  {r.open_questions?.length > 0 && <div className="text-amber-400/80 mt-1">{t('label_open_questions', { items: r.open_questions.join('; ') })}</div>}
                  {r.what_would_close && <div className="text-emerald-400/80 mt-1">{t('label_what_would_close', { value: r.what_would_close })}</div>}
                  {r.next_step && <div className="text-brain-400 mt-1">{t('label_next_step', { value: r.next_step })}</div>}
                </div>
              ))}
              {panelResult.skeptic?.weakest_point && (
                <div className="border-t border-brain-800 pt-2 mt-2 text-xs text-red-300">
                  {t('skeptic_weakest_point', { value: panelResult.skeptic.weakest_point })}
                  {panelResult.skeptic.fix_first && <> {t('skeptic_fix_first', { value: panelResult.skeptic.fix_first })}</>}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {tab === 'chat' && (
        <div className={box}>
          {!chatTarget ? (
            <div className="text-xs text-brain-400">{t('chat_pick_target')}</div>
          ) : (
            <>
              <div className="text-xs text-brain-300 mb-2">
                {t('chat_target_label')} <span className="text-brain-100">{chatTarget.name}</span>
                {chatTarget.kind === 'group' && <span className="text-amber-300"> {t('persona_hypothesis')}</span>}
              </div>
              {chatWindow(chatHistory, chatInput, setChatInput, sendChat,
                t('chat_placeholder'))}
            </>
          )}
        </div>
      )}

      {tab === 'partner' && (
        <div className="flex flex-col gap-3">
          <div className={box}>
            <div className="text-xs text-brain-300 mb-2">
              {t('partner_intro')}
            </div>
            {partners.length === 0 && (
              <div className="text-xs text-brain-400 mb-2">
                {t('partners_empty')}
              </div>
            )}
            <select className={inputCls} value={partner?.id || ''}
              onChange={(e) => {
                const p = partners.find((x) => x.id === e.target.value) || null
                setPartner(p); setPHistory([]); setPackMd('')
              }}>
              <option value="">{t('select_person')}</option>
              {partners.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}{p.internal ? ` ${t('employee_suffix')}` : ''}{p.category ? ` · ${p.category}` : ''}{p.role ? ` · ${p.role}` : ''}
                </option>
              ))}
            </select>
          </div>
          {partner && (
            <>
              <div className={box}>
                <div className="flex items-center gap-2 mb-2">
                  {([
                    ['co_create', 'mode_co_create'],
                    ['negotiation', 'mode_negotiation'],
                  ] as const).map(([id, labelKey]) => (
                    <button key={id}
                      onClick={() => { setPMode(id); setPHistory([]) }}
                      className={`px-2 py-1 rounded text-xs ${pMode === id ? 'bg-brain-700 text-white' : 'text-brain-400 hover:bg-brain-800/50'}`}>
                      {t(labelKey)}
                    </button>
                  ))}
                </div>
                <div className="text-xs text-brain-400 mb-2">
                  {pMode === 'co_create'
                    ? t('co_create_desc', { name: partner.name })
                    : t('negotiation_desc', { name: partner.name })}
                </div>
                {chatWindow(pHistory, pInput, setPInput, sendPartner,
                  pMode === 'co_create'
                    ? t('co_create_placeholder')
                    : t('negotiation_placeholder'))}
              </div>
              <div className={box}>
                <div className="text-xs text-brain-300 mb-2">{t('pack_intro')}</div>
                <div className="flex gap-2">
                  <input className={inputCls} placeholder={t('pack_focus_placeholder')}
                    value={packFocus} onChange={(e) => setPackFocus(e.target.value)} />
                  <button className={btnCls} onClick={buildPack} disabled={busy}>
                    <Handshake size={12} /> {t('build_pack')}
                  </button>
                </div>
                {packMd && (
                  <div className="prose prose-invert prose-sm max-w-none mt-3 text-xs">
                    <ReactMarkdown>{packMd}</ReactMarkdown>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// откат последнего сообщения пользователя, если симуляция не ответила
function chatUndo(hist: ChatMsg[]): ChatMsg[] {
  return hist.length && hist[hist.length - 1].role === 'user' ? hist.slice(0, -1) : hist
}
