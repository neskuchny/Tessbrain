import { NextRequest, NextResponse } from "next/server";
import { GoogleGenAI } from "@google/genai";
import OpenAI from "openai";
import { GenerateRequest, GenerateResponse, ModelType } from "@/banana/types";

export const maxDuration = 300;
export const dynamic = "force-dynamic";

// Backend URL for usage tracking
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

// Авторизация: проверяем Bearer-токен через backend (/auth/me) и возвращаем
// user_id. Без валидного токена генерация запрещена — иначе любой аноним жёг
// бы платный ключ компании. Отключаемо только явным BANANA_REQUIRE_AUTH=false
// (для доверенного single-user окружения).
async function authUserId(request: NextRequest): Promise<string | null> {
  if (process.env.BANANA_REQUIRE_AUTH === "false") return "anonymous";
  const authz = request.headers.get("authorization") || "";
  if (!authz.toLowerCase().startsWith("bearer ")) return null;
  try {
    const res = await fetch(`${BACKEND_URL}/api/v1/auth/me`, {
      headers: { Authorization: authz },
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data?.user?.id || null;
  } catch {
    return null;
  }
}

// P4: предохранитель бюджета. Если per-user квота включена и исчерпана —
// генерацию не запускаем (image-расход уже учитывается в той же квоте).
// Fail-open при сетевой ошибке/выключенной квоте (гард, не жёсткий гейт).
async function overQuota(request: NextRequest, userId: string): Promise<boolean> {
  if (userId === "anonymous") return false;
  const authz = request.headers.get("authorization") || "";
  try {
    const res = await fetch(
      `${BACKEND_URL}/api/v1/usage/quota?user_id=${encodeURIComponent(userId)}`,
      { headers: authz ? { Authorization: authz } : {} }
    );
    if (!res.ok) return false;
    const q = await res.json();
    if (!q?.enabled) return false;
    const remTok = q?.user?.remaining;
    const remCost = q?.user?.cost?.remaining;
    return (typeof remTok === "number" && remTok <= 0) ||
           (typeof remCost === "number" && remCost <= 0);
  } catch {
    return false;
  }
}

// Track image generation usage
async function trackImageUsage(
  provider: string,
  model: string,
  quality: string,
  size: string,
  success: boolean,
  latencyMs: number,
  promptLength: number,
  error?: string,
  userId?: string | null
) {
  try {
    await fetch(`${BACKEND_URL}/api/v1/usage/track-image`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider,
        model,
        quality,
        size,
        success,
        latency_ms: latencyMs,
        prompt_length: promptLength,
        error,
        user_id: userId || null,
        agent_mode: "board",
        request_type: "image_generation",
      }),
    });
  } catch (e) {
    console.error("[Usage Tracking] Failed to track image usage:", e);
  }
}

const GOOGLE_MODEL_MAP: Record<string, string> = {
  // Defaults can be overridden via env in production:
  // - BANANA_MODEL_STANDARD
  // - BANANA_MODEL_PRO
  "nano-banana": process.env.BANANA_MODEL_STANDARD || "gemini-2.5-flash-image",
  "nano-banana-pro": process.env.BANANA_MODEL_PRO || "gemini-3-pro-image-preview",
};

function safeError(error: unknown) {
  if (error instanceof Error) {
    return {
      name: error.name,
      message: error.message,
      stack: error.stack,
      ...(error as any).code ? { code: (error as any).code } : {},
      ...(error as any).status ? { status: (error as any).status } : {},
    };
  }
  return { message: String(error) };
}

// OpenAI model mapping
const OPENAI_MODELS = ["gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini", "dall-e-3"] as const;
type OpenAIImageModel = typeof OPENAI_MODELS[number];

