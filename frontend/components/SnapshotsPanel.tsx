"use client";

import React, { useState, useEffect, useCallback } from "react";
import { authFetch } from "@/lib/authFetch";
import { useTranslations } from "next-intl";
import Graph3DView from "@/components/Graph3DView";
import OrgSchemeView from "@/components/OrgSchemeView";
import SnapshotTimeline from "@/components/SnapshotTimeline";
import OrgGraphView from "@/components/OrgGraphView";
import PsychInsightsBlock from "@/components/PsychInsightsBlock";

// Types
interface CompanySnapshot {
  name: string;
  mission: string;
  description: string;
  industry: string;
  size: string;
  founded: string;
  location: string;
  website: string;
  stage: string;
  
  // Founder
  founder: {
    name: string;
    role: string;
    background: string;
    linkedin?: string;
  };
  
  // Products & Business
  products: Array<{ name: string; description: string; status: string; target_audience: string }>;
  business_model: string;
  revenue_model: string;
  target_market: string;
  competitors: string[];
  
  // Status
  current_status: string;
  current_priorities: string[];
  current_challenges: string[];
  
  // Structure
  departments: Array<{ name: string; head: string; employees_count: number; description: string; members?: string[] }>;
  teams: Array<{ name: string; lead: string; members_count: number; focus: string }>;
  key_people: Array<{ person_id?: string; name: string; role: string; responsibilities?: string }>;
  related_people?: Array<{ person_id?: string; name: string; role: string; category?: string }>;

  // Projects
  active_projects: Array<{ name: string; status: string; progress?: number; description: string; lead: string }>;
  completed_projects_count: number;
  
  // Goals
  strategic_goals: Array<{ goal: string; timeframe: string; progress: number }>;
  
  // SWOT
  strengths: string[];
  weaknesses: string[];
  opportunities: string[];
  threats: string[];
  
  // Achievements & Problems
  recent_achievements: Array<{ achievement: string; date: string; impact: string }>;
  current_problems: Array<{ problem: string; severity: string; responsible: string; status: string }>;
  
  // Tech
  tech_stack: string[];
  tools: string[];
  
  // Metrics
  kpis: Array<{ name: string; current_value: any; target?: any; trend: string }>;
  health_score: number;
  
  // Resources
  resources: Array<{ name: string; url: string; type: string }>;
  
  last_updated: string;
  version: number;
  snippet: string;
  delta_summary: string;
}

interface PersonSnapshot {
  person_id: string;
  name: string;
  role: string;
  department: string;
  email: string;
  phone: string;
  manager: string;
  colleagues: string[];
  current_tasks: Array<{ task: string; status: string; deadline: string }>;
  skills: Array<{ skill: string; level: string }>;
  performance: {
    rating: number;
    strengths: string[];
    areas_to_improve: string[];
    recent_achievements: string[];
  };
  communication_style: string;
  preferences: Record<string, any>;
  last_updated: string;
  // Скоринг вклада (грунтуется на атрибутированных данных графа).
  decisions_made?: number;
  collaboration_score?: number;
  meetings_participated?: number;
  tasks_completed_week?: number;
  tasks_in_progress?: number;
  // История из TemporalTracker (приходит отдельным полем ответа, мержим сюда).
  timeline?: Array<{ event_type?: string; entity_type?: string; event_time?: string; description?: string; meeting_id?: string }>;
  // Атрибутированный вклад из графа (что человек реально делал).
  decisions?: Array<{ summary: string; category?: string; date?: string }>;
  ideas?: string[];
  opinions?: Array<{ summary: string; sentiment?: string }>;
  contradictions?: string[];
  recent_achievements?: string[];
  current_challenges?: string[];
  current_projects?: Array<{ project: string; role?: string; status?: string }>;
  areas_for_improvement?: string[];
  psychological?: {
    personality_type?: string; team_role?: string; leadership_style?: string;
    communication_style?: string; dominant_traits?: string[]; strengths?: string[];
    motivation_drivers?: string[];
  };
}

interface ProjectSnapshot {
  project_id: string;
  name: string;
  description: string;
  status: string;
  progress: number;
  lead: string;
  team_members: Array<{ name: string }>;
  start_date: string;
  deadline: string;
  tasks_total: number;
  tasks_completed: number;
  tasks_in_progress: number;
  tasks_blocked: number;
  risks: Array<{ risk: string; severity: string }>;
  recent_decisions: Array<{ decision: string; date: string }>;
  // Считаются в бэке, но не показывались
  milestones?: Array<{ name?: string; title?: string; status?: string; date?: string; due_date?: string }>;
  blockers?: string[];
  health_score?: number;
  velocity?: number;
  last_updated: string;
  timeline?: Array<{ event_type?: string; entity_type?: string; event_time?: string; description?: string; meeting_id?: string }>;
}

// API functions
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function fetchCompanySnapshot(userId: string, format: "json" | "text" = "json", regenerate = false) {
  const res = await authFetch(
    `${API_BASE}/api/v1/snapshots/enhanced/company?user_id=${userId}&format=${format}${regenerate ? "&regenerate=true" : ""}`
  );
  if (!res.ok) throw new Error("Failed to fetch company snapshot");
  return res.json();
}

async function fetchPersonSnapshot(userId: string, personId: string, format: "json" | "text" = "json", name = "") {
  // name — запасной ключ: синтетический id из иерархии (p:…) может не быть
  // узлом графа, а по имени бэкенд человека резолвит штатно
  const nameParam = name ? `&name=${encodeURIComponent(name)}` : "";
  const res = await authFetch(
    `${API_BASE}/api/v1/snapshots/enhanced/person/${encodeURIComponent(personId)}?user_id=${userId}&format=${format}${nameParam}`
  );
  if (!res.ok) throw new Error("Failed to fetch person snapshot");
  return res.json();
}

async function fetchProjectSnapshot(userId: string, projectId: string, format: "json" | "text" = "json") {
  const res = await authFetch(
    `${API_BASE}/api/v1/snapshots/enhanced/project/${projectId}?user_id=${userId}&format=${format}`
  );
  if (!res.ok) throw new Error("Failed to fetch project snapshot");
  return res.json();
}

// Создать исправление факта (Two-Phase Corrections, ФАЗА 1)
async function submitFactCorrection(
  userId: string,
  data: {
    entity_id: string;
    entity_type: string;
    field: string;
    old_value: any;
    new_value: any;
    reason: string;
  }
) {
  const res = await authFetch(`${API_BASE}/api/v1/corrections/?user_id=${userId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to submit correction");
  const result = await res.json();
  if (result.success === false) throw new Error(result.error || "Failed to submit correction");
  return result;
}

// Ручная правка поля снапшота компании (переживает ре-генерацию, не
// перетирается авто-генерацией — в отличие от fact-correction по графу).
async function submitCompanyOverride(userId: string, field: string, value: any) {
  const res = await authFetch(
    `${API_BASE}/api/v1/snapshots/enhanced/company/override?user_id=${userId}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field, value }),
    }
  );
  if (!res.ok) throw new Error("Failed to save override");
  const result = await res.json();
  if (result.status === "error") throw new Error(result.message || "Failed to save override");
  return result; // { status, field, snapshot }
}

// Ручная классификация человека (employee/partner/external/candidate/client).
// Позволяет перевести человека, напр., из «внешних» в «сотрудники».
async function submitPersonClassification(
  userId: string,
  personIdOrName: string,
  classification: string
) {
  const res = await authFetch(
    `${API_BASE}/api/v1/snapshots/enhanced/person/${encodeURIComponent(personIdOrName)}/classification?user_id=${userId}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ classification }),
    }
  );
  if (!res.ok) throw new Error("Failed to set classification");
  const result = await res.json();
  if (result.status === "error") throw new Error(result.message || "Failed to set classification");
  return result; // { status, snapshot }
}

// Удалить сущность из снепшота НАСОВСЕМ (ошибка распознавания: «Ишка» вместо
// «ИИшка»). Узел удаляется из графа + tombstone «не пересоздавать» — удаление
// переживает пересборку памяти.
async function deleteSnapshotEntity(
  userId: string,
  name: string,
  opts?: { entityId?: string; label?: string; comment?: string }
) {
  const res = await authFetch(
    `${API_BASE}/api/v1/snapshots/enhanced/entity/delete?user_id=${userId}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        entity_id: opts?.entityId || null,
        label: opts?.label || null,
        comment: opts?.comment || null,
      }),
    }
  );
  if (!res.ok) throw new Error("Failed to delete entity");
  const result = await res.json();
  if (result.status === "error") throw new Error(result.message || "Failed to delete entity");
  return result; // { status, removed_node_ids, snapshot }
}

// Узлы по метке (Goal / ConflictRisk / Scenario / Prediction).
async function fetchByLabel(userId: string, label: string): Promise<any[]> {
  try {
    const res = await authFetch(`${API_BASE}/api/v1/entities/by-label?label=${label}&user_id=${userId}&limit=50`);
    if (!res.ok) return [];
    const data = await res.json();
    return data.items || [];
  } catch { return []; }
}

// Маркетинговые наработки Mark (Knowledge category=marketing).
async function fetchMarketing(userId: string): Promise<any[]> {
  try {
    const res = await authFetch(`${API_BASE}/api/v1/entities/by-label?label=Knowledge&category=marketing&user_id=${userId}&limit=20`);
    if (!res.ok) return [];
    const data = await res.json();
    return data.items || [];
  } catch { return []; }
}

// Лента сущностей: by=mentions («в фокусе») или by=salience («горячее»).
async function fetchMostMentioned(userId: string, by: string = "mentions"): Promise<any[]> {
  try {
    const res = await authFetch(`${API_BASE}/api/v1/entities/most-mentioned?user_id=${userId}&limit=15&by=${by}`);
    if (!res.ok) return [];
    const data = await res.json();
    return data.items || [];
  } catch { return []; }
}

// Принципы (Wisdom) — с доказательной базой.
async function fetchPrinciples(userId: string): Promise<any[]> {
  try {
    const res = await authFetch(`${API_BASE}/api/v1/wisdom/principles?user_id=${userId}`);
    if (!res.ok) return [];
    const data = await res.json();
    return data.items || [];
  } catch { return []; }
}

// Правила (Rule Book) — регламенты компании.
async function fetchRules(userId: string): Promise<any[]> {
  try {
    const res = await authFetch(`${API_BASE}/api/v1/wisdom/rules?user_id=${userId}`);
    if (!res.ok) return [];
    const data = await res.json();
    return data.rules || [];
  } catch { return []; }
}

// Список людей для выбора (CEO/руководитель и т.п. — не free-text, а выбор).
async function fetchPeopleList(userId: string): Promise<Array<{ person_id: string; name: string; role?: string }>> {
  try {
    const res = await authFetch(`${API_BASE}/api/v1/snapshots/people?user_id=${userId}`);
    if (!res.ok) return [];
    const data = await res.json();
    return (data.profiles || []).map((p: any) => ({
      person_id: p.person_id || p.id || "",
      name: p.name || "",
      role: p.role || "",
    })).filter((p: any) => p.name);
  } catch {
    return [];
  }
}

// Components
function ProgressBar({ value, max = 100, color = "blue" }: { value: number; max?: number; color?: string }) {
  const percent = Math.min(100, (value / max) * 100);
  const colors = {
    blue: "bg-blue-500",
    green: "bg-emerald-500",
    yellow: "bg-amber-500",
    red: "bg-red-500",
  };
  
  return (
    <div className="w-full bg-brain-700/40 rounded-full h-2">
      <div
        className={`${colors[color as keyof typeof colors] || colors.blue} h-2 rounded-full transition-all`}
        style={{ width: `${percent}%` }}
      />
    </div>
  );
}

function SeverityBadge({ severity }: { severity: string }) {
  const colors: Record<string, string> = {
    high: "bg-red-500/20 text-red-400 border-red-500/30",
    critical: "bg-red-500/20 text-red-400 border-red-500/30",
    medium: "bg-amber-500/20 text-amber-400 border-amber-500/30",
    low: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  };
  
  return (
    <span className={`px-2 py-0.5 text-xs rounded border ${colors[severity.toLowerCase()] || colors.medium}`}>
      {severity}
    </span>
  );
}

// Метка «давно не подтверждалось». Бэкенд считает уровень теми же
// порогами, что и механизм архивации, и сам решает, показывать ли:
// свежее и недавнее не помечаем, иначе метка на каждой карточке
// перестанет что-либо значить там, где она действительно нужна.
type Freshness = { level: string; age_days: number | null; label: string; show: boolean };

function FreshnessBadge({ f }: { f?: Freshness }) {
  if (!f?.show) return null;
  const tone = f.level === "ancient"
    ? "bg-red-500/15 text-red-300"
    : f.level === "stale"
      ? "bg-amber-500/15 text-amber-300"
      : "bg-slate-500/15 text-slate-400";
  const months = f.age_days != null ? Math.round(f.age_days / 30) : null;
  return (
    <span className={`px-1.5 py-0.5 text-[10px] rounded whitespace-nowrap ${tone}`}
          title={f.age_days != null ? `${f.age_days} дн. с последнего обновления` : undefined}>
      {f.label}{months ? ` · ${months} мес.` : ""}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    active: "bg-emerald-500/20 text-emerald-400",
    completed: "bg-blue-500/20 text-blue-400",
    blocked: "bg-red-500/20 text-red-400",
    pending: "bg-amber-500/20 text-amber-400",
    in_progress: "bg-cyan-500/20 text-cyan-400",
  };
  
  return (
    <span className={`px-2 py-0.5 text-xs rounded ${colors[status.toLowerCase()] || "bg-slate-500/20 text-slate-400"}`}>
      {status}
    </span>
  );
}

