# -*- coding: utf-8 -*-
"""Нативные вызыватели провайдеров (НЕ через OpenRouter).

Один интерфейс: async .generate(prompt)->str, копит .total_in/.total_out
(вывод ВКЛЮЧАЕТ reasoning-токены — по CSV они 70-130% от completion и биллятся),
.aclose(). Фабрика make_caller(native, model_id, api_key) выбирает класс по
провайдеру из model_router.NATIVE_ENDPOINT.

Три формата:
  • Anthropic     — x-api-key, /v1/messages
  • OpenAI-совмест — bearer, /chat/completions (openai, xai, deepseek, qwen)
  • Gemini        — ?key=, /models/{id}:generateContent

Модуль флаг-независимый; используется и продуктом (за флагом), и бенчем."""
from __future__ import annotations

from typing import Any, Dict, Optional

from backend.core.llm.model_router import NATIVE_ENDPOINT

_TIMEOUT = 180
_MAX_OUT = 8000


class _Base:
    def __init__(self, model_id: str, api_key: str):
        self.model, self.key = model_id, api_key
        self.total_in = self.total_out = self.total_reasoning = 0
        self.provider = ""       # ставит make_caller — нужен для учёта расхода
        self._flushed = False
        self._c = None

    async def _client(self):
        import httpx
        if self._c is None:
            self._c = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._c

    def flush_usage(self, **attrs) -> None:
        """Записать накопленные токены в usage_tracker (ОДИН раз на вызыватель).

        Токены уже честно посчитаны из ответов API (см. total_in/total_out), но
        раньше их НИКТО не читал: весь нативный/тировый путь и доска не писали
        расход, поэтому квоты недосчитывали трафик. Пишем здесь — единая точка
        для всех потребителей. user_id/сущность подхватываются из окружающего
        cost_scope (usage_context-fallback в UsageTracker.track). Never-raise:
        учёт не должен ронять генерацию."""
        if self._flushed:
            return
        self._flushed = True
        if not (self.total_in or self.total_out):
            return                      # нечего писать (ошибка/пустой прогон)
        try:
            from backend.core.llm.usage_tracker import track_usage
            track_usage(provider=(self.provider or "native"), model=self.model,
                        input_tokens=int(self.total_in),
                        output_tokens=int(self.total_out), **attrs)
        except Exception:                # noqa: BLE001 — учёт не критичен
            import logging
            logging.getLogger(__name__).debug("usage flush skipped", exc_info=True)

    async def aclose(self):
        # Флашим ДО закрытия клиента: aclose() зовут все потребители (в т.ч. в
        # finally), поэтому расход попадает в учёт автоматически.
        self.flush_usage()
        if self._c:
            await self._c.aclose()
            self._c = None


class AnthropicCaller(_Base):
    async def generate(self, prompt: str) -> str:
        c = await self._client()
        r = await c.post(
            NATIVE_ENDPOINT["anthropic"]["base"],
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": _MAX_OUT, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]})
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic {r.status_code} для '{self.model}': {r.text[:400]}")
        d = r.json()
        u = d.get("usage") or {}
        self.total_in += int(u.get("input_tokens") or 0)
        self.total_out += int(u.get("output_tokens") or 0)  # Anthropic output уже полный
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


class OpenAICompatCaller(_Base):
    """OpenAI-совместимый chat/completions: openai, xai, deepseek, qwen(DashScope).

    send_temperature=False — не слать temperature вовсе: reasoning-модели
    OpenAI (gpt-5*) отвергают любое значение кроме дефолтного и падают 400
    «'temperature' does not support 0» (поймано живой проверкой)."""
    def __init__(self, model_id: str, api_key: str, base_url: str,
                 send_temperature: bool = True):
        super().__init__(model_id, api_key)
        self.base_url = base_url
        self.send_temperature = send_temperature

    async def generate(self, prompt: str) -> str:
        c = await self._client()
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.send_temperature:
            payload["temperature"] = 0
        r = await c.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.base_url} {r.status_code} для '{self.model}': {r.text[:400]}")
        d = r.json()
        u = d.get("usage") or {}
        self.total_in += int(u.get("prompt_tokens") or 0)
        comp = int(u.get("completion_tokens") or 0)
        # reasoning-токены: в completion_tokens_details.reasoning_tokens; у части
        # провайдеров они НЕ входят в completion_tokens — добираем в вывод.
        details = u.get("completion_tokens_details") or {}
        rea = int(details.get("reasoning_tokens") or u.get("reasoning_tokens") or 0)
        self.total_reasoning += rea
        self.total_out += comp if rea and rea <= comp else comp + rea
        ch = (d.get("choices") or [{}])[0]
        return (ch.get("message") or {}).get("content") or ""


class GeminiCaller(_Base):
    """Нативный Gemini API (generativelanguage), ключ в query."""
    async def generate(self, prompt: str) -> str:
        c = await self._client()
        base = NATIVE_ENDPOINT["google"]["base"]
        url = f"{base}/{self.model}:generateContent?key={self.key}"
        r = await c.post(url, headers={"Content-Type": "application/json"},
                         json={"contents": [{"parts": [{"text": prompt}]}],
                               "generationConfig": {"temperature": 0}})
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini {r.status_code} для '{self.model}': {r.text[:400]}")
        d = r.json()
        um = d.get("usageMetadata") or {}
        self.total_in += int(um.get("promptTokenCount") or 0)
        self.total_out += int(um.get("candidatesTokenCount") or 0) + int(um.get("thoughtsTokenCount") or 0)
        parts = (((d.get("candidates") or [{}])[0]).get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts)


def make_caller(native: str, model_id: str, api_key: str) -> Optional[_Base]:
    """Собрать нативный вызыватель по провайдеру. None, если провайдер не нативный."""
    caller: Optional[_Base] = None
    if native == "anthropic":
        caller = AnthropicCaller(model_id, api_key)
    elif native == "google":
        caller = GeminiCaller(model_id, api_key)
    elif native in ("openai", "xai", "deepseek", "qwen", "moonshot"):
        caller = OpenAICompatCaller(model_id, api_key, NATIVE_ENDPOINT[native]["base"],
                                    send_temperature=(native != "openai"))
    if caller is not None:
        caller.provider = native      # для учёта расхода (flush_usage)
    return caller
