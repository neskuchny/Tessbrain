"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import {
  BoardMeta,
  deleteBoard,
  getBoard,
  listBoards,
  saveBoard,
} from "@/banana/serverBoards";

/**
 * «Мои доски» (P1): серверное хранение досок. В отличие от «Сохранить» (файл)
 * и шаблонов (localStorage браузера) — переживает смену браузера/устройства.
 */
export function MyBoardsMenu() {
  const t = useTranslations("banana_boards_menu");
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const edgeStyle = useWorkflowStore((s) => s.edgeStyle);
  const boardKind = useWorkflowStore((s) => s.boardKind);
  const setBoardKind = useWorkflowStore((s) => s.setBoardKind);
  const setBoardId = useWorkflowStore((s) => s.setBoardId);
  const setBoardName = useWorkflowStore((s) => s.setBoardName);
  const loadWorkflow = useWorkflowStore((s) => s.loadWorkflow);

  const [open, setOpen] = useState(false);
  const [boards, setBoards] = useState<BoardMeta[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  // Автоматизации по доскам: ⏰ расписание / ⚡ событие + вкл/выкл — чтобы
  // видеть, ЧТО сработает само, и выключать прямо из списка.
  const [autos, setAutos] = useState<Record<string, { trigger: string; enabled: boolean }>>({});

  const refresh = async () => {
    setBoards(await listBoards());
    try {
      const r = await fetch("/api/v1/boards/schedules");
      const d = await r.json();
      const map: Record<string, { trigger: string; enabled: boolean }> = {};
      (d.schedules || []).forEach((s: any) => {
        if (s.id) map[s.id] = { trigger: s.trigger || "schedule", enabled: s.enabled !== false };
      });
      setAutos(map);
    } catch { /* бейджей не будет — список работает как раньше */ }
  };

  const toggleAuto = async (id: string) => {
    const cur = autos[id];
    if (!cur) return;
    try {
      await fetch(`/api/v1/boards/${id}/toggle-automation`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !cur.enabled }),
      });
      await refresh();
    } catch { /* оставим как есть */ }
  };

  useEffect(() => {
    if (open) refresh();
  }, [open]);

  const doSave = async () => {
    setBusy(true);
    setMsg("");
    const graph = { version: 1 as const, name: name || t("untitled"), nodes, edges, edgeStyle };
    const res = await saveBoard(name || t("untitled"), graph, currentId || undefined, boardKind);
    if (res) {
      setCurrentId(res.id); setBoardId(res.id);
      setBoardName(name || t("untitled"));
      setMsg(`✅ ${t("saved_on_server")}`);
      await refresh();
    } else {
      setMsg(`⚠️ ${t("save_failed")}`);
    }
    setBusy(false);
  };

  const doLoad = async (id: string, boardName: string) => {
    setBusy(true);
    const b = await getBoard(id);
    if (b?.graph?.nodes) {
      loadWorkflow(b.graph);
      if (b.kind === "creative" || b.kind === "process") setBoardKind(b.kind);
      setCurrentId(id); setBoardId(id);
      setBoardName(boardName);
      setName(boardName);
      setMsg("");
      setOpen(false);
    } else {
      setMsg(`⚠️ ${t("open_failed")}`);
    }
    setBusy(false);
  };

  const doDelete = async (id: string) => {
    if (!confirm(t("delete_confirm"))) return;
    await deleteBoard(id);
    if (currentId === id) { setCurrentId(null); setBoardId(null); }
    await refresh();
  };

  return (
    <div className="relative" ref={menuRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        title={t("menu_title")}
        className="p-1.5 text-brain-200/70 hover:text-brain-50 hover:bg-brain-800 rounded transition-colors"
      >
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round"
            d="M3 15a4 4 0 004 4h10a4 4 0 000-8 5 5 0 00-9.584-1.5A3.5 3.5 0 003 15z" />
        </svg>
      </button>

      {open && (
        <div className="absolute bottom-full right-0 mb-2 w-72 bg-brain-900 border border-brain-700/60 rounded-lg shadow-xl overflow-hidden text-[12px]">
          <div className="p-2 border-b border-brain-700/60 space-y-2">
            <div className="text-brain-200 font-medium">{t("menu_title")}</div>
            <div className="flex gap-1">
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t("name_placeholder")}
                className="flex-1 bg-brain-800 border border-brain-700/60 rounded px-2 py-1 text-brain-100 placeholder-brain-500 focus:outline-none focus:border-purple-500"
              />
              <button
                onClick={doSave}
                disabled={busy}
                className="px-2 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white disabled:opacity-50"
              >
                {currentId ? t("update") : t("save")}
              </button>
            </div>
            {msg && <div className="text-[11px] text-brain-400">{msg}</div>}
          </div>
          <div className="max-h-56 overflow-y-auto divide-y divide-brain-800">
            {boards.length === 0 ? (
              <div className="px-3 py-3 text-brain-500">{t("empty_state")}</div>
            ) : (
              boards.map((b) => (
                <div key={b.id} className="flex items-center gap-1 px-2 py-1.5 hover:bg-brain-800/50">
                  <button
                    onClick={() => doLoad(b.id, b.name)}
                    className={`flex-1 text-left truncate ${currentId === b.id ? "text-purple-300" : "text-brain-200"}`}
                    title={b.name}
                  >
                    {currentId === b.id ? "● " : ""}{b.name}
                  </button>
                  {autos[b.id] && (
                    <button
                      onClick={() => toggleAuto(b.id)}
                      title={autos[b.id].enabled ? t("auto_on_title") : t("auto_off_title")}
                      className={`px-1 text-[13px] ${autos[b.id].enabled ? "" : "opacity-40 grayscale"}`}
                    >
                      {autos[b.id].trigger === "event" ? "⚡" : "⏰"}
                    </button>
                  )}
                  <button
                    onClick={() => doDelete(b.id)}
                    className="text-brain-500 hover:text-red-300 px-1"
                    title={t("delete")}
                  >
                    ✕
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
