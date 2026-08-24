"use client";

import { useState, useRef, useEffect } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useTemplatesStore, WorkflowTemplate } from "@/banana/store/templatesStore";
import { useWorkflowStore, WorkflowFile } from "@/banana/store/workflowStore";
import { Folder, FileJson, Plus, Trash2, Download, X, Check, Edit2, Loader2 } from "lucide-react";

type TemplateCategory = "presentation" | "creative" | "process" | "visual";

interface BuiltinTemplateInfo {
  id: string;
  name: string;
  filename: string;
  description: string;         // формальное описание
  category: TemplateCategory;
  metaphor?: string;          // визуально-метафорическое описание (что эмоционально передаёт)
}

interface TemplatesPanelProps {
  isOpen: boolean;
  onClose: () => void;
}

export function TemplatesPanel({ isOpen, onClose }: TemplatesPanelProps) {
  const t = useTranslations("banana");
  const locale = useLocale();
  const { templates, deleteTemplate, addTemplate, getUserTemplates } = useTemplatesStore();
  const { loadWorkflow, nodes, edges, edgeStyle } = useWorkflowStore();
  
  const [activeTab, setActiveTab] = useState<"builtin" | "user">("builtin");
  const [showSaveDialog, setShowSaveDialog] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saveDescription, setSaveDescription] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [showVisualConcept, setShowVisualConcept] = useState(false);
  
  const builtinTemplatesList: BuiltinTemplateInfo[] = [
    { id: "builtin-dual-report", name: t("tpl_dual_report_name"), filename: "process-dual-report.json", description: t("tpl_dual_report_desc"), category: "visual", metaphor: t("tpl_dual_report_metaphor") },
    { id: "builtin-meeting-mindmap", name: t("tpl_mindmap_name"), filename: "process-meeting-mindmap.json", description: t("tpl_mindmap_desc"), category: "visual", metaphor: t("tpl_mindmap_metaphor") },
    { id: "builtin-company-organism", name: t("tpl_organism_name"), filename: "process-company-organism.json", description: t("tpl_organism_desc"), category: "visual", metaphor: t("tpl_organism_metaphor") },
    { id: "builtin-employee-portrait", name: t("tpl_portrait_name"), filename: "process-employee-portrait.json", description: t("tpl_portrait_desc"), category: "visual", metaphor: t("tpl_portrait_metaphor") },
    { id: "builtin-weekly-audio", name: t("tpl_audio_name"), filename: "process-weekly-audio.json", description: t("tpl_audio_desc"), category: "visual", metaphor: t("tpl_audio_metaphor") },
    { id: "builtin-team-visual", name: t("tpl_team_visual_name"), filename: "process-weekly-team-visual.json", description: t("tpl_team_visual_desc"), category: "visual", metaphor: t("tpl_team_visual_metaphor") },
    { id: "builtin-weekly-comic", name: t("tpl_weekly_comic_name"), filename: "process-weekly-comic.json", description: t("tpl_weekly_comic_desc"), category: "visual", metaphor: t("tpl_weekly_comic_metaphor") },
    { id: "builtin-company-weather", name: t("tpl_weather_name"), filename: "process-company-weather.json", description: t("tpl_weather_desc"), category: "visual", metaphor: t("tpl_weather_metaphor") },
    { id: "builtin-monthly-journey", name: t("tpl_journey_name"), filename: "process-monthly-journey.json", description: t("tpl_journey_desc"), category: "visual", metaphor: t("tpl_journey_metaphor") },
    { id: "builtin-daily-digest-image", name: t("tpl_digest_image_name"), filename: "process-daily-digest-image.json", description: t("tpl_digest_image_desc"), category: "visual", metaphor: t("tpl_digest_image_metaphor") },
    { id: "builtin-mixed-promo", name: t("tpl_mixed_promo_name"), filename: "mixed-promo-after-meeting.json", description: t("tpl_mixed_promo_desc"), category: "process" },
    { id: "builtin-mixed-pcp", name: t("tpl_mixed_pcp_name"), filename: "mixed-process-creative-process.json", description: t("tpl_mixed_pcp_desc"), category: "process" },
    { id: "builtin-process-weekly", name: t("tpl_process_weekly_name"), filename: "process-weekly-digest.json", description: t("tpl_process_weekly_desc"), category: "process" },
    { id: "builtin-process-followup", name: t("tpl_process_followup_name"), filename: "process-meeting-followup.json", description: t("tpl_process_followup_desc"), category: "process" },
    { id: "builtin-client-summary-mindmap", name: t("tpl_client_summary_name"), filename: "process-client-summary-mindmap.json", description: t("tpl_client_summary_desc"), category: "process" },
    { id: "builtin-approval-client-mindmap", name: t("tpl_approval_name"), filename: "process-approval-client-mindmap.json", description: t("tpl_approval_desc"), category: "process" },
    { id: "builtin-meeting-to-telegram", name: t("tpl_meeting_tg_name"), filename: "process-meeting-to-telegram.json", description: t("tpl_meeting_tg_desc"), category: "process" },
    { id: "builtin-kp-from-meeting", name: t("tpl_kp_name"), filename: "process-kp-from-meeting.json", description: t("tpl_kp_desc"), category: "process" },
    { id: "builtin-lead-to-kp", name: t("tpl_lead_kp_name"), filename: "process-lead-to-kp.json", description: t("tpl_lead_kp_desc"), category: "process" },
    { id: "builtin-crm-to-xlsx", name: t("tpl_crm_xlsx_name"), filename: "process-crm-to-xlsx.json", description: t("tpl_crm_xlsx_desc"), category: "process" },
    { id: "builtin-data-analyst", name: t("tpl_data_analyst_name"), filename: "process-data-analyst.json", description: t("tpl_data_analyst_desc"), category: "process" },
    { id: "builtin-tg-digest", name: t("tpl_tg_digest_name"), filename: "process-tg-digest.json", description: t("tpl_tg_digest_desc"), category: "process" },
    { id: "builtin-process-slack-digest", name: t("tpl_slack_digest_name"), filename: "process-slack-digest.json", description: t("tpl_slack_digest_desc"), category: "process" },
    { id: "builtin-presentation-deck", name: t("tpl_presentation_deck_name"), filename: "presentation-deck.json", description: t("tpl_presentation_deck_desc"), category: "presentation" },
    { id: "builtin-pitch-deck", name: t("tpl_pitch_deck_name"), filename: "pitch_deck.json", description: t("tpl_pitch_deck_desc"), category: "presentation" },
    { id: "builtin-video-storyboard", name: t("tpl_video_storyboard_name"), filename: "video-storyboard.json", description: t("tpl_video_storyboard_desc"), category: "creative" },
    { id: "builtin-cinematic-storyboard", name: t("tpl_cinematic_name"), filename: "cinematic-storyboard.json", description: t("tpl_cinematic_desc"), category: "creative" },
    { id: "builtin-interior-design", name: t("tpl_interior_name"), filename: "interior-design-renovation.json", description: t("tpl_interior_desc"), category: "creative" },
    { id: "builtin-creative-ads-workflow", name: t("tpl_creative_ads_name"), filename: "creative-ads-workflow.json", description: t("tpl_creative_ads_desc"), category: "creative" },
    { id: "landing-page-workflow", name: t("tpl_landing_name"), filename: "landing-page-workflow.json", description: t("tpl_landing_desc"), category: "creative" },
    { id: "builtin-contact-sheet", name: t("tpl_contact_sheet_name"), filename: "contact-sheet.json", description: t("tpl_contact_sheet_desc"), category: "creative" },
  ];
  
  const [loadingTemplate, setLoadingTemplate] = useState<string | null>(null);
  
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(event.target as Node)) {
        onClose();
      }
    };
    if (isOpen) {
      document.addEventListener("mousedown", handleClickOutside);
    }
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const userTemplates = getUserTemplates();

  const handleLoadBuiltinTemplate = async (template: BuiltinTemplateInfo) => {
    setLoadingTemplate(template.id);
    try {
      // Шаблон под язык интерфейса: сначала /templates/<locale>/<file>
      // (тексты промптов и заметок на языке пользователя), иначе — базовый
      // русский. Так содержимое доски переводится вместе с интерфейсом.
      let res = locale && locale !== "ru"
        ? await fetch(`/templates/${locale}/${template.filename}`)
        : null;
      if (!res || !res.ok) {
        res = await fetch(`/templates/${template.filename}`);
      }
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const workflow = await res.json();
      if (workflow && workflow.nodes && workflow.edges) {
        loadWorkflow(workflow);
        onClose();
      } else {
        alert(t("alert_invalid_template"));
      }
    } catch (err) {
      console.error("Failed to load template:", err);
      alert(t("alert_load_template_error"));
    } finally {
      setLoadingTemplate(null);
    }
  };

  const handleLoadUserTemplate = (template: WorkflowTemplate) => {
    loadWorkflow(template.workflow);
    onClose();
  };

  const handleDeleteTemplate = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (confirm(t("confirm_delete_template"))) {
      deleteTemplate(id);
    }
  };

  const handleSaveAsTemplate = () => {
    if (!saveName.trim()) return;
    
    const workflow: WorkflowFile = {
      version: 1,
      name: saveName.trim(),
      nodes,
      edges,
      edgeStyle,
    };
    
    addTemplate(saveName.trim(), saveDescription.trim(), workflow);
    setShowSaveDialog(false);
    setSaveName("");
    setSaveDescription("");
    setActiveTab("user");
  };

  const handleExportTemplate = (template: WorkflowTemplate, e: React.MouseEvent) => {
    e.stopPropagation();
    const json = JSON.stringify(template.workflow, null, 2);
    const blob = new Blob([json], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${template.name}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const handleStartEdit = (template: WorkflowTemplate, e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingId(template.id);
    setEditName(template.name);
  };

  const handleSaveEdit = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (editName.trim()) {
      useTemplatesStore.getState().updateTemplate(id, { name: editName.trim() });
    }
    setEditingId(null);
  };

  const handleCancelEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingId(null);
  };

  return (
    <div
      ref={panelRef}
      className="absolute bottom-full left-0 mb-2 w-[380px] bg-brain-900 border border-brain-700/60 rounded-lg shadow-2xl overflow-hidden"
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-brain-700/40">
        <h3 className="text-sm font-semibold text-brain-100">{t("templates_heading")}</h3>
        <button
          onClick={onClose}
          className="p-1 text-brain-400 hover:text-brain-200 transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-brain-700/40">
        <button
          onClick={() => setActiveTab("builtin")}
          className={`flex-1 px-4 py-2 text-xs font-medium transition-colors ${
            activeTab === "builtin"
              ? "text-brain-100 bg-brain-800/50 border-b-2 border-brain-400"
              : "text-brain-400 hover:text-brain-200"
          }`}
        >
          <Folder className="w-3.5 h-3.5 inline-block mr-1.5" />
          {t('tab_builtin', { count: builtinTemplatesList.length })}
        </button>
        <button
          onClick={() => setActiveTab("user")}
          className={`flex-1 px-4 py-2 text-xs font-medium transition-colors ${
            activeTab === "user"
              ? "text-brain-100 bg-brain-800/50 border-b-2 border-brain-400"
              : "text-brain-400 hover:text-brain-200"
          }`}
        >
          <FileJson className="w-3.5 h-3.5 inline-block mr-1.5" />
          {t('tab_user', { count: userTemplates.length })}
        </button>
      </div>

      {/* Templates List */}
      <div className="max-h-[300px] overflow-y-auto">
        {activeTab === "builtin" ? (
          // Builtin templates from public/templates
          builtinTemplatesList.length === 0 ? (
            <div className="px-4 py-8 text-center text-brain-500 text-xs">
              {t('empty_builtin')}
            </div>
          ) : (
            <div className="p-2 space-y-1">
              {(["presentation", "creative", "process", "visual"] as TemplateCategory[]).map((cat) => {
                const inCat = builtinTemplatesList.filter((tpl) => tpl.category === cat);
                if (inCat.length === 0) return null;
                const isVisual = cat === "visual";
                return (
                  <div key={cat} className={`mb-1 ${isVisual ? "mt-3 pt-2 border-t border-brain-700/40" : ""}`}>
                    <div className="px-1 pt-2 pb-1 flex items-center gap-1.5">
                      <span className="text-[10px] uppercase tracking-wide text-brain-500 font-semibold">
                        {t(`tpl_cat_${cat}`)}
                      </span>
                      {isVisual && (
                        <button
                          onClick={(e) => { e.stopPropagation(); setShowVisualConcept((v) => !v); }}
                          title={t("visual_concept_button_title")}
                          className="w-4 h-4 flex items-center justify-center rounded-full text-[9px] leading-none text-fuchsia-300 hover:text-white border border-fuchsia-500/40 hover:border-fuchsia-400/70"
                        >
                          ?
                        </button>
                      )}
                    </div>
                    {isVisual && showVisualConcept && (
                      <div className="mx-1 mb-2 rounded-lg border border-fuchsia-500/30 bg-fuchsia-500/5 p-2.5">
                        <div className="text-[11px] font-medium text-fuchsia-200">{t("visual_concept_title")}</div>
                        <div className="text-[10.5px] text-brain-300 leading-snug mt-1">{t("visual_concept_body")}</div>
                      </div>
                    )}
                    {inCat.map((template) => (
                      <div
                        key={template.id}
                        onClick={() => handleLoadBuiltinTemplate(template)}
                        className={`group flex items-start gap-3 p-3 rounded-lg hover:bg-brain-800/50 cursor-pointer transition-colors ${
                          loadingTemplate === template.id ? "opacity-50 pointer-events-none" : ""
                        }`}
                      >
                        <div className={`flex-shrink-0 w-8 h-8 rounded flex items-center justify-center ${isVisual ? "bg-fuchsia-700/30" : "bg-brain-700/50"}`}>
                          {loadingTemplate === template.id ? (
                            <Loader2 className="w-4 h-4 text-brain-400 animate-spin" />
                          ) : (
                            <FileJson className={`w-4 h-4 ${isVisual ? "text-fuchsia-300" : "text-brain-400"}`} />
                          )}
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="text-xs font-medium text-brain-100 truncate">{template.name}</div>
                          <div className="text-[10px] text-brain-500 mt-0.5 line-clamp-2">{template.description}</div>
                          {template.metaphor && (
                            <div className="text-[10px] text-fuchsia-300/80 italic mt-0.5 line-clamp-2">✦ {template.metaphor}</div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                );
              })}
            </div>
          )
        ) : (
          // User templates from localStorage
          userTemplates.length === 0 ? (
            <div className="px-4 py-8 text-center text-brain-500 text-xs">
              {t('empty_user')}
            </div>
          ) : (
            <div className="p-2 space-y-1">
              {userTemplates.map((template) => (
                <div
                  key={template.id}
                  onClick={() => handleLoadUserTemplate(template)}
                  className="group flex items-start gap-3 p-3 rounded-lg hover:bg-brain-800/50 cursor-pointer transition-colors"
                >
                  <div className="flex-shrink-0 w-8 h-8 rounded bg-brain-700/50 flex items-center justify-center">
                    <FileJson className="w-4 h-4 text-brain-400" />
                  </div>
                  <div className="flex-1 min-w-0">
                    {editingId === template.id ? (
                      <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="text"
                          value={editName}
                          onChange={(e) => setEditName(e.target.value)}
                          className="flex-1 px-2 py-1 text-xs bg-brain-800 border border-brain-600 rounded text-brain-100 focus:outline-none focus:border-brain-400"
                          autoFocus
                          onKeyDown={(e) => {
                            if (e.key === "Enter") handleSaveEdit(template.id, e as any);
                            if (e.key === "Escape") handleCancelEdit(e as any);
                          }}
                        />
                        <button onClick={(e) => handleSaveEdit(template.id, e)} className="p-1 text-green-500 hover:text-green-400">
                          <Check className="w-3.5 h-3.5" />
                        </button>
                        <button onClick={handleCancelEdit} className="p-1 text-brain-400 hover:text-brain-200">
                          <X className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    ) : (
                      <div className="text-xs font-medium text-brain-100 truncate">{template.name}</div>
                    )}
                    <div className="text-[10px] text-brain-500 mt-0.5 line-clamp-2">{template.description}</div>
                    <div className="text-[10px] text-brain-600 mt-1">
                      {t('template_summary', { nodes: template.workflow.nodes.length, edges: template.workflow.edges.length })}
                    </div>
                  </div>
                  <div className="flex-shrink-0 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={(e) => handleExportTemplate(template, e)}
                      className="p-1.5 text-brain-400 hover:text-brain-200 hover:bg-brain-700/50 rounded transition-colors"
                      title={t("title_export")}
                    >
                      <Download className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={(e) => handleStartEdit(template, e)}
                      className="p-1.5 text-brain-400 hover:text-brain-200 hover:bg-brain-700/50 rounded transition-colors"
                      title={t("title_rename")}
                    >
                      <Edit2 className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={(e) => handleDeleteTemplate(template.id, e)}
                      className="p-1.5 text-brain-400 hover:text-red-400 hover:bg-brain-700/50 rounded transition-colors"
                      title={t("title_delete")}
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )
        )}
      </div>

      {/* Save as Template */}
      {showSaveDialog ? (
        <div className="p-3 border-t border-brain-700/40 bg-brain-800/30">
          <div className="space-y-2">
            <input
              type="text"
              placeholder={t("placeholder_template_name")}
              value={saveName}
              onChange={(e) => setSaveName(e.target.value)}
              className="w-full px-3 py-2 text-xs bg-brain-800 border border-brain-600 rounded text-brain-100 placeholder-brain-500 focus:outline-none focus:border-brain-400"
              autoFocus
            />
            <textarea
              placeholder={t("placeholder_template_description")}
              value={saveDescription}
              onChange={(e) => setSaveDescription(e.target.value)}
              className="w-full px-3 py-2 text-xs bg-brain-800 border border-brain-600 rounded text-brain-100 placeholder-brain-500 focus:outline-none focus:border-brain-400 resize-none"
              rows={2}
            />
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setShowSaveDialog(false)}
                className="px-3 py-1.5 text-xs text-brain-400 hover:text-brain-200 transition-colors"
              >
                {t('cancel_button')}
              </button>
              <button
                onClick={handleSaveAsTemplate}
                disabled={!saveName.trim()}
                className="px-3 py-1.5 text-xs bg-brain-600 hover:bg-brain-500 text-white rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {t('save_button')}
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="p-3 border-t border-brain-700/40">
          <button
            onClick={() => setShowSaveDialog(true)}
            disabled={nodes.length === 0}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 text-xs font-medium text-brain-200 bg-brain-800 hover:bg-brain-700 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Plus className="w-3.5 h-3.5" />
            {t('save_current_as_template')}
          </button>
        </div>
      )}
    </div>
  );
}

