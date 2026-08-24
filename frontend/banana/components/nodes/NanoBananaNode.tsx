"use client";

import { useCallback } from "react";
import { Handle, Position, NodeProps, Node } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { BaseNode } from "./BaseNode";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { NanoBananaNodeData, AspectRatio, Resolution, ModelType, GPTImageSize, GPTImageQuality } from "@/banana/types";
import { nodeErrorText } from "@/banana/nodeError";

const ASPECT_RATIOS: AspectRatio[] = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"];
const RESOLUTIONS: Resolution[] = ["1K", "2K", "4K"];
const GPT_IMAGE_SIZES: GPTImageSize[] = ["auto", "1024x1024", "1024x1536", "1536x1024"];
const GPT_IMAGE_QUALITIES: GPTImageQuality[] = ["low", "medium", "high"];

const MODELS: { value: ModelType; label?: string; labelKey?: string; provider: string }[] = [
  { value: "nano-banana", label: "Nano Banana", provider: "Google" },
  { value: "nano-banana-pro", label: "Nano Banana Pro", provider: "Google" },
  { value: "gpt-image-1.5", label: "GPT Image 1.5", provider: "OpenAI" },
  { value: "gpt-image-1", label: "GPT Image 1", provider: "OpenAI" },
  { value: "gpt-image-1-mini", label: "GPT Image Mini", provider: "OpenAI" },
  { value: "dall-e-3", label: "DALL-E 3", provider: "OpenAI" },
  { value: "seedream-5-pro", label: "Seedream 5 Pro", provider: "ByteDance" },
  { value: "seedream-5-lite", label: "Seedream 5 Lite", provider: "ByteDance" },
  { value: "reve-2.1", label: "Reve 2.1", provider: "Reve" },
  { value: "veo-3.1-fast", labelKey: "model_veo_fast", provider: "Google" },
  { value: "veo-3.1", labelKey: "model_veo", provider: "Google" },
];

type NanoBananaNodeType = Node<NanoBananaNodeData, "nanoBanana">;

