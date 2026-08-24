"use client";

import React, { useState, useCallback } from "react";
import { useTranslations } from "next-intl";

// P8: визуализация трассы происхождения узла (data lineage).
// Бэкенд: GET /api/v1/lineage/trace?node_id=&user_id=

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface LineageEvent {
  stage: string;
  actor: string;
  op: string;
  ts: string;
  note?: string;
}

interface LineageRecord {
  created_by: string;
  pipeline_id: string;
  origin_stage: string;
  created_at: string;
  history: LineageEvent[];
}

interface TraceResponse {
  found: boolean;
  node_id: string;
  label?: string | null;
  lineage: Partial<LineageRecord>;
  trace: LineageEvent[];
  backend?: string | null;
  has_lineage?: boolean;
  error?: string;
}

export interface LineageGraphTrace {
  targets: Array<{
    entityId: string;
    label?: string;
    traceNodeKey?: string;
    isAnchor?: boolean;
  }>;
  edges: Array<{ fromKey: string; toKey: string; label?: string }>;
  reasoningSteps: string[];
  title: string;
  requestId: number;
}

interface PipelineResponse {
  found: boolean;
  pipeline_id: string;
  nodes: Array<{ id: string; label: string }>;
  edges: Array<{ from: string; to: string; label?: string }>;
  history: LineageEvent[];
  truncated?: boolean;
  error?: string;
}

interface Props {
  userId: string;
  onShowOnGraph?: (req: LineageGraphTrace) => void;
}

const opColor = (op: string): string => {
  switch (op) {
    case "create":
      return "#4CAF50";
    case "merge":
      return "#2196F3";
    case "update":
      return "#FF9800";
    case "transform":
      return "#9C27B0";
    default:
      return "#607D8B";
  }
};

