"use client";

import { useCallback } from "react";
import { useTranslations } from "next-intl";
import { Handle, Position, NodeProps, Node } from "@xyflow/react";
import { BaseNode } from "./BaseNode";
import { FolderPicker } from "@/banana/components/FolderPicker";
import { MeetingPicker } from "@/banana/components/MeetingPicker";
import { DatasetPicker } from "@/banana/components/DatasetPicker";
import { RecipientInput } from "@/banana/components/RecipientInput";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { ProcessNodeData } from "@/banana/types";

// Один компонент на все процесс-блоки: рендер по node.type (прогон — на сервере,
// см. backend/core/board/process_engine.py). Здесь только конфигурация узла.
type ProcNode = Node<ProcessNodeData>;

const TITLE_KEYS: Record<string, string> = {
  trigger: "title_trigger",
  ask_brain: "title_ask_brain",
  report: "title_report",
  notify: "title_notify",
  task: "title_task",
  action: "title_action",
  generate: "title_generate",
  condition: "title_condition",
  wait_reply: "title_wait_reply",
  meeting_data: "title_meeting_data",
  meeting_share: "title_meeting_share",
  document: "title_document",
  crm_data: "title_crm_data",
  web_search: "title_web_search",
  report_xlsx: "title_report_xlsx",
  translate: "title_translate",
  doc_edit: "title_doc_edit",
  crm_write: "title_crm_write",
  coding_agent: "title_coding_agent",
};

// CLI-исполнители vibe-tasking (совпадают с _AGENT_COMMANDS бэкенда; свои
// агенты добавляются оператором через TESSENT_AGENT_COMMANDS_JSON и вводятся
// в поле вручную).
const CODING_AGENTS = ["claude", "codex", "grok", "qwen", "cursor", "kimi"] as const;

// Провайдеры записи в CRM (совпадают с backend/crm_writeback.py).
const CRM_WRITE_PROVIDERS = ["amocrm", "bitrix24", "hubspot", "pipedrive"] as const;

// Языки для узла «Перевод» (код → подпись). Код уходит в бэкенд, где
// маппится в название языка для промпта переводчику.
const TRANSLATE_LANGS = ["en", "ru", "de", "fr", "es", "it", "zh", "tr",
  "ar", "kk", "pt", "ja"] as const;

const MEETING_KINDS = ["summary", "tasks", "decisions", "participants",
  "transcript", "agenda", "report"] as const;

// Курируемый каталог действий — мост к подключённым интеграциям. tool + kwargs
// точно совпадают с backend (registry.execute_tool). В любое поле можно вписать
// {{input}} — подставится текст с предыдущего шага. Не в каталоге инструмент?
// Впишите tool_name вручную (режим «другой инструмент»).
// Подписи каталога живут вне компонента (хуки недоступны) — поэтому здесь
// ключи перевода (labelKey/placeholderKey), а t() применяется при рендере.
interface ActionField { key: string; labelKey: string; placeholderKey?: string; placeholder?: string }
interface ActionDef { tool: string; label: string; labelKey?: string; fields: ActionField[] }
const ACTION_CATALOG: ActionDef[] = [
  { tool: "slack_send_message", label: "Slack", labelKey: "action_slack_send_message", fields: [
    { key: "channel", labelKey: "action_field_channel", placeholderKey: "action_channel_placeholder" },
    { key: "text", labelKey: "action_field_text", placeholder: "{{input}}" } ] },
  { tool: "gmail_send", label: "Gmail", labelKey: "action_gmail_send", fields: [
    { key: "to", labelKey: "action_field_to", placeholder: "email@company.com" },
    { key: "subject", labelKey: "action_field_subject" },
    { key: "body", labelKey: "action_field_body_text", placeholder: "{{input}}" } ] },
  { tool: "notion_create_page", label: "Notion", labelKey: "action_notion_create_page", fields: [
    { key: "parent_id", labelKey: "action_field_parent_id" },
    { key: "title", labelKey: "action_field_title" },
    { key: "content", labelKey: "action_field_content", placeholder: "{{input}}" } ] },
  { tool: "jira_create_issue", label: "Jira", labelKey: "action_jira_create_issue", fields: [
    { key: "project", labelKey: "action_field_project", placeholder: "ABC" },
    { key: "summary", labelKey: "action_field_title" },
    { key: "description", labelKey: "action_field_description", placeholder: "{{input}}" } ] },
  { tool: "trello_create_card", label: "Trello", labelKey: "action_trello_create_card", fields: [
    { key: "list_id", labelKey: "action_field_list_id" },
    { key: "name", labelKey: "action_field_name" },
    { key: "description", labelKey: "action_field_description", placeholder: "{{input}}" } ] },
  { tool: "github_create_issue", label: "GitHub · issue", fields: [
    { key: "repo", labelKey: "action_field_repo", placeholder: "owner/repo" },
    { key: "title", labelKey: "action_field_title" },
    { key: "body", labelKey: "action_field_body", placeholder: "{{input}}" } ] },
  { tool: "linear_create_issue", label: "Linear", labelKey: "action_linear_create_issue", fields: [
    { key: "title", labelKey: "action_field_title" },
    { key: "description", labelKey: "action_field_description", placeholder: "{{input}}" },
    { key: "team_id", labelKey: "action_field_team_id" } ] },
];

