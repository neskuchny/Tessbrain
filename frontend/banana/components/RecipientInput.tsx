"use client";

import { useEffect, useId, useState } from "react";
import { authFetch } from "@/lib/authFetch";

// Известные получатели из «Интеграций» (привязанный TG-чат, группы
// получателей, email'ы) — люди выбирают из списка, а не вспоминают ID.
// Пусто в интеграциях → обычное поле ввода, как раньше.
type Recips = { telegram: { id: string; label: string }[]; emails: string[] };
let _cache: Recips | null = null;
let _inflight: Promise<Recips> | null = null;

async function loadRecipients(): Promise<Recips> {
  if (_cache) return _cache;
  if (!_inflight) {
    _inflight = (async () => {
      try {
        const r = await authFetch("/api/v1/task-analysis/recipients");
        const d = await r.json();
        _cache = { telegram: d.telegram || [], emails: d.emails || [] };
      } catch { _cache = { telegram: [], emails: [] }; }
      return _cache as Recips;
    })();
  }
  return _inflight;
}

export function RecipientInput({ value, onChange, kind, placeholder, title, className }: {
  value: string;
  onChange: (v: string) => void;
  kind: "telegram" | "email";
  placeholder?: string;
  title?: string;
  className?: string;
}) {
  const listId = useId();
  const [opts, setOpts] = useState<{ id: string; label: string }[]>([]);
  useEffect(() => {
    loadRecipients().then((d) => {
      if (!d) return;
      setOpts(kind === "telegram" ? d.telegram
        : d.emails.map((e) => ({ id: e, label: e })));
    });
  }, [kind]);
  return (
    <>
      <input value={value} onChange={(e) => onChange(e.target.value)}
        list={opts.length ? listId : undefined}
        placeholder={placeholder} title={title} className={className} />
      {opts.length > 0 && (
        <datalist id={listId}>
          {opts.map((o) => (
            <option key={o.id} value={o.id}>{o.label}</option>
          ))}
        </datalist>
      )}
    </>
  );
}