export function NanoBananaNode({ id, data, selected }: NodeProps<NanoBananaNodeType>) {
  const t = useTranslations("banana");
  const nodeData = data;
  const updateNodeData = useWorkflowStore((state) => state.updateNodeData);

  const handleAspectRatioChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      updateNodeData(id, { aspectRatio: e.target.value as AspectRatio });
    },
    [id, updateNodeData]
  );

  const handleResolutionChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      updateNodeData(id, { resolution: e.target.value as Resolution });
    },
    [id, updateNodeData]
  );

  const handleModelChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      updateNodeData(id, { model: e.target.value as ModelType });
    },
    [id, updateNodeData]
  );

  const handleGoogleSearchToggle = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      updateNodeData(id, { useGoogleSearch: e.target.checked });
    },
    [id, updateNodeData]
  );

  const handleGptSizeChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      updateNodeData(id, { gptImageSize: e.target.value as GPTImageSize });
    },
    [id, updateNodeData]
  );

  const handleGptQualityChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      updateNodeData(id, { gptImageQuality: e.target.value as GPTImageQuality });
    },
    [id, updateNodeData]
  );

  const handleClearImage = useCallback(() => {
    updateNodeData(id, { outputImage: null, status: "idle", error: null });
  }, [id, updateNodeData]);

  const regenerateNode = useWorkflowStore((state) => state.regenerateNode);
  const isRunning = useWorkflowStore((state) => state.isRunning);

  const handleRegenerate = useCallback(() => {
    regenerateNode(id);
  }, [id, regenerateNode]);

  const isNanoBananaPro = nodeData.model === "nano-banana-pro";
  const isOpenAIModel = nodeData.model === "gpt-image-1.5" || nodeData.model === "gpt-image-1" || nodeData.model === "gpt-image-1-mini" || nodeData.model === "dall-e-3";
  const isGoogleModel = nodeData.model === "nano-banana" || nodeData.model === "nano-banana-pro";
  // Seedream 5 (ByteDance) / Reve 2.1 (Reve AI): текст→картинка + правка/
  // мульти-референс по входным картинкам. Вход НЕ обязателен.
  const isSeedreamModel = nodeData.model === "seedream-5-pro" || nodeData.model === "seedream-5-lite";
  const isReveModel = nodeData.model === "reve-2.1";

  return (
    <BaseNode id={id} title="Generate" selected={selected} hasError={nodeData.status === "error"}>
      <Handle type="target" position={Position.Left} id="image" style={{ top: "35%" }} data-handletype="image" isConnectable={true} />
      <Handle type="target" position={Position.Left} id="text" style={{ top: "65%" }} data-handletype="text" />
      <Handle type="source" position={Position.Right} id="image" data-handletype="image" />

      <div className="flex-1 flex flex-col min-h-0 gap-2">
        {nodeData.outputVideo ? (
          <div className="relative w-full flex-1 min-h-0">
            <video src={nodeData.outputVideo} controls loop
              className="w-full h-full object-contain rounded" />
          </div>
        ) : nodeData.outputImage ? (
          <div className="relative w-full flex-1 min-h-0">
            <img src={nodeData.outputImage} alt="Generated" className="w-full h-full object-contain rounded" />
            {nodeData.status === "loading" && (
              <div className="absolute inset-0 bg-brain-950/60 rounded flex items-center justify-center">
                <svg className="w-6 h-6 animate-spin text-brain-100" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                </svg>
              </div>
            )}
            <div className="absolute top-1 right-1 flex gap-1">
              <button
                onClick={handleRegenerate}
                disabled={isRunning}
                className="w-5 h-5 bg-brain-950/70 hover:bg-brain-600/80 disabled:opacity-50 disabled:cursor-not-allowed rounded flex items-center justify-center text-brain-200 hover:text-brain-50 transition-colors"
                title="Regenerate"
              >
                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </button>
              <button
                onClick={handleClearImage}
                className="w-5 h-5 bg-brain-950/70 hover:bg-red-600/80 rounded flex items-center justify-center text-brain-200 hover:text-brain-50 transition-colors"
                title="Clear image"
              >
                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          </div>
        ) : (
          <div className="w-full flex-1 min-h-[112px] border border-dashed border-brain-700/70 rounded flex flex-col items-center justify-center">
            {nodeData.status === "loading" ? (
              <svg className="w-4 h-4 animate-spin text-brain-200/70" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
              </svg>
            ) : nodeData.status === "error" ? (
              <span className="text-[10px] text-red-400 text-center px-2">{nodeErrorText(nodeData.error, t)}</span>
            ) : (
              <span className="text-brain-200/60 text-[10px]">{t("run_to_generate")}</span>
            )}
          </div>
        )}

        <select
          value={nodeData.model}
          onChange={handleModelChange}
          className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
        >
          {MODELS.map((m) => (
            <option key={m.value} value={m.value}>
              {m.labelKey ? t(m.labelKey) : m.label} ({m.provider})
            </option>
          ))}
        </select>

        {/* Google + Seedream + Reve: Aspect Ratio (Resolution — только Nano Banana Pro) */}
        {(isGoogleModel || isSeedreamModel || isReveModel) && (
          <div className="flex gap-1.5 shrink-0">
            <select
              value={nodeData.aspectRatio}
              onChange={handleAspectRatioChange}
              className="flex-1 text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100"
            >
              {ASPECT_RATIOS.map((ratio) => (
                <option key={ratio} value={ratio}>
                  {ratio}
                </option>
              ))}
            </select>
            {isNanoBananaPro && (
              <select
                value={nodeData.resolution}
                onChange={handleResolutionChange}
                className="w-12 text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100"
              >
                {RESOLUTIONS.map((res) => (
                  <option key={res} value={res}>
                    {res}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {/* OpenAI Models: Size + Quality */}
        {isOpenAIModel && (
          <div className="flex gap-1.5 shrink-0">
            <select
              value={nodeData.gptImageSize || "auto"}
              onChange={handleGptSizeChange}
              className="flex-1 text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100"
            >
              {GPT_IMAGE_SIZES.map((size) => (
                <option key={size} value={size}>
                  {size === "auto" ? "Auto" : size}
                </option>
              ))}
            </select>
            <select
              value={nodeData.gptImageQuality || "medium"}
              onChange={handleGptQualityChange}
              className="w-16 text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100"
            >
              {GPT_IMAGE_QUALITIES.map((q) => (
                <option key={q} value={q}>
                  {q}
                </option>
              ))}
            </select>
          </div>
        )}

        {isNanoBananaPro && (
          <label className="flex items-center gap-1.5 text-[10px] text-brain-100 shrink-0 cursor-pointer">
            <input
              type="checkbox"
              checked={nodeData.useGoogleSearch}
              onChange={handleGoogleSearchToggle}
              className="w-3 h-3 rounded border-brain-700/70 bg-brain-950/40 text-brain-500 focus:ring-1 focus:ring-brain-500 focus:ring-offset-0"
            />
            <span>Google Search</span>
          </label>
        )}
      </div>
    </BaseNode>
  );
}


