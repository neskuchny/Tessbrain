import { create } from "zustand";
import {
  Connection,
  EdgeChange,
  NodeChange,
  addEdge,
  applyNodeChanges,
  applyEdgeChanges,
  XYPosition,
} from "@xyflow/react";
import type {
  WorkflowNode,
  WorkflowEdge,
  NodeType,
  ImageInputNodeData,
  AnnotationNodeData,
  PromptNodeData,
  TextCombinerNodeData,
  NanoBananaNodeData,
  LLMGenerateNodeData,
  OutputNodeData,
  InfographicNodeData,
  AudioNodeData,
  NoteNodeData,
  ProcessNodeData,
  WorkflowNodeData,
  ImageHistoryItem,
} from "@/banana/types";
import { useToast } from "@/banana/components/Toast";

// Заголовки для вызовов /api/banana/*. Authorization подставляет глобальный
// патч fetch (lib/authFetch) — со СВЕЖИМ токеном (авто-refresh); ручной токен
// из localStorage здесь был бы протухшим через час и «Run ничего не делает».
function bananaHeaders(): Record<string, string> {
  return { "Content-Type": "application/json" };
}

export type EdgeStyle = "angular" | "curved";
export type { WorkflowNode, WorkflowEdge } from "@/banana/types";

// Workflow file format
export interface WorkflowFile {
  version: 1;
  name: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  edgeStyle: EdgeStyle;
}

// Clipboard data structure for copy/paste
interface ClipboardData {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
}

export type BoardKind = "creative" | "process";

interface WorkflowStore {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  edgeStyle: EdgeStyle;
  boardKind: BoardKind;   // creative (ИИ-холст) | process (конвейер, P3)
  boardId: string | null; // id текущей доски на сервере (для сохранения/прогона)
  boardName: string | null; // имя текущей доски (из шаблона/сохранения) — чтобы Run не перезаписывал именем «Процесс»
  setBoardName: (name: string | null) => void;
  clipboard: ClipboardData | null;

  // Settings
  setEdgeStyle: (style: EdgeStyle) => void;
  setBoardKind: (kind: BoardKind) => void;
  setBoardId: (id: string | null) => void;

  // Node operations
  addNode: (type: NodeType, position: XYPosition) => string;
  updateNodeData: (nodeId: string, data: Partial<WorkflowNodeData>) => void;
  removeNode: (nodeId: string) => void;
  onNodesChange: (changes: NodeChange<WorkflowNode>[]) => void;

  // Edge operations
  onEdgesChange: (changes: EdgeChange<WorkflowEdge>[]) => void;
  onConnect: (connection: Connection) => void;
  removeEdge: (edgeId: string) => void;
  toggleEdgePause: (edgeId: string) => void;

  // Copy/Paste operations
  copySelectedNodes: () => void;
  pasteNodes: (offset?: XYPosition) => void;

  // Execution
  isRunning: boolean;
  currentNodeId: string | null;
  pausedAtNodeId: string | null;
  executeWorkflow: (startFromNodeId?: string) => Promise<void>;
  regenerateNode: (nodeId: string) => Promise<void>;
  stopWorkflow: () => void;

  // Save/Load
  saveWorkflow: (name?: string) => void;
  loadWorkflow: (workflow: WorkflowFile) => void;
  clearWorkflow: () => void;

  // Helpers
  getNodeById: (id: string) => WorkflowNode | undefined;
  getConnectedInputs: (nodeId: string) => { images: string[]; text: string | null };
  validateWorkflow: () => { valid: boolean; errors: string[] };
  autoLayout: () => void;

  // Global Image History
  globalImageHistory: ImageHistoryItem[];
  addToGlobalHistory: (item: Omit<ImageHistoryItem, "id">) => void;
  clearGlobalHistory: () => void;
}

// Габариты узлов по типам — ЕДИНЫЙ источник: addNode задаёт их новым
// узлам, loadWorkflow досыпает узлам из шаблонов/старых досок (без style
// узел рендерился «натуральной» шириной — textCombiner расползался на
// ~450px и ложился на соседей).
export const NODE_DIMENSIONS: Record<NodeType, { width: number; height: number }> = {
  imageInput: { width: 300, height: 280 },
  annotation: { width: 300, height: 280 },
  prompt: { width: 320, height: 220 },
  textCombiner: { width: 320, height: 380 },
  nanoBanana: { width: 300, height: 300 },
  llmGenerate: { width: 320, height: 360 },
  infographic: { width: 300, height: 280 },
  audio: { width: 260, height: 220 },
  output: { width: 320, height: 320 },
  note: { width: 260, height: 160 },
  trigger: { width: 240, height: 150 },
  ask_brain: { width: 280, height: 200 },
  report: { width: 240, height: 160 },
  notify: { width: 280, height: 210 },
  task: { width: 240, height: 160 },
  action: { width: 280, height: 240 },
  generate: { width: 280, height: 200 },
  condition: { width: 240, height: 180 },
  wait_reply: { width: 280, height: 220 },
  meeting_data: { width: 260, height: 200 },
  meeting_share: { width: 260, height: 210 },
  document: { width: 280, height: 200 },
  crm_data: { width: 260, height: 180 },
  web_search: { width: 260, height: 150 },
  coding_agent: { width: 280, height: 190 },
  report_xlsx: { width: 260, height: 180 },
  translate: { width: 260, height: 140 },
  doc_edit: { width: 260, height: 150 },
  crm_write: { width: 260, height: 210 },
};

