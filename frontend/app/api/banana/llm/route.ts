import { NextRequest, NextResponse } from "next/server";
import { GoogleGenAI } from "@google/genai";
import { LLMGenerateRequest, LLMGenerateResponse, LLMModelType } from "@/banana/types";

export const maxDuration = 60;

// Backend URL for usage tracking
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

// Авторизация: без валидного Bearer-токена генерация запрещена (иначе аноним
// жёг бы платный ключ компании). Возвращает user_id или null.
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

// P4: предохранитель бюджета (см. generate/route.ts). Fail-open при ошибке.
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

// value → API-id. Новые модели + legacy (обратная совместимость). Для провайдера
// с локальным ключом id уходит в его API как есть; если id не принят или ключа
// нет — генерация проксируется в бэкенд (platform chain), см. ниже.
const GOOGLE_MODEL_MAP: Record<string, string> = {
  "gemini-3.1-pro": "gemini-3.1-pro",
  "gemini-omni-flash-preview": "gemini-omni-flash-preview",
  "gemini-3.6-flash": "gemini-3.6-flash",
  "gemini-3.5-flash": "gemini-3.5-flash",
  "gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
  "gemini-3.1-flash-lite": "gemini-3.1-flash-lite",
  // legacy
  "gemini-2.5-flash": "gemini-2.5-flash",
  "gemini-3-pro-preview": "gemini-3-pro-preview",
};

const OPENAI_MODEL_MAP: Record<string, string> = {
  "gpt-5.6-sol": "gpt-5.6-sol",
  "gpt-5.6-terra": "gpt-5.6-terra",
  "gpt-5.6-luna": "gpt-5.6-luna",
  "gpt-5.1": "gpt-5.1",
  // legacy
  "gpt-4.1-mini": "gpt-4.1-mini",
  "gpt-4.1-nano": "gpt-4.1-nano",
};

// Anthropic (Claude) — нативный Messages API (иная форма запроса/ответа).
// «opus-4.8» — символьное имя (реальный id провайдер-специфичен): при локальном
// ключе может не приняться → сработает фолбэк/бэкенд, где id резолвится из env.
const ANTHROPIC_MODEL_MAP: Record<string, string> = {
  "claude-sonnet-5": "claude-sonnet-5",
  "claude-sonnet-4-5": "claude-sonnet-4-5",
  "claude-haiku-4-5": "claude-haiku-4-5",
  "claude-opus-4-7": "claude-opus-4-7",
  "opus-4.8": "opus-4.8",
  "claude-fable-5": "claude-fable-5",
};

// DeepSeek / Qwen / xAI — все отдают OpenAI-совместимый /chat/completions,
// отличаются только базовым URL, env-ключом и id модели. Один генератор на всех.
const OPENAI_COMPAT: Record<
  string,
  { base: string; keyEnv: string[]; models: Record<string, string> }
