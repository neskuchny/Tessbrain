"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { authFetch, getUserIdFromToken } from "@/lib/authFetch";

interface DatasetItem {
  dataset_id: string;
  title: string;
  columns?: string[];
  rows_count?: number;
}

/** Выбор датасета для узла «Данные CRM» в человеческом виде: список
 *  подключённых таблиц с колонками и поиском — никто не вписывает ID руками.
 *  Пустое значение = авто-подбор по вопросу (консервативный: если вопрос не
 *  совпал с таблицей, узел теперь честно падает и подсказывает выбрать здесь). */
export function DatasetPicker({ value, valueTitle, onChange }: {
  value: string;
  valueTitle?: string;
  onChange: (id: string, title: string) => void;
}) {
  const t = useTranslations("banana_process_node");
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [items, setItems] = useState<DatasetItem[]>([]);
  const [q, setQ] = useState("");

  useEffect(() => {
    if (!open || loaded) return;
    (async () => {
      setLoading(true);
      try {
        const uid = getUserIdFromToken();
        const uq = uid ? `?user_id=${encodeURIComponent(uid)}` : "";
        const r = await authFetch(`/api/v1/ontology/datasets${uq}`);
        const d = await r.json();
        setItems((d?.datasets || []).map((x: any) => ({
          dataset_id: String(x.dataset_id || x.id || ""),
          title: x.title || "—",
          columns: Array.isArray(x.columns) ? x.columns.map(String) : [],
          rows_count: typeof x.rows_count === "number" ? x.rows_count : undefined,
        })).filter((x: DatasetItem) => x.dataset_id));
        setLoaded(true);
      } catch { /* список останется пустым — покажем подсказку */ }
      finally { setLoading(false); }
    })();
  }, [open, loaded]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((x) =>
      (x.title || "").toLowerCase().includes(needle) ||
      (x.columns || []).some((c) => c.toLowerCase().includes(needle)));
  }, [items, q]);

  const selectedLabel = value
    ? (valueTitle || items.find((x) => x.dataset_id === value)?.title || value)
    : t("dataset_picker_auto");

  return (
    <div className="space-y-1">
      <button type="button" onClick={() => setOpen(!open)}
        title={t("dataset_picker_title")}
        className="nodrag nopan w-full p-1.5 text-xs text-left text-brain-50 border border-brain-700/70 rounded bg-brain-950/40 hover:border-brain-500 truncate">
        {value ? "📊 " : "🎯 "}{selectedLabel}
      </button>
      {open && (
        <div className="nodrag nopan nowheel rounded border border-brain-700/60 bg-brain-950/60 p-1 space-y-1">
          <input value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("dataset_picker_search")}
            className="w-full p-1 text-[11px] text-brain-50 border border-brain-700/60 rounded bg-brain-950/40 focus:outline-none placeholder:text-brain-200/40" />
          {value && (
            <button type="button"
              onClick={() => { onChange("", ""); setOpen(false); }}
              className="w-full p-1 text-[10px] text-left text-amber-300/90 hover:text-amber-200 rounded bg-brain-900/60">
              🎯 {t("dataset_picker_reset")}
            </button>
          )}
          <div className="max-h-44 overflow-y-auto space-y-0.5">
            {loading ? (
              <div className="text-[10px] text-brain-500 px-1 py-2">…</div>
            ) : filtered.length === 0 ? (
              <div className="text-[10px] text-brain-500 px-1 py-2">
                {items.length === 0 ? t("dataset_picker_none") : t("dataset_picker_no_match")}
              </div>
            ) : filtered.map((x) => (
              <button key={x.dataset_id} type="button"
                onClick={() => { onChange(x.dataset_id, x.title); setOpen(false); }}
                className={`w-full px-1.5 py-1 text-[11px] text-left rounded hover:bg-brain-800/70 ${x.dataset_id === value ? "bg-brain-800/80 text-blue-200" : "text-brain-100"}`}>
                <div className="truncate">📊 {x.title}</div>
                {(x.columns || []).length > 0 && (
                  <div className="text-[9px] text-brain-500 truncate">
                    {(x.columns || []).slice(0, 6).join(" · ")}
                  </div>
                )}
              </button>
            ))}
          </div>
        </div>
      )}
      <div className="text-[9px] text-brain-500 px-0.5">{t("dataset_picker_hint")}</div>
    </div>
  );
}