// Generate image using OpenAI models
async function generateWithOpenAI(
  prompt: string,
  model: OpenAIImageModel,
  size: string | undefined,
  quality: string | undefined,
  requestId: string
): Promise<GenerateResponse & { latencyMs?: number }> {
  const startTime = Date.now();
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    return { success: false, error: "OpenAI API key not configured. Add OPENAI_API_KEY to .env.local" };
  }

  const openai = new OpenAI({ apiKey });

  // Map size param to OpenAI format
  let openaiSize: "1024x1024" | "1024x1536" | "1536x1024" | "1792x1024" | "1024x1792" | undefined;
  if (size && size !== "auto") {
    openaiSize = size as typeof openaiSize;
  }

  console.log(`[API:${requestId}] OpenAI ${model}`, {
    promptChars: prompt.length,
    size: openaiSize || "auto",
    quality: quality || "medium",
  });

  // Different models have different parameter support
  const isGptImage = model.startsWith("gpt-image");
  
  const result = await openai.images.generate({
    model,
    prompt,
    n: 1,
    ...(openaiSize && { size: openaiSize }),
    // quality parameter supported by gpt-image models
    ...(isGptImage && quality && { quality: quality as "low" | "medium" | "high" }),
    // dall-e-3 uses different quality values
    ...(!isGptImage && model === "dall-e-3" && { quality: "hd" }),
  });

  const latencyMs = Date.now() - startTime;

  // gpt-image-1 returns b64_json by default
  const imageData = result.data?.[0];
  if (!imageData) {
    return { success: false, error: "No image returned from OpenAI", latencyMs };
  }

  // Handle both base64 and URL responses
  if (imageData.b64_json) {
    const dataUrl = `data:image/png;base64,${imageData.b64_json}`;
    return { success: true, image: dataUrl, latencyMs };
  } else if (imageData.url) {
    // Fetch image from URL and convert to base64
    try {
      const imageResponse = await fetch(imageData.url);
      const arrayBuffer = await imageResponse.arrayBuffer();
      const base64 = Buffer.from(arrayBuffer).toString("base64");
      const contentType = imageResponse.headers.get("content-type") || "image/png";
      const dataUrl = `data:${contentType};base64,${base64}`;
      return { success: true, image: dataUrl, latencyMs };
    } catch (fetchError) {
      console.error(`[API:${requestId}] Failed to fetch image from URL`, fetchError);
      return { success: false, error: "Failed to fetch generated image", latencyMs };
    }
  }

  return { success: false, error: "No image data in response", latencyMs };
}

// Generate image using Google GenAI
async function generateWithGoogle(
  images: string[],
  prompt: string,
  model: ModelType,
  aspectRatio: string | undefined,
  resolution: string | undefined,
  useGoogleSearch: boolean | undefined,
  requestId: string
): Promise<GenerateResponse & { latencyMs?: number }> {
  const startTime = Date.now();
  const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!apiKey) {
    return { success: false, error: "API key not configured. Add GEMINI_API_KEY to .env.local" };
  }

  const imageData = images.map((image) => {
    if (image.includes("base64,")) {
      const [header, data] = image.split("base64,");
      const mimeMatch = header.match(/data:([^;]+)/);
      const mimeType = mimeMatch ? mimeMatch[1] : "image/png";
      return { data, mimeType };
    }
    return { data: image, mimeType: "image/png" };
  });

  const ai = new GoogleGenAI({ apiKey });

  const requestParts: Array<{ text: string } | { inlineData: { mimeType: string; data: string } }> = [
    { text: prompt },
    ...imageData.map(({ data, mimeType }) => ({ inlineData: { mimeType, data } })),
  ];

  const config: any = {
    responseModalities: ["IMAGE", "TEXT"],
  };

  if (aspectRatio) {
    config.imageConfig = { aspectRatio };
  }

  if (model === "nano-banana-pro" && resolution) {
    if (!config.imageConfig) config.imageConfig = {};
    config.imageConfig.imageSize = resolution;
  }

  const tools = [];
  if (model === "nano-banana-pro" && useGoogleSearch) {
    tools.push({ googleSearch: {} });
  }

  console.log(`[API:${requestId}] Google ${GOOGLE_MODEL_MAP[model]}`, {
    imagesCount: images.length,
    aspectRatio,
    resolution,
    useGoogleSearch,
    promptChars: prompt.length,
  });

  const response = await ai.models.generateContent({
    model: GOOGLE_MODEL_MAP[model],
    contents: [{ role: "user", parts: requestParts }],
    config,
    ...(tools.length > 0 && { tools }),
  });

  const latencyMs = Date.now() - startTime;
  console.log(`[API:${requestId}] candidates=${response.candidates?.length ?? 0}, latency=${latencyMs}ms`);

  const candidates = response.candidates;
  if (!candidates || candidates.length === 0) {
    return { success: false, error: "No response from AI model", latencyMs };
  }

  const parts = candidates[0].content?.parts;
  if (!parts) {
    return { success: false, error: "No content in response", latencyMs };
  }

  for (const part of parts) {
    const inline = (part as any).inlineData || (part as any).inline_data;
    if (inline && inline.data) {
      const mimeType = inline.mimeType || inline.mime_type || "image/png";
      const imgData = inline.data;
      const dataUrl = `data:${mimeType};base64,${imgData}`;
      return { success: true, image: dataUrl, latencyMs };
    }
  }

  for (const part of parts) {
    if ((part as any).text) {
      return { success: false, error: (part as any).text?.substring(0, 500) || "Model returned text instead of image", latencyMs };
    }
  }

  return { success: false, error: "No image in response", latencyMs };
}