> = {
  deepseek: {
    base: "https://api.deepseek.com/chat/completions",
    keyEnv: ["DEEPSEEK_API_KEY"],
    models: {
      "deepseek-v4-pro": "deepseek-v4-pro",
      "deepseek-v4-flash": "deepseek-v4-flash",
      // Снятые с обслуживания id → живые. Проверено 14.08.2026 по
      // api-docs.deepseek.com: /models отдаёт ТОЛЬКО v4-pro и v4-flash.
      // Не удаляем ключи, а перенаправляем: на сохранённых досках эти id
      // уже лежат, и удаление дало бы 400 при запуске.
      "deepseek-v3.2": "deepseek-v4-pro",
      "deepseek-chat": "deepseek-v4-pro",
      "deepseek-reasoner": "deepseek-v4-pro",
    },
  },
  qwen: {
    base: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
    keyEnv: ["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
    models: {
      "qwen3.8-max": "qwen3.8-max",
      "qwen3.7-max": "qwen3.7-max",
      "qwen3.6-plus": "qwen3.6-plus",
      // legacy
      "qwen-max": "qwen-max", "qwen-plus": "qwen-plus",
    },
  },
  xai: {
    base: "https://api.x.ai/v1/chat/completions",
    keyEnv: ["XAI_API_KEY"],
    models: {
      "grok-4.5": "grok-4.5",
      "grok-4.2": "grok-4.2",
      // legacy
      "grok-4": "grok-4", "grok-3-mini": "grok-3-mini",
    },
  },
  // Moonshot (Kimi) — OpenAI-совместимый. Базовый URL и ключ из env (регион/домен
  // у тенанта может отличаться). Если ключа во .env.local нет — узел проксирует
  // генерацию в бэкенд (там свой MOONSHOT_API_KEY + KIMI_BASE_URL).
  kimi: {
    base: process.env.KIMI_BASE_URL || process.env.MOONSHOT_BASE_URL
      || "https://api.moonshot.ai/v1/chat/completions",
    keyEnv: ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
    models: { "kimi-k3": "kimi-k3" },
  },
};

// Переопределение API-id модели из env BANANA_MODEL_IDS (JSON: {выбор: реальный
// id}). Чинит несовпавший id БЕЗ правки кода и может РАЗРЕШИТЬ новую модель, не
// зашитую в карты выше. Читается один раз.
let _ENV_MODEL_IDS: Record<string, string> | null = null;
function envModelIds(): Record<string, string> {
  if (_ENV_MODEL_IDS) return _ENV_MODEL_IDS;
  try { _ENV_MODEL_IDS = JSON.parse(process.env.BANANA_MODEL_IDS || "{}"); }
  catch { _ENV_MODEL_IDS = {}; }
  return _ENV_MODEL_IDS!;
}
function apiId(builtin: string | undefined, model: string): string | undefined {
  return envModelIds()[model] || builtin;
}

// id модели для трекинга/аллоу-листа (null → модель не разрешена у провайдера)
function resolveModelId(provider: string, model: string): string | null {
  if (provider === "google") return apiId(GOOGLE_MODEL_MAP[model], model) || null;
  if (provider === "openai") return apiId(OPENAI_MODEL_MAP[model], model) || null;
  if (provider === "anthropic") return apiId(ANTHROPIC_MODEL_MAP[model], model) || null;
  if (OPENAI_COMPAT[provider]) return apiId(OPENAI_COMPAT[provider].models[model], model) || null;
  return null;
}

// Track LLM usage
async function trackLLMUsage(
  provider: string,
  model: string,
  inputTokens: number,
  outputTokens: number,
  success: boolean,
  latencyMs: number,
  error?: string,
  userId?: string | null
) {
  try {
    await fetch(`${BACKEND_URL}/api/v1/usage/track`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider,
        model,
        input_tokens: inputTokens,
        output_tokens: outputTokens,
        success,
        latency_ms: latencyMs,
        error,
        user_id: userId || null,
        agent_mode: "board",
        request_type: "llm_generation",
      }),
    });
  } catch (e) {
    console.error("[Usage Tracking] Failed to track LLM usage:", e);
  }
}

// Estimate tokens (rough approximation: 1 token ≈ 4 characters)
function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

interface GenerationResult {
  text: string;
  inputTokens: number;
  outputTokens: number;
  latencyMs: number;
}

// Ключи креатив-узлов доски читаются из .env.local ФРОНТЕНДА (эти роуты живут в
// Next.js, а не в Python-бэкенде) — их частая путаница. Сообщения об ошибке прямо
// говорят, КУДА класть ключ, а Google-путь принимает и GOOGLE_API_KEY (как бэкенд).
const ENV_HINT = "в .env.local фронтенда (это ОТДЕЛЬНЫЙ файл от .env бэкенда), затем перезапустите фронтенд";

async function generateWithGoogle(prompt: string, model: LLMModelType, temperature: number, maxTokens: number): Promise<GenerationResult> {
  const startTime = Date.now();
  const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!apiKey) throw new Error(`Нет ключа Google: задайте GEMINI_API_KEY (или GOOGLE_API_KEY) ${ENV_HINT}`);

  const ai = new GoogleGenAI({ apiKey });
  const modelId = apiId(GOOGLE_MODEL_MAP[model], model) || model;

  const response = await ai.models.generateContent({
    model: modelId,
    contents: prompt,
    config: {
      temperature,
      maxOutputTokens: maxTokens,
    },
  });

  const latencyMs = Date.now() - startTime;
  const text = response.text;
  if (!text) throw new Error("No text in Google AI response");
  
  // Try to get actual token counts from response, fallback to estimates
  const usageMetadata = (response as any).usageMetadata;
  const inputTokens = usageMetadata?.promptTokenCount || estimateTokens(prompt);
  const outputTokens = usageMetadata?.candidatesTokenCount || estimateTokens(text);
  
  return { text, inputTokens, outputTokens, latencyMs };
}

