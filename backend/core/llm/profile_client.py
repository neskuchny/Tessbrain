"""W42b: BaseLLMClient adapter, бегущий через LLMProfile.

Один класс `ProfileBackedClient` — реализует BaseLLMClient interface и
маршрутизирует generate()/generate_json() в правильный backend:

- profile.provider == "turboquant" → TurboQuantClient (с двойной упаковкой response)
- profile.provider in ("openai", "runpod", "local_vllm") → AsyncOpenAI с
  profile.base_url + api_key
- profile.provider == "gemini" → используем GeminiClient если доступен,
  иначе fallback

Это позволяет LLMRouter использовать один и тот же интерфейс для всех
профилей, не зная про различия в API формате.

Дизайн:
- Lazy-инициализация backend client'а при первом generate() — не блокируем
  __init__
- enabled=True если backend готов; False при invalid config
- model/api_key для совместимости с BaseLLMClient
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.core.llm.base import BaseLLMClient

logger = logging.getLogger(__name__)


class ProfileBackedClient(BaseLLMClient):
    """Adapter: BaseLLMClient → LLMProfile-driven backend."""

    def __init__(
        self,
        *,
        provider: str,
        base_url: Optional[str],
        model: str,
        api_key: Optional[str],
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(model=model, api_key=api_key or "")
        self.provider = provider
        self.base_url = base_url
        self.config = config or {}
        self._backend: Any = None
        # Pre-determine enablement.
        self.enabled = self._can_init()

    # Phase 1 (multi-provider): cloud-провайдеры с OpenAI-compat API ходят
    # через AsyncOpenAI (тот же backend, разные base_url + api_key). Anthropic
    # имеет нативный SDK — отдельная ветка. Локальный Ollama тоже OpenAI-compat.
    _OPENAI_COMPAT_PROVIDERS = {
        "openai", "runpod", "local_vllm",
        "deepseek", "qwen", "openrouter", "together", "mistral", "groq", "ollama", "kimi",
        "xai", "yandex",
    }
    # Дефолтные cloud-endpoints: если профиль создан без base_url — подставим.
    _DEFAULT_BASE_URLS = {
        "openai":     "https://api.openai.com/v1",
        "deepseek":   "https://api.deepseek.com/v1",
        "kimi":       "https://api.moonshot.ai/v1",
        "qwen":       "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "together":   "https://api.together.xyz/v1",
        "mistral":    "https://api.mistral.ai/v1",
        "groq":       "https://api.groq.com/openai/v1",
        "xai":        "https://api.x.ai/v1",
        "yandex":     "https://llm.api.cloud.yandex.net/v1",
        "ollama":     "http://host.docker.internal:11434/v1",
    }

    def _resolved_base_url(self) -> Optional[str]:
        """base_url из профиля → env override → дефолт по провайдеру.

        Phase 2 (local Ollama): для provider='ollama' env-переменная
        OLLAMA_HOST имеет приоритет над дефолтом — это позволяет в проде
        указать удалённый Ollama-сервер без правки кода:
            OLLAMA_HOST=http://10.0.0.5:11434/v1
        Профиль с явным base_url всегда побеждает (per-profile override).
        """
        if self.base_url:
            return self.base_url
        if self.provider == "ollama":
            import os
            env_host = os.getenv("OLLAMA_HOST")
            if env_host:
                return env_host
        return self._DEFAULT_BASE_URLS.get(self.provider)

    def _can_init(self) -> bool:
        if not self.provider:
            return False
        if self.provider == "turboquant":
            return bool(self.base_url and self.model)
        if self.provider == "gemini":
            return bool(self.api_key)
        if self.provider == "anthropic":
            return bool(self.api_key and self.model)
        if self.provider in {"claude_cli", "codex_cli"}:
            # Phase 3 CLI-bridge: enabled только если CLI установлен в
            # контейнере (shutil.which). api_key не требуется — CLI
            # использует собственную subscription auth (claude login /
            # codex login). Если CLI отсутствует — клиент disabled,
            # роутер делает fallback на env-based / другой профиль.
            try:
                from backend.core.llm.cli_clients import (
                    ClaudeCLIClient,
                    CodexCLIClient,
                )
            except ImportError:
                return False
            cls = ClaudeCLIClient if self.provider == "claude_cli" else CodexCLIClient
            return cls(model=self.model)._check_cli_available()
        if self.provider in self._OPENAI_COMPAT_PROVIDERS:
            base = self._resolved_base_url()
            if not base:
                return False
            # local_vllm/ollama могут работать без api_key (если сервер открыт);
            # cloud-провайдеры требуют ключ.
            if self.provider in {"local_vllm", "ollama"}:
                return True
            return bool(self.api_key)
        return False

    async def _ensure_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        if self.provider == "turboquant":
            from backend.core.llm.turboquant_client import TurboQuantClient
            self._backend = TurboQuantClient(
                base_url=self.base_url or "",
                profile_id=self.model,
                api_key=self.api_key or None,
                timeout=float(self.config.get("timeout", 300.0)),
            )
        elif self.provider in self._OPENAI_COMPAT_PROVIDERS:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise RuntimeError(f"openai SDK required for provider={self.provider}: {exc}")
            kwargs: dict[str, Any] = {"api_key": self.api_key or "EMPTY"}
            base = self._resolved_base_url()
            if base:
                kwargs["base_url"] = base
            timeout = self.config.get("timeout")
            if timeout is not None:
                kwargs["timeout"] = float(timeout)
            self._backend = AsyncOpenAI(**kwargs)
        elif self.provider == "gemini":
            from backend.core.llm.gemini_client import GeminiClient
            self._backend = GeminiClient(api_key=self.api_key)
        elif self.provider == "anthropic":
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise RuntimeError(
                    "anthropic SDK required for provider=anthropic — "
                    f"add `anthropic` to requirements.txt: {exc}"
                )
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._backend = AsyncAnthropic(**kwargs)
        elif self.provider in {"claude_cli", "codex_cli"}:
            # Phase 3 CLI-bridge: bridge'аем chat-вызовы через локальный CLI
            # (claude / codex), который использует subscription auth юзера.
            # Subprocess one-shot per request; persistent pty — следующая
            # итерация.
            from backend.core.llm.cli_clients import (
                ClaudeCLIClient,
                CodexCLIClient,
            )
            cls = ClaudeCLIClient if self.provider == "claude_cli" else CodexCLIClient
            timeout = float(self.config.get("timeout", 120.0))
            self._backend = cls(
                model=self.model or "",
                timeout_seconds=timeout,
            )
        else:
            raise RuntimeError(f"unsupported provider: {self.provider}")
        return self._backend

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        **kwargs: Any,
    ) -> str:
        backend = await self._ensure_backend()
        self.stats["total_requests"] += 1
        try:
            if self.provider in {"claude_cli", "codex_cli"}:
                # CLI-bridge: backend сам делает session-аware subprocess.
                # session_id берём из kwargs если есть — для изоляции workdir.
                return await backend.generate(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    session_id=kwargs.get("session_id"),
                )
            if self.provider == "turboquant":
                # TurboQuantClient has direct generate() with same signature.
                return await backend.generate(
                    prompt=prompt, system_prompt=system_prompt,
                    max_tokens=max_tokens, temperature=temperature,
                    top_p=float(kwargs.get("top_p", 0.95)),
                    presence_penalty=float(kwargs.get("presence_penalty", 0.0)),
                )
            if self.provider == "gemini":
                return await backend.generate(
                    prompt=prompt, system_prompt=system_prompt,
                    temperature=temperature, max_tokens=max_tokens, **kwargs,
                )
            if self.provider == "anthropic":
                # Anthropic native SDK: messages-API. system отдельным параметром.
                # temperature НЕ передаём: текущие Claude (Sonnet 5 / Opus 4.x)
                # отклоняют её с 400, а её отсутствие всегда валидно (у старых
                # моделей дефолт разумный). Так Claude реально работает.
                resp = await backend.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system_prompt or "",
                    messages=[{"role": "user", "content": prompt}],
                )
                content = ""
                for block in (resp.content or []):
                    if getattr(block, "type", None) == "text":
                        content += getattr(block, "text", "") or ""
                usage = getattr(resp, "usage", None)
                if usage:
                    self.stats["total_input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
                    self.stats["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
                return content
            # OpenAI-compat path — все провайдеры через AsyncOpenAI.
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            resp = await backend.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if not resp.choices:
                return ""
            msg = resp.choices[0].message
            content = getattr(msg, "content", "") or ""
            usage = getattr(resp, "usage", None)
            if usage:
                self.stats["total_input_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
                self.stats["output_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
            return content
        except Exception as exc:
            self.stats["errors"] += 1
            logger.warning("ProfileBackedClient.generate failed (%s): %s", self.provider, exc)
            raise

    async def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        **kwargs: Any,
    ) -> Any:
        # Все backends умеют отдавать text — парсим через base extract_json
        # (он толерантен к code-fence wrapper'у).
        text = await self.generate(
            prompt=prompt, system_prompt=system_prompt,
            temperature=temperature, max_tokens=max_tokens, **kwargs,
        )
        parsed = self.extract_json(text)
        return parsed if parsed is not None else {}


__all__ = ["ProfileBackedClient"]
