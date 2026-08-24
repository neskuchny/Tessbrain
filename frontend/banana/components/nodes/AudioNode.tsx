"use client";

import { useCallback } from "react";
import { Handle, Position, NodeProps, Node } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { BaseNode } from "./BaseNode";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { AudioNodeData } from "@/banana/types";

// Провайдеры TTS. OpenAI работает на платформенном ключе; ElevenLabs — по BYO
// ключу из «Интеграций» (лучшее качество/эмоции).
// Подписи — ключи перевода: константа вне компонента, t() применяется при
// рендере списка (как в components/ModelSettingsPanel.tsx).
const PROVIDERS: { value: string; label: string; labelKey?: string }[] = [
  { value: "openai", label: "OpenAI", labelKey: "audio_provider_openai" },
  { value: "elevenlabs", label: "ElevenLabs", labelKey: "audio_provider_elevenlabs" },
];
const OPENAI_VOICES = ["alloy", "nova", "shimmer", "onyx", "echo", "fable"];

type AudioNodeType = Node<AudioNodeData, "audio">;

export function AudioNode({ id, data, selected }: NodeProps<AudioNodeType>) {
  const t = useTranslations("banana");
  const updateNodeData = useWorkflowStore((state) => state.updateNodeData);
  const provider = data.provider || "openai";

  const setProvider = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => updateNodeData(id, { provider: e.target.value }),
    [id, updateNodeData]
  );
  const setVoice = useCallback(
    (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => updateNodeData(id, { voice: e.target.value }),
    [id, updateNodeData]
  );

  return (
    <BaseNode id={id} title={t("node_audio_title")} selected={selected}>
      <Handle type="target" position={Position.Left} id="text" data-handletype="text" />
      <Handle type="source" position={Position.Right} id="text" data-handletype="text" />

      <div className="flex-1 flex flex-col min-h-0 gap-2">
        <div className="flex-1 min-h-[56px] border border-dashed border-brain-700/70 rounded p-2 flex flex-col items-center justify-center gap-1 text-center">
          <span className="text-brain-200/60 text-[10px]">{t("node_audio_hint")}</span>
          <span className="text-brain-200/40 text-[9px] leading-snug">{t("node_audio_sub")}</span>
        </div>
        <select
          value={provider}
          onChange={setProvider}
          className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
        >
          {PROVIDERS.map((p) => (
            <option key={p.value} value={p.value}>{p.labelKey ? t(p.labelKey) : p.label}</option>
          ))}
        </select>
        {provider === "openai" ? (
          <select
            value={data.voice || OPENAI_VOICES[0]}
            onChange={setVoice}
            className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
          >
            {OPENAI_VOICES.map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        ) : (
          <input
            value={data.voice || ""}
            onChange={setVoice}
            placeholder={t("node_audio_voice_placeholder")}
            className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
          />
        )}
      </div>
    </BaseNode>
  );
}
