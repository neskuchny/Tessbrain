"use client";

import { ReactNode, useCallback, useState } from "react";
import { NodeResizer, OnResize, useReactFlow } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { NODE_HELP } from "@/banana/nodeHelp";

interface BaseNodeProps {
  id: string;
  title: string;
  children: ReactNode;
  selected?: boolean;
  isExecuting?: boolean;
  hasError?: boolean;
  className?: string;
  minWidth?: number;
  minHeight?: number;
}

// Категория узла — чтобы на смешанной схеме с одного взгляда было видно,
// где ПРОЦЕСС (управление: триггеры, задачи, доставка), а где КРЕАТИВ
// (создание контента). Процесс — голубая кромка, креатив — фиолетовая.
const NODE_CATEGORY: Record<string, "process" | "creative"> = {
  trigger: "process", ask_brain: "process", report: "process",
  notify: "process", task: "process", action: "process", condition: "process",
  wait_reply: "process", meeting_data: "process", meeting_share: "process",
  document: "process", crm_data: "process", web_search: "process",
  report_xlsx: "process", translate: "process", doc_edit: "process",
  crm_write: "process",
  prompt: "creative", nanoBanana: "creative", llmGenerate: "creative",
  imageInput: "creative", textCombiner: "creative", generate: "creative",
  infographic: "creative", audio: "creative",
};
// Подпись категории — ключ перевода: константа вне компонента, хуки здесь
// недоступны, t() применяется при рендере бейджа.
const CATEGORY_STYLE: Record<"process" | "creative", { edge: string; badge: string; labelKey: string }> = {
  process: {
    edge: "border-l-[3px] border-l-sky-500/70",
    badge: "bg-sky-500/15 text-sky-300",
    labelKey: "category_process",
  },
  creative: {
    edge: "border-l-[3px] border-l-fuchsia-500/70",
    badge: "bg-fuchsia-500/15 text-fuchsia-300",
    labelKey: "category_creative",
  },
};

export function BaseNode({
  id,
  title,
  children,
  selected = false,
  isExecuting = false,
  hasError = false,
  className = "",
  minWidth = 180,
  minHeight = 100,
}: BaseNodeProps) {
  const t = useTranslations("banana_node_help");
  const currentNodeId = useWorkflowStore((state) => state.currentNodeId);
  const removeNode = useWorkflowStore((state) => state.removeNode);
  // Осмысленная подпись узла из данных (напр. «Слайд 1 · Титул + стиль» из
  // шаблона). Раньше нигде не рендерилась — лежала в JSON впустую. Показываем
  // второй строкой под техническим title, чтобы схема читалась без клика.
  const nodeLabel = useWorkflowStore(
    (state) => state.nodes.find((n) => n.id === id)?.data?.label as string | undefined
  );
  // Тип узла — для справки «?» (что делает блок, что подключать, пример)
  const nodeType = useWorkflowStore(
    (state) => state.nodes.find((n) => n.id === id)?.type as string | undefined
  );
  const isCurrentlyExecuting = currentNodeId === id;
  const { getNodes, setNodes } = useReactFlow();
  const [showHelp, setShowHelp] = useState(false);
  const help = nodeType ? NODE_HELP[nodeType] : undefined;
  const category = nodeType ? NODE_CATEGORY[nodeType] : undefined;
  const catStyle = category ? CATEGORY_STYLE[category] : undefined;

  // Synchronize resize across all selected nodes
  const handleResize: OnResize = useCallback(
    (_event, params) => {
      const allNodes = getNodes();
      const selectedNodes = allNodes.filter((node) => node.selected && node.id !== id);

      if (selectedNodes.length > 0) {
        setNodes((nodes) =>
          nodes.map((node) => {
            if (node.selected && node.id !== id) {
              return {
                ...node,
                style: {
                  ...node.style,
                  width: params.width,
                  height: params.height,
                },
              };
            }
            return node;
          })
        );
      }
    },
    [id, getNodes, setNodes]
  );

  return (
    <>
      {/* Рамка и уголки видимы у выделенного узла — иначе тянуть не за что
          (раньше были прозрачные: пользователь не понимал, где растягивать). */}
      <NodeResizer
        isVisible={selected}
        minWidth={minWidth}
        minHeight={minHeight}
        lineClassName="!border-brain-300/50"
        handleClassName="!w-2.5 !h-2.5 !bg-brain-200 !border !border-brain-500 !rounded-sm"
        onResize={handleResize}
      />
      <div
        className={`
          bg-brain-900 rounded-md shadow-lg border h-full w-full
          ${isCurrentlyExecuting || isExecuting ? "border-brain-500 ring-1 ring-brain-500/20" : "border-brain-700/60"}
          ${hasError ? "border-red-500" : ""}
          ${selected ? "border-brain-300 ring-1 ring-brain-300/30" : ""}
          ${catStyle ? catStyle.edge : ""}
          ${className}
        `}
      >
        <div className="px-3 pt-2 pb-1 flex items-center gap-1.5">
          <span className="text-xs font-semibold uppercase tracking-wide text-brain-200/80 flex-1 min-w-0 truncate" title={nodeLabel || title}>
            {nodeLabel || title}
          </span>
          {catStyle && (
            <span className={`text-[8px] px-1 py-0.5 rounded uppercase font-semibold flex-none ${catStyle.badge}`}>
              {t(catStyle.labelKey)}
            </span>
          )}
          {help && (
            <button
              onClick={(e) => { e.stopPropagation(); setShowHelp((v) => !v); }}
              title={t("help_button_title")}
              className="nodrag nopan w-4 h-4 flex items-center justify-center rounded-full text-[10px] leading-none text-brain-300 hover:text-white border border-brain-600/50 hover:border-brain-400/70 transition-colors flex-none"
            >
              ?
            </button>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); removeNode(id); }}
            title={t("delete_button_title")}
            className="nodrag nopan w-4 h-4 flex items-center justify-center rounded text-[11px] leading-none text-brain-400 hover:text-red-300 transition-colors flex-none"
          >
            ✕
          </button>
        </div>
        {showHelp && help && (
          <div
            className="nodrag nopan nowheel absolute left-1 right-1 top-8 z-20 rounded-md border border-brain-500/40 bg-brain-950/95 backdrop-blur-sm p-2.5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="text-[11px] text-brain-50 leading-snug">{t(help.whatKey)}</div>
            <div className="text-[10.5px] text-brain-300 leading-snug mt-1.5">
              <span className="text-brain-400">{t("connections_label")}</span> {t(help.connectKey)}
            </div>
            <div className="text-[10.5px] text-emerald-300/80 leading-snug mt-1">
              <span className="text-brain-400">{t("example_label")}</span> {t(help.exampleKey)}
            </div>
            <button
              onClick={() => setShowHelp(false)}
              className="mt-1.5 text-[10px] text-brain-400 hover:text-white"
            >
              {t("close")}
            </button>
          </div>
        )}
        <div className="px-3 pb-4 h-[calc(100%-28px)] overflow-hidden flex flex-col">{children}</div>
      </div>
    </>
  );
}
