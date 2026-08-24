"use client";

import { useCallback } from "react";
import { NodeProps, Node, NodeResizer } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { X } from "lucide-react";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { NoteNodeData } from "@/banana/types";

type NoteNodeType = Node<NoteNodeData, "note">;

/**
 * NoteNode — поясняющий стикер на холсте (без портов, не исполняется).
 * Объясняет пользователю схему: что делает блок/ветка, зачем, как пользоваться.
 * Визуально отличается от рабочих узлов (жёлтый стикер), чтобы читалось как
 * подпись, а не как исполняемый шаг.
 */
export function NoteNode({ id, data, selected }: NodeProps<NoteNodeType>) {
  const t = useTranslations("banana");
  const updateNodeData = useWorkflowStore((state) => state.updateNodeData);
  const removeNode = useWorkflowStore((state) => state.removeNode);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      updateNodeData(id, { text: e.target.value });
    },
    [id, updateNodeData]
  );

  return (
    <>
      <NodeResizer minWidth={160} minHeight={80} isVisible={selected} lineClassName="border-amber-400/40" handleClassName="bg-amber-400/70" />
      <div
        className={`h-full w-full rounded-md border shadow-md flex flex-col overflow-hidden ${
          selected ? "border-amber-400/70 ring-1 ring-amber-400/30" : "border-amber-500/40"
        }`}
        style={{ backgroundColor: "rgba(245, 204, 82, 0.10)" }}
      >
        <div className="px-2.5 pt-1.5 pb-1 flex items-center gap-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-amber-300/80 flex-1">
            {t("note_title")}
          </span>
          <button
            onClick={(e) => { e.stopPropagation(); removeNode(id); }}
            title={t("note_delete")}
            className="nodrag nopan w-4 h-4 flex items-center justify-center rounded text-amber-300/70 hover:text-white hover:bg-amber-500/20"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
        <textarea
          value={data.text || ""}
          onChange={handleChange}
          placeholder={t("note_placeholder")}
          className="nodrag nopan nowheel w-full flex-1 px-2.5 pb-2 text-[12px] leading-relaxed text-amber-50/90 bg-transparent resize-none focus:outline-none placeholder:text-amber-200/40"
        />
      </div>
    </>
  );
}
