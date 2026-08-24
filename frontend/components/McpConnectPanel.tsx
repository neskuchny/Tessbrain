'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import { Check, Copy, Link2, Trash2 } from 'lucide-react'

// «Подключение к Claude / Cursor»: человек нажимает кнопку и получает свой
// токен. Раньше токен выпускался ТОЛЬКО командой на сервере
// (python -m mcp_token_store mint <user_id>), то есть подключиться мог лишь
// администратор — и то, зная UUID. Бэкенд: /api/v1/mcp/tokens (+ connect-info).

type TokenRow = { token: string; label?: string; created_at?: number }
type ClientHint = { id: string; name: string; command?: string; config?: any }

function CopyBtn({ text, small }: { text: string; small?: boolean }) {
  const t = useTranslations('mcp_connect_panel')
  const [done, setDone] = useState(false)
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
        } catch {
          // Буфер недоступен (http или отказ в правах) — выделяем текст,
          // чтобы человек скопировал руками, вместо молчаливой неудачи.
          const ta = document.createElement('textarea')
          ta.value = text
          document.body.appendChild(ta)
          ta.select()
          try { document.execCommand('copy') } catch { /* ничего */ }
          document.body.removeChild(ta)
        }
        setDone(true)
        setTimeout(() => setDone(false), 2000)
      }}
      className={`inline-flex items-center gap-1 rounded-lg border border-brain-600 text-brain-300
        hover:text-white hover:bg-brain-800 ${small ? 'px-2 py-1 text-[11px]' : 'px-3 py-1.5 text-xs'}`}
    >
      {done ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
      {done ? t('copied') : t('copy')}
    </button>
  )
}

