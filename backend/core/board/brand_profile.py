# -*- coding: utf-8 -*-
"""Фирменный профиль тенанта для визуальных отчётов (Visual Reports §14).

Палитра/стиль/логотип/запрещённые и предпочтительные метафоры — на пользователя
(тенант). Применяется к промпту image-модели, чтобы отчёты компании были в едином
узнаваемом стиле. Файловое хранилище (как handoff_store): один JSON на user_id.
Всё опционально и аддитивно: нет профиля → поведение прежнее.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Поля профиля (все опциональны). Списки — короткие.
_FIELDS = ("name", "palette", "illustration_style", "tone", "logo",
           "forbidden_metaphors", "preferred_metaphors")


def _path(user_id: str) -> Optional[str]:
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        d = _DATA_ROOT / "brand_profiles"
        d.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")[:64]
        if not safe:
            return None
        return str(d / f"{safe}.json")
    except Exception:
        logger.debug("brand path failed", exc_info=True)
        return None


def _coerce(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Нормализовать вход: строки — строки, палитра/метафоры — списки строк."""
    out: Dict[str, Any] = {}
    for k in ("name", "illustration_style", "tone", "logo"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:200]
    for k in ("palette", "forbidden_metaphors", "preferred_metaphors"):
        v = raw.get(k)
        if isinstance(v, list):
            items = [str(x).strip()[:60] for x in v if str(x).strip()][:12]
            if items:
                out[k] = items
    return out


def get_brand(user_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Профиль тенанта или None. Never-raise."""
    if not user_id:
        return None
    p = _path(user_id)
    if not p or not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data else None
    except Exception:
        logger.debug("brand read failed", exc_info=True)
        return None


def set_brand(user_id: str, brand: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Сохранить (перезаписать) профиль. Пустой → удалить. Возврат — сохранённое."""
    p = _path(user_id)
    if not p:
        return None
    coerced = _coerce(brand or {})
    try:
        if not coerced:
            if os.path.isfile(p):
                os.remove(p)
            return {}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(coerced, f, ensure_ascii=False, indent=2)
        return coerced
    except Exception:
        logger.warning("brand write failed", exc_info=True)
        return None


def brand_prompt_block(brand: Optional[Dict[str, Any]], lang_ru: bool = True) -> str:
    """Директива фирменного стиля для промпта image-модели. Пусто → "".

    Мягкий оверлей поверх пресета: задаёт основные цвета, стиль иллюстраций, тон,
    логотип и запреты/предпочтения по метафорам, не ломая композицию формата."""
    if not isinstance(brand, dict) or not brand:
        return ""
    parts_ru, parts_en = [], []
    pal = brand.get("palette")
    if isinstance(pal, list) and pal:
        s = ", ".join(str(x) for x in pal[:8])
        parts_ru.append(f"основные фирменные цвета: {s}")
        parts_en.append(f"primary brand colors: {s}")
    st = str(brand.get("illustration_style") or "").strip()
    if st:
        parts_ru.append(f"стиль иллюстраций: {st}")
        parts_en.append(f"illustration style: {st}")
    tone = str(brand.get("tone") or "").strip()
    if tone:
        parts_ru.append(f"тон: {tone}")
        parts_en.append(f"tone: {tone}")
    logo = str(brand.get("logo") or "").strip()
    if logo:
        parts_ru.append(f"деликатно интегрируй логотип ({logo})")
        parts_en.append(f"tastefully integrate the logo ({logo})")
    forb = brand.get("forbidden_metaphors")
    if isinstance(forb, list) and forb:
        s = ", ".join(str(x) for x in forb[:8])
        parts_ru.append(f"ИЗБЕГАЙ образов: {s}")
        parts_en.append(f"AVOID imagery: {s}")
    pref = brand.get("preferred_metaphors")
    if isinstance(pref, list) and pref:
        s = ", ".join(str(x) for x in pref[:8])
        parts_ru.append(f"предпочитай образы: {s}")
        parts_en.append(f"prefer imagery: {s}")
    if not parts_ru:
        return ""
    if lang_ru:
        return " ФИРМЕННЫЙ СТИЛЬ (соблюдай): " + "; ".join(parts_ru) + "."
    return " BRAND STYLE (follow): " + "; ".join(parts_en) + "."


__all__ = ["brand_prompt_block", "get_brand", "set_brand"]
