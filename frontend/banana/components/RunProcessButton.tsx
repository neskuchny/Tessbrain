"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { runBoard, saveBoard } from "@/banana/serverBoards";

/**
 * P3: запустить процесс-доску на сервере. Перед прогоном авто-сохраняем граф
 * (создаём/обновляем доску kind=process) и зовём POST /boards/{id}/run.
 * Результат (порядок в dry-run или лог узлов) показываем в поповере.
 */
export function RunProcessButton() {
  const t = useTranslations("banana_run");
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const edgeStyle = useWorkflowStore((s) => s.edgeStyle);
  const boardId = useWorkflowStore((s) => s.boardId);
  const setBoardId = useWorkflowStore((s) => s.setBoardId);
  const boardName = useWorkflowStore((s) => s.boardName);
  const updateNodeData = useWorkflowStore((s) => s.updateNodeData);

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<any | null>(null);

  // Результат серверного прогона → в Output-узлы холста: берём выход узла,
  // с которого в output входит ребро (иначе результат жил только в поповере).
  const fillOutputs = (res: any) => {
    const log: any[] = Array.isArray(res?.log) ? res.log : [];
    if (!log.length) return;
    const byNode: Record<string, any> = {};
    for (const l of log) byNode[String(l.node)] = l;
    for (const n of nodes) {
      if (String(n.type) !== "output") continue;
      const inc = edges.find((e) => String(e.target) === String(n.id));
      const src = inc ? byNode[String(inc.source)] : null;
      if (!src || src.skipped) continue;
      const out = src.output;
      const text = typeof out === "string"
        ? out
        : String(out?.text || out?.message || JSON.stringify(out ?? ""));
      if (text) updateNodeData(n.id, { text: text.slice(0, 4000) } as any);
    }
  };

  const run = async () => {
    setBusy(true);
    setResult(null);
    // Имя доски: из шаблона/сохранения. Раньше тут был хардкод «Процесс» —
    // ДЕСЯТКИ досок в списках назывались одинаково, а Run ещё и
    // перезаписывал осмысленные имена при каждом запуске.
    const name = (boardName || "").trim() || t("default_board_name");
    const graph = { version: 1 as const, name, nodes, edges, edgeStyle };
    let id = boardId;
    const saved = await saveBoard(name, graph, id || undefined, "process");
    if (saved?.id) {
      id = saved.id;
      setBoardId(saved.id);
    }
    if (!id) {
      setResult({ status: "error", message: t("save_board_error") });
      setBusy(false);
      return;
    }
    const res = await runBoard(id);
    setResult(res);
    fillOutputs(res);
    setBusy(false);
  };

  return (
    <div className="relative">
      <button
        onClick={run}
        disabled={busy}
        className="px-2.5 py-1.5 text-[11px] font-medium rounded bg-emerald-600 hover:bg-emerald-500 text-white disabled:opacity-50 transition-colors"
      >
        ▶ {busy ? t("running") : t("run_process")}
      </button>

      {result && (
        <div className="absolute bottom-full right-0 mb-2 w-96 max-h-80 overflow-y-auto bg-brain-900 border border-brain-700/60 rounded-lg shadow-xl p-2 text-[11px] space-y-1.5">
          <div className="flex items-center justify-between">
            <span className={`font-medium ${result.status === "warning" || result.status === "partial" ? "text-amber-300" : "text-brain-200"}`}>
              {t("result_heading", { status: result.status })}
            </span>
            <button onClick={() => setResult(null)} className="text-brain-500 hover:text-white">✕</button>
          </div>
          {/* Частичный итог: бюджет прогона исчерпан или часть узлов упала.
              TODO-i18n: ключа в messages/*.json пока нет — русский литерал. */}
          {result.status === "partial" && (
            <div className="rounded bg-amber-500/10 border border-amber-500/30 p-1.5 text-amber-300">
              ⚠️ Выполнено частично
              {typeof result.errors_count === "number" && result.errors_count > 0
                ? ` — узлов с ошибкой: ${result.errors_count}` : ""}
            </div>
          )}
          {result.message && <div className="text-brain-400">{result.message}</div>}
          {/* Предупреждения о несоединённых блоках — иначе ✓ по каждому узлу
              вводит в заблуждение, хотя данные между блоками не шли. */}
          {Array.isArray(result.warnings) && result.warnings.length > 0 && (
            <div className="rounded bg-amber-500/10 border border-amber-500/30 p-1.5 space-y-1">
              {result.warnings.map((w: string, i: number) => (
                <div key={i} className="text-amber-300 text-[10.5px] leading-snug">⚠️ {w}</div>
              ))}
            </div>
          )}
          {result.status === "dry_run" && (
            <div className="text-amber-300">{t("order_label", { order: (result.order || []).join(" → ") })}</div>
          )}
          {(result.log || [])
            .filter((l: any) => l.type !== "note" && l.type !== "output")
            .map((l: any, i: number) => {
              const STEP_KEYS: Record<string, string> = {
                trigger: "step_trigger", ask_brain: "step_ask_brain",
                report: "step_report", notify: "step_notify",
                task: "step_task", action: "step_action",
                generate: "step_generate", condition: "step_condition",
              };
              const label = STEP_KEYS[String(l.type)]
                ? t(STEP_KEYS[String(l.type)]) : String(l.type);
              // Запись-остановка бюджета прогона: не «узел», а причина стопа.
              if (String(l.node) === "__budget__") {
                return (
                  <div key={i} className="border-t border-brain-800 pt-1 text-amber-300">
                    ⏱ {String(l.message || "")}
                  </div>
                );
              }
              if (l.skipped) {
                return (
                  <div key={i} className="border-t border-brain-800 pt-1 text-brain-500">
                    <b>{label}</b> · {t("node_skipped")}
                  </div>
                );
              }
              const out = l.output;
              const err = typeof out === "object" && out
                ? (out.error || (out.ok === false ? String(out.text || out.message || "") : ""))
                : "";
              const text = typeof out === "string"
                ? out
                : String(out?.text ?? out?.message ?? "");
              const isIntegrationErr = /no integration found/i.test(String(err));
              return (
                <div key={i} className="border-t border-brain-800 pt-1">
                  <div className={err ? "text-red-300" : "text-brain-100"}>
                    <b>{label}</b> {err ? "✗" : "✓"}
                  </div>
                  {err ? (
                    <div className="text-red-300/90">
                      {isIntegrationErr
                        ? t("integration_missing_hint", {
                            tool: String(out?.tool || "").trim() || "?",
                          })
                        : err}
                    </div>
                  ) : (
                    text && (
                      <div className="text-brain-300 whitespace-pre-wrap break-words max-h-24 overflow-y-auto">
                        {text.slice(0, 600)}
                      </div>
                    )
                  )}
                </div>
              );
            })}
        </div>
      )}
    </div>
  );
}
