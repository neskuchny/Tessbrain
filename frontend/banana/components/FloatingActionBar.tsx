"use client";

import { useRef, useState, useEffect, useMemo, useCallback } from "react";
import { useTranslations } from "next-intl";
import { useWorkflowStore, EdgeStyle, WorkflowFile } from "@/banana/store/workflowStore";
import { MyBoardsMenu } from "@/banana/components/MyBoardsMenu";
import { RunProcessButton } from "@/banana/components/RunProcessButton";
import { ProcessSchedules } from "@/banana/components/ProcessSchedules";
import { NodeType } from "@/banana/types";
import { useReactFlow } from "@xyflow/react";
import { TemplatesPanel } from "./TemplatesPanel";
import { NlDesignerPanel } from "./NlDesignerPanel";
import { useToast } from "@/banana/components/Toast";
import { Sparkles } from "lucide-react";


interface NodeButtonProps {
  type: NodeType;
  label: string;
}

function NodeButton({ type, label }: NodeButtonProps) {
  const addNode = useWorkflowStore((state) => state.addNode);
  const { screenToFlowPosition } = useReactFlow();

  const handleClick = () => {
    const centerX = window.innerWidth / 2;
    const centerY = window.innerHeight / 2;
    const position = screenToFlowPosition({ x: centerX + Math.random() * 100 - 50, y: centerY + Math.random() * 100 - 50 });
    addNode(type, position);
  };

  const handleDragStart = (event: React.DragEvent) => {
    event.dataTransfer.setData("application/node-type", type);
    event.dataTransfer.effectAllowed = "copy";
  };

  return (
    <button
      onClick={handleClick}
      draggable
      onDragStart={handleDragStart}
      className="px-2.5 py-1.5 text-[11px] font-medium text-brain-200/70 hover:text-brain-50 hover:bg-brain-800 rounded transition-colors cursor-grab active:cursor-grabbing"
    >
      {label}
    </button>
  );
}

