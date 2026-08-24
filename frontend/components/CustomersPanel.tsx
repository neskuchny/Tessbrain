'use client';

import { useState, useCallback, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import {
  Search, Phone, AlertCircle, AlertTriangle, ExternalLink,
  ShieldAlert, PhoneCall, MessageCircle, Briefcase, User,
  TrendingUp, Lightbulb, FileText, BarChart3,
} from 'lucide-react';

interface Snapshot {
  phone?: string;
  name_guess?: string;
  first_seen?: string;
  last_seen?: string;
  total_calls?: number;
  total_chats?: number;
  avg_score?: number;
  last_score?: number;
  stage_guess?: string;
  deal_value_total?: number;
  top_objections?: string[];
  managers?: string[];
  at_risk?: boolean;
  next_recommended_step?: string;
}

interface TimelineItem {
  ts?: string;
  type?: string;
  id?: number;
  agent?: string;
  channel?: string;
  score?: number;
  messages?: number;
  summary?: string;
  deep_link?: string;
}

interface CustomerCard {
  available: boolean;
  error?: string;
  phone_norm?: string | null;
  snapshot?: Snapshot | null;
  timeline?: TimelineItem[];
}

interface OurContext {
  meetings: Array<{ at?: string; text?: string; actor?: string }>;
  facts: Array<{ fact_key?: string; verified_by?: string }>;
}

interface AccruedInsight {
  insight_id: string;
  title?: string;
  description?: string;
  priority?: string;
  meta?: { deep_link?: string };
}

interface CustomersPanelProps {
  userId?: string;
}

const STAGE_COLORS: Record<string, string> = {
  hot: 'bg-red-500/20 text-red-300',
  warm: 'bg-orange-500/20 text-orange-300',
  cold: 'bg-blue-500/20 text-blue-300',
};

interface VoiceReport {
  complaints: { total: number; items: Array<{ at?: string; agent?: string; excerpt?: string; marker?: string; deep_link?: string }> };
  deals_at_risk: { total: number; items: Array<{ customer_phone?: string; key_insight?: string; agent?: string }> };
  objections: { rows: Array<{ objection: string; mentions: number; mentions_before: number; trend: string }> };
  coach: { advice_given: number; outcomes: { worked: number; worsened: number } };
  per_agent: Record<string, { complaints: number; at_risk: number; coach_advice: number }>;
  _note?: string;
}

const TREND_MARK: Record<string, string> = { new: '🆕', up: '↑', down: '↓', flat: '→' };

export default function CustomersPanel({ userId }: CustomersPanelProps) {
  const t = useTranslations('customers_panel');
  const [phone, setPhone] = useState('');
  const [name, setName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [card, setCard] = useState<CustomerCard | null>(null);
  const [ourContext, setOurContext] = useState<OurContext | null>(null);
  const [insights, setInsights] = useState<AccruedInsight[]>([]);
  const [showReport, setShowReport] = useState(false);
  const [report, setReport] = useState<VoiceReport | null>(null);
  const [reportLoading, setReportLoading] = useState(false);

  const loadReport = useCallback(async () => {
    if (!userId) return;
    setReportLoading(true);
    setError(null);
    try {
      // последние 7 дней — недельный срез «голоса клиента»
      const since = new Date(Date.now() - 7 * 86400_000).toISOString().slice(0, 10);
      const res = await fetch(`/api/v1/integrations/callinsight/voice-report?since=${since}&user_id=${userId}`);
      if (res.status === 403) { setDisabled(true); return; }
      setDisabled(false);
      const data = await res.json();
      setReport(data.report || null);
    } catch {
      setError(t('err_loading'));
    } finally {
      setReportLoading(false);
    }
  }, [userId, t]);

  const search = useCallback(async () => {
    if (!userId || !phone.trim()) return;
    setLoading(true);
    setError(null);
    setCard(null);
    try {
      const params = new URLSearchParams({ phone: phone.trim(), user_id: userId });
      if (name.trim()) params.set('name', name.trim());
      const res = await fetch(`/api/v1/integrations/callinsight/customer?${params}`);
      if (res.status === 403) { setDisabled(true); return; }
      setDisabled(false);
      const data = await res.json();
      setCard(data.card || null);
      setOurContext(data.our_context || null);
      setInsights(data.insights || []);
    } catch {
      setError(t('err_loading'));
    } finally {
      setLoading(false);
    }
  }, [userId, phone, name, t]);

  const snapshot = card?.snapshot;
  const stage = (snapshot?.stage_guess || '').toLowerCase();

  // Карточки клиентов из ночной консолидации (B2B): статус/договорённости/
  // риски/следующий шаг по данным встреч в графе.
  const [clientCards, setClientCards] = useState<any[]>([]);
  useEffect(() => {
    if (!userId) return;
    fetch(`/api/v1/snapshots/clients?user_id=${userId}`)
      .then((r) => r.json())
      .then((d) => setClientCards(Array.isArray(d.cards) ? d.cards : []))
      .catch(() => {});
  }, [userId]);

  const STATUS_STYLE: Record<string, string> = {
    'активный': 'bg-emerald-500/20 text-emerald-300',
    'переговоры': 'bg-sky-500/20 text-sky-300',
    'риск': 'bg-red-500/20 text-red-300',
    'неактивный': 'bg-slate-500/20 text-slate-400',
  };

  return (
    <div className="h-full p-4 overflow-auto custom-scrollbar">
      <div className="max-w-4xl mx-auto space-y-4">
        {/* Карточки клиентов (из памяти встреч) */}
        {clientCards.length > 0 && (
          <div>
            <div className="text-white text-sm font-semibold mb-2 flex items-center gap-2">
              💼 {t('clients_from_meetings')}
              <span className="text-slate-500 font-normal text-xs">· {clientCards.length}</span>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              {clientCards.map((c, i) => (
                <div key={i} className="bg-brain-800/40 border border-brain-600/30 rounded-xl p-3">
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="text-white text-sm font-medium truncate">{c.name}</span>
                    {c.status && (
                      <span className={`text-[10px] px-2 py-0.5 rounded-full flex-none ${STATUS_STYLE[c.status] || 'bg-brain-700/50 text-slate-300'}`}>
                        {c.status}
                      </span>
                    )}
                  </div>
                  {typeof c.revenue === 'number' && (
                    <div className="text-emerald-300 text-xs font-semibold mb-1">
                      💰 {Number(c.revenue).toLocaleString('ru-RU')}
                    </div>
                  )}
                  {c.summary && <p className="text-slate-300 text-xs mb-1.5">{c.summary}</p>}
                  {c.agreements && (
                    <div className="text-[11px] text-slate-400">🤝 {c.agreements}</div>
                  )}
                  {c.risks && <div className="text-[11px] text-amber-300/90">⚠ {c.risks}</div>}
                  {c.next_step && (
                    <div className="text-[11px] text-cyan-300/90 mt-1">→ {c.next_step}</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
        {/* Search */}
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Phone className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-brain-500" />
            <input
              type="text"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && search()}
              placeholder={t('phone_placeholder')}
              autoComplete="off"
              className="w-full bg-brain-900 border border-brain-700 rounded-lg pl-10 pr-4 py-2 text-sm text-white placeholder-brain-500 focus:outline-none focus:border-cyan-500"
            />
          </div>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && search()}
            placeholder={t('name_placeholder')}
            autoComplete="off"
            className="w-48 bg-brain-900 border border-brain-700 rounded-lg px-3 py-2 text-sm text-white placeholder-brain-500 focus:outline-none focus:border-cyan-500"
          />
          <button
            onClick={search}
            disabled={loading || !phone.trim()}
            className="px-4 py-2 rounded-lg bg-cyan-500/20 text-cyan-300 hover:bg-cyan-500/30 text-sm font-medium flex items-center gap-2 disabled:opacity-50"
          >
            <Search className="w-4 h-4" />{t('search_button')}
          </button>
          <button
            onClick={() => { const next = !showReport; setShowReport(next); if (next && !report) loadReport(); }}
            className={`px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-2 ${
              showReport ? 'bg-purple-500/30 text-purple-200' : 'bg-purple-500/15 text-purple-300 hover:bg-purple-500/25'
            }`}
          >
            <BarChart3 className="w-4 h-4" />{t('voice_report_button')}
          </button>
        </div>

        {/* Отчёт «Голос клиента» — недельный срез, числа считает код */}
        {showReport && (
          reportLoading ? (
            <div className="flex items-center justify-center py-8">
              <div className="animate-spin w-6 h-6 border-2 border-purple-500 border-t-transparent rounded-full" />
            </div>
          ) : report && (
            <div className="p-5 rounded-xl border border-purple-500/30 bg-purple-500/5 space-y-4">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
                <div><span className="text-brain-500 text-xs">{t('vr_complaints')}</span><p className="text-xl text-red-300 font-semibold">{report.complaints?.total ?? 0}</p></div>
                <div><span className="text-brain-500 text-xs">{t('vr_at_risk')}</span><p className="text-xl text-orange-300 font-semibold">{report.deals_at_risk?.total ?? 0}</p></div>
                <div><span className="text-brain-500 text-xs">{t('vr_advice')}</span><p className="text-xl text-cyan-300 font-semibold">{report.coach?.advice_given ?? 0}</p></div>
                <div><span className="text-brain-500 text-xs">{t('vr_outcomes')}</span><p className="text-xl text-brain-200 font-semibold">{report.coach?.outcomes?.worked ?? 0} / {report.coach?.outcomes?.worsened ?? 0}</p></div>
              </div>
              {(report.objections?.rows?.length || 0) > 0 && (
                <div>
                  <p className="text-xs text-brain-500 uppercase mb-1">{t('vr_objections')}</p>
                  <div className="space-y-1">
                    {report.objections.rows.map((r, i) => (
                      <p key={i} className="text-sm text-brain-300">
                        <span className="mr-1">{TREND_MARK[r.trend] || ''}</span>
                        {r.objection}: <span className="text-white">{r.mentions}</span>
                        <span className="text-brain-500 text-xs"> ({t('vr_was')} {r.mentions_before})</span>
                      </p>
                    ))}
                  </div>
                </div>
              )}
              {Object.keys(report.per_agent || {}).length > 0 && (
                <div>
                  <p className="text-xs text-brain-500 uppercase mb-1">{t('vr_per_agent')}</p>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-brain-500 text-xs border-b border-brain-700/50">
                        <th className="text-left py-1 pr-3">{t('vr_agent')}</th>
                        <th className="text-left py-1 pr-3">{t('vr_complaints')}</th>
                        <th className="text-left py-1 pr-3">{t('vr_at_risk')}</th>
                        <th className="text-left py-1 pr-3">{t('vr_advice')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(report.per_agent).map(([agent, row]) => (
                        <tr key={agent} className="border-b border-brain-800/50">
                          <td className="py-1 pr-3 text-brain-200">{agent}</td>
                          <td className="py-1 pr-3 text-brain-300">{row.complaints}</td>
                          <td className="py-1 pr-3 text-brain-300">{row.at_risk}</td>
                          <td className="py-1 pr-3 text-brain-300">{row.coach_advice}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="text-xs text-brain-600">{report._note}</p>
            </div>
          )
        )}

        {error && (
          <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg flex items-center gap-2 text-red-400 text-sm">
            <AlertCircle className="w-4 h-4" />{error}
          </div>
        )}

        {disabled && (
          <div className="flex flex-col items-center py-12 text-center">
            <ShieldAlert className="w-10 h-10 text-yellow-500 mb-3" />
            <p className="text-brain-300 font-medium">{t('disabled_title')}</p>
            <p className="text-brain-500 text-sm mt-1 max-w-md">{t('disabled_hint')}</p>
          </div>
        )}

        {loading && (
          <div className="flex items-center justify-center py-12">
            <div className="animate-spin w-8 h-8 border-2 border-cyan-500 border-t-transparent rounded-full" />
          </div>
        )}

        {/* «Невидимое ≠ несуществующее»: API недоступен — говорим честно */}
        {card && !card.available && (
          <div className="p-4 bg-yellow-500/10 border border-yellow-500/30 rounded-xl flex items-start gap-3">
            <AlertTriangle className="w-5 h-5 text-yellow-400 flex-shrink-0 mt-0.5" />
            <div>
              <p className="text-yellow-300 font-medium text-sm">{t('unavailable_title')}</p>
              <p className="text-brain-400 text-xs mt-1">{card.error}</p>
            </div>
          </div>
        )}

        {card?.available && !snapshot && (
          <p className="text-brain-500 text-sm text-center py-4">{t('not_found', { phone: card.phone_norm || phone })}</p>
        )}

        {/* Snapshot */}
        {snapshot && (
          <div className="p-5 rounded-xl border border-brain-700/50 bg-brain-800/30 space-y-3">
            <div className="flex items-center gap-3 flex-wrap">
              <User className="w-5 h-5 text-cyan-400" />
              <h3 className="text-white font-semibold">{snapshot.name_guess || card?.phone_norm}</h3>
              {stage && (
                <span className={`px-2 py-0.5 rounded-full text-xs ${STAGE_COLORS[stage] || 'bg-brain-700 text-brain-300'}`}>
                  {stage}
                </span>
              )}
              {snapshot.at_risk && (
                <span className="px-2 py-0.5 rounded-full text-xs bg-red-500/20 text-red-300 flex items-center gap-1">
                  <AlertTriangle className="w-3 h-3" />{t('at_risk')}
                </span>
              )}
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
              <div><span className="text-brain-500 text-xs">{t('calls')}</span><p className="text-brain-200">{snapshot.total_calls ?? '—'} / {snapshot.total_chats ?? 0} {t('chats_short')}</p></div>
              <div><span className="text-brain-500 text-xs">{t('avg_score')}</span><p className="text-brain-200">{snapshot.avg_score ?? '—'} ({t('last')}: {snapshot.last_score ?? '—'})</p></div>
              <div><span className="text-brain-500 text-xs">{t('deal_value')}</span><p className="text-brain-200">{snapshot.deal_value_total?.toLocaleString() ?? '—'}</p></div>
              <div><span className="text-brain-500 text-xs">{t('managers')}</span><p className="text-brain-200">{snapshot.managers?.join(', ') || '—'}</p></div>
            </div>
            {(snapshot.top_objections?.length || 0) > 0 && (
              <div className="flex items-center gap-2 flex-wrap text-xs">
                <span className="text-brain-500">{t('objections')}:</span>
                {snapshot.top_objections!.map((o, i) => (
                  <span key={i} className="px-2 py-0.5 rounded bg-brain-700/60 text-brain-300">{o}</span>
                ))}
              </div>
            )}
            {snapshot.next_recommended_step && (
              <div className="flex items-start gap-2 text-sm p-2 rounded-lg bg-cyan-500/10 border border-cyan-500/20">
                <TrendingUp className="w-4 h-4 text-cyan-400 flex-shrink-0 mt-0.5" />
                <span className="text-cyan-200">{snapshot.next_recommended_step}</span>
              </div>
            )}
            <p className="text-xs text-brain-600">{t('source_note')}</p>
          </div>
        )}

        {/* Накопленные инсайты по клиенту (жалобы / at-risk из webhooks) */}
        {insights.length > 0 && (
          <div className="p-4 rounded-xl border border-brain-700/50 bg-brain-800/30">
            <h4 className="text-sm font-medium text-white mb-2 flex items-center gap-2">
              <Lightbulb className="w-4 h-4 text-yellow-400" />{t('insights_title')}
            </h4>
            <div className="space-y-2">
              {insights.map((ins) => (
                <div key={ins.insight_id} className="text-sm flex items-start gap-2">
                  <span className={`px-1.5 py-0.5 rounded text-xs flex-shrink-0 ${
                    ins.priority === 'high' ? 'bg-red-500/20 text-red-300' : 'bg-yellow-500/20 text-yellow-300'
                  }`}>{ins.priority}</span>
                  <div className="min-w-0">
                    <p className="text-brain-200">{ins.title}</p>
                    {ins.description && <p className="text-brain-500 text-xs truncate">{ins.description}</p>}
                  </div>
                  {ins.meta?.deep_link && (
                    <a href={ins.meta.deep_link} target="_blank" rel="noreferrer" className="ml-auto text-cyan-400 hover:text-cyan-300 flex-shrink-0">
                      <ExternalLink className="w-3.5 h-3.5" />
                    </a>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Timeline */}
        {(card?.timeline?.length || 0) > 0 && (
          <div className="p-4 rounded-xl border border-brain-700/50 bg-brain-800/30">
            <h4 className="text-sm font-medium text-white mb-2 flex items-center gap-2">
              <PhoneCall className="w-4 h-4 text-cyan-400" />{t('timeline_title')}
            </h4>
            <div className="space-y-2">
              {card!.timeline!.map((item, i) => (
                <div key={i} className="flex items-start gap-2 text-sm border-b border-brain-800/50 pb-2 last:border-0">
                  {item.type === 'chat'
                    ? <MessageCircle className="w-4 h-4 text-brain-500 flex-shrink-0 mt-0.5" />
                    : item.type === 'deal'
                      ? <Briefcase className="w-4 h-4 text-brain-500 flex-shrink-0 mt-0.5" />
                      : <PhoneCall className="w-4 h-4 text-brain-500 flex-shrink-0 mt-0.5" />}
                  <div className="min-w-0 flex-1">
                    <p className="text-brain-200">{item.summary || item.type}</p>
                    <p className="text-brain-500 text-xs">
                      {item.ts?.slice(0, 16).replace('T', ' ')}{item.agent ? ` · ${item.agent}` : ''}
                      {typeof item.score === 'number' ? ` · ${t('score')}: ${item.score}` : ''}
                    </p>
                  </div>
                  {item.deep_link && (
                    <a href={item.deep_link} target="_blank" rel="noreferrer" className="text-cyan-400 hover:text-cyan-300 flex-shrink-0">
                      <ExternalLink className="w-3.5 h-3.5" />
                    </a>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Наш контекст: встречи/факты с упоминанием клиента */}
        {ourContext && (ourContext.meetings.length > 0 || ourContext.facts.length > 0) && (
          <div className="p-4 rounded-xl border border-brain-700/50 bg-brain-800/30">
            <h4 className="text-sm font-medium text-white mb-2 flex items-center gap-2">
              <FileText className="w-4 h-4 text-purple-400" />{t('our_context_title')}
            </h4>
            {ourContext.meetings.map((m, i) => (
              <p key={`m${i}`} className="text-sm text-brain-300 mb-1">
                <span className="text-brain-500 text-xs mr-2">{m.at?.slice(0, 10)}</span>{m.text}
              </p>
            ))}
            {ourContext.facts.map((f, i) => (
              <p key={`f${i}`} className="text-sm text-brain-400 mb-1">✓ {f.fact_key}</p>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
