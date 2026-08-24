import { Node, Edge } from "@xyflow/react";

// Node Types
export type NodeType =
  | "imageInput"
  | "annotation"
  | "prompt"
  | "textCombiner"
  | "nanoBanana"
  | "llmGenerate"
  | "output"
  | "note"       // поясняющий текст-блок на холсте (без портов, не исполняется)
  // Процесс-блоки (режим «Процессы», прогон на сервере — process_engine):
  | "trigger"
  | "ask_brain"
  | "report"
  | "notify"
  | "task"
  | "action"
  | "generate"   // серверная генерация текста (креатив внутри автоматизации)
  | "condition"
  | "wait_reply" // человек-в-цикле: отправить в Telegram и дождаться ответа человека
  | "meeting_data" // тянет артефакт встречи (саммари/задачи/транскрипт/повестка/отчёт)
  | "meeting_share" // создаёт публичную ссылку на встречу (MeetFlow, сторонний вход)
  | "document"    // КП/договор/карточка через реальный модуль документов
  | "crm_data"    // контекст из CRM/датасета (клиент, цены)
  | "web_search"  // веб-поиск (сайт клиента / рынок)
  | "report_xlsx" // нативный Excel-отчёт из данных предыдущих шагов
  | "translate"   // перевод текста/документа на целевой язык
  | "doc_edit"    // правка документа командой («убери раздел», «короче»)
  | "crm_write"   // запись в CRM (создать сделку/лид, комментарий) — env-гейт
  | "coding_agent" // исполнитель: документ LLM или CLI-агент (claude/codex/grok…)
  | "infographic" // богатая инфографика встречи: image-модель рисует, текст дублирует факты
  | "audio";       // озвучка отчёта (TTS): текст → аудио для доставки

// Данные процесс-блоков (общая структура; поля используются по типу узла).
export interface ProcessNodeData extends BaseNodeData {
  payload?: string;      // trigger
  trigger_type?: string; // trigger: manual | schedule | event
  interval_min?: number; // trigger (schedule, kind=interval): интервал в минутах
  interval_unit?: string; // trigger (schedule, interval): единица ввода "min"|"hour" (UI-only; хранится interval_min)
  schedule_kind?: string; // trigger (schedule): interval | daily | weekly
  daily_time?: string;   // trigger (schedule, daily/weekly): "HH:MM" (UTC)
  weekday?: number;      // trigger (schedule, kind=weekly): 0=Пн .. 6=Вс
  event_type?: string;   // trigger (event): meeting_ended | task_overdue | kpi_changed | …
  enabled?: boolean;     // trigger (event): автоматизация включена (false = сборка/тест, не стреляет)
  folder_ids?: string[]; // trigger (event=meeting_ended): фильтр по папкам встреч (пусто = любая)
  prompt?: string;       // ask_brain
  report_type?: string;  // report
  text?: string;         // notify
  channel?: string;      // notify (telegram|email)
  subject?: string;      // notify
  chat_id?: string;      // notify: явный адресат(ы) Telegram — группа/канал/личка (через запятую)
  title?: string;        // task
  contains?: string;     // condition (предикат: upstream содержит …)
  op?: string;           // condition: оператор (contains|equals|regex|gt|lt|…)
  tool_name?: string;    // action: имя инструмента интеграции
  params?: Record<string, string>; // action: параметры инструмента
  message?: string;      // wait_reply: текст вопроса (fallback — text)
  timeout_min?: number;  // wait_reply: сколько минут ждать ответа (0 = без лимита)
  kind?: string;         // meeting_data: какой артефакт (summary|tasks|decisions|participants|transcript|agenda|report)
  meeting_id?: string;   // meeting_data / meeting_share: явная встреча (пусто = встреча из триггера)
  meeting_title?: string; // meeting_data / meeting_share: название выбранной встречи (для карточки узла; бэкенд читает только meeting_id)
  share_kind?: string;   // meeting_share: link (публичная ссылка) | grant (доступ по email)
  access_level?: string; // meeting_share link: view|comment
  expires_in_days?: number; // meeting_share link: срок ссылки в днях (пусто = бессрочно)
  max_views?: number;    // meeting_share link: лимит открытий ссылки
  allowed_domains?: string; // meeting_share link: домены-ограничение (через запятую)
  password?: string;     // meeting_share link: пароль на ссылку
  emails?: string;       // meeting_share grant: email(ы) получателей (через запятую)
  permission_type?: string; // meeting_share grant: read|write|admin
  doc_kind?: string;     // document: kp|contract|card|free
  custom_prompt?: string; // document: пожелания к формату/тону
  render?: string;       // document: ""|pdf|docx — рендерить файл-вложение
  query?: string;        // crm_data / web_search: запрос (пусто = из входа)
  dataset_id?: string;   // crm_data: конкретный датасет (пусто = авто-подбор)
  dataset_title?: string; // crm_data: название выбранного датасета (для карточки узла; бэкенд читает только dataset_id)
  max_results?: number;  // web_search: сколько результатов
  instruction?: string;  // report_xlsx: пожелание к таблице; doc_edit: что изменить
  target_lang?: string;  // translate: код целевого языка (en|ru|de|…)
  lang?: string;         // trigger: язык вывода доски (auto|ru|en) — расписания/события берут отсюда
  mode?: string;         // coding_agent: document|code
  agent?: string;        // coding_agent: claude|codex|grok|qwen|cursor|свой
  repo_path?: string;    // coding_agent(code): репозиторий; пусто = artifact
  provider?: string;     // crm_write: amocrm|bitrix24|hubspot|pipedrive
  value?: string;        // crm_write (create): сумма сделки
  entity_id?: string;    // crm_write (note): ID записи для комментария
  note_text?: string;    // crm_write (note): текст комментария (пусто = из входа)
}