const inputCls =
  "nodrag nopan nowheel w-full p-1.5 text-xs text-brain-50 border border-brain-700/70 rounded bg-brain-950/40 focus:outline-none focus:ring-1 focus:ring-brain-500 placeholder:text-brain-200/40";

export function ProcessNode({ id, type, data, selected }: NodeProps<ProcNode>) {
  const t = useTranslations("banana_process_node");
  // Часть ключей узла (coding_agent, язык триггера) исторически лежит в
  // общем каталоге «banana» — без этого фолбэка узел рисовал имя ключа
  // вместо текста. Ищем сначала в своём неймспейсе, затем в общем.
  const tb = useTranslations("banana");
  const tt = (key: string) => (t.has(key) ? t(key) : tb(key));
  const updateNodeData = useWorkflowStore((s) => s.updateNodeData);
  const set = useCallback(
    (patch: Partial<ProcessNodeData>) => updateNodeData(id, patch),
    [id, updateNodeData]
  );
  const nodeType = String(type || "");
  const d = data as ProcessNodeData;

  // Подсказка под узлом — только когда узел ВЫДЕЛЕН: на собранной схеме
  // десяток always-on пояснений раздувал карточки и мешал читать поток.
  // Полная справка всегда доступна по кнопке «?» в шапке (BaseNode).
  const hint = (key: string) =>
    selected ? (
      <div className="text-[9px] text-brain-400 px-0.5 leading-snug">{tt(key)}</div>
    ) : null;

  return (
    <BaseNode id={id} title={TITLE_KEYS[nodeType] ? tt(TITLE_KEYS[nodeType]) : nodeType} selected={selected}>
      {nodeType !== "trigger" && (
        <Handle type="target" position={Position.Left} id="in" data-handletype="text" />
      )}

      {nodeType === "trigger" && (
        <div className="space-y-1">
          <select value={d.trigger_type || "manual"} onChange={(e) => set({ trigger_type: e.target.value })}
            className={inputCls}>
            <option value="manual">{t("trigger_manual")}</option>
            <option value="schedule">{t("trigger_schedule")}</option>
            <option value="event">{t("trigger_event")}</option>
          </select>
          {d.trigger_type === "schedule" && (
            <div className="space-y-1">
              <select value={d.schedule_kind || "interval"}
                onChange={(e) => set({ schedule_kind: e.target.value })}
                className={inputCls}>
                <option value="interval">{t("schedule_interval")}</option>
                <option value="daily">{t("schedule_daily")}</option>
                <option value="weekly">{t("schedule_weekly")}</option>
              </select>
              {(d.schedule_kind || "interval") === "interval" ? (() => {
                // interval_min — источник правды (бэкенд читает его). Единица
                // (мин/час) — только для удобного ввода «каждые 2 часа».
                const unit = d.interval_unit === "hour" ? "hour" : "min";
                const total = d.interval_min ?? 60;
                const shown = unit === "hour" ? Math.max(1, Math.round(total / 60)) : total;
                const onNum = (raw: string) => {
                  const n = Math.max(1, parseInt(raw || "1", 10));
                  set({ interval_min: unit === "hour" ? n * 60 : Math.max(5, n),
                        interval_unit: unit });
                };
                return (
                  <label className="flex items-center gap-1 text-[10px] text-brain-300">
                    {t("every")}
                    <input type="number" min={1} value={shown}
                      onChange={(e) => onNum(e.target.value)}
                      className={`${inputCls} w-14`} />
                    <select value={unit}
                      onChange={(e) => set({ interval_unit: e.target.value })}
                      className={`${inputCls} w-20`}>
                      <option value="min">{t("unit_minutes")}</option>
                      <option value="hour">{t("unit_hours")}</option>
                    </select>
                  </label>
                );
              })() : (
                <div className="flex items-center gap-1 text-[10px] text-brain-300 flex-wrap">
                  {d.schedule_kind === "weekly" && (
                    <select value={String(d.weekday ?? 0)}
                      onChange={(e) => set({ weekday: parseInt(e.target.value, 10) })}
                      className={`${inputCls} w-20`}>
                      {[0, 1, 2, 3, 4, 5, 6].map((wd) => (
                        <option key={wd} value={wd}>{t(`weekday_${wd}`)}</option>
                      ))}
                    </select>
                  )}
                  {t("daily_at")}
                  <input type="time" value={d.daily_time || "09:00"}
                    onChange={(e) => set({ daily_time: e.target.value })}
                    className={`${inputCls} w-24`} />
                  <span className="text-brain-500">{t("utc_hint")}</span>
                </div>
              )}
            </div>
          )}
          {d.trigger_type === "event" && (
            <select value={d.event_type || "meeting_ended"} onChange={(e) => set({ event_type: e.target.value })}
              className={inputCls}>
              <option value="meeting_ended">{t("event_meeting_ended")}</option>
              <option value="new_meeting_scheduled">{t("event_new_meeting_scheduled")}</option>
              <option value="new_email">{t("event_new_email")}</option>
              <option value="new_lead">{t("event_new_lead")}</option>
              <option value="task_overdue">{t("event_task_overdue")}</option>
              <option value="task_completed">{t("event_task_completed")}</option>
              <option value="kpi_changed">{t("event_kpi_changed")}</option>
              <option value="document_updated">{t("event_document_updated")}</option>
            </select>
          )}
          {d.trigger_type === "event" &&
            (d.event_type || "meeting_ended") === "meeting_ended" && (
            <FolderPicker
              value={d.folder_ids || []}
              onChange={(ids) => set({ folder_ids: ids })}
            />
          )}
          {(d.trigger_type === "event" || d.trigger_type === "schedule") && (
            <label className={`flex items-center gap-2 text-[11px] px-1.5 py-1 rounded cursor-pointer ${d.enabled !== false ? "bg-emerald-500/10 text-emerald-300" : "bg-brain-800/60 text-brain-400"}`}>
              <input type="checkbox" checked={d.enabled !== false}
                onChange={(e) => set({ enabled: e.target.checked })}
                className="w-3.5 h-3.5 rounded bg-brain-700 border-brain-600 text-emerald-500" />
              {d.enabled !== false ? t("trigger_automation_on") : t("trigger_automation_off")}
            </label>
          )}
          <textarea value={d.payload || ""} onChange={(e) => set({ payload: e.target.value })}
            placeholder={t("payload_placeholder")}
            className={`${inputCls} min-h-[40px] resize-none`} />
          {/* Язык вывода ДОСКИ: расписания/события запускаются без браузера,
              поэтому язык живёт на триггере (авто = язык интерфейса при
              ручном запуске, иначе ru/персона). */}
          <select value={d.lang || "auto"} onChange={(e) => set({ lang: e.target.value })}
            title={tt("trigger_lang_title")} className={inputCls}>
            <option value="auto">{tt("trigger_lang_auto")}</option>
            <option value="ru">Русский</option>
            <option value="en">English</option>
          </select>
        </div>
      )}
      {nodeType === "ask_brain" && (
        <textarea value={d.prompt || ""} onChange={(e) => set({ prompt: e.target.value })}
          placeholder={t("prompt_placeholder")}
          className={`${inputCls} flex-1 min-h-[70px] resize-none`} />
      )}
      {nodeType === "generate" && (
        <textarea value={d.prompt || ""} onChange={(e) => set({ prompt: e.target.value })}
          placeholder={t("generate_placeholder")}
          className={`${inputCls} flex-1 min-h-[70px] resize-none`} />
      )}
      {nodeType === "report" && (
        <div className="space-y-1">
          <select value={d.report_type || "summary"}
            onChange={(e) => set({ report_type: e.target.value })}
            className={inputCls}>
            <option value="summary">{t("report_summary")}</option>
            <option value="progress">{t("report_progress")}</option>
            <option value="decisions">{t("report_decisions")}</option>
            <option value="custom">{t("report_custom")}</option>
          </select>
          {hint("report_hint")}
        </div>
      )}
      {nodeType === "notify" && (
        <div className="space-y-1">
          <select value={d.channel || "telegram"} onChange={(e) => set({ channel: e.target.value })}
            className={inputCls}>
            <option value="telegram">Telegram</option>
            <option value="email">Email</option>
          </select>
          {(d.channel || "telegram") === "telegram" && (
            <RecipientInput kind="telegram" value={d.chat_id || ""}
              onChange={(v) => set({ chat_id: v })}
              placeholder={t("notify_chat_id_placeholder")}
              title={t("notify_chat_id_title")}
              className={inputCls} />
          )}
          <textarea value={d.text || ""} onChange={(e) => set({ text: e.target.value })}
            placeholder={t("text_placeholder")}
            className={`${inputCls} min-h-[50px] resize-none`} />
        </div>
      )}
      {nodeType === "task" && (
        <input value={d.title || ""} onChange={(e) => set({ title: e.target.value })}
          placeholder={t("task_title_placeholder")} className={inputCls} />
      )}
      {nodeType === "action" && (() => {
        const known = ACTION_CATALOG.find((a) => a.tool === d.tool_name);
        const params = d.params || {};
        const setParam = (k: string, v: string) => set({ params: { ...params, [k]: v } });
        return (
          <div className="space-y-1">
            <select
              value={known ? d.tool_name : (d.tool_name ? "__custom" : "")}
              onChange={(e) => {
                const v = e.target.value;
                if (v === "__custom") { set({ tool_name: d.tool_name || "", params: {} }); return; }
                const def = ACTION_CATALOG.find((a) => a.tool === v);
                const seeded: Record<string, string> = {};
                def?.fields.forEach((f) => { seeded[f.key] = params[f.key] || ""; });
                set({ tool_name: v, params: seeded });
              }}
              className={inputCls}
            >
              <option value="">{t("action_pick")}</option>
              {ACTION_CATALOG.map((a) => (
                <option key={a.tool} value={a.tool}>{a.labelKey ? t(a.labelKey) : a.label}</option>
              ))}
              <option value="__custom">{t("action_custom")}</option>
            </select>

            {!known && (
              <input value={d.tool_name || ""} onChange={(e) => set({ tool_name: e.target.value })}
                placeholder={t("action_tool_placeholder")} className={inputCls} />
            )}

            {known?.fields.map((f) => {
              const fieldLabel = t(f.labelKey);
              const ph = f.placeholderKey ? t(f.placeholderKey) : f.placeholder;
              return (
                <input key={f.key} value={params[f.key] || ""}
                  onChange={(e) => setParam(f.key, e.target.value)}
                  placeholder={ph ? `${fieldLabel} · ${ph}` : fieldLabel}
                  className={inputCls} />
              );
            })}
            {hint("action_input_hint")}
          </div>
        );
      })()}
      {nodeType === "condition" && (
        <div className="space-y-1">
          <select value={d.op || "contains"} onChange={(e) => set({ op: e.target.value })}
            className={inputCls}>
            <option value="contains">{t("op_contains")}</option>
            <option value="not_contains">{t("op_not_contains")}</option>
            <option value="equals">{t("op_equals")}</option>
            <option value="regex">{t("op_regex")}</option>
            <option value="gt">{t("op_gt")}</option>
            <option value="lt">{t("op_lt")}</option>
            <option value="is_empty">{t("op_is_empty")}</option>
          </select>
          {d.op !== "is_empty" && (
            <input value={d.contains || ""} onChange={(e) => set({ contains: e.target.value })}
              placeholder={t("condition_contains_placeholder")} className={inputCls} />
          )}
          <div className="flex justify-between text-[10px] text-brain-300 px-0.5">
            <span className="text-emerald-300">{t("branch_yes")}</span>
            <span className="text-red-300">{t("branch_no")}</span>
          </div>
        </div>
      )}

      {nodeType === "document" && (
        <div className="space-y-1">
          <select value={d.doc_kind || "kp"} onChange={(e) => set({ doc_kind: e.target.value })}
            className={inputCls}>
            <option value="kp">{t("doc_kind_kp")}</option>
            <option value="contract">{t("doc_kind_contract")}</option>
            <option value="card">{t("doc_kind_card")}</option>
            <option value="free">{t("doc_kind_free")}</option>
          </select>
          <textarea value={d.custom_prompt || ""} onChange={(e) => set({ custom_prompt: e.target.value })}
            placeholder={t("document_prompt_placeholder")}
            className={`${inputCls} min-h-[40px] resize-none`} />
          <select value={d.render || ""} onChange={(e) => set({ render: e.target.value })}
            title={t("document_render_title")} className={inputCls}>
            <option value="">{t("document_render_none")}</option>
            <option value="pdf">{t("document_render_pdf")}</option>
            <option value="docx">{t("document_render_docx")}</option>
          </select>
          {hint("document_hint")}
        </div>
      )}
      {nodeType === "crm_data" && (
        <div className="space-y-1">
          <input value={d.query || ""} onChange={(e) => set({ query: e.target.value })}
            placeholder={t("crm_query_placeholder")} className={inputCls} />
          <DatasetPicker value={d.dataset_id || ""} valueTitle={d.dataset_title}
            onChange={(id, title) => set({ dataset_id: id, dataset_title: title })} />
          {hint("crm_data_hint")}
        </div>
      )}
      {nodeType === "web_search" && (
        <div className="space-y-1">
          <input value={d.query || ""} onChange={(e) => set({ query: e.target.value })}
            placeholder={t("web_query_placeholder")} className={inputCls} />
          {hint("web_search_hint")}
        </div>
      )}
      {nodeType === "report_xlsx" && (
        <div className="space-y-1">
          <input value={d.title || ""} onChange={(e) => set({ title: e.target.value })}
            placeholder={t("xlsx_title_placeholder")} className={inputCls} />
          <textarea value={d.instruction || ""} onChange={(e) => set({ instruction: e.target.value })}
            placeholder={t("xlsx_instruction_placeholder")}
            className={`${inputCls} min-h-[40px] resize-none`} />
          {hint("report_xlsx_hint")}
        </div>
      )}
      {nodeType === "translate" && (
        <div className="space-y-1">
          <select value={d.target_lang || "en"} onChange={(e) => set({ target_lang: e.target.value })}
            className={inputCls}>
            {TRANSLATE_LANGS.map((code) => (
              <option key={code} value={code}>{t(`lang_${code}`)}</option>
            ))}
          </select>
          {hint("translate_hint")}
        </div>
      )}
      {nodeType === "doc_edit" && (
        <div className="space-y-1">
          <textarea value={d.instruction || ""} onChange={(e) => set({ instruction: e.target.value })}
            placeholder={t("doc_edit_placeholder")}
            className={`${inputCls} min-h-[48px] resize-none`} />
          {hint("doc_edit_hint")}
        </div>
      )}
      {nodeType === "coding_agent" && (
        <div className="space-y-1">
          <select value={d.mode || "document"} onChange={(e) => set({ mode: e.target.value })}
            title={tt("coding_agent_mode_title")} className={inputCls}>
            <option value="document">{tt("coding_agent_mode_document")}</option>
            <option value="code">{tt("coding_agent_mode_code")}</option>
          </select>
          {(d.mode || "document") === "code" && (
            <>
              <select value={d.agent || "claude"} onChange={(e) => set({ agent: e.target.value })}
                className={inputCls}>
                {CODING_AGENTS.map((a) => (
                  <option key={a} value={a}>{a}</option>
                ))}
              </select>
              <input value={d.repo_path || ""} onChange={(e) => set({ repo_path: e.target.value })}
                placeholder={tt("coding_agent_repo_placeholder")} className={inputCls} />
            </>
          )}
          {hint("coding_agent_hint")}
        </div>
      )}
      {nodeType === "crm_write" && (
        <div className="space-y-1">
          <select value={d.provider || "amocrm"} onChange={(e) => set({ provider: e.target.value })}
            title={t("crm_write_provider_title")} className={inputCls}>
            {CRM_WRITE_PROVIDERS.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
          <select value={d.op || "create"} onChange={(e) => set({ op: e.target.value })}
            className={inputCls}>
            <option value="create">{t("crm_write_op_create")}</option>
            <option value="note">{t("crm_write_op_note")}</option>
          </select>
          {(d.op || "create") === "create" ? (
            <>
              <input value={d.title || ""} onChange={(e) => set({ title: e.target.value })}
                placeholder={t("crm_write_name_placeholder")} className={inputCls} />
              <input value={d.value || ""} onChange={(e) => set({ value: e.target.value })}
                placeholder={t("crm_write_value_placeholder")} className={inputCls} />
            </>
          ) : (
            <>
              <input value={d.entity_id || ""} onChange={(e) => set({ entity_id: e.target.value })}
                placeholder={t("crm_write_entity_placeholder")} className={inputCls} />
              <textarea value={d.note_text || ""} onChange={(e) => set({ note_text: e.target.value })}
                placeholder={t("crm_write_note_placeholder")}
                className={`${inputCls} min-h-[36px] resize-none`} />
            </>
          )}
          {hint("crm_write_hint")}
        </div>
      )}
      {nodeType === "meeting_data" && (
        <div className="space-y-1">
          <select value={d.kind || "report"} onChange={(e) => set({ kind: e.target.value })}
            className={inputCls}>
            {MEETING_KINDS.map((k) => (
              <option key={k} value={k}>{t(`meeting_kind_${k}`)}</option>
            ))}
          </select>
          <MeetingPicker value={d.meeting_id || ""} valueTitle={d.meeting_title}
            onChange={(id, title) => set({ meeting_id: id, meeting_title: title })} />
          {hint("meeting_data_hint")}
        </div>
      )}
      {nodeType === "meeting_share" && (
        <div className="space-y-1">
          <select value={d.share_kind || "link"} onChange={(e) => set({ share_kind: e.target.value })}
            className={inputCls}>
            <option value="link">{t("share_kind_link")}</option>
            <option value="grant">{t("share_kind_grant")}</option>
          </select>
          {(d.share_kind || "link") === "grant" ? (
            <>
              <input value={d.emails || ""} onChange={(e) => set({ emails: e.target.value })}
                placeholder={t("share_emails_placeholder")} className={inputCls} />
              <select value={d.permission_type || "read"} onChange={(e) => set({ permission_type: e.target.value })}
                className={inputCls}>
                <option value="read">{t("perm_read")}</option>
                <option value="write">{t("perm_write")}</option>
                <option value="admin">{t("perm_admin")}</option>
              </select>
            </>
          ) : (
            <>
              <select value={d.access_level || "view"} onChange={(e) => set({ access_level: e.target.value })}
                className={inputCls}>
                <option value="view">{t("share_access_view")}</option>
                <option value="comment">{t("share_access_comment")}</option>
              </select>
              <input type="number" min={0} value={d.expires_in_days ?? 7}
                onChange={(e) => set({ expires_in_days: e.target.value ? parseInt(e.target.value, 10) : undefined })}
                placeholder={t("share_expires_placeholder")}
                title={t("share_expires_title")}
                className={inputCls} />
              <input value={d.password || ""} onChange={(e) => set({ password: e.target.value })}
                placeholder={t("share_password_placeholder")} className={inputCls} />
            </>
          )}
          <MeetingPicker value={d.meeting_id || ""} valueTitle={d.meeting_title}
            onChange={(id, title) => set({ meeting_id: id, meeting_title: title })} />
          {hint("meeting_share_hint")}
        </div>
      )}
      {nodeType === "wait_reply" && (
        <div className="space-y-1">
          <RecipientInput kind="telegram" value={d.chat_id || ""}
            onChange={(v) => set({ chat_id: v })}
            placeholder={t("notify_chat_id_placeholder")}
            title={t("notify_chat_id_title")}
            className={inputCls} />
          <textarea value={d.text || ""} onChange={(e) => set({ text: e.target.value })}
            placeholder={t("wait_reply_message_placeholder")}
            className={`${inputCls} min-h-[50px] resize-none`} />
          <input type="number" min={0} value={d.timeout_min ?? ""}
            onChange={(e) => set({ timeout_min: e.target.value ? Math.max(0, parseInt(e.target.value, 10)) : 0 })}
            placeholder={t("wait_reply_timeout_placeholder")}
            title={t("wait_reply_timeout_title")}
            className={inputCls} />
          {hint("wait_reply_hint")}
        </div>
      )}

      {nodeType === "condition" ? (
        <>
          <Handle type="source" position={Position.Right} id="true" style={{ top: "38%" }} />
          <Handle type="source" position={Position.Right} id="false" style={{ top: "72%" }} />
        </>
      ) : (
        <Handle type="source" position={Position.Right} id="out" data-handletype="text" />
      )}
    </BaseNode>
  );
}
