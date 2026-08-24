"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";
import { useWorkflowStore } from "@/banana/store/workflowStore";
import { ImageHistoryItem } from "@/banana/types";

function formatRelativeTime(timestamp: number): string {
  const diff = Date.now() - timestamp;
  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ago`;
  if (minutes > 0) return `${minutes}m ago`;
  return "Just now";
}

function calculateFanPosition(index: number) {
  const verticalSpacing = 60;
  const curveStrength = 0.15;
  const xOffset = index * index * curveStrength;
  const x = -28 + xOffset;
  const y = -(index * verticalSpacing + 56);
  return { x, y };
}

function FanItem({
  item,
  index,
  onDragStart,
}: {
  item: ImageHistoryItem;
  index: number;
  onDragStart: (e: React.DragEvent, item: ImageHistoryItem) => void;
}) {
  const { x, y } = calculateFanPosition(index);
  const delay = index * 30;

  return (
    <button
      draggable
      onDragStart={(e) => onDragStart(e, item)}
      className="absolute w-14 h-14 rounded-lg overflow-hidden border-2 border-brain-700/70 hover:border-brain-400 shadow-lg cursor-grab active:cursor-grabbing transition-colors duration-150"
      style={
        {
          transform: `translate(${x}px, ${y}px)`,
          transitionDelay: `${delay}ms`,
          zIndex: 10 - index,
        } as React.CSSProperties
      }
      title={`${formatRelativeTime(item.timestamp)}\n${item.prompt?.substring(0, 50) || ""}...`}
    >
      <img src={item.image} alt={`History ${index + 1}`} className="w-full h-full object-cover pointer-events-none" draggable={false} />
    </button>
  );
}

function HistorySidebar({
  history,
  onClear,
  onClose,
  onDragStart,
  triggerRect,
}: {
  history: ImageHistoryItem[];
  onClear: () => void;
  onClose: () => void;
  onDragStart: (e: React.DragEvent, item: ImageHistoryItem) => void;
  triggerRect: DOMRect | null;
}) {
  const t = useTranslations("banana");
  const sidebarRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (sidebarRef.current && !sidebarRef.current.contains(event.target as Node)) onClose();
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [onClose]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const sidebarStyle: React.CSSProperties = { position: "fixed", zIndex: 200 };
  if (triggerRect) {
    const left = Math.max(16, triggerRect.left - 140);
    const bottom = window.innerHeight - triggerRect.top + 8;
    sidebarStyle.left = `${left}px`;
    sidebarStyle.bottom = `${bottom}px`;
  } else {
    sidebarStyle.right = "100px";
    sidebarStyle.bottom = "100px";
  }

  return createPortal(
    <div ref={sidebarRef} className="w-80 max-h-[420px] bg-brain-900 border border-brain-700/60 rounded-lg shadow-xl flex flex-col" style={sidebarStyle}>
      <div className="px-4 py-3 border-b border-brain-700/60 flex items-center justify-between shrink-0">
        <span className="text-sm text-brain-100 font-medium">{t('history_label', { count: history.length })}</span>
        <div className="flex items-center gap-2">
          <button onClick={onClear} className="text-[10px] text-brain-200/60 hover:text-red-400 transition-colors" title="Clear all history">
            Clear All
          </button>
          <button onClick={onClose} className="w-5 h-5 rounded hover:bg-brain-800 flex items-center justify-center text-brain-200/70 hover:text-brain-50 transition-colors" title="Close">
            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
        {history.map((item, index) => (
          <div
            key={item.id}
            draggable
            onDragStart={(e) => onDragStart(e, item)}
            className="flex gap-3 p-2 rounded-lg hover:bg-brain-800/60 cursor-grab active:cursor-grabbing group transition-colors"
          >
            <div className="w-14 h-14 rounded overflow-hidden shrink-0 border border-brain-700/70 group-hover:border-brain-400 transition-colors">
              <img src={item.image} alt={`History ${index + 1}`} className="w-full h-full object-cover pointer-events-none" draggable={false} />
            </div>
            <div className="flex-1 min-w-0 flex flex-col justify-center">
              <p className="text-[11px] text-brain-100/80 truncate">{item.prompt?.substring(0, 60) || "No prompt"}</p>
              <p className="text-[10px] text-brain-200/50 mt-0.5">
                {formatRelativeTime(item.timestamp)} · {item.model === "nano-banana-pro" ? "Pro" : "Standard"}
              </p>
            </div>
          </div>
        ))}
      </div>

      <div className="px-4 py-2 border-t border-brain-700/60 bg-brain-950/30 shrink-0">
        <span className="text-[10px] text-brain-200/50">Drag images to canvas to create nodes</span>
      </div>
    </div>,
    document.body
  );
}

export function GlobalImageHistory() {
  const t = useTranslations("banana");
  const [isOpen, setIsOpen] = useState(false);
  const [showSidebar, setShowSidebar] = useState(false);
  const drawerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const history = useWorkflowStore((state) => state.globalImageHistory);
  const clearGlobalHistory = useWorkflowStore((state) => state.clearGlobalHistory);

  const fanItems = history.slice(0, 10);
  const hasOverflow = history.length > 10;

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (drawerRef.current && !drawerRef.current.contains(event.target as Node)) setIsOpen(false);
    };
    if (isOpen && !showSidebar) document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [isOpen, showSidebar]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (showSidebar) setShowSidebar(false);
        else setIsOpen(false);
      }
    };
    if (isOpen || showSidebar) document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, showSidebar]);

  const handleDragStart = useCallback((e: React.DragEvent, item: ImageHistoryItem) => {
    e.dataTransfer.setData(
      "application/history-image",
      JSON.stringify({ image: item.image, prompt: item.prompt, timestamp: item.timestamp })
    );
  }, []);

  const handleOpenSidebar = useCallback(() => {
    setShowSidebar(true);
  }, []);

  const triggerRect = triggerRef.current?.getBoundingClientRect() || null;

  if (history.length === 0) return null;

  return (
    <>
      <div ref={drawerRef} className="fixed bottom-6 right-6 z-[120]">
        {isOpen && (
          <div className="absolute bottom-14 right-0">
            {fanItems.map((item, index) => (
              <FanItem key={item.id} item={item} index={index} onDragStart={handleDragStart} />
            ))}
          </div>
        )}

        <button
          ref={triggerRef}
          onClick={() => setIsOpen((v) => !v)}
          onDoubleClick={handleOpenSidebar}
          className="w-12 h-12 rounded-full bg-brain-900 border border-brain-700/60 shadow-lg flex items-center justify-center text-brain-100 hover:bg-brain-800 transition-colors"
          title={hasOverflow ? t("title_open_list") : t("title_history")}
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 7.5V6a2.25 2.25 0 012.25-2.25h13.5A2.25 2.25 0 0121 6v12a2.25 2.25 0 01-2.25 2.25H9.75M3 7.5h18M3 7.5v12a2.25 2.25 0 002.25 2.25h4.5" />
          </svg>
        </button>
      </div>

      {showSidebar && (
        <HistorySidebar
          history={history}
          onClear={clearGlobalHistory}
          onClose={() => setShowSidebar(false)}
          onDragStart={handleDragStart}
          triggerRect={triggerRect}
        />
      )}
    </>
  );
}