export default function LineageTracePanel({ userId, onShowOnGraph }: Props) {
  const t = useTranslations("lineage_trace_panel");
  const [nodeId, setNodeId] = useState("");
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<TraceResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [overlayLoading, setOverlayLoading] = useState(false);

  const fetchTrace = useCallback(async () => {
    const id = nodeId.trim();
    if (!id) return;
    setLoading(true);
    setErr(null);
    setData(null);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/lineage/trace?node_id=${encodeURIComponent(
          id
        )}&user_id=${encodeURIComponent(userId)}`
      );
      const json: TraceResponse = await res.json();
      setData(json);
      if (json.error) setErr(json.error);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "request failed");
    } finally {
      setLoading(false);
    }
  }, [nodeId, userId]);

  const showOnGraph = useCallback(async () => {
    const id = nodeId.trim();
    if (!id || !onShowOnGraph) return;
    setOverlayLoading(true);
    setErr(null);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/lineage/pipeline?node_id=${encodeURIComponent(
          id
        )}&user_id=${encodeURIComponent(userId)}`
      );
      const json: PipelineResponse = await res.json();
      if (json.error) {
        setErr(json.error);
        return;
      }
      if (!json.found || json.nodes.length === 0) {
        setErr("no_pipeline");
        return;
      }
      onShowOnGraph({
        targets: json.nodes.map((n) => ({
          entityId: n.id,
          label: n.label,
          traceNodeKey: n.id,
          isAnchor: n.id === id,
        })),
        edges: json.edges.map((e) => ({
          fromKey: e.from,
          toKey: e.to,
          label: e.label,
        })),
        reasoningSteps: (json.history || []).map(
          (h) => `${h.op} · ${h.stage} · ${h.actor}`
        ),
        title: `Lineage: ${json.pipeline_id}${
          json.truncated ? ` ${t("truncated_suffix")}` : ""
        }`,
        requestId: Date.now(),
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : "request failed");
    } finally {
      setOverlayLoading(false);
    }
  }, [nodeId, userId, onShowOnGraph]);

  const rec = data?.lineage || {};
  const history: LineageEvent[] = data?.trace || [];

  return (
    <div style={{ padding: 20, maxWidth: 820 }}>
      <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 4 }}>
        {t("title")}
      </h2>
      <p style={{ color: "#888", fontSize: 13, marginBottom: 16 }}>
        {t("subtitle")}
      </p>

      <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
        <input
          value={nodeId}
          onChange={(e) => setNodeId(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && fetchTrace()}
          placeholder={t("node_id_placeholder")}
          style={{
            flex: 1,
            padding: "8px 12px",
            border: "1px solid #ccc",
            borderRadius: 6,
            fontSize: 14,
          }}
        />
        <button
          onClick={fetchTrace}
          disabled={loading || !nodeId.trim()}
          style={{
            padding: "8px 18px",
            background: loading ? "#9e9e9e" : "#2196F3",
            color: "#fff",
            border: "none",
            borderRadius: 6,
            cursor: loading ? "default" : "pointer",
            fontSize: 14,
          }}
        >
          {loading ? t("loading") : t("show_trace")}
        </button>
      </div>

      {err && (
        <div
          style={{
            padding: 12,
            background: "#FFEBEE",
            color: "#C62828",
            borderRadius: 6,
            marginBottom: 16,
            fontSize: 13,
          }}
        >
          {err === "forbidden"
            ? t("error_forbidden")
            : err === "lookup failed"
            ? t("error_lookup_failed")
            : err === "no_pipeline"
            ? t("error_no_pipeline")
            : err}
        </div>
      )}

      {data && !data.found && !err && (
        <div style={{ color: "#888", fontSize: 14 }}>
          {t("not_found")}
        </div>
      )}

      {data && data.found && (
        <>
          <div
            style={{
              border: "1px solid #e0e0e0",
              borderRadius: 8,
              padding: 16,
              marginBottom: 20,
              background: "#FAFAFA",
            }}
          >
            <div style={{ fontWeight: 600, marginBottom: 8 }}>
              {data.label || t("node_fallback")}{" "}
              <span style={{ color: "#888", fontWeight: 400 }}>
                ({data.node_id})
              </span>
            </div>
            {data.has_lineage ? (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "160px 1fr",
                  rowGap: 6,
                  fontSize: 13,
                }}
              >
                <span style={{ color: "#888" }}>{t("created_by")}</span>
                <span>{rec.created_by || "—"}</span>
                <span style={{ color: "#888" }}>Pipeline</span>
                <span>{rec.pipeline_id || "—"}</span>
                <span style={{ color: "#888" }}>{t("origin_stage")}</span>
                <span>{rec.origin_stage || "—"}</span>
                <span style={{ color: "#888" }}>{t("created_at")}</span>
                <span>{rec.created_at || "—"}</span>
                <span style={{ color: "#888" }}>Backend</span>
                <span>{data.backend || "—"}</span>
              </div>
            ) : (
              <div style={{ color: "#888", fontSize: 13 }}>
                {t("no_lineage")}
              </div>
            )}
          </div>

          {onShowOnGraph && data.has_lineage && (
            <button
              onClick={showOnGraph}
              disabled={overlayLoading}
              style={{
                padding: "8px 16px",
                marginBottom: 20,
                background: overlayLoading ? "#9e9e9e" : "#673AB7",
                color: "#fff",
                border: "none",
                borderRadius: 6,
                cursor: overlayLoading ? "default" : "pointer",
                fontSize: 13,
              }}
            >
              {overlayLoading ? t("building_overlay") : t("show_on_graph")}
            </button>
          )}

          {history.length > 0 && (
            <div>
              <div style={{ fontWeight: 600, marginBottom: 12 }}>
                {t("history_title", { n: history.length })}
              </div>
              <div style={{ position: "relative", paddingLeft: 24 }}>
                <div
                  style={{
                    position: "absolute",
                    left: 7,
                    top: 4,
                    bottom: 4,
                    width: 2,
                    background: "#e0e0e0",
                  }}
                />
                {history.map((ev, i) => (
                  <div
                    key={i}
                    style={{ position: "relative", marginBottom: 18 }}
                  >
                    <div
                      style={{
                        position: "absolute",
                        left: -22,
                        top: 2,
                        width: 14,
                        height: 14,
                        borderRadius: "50%",
                        background: opColor(ev.op),
                        border: "2px solid #fff",
                        boxShadow: "0 0 0 1px #e0e0e0",
                      }}
                    />
                    <div style={{ fontSize: 13 }}>
                      <span
                        style={{
                          fontWeight: 600,
                          color: opColor(ev.op),
                        }}
                      >
                        {ev.op}
                      </span>{" "}
                      <span style={{ color: "#555" }}>{ev.stage}</span>
                    </div>
                    <div style={{ fontSize: 12, color: "#888" }}>
                      {ev.actor} · {ev.ts}
                      {ev.note ? ` · ${ev.note}` : ""}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
