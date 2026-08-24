# -*- coding: utf-8 -*-
"""Файлы фирменного стиля тенанта: логотип и бланки (letterheads).

Профиль (brand_profile.py) хранит ТЕКСТ (палитра/тон/описание). Здесь — сами
ФАЙЛЫ: логотип-картинка и бланки, которые надо загрузить и сохранить в аккаунте.
Логотип позже используется как референс-изображение в визуальных отчётах; бланки
пока просто хранятся (подстановка в документы — отдельным шагом).

Байты — на диске (_DATA_ROOT/brand_assets/<uid>/), метаданные — index.json.
Never-raise. Всё опционально и аддитивно.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# style_ref — ОБРАЗЕЦ фирменного стиля (пример схемы/иллюстрации компании):
# уходит референс-картинкой в image-модель, чтобы визуальные отчёты выходили
# «в нашем стиле» (VISUAL_REPORTS §9, фаза 3). До 3 образцов, старший вылетает.
KINDS = ("logo", "letterhead", "style_ref")
_MAX_STYLE_REFS = 3
_MAX_BYTES = 8 * 1024 * 1024  # 8 МБ на файл — логотип/бланк с запасом
# Единственный список того, что вообще принимаем. Он же задаёт расширение на
# диске и тип, который потом уходит в data-URI превью: и то и другое клиенту
# задавать нельзя. Логотип/бланк — это картинка или документ, больше сюда
# ничего класть не надо.
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/svg+xml": ".svg", "image/webp": ".webp",
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}


def _dir(user_id: str) -> Optional[str]:
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        safe = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")[:64]
        if not safe:
            return None
        d = _DATA_ROOT / "brand_assets" / safe
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except Exception:
        logger.debug("brand asset dir failed", exc_info=True)
        return None


def _index_path(user_id: str) -> Optional[str]:
    d = _dir(user_id)
    return os.path.join(d, "index.json") if d else None


def _load_index(user_id: str) -> List[Dict[str, Any]]:
    p = _index_path(user_id)
    if not p or not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        logger.debug("brand asset index load failed", exc_info=True)
        return []


def _save_index(user_id: str, items: List[Dict[str, Any]]) -> None:
    p = _index_path(user_id)
    if not p:
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.warning("brand asset index save failed", exc_info=True)


def _decode_data_uri(data: str) -> Tuple[Optional[bytes], str]:
    """base64 (или data:...;base64,) → (bytes, content_type|"")."""
    s = (data or "").strip()
    ctype = ""
    if s.startswith("data:"):
        try:
            head, b64 = s.split(",", 1)
            ctype = head[5:].split(";")[0].strip()
            s = b64
        except ValueError:
            return None, ""
    try:
        return base64.b64decode(s, validate=False), ctype
    except Exception:
        return None, ""


def save_asset(user_id: str, kind: str, filename: str, data: str,
               content_type: str = "", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Сохранить файл фирменного стиля (data — base64 или data-URI).
    Возврат — метаданные записи (без байтов) или None. Логотип — один: при
    загрузке нового старые логотипы удаляются (актуальный референс)."""
    kind = (kind or "").strip().lower()
    if kind not in KINDS or not user_id:
        return None
    raw, ct_from_uri = _decode_data_uri(data)
    if not raw:
        return None
    if len(raw) > _MAX_BYTES:
        return None
    # Тип и расширение — ТОЛЬКО из нашей таблицы. Раньше и то и другое
    # приходило от клиента: при незнакомом content_type расширение брали из
    # присланного имени файла, так что на диск ложился asset_<id>.php или
    # .html, а сам тип потом уходил в data-URI превью как есть
    # (`data:text/html;base64,…`). Имя подделать было нельзя — оно из uuid,
    # — и эту папку никто не исполняет; но «расширение и тип задаёт
    # загружающий» — ровно тот механизм, которым загрузка аватарки
    # превращается в загрузку скрипта, и дальше хватает одной ошибки в
    # раздаче статики. Незнакомый тип отклоняем: логотип и бланк — это
    # картинка или документ, что-то ещё сюда класть незачем.
    ctype = (content_type or ct_from_uri or "").strip().lower().split(";")[0]
    ext = _EXT.get(ctype)
    if ext is None:
        logger.info("brand asset: тип %r не поддерживается — отказ", ctype[:60])
        return None
    d = _dir(user_id)
    if not d:
        return None
    asset_id = "asset_" + uuid.uuid4().hex[:14]
    try:
        with open(os.path.join(d, asset_id + ext), "wb") as f:
            f.write(raw)
    except Exception:
        logger.warning("brand asset write failed", exc_info=True)
        return None

    items = _load_index(user_id)
    if kind == "logo":  # логотип один — чистим прежние
        for old in [x for x in items if x.get("kind") == "logo"]:
            _remove_file(d, old)
        items = [x for x in items if x.get("kind") != "logo"]
    elif kind == "style_ref":  # образцов стиля до 3 — старейший вылетает
        refs = sorted([x for x in items if x.get("kind") == "style_ref"],
                      key=lambda x: float(x.get("created_at") or 0))
        while len(refs) >= _MAX_STYLE_REFS:
            old = refs.pop(0)
            _remove_file(d, old)
            items = [x for x in items
                     if x.get("asset_id") != old.get("asset_id")]
    rec = {"asset_id": asset_id, "kind": kind,
           "filename": (filename or (asset_id + ext))[:200],
           "content_type": ctype, "ext": ext, "size": len(raw),
           "created_at": float(now if now is not None else time.time())}
    items.append(rec)
    _save_index(user_id, items)
    return rec


