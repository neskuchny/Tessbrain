'use client'

import { useState } from 'react'
import { useTranslations } from 'next-intl'

// Редактор методики оценки (AnalysisPlaybook): клиент со своей методологией
// заводит параметры, формулы и нормы кликами. Сохраняется как playbook,
// потом переиспользуется в панели «Анализ» N раз.

interface Props {
  userId?: string | null
  onClose: () => void
  onSaved: (pb: { id: string; name: string }) => void
  // Начальный режим: собрать вручную ('manual') или загрузить методику
  // документом/текстом ('import'). По умолчанию — вручную (как раньше).
  initialMode?: 'manual' | 'import'
  // Уже загруженные в панель «Анализ» документы — можно выбрать один как
  // источник методологии (система разберёт его в параметры разбора).
  uploadedDocs?: Array<{ id: string; title: string }>
}

interface Dim {
  key: string
  label: string
  kind: 'quantitative' | 'qualitative'
  extraction_hint: string
  rationale: string
  computation: string
  inputs: string
  benchDir: 'none' | 'higher_better' | 'higher_worse'
  red_flag: string
  warning: string
}

function authHeader(): Record<string, string> {
  if (typeof window === 'undefined') return {}
  const token = localStorage.getItem('tessent_access_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

const emptyDim = (): Dim => ({
  key: '', label: '', kind: 'qualitative', extraction_hint: '', rationale: '',
  computation: '', inputs: '', benchDir: 'none', red_flag: '', warning: '',
})

export default function MethodologyEditor({ userId, onClose, onSaved, initialMode = 'manual', uploadedDocs = [] }: Props) {
  const t = useTranslations('methodology_editor')
  const [mode, setMode] = useState<'manual' | 'import'>(initialMode)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [useSnapshot, setUseSnapshot] = useState(true)
  const [reverseAnalysis, setReverseAnalysis] = useState(false)
  const [custom, setCustom] = useState('')
  const [dims, setDims] = useState<Dim[]>([emptyDim()])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Режим «загрузить методику»: текст методологии или ссылка на документ.
  const [importText, setImportText] = useState('')
  const [importDocId, setImportDocId] = useState('')
  const [importWarnings, setImportWarnings] = useState<string[]>([])

  // Загрузка методики документом/текстом: бэкенд сам разложит её на параметры
  // разбора (from-document). Не распозналось идеально → сохранится черновиком.
  const importMethodology = async () => {
    setError(null); setImportWarnings([])
    const text = importText.trim()
    if (!text && !importDocId) { setError(t('import_source_required')); return }
    setSaving(true)
    try {
      const body: any = { name: name.trim() || undefined }
      if (importDocId) body.document_id = importDocId
      else body.text = text
      const r = await fetch('/api/v1/analysis/playbooks/from-document', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify(body),
      })
      const data = await r.json()
      if (data?.id) {
        if (Array.isArray(data.import_warnings) && data.import_warnings.length) {
          // Сохранилось, но с оговорками — покажем и всё равно добавим методику.
          setImportWarnings(data.import_warnings)
        }
        onSaved({ id: data.id, name: data.name || name || t('import_default_name') })
        onClose()
      } else if (data?.detail) {
        setError(typeof data.detail === 'string' ? data.detail : t('import_failed'))
      } else {
        setError(data?.message || t('import_failed'))
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const setDim = (i: number, patch: Partial<Dim>) =>
    setDims((d) => d.map((x, j) => (j === i ? { ...x, ...patch } : x)))

  const save = async () => {
    setError(null)
    if (!name.trim()) { setError(t('name_required')); return }
    const cleanDims = dims.filter((d) => d.key.trim() && d.label.trim())
    if (!cleanDims.length) { setError(t('dim_required')); return }
    setSaving(true)
    try {
      const payload: any = {
        name: name.trim(),
        description: description.trim(),
        analysis_type: 'custom',
        company_context: { use_snapshot: useSnapshot, reverse_analysis: reverseAnalysis, custom: custom.trim() || undefined },
        dimensions: cleanDims.map((d) => {
          const dim: any = {
            key: d.key.trim(), label: d.label.trim(), kind: d.kind,
            extraction_hint: d.extraction_hint.trim() || d.label.trim(),
            rationale: d.rationale.trim() || undefined,
          }
          if (d.kind === 'quantitative' && d.computation.trim()) {
            dim.computation = d.computation.trim()
            dim.inputs = d.inputs.split(',').map((s) => s.trim()).filter(Boolean)
          }
          if (d.benchDir !== 'none') {
            // Порог 0 — валиден; `parseFloat(x)||0` терял бы различие «нет
            // порога» vs «порог=0». Пустое поле → не отправляем ключ.
            const num = (v: string) => { const n = parseFloat(v); return v.trim() !== '' && Number.isFinite(n) ? n : undefined }
            dim.benchmark = { direction: d.benchDir }
            const rf = num(d.red_flag); const wn = num(d.warning)
            if (rf !== undefined) dim.benchmark.red_flag = rf
            if (wn !== undefined) dim.benchmark.warning = wn
          }
          return dim
        }),
      }
      const r = await fetch('/api/v1/analysis/playbooks', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify(payload),
      })
      const data = await r.json()
      if (data?.id) {
        onSaved({ id: data.id, name: data.name || name })
        onClose()
      } else if (data?.detail?.validation_errors) {
        setError(t('validation_errors', { errors: data.detail.validation_errors.join('; ') }))
      } else {
        setError(data?.message || data?.detail || t('save_failed'))
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const inp = 'w-full px-2 py-1 bg-brain-800/40 border border-brain-600/40 rounded text-white text-xs'

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-brain-900/40 border border-brain-600/30 rounded-xl w-full max-w-3xl max-h-[88vh] overflow-y-auto p-4"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-white font-semibold">{mode === 'import' ? t('import_title') : t('title')}</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-white">✕</button>
        </div>

        {/* Переключатель: собрать вручную ↔ загрузить методику документом */}
        <div className="flex gap-1.5 mb-3">
          <button onClick={() => { setMode('manual'); setError(null) }}
                  className={`text-xs px-3 py-1.5 rounded border ${mode === 'manual' ? 'bg-cyan-600/30 border-cyan-500/50 text-cyan-200' : 'bg-brain-800/40 border-brain-600/40 text-slate-300 hover:bg-brain-700/50'}`}>
            {t('mode_manual')}
          </button>
          <button onClick={() => { setMode('import'); setError(null) }}
                  className={`text-xs px-3 py-1.5 rounded border ${mode === 'import' ? 'bg-cyan-600/30 border-cyan-500/50 text-cyan-200' : 'bg-brain-800/40 border-brain-600/40 text-slate-300 hover:bg-brain-700/50'}`}>
            {t('mode_import')}
          </button>
        </div>

        {mode === 'import' ? (
          <div className="space-y-2">
            <p className="text-xs text-slate-400">{t('import_hint')}</p>
            <input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={t('import_name_placeholder')} className={inp} />
            {uploadedDocs.length > 0 && (
              <select value={importDocId} onChange={(e) => setImportDocId(e.target.value)} className={inp}>
                <option value="">{t('import_pick_doc')}</option>
                {uploadedDocs.map((d) => <option key={d.id} value={d.id}>{d.title}</option>)}
              </select>
            )}
            <textarea value={importText} onChange={(e) => setImportText(e.target.value)}
                      rows={10} disabled={!!importDocId}
                      placeholder={t('import_text_placeholder')}
                      className={inp + ' disabled:opacity-40'} />
            {importDocId && <p className="text-[11px] text-slate-500">{t('import_doc_selected')}</p>}
            {importWarnings.length > 0 && (
              <div className="text-[11px] text-amber-300/90 space-y-0.5">
                {importWarnings.map((w, i) => <div key={i}>⚠️ {w}</div>)}
              </div>
            )}
            {error && <div className="text-xs text-red-400">{error}</div>}
            <div className="flex justify-end gap-2 pt-1">
              <button onClick={onClose} className="px-3 py-1.5 bg-brain-700/40 hover:bg-brain-600/60 text-white rounded text-sm">{t('cancel')}</button>
              <button onClick={importMethodology} disabled={saving}
                      className="px-4 py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white rounded text-sm">
                {saving ? t('import_parsing') : t('import_button')}
              </button>
            </div>
          </div>
        ) : (
        <>
        <div className="space-y-2 mb-4">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t('name_placeholder')} className={inp} />
          <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t('description_placeholder')} className={inp} />
          <label className="flex items-center gap-2 text-xs text-slate-300">
            <input type="checkbox" checked={useSnapshot} onChange={(e) => setUseSnapshot(e.target.checked)} />
            {t('use_snapshot_label')}
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-300" title={t('reverse_analysis_title')}>
            <input type="checkbox" checked={reverseAnalysis} onChange={(e) => setReverseAnalysis(e.target.checked)} />
            {t('reverse_analysis_label')}
          </label>
          <textarea value={custom} onChange={(e) => setCustom(e.target.value)} rows={2}
                    placeholder={t('custom_placeholder')} className={inp} />
        </div>

        <div className="text-xs text-slate-400 mb-1">{t('dims_heading')}</div>
        <div className="space-y-3">
          {dims.map((d, i) => (
            <div key={i} className="border border-brain-600/20 rounded p-2 space-y-1.5">
              <div className="flex gap-1.5">
                <input value={d.key} onChange={(e) => setDim(i, { key: e.target.value })} placeholder="key (debt_load)" className={inp} />
                <input value={d.label} onChange={(e) => setDim(i, { label: e.target.value })} placeholder={t('dim_label_placeholder')} className={inp} />
                <select value={d.kind} onChange={(e) => setDim(i, { kind: e.target.value as Dim['kind'] })} className={inp + ' w-40'}>
                  <option value="qualitative">{t('kind_qualitative')}</option>
                  <option value="quantitative">{t('kind_quantitative')}</option>
                </select>
              </div>
              <input value={d.extraction_hint} onChange={(e) => setDim(i, { extraction_hint: e.target.value })} placeholder={t('extraction_hint_placeholder')} className={inp} />
              <input value={d.rationale} onChange={(e) => setDim(i, { rationale: e.target.value })} placeholder={t('rationale_placeholder')} className={inp} />
              {d.kind === 'quantitative' && (
                <div className="flex gap-1.5">
                  <input value={d.computation} onChange={(e) => setDim(i, { computation: e.target.value })} placeholder={t('computation_placeholder')} className={inp} />
                  <input value={d.inputs} onChange={(e) => setDim(i, { inputs: e.target.value })} placeholder={t('inputs_placeholder')} className={inp} />
                </div>
              )}
              <div className="flex gap-1.5 items-center">
                <select value={d.benchDir} onChange={(e) => setDim(i, { benchDir: e.target.value as Dim['benchDir'] })} className={inp + ' w-44'}>
                  <option value="none">{t('bench_none')}</option>
                  <option value="higher_better">{t('bench_higher_better')}</option>
                  <option value="higher_worse">{t('bench_higher_worse')}</option>
                </select>
                {d.benchDir !== 'none' && (
                  <>
                    <input value={d.red_flag} onChange={(e) => setDim(i, { red_flag: e.target.value })} placeholder={t('red_flag_placeholder')} className={inp + ' w-32'} />
                    <input value={d.warning} onChange={(e) => setDim(i, { warning: e.target.value })} placeholder={t('warning_placeholder')} className={inp + ' w-32'} />
                  </>
                )}
                {dims.length > 1 && (
                  <button onClick={() => setDims((x) => x.filter((_, j) => j !== i))} className="text-red-400 hover:text-red-300 text-xs ml-auto">{t('remove_dim')}</button>
                )}
              </div>
            </div>
          ))}
        </div>
        <button onClick={() => setDims((d) => [...d, emptyDim()])} className="text-xs text-cyan-300 hover:text-cyan-200 mt-2">{t('add_dim')}</button>

        {error && <div className="text-xs text-red-400 mt-2">{error}</div>}
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="px-3 py-1.5 bg-brain-700/40 hover:bg-brain-600/60 text-white rounded text-sm">{t('cancel')}</button>
          <button onClick={save} disabled={saving} className="px-4 py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white rounded text-sm">
            {saving ? t('saving') : t('save')}
          </button>
        </div>
        </>
        )}
      </div>
    </div>
  )
}
