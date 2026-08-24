'use client'

/**
 * GuideChatPanel — «Гид по Tessent»: помощник-онбординг.
 *
 * Отдельная лёгкая панель (НЕ трогает память компании): спрашивает у
 * /api/v1/guide/ask по корпусу справки и объясняет, как пользоваться
 * системой. Открывается плавающей кнопкой «?».
 */
import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { X, Send, Loader2, HelpCircle, MessageSquareWarning } from 'lucide-react'
import { useTranslations } from 'next-intl'
import { authHeaders } from '@/lib/authFetch'

interface Msg { role: 'user' | 'assistant'; content: string; sources?: { slug: string; title: string }[] }
type Mode = 'help' | 'feedback'
type Kind = 'problem' | 'wish' | 'confusion'
interface Props {
  userId?: string
  currentTab?: string
  onClose: () => void
  onOpenSection?: (slug: string) => void
}

const KIND_LABEL_KEYS: Record<Kind, string> = {
  problem: 'kind_problem', wish: 'kind_wish', confusion: 'kind_confusion',
}

const SUGGESTION_KEYS = [
  'suggestion_start',
  'suggestion_capabilities',
  'suggestion_meetings',
  'suggestion_contract',
]

export default function GuideChatPanel({ userId, currentTab, onClose, onOpenSection }: Props) {
  const t = useTranslations('guide_chat_panel')
  const [mode, setMode] = useState<Mode>('help')
  const [fbKind, setFbKind] = useState<Kind>('problem')
  const [messages, setMessages] = useState<Msg[]>([{
    role: 'assistant',
    content: t('greeting'),
  }])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [myTickets, setMyTickets] = useState<{ id: string; kind: string; message: string; status: string; resolution: string }[]>([])
  const sessionRef = useRef<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, loading])

  const uidQ = userId ? `?user_id=${encodeURIComponent(userId)}` : ''

  // «Мои обращения» — чтобы клиент видел, что починили, прямо в интерфейсе.
  const loadMyTickets = async () => {
    if (!userId) return
    try {
      const r = await fetch(`/api/v1/guide/feedback/mine${uidQ}`, { headers: authHeaders() })
      if (r.ok) { const d = await r.json(); setMyTickets((d.tickets || []).slice().reverse()) }
    } catch { /* ignore */ }
  }
  useEffect(() => { if (mode === 'feedback') loadMyTickets() }, [mode])

  const sendHelp = async (q: string) => {
    const history = messages.map((m) => ({ role: m.role, content: m.content }))
    const res = await fetch(`/api/v1/guide/ask${uidQ}`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ question: q, session_id: sessionRef.current, history }),
    })
    const d = await res.json().catch(() => ({}))
    sessionRef.current = d.session_id || sessionRef.current
    setMessages((m) => [...m, {
      role: 'assistant',
      content: d.answer || t('answer_failed'),
      sources: d.sources || [],
    }])
  }

  const sendFeedback = async (text: string) => {
    const res = await fetch(`/api/v1/guide/feedback${uidQ}`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ message: text, kind: fbKind, session_id: sessionRef.current, context: { tab: currentTab || '' } }),
    })
    const d = await res.json().catch(() => ({}))
    const forwarded = d.forwarded ? `\n\n${t('forwarded_note')}` : ''
    setMessages((m) => [...m, {
      role: 'assistant',
      content: (d.reply || t('feedback_thanks')) + forwarded,
    }])
    loadMyTickets()
  }

  const send = async (text: string) => {
    const q = text.trim()
    if (!q || loading) return
    setMessages((m) => [...m, { role: 'user', content: q }])
    setInput('')
    setLoading(true)
    try {
      if (mode === 'feedback') await sendFeedback(q)
      else await sendHelp(q)
    } catch {
      setMessages((m) => [...m, { role: 'assistant', content: t('network_error') }])
    } finally { setLoading(false) }
  }

  return (
    <div className="fixed bottom-6 right-6 z-50 w-[min(420px,calc(100vw-2rem))] h-[min(600px,calc(100vh-6rem))] flex flex-col rounded-2xl border border-brain-700 bg-brain-900 shadow-2xl overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-brain-700 bg-brain-800/60">
        <div className="flex items-center gap-2">
          <HelpCircle className="w-5 h-5 text-purple-400" />
          <div>
            <div className="text-sm font-semibold text-white">{t('title')}</div>
            <div className="text-[11px] text-brain-500">{t('subtitle')}</div>
          </div>
        </div>
        <button onClick={onClose} className="p-1.5 rounded-lg text-brain-400 hover:bg-brain-700">
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Режим: Помощь / Обратная связь */}
      <div className="flex gap-1 px-3 pt-2">
        {([['help', 'tab_help', HelpCircle], ['feedback', 'tab_feedback', MessageSquareWarning]] as const).map(([m, labelKey, Icon]) => (
          <button key={m} onClick={() => setMode(m)}
            className={`flex-1 inline-flex items-center justify-center gap-1.5 px-2 py-1.5 rounded-lg text-xs transition-colors ${mode === m ? 'bg-brain-700 text-white' : 'text-brain-400 hover:bg-brain-800'}`}>
            <Icon className="w-3.5 h-3.5" /> {t(labelKey)}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-3">
        {messages.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
            <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm ${m.role === 'user' ? 'bg-purple-600 text-white' : 'bg-brain-800 text-brain-100'}`}>
              {m.role === 'assistant' ? (
                <div className="prose prose-invert prose-sm max-w-none prose-p:my-1 prose-ul:my-1 prose-ol:my-1">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                </div>
              ) : m.content}
              {m.sources && m.sources.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {m.sources.map((s) => (
                    <button key={s.slug}
                      onClick={() => onOpenSection?.(s.slug)}
                      className="text-[11px] px-2 py-0.5 rounded-full bg-brain-700/70 text-brain-300 hover:bg-brain-700 hover:text-white"
                      title={s.slug}>
                      {s.title}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="rounded-2xl px-3 py-2 bg-brain-800 text-brain-400 text-sm inline-flex items-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin" /> {t('thinking')}
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {mode === 'help' && messages.length <= 1 && (
        <div className="px-3 pb-2 flex flex-wrap gap-1.5">
          {SUGGESTION_KEYS.map((s) => (
            <button key={s} onClick={() => send(t(s))}
              className="text-xs px-2.5 py-1 rounded-full border border-brain-700 text-brain-300 hover:bg-brain-800">
              {t(s)}
            </button>
          ))}
        </div>
      )}

      {mode === 'feedback' && (
        <div className="px-3 pb-2 space-y-1.5">
          <div className="flex gap-1.5">
            {(Object.keys(KIND_LABEL_KEYS) as Kind[]).map((k) => (
              <button key={k} onClick={() => setFbKind(k)}
                className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${fbKind === k ? 'border-purple-500 bg-purple-600/20 text-purple-200' : 'border-brain-700 text-brain-400 hover:bg-brain-800'}`}>
                {t(KIND_LABEL_KEYS[k])}
              </button>
            ))}
          </div>
          <p className="text-[11px] text-brain-500">{t('feedback_hint')}</p>
          {myTickets.length > 0 && (
            <div className="max-h-28 overflow-y-auto rounded-lg bg-brain-800/50 border border-brain-700 p-1.5 space-y-1">
              <div className="text-[11px] text-brain-500 px-1">{t('my_tickets')}</div>
              {myTickets.map((tk) => (
                <div key={tk.id} className="flex items-start gap-2 px-1 py-0.5">
                  <span className={`shrink-0 mt-0.5 text-[10px] px-1.5 py-0.5 rounded-full ${tk.status === 'resolved' ? 'bg-green-500/15 text-green-300' : 'bg-amber-500/15 text-amber-300'}`}>
                    {tk.status === 'resolved' ? t('status_resolved') : t('status_in_progress')}
                  </span>
                  <div className="min-w-0">
                    <div className="text-[11px] text-brain-300 truncate">{tk.message}</div>
                    {tk.status === 'resolved' && tk.resolution && (
                      <div className="text-[11px] text-green-300/80">{tk.resolution}</div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="p-3 border-t border-brain-700 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input) } }}
          placeholder={mode === 'feedback' ? t('feedback_placeholder') : t('help_placeholder')}
          className="flex-1 px-3 py-2 rounded-xl bg-brain-800 border border-brain-700 text-brain-100 text-sm focus:outline-none focus:border-purple-500"
        />
        <button onClick={() => send(input)} disabled={loading || !input.trim()}
          className="px-3 rounded-xl bg-purple-600 hover:bg-purple-500 text-white disabled:opacity-40">
          <Send className="w-4 h-4" />
        </button>
      </div>
    </div>
  )
}