// Aspect Ratios (supported by both Nano Banana and Nano Banana Pro)
export type AspectRatio =
  | "1:1"
  | "2:3"
  | "3:2"
  | "3:4"
  | "4:3"
  | "4:5"
  | "5:4"
  | "9:16"
  | "16:9"
  | "21:9";

// Resolution Options (only supported by Nano Banana Pro)
export type Resolution = "1K" | "2K" | "4K";

// Size Options for GPT Image (OpenAI)
export type GPTImageSize = "1024x1024" | "1024x1536" | "1536x1024" | "auto";

// Quality Options for GPT Image (OpenAI)
export type GPTImageQuality = "low" | "medium" | "high";

// Image Generation Model Options
// OpenAI models: gpt-image-1.5 (latest), gpt-image-1, gpt-image-1-mini, dall-e-3, dall-e-2
export type ModelType = "nano-banana" | "nano-banana-pro" | "gpt-image-1.5" | "gpt-image-1" | "gpt-image-1-mini" | "dall-e-3"
  | "seedream-5-pro" | "seedream-5-lite"
  | "reve-2.1"
  | "veo-3.1-fast"
  | "veo-3.1";

// LLM Provider Options
export type LLMProvider =
  | "google"
  | "openai"
  | "anthropic"
  | "deepseek"
  | "qwen"
  | "xai"
  | "kimi";

