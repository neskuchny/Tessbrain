"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { authFetch } from "@/lib/authFetch";

interface Brand {
  name?: string;
  palette?: string[];
  illustration_style?: string;
  tone?: string;
  logo?: string;
  forbidden_metaphors?: string[];
  preferred_metaphors?: string[];
}

interface BrandAsset {
  asset_id: string;
  kind: string;
  filename: string;
  content_type?: string;
  size?: number;
  preview?: string; // data-URI для картинок
}

const toCsv = (a?: string[]) => (a || []).join(", ");
const fromCsv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

const readAsDataUri = (file: File) => new Promise<string>((resolve, reject) => {
  const fr = new FileReader();
  fr.onload = () => resolve(String(fr.result || ""));
  fr.onerror = () => reject(new Error("read failed"));
  fr.readAsDataURL(file);
});

/** Редактор фирменного профиля тенанта (Visual Reports §14). Один на компанию:
 *  палитра/стиль/лого/запрещённые метафоры → в промпт всех визуальных отчётов. */
export function BrandModal({ onClose }: { onClose: () => void }) {
  const t = useTranslations("banana");
  const [b, setB] = useState<Brand>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [assets, setAssets] = useState<BrandAsset[]>([]);
  const [uploading, setUploading] = useState("");

  const loadAssets = useCallback(async () => {
    try {
      const r = await authFetch("/api/v1/boards/brand/assets");
      const d = await r.json();
      if (Array.isArray(d?.assets)) setAssets(d.assets);
    } catch { /* нет файлов */ }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const r = await authFetch("/api/v1/boards/brand");
        const d = await r.json();
        if (d?.brand) setB(d.brand);
      } catch { /* нет профиля — пустая форма */ }
    })();
    loadAssets();
  }, [loadAssets]);

  const uploadAsset = useCallback(async (kind: string, file: File) => {
    setUploading(kind); setMsg(null);
    try {
      const dataUri = await readAsDataUri(file);
      const r = await authFetch("/api/v1/boards/brand/assets", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, filename: file.name, content_type: file.type, data: dataUri }),
      });
      const d = await r.json();
      if (d?.status !== "success") throw new Error(d?.message || `HTTP ${r.status}`);
      await loadAssets();
    } catch (e) {
      setMsg("❌ " + (e as Error).message);
    } finally { setUploading(""); }
  }, [loadAssets]);

  const removeAsset = useCallback(async (assetId: string) => {
    try {
      await authFetch(`/api/v1/boards/brand/assets/${assetId}`, { method: "DELETE" });
      await loadAssets();
    } catch { /* ignore */ }
  }, [loadAssets]);

  const logoAsset = assets.find((a) => a.kind === "logo");
  const letterheads = assets.filter((a) => a.kind === "letterhead");
  const styleRefs = assets.filter((a) => a.kind === "style_ref");

  const save = useCallback(async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await authFetch("/api/v1/boards/brand", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ brand: b }),
      });
      const d = await r.json();
      if (d?.status !== "success") throw new Error(d?.message || `HTTP ${r.status}`);
      setMsg(t("brand_saved"));
      setTimeout(onClose, 700);
    } catch (e) {
      setMsg("❌ " + (e as Error).message);
    } finally { setBusy(false); }
  }, [b, onClose, t]);

  const field = "w-full px-2 py-1.5 rounded bg-brain-900/60 border border-brain-700 text-sm text-white";
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}>
      <div className="w-full max-w-md rounded-xl border border-brain-700 bg-brain-900 p-4 space-y-2"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold text-brain-100">{t("brand_title")}</div>
          <button onClick={onClose} className="text-brain-400 hover:text-white text-lg leading-none">✕</button>
        </div>
        <p className="text-[11px] text-brain-500">{t("brand_hint")}</p>
        <input className={field} placeholder={t("brand_name")} value={b.name || ""}
          onChange={(e) => setB({ ...b, name: e.target.value })} />
        <input className={field} placeholder={t("brand_palette")} value={toCsv(b.palette)}
          onChange={(e) => setB({ ...b, palette: fromCsv(e.target.value) })} />
        <input className={field} placeholder={t("brand_style")} value={b.illustration_style || ""}
          onChange={(e) => setB({ ...b, illustration_style: e.target.value })} />
        <input className={field} placeholder={t("brand_tone")} value={b.tone || ""}
          onChange={(e) => setB({ ...b, tone: e.target.value })} />
        <input className={field} placeholder={t("brand_logo")} value={b.logo || ""}
          onChange={(e) => setB({ ...b, logo: e.target.value })} />

        {/* Логотип-файл: загрузка + превью + удаление (сохраняется в аккаунте) */}
        <div className="rounded border border-brain-700 p-2 space-y-1.5">
          <div className="text-[11px] text-brain-300 font-medium">{t("brand_logo_file")}</div>
          <div className="flex items-center gap-2">
            {logoAsset?.preview && (
              <img src={logoAsset.preview} alt="logo"
                className="w-10 h-10 object-contain rounded bg-white/90 border border-brain-700" />
            )}
            <label className="px-2 py-1 rounded bg-brain-800 hover:bg-brain-700 text-xs text-brain-100 cursor-pointer">
              {uploading === "logo" ? "…" : (logoAsset ? t("brand_logo_replace") : t("brand_logo_upload"))}
              <input type="file" accept="image/*" className="hidden"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadAsset("logo", f); e.target.value = ""; }} />
            </label>
            {logoAsset && (
              <button onClick={() => removeAsset(logoAsset.asset_id)}
                className="text-[11px] text-brain-400 hover:text-red-300">{t("brand_asset_delete")}</button>
            )}
          </div>
          <div className="text-[10px] text-brain-500">{t("brand_logo_file_hint")}</div>
        </div>

        {/* Бланки (letterheads): загрузка + список + удаление */}
        <div className="rounded border border-brain-700 p-2 space-y-1.5">
          <div className="flex items-center justify-between">
            <div className="text-[11px] text-brain-300 font-medium">{t("brand_letterheads")}</div>
            <label className="px-2 py-1 rounded bg-brain-800 hover:bg-brain-700 text-xs text-brain-100 cursor-pointer">
              {uploading === "letterhead" ? "…" : t("brand_letterhead_upload")}
              <input type="file" accept="image/*,.pdf,.docx" className="hidden"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadAsset("letterhead", f); e.target.value = ""; }} />
            </label>
          </div>
          {letterheads.length > 0 ? (
            <div className="space-y-1">
              {letterheads.map((a) => (
                <div key={a.asset_id} className="flex items-center gap-2 text-xs text-brain-200">
                  <span className="truncate flex-1">📄 {a.filename}</span>
                  <button onClick={() => removeAsset(a.asset_id)}
                    className="text-[11px] text-brain-400 hover:text-red-300">{t("brand_asset_delete")}</button>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-[10px] text-brain-500">{t("brand_letterheads_empty")}</div>
          )}
          <div className="text-[10px] text-brain-500">{t("brand_letterhead_note")}</div>
        </div>

        {/* Образцы фирменного СТИЛЯ (style_ref, до 3): пример схемы/иллюстрации
            «как у нас» → уходит референсом в image-модель визуальных отчётов. */}
        <div className="rounded border border-brain-700 p-2 space-y-1.5">
          <div className="flex items-center justify-between">
            <div className="text-[11px] text-brain-300 font-medium">{t("brand_style_refs")}</div>
            <label className="px-2 py-1 rounded bg-brain-800 hover:bg-brain-700 text-xs text-brain-100 cursor-pointer">
              {uploading === "style_ref" ? "…" : t("brand_style_ref_upload")}
              <input type="file" accept="image/png,image/jpeg,image/webp" className="hidden"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadAsset("style_ref", f); e.target.value = ""; }} />
            </label>
          </div>
          {styleRefs.length > 0 ? (
            <div className="flex gap-2 flex-wrap">
              {styleRefs.map((a) => (
                <div key={a.asset_id} className="flex items-center gap-1.5 text-xs text-brain-200">
                  {a.preview && (
                    <img src={a.preview} alt="style"
                      className="w-10 h-10 object-cover rounded border border-brain-700" />
                  )}
                  <button onClick={() => removeAsset(a.asset_id)}
                    className="text-[11px] text-brain-400 hover:text-red-300">{t("brand_asset_delete")}</button>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-[10px] text-brain-500">{t("brand_style_refs_empty")}</div>
          )}
          <div className="text-[10px] text-brain-500">{t("brand_style_ref_note")}</div>
        </div>
        <input className={field} placeholder={t("brand_forbidden")} value={toCsv(b.forbidden_metaphors)}
          onChange={(e) => setB({ ...b, forbidden_metaphors: fromCsv(e.target.value) })} />
        <input className={field} placeholder={t("brand_preferred")} value={toCsv(b.preferred_metaphors)}
          onChange={(e) => setB({ ...b, preferred_metaphors: fromCsv(e.target.value) })} />
        <div className="flex items-center gap-2 pt-1">
          <button onClick={save} disabled={busy}
            className="px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white text-sm font-medium">
            {busy ? "…" : t("brand_save")}
          </button>
          {msg && <span className="text-xs text-brain-300">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
