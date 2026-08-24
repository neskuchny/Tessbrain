# -*- coding: utf-8 -*-
"""
API «Документы по встрече»: заполнение шаблонов (Режим A) и сборка (Режим B).

Роуты (под /api/v1):
  POST /meeting-docs/analyze-template  — {template_base64} → список плейсхолдеров
  POST /meeting-docs/fill-template     — Режим A: заполнить шаблон по встрече
                                          → {docx_base64, confidence, filename}
  POST /meeting-docs/compose           — Режим B: собрать содержимое (markdown)
  POST /meeting-docs/render            — {markdown, format} → файл (docx)
  GET/PUT /meeting-docs/requisites     — реквизиты компании (per-user)

Всё user-scoped; текст встреч грузится по логину.
"""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, Dict, List, Optional

from litestar import Router, delete, get, post, put
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.response import Response

from backend.api.routes.documents import get_user_id
from backend.core.documents import meeting_doc_engine as E
from backend.core.documents import meeting_doc_service as S
from backend.core.documents import meeting_doc_store as TStore
from backend.core.llm.router import get_llm_router
from backend.core.llm.usage_tracker import cost_scope

logger = logging.getLogger(__name__)

_DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _auth(authorization: Optional[str], user_id: Optional[str]) -> str:
    uid = get_user_id(authorization, user_id)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    return uid


def _decode_b64(s: str) -> bytes:
    try:
        if "," in s and s.strip().startswith("data:"):
            s = s.split(",", 1)[1]  # data:...;base64,XXXX
        return base64.b64decode(s)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"bad base64: {e}")


async def _load_meeting_text(user_id: str, meeting_ids: List[str]) -> str:
    """Собрать текст встреч (транскрипт/саммари) по id, СТРОГО по владельцу."""
    from backend.db.supabase_client import SupabaseClient
    sb = SupabaseClient()
    parts: List[str] = []
    for mid in meeting_ids[:5]:
        try:
            rows = await sb._request(
                "GET", "/rest/v1/meetings",
                params={"id": f"eq.{mid}", "user_id": f"eq.{user_id}",
                        "select": "title,transcription_text,summary", "limit": "1"})
            if not rows:
                # запасной путь: по meeting_id
                rows = await sb._request(
                    "GET", "/rest/v1/meetings",
                    params={"meeting_id": f"eq.{mid}", "user_id": f"eq.{user_id}",
                            "select": "title,transcription_text,summary", "limit": "1"})
            if rows:
                m = rows[0]
                body = m.get("transcription_text") or m.get("summary") or ""
                if body:
                    parts.append(f"[Встреча: {m.get('title', '')}]\n{body}")
        except Exception as e:
            logger.warning("meeting load failed %s: %s", mid, e)
    return "\n\n".join(parts)


# ── Реквизиты компании (per-user, через user_integrations, без миграций) ────

_REQ_PROVIDER = "document_requisites"


async def _get_requisites(user_id: str) -> Dict[str, str]:
    from backend.db.supabase_client import SupabaseClient
    try:
        rows = await SupabaseClient()._request(
            "GET", "/rest/v1/user_integrations",
            params={"user_id": f"eq.{user_id}", "provider": f"eq.{_REQ_PROVIDER}",
                    "select": "meta", "limit": "1"})
        if rows and isinstance(rows[0].get("meta"), dict):
            return {str(k): str(v) for k, v in rows[0]["meta"].items() if v is not None}
    except Exception as e:
        logger.debug("requisites load failed: %s", e)
    return {}


# ── Роуты ───────────────────────────────────────────────────────────────────