// LLM Model Options. НОВЫЕ модели — в начале (актуальные); старые значения
// СОХРАНЕНЫ ниже для обратной совместимости (доски, сохранённые с ними, не
// ломаются), хотя в выпадашке узла показываем уже новый набор.
export type LLMModelType =
  // Google
  | "gemini-3.1-pro"
  | "gemini-omni-flash-preview"
  | "gemini-3.6-flash"
  | "gemini-3.5-flash"
  | "gemini-3.5-flash-lite"
  | "gemini-3.1-flash-lite"
  // OpenAI
  | "gpt-5.6-sol"
  | "gpt-5.6-terra"
  | "gpt-5.6-luna"
  | "gpt-5.1"
  // Anthropic (Claude) — нативный Messages API
  | "claude-sonnet-5"
  | "claude-sonnet-4-5"
  | "claude-haiku-4-5"
  | "claude-opus-4-7"
  | "opus-4.8"
  | "claude-fable-5"
  // DeepSeek / Qwen / xAI — OpenAI-совместимый chat/completions
  | "deepseek-v4-pro"
  | "deepseek-v4-flash"
  // снята с обслуживания 08.2026; тип оставлен — id лежит на сохранённых
  // досках, маппинг в route.ts ведёт его на живую модель
  | "deepseek-v3.2"
  | "qwen3.8-max"
  | "qwen3.7-max"
  | "qwen3.6-plus"
  | "grok-4.5"
  | "grok-4.2"
  // Moonshot (Kimi)
  | "kimi-k3"
  // — legacy (обратная совместимость сохранённых досок) —
  | "gemini-2.5-flash"
  | "gemini-3-pro-preview"
  | "gpt-4.1-mini"
  | "gpt-4.1-nano"
  | "deepseek-chat"
  | "deepseek-reasoner"
  | "qwen-max"
  | "qwen-plus"
  | "grok-4"
  | "grok-3-mini";

// Node Status
export type NodeStatus = "idle" | "loading" | "complete" | "error";

// Base node data - using Record to satisfy React Flow's type constraints
export interface BaseNodeData extends Record<string, unknown> {
  label?: string;
}

// Image Input Node Data
export interface ImageInputNodeData extends BaseNodeData {
  image: string | null;
  filename: string | null;
  dimensions: { width: number; height: number } | null;
}

// Annotation Shape Types
export type ShapeType = "rectangle" | "circle" | "arrow" | "freehand" | "text";

export interface BaseShape {
  id: string;
  type: ShapeType;
  x: number;
  y: number;
  stroke: string;
  strokeWidth: number;
  opacity: number;
}

export interface RectangleShape extends BaseShape {
  type: "rectangle";
  width: number;
  height: number;
  fill: string | null;
}

export interface CircleShape extends BaseShape {
  type: "circle";
  radiusX: number;
  radiusY: number;
  fill: string | null;
}

export interface ArrowShape extends BaseShape {
  type: "arrow";
  points: number[];
}

export interface FreehandShape extends BaseShape {
  type: "freehand";
  points: number[];
}

export interface TextShape extends BaseShape {
  type: "text";
  text: string;
  fontSize: number;
  fill: string;
}

export type AnnotationShape =
  | RectangleShape
  | CircleShape
  | ArrowShape
  | FreehandShape
  | TextShape;

// Annotation Node Data
export interface AnnotationNodeData extends BaseNodeData {
  sourceImage: string | null;
  annotations: AnnotationShape[];
  outputImage: string | null;
}

// Prompt Node Data
export interface PromptNodeData extends BaseNodeData {
  prompt: string;
}

// Text Combiner Node Data - combines multiple text inputs using a template
export interface TextCombinerNodeData extends BaseNodeData {
  template: string; // Template with placeholders like {{input1}}, {{input2}}
  inputs: Record<string, string | null>; // Connected text inputs by handle id
  outputText: string | null; // Combined result
}

// Image History Item (for tracking generated images)
export interface ImageHistoryItem {
  id: string;
  image: string; // Base64 data URL
  timestamp: number; // For display & sorting
  prompt: string; // The prompt used
  aspectRatio: AspectRatio;
  model: ModelType;
}

// Nano Banana Node Data (Image Generation)
export interface NanoBananaNodeData extends BaseNodeData {
  inputImages: string[]; // Now supports multiple images
  inputPrompt: string | null;
  outputImage: string | null;
  outputVideo?: string | null; // Veo: data-URL видео (терминальный выход)
  aspectRatio: AspectRatio;
  resolution: Resolution; // Only used by Nano Banana Pro
  model: ModelType;
  useGoogleSearch: boolean; // Only available for Nano Banana Pro
  // GPT Image specific options
  gptImageSize: GPTImageSize; // Only used by gpt-image-1
  gptImageQuality: GPTImageQuality; // Only used by gpt-image-1
  status: NodeStatus;
  error: string | null;
}

