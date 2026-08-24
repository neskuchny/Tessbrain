import { NextRequest, NextResponse } from "next/server";
import { GoogleGenAI } from "@google/genai";

// Генерация ВИДЕО на Доске — Veo через тот же GEMINI_API_KEY, которым
// уже генерятся картинки (nano-banana): ноль новых аккаунтов/провайдеров.
// Синхронный роут с поллингом операции (self-hosted Node, таймауты Vercel
// не применимы). text-to-video и image-to-video (первый кадр).

export const maxDuration = 600;

const MODEL_MAP: Record<string, string> = {
  "veo-3.1-fast": "veo-3.1-fast-generate-preview",
  "veo-3.1": "veo-3.1-generate-preview",
};

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const prompt: string = (body.prompt || "").trim();
    const model: string = MODEL_MAP[body.model] || MODEL_MAP["veo-3.1-fast"];
    const image: string | undefined = body.image; // data-URL первого кадра

    if (!prompt) {
      return NextResponse.json({ success: false, error: "prompt required" }, { status: 400 });
    }
    const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
    if (!apiKey) {
      return NextResponse.json(
        { success: false, error: "GEMINI_API_KEY не настроен (тот же ключ, что для картинок)" },
        { status: 500 },
      );
    }

    const ai = new GoogleGenAI({ apiKey });
    const params: any = { model, prompt };
    if (image && image.startsWith("data:")) {
      const [head, b64] = image.split(",", 2);
      const mime = head.slice(5, head.indexOf(";"));
      params.image = { imageBytes: b64, mimeType: mime || "image/png" };
    }

    let operation: any = await ai.models.generateVideos(params);

    // Поллинг: Veo обычно 1–3 минуты; предел ~8 минут.
    const deadline = Date.now() + 8 * 60 * 1000;
    while (!operation.done && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 10_000));
      operation = await ai.operations.getVideosOperation({ operation });
    }
    if (!operation.done) {
      return NextResponse.json({ success: false, error: "Veo: таймаут генерации (8 мин)" }, { status: 504 });
    }
    if (operation.error) {
      return NextResponse.json(
        { success: false, error: `Veo: ${operation.error.message || JSON.stringify(operation.error)}` },
        { status: 502 },
      );
    }

    const video = operation.response?.generatedVideos?.[0]?.video;
    if (!video) {
      return NextResponse.json({ success: false, error: "Veo: пустой ответ (возможно, фильтр контента)" }, { status: 502 });
    }
    // SDK отдаёт либо байты, либо uri файла — приводим к data-URL
    let b64: string | null = video.videoBytes || null;
    if (!b64 && video.uri) {
      const sep = video.uri.includes("?") ? "&" : "?";
      const resp = await fetch(`${video.uri}${sep}key=${apiKey}`);
      if (!resp.ok) {
        return NextResponse.json({ success: false, error: `Veo: download HTTP ${resp.status}` }, { status: 502 });
      }
      b64 = Buffer.from(await resp.arrayBuffer()).toString("base64");
    }
    if (!b64) {
      return NextResponse.json({ success: false, error: "Veo: видео без содержимого" }, { status: 502 });
    }
    return NextResponse.json({ success: true, video: `data:video/mp4;base64,${b64}` });
  } catch (e: any) {
    return NextResponse.json(
      { success: false, error: `Veo error: ${e?.message || String(e)}` },
      { status: 500 },
    );
  }
}
