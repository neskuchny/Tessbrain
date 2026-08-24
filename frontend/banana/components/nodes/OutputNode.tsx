"use client";

import { useCallback, useState } from "react";
import { Handle, Position, NodeProps, Node } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { BaseNode } from "./BaseNode";
import { OutputNodeData } from "@/banana/types";

type OutputNodeType = Node<OutputNodeData, "output">;

export function OutputNode({ data, selected, id }: NodeProps<OutputNodeType>) {
  const t = useTranslations("banana");
  const nodeData = data;
  const [showLightbox, setShowLightbox] = useState(false);

  const handleDownload = useCallback(() => {
    if (!nodeData.image) return;
    const link = document.createElement("a");
    link.href = nodeData.image;
    link.download = `generated-${Date.now()}.png`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }, [nodeData.image]);

  return (
    <>
      <BaseNode id={id} title="Output" selected={selected} className="min-w-[200px]">
        <Handle type="target" position={Position.Left} id="image" data-handletype="image" />

        {nodeData.image ? (
          <div className="flex-1 flex flex-col min-h-0 gap-2">
            <div className="relative cursor-pointer group flex-1 min-h-0" onClick={() => setShowLightbox(true)}>
              <img src={nodeData.image} alt="Output" className="w-full h-full object-contain rounded" />
              <div className="absolute inset-0 bg-black/0 group-hover:bg-black/10 transition-colors flex items-center justify-center rounded">
                <span className="text-[10px] font-medium text-white opacity-0 group-hover:opacity-100 transition-opacity bg-black/50 px-2 py-1 rounded">
                  {t("node_open")}
                </span>
              </div>
            </div>
            <button onClick={handleDownload} className="w-full py-1.5 bg-brain-100 hover:bg-brain-200 text-brain-950 text-[10px] font-medium rounded transition-colors shrink-0">
              {t("node_download")}
            </button>
          </div>
        ) : nodeData.text ? (
          // flex-1 без max-h: текст заполняет узел и растёт при его расширении
          // (раньше max-h-56 обрезал высоту → рамку тянешь, а текст не растёт).
          <div className="w-full flex-1 min-h-[80px] overflow-y-auto border border-brain-700/70 rounded bg-brain-950/40 p-2">
            <pre className="whitespace-pre-wrap break-words text-[10px] text-brain-100 font-sans">{nodeData.text}</pre>
          </div>
        ) : (
          <div className="w-full flex-1 min-h-[144px] border border-dashed border-brain-700/70 rounded flex flex-col items-center justify-center gap-1 p-2 text-center">
            <span className="text-brain-200/60 text-[10px]">{t("waiting_for_result")}</span>
            <span className="text-brain-200/40 text-[9px] leading-snug">{t("output_hint")}</span>
          </div>
        )}
      </BaseNode>

      {showLightbox && nodeData.image && (
        <div className="fixed inset-0 bg-black/90 z-[100] flex items-center justify-center p-8" onClick={() => setShowLightbox(false)}>
          <div className="relative max-w-full max-h-full">
            <img src={nodeData.image} alt="Output full size" className="max-w-full max-h-[90vh] object-contain rounded" />
            <button
              onClick={() => setShowLightbox(false)}
              className="absolute top-4 right-4 w-8 h-8 bg-white/10 hover:bg-white/20 rounded text-white text-sm transition-colors flex items-center justify-center"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      )}
    </>
  );
}