function StageBadge({ stage }: { stage: string }) {
  const t = useTranslations("snapshots_panel");
  const stageConfig: Record<string, { label: string; color: string }> = {
    idea: { label: t("stage_idea"), color: "bg-purple-500/20 text-purple-400 border-purple-500/30" },
    mvp: { label: "MVP", color: "bg-amber-500/20 text-amber-400 border-amber-500/30" },
    growth: { label: t("stage_growth"), color: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30" },
    scale: { label: t("stage_scale"), color: "bg-blue-500/20 text-blue-400 border-blue-500/30" },
    mature: { label: t("stage_mature"), color: "bg-slate-500/20 text-slate-400 border-slate-500/30" },
  };
  const cfg = stageConfig[stage?.toLowerCase()] || { label: stage, color: "bg-slate-500/20 text-slate-400 border-slate-500/30" };
  return (
    <span className={`px-2 py-0.5 text-xs rounded border ${cfg.color}`}>{cfg.label}</span>
  );
}

function SectionCard({ title, icon, children, accent }: { title: string; icon: string; children: React.ReactNode; accent?: string }) {
  const borderColor = accent || "border-brain-600/20";
  return (
    <div className={`bg-brain-800/30 rounded-xl p-5 border ${borderColor}`}>
      <h4 className="text-white font-semibold mb-3 flex items-center gap-2 text-sm uppercase tracking-wide">
        <span>{icon}</span> {title}
      </h4>
      {children}
    </div>
  );
}

// Редактируемая оргструктура: отдел → руководитель (выбор из людей) →
// участники (добавить/убрать). Работает на данных снапшота (departments с
// head/members), правки сохраняются как company-override "departments" и
// переживают ре-генерацию.
// Настоящий выпадающий список людей (вместо datalist, который показывал
// варианты только при наборе). Текущее значение, если его нет в списке,
// добавляется как опция — чтобы не потерять уже вписанное вручную.
function PersonSelect({ value, people, onChange, placeholder, className }: {
  value: string;
  people: Array<{ person_id: string; name: string; role?: string }>;
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
}) {
  const t = useTranslations("snapshots_panel");
  const opts = [...people];
  if (value && !opts.some((p) => p.name === value)) opts.unshift({ person_id: "", name: value });
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={className || "flex-1 px-2 py-1 bg-brain-800/40 border border-brain-600/40 rounded text-white text-xs"}
    >
      <option value="">{placeholder || t("select_person_placeholder")}</option>
      {opts.map((p) => (
        <option key={p.person_id || p.name} value={p.name}>
          {p.role ? `${p.name} — ${p.role}` : p.name}
        </option>
      ))}
    </select>
  );
}

function OrgStructureEditor({
  departments, userId, people, onUpdated,
}: {
  departments: Array<{ name: string; head: string; employees_count: number; description: string; members?: string[] }>;
  userId?: string;
  people: Array<{ person_id: string; name: string; role?: string }>;
  onUpdated?: (snap: CompanySnapshot) => void;
}) {
  const t = useTranslations("snapshots_panel");
  const [depts, setDepts] = useState(() => departments.map((d) => ({ ...d, members: d.members || [] })));
  const [saving, setSaving] = useState(false);
  const [addMemberFor, setAddMemberFor] = useState<number | null>(null);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    setDepts(departments.map((d) => ({ ...d, members: d.members || [] })));
  }, [departments]);

  const persist = async (next: typeof depts) => {
    setDepts(next);
    if (!userId) return;
    setSaving(true);
    try {
      const payload = next.map((d) => ({
        name: d.name, description: d.description, head: d.head,
        members: d.members, employees_count: (d.members || []).length,
      }));
      const res = await submitCompanyOverride(userId, "departments", payload);
      if (res?.snapshot && onUpdated) onUpdated(res.snapshot);
    } catch (e) {
      console.error("Failed to save org structure:", e);
    } finally {
      setSaving(false);
    }
  };

  const setHead = (i: number, head: string) => {
    const next = depts.map((d, j) => (j === i ? { ...d, head } : d));
    persist(next);
  };
  const removeMember = (i: number, name: string) => {
    const next = depts.map((d, j) => (j === i ? { ...d, members: (d.members || []).filter((m) => m !== name) } : d));
    persist(next);
  };
  const addMember = (i: number, name: string) => {
    const nm = name.trim();
    if (!nm) return;
    const next = depts.map((d, j) => (j === i && !(d.members || []).includes(nm)
      ? { ...d, members: [...(d.members || []), nm] } : d));
    persist(next);
    setAddMemberFor(null);
    setDraft("");
  };

  return (
    <SectionCard title={t("org_structure_title")} icon="🏢" accent="border-teal-500/30">
      {saving && <div className="text-[11px] text-teal-300 mb-2">{t("edit_saving")}</div>}
      <div className="grid gap-3 grid-cols-1">
        {depts.map((dept, i) => (
          <div key={i} className="bg-brain-900/30 rounded-lg p-3 border border-brain-700/25">
            <div className="flex items-center justify-between gap-2 mb-2">
              <div className="text-white text-sm font-semibold truncate">🏢 {dept.name}</div>
              <span className="text-xs text-teal-300 bg-teal-500/10 px-2 py-0.5 rounded flex-shrink-0">
                {t("dept_members_count", { count: (dept.members || []).length })}
              </span>
            </div>
            {dept.description && <div className="text-slate-500 text-xs mb-2 line-clamp-2">{dept.description}</div>}
            {/* Руководитель — выбор из людей */}
            <div className="flex items-center gap-2 mb-2">
              <span className="text-slate-400 text-xs flex-shrink-0">{t("dept_head_label")}</span>
              {userId ? (
                <PersonSelect
                  value={dept.head || ""}
                  people={people}
                  onChange={(v) => { if (v !== (dept.head || "")) setHead(i, v); }}
                  placeholder={t("select_head_placeholder")}
                />
              ) : (
                <span className="text-slate-300 text-xs">{dept.head || "—"}</span>
              )}
            </div>
            {/* Участники — чипы с удалением + добавление */}
            <div className="flex flex-wrap gap-1.5 items-center">
              {(dept.members || []).map((m) => (
                <span key={m} className="inline-flex items-center gap-1 text-[11px] text-teal-200 bg-teal-500/10 px-2 py-0.5 rounded">
                  {m}
                  {userId && (
                    <button onClick={() => removeMember(i, m)} className="text-teal-400 hover:text-red-300" title={t("remove_from_dept_title")}>✕</button>
                  )}
                </span>
              ))}
              {userId && addMemberFor === i ? (
                <PersonSelect
                  value=""
                  people={people}
                  onChange={(v) => { if (v) addMember(i, v); else setAddMemberFor(null); }}
                  placeholder={t("add_person_placeholder")}
                  className="px-2 py-0.5 bg-brain-800/40 border border-brain-600/40 rounded text-white text-[11px]"
                />
              ) : userId ? (
                <button onClick={() => { setAddMemberFor(i); setDraft(""); }} className="text-[11px] text-slate-400 hover:text-teal-300 px-1.5 py-0.5 rounded border border-brain-600/20">
                  {t("add_member_button")}
                </button>
              ) : null}
            </div>
          </div>
        ))}
      </div>
    </SectionCard>
  );
}