// LLM Generate Node Data (Text Generation)
export interface LLMGenerateNodeData extends BaseNodeData {
  inputPrompt: string | null;
  outputText: string | null;
  provider: LLMProvider;
  model: LLMModelType;
  temperature: number;
  maxTokens: number;
  status: NodeStatus;
  error: string | null;
}

// Output Node Data
export interface OutputNodeData extends BaseNodeData {
  image: string | null;
  text?: string | null; // текстовый результат процесс-прогона (не картинка)
}

// Note Node Data — поясняющий текст-блок на холсте (без портов).
// Объясняет схему пользователю: что делает блок/ветка, зачем, как пользоваться.
export interface NoteNodeData extends BaseNodeData {
  text: string;
  color?: string;   // тон стикера (по умолчанию — нейтральный)
}

// Инфографика встречи: вход — текст (структура встречи), выход — картинка.
// Рисует image-модель по плотному промпту; ключевые факты дублируются текстом.
export interface InfographicNodeData extends BaseNodeData {
  format?: string;       // формат визуального отчёта: infographic | organism | …
  audience?: string;     // аудитория: public (команде) | private (лично) — политика эмоций
  render?: "model" | "code"; // чем рисовать: image-модель или код (без LLM-картинки)
  model?: string;        // nano-banana | gpt-image-1 | dall-e-3 (для render=model)
  style?: string;        // стиль-пресет: auto | buzan | poster | neon | editorial
  lang?: string;         // язык отчёта: auto (язык пользователя) | ru | en
  recipient?: string;    // персонализация личной версии: имя/роль получателя
  use_brand?: boolean;   // применять фирменный профиль тенанта (по умолчанию да)
  thread_scope?: string; // серия для сериальности: сюжеты тянутся между отчётами
  image?: string | null; // превью результата (если пришло)
}

// Озвучка (TTS): вход — текст, выход — аудио-файл (в data — метки провайдера/голоса).
export interface AudioNodeData extends BaseNodeData {
  provider?: string;  // openai | elevenlabs
  voice?: string;     // голос (openai: alloy…; elevenlabs: voice_id)
}

// Union of all node data types
export type WorkflowNodeData =
  | ImageInputNodeData
  | AnnotationNodeData
  | PromptNodeData
  | TextCombinerNodeData
  | NanoBananaNodeData
  | LLMGenerateNodeData
  | OutputNodeData
  | NoteNodeData
  | InfographicNodeData
  | AudioNodeData
  | ProcessNodeData;

// Workflow Node with typed data
export type WorkflowNode = Node<WorkflowNodeData, NodeType>;

// Workflow Edge Data
export interface WorkflowEdgeData extends Record<string, unknown> {
  hasPause?: boolean;
  offsetX?: number;
  offsetY?: number;
}

// Workflow Edge
export type WorkflowEdge = Edge<WorkflowEdgeData>;

// API Request/Response types for Image Generation
export interface GenerateRequest {
  images: string[]; // Now supports multiple images
  prompt: string;
  aspectRatio?: AspectRatio;
  resolution?: Resolution; // Only for Nano Banana Pro
  model?: ModelType;
  useGoogleSearch?: boolean; // Only for Nano Banana Pro
  // GPT Image specific options
  gptImageSize?: GPTImageSize; // Only for gpt-image-1.5
  gptImageQuality?: GPTImageQuality; // Only for    gpt-image-1.5
}

export interface GenerateResponse {
  success: boolean;
  image?: string;
  error?: string;
}

// API Request/Response types for LLM Text Generation
export interface LLMGenerateRequest {
  prompt: string;
  provider: LLMProvider;
  model: LLMModelType;
  temperature?: number;
  maxTokens?: number;
}

export interface LLMGenerateResponse {
  success: boolean;
  text?: string;
  error?: string;
}

// Tool Types for annotation
export type ToolType = "select" | "rectangle" | "circle" | "arrow" | "freehand" | "text";

// Tool Options
export interface ToolOptions {
  strokeColor: string;
  strokeWidth: number;
  fillColor: string | null;
  fontSize: number;
  opacity: number;
}