function GenerateComboButton() {
  const t = useTranslations("banana");
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const addNode = useWorkflowStore((state) => state.addNode);
  const { screenToFlowPosition } = useReactFlow();

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setIsOpen(false);
    };
    if (isOpen) document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [isOpen]);

  const handleAddNode = (type: NodeType) => {
    const centerX = window.innerWidth / 2;
    const centerY = window.innerHeight / 2;
    const position = screenToFlowPosition({ x: centerX + Math.random() * 100 - 50, y: centerY + Math.random() * 100 - 50 });
    addNode(type, position);
    setIsOpen(false);
  };

  const handleDragStart = (event: React.DragEvent, type: NodeType) => {
    event.dataTransfer.setData("application/node-type", type);
    event.dataTransfer.effectAllowed = "copy";
    setIsOpen(false);
  };

  return (
    <div className="relative" ref={menuRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="px-2.5 py-1.5 text-[11px] font-medium text-brain-200/70 hover:text-brain-50 hover:bg-brain-800 rounded transition-colors flex items-center gap-1"
      >
        Generate
        <svg className={`w-3 h-3 transition-transform ${isOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
        </svg>
      </button>

      {isOpen && (
        <div className="absolute bottom-full left-0 mb-2 bg-brain-900 border border-brain-700/60 rounded-lg shadow-xl overflow-hidden min-w-[140px]">
          <button
            onClick={() => handleAddNode("nanoBanana")}
            draggable
            onDragStart={(e) => handleDragStart(e, "nanoBanana")}
            className="w-full px-3 py-2 text-left text-[11px] font-medium text-brain-100/80 hover:bg-brain-800 hover:text-brain-50 transition-colors flex items-center gap-2 cursor-grab active:cursor-grabbing"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909m-18 3.75h16.5a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5H3.75A1.5 1.5 0 002.25 6v12a1.5 1.5 0 001.5 1.5zm10.5-11.25h.008v.008h-.008V8.25zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0z" />
            </svg>
            Image
          </button>
          <button
            onClick={() => handleAddNode("llmGenerate")}
            draggable
            onDragStart={(e) => handleDragStart(e, "llmGenerate")}
            className="w-full px-3 py-2 text-left text-[11px] font-medium text-brain-100/80 hover:bg-brain-800 hover:text-brain-50 transition-colors flex items-center gap-2 cursor-grab active:cursor-grabbing"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
            </svg>
            Text (LLM)
          </button>
          <button
            onClick={() => handleAddNode("infographic")}
            draggable
            onDragStart={(e) => handleDragStart(e, "infographic")}
            className="w-full px-3 py-2 text-left text-[11px] font-medium text-brain-100/80 hover:bg-brain-800 hover:text-brain-50 transition-colors flex items-center gap-2 cursor-grab active:cursor-grabbing"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5M9 11.25v1.5M12 9v3.75m3-6v6" />
            </svg>
            {t("node_infographic")}
          </button>
          <button
            onClick={() => handleAddNode("audio")}
            draggable
            onDragStart={(e) => handleDragStart(e, "audio")}
            className="w-full px-3 py-2 text-left text-[11px] font-medium text-brain-100/80 hover:bg-brain-800 hover:text-brain-50 transition-colors flex items-center gap-2 cursor-grab active:cursor-grabbing"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z" />
            </svg>
            {t("node_audio")}
          </button>
        </div>
      )}
    </div>
  );
}

// Категория процесс-узлов — раскрывающийся список, чтобы нижняя панель не была
// «простынёй» из 18 кнопок в ряд. Внутри — те же перетаскиваемые кнопки узлов.
function NodeGroupButton({ label, items }: { label: string; items: { type: NodeType; label: string }[] }) {
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const addNode = useWorkflowStore((state) => state.addNode);
  const { screenToFlowPosition } = useReactFlow();

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setIsOpen(false);
    };
    if (isOpen) document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [isOpen]);

  const add = (type: NodeType) => {
    const position = screenToFlowPosition({
      x: window.innerWidth / 2 + Math.random() * 100 - 50,
      y: window.innerHeight / 2 + Math.random() * 100 - 50,
    });
    addNode(type, position);
    setIsOpen(false);
  };
  const onDragStart = (event: React.DragEvent, type: NodeType) => {
    event.dataTransfer.setData("application/node-type", type);
    event.dataTransfer.effectAllowed = "copy";
    setIsOpen(false);
  };

  return (
    <div className="relative" ref={menuRef}>
      <button
        onClick={() => setIsOpen((v) => !v)}
        className="px-2.5 py-1.5 text-[11px] font-medium text-brain-200/70 hover:text-brain-50 hover:bg-brain-800 rounded transition-colors flex items-center gap-1"
      >
        {label}
        <svg className={`w-3 h-3 transition-transform ${isOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
        </svg>
      </button>
      {isOpen && (
        <div className="absolute bottom-full left-0 mb-2 bg-brain-900 border border-brain-700/60 rounded-lg shadow-xl overflow-hidden min-w-[170px] py-0.5">
          {items.map((it) => (
            <button
              key={it.type}
              onClick={() => add(it.type)}
              draggable
              onDragStart={(e) => onDragStart(e, it.type)}
              className="w-full px-3 py-1.5 text-left text-[11px] font-medium text-brain-100/80 hover:bg-brain-800 hover:text-brain-50 transition-colors cursor-grab active:cursor-grabbing"
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function FloatingActionBar() {
  const t = useTranslations("banana");
  const {
    nodes,
    isRunning,
    executeWorkflow,
    regenerateNode,
    stopWorkflow,
    validateWorkflow,
    edgeStyle,
    setEdgeStyle,
    saveWorkflow,
    loadWorkflow,
    boardKind,
    setBoardKind,
    autoLayout,
  } = useWorkflowStore();
  
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [runMenuOpen, setRunMenuOpen] = useState(false);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [nlOpen, setNlOpen] = useState(false);
  const runMenuRef = useRef<HTMLDivElement>(null);
  const templatesButtonRef = useRef<HTMLDivElement>(null);

  const { valid, errors } = validateWorkflow();

  const selectedNode = useMemo(() => {
    const selected = nodes.filter((n) => n.selected);
    return selected.length === 1 ? selected[0] : null;
  }, [nodes]);

  // Есть процесс-узлы → запуск ТОЛЬКО серверный («Запустить процесс»).
  // Клиентский Run для таких схем «не работал» и путал двумя кнопками.
  const PROCESS_TYPES = useMemo(() => new Set([
    "trigger", "ask_brain", "report", "notify", "task", "action",
    "generate", "condition", "wait_reply", "meeting_data", "meeting_share",
    "document", "crm_data", "web_search", "report_xlsx", "translate", "doc_edit",
    "crm_write", "coding_agent",
    // Аудит: infographic/audio исполняются ТОЛЬКО серверным движком — в
    // клиентском прогоне они молча пропускались («узел для вида»). Доска с
    // ними — серверный запуск (движок исполняет и креативные узлы тоже).
    "infographic", "audio",
  ]), []);
  const hasProcessNodes = useMemo(
    () => nodes.some((n) => PROCESS_TYPES.has(String(n.type))),
    [nodes, PROCESS_TYPES]);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (runMenuRef.current && !runMenuRef.current.contains(event.target as Node)) setRunMenuOpen(false);
    };
    if (runMenuOpen) document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [runMenuOpen]);

  const toggleEdgeStyle = () => {
    setEdgeStyle(edgeStyle === ("angular" as EdgeStyle) ? ("curved" as EdgeStyle) : ("angular" as EdgeStyle));
  };

  const handleRunClick = () => {
    if (isRunning) stopWorkflow();
    else executeWorkflow();
  };

  const handleRunFromSelected = () => {
    if (selectedNode) {
      executeWorkflow(selectedNode.id);
      setRunMenuOpen(false);
    }
  };

  const handleRunSelectedOnly = () => {
    if (selectedNode) {
      regenerateNode(selectedNode.id);
      setRunMenuOpen(false);
    }
  };

  const handleLoadClick = () => fileInputRef.current?.click();

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (event) => {
      try {
        const workflow = JSON.parse(event.target?.result as string) as WorkflowFile;
        if (workflow.version && workflow.nodes && workflow.edges) loadWorkflow(workflow);
        else alert("Invalid workflow file format");
      } catch {
        alert("Failed to parse workflow file");
      }
    };
    reader.readAsText(file);
    e.target.value = "";
  };

  return (
    <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[9999] pointer-events-none">
      <input ref={fileInputRef} type="file" accept=".json" onChange={handleFileChange} className="hidden" />
      <div className="flex items-center gap-0.5 bg-brain-900/95 backdrop-blur-sm rounded-lg shadow-2xl border border-brain-700/60 px-1.5 py-1 pointer-events-auto">
        {/* Режим доски: Креатив (ИИ-холст) / Процессы (конвейер, P3) */}
        <div className="flex rounded border border-brain-700/60 overflow-hidden mr-1">
          {(["creative", "process"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setBoardKind(k)}
              className={`px-2 py-1 text-[10px] font-medium transition-colors ${
                boardKind === k ? "bg-brain-700 text-white" : "text-brain-400 hover:bg-brain-800"
              }`}
            >
              {k === "creative" ? t("mode_creative") : t("mode_process")}
            </button>
          ))}
        </div>

        {boardKind === "creative" ? (
          <>
            <NodeButton type="imageInput" label="Image" />
            <NodeButton type="annotation" label="Annotate" />
            <NodeButton type="prompt" label="Prompt" />
            <NodeButton type="textCombiner" label="Combiner" />
            <GenerateComboButton />
          </>
        ) : (
          <>
            {/* Триггер — точка входа, оставляем отдельной кнопкой; остальные
                18 узлов свёрнуты в 4 категории, чтобы панель не была «простынёй». */}
            <NodeButton type="trigger" label={t("node_trigger")} />
            <NodeGroupButton label={t("group_sources")} items={[
              { type: "ask_brain", label: t("node_brain") },
              { type: "meeting_data", label: t("node_meeting_data") },
              { type: "crm_data", label: t("node_crm_data") },
              { type: "web_search", label: t("node_web_search") },
            ]} />
            <NodeGroupButton label={t("group_docs")} items={[
              { type: "document", label: t("node_document") },
              { type: "report", label: t("node_report") },
              { type: "report_xlsx", label: t("node_report_xlsx") },
              { type: "translate", label: t("node_translate") },
              { type: "doc_edit", label: t("node_doc_edit") },
              { type: "generate", label: t("node_generate") },
            ]} />
            <NodeGroupButton label={t("group_delivery")} items={[
              { type: "notify", label: t("node_notify") },
              { type: "task", label: t("node_task") },
              { type: "coding_agent", label: t("node_coding_agent") },
              { type: "action", label: t("node_action") },
              { type: "crm_write", label: t("node_crm_write") },
              { type: "meeting_share", label: t("node_meeting_share") },
            ]} />
            <NodeGroupButton label={t("group_logic")} items={[
              { type: "condition", label: t("node_condition") },
              { type: "wait_reply", label: t("node_wait_reply") },
            ]} />
            <ProcessSchedules />
            <RunProcessButton />
          </>
        )}
        <NodeButton type="output" label="Output" />
        {/* Заметка — универсальна для обоих режимов: объясняет схему */}
        <NodeButton type="note" label={t("node_note")} />

        <div className="w-px h-5 bg-brain-700/60 mx-1.5" />

        <button onClick={() => saveWorkflow()} title="Save workflow" className="p-1.5 text-brain-200/70 hover:text-brain-50 hover:bg-brain-800 rounded transition-colors">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
          </svg>
        </button>

        <button onClick={handleLoadClick} title="Load workflow" className="p-1.5 text-brain-200/70 hover:text-brain-50 hover:bg-brain-800 rounded transition-colors">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
          </svg>
        </button>

        <MyBoardsMenu />

        <div className="relative" ref={templatesButtonRef}>
          <button
            onClick={() => setTemplatesOpen(!templatesOpen)}
            title={t("title_templates_attr")}
            className={`p-1.5 rounded transition-colors ${
              templatesOpen
                ? "text-brain-50 bg-brain-700"
                : "text-brain-200/70 hover:text-brain-50 hover:bg-brain-800"
            }`}
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
            </svg>
          </button>
          <TemplatesPanel isOpen={templatesOpen} onClose={() => setTemplatesOpen(false)} />
        </div>

        {/* Процесс из естественного языка (F): описал словами → готовая схема */}
        <div className="relative">
          <button
            onClick={() => { setNlOpen(!nlOpen); setTemplatesOpen(false); }}
            title={t("nl_title_attr")}
            className={`p-1.5 rounded transition-colors ${
              nlOpen
                ? "text-brain-50 bg-purple-700"
                : "text-purple-300 hover:text-brain-50 hover:bg-brain-800"
            }`}
          >
            <Sparkles className="w-4 h-4" />
          </button>
          <NlDesignerPanel isOpen={nlOpen} onClose={() => setNlOpen(false)} />
        </div>

        <button onClick={toggleEdgeStyle} title="Toggle edge style" className="p-1.5 text-brain-200/70 hover:text-brain-50 hover:bg-brain-800 rounded transition-colors">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 7h7M3 17h7M14 7h7M14 17h7M10 7v10M14 7v10" />
          </svg>
        </button>

        {/* «Разложить»: авто-раскладка по слоям с реальными размерами узлов —
            лечит доски, где карточки легли друг на друга */}
        <button onClick={autoLayout} title={t("auto_layout_title")}
          className="p-1.5 text-brain-200/70 hover:text-brain-50 hover:bg-brain-800 rounded transition-colors">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <rect x="3" y="4" width="7" height="6" rx="1" />
            <rect x="14" y="4" width="7" height="6" rx="1" />
            <rect x="3" y="14" width="7" height="6" rx="1" />
            <rect x="14" y="14" width="7" height="6" rx="1" />
          </svg>
        </button>

        <div className="w-px h-5 bg-brain-700/60 mx-1.5" />

        {/* Схема с процесс-узлами запускается только сервером — клиентский
            Run прячем, остаётся одна кнопка «Запустить процесс» */}
        {hasProcessNodes ? (
          boardKind !== "process" ? <RunProcessButton /> : null
        ) : (
        <div className="relative" ref={runMenuRef}>
          <button
            onClick={() => {
              // Раньше: клик лишь открывал меню, а меню рендерится только при
              // выбранном узле → без выделения Run «ничего не делал». Плюс при
              // невалидном графе кнопка была серой без объяснения. Теперь:
              // Stop / объяснение тостом / меню при выделении / прямой запуск.
              if (isRunning) { stopWorkflow(); return; }
              if (!valid) {
                useToast.getState().show(
                  errors[0] || t("board_empty_warning"),
                  "warning");
                return;
              }
              if (selectedNode) setRunMenuOpen((v) => !v);
              else executeWorkflow();
            }}
            className={`px-3 py-1.5 rounded text-[11px] font-semibold transition-colors ${
              isRunning
                ? "bg-red-600 hover:bg-red-500 text-white"
                : valid
                ? "bg-brain-600 hover:bg-brain-500 text-white"
                : "bg-brain-800 text-brain-200/70"
            }`}
            title={!valid ? errors.join("\n") : "Run"}
          >
            {isRunning ? "Stop" : "Run"}
          </button>

          {runMenuOpen && selectedNode && (
            <div className="absolute bottom-full right-0 mb-2 bg-brain-900 border border-brain-700/60 rounded-lg shadow-xl overflow-hidden min-w-[190px]">
              <button onClick={handleRunFromSelected} className="w-full px-3 py-2 text-left text-[11px] text-brain-100/80 hover:bg-brain-800 hover:text-brain-50 transition-colors">
                Run from selected
              </button>
              <button onClick={handleRunSelectedOnly} className="w-full px-3 py-2 text-left text-[11px] text-brain-100/80 hover:bg-brain-800 hover:text-brain-50 transition-colors">
                Run selected only
              </button>
              <button onClick={handleRunClick} className="w-full px-3 py-2 text-left text-[11px] text-brain-100/80 hover:bg-brain-800 hover:text-brain-50 transition-colors">
                Run all
              </button>
            </div>
          )}
        </div>
        )}
      </div>
    </div>
  );
}


