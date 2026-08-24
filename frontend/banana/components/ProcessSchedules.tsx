"use client";

/**
 * ProcessSchedules — «за чем следить»: повторяющиеся процессы пользователя.
 *
 * Кнопка ⏱ рядом с «Запустить процесс»: поповер со списком процессных досок,
 * у которых стоит schedule-триггер — расписание человеческим языком, последний
 * запуск, оценка следующего (UTC), пометка «пора» и честное предупреждение,
 * если серверные триггеры выключены (BOARD_PROCESS_TRIGGERS=off).
 */
import { useCallback, useState } from "react";
import { useTranslations } from "next-intl";
import { deleteBoard, getBoard } from "@/banana/serverBoards";
import { useWorkflowStore } from "@/banana/store/workflowStore";

function authHeaders(): Record<string, string> {
  const t = typeof window !== "undefined"
    ? localStorage.getItem("tessent_access_token") : null;
  return t ? { Authorization: `Bearer ${t}` } : {};
}

interface ScheduleRow {
  id: string;
  name: string;
  summary?: string; // «что делает»: цепочка узлов человеческими словами
  trigger?: string; // "schedule" | "event"
  enabled?: boolean;
  event_type?: string;
  schedule?: { kind: string; minutes?: number; time?: string };
  last_triggered_at?: string | null;
  next_estimate?: string | null;
  due_now?: boolean;
}

function fmt(iso?: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return String(iso);
  }
}

export function ProcessSchedules() {
  const t = useTranslations("banana_run");
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [rows, setRows] = useState<ScheduleRow[] | null>(null);
  const [triggersOn, setTriggersOn] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/v1/boards/schedules", { headers: authHeaders() });
      const d = await r.json();
      setRows(Array.isArray(d.schedules) ? d.schedules : []);
      if (typeof d.triggers_enabled === "boolean") setTriggersOn(d.triggers_enabled);
    } catch {
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) load();
  };

  // Управление прямо из списка: раньше панель была read-only, и чтобы
  // выключить/переделать сработавшую доску, приходилось искать её в «Моих
  // досках». Теперь: открыть на холсте / вкл-выкл автоматизацию / удалить.
  const loadWorkflow = useWorkflowStore((s) => s.loadWorkflow);
  const setBoardKind = useWorkflowStore((s) => s.setBoardKind);
  const setBoardId = useWorkflowStore((s) => s.setBoardId);

  const openBoard = async (id: string) => {
    const b = await getBoard(id);
    if (b?.graph?.nodes) {
      loadWorkflow(b.graph);
      setBoardKind(b.kind === "creative" ? "creative" : "process");
      setBoardId(id);
      setOpen(false);
    }
  };

  const toggleAutomation = async (r: ScheduleRow) => {
    try {
      await fetch(`/api/v1/boards/${r.id}/toggle-automation`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ enabled: r.enabled === false }),
      });
      await load();
    } catch { /* список не перезагрузится — состояние видно по бейджу */ }
  };

  const removeBoard = async (id: string) => {
    if (!confirm(t("sched_delete_confirm"))) return;
    await deleteBoard(id);
    await load();
  };

  // Строки бывают трёх видов: расписание (есть schedule), событие
  // (trigger="event", schedule нет) и выключенная автоматизация (enabled=false,
  // schedule тоже нет) — подпись собираем без обращения к отсутствующим полям.
  const scheduleLabel = (r: ScheduleRow) => {
    if (r.enabled === false) return `⏸ ${t("sched_off")}`;
    if (r.trigger === "event" || !r.schedule) {
      const ev = r.event_type === "meeting_ended"
        ? t("event_meeting_ended")
        : r.event_type || "—";
      return `⚡ ${t("sched_event", { event: ev })}`;
    }
    return r.schedule.kind === "daily"
      ? t("sched_daily", { time: r.schedule.time || "09:00" })
      : t("sched_interval", { minutes: r.schedule.minutes || 60 });
  };

  return (
    <div className="relative">
      <button
        onClick={toggle}
        title={t("schedules_title")}
        className="px-2 py-1.5 text-[11px] rounded bg-brain-800 hover:bg-brain-700 text-brain-200 transition-colors"
      >
        ⏱ {t("schedules_button")}
      </button>

      {open && (
        <div className="absolute bottom-full right-0 mb-2 w-96 max-h-80 overflow-y-auto bg-brain-900 border border-brain-700/60 rounded-lg shadow-xl p-2 text-[11px] space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-brain-200 font-medium">{t("schedules_title")}</span>
            <button onClick={() => setOpen(false)} className="text-brain-500 hover:text-white">✕</button>
          </div>
          {!triggersOn && (
            <div className="text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded px-2 py-1">
              {t("triggers_disabled_hint")}
            </div>
          )}
          {loading && <div className="text-brain-400">{t("running")}</div>}
          {rows && rows.length === 0 && !loading && (
            <div className="text-brain-500">{t("no_schedules_hint")}</div>
          )}
          {(rows || []).map((r) => (
            <div key={r.id} className="border-t border-brain-800 pt-1">
              <div className="flex items-center gap-1.5">
                <b className="text-brain-100 flex-1 truncate">{r.name}</b>
                {r.due_now && (
                  <span className="px-1 rounded bg-emerald-500/20 text-emerald-300 text-[9px] uppercase">
                    {t("due_now")}
                  </span>
                )}
                <button onClick={() => openBoard(r.id)} title={t("sched_open")}
                  className="text-brain-400 hover:text-brain-100 px-0.5">✎</button>
                <button onClick={() => toggleAutomation(r)}
                  title={r.enabled === false ? t("sched_resume") : t("sched_pause")}
                  className="text-brain-400 hover:text-brain-100 px-0.5">
                  {r.enabled === false ? "▶" : "⏸"}
                </button>
                <button onClick={() => removeBoard(r.id)} title={t("sched_delete")}
                  className="text-brain-500 hover:text-red-300 px-0.5">✕</button>
              </div>
              {r.summary && (
                <div className="text-brain-400 text-[10px] truncate" title={r.summary}>
                  {r.summary}
                </div>
              )}
              <div className="text-brain-300">{scheduleLabel(r)}</div>
              <div className="text-brain-500">
                {t("last_run", { at: fmt(r.last_triggered_at) })}
                {r.next_estimate ? <> · {t("next_run", { at: fmt(r.next_estimate) })}</> : null}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