@post("/meeting-docs/analyze-template")
async def analyze_template(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Список плейсхолдеров {{поле}} из загруженного DOCX-шаблона."""
    _auth(authorization, user_id)
    tpl = _decode_b64(str(data.get("template_base64") or ""))
    try:
        placeholders = E.extract_placeholders(tpl)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать шаблон DOCX: {e}")
    company = [p for p in placeholders if S._is_company_slot(p)]
    return {"placeholders": placeholders, "company_slots": company,
            "meeting_slots": [p for p in placeholders if p not in company]}


@post("/meeting-docs/fill-template")
async def fill_template(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Режим A: заполнить DOCX-шаблон по встрече, с пометками уверенности.

    Body: {template_base64, meeting_ids[], field_hints?, requisites?,
    extra_context?}. Возвращает {docx_base64, filename, confidence, filled}.
    """
    uid = _auth(authorization, user_id)
    # Шаблон: base64 из запроса ИЛИ сохранённый по template_id.
    b64 = str(data.get("template_base64") or "")
    if not b64 and data.get("template_id"):
        saved = await TStore.get_template(uid, str(data["template_id"]))
        if not saved:
            raise HTTPException(status_code=404, detail="Шаблон не найден")
        b64 = saved.get("docx_base64", "")
    tpl = _decode_b64(b64)
    meeting_ids = [str(x) for x in (data.get("meeting_ids") or []) if str(x).strip()]
    if not meeting_ids:
        raise HTTPException(status_code=400, detail="meeting_ids required")

    try:
        placeholders = E.extract_placeholders(tpl)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать шаблон: {e}")
    if not placeholders:
        raise HTTPException(status_code=400, detail="В шаблоне нет плейсхолдеров {{поле}}")

    requisites = {**(await _get_requisites(uid)), **(data.get("requisites") or {})}
    meeting_text = await _load_meeting_text(uid, meeting_ids)

    async with cost_scope(user_id=uid, agent_mode="documents",
                          request_type="meeting_doc_fill", surface="meeting_docs"):
        fields = await S.extract_fields_from_meeting(
            get_llm_router(), placeholders=placeholders, meeting_text=meeting_text,
            requisites=requisites, field_hints=data.get("field_hints") or {},
            extra_context=str(data.get("extra_context") or ""))

    values = E.values_from_fields(fields)

    # Позиции сметы: считаем деньги КОДОМ, размножаем строку таблицы
    # {{items.*}} и добавляем итоговые слоты {{subtotal}}/{{vat}}/{{total}}/…
    table_items = None
    line_items = data.get("line_items") or []
    if line_items:
        money = E.compute_line_items(line_items, vat_rate=float(data.get("vat_rate", 20) or 0))
        table_items = [{
            "name": ln["name"], "unit": ln["unit"],
            "qty": f"{ln['qty']:g}", "price": f"{ln['price']:,.2f}",
            "amount": f"{ln['amount']:,.2f}",
        } for ln in money["lines"]]
        values.setdefault("subtotal", f"{money['subtotal']:,.2f}")
        values.setdefault("vat", f"{money['vat']:,.2f}")
        values.setdefault("total", f"{money['total']:,.2f}")
        values.setdefault("total_in_words", money["total_in_words"])

    docx_bytes = E.fill_template(tpl, values, table_items=table_items)
    company_keys = [p for p in placeholders if S._is_company_slot(p) or p in requisites]
    report = E.build_confidence_report(fields, company_keys=company_keys)
    return {
        "docx_base64": base64.b64encode(docx_bytes).decode("ascii"),
        "filename": "document.docx",
        "confidence": report,
        "filled": len([v for v in values.values() if v]),
        "total_fields": len(placeholders),
    }


@post("/meeting-docs/compose")
async def compose(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Режим B: собрать содержимое документа по встрече (markdown для превью).

    Body: {meeting_ids[], doc_kind (kp|contract|card|free), style_example?,
    custom_prompt?, extra_context?}.
    """
    uid = _auth(authorization, user_id)
    meeting_ids = [str(x) for x in (data.get("meeting_ids") or []) if str(x).strip()]
    if not meeting_ids:
        raise HTTPException(status_code=400, detail="meeting_ids required")
    meeting_text = await _load_meeting_text(uid, meeting_ids)
    if not meeting_text.strip():
        raise HTTPException(status_code=422, detail="У встреч нет транскрипта/саммари")

    async with cost_scope(user_id=uid, agent_mode="documents",
                          request_type="meeting_doc_compose", surface="meeting_docs"):
        markdown = await S.compose_document(
            get_llm_router(), doc_kind=str(data.get("doc_kind") or "free"),
            meeting_text=meeting_text,
            style_example=str(data.get("style_example") or ""),
            custom_prompt=str(data.get("custom_prompt") or ""),
            extra_context=str(data.get("extra_context") or ""),
            requisites=await _get_requisites(uid))
    if not markdown:
        raise HTTPException(status_code=502, detail="LLM недоступен — документ не собран")

    # Деньги — КОДОМ (модель числа не выдумывает): если пришли позиции сметы,
    # считаем и дописываем таблицу с итогом и суммой прописью.
    money = None
    items = data.get("line_items") or []
    if items:
        money = E.compute_line_items(items, vat_rate=float(data.get("vat_rate", 20) or 0))
        markdown = markdown.rstrip() + "\n\n" + _money_markdown(money)
    return {"markdown": markdown, "doc_kind": data.get("doc_kind") or "free", "money": money}


def _money_markdown(m: Dict[str, Any]) -> str:
    """Смета → markdown-таблица с итогом (числа посчитаны кодом)."""
    lines = ["## Стоимость", "", "| Наименование | Кол-во | Цена | Сумма |",
             "| --- | ---: | ---: | ---: |"]
    for ln in m.get("lines", []):
        unit = f" {ln['unit']}" if ln.get("unit") else ""
        lines.append(f"| {ln['name']} | {ln['qty']:g}{unit} | {ln['price']:,.2f} | {ln['amount']:,.2f} |")
    lines.append("")
    lines.append(f"**Итого без НДС:** {m['subtotal']:,.2f} ₽")
    if m.get("vat"):
        lines.append(f"**НДС {m['vat_rate']:g}%:** {m['vat']:,.2f} ₽")
    lines.append(f"**Итого к оплате:** {m['total']:,.2f} ₽")
    lines.append(f"_Сумма прописью: {m['total_in_words']}_")
    return "\n".join(lines)


@post("/meeting-docs/render")
async def render_doc(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Response:
    """{markdown, title?, use_letterhead?, format?} → файл на скачивание.
    format: "docx" (умолч.) или "pdf". Фирменный бланк-картинка (если загружен)
    ставится шапкой (use_letterhead:false — выключить)."""
    uid = _auth(authorization, user_id)
    markdown = str(data.get("markdown") or "")
    if not markdown.strip():
        raise HTTPException(status_code=400, detail="markdown required")
    title = str(data.get("title") or "Документ")
    fmt = str(data.get("format") or "docx").strip().lower()
    letterhead_png = None
    if data.get("use_letterhead", True):
        try:
            from backend.core.board.brand_assets import get_letterhead_image_bytes
            got = get_letterhead_image_bytes(uid or "")
            if got and got[0]:
                letterhead_png = got[0]
        except Exception:
            logger.debug("letterhead load skipped", exc_info=True)

    pdf_fallback = False
    if fmt == "pdf":
        from backend.core.analysis.export import markdown_to_pdf_bytes
        try:
            pdf_bytes = markdown_to_pdf_bytes(markdown, title=title,
                                              letterhead_png=letterhead_png)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"pdf render failed: {e}")
        if pdf_bytes:
            return Response(content=pdf_bytes, media_type="application/pdf",
                            headers={"Content-Disposition": 'attachment; filename="document.pdf"'})
        # PDF-движок не установлен (нужен fpdf2 или LibreOffice) → отдаём DOCX,
        # но НЕ молча: заголовок X-PDF-Fallback подхватывает фронт и предупреждает.
        pdf_fallback = True
        logger.warning("PDF unavailable (нет fpdf2/LibreOffice), falling back to DOCX")

    from backend.core.analysis.export import markdown_to_docx_bytes
    try:
        docx_bytes = markdown_to_docx_bytes(markdown, title=title,
                                            letterhead_png=letterhead_png)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"docx render failed: {e}")
    headers = {"Content-Disposition": 'attachment; filename="document.docx"'}
    if pdf_fallback:
        headers["X-PDF-Fallback"] = "1"  # фронт: «PDF-движок не установлен → DOCX»
    return Response(content=docx_bytes, media_type=_DOCX_CT, headers=headers)


@get("/meeting-docs/requisites")
async def get_requisites_route(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    uid = _auth(authorization, user_id)
    return {"requisites": await _get_requisites(uid)}


@put("/meeting-docs/requisites")
async def put_requisites_route(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Сохранить реквизиты компании (per-user)."""
    uid = _auth(authorization, user_id)
    req = {str(k): str(v) for k, v in (data.get("requisites") or {}).items() if v is not None}
    from backend.db.supabase_client import SupabaseClient
    sb = SupabaseClient()
    try:
        await sb._request("DELETE", "/rest/v1/user_integrations",
                          params={"user_id": f"eq.{uid}", "provider": f"eq.{_REQ_PROVIDER}"})
        await sb._request("POST", "/rest/v1/user_integrations",
                          json_data={"user_id": uid, "provider": _REQ_PROVIDER,
                                     "key_name": "company", "meta": req, "secret_enc": ""})
        return {"saved": True, "count": len(req)}
    except Exception as e:
        logger.warning("requisites save failed: %s", e)
        raise HTTPException(status_code=500, detail="Не удалось сохранить реквизиты")


@post("/meeting-docs/compute-money")
async def compute_money(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Посчитать позиции сметы КОДОМ: {items:[{name,qty,price,unit}], vat_rate}
    → {lines, subtotal, vat, total, total_in_words}. Модель числа не выдумывает."""
    _auth(authorization, user_id)
    items = data.get("items") or []
    vat = float(data.get("vat_rate", 20) or 0)
    return E.compute_line_items(items, vat_rate=vat)


@post("/meeting-docs/templates")
async def save_template_route(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Сохранить именованный DOCX-шаблон. Body: {name, kind?, template_base64}."""
    uid = _auth(authorization, user_id)
    b64 = str(data.get("template_base64") or "")
    tpl = _decode_b64(b64)
    try:
        placeholders = E.extract_placeholders(tpl)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не DOCX или битый шаблон: {e}")
    return await TStore.save_template(
        uid, name=str(data.get("name") or "Шаблон"),
        kind=str(data.get("kind") or "custom"),
        docx_base64=b64, placeholders=placeholders)


@get("/meeting-docs/templates")
async def list_templates_route(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    uid = _auth(authorization, user_id)
    return {"templates": await TStore.list_templates(uid)}


@delete("/meeting-docs/templates/{template_id:str}", status_code=200)
async def delete_template_route(
    template_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    uid = _auth(authorization, user_id)
    ok = await TStore.delete_template(uid, template_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    return {"deleted": template_id}


@get("/meeting-docs/snippets")
async def list_snippets_route(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Заготовки контекста (реюз-блоки): не вписывать одно и то же каждый раз."""
    uid = _auth(authorization, user_id)
    return {"snippets": await TStore.list_snippets(uid)}


@post("/meeting-docs/snippets")
async def add_snippet_route(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    uid = _auth(authorization, user_id)
    name = str(data.get("name") or "Блок").strip()
    text = str(data.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    return await TStore.add_snippet(uid, name, text)


@delete("/meeting-docs/snippets/{snippet_id:str}", status_code=200)
async def delete_snippet_route(
    snippet_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    uid = _auth(authorization, user_id)
    ok = await TStore.delete_snippet(uid, snippet_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Заготовка не найдена")
    return {"deleted": snippet_id}


router = Router(path="", route_handlers=[
    analyze_template, fill_template, compose, render_doc,
    get_requisites_route, put_requisites_route,
    compute_money, save_template_route, list_templates_route, delete_template_route,
    list_snippets_route, add_snippet_route, delete_snippet_route,
])
