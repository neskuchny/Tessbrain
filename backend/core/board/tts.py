# -*- coding: utf-8 -*-
"""Озвучка отчётов (Text-to-Speech): текст → аудио для доставки в Telegram/почту.

Смысл: люди не любят читать длинные отчёты — но охотно СЛУШАЮТ (как подкаст,
NotebookLM). Мозг собрал сводку недели/месяца → озвучили → слушаешь фоном.

Провайдеры (выбираются на узле «Озвучка»):
- openai   — работает сразу на платформенном ключе (gpt-4o-mini-tts / tts-1);
- elevenlabs — лучшее качество/эмоции, по BYO-ключу из «Интеграций» (elevenlabs).
Ключи: платформенный env (OPENAI_API_KEY) → BYO из «Интеграций» тенанта.
Никогда не raises: возвращает {"audio": bytes|None, "ext", "provider", "error"}.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Голоса по провайдерам (id как ждёт API). Дефолт — первый.
OPENAI_VOICES = ["alloy", "nova", "shimmer", "onyx", "echo", "fable"]
_OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
_ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
_ELEVEN_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel (публичный дефолтный голос)


async def _integration_key(user_id: Optional[str], provider: str) -> str:
    if not user_id:
        return ""
    try:
        from backend.api.routes.integrations import get_user_integration_keys
        keys = await get_user_integration_keys(user_id, provider) or {}
        return str(keys.get("api_key") or "").strip()
    except Exception:
        logger.debug("tts: integration key lookup failed", exc_info=True)
        return ""


async def _openai_tts(user_id: Optional[str], text: str, voice: str) -> Optional[bytes]:
    key = (os.environ.get("OPENAI_API_KEY") or "").strip() or await _integration_key(user_id, "openai")
    if not key:
        return None
    v = voice if voice in OPENAI_VOICES else OPENAI_VOICES[0]
    import httpx
    for model in ("gpt-4o-mini-tts", "tts-1"):
        try:
            async with httpx.AsyncClient(timeout=90.0) as c:
                r = await c.post(
                    _OPENAI_TTS_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": model, "voice": v, "input": text[:4000],
                          "response_format": "mp3"})
            if r.status_code == 200 and r.content:
                return r.content
            logger.info("tts openai %s -> %s %s", model, r.status_code, r.text[:200])
        except Exception as e:
            logger.info("tts openai %s failed: %s", model, e)
    return None


async def _elevenlabs_tts(user_id: Optional[str], text: str, voice: str) -> Optional[bytes]:
    key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip() or await _integration_key(user_id, "elevenlabs")
    if not key:
        return None
    vid = voice.strip() if voice and not voice.startswith("_") else _ELEVEN_DEFAULT_VOICE
    import httpx
    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                _ELEVEN_URL.format(voice=vid),
                headers={"xi-api-key": key, "accept": "audio/mpeg"},
                json={"text": text[:5000], "model_id": "eleven_multilingual_v2"})
        if r.status_code == 200 and r.content:
            return r.content
        logger.info("tts elevenlabs -> %s %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.info("tts elevenlabs failed: %s", e)
    return None


async def synthesize_speech(user_id: Optional[str], text: str, *,
                            provider: str = "openai",
                            voice: str = "") -> Dict[str, Any]:
    """Озвучить текст. Возвращает {"audio": bytes|None, "ext", "provider", "error"}."""
    text = (text or "").strip()
    if not text:
        return {"audio": None, "ext": "mp3", "provider": "", "error": "пустой текст для озвучки"}
    provider = (provider or "openai").strip().lower()
    audio: Optional[bytes] = None
    used = provider
    if provider == "elevenlabs":
        audio = await _elevenlabs_tts(user_id, text, voice)
        if not audio:  # мягкий фолбэк на openai, если BYO-ключа нет
            audio = await _openai_tts(user_id, text, voice)
            used = "openai" if audio else provider
    else:
        audio = await _openai_tts(user_id, text, voice)
        used = "openai"
    if not audio:
        return {"audio": None, "ext": "mp3", "provider": provider,
                "error": ("озвучка не удалась: нет ключа TTS. Для OpenAI задайте "
                          "OPENAI_API_KEY (или ключ OpenAI в «Интеграциях»); для "
                          "ElevenLabs — ключ elevenlabs во вкладке «Интеграции».")}
    return {"audio": audio, "ext": "mp3", "provider": used, "error": None}


def save_board_audio(audio: bytes, user_id: Optional[str], ext: str = "mp3") -> str:
    """Сохранить аудио в data/board_audio/<uid>/ и вернуть путь."""
    from backend.core.store.tenant_paths import _DATA_ROOT
    safe_uid = "".join(c for c in str(user_id or "anon")
                       if c.isalnum() or c == "-")[:40] or "anon"
    d = _DATA_ROOT / "board_audio" / safe_uid
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uuid.uuid4().hex[:16]}.{ext}"
    path.write_bytes(audio)
    return str(path)


__all__ = ["synthesize_speech", "save_board_audio", "OPENAI_VOICES"]
