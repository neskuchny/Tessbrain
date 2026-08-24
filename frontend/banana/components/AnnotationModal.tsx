"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Stage, Layer, Image as KonvaImage, Rect, Ellipse, Arrow, Line, Text, Transformer } from "react-konva";
import { useAnnotationStore } from "@/banana/store/annotationStore";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { AnnotationShape, RectangleShape, CircleShape, ArrowShape, FreehandShape, TextShape, ToolType } from "@/banana/types";
import Konva from "konva";

const COLORS = ["#ef4444", "#f97316", "#eab308", "#22c55e", "#3b82f6", "#8b5cf6", "#000000", "#ffffff"];
const STROKE_WIDTHS = [2, 4, 8];

export function AnnotationModal() {
  const {
    isModalOpen,
    sourceNodeId,
    sourceImage,
    annotations,
    selectedShapeId,
    currentTool,
    toolOptions,
    closeModal,
    addAnnotation,
    updateAnnotation,
    deleteAnnotation,
    clearAnnotations,
    selectShape,
    setCurrentTool,
    setToolOptions,
    undo,
    redo,
  } = useAnnotationStore();

  const updateNodeData = useWorkflowStore((state) => state.updateNodeData);

  const stageRef = useRef<Konva.Stage>(null);
  const transformerRef = useRef<Konva.Transformer>(null);
  const textInputRef = useRef<HTMLInputElement>(null);
  const [image, setImage] = useState<HTMLImageElement | null>(null);
  const [stageSize, setStageSize] = useState({ width: 800, height: 600 });
  const [scale, setScale] = useState(1);
  const [position, setPosition] = useState({ x: 0, y: 0 });
  const [isDrawing, setIsDrawing] = useState(false);
  const [drawStart, setDrawStart] = useState({ x: 0, y: 0 });
  const [currentShape, setCurrentShape] = useState<AnnotationShape | null>(null);
  const [editingTextId, setEditingTextId] = useState<string | null>(null);
  const [textInputPosition, setTextInputPosition] = useState<{ x: number; y: number } | null>(null);
  const [pendingTextPosition, setPendingTextPosition] = useState<{ x: number; y: number } | null>(null);
  const textInputCreatedAt = useRef<number>(0);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (sourceImage) {
      const img = new window.Image();
      img.onload = () => {
        setImage(img);
        if (containerRef.current) {
          const containerWidth = containerRef.current.clientWidth - 100;
          const containerHeight = containerRef.current.clientHeight - 100;
          const scaleX = containerWidth / img.width;
          const scaleY = containerHeight / img.height;
          const newScale = Math.min(scaleX, scaleY, 1);
          setScale(newScale);
          setStageSize({ width: img.width, height: img.height });
          setPosition({
            x: (containerWidth - img.width * newScale) / 2 + 50,
            y: (containerHeight - img.height * newScale) / 2 + 50,
          });
        }
      };
      img.src = sourceImage;
    }
  }, [sourceImage]);

  useEffect(() => {
    if (transformerRef.current && stageRef.current) {
      const selectedNode = stageRef.current.findOne(`#${selectedShapeId}`);
      if (selectedNode && currentTool === "select") transformerRef.current.nodes([selectedNode]);
      else transformerRef.current.nodes([]);
      transformerRef.current.getLayer()?.batchDraw();
    }
  }, [selectedShapeId, currentTool]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!isModalOpen) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        if (selectedShapeId && !editingTextId) deleteAnnotation(selectedShapeId);
      }
      if (e.key === "Escape") {
        if (editingTextId) {
          setEditingTextId(null);
          setTextInputPosition(null);
          setPendingTextPosition(null);
        } else {
          closeModal();
        }
      }
      if (e.ctrlKey || e.metaKey) {
        if (e.key === "z") {
          e.preventDefault();
          if (e.shiftKey) redo();
          else undo();
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isModalOpen, selectedShapeId, editingTextId, deleteAnnotation, closeModal, undo, redo]);

  const getRelativePointerPosition = useCallback(() => {
    const stage = stageRef.current;
    if (!stage) return { x: 0, y: 0 };
    const transform = stage.getAbsoluteTransform().copy().invert();
    const pos = stage.getPointerPosition();
    if (!pos) return { x: 0, y: 0 };
    return transform.point(pos);
  }, []);

  const handleMouseDown = useCallback(
    (e: Konva.KonvaEventObject<MouseEvent>) => {
      if (currentTool === "select") {
        const clickedOnEmpty = e.target === e.target.getStage() || e.target.getClassName() === "Image";
        if (clickedOnEmpty) selectShape(null);
        return;
      }

      const pos = getRelativePointerPosition();
      setIsDrawing(true);
      setDrawStart(pos);

      const id = `shape-${Date.now()}`;
      const baseShape = {
        id,
        x: pos.x,
        y: pos.y,
        stroke: toolOptions.strokeColor,
        strokeWidth: toolOptions.strokeWidth,
        opacity: toolOptions.opacity,
      };

      let newShape: AnnotationShape | null = null;

      switch (currentTool) {
        case "rectangle":
          newShape = { ...baseShape, type: "rectangle", width: 0, height: 0, fill: toolOptions.fillColor } as RectangleShape;
          break;
        case "circle":
          newShape = { ...baseShape, type: "circle", radiusX: 0, radiusY: 0, fill: toolOptions.fillColor } as CircleShape;
          break;
        case "arrow":
          newShape = { ...baseShape, type: "arrow", points: [0, 0, 0, 0] } as ArrowShape;
          break;
        case "freehand":
          newShape = { ...baseShape, type: "freehand", points: [0, 0] } as FreehandShape;
          break;
        case "text": {
          const stage = stageRef.current;
          if (stage) {
            const container = stage.container();
            const stageBox = container?.getBoundingClientRect();
            if (stageBox) {
              const screenX = stageBox.left + pos.x * scale + position.x;
              const screenY = stageBox.top + pos.y * scale + position.y;
              setTextInputPosition({ x: screenX, y: screenY });
              setPendingTextPosition({ x: pos.x, y: pos.y });
            }
          }
          textInputCreatedAt.current = Date.now();
          setEditingTextId("new");
          setIsDrawing(false);
          setTimeout(() => textInputRef.current?.focus(), 0);
          return;
        }
      }

      if (newShape) setCurrentShape(newShape);
    },
    [currentTool, toolOptions, getRelativePointerPosition, selectShape, scale, position]
  );

  const handleMouseMove = useCallback(() => {
    if (!isDrawing || !currentShape) return;
    const pos = getRelativePointerPosition();

    switch (currentShape.type) {
      case "rectangle": {
        const width = pos.x - drawStart.x;
        const height = pos.y - drawStart.y;
        setCurrentShape({
          ...currentShape,
          x: width < 0 ? pos.x : drawStart.x,
          y: height < 0 ? pos.y : drawStart.y,
          width: Math.abs(width),
          height: Math.abs(height),
        } as RectangleShape);
        break;
      }
      case "circle": {
        const radiusX = Math.abs(pos.x - drawStart.x) / 2;
        const radiusY = Math.abs(pos.y - drawStart.y) / 2;
        setCurrentShape({
          ...currentShape,
          x: (drawStart.x + pos.x) / 2,
          y: (drawStart.y + pos.y) / 2,
          radiusX,
          radiusY,
        } as CircleShape);
        break;
      }
      case "arrow":
        setCurrentShape({ ...currentShape, points: [0, 0, pos.x - drawStart.x, pos.y - drawStart.y] } as ArrowShape);
        break;
      case "freehand": {
        const freehand = currentShape as FreehandShape;
        setCurrentShape({ ...freehand, points: [...freehand.points, pos.x - drawStart.x, pos.y - drawStart.y] } as FreehandShape);
        break;
      }
    }
  }, [isDrawing, currentShape, drawStart, getRelativePointerPosition]);

  const handleMouseUp = useCallback(() => {
    if (!isDrawing || !currentShape) return;
    setIsDrawing(false);

    let shouldAdd = true;
    if (currentShape.type === "rectangle") {
      const rect = currentShape as RectangleShape;
      shouldAdd = rect.width > 5 && rect.height > 5;
    } else if (currentShape.type === "circle") {
      const circle = currentShape as CircleShape;
      shouldAdd = circle.radiusX > 5 && circle.radiusY > 5;
    } else if (currentShape.type === "arrow") {
      const arrow = currentShape as ArrowShape;
      const dx = arrow.points[2];
      const dy = arrow.points[3];
      shouldAdd = Math.sqrt(dx * dx + dy * dy) > 10;
    }

    if (shouldAdd) addAnnotation(currentShape);
    setCurrentShape(null);
  }, [isDrawing, currentShape, addAnnotation]);

  const handleShapeClick = useCallback(
    (e: Konva.KonvaEventObject<MouseEvent>, shapeId: string) => {
      e.cancelBubble = true;
      if (currentTool === "select") selectShape(shapeId);
    },
    [currentTool, selectShape]
  );

  const handleShapeDragEnd = useCallback(
    (e: Konva.KonvaEventObject<DragEvent>, shapeId: string) => {
      const node = e.target;
      updateAnnotation(shapeId, { x: node.x(), y: node.y() } as any);
    },
    [updateAnnotation]
  );

  const handleShapeTransformEnd = useCallback(
    (shapeId: string) => {
      const stage = stageRef.current;
      if (!stage) return;
      const node = stage.findOne(`#${shapeId}`) as any;
      if (!node) return;

      const shape = annotations.find((s) => s.id === shapeId);
      if (!shape) return;

      if (shape.type === "rectangle") {
        const rectNode = node as Konva.Rect;
        const scaleX = rectNode.scaleX();
        const scaleY = rectNode.scaleY();
        rectNode.scaleX(1);
        rectNode.scaleY(1);
        updateAnnotation(shapeId, {
          x: rectNode.x(),
          y: rectNode.y(),
          width: Math.max(5, rectNode.width() * scaleX),
          height: Math.max(5, rectNode.height() * scaleY),
        } as any);
      } else if (shape.type === "circle") {
        const circleNode = node as Konva.Ellipse;
        const scaleX = circleNode.scaleX();
        const scaleY = circleNode.scaleY();
        circleNode.scaleX(1);
        circleNode.scaleY(1);
        updateAnnotation(shapeId, {
          x: circleNode.x(),
          y: circleNode.y(),
          radiusX: Math.max(5, circleNode.radiusX() * scaleX),
          radiusY: Math.max(5, circleNode.radiusY() * scaleY),
        } as any);
      } else if (shape.type === "text") {
        const textNode = node as Konva.Text;
        const scaleX = textNode.scaleX();
        textNode.scaleX(1);
        updateAnnotation(shapeId, {
          x: textNode.x(),
          y: textNode.y(),
          fontSize: Math.max(10, textNode.fontSize() * scaleX),
        } as any);
      }
    },
    [annotations, updateAnnotation]
  );

  const handleSave = useCallback(() => {
    if (!sourceNodeId) return;
    updateNodeData(sourceNodeId, { annotations, outputImage: sourceImage || null });
    closeModal();
  }, [sourceNodeId, annotations, updateNodeData, closeModal, sourceImage]);

  const handleTextInputBlur = useCallback(() => {
    if (!editingTextId) return;
    const text = textInputRef.current?.value || "";
    if (text.trim().length === 0) {
      setEditingTextId(null);
      setTextInputPosition(null);
      setPendingTextPosition(null);
      return;
    }

    if (editingTextId === "new" && pendingTextPosition) {
      const id = `shape-${Date.now()}`;
      addAnnotation({
        id,
        type: "text",
        x: pendingTextPosition.x,
        y: pendingTextPosition.y,
        stroke: toolOptions.strokeColor,
        strokeWidth: toolOptions.strokeWidth,
        opacity: toolOptions.opacity,
        text,
        fontSize: toolOptions.fontSize,
        fill: toolOptions.strokeColor,
      } as TextShape);
    } else {
      updateAnnotation(editingTextId, { text } as any);
    }

    setEditingTextId(null);
    setTextInputPosition(null);
    setPendingTextPosition(null);
  }, [editingTextId, pendingTextPosition, addAnnotation, toolOptions, updateAnnotation]);

  const handleTextShapeDoubleClick = useCallback(
    (shapeId: string) => {
      const shape = annotations.find((s) => s.id === shapeId) as TextShape | undefined;
      if (!shape) return;
      const stage = stageRef.current;
      if (!stage) return;
      const container = stage.container();
      const stageBox = container?.getBoundingClientRect();
      if (!stageBox) return;

      const screenX = stageBox.left + shape.x * scale + position.x;
      const screenY = stageBox.top + shape.y * scale + position.y;
      setEditingTextId(shapeId);
      setTextInputPosition({ x: screenX, y: screenY });
      setTimeout(() => {
        if (textInputRef.current) {
          textInputRef.current.value = shape.text;
          textInputRef.current.focus();
          textInputRef.current.select();
        }
      }, 0);
    },
    [annotations, scale, position]
  );

  const handleToolChange = (tool: ToolType) => {
    setCurrentTool(tool);
    selectShape(null);
  };

  if (!isModalOpen || !sourceImage) return null;

  return (
    <div className="fixed inset-0 z-[200] bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
      <div ref={containerRef} className="w-full max-w-6xl h-[85vh] bg-brain-950 border border-brain-700/60 rounded-xl shadow-2xl flex flex-col overflow-hidden">
        <div className="px-4 py-3 border-b border-brain-700/60 bg-brain-900 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-sm font-semibold text-brain-100">Annotation</span>
            <div className="flex items-center gap-1">
              {(["select", "rectangle", "circle", "arrow", "freehand", "text"] as ToolType[]).map((tool) => (
                <button
                  key={tool}
                  onClick={() => handleToolChange(tool)}
                  className={`px-2 py-1 rounded text-[11px] transition-colors ${
                    currentTool === tool ? "bg-brain-600 text-white" : "bg-brain-800 text-brain-100/80 hover:bg-brain-700"
                  }`}
                >
                  {tool}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button onClick={undo} className="px-2 py-1 rounded bg-brain-800 text-brain-100/80 hover:bg-brain-700 text-[11px]">Undo</button>
            <button onClick={redo} className="px-2 py-1 rounded bg-brain-800 text-brain-100/80 hover:bg-brain-700 text-[11px]">Redo</button>
            <button onClick={clearAnnotations} className="px-2 py-1 rounded bg-brain-800 text-brain-100/80 hover:bg-brain-700 text-[11px]">Clear</button>
            <button onClick={handleSave} className="px-3 py-1 rounded bg-brain-600 hover:bg-brain-500 text-white text-[11px] font-semibold">Save</button>
            <button onClick={closeModal} className="px-2 py-1 rounded bg-brain-800 text-brain-100/80 hover:bg-brain-700 text-[11px]">Close</button>
          </div>
        </div>

        <div className="flex-1 relative">
          {textInputPosition && (
            <input
              ref={textInputRef}
              type="text"
              className="absolute z-[300] px-2 py-1 text-sm border border-brain-700/60 rounded bg-brain-950 text-brain-100 outline-none"
              style={{ left: textInputPosition.x, top: textInputPosition.y }}
              onBlur={handleTextInputBlur}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleTextInputBlur();
                if (e.key === "Escape") {
                  setEditingTextId(null);
                  setTextInputPosition(null);
                  setPendingTextPosition(null);
                }
              }}
            />
          )}

          <Stage
            ref={stageRef}
            width={containerRef.current?.clientWidth || 800}
            height={(containerRef.current?.clientHeight || 600) - 56}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            draggable={currentTool === "select"}
            scaleX={scale}
            scaleY={scale}
            x={position.x}
            y={position.y}
          >
            <Layer>
              {image && <KonvaImage image={image} />}

              {annotations.map((shape) => {
                const commonProps = {
                  id: shape.id,
                  key: shape.id,
                  x: shape.x,
                  y: shape.y,
                  opacity: shape.opacity,
                  draggable: currentTool === "select",
                  onClick: (e: any) => handleShapeClick(e, shape.id),
                  onDragEnd: (e: any) => handleShapeDragEnd(e, shape.id),
                  onTransformEnd: () => handleShapeTransformEnd(shape.id),
                };

                if (shape.type === "rectangle") {
                  const s = shape as RectangleShape;
                  return <Rect {...commonProps} width={s.width} height={s.height} stroke={s.stroke} strokeWidth={s.strokeWidth} fill={s.fill || undefined} />;
                }
                if (shape.type === "circle") {
                  const s = shape as CircleShape;
                  return <Ellipse {...commonProps} radiusX={s.radiusX} radiusY={s.radiusY} stroke={s.stroke} strokeWidth={s.strokeWidth} fill={s.fill || undefined} />;
                }
                if (shape.type === "arrow") {
                  const s = shape as ArrowShape;
                  return <Arrow {...commonProps} points={[0, 0, s.points[2], s.points[3]]} stroke={s.stroke} strokeWidth={s.strokeWidth} />;
                }
                if (shape.type === "freehand") {
                  const s = shape as FreehandShape;
                  return <Line {...commonProps} points={s.points} stroke={s.stroke} strokeWidth={s.strokeWidth} lineCap="round" lineJoin="round" />;
                }
                if (shape.type === "text") {
                  const s = shape as TextShape;
                  return (
                    <Text
                      {...commonProps}
                      text={s.text}
                      fontSize={s.fontSize}
                      fill={s.fill}
                      onDblClick={() => handleTextShapeDoubleClick(shape.id)}
                    />
                  );
                }
                return null;
              })}

              {currentShape && currentShape.type === "rectangle" && (
                <Rect
                  x={(currentShape as RectangleShape).x}
                  y={(currentShape as RectangleShape).y}
                  width={(currentShape as RectangleShape).width}
                  height={(currentShape as RectangleShape).height}
                  stroke={currentShape.stroke}
                  strokeWidth={currentShape.strokeWidth}
                  opacity={currentShape.opacity}
                  fill={(currentShape as RectangleShape).fill || undefined}
                />
              )}
              {currentShape && currentShape.type === "circle" && (
                <Ellipse
                  x={(currentShape as CircleShape).x}
                  y={(currentShape as CircleShape).y}
                  radiusX={(currentShape as CircleShape).radiusX}
                  radiusY={(currentShape as CircleShape).radiusY}
                  stroke={currentShape.stroke}
                  strokeWidth={currentShape.strokeWidth}
                  opacity={currentShape.opacity}
                  fill={(currentShape as CircleShape).fill || undefined}
                />
              )}
              {currentShape && currentShape.type === "arrow" && (
                <Arrow
                  x={(currentShape as ArrowShape).x}
                  y={(currentShape as ArrowShape).y}
                  points={(currentShape as ArrowShape).points}
                  stroke={currentShape.stroke}
                  strokeWidth={currentShape.strokeWidth}
                  opacity={currentShape.opacity}
                />
              )}
              {currentShape && currentShape.type === "freehand" && (
                <Line
                  x={(currentShape as FreehandShape).x}
                  y={(currentShape as FreehandShape).y}
                  points={(currentShape as FreehandShape).points}
                  stroke={currentShape.stroke}
                  strokeWidth={currentShape.strokeWidth}
                  opacity={currentShape.opacity}
                  lineCap="round"
                  lineJoin="round"
                />
              )}

              <Transformer
                ref={transformerRef}
                rotateEnabled={false}
                enabledAnchors={["top-left", "top-right", "bottom-left", "bottom-right"]}
                boundBoxFunc={(_oldBox, newBox) => {
                  if (newBox.width < 5 || newBox.height < 5) return _oldBox;
                  return newBox;
                }}
              />
            </Layer>
          </Stage>
        </div>

        <div className="px-4 py-3 border-t border-brain-700/60 bg-brain-900 flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-brain-200/70">Color:</span>
            {COLORS.map((c) => (
              <button
                key={c}
                onClick={() => setToolOptions({ strokeColor: c, fillColor: toolOptions.fillColor })}
                className={`w-5 h-5 rounded border ${toolOptions.strokeColor === c ? "border-white" : "border-brain-700/60"}`}
                style={{ backgroundColor: c }}
              />
            ))}
          </div>

          <div className="flex items-center gap-2">
            <span className="text-[11px] text-brain-200/70">Stroke:</span>
            {STROKE_WIDTHS.map((w) => (
              <button
                key={w}
                onClick={() => setToolOptions({ strokeWidth: w })}
                className={`px-2 py-1 rounded text-[11px] border ${
                  toolOptions.strokeWidth === w ? "border-brain-300 text-brain-50" : "border-brain-700/60 text-brain-100/70"
                } hover:bg-brain-800`}
              >
                {w}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <span className="text-[11px] text-brain-200/70">Opacity:</span>
            <input
              type="range"
              min={0.1}
              max={1}
              step={0.1}
              value={toolOptions.opacity}
              onChange={(e) => setToolOptions({ opacity: parseFloat(e.target.value) })}
              className="w-32 accent-brain-400"
            />
          </div>
        </div>
      </div>
    </div>
  );
}


