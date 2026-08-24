'use client'

// Документы по шаблону (MeetFlow-перенос): шаблон с {{плейсхолдерами}} или
// эталон стиля + встречи → заполненный документ. Каждое поле помечено:
// ✅ найдено / ⚠️ предположено / ❌ не найдено. Деньги считает код.
import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import {
  FileSignature, Plus, Trash2, RefreshCw, Download, Copy, Building2,
} from 'lucide-react'
import { authHeaders } from '@/lib/authFetch'

interface DocTemplate {
  id: string; title: string; body: string
  mode: 'strict' | 'flexible'; placeholders: string[]
}
interface MeetingSlim { id: string; title?: string; name?: string; date?: string }

export default function FillDocumentsPanel(
  { userId, variant = 'full' }: { userId?: string; variant?: 'full' | 'mediaplan' },
) {
  const t = useTranslations('fill_docs')
  const uq = userId ? `?user_id=${userId}` : ''
  // variant='mediaplan' — только сборка медиаплана (для раздела Mark): прячем
  // шаблоны/реквизиты/заполнение документов, оставляем выбор материалов и
  // сам медиаплан.
  const full = variant === 'full'

  const [templates, setTemplates] = useState<DocTemplate[]>([])
  const [selected, setSelected] = useState<DocTemplate | null>(null)
  const [editTitle, setEditTitle] = useState('')
  const [editBody, setEditBody] = useState('')
  const [editMode, setEditMode] = useState<'strict' | 'flexible'>('strict')
  const [showEditor, setShowEditor] = useState(false)

  const [requisites, setRequisites] = useState<Record<string, string>>({})
  const [showReq, setShowReq] = useState(false)
  const [reqKey, setReqKey] = useState('')
  const [reqVal, setReqVal] = useState('')

  const [meetings, setMeetings] = useState<MeetingSlim[]>([])
  const [meetingSearch, setMeetingSearch] = useState('')
  const [pickedMeetings, setPickedMeetings] = useState<string[]>([])
  const [extraContext, setExtraContext] = useState('')
  const [presets, setPresets] = useState<{ id: string; title: string; text: string }[]>([])
  const [pickedPresets, setPickedPresets] = useState<string[]>([])

  const [busy, setBusy] = useState(false)
  const [mpClient, setMpClient] = useState('')
  const [mpBudget, setMpBudget] = useState('')
  const [mpBusy, setMpBusy] = useState(false)
  const [mpResult, setMpResult] = useState<any | null>(null)
  const [error, setError] = useState('')
  const [result, setResult] = useState<any | null>(null)

  const load = useCallback(async () => {
    try {
      const [tr, rr, pr] = await Promise.all([
        fetch(`/api/v1/documents/fill-templates${uq}`, { headers: authHeaders() }),
        fetch(`/api/v1/documents/requisites${uq}`, { headers: authHeaders() }),
        fetch(`/api/v1/documents/context-presets${uq}`, { headers: authHeaders() }),
      ])
      const td = await tr.json(); const rd = await rr.json(); const pd = await pr.json()
      setTemplates(td.templates || [])
      setRequisites(rd.requisites || {})
      setPresets(pd.presets || [])
    } catch (e) { setError((e as Error).message) }
  }, [uq])
  useEffect(() => { load() }, [load])

  const loadMeetings = useCallback(async () => {
    try {
      const params = new URLSearchParams()
      if (userId) params.set('user_id', userId)
      if (meetingSearch.trim()) params.set('search', meetingSearch.trim())
      params.set('limit', '30')
      const r = await fetch(`/api/v1/meetings-for-extraction?${params.toString()}`,
        { headers: authHeaders() })
      const d = await r.json()
      setMeetings(d.meetings || d.items || [])
    } catch { /* список пуст — не страшно */ }
  }, [userId, meetingSearch])
  useEffect(() => { loadMeetings() }, [loadMeetings])

  async function saveTemplate() {
    if (!editBody.trim()) return
    setBusy(true); setError('')
    try {
      const r = await fetch(`/api/v1/documents/fill-templates${uq}`, {
        method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          template_id: selected?.id, title: editTitle, body: editBody, mode: editMode,
        }),
      })
      const d = await r.json()
      if (d.success === false) setError(d.error)
      else { setShowEditor(false); setSelected(d.template); load() }
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  async function uploadDocxTemplate(f: File | null) {
    if (!f) return
    setBusy(true); setError('')
    try {
      const buf = await f.arrayBuffer()
      const bytes = new Uint8Array(buf)
      let bin = ''
      for (let i = 0; i < bytes.length; i += 0x8000) {
        bin += String.fromCharCode(...Array.from(bytes.subarray(i, i + 0x8000)))
      }
      const r = await fetch(`/api/v1/documents/fill-templates${uq}`, {
        method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ title: f.name.replace(/\.docx$/i, ''), docx_b64: btoa(bin) }),
      })
      const d = await r.json()
      if (d.success === false) setError(d.error)
      else { setSelected(d.template); load() }
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  async function removeTemplate(id: string) {
    if (!confirm(t('delete_confirm'))) return
    await fetch(`/api/v1/documents/fill-templates/${id}${uq}`,
      { method: 'DELETE', headers: authHeaders() })
    if (selected?.id === id) setSelected(null)
    load()
  }

  async function saveReq() {
    if (!reqKey.trim()) return
    const next = { ...requisites, [reqKey.trim()]: reqVal.trim() }
    setRequisites(next); setReqKey(''); setReqVal('')
    await fetch(`/api/v1/documents/requisites${uq}`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ requisites: next }),
    })
  }

  async function removeReq(k: string) {
    const next = { ...requisites }; delete next[k]
    setRequisites(next)
    await fetch(`/api/v1/documents/requisites${uq}`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ requisites: next }),
    })
  }

  async function savePresetFromContext() {
    const text = extraContext.trim()
    if (!text) return
    const title = prompt(t('preset_name_prompt')) || ''
    if (!title.trim()) return
    const r = await fetch(`/api/v1/documents/context-presets${uq}`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ title: title.trim(), text }),
    })
    const d = await r.json()
    if (d.success) { setPresets([...presets.filter(p => p.id !== d.preset.id), d.preset]); setExtraContext('') }
  }

  async function removePreset(id: string) {
    await fetch(`/api/v1/documents/context-presets/${id}${uq}`,
      { method: 'DELETE', headers: authHeaders() })
    setPresets(presets.filter(p => p.id !== id))
    setPickedPresets(pickedPresets.filter(x => x !== id))
  }

  async function fill() {
    if (!selected) return
    setBusy(true); setError(''); setResult(null)
    try {
      const r = await fetch(`/api/v1/documents/fill${uq}`, {
        method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          template_id: selected.id,
          meeting_ids: pickedMeetings,
          extra_context: extraContext || undefined,
          preset_ids: pickedPresets.length ? pickedPresets : undefined,
        }),
      })
      const d = await r.json()
      if (d.success === false) setError(d.error)
      else setResult(d)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  async function buildMediaplan() {
    if (pickedMeetings.length === 0 && pickedPresets.length === 0 && !extraContext.trim()) return
    setMpBusy(true); setError(''); setMpResult(null)
    try {
      const r = await fetch(`/api/v1/documents/mediaplan${uq}`, {
        method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          client: mpClient || undefined,
          meeting_ids: pickedMeetings,
          preset_ids: pickedPresets.length ? pickedPresets : undefined,
          extra_context: extraContext || undefined,
          total_budget: mpBudget ? Number(mpBudget.replace(/\s/g, '')) : undefined,
        }),
      })
      const d = await r.json()
      if (d.success === false) setError(d.error)
      else setMpResult(d)
    } catch (e) { setError((e as Error).message) } finally { setMpBusy(false) }
  }

  async function downloadMediaplanXlsx() {
    if (!mpResult?.table) return
    const r = await fetch(`/api/v1/documents/mediaplan/export.xlsx${uq}`, {
      method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ title: `Медиаплан ${mpClient || ''}`.trim(), table: mpResult.table }),
    })
    if (!r.ok) { setError('export failed'); return }
    const blob = await r.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `mediaplan${mpClient ? '_' + mpClient : ''}.xlsx`
    a.click(); URL.revokeObjectURL(a.href)
  }

  function downloadMd() {
    if (!result?.document) return
    const blob = new Blob([result.document], { type: 'text/markdown;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${selected?.title || 'document'}.md`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  async function exportAs(fmt: 'docx' | 'xlsx' | 'pdf') {
    if (!result?.document) return
    try {
      const r = await fetch(`/api/v1/documents/fill/export${uq}`, {
        method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          title: selected?.title || 'Документ',
          document: result.document,
          format: fmt,
          money: result.money || undefined,
        }),
      })
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        setError(d.error || `export ${fmt} failed`)
        return
      }
      if (fmt === 'pdf') {
        // печатная страница: браузер откроет диалог «Сохранить как PDF»
        const html = await r.text()
        const w = window.open('', '_blank')
        if (w) { w.document.write(html); w.document.close() }
        return
      }
      const blob = await r.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `${selected?.title || 'document'}.${fmt}`
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) { setError((e as Error).message) }
  }

  const confColor = (c: string) =>
    c === 'found' ? 'text-green-300' : c === 'assumed' ? 'text-amber-300' : 'text-red-300'
  const confIcon = (c: string) => (c === 'found' ? '✅' : c === 'assumed' ? '⚠️' : '❌')

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <FileSignature className="w-5 h-5 text-purple-400" />
        <span className="text-sm text-brain-300">{full ? t('intro') : `📊 ${t('mp_title')}`}</span>
        {full && (<>
        <button onClick={() => { setSelected(null); setEditTitle(''); setEditBody(''); setEditMode('strict'); setShowEditor(true) }}
          className="ml-auto flex items-center gap-1 px-3 py-1.5 rounded-lg bg-purple-600 hover:bg-purple-500 text-sm">
          <Plus className="w-4 h-4" />{t('new_template')}
        </button>
        <label className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-brain-600/40 hover:bg-brain-700/50 text-sm cursor-pointer">
          <Plus className="w-4 h-4" />{t('upload_docx')}
          <input type="file" accept=".docx" className="hidden"
            onChange={e => uploadDocxTemplate(e.target.files?.[0] || null)} />
        </label>
        <a href="/api/v1/documents/fill/example-template.docx" download
          className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-dashed border-brain-600/40 hover:bg-brain-700/50 text-sm text-brain-300"
          title={t('example_docx_hint')}>
          <Download className="w-4 h-4" />{t('example_docx')}
        </a>
        <button onClick={() => setShowReq(!showReq)}
          className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-brain-600/40 hover:bg-brain-700/50 text-sm">
          <Building2 className="w-4 h-4" />{t('requisites')} ({Object.keys(requisites).length})
        </button>
        </>)}
      </div>

      {error && <div className="p-2 rounded bg-amber-500/10 border border-amber-500/30 text-amber-300 text-sm">{error}</div>}

      {/* Реквизиты: один раз → во все документы */}
      {full && showReq && (
        <div className="p-3 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-2">
          <p className="text-xs text-brain-400">{t('requisites_hint')}</p>
          {Object.entries(requisites).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2 text-xs">
              <span className="font-medium w-44 truncate">{k}</span>
              <span className="text-brain-300 flex-1 truncate">{v}</span>
              <Trash2 className="w-3.5 h-3.5 text-brain-500 hover:text-red-400 cursor-pointer" onClick={() => removeReq(k)} />
            </div>
          ))}
          <div className="flex items-center gap-2">
            <input value={reqKey} onChange={e => setReqKey(e.target.value)} placeholder={t('req_key')}
              className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs w-44" />
            <input value={reqVal} onChange={e => setReqVal(e.target.value)} placeholder={t('req_val')}
              className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs flex-1" />
            <button onClick={saveReq} className="px-2 py-1 rounded bg-purple-600 hover:bg-purple-500 text-xs">{t('add')}</button>
          </div>
        </div>
      )}

      {/* Редактор шаблона */}
      {full && showEditor && (
        <div className="p-3 rounded-xl border border-purple-500/30 bg-purple-500/5 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <input value={editTitle} onChange={e => setEditTitle(e.target.value)} placeholder={t('tpl_title')}
              className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-sm w-64" />
            <select value={editMode} onChange={e => setEditMode(e.target.value as any)}
              className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs">
              <option value="strict">{t('mode_strict')}</option>
              <option value="flexible">{t('mode_flexible')}</option>
            </select>
            <button onClick={saveTemplate} disabled={busy || !editBody.trim()}
              className="px-3 py-1 rounded bg-purple-600 hover:bg-purple-500 disabled:opacity-40 text-xs">{t('save')}</button>
            <button onClick={() => setShowEditor(false)} className="px-2 py-1 text-xs text-brain-400 hover:text-white">{t('cancel')}</button>
          </div>
          <p className="text-[11px] text-brain-500">{editMode === 'strict' ? t('strict_hint') : t('flexible_hint')}</p>
          <textarea value={editBody} onChange={e => setEditBody(e.target.value)} rows={8}
            placeholder={t('tpl_body_placeholder')}
            className="w-full px-3 py-2 rounded-lg bg-brain-900/40 border border-brain-600/30 text-xs font-mono" />
        </div>
      )}

      {/* Шаблоны */}
      {full && (
      <div className="flex flex-wrap gap-1.5">
        {templates.map(tp => (
          <span key={tp.id} className={
            'flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-lg border cursor-pointer ' +
            (selected?.id === tp.id
              ? 'border-purple-400 text-purple-200 bg-purple-500/10'
              : 'border-brain-600/40 text-brain-300 hover:border-brain-400')
          }>
            <span onClick={() => { setSelected(tp); setResult(null) }}>
              {(tp as any).kind === 'docx' ? '📄 ' : ''}{tp.title} · {(tp as any).kind === 'docx'
                ? (tp.placeholders.length > 0
                    ? t('docx_fields_count', { count: tp.placeholders.length })
                    : `⚠️ ${t('docx_no_fields')}`)
                : tp.mode === 'strict' ? t('docx_fields_count', { count: tp.placeholders.length }) : t('mode_flexible_short')}
            </span>
            <Copy className="w-3 h-3 text-brain-500 hover:text-white"
              onClick={() => { setSelected(tp); setEditTitle(tp.title); setEditBody(tp.body); setEditMode(tp.mode); setShowEditor(true) }} />
            <Trash2 className="w-3 h-3 text-brain-500 hover:text-red-400" onClick={() => removeTemplate(tp.id)} />
          </span>
        ))}
        {templates.length === 0 && !showEditor && (
          <p className="text-sm text-brain-500">{t('empty_templates')}</p>
        )}
      </div>
      )}

      {/* Материалы: встречи + заготовки + контекст (общие для заполнения и медиаплана) */}
      {(!full || templates.length > 0 || meetings.length > 0 || presets.length > 0) && (
        <div className="p-3 rounded-xl border border-brain-600/30 bg-brain-800/30 space-y-2">
          <div className="flex items-center gap-2">
            <input value={meetingSearch} onChange={e => setMeetingSearch(e.target.value)}
              placeholder={t('search_meetings')}
              className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs w-64" />
            <span className="text-[11px] text-brain-500">{t('picked_count', { count: pickedMeetings.length })}</span>
            {full && (
            <button onClick={fill} disabled={busy || !selected || pickedMeetings.length === 0}
              title={!selected ? t('pick_template_hint') : undefined}
              className="ml-auto flex items-center gap-1 px-4 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-sm font-medium">
              {busy ? <RefreshCw className="w-4 h-4 animate-spin" /> : <FileSignature className="w-4 h-4" />}
              {t('fill_button')}
            </button>
            )}
          </div>
          <div className="max-h-40 overflow-y-auto space-y-1">
            {meetings.map(m => {
              const id = m.id
              const on = pickedMeetings.includes(id)
              return (
                <label key={id} className="flex items-center gap-2 text-xs cursor-pointer hover:bg-brain-700/30 rounded px-1 py-0.5">
                  <input type="checkbox" checked={on}
                    onChange={() => setPickedMeetings(on ? pickedMeetings.filter(x => x !== id) : [...pickedMeetings, id])} />
                  <span className="truncate">{m.title || m.name || id}</span>
                  <span className="ml-auto text-brain-500">{String(m.date || '').slice(0, 10)}</span>
                </label>
              )
            })}
          </div>
          {presets.length > 0 && (
            <div className="flex flex-wrap gap-1.5 items-center">
              <span className="text-[11px] text-brain-500">{t('presets_label')}</span>
              {presets.map(p => {
                const on = pickedPresets.includes(p.id)
                return (
                  <span key={p.id} title={p.text.slice(0, 200)}
                    className={'flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border cursor-pointer ' +
                      (on ? 'border-emerald-400 text-emerald-300 bg-emerald-500/10' : 'border-brain-600/40 text-brain-300')}>
                    <span onClick={() => setPickedPresets(on ? pickedPresets.filter(x => x !== p.id) : [...pickedPresets, p.id])}>
                      {on ? '☑' : '☐'} {p.title}
                    </span>
                    <Trash2 className="w-3 h-3 text-brain-500 hover:text-red-400" onClick={() => removePreset(p.id)} />
                  </span>
                )
              })}
            </div>
          )}
          <div className="flex items-start gap-2">
            <textarea value={extraContext} onChange={e => setExtraContext(e.target.value)} rows={2}
              placeholder={t('extra_context')}
              className="flex-1 px-3 py-2 rounded-lg bg-brain-900/40 border border-brain-600/30 text-xs" />
            <button onClick={savePresetFromContext} disabled={!extraContext.trim()}
              title={t('save_preset_hint')}
              className="px-2 py-1.5 rounded border border-brain-600/40 text-[11px] hover:bg-brain-700/50 disabled:opacity-40 whitespace-nowrap">
              {t('save_preset')}
            </button>
          </div>
        </div>
      )}

      {/* Медиаплан: LLM извлекает ставки из материалов, считает КОД */}
      <div className="p-3 rounded-xl border border-sky-500/25 bg-sky-500/5 space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium text-sky-200">📊 {t('mp_title')}</span>
          <input value={mpClient} onChange={e => setMpClient(e.target.value)}
            placeholder={t('mp_client')}
            className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs w-52" />
          <input value={mpBudget} onChange={e => setMpBudget(e.target.value)}
            placeholder={t('mp_budget')}
            className="px-2 py-1 rounded bg-brain-900/40 border border-brain-600/30 text-xs w-36" />
          <button onClick={buildMediaplan}
            disabled={mpBusy || (pickedMeetings.length === 0 && pickedPresets.length === 0 && !extraContext.trim())}
            className="ml-auto flex items-center gap-1 px-3 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-500 disabled:opacity-40 text-sm font-medium">
            {mpBusy ? <RefreshCw className="w-4 h-4 animate-spin" /> : '📊'} {t('mp_build')}
          </button>
        </div>
        <p className="text-[10px] text-slate-500">{t('mp_hint')}</p>

        {mpResult?.table && (
          <div className="space-y-2">
            <div className="overflow-x-auto rounded-lg border border-brain-600/30">
              <table className="text-xs min-w-full">
                <thead>
                  <tr className="bg-brain-900/60 text-slate-400">
                    {mpResult.table.columns.map((c: string) => (
                      <th key={c} className="px-2 py-1.5 text-left whitespace-nowrap font-medium">{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {mpResult.table.rows.map((r: any, i: number) => (
                    <tr key={i} className="border-t border-brain-700/30">
                      {mpResult.table.columns.map((c: string) => (
                        <td key={c} className="px-2 py-1 whitespace-nowrap text-slate-200"
                            style={{ fontVariantNumeric: 'tabular-nums' }}>{r[c]}</td>
                      ))}
                    </tr>
                  ))}
                  <tr className="border-t-2 border-brain-500/40 bg-brain-900/40 font-semibold">
                    {mpResult.table.columns.map((c: string) => (
                      <td key={c} className="px-2 py-1 whitespace-nowrap text-white"
                          style={{ fontVariantNumeric: 'tabular-nums' }}>{mpResult.table.totals[c]}</td>
                    ))}
                  </tr>
                </tbody>
              </table>
            </div>
            {mpResult.rationale && (
              <p className="text-xs text-slate-300"><span className="text-sky-300 font-medium">{t('mp_why')}:</span> {mpResult.rationale}</p>
            )}
            {(mpResult.assumptions || []).map((a: string, i: number) => (
              <p key={i} className="text-[11px] text-amber-300/90">⚠️ {a}</p>
            ))}
            {(mpResult.table.notes || []).map((n: string, i: number) => (
              <p key={i} className="text-[11px] text-slate-400">• {n}</p>
            ))}
            <div className="flex flex-wrap gap-1.5 items-center">
              {(mpResult.channels_meta || []).map((c: any, i: number) => {
                const o = c.rate_origin || 'assumed'
                const icon = o === 'client_data' ? '🎯' : o === 'methodology' ? '📐' : o === 'adjusted_benchmark' ? '🔧' : o === 'benchmark_asis' ? '📋' : o === 'missing' ? '❌' : '⚠️'
                const color = o === 'client_data' ? 'text-green-300' : o === 'methodology' ? 'text-emerald-300' : o === 'adjusted_benchmark' ? 'text-sky-300' : o === 'missing' ? 'text-red-300' : 'text-amber-300'
                const tip = [
                  c.why,
                  o === 'methodology' ? t('origin_methodology_tip') : '',
                  o === 'adjusted_benchmark' && c.baseline ? t('benchmark_tip', { value: c.baseline }) : '',
                  c.adjustment_reason ? t('adjustment_tip', { reason: c.adjustment_reason }) : '',
                  o === 'benchmark_asis' ? t('benchmark_asis_tip') : '',
                  c.source,
                ].filter(Boolean).join('\n')
                return (
                  <span key={i} title={tip}
                    className={`text-[10px] px-1.5 py-0.5 rounded border border-brain-600/40 ${color}`}>
                    {icon} {c.name}
                  </span>
                )
              })}
              <span className="text-[9px] text-slate-600">{t('origin_legend')}</span>
              <button onClick={downloadMediaplanXlsx}
                className="ml-auto flex items-center gap-1 px-2 py-1 rounded bg-emerald-600 hover:bg-emerald-500 text-xs font-medium">
                <Download className="w-3.5 h-3.5" />Excel
              </button>
              <button onClick={async () => {
                const r = await fetch(`/api/v1/documents/mediaplan/export.gsheet${uq}`, {
                  method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
                  body: JSON.stringify({ title: `Медиаплан ${mpClient || ''}`.trim(), table: mpResult.table }),
                })
                const d = await r.json()
                if (d.success && d.url) window.open(d.url, '_blank')
                else setError(d.error || 'gsheet export failed')
              }}
                className="flex items-center gap-1 px-2 py-1 rounded border border-brain-600/40 text-xs hover:bg-brain-700/50">
                → Google Sheets
              </button>
              {mpResult.rationale && (
                <button onClick={async () => {
                  const parts = [`Медиаплан: ${mpClient || ''}`, '', 'ОБОСНОВАНИЕ', mpResult.rationale, '']
                  ;(mpResult.assumptions || []).forEach((a: string) => parts.push(`Предположение: ${a}`))
                  ;(mpResult.channels_meta || []).forEach((c: any) => {
                    if (c.adjustment_reason) parts.push(`${c.name}: ${c.adjustment_reason}`)
                  })
                  const r = await fetch(`/api/v1/documents/mediaplan/export.gdoc${uq}`, {
                    method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify({ title: `Обоснование медиаплана ${mpClient || ''}`.trim(), text: parts.join('\n') }),
                  })
                  const d = await r.json()
                  if (d.success && d.url) window.open(d.url, '_blank')
                  else setError(d.error || 'gdoc export failed')
                }}
                  className="flex items-center gap-1 px-2 py-1 rounded border border-brain-600/40 text-xs hover:bg-brain-700/50">
                  → Google Docs
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Результат */}
      {full && result && (
        <div className="p-3 rounded-xl border border-emerald-500/30 bg-emerald-500/5 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium text-emerald-200">{t('result_title')}</span>
            {(result.unfilled || []).length > 0 && (
              <span className="text-[11px] text-red-300">{t('unfilled_note', { count: result.unfilled.length })}</span>
            )}
            <button onClick={() => navigator.clipboard.writeText(result.document)}
              className="ml-auto flex items-center gap-1 px-2 py-1 rounded border border-brain-600/40 text-xs hover:bg-brain-700/50">
              <Copy className="w-3.5 h-3.5" />{t('copy')}
            </button>
            {result.docx_b64 && (
              <button onClick={() => {
                const bin = atob(result.docx_b64)
                const bytes = new Uint8Array(bin.length)
                for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
                const blob = new Blob([bytes], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })
                const a = document.createElement('a')
                a.href = URL.createObjectURL(blob)
                a.download = `${selected?.title || 'document'}.docx`
                a.click(); URL.revokeObjectURL(a.href)
              }}
                className="flex items-center gap-1 px-2 py-1 rounded bg-emerald-600 hover:bg-emerald-500 text-xs font-medium">
                <Download className="w-3.5 h-3.5" />{t('filled_docx')}
              </button>
            )}
            {!result.docx_b64 && <button onClick={() => exportAs('docx')}
              className="flex items-center gap-1 px-2 py-1 rounded border border-brain-600/40 text-xs hover:bg-brain-700/50">
              <Download className="w-3.5 h-3.5" />Word
            </button>}
            {result.money?.lines?.length > 0 && (
              <button onClick={() => exportAs('xlsx')}
                className="flex items-center gap-1 px-2 py-1 rounded border border-brain-600/40 text-xs hover:bg-brain-700/50">
                <Download className="w-3.5 h-3.5" />Excel
              </button>
            )}
            <button onClick={() => exportAs('pdf')}
              className="flex items-center gap-1 px-2 py-1 rounded border border-brain-600/40 text-xs hover:bg-brain-700/50">
              <Download className="w-3.5 h-3.5" />PDF
            </button>
            <button onClick={downloadMd}
              className="flex items-center gap-1 px-2 py-1 rounded border border-brain-600/40 text-xs hover:bg-brain-700/50">
              <Download className="w-3.5 h-3.5" />.md
            </button>
          </div>
          {Object.keys(result.fields || {}).length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(result.fields).map(([name, f]: [string, any]) => (
                <span key={name} title={f.source || ''}
                  className={`text-[11px] px-2 py-0.5 rounded border border-brain-600/40 ${confColor(f.confidence)}`}>
                  {confIcon(f.confidence)} {name}{f.value ? `: ${String(f.value).slice(0, 30)}` : ''}
                </span>
              ))}
            </div>
          )}
          {result.money && (
            <p className="text-[11px] text-brain-300">
              💰 {t('money_note')}: {result.money.total} ({result.money.total_in_words})
            </p>
          )}
          <pre className="text-xs text-gray-100 whitespace-pre-wrap max-h-96 overflow-y-auto p-2 rounded bg-brain-900/40">{result.document}</pre>
        </div>
      )}
    </div>
  )
}
