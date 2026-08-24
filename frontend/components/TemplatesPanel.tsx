"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { useTranslations, useLocale } from "next-intl";
import FillDocumentsPanel from "./FillDocumentsPanel";
import SafeHtml from "./SafeHtml";
import {
  FileText,
  Plus, 
  Search, 
  Play, 
  Edit2, 
  Trash2, 
  Clock, 
  TrendingUp,
  ChevronRight,
  Sparkles,
  BookOpen,
  Briefcase,
  Code,
  BarChart3,
  X,
  Check,
  AlertCircle,
  Download,
  RefreshCw,
  Filter,
  Users,
  CheckCircle,
  Loader2,
  Eye,
  List,
  FileCode,
  Folder,
  FolderOpen
} from "lucide-react";

// Типы
interface TemplateStep {
  step_id: string;
  agent: string;
  action: string;
  params: Record<string, any>;
  output_variable: string;
  order: number;
}

interface WorkflowTemplate {
  template_id: string;
  name: string;
  description: string;
  category: string;
  visibility: string;
  trigger_patterns: string[];
  steps: TemplateStep[];
  output_format: string;
  usage_count: number;
  last_used_at: string | null;
  tags: string[];
  icon: string;
  color: string;
  created_by: string | null;
  created_at: string;
}

interface ExtractedTemplate {
  template_id: string;
  template_type: string;
  title: string;
  description: string;
  items: string[];
  use_case: string;
  category: string;
  source_meeting_id: string | null;
  extracted_at: string;
  usage_count: number;
}

interface Regulation {
  regulation_id: string;
  regulation_type: string;
  title: string;
  description: string;
  scope: string;
  mandatory: boolean;
  enforcement: string;
  conditions?: string[];
  exceptions?: string[];
  source_meeting_id: string | null;
  extracted_at: string;
}

// Иконки категорий
const categoryIcons: Record<string, React.ReactNode> = {
  analysis: <BarChart3 className="w-4 h-4" />,
  generation: <Sparkles className="w-4 h-4" />,
  report: <FileText className="w-4 h-4" />,
  extraction: <BookOpen className="w-4 h-4" />,
  automation: <Code className="w-4 h-4" />,
  integration: <Briefcase className="w-4 h-4" />,
};

// Цвета категорий
const categoryColors: Record<string, string> = {
  analysis: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  generation: "bg-purple-500/10 text-purple-400 border-purple-500/20",
  report: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  extraction: "bg-blue-500/10 text-blue-400 border-blue-500/20",
  automation: "bg-rose-500/10 text-rose-400 border-rose-500/20",
  integration: "bg-cyan-500/10 text-cyan-400 border-cyan-500/20",
};

// Типы для сгенерированных документов
interface GeneratedDocument {
  document_id: string;
  title: string;
  document_type: string;
  version: string;
  created_at: string;
  updated_at: string;
  summary: string;
  topic: string;
  keywords: string[];
  source_meetings: string[];
  status: string;
  word_count: number;
  content_preview?: string;
  content_markdown?: string;
  content_html?: string;
  sections?: any[];
  table_of_contents?: any[];
  authors?: string[];
  folder?: string;
}

const buildDocumentTypeConfig = (t: (k: string) => string): Record<string, { icon: React.ReactNode; color: string; label: string }> => ({
  regulation: { icon: <FileText className="w-4 h-4" />, color: "blue", label: t("doctype_regulation") },
  sop: { icon: <List className="w-4 h-4" />, color: "green", label: "SOP" },
  checklist: { icon: <CheckCircle className="w-4 h-4" />, color: "purple", label: t("doctype_checklist") },
  specification: { icon: <FileCode className="w-4 h-4" />, color: "amber", label: t("doctype_specification") },
  guideline: { icon: <BookOpen className="w-4 h-4" />, color: "cyan", label: t("doctype_guideline") },
  // Результаты из других потоков (медиаплан, выполненное ТЗ) — библиотека
  // результатов: попадают в тот же список «Сгенерированные».
  mediaplan: { icon: <List className="w-4 h-4" />, color: "orange", label: t("doctype_mediaplan") },
  task_result: { icon: <CheckCircle className="w-4 h-4" />, color: "green", label: t("doctype_task_result") },
  result: { icon: <FileText className="w-4 h-4" />, color: "blue", label: t("doctype_result") },
});

