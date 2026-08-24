"use client";

import { useCallback, useState } from "react";
import { Handle, Position, NodeProps, Node } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { BaseNode } from "./BaseNode";
import { BrandModal } from "@/banana/components/BrandModal";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { InfographicNodeData } from "@/banana/types";

// Модели-картинки. Всё рисует модель по метафоричному промпту; ключевую суть
// (essence + цифры) текст дополняет, не дублируя. BYO-провайдеры (FLUX/Ideogram/
// Recraft) работают при наличии ключа (env/Интеграции), иначе — фолбэк.
const IMAGE_MODELS: { value: string; label: string; labelKey?: string }[] = [
  { value: "gemini-3.1-flash-image", label: "Gemini 3.1 Flash Image" },
  { value: "gemini-3-pro-image", label: "Gemini 3 Pro Image" },
  { value: "gemini-3.1-flash-lite-image", label: "Gemini 3.1 Flash-Lite Image" },
  { value: "gpt-image-2", label: "GPT Image 2 (OpenAI)" },
  { value: "flux-1.1-pro", label: "FLUX 1.1 Pro (Replicate)" },
  { value: "ideogram-v3", label: "Ideogram 4.0" },
  { value: "recraft-v3", label: "Recraft V4" },
  { value: "gpt-image-1", label: "GPT Image 1" },
  { value: "dall-e-3", label: "DALL·E 3" },
  // Подпись «(старое)» переводится: константа вне компонента → ключ + t() при рендере.
  { value: "gemini-2.5-flash-image", label: "Gemini 2.5 Flash Image",
    labelKey: "image_model_gemini_25_flash_legacy" },
];

type InfographicNodeType = Node<InfographicNodeData, "infographic">;

