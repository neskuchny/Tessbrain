"use client";

import { useCallback, useMemo } from "react";
import { Handle, Position, NodeProps, Node, useEdges } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { BaseNode } from "./BaseNode";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { TextCombinerNodeData } from "@/banana/types";

type TextCombinerNodeType = Node<TextCombinerNodeData, "textCombiner">;

export function TextCombinerNode({ id, data, selected }: NodeProps<TextCombinerNodeType>) {
  const t = useTranslations("banana");
  const nodeData = data;
  const updateNodeData = useWorkflowStore((state) => state.updateNodeData);
  const edges = useEdges();

  // Extract placeholder names from template
  const placeholders = useMemo(() => {
    const regex = /\{\{(\w+)\}\}/g;
    const matches: string[] = [];
    let match;
    while ((match = regex.exec(nodeData.template)) !== null) {
      if (!matches.includes(match[1])) {
        matches.push(match[1]);
      }
    }
    return matches;
  }, [nodeData.template]);

  // Check which inputs are connected
  const connectedInputs = useMemo(() => {
    const connected: Record<string, boolean> = {};
    placeholders.forEach((p) => {
      connected[p] = edges.some((e) => e.target === id && e.targetHandle === p);
    });
    return connected;
  }, [edges, id, placeholders]);

  const handleTemplateChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      updateNodeData(id, { template: e.target.value });
    },
    [id, updateNodeData]
  );

  // Preview the output with current inputs. РАЗЛИЧАЕМ два состояния:
  // «нет связи» (соедини вход) и «связь есть, значение придёт при запуске».
  // Раньше оба показывались как «не подключено» — вход БЫЛ подключён, а
  // текст врал, что нет (жалоба про шаблон презентации).
  const preview = useMemo(() => {
    let result = nodeData.template;
    placeholders.forEach((p) => {
      const value = nodeData.inputs[p];
      const placeholder = `{{${p}}}`;
      const label = value
        ? value
        : connectedInputs[p]
          ? `[${p}: ${t("will_fill_on_run")}]`
          : `[${p}: ${t("not_connected_placeholder")}]`;
      result = result.replace(
        new RegExp(placeholder.replace(/[{}]/g, "\\$&"), "g"), label);
    });
    return result;
  }, [nodeData.template, nodeData.inputs, placeholders, connectedInputs, t]);

  return (
    <BaseNode id={id} title={t("text_combiner_title")} selected={selected}>
      {/* Dynamic input handles based on template placeholders */}
      {placeholders.map((placeholder, index) => (
        <Handle
          key={placeholder}
          type="target"
          position={Position.Left}
          id={placeholder}
          style={{ top: `${30 + index * 24}px` }}
          data-handletype="text"
          title={placeholder}
        />
      ))}

      {/* Output handle */}
      <Handle type="source" position={Position.Right} id="text" data-handletype="text" />

      <div className="flex-1 flex flex-col min-h-0 gap-2">
        {/* Input labels */}
        <div className="flex flex-col gap-1">
          {placeholders.map((placeholder, index) => (
            <div
              key={placeholder}
              className="flex items-center gap-1.5 text-[10px]"
              style={{ marginTop: index === 0 ? 0 : 0 }}
            >
              <span
                className={`w-2 h-2 rounded-full ${
                  connectedInputs[placeholder] ? "bg-green-500" : "bg-brain-600"
                }`}
              />
              <span className="text-brain-200/80 font-mono">{`{{${placeholder}}}`}</span>
              {nodeData.inputs[placeholder] && (
                <span className="text-brain-400 truncate max-w-[120px]" title={nodeData.inputs[placeholder] || ""}>
                  {nodeData.inputs[placeholder]?.substring(0, 30)}...
                </span>
              )}
            </div>
          ))}
        </div>

        {/* Template editor */}
        <div className="flex flex-col gap-1">
          <label className="text-[9px] text-brain-200/60 uppercase tracking-wider">{t("template_label")}</label>
          <textarea
            value={nodeData.template}
            onChange={handleTemplateChange}
            placeholder={t("template_placeholder")}
            className="nodrag nopan nowheel w-full min-h-[60px] p-2 text-[10px] leading-relaxed text-brain-50 border border-brain-700/70 rounded bg-brain-950/40 resize-none focus:outline-none focus:ring-1 focus:ring-brain-500 focus:border-brain-500 placeholder:text-brain-200/40 font-mono"
          />
        </div>

        {/* Preview */}
        <div className="flex flex-col gap-1 flex-1 min-h-0">
          <label className="text-[9px] text-brain-200/60 uppercase tracking-wider">{t("preview_label")}</label>
          <div className="flex-1 min-h-[40px] p-2 text-[10px] leading-relaxed text-brain-100/70 border border-dashed border-brain-700/50 rounded bg-brain-950/20 overflow-auto whitespace-pre-wrap">
            {preview}
          </div>
        </div>

        {/* Output display */}
        {nodeData.outputText && (
          <div className="flex flex-col gap-1">
            <label className="text-[9px] text-brain-200/60 uppercase tracking-wider flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-green-500" />
              {t('result_label')}
            </label>
            <div className="p-2 text-[10px] leading-relaxed text-brain-50 border border-green-700/50 rounded bg-green-950/20 max-h-[60px] overflow-auto whitespace-pre-wrap">
              {nodeData.outputText}
            </div>
          </div>
        )}
      </div>
    </BaseNode>
  );
}