function CompanySnapshotView({ snapshot, userId, onUpdated }: {
  snapshot: CompanySnapshot;
  userId?: string;
  onUpdated?: (snap: CompanySnapshot) => void;
}) {
  const t = useTranslations("snapshots_panel");
  const s = snapshot;
  const [editingFounder, setEditingFounder] = useState(false);
  const [founderName, setFounderName] = useState(s.founder?.name || "");
  const [founderRole, setFounderRole] = useState(s.founder?.role || "");
  const [savingFounder, setSavingFounder] = useState(false);
  const [people, setPeople] = useState<Array<{ person_id: string; name: string; role?: string }>>([]);
  const [classBusy, setClassBusy] = useState<string | null>(null);
  const [principles, setPrinciples] = useState<any[]>([]);
  const [rules, setRules] = useState<any[]>([]);
  const [focus, setFocus] = useState<any[]>([]);
  const [hot, setHot] = useState<any[]>([]);
  const [goals, setGoals] = useState<any[]>([]);
  const [conflictRisks, setConflictRisks] = useState<any[]>([]);
  const [predictions, setPredictions] = useState<any[]>([]);
  const [scenarios, setScenarios] = useState<any[]>([]);
  const [marketing, setMarketing] = useState<any[]>([]);
  const [openMkt, setOpenMkt] = useState<number | null>(null);

  useEffect(() => {
    if (userId) {
      fetchPeopleList(userId).then(setPeople).catch(() => {});
      fetchPrinciples(userId).then(setPrinciples).catch(() => {});
      fetchRules(userId).then(setRules).catch(() => {});
      fetchMostMentioned(userId, "mentions").then(setFocus).catch(() => {});
      fetchMostMentioned(userId, "salience").then(setHot).catch(() => {});
      fetchByLabel(userId, "Goal").then(setGoals).catch(() => {});
      fetchByLabel(userId, "ConflictRisk").then(setConflictRisks).catch(() => {});
      fetchByLabel(userId, "Prediction").then(setPredictions).catch(() => {});
      fetchByLabel(userId, "Scenario").then(setScenarios).catch(() => {});
      fetchMarketing(userId).then(setMarketing).catch(() => {});
    }
  }, [userId]);

  // Перевести человека в другую классификацию (external→internal и т.п.)
  const reclassify = async (personIdOrName: string, classification: string) => {
    if (!userId || !personIdOrName) return;
    setClassBusy(personIdOrName + classification);
    try {
      const res = await submitPersonClassification(userId, personIdOrName, classification);
      if (res?.snapshot && onUpdated) onUpdated(res.snapshot);
    } catch (e) {
      console.error("Failed to reclassify person:", e);
    } finally {
      setClassBusy(null);
    }
  };

  // Удалить сущность насовсем (ошибка распознавания, не человек и т.п.).
  // Tombstone на бэке гарантирует, что извлечение её не пересоздаст.
  const removeEntity = async (person: { person_id?: string; name: string }, label = "Person") => {
    if (!userId || !person?.name) return;
    if (!window.confirm(t("confirm_delete_entity", { name: person.name }))) return;
    const key = (person.person_id || person.name) + "__delete";
    setClassBusy(key);
    try {
      const res = await deleteSnapshotEntity(userId, person.name, {
        entityId: person.person_id, label,
        comment: "удалено пользователем из снепшота",
      });
      if (res?.snapshot && onUpdated) onUpdated(res.snapshot);
    } catch (e) {
      console.error("Failed to delete entity:", e);
    } finally {
      setClassBusy(null);
    }
  };

  const saveFounder = async () => {
    if (!userId) return;
    setSavingFounder(true);
    try {
      const res = await submitCompanyOverride(userId, "founder", {
        name: founderName,
        role: founderRole,
        background: s.founder?.background || "",
      });
      if (res?.snapshot && onUpdated) onUpdated(res.snapshot);
      setEditingFounder(false);
    } catch (e) {
      console.error("Failed to save founder override:", e);
    } finally {
      setSavingFounder(false);
    }
  };

  // Закрепить имя компании (override): авто-генерация имя не меняет, но
  // если LLM когда-то успел его переврать — правка фиксирует правильное
  // и переживает все регенерации.
  const renameCompany = async () => {
    if (!userId) return;
    const next = window.prompt(t("company_name_prompt"), s.name || "");
    if (next === null) return;
    const clean = next.trim();
    if (!clean || clean === s.name) return;
    try {
      const res = await submitCompanyOverride(userId, "name", clean);
      if (res?.snapshot && onUpdated) onUpdated(res.snapshot);
    } catch (e) {
      console.error("Failed to rename company:", e);
    }
  };

  return (
    <div className="space-y-4">
      {/* ═══ Header Card — Company Overview ═══ */}
      <div className="bg-gradient-to-br from-blue-600/20 via-purple-600/15 to-indigo-600/20 rounded-xl p-6 border border-blue-500/30 relative overflow-hidden">
        <div className="absolute top-0 right-0 w-32 h-32 bg-blue-500/5 rounded-full -mr-10 -mt-10" />
        <div className="relative">
          <div className="flex items-start justify-between">
            <div>
              <h3 className="text-2xl font-bold text-white mb-1">
                {s.name || t("company_fallback")}
                {userId && (
                  <button
                    onClick={renameCompany}
                    title={t("rename_company_title")}
                    className="ml-2 text-slate-500 hover:text-white text-sm align-middle"
                  >✏️</button>
                )}
              </h3>
              {s.industry && <p className="text-blue-300 text-sm font-medium">{s.industry}</p>}
            </div>
            <div className="flex items-center gap-2">
              {s.stage && <StageBadge stage={s.stage} />}
              {s.current_status && <StatusBadge status={s.current_status} />}
            </div>
          </div>
          
          {s.description && (
            <p className="text-slate-300 text-sm mt-3 leading-relaxed">{s.description}</p>
          )}
          {s.mission && s.mission !== s.description && (
            <p className="text-slate-400 text-sm mt-2 italic">&quot;{s.mission}&quot;</p>
          )}
          
          <div className="flex flex-wrap gap-4 mt-4 text-xs text-slate-400">
            {s.founded && <span>{t("founded_prefix", { value: s.founded })}</span>}
            {s.location && <span>📍 {s.location}</span>}
            {s.website && <span>🌐 {s.website}</span>}
            {s.key_people?.length > 0 && <span>{t("people_count_prefix", { count: s.key_people.length })}</span>}
          </div>
        </div>
      </div>

      {/* ═══ Founder / CEO (с ручной правкой — override) ═══ */}
      {(s.founder?.name || userId) && (
        <SectionCard title={t("section_founder")} icon="👤" accent="border-cyan-500/30">
          {editingFounder ? (
            <div className="space-y-2">
              <PersonSelect
                value={founderName}
                people={people}
                onChange={(v) => {
                  setFounderName(v);
                  const picked = people.find((p) => p.name === v);
                  if (picked?.role) setFounderRole(picked.role);
                }}
                placeholder={t("select_founder_placeholder")}
                className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm"
              />
              <input
                value={founderRole}
                onChange={(e) => setFounderRole(e.target.value)}
                placeholder={t("founder_role_placeholder")}
                className="w-full px-3 py-2 bg-brain-800/40 border border-brain-600/40 rounded text-white text-sm"
              />
              <div className="flex gap-2">
                <button
                  onClick={saveFounder}
                  disabled={savingFounder}
                  className="px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white rounded text-sm"
                >
                  {savingFounder ? t("edit_saving") : t("edit_save")}
                </button>
                <button
                  onClick={() => { setEditingFounder(false); setFounderName(s.founder?.name || ""); setFounderRole(s.founder?.role || ""); }}
                  className="px-3 py-1.5 bg-brain-700/40 hover:bg-brain-600/60 text-white rounded text-sm"
                >
                  {t("edit_cancel")}
                </button>
              </div>
            </div>
          ) : (
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-start gap-4">
                <div className="w-12 h-12 rounded-full bg-gradient-to-br from-cyan-500/30 to-blue-500/30 flex items-center justify-center text-white text-lg font-bold border border-cyan-500/20">
                  {(s.founder?.name || "?").charAt(0)}
                </div>
                <div>
                  <div className="text-white font-medium">{s.founder?.name || t("not_specified")}</div>
                  {s.founder?.role && <div className="text-cyan-400 text-sm">{s.founder.role}</div>}
                  {s.founder?.background && <div className="text-slate-400 text-sm mt-1">{s.founder.background}</div>}
                </div>
              </div>
              {userId && (
                <button
                  onClick={() => { setFounderName(s.founder?.name || ""); setFounderRole(s.founder?.role || ""); setEditingFounder(true); }}
                  className="text-slate-400 hover:text-cyan-300 text-sm px-2 py-1"
                  title={t("edit_founder_title")}
                >
                  ✎
                </button>
              )}
            </div>
          )}
        </SectionCard>
      )}

      {/* ═══ Products & Business ═══ */}
      {((s.products?.length > 0) || s.business_model || s.target_market) && (
        <SectionCard title={t("section_products")} icon="📦" accent="border-purple-500/30">
          {s.products?.length > 0 && (
            <div className="space-y-3 mb-4">
              {s.products.map((product, i) => (
                <div key={i} className="bg-brain-900/40 rounded-lg p-3 border border-brain-600/20">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-white font-medium text-sm">{product.name}</span>
                    {product.status && <StatusBadge status={product.status} />}
                  </div>
                  {product.description && <p className="text-slate-400 text-xs">{product.description}</p>}
                  {product.target_audience && (
                    <p className="text-slate-500 text-xs mt-1">{t("audience_label", { value: product.target_audience })}</p>
                  )}
                </div>
              ))}
            </div>
          )}
          
          <div className="grid grid-cols-2 gap-3">
            {s.business_model && (
              <div className="bg-brain-900/20 rounded p-3">
                <div className="text-slate-500 text-xs mb-1">{t("business_model_label")}</div>
                <div className="text-slate-300 text-sm">{s.business_model}</div>
              </div>
            )}
            {s.target_market && (
              <div className="bg-brain-900/20 rounded p-3">
                <div className="text-slate-500 text-xs mb-1">{t("target_market_label")}</div>
                <div className="text-slate-300 text-sm">{s.target_market}</div>
              </div>
            )}
            {s.revenue_model && (
              <div className="bg-brain-900/20 rounded p-3">
                <div className="text-slate-500 text-xs mb-1">{t("monetization_label")}</div>
                <div className="text-slate-300 text-sm">{s.revenue_model}</div>
              </div>
            )}
          </div>
          
          {s.competitors?.length > 0 && (
            <div className="mt-3">
              <div className="text-slate-500 text-xs mb-1">{t("competitors_label")}</div>
              <div className="flex flex-wrap gap-2">
                {s.competitors.map((c, i) => (
                  <span key={i} className="px-2 py-1 bg-brain-700/40 text-slate-300 text-xs rounded">{c}</span>
                ))}
              </div>
            </div>
          )}
        </SectionCard>
      )}

      {/* ═══ Current Status & Priorities ═══ */}
      <SectionCard title={t("section_current_state")} icon="📊" accent="border-amber-500/30">
        <div className="space-y-3">
          {s.current_priorities?.length > 0 && (
            <div>
              <div className="text-slate-500 text-xs mb-2 uppercase tracking-wide">{t("priorities_label")}</div>
              <div className="space-y-1.5">
                {s.current_priorities.slice(0, 5).map((p, i) => (
                  <div key={i} className="flex items-start gap-2 text-sm">
                    <span className="text-amber-400 mt-0.5 text-xs">●</span>
                    <span className="text-slate-300">{p}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          
          {s.current_challenges?.length > 0 && (
            <div className="mt-3">
              <div className="text-slate-500 text-xs mb-2 uppercase tracking-wide">{t("challenges_label")}</div>
              <div className="space-y-1.5">
                {s.current_challenges.slice(0, 5).map((c, i) => (
                  <div key={i} className="flex items-start gap-2 text-sm">
                    <span className="text-red-400 mt-0.5 text-xs">▲</span>
                    <span className="text-slate-300">{c}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </SectionCard>

      {/* ═══ Team ═══ */}
      {s.key_people?.length > 0 && (
        <SectionCard title={t("section_team")} icon="👥" accent="border-cyan-500/30">
          <div className="grid gap-2 grid-cols-1 sm:grid-cols-2">
            {s.key_people.slice(0, 12).map((person, i) => {
              const pkey = person.person_id || person.name;
              return (
              <div key={i} className="flex items-center gap-3 bg-brain-900/30 rounded-lg p-2.5 border border-brain-700/25">
                <div className="w-8 h-8 rounded-full bg-gradient-to-br from-cyan-500/20 to-blue-500/20 flex items-center justify-center text-white text-xs font-bold flex-shrink-0">
                  {person.name?.charAt(0) || "?"}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="text-white text-sm font-medium truncate">{person.name}</div>
                  {person.role && <div className="text-slate-500 text-xs truncate">{person.role}</div>}
                </div>
                {userId && (
                  <button
                    onClick={() => reclassify(pkey, "external")}
                    disabled={classBusy === pkey + "external"}
                    className="text-[10px] text-slate-400 hover:text-amber-300 px-1.5 py-0.5 rounded border border-brain-600/20 flex-shrink-0 disabled:opacity-50"
                    title={t("reclassify_external_title")}
                  >
                    {t("to_external_button")}
                  </button>
                )}
                {userId && (
                  <button
                    onClick={() => removeEntity(person)}
                    disabled={classBusy === pkey + "__delete"}
                    className="text-[10px] text-slate-500 hover:text-rose-400 px-1.5 py-0.5 rounded border border-brain-600/20 flex-shrink-0 disabled:opacity-50"
                    title={t("delete_entity_title")}
                  >
                    ✕
                  </button>
                )}
              </div>
            );})}
          </div>
        </SectionCard>
      )}

      {/* ═══ В фокусе (топ по упоминаниям) ═══ */}
      {focus.length > 0 && (
        <SectionCard title={t("section_focus")} icon="🔥" accent="border-rose-500/30">
          <div className="flex flex-wrap gap-2">
            {focus.map((it, i) => (
              <span key={i} className="inline-flex items-center gap-1.5 text-sm bg-brain-900/30 border border-brain-600/20 rounded-full px-3 py-1">
                <span>{it.emoji}</span>
                <span className="text-white">{it.name}</span>
                <span className="text-rose-300/80 text-xs font-mono">{it.mentions}</span>
              </span>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Горячее (по эмоциональной значимости) ═══ */}
      {hot.length > 0 && (
        <SectionCard title={t("section_hot")} icon="🌡️" accent="border-orange-500/30">
          <div className="flex flex-wrap gap-2">
            {hot.map((it, i) => (
              <span key={i} className="inline-flex items-center gap-1.5 text-sm bg-brain-900/30 border border-orange-700/30 rounded-full px-3 py-1">
                <span>{it.emoji}</span>
                <span className="text-white">{it.name}</span>
                <span className="text-orange-300/80 text-xs font-mono">{Math.round((it.score ?? 0) * 100)}</span>
              </span>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Цели компании (goals_tracking) ═══ */}
      {goals.length > 0 && (
        <SectionCard title={t("section_goals_progress")} icon="🎯" accent="border-emerald-500/30">
          <div className="space-y-2">
            {goals.slice(0, 15).map((g, i) => (
              <div key={i} className="bg-brain-900/30 rounded-lg p-2.5 border border-brain-700/25">
                <div className="flex items-start justify-between gap-2">
                  <div className="text-white text-sm">{g.goal_statement}</div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <FreshnessBadge f={g.freshness} />
                    {g.status && <StatusBadge status={g.status} />}
                  </div>
                </div>
                <div className="flex items-center gap-2 mt-1 flex-wrap">
                  {g.level && <span className="text-[10px] text-slate-500">{g.level}{g.entity_name ? `: ${g.entity_name}` : ""}</span>}
                  {g.timeline && <span className="text-[10px] text-slate-500">⏱ {g.timeline}</span>}
                  {(g.progress_percentage ?? 0) > 0 && (
                    <span className="text-[10px] text-emerald-300">{g.progress_percentage}%</span>
                  )}
                </div>
                {(g.progress_percentage ?? 0) > 0 && (
                  <div className="w-full bg-brain-700/40 rounded-full h-1 mt-1">
                    <div className="bg-emerald-500 h-1 rounded-full" style={{ width: `${Math.min(100, g.progress_percentage)}%` }} />
                  </div>
                )}
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Риски конфликтов (conflict_risks) ═══ */}
      {conflictRisks.length > 0 && (
        <SectionCard title={t("section_conflict_risks")} icon="⚠️" accent="border-amber-500/30">
          <div className="space-y-2">
            {conflictRisks.slice(0, 12).map((c, i) => (
              <div key={i} className="bg-brain-900/30 rounded-lg p-2.5 border border-brain-700/25">
                <div className="flex items-start justify-between gap-2">
                  <div className="text-white text-sm">{c.description}</div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <FreshnessBadge f={c.freshness} />
                    {c.risk_level && <SeverityBadge severity={c.risk_level} />}
                  </div>
                </div>
                <div className="flex items-center gap-2 mt-1 flex-wrap text-[10px] text-slate-500">
                  {Array.isArray(c.parties_involved) && c.parties_involved.length > 0 && <span>👥 {c.parties_involved.join(", ")}</span>}
                  {(c.probability ?? 0) > 0 && <span>{t("probability_label", { value: Math.round(c.probability * 100) })}</span>}
                </div>
                {Array.isArray(c.resolution_suggestions) && c.resolution_suggestions.length > 0 && (
                  <div className="text-[10px] text-cyan-300/70 mt-1">→ {c.resolution_suggestions.slice(0, 2).join("; ")}</div>
                )}
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Прогнозы и сценарии (future_predictions) ═══ */}
      {(predictions.length > 0 || scenarios.length > 0) && (
        <SectionCard title={t("section_predictions")} icon="🔮" accent="border-violet-500/30">
          <div className="space-y-2">
            {scenarios.slice(0, 8).map((sc, i) => (
              <div key={`sc${i}`} className="bg-brain-900/30 rounded-lg p-2.5 border border-brain-700/25">
                <div className="flex items-start justify-between gap-2">
                  <div className="text-white text-sm">📈 {sc.name}</div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <FreshnessBadge f={sc.freshness} />
                    <span className="text-[10px] text-violet-300">{Math.round((sc.probability ?? 0) * 100)}%{sc.time_horizon ? ` · ${sc.time_horizon}` : ""}</span>
                  </div>
                </div>
                {sc.description && <div className="text-[11px] text-slate-400 mt-0.5 line-clamp-2">{sc.description}</div>}
              </div>
            ))}
            {predictions.slice(0, 8).map((pr, i) => (
              <div key={`pr${i}`} className="bg-brain-900/30 rounded-lg p-2.5 border border-brain-700/25">
                <div className="flex items-start justify-between gap-2">
                  <div className="text-white text-sm">🎯 {pr.subject}</div>
                  {(pr.success_probability ?? 0) > 0 && (
                    <span className={`text-[10px] flex-shrink-0 ${pr.success_probability >= 0.6 ? "text-emerald-300" : "text-amber-300"}`}>
                      {t("success_probability_label", { value: Math.round(pr.success_probability * 100) })}
                    </span>
                  )}
                </div>
                {Array.isArray(pr.key_risks) && pr.key_risks.length > 0 && (
                  <div className="text-[10px] text-amber-300/70 mt-0.5">{t("key_risks_label", { value: pr.key_risks.slice(0, 3).join(", ") })}</div>
                )}
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Оргструктура (редактируемая) ═══ */}
      {s.departments?.length > 0 && (
        <OrgStructureEditor
          departments={s.departments}
          userId={userId}
          people={people}
          onUpdated={onUpdated}
        />
      )}

      {/* ═══ Принципы (Wisdom) — с доказательной базой ═══ */}
      {principles.length > 0 && (
        <SectionCard title={t("section_principles")} icon="🧠" accent="border-orange-500/30">
          <div className="space-y-2">
            {principles.slice(0, 15).map((p, i) => (
              <div key={i} className="bg-brain-900/30 rounded-lg p-2.5 border border-brain-700/25">
                <div className="flex items-start justify-between gap-2">
                  <div className="text-white text-sm">{p.statement}</div>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded flex-shrink-0 ${
                    (p.confidence || 0) >= 0.8 ? "text-emerald-300 bg-emerald-500/10"
                    : (p.confidence || 0) >= 0.6 ? "text-amber-300 bg-amber-500/10"
                    : "text-slate-400 bg-slate-500/10"}`}>
                    {Math.round((p.confidence || 0) * 100)}%
                  </span>
                </div>
                <div className="flex items-center gap-2 mt-1 flex-wrap">
                  {p.domain && <span className="text-[10px] text-slate-500">{p.domain}</span>}
                  {p.evidence_count > 0 && (
                    <span className="text-[10px] text-indigo-300/70">{t("evidence_count_label", { count: p.evidence_count })}</span>
                  )}
                  {p.needs_validation && (
                    <span className="text-[10px] text-amber-300 bg-amber-500/10 px-1 rounded" title={t("needs_validation_title")}>{t("needs_validation_badge")}</span>
                  )}
                </div>
                {Array.isArray(p.evidence) && p.evidence.length > 0 && (
                  <div className="text-[10px] text-slate-500 mt-1 line-clamp-2">{t("based_on_label", { value: p.evidence.join("; ") })}</div>
                )}
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Rule Book / Правила компании ═══ */}
      {rules.length > 0 && (
        <SectionCard title={t("section_rules")} icon="📋" accent="border-blue-500/30">
          <div className="space-y-1.5">
            {rules.slice(0, 20).map((r, i) => (
              <div key={i} className="flex items-start gap-2 bg-brain-900/30 rounded-lg p-2 border border-brain-700/25">
                <span className="text-[10px] text-blue-300 bg-blue-500/10 px-1.5 py-0.5 rounded flex-shrink-0 mt-0.5">
                  {r.category || "general"}
                </span>
                <div className="text-white text-sm flex-1">{r.text}</div>
                {r.priority >= 8 && <span className="text-[10px] text-amber-300 flex-shrink-0" title={t("high_priority_title")}>★</span>}
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Маркетинговые наработки (Mark) ═══ */}
      {marketing.length > 0 && (
        <SectionCard title={t("section_marketing")} icon="📣" accent="border-pink-500/30">
          <div className="space-y-2">
            {marketing.slice(0, 12).map((m, i) => (
              <div key={i} className="bg-brain-900/30 rounded-lg p-2.5 border border-brain-700/25">
                <button
                  onClick={() => setOpenMkt(openMkt === i ? null : i)}
                  className="w-full text-left flex items-start justify-between gap-2"
                >
                  <span className="text-white text-sm">{m.name}</span>
                  <span className="text-slate-500 text-xs flex-shrink-0">{(m.created_at || "").slice(0, 10)} {openMkt === i ? "▲" : "▼"}</span>
                </button>
                {Array.isArray(m.tags) && m.tags.length > 0 && (
                  <div className="text-[10px] text-pink-300/70 mt-0.5">{m.tags.join(" · ")}</div>
                )}
                {openMkt === i && m.content && (
                  <div className="text-xs text-slate-300 mt-2 whitespace-pre-wrap border-t border-brain-600/20 pt-2 max-h-72 overflow-y-auto">
                    {m.content}
                  </div>
                )}
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Related people / Партнёры, внешние, кандидаты, клиенты ═══ */}
      {(s.related_people?.length ?? 0) > 0 && (
        <SectionCard title={t("section_related_people")} icon="🤝" accent="border-amber-500/30">
          <div className="grid gap-2 grid-cols-1 sm:grid-cols-2">
            {s.related_people!.slice(0, 20).map((person, i) => {
              const catLabel: Record<string, string> = {
                partner: t("category_partner"), external: t("category_external"), candidate: t("category_candidate"),
                client: t("category_client"),
              };
              return (
                <div key={i} className="flex items-center gap-3 bg-brain-900/30 rounded-lg p-2.5 border border-brain-700/25">
                  <div className="w-8 h-8 rounded-full bg-gradient-to-br from-amber-500/20 to-orange-500/20 flex items-center justify-center text-white text-xs font-bold flex-shrink-0">
                    {person.name?.charAt(0) || "?"}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-white text-sm font-medium truncate">{person.name}</div>
                    {person.role && <div className="text-slate-500 text-xs truncate">{person.role}</div>}
                  </div>
                  {person.category && (
                    <span className="text-[10px] text-amber-300 bg-amber-500/10 px-1.5 py-0.5 rounded flex-shrink-0">
                      {catLabel[person.category] || person.category}
                    </span>
                  )}
                  {userId && (
                    <button
                      onClick={() => removeEntity(person)}
                      disabled={classBusy === (person.person_id || person.name) + "__delete"}
                      className="text-[10px] text-slate-500 hover:text-rose-400 px-1.5 py-0.5 rounded border border-brain-600/20 flex-shrink-0 disabled:opacity-50"
                      title={t("delete_entity_title")}
                    >
                      ✕
                    </button>
                  )}
                  {userId && (
                    <button
                      onClick={() => reclassify(person.person_id || person.name, "employee")}
                      disabled={classBusy === (person.person_id || person.name) + "employee"}
                      className="text-[10px] text-slate-400 hover:text-emerald-300 px-1.5 py-0.5 rounded border border-brain-600/20 flex-shrink-0 disabled:opacity-50"
                      title={t("reclassify_employee_title")}
                    >
                      {t("to_employee_button")}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </SectionCard>
      )}

      {/* ═══ Projects ═══ */}
      {s.active_projects?.length > 0 && (
        <SectionCard title={t("section_projects")} icon="📁" accent="border-indigo-500/30">
          <div className="space-y-2">
            {s.active_projects.slice(0, 8).map((proj, i) => (
              <div key={i} className="bg-brain-900/30 rounded-lg p-3 border border-brain-700/25">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-white font-medium text-sm">{proj.name}</span>
                  <StatusBadge status={proj.status || "active"} />
                </div>
                {proj.description && <p className="text-slate-400 text-xs">{proj.description}</p>}
                {proj.lead && <p className="text-slate-500 text-xs mt-1">Lead: {proj.lead}</p>}
                {typeof proj.progress === "number" && proj.progress > 0 && (
                  <div className="mt-2">
                    <ProgressBar value={proj.progress} color="blue" />
                  </div>
                )}
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Strategic Goals ═══ */}
      {s.strategic_goals?.length > 0 && (
        <SectionCard title={t("section_goals")} icon="🎯" accent="border-emerald-500/30">
          <div className="space-y-3">
            {s.strategic_goals.slice(0, 5).map((goal, i) => (
              <div key={i}>
                <div className="flex justify-between text-sm mb-1">
                  <span className="text-slate-300">{goal.goal}</span>
                  <span className="text-slate-500">{goal.progress || 0}%</span>
                </div>
                <ProgressBar value={goal.progress || 0} color="green" />
                {goal.timeframe && <div className="text-slate-500 text-xs mt-1">{goal.timeframe}</div>}
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ SWOT Analysis ═══ */}
      {(s.strengths?.length > 0 || s.weaknesses?.length > 0 || s.opportunities?.length > 0 || s.threats?.length > 0) && (
        <div className="grid grid-cols-2 gap-3">
          {s.strengths?.length > 0 && (
            <div className="bg-emerald-500/10 rounded-xl p-4 border border-emerald-500/20">
              <h4 className="text-emerald-400 font-semibold text-xs uppercase tracking-wide mb-2">{t("swot_strengths")}</h4>
              <ul className="text-slate-300 text-sm space-y-1">
                {s.strengths.slice(0, 5).map((item, i) => (
                  <li key={i} className="flex items-start gap-1.5">
                    <span className="text-emerald-400 text-xs mt-1">+</span>
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {s.weaknesses?.length > 0 && (
            <div className="bg-red-500/10 rounded-xl p-4 border border-red-500/20">
              <h4 className="text-red-400 font-semibold text-xs uppercase tracking-wide mb-2">{t("swot_weaknesses")}</h4>
              <ul className="text-slate-300 text-sm space-y-1">
                {s.weaknesses.slice(0, 5).map((item, i) => (
                  <li key={i} className="flex items-start gap-1.5">
                    <span className="text-red-400 text-xs mt-1">−</span>
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {s.opportunities?.length > 0 && (
            <div className="bg-blue-500/10 rounded-xl p-4 border border-blue-500/20">
              <h4 className="text-blue-400 font-semibold text-xs uppercase tracking-wide mb-2">{t("swot_opportunities")}</h4>
              <ul className="text-slate-300 text-sm space-y-1">
                {s.opportunities.slice(0, 5).map((item, i) => (
                  <li key={i} className="flex items-start gap-1.5">
                    <span className="text-blue-400 text-xs mt-1">↗</span>
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {s.threats?.length > 0 && (
            <div className="bg-amber-500/10 rounded-xl p-4 border border-amber-500/20">
              <h4 className="text-amber-400 font-semibold text-xs uppercase tracking-wide mb-2">{t("swot_threats")}</h4>
              <ul className="text-slate-300 text-sm space-y-1">
                {s.threats.slice(0, 5).map((item, i) => (
                  <li key={i} className="flex items-start gap-1.5">
                    <span className="text-amber-400 text-xs mt-1">!</span>
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* ═══ Problems & Achievements ═══ */}
      {s.current_problems?.length > 0 && (
        <SectionCard title={t("section_problems")} icon="⚠️" accent="border-red-500/30">
          <div className="space-y-2">
            {s.current_problems.slice(0, 5).map((problem, i) => (
              <div key={i} className="flex items-center justify-between bg-brain-900/40 rounded p-2.5">
                <span className="text-slate-300 text-sm">{problem.problem}</span>
                <SeverityBadge severity={problem.severity || "medium"} />
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {s.recent_achievements?.length > 0 && (
        <SectionCard title={t("section_achievements")} icon="🏆" accent="border-emerald-500/30">
          <div className="space-y-2">
            {s.recent_achievements.slice(0, 5).map((a, i) => (
              <div key={i} className="bg-brain-900/30 rounded p-2.5 flex items-start gap-2">
                <span className="text-emerald-400 text-xs mt-0.5">✓</span>
                <div>
                  <span className="text-slate-300 text-sm">{a.achievement}</span>
                  {a.date && <span className="text-slate-500 text-xs ml-2">{a.date}</span>}
                </div>
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Technologies ═══ */}
      {s.tech_stack?.length > 0 && (
        <SectionCard title={t("section_tech")} icon="🛠" accent="border-violet-500/30">
          <div className="flex flex-wrap gap-2">
            {s.tech_stack.map((tech, i) => (
              <span key={i} className="px-3 py-1.5 bg-violet-500/10 text-violet-300 text-xs rounded-lg border border-violet-500/20 font-medium">
                {tech}
              </span>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Финансы: реальные ряды из онтологии цифр (KPI-узлы с series) ═══ */}
      {(s as any).financial_kpis?.length > 0 && (
        <SectionCard title={t("section_finance")} icon="💰" accent="border-emerald-500/30">
          <div className="grid gap-3 sm:grid-cols-2">
            {(s as any).financial_kpis.slice(0, 6).map((fk: any, i: number) => {
              const entries = Object.entries(fk.series || {}).sort() as [string, number][]
              const vals = entries.map(([, v]) => Number(v) || 0)
              const max = Math.max(...vals, 1)
              const last = vals[vals.length - 1] ?? 0
              const prev = vals[vals.length - 2] ?? last
              const up = last >= prev
              return (
                <div key={i} className="bg-brain-900/30 rounded-lg p-3 border border-brain-700/25">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-slate-400 text-xs truncate">{fk.name}</span>
                    <span className={`text-[10px] ${up ? 'text-emerald-400' : 'text-red-400'}`}>
                      {prev ? `${up ? '▲' : '▼'} ${Math.abs(((last - prev) / prev) * 100).toFixed(0)}%` : ''}
                    </span>
                  </div>
                  <div className="text-white text-xl font-bold">
                    {Number(fk.value ?? last).toLocaleString('ru-RU')}
                    <span className="text-slate-500 text-xs font-normal ml-1.5">{fk.period}</span>
                  </div>
                  {/* спарклайн: столбики по месяцам */}
                  <div className="flex items-end gap-0.5 h-8 mt-2">
                    {entries.slice(-12).map(([m, v], j) => (
                      <div key={j} title={`${m}: ${Number(v).toLocaleString('ru-RU')}`}
                           className={`flex-1 rounded-sm ${j === entries.slice(-12).length - 1 ? 'bg-emerald-400' : 'bg-brain-500/50'}`}
                           style={{ height: `${Math.max(8, (Number(v) / max) * 100)}%` }} />
                    ))}
                  </div>
                  {fk.source && <div className="text-slate-600 text-[10px] mt-1 truncate">{t("from_source_label", { value: fk.source })}</div>}
                </div>
              )
            })}
          </div>
        </SectionCard>
      )}

      {/* ═══ Key Metrics ═══ */}
      {s.kpis?.length > 0 && (
        <SectionCard title={t("section_metrics")} icon="📈" accent="border-blue-500/30">
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {s.kpis.slice(0, 6).map((kpi, i) => (
              <div key={i} className="bg-brain-900/30 rounded-lg p-3 text-center border border-brain-700/25">
                <div className="text-2xl font-bold text-white">{kpi.current_value}</div>
                <div className="text-slate-500 text-xs mt-1">{kpi.name}</div>
                <div className="text-slate-600 text-xs">{kpi.trend}</div>
              </div>
            ))}
          </div>
          {s.health_score > 0 && (
            <div className="mt-4">
              <div className="flex justify-between text-sm mb-1">
                <span className="text-slate-400">Health Score</span>
                <span className="text-white font-bold">{Math.round(s.health_score)}%</span>
              </div>
              <ProgressBar
                value={s.health_score}
                color={s.health_score >= 70 ? "green" : s.health_score >= 40 ? "yellow" : "red"}
              />
            </div>
          )}
        </SectionCard>
      )}

      {/* ═══ Resources ═══ */}
      {s.resources?.length > 0 && (
        <SectionCard title={t("section_resources")} icon="🔗" accent="border-brain-600/40">
          <div className="space-y-2">
            {s.resources.map((r, i) => (
              <div key={i} className="flex items-center gap-2 text-sm">
                <span className="text-slate-500">
                  {r.type === "channel" ? "💬" : r.type === "tool" ? "🔧" : "📄"}
                </span>
                {r.url ? (
                  <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:underline">
                    {r.name}
                  </a>
                ) : (
                  <span className="text-slate-300">{r.name}</span>
                )}
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* ═══ Footer ═══ */}
      <div className="flex items-center justify-between text-slate-500 text-xs px-1">
        <span>
          {s.version > 0 && `v${s.version}`}
          {s.delta_summary && ` · ${s.delta_summary}`}
        </span>
        <span>{t("updated_label", { value: s.last_updated ? new Date(s.last_updated).toLocaleString("ru-RU") : "N/A" })}</span>
      </div>
    </div>
  );
}

// ── Инлайн-правка фактов (карандаш → форма «новое значение + причина») ──
function FactEditButton({
  active,
  title,
  onClick,
}: {
  active: boolean;
  title: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`ml-1 text-xs leading-none transition-colors ${
        active ? "text-blue-400" : "text-slate-500 hover:text-blue-400"
      }`}
    >
      ✎
    </button>
  );
}

function FactCorrectionForm({
  userId,
  entityId,
  entityType,
  field,
  oldValue,
  onClose,
  onSaved,
}: {
  userId: string;
  entityId: string;
  entityType: string;
  field: string;
  oldValue: any;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useTranslations("snapshots_panel");
  const [newValue, setNewValue] = useState(oldValue ? String(oldValue) : "");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    if (!newValue.trim() || !reason.trim()) return;
    try {
      setSaving(true);
      setError(null);
      await submitFactCorrection(userId, {
        entity_id: entityId,
        entity_type: entityType,
        field,
        old_value: oldValue ?? null,
        new_value: newValue.trim(),
        reason: reason.trim(),
      });
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("edit_error"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-3 bg-brain-900/50 border border-brain-600/20 rounded-lg p-3">
      <div className="text-slate-400 text-xs mb-2">
        {t("edit_field_title", { field })}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-2">
        <input
          type="text"
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          placeholder={t("edit_new_value_placeholder")}
          className="w-full bg-brain-900/40 border border-brain-600/30 rounded p-2 text-white text-sm focus:border-brain-600/40 focus:outline-none"
        />
        <input
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder={t("edit_reason_placeholder")}
          className="w-full bg-brain-900/40 border border-brain-600/30 rounded p-2 text-white text-sm focus:border-brain-600/40 focus:outline-none"
        />
      </div>
      {error && <div className="text-red-400 text-xs mb-2">{error}</div>}
      <div className="flex justify-end gap-2">
        <button
          onClick={onClose}
          className="px-3 py-1 text-xs text-slate-400 hover:text-white transition-colors"
        >
          {t("edit_cancel")}
        </button>
        <button
          onClick={handleSave}
          disabled={!newValue.trim() || !reason.trim() || saving}
          className="px-3 py-1 text-xs bg-blue-500 text-white rounded hover:bg-blue-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? t("edit_saving") : t("edit_save")}
        </button>
      </div>
    </div>
  );
}

function PersonSnapshotView({ snapshot, userId }: { snapshot: PersonSnapshot; userId: string }) {
  const t = useTranslations("snapshots_panel");
  const [editingField, setEditingField] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const toggleEdit = (field: string) => {
    setEditingField((prev) => (prev === field ? null : field));
  };

  const handleSaved = () => {
    setEditingField(null);
    setToast(t("edit_success"));
    setTimeout(() => setToast(null), 8000);
  };

  return (
    <div className="space-y-4">
      {/* Toast: исправление записано */}
      {toast && (
        <div className="bg-emerald-500/10 border border-emerald-500/20 rounded-lg p-3 text-emerald-400 text-sm">
          ✓ {toast}
        </div>
      )}

      {/* Header */}
      <div className="bg-gradient-to-r from-cyan-500/20 to-blue-500/20 rounded-lg p-4 border border-cyan-500/30">
        <h3 className="text-xl font-bold text-white mb-1">{snapshot.name}</h3>
        <p className="text-slate-400">
          {snapshot.role || "—"}
          <FactEditButton
            active={editingField === "role"}
            title={t("edit_button_title")}
            onClick={() => toggleEdit("role")}
          />
        </p>
        <div className="flex gap-4 mt-3 text-xs text-slate-500">
          <span>
            🏢 {snapshot.department || "—"}
            <FactEditButton
              active={editingField === "department"}
              title={t("edit_button_title")}
              onClick={() => toggleEdit("department")}
            />
          </span>
          <span>
            {t("manager_label", { value: snapshot.manager || "N/A" })}
            <FactEditButton
              active={editingField === "manager"}
              title={t("edit_button_title")}
              onClick={() => toggleEdit("manager")}
            />
          </span>
        </div>

        {/* Инлайн-форма исправления */}
        {editingField && (
          <FactCorrectionForm
            userId={userId}
            entityId={snapshot.person_id}
            entityType="Person"
            field={editingField}
            oldValue={(snapshot as any)[editingField] ?? null}
            onClose={() => setEditingField(null)}
            onSaved={handleSaved}
          />
        )}
      </div>

      {/* Вердикт (AI): чем занимается и что важно — первым, до фактов
          (принцип «вердикт → цифры → детали») */}
      {(snapshot as any).activity_summary && (
        <div className="bg-gradient-to-br from-brain-800/50 to-cyan-900/15 rounded-lg p-4 border border-cyan-500/20">
          <div className="text-cyan-300 text-xs font-semibold mb-1.5">{t("person_summary_heading")}</div>
          <p className="text-slate-200 text-sm whitespace-pre-wrap">{(snapshot as any).activity_summary}</p>
        </div>
      )}

      {/* Tasks — split into "to do" and "done" so the card shows what's
          pending vs finished, not a raw dump of the first N rows. */}
      {(() => {
        const DONE = new Set(["completed", "done", "выполнено"]);
        const realTasks = (snapshot.current_tasks || []).filter(
          (tk) => tk.task && tk.task.trim() && tk.task.trim().toUpperCase() !== "N/A"
        );
        const doneTasks = realTasks.filter((tk) => DONE.has((tk.status || "").toLowerCase()));
        const todoTasks = realTasks.filter((tk) => !DONE.has((tk.status || "").toLowerCase()));
        if (realTasks.length === 0) return null;
        const TaskRow = ({ task }: { task: { task: string; status: string; deadline?: string } }) => (
          <div className="flex items-center justify-between bg-brain-900/40 rounded p-2">
            <span className="text-slate-300 text-sm">{task.task}</span>
            <div className="flex items-center gap-2">
              <StatusBadge status={task.status} />
              {task.deadline && <span className="text-slate-500 text-xs">{task.deadline}</span>}
            </div>
          </div>
        );
        return (
          <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
            <h4 className="text-white font-medium mb-3">{t("current_tasks_heading")}</h4>
            {todoTasks.length > 0 && (
              <div className="mb-3">
                <div className="text-slate-400 text-xs mb-2 uppercase tracking-wide">
                  {t("tasks_todo_heading")} ({todoTasks.length})
                </div>
                <div className="space-y-2">
                  {todoTasks.slice(0, 6).map((task, i) => (
                    <TaskRow key={`todo-${i}`} task={task} />
                  ))}
                </div>
              </div>
            )}
            {doneTasks.length > 0 && (
              <div>
                <div className="text-slate-400 text-xs mb-2 uppercase tracking-wide">
                  {t("tasks_done_heading")} ({doneTasks.length})
                </div>
                <div className="space-y-2">
                  {doneTasks.slice(0, 6).map((task, i) => (
                    <TaskRow key={`done-${i}`} task={task} />
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      })()}

      {/* Skills */}
      {snapshot.skills && snapshot.skills.length > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-3">{t("skills_heading")}</h4>
          <div className="flex flex-wrap gap-2">
            {snapshot.skills.map((skill, i) => (
              <span
                key={i}
                className="px-2 py-1 bg-brain-700/40 text-slate-300 text-xs rounded"
              >
                {skill.skill} ({skill.level})
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Performance */}
      {snapshot.performance && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-3">{t("performance_heading")}</h4>
          <div className="mb-3">
            <div className="flex justify-between text-sm mb-1">
              <span className="text-slate-400">{t("rating_label")}</span>
              <span className="text-white">{snapshot.performance.rating}/5</span>
            </div>
            <ProgressBar value={snapshot.performance.rating} max={5} color="green" />
          </div>
          {snapshot.performance.recent_achievements && snapshot.performance.recent_achievements.length > 0 && (
            <div>
              <div className="text-slate-400 text-xs mb-1">{t("achievements_label")}</div>
              <ul className="text-slate-300 text-sm space-y-1">
                {snapshot.performance.recent_achievements.slice(0, 3).map((a, i) => (
                  <li key={i}>✓ {a}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* Colleagues */}
      {snapshot.colleagues && snapshot.colleagues.length > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-2">{t("colleagues_heading")}</h4>
          <div className="flex flex-wrap gap-2">
            {snapshot.colleagues.slice(0, 10).map((colleague, i) => (
              <span key={i} className="px-2 py-1 bg-brain-700/40 text-slate-300 text-xs rounded">
                {colleague}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Вклад (грунтуется на атрибуции: решения/задачи/встречи/связность) */}
      {(snapshot.collaboration_score != null || snapshot.decisions_made != null) && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <div className="flex items-center justify-between mb-3">
            <h4 className="text-white font-medium">{t("contribution_heading")}</h4>
            {snapshot.collaboration_score != null && (
              <span className="text-cyan-300 text-sm font-mono">{snapshot.collaboration_score}/100</span>
            )}
          </div>
          {snapshot.collaboration_score != null && (
            <div className="w-full h-2 bg-brain-700/40 rounded-full overflow-hidden mb-3">
              <div className="h-full bg-gradient-to-r from-cyan-500 to-blue-500 rounded-full"
                   style={{ width: `${Math.min(100, snapshot.collaboration_score)}%` }} />
            </div>
          )}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-center">
            <div className="bg-brain-900/30 rounded p-2">
              <div className="text-lg font-bold text-white">{snapshot.decisions_made ?? 0}</div>
              <div className="text-[11px] text-slate-500">{t("stat_decisions")}</div>
            </div>
            <div className="bg-brain-900/30 rounded p-2">
              <div className="text-lg font-bold text-white">{snapshot.tasks_completed_week ?? 0}</div>
              <div className="text-[11px] text-slate-500">{t("stat_tasks_closed")}</div>
            </div>
            <div className="bg-brain-900/30 rounded p-2">
              <div className="text-lg font-bold text-white">{snapshot.tasks_in_progress ?? 0}</div>
              <div className="text-[11px] text-slate-500">{t("stat_in_progress")}</div>
            </div>
            <div className="bg-brain-900/30 rounded p-2">
              <div className="text-lg font-bold text-white">{snapshot.meetings_participated ?? 0}</div>
              <div className="text-[11px] text-slate-500">{t("stat_meetings")}</div>
            </div>
          </div>
        </div>
      )}

      {/* Психологический профиль */}
      {snapshot.psychological && (snapshot.psychological.personality_type || (snapshot.psychological.dominant_traits?.length ?? 0) > 0) && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-2">{t("psych_profile_heading")}</h4>
          <div className="space-y-1 text-sm">
            {snapshot.psychological.personality_type && <p className="text-slate-300"><span className="text-slate-500">{t("personality_type_label")}</span> {snapshot.psychological.personality_type}</p>}
            {snapshot.psychological.team_role && <p className="text-slate-300"><span className="text-slate-500">{t("team_role_label")}</span> {snapshot.psychological.team_role}</p>}
            {snapshot.psychological.leadership_style && <p className="text-slate-300"><span className="text-slate-500">{t("leadership_style_label")}</span> {snapshot.psychological.leadership_style}</p>}
            {snapshot.psychological.communication_style && <p className="text-slate-300"><span className="text-slate-500">{t("communication_style_label")}</span> {snapshot.psychological.communication_style}</p>}
            {(snapshot.psychological.dominant_traits?.length ?? 0) > 0 && <p className="text-slate-300"><span className="text-slate-500">{t("dominant_traits_label")}</span> {snapshot.psychological.dominant_traits!.join(", ")}</p>}
            {(snapshot.psychological.strengths?.length ?? 0) > 0 && <p className="text-slate-300"><span className="text-slate-500">{t("strengths_label")}</span> {snapshot.psychological.strengths!.join(", ")}</p>}
            {(snapshot.psychological.motivation_drivers?.length ?? 0) > 0 && <p className="text-slate-300"><span className="text-slate-500">{t("motivators_label")}</span> {snapshot.psychological.motivation_drivers!.join(", ")}</p>}
          </div>
        </div>
      )}

      {/* Психоаналитика встреч — ленивый блок, доступ решает бэкенд */}
      {snapshot.person_id && (
        <PsychInsightsBlock personId={snapshot.person_id} userId={userId} />
      )}

      {/* Достижения (выполненные задачи + принятые решения) */}
      {(snapshot.recent_achievements?.length ?? 0) > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-emerald-700/30">
          <h4 className="text-white font-medium mb-2">{t("achievements_heading_count", { count: snapshot.recent_achievements!.length })}</h4>
          <ul className="space-y-1">
            {snapshot.recent_achievements!.slice(0, 8).map((a, i) => (
              <li key={i} className="text-sm text-emerald-100/90 flex items-start gap-2"><span className="mt-0.5">✓</span><span>{a}</span></li>
            ))}
          </ul>
        </div>
      )}

      {/* Вызовы / сложности (заблокированные задачи + противоречия) */}
      {(snapshot.current_challenges?.length ?? 0) > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-amber-700/30">
          <h4 className="text-white font-medium mb-2">{t("challenges_heading_count", { count: snapshot.current_challenges!.length })}</h4>
          <ul className="space-y-1">
            {snapshot.current_challenges!.slice(0, 8).map((c, i) => (
              <li key={i} className="text-sm text-amber-100/90 flex items-start gap-2"><span className="mt-0.5">•</span><span>{c}</span></li>
            ))}
          </ul>
        </div>
      )}

      {/* Чем занимается для компании (проекты + роль) */}
      {(snapshot.current_projects?.length ?? 0) > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-2">{t("company_work_heading")}</h4>
          <div className="space-y-1.5">
            {snapshot.current_projects!.slice(0, 10).map((p, i) => (
              <div key={i} className="text-sm text-slate-300 flex items-start gap-2">
                <span className="text-cyan-400 mt-0.5">▸</span>
                <span>
                  {p.project}
                  {p.role && <span className="text-slate-500 text-xs"> — {p.role}</span>}
                  {p.status && <span className="text-slate-600 text-xs"> [{p.status}]</span>}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Зоны роста (на чём стоит сфокусироваться) */}
      {(snapshot.areas_for_improvement?.length ?? 0) > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-blue-700/30">
          <h4 className="text-white font-medium mb-2">{t("growth_areas_heading")}</h4>
          <ul className="space-y-1">
            {snapshot.areas_for_improvement!.slice(0, 8).map((a, i) => (
              <li key={i} className="text-sm text-blue-100/90 flex items-start gap-2"><span className="mt-0.5">↗</span><span>{a}</span></li>
            ))}
          </ul>
        </div>
      )}

      {/* Решения, принятые человеком */}
      {(snapshot.decisions?.length ?? 0) > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-2">{t("decisions_heading_count", { count: snapshot.decisions!.length })}</h4>
          <div className="space-y-1.5">
            {snapshot.decisions!.slice(0, 12).map((d, i) => (
              <div key={i} className="text-sm text-slate-300 flex items-start gap-2">
                <span className="text-emerald-400 mt-0.5">•</span>
                <span>{d.summary}{d.category && <span className="text-slate-500 text-xs"> ({d.category})</span>}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Идеи / Мнения */}
      {((snapshot.ideas?.length ?? 0) > 0 || (snapshot.opinions?.length ?? 0) > 0) && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20 space-y-3">
          {(snapshot.ideas?.length ?? 0) > 0 && (
            <div>
              <h4 className="text-white font-medium mb-1">{t("ideas_heading_count", { count: snapshot.ideas!.length })}</h4>
              <ul className="space-y-1">
                {snapshot.ideas!.slice(0, 10).map((idea, i) => (
                  <li key={i} className="text-sm text-slate-300 flex items-start gap-2"><span className="text-amber-400 mt-0.5">•</span><span>{idea}</span></li>
                ))}
              </ul>
            </div>
          )}
          {(snapshot.opinions?.length ?? 0) > 0 && (
            <div>
              <h4 className="text-white font-medium mb-1">{t("opinions_heading_count", { count: snapshot.opinions!.length })}</h4>
              <ul className="space-y-1">
                {snapshot.opinions!.slice(0, 10).map((op, i) => (
                  <li key={i} className="text-sm text-slate-300 flex items-start gap-2">
                    <span className="text-blue-400 mt-0.5">•</span>
                    <span>{op.summary}{op.sentiment && <span className="text-slate-500 text-xs"> [{op.sentiment}]</span>}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* Противоречия, в которых замечен человек */}
      {(snapshot.contradictions?.length ?? 0) > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-amber-700/30">
          <h4 className="text-white font-medium mb-2">{t("contradictions_heading_count", { count: snapshot.contradictions!.length })}</h4>
          <ul className="space-y-1">
            {snapshot.contradictions!.slice(0, 8).map((c, i) => (
              <li key={i} className="text-sm text-amber-200/90 flex items-start gap-2"><span className="mt-0.5">•</span><span>{c}</span></li>
            ))}
          </ul>
        </div>
      )}

      {/* История (timeline из TemporalTracker / синтетический из графа) */}
      {snapshot.timeline && snapshot.timeline.length > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-3">{t("history_heading")}</h4>
          <div className="relative pl-4 space-y-3 border-l border-brain-600/40">
            {snapshot.timeline.slice(0, 30).map((ev, i) => {
              const dt = (ev.event_time || "").slice(0, 10);
              return (
                <div key={i} className="relative">
                  <span className="absolute -left-[21px] top-1.5 w-2 h-2 rounded-full bg-cyan-400" />
                  <div className="text-xs text-cyan-300 font-mono">{dt || "—"}</div>
                  <div className="text-sm text-slate-300">
                    {ev.description || ev.event_type || t("event_fallback")}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Footer */}
      <div className="text-slate-500 text-xs text-right">
        {t("updated_label", { value: new Date(snapshot.last_updated).toLocaleString("ru-RU") })}
      </div>
    </div>
  );
}

function ProjectSnapshotView({ snapshot }: { snapshot: ProjectSnapshot }) {
  const t = useTranslations("snapshots_panel");
  const tasksProgress = snapshot.tasks_total > 0
    ? Math.round((snapshot.tasks_completed / snapshot.tasks_total) * 100)
    : 0;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="bg-gradient-to-r from-purple-500/20 to-pink-500/20 rounded-lg p-4 border border-purple-500/30">
        <div className="flex items-center justify-between mb-1">
          <h3 className="text-xl font-bold text-white">{snapshot.name}</h3>
          <StatusBadge status={snapshot.status} />
        </div>
        <p className="text-slate-400 text-sm">{snapshot.description}</p>
        <div className="flex gap-4 mt-3 text-xs text-slate-500">
          <span>👤 Lead: {snapshot.lead || "N/A"}</span>
          <span>{t("deadline_label", { value: snapshot.deadline || "N/A" })}</span>
        </div>
      </div>

      {/* Вердикт (AI): этап / что хорошо / что плохо / перспективы — первым,
          до цифр и деталей (принцип «вердикт → цифры → детали») */}
      {(snapshot as any).ai_summary && (
        <div className="bg-gradient-to-br from-brain-800/50 to-emerald-900/15 rounded-lg p-4 border border-emerald-500/20">
          <div className="text-emerald-300 text-xs font-semibold mb-1.5">{t("project_state_heading")}</div>
          <p className="text-slate-200 text-sm whitespace-pre-wrap">{(snapshot as any).ai_summary}</p>
        </div>
      )}

      {/* Progress */}
      <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
        <div className="flex justify-between items-center mb-2">
          <h4 className="text-white font-medium">{t("progress_heading")}</h4>
          <span className="text-white font-bold">{snapshot.progress || tasksProgress}%</span>
        </div>
        <ProgressBar value={snapshot.progress || tasksProgress} color="purple" />
        
        <div className="grid grid-cols-4 gap-2 mt-4">
          <div className="bg-brain-900/40 rounded p-2 text-center">
            <div className="text-lg font-bold text-white">{snapshot.tasks_total}</div>
            <div className="text-xs text-slate-500">{t("progress_total")}</div>
          </div>
          <div className="bg-emerald-500/10 rounded p-2 text-center">
            <div className="text-lg font-bold text-emerald-400">{snapshot.tasks_completed}</div>
            <div className="text-xs text-slate-500">{t("progress_done")}</div>
          </div>
          <div className="bg-cyan-500/10 rounded p-2 text-center">
            <div className="text-lg font-bold text-cyan-400">{snapshot.tasks_in_progress}</div>
            <div className="text-xs text-slate-500">{t("progress_in_progress")}</div>
          </div>
          <div className="bg-red-500/10 rounded p-2 text-center">
            <div className="text-lg font-bold text-red-400">{snapshot.tasks_blocked}</div>
            <div className="text-xs text-slate-500">{t("progress_blockers")}</div>
          </div>
        </div>
      </div>

      {/* Team */}
      {snapshot.team_members && snapshot.team_members.length > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-2">{t("team_heading")}</h4>
          <div className="flex flex-wrap gap-2">
            {snapshot.team_members.map((member, i) => (
              <span key={i} className="px-2 py-1 bg-brain-700/40 text-slate-300 text-xs rounded">
                {member.name}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Здоровье проекта / velocity / заблокированные задачи */}
      {(snapshot.health_score != null || snapshot.velocity != null || (snapshot.tasks_blocked ?? 0) > 0) && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-3">{t("project_health_heading")}</h4>
          <div className="grid grid-cols-3 gap-2 text-center">
            {snapshot.health_score != null && (
              <div className="bg-brain-900/30 rounded p-2">
                <div className={`text-lg font-bold ${snapshot.health_score >= 70 ? "text-emerald-300" : snapshot.health_score >= 40 ? "text-amber-300" : "text-red-300"}`}>
                  {Math.round(snapshot.health_score)}
                </div>
                <div className="text-[11px] text-slate-500">{t("stat_health")}</div>
              </div>
            )}
            {snapshot.velocity != null && (
              <div className="bg-brain-900/30 rounded p-2">
                <div className="text-lg font-bold text-white">{snapshot.velocity}</div>
                <div className="text-[11px] text-slate-500">velocity</div>
              </div>
            )}
            {(snapshot.tasks_blocked ?? 0) > 0 && (
              <div className="bg-brain-900/30 rounded p-2">
                <div className="text-lg font-bold text-red-300">{snapshot.tasks_blocked}</div>
                <div className="text-[11px] text-slate-500">{t("stat_blocked_abbrev")}</div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Вехи (milestones) */}
      {(snapshot.milestones?.length ?? 0) > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-3">{t("milestones_heading_count", { count: snapshot.milestones!.length })}</h4>
          <div className="space-y-1.5">
            {snapshot.milestones!.slice(0, 10).map((m, i) => (
              <div key={i} className="flex items-center justify-between bg-brain-900/30 rounded p-2">
                <span className="text-slate-300 text-sm">{m.name || m.title}</span>
                <div className="flex items-center gap-2 flex-shrink-0">
                  {(m.date || m.due_date) && <span className="text-slate-500 text-xs">{m.date || m.due_date}</span>}
                  {m.status && <StatusBadge status={m.status} />}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Блокеры */}
      {(snapshot.blockers?.length ?? 0) > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-red-700/30">
          <h4 className="text-white font-medium mb-2">{t("blockers_heading_count", { count: snapshot.blockers!.length })}</h4>
          <ul className="space-y-1">
            {snapshot.blockers!.slice(0, 8).map((b, i) => (
              <li key={i} className="text-sm text-red-200/90 flex items-start gap-2"><span className="mt-0.5">•</span><span>{b}</span></li>
            ))}
          </ul>
        </div>
      )}

      {/* Risks */}
      {snapshot.risks && snapshot.risks.length > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-3">{t("risks_heading")}</h4>
          <div className="space-y-2">
            {snapshot.risks.map((risk, i) => (
              <div key={i} className="flex items-center justify-between bg-brain-900/40 rounded p-2">
                <span className="text-slate-300 text-sm">{risk.risk}</span>
                <SeverityBadge severity={risk.severity} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Recent Decisions */}
      {snapshot.recent_decisions && snapshot.recent_decisions.length > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-3">{t("recent_decisions_heading")}</h4>
          <div className="space-y-2">
            {snapshot.recent_decisions.map((decision, i) => (
              <div key={i} className="bg-brain-900/40 rounded p-2">
                <p className="text-slate-300 text-sm">{decision.decision}</p>
                {decision.date && (
                  <p className="text-slate-500 text-xs mt-1">{decision.date}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* История проекта (timeline из TemporalTracker) */}
      {snapshot.timeline && snapshot.timeline.length > 0 && (
        <div className="bg-brain-800/30 rounded-lg p-4 border border-brain-600/20">
          <h4 className="text-white font-medium mb-3">{t("project_history_heading")}</h4>
          <div className="relative pl-4 space-y-3 border-l border-brain-600/40">
            {snapshot.timeline.slice(0, 30).map((ev, i) => {
              const dt = (ev.event_time || "").slice(0, 10);
              return (
                <div key={i} className="relative">
                  <span className="absolute -left-[21px] top-1.5 w-2 h-2 rounded-full bg-indigo-400" />
                  <div className="text-xs text-indigo-300 font-mono">{dt || "—"}</div>
                  <div className="text-sm text-slate-300">
                    {ev.description || ev.event_type || t("event_fallback")}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Footer */}
      <div className="text-slate-500 text-xs text-right">
        {t("updated_label", { value: new Date(snapshot.last_updated).toLocaleString("ru-RU") })}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// Navigation Tree Diagram — Company → Departments → People/Projects
// ═══════════════════════════════════════════════════════════

function TreeNodeBadge({ type, status }: { type: string; status?: string }) {
  const config: Record<string, { bg: string; text: string; icon: string }> = {
    company:    { bg: "bg-blue-500/20",   text: "text-blue-400",   icon: "🏢" },
    department: { bg: "bg-purple-500/20",  text: "text-purple-400", icon: "🏗️" },
    group:      { bg: "bg-slate-500/20",   text: "text-slate-400",  icon: "📂" },
    person:     { bg: "bg-cyan-500/20",    text: "text-cyan-400",   icon: "👤" },
    project:    { bg: "bg-amber-500/20",   text: "text-amber-400",  icon: "📁" },
  };
  const c = config[type] || config.group;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs ${c.bg} ${c.text}`}>
      <span>{c.icon}</span>
      {status && <span className="opacity-70">{status}</span>}
    </span>
  );
}

function HealthBar({ score }: { score: number }) {
  const color = score >= 70 ? "bg-green-500" : score >= 40 ? "bg-amber-500" : "bg-red-500";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-brain-700/40 rounded-full overflow-hidden">
        <div className={`h-full ${color} rounded-full transition-all`} style={{ width: `${Math.min(100, score)}%` }} />
      </div>
      <span className="text-xs text-slate-400 w-8 text-right">{Math.round(score)}%</span>
    </div>
  );
}

function NavigationTreeDiagram({
  hierarchy,
  delta,
  userId,
  onOpenCompany,
  onOpenPerson,
  onOpenProject,
}: {
  hierarchy: any;
  delta: any;
  userId: string;
  onOpenCompany: () => void;
  onOpenPerson: (personId: string, name?: string) => void;
  onOpenProject: (projectId: string) => void;
}) {
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set(["root"]));
  const t = useTranslations("snapshots_panel");
  const tree = hierarchy?.tree;
  // Карточка отдела: клик по 📸 на ветке подгружает снапшот отдела (текст)
  // и раскрывает его прямо под заголовком ветки.
  const [deptCard, setDeptCard] = useState<{ name: string; text: string } | null>(null);
  const [deptLoading, setDeptLoading] = useState<string | null>(null);

  const openDeptCard = async (deptName: string) => {
    if (deptCard?.name === deptName) { setDeptCard(null); return; } // toggle
    setDeptLoading(deptName);
    try {
      const res = await authFetch(
        `${API_BASE_H}/api/v1/snapshots/department/${encodeURIComponent(deptName)}?user_id=${userId}&format=text`
      );
      const data = await res.json();
      const text = data?.content || data?.text ||
        (data?.snapshot ? JSON.stringify(data.snapshot, null, 2) : t("dept_snapshot_not_generated"));
      setDeptCard({ name: deptName, text });
    } catch {
      setDeptCard({ name: deptName, text: t("dept_snapshot_load_failed") });
    } finally {
      setDeptLoading(null);
    }
  };

  const toggleNode = (nodeId: string) => {
    setExpandedNodes((prev) => {
      const next = new Set(prev);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  };

  const expandAll = () => {
    const all = new Set<string>(["root"]);
    if (tree?.children) {
      tree.children.forEach((_: any, i: number) => all.add(`dept_${i}`));
    }
    setExpandedNodes(all);
  };

  const collapseAll = () => {
    setExpandedNodes(new Set(["root"]));
  };

  // ── Leaf node (person or project) ──
  const renderLeaf = (child: any) => {
    const isPerson = child.type === "person";
    const isProject = child.type === "project";
    return (
      <div
        key={`${child.type}_${child.id}`}
        className={`group flex items-center gap-2 py-1.5 px-3 rounded-lg cursor-pointer transition-all
          hover:bg-brain-700/50 active:scale-[0.98]`}
        onClick={() => {
          if (isPerson) onOpenPerson(child.id, child.name || "");
          if (isProject) onOpenProject(child.id);
        }}
      >
        {/* Connector line */}
        <div className="w-4 h-px bg-brain-600/50 group-hover:bg-brain-500/70 flex-shrink-0" />
        <TreeNodeBadge type={child.type} status={isProject ? child.status : undefined} />
        <div className="flex-1 min-w-0">
          <span className="text-white text-sm truncate block">{child.name}</span>
          {child.role && <span className="text-slate-500 text-xs">{child.role}</span>}
        </div>
        <span className="text-slate-600 text-xs opacity-0 group-hover:opacity-100 transition-opacity">
          {t('open_arrow')}
        </span>
      </div>
    );
  };

  // ── Department / Group node ──
  const renderBranch = (branch: any, idx: number) => {
    const nodeId = `dept_${idx}`;
    const isExpanded = expandedNodes.has(nodeId);
    const children = branch.children || [];
    const personCount = children.filter((c: any) => c.type === "person").length;
    const projectCount = children.filter((c: any) => c.type === "project").length;

    return (
      <div key={nodeId} className="relative">
        {/* Vertical connector */}
        <div className="absolute left-5 top-0 w-px h-full bg-brain-700/40" />

        {/* Branch header */}
        <div
          className="flex items-center gap-2 py-2 px-2 rounded-lg cursor-pointer hover:bg-brain-800/50 transition-colors"
          onClick={() => toggleNode(nodeId)}
        >
          {/* Expand/collapse chevron */}
          <span className={`text-slate-500 text-xs transition-transform w-4 text-center flex-shrink-0 ${isExpanded ? "rotate-90" : ""}`}>
            {children.length > 0 ? "▶" : "─"}
          </span>
          <TreeNodeBadge type={branch.type} />
          <span className="text-white text-sm font-medium">{branch.name}</span>
          {branch.head && (
            <span className="text-slate-500 text-xs hidden sm:inline">({branch.head})</span>
          )}
          <span className="ml-auto flex items-center gap-2 text-xs text-slate-500 flex-shrink-0">
            {personCount > 0 && <span>👤 {personCount}</span>}
            {projectCount > 0 && <span>📁 {projectCount}</span>}
            {/* Карточка отдела: снапшот по клику, не разворачивая детей */}
            <button
              onClick={(e) => { e.stopPropagation(); openDeptCard(branch.name); }}
              title={t("open_dept_card_title")}
              className={`px-1.5 py-0.5 rounded border text-[11px] transition-colors ${
                deptCard?.name === branch.name
                  ? "border-brain-400/60 text-brain-200 bg-brain-700/40"
                  : "border-brain-600/30 text-slate-400 hover:text-white hover:border-brain-500/50"
              }`}
            >
              {deptLoading === branch.name ? "…" : "📸"}
            </button>
          </span>
        </div>

        {/* Inline-карточка отдела (снапшот) */}
        {deptCard?.name === branch.name && (
          <div className="ml-9 mr-2 mb-2 bg-brain-800/40 border border-brain-600/30 rounded-lg p-3">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-brain-200 text-xs font-semibold">📸 {branch.name}</span>
              <button onClick={() => setDeptCard(null)} className="text-slate-500 hover:text-white text-xs">✕</button>
            </div>
            <pre className="text-slate-300 text-xs whitespace-pre-wrap font-sans max-h-64 overflow-y-auto">
              {deptCard?.text}
            </pre>
          </div>
        )}

        {/* Children */}
        {isExpanded && children.length > 0 && (
          <div className="ml-5 pl-3 border-l border-brain-600/20 space-y-0.5">
            {children.map((child: any) => renderLeaf(child))}
          </div>
        )}
      </div>
    );
  };

  // ── Empty state ──
  if (!tree && !hierarchy?.company) {
    return (
      <div className="text-center text-slate-500 py-12">
        <div className="text-4xl mb-3 opacity-50">🌳</div>
        <div className="text-lg mb-1">{t("empty_tree_title")}</div>
        <div className="text-sm">{t("empty_tree_hint")}</div>
      </div>
    );
  }

  const companyData = hierarchy.company;
  const treeChildren = tree?.children || [];

  return (
    <div className="space-y-4">
      {/* ═══ Toolbar ═══ */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-white text-sm font-semibold">{t("navigation_map")}</span>
          <span className="text-slate-500 text-xs">
            {t("tree_summary", { deps: tree?.total_departments || 0, people: tree?.total_persons || 0, projects: tree?.total_projects || 0 })}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={expandAll} className="text-xs text-slate-400 hover:text-white transition-colors px-2 py-1 rounded hover:bg-brain-800/40">
            {t('expand_all')}
          </button>
          <button onClick={collapseAll} className="text-xs text-slate-400 hover:text-white transition-colors px-2 py-1 rounded hover:bg-brain-800/40">
            {t('collapse_all')}
          </button>
        </div>
      </div>

      {/* ═══ Company Root Node ═══ */}
      {companyData && (
        <div
          className="bg-gradient-to-r from-blue-600/15 via-purple-600/10 to-indigo-600/15 rounded-xl p-4 border border-blue-500/25 cursor-pointer hover:border-blue-500/40 transition-all"
          onClick={() => toggleNode("root")}
        >
          <div className="flex items-start gap-3">
            {/* Icon */}
            <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center text-xl flex-shrink-0">
              🏢
            </div>
            {/* Content */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <h3 className="text-white font-bold text-lg truncate">{companyData.name || t("company_fallback")}</h3>
                {companyData.stage && (
                  <span className="text-xs px-2 py-0.5 rounded bg-blue-500/20 text-blue-400">{companyData.stage}</span>
                )}
                <span className="text-xs text-slate-500">v{companyData.version}</span>
              </div>
              <p className="text-slate-300 text-sm line-clamp-2">{companyData.snippet || t("no_description")}</p>
              {companyData.health_score != null && (
                <div className="mt-2 max-w-xs">
                  <HealthBar score={companyData.health_score} />
                </div>
              )}
            </div>
            {/* Actions */}
            <div className="flex flex-col items-end gap-1 flex-shrink-0">
              <button
                onClick={(e) => { e.stopPropagation(); onOpenCompany(); }}
                className="text-blue-400 text-xs hover:underline"
              >
                {t('full_profile')}
              </button>
              <span className={`text-xs text-slate-500 transition-transform ${expandedNodes.has("root") ? "rotate-90" : ""}`}>
                {t('sections_count', { count: treeChildren.length })}
              </span>
            </div>
          </div>

          {/* Delta */}
          {companyData.delta_summary && (
            <div className="mt-3 bg-blue-500/10 border border-blue-500/15 rounded-lg px-3 py-2">
              <span className="text-blue-400 text-xs font-medium">{t("changes_label")} </span>
              <span className="text-blue-300 text-xs">{companyData.delta_summary}</span>
            </div>
          )}
        </div>
      )}

      {/* ═══ Tree Branches ═══ */}
      {expandedNodes.has("root") && treeChildren.length > 0 && (
        <div className="ml-2 space-y-1 border-l-2 border-blue-500/20 pl-2">
          {treeChildren.map((branch: any, i: number) => renderBranch(branch, i))}
        </div>
      )}

      {/* ═══ Fallback: flat lists if no tree ═══ */}
      {(!tree || treeChildren.length === 0) && expandedNodes.has("root") && (
        <div className="ml-2 space-y-3 border-l-2 border-blue-500/20 pl-4">
          {/* Departments flat */}
          {hierarchy.departments?.length > 0 && (
            <div className="space-y-1">
              <div className="text-slate-400 text-xs font-medium uppercase tracking-wider mb-1">{t("departments_label")}</div>
              {hierarchy.departments.map((dept: any, i: number) => (
                <div key={i} className="flex items-center gap-2 py-1.5 px-3 rounded-lg bg-brain-800/30">
                  <TreeNodeBadge type="department" />
                  <span className="text-white text-sm">{dept.name}</span>
                  {dept.head && <span className="text-slate-500 text-xs">({dept.head})</span>}
                </div>
              ))}
            </div>
          )}
          {/* Persons flat */}
          {hierarchy.persons?.length > 0 && (
            <div className="space-y-1">
              <div className="text-slate-400 text-xs font-medium uppercase tracking-wider mb-1">{t("employees_label")}</div>
              {hierarchy.persons.map((person: any, i: number) => (
                <div
                  key={i}
                  className="flex items-center gap-2 py-1.5 px-3 rounded-lg cursor-pointer hover:bg-brain-700/50 transition-colors"
                  onClick={() => onOpenPerson(person.person_id, person.name || "")}
                >
                  <TreeNodeBadge type="person" />
                  <span className="text-white text-sm">{person.name || person.snippet}</span>
                  {person.role && <span className="text-slate-500 text-xs">{person.role}</span>}
                </div>
              ))}
            </div>
          )}
          {/* Projects flat */}
          {hierarchy.projects?.length > 0 && (
            <div className="space-y-1">
              <div className="text-slate-400 text-xs font-medium uppercase tracking-wider mb-1">{t("projects_label")}</div>
              {hierarchy.projects.map((proj: any, i: number) => (
                <div
                  key={i}
                  className="flex items-center gap-2 py-1.5 px-3 rounded-lg cursor-pointer hover:bg-brain-700/50 transition-colors"
                  onClick={() => onOpenProject(proj.project_id || proj.name?.toLowerCase().replace(/\s+/g, '_'))}
                >
                  <TreeNodeBadge type="project" status={proj.status} />
                  <span className="text-white text-sm">{proj.name}</span>
                  {proj.lead && <span className="text-slate-500 text-xs">({proj.lead})</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ═══ Global Delta ═══ */}
      {delta && delta.status === "ok" && delta.delta_summary && (
        <div className="bg-amber-500/10 border border-amber-500/20 rounded-lg p-4">
          <h3 className="text-amber-400 font-semibold text-sm mb-2">{t("recent_changes_heading")}</h3>
          {delta.delta_details && Object.keys(delta.delta_details).length > 0 ? (
            <DeltaDetailsPretty details={delta.delta_details} />
          ) : (
            <p className="text-amber-300 text-sm">{delta.delta_summary}</p>
          )}
        </div>
      )}
    </div>
  );
}

// Человеческий рендер дельты снапшота (вместо сырого JSON-дампа).
// Известные поля — осмысленными строками/чипами; неизвестные не показываем.
function DeltaDetailsPretty({ details }: { details: Record<string, any> }) {
  const t = useTranslations("snapshots_panel");
  const FIELD_LABEL: Record<string, string> = {
    name: t("delta_field_name"),
    current_status: t("delta_field_status"),
    size: t("delta_field_size"),
    health_score: t("delta_field_health"),
  };
  const Chip = ({ text, tone }: { text: string; tone: "add" | "del" | "info" }) => (
    <span
      className={`inline-block text-[11px] rounded-full px-2 py-0.5 mr-1 mb-1 border ${
        tone === "add"
          ? "text-emerald-300 bg-emerald-500/10 border-emerald-500/25"
          : tone === "del"
          ? "text-rose-300 bg-rose-500/10 border-rose-500/25 line-through"
          : "text-slate-300 bg-brain-700/30 border-brain-600/30"
      }`}
    >
      {text}
    </span>
  );
  const rows: React.ReactNode[] = [];
  for (const [field, label] of Object.entries(FIELD_LABEL)) {
    const d = details[field];
    if (d && typeof d === "object" && ("old" in d || "new" in d)) {
      rows.push(
        <div key={field} className="flex items-baseline gap-2 text-sm">
          <span className="text-slate-400 flex-none">{label}:</span>
          <span className="text-slate-400 line-through">{String(d.old ?? "—")}</span>
          <span className="text-slate-500">→</span>
          <span className="text-amber-200 font-medium">{String(d.new ?? "—")}</span>
        </div>
      );
    }
  }
  const proj = details.active_projects;
  if (proj && (proj.added?.length || proj.removed?.length)) {
    rows.push(
      <div key="projects" className="text-sm">
        <span className="text-slate-400">{t("delta_projects_label")} </span>
        {(proj.added || []).map((p: string, i: number) => (
          <Chip key={`a${i}`} text={`+ ${p}`} tone="add" />
        ))}
        {(proj.removed || []).map((p: string, i: number) => (
          <Chip key={`r${i}`} text={p} tone="del" />
        ))}
      </div>
    );
  }
  const people = details.key_people;
  if (people?.added?.length) {
    rows.push(
      <div key="people" className="text-sm">
        <span className="text-slate-400">{t("delta_new_people_label")} </span>
        {people.added.map((p: string, i: number) => (
          <Chip key={i} text={p} tone="add" />
        ))}
      </div>
    );
  }
  if (details.kpis_updated) {
    rows.push(
      <div key="kpi" className="text-sm">
        <Chip text={t("delta_kpis_updated")} tone="info" />
      </div>
    );
  }
  if (!rows.length) return null;
  return <div className="space-y-1.5">{rows}</div>;
}

// Main Component
// Hierarchy API
const API_BASE_H = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Порядок и подписи категорий людей (category приходит с бэка:
// management/employee/partner/client/candidate/external).
const PERSON_CATEGORY_ORDER: { key: string; labelKey: string; icon: string }[] = [
  { key: "management", labelKey: "category_management", icon: "👑" },
  { key: "employee", labelKey: "employees_label", icon: "👤" },
  { key: "partner", labelKey: "category_partners", icon: "🤝" },
  { key: "client", labelKey: "category_clients", icon: "💼" },
  { key: "candidate", labelKey: "category_candidates", icon: "🎯" },
  { key: "external", labelKey: "category_external_contacts", icon: "🌐" },
];

async function fetchHierarchy(userId: string) {
  const res = await authFetch(`${API_BASE_H}/api/v1/snapshots/hierarchy?user_id=${userId}`);
  if (!res.ok) throw new Error("Failed to fetch hierarchy");
  return res.json();
}

async function fetchDelta(userId: string, snapshotType: string, entityId: string = "company") {
  const res = await authFetch(`${API_BASE_H}/api/v1/snapshots/delta/${snapshotType}?user_id=${userId}&entity_id=${entityId}`);
  if (!res.ok) throw new Error("Failed to fetch delta");
  return res.json();
}

interface SnapshotsPanelFocusRequest {
  tab: "company" | "person" | "project";
  entityId?: string;
  requestId: number;
}

export default function SnapshotsPanel({
  userId,
  focusRequest,
}: {
  userId: string;
  focusRequest?: SnapshotsPanelFocusRequest | null;
}) {
  const t = useTranslations("snapshots_panel");
  const [activeTab, setActiveTab] = useState<"hierarchy" | "company" | "person" | "project">("hierarchy");
  const [companySnapshot, setCompanySnapshot] = useState<CompanySnapshot | null>(null);
  const [personSnapshots, setPersonSnapshots] = useState<PersonSnapshot[]>([]);
  const [projectSnapshots, setProjectSnapshots] = useState<ProjectSnapshot[]>([]);
  const [selectedPersonId, setSelectedPersonId] = useState<string>("");
  const [selectedPersonName, setSelectedPersonName] = useState<string>("");
  const [selectedProjectId, setSelectedProjectId] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [textView, setTextView] = useState(false);
  const [textContent, setTextContent] = useState<string>("");
  // Дефолт — текстовое дерево (описание + иерархия), а не 3D: на жидкой
  // оргструктуре (отделы/связи ещё не наполнены ресинком) пустой 3D-граф
  // бесполезен. 3D включается кнопкой «✨ 3D-оргсхема».
  // Вид иерархии: 'scheme' — структурная оргсхема (колонки отделов,
  // drag&drop, поиск), 'map' — навигационная карта (иерархия + дельта),
  // '3d' — живой 3D-граф. По умолчанию оргсхема.
  const [hierView, setHierView] = useState<'scheme' | 'map' | '3d'>('scheme');
  const [hierarchy, setHierarchy] = useState<any>(null);
  const [delta, setDelta] = useState<any>(null);
  // Drag&drop переклассификация людей: локальный оверлей поверх category
  // с бэка (оптимистичное обновление) + индикатор сохранения.
  const [catOverrides, setCatOverrides] = useState<Record<string, string>>({});
  const [dragOverCat, setDragOverCat] = useState<string | null>(null);
  const [reclassifyingId, setReclassifyingId] = useState<string | null>(null);

  const reclassifyPerson = useCallback(async (personId: string, cat: string) => {
    setCatOverrides((prev) => ({ ...prev, [personId]: cat }));
    setReclassifyingId(personId);
    try {
      const res = await authFetch(
        `${API_BASE_H}/api/v1/snapshots/enhanced/person/${encodeURIComponent(personId)}/classification?user_id=${userId}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ classification: cat }),
        }
      );
      const data = await res.json();
      if (data.status !== "ok") throw new Error(data.message || "failed");
    } catch (e) {
      // откат оптимистичного обновления
      setCatOverrides((prev) => {
        const next = { ...prev };
        delete next[personId];
        return next;
      });
      console.error("reclassify failed:", e);
    } finally {
      setReclassifyingId(null);
    }
  }, [userId]);

  const loadHierarchy = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await fetchHierarchy(userId);
      setHierarchy(data.hierarchy || null);
      // Also load company delta
      try {
        const deltaData = await fetchDelta(userId, "company");
        setDelta(deltaData);
      } catch { /* delta optional */ }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("err_loading_hierarchy"));
    } finally {
      setLoading(false);
    }
  }, [userId]);

  const loadCompanySnapshot = useCallback(async (regenerate = false) => {
    try {
      setLoading(true);
      setError(null);
      const format = textView ? "text" : "json";
      const data = await fetchCompanySnapshot(userId, format, regenerate);
      
      if (textView) {
        setTextContent(data.content || "");
      } else {
        setCompanySnapshot(data.snapshot || null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("err_loading"));
    } finally {
      setLoading(false);
    }
  }, [userId, textView]);

  const loadPersonSnapshot = useCallback(async (personId: string, personName = "") => {
    if (!personId) return;
    try {
      setLoading(true);
      setError(null);
      const format = textView ? "text" : "json";
      const data = await fetchPersonSnapshot(userId, personId, format, personName);
      
      if (data.error) {
        setError(data.message || data.error);
      } else if (textView) {
        setTextContent(data.content || "");
      } else if (data.snapshot) {
        // Мержим timeline (отдельное поле ответа) в объект снапшота.
        const snapWithTimeline = { ...data.snapshot, timeline: data.timeline || [] };
        setPersonSnapshots((prev) => {
          const idx = prev.findIndex((p) => p.person_id === personId);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = snapWithTimeline;
            return next;
          }
          return [...prev, snapWithTimeline];
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("err_loading"));
    } finally {
      setLoading(false);
    }
  }, [userId, textView]);

  const loadProjectSnapshot = useCallback(async (projectId: string) => {
    if (!projectId) return;
    try {
      setLoading(true);
      setError(null);
      const format = textView ? "text" : "json";
      const data = await fetchProjectSnapshot(userId, projectId, format);
      
      if (data.error) {
        setError(data.message || data.error);
      } else if (textView) {
        setTextContent(data.content || "");
      } else if (data.snapshot) {
        const snapWithTimeline = { ...data.snapshot, timeline: data.timeline || [] };
        setProjectSnapshots((prev) => {
          const idx = prev.findIndex((p) => p.project_id === projectId);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = snapWithTimeline;
            return next;
          }
          return [...prev, snapWithTimeline];
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("err_loading"));
    } finally {
      setLoading(false);
    }
  }, [userId, textView]);

  // Load hierarchy on mount (needed for person/project card selectors)
  useEffect(() => {
    loadHierarchy();
  }, [loadHierarchy]);
  
  useEffect(() => {
    if (activeTab === "company") {
      loadCompanySnapshot();
    }
  }, [activeTab, loadCompanySnapshot]);

  useEffect(() => {
    if (activeTab === "person" && selectedPersonId) {
      loadPersonSnapshot(selectedPersonId, selectedPersonName);
    }
  }, [activeTab, selectedPersonId, selectedPersonName, loadPersonSnapshot]);

  useEffect(() => {
    if (activeTab === "project" && selectedProjectId) {
      loadProjectSnapshot(selectedProjectId);
    }
  }, [activeTab, selectedProjectId, loadProjectSnapshot]);

  useEffect(() => {
    if (!focusRequest) return;

    if (focusRequest.tab === "company") {
      setSelectedPersonId("");
      setSelectedProjectId("");
      setActiveTab("company");
      return;
    }

    if (focusRequest.tab === "person" && focusRequest.entityId) {
      setSelectedProjectId("");
      setSelectedPersonId(focusRequest.entityId);
      setActiveTab("person");
      return;
    }

    if (focusRequest.tab === "project" && focusRequest.entityId) {
      setSelectedPersonId("");
      setSelectedProjectId(focusRequest.entityId);
      setActiveTab("project");
    }
  }, [focusRequest?.requestId, focusRequest]);

  const currentPersonSnapshot = personSnapshots.find((p) => p.person_id === selectedPersonId);
  const currentProjectSnapshot = projectSnapshots.find((p) => p.project_id === selectedProjectId);

  return (
    <div className="h-full flex flex-col bg-gradient-to-b from-brain-900/30 via-brain-900/40 to-brain-950/60">
      {/* Header */}
      <div className="p-4 border-b border-brain-700/30 bg-brain-900/30 backdrop-blur-sm">
        <div className="flex items-center justify-between mb-4 gap-2">
          <h2 className="text-white text-lg font-semibold">{t("panel_title")}</h2>
          {/* uid — мгновенная диагностика «чей это снапшот» */}
          <span className="text-[10px] font-mono text-slate-600 mr-auto ml-2"
                title={t("uid_title")}>
            uid: {String(userId || '').slice(0, 8)}…
          </span>
          {activeTab === "company" && (
            <button
              onClick={() => loadCompanySnapshot(true)}
              disabled={loading}
              title={t("regenerate_title")}
              className="px-3 py-1 text-sm rounded transition-colors bg-brain-800/40 text-slate-400 hover:text-white disabled:opacity-40"
            >
              {t("regenerate_button")}
            </button>
          )}
          <button
            onClick={() => setTextView(!textView)}
            className={`px-3 py-1 text-sm rounded transition-colors ${
              textView
                ? "bg-brain-600 text-white shadow-lg shadow-brain-600/25"
                : "bg-brain-800/40 text-slate-400 hover:text-white"
            }`}
          >
            {textView ? t("view_text") : t("view_json")}
          </button>
        </div>

        {/* Tabs */}
        <div className="flex gap-2 flex-wrap">
          {(["hierarchy", "company", "person", "project"] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2 text-sm rounded-lg transition-colors ${
                activeTab === tab
                  ? "bg-brain-600 text-white shadow-lg shadow-brain-600/25"
                  : "bg-brain-800/40 text-slate-400 hover:text-white"
              }`}
            >
              {tab === "hierarchy" ? t("tab_hierarchy") : tab === "company" ? t("tab_company") : tab === "person" ? t("tab_person") : t("tab_project")}
            </button>
          ))}
        </div>

        {/* Breadcrumb navigation */}
        {(activeTab === "person" || activeTab === "project") && (
          <div className="mt-3 flex items-center gap-2 text-sm">
            <button onClick={() => setActiveTab("hierarchy")} className="text-blue-400 hover:underline">
              {t('breadcrumb_hierarchy')}
            </button>
            <span className="text-slate-600">/</span>
            <span className="text-slate-300">
              {activeTab === "person" ? (currentPersonSnapshot?.name || selectedPersonId || t("breadcrumb_person_fallback")) : 
               (currentProjectSnapshot?.name || selectedProjectId || t("breadcrumb_project_fallback"))}
            </span>
          </div>
        )}
        
        {/* Person card selector (from hierarchy) — сгруппирован по категориям.
            Карточки ПЕРЕТАСКИВАЮТСЯ между категориями (drag&drop): бросил
            человека в «Клиенты» → ручная классификация сохраняется на бэке
            и переживает ре-генерацию снапшотов. */}
        {activeTab === "person" && !selectedPersonId && hierarchy?.persons && (
          <div className="mt-3 max-h-[65vh] overflow-y-auto pr-1 space-y-3">
            <div className="text-slate-500 text-[11px] px-1">
              {t("drag_hint")}
            </div>
            {PERSON_CATEGORY_ORDER.map(({ key, labelKey, icon }) => {
              const group = (hierarchy.persons as any[]).filter(
                (p) => (catOverrides[p.person_id] || p.category || "employee") === key
              );
              return (
                <div
                  key={key}
                  onDragOver={(e) => { e.preventDefault(); setDragOverCat(key); }}
                  onDragLeave={() => setDragOverCat((c) => (c === key ? null : c))}
                  onDrop={(e) => {
                    e.preventDefault();
                    setDragOverCat(null);
                    const pid = e.dataTransfer.getData("text/person-id");
                    if (pid) reclassifyPerson(pid, key);
                  }}
                  className={`rounded-lg transition-colors ${dragOverCat === key ? "bg-brain-700/30 ring-1 ring-blue-400/50" : ""}`}
                >
                  <div className="sticky top-0 bg-brain-900/90 backdrop-blur-sm text-slate-400 text-xs font-semibold uppercase tracking-wider py-1.5 px-1 flex items-center gap-1.5">
                    <span>{icon}</span>
                    <span>{t(labelKey)}</span>
                    <span className="text-slate-600 font-normal">· {group.length}</span>
                    {dragOverCat === key && <span className="text-blue-400 normal-case font-normal">{t("drop_here_hint")}</span>}
                  </div>
                  {group.length === 0 && dragOverCat !== key ? (
                    <div className="text-slate-700 text-[11px] px-2 py-1">{t("empty_placeholder")}</div>
                  ) : (
                  <div className="grid gap-2 grid-cols-1 mt-1">
                    {group.map((person: any, i: number) => (
                      <button
                        key={`${key}_${i}`}
                        draggable
                        onDragStart={(e) => {
                          e.dataTransfer.setData("text/person-id", person.person_id || "");
                          e.dataTransfer.effectAllowed = "move";
                        }}
                        onClick={() => { setSelectedPersonId(person.person_id); setSelectedPersonName(person.name || ""); }}
                        className={`bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/30 rounded p-3 text-left transition-colors cursor-grab active:cursor-grabbing ${reclassifyingId === person.person_id ? "opacity-50" : ""}`}
                        title={t("drag_to_reclassify_title")}
                      >
                        <div className="text-white text-sm font-medium">{person.name || person.snippet}</div>
                        <div className="flex items-center gap-2 mt-0.5">
                          {person.role && <span className="text-slate-400 text-xs">{person.role}</span>}
                          {person.department && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-brain-700/40 text-brain-200">🏢 {person.department}</span>
                          )}
                        </div>
                      </button>
                    ))}
                  </div>
                  )}
                </div>
              );
            })}
            {(!hierarchy.persons || hierarchy.persons.length === 0) && (
              <div className="text-slate-500 text-sm py-2">{t("no_employees_hint")}</div>
            )}
          </div>
        )}
        
        {/* Project card selector (from hierarchy) */}
        {activeTab === "project" && !selectedProjectId && hierarchy?.projects && (
          <div className="mt-3 grid gap-2 grid-cols-1 max-h-[65vh] overflow-y-auto pr-1">
            {hierarchy.projects.map((proj: any, i: number) => (
              <button
                key={i}
                onClick={() => { setSelectedProjectId(proj.project_id || proj.name?.toLowerCase().replace(/\s+/g, '_') || `project_${i}`); }}
                className="bg-brain-800/40 hover:bg-brain-700/50 border border-brain-600/30 rounded p-3 text-left transition-colors"
              >
                <div className="flex items-center gap-2">
                  <span className="text-white text-sm font-medium">{proj.name}</span>
                  <span className={`text-xs px-2 py-0.5 rounded ${
                    proj.status === 'active' ? 'bg-green-500/20 text-green-400' : 'bg-slate-500/20 text-slate-400'
                  }`}>{proj.status}</span>
                </div>
                <div className="text-slate-400 text-xs mt-1">{proj.snippet}</div>
              </button>
            ))}
            {(!hierarchy.projects || hierarchy.projects.length === 0) && (
              <div className="text-slate-500 text-sm py-2">{t("no_projects_hint")}</div>
            )}
          </div>
        )}
        
        {/* Back button when viewing specific snapshot */}
        {activeTab === "person" && selectedPersonId && (
          <button onClick={() => setSelectedPersonId("")} className="mt-2 text-blue-400 text-xs hover:underline">
            {t('back_to_employees')}
          </button>
        )}
        {activeTab === "project" && selectedProjectId && (
          <button onClick={() => setSelectedProjectId("")} className="mt-2 text-blue-400 text-xs hover:underline">
            {t('back_to_projects')}
          </button>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4">
        {error && (
          <div className="bg-red-500/20 border border-red-500/30 rounded p-3 text-red-400 text-sm mb-4">
            {error}
          </div>
        )}

        {loading ? (
          <div className="flex flex-col items-center justify-center py-16 text-slate-400">
            <div className="w-8 h-8 border-2 border-brain-500/30 border-t-brain-400 rounded-full animate-spin mb-3" />
            <div className="text-sm">{t("loading")}</div>
          </div>
        ) : textView ? (
          <pre className="bg-brain-800/30 rounded-lg p-4 text-slate-300 text-sm whitespace-pre-wrap font-mono overflow-x-auto">
            {textContent || t("no_data")}
          </pre>
        ) : (
          <>
            {/* Hierarchy View — переключатель: оргсхема / карта / 3D */}
            {activeTab === "hierarchy" && (
              <div>
                <div className="flex justify-end gap-1 mb-2">
                  {([
                    ['scheme', t('view_scheme')],
                    ['map', t('view_map')],
                    ['3d', '✨ 3D'],
                  ] as const).map(([key, label]) => (
                    <button
                      key={key}
                      onClick={() => setHierView(key)}
                      className={`text-[11px] rounded-md px-2.5 py-1 border transition-colors ${
                        hierView === key
                          ? 'text-white border-brain-400/60 bg-brain-700/40'
                          : 'text-brain-300 hover:text-white border-brain-600/30 hover:border-brain-500/50'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {hierView === '3d' && (
                  <div className="h-[660px] rounded-xl overflow-hidden border border-brain-700/30 bg-brain-950/40">
                    <Graph3DView
                      userId={userId}
                      mode="org"
                      onFallback={() => setHierView('scheme')}
                      fallbackLabel={t("scheme_fallback_label")}
                      onOpenEntity={(type, id) => {
                        if (type === 'person') { setSelectedPersonId(id); setActiveTab('person') }
                      }}
                    />
                  </div>
                )}
                {hierView === 'scheme' && (
                  <div className="h-[660px] rounded-xl overflow-hidden border border-brain-700/30 bg-brain-950/40 p-3">
                    <OrgGraphView
                      userId={userId}
                      onOpenEntity={(type: string, id: string) => {
                        if (type === 'person') { setSelectedPersonId(id); setActiveTab('person') }
                      }}
                    />
                  </div>
                )}
                {hierView === 'map' && (hierarchy ? (
                  <NavigationTreeDiagram
                    hierarchy={hierarchy}
                    delta={delta}
                    userId={userId}
                    onOpenCompany={() => { setActiveTab("company"); loadCompanySnapshot(); }}
                    onOpenPerson={(personId: string, name?: string) => { setSelectedPersonId(personId); setSelectedPersonName(name || ""); setActiveTab("person"); }}
                    onOpenProject={(projectId: string) => { setSelectedProjectId(projectId); setActiveTab("project"); }}
                  />
                ) : !loading ? (
                  <div className="flex flex-col items-center justify-center py-16 text-center">
                    <div className="text-4xl mb-3 opacity-60">🗂️</div>
                    <div className="text-slate-300 text-sm max-w-sm">{t("hierarchy_not_loaded")}</div>
                  </div>
                ) : null)}
              </div>
            )}

            {activeTab === "company" && companySnapshot && (
              <>
                <SnapshotTimeline entityType="company" entityId="company" userId={userId} />
                <CompanySnapshotView snapshot={companySnapshot} userId={userId} onUpdated={setCompanySnapshot} />
              </>
            )}
            {activeTab === "person" && currentPersonSnapshot && (
              <>
                <SnapshotTimeline entityType="person" entityId={selectedPersonId} userId={userId} />
                <PersonSnapshotView snapshot={currentPersonSnapshot} userId={userId} />
              </>
            )}
            {activeTab === "project" && currentProjectSnapshot && (
              <>
                <SnapshotTimeline entityType="project" entityId={selectedProjectId} userId={userId} />
                <ProjectSnapshotView snapshot={currentProjectSnapshot} />
              </>
            )}
            {!companySnapshot && activeTab === "company" && (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <div className="text-4xl mb-3 opacity-60">🏢</div>
                <div className="text-slate-300 text-sm max-w-sm">{t("company_snapshot_not_found")}</div>
              </div>
            )}
            {!currentPersonSnapshot && activeTab === "person" && selectedPersonId && !loading && (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <div className="text-4xl mb-3 opacity-60">👤</div>
                <div className="text-slate-300 text-base mb-2">{t("person_snapshot_not_found")}</div>
                {error ? (
                  <div className="text-amber-400/80 text-sm max-w-sm">{error}</div>
                ) : (
                  <div className="text-slate-500 text-sm max-w-sm">{t("person_snapshot_hint")}</div>
                )}
              </div>
            )}
            {!currentProjectSnapshot && activeTab === "project" && selectedProjectId && !loading && (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <div className="text-4xl mb-3 opacity-60">📁</div>
                <div className="text-slate-300 text-base mb-2">{t("project_snapshot_not_found")}</div>
                {error ? (
                  <div className="text-amber-400/80 text-sm max-w-sm">{error}</div>
                ) : (
                  <div className="text-slate-500 text-sm max-w-sm">{t("project_snapshot_hint")}</div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}


