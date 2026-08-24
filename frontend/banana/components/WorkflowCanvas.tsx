"use client";

import { useCallback, useRef, useState, useEffect, DragEvent } from "react";
import { useTranslations } from "next-intl";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Panel,
  NodeTypes,
  EdgeTypes,
  Connection,
  Edge,
  useReactFlow,
  OnConnectEnd,
} from "@xyflow/react";

import { useWorkflowStore, WorkflowFile } from "@/banana/store/workflowStore";
import { ImageInputNode, AnnotationNode, PromptNode, TextCombinerNode, NanoBananaNode, LLMGenerateNode, OutputNode, NoteNode, ProcessNode, InfographicNode, AudioNode } from "./nodes";

import { EditableEdge } from "./edges";
import { ConnectionDropMenu, MenuAction } from "./ConnectionDropMenu";
import { MultiSelectToolbar } from "./MultiSelectToolbar";
import { EdgeToolbar } from "./EdgeToolbar";
import { GlobalImageHistory } from "./GlobalImageHistory";
import { NodeType, NanoBananaNodeData } from "@/banana/types";
import { detectAndSplitGrid } from "@/banana/utils/gridSplitter";

const nodeTypes: NodeTypes = {
  imageInput: ImageInputNode,
  annotation: AnnotationNode,
  prompt: PromptNode,
  textCombiner: TextCombinerNode,
  nanoBanana: NanoBananaNode,
  llmGenerate: LLMGenerateNode,
  infographic: InfographicNode,
  audio: AudioNode,
  output: OutputNode,
  note: NoteNode,
  // Процесс-блоки (все на одном компоненте ProcessNode):
  trigger: ProcessNode,
  ask_brain: ProcessNode,
  report: ProcessNode,
  notify: ProcessNode,
  task: ProcessNode,
  action: ProcessNode,
  generate: ProcessNode,
  condition: ProcessNode,
  wait_reply: ProcessNode,
  meeting_data: ProcessNode,
  meeting_share: ProcessNode,
  document: ProcessNode,
  crm_data: ProcessNode,
  web_search: ProcessNode,
  report_xlsx: ProcessNode,
  translate: ProcessNode,
  doc_edit: ProcessNode,
  crm_write: ProcessNode,
};

const edgeTypes: EdgeTypes = {
  editable: EditableEdge,
};

// Креатив-узлы имеют типизированные хэндлы "image"/"text" — их пайпы должны
// совпадать по типу (картинка↔картинка, текст↔текст). Процесс-узлы (ProcessNode)
// используют ОБЩИЕ хэндлы "in"/"out"/"true"/"false" — они принимают любой пайп
// (бэкенд-движок собирает все входы независимо от типа хэндла). Раньше правило
// рубило связь креатив→процесс (напр. «Картинка» → «Уведомление»): source="image",
// target="in" ⇒ image≠in ⇒ отказ. Из-за этого процесс и креатив «не связывались
// руками». Теперь тип сверяем ТОЛЬКО когда оба хэндла креативные.
const _CREATIVE_HANDLES = new Set(["image", "text"]);
const isValidConnection = (connection: Edge | Connection): boolean => {
  const s = connection.sourceHandle ?? "";
  const t = connection.targetHandle ?? "";
  if (_CREATIVE_HANDLES.has(s) && _CREATIVE_HANDLES.has(t)) {
    return s === t;
  }
  return true;
};

const getNodeHandles = (nodeType: string): { inputs: string[]; outputs: string[] } => {
  switch (nodeType) {
    case "imageInput":
      return { inputs: [], outputs: ["image"] };
    case "annotation":
      return { inputs: ["image"], outputs: ["image"] };
    case "prompt":
      return { inputs: ["text"], outputs: ["text"] };
    case "textCombiner":
      return { inputs: ["text"], outputs: ["text"] }; // Dynamic inputs based on template
    case "nanoBanana":
      return { inputs: ["image", "text"], outputs: ["image"] };
    case "llmGenerate":
      return { inputs: ["text", "image"], outputs: ["text"] };
    case "infographic":
      return { inputs: ["text"], outputs: ["image"] };
    case "audio":
      return { inputs: ["text"], outputs: ["text"] };
    case "output":
      return { inputs: ["image"], outputs: [] };
    default:
      return { inputs: [], outputs: [] };
  }
};

