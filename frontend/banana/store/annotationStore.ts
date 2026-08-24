import { create } from "zustand";
import { AnnotationShape, ToolType, ToolOptions } from "@/banana/types";

interface AnnotationStore {
  // Modal state
  isModalOpen: boolean;
  sourceNodeId: string | null;
  sourceImage: string | null;

  // Annotations
  annotations: AnnotationShape[];
  selectedShapeId: string | null;

  // History for undo/redo
  history: AnnotationShape[][];
  historyIndex: number;

  // Current tool and options
  currentTool: ToolType;
  toolOptions: ToolOptions;

  // Modal actions
  openModal: (nodeId: string, image: string, existingAnnotations?: AnnotationShape[]) => void;
  closeModal: () => void;

  // Annotation actions
  addAnnotation: (shape: AnnotationShape) => void;
  updateAnnotation: (id: string, updates: Partial<AnnotationShape>) => void;
  deleteAnnotation: (id: string) => void;
  clearAnnotations: () => void;
  selectShape: (id: string | null) => void;

  // History actions
  undo: () => void;
  redo: () => void;
  pushHistory: () => void;

  // Tool actions
  setCurrentTool: (tool: ToolType) => void;
  setToolOptions: (options: Partial<ToolOptions>) => void;
}

const defaultToolOptions: ToolOptions = {
  strokeColor: "#ef4444", // red-500
  strokeWidth: 3,
  fillColor: null,
  fontSize: 24,
  opacity: 1,
};

export const useAnnotationStore = create<AnnotationStore>((set, get) => ({
  isModalOpen: false,
  sourceNodeId: null,
  sourceImage: null,
  annotations: [],
  selectedShapeId: null,
  history: [[]],
  historyIndex: 0,
  currentTool: "rectangle",
  toolOptions: defaultToolOptions,

  openModal: (nodeId: string, image: string, existingAnnotations: AnnotationShape[] = []) => {
    set({
      isModalOpen: true,
      sourceNodeId: nodeId,
      sourceImage: image,
      annotations: existingAnnotations,
      selectedShapeId: null,
      history: [existingAnnotations],
      historyIndex: 0,
    });
  },

  closeModal: () => {
    set({
      isModalOpen: false,
      sourceNodeId: null,
      sourceImage: null,
      annotations: [],
      selectedShapeId: null,
      history: [[]],
      historyIndex: 0,
    });
  },

  addAnnotation: (shape: AnnotationShape) => {
    const { pushHistory } = get();
    pushHistory();
    set((state) => ({
      annotations: [...state.annotations, shape],
    }));
  },

  updateAnnotation: (id: string, updates: Partial<AnnotationShape>) => {
    set((state) => ({
      annotations: state.annotations.map((shape) => (shape.id === id ? ({ ...shape, ...updates } as AnnotationShape) : shape)),
    }));
  },

  deleteAnnotation: (id: string) => {
    const { pushHistory } = get();
    pushHistory();
    set((state) => ({
      annotations: state.annotations.filter((shape) => shape.id !== id),
      selectedShapeId: state.selectedShapeId === id ? null : state.selectedShapeId,
    }));
  },

  clearAnnotations: () => {
    const { pushHistory } = get();
    pushHistory();
    set({
      annotations: [],
      selectedShapeId: null,
    });
  },

  selectShape: (id: string | null) => {
    set({ selectedShapeId: id });
  },

  pushHistory: () => {
    set((state) => {
      const newHistory = state.history.slice(0, state.historyIndex + 1);
      newHistory.push([...state.annotations]);
      return {
        history: newHistory,
        historyIndex: newHistory.length - 1,
      };
    });
  },

  undo: () => {
    set((state) => {
      if (state.historyIndex > 0) {
        const newIndex = state.historyIndex - 1;
        return {
          historyIndex: newIndex,
          annotations: [...state.history[newIndex]],
          selectedShapeId: null,
        };
      }
      return state;
    });
  },

  redo: () => {
    set((state) => {
      if (state.historyIndex < state.history.length - 1) {
        const newIndex = state.historyIndex + 1;
        return {
          historyIndex: newIndex,
          annotations: [...state.history[newIndex]],
          selectedShapeId: null,
        };
      }
      return state;
    });
  },

  setCurrentTool: (tool: ToolType) => {
    set({ currentTool: tool, selectedShapeId: null });
  },

  setToolOptions: (options: Partial<ToolOptions>) => {
    set((state) => ({
      toolOptions: { ...state.toolOptions, ...options },
    }));
  },
}));


