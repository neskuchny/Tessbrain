# -*- coding: utf-8 -*-
"""Заполнение документов по шаблону из встреч (MeetFlow-перенос):
шаблоны с {{плейсхолдерами}}, реквизиты компании, деньги считает код,
каждое поле помечено найдено/предположено/не найдено."""
from __future__ import annotations

import logging
from typing import Optional

from litestar import Request, Response, Router, delete, get, post
from litestar.exceptions import HTTPException
from litestar.params import Body, Parameter

logger = logging.getLogger(__name__)


def _content_disposition(filename: str, fallback: str = "document") -> str:
    """Content-Disposition для имён с кириллицей (RFC 5987).

    Litestar кодирует HTTP-заголовки latin-1 → сырая кириллица в filename
    роняла ответ UnicodeEncodeError (падал экспорт .xlsx/.docx с русским
    названием). Отдаём ASCII-fallback в `filename=` и полное UTF-8-имя в
    `filename*=UTF-8''…`; современные браузеры берут второе.
    """
    from urllib.parse import quote
    ascii_name = (filename.encode("ascii", "ignore").decode("ascii")
                  .replace('"', "").replace(";", "").strip())
    if not ascii_name or ascii_name.startswith("."):
        ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
        ascii_name = f"{fallback}.{ext}" if ext else fallback
    return (f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(filename, safe='')}")


def _uid(request: Request, user_id: Optional[str]) -> str:
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _src = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    except Exception:
        uid = user_id or ""
    if not uid:
        raise HTTPException(status_code=401, detail="user_id required")
    return uid


@get("/fill/example-template.docx")
async def example_template_route() -> Response:
    """«Рыба» — скачиваемый пример DOCX-шаблона с плейсхолдерами {{поле}}
    и табличной частью {{items.*}}. Отправная точка для «Заполнить мой
    шаблон». Без аутентификации — статический образец, не данные юзера."""
    from backend.core.documents.fill_engine import build_example_template_docx
    blob = build_example_template_docx()
    return Response(
        content=blob,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"),
        headers={"Content-Disposition":
                 'attachment; filename="example-template.docx"'})


@get("/requisites")
async def get_requisites_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Реквизиты компании (заполняются один раз, подставляются во все
    документы)."""
    from backend.core.documents.fill_engine import get_requisites
    return {"requisites": get_requisites(_uid(request, user_id))}


@post("/requisites")
async def save_requisites_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    from backend.core.documents.fill_engine import save_requisites
    return {"requisites": save_requisites(
        _uid(request, user_id), data.get("requisites") or data)}


@get("/fill-templates")
async def list_templates_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    from backend.core.documents.fill_engine import list_doc_templates
    items = list_doc_templates(_uid(request, user_id))
    return {"templates": items, "total": len(items)}


@post("/fill-templates")
async def save_template_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """{title, body?|docx_b64?, mode, template_id?} — upsert.
    strict: {{плейсхолдеры}} заполняются; flexible: body = эталон стиля;
    docx_b64: фирменный бланк Word — поля заполняются прямо в docx."""
    from backend.core.documents.fill_engine import save_doc_template
    if not (data.get("body") or "").strip() and not data.get("docx_b64"):
        return {"success": False, "error": "нужен body или docx_b64"}
    try:
        rec = save_doc_template(
            _uid(request, user_id), title=data.get("title") or "",
            body=data.get("body") or "", mode=data.get("mode") or "strict",
            template_id=data.get("template_id"),
            docx_b64=data.get("docx_b64"))
    except Exception as e:
        return {"success": False, "error": f"шаблон не сохранён: {e}"}
    return {"success": True, "template": rec}


@delete("/fill-templates/{template_id:str}", status_code=200)
async def delete_template_route(
        request: Request, template_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    from backend.core.documents.fill_engine import delete_doc_template
    return {"deleted": delete_doc_template(_uid(request, user_id), template_id)}


@get("/context-presets")
async def list_presets_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Заготовки контекста (типовая услуга/прайс/условия) — вставляются
    галочками при заполнении, чтобы не набирать одно и то же."""
    from backend.core.documents.fill_engine import list_context_presets
    items = list_context_presets(_uid(request, user_id))
    return {"presets": items, "total": len(items)}