// Seedream 5 (ByteDance) через Replicate. Text-to-image + правка/мульти-референс:
// если поданы входные картинки (data-URI) — уходят как image_input (до 14).
// pro-slug с фолбэком на lite (если pro не доступен на аккаунте Replicate).
const SEEDREAM_MODELS = ["seedream-5-pro", "seedream-5-lite"] as const;
type SeedreamModel = typeof SEEDREAM_MODELS[number];

async function generateWithSeedream(
  images: string[],
  prompt: string,
  model: SeedreamModel,
  aspectRatio: string | undefined,
  requestId: string
): Promise<GenerateResponse & { latencyMs?: number }> {
  const startTime = Date.now();
  const apiKey = process.env.REPLICATE_API_TOKEN || process.env.SEEDREAM_API_KEY;
  if (!apiKey) {
    return { success: false, error: "Seedream недоступен: задайте REPLICATE_API_TOKEN в окружении." };
  }
  const slugs = model === "seedream-5-pro"
    ? ["bytedance/seedream-5-pro", "bytedance/seedream-5-lite"]
    : ["bytedance/seedream-5-lite"];
  const input: Record<string, unknown> = {
    prompt: prompt.slice(0, 3800), size: "2K", aspect_ratio: aspectRatio || "16:9",
  };
  if (images && images.length > 0) input.image_input = images.slice(0, 14);

  console.log(`[API:${requestId}] Seedream ${model}`, { images: images?.length || 0, aspectRatio });
  let lastErr = "Seedream: генерация не удалась";
  for (const slug of slugs) {
    try {
      const r = await fetch(`https://api.replicate.com/v1/models/${slug}/predictions`, {
        method: "POST",
        headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json", Prefer: "wait" },
        body: JSON.stringify({ input }),
      });
      if (!r.ok) { lastErr = `Seedream ${slug}: HTTP ${r.status}`; continue; }
      let body: any = await r.json();
      for (let i = 0; i < 30; i++) {
        if (body.status === "succeeded") {
          const out = body.output;
          const url = Array.isArray(out) ? out[0] : (typeof out === "string" ? out : null);
          if (!url) break;
          const img = await fetch(url);
          const buf = Buffer.from(await img.arrayBuffer());
          const ct = img.headers.get("content-type") || "image/png";
          return { success: true, image: `data:${ct};base64,${buf.toString("base64")}`, latencyMs: Date.now() - startTime };
        }
        if (body.status === "failed" || body.status === "canceled") { lastErr = `Seedream ${slug}: ${body.error || body.status}`; break; }
        const getUrl = body.urls?.get;
        if (!getUrl) break;
        await new Promise((res) => setTimeout(res, 2000));
        const pr = await fetch(getUrl, { headers: { Authorization: `Bearer ${apiKey}` } });
        if (!pr.ok) break;
        body = await pr.json();
      }
    } catch (e) {
      lastErr = `Seedream ${slug}: ${(e as Error).message}`;
    }
  }
  return { success: false, error: lastErr, latencyMs: Date.now() - startTime };
}

