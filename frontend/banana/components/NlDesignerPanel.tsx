"use client";

/**
 * NlDesignerPanel — процесс из естественного языка (F).
 * Пользователь описывает процесс словами → premium-модель строит схему →
 * панель показывает ПРЕВЬЮ с пояснениями (что за схема, из каких блоков,
 * что каждый делает, какие предупреждения), и по кнопке кладёт готовый
 * workflow в редактор. Ничего не запускает само — пользователь смотрит,
 * правит и осознанно жмёт «Запустить».
 */
import { useRef, useState, useEffect } from "react";
import { useTranslations } from "next-intl";
import { Sparkles, X, Loader2, AlertTriangle, ArrowRight, Save, Pencil } from "lucide-react";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { designBoard, saveBoard, DesignResult } from "@/banana/serverBoards";
import { useToast } from "@/banana/components/Toast";

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

export function NlDesignerPanel({ isOpen, onClose }: Props) {
  const t = useTranslations("banana");
  // Примеры — это текст ЗАПРОСА к планировщику, поэтому они локализуются
  // (планировщик понимает и английский).
  const EXAMPLES = [
    t("nl_example_digest"),
    t("nl_example_meeting_tasks"),
    t("nl_example_plan_fact"),
  ];
  const { loadWorkflow, nodes, edges } = useWorkflowStore();
  const showToast = useToast((s) => s.show);
  const panelRef = useRef<HTMLDivElement>(null);

  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DesignResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Правка словами (F.2): если на доске уже есть исполняемые блоки (не только
  // note), предлагаем менять текущую схему, а не строить с нуля.
  const hasBoard = nodes.filter((n: any) => n.type !== "note").length > 0;
  const [editMode, setEditMode] = useState(false);
  const isEdit = editMode && hasBoard;

  useEffect(() => {
    const onOutside = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) onClose();
    };
    if (isOpen) document.addEventListener("mousedown", onOutside);
    return () => document.removeEventListener("mousedown", onOutside);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const minLen = isEdit ? 4 : 10;
  const design = async () => {
    if (text.trim().length < minLen || busy) return;
    setBusy(true); setError(null); setResult(null);
    try {
      const current = isEdit ? { nodes, edges } : undefined;
      const res = await designBoard(text.trim(), current);
      if (!res.success) { setError(res.error || t("nl_error")); return; }
      setResult(res);
    } catch (e) {
      setError(String(e));
    } finally { setBusy(false); }
  };

  const addToBoard = () => {
    if (!result?.workflow) return;
    loadWorkflow(result.workflow);
    showToast(t("nl_added"), "success");
    onClose();
  };

  const saveAsBoard = async () => {
    if (!result?.workflow || saving) return;
    setSaving(true);
    try {
      const meta = await saveBoard(result.title || t("nl_default_board_name"), result.workflow,
        undefined, "process");
      showToast(meta ? t("nl_saved") : t("nl_save_error"),
        meta ? "success" : "error");
      if (meta) { loadWorkflow(result.workflow); onClose(); }
    } finally { setSaving(false); }
  };

  return (
    <div
      ref={panelRef}
      /* Открываемся ВВЕРХ от нижней панели (bottom-full) и прижимаемся к
         правому краю кнопки (right-0) — как Templates/Generate/Run. Раньше
         было top-full/left-0: панель уезжала ВНИЗ за нижнюю панель, за край
         экрана, наезжая на миникарту — «появляется непонятно где». */
      className="absolute bottom-full mb-2 right-0 z-50 w-[420px] max-h-[70vh] overflow-y-auto rounded-xl border border-brain-700/50 bg-brain-900 shadow-2xl p-4 space-y-3"
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-brain-50">
          <Sparkles className="w-4 h-4 text-purple-400" /> {t("nl_title")}
        </div>
        <button onClick={onClose} className="p-1 rounded text-brain-400 hover:bg-brain-800">
          <X className="w-4 h-4" />
        </button>
      </div>
      <p className="text-[11px] text-brain-300/80">{t("nl_hint")}</p>

      {/* F.2: переключатель создать/править — только если на доске уже есть схема */}
      {hasBoard && (
        <div className="flex gap-1 p-0.5 rounded-lg bg-brain-950 border border-brain-700/40 text-[11px]">
          <button onClick={() => { setEditMode(false); setResult(null); }}
            className={`flex-1 py-1 rounded-md transition-colors ${!editMode ? "bg-brain-700 text-brain-50" : "text-brain-300"}`}>
            {t("nl_mode_new")}
          </button>
          <button onClick={() => { setEditMode(true); setResult(null); }}
            className={`flex-1 py-1 rounded-md flex items-center justify-center gap-1 transition-colors ${editMode ? "bg-purple-700 text-white" : "text-brain-300"}`}>
            <Pencil className="w-3 h-3" /> {t("nl_mode_edit")}
          </button>
        </div>
      )}

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={isEdit ? t("nl_edit_placeholder") : t("nl_placeholder")}
        rows={3}
        className="w-full px-3 py-2 rounded-lg bg-brain-950 border border-brain-700/50 text-sm text-brain-50 placeholder:text-brain-500 resize-none"
      />

      {!result && !isEdit && (
        <div className="flex flex-wrap gap-1.5">
          {EXAMPLES.map((ex, i) => (
            <button key={i} onClick={() => setText(ex)}
              className="text-[10.5px] px-2 py-1 rounded-md border border-brain-700/40 text-brain-300 hover:border-brain-500 text-left">
              {ex}
            </button>
          ))}
        </div>
      )}

      <button
        onClick={design}
        disabled={text.trim().length < minLen || busy}
        className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm disabled:opacity-50"
      >
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
        {busy ? t("nl_designing") : isEdit ? t("nl_apply_edit") : t("nl_design")}
      </button>

      {error && (
        <p className="text-xs text-red-400 flex items-start gap-1.5">
          <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />{error}
        </p>
      )}

      {result?.success && (
        <div className="space-y-2 border-t border-brain-700/40 pt-3">
          <p className="text-sm font-medium text-brain-50">{result.title}</p>
          {result.summary && (
            <p className="text-[11.5px] text-brain-300 leading-snug">{result.summary}</p>
          )}
          {/* блоки схемы с пояснением что/зачем — цепочка */}
          <div className="space-y-1.5">
            {(result.blocks || []).map((b, i) => (
              <div key={b.id} className="flex items-start gap-2 text-[11px]">
                <span className="shrink-0 w-5 h-5 rounded-full bg-brain-800 text-brain-300 flex items-center justify-center text-[10px] font-medium">{i + 1}</span>
                <div>
                  <span className="text-brain-100 font-medium">{b.label}</span>
                  <span className="text-brain-500 ml-1.5">· {b.type}</span>
                  {b.comment && <p className="text-brain-400 leading-snug">{b.comment}</p>}
                </div>
              </div>
            ))}
          </div>
          {(result.warnings?.length || 0) > 0 && (
            <div className="rounded-md bg-amber-500/10 border border-amber-500/30 p-2 space-y-0.5">
              <p className="text-[10.5px] text-amber-300 font-medium flex items-center gap-1">
                <AlertTriangle className="w-3 h-3" /> {t("nl_warnings")}
              </p>
              {result.warnings!.map((w, i) => (
                <p key={i} className="text-[10.5px] text-amber-200/90">· {w}</p>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <button
              onClick={addToBoard}
              className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm"
            >
              {isEdit ? t("nl_apply") : t("nl_add")} <ArrowRight className="w-4 h-4" />
            </button>
            {!isEdit && (
              <button
                onClick={saveAsBoard} disabled={saving}
                title={t("nl_save")}
                className="flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg border border-brain-600/50 text-brain-200 hover:bg-brain-800 text-sm disabled:opacity-50"
              >
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                {t("nl_save")}
              </button>
            )}
          </div>
          <p className="text-[10px] text-brain-500 text-center">
            {isEdit ? t("nl_apply_hint") : t("nl_add_hint")}
          </p>
        </div>
      )}
    </div>
  );
}
