"""Telegram voice/audio handler (Phase A.4).

Принимает voice / audio / video_note из Telegram, транскрибирует через
Gemini Audio API, возвращает текст. Используется в telegram_webhook
до того как сообщение попадает в `route_inbound_message`.

Flow:
1. Webhook видит `message.voice` или `message.audio` (с file_id)
2. Через Telegram Bot API скачиваем файл (≤20MB лимит Bot API)
3. Передаём в transcribe_telegram_voice() — отдаёт plain text
4. Этот текст подмешивается как `message.text` в обычный flow

Gemini Audio API:
- Поддерживает ogg, mp3, m4a, wav, webm, opus
- Telegram voice по умолчанию: voice → ogg/opus, audio → mp3/m4a
- Цена ~$0.07 / 1M аудио-токенов (≈ дешевле Whisper API)

Бюджет звонка:
- ≤60 сек voice note: ~$0.0003 за транскрипцию
- 5 мин: ~$0.0015
- Час: ~$0.02

Never-raise: при ошибке транскрипции возвращаем None и логируем — webhook
продолжит работу с fallback-текстом «🎤 не получилось транскрибировать».
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# Telegram file_id → bytes
async def _download_telegram_file(
    bot_token: str, file_id: str, timeout_s: float = 30.0,
) -> Optional[tuple[bytes, str]]:
    """Скачать файл из Telegram. Возвращает (bytes, mime_type) или None."""
    if not (bot_token and file_id):
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            # 1. Получаем file_path через getFile
            r = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getFile",
                params={"file_id": file_id},
            )
            r.raise_for_status()
            info = r.json()
            if not info.get("ok"):
                logger.warning("getFile failed: %s", info)
                return None
            file_path = info["result"].get("file_path")
            if not file_path:
                logger.warning("getFile: no file_path in response")
                return None

            # 2. Скачиваем содержимое
            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            r2 = await client.get(download_url)
            r2.raise_for_status()

            # 3. Определяем mime по расширению file_path
            ext = (file_path.rsplit(".", 1)[-1] or "").lower()
            mime_map = {
                "ogg": "audio/ogg",
                "oga": "audio/ogg",
                "mp3": "audio/mpeg",
                "m4a": "audio/mp4",
                "wav": "audio/wav",
                "webm": "audio/webm",
                "opus": "audio/opus",
            }
            mime = mime_map.get(ext, "audio/ogg")
            return r2.content, mime
    except Exception as exc:
        logger.warning("Telegram file download failed: %s", exc)
        return None


async def _transcribe_via_gemini(
    audio_bytes: bytes, mime_type: str,
) -> Optional[str]:
    """Транскрибировать через Gemini Audio API.

    Best-effort: ошибки логируем, возвращаем None — webhook продолжит
    работу. Не зависит от LLMRouter — используем google-generativeai
    напрямую с GOOGLE_API_KEY/GEMINI_API_KEY.
    """
    api_key = (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
    )
    if not api_key:
        logger.warning("voice handler: GEMINI_API_KEY/GOOGLE_API_KEY not set")
        return None
    try:
        import google.generativeai as genai
    except ImportError:
        logger.warning("voice handler: google-generativeai не установлен")
        return None

    try:
        genai.configure(api_key=api_key)
        # Аудио→текст — мультимодальная задача (Qwen-текст её не делает),
        # поэтому путь остаётся Gemini/мультимодальным. Модель настраиваемая
        # (VOICE_TRANSCRIBE_MODEL) — для локального STT-прокси/другого id.
        _voice_model = os.getenv("VOICE_TRANSCRIBE_MODEL",
                                 "gemini-flash-lite-latest")
        model = genai.GenerativeModel(_voice_model)
        # Передаём аудио inline (≤20MB; для больших — File API, но в Telegram
        # лимит 20MB у Bot API так что вписываемся)
        audio_part = {"mime_type": mime_type, "data": audio_bytes}
        # issue #112 T-1: сохранять speaker diarization labels если Gemini
        # их распознал. РАНЬШЕ промпт явно требовал их убирать — это
        # выбрасывало информацию, на которой держится attribution в системе
        # памяти (кто сказал → кому приписать задачу/решение). Теперь:
        #   - один спикер в аудио → Gemini не добавит labels (естественное
        #     поведение) → текст как был, downstream Telegram-чат не сломается
        #   - несколько спикеров → текст с 'Speaker 1: ..., Speaker 2: ...'
        #     → downstream extractor'ы получают context для attribution
        # Префиксы типа 'Транскрипция:' по-прежнему запрещены — это шум,
        # не сигнал, и его действительно нужно убирать.
        prompt = (
            "Транскрибируй эту голосовую запись на исходном языке. "
            "Если в аудио несколько говорящих — сохрани разделение по "
            "спикерам в формате 'Speaker 1: ..., Speaker 2: ...' "
            "(диаризация важна для attribution в downstream-обработке). "
            "Если спикер один — просто текст без префиксов. "
            "Не добавляй служебные подписи типа 'Транскрипция:' или "
            "'Текст:'."
        )
        resp = model.generate_content([prompt, audio_part])
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            logger.warning("voice handler: Gemini returned empty text")
            return None
        return text
    except Exception as exc:
        logger.warning("voice handler: Gemini transcribe failed: %s", exc)
        return None


async def transcribe_telegram_voice(
    bot_token: str, message: dict[str, Any],
) -> Optional[str]:
    """Извлечь и транскрибировать voice/audio/video_note из Telegram message.

    Возвращает text транскрипта или None если:
    - в сообщении нет voice/audio полей
    - скачивание/транскрипция упали
    - GEMINI_API_KEY не настроен

    `message` — это `update.message` из Telegram webhook payload.
    """
    if not isinstance(message, dict):
        return None

    # Telegram имеет три формата голоса:
    # - voice — голосовое сообщение (ogg/opus, всегда)
    # - audio — аудио-файл (mp3/m4a)
    # - video_note — кружок (video с audio)
    voice_obj = (
        message.get("voice")
        or message.get("audio")
        or message.get("video_note")
    )
    if not voice_obj or not isinstance(voice_obj, dict):
        return None

    file_id = voice_obj.get("file_id")
    if not file_id:
        return None

    # Проверка размера — Telegram Bot API даёт ≤20MB
    file_size = voice_obj.get("file_size") or 0
    if file_size > 20 * 1024 * 1024:
        logger.warning(
            "voice handler: file_size %d exceeds 20MB Bot API limit", file_size,
        )
        return None

    downloaded = await _download_telegram_file(bot_token, file_id)
    if downloaded is None:
        return None
    audio_bytes, mime_type = downloaded

    text = await _transcribe_via_gemini(audio_bytes, mime_type)
    if text:
        logger.info(
            "voice handler: transcribed %d bytes (%s) → %d chars",
            len(audio_bytes), mime_type, len(text),
        )
    return text


__all__ = ["transcribe_telegram_voice"]
