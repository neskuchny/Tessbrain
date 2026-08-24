"use client";

import { useWorkflowStore } from "@/banana/store/workflowStore";
import { useMemo, useEffect, useState, useRef } from "react";

export function EdgeToolbar() {
  const { edges, toggleEdgePause, removeEdge } = useWorkflowStore();
  const [clickPosition, setClickPosition] = useState<{ x: number; y: number } | null>(null);
  const previousSelectedEdgeId = useRef<string | null>(null);

  const selectedEdge = useMemo(() => edges.find((edge) => edge.selected), [edges]);

  useEffect(() => {
    const handleMouseDown = (e: MouseEvent) => {
      const target = e.target as Element;
      if (target.closest(".react-flow__edge")) {
        setClickPosition({ x: e.clientX, y: e.clientY - 40 });
      }
    };
    document.addEventListener("mousedown", handleMouseDown);
    return () => document.removeEventListener("mousedown", handleMouseDown);
  }, []);

  useEffect(() => {
    if (!selectedEdge && previousSelectedEdgeId.current) setClickPosition(null);
    previousSelectedEdgeId.current = selectedEdge?.id || null;
  }, [selectedEdge]);

  if (!clickPosition || !selectedEdge) return null;

  const hasPause = selectedEdge.data?.hasPause;

  return (
    <div
      className="fixed z-[100] flex items-center gap-1 bg-brain-900 border border-brain-700/60 rounded-lg shadow-xl p-1"
      style={{ left: clickPosition.x, top: clickPosition.y, transform: "translateX(-50%)" }}
    >
      <button
        onClick={() => toggleEdgePause(selectedEdge.id)}
        className={`p-1.5 rounded hover:bg-brain-800 transition-colors ${hasPause ? "text-amber-400 hover:text-amber-300" : "text-brain-200/80 hover:text-brain-50"}`}
        title={hasPause ? "Remove pause" : "Add pause"}
      >
        {hasPause ? (
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
            <path d="M8 5v14l11-7z" />
          </svg>
        ) : (
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
            <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" />
          </svg>
        )}
      </button>
      <button
        onClick={() => removeEdge(selectedEdge.id)}
        className="p-1.5 rounded hover:bg-brain-800 text-brain-200/80 hover:text-red-400 transition-colors"
        title="Delete"
      >
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0"
          />
        </svg>
      </button>
    </div>
  );
}


