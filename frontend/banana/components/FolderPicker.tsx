"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { authFetch } from "@/lib/authFetch";

interface Folder { id: string; name: string }

/** Компактный выбор папок встреч для триггера доски (folder-scoped triggers).
 *  Пусто = любая папка. Значение — массив folder_id. */
export function FolderPicker({ value, onChange }: {
  value: string[];
  onChange: (ids: string[]) => void;
}) {
  const t = useTranslations("banana_process_node");
  const [folders, setFolders] = useState<Folder[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const r = await authFetch("/api/v1/meetflow/folders");
        const d = await r.json();
        setFolders((d?.folders || []).map((f: any) => ({ id: String(f.id), name: f.name || "—" })));
      } catch { /* оставим пусто */ } finally { setLoading(false); }
    })();
  }, []);

  const toggle = (id: string) => {
    onChange(value.includes(id) ? value.filter((x) => x !== id) : [...value, id]);
  };

  return (
    <div className="space-y-1">
      <div className="text-[9px] text-brain-400 px-0.5">{t("trigger_folders_label")}</div>
      {loading ? (
        <div className="text-[10px] text-brain-500 px-0.5">…</div>
      ) : folders.length === 0 ? (
        <div className="text-[10px] text-brain-500 px-0.5">{t("trigger_folders_none")}</div>
      ) : (
        <div className="nodrag nowheel max-h-24 overflow-y-auto rounded border border-brain-700/60 bg-brain-950/40 p-1 space-y-0.5">
          {folders.map((f) => (
            <label key={f.id} className="flex items-center gap-1.5 text-[10px] text-brain-200 cursor-pointer">
              <input type="checkbox" checked={value.includes(f.id)} onChange={() => toggle(f.id)}
                className="w-3 h-3 rounded bg-brain-700 border-brain-600 text-blue-500" />
              <span className="truncate">{f.name}</span>
            </label>
          ))}
        </div>
      )}
      <div className="text-[9px] text-brain-500 px-0.5">{t("trigger_folders_hint")}</div>
    </div>
  );
}
