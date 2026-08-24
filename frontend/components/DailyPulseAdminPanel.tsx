'use client';

import { useState, useEffect, useCallback } from 'react';
import { useTranslations } from 'next-intl';
import {
  Activity, X, Plus, Check, ShieldCheck, ShieldAlert,
  RefreshCw, Link2, AlertCircle, CheckCircle2, XCircle,
} from 'lucide-react';

interface Mapping {
  external_system: string;
  external_id: string;
  tessent_user_id: string;
  email: string | null;
  verified: boolean;
  created_at: string | null;
}

interface IngestEvent {
  event: string;
  team_ref: string | null;
  user_ref: string | null;
  occurred_at: string | null;
  received_at: string | null;
  signature_ok: boolean;
}

interface DailyPulseAdminPanelProps {
  userId: string | null;
  isOpen: boolean;
  onClose: () => void;
}

type Section = 'identity' | 'events';

const SOURCES = ['daily_pulse', 'meetflow'];

export default function DailyPulseAdminPanel({ userId, isOpen, onClose }: DailyPulseAdminPanelProps) {
  const t = useTranslations('daily_pulse_admin');
  const [section, setSection] = useState<Section>('identity');
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [events, setEvents] = useState<IngestEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [disabled, setDisabled] = useState(false);

  // new mapping form
  const [form, setForm] = useState({
    source: 'daily_pulse', external_id: '', tessent_user_id: '',
    email: '', verified: false,
  });
  const [saving, setSaving] = useState(false);

  const fetchMappings = useCallback(async () => {
    if (!userId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/v1/integrations/identity/mappings?user_id=${userId}`);
      if (res.status === 403) { setDisabled(true); setMappings([]); return; }
      setDisabled(false);
      const data = await res.json();
      setMappings(data.mappings || []);
    } catch {
      setError(t('err_loading'));
    } finally {
      setLoading(false);
    }
  }, [userId, t]);

  const fetchEvents = useCallback(async () => {
    if (!userId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/v1/integrations/events/recent?user_id=${userId}`);
      if (res.status === 403) { setDisabled(true); setEvents([]); return; }
      setDisabled(false);
      const data = await res.json();
      setEvents(data.events || []);
    } catch {
      setError(t('err_loading'));
    } finally {
      setLoading(false);
    }
  }, [userId, t]);

  const refresh = useCallback(() => {
    if (section === 'identity') fetchMappings();
    else fetchEvents();
  }, [section, fetchMappings, fetchEvents]);

  useEffect(() => {
    if (isOpen && userId) refresh();
  }, [isOpen, userId, section, refresh]);

  const handleLink = async () => {
    if (!userId || !form.external_id || !form.tessent_user_id) {
      setError(t('err_required'));
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch(`/api/v1/integrations/identity/link?user_id=${userId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      const data = await res.json();
      if (data.success) {
        setNotice(t('linked_ok'));
        setForm({ source: 'daily_pulse', external_id: '', tessent_user_id: '', email: '', verified: false });
        fetchMappings();
      } else {
        setError(data.error || t('err_saving'));
      }
    } catch {
      setError(t('err_saving'));
    } finally {
      setSaving(false);
    }
  };

  const handleVerify = async (m: Mapping, verified: boolean) => {
    if (!userId) return;
    setError(null);
    try {
      const res = await fetch(`/api/v1/integrations/identity/verify?user_id=${userId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: m.external_system, external_id: m.external_id, verified }),
      });
      const data = await res.json();
      if (data.success) fetchMappings();
      else setError(data.error || t('err_saving'));
    } catch {
      setError(t('err_saving'));
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-4xl max-h-[85vh] overflow-hidden shadow-2xl flex flex-col">
        {/* Header */}
        <div className="p-6 border-b border-brain-700/50 flex-shrink-0">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-cyan-500/20 rounded-xl">
                <Activity className="w-6 h-6 text-cyan-400" />
              </div>
              <div>
                <h2 className="text-xl font-semibold text-white">{t('title')}</h2>
                <p className="text-sm text-brain-400">{t('description')}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={refresh}
                className="p-2 rounded-lg hover:bg-brain-800 text-brain-400 hover:text-white transition-colors"
                title={t('refresh')}
              >
                <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
              </button>
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-brain-800 text-brain-400 hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>

          {/* Section tabs */}
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => setSection('identity')}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors flex items-center gap-2 ${
                section === 'identity' ? 'bg-cyan-500/20 text-cyan-300' : 'bg-brain-800/50 text-brain-400 hover:text-white'
              }`}
            >
              <Link2 className="w-4 h-4" />{t('tab_identity')}
            </button>
            <button
              onClick={() => setSection('events')}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors flex items-center gap-2 ${
                section === 'events' ? 'bg-cyan-500/20 text-cyan-300' : 'bg-brain-800/50 text-brain-400 hover:text-white'
              }`}
            >
              <Activity className="w-4 h-4" />{t('tab_events')}
            </button>
          </div>
        </div>

        {/* Notices */}
        {error && (
          <div className="mx-6 mt-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg flex items-center gap-2 text-red-400 text-sm">
            <AlertCircle className="w-4 h-4" />{error}
            <button onClick={() => setError(null)} className="ml-auto"><X className="w-4 h-4" /></button>
          </div>
        )}
        {notice && (
          <div className="mx-6 mt-4 p-3 bg-green-500/10 border border-green-500/30 rounded-lg flex items-center gap-2 text-green-400 text-sm">
            <CheckCircle2 className="w-4 h-4" />{notice}
            <button onClick={() => setNotice(null)} className="ml-auto"><X className="w-4 h-4" /></button>
          </div>
        )}

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6 custom-scrollbar">
          {disabled ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <ShieldAlert className="w-10 h-10 text-yellow-500 mb-3" />
              <p className="text-brain-300 font-medium">{t('disabled_title')}</p>
              <p className="text-brain-500 text-sm mt-1 max-w-md">{t('disabled_hint')}</p>
            </div>
          ) : section === 'identity' ? (
            <div className="space-y-5">
              {/* Add mapping form */}
              <div className="p-4 rounded-xl border border-brain-700/50 bg-brain-800/30">
                <h3 className="text-sm font-medium text-white mb-3 flex items-center gap-2">
                  <Plus className="w-4 h-4 text-cyan-400" />{t('link_title')}
                </h3>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  <select
                    value={form.source}
                    onChange={(e) => setForm({ ...form, source: e.target.value })}
                    className="bg-brain-900 border border-brain-600 rounded px-2 py-1.5 text-sm text-white"
                  >
                    {SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                  <input
                    type="text" value={form.external_id}
                    onChange={(e) => setForm({ ...form, external_id: e.target.value })}
                    placeholder={t('ph_external_id')} autoComplete="off"
                    className="bg-brain-900 border border-brain-600 rounded px-2 py-1.5 text-sm text-white placeholder-brain-500"
                  />
                  <input
                    type="text" value={form.tessent_user_id}
                    onChange={(e) => setForm({ ...form, tessent_user_id: e.target.value })}
                    placeholder={t('ph_user_id')} autoComplete="off"
                    className="bg-brain-900 border border-brain-600 rounded px-2 py-1.5 text-sm text-white placeholder-brain-500"
                  />
                  <input
                    type="text" value={form.email}
                    onChange={(e) => setForm({ ...form, email: e.target.value })}
                    placeholder={t('ph_email')} autoComplete="off"
                    className="bg-brain-900 border border-brain-600 rounded px-2 py-1.5 text-sm text-white placeholder-brain-500"
                  />
                </div>
                <div className="flex items-center justify-between mt-3">
                  <label className="flex items-center gap-2 text-sm text-brain-300 cursor-pointer">
                    <input
                      type="checkbox" checked={form.verified}
                      onChange={(e) => setForm({ ...form, verified: e.target.checked })}
                      className="accent-cyan-500"
                    />
                    {t('verified_now')}
                  </label>
                  <button
                    onClick={handleLink} disabled={saving}
                    className="px-4 py-1.5 rounded-lg bg-cyan-500/20 text-cyan-300 hover:bg-cyan-500/30 text-sm font-medium flex items-center gap-2 disabled:opacity-50"
                  >
                    <Link2 className="w-4 h-4" />{t('link_button')}
                  </button>
                </div>
              </div>

              {/* Mappings table */}
              {loading ? (
                <div className="flex items-center justify-center py-8">
                  <div className="animate-spin w-6 h-6 border-2 border-cyan-500 border-t-transparent rounded-full" />
                </div>
              ) : mappings.length === 0 ? (
                <p className="text-brain-500 text-sm text-center py-6">{t('no_mappings')}</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-brain-500 text-xs border-b border-brain-700/50">
                        <th className="text-left py-2 pr-3">{t('col_source')}</th>
                        <th className="text-left py-2 pr-3">{t('col_external_id')}</th>
                        <th className="text-left py-2 pr-3">{t('col_user_id')}</th>
                        <th className="text-left py-2 pr-3">{t('col_email')}</th>
                        <th className="text-left py-2 pr-3">{t('col_verified')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {mappings.map((m, i) => (
                        <tr key={`${m.external_system}-${m.external_id}-${i}`} className="border-b border-brain-800/50">
                          <td className="py-2 pr-3 text-brain-300">{m.external_system}</td>
                          <td className="py-2 pr-3 text-brain-300 font-mono text-xs">{m.external_id}</td>
                          <td className="py-2 pr-3 text-brain-400 font-mono text-xs">{m.tessent_user_id}</td>
                          <td className="py-2 pr-3 text-brain-400">{m.email || '—'}</td>
                          <td className="py-2 pr-3">
                            {m.verified ? (
                              <button
                                onClick={() => handleVerify(m, false)}
                                className="flex items-center gap-1 text-green-400 hover:text-green-300 text-xs"
                                title={t('click_unverify')}
                              >
                                <ShieldCheck className="w-4 h-4" />{t('verified')}
                              </button>
                            ) : (
                              <button
                                onClick={() => handleVerify(m, true)}
                                className="flex items-center gap-1 text-yellow-400 hover:text-yellow-300 text-xs"
                                title={t('click_verify')}
                              >
                                <ShieldAlert className="w-4 h-4" />{t('unverified')}
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ) : (
            /* Events diagnostics */
            loading ? (
              <div className="flex items-center justify-center py-8">
                <div className="animate-spin w-6 h-6 border-2 border-cyan-500 border-t-transparent rounded-full" />
              </div>
            ) : events.length === 0 ? (
              <p className="text-brain-500 text-sm text-center py-6">{t('no_events')}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-brain-500 text-xs border-b border-brain-700/50">
                      <th className="text-left py-2 pr-3">{t('col_event')}</th>
                      <th className="text-left py-2 pr-3">{t('col_team')}</th>
                      <th className="text-left py-2 pr-3">{t('col_user')}</th>
                      <th className="text-left py-2 pr-3">{t('col_received')}</th>
                      <th className="text-left py-2 pr-3">{t('col_signature')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {events.map((e, i) => (
                      <tr key={i} className="border-b border-brain-800/50">
                        <td className="py-2 pr-3 text-brain-300 font-mono text-xs">{e.event || '—'}</td>
                        <td className="py-2 pr-3 text-brain-400">{e.team_ref || '—'}</td>
                        <td className="py-2 pr-3 text-brain-400">{e.user_ref || '—'}</td>
                        <td className="py-2 pr-3 text-brain-500 text-xs">{e.received_at?.slice(0, 19) || '—'}</td>
                        <td className="py-2 pr-3">
                          {e.signature_ok ? (
                            <span className="flex items-center gap-1 text-green-400 text-xs"><Check className="w-3 h-3" />HMAC</span>
                          ) : (
                            <span className="flex items-center gap-1 text-red-400 text-xs"><XCircle className="w-3 h-3" />{t('bad_signature')}</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}
        </div>

        {/* Footer */}
        <div className="p-4 border-t border-brain-700/50 bg-brain-900/50 flex-shrink-0">
          <p className="text-xs text-brain-500">{t('footer_note')}</p>
        </div>
      </div>
    </div>
  );
}
