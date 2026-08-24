import { create } from "zustand";
import { persist } from "zustand/middleware";
import { WorkflowFile, WorkflowNode, WorkflowEdge } from "./workflowStore";

export interface WorkflowTemplate {
  id: string;
  name: string;
  description: string;
  category: "user";
  workflow: WorkflowFile;
  createdAt: number;
  updatedAt: number;
}

// Lightweight version without images for localStorage
interface StoredTemplate {
  id: string;
  name: string;
  description: string;
  // Store only structure, not image data
  nodeCount: number;
  edgeCount: number;
  createdAt: number;
  updatedAt: number;
}

interface TemplatesStore {
  templates: WorkflowTemplate[];
  
  // Actions
  addTemplate: (name: string, description: string, workflow: WorkflowFile) => string;
  updateTemplate: (id: string, updates: Partial<Pick<WorkflowTemplate, "name" | "description">>) => void;
  deleteTemplate: (id: string) => void;
  getTemplateById: (id: string) => WorkflowTemplate | undefined;
  getUserTemplates: () => WorkflowTemplate[];
}

const generateId = () => `tpl-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

// Strip image data from workflow to save space
function stripImagesFromWorkflow(workflow: WorkflowFile): WorkflowFile {
  const strippedNodes = workflow.nodes.map((node) => {
    const data = { ...node.data };
    // Remove image data from various node types
    if ('image' in data) data.image = null;
    if ('outputImage' in data) data.outputImage = null;
    if ('sourceImage' in data) data.sourceImage = null;
    if ('inputImages' in data) data.inputImages = [];
    return { ...node, data };
  });
  
  return {
    ...workflow,
    nodes: strippedNodes as WorkflowNode[],
  };
}

export const useTemplatesStore = create<TemplatesStore>()(
  persist(
    (set, get) => ({
      templates: [],

      addTemplate: (name, description, workflow) => {
        const id = generateId();
        const now = Date.now();
        
        // Strip images to avoid localStorage quota issues
        const strippedWorkflow = stripImagesFromWorkflow(workflow);
        
        const template: WorkflowTemplate = {
          id,
          name,
          description,
          category: "user",
          workflow: strippedWorkflow,
          createdAt: now,
          updatedAt: now,
        };
        set((state) => ({ templates: [...state.templates, template] }));
        return id;
      },

      updateTemplate: (id, updates) => {
        set((state) => ({
          templates: state.templates.map((t) =>
            t.id === id ? { ...t, ...updates, updatedAt: Date.now() } : t
          ),
        }));
      },

      deleteTemplate: (id) => {
        set((state) => ({
          templates: state.templates.filter((t) => t.id !== id),
        }));
      },

      getTemplateById: (id) => get().templates.find((t) => t.id === id),

      getUserTemplates: () => get().templates,
    }),
    {
      name: "banana-templates-v2",
      // Личные шаблоны — per-user: ключ дополняется uid из токена в момент
      // обращения. Без этого шаблоны юзера A (с промптами компании) были
      // видны юзеру B в том же браузере. Легаси-ключ без uid читается как
      // fallback (миграция без потери старых шаблонов).
      storage: {
        getItem: (name: string) => {
          try {
            const { getUserIdFromToken } = require("@/lib/authFetch");
            const uid = getUserIdFromToken();
            const raw =
              (uid && localStorage.getItem(`${name}:${uid}`)) ||
              localStorage.getItem(name);
            return raw ? JSON.parse(raw) : null;
          } catch {
            return null;
          }
        },
        setItem: (name: string, value: unknown) => {
          try {
            const { getUserIdFromToken } = require("@/lib/authFetch");
            const uid = getUserIdFromToken();
            localStorage.setItem(
              uid ? `${name}:${uid}` : name, JSON.stringify(value));
          } catch { /* quota/SSR */ }
        },
        removeItem: (name: string) => {
          try {
            const { getUserIdFromToken } = require("@/lib/authFetch");
            const uid = getUserIdFromToken();
            localStorage.removeItem(uid ? `${name}:${uid}` : name);
          } catch { /* noop */ }
        },
      } as any,
    }
  )
);