// Reve 2.1 (Reve AI) через fal.ai. Layout-first 4K, силён в тексте на картинке.
// Вход есть → правка/мульти-референс (fal-ai/reve/remix, image_urls 1–6); нет →
// text-to-image. sync_mode:true → картинка приходит data-URI сразу.
async function generateWithReve(
  images: string[],
  prompt: string,
  aspectRatio: string | undefined,
  requestId: string
): Promise<GenerateResponse & { latencyMs?: number }> {
  const startTime = Date.now();
  const apiKey = process.env.FAL_KEY || process.env.REVE_API_KEY;
  if (!apiKey) {
    return { success: false, error: "Reve недоступен: задайте FAL_KEY в окружении." };
  }
  const hasRefs = images && images.length > 0;
  const endpoint = hasRefs ? "fal-ai/reve/remix" : "fal-ai/reve/text-to-image";
  const input: Record<string, unknown> = {
    prompt: prompt.slice(0, 3800), aspect_ratio: aspectRatio || "16:9",
    num_images: 1, sync_mode: true,
  };
  if (hasRefs) input.image_urls = images.slice(0, 6);
  console.log(`[API:${requestId}] Reve ${endpoint}`, { images: images?.length || 0, aspectRatio });
  try {
    const r = await fetch(`https://fal.run/${endpoint}`, {
      method: "POST",
      headers: { Authorization: `Key ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
    if (!r.ok) return { success: false, error: `Reve: HTTP ${r.status}`, latencyMs: Date.now() - startTime };
    const data: any = await r.json();
    const url = data?.images?.[0]?.url;
    if (!url) return { success: false, error: "Reve: пустой ответ", latencyMs: Date.now() - startTime };
    if (String(url).startsWith("data:")) {
      return { success: true, image: url, latencyMs: Date.now() - startTime };
    }
    const img = await fetch(url);
    const buf = Buffer.from(await img.arrayBuffer());
    const ct = img.headers.get("content-type") || "image/png";
    return { success: true, image: `data:${ct};base64,${buf.toString("base64")}`, latencyMs: Date.now() - startTime };
  } catch (e) {
    return { success: false, error: `Reve: ${(e as Error).message}`, latencyMs: Date.now() - startTime };
  }
}

// Прокси генерации картинки в Python-бэкенд (image_gen: выбранная модель +
// фолбэк на платформенные gemini/openai). Используется, когда во
// frontend/.env.local ключа нет — чтобы ключи жили в ОДНОМ месте. Бэкенд сам
// учитывает расход, поэтому вызывающий НЕ дублирует трекинг.
async function backendImageGenerate(
  request: NextRequest, prompt: string, model: string, images: string[]
): Promise<{ success: boolean; image?: string; error?: string; latencyMs?: number }> {
  const startTime = Date.now();
  const authz = request.headers.get("authorization") || "";
  try {
    const res = await fetch(`${BACKEND_URL}/api/v1/boards/image-generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(authz ? { Authorization: authz } : {}) },
      body: JSON.stringify({ prompt, model, images: (images || []).slice(0, 4) }),
    });
    const data = await res.json().catch(() => ({} as Record<string, unknown>));
    if (!res.ok || data?.success === false || !data?.image) {
      return { success: false, latencyMs: Date.now() - startTime,
               error: String(data?.error || `бэкенд-генерация картинки не удалась (${res.status})`) };
    }
    return { success: true, image: String(data.image), latencyMs: Date.now() - startTime };
  } catch (e) {
    return { success: false, latencyMs: Date.now() - startTime,
             error: `бэкенд недоступен: ${(e as Error).message}` };
  }
}