const createDefaultNodeData = (type: NodeType): WorkflowNodeData => {
  switch (type) {
    case "imageInput":
      return {
        image: null,
        filename: null,
        dimensions: null,
      } as ImageInputNodeData;
    case "annotation":
      return {
        sourceImage: null,
        annotations: [],
        outputImage: null,
      } as AnnotationNodeData;
    case "prompt":
      return {
        prompt: "",
      } as PromptNodeData;
    case "textCombiner":
      return {
        template: `Context:\n{{context}}\n\nTask:\n{{task}}`,
        inputs: {},
        outputText: null,
      } as TextCombinerNodeData;
    case "nanoBanana":
      return {
        inputImages: [],
        inputPrompt: null,
        outputImage: null,
        outputVideo: null,
        aspectRatio: "1:1",
        resolution: "1K",
        model: "nano-banana-pro",
        useGoogleSearch: false,
        gptImageSize: "auto",
        gptImageQuality: "medium",
        status: "idle",
        error: null,
      } as NanoBananaNodeData;
    case "llmGenerate":
      return {
        inputPrompt: null,
        outputText: null,
        // Дефолт платформы — DeepSeek v4 Pro (ключ берётся из «Интеграций»).
        provider: "deepseek",
        model: "deepseek-v4-pro",
        temperature: 0.7,
        maxTokens: 1024,
        status: "idle",
        error: null,
      } as LLMGenerateNodeData;
    case "infographic":
      return {
        model: "nano-banana",
        image: null,
      } as InfographicNodeData;
    case "audio":
      return {
        provider: "openai",
        voice: "alloy",
      } as AudioNodeData;
    case "output":
      return {
        image: null,
      } as OutputNodeData;
    case "note":
      return {
        text: "",
      } as NoteNodeData;
    case "trigger":
      return { payload: "" } as ProcessNodeData;
    case "ask_brain":
      return { prompt: "" } as ProcessNodeData;
    case "report":
      return { report_type: "summary" } as ProcessNodeData;
    case "notify":
      return { text: "", channel: "telegram" } as ProcessNodeData;
    case "task":
      return { title: "" } as ProcessNodeData;
    case "action":
      return { tool_name: "", params: {} } as ProcessNodeData;
    case "condition":
      return { contains: "", op: "contains" } as ProcessNodeData;
    case "generate":
      return { prompt: "" } as ProcessNodeData;
    case "wait_reply":
      return { text: "", chat_id: "", timeout_min: 0 } as ProcessNodeData;
    case "meeting_data":
      return { kind: "report", meeting_id: "" } as ProcessNodeData;
    case "meeting_share":
      return { access_level: "view", meeting_id: "" } as ProcessNodeData;
    case "document":
      return { doc_kind: "kp", custom_prompt: "" } as ProcessNodeData;
    case "crm_data":
      return { query: "", dataset_id: "" } as ProcessNodeData;
    case "web_search":
      return { query: "" } as ProcessNodeData;
    case "coding_agent":
      return { mode: "document", agent: "claude", repo_path: "" } as ProcessNodeData;
    case "report_xlsx":
      // Заголовок оставляем пустым: это РЕДАКТИРУЕМОЕ поле узла, и русский
      // дефолт «Отчёт» показывался англоязычному пользователю прямо в input.
      // Подпись самого узла берётся из типа и переводится (см. TITLE_KEYS),
      // а в поле работает placeholder.
      return { title: "", instruction: "" } as ProcessNodeData;
    case "translate":
      return { target_lang: "en" } as ProcessNodeData;
    case "doc_edit":
      return { instruction: "" } as ProcessNodeData;
    case "crm_write":
      return { provider: "amocrm", op: "create", title: "", value: "" } as ProcessNodeData;
  }
};

let nodeIdCounter = 0;

// Владелец текущего холста. Zustand-store — модульный синглтон и переживает
// смену аккаунта в том же SPA-сеансе: без сброса доска юзера A оставалась
// на экране у юзера B («открыл в одном аккаунте — открылось и в другом»).
let boardOwnerUid: string | null = null;

export function ensureBoardOwner(uid: string | null): void {
  if (uid === boardOwnerUid) return;
  const hadContent = useWorkflowStore.getState().nodes.length > 0;
  boardOwnerUid = uid;
  if (hadContent || useWorkflowStore.getState().boardId) {
    useWorkflowStore.setState({
      nodes: [], edges: [], boardId: null, clipboard: null,
      globalImageHistory: [], isRunning: false,
      currentNodeId: null, pausedAtNodeId: null,
    });
  }
}