export default function TemplatesPanel() {
  const t = useTranslations("templates_panel");
  const locale = useLocale();
  const documentTypeConfig = buildDocumentTypeConfig(t);
  // «Workflow»-таб убран как мёртвый дубль вкладки «Автоматизации» (executor
  // был заглушкой: llm_router=None, 5 из 7 агентов — несуществующие классы).
  const [activeTab, setActiveTab] = useState<"workflows" | "extracted" | "regulations" | "generated" | "fill">("generated");
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
  const [extractedTemplates, setExtractedTemplates] = useState<ExtractedTemplate[]>([]);
  const [regulations, setRegulations] = useState<Regulation[]>([]);
  const [generatedDocs, setGeneratedDocs] = useState<GeneratedDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [selectedTemplate, setSelectedTemplate] = useState<WorkflowTemplate | null>(null);
  const [selectedRegulation, setSelectedRegulation] = useState<Regulation | null>(null);
  const [selectedExtracted, setSelectedExtracted] = useState<ExtractedTemplate | null>(null);
  const [selectedGenerated, setSelectedGenerated] = useState<GeneratedDocument | null>(null);
  const [executeQuery, setExecuteQuery] = useState("");
  const [executing, setExecuting] = useState(false);
  const [executionResult, setExecutionResult] = useState<any>(null);
  const [showExtractModal, setShowExtractModal] = useState(false);
  const [extractMeetingId, setExtractMeetingId] = useState("");
  const [extractTranscript, setExtractTranscript] = useState("");
  const [extracting, setExtracting] = useState(false);
  const [extractResult, setExtractResult] = useState<any>(null);
  const [discovering, setDiscovering] = useState(false);
  const [discoveryResult, setDiscoveryResult] = useState<any>(null);
  
  // Новые состояния для улучшенного извлечения
  const [availableMeetings, setAvailableMeetings] = useState<any[]>([]);
  const [selectedMeetingIds, setSelectedMeetingIds] = useState<string[]>([]);
  const [desiredRegulation, setDesiredRegulation] = useState("");
  const [extractModelTier, setExtractModelTier] = useState<"standard" | "premium">("standard");
  const [meetingsLoading, setMeetingsLoading] = useState(false);
  const [meetingSearchQuery, setMeetingSearchQuery] = useState("");
  // Фильтр встреч по проекту/папке (жалоба: «нельзя выбрать проекты, папки»)
  const [projects, setProjects] = useState<Array<{ project_id: string; name: string }>>([]);
  const [folders, setFolders] = useState<Array<{ id: string; name: string; project_id: string }>>([]);
  const [filterProject, setFilterProject] = useState("");
  const [filterFolder, setFilterFolder] = useState("");
  // Папки сгенерированных документов — плоские ярлыки (не папки встреч MeetFlow)
  const [docFolderFilter, setDocFolderFilter] = useState("");
  const [docFolderMenuFor, setDocFolderMenuFor] = useState<string | null>(null);

  // Auth helper - получаем токен для авторизации
  const getAuthHeader = useCallback((): Record<string, string> => {
    if (typeof window !== 'undefined') {
      // Пробуем оба варианта ключа токена
      const token = localStorage.getItem('tessent_access_token') || localStorage.getItem('access_token');
      if (token) {
        return { Authorization: `Bearer ${token}` };
      }
    }
    return {};
  }, []);

  // Получаем user_id из localStorage (с fallback на тестового пользователя)
  const getUserId = useCallback(() => {
    if (typeof window !== 'undefined') {
      const storedUserId = localStorage.getItem('tessent_user_id');
      if (storedUserId) {
        return storedUserId;
      }
      // Fallback: пробуем получить из tessent_user
      const storedUser = localStorage.getItem('tessent_user');
      if (storedUser) {
        try {
          const user = JSON.parse(storedUser);
          if (user.id) return user.id;
        } catch {}
      }
    }
    // Нет user_id в localStorage → НЕ шлём ?user_id= (бэкенд возьмёт из
    // токена). Раньше здесь был хардкод тестового UUID: он уходил query-
    // параметром вместе с валидным токеном реального юзера → бэк отвечал
    // «запрошен чужой user_id — отказ» и список приходил пустым.
    return '';
  }, []);

  // Загрузка данных
  useEffect(() => {
    loadData();
  }, [activeTab]);

  const loadData = async () => {
    setLoading(true);
    try {
      const authHeaders = getAuthHeader();
      const userId = getUserId();
      const userQuery = userId ? `?user_id=${userId}` : '';
      
      if (activeTab === "workflows") {
        const res = await fetch("/api/v1/templates", { headers: authHeaders });
        if (res.ok) {
          const data = await res.json();
          setTemplates(data.templates || []);
        }
      } else if (activeTab === "extracted") {
        const res = await fetch(`/api/v1/extracted-templates${userQuery}`, { headers: authHeaders });
        if (res.ok) {
          const data = await res.json();
          setExtractedTemplates(data.templates || []);
        }
      } else if (activeTab === "regulations") {
        const res = await fetch(`/api/v1/regulations${userQuery}`, { headers: authHeaders });
        if (res.ok) {
          const data = await res.json();
          setRegulations(data.regulations || []);
        }
      } else if (activeTab === "generated") {
        const res = await fetch(`/api/v1/generated-documents${userQuery}`, { headers: authHeaders });
        if (res.ok) {
          const data = await res.json();
          setGeneratedDocs(data.documents || []);
        }
      }
    } catch (error) {
      console.error("Failed to load data:", error);
    } finally {
      setLoading(false);
    }
  };

  // Обнаружение и генерация документов
  const discoverDocuments = async () => {
    setDiscovering(true);
    setDiscoveryResult(null);
    
    try {
      const authHeaders = getAuthHeader();
      const userId = getUserId();
      console.log("🔑 discoverDocuments userId:", userId, "authHeaders:", authHeaders);
      
      // Передаём user_id как query параметр (надёжнее чем через header)
      const url = userId 
        ? `/api/v1/generated-documents/discover?user_id=${userId}`
        : "/api/v1/generated-documents/discover";
      
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({ min_meetings: 1, force_regenerate: false })
      });
      
      const result = await res.json();
      setDiscoveryResult(result);
      
      if (result.success) {
        // Перезагрузить документы
        const docsUrl = userId 
          ? `/api/v1/generated-documents?user_id=${userId}`
          : "/api/v1/generated-documents";
        const docsRes = await fetch(docsUrl, { headers: authHeaders });
        if (docsRes.ok) {
          const data = await docsRes.json();
          setGeneratedDocs(data.documents || []);
        }
      }
    } catch (error) {
      console.error("Discovery failed:", error);
      setDiscoveryResult({ success: false, errors: [String(error)] });
    } finally {
      setDiscovering(false);
    }
  };

  // Загрузка полного документа. Раньше при !res.ok функция молча выходила —
  // клик по документу «ничего не делал»; а бэкенд при отсутствии отдавал
  // 200+{error}, который клался в стейт → открывалась пустая битая модалка.
  const loadFullDocument = async (documentId: string) => {
    try {
      const authHeaders = getAuthHeader();
      const userId = getUserId();
      const userQuery = userId ? `?user_id=${userId}` : '';
      const res = await fetch(`/api/v1/generated-documents/${documentId}${userQuery}`, { headers: authHeaders });
      const doc = res.ok ? await res.json() : null;
      if (!doc || doc.error || !doc.document_id) {
        setDiscoveryResult({ success: false, errors: [doc?.error || t("open_doc_failed", { status: res.status })] });
        return;
      }
      setSelectedGenerated(doc);
    } catch (error) {
      console.error("Failed to load document:", error);
      setDiscoveryResult({ success: false, errors: [String(error)] });
    }
  };

  // Экспорт документа
  const exportDocument = async (documentId: string, format: string) => {
    try {
      const authHeaders = getAuthHeader();
      const res = await fetch(`/api/v1/generated-documents/${documentId}/export?format=${format}`, {
        headers: authHeaders
      });
      
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `document.${format === "markdown" ? "md" : format}`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (error) {
      console.error("Export failed:", error);
    }
  };

  // Удаление документа
  const deleteGeneratedDoc = async (documentId: string) => {
    if (!confirm(t("confirm_delete_doc"))) return;
    
    try {
      const authHeaders = getAuthHeader();
      await fetch(`/api/v1/generated-documents/${documentId}`, {
        method: "DELETE",
        headers: authHeaders
      });
      setGeneratedDocs(prev => prev.filter(d => d.document_id !== documentId));
      if (selectedGenerated?.document_id === documentId) {
        setSelectedGenerated(null);
      }
    } catch (error) {
      console.error("Delete failed:", error);
    }
  };

  // Папка документа: плоский ярлык, POST /generated-documents/{id}/folder
  const docFolderNames = Array.from(new Set(
    generatedDocs.map(d => (d.folder || "").trim()).filter(Boolean)
  )).sort((a, b) => a.localeCompare(b, "ru"));

  const setDocumentFolder = async (documentId: string, folder: string) => {
    setDocFolderMenuFor(null);
    const prev = generatedDocs;
    setGeneratedDocs(list => list.map(d =>
      d.document_id === documentId ? { ...d, folder } : d));
    try {
      const authHeaders = getAuthHeader();
      const userId = getUserId();
      const userQuery = userId ? `?user_id=${userId}` : '';
      const res = await fetch(`/api/v1/generated-documents/${documentId}/folder${userQuery}`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ folder }),
      });
      if (!res.ok) setGeneratedDocs(prev);
    } catch (error) {
      console.error("Set folder failed:", error);
      setGeneratedDocs(prev);
    }
  };

  const promptNewDocFolder = (documentId: string) => {
    const name = window.prompt(t("doc_folder_prompt"))?.trim();
    if (name) setDocumentFolder(documentId, name.slice(0, 60));
    else setDocFolderMenuFor(null);
  };

  // Выполнение шаблона
  const executeTemplate = async (template: WorkflowTemplate) => {
    if (!executeQuery.trim()) return;
    
    setExecuting(true);
    setExecutionResult(null);
    
    try {
      const res = await fetch(`/api/v1/templates/${template.template_id}/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: executeQuery,
          input_params: {}
        })
      });
      
      const data = await res.json();
      setExecutionResult(data);
    } catch (error) {
      console.error("Execution failed:", error);
      setExecutionResult({ error: t("execution_error") });
    } finally {
      setExecuting(false);
    }
  };

  // Удаление шаблона
  const deleteTemplate = async (templateId: string) => {
    if (!confirm(t("confirm_delete_template"))) return;
    
    try {
      await fetch(`/api/v1/templates/${templateId}`, { method: "DELETE" });
      loadData();
    } catch (error) {
      console.error("Delete failed:", error);
    }
  };

  // Загрузка списка встреч для выбора (поиск/фильтры выполняются на бэкенде)
  const loadMeetingsForExtraction = useCallback(async (searchQuery?: string, projectId?: string, folderId?: string) => {
    setMeetingsLoading(true);
    try {
      const authHeaders = getAuthHeader();
      const userId = getUserId();
      const params = new URLSearchParams();
      if (userId) params.set('user_id', userId);
      const hasFilter = (searchQuery && searchQuery.trim()) || projectId || folderId;
      if (searchQuery && searchQuery.trim()) params.set('search', searchQuery.trim());
      if (projectId) params.set('project_id', projectId);
      if (folderId) params.set('folder_id', folderId);
      params.set('limit', hasFilter ? '100' : '10');

      const res = await fetch(`/api/v1/meetings-for-extraction?${params.toString()}`, {
        headers: authHeaders
      });
      const data = await res.json();
      setAvailableMeetings(data.meetings || []);
    } catch (error) {
      console.error("Failed to load meetings:", error);
    } finally {
      setMeetingsLoading(false);
    }
  }, [getAuthHeader, getUserId]);

  // Проекты и папки для фильтра (при открытии модала)
  useEffect(() => {
    if (!showExtractModal) return;
    (async () => {
      try {
        const res = await fetch('/api/v1/automations/projects-list', { headers: getAuthHeader() });
        if (res.ok) {
          const data = await res.json();
          setProjects(data.projects || []);
          setFolders(data.folders || []);
        }
      } catch { /* оставим пустым — фильтр просто не покажет опции */ }
    })();
  }, [showExtractModal, getAuthHeader]);

  // Загружаем встречи при открытии модала (последние 10)
  useEffect(() => {
    if (showExtractModal) {
      loadMeetingsForExtraction();
    }
  }, [showExtractModal, loadMeetingsForExtraction]);

  // Debounced загрузка встреч при изменении поиска/фильтров
  useEffect(() => {
    if (!showExtractModal) return;

    const timeoutId = setTimeout(() => {
      loadMeetingsForExtraction(meetingSearchQuery, filterProject, filterFolder);
    }, 300); // 300ms debounce

    return () => clearTimeout(timeoutId);
  }, [meetingSearchQuery, filterProject, filterFolder, showExtractModal, loadMeetingsForExtraction]);

  // Переключение выбора встречи
  const toggleMeetingSelection = (meetingId: string) => {
    setSelectedMeetingIds(prev => 
      prev.includes(meetingId) 
        ? prev.filter(id => id !== meetingId)
        : [...prev, meetingId]
    );
  };

  // Создание документа из выбранных встреч
  const extractFromMeeting = async () => {
    if (!extractTranscript.trim() && selectedMeetingIds.length === 0) return;
    
    setExtracting(true);
    setExtractResult(null);
    
    try {
      const authHeaders = getAuthHeader();
      const userId = getUserId();
      const userQuery = userId ? `?user_id=${userId}` : '';
      
      // Определяем тему документа
      const topic = desiredRegulation || t("default_topic");
      
      // Генерируем документ через DocumentGenerationSystem
      const docRes = await fetch(`/api/v1/generated-documents/generate${userQuery}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({
          topic: topic,
          document_type: "regulation",
          meeting_ids: selectedMeetingIds,
          transcript: extractTranscript.trim() || undefined,
          model_tier: extractModelTier,
        })
      });
      const docData = await docRes.json();
      
      if (docData.success && docData.document) {
        setExtractResult({
          success: true,
          document: docData.document,
          message: t("doc_created_msg", { title: docData.document.title })
        });
        
        // Обновляем список документов
        const docsUrl = userId 
          ? `/api/v1/generated-documents?user_id=${userId}`
          : "/api/v1/generated-documents";
        const docsRes = await fetch(docsUrl, { headers: authHeaders });
        if (docsRes.ok) {
          const data = await docsRes.json();
          setGeneratedDocs(data.documents || []);
        }
      } else {
      setExtractResult({
          error: docData.error || t("doc_create_failed"),
          details: docData 
        });
      }
      
      // Обновляем списки
      loadData();
      
    } catch (error) {
      console.error("Document generation failed:", error);
      setExtractResult({ error: t("doc_create_error") });
    } finally {
      setExtracting(false);
    }
  };

  // Фильтрация
  const filteredTemplates = templates.filter(t => {
    const matchesSearch = !searchQuery || 
      t.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      t.description.toLowerCase().includes(searchQuery.toLowerCase());
    const matchesCategory = !selectedCategory || t.category === selectedCategory;
    return matchesSearch && matchesCategory;
  });

  const filteredExtracted = extractedTemplates.filter(t => {
    return !searchQuery || 
      t.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
      t.description.toLowerCase().includes(searchQuery.toLowerCase());
  });

  const filteredRegulations = regulations.filter(r => {
    // null-guard: узлы графа могут прийти с пустым title/description
    // (частично сохранённые) — без guard .toLowerCase() ронял всю вкладку
    return !searchQuery ||
      (r.title || '').toLowerCase().includes(searchQuery.toLowerCase()) ||
      (r.description || '').toLowerCase().includes(searchQuery.toLowerCase());
  });

  // Встречи теперь фильтруются на бэкенде, используем availableMeetings напрямую

  return (
    <div className="h-full flex flex-col bg-brain-950">
      {/* Header */}
      <div className="border-b border-brain-700/30 bg-brain-900/50 backdrop-blur-sm p-4">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-xl bg-gradient-to-br from-purple-500/20 to-indigo-500/20 border border-purple-500/20">
              <FileText className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-white">{t("title")}</h2>
              <p className="text-xs text-brain-400">{t("subtitle")}</p>
            </div>
          </div>
          
          <button
            onClick={() => setShowExtractModal(true)}
            className="flex items-center gap-2 px-3 py-2 rounded-lg bg-purple-600/20 hover:bg-purple-600/30 border border-purple-500/30 text-purple-400 transition-colors text-sm"
          >
            <Sparkles className="w-4 h-4" />
            {t('extract_from_text')}
          </button>
        </div>
        
        {/* Tabs */}
        <div className="flex gap-1 flex-wrap">
          {[
            { id: "generated", label: t("tab_generated"), icon: <FileCode className="w-4 h-4" />, count: generatedDocs.length },
            { id: "regulations", label: t("tab_regulations"), icon: <FileText className="w-4 h-4" /> },
            { id: "extracted", label: t("tab_extracted"), icon: <BookOpen className="w-4 h-4" /> },
            { id: "fill", label: t("tab_fill"), icon: <FileText className="w-4 h-4" /> },
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as any)}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg transition-colors text-sm ${
                activeTab === tab.id
                  ? "bg-purple-600/20 text-purple-400 border border-purple-500/30"
                  : "text-brain-400 hover:text-white hover:bg-brain-800/50"
              }`}
            >
              {tab.icon}
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Search & Filters */}
      <div className="p-4 border-b border-brain-700/30">
        <div className="flex gap-3">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-brain-500" />
            <input
              type="text"
              placeholder={t("search_placeholder")}
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              className="w-full pl-9 pr-4 py-2 rounded-lg bg-brain-900/50 border border-brain-700/50 focus:border-purple-500/50 focus:ring-1 focus:ring-purple-500/20 outline-none transition-all text-white placeholder-brain-500 text-sm"
            />
          </div>
          
          {activeTab === "workflows" && (
            <div className="flex gap-1">
              {Object.entries(categoryIcons).map(([cat, icon]) => (
                <button
                  key={cat}
                  onClick={() => setSelectedCategory(selectedCategory === cat ? null : cat)}
                  className={`p-2 rounded-lg border transition-colors ${
                    selectedCategory === cat
                      ? categoryColors[cat]
                      : "border-brain-700/50 text-brain-500 hover:text-white hover:border-brain-600"
                  }`}
                  title={cat}
                >
                  {icon}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto p-4">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-purple-500"></div>
          </div>
        ) : (
          <>
            {/* Workflow Templates */}
            {activeTab === "workflows" && (
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                {filteredTemplates.map(template => (
                  <div
                    key={template.template_id}
                    className="group p-4 rounded-xl bg-brain-900/30 border border-brain-700/30 hover:border-brain-600/50 transition-all cursor-pointer"
                    onClick={() => setSelectedTemplate(template)}
                  >
                    <div className="flex items-start justify-between mb-3">
                      <div className="flex items-center gap-3">
                        <span className="text-2xl">{template.icon}</span>
                        <div>
                          <h3 className="font-medium text-white group-hover:text-purple-400 transition-colors">
                            {template.name}
                          </h3>
                          <span className={`text-xs px-2 py-0.5 rounded-full border ${categoryColors[template.category] || categoryColors.analysis}`}>
                            {template.category}
                          </span>
                        </div>
                      </div>
                      
                      <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        <button
                          onClick={e => { e.stopPropagation(); deleteTemplate(template.template_id); }}
                          className="p-1.5 rounded-lg hover:bg-brain-800 text-brain-500 hover:text-red-400"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </div>
                    
                    <p className="text-sm text-brain-400 mb-3 line-clamp-2">
                      {template.description}
                    </p>
                    
                    {/* Triggers */}
                    {template.trigger_patterns.length > 0 && (
                      <div className="mb-3">
                        <div className="text-xs text-brain-500 mb-1">{t("triggers_label")}</div>
                        <div className="flex flex-wrap gap-1">
                          {template.trigger_patterns.slice(0, 2).map((pattern, i) => (
                            <span key={i} className="text-xs px-2 py-0.5 rounded bg-brain-800 text-brain-400">
                              {pattern.replace(/\(.*\)/g, "*")}
                            </span>
                          ))}
                          {template.trigger_patterns.length > 2 && (
                            <span className="text-xs text-brain-500">
                              +{template.trigger_patterns.length - 2}
                            </span>
                          )}
                        </div>
                      </div>
                    )}
                    
                    {/* Stats */}
                    <div className="flex items-center gap-4 text-xs text-brain-500">
                      <div className="flex items-center gap-1">
                        <TrendingUp className="w-3 h-3" />
                        {t('usages_count', { count: template.usage_count })}
                      </div>
                      <div className="flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        {t('steps_count', { count: template.steps.length })}
                      </div>
                    </div>
                  </div>
                ))}
                
                {filteredTemplates.length === 0 && (
                  <div className="col-span-full text-center py-12 text-brain-500">
                    <FileText className="w-12 h-12 mx-auto mb-3 opacity-50" />
                    <p>{t("templates_not_found")}</p>
                  </div>
                )}
              </div>
            )}

            {/* Extracted Templates */}
            {activeTab === "extracted" && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {filteredExtracted.map(template => (
                  <div
                    key={template.template_id}
                    className="p-4 rounded-xl bg-brain-900/30 border border-brain-700/30 hover:border-brain-600/50 transition-all cursor-pointer"
                    onClick={() => setSelectedExtracted(template)}
                  >
                    <div className="flex items-start justify-between mb-3">
                      <div>
                        <span className={`text-xs px-2 py-0.5 rounded-full border ${
                          template.template_type === "checklist" ? "bg-green-500/10 text-green-400 border-green-500/20" :
                          template.template_type === "process" ? "bg-blue-500/10 text-blue-400 border-blue-500/20" :
                          template.template_type === "structure" ? "bg-purple-500/10 text-purple-400 border-purple-500/20" :
                          "bg-amber-500/10 text-amber-400 border-amber-500/20"
                        }`}>
                          {template.template_type}
                        </span>
                        <h3 className="font-medium text-white mt-2">{template.title}</h3>
                      </div>
                    </div>
                    
                    <p className="text-sm text-brain-400 mb-3">{template.description}</p>
                    
                   {/* Items */}
                   <div className="space-y-1 mb-3">
                     {(template.items || []).slice(0, 4).map((item, i) => (
                       <div key={i} className="flex items-start gap-2 text-sm">
                         <Check className="w-3 h-3 text-purple-400 mt-1 flex-shrink-0" />
                         <span className="text-brain-300">{item}</span>
                       </div>
                     ))}
                     {(template.items?.length || 0) > 4 && (
                       <div className="text-xs text-brain-500 pl-5">
                         {t('more_items_suffix', { count: (template.items?.length || 0) - 4 })}
                       </div>
                     )}
                   </div>
                    
                    {template.use_case && (
                      <div className="text-xs text-brain-500 bg-brain-800/50 rounded-lg p-2">
                        <span className="text-brain-400">{t("when_to_use_label")}</span> {template.use_case}
                      </div>
                    )}
                  </div>
                ))}
                
                {filteredExtracted.length === 0 && (
                  <div className="col-span-full text-center py-12 text-brain-500">
                    <BookOpen className="w-12 h-12 mx-auto mb-3 opacity-50" />
                    <p>{t("extracted_not_found")}</p>
                    <p className="text-xs mt-1">{t("extracted_not_found_hint")}</p>
                  </div>
                )}
              </div>
            )}

            {/* Regulations */}
            {activeTab === "regulations" && (
              <div className="space-y-4">
                {filteredRegulations.map(reg => (
                  <div
                    key={reg.regulation_id}
                    className="p-4 rounded-xl bg-brain-900/30 border border-brain-700/30 hover:border-brain-600/50 transition-all cursor-pointer"
                    onClick={() => setSelectedRegulation(reg)}
                  >
                    <div className="flex items-start justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <span className={`text-xs px-2 py-0.5 rounded-full border ${
                          reg.regulation_type === "regulation" ? "bg-blue-500/10 text-blue-400 border-blue-500/20" :
                          reg.regulation_type === "requirement" ? "bg-red-500/10 text-red-400 border-red-500/20" :
                          reg.regulation_type === "policy" ? "bg-purple-500/10 text-purple-400 border-purple-500/20" :
                          "bg-amber-500/10 text-amber-400 border-amber-500/20"
                        }`}>
                          {reg.regulation_type}
                        </span>
                        {reg.mandatory && (
                          <span className="text-xs px-2 py-0.5 rounded-full bg-red-500/10 text-red-400 border border-red-500/20 flex items-center gap-1">
                            <AlertCircle className="w-3 h-3" />
                            {t('required_badge')}
                          </span>
                        )}
                      </div>
                    </div>
                    
                    <h3 className="font-medium text-white mb-2">{reg.title}</h3>
                    <p className="text-sm text-brain-400 mb-3">{reg.description}</p>
                    
                    <div className="grid grid-cols-2 gap-4 text-sm">
                      {reg.scope && (
                        <div>
                          <span className="text-brain-500">{t("scope_label")}</span>
                          <span className="text-brain-300 ml-2">{reg.scope}</span>
                        </div>
                      )}
                      {reg.enforcement && (
                        <div>
                          <span className="text-brain-500">{t("control_label")}</span>
                          <span className="text-brain-300 ml-2">{reg.enforcement}</span>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
                
                {filteredRegulations.length === 0 && (
                  <div className="text-center py-12 text-brain-500">
                    <FileText className="w-12 h-12 mx-auto mb-3 opacity-50" />
                    <p>{t("regulations_not_found")}</p>
                    <p className="text-xs mt-1">{t("regulations_not_found_hint")}</p>
                  </div>
                )}
              </div>
            )}

            {/* Документы по шаблону (MeetFlow-перенос) */}
            {activeTab === "fill" && (
              <FillDocumentsPanel userId={getUserId() || undefined} />
            )}

            {/* Generated Documents */}
            {activeTab === "generated" && (
              <div className="space-y-4">
                {/* Discovery Button & Result */}
                <div className="flex items-center justify-between mb-4">
                  <button
                    onClick={discoverDocuments}
                    disabled={discovering}
                    className="flex items-center gap-2 px-4 py-2 rounded-lg bg-gradient-to-r from-emerald-500 to-teal-500 text-white font-medium hover:opacity-90 transition-opacity disabled:opacity-50"
                  >
                    {discovering ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Sparkles className="w-4 h-4" />
                    )}
                    {discovering ? t("analyzing") : t("find_and_create")}
                  </button>

                  {/* Фильтр по папкам документов */}
                  {docFolderNames.length > 0 && (
                    <div className="flex items-center gap-2">
                      <FolderOpen className="w-4 h-4 text-amber-500/80" />
                      <select
                        value={docFolderFilter}
                        onChange={(e) => setDocFolderFilter(e.target.value)}
                        className="px-3 py-2 rounded-lg bg-brain-900 border border-brain-700/50 text-sm text-brain-200 focus:outline-none focus:border-brain-500"
                      >
                        <option value="">{t("doc_folder_all")}</option>
                        {docFolderNames.map(name => (
                          <option key={name} value={name}>{name}</option>
                        ))}
                        <option value="__none__">{t("doc_folder_none")}</option>
                      </select>
                    </div>
                  )}
                </div>

                {/* Discovery Result */}
                {discoveryResult && (
                  <div className={`p-4 rounded-lg border ${
                    discoveryResult.success
                      ? "bg-emerald-500/10 border-emerald-500/30"
                      : "bg-red-500/10 border-red-500/30"
                  }`}>
                    <div className="flex items-start justify-between">
                      <div>
                        {discoveryResult.success ? (
                          <>
                            <div className="flex items-center gap-2 text-emerald-400 mb-2">
                              <CheckCircle className="w-4 h-4" />
                              <span className="font-medium">{t("analysis_complete")}</span>
                            </div>
                            <div className="grid grid-cols-4 gap-4 text-sm">
                              <div>
                                <span className="text-brain-500">{t("candidates_label")}</span>
                                <span className="text-white ml-2">{discoveryResult.candidates_found}</span>
                              </div>
                              <div>
                                <span className="text-brain-500">{t("clusters_label")}</span>
                                <span className="text-white ml-2">{discoveryResult.clusters_created}</span>
                              </div>
                              <div>
                                <span className="text-brain-500">{t("created_label")}</span>
                                <span className="text-emerald-400 ml-2">{discoveryResult.documents_generated}</span>
                              </div>
                              <div>
                                <span className="text-brain-500">{t("updated_label")}</span>
                                <span className="text-blue-400 ml-2">{discoveryResult.documents_updated}</span>
                              </div>
                            </div>
                          </>
                        ) : (
                          <div className="flex items-center gap-2 text-red-400">
                            <AlertCircle className="w-4 h-4" />
                            <span>{discoveryResult.errors?.[0] || t("analysis_error")}</span>
                          </div>
                        )}
                      </div>
                      <button
                        onClick={() => setDiscoveryResult(null)}
                        className="p-1 hover:bg-brain-800 rounded"
                      >
                        <X className="w-4 h-4 text-brain-500" />
                      </button>
                    </div>
                  </div>
                )}

                {/* Documents List */}
                {generatedDocs.length === 0 ? (
                  <div className="text-center py-12 text-brain-500">
                    <FileCode className="w-12 h-12 mx-auto mb-3 opacity-50" />
                    <p>{t("docs_not_found")}</p>
                    <p className="text-xs mt-1">{t("docs_not_found_hint")}</p>
                  </div>
                ) : (
                  generatedDocs.filter(doc => {
                    if (!docFolderFilter) return true;
                    const f = (doc.folder || "").trim();
                    return docFolderFilter === "__none__" ? !f : f === docFolderFilter;
                  }).map(doc => {
                    const typeConfig = documentTypeConfig[doc.document_type] || documentTypeConfig.regulation;

                    return (
                      <div
                        key={doc.document_id}
                        className="relative p-4 rounded-xl bg-brain-900/30 border border-brain-700/30 hover:border-brain-600/50 transition-all cursor-pointer group"
                        onClick={() => loadFullDocument(doc.document_id)}
                      >
                        <div className="flex items-start justify-between mb-2">
                          <div className="flex items-center gap-2">
                            <span className="p-1.5 rounded-lg bg-emerald-500/20 text-emerald-400">
                              {typeConfig.icon}
                            </span>
                            <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                              {typeConfig.label}
                            </span>
                            <span className="text-xs text-brain-500">v{doc.version}</span>
                            {(doc.folder || "").trim() && (
                              <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400/90 border border-amber-500/20 max-w-[10rem]">
                                <Folder className="w-3 h-3 flex-shrink-0" />
                                <span className="truncate">{(doc.folder || "").trim()}</span>
                              </span>
                            )}
                          </div>

                          <div className={`flex gap-1 transition-opacity ${
                            docFolderMenuFor === doc.document_id ? "opacity-100" : "opacity-0 group-hover:opacity-100"
                          }`}>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setDocFolderMenuFor(docFolderMenuFor === doc.document_id ? null : doc.document_id);
                              }}
                              className="p-1.5 rounded-lg hover:bg-brain-800 text-brain-500 hover:text-amber-400"
                              title={t("doc_folder_title")}
                            >
                              <Folder className="w-4 h-4" />
                            </button>
                            <button
                              onClick={(e) => { e.stopPropagation(); exportDocument(doc.document_id, "markdown"); }}
                              className="p-1.5 rounded-lg hover:bg-brain-800 text-brain-500 hover:text-white"
                              title={t("download_md_title")}
                            >
                              <Download className="w-4 h-4" />
                            </button>
                            <button
                              onClick={(e) => { e.stopPropagation(); deleteGeneratedDoc(doc.document_id); }}
                              className="p-1.5 rounded-lg hover:bg-brain-800 text-brain-500 hover:text-red-400"
                              title={t("delete_title")}
                            >
                              <Trash2 className="w-4 h-4" />
                            </button>
                          </div>
                        </div>

                        {/* Folder menu */}
                        {docFolderMenuFor === doc.document_id && (
                          <>
                            <div
                              className="fixed inset-0 z-20"
                              onClick={(e) => { e.stopPropagation(); setDocFolderMenuFor(null); }}
                            />
                            <div
                              className="absolute right-3 top-12 z-30 w-52 py-1 bg-brain-900 border border-brain-700 rounded-lg shadow-xl"
                              onClick={(e) => e.stopPropagation()}
                            >
                              {docFolderNames.map(name => (
                                <button
                                  key={name}
                                  onClick={() => setDocumentFolder(doc.document_id, name)}
                                  className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-brain-800 ${
                                    (doc.folder || "").trim() === name ? "text-amber-400" : "text-brain-300"
                                  }`}
                                >
                                  <Folder className="w-3 h-3 flex-shrink-0" />
                                  <span className="truncate">{name}</span>
                                </button>
                              ))}
                              <button
                                onClick={() => promptNewDocFolder(doc.document_id)}
                                className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-brain-300 hover:bg-brain-800"
                              >
                                <Plus className="w-3 h-3 flex-shrink-0" />
                                <span>{t("doc_folder_new")}</span>
                              </button>
                              {(doc.folder || "").trim() && (
                                <button
                                  onClick={() => setDocumentFolder(doc.document_id, "")}
                                  className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-brain-400 hover:bg-brain-800"
                                >
                                  <X className="w-3 h-3 flex-shrink-0" />
                                  <span>{t("doc_folder_remove")}</span>
                                </button>
                              )}
                            </div>
                          </>
                        )}
                        
                        <h3 className="font-medium text-white mb-1">{doc.title}</h3>
                        <p className="text-sm text-brain-400 mb-3 line-clamp-2">{doc.summary}</p>
                        
                        <div className="flex items-center gap-4 text-xs text-brain-500">
                          <span className="flex items-center gap-1">
                            <Clock className="w-3 h-3" />
                            {new Date(doc.updated_at).toLocaleDateString("ru-RU")}
                          </span>
                          <span className="flex items-center gap-1">
                            <FileText className="w-3 h-3" />
                            {t('words_count', { count: doc.word_count })}
                          </span>
                          <span className="flex items-center gap-1">
                            <Users className="w-3 h-3" />
                            {t('meetings_count', { count: doc.source_meetings.length })}
                          </span>
                        </div>
                        
                        {doc.keywords && doc.keywords.length > 0 && (
                          <div className="flex flex-wrap gap-1 mt-2">
                            {doc.keywords.slice(0, 5).map((keyword, i) => (
                              <span
                                key={i}
                                className="text-xs px-2 py-0.5 rounded-full bg-brain-800 text-brain-400"
                              >
                                {keyword}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            )}
          </>
        )}
      </div>

      {/* Extract Modal */}
      {showExtractModal && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-3xl max-h-[90vh] overflow-hidden shadow-2xl">
            <div className="p-6 border-b border-brain-700/50">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-xl bg-gradient-to-br from-purple-500/20 to-indigo-500/20">
                    <Sparkles className="w-5 h-5 text-purple-400" />
                  </div>
                  <div>
                    <h2 className="text-xl font-semibold text-white">{t("create_doc_modal_title")}</h2>
                    <p className="text-sm text-brain-400">{t("create_doc_modal_subtitle")}</p>
                  </div>
                </div>
                <button
                  onClick={() => {
                    setShowExtractModal(false);
                    setExtractResult(null);
                    setExtractTranscript("");
                    setExtractMeetingId("");
                    setSelectedMeetingIds([]);
                    setDesiredRegulation("");
                  }}
                  className="p-2 rounded-lg hover:bg-brain-800 text-brain-500 hover:text-white"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
            </div>
            
            <div className="p-6 overflow-y-auto max-h-[70vh]">
              {/* Model Tier Selector */}
              <div className="mb-5 p-4 bg-brain-800/50 rounded-xl border border-brain-700">
                <label className="text-sm font-medium text-brain-300 mb-3 block">{t("llm_model_label")}</label>
                <div className="flex gap-3">
                  <button
                    onClick={() => setExtractModelTier("standard")}
                    className={`flex-1 px-4 py-3 rounded-lg border transition-all ${
                      extractModelTier === "standard"
                        ? "bg-brain-600 border-brain-500 text-white"
                        : "bg-brain-800 border-brain-700 text-brain-400 hover:border-brain-600"
                    }`}
                  >
                    <div className="font-medium">{t("model_standard")}</div>
                    <div className="text-xs mt-1 opacity-70">{t("model_standard_hint")}</div>
                  </button>
                  <button
                    onClick={() => setExtractModelTier("premium")}
                    className={`flex-1 px-4 py-3 rounded-lg border transition-all ${
                      extractModelTier === "premium"
                        ? "bg-amber-600 border-amber-500 text-white"
                        : "bg-brain-800 border-brain-700 text-brain-400 hover:border-amber-600/50"
                    }`}
                  >
                    <div className="font-medium">{t("model_premium")}</div>
                    <div className="text-xs mt-1 opacity-70">{t("model_premium_hint")}</div>
                  </button>
                </div>
              </div>

              {/* Document Topic */}
              <div className="mb-5">
                <label className="text-sm font-medium text-brain-300 mb-2 block">{t("doc_topic_label")}</label>
                <textarea
                  placeholder={t("doc_topic_placeholder")}
                  value={desiredRegulation}
                  onChange={e => setDesiredRegulation(e.target.value)}
                  rows={3}
                  className="w-full px-4 py-3 rounded-lg bg-brain-800 border border-brain-700 focus:border-purple-500/50 outline-none text-white placeholder-brain-500 text-sm resize-none"
                />
              </div>

              {/* Meetings Selection */}
              <div className="mb-5">
                <label className="text-sm font-medium text-brain-300 mb-2 block">{t("select_meetings_label")}</label>
                {/* Фильтр по проекту/папке */}
                <div className="flex gap-2 mb-2">
                  <select
                    value={filterProject}
                    onChange={e => { setFilterProject(e.target.value); setFilterFolder(""); }}
                    className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 focus:border-purple-500/50 outline-none text-white text-sm"
                  >
                    <option value="">{t("all_projects")}</option>
                    {projects.map(p => (
                      <option key={p.project_id} value={p.project_id}>{p.name}</option>
                    ))}
                  </select>
                  {filterProject && folders.some(f => f.project_id === filterProject) && (
                    <select
                      value={filterFolder}
                      onChange={e => setFilterFolder(e.target.value)}
                      className="flex-1 px-3 py-2 rounded-lg bg-brain-800 border border-brain-700 focus:border-purple-500/50 outline-none text-white text-sm"
                    >
                      <option value="">{t("all_project_folders")}</option>
                      {folders.filter(f => f.project_id === filterProject).map(f => (
                        <option key={f.id} value={f.id}>{f.name}</option>
                      ))}
                    </select>
                  )}
                </div>
                <div className="mb-2">
                <input
                  type="text"
                    placeholder={t("search_meetings_placeholder")}
                    value={meetingSearchQuery}
                    onChange={e => setMeetingSearchQuery(e.target.value)}
                  className="w-full px-4 py-2 rounded-lg bg-brain-800 border border-brain-700 focus:border-purple-500/50 outline-none text-white placeholder-brain-500 text-sm"
                />
                </div>
                <div className="max-h-48 overflow-y-auto rounded-lg border border-brain-700 bg-brain-800/50">
                  {meetingsLoading ? (
                    <div className="p-4 text-center text-brain-500">
                      <Loader2 className="w-5 h-5 animate-spin mx-auto mb-2" />
                      {t('loading_meetings')}
                    </div>
                  ) : availableMeetings.length === 0 ? (
                    <div className="p-4 text-center text-brain-500">
                      {meetingSearchQuery ? (
                        <>
                          {t("no_meetings_for_query", { query: meetingSearchQuery })}
                        </>
                      ) : (
                        t("no_meetings_available")
                      )}
                    </div>
                  ) : (
                    availableMeetings.map(meeting => (
                      <div
                        key={meeting.id}
                        onClick={() => meeting.has_transcript && toggleMeetingSelection(meeting.id)}
                        className={`p-3 border-b border-brain-700/50 transition-colors ${
                          !meeting.has_transcript 
                            ? "opacity-50 cursor-not-allowed"
                            : selectedMeetingIds.includes(meeting.id)
                              ? "bg-purple-500/20 border-l-2 border-l-purple-500 cursor-pointer"
                              : "hover:bg-brain-700/50 cursor-pointer"
                        }`}
                      >
                        <div className="flex items-center gap-3">
                          <div className={`w-5 h-5 rounded border flex items-center justify-center ${
                            selectedMeetingIds.includes(meeting.id)
                              ? "bg-purple-500 border-purple-500"
                              : meeting.has_transcript 
                                ? "border-brain-600"
                                : "border-brain-700 bg-brain-800"
                          }`}>
                            {selectedMeetingIds.includes(meeting.id) && <Check className="w-3 h-3 text-white" />}
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className={`font-medium truncate ${meeting.has_transcript ? 'text-white' : 'text-brain-500'}`}>
                              {meeting.title}
                            </div>
                            <div className="text-xs text-brain-500">
                              {new Date(meeting.created_at).toLocaleDateString(locale)} • 
                              {meeting.has_transcript 
                                ? t("chars_count", { count: meeting.transcript_length?.toLocaleString() || 0 })
                                : t("no_transcript")
                              }
                            </div>
                          </div>
                        </div>
                      </div>
                    ))
                  )}
                </div>
                {selectedMeetingIds.length > 0 && (
                  <div className="mt-2 text-sm text-purple-400">
                    {t("selected_meetings_count", { count: selectedMeetingIds.length })}
                  </div>
                )}
              </div>

              {/* OR Divider */}
              <div className="flex items-center gap-4 my-5">
                <div className="flex-1 h-px bg-brain-700"></div>
                <span className="text-brain-500 text-sm">{t('or_paste_text')}</span>
                <div className="flex-1 h-px bg-brain-700"></div>
              </div>
              
              {/* Transcript */}
              <div className="mb-5">
                <label className="text-sm font-medium text-brain-300 mb-2 block">{t("text_for_analysis_label")}</label>
                <textarea
                  placeholder={t("text_for_analysis_placeholder")}
                  value={extractTranscript}
                  onChange={e => setExtractTranscript(e.target.value)}
                  rows={8}
                  className="w-full px-4 py-3 rounded-lg bg-brain-800 border border-brain-700 focus:border-purple-500/50 outline-none text-white placeholder-brain-500 text-sm resize-none"
                />
                <p className="text-xs text-brain-500 mt-1">
                  {t("current_chars", { count: extractTranscript.length })}
                </p>
              </div>
              
              {/* Extract Button */}
              <button
                onClick={extractFromMeeting}
                disabled={extracting || (extractTranscript.length < 100 && selectedMeetingIds.length === 0)}
                className={`w-full flex items-center justify-center gap-2 px-4 py-3 rounded-lg ${
                  extractModelTier === "premium"
                    ? "bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-500 hover:to-orange-500"
                    : "bg-purple-600 hover:bg-purple-500"
                } disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-white font-medium`}
              >
                {extracting ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    {t("analyzing_short")}{extractModelTier === "premium" ? t("analyzing_premium") : ""}...
                  </>
                ) : (
                  <>
                    <Sparkles className="w-4 h-4" />
                    {extractModelTier === "premium" ? "👑 " : ""}{t("create_doc_button")}
                  </>
                )}
              </button>
              
              {/* Results */}
              {extractResult && (
                <div className="mt-6 bg-brain-800/50 rounded-lg p-4">
                  {extractResult.error ? (
                    <div className="text-red-400 flex items-center gap-2">
                      <AlertCircle className="w-4 h-4" />
                      {extractResult.error}
                    </div>
                  ) : extractResult.success && extractResult.document ? (
                    <div className="space-y-4">
                      {/* Успешно создан документ */}
                      <div className="flex items-center gap-3 p-4 bg-green-500/10 border border-green-500/30 rounded-lg">
                        <div className="p-2 bg-green-500/20 rounded-lg">
                          <Check className="w-5 h-5 text-green-400" />
                        </div>
                        <div className="flex-1">
                          <h4 className="font-medium text-green-400">{t("doc_created_title")}</h4>
                          <p className="text-sm text-brain-300 mt-1">{extractResult.document.title}</p>
                        </div>
                      </div>
                      
                      <div className="p-4 bg-brain-900/50 rounded-lg border border-brain-700">
                        <div className="flex items-center gap-4 text-sm text-brain-400 mb-3">
                          <span>{t("doc_type_label")} {extractResult.document.document_type}</span>
                          <span>{t("doc_words_label")} {extractResult.document.word_count}</span>
                        </div>
                      
                        {extractResult.document.content_preview && (
                          <div className="text-sm text-brain-300 line-clamp-4">
                            {extractResult.document.content_preview}
                        </div>
                      )}
                      </div>
                      
                      <p className="text-sm text-brain-500">
                        {t("doc_added_hint")}
                      </p>
                    </div>
                  ) : (
                    <div className="p-4 bg-yellow-500/10 border border-yellow-500/30 rounded-lg">
                      <p className="text-yellow-400 text-sm">
                        {extractResult.message || t("doc_not_created_hint")}
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Template Detail Modal */}
      {selectedTemplate && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-2xl max-h-[80vh] overflow-hidden shadow-2xl">
            <div className="p-6 border-b border-brain-700/50">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <span className="text-3xl">{selectedTemplate.icon}</span>
                  <div>
                    <h2 className="text-xl font-semibold text-white">{selectedTemplate.name}</h2>
                    <p className="text-sm text-brain-400">{selectedTemplate.description}</p>
                  </div>
                </div>
                <button
                  onClick={() => {
                    setSelectedTemplate(null);
                    setExecutionResult(null);
                    setExecuteQuery("");
                  }}
                  className="p-2 rounded-lg hover:bg-brain-800 text-brain-500 hover:text-white"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
            </div>
            
            <div className="p-6 overflow-y-auto max-h-[60vh]">
              {/* Triggers */}
              {selectedTemplate.trigger_patterns.length > 0 && (
                <div className="mb-6">
                  <h3 className="text-sm font-medium text-brain-400 mb-2">{t("triggers_heading")}</h3>
                  <div className="space-y-1">
                    {selectedTemplate.trigger_patterns.map((pattern, i) => (
                      <code key={i} className="block text-sm bg-brain-800 rounded px-3 py-1.5 text-purple-400 font-mono">
                        {pattern}
                      </code>
                    ))}
                  </div>
                </div>
              )}
              
              {/* Steps */}
              <div className="mb-6">
                <h3 className="text-sm font-medium text-brain-400 mb-2">{t("execution_steps_heading")}</h3>
                <div className="space-y-2">
                  {selectedTemplate.steps.map((step, i) => (
                    <div key={step.step_id} className="flex items-center gap-3">
                      <div className="w-6 h-6 rounded-full bg-purple-500/20 text-purple-400 flex items-center justify-center text-xs font-medium flex-shrink-0">
                        {i + 1}
                      </div>
                      <div className="flex-1 bg-brain-800/50 rounded-lg px-3 py-2">
                        <span className="text-white font-medium">{step.agent}</span>
                        <span className="text-brain-500 mx-2">→</span>
                        <span className="text-purple-400">{step.action}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
              
              {/* Execute */}
              <div className="mb-6">
                <h3 className="text-sm font-medium text-brain-400 mb-2">{t("execute_heading")}</h3>
                <div className="flex gap-2">
                  <input
                    type="text"
                    placeholder={t("enter_query_placeholder")}
                    value={executeQuery}
                    onChange={e => setExecuteQuery(e.target.value)}
                    className="flex-1 px-4 py-2 rounded-lg bg-brain-800 border border-brain-700 focus:border-purple-500/50 outline-none text-white placeholder-brain-500"
                    onKeyDown={e => e.key === 'Enter' && executeTemplate(selectedTemplate)}
                  />
                  <button
                    onClick={() => executeTemplate(selectedTemplate)}
                    disabled={executing || !executeQuery.trim()}
                    className="flex items-center gap-2 px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-white"
                  >
                    {executing ? (
                      <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                    ) : (
                      <Play className="w-4 h-4" />
                    )}
                    {t('run_button')}
                  </button>
                </div>
              </div>
              
              {/* Result */}
              {executionResult && (
                <div className="bg-brain-800/50 rounded-lg p-4">
                  <h3 className="text-sm font-medium text-brain-400 mb-2">{t("result_heading")}</h3>
                  {executionResult.error ? (
                    <div className="text-red-400 flex items-center gap-2">
                      <AlertCircle className="w-4 h-4" />
                      {executionResult.error}
                    </div>
                  ) : (
                    <div className="text-sm">
                      <pre className="text-brain-300 overflow-auto max-h-60 whitespace-pre-wrap">
                        {typeof executionResult === 'object' 
                          ? JSON.stringify(executionResult, null, 2)
                          : executionResult
                        }
                      </pre>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Regulation Detail Modal */}
      {selectedRegulation && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-2xl max-h-[80vh] overflow-hidden shadow-2xl">
            <div className="p-6 border-b border-brain-700/50">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-xl bg-gradient-to-br from-blue-500/20 to-cyan-500/20">
                    <FileText className="w-5 h-5 text-blue-400" />
                  </div>
                  <div>
                    <h2 className="text-lg font-semibold text-white">{selectedRegulation.title}</h2>
                    <div className="flex items-center gap-2 mt-1">
                      <span className={`text-xs px-2 py-0.5 rounded-full border ${
                        selectedRegulation.regulation_type === "regulation" ? "bg-blue-500/10 text-blue-400 border-blue-500/20" :
                        selectedRegulation.regulation_type === "requirement" ? "bg-red-500/10 text-red-400 border-red-500/20" :
                        selectedRegulation.regulation_type === "policy" ? "bg-purple-500/10 text-purple-400 border-purple-500/20" :
                        "bg-amber-500/10 text-amber-400 border-amber-500/20"
                      }`}>
                        {selectedRegulation.regulation_type}
                      </span>
                      {selectedRegulation.mandatory && (
                        <span className="text-xs px-2 py-0.5 rounded-full bg-red-500/10 text-red-400 border border-red-500/20 flex items-center gap-1">
                          <AlertCircle className="w-3 h-3" />
                          {t('required_badge')}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                <button
                  onClick={() => setSelectedRegulation(null)}
                  className="p-2 rounded-lg hover:bg-brain-800 text-brain-500 hover:text-white transition-colors"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
            </div>
            
            <div className="p-6 overflow-y-auto max-h-[60vh] space-y-6">
              {/* Description */}
              <div>
                <h3 className="text-sm font-medium text-brain-400 mb-2">{t("description_heading")}</h3>
                <p className="text-brain-300">{selectedRegulation.description}</p>
              </div>
              
              {/* Metadata */}
              <div className="grid grid-cols-2 gap-4">
                {selectedRegulation.scope && (
                  <div className="bg-brain-800/50 rounded-lg p-3">
                    <span className="text-xs text-brain-500 block mb-1">{t("scope_heading")}</span>
                    <span className="text-brain-300">{selectedRegulation.scope}</span>
                  </div>
                )}
                {selectedRegulation.enforcement && (
                  <div className="bg-brain-800/50 rounded-lg p-3">
                    <span className="text-xs text-brain-500 block mb-1">{t("control_heading")}</span>
                    <span className="text-brain-300">{selectedRegulation.enforcement}</span>
                  </div>
                )}
              </div>
              
              {/* Conditions */}
              {Array.isArray(selectedRegulation.conditions) && selectedRegulation.conditions.length > 0 && (
                <div>
                  <h3 className="text-sm font-medium text-brain-400 mb-2">{t("conditions_heading")}</h3>
                  <ul className="space-y-2">
                    {selectedRegulation.conditions.map((condition, i) => (
                      <li key={i} className="flex items-start gap-2 text-sm">
                        <ChevronRight className="w-4 h-4 text-blue-400 mt-0.5 flex-shrink-0" />
                        <span className="text-brain-300">{condition}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              
              {/* Exceptions */}
              {Array.isArray(selectedRegulation.exceptions) && selectedRegulation.exceptions.length > 0 && (
                <div>
                  <h3 className="text-sm font-medium text-brain-400 mb-2">{t("exceptions_heading")}</h3>
                  <ul className="space-y-2">
                    {selectedRegulation.exceptions.map((exception, i) => (
                      <li key={i} className="flex items-start gap-2 text-sm">
                        <AlertCircle className="w-4 h-4 text-amber-400 mt-0.5 flex-shrink-0" />
                        <span className="text-brain-300">{exception}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              
              {/* Source */}
              {selectedRegulation.source_meeting_id && (
                <div className="bg-brain-800/30 rounded-lg p-3 text-xs text-brain-500">
                  <span>{t("source_meeting_prefix")}</span>
                  <code className="text-brain-400">{selectedRegulation.source_meeting_id}</code>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Extracted Template Detail Modal */}
      {selectedExtracted && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-2xl max-h-[80vh] overflow-hidden shadow-2xl">
            <div className="p-6 border-b border-brain-700/50">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-xl bg-gradient-to-br from-green-500/20 to-emerald-500/20">
                    <BookOpen className="w-5 h-5 text-green-400" />
                  </div>
                  <div>
                    <h2 className="text-lg font-semibold text-white">{selectedExtracted.title}</h2>
                    <span className={`text-xs px-2 py-0.5 rounded-full border ${
                      selectedExtracted.template_type === "checklist" ? "bg-green-500/10 text-green-400 border-green-500/20" :
                      selectedExtracted.template_type === "process" ? "bg-blue-500/10 text-blue-400 border-blue-500/20" :
                      selectedExtracted.template_type === "structure" ? "bg-purple-500/10 text-purple-400 border-purple-500/20" :
                      "bg-amber-500/10 text-amber-400 border-amber-500/20"
                    }`}>
                      {selectedExtracted.template_type}
                    </span>
                  </div>
                </div>
                <button
                  onClick={() => setSelectedExtracted(null)}
                  className="p-2 rounded-lg hover:bg-brain-800 text-brain-500 hover:text-white transition-colors"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
            </div>
            
            <div className="p-6 overflow-y-auto max-h-[60vh] space-y-6">
              {/* Description */}
              <div>
                <h3 className="text-sm font-medium text-brain-400 mb-2">{t("description_heading")}</h3>
                <p className="text-brain-300">{selectedExtracted.description}</p>
              </div>
              
              {/* Items */}
              {Array.isArray(selectedExtracted.items) && selectedExtracted.items.length > 0 && (
                <div>
                  <h3 className="text-sm font-medium text-brain-400 mb-2">{t("items_heading", { count: selectedExtracted.items.length })}</h3>
                  <ul className="space-y-2">
                    {selectedExtracted.items.map((item, i) => (
                      <li key={i} className="flex items-start gap-2 text-sm bg-brain-800/30 rounded-lg p-2">
                        <span className="text-green-400 font-medium min-w-[24px]">{i + 1}.</span>
                        <span className="text-brain-300">{item}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              
              {/* Use Case */}
              {selectedExtracted.use_case && (
                <div className="bg-brain-800/50 rounded-lg p-4">
                  <h3 className="text-sm font-medium text-brain-400 mb-2">{t("when_to_use_heading")}</h3>
                  <p className="text-brain-300">{selectedExtracted.use_case}</p>
                </div>
              )}
              
              {/* Source */}
              {selectedExtracted.source_meeting_id && (
                <div className="bg-brain-800/30 rounded-lg p-3 text-xs text-brain-500">
                  <span>{t("source_meeting_prefix")}</span>
                  <code className="text-brain-400">{selectedExtracted.source_meeting_id}</code>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Generated Document Detail Modal */}
      {selectedGenerated && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-4xl max-h-[90vh] overflow-hidden shadow-2xl flex flex-col">
            {/* Modal Header */}
            <div className="p-6 border-b border-brain-700/50 flex-shrink-0">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-xl bg-gradient-to-br from-emerald-500/20 to-teal-500/20">
                    <FileCode className="w-5 h-5 text-emerald-400" />
                  </div>
                  <div>
                    <h2 className="text-lg font-semibold text-white">{selectedGenerated.title}</h2>
                    <div className="flex items-center gap-2 mt-1 text-xs text-brain-500">
                      <span>v{selectedGenerated.version}</span>
                      <span>•</span>
                      <span>{t('words_count', { count: selectedGenerated.word_count })}</span>
                      <span>•</span>
                      <span>{new Date(selectedGenerated.updated_at).toLocaleDateString("ru-RU")}</span>
                    </div>
                  </div>
                </div>
                
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => exportDocument(selectedGenerated.document_id, "markdown")}
                    className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-brain-800 text-brain-300 hover:text-white text-sm"
                  >
                    <Download className="w-4 h-4" />
                    MD
                  </button>
                  <button
                    onClick={() => exportDocument(selectedGenerated.document_id, "html")}
                    className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-brain-800 text-brain-300 hover:text-white text-sm"
                  >
                    <Download className="w-4 h-4" />
                    HTML
                  </button>
                  <button
                    onClick={() => setSelectedGenerated(null)}
                    className="p-2 rounded-lg hover:bg-brain-800 text-brain-500 hover:text-white"
                  >
                    <X className="w-5 h-5" />
                  </button>
                </div>
              </div>
            </div>
            
            {/* Modal Content */}
            <div className="flex-1 overflow-y-auto p-6">
              {/* Summary */}
              <div className="mb-6 p-4 rounded-lg bg-brain-800/50">
                <h3 className="text-sm font-medium text-brain-400 mb-2">{t("summary_heading")}</h3>
                <p className="text-brain-300">{selectedGenerated.summary}</p>
              </div>
              
              {/* Table of Contents */}
              {selectedGenerated.table_of_contents && selectedGenerated.table_of_contents.length > 0 && (
                <div className="mb-6">
                  <h3 className="text-sm font-medium text-brain-400 mb-2">{t("content_heading")}</h3>
                  <div className="space-y-1">
                    {selectedGenerated.table_of_contents.map((item: any, i: number) => (
                      <div
                        key={i}
                        className="flex items-center gap-2 text-sm"
                        style={{ paddingLeft: `${(parseInt(item.level) - 1) * 16}px` }}
                      >
                        <ChevronRight className="w-3 h-3 text-emerald-400" />
                        <span className="text-brain-300">{item.title}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              
              {/* Full Content */}
              <div className="prose prose-invert prose-sm max-w-none">
                <SafeHtml
                  html={selectedGenerated.content_html}
                  fallback={
                    <pre className="whitespace-pre-wrap text-brain-300 text-sm">
                      {selectedGenerated.content_markdown}
                    </pre>
                  }
                />
              </div>
              
              {/* Source Meetings */}
              {selectedGenerated.source_meetings && selectedGenerated.source_meetings.length > 0 && (
                <div className="mt-6 p-4 rounded-lg bg-brain-800/30">
                  <h3 className="text-sm font-medium text-brain-400 mb-2">{t("sources_heading")}</h3>
                  <div className="flex flex-wrap gap-2">
                    {selectedGenerated.source_meetings.map((meetingId: string, i: number) => (
                      <span
                        key={i}
                        className="text-xs px-2 py-1 rounded bg-brain-700 text-brain-400"
                      >
                        {meetingId.slice(0, 8)}...
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