def _remove_file(d: str, rec: Dict[str, Any]) -> None:
    try:
        p = os.path.join(d, str(rec.get("asset_id")) + str(rec.get("ext") or ""))
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        logger.debug("brand asset file remove failed", exc_info=True)


def list_assets(user_id: str) -> List[Dict[str, Any]]:
    """Метаданные всех файлов фирменного стиля (без байтов)."""
    return [dict(x) for x in _load_index(user_id)]


def get_asset_bytes(user_id: str, asset_id: str) -> Optional[Tuple[bytes, str, str]]:
    """(bytes, content_type, filename) по asset_id, иначе None."""
    d = _dir(user_id)
    if not d:
        return None
    for rec in _load_index(user_id):
        if str(rec.get("asset_id")) == str(asset_id):
            p = os.path.join(d, str(asset_id) + str(rec.get("ext") or ""))
            try:
                with open(p, "rb") as f:
                    return f.read(), str(rec.get("content_type") or ""), str(rec.get("filename") or "")
            except Exception:
                return None
    return None


def get_asset_data_uri(user_id: str, asset_id: str) -> Optional[str]:
    """data:<ctype>;base64,<...> по asset_id (для превью на фронте)."""
    got = get_asset_bytes(user_id, asset_id)
    if not got:
        return None
    raw, ctype, _ = got
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{ctype or 'application/octet-stream'};base64,{b64}"


def get_logo_bytes(user_id: str) -> Optional[Tuple[bytes, str]]:
    """(bytes, content_type) актуального логотипа (для референс-картинки), иначе None."""
    logos = [x for x in _load_index(user_id) if x.get("kind") == "logo"]
    if not logos:
        return None
    logos.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
    got = get_asset_bytes(user_id, str(logos[0].get("asset_id")))
    if not got:
        return None
    return got[0], got[1]


def get_style_ref_bytes(user_id: str, limit: int = 2) -> List[Tuple[bytes, str]]:
    """[(bytes, content_type)] свежих ОБРАЗЦОВ фирменного стиля (только
    image/* — SVG/PDF image-модели референсом не принимают). Пусто — []."""
    refs = [x for x in _load_index(user_id)
            if x.get("kind") == "style_ref"
            and str(x.get("content_type") or "").startswith("image/")
            and "svg" not in str(x.get("content_type") or "")]
    refs.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
    out: List[Tuple[bytes, str]] = []
    for r in refs[:max(1, int(limit))]:
        got = get_asset_bytes(user_id, str(r.get("asset_id")))
        if got:
            out.append((got[0], got[1]))
    return out


def get_letterhead_image_bytes(user_id: str) -> Optional[Tuple[bytes, str]]:
    """(bytes, content_type) свежего бланка-КАРТИНКИ (для шапки DOCX), иначе None.
    Только image/* — PDF/DOCX-бланки для подстановки картинкой не годятся."""
    letters = [x for x in _load_index(user_id)
               if x.get("kind") == "letterhead"
               and str(x.get("content_type") or "").startswith("image/")]
    if not letters:
        return None
    letters.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
    got = get_asset_bytes(user_id, str(letters[0].get("asset_id")))
    if not got:
        return None
    return got[0], got[1]


def delete_asset(user_id: str, asset_id: str) -> bool:
    d = _dir(user_id)
    if not d:
        return False
    items = _load_index(user_id)
    keep, removed = [], False
    for rec in items:
        if str(rec.get("asset_id")) == str(asset_id):
            _remove_file(d, rec)
            removed = True
        else:
            keep.append(rec)
    if removed:
        _save_index(user_id, keep)
    return removed


__all__ = ["KINDS", "delete_asset", "get_asset_bytes", "get_asset_data_uri",
           "get_letterhead_image_bytes", "get_logo_bytes", "list_assets", "save_asset"]
