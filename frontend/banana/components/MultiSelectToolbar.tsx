"use client";

import { useReactFlow } from "@xyflow/react";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { useMemo } from "react";

const STACK_GAP = 20;

export function MultiSelectToolbar() {
  const { nodes, onNodesChange } = useWorkflowStore();
  const { getViewport } = useReactFlow();

  const selectedNodes = useMemo(() => nodes.filter((node) => node.selected), [nodes]);

  const toolbarPosition = useMemo(() => {
    if (selectedNodes.length < 2) return null;
    const viewport = getViewport();

    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;

    selectedNodes.forEach((node) => {
      const nodeWidth = (node.style?.width as number) || node.measured?.width || 220;
      minX = Math.min(minX, node.position.x);
      minY = Math.min(minY, node.position.y);
      maxX = Math.max(maxX, node.position.x + nodeWidth);
    });

    const centerX = (minX + maxX) / 2;
    const screenX = centerX * viewport.zoom + viewport.x;
    const screenY = minY * viewport.zoom + viewport.y - 50;
    return { x: screenX, y: screenY };
  }, [selectedNodes, getViewport]);

  const handleStackHorizontally = () => {
    if (selectedNodes.length < 2) return;
    const sortedNodes = [...selectedNodes].sort((a, b) => a.position.x - b.position.x);
    const alignY = Math.min(...sortedNodes.map((n) => n.position.y));
    let currentX = sortedNodes[0].position.x;

    sortedNodes.forEach((node) => {
      const nodeWidth = (node.style?.width as number) || node.measured?.width || 220;
      onNodesChange([{ type: "position", id: node.id, position: { x: currentX, y: alignY } }]);
      currentX += nodeWidth + STACK_GAP;
    });
  };

  const handleStackVertically = () => {
    if (selectedNodes.length < 2) return;
    const sortedNodes = [...selectedNodes].sort((a, b) => a.position.y - b.position.y);
    const alignX = Math.min(...sortedNodes.map((n) => n.position.x));
    let currentY = sortedNodes[0].position.y;

    sortedNodes.forEach((node) => {
      const nodeHeight = (node.style?.height as number) || node.measured?.height || 200;
      onNodesChange([{ type: "position", id: node.id, position: { x: alignX, y: currentY } }]);
      currentY += nodeHeight + STACK_GAP;
    });
  };

  const handleArrangeAsGrid = () => {
    if (selectedNodes.length < 2) return;
    const count = selectedNodes.length;
    const cols = Math.ceil(Math.sqrt(count));

    const sortedNodes = [...selectedNodes].sort((a, b) => {
      const rowA = Math.floor(a.position.y / 100);
      const rowB = Math.floor(b.position.y / 100);
      if (rowA !== rowB) return rowA - rowB;
      return a.position.x - b.position.x;
    });

    const startX = Math.min(...sortedNodes.map((n) => n.position.x));
    const startY = Math.min(...sortedNodes.map((n) => n.position.y));

    const maxWidth = Math.max(...sortedNodes.map((n) => (n.style?.width as number) || n.measured?.width || 220));
    const maxHeight = Math.max(...sortedNodes.map((n) => (n.style?.height as number) || n.measured?.height || 200));

    sortedNodes.forEach((node, index) => {
      const col = index % cols;
      const row = Math.floor(index / cols);
      onNodesChange([
        {
          type: "position",
          id: node.id,
          position: { x: startX + col * (maxWidth + STACK_GAP), y: startY + row * (maxHeight + STACK_GAP) },
        },
      ]);
    });
  };

  if (!toolbarPosition || selectedNodes.length < 2) return null;

  return (
    <div
      className="fixed z-[100] flex items-center gap-1 bg-brain-900 border border-brain-700/60 rounded-lg shadow-xl p-1"
      style={{ left: toolbarPosition.x, top: toolbarPosition.y, transform: "translateX(-50%)" }}
    >
      <button
        onClick={handleStackHorizontally}
        className="p-1.5 rounded hover:bg-brain-800 text-brain-200/80 hover:text-brain-50 transition-colors"
        title="Stack horizontally (H)"
      >
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 4h4v16H6zM14 4h4v16h-4z" />
        </svg>
      </button>
      <button
        onClick={handleStackVertically}
        className="p-1.5 rounded hover:bg-brain-800 text-brain-200/80 hover:text-brain-50 transition-colors"
        title="Stack vertically (V)"
      >
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16v4H4zM4 14h16v4H4z" />
        </svg>
      </button>
      <button
        onClick={handleArrangeAsGrid}
        className="p-1.5 rounded hover:bg-brain-800 text-brain-200/80 hover:text-brain-50 transition-colors"
        title="Arrange as grid (G)"
      >
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" />
        </svg>
      </button>
    </div>
  );
}


