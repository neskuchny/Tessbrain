'use client';

import { useState, useEffect, useCallback } from 'react';
import { useTranslations } from 'next-intl';
import {
  Share2, Plus, Trash2, Copy, Check, X, KeyRound,
  ArrowDownToLine, Eye, AlertCircle, ScrollText, Users, HelpCircle,
} from 'lucide-react';
import SetupGuide from './SetupGuide';

interface Consumer {
  id: string;
  name: string;
  consumer_type: string;
  contact_email?: string;
  api_key_prefix?: string;
  is_active: boolean;
  allow_ingest?: boolean;
}

interface AuditEntry {
  id: string;
  timestamp: string;
  consumer_name: string;
  query_type: string;
  query_text?: string;
  results_count: number;
  status_code: number;
  error?: string;
}

interface PolicyTemplate {
  name: string;
  description: string;
}

interface DataBusPanelProps {
  userId: string | null;
  isOpen: boolean;
  onClose: () => void;
}

const CONSUMER_TYPES = [
  'partner', 'investor', 'contractor', 'ai_agent',
  'internal_dept', 'auditor', 'consultant', 'hr_partner',
];

const API = '/api/v1/data-bus/admin';

export default function DataBusPanel({ userId, isOpen, onClose }: DataBusPanelProps) {
  const t = useTranslations('data_bus');
  const [showGuide, setShowGuide] = useState(false);
  const [tab, setTab] = useState<'consumers' | 'audit'>('consumers');
  const [consumers, setConsumers] = useState<Consumer[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [templates, setTemplates] = useState<Record<string, PolicyTemplate>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [ingestOnly, setIngestOnly] = useState(false);

  const emptyForm = {
    name: '', consumer_type: 'partner', contact_email: '',
    policy_template: 'partner_public', allow_ingest: false,
    ingest_max_items_per_request: 100,
    allowed_meeting_ids: [] as string[],
    allowed_folders: '' as string,
    answer_prompt: '',
    allow_deep_search: false,
    max_queries_per_day: '' as string,
    expires_days: '' as string,
  };
  const [form, setForm] = useState(emptyForm);
  // Срез данных: список встреч для мультивыбора (тот же источник, что
  // фильтры чата) + строка поиска.
  const [meetings, setMeetings] = useState<Array<{ id: string; title: string; date?: string; project_id?: string }>>([]);
  const [meetingSearch, setMeetingSearch] = useState('');
  // Каскад выбора среза: проект → папки → встречи.
  const [projects, setProjects] = useState<Array<{ id: string; name: string }>>([]);
  const [folders, setFolders] = useState<Array<{ id: string; name: string; project_id?: string }>>([]);
  const [sliceProjectId, setSliceProjectId] = useState<string>('');

  const load = useCallback(async () => {
    if (!userId) return;
    setLoading(true);
    setError(null);
    try {
      const [cRes, tRes, aRes] = await Promise.all([
        fetch(`${API}/consumers?user_id=${userId}`),
        fetch(`${API}/policies`),
        fetch(`${API}/audit?user_id=${userId}&limit=100`),
      ]);
      if (cRes.ok) setConsumers((await cRes.json()).consumers || []);
      if (tRes.ok) setTemplates((await tRes.json()).templates || {});
      if (aRes.ok) setAudit((await aRes.json()).entries || []);
      if (!cRes.ok) setError(t('err_load'));
    } catch {
      setError(t('err_load'));
    } finally {
      setLoading(false);
    }
  }, [userId, t]);

  useEffect(() => {
    if (isOpen && userId) load();
  }, [isOpen, userId, load]);

  // Проекты и папки для каскада среза (тот же источник, что синхронизация).
  useEffect(() => {
    if (!isOpen || !userId) return;
    fetch(`/api/v1/meetflow/projects?user_id=${userId}`)
      .then((r) => (r.ok ? r.json() : { projects: [] }))
      .then((d) => setProjects((d.projects || []).map((p: any) => ({
        id: String(p.id), name: p.name || p.title || t('fallback_project'),
      }))))
      .catch(() => {});
    fetch(`/api/v1/meetflow/folders?user_id=${userId}`)
      .then((r) => (r.ok ? r.json() : { folders: [] }))
      .then((d) => setFolders((d.folders || []).map((f: any) => ({
        id: String(f.id), name: f.name || f.title || t('fallback_folder'),
        project_id: f.project_id ? String(f.project_id) : '',
      }))))
      .catch(() => {});
  }, [isOpen, userId]);

  // Встречи для среза — перезагружаются при смене проекта (фильтр по проекту).
  useEffect(() => {
    if (!isOpen || !userId) return;
    const pj = sliceProjectId ? `&project_id=${sliceProjectId}` : '';
    fetch(`/api/v1/meetflow/meetings?user_id=${userId}&limit=1000${pj}`)
      .then((r) => (r.ok ? r.json() : { meetings: [] }))
      .then((d) =>
        setMeetings((d.meetings || []).map((m: any) => ({
          id: String(m.id), title: m.title || t('fallback_untitled'),
          date: m.date || m.created_at || '', project_id: m.project_id ? String(m.project_id) : '',
        })))
      )
      .catch(() => {});
  }, [isOpen, userId, sliceProjectId]);

  const createConsumer = async () => {
    if (!userId || !form.name.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const payload: any = {
        ...form,
        allowed_folders: form.allowed_folders
          .split(',').map((s) => s.trim()).filter(Boolean),
        allowed_meeting_ids: form.allowed_meeting_ids,
        answer_prompt: form.answer_prompt.trim(),
        max_queries_per_day: form.max_queries_per_day ? Number(form.max_queries_per_day) : null,
      };
      delete payload.expires_days;
      if (form.expires_days && Number(form.expires_days) > 0) {
        payload.expires_at = new Date(Date.now() + Number(form.expires_days) * 86400000).toISOString();
      }
      const res = await fetch(`${API}/consumers?user_id=${userId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (res.ok && data.api_key) {
        setCreatedKey(data.api_key);
        setForm(emptyForm);
        setMeetingSearch('');
        await load();
      } else {
        setError(data.error || t('err_load'));
      }
    } catch {
      setError(t('err_load'));
    } finally {
      setCreating(false);
    }
  };

  const deactivate = async (id: string) => {
    if (!userId || !window.confirm(t('confirm_deactivate'))) return;
    await fetch(`${API}/consumers/${id}/deactivate?user_id=${userId}`, { method: 'POST' });
    await load();
  };

  const copyKey = () => {
    if (!createdKey) return;
    navigator.clipboard?.writeText(createdKey);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };

  const typeLabel = (ty: string) => {
    const k = `type_${ty}`;
    const v = t(k);
    return v === k ? ty : v;
  };

  if (!isOpen) return null;

  const shownAudit = ingestOnly
    ? audit.filter((a) => a.query_type === 'ingest')
    : audit;

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-4xl max-h-[85vh] overflow-hidden shadow-2xl flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-brain-800">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-purple-500/20 rounded-xl">
              <Share2 className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-white">{t('title')}</h2>
              <p className="text-xs text-brain-400">{t('subtitle')}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowGuide(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-brain-700 text-brain-300 hover:bg-brain-800 text-sm transition-colors"
              title={t('setup_guide_title')}
            >
              <HelpCircle className="w-4 h-4" />
              {t('setup_guide_button')}
            </button>
            <button
              onClick={onClose}
              className="p-2 rounded-lg hover:bg-brain-800 text-brain-400 hover:text-white transition-colors"
              title={t('close')}
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {showGuide && <SetupGuide initialSection="databus" onClose={() => setShowGuide(false)} />}

        {/* Tabs */}
        <div className="flex gap-1 px-6 pt-4">
          {(['consumers', 'audit'] as const).map((tk) => (
            <button
              key={tk}
              onClick={() => setTab(tk)}
              className={`px-4 py-2 text-sm rounded-lg flex items-center gap-2 transition-colors ${
                tab === tk
                  ? 'bg-purple-500/20 text-purple-300'
                  : 'text-brain-400 hover:bg-brain-800'
              }`}
            >
              {tk === 'consumers' ? <Users className="w-4 h-4" /> : <ScrollText className="w-4 h-4" />}
              {t(tk === 'consumers' ? 'tab_consumers' : 'tab_audit')}
            </button>
          ))}
        </div>

        {error && (
          <div className="mx-6 mt-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg flex items-center gap-2 text-red-400 text-sm">
            <AlertCircle className="w-4 h-4" /> {error}
          </div>
        )}

        {createdKey && (
          <div className="mx-6 mt-4 p-4 bg-amber-500/10 border border-amber-500/40 rounded-lg">
            <div className="flex items-center gap-2 text-amber-300 text-sm font-medium mb-2">
              <KeyRound className="w-4 h-4" /> {t('key_title')}
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-brain-950 px-3 py-2 rounded text-xs text-amber-200 break-all">
                {createdKey}
              </code>
              <button
                onClick={copyKey}
                className="px-3 py-2 bg-brain-800 hover:bg-brain-700 rounded text-xs text-white flex items-center gap-1"
              >
                {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                {copied ? t('key_copied') : t('key_copy')}
              </button>
              <button
                onClick={() => setCreatedKey(null)}
                className="px-3 py-2 bg-purple-500/30 hover:bg-purple-500/40 rounded text-xs text-purple-200"
              >
                {t('key_done')}
              </button>
            </div>
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-6">
          {loading ? (
            <div className="flex justify-center py-12">
              <div className="animate-spin w-8 h-8 border-2 border-purple-500 border-t-transparent rounded-full" />
            </div>
          ) : tab === 'consumers' ? (
            <div className="space-y-6">
              {/* Create form */}
              <div className="border border-brain-700/50 rounded-xl p-4 space-y-3">
                <div className="flex items-center gap-2 text-sm font-medium text-white">
                  <Plus className="w-4 h-4 text-purple-400" /> {t('create_title')}
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder={t('f_name_ph')}
                    className="bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white placeholder-brain-500 focus:outline-none focus:border-purple-500"
                  />
                  <input
                    value={form.contact_email}
                    onChange={(e) => setForm({ ...form, contact_email: e.target.value })}
                    placeholder={t('f_email')}
                    className="bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white placeholder-brain-500 focus:outline-none focus:border-purple-500"
                  />
                  <select
                    value={form.consumer_type}
                    onChange={(e) => setForm({ ...form, consumer_type: e.target.value })}
                    className="bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-purple-500"
                  >
                    {CONSUMER_TYPES.map((ty) => (
                      <option key={ty} value={ty}>{typeLabel(ty)}</option>
                    ))}
                  </select>
                  <select
                    value={form.policy_template}
                    onChange={(e) => setForm({ ...form, policy_template: e.target.value })}
                    className="bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-purple-500"
                  >
                    {Object.keys(templates).length === 0 && (
                      <option value="partner_public">partner_public</option>
                    )}
                    {Object.entries(templates).map(([k, v]) => (
                      <option key={k} value={k}>{v.name || k}</option>
                    ))}
                  </select>
                </div>

                {/* СРЕЗ ДАННЫХ: какие встречи/папки открыть этому потребителю */}
                <div className="p-3 rounded-lg bg-brain-800/60 border border-brain-700 space-y-2">
                  <div className="text-sm text-white flex items-center gap-2">
                    <Eye className="w-4 h-4 text-purple-400" /> {t('slice_title')}
                  </div>
                  <p className="text-xs text-brain-400">
                    {t('slice_hint')}
                  </p>
                  {/* Проект → папки → встречи (каскад выбора) */}
                  <select
                    value={sliceProjectId}
                    onChange={(e) => setSliceProjectId(e.target.value)}
                    className="w-full bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-purple-500"
                  >
                    <option value="">{t('all_projects')}</option>
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>

                  {/* Папки — мультивыбор чипами (имена пишутся в allowed_folders) */}
                  {(() => {
                    const picked = new Set(
                      form.allowed_folders.split(',').map((s) => s.trim()).filter(Boolean)
                    );
                    const shown = folders.filter(
                      (f) => !sliceProjectId || !f.project_id || f.project_id === sliceProjectId
                    );
                    if (shown.length === 0) return null;
                    const toggleFolder = (name: string) => {
                      const next = new Set(picked);
                      if (next.has(name)) next.delete(name); else next.add(name);
                      setForm((f) => ({ ...f, allowed_folders: Array.from(next).join(', ') }));
                    };
                    return (
                      <div className="flex flex-wrap gap-1">
                        {shown.slice(0, 40).map((f) => (
                          <button
                            key={f.id}
                            onClick={() => toggleFolder(f.name)}
                            className={`text-xs px-2 py-1 rounded border transition-colors ${
                              picked.has(f.name)
                                ? 'bg-purple-500/20 border-purple-500/50 text-purple-200'
                                : 'border-brain-700 text-brain-300 hover:bg-brain-700/50'
                            }`}
                          >
                            {picked.has(f.name) ? '✓ ' : ''}📁 {f.name}
                          </button>
                        ))}
                      </div>
                    );
                  })()}

                  {/* Встречи — список сразу виден, поиск лишь фильтрует */}
                  <input
                    value={meetingSearch}
                    onChange={(e) => setMeetingSearch(e.target.value)}
                    placeholder={t('meeting_filter_ph')}
                    className="w-full bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white placeholder-brain-500 focus:outline-none focus:border-purple-500"
                  />
                  {meetings.length > 0 ? (
                    <div className="max-h-40 overflow-y-auto border border-brain-700 rounded-lg divide-y divide-brain-800">
                      {meetings
                        .filter((m) => !meetingSearch.trim() || (m.title || '').toLowerCase().includes(meetingSearch.toLowerCase()))
                        .slice(0, 40)
                        .map((m) => (
                          <button
                            key={m.id}
                            onClick={() => {
                              setForm((f) => ({
                                ...f,
                                allowed_meeting_ids: f.allowed_meeting_ids.includes(m.id)
                                  ? f.allowed_meeting_ids.filter((x) => x !== m.id)
                                  : [...f.allowed_meeting_ids, m.id],
                              }));
                            }}
                            className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                              form.allowed_meeting_ids.includes(m.id)
                                ? 'bg-purple-500/20 text-purple-200'
                                : 'text-brain-300 hover:bg-brain-700/50'
                            }`}
                          >
                            {form.allowed_meeting_ids.includes(m.id) ? '✓ ' : ''}
                            {(m.date || '').slice(0, 10)} — {m.title}
                          </button>
                        ))}
                    </div>
                  ) : (
                    <p className="text-xs text-brain-500">{t('meetings_empty')}</p>
                  )}
                  {form.allowed_meeting_ids.length > 0 && (
                    <div className="text-xs text-purple-300">
                      {t('selected_meetings', { n: form.allowed_meeting_ids.length })}
                      <button
                        onClick={() => setForm({ ...form, allowed_meeting_ids: [] })}
                        className="ml-2 text-brain-400 hover:text-white underline"
                      >
                        {t('clear_selection')}
                      </button>
                    </div>
                  )}
                  <textarea
                    value={form.answer_prompt}
                    onChange={(e) => setForm({ ...form, answer_prompt: e.target.value })}
                    placeholder={t('answer_prompt_ph')}
                    rows={3}
                    className="w-full bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white placeholder-brain-500 focus:outline-none focus:border-purple-500 resize-y"
                  />
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={form.allow_deep_search}
                      onChange={(e) => setForm({ ...form, allow_deep_search: e.target.checked })}
                      className="mt-0.5 accent-purple-500"
                    />
                    <span className="text-xs text-brain-300">
                      <span className="text-white">{t('deep_search_title')}</span> — {t('deep_search_desc')} <span className="text-amber-300">{t('deep_search_warn')}</span>
                    </span>
                  </label>
                  <div className="grid grid-cols-2 gap-3">
                    <label className="text-xs text-brain-400">
                      {t('daily_limit_label')}
                      <input
                        type="number" min={1} max={10000}
                        value={form.max_queries_per_day}
                        onChange={(e) => setForm({ ...form, max_queries_per_day: e.target.value })}
                        placeholder={t('by_template_ph')}
                        className="mt-1 w-full bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white placeholder-brain-500 focus:outline-none focus:border-purple-500"
                      />
                    </label>
                    <label className="text-xs text-brain-400">
                      {t('expires_days_label')}
                      <input
                        type="number" min={1} max={3650}
                        value={form.expires_days}
                        onChange={(e) => setForm({ ...form, expires_days: e.target.value })}
                        placeholder={t('no_expiry_ph')}
                        className="mt-1 w-full bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white placeholder-brain-500 focus:outline-none focus:border-purple-500"
                      />
                    </label>
                  </div>
                </div>

                {/* Ingest toggle */}
                <label className="flex items-start gap-3 p-3 rounded-lg bg-brain-800/60 border border-brain-700 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.allow_ingest}
                    onChange={(e) => setForm({ ...form, allow_ingest: e.target.checked })}
                    className="mt-0.5 accent-purple-500"
                  />
                  <div className="flex-1">
                    <div className="flex items-center gap-2 text-sm text-white">
                      <ArrowDownToLine className="w-4 h-4 text-purple-400" />
                      {t('f_allow_ingest')}
                    </div>
                    <p className="text-xs text-brain-400 mt-1">{t('allow_ingest_hint')}</p>
                  </div>
                </label>
                {form.allow_ingest && (
                  <div className="flex items-center gap-2 text-sm text-brain-300 pl-1">
                    <span>{t('f_ingest_limit')}</span>
                    <input
                      type="number"
                      min={1}
                      max={1000}
                      value={form.ingest_max_items_per_request}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          ingest_max_items_per_request: Number(e.target.value) || 100,
                        })
                      }
                      className="w-24 bg-brain-800 border border-brain-700 rounded-lg px-2 py-1 text-sm text-white focus:outline-none focus:border-purple-500"
                    />
                  </div>
                )}

                <button
                  onClick={createConsumer}
                  disabled={creating || !form.name.trim()}
                  className="px-4 py-2 bg-purple-500/30 hover:bg-purple-500/40 disabled:opacity-40 rounded-lg text-sm text-purple-200 flex items-center gap-2"
                >
                  <Plus className="w-4 h-4" />
                  {creating ? t('creating') : t('create_btn')}
                </button>
              </div>

              {/* Consumers list */}
              {consumers.length === 0 ? (
                <p className="text-center text-brain-500 text-sm py-8">{t('none')}</p>
              ) : (
                <div className="space-y-2">
                  {consumers.map((c) => (
                    <div
                      key={c.id}
                      className="flex items-center justify-between p-3 rounded-xl border border-brain-700/50 bg-brain-800/40"
                    >
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sm text-white font-medium truncate">{c.name}</span>
                          <span className="px-2 py-0.5 bg-brain-700 text-brain-300 text-xs rounded-full">
                            {typeLabel(c.consumer_type)}
                          </span>
                          {c.allow_ingest ? (
                            <span className="px-2 py-0.5 bg-purple-500/20 text-purple-300 text-xs rounded-full flex items-center gap-1">
                              <ArrowDownToLine className="w-3 h-3" /> {t('badge_ingest')}
                            </span>
                          ) : (
                            <span className="px-2 py-0.5 bg-brain-700/60 text-brain-400 text-xs rounded-full flex items-center gap-1">
                              <Eye className="w-3 h-3" /> {t('badge_readonly')}
                            </span>
                          )}
                          {!c.is_active && (
                            <span className="px-2 py-0.5 bg-red-500/20 text-red-400 text-xs rounded-full">
                              {t('badge_inactive')}
                            </span>
                          )}
                        </div>
                        <div className="text-xs text-brain-500 mt-1 font-mono">
                          {c.api_key_prefix}…
                        </div>
                      </div>
                      {c.is_active && (
                        <button
                          onClick={() => deactivate(c.id)}
                          className="p-2 rounded-lg hover:bg-red-500/20 text-brain-400 hover:text-red-400 transition-colors"
                          title={t('deactivate')}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            // Audit tab
            <div className="space-y-3">
              <label className="flex items-center gap-2 text-sm text-brain-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={ingestOnly}
                  onChange={(e) => setIngestOnly(e.target.checked)}
                  className="accent-purple-500"
                />
                {t('audit_ingest_only')}
              </label>
              {shownAudit.length === 0 ? (
                <p className="text-center text-brain-500 text-sm py-8">{t('audit_none')}</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-brain-500 text-xs border-b border-brain-800">
                        <th className="py-2 pr-4">{t('col_when')}</th>
                        <th className="py-2 pr-4">{t('col_consumer')}</th>
                        <th className="py-2 pr-4">{t('col_action')}</th>
                        <th className="py-2">{t('col_result')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {shownAudit.map((a) => (
                        <tr key={a.id} className="border-b border-brain-800/50">
                          <td className="py-2 pr-4 text-brain-400 text-xs whitespace-nowrap">
                            {a.timestamp?.slice(0, 19).replace('T', ' ')}
                          </td>
                          <td className="py-2 pr-4 text-brain-300">{a.consumer_name}</td>
                          <td className="py-2 pr-4">
                            <span
                              className={`px-2 py-0.5 text-xs rounded-full ${
                                a.query_type === 'ingest'
                                  ? 'bg-purple-500/20 text-purple-300'
                                  : 'bg-brain-700 text-brain-300'
                              }`}
                            >
                              {a.query_type}
                            </span>
                          </td>
                          <td className="py-2 text-brain-400 text-xs">
                            {a.error
                              ? <span className="text-red-400">{a.error}</span>
                              : `${a.status_code} · ${a.results_count}`}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