async function generateWithOpenAI(prompt: string, model: LLMModelType, temperature: number, maxTokens: number): Promise<GenerationResult> {
  const startTime = Date.now();
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error(`Нет ключа OpenAI: задайте OPENAI_API_KEY ${ENV_HINT}`);

  const modelId = apiId(OPENAI_MODEL_MAP[model], model) || model;

  const response = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model: modelId,
      messages: [{ role: "user", content: prompt }],
      temperature,
      max_tokens: maxTokens,
    }),
  });

  const latencyMs = Date.now() - startTime;

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error?.message || `OpenAI API error: ${response.status}`);
  }

  const data = await response.json();
  const text = data.choices?.[0]?.message?.content;
  if (!text) throw new Error("No text in OpenAI response");

  // Get token counts from OpenAI response
  const inputTokens = data.usage?.prompt_tokens || estimateTokens(prompt);
  const outputTokens = data.usage?.completion_tokens || estimateTokens(text);

  return { text, inputTokens, outputTokens, latencyMs };
}

// DeepSeek / Qwen / xAI — единый OpenAI-совместимый путь (тот же формат, что
// generateWithOpenAI, но с базовым URL и ключом из конфигурации провайдера).
async function generateWithOpenAICompatible(
  cfg: { base: string; keyEnv: string[]; models: Record<string, string> },
  model: LLMModelType, prompt: string, temperature: number, maxTokens: number
): Promise<GenerationResult> {
  const startTime = Date.now();
  const apiKey = cfg.keyEnv.map((k) => process.env[k]).find(Boolean);
  if (!apiKey) throw new Error(`Нет ключа: задайте ${cfg.keyEnv.join(" или ")} ${ENV_HINT}`);
  const modelId = apiId(cfg.models[model], model) || model;

  const response = await fetch(cfg.base, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      model: modelId,
      messages: [{ role: "user", content: prompt }],
      temperature,
      max_tokens: maxTokens,
    }),
  });

  const latencyMs = Date.now() - startTime;
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error?.message || `API error: ${response.status}`);
  }
  const data = await response.json();
  const text = data.choices?.[0]?.message?.content;
  if (!text) throw new Error("No text in response");
  const inputTokens = data.usage?.prompt_tokens || estimateTokens(prompt);
  const outputTokens = data.usage?.completion_tokens || estimateTokens(text);
  return { text, inputTokens, outputTokens, latencyMs };
}

// Anthropic (Claude) — нативный Messages API. temperature НЕ передаём: текущие
// Claude (Sonnet 5) отклоняют её (400); её отсутствие всегда принимается.
async function generateWithAnthropic(
  model: LLMModelType, prompt: string, maxTokens: number
): Promise<GenerationResult> {
  const startTime = Date.now();
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) throw new Error(`Нет ключа Anthropic: задайте ANTHROPIC_API_KEY ${ENV_HINT}`);
  const modelId = apiId(ANTHROPIC_MODEL_MAP[model], model) || model;

  const response = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: modelId,
      max_tokens: maxTokens,
      messages: [{ role: "user", content: prompt }],
    }),
  });

  const latencyMs = Date.now() - startTime;
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error?.message || `Anthropic API error: ${response.status}`);
  }
  const data = await response.json();
  // ответ: { content: [{type:"text", text:"..."}, ...] } — берём текстовые блоки
  const text = Array.isArray(data.content)
    ? data.content.filter((b: { type?: string }) => b.type === "text")
        .map((b: { text?: string }) => b.text || "").join("")
    : "";
  if (!text) throw new Error("No text in Anthropic response");
  const inputTokens = data.usage?.input_tokens || estimateTokens(prompt);
  const outputTokens = data.usage?.output_tokens || estimateTokens(text);
  return { text, inputTokens, outputTokens, latencyMs };
}