@post("/context-presets")
async def save_preset_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    from backend.core.documents.fill_engine import save_context_preset
    if not (data.get("text") or "").strip():
        return {"success": False, "error": "text обязателен"}
    rec = save_context_preset(
        _uid(request, user_id), title=data.get("title") or "",
        text=data["text"], preset_id=data.get("preset_id"))
    return {"success": True, "preset": rec}


@delete("/context-presets/{preset_id:str}", status_code=200)
async def delete_preset_route(
        request: Request, preset_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    from backend.core.documents.fill_engine import delete_context_preset
    return {"deleted": delete_context_preset(_uid(request, user_id), preset_id)}


@post("/fill")
async def fill_document_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Заполнить документ: {template_id | template_text, meeting_ids[],
    extra_context?, mode?, vat_rate?, vat_included?}. Ответ: document +
    fields (found/assumed/missing) + money (посчитан кодом) + unfilled."""
    uid = _uid(request, user_id)
    from backend.core.documents.fill_engine import (
        fill_document,
        list_doc_templates,
    )
    template_text = data.get("template_text") or ""
    mode = data.get("mode") or ""
    template_docx = None
    if data.get("template_id"):
        tpl = next((t for t in list_doc_templates(uid)
                    if t.get("id") == data["template_id"]), None)
        if not tpl:
            return {"success": False, "error": "шаблон не найден"}
        if tpl.get("kind") == "docx":
            from backend.core.documents.fill_engine import load_template_docx
            template_docx = load_template_docx(uid, tpl["id"])
            if not template_docx:
                return {"success": False, "error": "docx-бланк шаблона потерян"}
        template_text = tpl.get("body") or ""
        mode = mode or tpl.get("mode") or "strict"
    if not template_text.strip() and not template_docx:
        return {"success": False, "error": "нужен template_id или template_text"}
    try:
        extra = data.get("extra_context") or ""
        if data.get("preset_ids"):
            from backend.core.documents.fill_engine import presets_text
            pt = presets_text(uid, list(data["preset_ids"])[:10])
            extra = f"{pt}\n\n{extra}".strip() if pt else extra
        result = await fill_document(
            uid, template_text=template_text,
            meeting_ids=data.get("meeting_ids") or [],
            extra_context=extra,
            mode=mode or "strict",
            vat_rate=float(data.get("vat_rate") or 0),
            vat_included=bool(data.get("vat_included")),
            template_docx=template_docx)
        return {"success": True, **result}
    except HTTPException:
        raise
    except ValueError as e:
        # Понятная причина (напр. «у встреч нет транскрипта») — без тех-префикса.
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("fill_document failed")
        return {"success": False, "error": f"ошибка заполнения: {e}"}


@post("/fill/export")
async def export_filled_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> "Response":
    """Экспорт заполненного документа: {title, document, format, money?}.
    format: docx (Word, python-docx) | xlsx (позиции+суммы, если money) |
    pdf (печатная HTML-страница: браузер сохраняет в PDF без зависимостей
    на сервере — Ctrl+P открывается сам)."""
    from litestar import Response
    _uid(request, user_id)   # auth-гейт; контент приходит телом
    title = (data.get("title") or "Документ").strip()[:80]
    doc_md = data.get("document") or ""
    fmt = (data.get("format") or "docx").lower()
    fname = "".join(c if c.isalnum() or c in " -_" else "_"
                    for c in title).strip() or "document"
    if fmt == "docx":
        from backend.core.analysis.export import markdown_to_docx_bytes
        blob = markdown_to_docx_bytes(doc_md, title=title)
        return Response(
            content=blob,
            media_type=("application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"),
            headers={"Content-Disposition":
                     _content_disposition(f"{fname}.docx")})
    if fmt == "xlsx":
        from backend.core.ontology.xlsx_export import rows_to_xlsx
        money = data.get("money") or {}
        lines = money.get("lines") or []
        if not lines:
            return Response(content={"error": "нет позиций для Excel "
                                              "(money.lines пуст)"},
                            status_code=400)
        cols = ["№", "Наименование", "Кол-во", "Ед.", "Цена", "Сумма"]
        rows = [{"№": i + 1, "Наименование": ln.get("name", ""),
                 "Кол-во": ln.get("qty", ""), "Ед.": ln.get("unit", ""),
                 "Цена": ln.get("price", ""),
                 "Сумма": ln.get("line_total", "")}
                for i, ln in enumerate(lines)]
        rows.append({"№": "", "Наименование": "ИТОГО (с НДС)" if money.get(
            "vat_amount") not in ("0.00", None) else "ИТОГО",
            "Кол-во": "", "Ед.": "", "Цена": "",
            "Сумма": money.get("total", "")})
        blob = rows_to_xlsx(cols, rows, sheet_name=title[:28] or "Позиции")
        return Response(
            content=blob,
            media_type=("application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet"),
            headers={"Content-Disposition":
                     _content_disposition(f"{fname}.xlsx")})
    if fmt == "pdf":
        import html as _html
        body = _html.escape(doc_md)
        page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{_html.escape(title)}</title><style>
body{{font-family:'Times New Roman',serif;font-size:12pt;line-height:1.45;
max-width:19cm;margin:1.5cm auto;color:#111}}
pre{{white-space:pre-wrap;font:inherit;margin:0}}
h1{{font-size:16pt;margin-bottom:12pt}}
@media print{{body{{margin:0}}.noprint{{display:none}}}}
</style></head><body>
<p class="noprint" style="background:#f5f5f5;padding:8px;border-radius:6px">
Сейчас откроется диалог печати — выберите «Сохранить как PDF».</p>
<h1>{_html.escape(title)}</h1><pre>{body}</pre>
<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),300))</script>
</body></html>"""
        return Response(content=page, media_type="text/html")
    return Response(content={"error": f"format {fmt}: docx|xlsx|pdf"},
                    status_code=400)


def _mediaplan_to_markdown(client: str, result: dict) -> str:
    """Медиаплан → markdown (таблица + обоснование + предположения) для
    сохранения в библиотеку результатов."""
    table = result.get("table") or {}
    cols = table.get("columns") or []
    rows = list(table.get("rows") or [])
    if table.get("totals"):
        rows.append(table["totals"])
    lines = [f"# Медиаплан — {client}", ""]
    rationale = str(result.get("rationale") or "").strip()
    if rationale:
        lines += [rationale, ""]
    if cols:
        lines.append("| " + " | ".join(str(c) for c in cols) + " |")
        lines.append("| " + " | ".join("---" for _ in cols) + " |")
        for r in rows:
            lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    assumptions = result.get("assumptions") or []
    if assumptions:
        lines += ["", "## Предположения"] + [f"- {a}" for a in assumptions]
    return "\n".join(lines)


@post("/mediaplan")
async def build_mediaplan_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Медиаплан для клиента: {client, meeting_ids[], preset_ids?,
    extra_context?, total_budget?}. LLM извлекает ставки ИЗ материалов
    и обосновывает сплит, каскад (показы/клики/лиды/CPA) считает код.
    Ответ: table (rows+totals) + rationale + assumptions + rates_missing."""
    uid = _uid(request, user_id)
    from backend.core.documents.mediaplan_engine import build_mediaplan
    try:
        result = await build_mediaplan(
            uid, client_query=(data.get("client") or "").strip(),
            meeting_ids=data.get("meeting_ids") or [],
            extra_context=data.get("extra_context") or "",
            preset_ids=data.get("preset_ids") or [],
            total_budget=data.get("total_budget"))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("build_mediaplan failed")
        return {"success": False, "error": f"ошибка сборки медиаплана: {e}"}
    # Собранный медиаплан → в библиотеку результатов (best-effort, чтобы
    # не терялся: жалоба «ничего не сохраняется»).
    if isinstance(result, dict) and result.get("success"):
        try:
            from backend.core.documents.results_library import save_result_document
            client = (data.get("client") or "").strip() or "клиент"
            await save_result_document(
                uid, title=f"Медиаплан — {client}"[:120],
                content_markdown=_mediaplan_to_markdown(client, result),
                document_type="mediaplan",
                summary=str(result.get("rationale") or "")[:500],
                source_meetings=[str(m) for m in (data.get("meeting_ids") or [])])
        except Exception:
            logger.debug("save mediaplan to library skipped", exc_info=True)
    return result


@post("/mediaplan/export.xlsx")
async def export_mediaplan_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> "Response":
    """Экспорт таблицы медиаплана в Excel: {title?, table:{columns,rows,totals}}."""
    from litestar import Response
    _uid(request, user_id)
    table = data.get("table") or {}
    cols = table.get("columns") or []
    rows = list(table.get("rows") or [])
    if table.get("totals"):
        rows.append(table["totals"])
    if not cols or not rows:
        return Response(content={"error": "пустая таблица"}, status_code=400)
    # производные колонки — живыми формулами (менеджер правит бюджет/ставку —
    # таблица пересчитывается), остальное числами, чтобы Excel суммировал
    try:
        from backend.core.documents.mediaplan_engine import formulas_table
        typed = formulas_table(table)
    except Exception:
        logger.debug("formulas_table failed — статичные числа", exc_info=True)
        typed = []
        for r in rows:
            tr = {}
            for c in cols:
                v = r.get(c, "")
                try:
                    tr[c] = float(v) if v not in ("", None) and c != "Канал" else v
                except (TypeError, ValueError):
                    tr[c] = v
            typed.append(tr)
    from backend.core.ontology.xlsx_export import rows_to_xlsx
    blob = rows_to_xlsx(cols, typed,
                        sheet_name=(data.get("title") or "Медиаплан")[:28])
    fname = "".join(ch if ch.isalnum() or ch in " -_" else "_"
                    for ch in (data.get("title") or "mediaplan")).strip() or "mediaplan"
    return Response(
        content=blob,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition": _content_disposition(f"{fname}.xlsx")})


@post("/mediaplan/export.gsheet")
async def export_mediaplan_gsheet_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Медиаплан → Google Sheets (сервисный аккаунт). Возвращает {url}.
    Не настроен ключ → честная подсказка (GOOGLE_SERVICE_ACCOUNT_JSON)."""
    _uid(request, user_id)
    from backend.core.integrations import google_export as g
    if not g.is_configured():
        return {"success": False, "error": g.config_hint()}
    table = data.get("table") or {}
    cols = table.get("columns") or []
    rows = list(table.get("rows") or [])
    if table.get("totals"):
        rows.append(table["totals"])
    if not cols or not rows:
        return {"success": False, "error": "пустая таблица"}
    # живые формулы: USER_ENTERED интерпретирует "=..." как формулу
    try:
        from backend.core.documents.mediaplan_engine import formulas_table
        rows = formulas_table(table)
    except Exception:
        logger.debug("formulas_table failed — статичные числа", exc_info=True)
    try:
        res = await g.export_table_to_sheets(
            title=data.get("title") or "Медиаплан", columns=cols, rows=rows)
        return {"success": True, **res}
    except Exception as e:
        logger.exception("gsheet export failed")
        return {"success": False, "error": f"Google Sheets: {e}"}


@post("/mediaplan/export.gdoc")
async def export_mediaplan_gdoc_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Обоснование медиаплана → Google Docs. {title, text} → {url}."""
    _uid(request, user_id)
    from backend.core.integrations import google_export as g
    if not g.is_configured():
        return {"success": False, "error": g.config_hint()}
    text = (data.get("text") or "").strip()
    if not text:
        return {"success": False, "error": "пустой текст"}
    try:
        res = await g.export_text_to_doc(
            title=data.get("title") or "Обоснование", text=text)
        return {"success": True, **res}
    except Exception as e:
        logger.exception("gdoc export failed")
        return {"success": False, "error": f"Google Docs: {e}"}


documents_fill_router = Router(
    path="/documents",
    route_handlers=[
        example_template_route,
        get_requisites_route, save_requisites_route,
        list_templates_route, save_template_route, delete_template_route,
        fill_document_route, export_filled_route,
        list_presets_route, save_preset_route, delete_preset_route,
        build_mediaplan_route, export_mediaplan_route,
        export_mediaplan_gsheet_route, export_mediaplan_gdoc_route,
    ],
    tags=["Documents Fill"],
)
