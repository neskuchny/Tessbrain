"use client";

import { useEffect } from "react";
import { useTranslations } from "next-intl";
import { create } from "zustand";

interface ToastState {
  message: string | null;
  type: "info" | "success" | "warning" | "error";
  show: (message: string, type?: "info" | "success" | "warning" | "error") => void;
  hide: () => void;
}

export const useToast = create<ToastState>((set) => ({
  message: null,
  type: "info",
  show: (message, type = "info") => set({ message, type }),
  hide: () => set({ message: null }),
}));

const typeStyles = {
  info: "bg-brain-900 border-brain-700 text-brain-100",
  success: "bg-green-900 border-green-700 text-green-100",
  warning: "bg-orange-900 border-orange-600 text-orange-100",
  error: "bg-red-900 border-red-700 text-red-100",
};

const typeIcons = {
  info: (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  ),
  success: (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  ),
  warning: (
    <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
      <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" />
    </svg>
  ),
  error: (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  ),
};

export function Toast() {
  const { message, type, hide } = useToast();
  // Сообщение может прийти ключом («i18n:...») из стора, где хуков нет:
  // переводим здесь. Обычный текст (ошибка сети, ответ сервера) — как есть.
  const t = useTranslations("banana");
  const text = message?.startsWith("i18n:")
    ? t(message.slice(5))
    : message;

  useEffect(() => {
    if (message) {
      const timer = setTimeout(() => {
        hide();
      }, 4000);
      return () => clearTimeout(timer);
    }
  }, [message, hide]);

  if (!message) return null;

  return (
    <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[200] animate-in fade-in slide-in-from-bottom-4 duration-300">
      <div className={`flex items-center gap-3 px-4 py-3 rounded-lg border shadow-xl ${typeStyles[type]}`}>
        {typeIcons[type]}
        <span className="text-sm font-medium">{text}</span>
        <button onClick={hide} className="ml-2 p-1 rounded hover:bg-white/10 transition-colors">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  );
}