interface ConnectionDropState {
  position: { x: number; y: number };
  flowPosition: { x: number; y: number };
  handleType: "image" | "text" | null;
  connectionType: "source" | "target";
  sourceNodeId: string | null;
  sourceHandleId: string | null;
}

function WorkflowCanvasInner() {
  const t = useTranslations("banana");
  const { nodes, edges, onNodesChange, onEdgesChange, onConnect, addNode, updateNodeData, loadWorkflow, getNodeById, addToGlobalHistory, boardKind, boardName, setBoardName } = useWorkflowStore();
  const { screenToFlowPosition } = useReactFlow();
  const [isDragOver, setIsDragOver] = useState(false);
  const [dropType, setDropType] = useState<"image" | "workflow" | "node" | null>(null);
  const [connectionDrop, setConnectionDrop] = useState<ConnectionDropState | null>(null);
  const [isSplitting, setIsSplitting] = useState(false);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!isValidConnection(connection)) return;

      const selectedNodes = nodes.filter((node) => node.selected);
      const sourceNode = nodes.find((node) => node.id === connection.source);

      if (sourceNode?.selected && selectedNodes.length > 1 && connection.sourceHandle) {
        selectedNodes.forEach((node) => {
          if (node.id === connection.source) {
            onConnect(connection);
            return;
          }
          const nodeHandles = getNodeHandles(node.type || "");
          if (!nodeHandles.outputs.includes(connection.sourceHandle as string)) return;
          const multiConnection: Connection = {
            source: node.id,
            sourceHandle: connection.sourceHandle,
            target: connection.target,
            targetHandle: connection.targetHandle,
          };
          if (isValidConnection(multiConnection)) onConnect(multiConnection);
        });
      } else {
        onConnect(connection);
      }
    },
    [onConnect, nodes]
  );

  const handleConnectEnd: OnConnectEnd = useCallback(
    (event, connectionState) => {
      if (connectionState.isValid || !connectionState.fromNode) return;

      const { clientX, clientY } = event as MouseEvent;
      const fromHandleId = connectionState.fromHandle?.id || null;
      const fromHandleType = fromHandleId === "image" || fromHandleId === "text" ? fromHandleId : null;
      const isFromSource = connectionState.fromHandle?.type === "source";

      const elementsUnderCursor = document.elementsFromPoint(clientX, clientY);
      const nodeElement = elementsUnderCursor.find((el) => el.closest(".react-flow__node"));

      if (nodeElement) {
        const nodeWrapper = nodeElement.closest(".react-flow__node") as HTMLElement;
        const targetNodeId = nodeWrapper?.dataset.id;
        if (targetNodeId && targetNodeId !== connectionState.fromNode.id && fromHandleType) {
          const targetNode = nodes.find((n) => n.id === targetNodeId);
          if (targetNode) {
            const targetHandles = getNodeHandles(targetNode.type || "");
            let compatibleHandle: string | null = null;
            if (isFromSource) {
              if (targetHandles.inputs.includes(fromHandleType)) compatibleHandle = fromHandleType;
            } else {
              if (targetHandles.outputs.includes(fromHandleType)) compatibleHandle = fromHandleType;
            }

            if (compatibleHandle) {
              const connection: Connection = isFromSource
                ? { source: connectionState.fromNode.id, sourceHandle: fromHandleId, target: targetNodeId, targetHandle: compatibleHandle }
                : { source: targetNodeId, sourceHandle: compatibleHandle, target: connectionState.fromNode.id, targetHandle: fromHandleId };
              if (isValidConnection(connection)) {
                handleConnect(connection);
                return;
              }
            }
          }
        }
      }

      const flowPos = screenToFlowPosition({ x: clientX, y: clientY });
      setConnectionDrop({
        position: { x: clientX, y: clientY },
        flowPosition: flowPos,
        handleType: fromHandleType,
        connectionType: isFromSource ? "source" : "target",
        sourceNodeId: connectionState.fromNode.id,
        sourceHandleId: fromHandleId,
      });
    },
    [screenToFlowPosition, nodes, handleConnect]
  );

  const handleSplitGridAction = useCallback(
    async (sourceNodeId: string, flowPosition: { x: number; y: number }) => {
      const sourceNode = getNodeById(sourceNodeId);
      if (!sourceNode) return;

      let sourceImage: string | null = null;
      if (sourceNode.type === "nanoBanana") sourceImage = (sourceNode.data as NanoBananaNodeData).outputImage;
      else if (sourceNode.type === "imageInput") sourceImage = (sourceNode.data as { image: string | null }).image;
      else if (sourceNode.type === "annotation") sourceImage = (sourceNode.data as { outputImage: string | null }).outputImage;

      if (!sourceImage) {
        alert(t("alert_no_image_to_cut"));
        return;
      }

      const sourceNodeData = sourceNode.type === "nanoBanana" ? (sourceNode.data as NanoBananaNodeData) : null;
      setIsSplitting(true);
      try {
        const { grid, images } = await detectAndSplitGrid(sourceImage);
        if (images.length === 0) {
          alert(t("alert_cannot_recognize_grid"));
          setIsSplitting(false);
          return;
        }

        const nodeWidth = 300;
        const nodeHeight = 280;
        const gap = 20;

        images.forEach((imageData: string, index: number) => {
          const row = Math.floor(index / grid.cols);
          const col = index % grid.cols;
          addToGlobalHistory({
            image: imageData,
            timestamp: Date.now() + index,
            prompt: `Split ${row + 1}-${col + 1} from ${grid.rows}x${grid.cols} grid`,
            aspectRatio: sourceNodeData?.aspectRatio || "1:1",
            model: sourceNodeData?.model || "nano-banana",
          });
        });

        images.forEach((imageData: string, index: number) => {
          const row = Math.floor(index / grid.cols);
          const col = index % grid.cols;
          const nodeId = addNode("imageInput", { x: flowPosition.x + col * (nodeWidth + gap), y: flowPosition.y + row * (nodeHeight + gap) });
          const img = new Image();
          img.onload = () => {
            updateNodeData(nodeId, { image: imageData, filename: `split-${row + 1}-${col + 1}.png`, dimensions: { width: img.width, height: img.height } });
          };
          img.src = imageData;
        });
      } catch (error) {
        console.error("[SplitGrid] Error:", error);
        alert("Failed to split image grid: " + (error instanceof Error ? error.message : "Unknown error"));
      } finally {
        setIsSplitting(false);
      }
    },
    [getNodeById, addNode, updateNodeData, addToGlobalHistory]
  );

  const getImageFromNode = useCallback(
    (nodeId: string): string | null => {
      const node = getNodeById(nodeId);
      if (!node) return null;
      switch (node.type) {
        case "imageInput":
          return (node.data as { image: string | null }).image;
        case "annotation":
          return (node.data as { outputImage: string | null }).outputImage;
        case "nanoBanana":
          return (node.data as { outputImage: string | null }).outputImage;
        default:
          return null;
      }
    },
    [getNodeById]
  );

  const handleMenuSelect = useCallback(
    (selection: { type: NodeType | MenuAction; isAction: boolean }) => {
      if (!connectionDrop) return;

      const { flowPosition, sourceNodeId, sourceHandleId, connectionType, handleType } = connectionDrop;

      if (selection.isAction) {
        if (selection.type === "splitGrid" && sourceNodeId) handleSplitGridAction(sourceNodeId, flowPosition);
        setConnectionDrop(null);
        return;
      }

      const nodeType = selection.type as NodeType;
      const newNodeId = addNode(nodeType, flowPosition);

      if (nodeType === "annotation" && connectionType === "source" && handleType === "image" && sourceNodeId) {
        const sourceImage = getImageFromNode(sourceNodeId);
        if (sourceImage) updateNodeData(newNodeId, { sourceImage, outputImage: sourceImage });
      }

      let targetHandleId: string | null = null;
      let sourceHandleIdForNewNode: string | null = null;

      if (handleType === "image") {
        if (nodeType === "annotation" || nodeType === "output" || nodeType === "nanoBanana") targetHandleId = "image";
        else if (nodeType === "imageInput") sourceHandleIdForNewNode = "image";
      } else if (handleType === "text") {
        if (nodeType === "nanoBanana" || nodeType === "llmGenerate") {
          targetHandleId = "text";
          if (nodeType === "llmGenerate") sourceHandleIdForNewNode = "text";
        } else if (nodeType === "prompt") sourceHandleIdForNewNode = "text";
      }

      const selectedNodes = nodes.filter((node) => node.selected);
      const sourceNode = nodes.find((node) => node.id === sourceNodeId);

      if (sourceNode?.selected && selectedNodes.length > 1 && sourceHandleId) {
        selectedNodes.forEach((node) => {
          if (connectionType === "source" && targetHandleId) {
            const connection: Connection = { source: node.id, sourceHandle: sourceHandleId, target: newNodeId, targetHandle: targetHandleId };
            if (isValidConnection(connection)) onConnect(connection);
          } else if (connectionType === "target" && sourceHandleIdForNewNode) {
            const connection: Connection = { source: newNodeId, sourceHandle: sourceHandleIdForNewNode, target: node.id, targetHandle: sourceHandleId };
            if (isValidConnection(connection)) onConnect(connection);
          }
        });
      } else {
        if (connectionType === "source" && sourceNodeId && sourceHandleId && targetHandleId) {
          onConnect({ source: sourceNodeId, sourceHandle: sourceHandleId, target: newNodeId, targetHandle: targetHandleId });
        } else if (connectionType === "target" && sourceNodeId && sourceHandleId && sourceHandleIdForNewNode) {
          onConnect({ source: newNodeId, sourceHandle: sourceHandleIdForNewNode, target: sourceNodeId, targetHandle: sourceHandleId });
        }
      }

      setConnectionDrop(null);
    },
    [connectionDrop, addNode, onConnect, nodes, handleSplitGridAction, getImageFromNode, updateNodeData]
  );

  const handleCloseDropMenu = useCallback(() => setConnectionDrop(null), []);

  const handleDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";

    const hasNodeType = Array.from(event.dataTransfer.types).includes("application/node-type");
    if (hasNodeType) {
      setIsDragOver(true);
      setDropType("node");
      return;
    }
    const hasHistoryImage = Array.from(event.dataTransfer.types).includes("application/history-image");
    if (hasHistoryImage) {
      setIsDragOver(true);
      setDropType("image");
      return;
    }

    const items = Array.from(event.dataTransfer.items);
    const hasImageFile = items.some((item) => item.kind === "file" && item.type.startsWith("image/"));
    const hasJsonFile = items.some((item) => item.kind === "file" && item.type === "application/json");

    if (hasJsonFile) {
      setIsDragOver(true);
      setDropType("workflow");
    } else if (hasImageFile) {
      setIsDragOver(true);
      setDropType("image");
    }
  }, []);

  const handleDragLeave = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragOver(false);
    setDropType(null);
  }, []);

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setIsDragOver(false);
      setDropType(null);

      const nodeType = event.dataTransfer.getData("application/node-type") as NodeType;
      if (nodeType) {
        const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
        addNode(nodeType, position);
        return;
      }

      const historyImageData = event.dataTransfer.getData("application/history-image");
      if (historyImageData) {
        try {
          const { image, prompt } = JSON.parse(historyImageData);
          const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
          const nodeId = addNode("imageInput", position);
          const img = new Image();
          img.onload = () => updateNodeData(nodeId, { image, filename: `history-${Date.now()}.png`, dimensions: { width: img.width, height: img.height } });
          img.src = image;
          void prompt;
          return;
        } catch (err) {
          console.error("Failed to parse history image data:", err);
        }
      }

      const allFiles = Array.from(event.dataTransfer.files);

      const jsonFiles = allFiles.filter((file) => file.type === "application/json" || file.name.endsWith(".json"));
      if (jsonFiles.length > 0) {
        const file = jsonFiles[0];
        const reader = new FileReader();
        reader.onload = (e) => {
          try {
            const workflow = JSON.parse(e.target?.result as string) as WorkflowFile;
            if (workflow.version && workflow.nodes && workflow.edges) loadWorkflow(workflow);
            else alert("Invalid workflow file format");
          } catch {
            alert("Failed to parse workflow file");
          }
        };
        reader.readAsText(file);
        return;
      }

      const imageFiles = allFiles.filter((file) => file.type.startsWith("image/"));
      if (imageFiles.length === 0) return;

      const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      imageFiles.forEach((file, index) => {
        const reader = new FileReader();
        reader.onload = (e) => {
          const dataUrl = e.target?.result as string;
          const img = new Image();
          img.onload = () => {
            const nodeId = addNode("imageInput", { x: position.x + index * 240, y: position.y });
            updateNodeData(nodeId, { image: dataUrl, filename: file.name, dimensions: { width: img.width, height: img.height } });
          };
          img.src = dataUrl;
        };
        reader.readAsDataURL(file);
      });
    },
    [screenToFlowPosition, addNode, updateNodeData, loadWorkflow]
  );

  useEffect(() => {
    const { copySelectedNodes, pasteNodes } = useWorkflowStore.getState();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
      if ((event.ctrlKey || event.metaKey) && event.key === "c") {
        event.preventDefault();
        copySelectedNodes();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key === "v") {
        event.preventDefault();
        pasteNodes();
        return;
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  return (
    <div
      ref={reactFlowWrapper}
      className={`w-full h-full bg-brain-950 relative ${isDragOver ? "ring-2 ring-inset ring-brain-400" : ""}`}
      style={{ minHeight: "400px" }}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {isDragOver && (
        <div className="absolute inset-0 bg-brain-500/10 z-50 pointer-events-none flex items-center justify-center">
          <div className="bg-brain-900 border border-brain-700/60 rounded-lg px-6 py-4 shadow-xl">
            <p className="text-brain-100 text-sm font-medium">
              {dropType === "workflow" ? "Drop to load workflow" : dropType === "node" ? "Drop to create node" : "Drop image to create node"}
            </p>
          </div>
        </div>
      )}

      {isSplitting && (
        <div className="absolute inset-0 bg-black/50 z-50 flex items-center justify-center">
          <div className="bg-brain-900 border border-brain-700/60 rounded-lg px-6 py-4 shadow-xl flex items-center gap-3">
            <div className="w-5 h-5 border-2 border-brain-500 border-t-transparent rounded-full animate-spin" />
            <p className="text-brain-100 text-sm font-medium">Splitting image grid...</p>
          </div>
        </div>
      )}

      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={handleConnect}
        onConnectEnd={handleConnectEnd}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        isValidConnection={isValidConnection}
        fitView
        deleteKeyCode={["Backspace", "Delete"]}
        multiSelectionKeyCode="Shift"
        selectionOnDrag={false}
        panOnDrag
        selectNodesOnDrag={false}
        nodeDragThreshold={5}
        className="banana-flow bg-brain-950"
        defaultEdgeOptions={{ type: "editable", animated: false }}
      >
        <Background color="rgba(224, 239, 254, 0.18)" gap={20} size={1} />
        {/* Название доски прямо на холсте: раньше было «не понятно, какая это
            доска». Всегда видно + клик = переименовать (Enter/blur сохраняет). */}
        <Panel position="top-left" className="!m-3">
          <input
            value={boardName ?? ""}
            onChange={(e) => setBoardName(e.target.value)}
            placeholder={t("board_name_placeholder")}
            title={t("board_name_title")}
            className="nodrag nopan px-3 py-1.5 rounded-lg bg-brain-900/80 border border-brain-700/60 text-brain-50 text-sm font-medium shadow-lg backdrop-blur-sm min-w-[160px] max-w-[320px] focus:outline-none focus:ring-1 focus:ring-cyan-500/60 placeholder:text-brain-300/50"
          />
        </Panel>
        <Controls className="bg-brain-900 border border-brain-700/60 rounded-lg shadow-lg [&>button]:bg-brain-900 [&>button]:border-brain-700/60 [&>button]:fill-brain-200 [&>button:hover]:bg-brain-800 [&>button:hover]:fill-brain-50" />
        <MiniMap
          className="bg-brain-900 border border-brain-700/60 rounded-lg shadow-lg"
          maskColor="rgba(0, 0, 0, 0.55)"
          nodeColor={(node) => {
            switch (node.type) {
              case "imageInput":
                return "#36a7f6";
              case "annotation":
                return "#8b5cf6";
              case "prompt":
                return "#f97316";
              case "nanoBanana":
                return "#22c55e";
              case "llmGenerate":
                return "#06b6d4";
              case "output":
                return "#ef4444";
              default:
                return "#b9dffd";
            }
          }}
        />
      </ReactFlow>

      {connectionDrop && connectionDrop.handleType && (
        <ConnectionDropMenu
          position={connectionDrop.position}
          handleType={connectionDrop.handleType}
          connectionType={connectionDrop.connectionType}
          onSelect={handleMenuSelect}
          onClose={handleCloseDropMenu}
        />
      )}

      {nodes.length === 0 && boardKind === "process" && (
        <div className="absolute inset-0 z-30 flex items-center justify-center pointer-events-none">
          <div className="pointer-events-auto max-w-lg w-[92%] bg-brain-900/95 border border-brain-700/60 rounded-2xl shadow-2xl p-6 text-center backdrop-blur">
            <div className="text-3xl mb-2">⚙️</div>
            <h3 className="text-brain-50 text-lg font-semibold mb-1">{t("process_empty_title")}</h3>
            <p className="text-brain-300 text-sm mb-4">
              {t("process_empty_desc")}
            </p>
            <div className="text-left text-xs text-brain-400 bg-brain-950/60 border border-brain-700/40 rounded-lg p-3 mb-4 space-y-1">
              <div><span className="text-brain-200">{t("empty_blocks_label")}</span> {t("process_empty_blocks")}</div>
              <div><span className="text-brain-200">{t("empty_example_label")}</span> {t("process_empty_example")}</div>
              <div><span className="text-brain-200">{t("empty_run_label")}</span> {t("process_empty_run")}</div>
            </div>
            <div className="flex flex-wrap items-center justify-center gap-2">
              <button
                onClick={() => { addNode("trigger", { x: 100, y: 160 }); addNode("ask_brain", { x: 400, y: 160 }); addNode("notify", { x: 700, y: 160 }); }}
                className="px-3 py-2 rounded-lg text-sm bg-indigo-600 hover:bg-indigo-500 text-white transition-colors"
              >
                {t("process_empty_build_example")}
              </button>
            </div>
            <p className="text-brain-500 text-[11px] mt-3">{t("process_empty_hint")}</p>
          </div>
        </div>
      )}

      {nodes.length === 0 && boardKind !== "process" && (
        <div className="absolute inset-0 z-30 flex items-center justify-center pointer-events-none">
          <div className="pointer-events-auto max-w-lg w-[92%] bg-brain-900/95 border border-brain-700/60 rounded-2xl shadow-2xl p-6 text-center backdrop-blur">
            <div className="text-3xl mb-2">🍌</div>
            <h3 className="text-brain-50 text-lg font-semibold mb-1">{t("board_empty_title")}</h3>
            <p className="text-brain-300 text-sm mb-4">
              {t("board_empty_desc")}
            </p>
            <div className="text-left text-xs text-brain-400 bg-brain-950/60 border border-brain-700/40 rounded-lg p-3 mb-4 space-y-1">
              <div><span className="text-brain-200">{t("empty_blocks_label")}</span> {t("board_empty_blocks")}</div>
              <div><span className="text-brain-200">{t("empty_example_label")}</span> {t("board_empty_example")}</div>
              <div><span className="text-brain-200">{t("empty_run_label")}</span> {t("board_empty_run")}</div>
            </div>
            <div className="flex flex-wrap items-center justify-center gap-2">
              <button
                onClick={() => addNode("prompt", { x: 120, y: 160 })}
                className="px-3 py-2 rounded-lg text-sm bg-orange-600 hover:bg-orange-500 text-white transition-colors"
              >
                {t("board_empty_add_prompt")}
              </button>
              <button
                onClick={() => addNode("nanoBanana", { x: 480, y: 160 })}
                className="px-3 py-2 rounded-lg text-sm bg-green-600 hover:bg-green-500 text-white transition-colors"
              >
                {t("board_empty_add_generator")}
              </button>
            </div>
            <p className="text-brain-500 text-[11px] mt-3">{t("board_empty_hint")}</p>
          </div>
        </div>
      )}

      <MultiSelectToolbar />
      <EdgeToolbar />
      <GlobalImageHistory />
    </div>
  );
}

export function WorkflowCanvas() {
  return <WorkflowCanvasInner />;
}


