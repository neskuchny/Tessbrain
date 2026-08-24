"use client";
/**
 * Психоаналитика человека — мотивы, прогнозы поведения, рекомендации.
 *
 * Слой хранится с грифом и не участвует в общем поиске; этот блок —
 * единственный экран его чтения. Доступ решает БЭКЕНД (сам человек /
 * руководитель по цепочке / админ) — компонент лишь честно показывает
 * отказ, не пытаясь его обойти или спрятать.
 *
 * Ленивая загрузка по клику: чувствительный слой не должен грузиться
 * автоматически при каждом открытии карточки — и трафика меньше, и в
 * журнале доступа остаются только осознанные обращения.
 */
import React, { useState } from "react";
import { authFetch } from "@/lib/authFetch";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type Insight = Record<string, any>;

interface InsightsPayload {
  person_name: string;
  insights: {
    motives?: Insight[];
    behavior_forecast?: Insight[];
    recommendation?: Insight[];
  };
  counts: Record<string, number>;
  disclaimer: string;
}

function List({ items }: { items?: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <ul className="list-disc list-inside text-slate-300">
      {items.slice(0, 6).map((x, i) => (
        <li key={i}>{x}</li>
      ))}
    </ul>
  );
}

function Row({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <p className="text-slate-300">
      <span className="text-slate-500">{label}: </span>
      {value}
    </p>
  );
}

export default function PsychInsightsBlock({
  personId,
  userId,
}: {
  personId: string;
  userId: string;
}) {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "loading" }
    | { kind: "denied"; detail: string }
    | { kind: "empty" }
    | { kind: "ready"; data: InsightsPayload }
  >({ kind: "idle" });

  const load = async () => {
    setState({ kind: "loading" });
    try {
      const res = await authFetch(
        `${API_BASE}/api/v1/psych-insights/person/${encodeURIComponent(
          personId
        )}?user_id=${encodeURIComponent(userId)}`
      );
      if (res.status === 403) {
        const body = await res.json().catch(() => ({}));
        setState({
          kind: "denied",
          detail:
            body?.detail ||
            "Доступно самому человеку, его руководителю и администратору",
        });
        return;
      }
      if (!res.ok) {
        setState({ kind: "denied", detail: `Ошибка загрузки (${res.status})` });
        return;
      }
      const data: InsightsPayload = await res.json();
      const total = Object.values(data.counts || {}).reduce(
        (a, b) => a + (b || 0),
        0
      );
      setState(total === 0 ? { kind: "empty" } : { kind: "ready", data });
    } catch {
      setState({ kind: "denied", detail: "Сервис недоступен" });
    }
  };

  return (
    <div className="bg-brain-800/30 rounded-lg p-4 border border-amber-700/30">
      <div className="flex items-center justify-between">
        <h4 className="text-white font-medium">
          Психоаналитика встреч{" "}
          <span className="text-amber-400/80 text-xs align-middle">
            ограниченный доступ
          </span>
        </h4>
        {state.kind === "idle" && (
          <button
            onClick={load}
            className="text-xs px-2 py-1 rounded bg-amber-900/40 text-amber-200 hover:bg-amber-900/60"
          >
            Показать
          </button>
        )}
      </div>

      {state.kind === "idle" && (
        <p className="text-xs text-slate-500 mt-1">
          Мотивы, прогнозы поведения и рекомендации из разборов встреч.
          Загружается по запросу; доступ проверяет сервер.
        </p>
      )}
      {state.kind === "loading" && (
        <p className="text-sm text-slate-400 mt-2">Загрузка…</p>
      )}
      {state.kind === "denied" && (
        <p className="text-sm text-amber-200/80 mt-2">{state.detail}</p>
      )}
      {state.kind === "empty" && (
        <p className="text-sm text-slate-400 mt-2">
          По этому человеку психоаналитика пока не накоплена — она
          собирается из встреч, обработанных после включения сохранения.
        </p>
      )}

      {state.kind === "ready" && (
        <div className="mt-3 space-y-4 text-sm">
          {(state.data.insights.motives?.length ?? 0) > 0 && (
            <div>
              <div className="text-amber-300 text-xs font-semibold mb-1">
                Мотивы (наблюдения модели)
              </div>
              {state.data.insights.motives!.slice(0, 3).map((m, i) => (
                <div key={i} className="mb-2 pl-2 border-l border-amber-700/40">
                  <Row label="Открытая позиция" value={m.stated_position} />
                  <List items={m.true_intentions} />
                  <Row label="Стремление к влиянию" value={m.power_seeking} />
                  {(m.manipulation_signs?.length ?? 0) > 0 && (
                    <div className="text-rose-300/90">
                      <span className="text-slate-500">Признаки манипуляции: </span>
                      {m.manipulation_signs.join("; ")}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {(state.data.insights.behavior_forecast?.length ?? 0) > 0 && (
            <div>
              <div className="text-cyan-300 text-xs font-semibold mb-1">
                Прогноз поведения
              </div>
              {state.data.insights.behavior_forecast!.slice(0, 2).map((p, i) => (
                <div key={i} className="mb-2 pl-2 border-l border-cyan-700/40">
                  <Row label="Стресс" value={p.stress_reaction} />
                  <Row label="Критика" value={p.criticism_response} />
                  <Row label="Дедлайны" value={p.deadline_pressure} />
                  <Row label="Конфликт" value={p.conflict_behavior} />
                  <List items={p.likely_triggers} />
                </div>
              ))}
            </div>
          )}

          {(state.data.insights.recommendation?.length ?? 0) > 0 && (
            <div>
              <div className="text-emerald-300 text-xs font-semibold mb-1">
                Рекомендации по взаимодействию
              </div>
              {state.data.insights.recommendation!.slice(0, 4).map((r, i) => (
                <div key={i} className="mb-1.5 pl-2 border-l border-emerald-700/40">
                  <p className="text-slate-200">{r.recommendation}</p>
                  <Row label="Зачем" value={r.rationale} />
                </div>
              ))}
            </div>
          )}

          <p className="text-[11px] text-slate-500 border-t border-brain-700/40 pt-2">
            {state.data.disclaimer}
          </p>
        </div>
      )}
    </div>
  );
}
