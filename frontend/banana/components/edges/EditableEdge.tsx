"use client";

import { useState, useCallback, useMemo } from "react";
import { BaseEdge, EdgeProps, getSmoothStepPath, getBezierPath, useReactFlow } from "@xyflow/react";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { NanoBananaNodeData, WorkflowEdgeData } from "@/banana/types";

interface EdgeData extends WorkflowEdgeData {
  offsetX?: number;
  offsetY?: number;
}

const EDGE_COLORS = {
  image: "#36a7f6", // brain-400
  prompt: "#7cc5fb", // brain-300
  default: "#b9dffd", // brain-200
  pause: "#f97316", // Orange for paused edges
};

export function EditableEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  style,
  markerEnd,
  selected,
  data,
  sourceHandleId,
  targetHandleId,
  target,
}: EdgeProps) {
  const { setEdges } = useReactFlow();
  const edgeStyle = useWorkflowStore((state) => state.edgeStyle);
  const nodes = useWorkflowStore((state) => state.nodes);
  const [isDragging, setIsDragging] = useState(false);

  const edgeData = data as EdgeData | undefined;
  const offsetX = edgeData?.offsetX ?? 0;
  const offsetY = edgeData?.offsetY ?? 0;
  const hasPause = edgeData?.hasPause ?? false;

  const isTargetLoading = useMemo(() => {
    const targetNode = nodes.find((n) => n.id === target);
    if (targetNode?.type === "nanoBanana") {
      const nodeData = targetNode.data as NanoBananaNodeData;
      return nodeData.status === "loading";
    }
    return false;
  }, [target, nodes]);

  const edgeColor = useMemo(() => {
    if (hasPause) return EDGE_COLORS.pause;
    const handleType = sourceHandleId || targetHandleId;
    if (handleType === "image") return EDGE_COLORS.image;
    if (handleType === "prompt" || handleType === "text") return EDGE_COLORS.prompt;
    return EDGE_COLORS.default;
  }, [hasPause, sourceHandleId, targetHandleId]);

  const [edgePath] = useMemo(() => {
    if (edgeStyle === "curved") {
      return getBezierPath({
        sourceX,
        sourceY,
        sourcePosition,
        targetX,
        targetY,
        targetPosition,
        curvature: 0.25,
      });
    }
    return getSmoothStepPath({
      sourceX,
      sourceY,
      sourcePosition,
      targetX,
      targetY,
      targetPosition,
      borderRadius: 8,
      offset: offsetX,
    });
  }, [edgeStyle, sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition, offsetX]);

  const handlePositions = useMemo(() => {
    if (edgeStyle === "curved") return [];
    const handles: { x: number; y: number; direction: "horizontal" | "vertical" }[] = [];
    const midX = (sourceX + targetX) / 2 + offsetX;
    const midY = (sourceY + targetY) / 2 + offsetY;
    if (Math.abs(targetX - sourceX) > 50) {
      handles.push({ x: midX, y: midY, direction: "horizontal" });
    }
    return handles;
  }, [edgeStyle, sourceX, sourceY, targetX, targetY, offsetX, offsetY]);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent, direction: "horizontal" | "vertical") => {
      e.stopPropagation();
      e.preventDefault();
      setIsDragging(true);

      const startX = e.clientX;
      const startY = e.clientY;
      const startOffsetX = offsetX;
      const startOffsetY = offsetY;

      const handleMouseMove = (moveEvent: MouseEvent) => {
        const deltaX = moveEvent.clientX - startX;
        const deltaY = moveEvent.clientY - startY;

        setEdges((edges) =>
          edges.map((edge) => {
            if (edge.id === id) {
              return {
                ...edge,
                data: {
                  ...edge.data,
                  offsetX: direction === "horizontal" ? startOffsetX + deltaX : startOffsetX,
                  offsetY: direction === "vertical" ? startOffsetY + deltaY : startOffsetY,
                },
              };
            }
            return edge;
          })
        );
      };

      const handleMouseUp = () => {
        setIsDragging(false);
        document.removeEventListener("mousemove", handleMouseMove);
        document.removeEventListener("mouseup", handleMouseUp);
      };

      document.addEventListener("mousemove", handleMouseMove);
      document.addEventListener("mouseup", handleMouseUp);
    },
    [id, offsetX, offsetY, setEdges]
  );

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          ...style,
          stroke: edgeColor,
          strokeWidth: 3,
          strokeLinecap: "round",
          strokeLinejoin: "round",
        }}
      />

      {isTargetLoading && (
        <>
          <path
            d={edgePath}
            fill="none"
            stroke={edgeColor}
            strokeWidth={10}
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{ opacity: 0.2, filter: "blur(6px)" }}
          />
          <path
            d={edgePath}
            fill="none"
            stroke={edgeColor}
            strokeWidth={5}
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeDasharray="20 30"
            style={{ animation: "flowPulse 1s linear infinite" }}
          />
        </>
      )}

      <path d={edgePath} fill="none" strokeWidth={15} stroke="transparent" className="react-flow__edge-interaction" />

      {hasPause && (
        <g transform={`translate(${targetX - 24}, ${targetY})`}>
          <circle r={10} fill="#0a3f6e" stroke={edgeColor} strokeWidth={2} />
          <rect x={-4} y={-5} width={2.5} height={10} fill={edgeColor} rx={1} />
          <rect x={1.5} y={-5} width={2.5} height={10} fill={edgeColor} rx={1} />
        </g>
      )}

      {(selected || isDragging) &&
        handlePositions.map((handle, index) => (
          <g key={index}>
            <circle
              cx={handle.x}
              cy={handle.y}
              r={6}
              fill="white"
              stroke={EDGE_COLORS.image}
              strokeWidth={2}
              style={{ cursor: handle.direction === "horizontal" ? "ew-resize" : "ns-resize" }}
              onMouseDown={(e) => handleMouseDown(e, handle.direction)}
            />
          </g>
        ))}
    </>
  );
}