// Прокси генерации в Python-бэкенд: там все ключи провайдеров + платформенная
// цепочка (grok→deepseek→gemini). Используется, когда во frontend/.env.local
// ключа нет — чтобы НЕ держать ключи в двух env-файлах. Бэкенд сам учитывает
// расход (cost_scope), поэтому вызывающий НЕ дублирует трекинг. Бросает при сбое.
async function backendGenerate(request: NextRequest, prompt: string,
                               provider?: string, model?: string): Promise<GenerationResult> {
  const startTime = Date.now();
  const authz = request.headers.get("authorization") || "";
  const res = await fetch(`${BACKEND_URL}/api/v1/boards/llm-generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(authz ? { Authorization: authz } : {}) },
    body: JSON.stringify({ prompt, provider, model }),
  });
  const data = await res.json().catch(() => ({} as Record<string, unknown>));
  if (!res.ok || data?.success === false || !data?.text) {
    throw new Error(String(data?.error || `бэкенд-генерация не удалась (${res.status})`));
  }
  const text = String(data.text);
  return { text, inputTokens: estimateTokens(prompt),
           outputTokens: estimateTokens(text), latencyMs: Date.now() - startTime };
}

export async function POST(request: NextRequest) {
  const startTime = Date.now();
  let bodyData: LLMGenerateRequest | null = null;

  // Авторизация перед генерацией.
  const userId = await authUserId(request);
  if (!userId) {
    return NextResponse.json<LLMGenerateResponse>(
      { success: false, error: "Unauthorized" },
      { status: 401 }
    );
  }

  // P4: бюджетный предохранитель.
  if (await overQuota(request, userId)) {
    return NextResponse.json<LLMGenerateResponse>(
      { success: false, error: "Лимит расхода исчерпан (квота)" },
      { status: 429 }
    );
  }

  try {
    bodyData = await request.json();
    if (!bodyData) {
      return NextResponse.json<LLMGenerateResponse>(
        { success: false, error: "Request body is required" },
        { status: 400 }
      );
    }

    const { prompt, provider, model, temperature = 0.7, maxTokens = 1024 } = bodyData;

    if (!prompt) {
      return NextResponse.json<LLMGenerateResponse>({ success: false, error: "Prompt is required" }, { status: 400 });
    }
    const KNOWN_PROVIDERS = ["google", "openai", "anthropic", "deepseek", "qwen", "xai", "kimi"];
    if (!KNOWN_PROVIDERS.includes(provider)) {
      return NextResponse.json<LLMGenerateResponse>(
        { success: false, error: `Unknown provider: ${String(provider)}` },
        { status: 400 }
      );
    }
    const actualModel = resolveModelId(provider, model);
    if (!actualModel) {
      return NextResponse.json<LLMGenerateResponse>(
        { success: false, error: `Unknown model for ${provider}: ${String(model)}` },
        { status: 400 }
      );
    }

    const runPrimary = (): Promise<GenerationResult> => {
      if (provider === "google") return generateWithGoogle(prompt, model, temperature, maxTokens);
      if (provider === "openai") return generateWithOpenAI(prompt, model, temperature, maxTokens);
      if (provider === "anthropic") return generateWithAnthropic(model, prompt, maxTokens);
      return generateWithOpenAICompatible(
        OPENAI_COMPAT[provider], model, prompt, temperature, maxTokens);
    };
    let result: GenerationResult;
    let viaBackend = false;
    try {
      result = await runPrimary();
    } catch {
      // 1) локальный Gemini-фолбэк (если ключ есть во frontend/.env.local).
      if (provider !== "google" && (process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY)) {
        try {
          result = await generateWithGoogle(
            prompt, "gemini-2.5-flash" as LLMModelType, temperature, maxTokens);
        } catch {
          result = await backendGenerate(request, prompt, provider, model); viaBackend = true;
        }
      } else {
        // 2) ключа во .env.local нет → генерируем на ключах БЭКЕНДА (единый
        // источник ключей). Так креатив-доска работает без второго env-файла.
        result = await backendGenerate(request, prompt); viaBackend = true;
      }
    }

    // Учёт расхода: НЕ дублируем — если генерил бэкенд, он уже учёл (cost_scope).
    if (!viaBackend) {
      trackLLMUsage(
        provider,
        actualModel || model,
        result.inputTokens,
        result.outputTokens,
        true,
        result.latencyMs,
        undefined,
        userId
      );
    }

    return NextResponse.json<LLMGenerateResponse>({ success: true, text: result.text });
  } catch (error) {
    console.error("LLM generation error:", error);
    
    // Track failed usage
    if (bodyData) {
      const actualModel = resolveModelId(bodyData.provider, bodyData.model);
      trackLLMUsage(
        bodyData.provider,
        actualModel || bodyData.model,
        estimateTokens(bodyData.prompt || ""),
        0,
        false,
        Date.now() - startTime,
        error instanceof Error ? error.message : "Unknown error",
        userId
      );
    }

    if (error instanceof Error && error.message.includes("429")) {
      return NextResponse.json<LLMGenerateResponse>({ success: false, error: "Rate limit reached. Please wait and try again." }, { status: 429 });
    }

    return NextResponse.json<LLMGenerateResponse>(
      { success: false, error: error instanceof Error ? error.message : "LLM generation failed" },
      { status: 500 }
    );
  }
}


