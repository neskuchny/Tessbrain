'use client'

// Ресурсы компании: сохранённые ссылки (сайт, прайс, шаблоны, страницы).
// Хранятся per-user (/data-bus/resources) и автоматически подкладываются
// системе в контекст генерации ТЗ и готовых документов (КП и т.п.).
import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { Link2, Plus, Trash2, Loader2 } from 'lucide-react'
import { authFetch } from '@/lib/authFetch'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''

interface Resource {
  id: string
  url: string
  title?: string
  note?: string
  kind?: string
  created_at?: string
}

const KIND_LABEL: Record<string, string> = {
  link: '🔗', price: '💰', template: '📄', page: '🌐', doc: '📑',
}

export default function ResourcesPanel({ userId }: { userId?: string | null }) {
  const t = useTranslations('resources_panel')
  const [items, setItems] = useState<Resource[]>([])
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)

  const load = useCallback(async () => {
    if (!userId) return
    try {
      const r = await authFetch(`${API_BASE}/api/v1/data-bus/resources?user_id=${encodeURIComponent(userId)}`)
      if (r.ok) {
        const d = await r.json()
        setItems(Array.isArray(d.resources) ? d.resources : [])
      }
    } catch { /* тихо */ }
  }, [userId])

  useEffect(() => { load() }, [load])

  const add = async () => {
    if (!userId || !url.trim()) return
    setBusy(true)
    try {
      const r = await authFetch(`${API_BASE}/api/v1/data-bus/resources?user_id=${encodeURIComponent(userId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url.trim(), title: title.trim(), note: note.trim() }),
      })
      const d = await r.json().catch(() => ({}))
      if (Array.isArray(d.resources)) setItems(d.resources)
      setUrl(''); setTitle(''); setNote('')
    } catch { /* тихо */ } finally { setBusy(false) }
  }

  const del = async (id: string) => {
    if (!userId) return
    try {
      const r = await authFetch(
        `${API_BASE}/api/v1/data-bus/resources/${id}/delete?user_id=${encodeURIComponent(userId)}`,
        { method: 'POST' })
      const d = await r.json().catch(() => ({}))
      if (Array.isArray(d.resources)) setItems(d.resources)
    } catch { /* тихо */ }
  }

  if (!userId) return null
  return (
    <div className="border border-brain-700/50 rounded-xl bg-brain-900/40">
      <button onClick={() => setOpen(!open)}
              className="w-full flex items-center gap-2 px-4 py-3 text-left">
        <Link2 className="w-4 h-4 text-sky-400" />
        <span className="text-sm font-semibold text-white">{t('title')}</span>
        <span className="text-[11px] text-slate-500">
          {t('subtitle')}
        </span>
        <span className="ml-auto text-[11px] font-mono text-sky-300 bg-sky-500/10 px-2 py-0.5 rounded-full">
          {items.length}
        </span>
        <span className="text-slate-500 text-xs">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-3">
          {/* добавление */}
          <div className="flex flex-wrap gap-2">
            <input value={url} onChange={(e) => setUrl(e.target.value)}
                   placeholder={t('url_placeholder')}
                   className="flex-1 min-w-[220px] bg-brain-800 border border-brain-700 rounded-lg px-3 py-1.5 text-sm text-white placeholder:text-slate-600 outline-none focus:border-sky-500" />
            <input value={title} onChange={(e) => setTitle(e.target.value)}
                   placeholder={t('name_placeholder')}
                   className="w-40 bg-brain-800 border border-brain-700 rounded-lg px-3 py-1.5 text-sm text-white placeholder:text-slate-600 outline-none focus:border-sky-500" />
            <input value={note} onChange={(e) => setNote(e.target.value)}
                   placeholder={t('note_placeholder')}
                   className="w-52 bg-brain-800 border border-brain-700 rounded-lg px-3 py-1.5 text-sm text-white placeholder:text-slate-600 outline-none focus:border-sky-500" />
            <button onClick={add} disabled={busy || !url.trim()}
                    className="px-3 py-1.5 rounded-lg bg-sky-600/80 hover:bg-sky-600 text-white text-sm disabled:opacity-40 inline-flex items-center gap-1">
              {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />} {t('save_button')}
            </button>
          </div>

          {/* список */}
          {items.length === 0 ? (
            <div className="text-[12px] text-slate-500 italic">
              {t('empty_state')}
            </div>
          ) : (
            <div className="space-y-1">
              {items.map((it) => (
                <div key={it.id}
                     className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 bg-brain-800/50 border border-brain-700/40 hover:border-brain-600 transition-colors">
                  <span className="text-sm flex-none">{KIND_LABEL[it.kind || 'link'] || '🔗'}</span>
                  <a href={it.url} target="_blank" rel="noreferrer"
                     className="text-[13px] text-sky-300 hover:text-sky-200 hover:underline truncate max-w-[38%]"
                     title={it.url}>
                    {it.title || it.url}
                  </a>
                  {it.note && <span className="text-[11px] text-slate-500 truncate flex-1">{it.note}</span>}
                  <button onClick={() => del(it.id)}
                          className="ml-auto flex-none text-slate-600 hover:text-rose-400 transition-colors"
                          title={t('delete_title')}>
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
