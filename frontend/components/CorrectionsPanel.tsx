"use client";

import React, { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";

// Types
interface Correction {
  id: string;
  entity_type: string;
  entity_id: string;
  field: string;
  old_value: any;
  new_value: any;
  reason: string;
  source: string;
  status: "pending" | "approved" | "rejected" | "applied";
  created_at: string;
  applied_at?: string;
  conflict_with?: string;
}

interface CorrectionStats {
  total: number;
  pending: number;
  approved: number;
  rejected: number;
  applied: number;
  conflicts: number;
}

interface CorrectionsResponse {
  corrections: Correction[];
  total: number;
}

// API functions
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function fetchPendingCorrections(userId: string): Promise<CorrectionsResponse> {
  const res = await fetch(`${API_BASE}/api/v1/corrections/pending?user_id=${userId}`);
  if (!res.ok) throw new Error("Failed to fetch corrections");
  return res.json();
}

async function fetchCorrectionStats(userId: string): Promise<CorrectionStats> {
  const res = await fetch(`${API_BASE}/api/v1/corrections/stats?user_id=${userId}`);
  if (!res.ok) throw new Error("Failed to fetch stats");
  return res.json();
}

async function approveCorrection(correctionId: string, userId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/v1/corrections/${correctionId}/approve?user_id=${userId}`, {
    method: "PUT",
  });
  if (!res.ok) throw new Error("Failed to approve correction");
}

async function rejectCorrection(correctionId: string, userId: string, reason: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/v1/corrections/${correctionId}/reject?user_id=${userId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!res.ok) throw new Error("Failed to reject correction");
}

async function submitCorrection(
  userId: string,
  data: {
    entity_type: string;
    entity_id: string;
    field: string;
    old_value: any;
    new_value: any;
    reason: string;
    source?: string;
  }
): Promise<Correction> {
  const res = await fetch(`${API_BASE}/api/v1/corrections/?user_id=${userId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to submit correction");
  return res.json();
}

async function applyPendingCorrections(userId: string): Promise<{ applied: number; failed: number }> {
  const res = await fetch(`${API_BASE}/api/v1/corrections/apply?user_id=${userId}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Failed to apply corrections");
  return res.json();
}

// Components
function StatusBadge({ status }: { status: Correction["status"] }) {
  const t = useTranslations("corrections");
  const colors = {
    pending: "bg-amber-500/20 text-amber-400 border-amber-500/30",
    approved: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
    rejected: "bg-red-500/20 text-red-400 border-red-500/30",
    applied: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  };

  const labels = {
    pending: t("status_pending"),
    approved: t("status_approved"),
    rejected: t("status_rejected"),
    applied: t("status_applied"),
  };

  return (
    <span className={`px-2 py-0.5 text-xs font-medium rounded border ${colors[status]}`}>
      {labels[status]}
    </span>
  );
}

function CorrectionCard({
  correction,
  onApprove,
  onReject,
}: {
  correction: Correction;
  onApprove: () => void;
  onReject: (reason: string) => void;
}) {
  const t = useTranslations("corrections");
  const [showRejectModal, setShowRejectModal] = useState(false);
  const [rejectReason, setRejectReason] = useState("");

  const handleReject = () => {
    onReject(rejectReason);
    setShowRejectModal(false);
    setRejectReason("");
  };

  return (
    <div className="bg-brain-800/30 border border-brain-600/20 rounded-lg p-4 hover:border-brain-600/40 transition-colors">
      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <span className="text-slate-400 text-xs uppercase tracking-wide">
              {correction.entity_type}
            </span>
            <span className="text-slate-600">•</span>
            <span className="text-slate-500 text-xs font-mono">{correction.entity_id}</span>
          </div>
          <div className="text-white font-medium">{correction.field}</div>
        </div>
        <StatusBadge status={correction.status} />
      </div>

      <div className="grid grid-cols-2 gap-3 mb-3">
        <div className="bg-red-500/10 border border-red-500/20 rounded p-2">
          <div className="text-red-400 text-xs mb-1">{t("before_label")}</div>
          <div className="text-slate-300 text-sm font-mono break-all">
            {JSON.stringify(correction.old_value, null, 2)}
          </div>
        </div>
        <div className="bg-emerald-500/10 border border-emerald-500/20 rounded p-2">
          <div className="text-emerald-400 text-xs mb-1">{t("after_label")}</div>
          <div className="text-slate-300 text-sm font-mono break-all">
            {JSON.stringify(correction.new_value, null, 2)}
          </div>
        </div>
      </div>

      {correction.reason && (
        <div className="text-slate-400 text-sm mb-3">
          <span className="text-slate-500">{t("reason_label")}</span> {correction.reason}
        </div>
      )}

      <div className="flex items-center justify-between">
        <div className="text-slate-500 text-xs">
          {new Date(correction.created_at).toLocaleString("ru-RU")}
          {correction.source && (
            <span className="ml-2 text-slate-600">• {correction.source}</span>
          )}
        </div>

        {correction.status === "pending" && (
          <div className="flex gap-2">
            <button
              onClick={onApprove}
              className="px-3 py-1 text-sm bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded hover:bg-emerald-500/30 transition-colors"
            >
              {t('approve_button')}
            </button>
            <button
              onClick={() => setShowRejectModal(true)}
              className="px-3 py-1 text-sm bg-red-500/20 text-red-400 border border-red-500/30 rounded hover:bg-red-500/30 transition-colors"
            >
              {t('reject_button')}
            </button>
          </div>
        )}
      </div>

      {/* Reject Modal */}
      {showRejectModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-brain-800/40 border border-brain-600/30 rounded-lg p-6 w-full max-w-md">
            <h3 className="text-white text-lg font-medium mb-4">{t("reject_modal_title")}</h3>
            <textarea
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              placeholder={t("reject_reason_placeholder")}
              className="w-full bg-brain-900/40 border border-brain-600/30 rounded p-3 text-white text-sm resize-none h-24 focus:border-brain-600/40 focus:outline-none"
            />
            <div className="flex justify-end gap-2 mt-4">
              <button
                onClick={() => setShowRejectModal(false)}
                className="px-4 py-2 text-sm text-slate-400 hover:text-white transition-colors"
              >
                {t('cancel_button')}
              </button>
              <button
                onClick={handleReject}
                className="px-4 py-2 text-sm bg-red-500 text-white rounded hover:bg-red-600 transition-colors"
              >
                {t('reject_submit')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function NewCorrectionForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (data: any) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("corrections");
  const [formData, setFormData] = useState({
    entity_type: "Person",
    entity_id: "",
    field: "",
    old_value: "",
    new_value: "",
    reason: "",
  });

  const entityTypes = ["Person", "Project", "Task", "Decision", "Risk", "Company", "Department"];

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit({
      ...formData,
      old_value: formData.old_value || null,
    });
  };

  return (
    <form onSubmit={handleSubmit} className="bg-brain-800/30 border border-brain-600/20 rounded-lg p-4">
      <h3 className="text-white font-medium mb-4">{t("new_correction_title")}</h3>

      <div className="grid grid-cols-2 gap-4 mb-4">
        <div>
          <label className="block text-slate-400 text-sm mb-1">{t("entity_type_label")}</label>
          <select
            value={formData.entity_type}
            onChange={(e) => setFormData({ ...formData, entity_type: e.target.value })}
            className="w-full bg-brain-900/40 border border-brain-600/30 rounded p-2 text-white text-sm focus:border-brain-600/40 focus:outline-none"
          >
            {entityTypes.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-slate-400 text-sm mb-1">{t("entity_id_label")}</label>
          <input
            type="text"
            value={formData.entity_id}
            onChange={(e) => setFormData({ ...formData, entity_id: e.target.value })}
            placeholder="person_123"
            className="w-full bg-brain-900/40 border border-brain-600/30 rounded p-2 text-white text-sm focus:border-brain-600/40 focus:outline-none"
            required
          />
        </div>
      </div>

      <div className="mb-4">
        <label className="block text-slate-400 text-sm mb-1">{t("field_label")}</label>
        <input
          type="text"
          value={formData.field}
          onChange={(e) => setFormData({ ...formData, field: e.target.value })}
          placeholder="name, role, status..."
          className="w-full bg-brain-900/40 border border-brain-600/30 rounded p-2 text-white text-sm focus:border-brain-600/40 focus:outline-none"
          required
        />
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4">
        <div>
          <label className="block text-slate-400 text-sm mb-1">{t("old_value_label")}</label>
          <input
            type="text"
            value={formData.old_value}
            onChange={(e) => setFormData({ ...formData, old_value: e.target.value })}
            placeholder={t("old_value_placeholder")}
            className="w-full bg-brain-900/40 border border-brain-600/30 rounded p-2 text-white text-sm focus:border-brain-600/40 focus:outline-none"
          />
        </div>
        <div>
          <label className="block text-slate-400 text-sm mb-1">{t("new_value_label")}</label>
          <input
            type="text"
            value={formData.new_value}
            onChange={(e) => setFormData({ ...formData, new_value: e.target.value })}
            placeholder={t("new_value_placeholder")}
            className="w-full bg-brain-900/40 border border-brain-600/30 rounded p-2 text-white text-sm focus:border-brain-600/40 focus:outline-none"
            required
          />
        </div>
      </div>

      <div className="mb-4">
        <label className="block text-slate-400 text-sm mb-1">{t("reason_change_label")}</label>
        <textarea
          value={formData.reason}
          onChange={(e) => setFormData({ ...formData, reason: e.target.value })}
          placeholder={t("reason_change_placeholder")}
          className="w-full bg-brain-900/40 border border-brain-600/30 rounded p-2 text-white text-sm resize-none h-20 focus:border-brain-600/40 focus:outline-none"
          required
        />
      </div>

      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 text-sm text-slate-400 hover:text-white transition-colors"
        >
          {t('cancel_button')}
        </button>
        <button
          type="submit"
          className="px-4 py-2 text-sm bg-blue-500 text-white rounded hover:bg-blue-600 transition-colors"
        >
          {t('submit_button')}
        </button>
      </div>
    </form>
  );
}

// Main Component
export default function CorrectionsPanel({ userId }: { userId: string }) {
  const t = useTranslations("corrections");
  const [corrections, setCorrections] = useState<Correction[]>([]);
  const [stats, setStats] = useState<CorrectionStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showNewForm, setShowNewForm] = useState(false);
  const [filter, setFilter] = useState<"all" | "pending" | "approved" | "rejected">("pending");

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const [correctionsData, statsData] = await Promise.all([
        fetchPendingCorrections(userId),
        fetchCorrectionStats(userId),
      ]);
      setCorrections(correctionsData.corrections || []);
      setStats(statsData);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("err_loading"));
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleApprove = async (correctionId: string) => {
    try {
      await approveCorrection(correctionId, userId);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("err_approval"));
    }
  };

  const handleReject = async (correctionId: string, reason: string) => {
    try {
      await rejectCorrection(correctionId, userId, reason);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("err_rejection"));
    }
  };

  const handleSubmit = async (data: any) => {
    try {
      await submitCorrection(userId, data);
      setShowNewForm(false);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("err_submit"));
    }
  };

  const handleApplyAll = async () => {
    try {
      const result = await applyPendingCorrections(userId);
      alert(t("applied_alert", { applied: result.applied, failed: result.failed }));
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("err_apply"));
    }
  };

  const filteredCorrections = corrections.filter((c) => {
    if (filter === "all") return true;
    return c.status === filter;
  });

  return (
    <div className="h-full flex flex-col bg-brain-900/40">
      {/* Header */}
      <div className="p-4 border-b border-brain-700/30">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-white text-lg font-semibold">{t("panel_title")}</h2>
          <button
            onClick={() => setShowNewForm(!showNewForm)}
            className="px-3 py-1.5 text-sm bg-blue-500 text-white rounded hover:bg-blue-600 transition-colors"
          >
            {t('new_button')}
          </button>
        </div>

        {/* Stats */}
        {stats && (
          <div className="grid grid-cols-5 gap-2 mb-4">
            <div className="bg-brain-800/30 rounded p-2 text-center">
              <div className="text-2xl font-bold text-white">{stats.total}</div>
              <div className="text-xs text-slate-500">{t("stat_total")}</div>
            </div>
            <div className="bg-amber-500/10 rounded p-2 text-center">
              <div className="text-2xl font-bold text-amber-400">{stats.pending}</div>
              <div className="text-xs text-slate-500">{t("stat_pending")}</div>
            </div>
            <div className="bg-emerald-500/10 rounded p-2 text-center">
              <div className="text-2xl font-bold text-emerald-400">{stats.approved}</div>
              <div className="text-xs text-slate-500">{t("stat_approved")}</div>
            </div>
            <div className="bg-blue-500/10 rounded p-2 text-center">
              <div className="text-2xl font-bold text-blue-400">{stats.applied}</div>
              <div className="text-xs text-slate-500">{t("stat_applied")}</div>
            </div>
            <div className="bg-red-500/10 rounded p-2 text-center">
              <div className="text-2xl font-bold text-red-400">{stats.rejected}</div>
              <div className="text-xs text-slate-500">{t("stat_rejected")}</div>
            </div>
          </div>
        )}

        {/* Filters */}
        <div className="flex items-center gap-2">
          {(["pending", "approved", "rejected", "all"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1 text-sm rounded transition-colors ${
                filter === f
                  ? "bg-blue-500 text-white"
                  : "bg-brain-800/40 text-slate-400 hover:text-white"
              }`}
            >
              {f === "all" ? t("filter_all") : f === "pending" ? t("filter_pending") : f === "approved" ? t("filter_approved") : t("filter_rejected")}
            </button>
          ))}
          
          {stats && stats.approved > 0 && (
            <button
              onClick={handleApplyAll}
              className="ml-auto px-3 py-1 text-sm bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded hover:bg-emerald-500/30 transition-colors"
            >
              {t('apply_all_approved')}
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {error && (
          <div className="bg-red-500/20 border border-red-500/30 rounded p-3 text-red-400 text-sm">
            {error}
          </div>
        )}

        {showNewForm && (
          <NewCorrectionForm
            onSubmit={handleSubmit}
            onCancel={() => setShowNewForm(false)}
          />
        )}

        {loading ? (
          <div className="text-center text-slate-500 py-8">{t("loading")}</div>
        ) : filteredCorrections.length === 0 ? (
          <div className="text-center text-slate-500 py-8">
            {filter === "pending" ? t("no_pending") : t("no_corrections")}
          </div>
        ) : (
          filteredCorrections.map((correction) => (
            <CorrectionCard
              key={correction.id}
              correction={correction}
              onApprove={() => handleApprove(correction.id)}
              onReject={(reason) => handleReject(correction.id, reason)}
            />
          ))
        )}
      </div>
    </div>
  );
}


