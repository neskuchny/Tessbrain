"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { authFetch, getUserIdFromToken } from "@/lib/authFetch";

interface Meeting {
  id: string;
  title: string;
  created_at?: string;
  project_id?: string;
  folder_id?: string;
}
type NameMap = Record<string, string>;

/** Выбор встречи для узлов доски (meeting_data / meeting_share) в человеческом
 *  виде: список встреч, сгруппированный по проектам/папкам, с поиском по
 *  названию. Никто не должен вписывать UUID руками. Пустое значение =
 *  «встреча из триггера» (доска, запущенная событием завершения встречи,
 *  возьмёт именно её). Значение — meeting_id, как и раньше: бэкенд не менялся. */
export function MeetingPicker({ value, valueTitle, onChange }: {
  value: string;
  valueTitle?: string;
  onChange: (id: string, title: string) => void;
}) {
  const t = useTranslations("banana_process_node");
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [projects, setProjects] = useState<NameMap>({});
  const [folders, setFolders] = useState<NameMap>({});
  const [q, setQ] = useState("");

  useEffect(() => {
    if (!open || loaded) return;
    (async () => {
      setLoading(true);
      try {
        const uid = getUserIdFromToken();
        const uq = uid ? `user_id=${encodeURIComponent(uid)}` : "";
        const [rm, rp, rf] = await Promise.all([
          authFetch(`/api/v1/meetflow/meetings?limit=200&${uq}`),
          authFetch(`/api/v1/meetflow/projects?${uq}`),
          authFetch(`/api/v1/meetflow/folders?${uq}`),
        ]);
        const dm = await rm.json();
        const dp = await rp.json();
        const df = await rf.json();
        setMeetings((dm?.meetings || []).map((m: any) => ({
          id: String(m.id),
          title: m.title || "—",
          created_at: m.created_at,
          project_id: m.project_id ? String(m.project_id) : "",
          folder_id: m.folder_id ? String(m.folder_id) : "",
        })));
        const pm: NameMap = {};
        (dp?.projects || []).forEach((p: any) => { pm[String(p.id)] = p.name || "—"; });
        const fm: NameMap = {};
        (df?.folders || []).forEach((f: any) => { fm[String(f.id)] = f.name || "—"; });
        setProjects(pm);
        setFolders(fm);
        setLoaded(true);
      } catch { /* список останется пустым — покажем подсказку */ }
      finally { setLoading(false); }
    })();
  }, [open, loaded]);

  // Проект / папка → встречи (API уже отдаёт свежие сверху).
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = needle
      ? meetings.filter((m) => (m.title || "").toLowerCase().includes(needle))
      : meetings;
    const by: { key: string; label: string; items: Meeting[] }[] = [];
    const idx: Record<string, number> = {};
    for (const m of list) {
      const pname = m.project_id ? (projects[m.project_id] || "") : "";
      const fname = m.folder_id ? (folders[m.folder_id] || "") : "";
      const label = [pname, fname].filter(Boolean).join(" / ")
        || t("meeting_picker_ungrouped");
      const key = `${m.project_id}|${m.folder_id}`;
      if (!(key in idx)) { idx[key] = by.length; by.push({ key, label, items: [] }); }
      by[idx[key]].items.push(m);
    }
    return by;
  }, [meetings, projects, folders, q, t]);

  const fmtDate = (s?: string) => {
    try { return s ? new Date(s).toLocaleDateString() : ""; } catch { return ""; }
  };

  const selectedLabel = value === "__latest__"
    ? t("meeting_picker_latest")
    : value
      ? (valueTitle || meetings.find((m) => m.id === value)?.title || value)
      : t("meeting_picker_trigger");

  return (
    <div className="space-y-1">
      <button type="button" onClick={() => setOpen(!open)}
        title={t("meeting_data_id_title")}
        className="nodrag nopan w-full p-1.5 text-xs text-left text-brain-50 border border-brain-700/70 rounded bg-brain-950/40 hover:border-brain-500 truncate">
        {value ? "📅 " : "⚡ "}{selectedLabel}
      </button>
      {open && (
        <div className="nodrag nopan nowheel rounded border border-brain-700/60 bg-brain-950/60 p-1 space-y-1">
          <input value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("meeting_picker_search")}
            className="w-full p-1 text-[11px] text-brain-50 border border-brain-700/60 rounded bg-brain-950/40 focus:outline-none placeholder:text-brain-200/40" />
          {value && (
            <button type="button"
              onClick={() => { onChange("", ""); setOpen(false); }}
              className="w-full p-1 text-[10px] text-left text-amber-300/90 hover:text-amber-200 rounded bg-brain-900/60">
              ⚡ {t("meeting_picker_reset")}
            </button>
          )}
          <button type="button"
            onClick={() => { onChange("__latest__", ""); setOpen(false); }}
            className={`w-full p-1 text-[10px] text-left rounded ${value === "__latest__" ? "bg-brain-800/80 text-blue-200" : "text-emerald-300/90 hover:text-emerald-200 bg-brain-900/60"}`}>
            🕐 {t("meeting_picker_latest")}
          </button>
          <div className="max-h-44 overflow-y-auto space-y-1">
            {loading ? (
              <div className="text-[10px] text-brain-500 px-1 py-2">…</div>
            ) : groups.length === 0 ? (
              <div className="text-[10px] text-brain-500 px-1 py-2">{t("meeting_picker_none")}</div>
            ) : groups.map((g) => (
              <div key={g.key}>
                <div className="text-[9px] uppercase tracking-wide text-brain-400 px-1 pt-1">
                  📁 {g.label}
                </div>
                {g.items.map((m) => (
                  <button key={m.id} type="button"
                    onClick={() => { onChange(m.id, m.title); setOpen(false); }}
                    className={`w-full px-1.5 py-1 text-[11px] text-left rounded flex items-baseline gap-1.5 hover:bg-brain-800/70 ${m.id === value ? "bg-brain-800/80 text-blue-200" : "text-brain-100"}`}>
                    <span className="truncate flex-1">{m.title}</span>
                    <span className="text-[9px] text-brain-500 shrink-0">{fmtDate(m.created_at)}</span>
                  </button>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="text-[9px] text-brain-500 px-0.5">{t("meeting_picker_hint")}</div>
    </div>
  );
}
