# -*- coding: utf-8 -*-
"""Серверная генерация изображений для процесс-досок.

Закрывает формат «процесс увидел событие → достал данные → КАРТИНКА →
доставка в Telegram»: движок процессов исполняет картиночный шаг сам, без
браузера (клиентский креатив-режим остаётся как был).

Провайдер выбирается по id модели с карточки (см. _MODEL_PROVIDER): Gemini
(новый google.genai SDK → старый как фолбэк), OpenAI (gpt-image-2/1, dall-e-3),
BYO — Ideogram / Recraft / Replicate-FLUX. При сбое выбранного — честный откат
на платформенные gemini/openai.

Возвращает PNG-байты; сохранение/доставка — забота вызывающего.
Never-raise: {"png": None, "error": "..."} при любом сбое.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_OPENAI_URL = "https://api.openai.com/v1/images/generations"
_TIMEOUT = 120.0


# Каталог: id модели с карточки узла → провайдер. Новый провайдер/модель =
# одна строка тут + вызыватель ниже. Неизвестная модель → провайдер по префиксу.
_MODEL_PROVIDER: Dict[str, str] = {
    # Gemini image («nano banana»): 3.x — новое поколение, 2.5 — прошлое.
    "gemini-3-pro-image": "gemini",
    "gemini-3.1-flash-image": "gemini",
    "gemini-3.1-flash-lite-image": "gemini",
    "gemini-2.5-flash-image": "gemini",
    "nano-banana": "gemini",
    # OpenAI Images: gpt-image-2 / gpt-image-1 (size), dall-e-3.
    "gpt-image-2": "openai",
    "gpt-image-1": "openai",
    "dall-e-3": "openai",
    # BYO-провайдеры (ключ из env/Интеграций): FLUX (Replicate), Ideogram, Recraft.
    "flux-1.1-pro": "replicate",
    "ideogram-v3": "ideogram",
    "recraft-v3": "recraft",
    # Seedream 5 (ByteDance): флагман по тексту/дизайну + правка/мульти-референс.
    "seedream-5-pro": "seedream",
    "seedream-5-lite": "seedream",
    "seedream-5": "seedream",
    # Reve 2.1 (Reve AI, через fal.ai): layout-first 4K, силён в тексте на картинке.
    "reve-2.1": "reve",
    "reve-2": "reve",
    "reve": "reve",
}
# Дефолтная модель Gemini, если с карточки прилетел общий «nano-banana»/«gemini»:
# новое поколение, а не прошлое. Реальный id всё равно подбирается по цепочке.
_GEMINI_DEFAULT_CHAIN = ("gemini-3.1-flash-image", "gemini-2.5-flash-image",
                         "gemini-2.0-flash-preview-image-generation")


def _provider_of(model: str) -> str:
    m = (model or "").strip()
    if m in _MODEL_PROVIDER:
        return _MODEL_PROVIDER[m]
    if m.startswith("gpt-image") or m.startswith("dall-e"):
        return "openai"
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith("flux"):
        return "replicate"
    if m.startswith("seedream"):
        return "seedream"
    if m.startswith("reve"):
        return "reve"
    return "gemini"


async def _openai_png(prompt: str, api_key: str, model: str) -> Optional[bytes]:
    import httpx
    payload: Dict[str, Any] = {"model": model, "prompt": prompt[:3800], "n": 1}
    if model.startswith("dall-e"):
        # Широкий кадр (16:9-подобный) — отчёт-инфографика не должна кропаться в квадрат.
        payload.update({"size": "1792x1024", "response_format": "b64_json"})
    else:  # gpt-image-1 / gpt-image-2 — Images API: широкий размер через size
        # (resolution/aspect_ratio API не принимает — «Unknown parameter»).
        payload["size"] = "1536x1024"
    # gpt-image рисует широкий кадр 2-4 минуты — общий 120s-таймаут рвал запрос
    # ПУСТЫМ ReadTimeout (лог «openai/gpt-image-2 failed:» без причины), и
    # выбранная пользователем модель молча подменялась фолбэком.
    timeout = httpx.Timeout(_TIMEOUT if model.startswith("dall-e") else 300.0,
                            connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            _OPENAI_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload)
        if r.status_code != 200:
            logger.info("image_gen: openai %s -> %s %s",
                        model, r.status_code, r.text[:200])
            raise RuntimeError(f"OpenAI HTTP {r.status_code}: {r.text[:160]}")
        data = r.json()
        b64 = ((data.get("data") or [{}])[0]).get("b64_json")
        return base64.b64decode(b64) if b64 else None


def _extract_inline_png(resp: Any) -> Optional[bytes]:
    """Достать image-байты из ответа Gemini (одинаковая форма у нового и старого
    SDK): candidates[].content.parts[].inline_data.data."""
    for cand in getattr(resp, "candidates", []) or []:
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                return data if isinstance(data, bytes) else base64.b64decode(data)
    return None


def _gemini_new_sdk(prompt: str, model_name: str, key: str,
                    ref_images: Optional[List[bytes]] = None) -> Optional[bytes]:
    """Новый google.genai SDK (актуальный, поддерживает image-модели 3.x).

    ref_images — референс-картинки (напр., логотип): модель ориентируется на них.
    Пусто → contents ровно как раньше (поведение без референсов не меняется)."""
    from google import genai
    client = genai.Client(api_key=key)
    contents: Any = prompt[:3800]
    if ref_images:
        # Референсы + текст. Если типы/парты недоступны — тихо откатываемся к
        # чистому промпту (генерация не должна падать из-за референса).
        try:
            from google.genai import types
            parts: List[Any] = [types.Part.from_bytes(data=img, mime_type="image/png")
                                for img in ref_images if img]
            if parts:
                contents = [prompt[:3800], *parts]
        except Exception:
            logger.debug("gemini ref-images skipped", exc_info=True)
    kwargs: Dict[str, Any] = {"model": model_name, "contents": contents}
    try:  # для image-моделей просим модальность IMAGE, если типы доступны
        from google.genai import types
        kwargs["config"] = types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"])
    except Exception:
        pass
    resp = client.models.generate_content(**kwargs)
    return _extract_inline_png(resp)


def _gemini_old_sdk(prompt: str, model_name: str) -> Optional[bytes]:
    """Устаревший google.generativeai — запасной путь, если новый SDK не смог."""
    import google.generativeai as genai
    model = genai.GenerativeModel(model_name)
    resp = model.generate_content(prompt[:3800])
    return _extract_inline_png(resp)


def _gemini_png_sync(prompt: str, preferred_model: str = "",
                     ref_images: Optional[List[bytes]] = None) -> Optional[bytes]:
    """Gemini image-модель. Пробуем выбранную модель, затем цепочку известных
    (новое поколение → прошлое). На каждой — сперва НОВЫЙ SDK (умеет 3.x-image),
    при сбое — старый. Модель не найдена → молча к следующей.
    ref_images (логотип и т.п.) поддерживает только новый SDK."""
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    chain = []
    if preferred_model and preferred_model not in ("nano-banana",):
        chain.append(preferred_model)
    chain += [m for m in _GEMINI_DEFAULT_CHAIN if m not in chain]
    for model_name in chain:
        if key:
            try:
                png = _gemini_new_sdk(prompt, model_name, key, ref_images=ref_images)
                if png:
                    return png
            except Exception as e:
                logger.debug("image_gen: gemini(new) %s skipped: %s", model_name, e)
        try:
            png = _gemini_old_sdk(prompt, model_name)
            if png:
                return png
        except Exception as e:
            logger.debug("image_gen: gemini(old) %s skipped: %s", model_name, e)
    return None


async def _ideogram_png(prompt: str, api_key: str) -> Optional[bytes]:
    """Ideogram v3 (сильна в тексте/логотипах). Синхронный ответ с URL картинки."""
    import httpx
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            "https://api.ideogram.ai/v1/ideogram-v3/generate",
            headers={"Api-Key": api_key},
            json={"prompt": prompt[:3800], "rendering_speed": "DEFAULT",
                  "aspect_ratio": "16x9", "style_type": "AUTO"})
        if r.status_code != 200:
            logger.info("image_gen: ideogram -> %s %s", r.status_code, r.text[:200])
            return None
        url = (((r.json().get("data") or [{}])[0]) or {}).get("url")
        if not url:
            return None
        img = await client.get(url)
        return img.content if img.status_code == 200 else None


async def _recraft_png(prompt: str, api_key: str) -> Optional[bytes]:
    """Recraft V3: images/generations, ответ — URL."""
    import httpx
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            "https://external.api.recraft.ai/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"prompt": prompt[:3800], "model": "recraftv3",
                  "size": "1707x1024"})
        if r.status_code != 200:
            logger.info("image_gen: recraft -> %s %s", r.status_code, r.text[:200])
            return None
        url = (((r.json().get("data") or [{}])[0]) or {}).get("url")
        if not url:
            return None
        img = await client.get(url)
        return img.content if img.status_code == 200 else None


async def _replicate_run(model: str, api_key: str,
                         input_payload: Dict[str, Any]) -> Optional[bytes]:
    """Общий раннер Replicate: create prediction (Prefer:wait) → опрос → URL →
    байты. Используют и FLUX, и Seedream. Never-raise наружу — None при сбое."""
    import asyncio

    import httpx
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"https://api.replicate.com/v1/models/{model}/predictions",
            headers={"Authorization": f"Bearer {api_key}", "Prefer": "wait"},
            json={"input": input_payload})
        if r.status_code not in (200, 201):
            logger.info("image_gen: replicate %s -> %s %s",
                        model, r.status_code, r.text[:200])
            return None
        body = r.json()
        # Prefer:wait часто отдаёт готовый output сразу; иначе — опрашиваем.
        for _ in range(30):
            status = body.get("status")
            if status == "succeeded":
                out = body.get("output")
                url = out[0] if isinstance(out, list) and out else (out if isinstance(out, str) else None)
                if not url:
                    return None
                img = await client.get(url)
                return img.content if img.status_code == 200 else None
            if status in ("failed", "canceled"):
                logger.info("image_gen: replicate %s status=%s err=%s",
                            model, status, str(body.get("error"))[:200])
                return None
            get_url = (body.get("urls") or {}).get("get")
            if not get_url:
                return None
            await asyncio.sleep(2)
            pr = await client.get(get_url, headers={"Authorization": f"Bearer {api_key}"})
            if pr.status_code != 200:
                return None
            body = pr.json()
    return None


async def _replicate_png(prompt: str, api_key: str,
                         model: str = "black-forest-labs/flux-1.1-pro") -> Optional[bytes]:
    """Replicate FLUX 1.1 Pro: text-to-image."""
    return await _replicate_run(model, api_key, {
        "prompt": prompt[:3800], "aspect_ratio": "16:9",
        "output_format": "png", "output_quality": 90})


# Seedream 5 (ByteDance) через Replicate: text-to-image + правка/мульти-референс
# через image_input (data-URI). Slug'и Replicate: pro (флагман) → lite (fallback).
_SEEDREAM_REPLICATE = {
    "seedream-5-pro": ("bytedance/seedream-5-pro", "bytedance/seedream-5-lite"),
    "seedream-5-lite": ("bytedance/seedream-5-lite",),
    "seedream-5": ("bytedance/seedream-5-lite",),
    "seedream": ("bytedance/seedream-5-lite",),
}


async def _seedream_png(prompt: str, api_key: str, model: str,
                        ref_images: Optional[List[bytes]] = None) -> Optional[bytes]:
    """Seedream 5 через Replicate. Доп-функции: если поданы ref_images —
    это правка/мульти-референс (image_input, до 14 картинок как data-URI).
    Пробуем pro-slug, затем lite (если pro недоступен на аккаунте)."""
    inp: Dict[str, Any] = {"prompt": prompt[:3800], "size": "2K", "aspect_ratio": "16:9"}
    if ref_images:
        uris: List[str] = []
        for img in ref_images[:14]:
            if img:
                uris.append("data:image/png;base64," + base64.b64encode(img).decode("ascii"))
        if uris:
            inp["image_input"] = uris
    slugs = _SEEDREAM_REPLICATE.get(model, _SEEDREAM_REPLICATE["seedream-5"])
    for slug in slugs:
        png = await _replicate_run(slug, api_key, inp)
        if png:
            return png
    return None


async def _fal_run(endpoint: str, api_key: str,
                   input_payload: Dict[str, Any]) -> Optional[bytes]:
    """Синхронный вызов fal.ai (fal.run). sync_mode=true → картинка приходит
    data-URI прямо в images[0].url. Иначе — URL, докачиваем. Never-raise → None."""
    import httpx
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"https://fal.run/{endpoint}",
            headers={"Authorization": f"Key {api_key}", "Content-Type": "application/json"},
            json=input_payload)
        if r.status_code != 200:
            logger.info("image_gen: fal %s -> %s %s", endpoint, r.status_code, r.text[:200])
            return None
        imgs = (r.json().get("images") or [])
        url = (imgs[0].get("url") if imgs and isinstance(imgs[0], dict) else None)
        if not url:
            return None
        if url.startswith("data:"):
            b64 = url.split(",", 1)[1] if "," in url else ""
            try:
                return base64.b64decode(b64)
            except Exception:
                return None
        img = await client.get(url)
        return img.content if img.status_code == 200 else None


async def _reve_png(prompt: str, api_key: str,
                    ref_images: Optional[List[bytes]] = None) -> Optional[bytes]:
    """Reve 2.1 (Reve AI) через fal.ai: layout-first, 4K, силён в тексте на
    картинке. Доп-функции: с ref_images — мульти-референс/правка (fal-ai/reve/
    remix, image_urls 1–6); без — text-to-image."""
    if ref_images:
        uris: List[str] = []
        for img in ref_images[:6]:
            if img:
                uris.append("data:image/png;base64," + base64.b64encode(img).decode("ascii"))
        if uris:
            return await _fal_run("fal-ai/reve/remix", api_key, {
                "prompt": prompt[:3800], "image_urls": uris,
                "aspect_ratio": "16:9", "num_images": 1, "sync_mode": True})
    return await _fal_run("fal-ai/reve/text-to-image", api_key, {
        "prompt": prompt[:3800], "aspect_ratio": "16:9",
        "num_images": 1, "sync_mode": True})


async def _byo_key(user_id: Optional[str], integ: str, *env: str) -> Optional[str]:
    """BYO-ключ провайдера: сперва env платформы, затем ключ пользователя из
    Интеграций. None — если нигде нет (провайдер тогда пропускается)."""
    for e in env:
        if os.environ.get(e):
            return os.environ[e]
    if user_id:
        try:
            from backend.api.routes.integrations import get_user_integration_keys
            keys = await get_user_integration_keys(user_id, integ)
            return (keys or {}).get("api_key") or None
        except Exception:
            logger.debug("image_gen: BYO key lookup failed (%s)", integ, exc_info=True)
    return None


async def generate_image_png(prompt: str,
                             user_id: Optional[str] = None,
                             preferred: Optional[str] = None,
                             ref_images: Optional[List[bytes]] = None) -> Dict[str, Any]:
    """PNG по промпту. {"png": bytes|None, "provider": str, "error": str|None}.

    preferred — id выбранной на карточке модели: gemini-3.1-flash-image /
    gemini-3-pro-image / gemini-3.1-flash-lite-image / gpt-image-2 / gpt-image-1 /
    dall-e-3 / flux-1.1-pro / ideogram-v3 / recraft-v3 (плюс legacy «gemini»,
    «openai:<model>», «nano-banana»). Пробуем выбранного провайдера; при неудаче —
    честный фолбэк на платформенные gemini/openai (BYO-провайдеры не всегда есть).

    ref_images — референс-картинки (напр., логотип бренда); учитывает пока только
    Gemini (новый SDK). Пусто → генерация ровно как раньше."""
    prompt = (prompt or "").strip()
    if len(prompt) < 8:
        return {"png": None, "provider": "", "error": "пустой промпт изображения"}

    # Нормализуем preferred → (provider, model_id).
    sel = (preferred or "").strip()
    if sel.startswith("openai:"):
        sel_provider, sel_model = "openai", (sel.split(":", 1)[1].strip() or "gpt-image-1")
    elif sel.startswith("gemini:"):
        sel_provider, sel_model = "gemini", (sel.split(":", 1)[1].strip() or "nano-banana")
    elif sel in ("gemini", "", "nano-banana"):
        sel_provider, sel_model = "gemini", "nano-banana"
    elif sel == "openai":
        sel_provider, sel_model = "openai", "gpt-image-1"
    else:
        sel_provider, sel_model = _provider_of(sel), sel

    # OpenAI: env платформы ИЛИ ключ пользователя из «Интеграций» (раньше —
    # только env: BYO-ключ OpenAI молча игнорировался, выбранная модель
    # «не работала» без объяснения).
    openai_key = ((os.environ.get("OPENAI_API_KEY") or "").strip()
                  or await _byo_key(user_id, "openai") or "")
    has_gemini = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    # Причины неудач по попыткам — для честного «почему сработал фолбэк».
    attempt_errors: Dict[str, str] = {}

    async def _run(provider: str, model: str) -> Optional[Dict[str, Any]]:
        akey = f"{provider}/{model or 'auto'}"
        try:
            if provider == "openai":
                if not openai_key:
                    attempt_errors[akey] = "нет ключа OpenAI (env/Интеграции)"
                    return None
                png = await _openai_png(prompt, openai_key, model or "gpt-image-1")
                return {"png": png, "provider": f"openai/{model}"} if png else None
            if provider == "gemini":
                if not has_gemini:
                    return None
                import asyncio
                png = await asyncio.to_thread(_gemini_png_sync, prompt, model, ref_images)
                return {"png": png, "provider": f"gemini/{model or 'auto'}"} if png else None
            if provider == "ideogram":
                key = await _byo_key(user_id, "ideogram", "IDEOGRAM_API_KEY")
                if not key:
                    return None
                png = await _ideogram_png(prompt, key)
                return {"png": png, "provider": "ideogram"} if png else None
            if provider == "recraft":
                key = await _byo_key(user_id, "recraft", "RECRAFT_API_KEY")
                if not key:
                    return None
                png = await _recraft_png(prompt, key)
                return {"png": png, "provider": "recraft"} if png else None
            if provider == "replicate":
                key = await _byo_key(user_id, "replicate", "REPLICATE_API_TOKEN")
                if not key:
                    return None
                png = await _replicate_png(prompt, key)
                return {"png": png, "provider": "replicate/flux-1.1-pro"} if png else None
            if provider == "seedream":
                # Seedream 5 идёт через Replicate (ключ REPLICATE_API_TOKEN или
                # из «Интеграций»). Отдельный SEEDREAM_API_KEY тоже принимаем как
                # replicate-токен (BYO). ref_images → правка/мульти-референс.
                key = await _byo_key(user_id, "replicate",
                                     "REPLICATE_API_TOKEN", "SEEDREAM_API_KEY")
                if not key:
                    return None
                png = await _seedream_png(prompt, key, model or "seedream-5",
                                          ref_images=ref_images)
                return {"png": png, "provider": f"seedream/{model or 'seedream-5'}"} if png else None
            if provider == "reve":
                # Reve 2.1 через fal.ai (ключ FAL_KEY или REVE_API_KEY / «Интеграции»).
                key = await _byo_key(user_id, "fal", "FAL_KEY", "REVE_API_KEY")
                if not key:
                    return None
                png = await _reve_png(prompt, key, ref_images=ref_images)
                return {"png": png, "provider": f"reve/{model or 'reve-2.1'}"} if png else None
        except Exception as e:
            # repr: у таймаутов httpx str(e) ПУСТОЙ — лог «failed: » ничего
            # не объяснял и проблему было не диагностировать.
            reason = f"{type(e).__name__}: {e}".rstrip(": ").strip()
            attempt_errors[akey] = reason
            logger.info("image_gen: %s/%s failed: %s", provider, model, reason)
        return None

    # Порядок: выбранный провайдер → платформенные фолбэки (openai, gemini),
    # без повторов выбранного. BYO-провайдер недоступен/упал → всегда есть
    # платформенная страховка, поведение прежних досок не ломается.
    tried = {(sel_provider, sel_model)}
    attempts = [(sel_provider, sel_model)]
    for fb in (("openai", "gpt-image-1"), ("gemini", "nano-banana")):
        if fb not in tried:
            attempts.append(fb); tried.add(fb)

    sel_key = f"{sel_provider}/{sel_model or 'auto'}"
    for provider, model in attempts:
        res = await _run(provider, model)
        akey = f"{provider}/{model or 'auto'}"
        if res and res.get("png"):
            res["error"] = None
            # Честность фолбэка: если это НЕ выбранная модель — приложить
            # причину, почему выбранная не сработала (узел покажет её в note).
            if akey != sel_key:
                res["preferred_error"] = attempt_errors.get(
                    sel_key, "нет ключа или провайдер не вернул изображение")
            return res
        attempt_errors.setdefault(
            akey, "не вернул изображение (нет ключа или пустой ответ)")

    detail = "; ".join(f"{k}: {v}" for k, v in attempt_errors.items())
    return {"png": None, "provider": "",
            "error": ("генерация изображений недоступна: нужен ключ выбранного "
                      "провайдера (OpenAI/Gemini/Ideogram/Recraft/Replicate)"
                      + (f". Детали: {detail}" if detail else ""))}


def save_board_image(png: bytes, user_id: Optional[str]) -> str:
    """Сохранить PNG в data/board_images/<uid>/ и вернуть путь."""
    import uuid

    from backend.core.store.tenant_paths import _DATA_ROOT
    safe_uid = "".join(c for c in str(user_id or "anon")
                       if c.isalnum() or c == "-")[:40] or "anon"
    d = _DATA_ROOT / "board_images" / safe_uid
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uuid.uuid4().hex[:16]}.png"
    path.write_bytes(png)
    return str(path)