export const useWorkflowStore = create<WorkflowStore>((set, get) => ({
  nodes: [],
  edges: [],
  edgeStyle: "curved" as EdgeStyle,
  boardKind: "creative" as BoardKind,
  boardId: null,
  boardName: null,
  setBoardName: (name: string | null) => set({ boardName: name }),
  clipboard: null,
  isRunning: false,
  currentNodeId: null,
  pausedAtNodeId: null,
  globalImageHistory: [],

  setEdgeStyle: (style: EdgeStyle) => {
    set({ edgeStyle: style });
  },

  setBoardKind: (kind: BoardKind) => {
    set({ boardKind: kind });
  },

  setBoardId: (id: string | null) => {
    set({ boardId: id });
  },

  addNode: (type: NodeType, position: XYPosition) => {
    const id = `${type}-${++nodeIdCounter}`;

    // Default dimensions based on node type
    const { width, height } = NODE_DIMENSIONS[type];

    const newNode: WorkflowNode = {
      id,
      type,
      position,
      data: createDefaultNodeData(type),
      style: { width, height },
    };

    set((state) => ({
      nodes: [...state.nodes, newNode],
    }));

    return id;
  },

  updateNodeData: (nodeId: string, data: Partial<WorkflowNodeData>) => {
    set((state) => ({
      nodes: state.nodes.map((node) =>
        node.id === nodeId ? { ...node, data: { ...node.data, ...data } as WorkflowNodeData } : node
      ) as WorkflowNode[],
    }));
  },

  removeNode: (nodeId: string) => {
    set((state) => ({
      nodes: state.nodes.filter((node) => node.id !== nodeId),
      edges: state.edges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId),
    }));
  },

  onNodesChange: (changes: NodeChange<WorkflowNode>[]) => {
    set((state) => ({
      nodes: applyNodeChanges(changes, state.nodes),
    }));
  },

  onEdgesChange: (changes: EdgeChange<WorkflowEdge>[]) => {
    set((state) => ({
      edges: applyEdgeChanges(changes, state.edges),
    }));
  },

  onConnect: (connection: Connection) => {
    set((state) => ({
      edges: addEdge(
        {
          ...connection,
          id: `edge-${connection.source}-${connection.target}-${connection.sourceHandle || "default"}-${connection.targetHandle || "default"}`,
        },
        state.edges
      ),
    }));
  },

  removeEdge: (edgeId: string) => {
    set((state) => ({
      edges: state.edges.filter((edge) => edge.id !== edgeId),
    }));
  },

  toggleEdgePause: (edgeId: string) => {
    set((state) => ({
      edges: state.edges.map((edge) => (edge.id === edgeId ? { ...edge, data: { ...edge.data, hasPause: !edge.data?.hasPause } } : edge)),
    }));
  },

  copySelectedNodes: () => {
    const { nodes, edges } = get();
    const selectedNodes = nodes.filter((node) => node.selected);
    if (selectedNodes.length === 0) return;

    const selectedNodeIds = new Set(selectedNodes.map((n) => n.id));

    // Copy edges that connect selected nodes to each other
    const connectedEdges = edges.filter((edge) => selectedNodeIds.has(edge.source) && selectedNodeIds.has(edge.target));

    // Deep clone to avoid reference issues
    const clonedNodes = JSON.parse(JSON.stringify(selectedNodes)) as WorkflowNode[];
    const clonedEdges = JSON.parse(JSON.stringify(connectedEdges)) as WorkflowEdge[];

    set({ clipboard: { nodes: clonedNodes, edges: clonedEdges } });
  },

  pasteNodes: (offset: XYPosition = { x: 50, y: 50 }) => {
    const { clipboard, nodes, edges } = get();
    if (!clipboard || clipboard.nodes.length === 0) return;

    // mapping old node IDs to new node IDs
    const idMapping = new Map<string, string>();
    clipboard.nodes.forEach((node) => {
      const newId = `${node.type}-${++nodeIdCounter}`;
      idMapping.set(node.id, newId);
    });

    const newNodes: WorkflowNode[] = clipboard.nodes.map((node) => ({
      ...node,
      id: idMapping.get(node.id)!,
      position: { x: node.position.x + offset.x, y: node.position.y + offset.y },
      selected: true,
      data: { ...node.data },
    }));

    const newEdges: WorkflowEdge[] = clipboard.edges.map((edge) => ({
      ...edge,
      id: `edge-${idMapping.get(edge.source)}-${idMapping.get(edge.target)}-${edge.sourceHandle || "default"}-${edge.targetHandle || "default"}`,
      source: idMapping.get(edge.source)!,
      target: idMapping.get(edge.target)!,
    }));

    const updatedNodes = nodes.map((node) => ({ ...node, selected: false }));

    set({
      nodes: [...updatedNodes, ...newNodes] as WorkflowNode[],
      edges: [...edges, ...newEdges],
    });
  },

  getNodeById: (id: string) => get().nodes.find((node) => node.id === id),

  getConnectedInputs: (nodeId: string) => {
    const { edges, nodes } = get();
    const images: string[] = [];
    let text: string | null = null;

    edges
      .filter((edge) => edge.target === nodeId)
      .forEach((edge) => {
        const sourceNode = nodes.find((n) => n.id === edge.source);
        if (!sourceNode) return;

        const handleId = edge.targetHandle;

        if (handleId === "image" || !handleId) {
          // collect all connected images
          if (sourceNode.type === "imageInput") {
            const sourceImage = (sourceNode.data as ImageInputNodeData).image;
            if (sourceImage) images.push(sourceImage);
          } else if (sourceNode.type === "annotation") {
            const sourceImage = (sourceNode.data as AnnotationNodeData).outputImage;
            if (sourceImage) images.push(sourceImage);
          } else if (sourceNode.type === "nanoBanana") {
            const sourceImage = (sourceNode.data as NanoBananaNodeData).outputImage;
            if (sourceImage) images.push(sourceImage);
          }
        }

        if (handleId === "text") {
          if (sourceNode.type === "prompt") {
            text = (sourceNode.data as PromptNodeData).prompt;
          } else if (sourceNode.type === "llmGenerate") {
            text = (sourceNode.data as LLMGenerateNodeData).outputText;
          } else if (sourceNode.type === "textCombiner") {
            text = (sourceNode.data as TextCombinerNodeData).outputText;
          }
        }
      });

    return { images, text };
  },

  validateWorkflow: () => {
    const { nodes, edges } = get();
    const errors: string[] = [];

    if (nodes.length === 0) {
      errors.push("Workflow is empty");
      return { valid: false, errors };
    }

    // Генерации нужен текстовый вход; картинки-референсы опциональны у ВСЕХ
    // моделей (Google-модели умеют text-to-image так же, как OpenAI).
    nodes
      .filter((n) => n.type === "nanoBanana")
      .forEach((node) => {
        const textConnected = edges.some((e) => e.target === node.id && e.targetHandle === "text");
        if (!textConnected) errors.push(`Generate node "${node.id}" missing text input`);
      });

    // Annotation should have an input (connected or manual)
    nodes
      .filter((n) => n.type === "annotation")
      .forEach((node) => {
        const imageConnected = edges.some((e) => e.target === node.id);
        const hasManualImage = (node.data as AnnotationNodeData).sourceImage !== null;
        if (!imageConnected && !hasManualImage) errors.push(`Annotation node "${node.id}" missing image input`);
      });

    // Output should have image input
    nodes
      .filter((n) => n.type === "output")
      .forEach((node) => {
        const imageConnected = edges.some((e) => e.target === node.id);
        if (!imageConnected) errors.push(`Output node "${node.id}" missing image input`);
      });

    return { valid: errors.length === 0, errors };
  },

  executeWorkflow: async (startFromNodeId?: string) => {
    const { nodes, edges, updateNodeData, getConnectedInputs, isRunning } = get();
    if (isRunning) return;

    const isResuming = startFromNodeId === get().pausedAtNodeId;
    set({ isRunning: true, pausedAtNodeId: null });

    // Topological sort
    const sorted: WorkflowNode[] = [];
    const visited = new Set<string>();
    const visiting = new Set<string>();

    const visit = (nodeId: string) => {
      if (visited.has(nodeId)) return;
      if (visiting.has(nodeId)) throw new Error("Cycle detected in workflow");

      visiting.add(nodeId);
      edges.filter((e) => e.target === nodeId).forEach((e) => visit(e.source));
      visiting.delete(nodeId);
      visited.add(nodeId);

      const node = nodes.find((n) => n.id === nodeId);
      if (node) sorted.push(node);
    };

    try {
      nodes.forEach((node) => visit(node.id));

      let startIndex = 0;
      if (startFromNodeId) {
        const nodeIndex = sorted.findIndex((n) => n.id === startFromNodeId);
        if (nodeIndex !== -1) startIndex = nodeIndex;
      }

      for (let i = startIndex; i < sorted.length; i++) {
        const node = sorted[i];
        if (!get().isRunning) break;

        // pause edges
        const isResumingThisNode = isResuming && node.id === startFromNodeId;
        if (!isResumingThisNode) {
          const incomingEdges = edges.filter((e) => e.target === node.id);
          const pauseEdge = incomingEdges.find((e) => e.data?.hasPause);
          if (pauseEdge) {
            set({ pausedAtNodeId: node.id, isRunning: false, currentNodeId: null });
            useToast.getState().show("Workflow paused - click Run to continue", "warning");
            return;
          }
        }

        set({ currentNodeId: node.id });

        switch (node.type) {
          case "imageInput":
            break;

          case "annotation": {
            const { images } = getConnectedInputs(node.id);
            const image = images[0] || null;
            if (image) {
              updateNodeData(node.id, { sourceImage: image });
              const nodeData = node.data as AnnotationNodeData;
              if (!nodeData.outputImage) updateNodeData(node.id, { outputImage: image });
            }
            break;
          }

          case "prompt":
            break;

          case "textCombiner": {
            // Collect all text inputs connected to this node
            const nodeData = node.data as TextCombinerNodeData;
            const inputValues: Record<string, string | null> = {};
            
            // Find all edges targeting this node
            edges
              .filter((edge) => edge.target === node.id)
              .forEach((edge) => {
                const sourceNode = nodes.find((n) => n.id === edge.source);
                if (!sourceNode) return;
                
                const handleId = edge.targetHandle || "default";
                let textValue: string | null = null;
                
                if (sourceNode.type === "prompt") {
                  textValue = (sourceNode.data as PromptNodeData).prompt;
                } else if (sourceNode.type === "llmGenerate") {
                  textValue = (sourceNode.data as LLMGenerateNodeData).outputText;
                } else if (sourceNode.type === "textCombiner") {
                  textValue = (sourceNode.data as TextCombinerNodeData).outputText;
                }
                
                if (textValue) {
                  inputValues[handleId] = textValue;
                }
              });
            
            // Apply template substitution
            let outputText = nodeData.template;
            const placeholderRegex = /\{\{(\w+)\}\}/g;
            let match;
            while ((match = placeholderRegex.exec(nodeData.template)) !== null) {
              const placeholder = match[1];
              const value = inputValues[placeholder];
              if (value) {
                outputText = outputText.replace(new RegExp(`\\{\\{${placeholder}\\}\\}`, "g"), value);
              }
            }
            
            updateNodeData(node.id, { inputs: inputValues, outputText });
            break;
          }

          case "nanoBanana": {
            const { images, text } = getConnectedInputs(node.id);
            const isVeo = String((node.data as NanoBananaNodeData).model || "").startsWith("veo");
            if (isVeo) {
              // Видео (Veo, тот же GEMINI-ключ): text-to-video или
              // image-to-video (первый кадр). Выход терминальный.
              if (!text) {
                updateNodeData(node.id, { status: "error", error: "i18n:err_need_prompt" });
                set({ isRunning: false, currentNodeId: null });
                return;
              }
              updateNodeData(node.id, { inputPrompt: text, status: "loading", error: null, outputVideo: null });
              try {
                const vres = await fetch("/api/banana/video", {
                  method: "POST",
                  headers: bananaHeaders(),
                  body: JSON.stringify({
                    prompt: text,
                    model: (node.data as NanoBananaNodeData).model,
                    image: images[0] || undefined,
                  }),
                });
                const vjson = await vres.json().catch(() => ({}));
                if (!vres.ok || !vjson.success) {
                  updateNodeData(node.id, { status: "error", error: vjson.error || `HTTP ${vres.status}` });
                  set({ isRunning: false, currentNodeId: null });
                  return;
                }
                updateNodeData(node.id, { outputVideo: vjson.video, status: "complete", error: null });
              } catch (e) {
                updateNodeData(node.id, { status: "error", error: e instanceof Error ? e.message : String(e) });
                set({ isRunning: false, currentNodeId: null });
                return;
              }
              break;
            }
            {
              // Картинка на входе ОПЦИОНАЛЬНА для всех моделей: Google-модели
              // (nano-banana*) умеют text-to-image так же, как OpenAI —
              // бэкенд шлёт parts=[prompt] без картинок. Референсы, если
              // подключены и заполнены, уходят все (мульти-референс).
              if (!text) {
                updateNodeData(node.id, { status: "error", error: "i18n:err_need_prompt" });
                set({ isRunning: false, currentNodeId: null });
                return;
              }
            }

            updateNodeData(node.id, { inputImages: images, inputPrompt: text, status: "loading", error: null });

            try {
              const nodeData = node.data as NanoBananaNodeData;
              const response = await fetch("/api/banana/generate", {
                method: "POST",
                headers: bananaHeaders(),
                body: JSON.stringify({
                  images,
                  prompt: text,
                  aspectRatio: nodeData.aspectRatio,
                  resolution: nodeData.resolution,
                  model: nodeData.model,
                  useGoogleSearch: nodeData.useGoogleSearch,
                  gptImageSize: nodeData.gptImageSize,
                  gptImageQuality: nodeData.gptImageQuality,
                }),
              });

              if (!response.ok) {
                const errorText = await response.text();
                let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
                try {
                  const errorJson = JSON.parse(errorText);
                  errorMessage = errorJson.error || errorMessage;
                } catch {
                  if (errorText) errorMessage += ` - ${errorText.substring(0, 200)}`;
                }
                updateNodeData(node.id, { status: "error", error: errorMessage });
                set({ isRunning: false, currentNodeId: null });
                return;
              }

              const result = await response.json();
              if (result.success && result.image) {
                get().addToGlobalHistory({
                  image: result.image,
                  timestamp: Date.now(),
                  prompt: text,
                  aspectRatio: nodeData.aspectRatio,
                  model: nodeData.model,
                });
                updateNodeData(node.id, { outputImage: result.image, status: "complete", error: null });
              } else {
                updateNodeData(node.id, { status: "error", error: result.error || "Generation failed" });
                set({ isRunning: false, currentNodeId: null });
                return;
              }
            } catch (error) {
              const errorMessage = error instanceof Error ? error.message : "Generation failed";
              updateNodeData(node.id, { status: "error", error: errorMessage });
              set({ isRunning: false, currentNodeId: null });
              return;
            }

            break;
          }

          case "llmGenerate": {
            const { text } = getConnectedInputs(node.id);
            if (!text) {
              updateNodeData(node.id, { status: "error", error: "Missing text input" });
              set({ isRunning: false, currentNodeId: null });
              return;
            }

            updateNodeData(node.id, { inputPrompt: text, status: "loading", error: null });

            try {
              const nodeData = node.data as LLMGenerateNodeData;
              const response = await fetch("/api/banana/llm", {
                method: "POST",
                headers: bananaHeaders(),
                body: JSON.stringify({
                  prompt: text,
                  provider: nodeData.provider,
                  model: nodeData.model,
                  temperature: nodeData.temperature,
                  maxTokens: nodeData.maxTokens,
                }),
              });

              if (!response.ok) {
                const errorText = await response.text();
                let errorMessage = `HTTP ${response.status}`;
                try {
                  const errorJson = JSON.parse(errorText);
                  errorMessage = errorJson.error || errorMessage;
                } catch {
                  if (errorText) errorMessage += ` - ${errorText.substring(0, 200)}`;
                }
                updateNodeData(node.id, { status: "error", error: errorMessage });
                set({ isRunning: false, currentNodeId: null });
                return;
              }

              const result = await response.json();
              if (result.success && result.text) {
                updateNodeData(node.id, { outputText: result.text, status: "complete", error: null });
              } else {
                updateNodeData(node.id, { status: "error", error: result.error || "LLM generation failed" });
                set({ isRunning: false, currentNodeId: null });
                return;
              }
            } catch (error) {
              updateNodeData(node.id, { status: "error", error: error instanceof Error ? error.message : "LLM generation failed" });
              set({ isRunning: false, currentNodeId: null });
              return;
            }

            break;
          }

          case "output": {
            const { images } = getConnectedInputs(node.id);
            const image = images[0] || null;
            if (image) updateNodeData(node.id, { image });
            break;
          }
        }
      }

      set({ isRunning: false, currentNodeId: null });
    } catch (e) {
      // Тихий catch делал «Run ничего не делает»: цикл в графе или падение
      // до первого узла заканчивались молча. Теперь — тост с причиной.
      set({ isRunning: false, currentNodeId: null });
      try {
        useToast.getState().show(
          e instanceof Error ? e.message : "i18n:err_run_failed", "error");
      } catch { /* noop */ }
    }
  },

  stopWorkflow: () => set({ isRunning: false, currentNodeId: null }),

  regenerateNode: async (nodeId: string) => {
    const { nodes, updateNodeData, getConnectedInputs, isRunning } = get();
    if (isRunning) return;

    const node = nodes.find((n) => n.id === nodeId);
    if (!node) return;

    set({ isRunning: true, currentNodeId: nodeId });

    try {
      if (node.type === "nanoBanana") {
        const nodeData = node.data as NanoBananaNodeData;
        const inputs = getConnectedInputs(nodeId);
        const images = inputs.images.length > 0 ? inputs.images : nodeData.inputImages;
        const text = inputs.text ?? nodeData.inputPrompt;

        if (!images || images.length === 0 || !text) {
          updateNodeData(nodeId, { status: "error", error: "Missing image or text input" });
          set({ isRunning: false, currentNodeId: null });
          return;
        }

        updateNodeData(nodeId, { status: "loading", error: null });

        const response = await fetch("/api/banana/generate", {
          method: "POST",
          headers: bananaHeaders(),
          body: JSON.stringify({
            images,
            prompt: text,
            aspectRatio: nodeData.aspectRatio,
            resolution: nodeData.resolution,
            model: nodeData.model,
            useGoogleSearch: nodeData.useGoogleSearch,
            gptImageSize: nodeData.gptImageSize,
            gptImageQuality: nodeData.gptImageQuality,
          }),
        });

        if (!response.ok) {
          const errorText = await response.text();
          let errorMessage = `HTTP ${response.status}`;
          try {
            const errorJson = JSON.parse(errorText);
            errorMessage = errorJson.error || errorMessage;
          } catch {
            if (errorText) errorMessage += ` - ${errorText.substring(0, 200)}`;
          }
          updateNodeData(nodeId, { status: "error", error: errorMessage });
          set({ isRunning: false, currentNodeId: null });
          return;
        }

        const result = await response.json();
        if (result.success && result.image) {
          get().addToGlobalHistory({
            image: result.image,
            timestamp: Date.now(),
            prompt: text,
            aspectRatio: nodeData.aspectRatio,
            model: nodeData.model,
          });
          updateNodeData(nodeId, { outputImage: result.image, status: "complete", error: null });
        } else {
          updateNodeData(nodeId, { status: "error", error: result.error || "Generation failed" });
        }
      } else if (node.type === "llmGenerate") {
        const nodeData = node.data as LLMGenerateNodeData;
        const inputs = getConnectedInputs(nodeId);
        const text = inputs.text ?? nodeData.inputPrompt;

        if (!text) {
          updateNodeData(nodeId, { status: "error", error: "Missing text input" });
          set({ isRunning: false, currentNodeId: null });
          return;
        }

        updateNodeData(nodeId, { status: "loading", error: null });

        const response = await fetch("/api/banana/llm", {
          method: "POST",
          headers: bananaHeaders(),
          body: JSON.stringify({
            prompt: text,
            provider: nodeData.provider,
            model: nodeData.model,
            temperature: nodeData.temperature,
            maxTokens: nodeData.maxTokens,
          }),
        });

        if (!response.ok) {
          const errorText = await response.text();
          let errorMessage = `HTTP ${response.status}`;
          try {
            const errorJson = JSON.parse(errorText);
            errorMessage = errorJson.error || errorMessage;
          } catch {
            if (errorText) errorMessage += ` - ${errorText.substring(0, 200)}`;
          }
          updateNodeData(nodeId, { status: "error", error: errorMessage });
          set({ isRunning: false, currentNodeId: null });
          return;
        }

        const result = await response.json();
        if (result.success && result.text) {
          updateNodeData(nodeId, { outputText: result.text, status: "complete", error: null });
        } else {
          updateNodeData(nodeId, { status: "error", error: result.error || "LLM generation failed" });
        }
      }

      set({ isRunning: false, currentNodeId: null });
    } catch (error) {
      updateNodeData(nodeId, { status: "error", error: error instanceof Error ? error.message : "Regeneration failed" });
      set({ isRunning: false, currentNodeId: null });
    }
  },

  saveWorkflow: (name?: string) => {
    const { nodes, edges, edgeStyle } = get();
    const workflow: WorkflowFile = {
      version: 1,
      name: name || `workflow-${new Date().toISOString().slice(0, 10)}`,
      nodes,
      edges,
      edgeStyle,
    };

    const json = JSON.stringify(workflow, null, 2);
    const blob = new Blob([json], { type: "application/json" });
    const url = URL.createObjectURL(blob);

    const link = document.createElement("a");
    link.href = url;
    link.download = `${workflow.name}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  },

  loadWorkflow: (workflow: WorkflowFile) => {
    const maxId = workflow.nodes.reduce((max, node) => {
      const match = node.id.match(/-(\d+)$/);
      if (match) return Math.max(max, parseInt(match[1], 10));
      return max;
    }, 0);
    nodeIdCounter = maxId;

    // Нормализация рёбер без handle-меток (старые шаблоны/сохранённые доски):
    // getConnectedInputs читает текст ТОЛЬКО с targetHandle="text" — ребро
    // prompt→nanoBanana без метки молча теряло промпт, и Run падал валидацией
    // («не рабочий воркфлоу»). Выводим метки из типов узлов; textCombiner-входы
    // не трогаем (там handle = имя плейсхолдера, его не угадать).
    const TEXT_SOURCES = new Set(["prompt", "llmGenerate", "textCombiner"]);
    const IMAGE_SOURCES = new Set(["imageInput", "annotation", "nanoBanana"]);
    const byId = new Map(workflow.nodes.map((n) => [n.id, n]));
    const normalizedEdges = workflow.edges.map((e) => {
      if (e.sourceHandle && e.targetHandle) return e;
      const st = String(byId.get(e.source)?.type || "");
      const tt = String(byId.get(e.target)?.type || "");
      let sourceHandle = e.sourceHandle;
      let targetHandle = e.targetHandle;
      if (TEXT_SOURCES.has(st)) {
        sourceHandle = sourceHandle || "text";
        if (!targetHandle && tt !== "textCombiner") targetHandle = "text";
      } else if (IMAGE_SOURCES.has(st)) {
        sourceHandle = sourceHandle || "image";
        targetHandle = targetHandle || "image";
      }
      if (sourceHandle === e.sourceHandle && targetHandle === e.targetHandle) return e;
      return { ...e, sourceHandle, targetHandle };
    });
    // Габариты узлам без style (шаблоны, старые/сгенерированные доски):
    // иначе узел рендерится «натуральной» шириной и ложится на соседей.
    const sizedNodes = workflow.nodes.map((n) => {
      const st: any = (n as any).style;
      if (st && st.width) return n;
      const dims = NODE_DIMENSIONS[(n.type || "prompt") as NodeType];
      if (!dims) return n;
      return { ...n, style: { ...(st || {}), ...dims } } as WorkflowNode;
    });
    workflow = { ...workflow, nodes: sizedNodes, edges: normalizedEdges };

    // Режим доски определяется СОДЕРЖИМЫМ: процесс-шаблон, загруженный в
    // режиме «Креатив», раньше оставлял старый режим — Run был неактивен
    // («не переключается автоматом с креатива на триггеры»).
    // ПОЛНЫЙ набор процесс-узлов (аудит: урезанный список из 8 типов не
    // переключал доску из «только web_search+document» и т.п. в процесс-режим).
    const processTypes = new Set([
      "trigger", "ask_brain", "report", "notify", "task", "action", "generate",
      "condition", "wait_reply", "meeting_data", "meeting_share", "document",
      "crm_data", "web_search", "report_xlsx", "translate", "doc_edit",
      "crm_write", "coding_agent",
    ]);
    const isProcess = workflow.nodes.some(
      (n) => processTypes.has(String(n.type || "")));

    set({
      nodes: workflow.nodes,
      edges: workflow.edges,
      edgeStyle: workflow.edgeStyle || "angular",
      boardKind: isProcess ? "process" : "creative",
      // имя из шаблона/файла — Run сохранит доску под ним, а не «Процесс»
      boardName: (workflow.name || "").trim() || null,
      // Загрузка шаблона/файла = НОВЫЙ несохранённый холст. Раньше boardId
      // оставался от прошлой доски → Run перезаписывал её содержимым шаблона
      // (потеря данных) — либо, при null, каждый Run плодил доску-копию.
      // «Мои доски» ставит boardId сам сразу после loadWorkflow.
      boardId: null,
      isRunning: false,
      currentNodeId: null,
    });
  },

  clearWorkflow: () => set({ nodes: [], edges: [], boardName: null, isRunning: false, currentNodeId: null }),

  // «Разложить»: пере-раскладка доски по слоям топологической сортировки
  // с РЕАЛЬНО измеренными размерами узлов (React Flow заполняет
  // node.measured после рендера). Лечит «карточки одна на другой» у досок,
  // собранных до фикса раскладки, и у любых захламлённых схем. Note-узлы
  // (без связей) уходят отдельной колонкой слева.
  autoLayout: () => {
    const { nodes, edges } = get();
    if (nodes.length === 0) return;
    const W = (n: WorkflowNode) =>
      (n as any).measured?.width || (n as any).width || 300;
    const H = (n: WorkflowNode) =>
      (n as any).measured?.height || (n as any).height || 240;

    const indeg: Record<string, number> = {};
    const adj: Record<string, string[]> = {};
    nodes.forEach((n) => { indeg[n.id] = 0; });
    edges.forEach((e) => {
      if (!(e.source in indeg) || !(e.target in indeg)) return;
      indeg[e.target] += 1;
      (adj[e.source] ||= []).push(e.target);
    });
    const layer: Record<string, number> = {};
    const queue = nodes.filter((n) => indeg[n.id] === 0).map((n) => n.id);
    queue.forEach((id) => { layer[id] = 0; });
    while (queue.length) {
      const u = queue.shift()!;
      for (const v of adj[u] || []) {
        layer[v] = Math.max(layer[v] ?? 0, layer[u] + 1);
        if (--indeg[v] === 0) queue.push(v);
      }
    }
    nodes.forEach((n) => { layer[n.id] ??= 0; });

    const byId = new Map(nodes.map((n) => [n.id, n]));
    const isNote = (id: string) => byId.get(id)?.type === "note";
    const GAP_X = 70, GAP_Y = 50, X0 = 40, Y0 = 60;

    // ширина каждой колонки = максимум измеренных ширин её узлов
    const colWidth: Record<number, number> = {};
    nodes.forEach((n) => {
      if (isNote(n.id)) return;
      const l = layer[n.id];
      colWidth[l] = Math.max(colWidth[l] || 0, W(n));
    });
    const colX: Record<number, number> = {};
    let x = X0;
    Object.keys(colWidth).map(Number).sort((a, b) => a - b).forEach((l) => {
      colX[l] = x;
      x += (colWidth[l] || 300) + GAP_X;
    });

    const yCursor: Record<string, number> = {};
    const positioned = nodes.map((n) => {
      if (isNote(n.id)) {
        const y = yCursor["note"] ?? Y0;
        yCursor["note"] = y + H(n) + GAP_Y;
        return { ...n, position: { x: X0 - (W(n) + GAP_X), y } };
      }
      const l = layer[n.id];
      const key = String(l);
      const y = yCursor[key] ?? Y0;
      yCursor[key] = y + H(n) + GAP_Y;
      return { ...n, position: { x: colX[l] ?? X0, y } };
    });
    set({ nodes: positioned });
  },

  addToGlobalHistory: (item: Omit<ImageHistoryItem, "id">) => {
    const newItem: ImageHistoryItem = {
      ...item,
      id: `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
    };
    set((state) => ({ globalImageHistory: [newItem, ...state.globalImageHistory] }));
  },

  clearGlobalHistory: () => set({ globalImageHistory: [] }),
}));


