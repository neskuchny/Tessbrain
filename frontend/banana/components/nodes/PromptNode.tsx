"use client";

import { useCallback } from "react";
import { Handle, Position, NodeProps, Node } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { BaseNode } from "./BaseNode";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { PromptNodeData } from "@/banana/types";

type PromptNodeType = Node<PromptNodeData, "prompt">;

export function PromptNode({ id, data, selected }: NodeProps<PromptNodeType>) {
  const t = useTranslations("banana");
  const nodeData = data;
  const updateNodeData = useWorkflowStore((state) => state.updateNodeData);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      updateNodeData(id, { prompt: e.target.value });
    },
    [id, updateNodeData]
  );

  return (
    <BaseNode id={id} title="Prompt" selected={selected}>
      <textarea
        value={nodeData.prompt}
        onChange={handleChange}
        placeholder={t("node_prompt_placeholder")}
        className="nodrag nopan nowheel w-full flex-1 min-h-[70px] p-2 text-xs leading-relaxed text-brain-50 border border-brain-700/70 rounded bg-brain-950/40 resize-none focus:outline-none focus:ring-1 focus:ring-brain-500 focus:border-brain-500 placeholder:text-brain-200/40"
      />

      {/* Вход: данные из процесса/цепочки дописываются к промпту (бэкенд
          подставляет {{input}} или добавляет блоком). Без этого хэндла ребро
          «Структура встречи → Промпт» не рисовалось — половины схемы не связывались. */}
      <Handle type="target" position={Position.Left} id="text" data-handletype="text" />
      <Handle type="source" position={Position.Right} id="text" data-handletype="text" />
    </BaseNode>
  );
}