export async function POST(request: NextRequest) {
  const requestId = Math.random().toString(36).substring(7);
  const startTime = Date.now();
  let body: Partial<GenerateRequest> = {};

  const statusFromError = (error: unknown, fallback = 500) => {
    const anyErr = error as any;
    const status = typeof anyErr?.status === "number" ? anyErr.status : undefined;
    if (status) return status;
    const msg = (anyErr?.message ? String(anyErr.message) : String(error || "")).toLowerCase();
    if (msg.includes("resource_exhausted") || msg.includes("quota exceeded") || msg.includes("rate limit") || msg.includes("429")) {
      return 429;
    }
    return fallback;
  };

  // Авторизация ПЕРЕД любой генерацией — иначе аноним жёг бы ключ компании.
  const userId = await authUserId(request);
  if (!userId) {
    return NextResponse.json<GenerateResponse>(
      { success: false, error: "Unauthorized" },
      { status: 401 }
    );
  }

  // P4: бюджетный предохранитель.
  if (await overQuota(request, userId)) {
    return NextResponse.json<GenerateResponse>(
      { success: false, error: "Лимит расхода исчерпан (квота)" },
      { status: 429 }
    );
  }

  try {
    // IMPORTANT: NextRequest body is a stream; it can only be consumed once.
    // We read and parse it once here and reuse for tracking in error paths.
    const raw = await request.text();
    body = raw ? (JSON.parse(raw) as GenerateRequest) : {};
  } catch {
    return NextResponse.json<GenerateResponse>(
      { success: false, error: "Invalid JSON body" },
      { status: 400 }
    );
  }

  try {
    const { images, prompt, model = "nano-banana-pro", aspectRatio, resolution, useGoogleSearch, gptImageSize, gptImageQuality } = body as GenerateRequest;

    if (!prompt) {
      return NextResponse.json<GenerateResponse>({ success: false, error: "Prompt is required" }, { status: 400 });
    }

    let result: GenerateResponse & { latencyMs?: number };

    // Check model family
    const isOpenAIModel = OPENAI_MODELS.includes(model as OpenAIImageModel);
    const isSeedreamModel = SEEDREAM_MODELS.includes(model as SeedreamModel);
    const isReveModel = model === "reve-2.1";
    const provider = isOpenAIModel ? "openai" : isSeedreamModel ? "bytedance" : isReveModel ? "reve" : "google";
    const actualModel = (isOpenAIModel || isSeedreamModel || isReveModel) ? model : GOOGLE_MODEL_MAP[model] || model;

    if (isOpenAIModel) {
      // OpenAI models - text-to-image only, no input images required
      result = await generateWithOpenAI(prompt, model as OpenAIImageModel, gptImageSize, gptImageQuality, requestId);
    } else if (isSeedreamModel) {
      // Seedream 5 — text-to-image; входные картинки опциональны (правка/референс)
      result = await generateWithSeedream(images || [], prompt, model as SeedreamModel, aspectRatio, requestId);
    } else if (isReveModel) {
      // Reve 2.1 — text-to-image; входные картинки опциональны (правка/референс)
      result = await generateWithReve(images || [], prompt, aspectRatio, requestId);
    } else {
      // Google models - require input images
      if (!images || images.length === 0) {
        return NextResponse.json<GenerateResponse>({ success: false, error: "At least one image is required for Google models" }, { status: 400 });
      }
      result = await generateWithGoogle(images, prompt, model, aspectRatio, resolution, useGoogleSearch, requestId);
    }

    // Нет ключа во frontend/.env.local → генерируем на ключах БЭКЕНДА (единый
    // источник ключей). Так узлы-картинки работают без второго env-файла.
    let viaBackend = false;
    if (!result.success && /not configured|api key|нет ключа|ключ/i.test(String(result.error || ""))) {
      const b = await backendImageGenerate(request, prompt, model as string, images || []);
      if (b.success) {
        result = { success: true, image: b.image, latencyMs: b.latencyMs };
        viaBackend = true;
      } else if (b.error) {
        result = { success: false, error: b.error, latencyMs: b.latencyMs };
      }
    }

    // Учёт расхода: НЕ дублируем — если генерил бэкенд, он уже учёл (cost_scope).
    const latencyMs = result.latencyMs || (Date.now() - startTime);
    if (!viaBackend) {
      trackImageUsage(
        provider,
        actualModel,
        gptImageQuality || "medium",
        gptImageSize || "1024x1024",
        result.success,
        latencyMs,
        prompt.length,
        result.error,
        userId
      );
    }

    if (!result.success) {
      const status = statusFromError(result.error, 500);
      return NextResponse.json<GenerateResponse>({ success: false, error: result.error }, { status });
    }

    const payload = { success: true, image: result.image };
    const responseSize = JSON.stringify(payload).length;

    const res = NextResponse.json<GenerateResponse>(payload);
    res.headers.set("Content-Type", "application/json");
    res.headers.set("Content-Length", responseSize.toString());
    return res;
  } catch (error) {
    const e = safeError(error);
    console.error(`[API:${requestId}] Generation error`, e);
    
    // Track failed generation
    const isOpenAIModel = OPENAI_MODELS.includes(body.model as OpenAIImageModel);
    const isSeedreamModel = SEEDREAM_MODELS.includes(body.model as SeedreamModel);
    const isReveModel = body.model === "reve-2.1";
    const provider = isOpenAIModel ? "openai" : isSeedreamModel ? "bytedance" : isReveModel ? "reve" : "google";
    const modelName = (isOpenAIModel || isSeedreamModel || isReveModel) ? (body.model as string) : (GOOGLE_MODEL_MAP[body.model as ModelType] || (body.model as string) || "unknown");
    trackImageUsage(
      provider,
      modelName,
      (body as any).gptImageQuality || "medium",
      (body as any).gptImageSize || "1024x1024",
      false,
      Date.now() - startTime,
      (body as any).prompt?.length || 0,
      e.message,
      userId
    );
    
    const status = statusFromError(e, 500);
    return NextResponse.json<GenerateResponse>(
      {
        success: false,
        error: e.message || "Generation failed",
        debug: process.env.NODE_ENV !== "production" ? { requestId, ...e } : { requestId },
      } as any,
      { status }
    );
  }
}