export function InfographicNode({ id, data, selected }: NodeProps<InfographicNodeType>) {
  const t = useTranslations("banana");
  const updateNodeData = useWorkflowStore((state) => state.updateNodeData);

  const handleModelChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => updateNodeData(id, { model: e.target.value }),
    [id, updateNodeData]
  );
  const handleRenderChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => updateNodeData(id, { render: e.target.value as "model" | "code" }),
    [id, updateNodeData]
  );
  const handleFormatChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => updateNodeData(id, { format: e.target.value }),
    [id, updateNodeData]
  );
  const handleAudienceChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => updateNodeData(id, { audience: e.target.value }),
    [id, updateNodeData]
  );
  const handleLangChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => updateNodeData(id, { lang: e.target.value }),
    [id, updateNodeData]
  );
  const handleRecipientChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => updateNodeData(id, { recipient: e.target.value }),
    [id, updateNodeData]
  );
  const handleStyleChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => updateNodeData(id, { style: e.target.value }),
    [id, updateNodeData]
  );
  const handleBrandToggle = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => updateNodeData(id, { use_brand: e.target.checked }),
    [id, updateNodeData]
  );
  const handleScopeChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => updateNodeData(id, { thread_scope: e.target.value }),
    [id, updateNodeData]
  );
  const [brandOpen, setBrandOpen] = useState(false);
  const useBrand = data.use_brand !== false;
  const style = data.style || "auto";
  const render = data.render || "model";
  const format = data.format || "infographic";
  const audience = data.audience || "public";
  const lang = data.lang || "auto";

  return (
    <BaseNode id={id} title={t("node_infographic_title")} selected={selected}>
      <Handle type="target" position={Position.Left} id="text" data-handletype="text" />
      <Handle type="source" position={Position.Right} id="image" data-handletype="image" />

      <div className="flex-1 flex flex-col min-h-0 gap-2">
        <div className="flex-1 min-h-[64px] border border-dashed border-brain-700/70 rounded p-2 flex flex-col items-center justify-center gap-1 text-center">
          {data.image ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={data.image} alt="infographic" className="max-w-full max-h-full object-contain rounded" />
          ) : (
            <>
              <span className="text-brain-200/60 text-[10px]">{t("node_infographic_hint")}</span>
              <span className="text-brain-200/40 text-[9px] leading-snug">{t("node_infographic_sub")}</span>
            </>
          )}
        </div>
        <select
          value={format}
          onChange={handleFormatChange}
          title={t("node_infographic_format_title")}
          className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
        >
          <option value="infographic">{t("node_format_infographic")}</option>
          <option value="organism">{t("node_format_organism")}</option>
          <option value="character">{t("node_format_character")}</option>
          <option value="comic">{t("node_format_comic")}</option>
          <option value="meme">{t("node_format_meme")}</option>
          <option value="metaphor">{t("node_format_metaphor")}</option>
        </select>
        {format === "infographic" && (
          <select
            value={style}
            onChange={handleStyleChange}
            title={t("node_style_title")}
            className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
          >
            <option value="auto">{t("node_style_auto")}</option>
            <option value="buzan">{t("node_style_buzan")}</option>
            <option value="buzan_vector">{t("node_style_buzan_vector")}</option>
            <option value="buzan_marker">{t("node_style_buzan_marker")}</option>
            <option value="buzan_chalk">{t("node_style_buzan_chalk")}</option>
            <option value="buzan_notebook">{t("node_style_buzan_notebook")}</option>
            <option value="poster">{t("node_style_poster")}</option>
            <option value="neon">{t("node_style_neon")}</option>
            <option value="editorial">{t("node_style_editorial")}</option>
            <option value="isometric">{t("node_style_isometric")}</option>
            <option value="blueprint">{t("node_style_blueprint")}</option>
            <option value="engraving">{t("node_style_engraving")}</option>
            <option value="magazine">{t("node_style_magazine")}</option>
            <option value="comic">{t("node_style_comic")}</option>
            <option value="comic_manga">{t("node_style_comic_manga")}</option>
            <option value="comic_superhero">{t("node_style_comic_superhero")}</option>
            <option value="comic_noir">{t("node_style_comic_noir")}</option>
            <option value="comic_newspaper">{t("node_style_comic_newspaper")}</option>
          </select>
        )}
        <div className="flex items-center gap-2 text-[10px] text-brain-300">
          <label className="flex items-center gap-1.5 cursor-pointer flex-1">
            <input type="checkbox" checked={useBrand} onChange={handleBrandToggle}
              className="w-3.5 h-3.5 rounded bg-brain-700 border-brain-600 text-blue-500" />
            {t("node_brand_toggle")}
          </label>
          <button onClick={(e) => { e.stopPropagation(); setBrandOpen(true); }}
            className="nodrag nopan px-1.5 py-0.5 rounded border border-brain-600/60 text-brain-300 hover:text-white hover:border-brain-400/70">
            {t("node_brand_edit")}
          </button>
        </div>
        {format === "infographic" && (
          <input
            value={data.thread_scope || ""}
            onChange={handleScopeChange}
            placeholder={t("node_thread_scope_placeholder")}
            title={t("node_thread_scope_title")}
            className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
          />
        )}
        {brandOpen && <BrandModal onClose={() => setBrandOpen(false)} />}
        <select
          value={audience}
          onChange={handleAudienceChange}
          title={t("node_audience_title")}
          className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
        >
          <option value="public">{t("node_audience_public")}</option>
          <option value="private">{t("node_audience_private")}</option>
        </select>
        {audience === "private" && (
          <input
            value={data.recipient || ""}
            onChange={handleRecipientChange}
            placeholder={t("node_recipient_placeholder")}
            title={t("node_recipient_title")}
            className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
          />
        )}
        <select
          value={lang}
          onChange={handleLangChange}
          title={t("node_lang_title")}
          className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
        >
          <option value="auto">{t("node_lang_auto")}</option>
          <option value="ru">{t("node_lang_ru")}</option>
          <option value="en">{t("node_lang_en")}</option>
        </select>
        <select
          value={render}
          onChange={handleRenderChange}
          title={t("node_infographic_render_title")}
          className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
        >
          <option value="model">{t("node_infographic_render_model")}</option>
          <option value="code">{t("node_infographic_render_code")}</option>
        </select>
        {render === "model" && (
          <select
            value={data.model || "gemini-3.1-flash-image"}
            onChange={handleModelChange}
            className="w-full text-[10px] py-1 px-1.5 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 text-brain-100 shrink-0"
          >
            {IMAGE_MODELS.map((m) => (
              <option key={m.value} value={m.value}>{m.labelKey ? t(m.labelKey) : m.label}</option>
            ))}
          </select>
        )}
      </div>
    </BaseNode>
  );
}
