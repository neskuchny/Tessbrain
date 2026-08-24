"use client";

import { useCallback } from "react";
import { Handle, Position, NodeProps, Node } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { BaseNode } from "./BaseNode";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { LLMGenerateNodeData, LLMProvider, LLMModelType } from "@/banana/types";
import { nodeErrorText } from "@/banana/nodeError";

const PROVIDERS: { value: LLMProvider; label: string }[] = [
  { value: "google", label: "Google" },
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "deepseek", label: "DeepSeek" },
  { value: "qwen", label: "Qwen" },
  { value: "xai", label: "xAI" },
  { value: "kimi", label: "Kimi (Moonshot)" },
];

const MODELS: Record<LLMProvider, { value: LLMModelType; label: string }[]> = {
  google: [
    { value: "gemini-3.1-pro", label: "Gemini 3.1 Pro" },
    { value: "gemini-omni-flash-preview", label: "Gemini Omni Flash Preview" },
    { value: "gemini-3.6-flash", label: "Gemini 3 Flash 3.6" },
    { value: "gemini-3.5-flash", label: "Gemini 3 Flash 3.5" },
    { value: "gemini-3.5-flash-lite", label: "Gemini 3.5 Flash Lite" },
    { value: "gemini-3.1-flash-lite", label: "Gemini 3.1 Flash Lite" },
  ],
  openai: [
    { value: "gpt-5.6-sol", label: "GPT-5.6 Sol" },
    { value: "gpt-5.6-terra", label: "GPT-5.6 Terra" },
    { value: "gpt-5.6-luna", label: "GPT-5.6 Luna" },
    { value: "gpt-5.1", label: "GPT-5.1" },
    { value: "gpt-4.1-mini", label: "GPT-4.1 Mini" },
  ],
  anthropic: [
    { value: "claude-sonnet-5", label: "Claude 5 Sonnet" },
    { value: "claude-sonnet-4-5", label: "Claude 4.5 Sonnet" },
    { value: "claude-haiku-4-5", label: "Claude 4.5 Haiku" },
    { value: "claude-opus-4-7", label: "Claude 4.7 Opus" },
    { value: "opus-4.8", label: "Claude 4.8 Opus" },
    { value: "claude-fable-5", label: "Fable 5" },
  ],
  deepseek: [
    // Только модели, живые в API (проверено 14.08.2026): V3.2 снята.
    { value: "deepseek-v4-pro", label: "DeepSeek V4 Pro" },
    { value: "deepseek-v4-flash", label: "DeepSeek V4 Flash" },
  ],
  qwen: [
    { value: "qwen3.8-max", label: "Qwen3.8 Max" },
    { value: "qwen3.7-max", label: "Qwen3.7 Max" },
    { value: "qwen3.6-plus", label: "Qwen3.6 Plus" },
  ],
  xai: [
    { value: "grok-4.5", label: "Grok 4.5" },
    { value: "grok-4.2", label: "Grok 4.2" },
  ],
  kimi: [
    { value: "kimi-k3", label: "Kimi K3" },
  ],
};

type LLMGenerateNodeType = Node<LLMGenerateNodeData, "llmGenerate">;

export function LLMGenerateNode({ id, data, selected }: NodeProps<LLMGenerateNodeType>) {
  const t = useTranslations("banana");
  const nodeData = data;
  const updateNodeData = useWorkflowStore((state) => state.updateNodeData);

  const handleProviderChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      const newProvider = e.target.value as LLMProvider;
      const firstModelForProvider = MODELS[newProvider][0].value;
      updateNodeData(id, { provider: newProvider, model: firstModelForProvider });
    },
    [id, updateNodeData]
  );

  const handleModelChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      updateNodeData(id, { model: e.target.value as LLMModelType });
    },
    [id, updateNodeData]
  );

  const handleTemperatureChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      updateNodeData(id, { temperature: parseFloat(e.target.value) });
    },
    [id, updateNodeData]
  );

  const handleMaxTokensChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      updateNodeData(id, { maxTokens: parseInt(e.target.value, 10) });
    },
    [id, updateNodeData]
  );

  const regenerateNode = useWorkflowStore((state) => state.regenerateNode);
  const isRunning = useWorkflowStore((state) => state.isRunning);

  const handleRegenerate = useCallback(() => {
    regenerateNode(id);
  }, [id, regenerateNode]);

  const handleClearOutput = useCallback(() => {
    updateNodeData(id, { outputText: null, status: "idle", error: null });
  }, [id, updateNodeData]);

  const availableModels = MODELS[nodeData.provider];

  return (
    <BaseNode id={id} title="LLM Generate" selected={selected} hasError={nodeData.status === "error"}>
      <Handle type="target" position={Position.Left} id="text" style={{ top: "50%" }} data-handletype="text" />
      <Handle type="source" position={Position.Right} id="text" data-handletype="text" />

      <div className="flex-1 flex flex-col min-h-0 gap-2">
        <div className="relative w-full flex-1 min-h-[80px] border border-dashed border-brain-700/70 rounded p-2 overflow-auto">
          {nodeData.status === "loading" ? (
            <div className="h-full flex items-center justify-center">
              <svg className="w-4 h-4 animate-spin text-brain-200/70" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
              </svg>
            </div>
          ) : nodeData.status === "error" ? (
            <span className="text-[10px] text-red-400">{nodeErrorText(nodeData.error, t)}</span>
          ) : nodeData.outputText ? (
            <>
              <p className="text-[10px] text-brain-100 whitespace-pre-wrap break-words pr-6">{nodeData.outputText}</p>
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
                  onClick={handleClearOutput}
                  className="w-5 h-5 bg-brain-950/70 hover:bg-red-600/80 rounded flex items-center justify-center text-brain-200 hover:text-brain-50 transition-colors"
                  title="Clear output"
                >
                  <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </>
          ) : (
            <div className="h-full flex items-center justify-center">
              <span className="text-brain-200/60 text-[10px]">{t("run_to_generate")}</span>
            </div>
          )}
        </div>

        <select
          value={nodeData.provider}
          onChange={handleProviderChange}
          className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
        >
          {PROVIDERS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>

        <select
          value={nodeData.model}
          onChange={handleModelChange}
          className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
        >
          {availableModels.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>

        <div className="flex gap-1.5 shrink-0">
          <div className="flex-1 flex flex-col gap-0.5">
            <label className="text-[9px] text-brain-200/60">Temp: {nodeData.temperature.toFixed(1)}</label>
            <input
              type="range"
              min="0"
              max="2"
              step="0.1"
              value={nodeData.temperature}
              onChange={handleTemperatureChange}
              className="w-full h-1 bg-brain-900 rounded-lg appearance-none cursor-pointer accent-brain-400"
            />
          </div>
          <select
            value={nodeData.maxTokens}
            onChange={handleMaxTokensChange}
            className="w-16 text-[10px] py-1 px-1 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100"
            title="Max tokens"
          >
            <option value={256}>256</option>
            <option value={512}>512</option>
            <option value={1024}>1K</option>
            <option value={2048}>2K</option>
            <option value={4096}>4K</option>
          </select>
        </div>
      </div>
    </BaseNode>
  );
}