export default function McpConnectPanel() {
  const t = useTranslations('mcp_connect_panel')
  const [tokens, setTokens] = useState<TokenRow[]>([])
  const [info, setInfo] = useState<{ url?: string; clients?: ClientHint[] }>({})
  const [fresh, setFresh] = useState<string>('')     // показывается один раз
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [open, setOpen] = useState(false)

  const load = useCallback(async () => {
    try {
      const [rt, ri] = await Promise.all([
        authFetch('/api/v1/mcp/tokens'),
        authFetch('/api/v1/mcp/connect-info'),
      ])
      if (rt.ok) setTokens(((await rt.json()) || {}).tokens || [])
      if (ri.ok) setInfo((await ri.json()) || {})
    } catch {
      /* панель необязательная — молча не показываем лишнего */
    }
  }, [])

  useEffect(() => { if (open) load() }, [open, load])

  const mint = async () => {
    setBusy(true); setMsg('')
    try {
      const r = await authFetch('/api/v1/mcp/tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: label || undefined }),
      })
      const d = await r.json().catch(() => ({}))
      if (r.ok && d.token) { setFresh(d.token); setLabel(''); load() }
      else setMsg(d.detail || d.message || t('error_mint_failed'))
    } catch (e: any) {
      setMsg(e?.message || t('error_network'))
    }
    setBusy(false)
  }

  const revoke = async (prefix: string) => {
    if (!confirm(t('confirm_revoke'))) return
    try {
      const r = await authFetch(`/api/v1/mcp/tokens/${encodeURIComponent(prefix)}`,
        { method: 'DELETE' })
      if (r.ok) { setFresh(''); load() }
      else setMsg(t('error_revoke_failed'))
    } catch { setMsg(t('error_network')) }
  }

  const url = info.url || ''
  const cmd = (info.clients || []).find((c) => c.id === 'claude_code')?.command || ''
  const cfg = (info.clients || []).find((c) => c.id === 'cursor')?.config

  return (
    <div className="rounded-xl border border-brain-700/50 overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-3 p-4 bg-brain-800/50 hover:bg-brain-800 transition-colors text-left"
      >
        <div className="p-1.5 bg-brain-700 rounded-lg text-brain-300">
          <Link2 className="w-4 h-4" />
        </div>
        <div className="flex-1">
          <div className="font-medium text-white">{t('title')}</div>
          <div className="text-xs text-brain-400 mt-0.5">
            {t('subtitle')}
          </div>
        </div>
        <span className="text-xs text-brain-500">{tokens.length ? t('tokens_count', { count: tokens.length }) : t('not_connected')}</span>
      </button>

      {open && (
        <div className="p-4 space-y-4 bg-brain-900/40">
          <p className="text-xs text-brain-400 leading-relaxed">
            {t('intro')}
          </p>

          {/* Шаг 1 — выпустить */}
          <div className="rounded-lg border border-brain-700/60 p-3 space-y-2">
            <div className="text-sm font-medium text-brain-100">{t('step1_title')}</div>
            <div className="flex flex-wrap gap-2">
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder={t('label_placeholder')}
                className="flex-1 min-w-[200px] px-2 py-1.5 rounded-lg bg-brain-900 border border-brain-700 text-brain-100 text-xs"
              />
              <button
                onClick={mint}
                disabled={busy}
                className="px-3 py-1.5 rounded-lg bg-blue-600/80 hover:bg-blue-600 text-white text-xs disabled:opacity-40"
              >
                {busy ? t('minting') : t('mint_token')}
              </button>
            </div>
            {msg && <div className="text-[11px] text-amber-300">⚠️ {msg}</div>}
            {fresh && (
              <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 space-y-2">
                <div className="text-[11px] text-emerald-200">
                  {t('copy_now_warning')}
                </div>
                <div className="flex items-center gap-2">
                  <code className="flex-1 break-all text-[11px] text-emerald-100 font-mono">{fresh}</code>
                  <CopyBtn text={fresh} small />
                </div>
              </div>
            )}
          </div>

          {/* Шаг 2 — подключить */}
          {url && (
            <div className="rounded-lg border border-brain-700/60 p-3 space-y-3">
              <div className="text-sm font-medium text-brain-100">{t('step2_title')}</div>

              <div className="space-y-1">
                <div className="text-[11px] text-brain-400">{t('connection_url')}</div>
                <div className="flex items-center gap-2">
                  <code className="flex-1 break-all text-[11px] text-brain-200 font-mono">{url}</code>
                  <CopyBtn text={url} small />
                </div>
              </div>

              {cmd && (
                <div className="space-y-1">
                  <div className="text-[11px] text-brain-400">{t('claude_code_hint')}</div>
                  <div className="flex items-start gap-2">
                    <code className="flex-1 break-all text-[11px] text-brain-200 font-mono whitespace-pre-wrap">
                      {fresh ? cmd.replace('<ВАШ_ТОКЕН>', fresh) : cmd}
                    </code>
                    <CopyBtn text={fresh ? cmd.replace('<ВАШ_ТОКЕН>', fresh) : cmd} small />
                  </div>
                </div>
              )}

              {cfg && (
                <div className="space-y-1">
                  <div className="text-[11px] text-brain-400">
                    {t('cursor_hint')}
                  </div>
                  <div className="flex items-start gap-2">
                    <pre className="flex-1 overflow-x-auto text-[11px] text-brain-200 font-mono">
{JSON.stringify(fresh
  ? JSON.parse(JSON.stringify(cfg).replace('<ВАШ_ТОКЕН>', fresh))
  : cfg, null, 2)}
                    </pre>
                    <CopyBtn
                      text={JSON.stringify(fresh
                        ? JSON.parse(JSON.stringify(cfg).replace('<ВАШ_ТОКЕН>', fresh))
                        : cfg, null, 2)}
                      small
                    />
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Выпущенные */}
          {tokens.length > 0 && (
            <div className="rounded-lg border border-brain-700/60 p-3 space-y-2">
              <div className="text-sm font-medium text-brain-100">{t('issued_tokens')}</div>
              {tokens.map((row) => (
                <div key={row.token} className="flex items-center gap-2 text-xs text-brain-300">
                  <code className="font-mono text-[11px]">{row.token}</code>
                  <span className="text-brain-500">{row.label || ''}</span>
                  {row.created_at ? (
                    <span className="text-brain-600">
                      {new Date(row.created_at * 1000).toLocaleDateString()}
                    </span>
                  ) : null}
                  <button
                    onClick={() => revoke(row.token)}
                    title={t('revoke')}
                    className="ml-auto p-1 rounded hover:bg-brain-800 text-brain-400 hover:text-red-300"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
