# -*- coding: utf-8 -*-
"""Онтология данных — API «палантир-модуля» (docs/ru/DATA_ONTOLOGY.md).

Endpoints (флаг enable_data_ontology, изоляция per-user):
- POST   /ontology/datasets            — зарегистрировать (content|url)
- GET    /ontology/datasets            — список (онтология + связи, без строк)
- GET    /ontology/datasets/{id}       — полная запись (профиль/онтология/находки)
- POST   /ontology/datasets/{id}/ask   — вопрос цифрами (план → код → оценка)
- POST   /ontology/datasets/{id}/refresh — сходить в источник по URL
- DELETE /ontology/datasets/{id}
"""
from __future__ import annotations

import logging
from typing import Optional

from litestar import Request, Router, delete, get, post
from litestar.exceptions import HTTPException
from litestar.params import Body, Parameter

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
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


def _guard() -> None:
    from backend.core.ontology.dataset_registry import ontology_enabled
    if not ontology_enabled():
        raise HTTPException(
            status_code=403,
            detail="онтология данных выключена (enable_data_ontology=false)")


@post("/datasets")
async def register_dataset_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Подключить таблицу: {title, content?|url?|content_b64?, format?}.
    content_b64 + format='xlsx' — загрузка Excel-файла. Система
    профилирует, понимает к чему относится (контекст компании), строит
    связи с другими датасетами."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_service import register_dataset
    if not (data.get("content") or data.get("url")
            or data.get("content_b64")):
        return {"success": False, "error": "нужен content, url или content_b64"}
    return await register_dataset(
        uid, title=data.get("title", ""), content=data.get("content"),
        url=data.get("url"), fmt=data.get("format"),
        content_b64=data.get("content_b64"))


@get("/datasets")
async def list_datasets_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_service import registry_for_user
    items = registry_for_user(uid).list()
    return {"datasets": items, "total": len(items)}


async def _granted_registry(uid: str, dataset_id: str,
                            owner: Optional[str], required: str):
    """S2: датасет другого владельца по гранту. Возвращает (registry,
    error|None). Невидимое неотличимо от несуществующего."""
    from backend.core.ontology.dataset_service import registry_for_user
    if not owner or owner == uid:
        return registry_for_user(uid), None
    try:
        from backend.core.auth.resource_permissions import check
        res = await check(uid, required, resource_type="dataset",
                          resource_id=dataset_id, owner_id=owner)
        if res["allowed"]:
            return registry_for_user(owner), None
    except Exception:
        logger.debug("dataset grant check skipped", exc_info=True)
    return None, {"success": False, "error": "датасет не найден"}


@get("/datasets/{dataset_id:str}")
async def get_dataset_route(
        request: Request, dataset_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
        owner: Optional[str] = Parameter(query="owner", default=None),
) -> dict:
    """owner=<uid> — чужой датасет по read-гранту (S2)."""
    _guard()
    uid = _resolve_user(request, user_id)
    reg, err = await _granted_registry(uid, dataset_id, owner, "read")
    if err:
        return err
    rec = reg.get(dataset_id)
    if not rec:
        return {"success": False, "error": "датасет не найден"}
    return {"success": True, "dataset": rec}


@post("/datasets/{dataset_id:str}/ask")
async def ask_dataset_route(
        request: Request, dataset_id: str, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Вопрос к датасету: {question}. Ответ — цифры с планом и честной
    оценкой (покрытие, заземление в контексте, свежесть)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_service import ask_dataset
    question = (data.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question обязателен"}
    # Enterprise-safe: ask никогда не должен ронять 500 с пустой ошибкой —
    # ловим всё, логируем трейс, возвращаем понятную причину во фронт.
    try:
        owner = data.get("owner")
        if owner and owner != uid:        # S2: чужой датасет по read-гранту
            reg, err = await _granted_registry(uid, dataset_id, owner, "read")
            if err:
                return err
            return await ask_dataset(uid, dataset_id, question, registry=reg)
        return await ask_dataset(uid, dataset_id, question)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("ask_dataset failed (dataset=%s): %s", dataset_id, e)
        return {"success": False,
                "error": f"внутренняя ошибка обработки таблицы: {e}",
                "dataset_id": dataset_id}


@post("/datasets/{dataset_id:str}/correct")
async def correct_dataset_route(
        request: Request, dataset_id: str, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Корректировка от пользователя: {column, entity_type?, kpi?, unit?,
    scale?, dtype?, label?, comment?}. «Система поняла не так — я поправил»:
    правка сильнее авто-грундинга, переживает refresh, все дальнейшие
    ответы считаются с ней. Пустое значение поля снимает правку."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_service import correct_dataset_column
    column = (data.get("column") or "").strip()
    if not column:
        return {"success": False, "error": "column обязателен"}
    patch = {k: v for k, v in data.items() if k != "column"}
    return correct_dataset_column(uid, dataset_id, column, patch)


@post("/datasets/{dataset_id:str}/refresh")
async def refresh_dataset_route(
        request: Request, dataset_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_service import refresh_dataset
    return await refresh_dataset(uid, dataset_id)


@post("/datasets/{dataset_id:str}/schedule")
async def set_dataset_schedule_route(
        request: Request, dataset_id: str, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Назначить датасету авто-синк по расписанию (A1). Тело — спека:
      {"kind":"interval","minutes":60} |
      {"kind":"daily","time":"09:00"} |
      {"kind":"weekly","time":"09:00","weekday":0}
    Только для источников crm/url (которые умеют refresh)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_sync import set_schedule
    return set_schedule(uid, dataset_id, data or {})


@delete("/datasets/{dataset_id:str}/schedule", status_code=200)
async def clear_dataset_schedule_route(
        request: Request, dataset_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Снять авто-синк с датасета."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_sync import clear_schedule
    return clear_schedule(uid, dataset_id)


@post("/datasets/{dataset_id:str}/sync-now")
async def sync_dataset_now_route(
        request: Request, dataset_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Синхронизировать сейчас + записать наблюдаемость (в отличие от голого
    /refresh — фиксирует статус/строки/ошибку/длительность в датасете)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_sync import sync_now
    return await sync_now(uid, dataset_id, trigger="manual")


@get("/datasets-sync/status")
async def dataset_sync_status_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Панель «Источники данных»: датасеты с авто-синком + последнее
    состояние синка (last_at/status/rows/error). Плюс глобальный флаг."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_sync import sync_status, sync_enabled
    return {"success": True, "enabled": sync_enabled(),
            "sources": sync_status(uid)}


@post("/datasets/{dataset_id:str}/workbook")
async def workbook_dataset_route(
        request: Request, dataset_id: str, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Воркбук: LLM пишет pandas-код под вопрос, код исполняется в
    песочнице (deny-list, урезанные builtins, таймаут), ответ приходит
    вместе с кодом. Для вопросов, которые не влезают в DSL «Спросить»."""
    _guard()
    uid = _resolve_user(request, user_id)
    question = (data.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question обязателен"}
    from backend.core.ontology.workbook import run_workbook
    try:
        return await run_workbook(uid, dataset_id, question)
    except Exception as e:
        logger.exception("workbook failed (dataset=%s): %s", dataset_id, e)
        return {"success": False, "error": f"воркбук упал: {e}"}


@get("/datasets/{dataset_id:str}/versions")
async def dataset_versions_route(
        request: Request, dataset_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """История версий строк датасета (архив при каждом refresh) +
    дифф последнего обновления (строки, суммы числовых колонок)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_service import registry_for_user
    reg = registry_for_user(uid)
    rec = reg.get(dataset_id)
    if not rec:
        return {"success": False, "error": "датасет не найден"}
    return {"success": True,
            "versions": reg.list_versions(dataset_id),
            "last_diff": rec.get("last_diff"),
            "current": {"rows_total": (rec.get("profile") or {}).get(
                "rows_total"), "refreshed_at": rec.get("refreshed_at")}}


@post("/datasets/{dataset_id:str}/rollback")
async def dataset_rollback_route(
        request: Request, dataset_id: str, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Откатить строки к версии N. Обратимо: текущее состояние само
    архивируется как новая версия."""
    _guard()
    uid = _resolve_user(request, user_id)
    try:
        version = int(data.get("version"))
    except (TypeError, ValueError):
        return {"success": False, "error": "version (число) обязателен"}
    from backend.core.ontology.dataset_service import rollback_dataset
    return rollback_dataset(uid, dataset_id, version)


@get("/crm/providers")
async def crm_providers_route() -> dict:
    """Список зарегистрированных CRM-провайдеров и их «дек» (сделки/
    контакты/задачи) + готовность по env."""
    _guard()
    from backend.core.ontology.crm_connectors import list_providers
    return {"providers": list_providers()}


@post("/crm/import")
async def crm_import_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Подключить дек CRM как датасет: {provider, kind, title?, limit?}.
    Те же ручки регистрации/refresh потом — система не отличает источник."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_service import register_from_crm
    provider, kind = data.get("provider"), data.get("kind")
    if not (provider and kind):
        return {"success": False, "error": "provider и kind обязательны"}
    return await register_from_crm(
        uid, provider=provider, kind=kind,
        title=data.get("title"), limit=int(data.get("limit") or 200))


@get("/metrics")
async def metrics_list_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Единые метрики (мост «цифры из встреч ↔ цифры из таблиц»): имя,
    единица, число точек, число источников, последнее значение."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.metric_registry import metrics_for_user
    items = metrics_for_user(uid).list_metrics()
    return {"metrics": items, "total": len(items)}


@get("/metrics-dashboard")
async def metrics_dashboard_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Дашборд трендов (B3): все метрики с компактными рядами для графиков
    одним запросом — факт из данных / факт из встреч / план по оси
    period|дата, плюс delta сверки и выполнение плана."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.metric_registry import dashboard_for_user
    items = dashboard_for_user(uid)
    return {"success": True, "metrics": items, "total": len(items)}


@get("/metrics/{name:str}")
async def metric_summary_route(
        request: Request, name: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Карточка метрики: последняя цифра из каждого источника, полный ряд
    с происхождением каждой точки, сверка «встреча vs данные»."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.metric_registry import metrics_for_user
    reg = metrics_for_user(uid)
    s = reg.summary(name)
    if not s:
        return {"success": False, "error": f"метрика «{name}» не найдена"}
    return {"success": True, **s, "series": reg.series(name)}


@get("/glossary")
async def glossary_list_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Корпоративный словарь синонимов/акронимов (Glean-перенос №3).
    CRUD доступен всегда; влияние на поиск — за флагом
    enable_company_glossary."""
    uid = _resolve_user(request, user_id)
    from backend.core.memory.company_glossary import glossary_for_user
    terms = glossary_for_user(uid).list()
    return {"terms": terms, "total": len(terms)}


@post("/glossary")
async def glossary_set_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Добавить/заменить термин: {term, aliases: [...]}. Удалить:
    {term, delete: true}. Проверить расширение: {test_query}."""
    uid = _resolve_user(request, user_id)
    from backend.core.memory.company_glossary import glossary_for_user
    g = glossary_for_user(uid)
    if data.get("test_query"):
        return {"success": True, **g.expand_query(data["test_query"])}
    term = data.get("term")
    if not term:
        return {"success": False, "error": "term обязателен"}
    if data.get("delete"):
        return {"success": True, "deleted": g.delete_term(term)}
    try:
        return {"success": True, **g.set_term(term, data.get("aliases") or [])}
    except ValueError as e:
        return {"success": False, "error": str(e)}


@post("/datasets/{dataset_id:str}/share")
async def share_dataset_route(
        request: Request, dataset_id: str, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Поделиться датасетом с организацией (Glean-перенос №5):
    {access_groups?: [...]} — пусто = вся организация. Личный реестр не
    меняется (копия в org-пространство)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.org_datasets import share_dataset
    return share_dataset(uid, dataset_id,
                         access_groups=data.get("access_groups"))


@get("/org-datasets")
async def org_datasets_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Org-датасеты, видимые пользователю (членство + группы доступа)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.org_datasets import list_visible_org_datasets
    items = list_visible_org_datasets(uid)
    return {"datasets": items, "total": len(items)}


@post("/org-datasets/{dataset_id:str}/ask")
async def ask_org_dataset_route(
        request: Request, dataset_id: str, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Вопрос цифрами к org-датасету с проверкой прав (невидимый
    неотличим от несуществующего)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.org_datasets import ask_org_dataset
    question = (data.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question обязателен"}
    return await ask_org_dataset(uid, dataset_id, question)


@get("/datasets/{dataset_id:str}/export.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
async def export_dataset_xlsx_route(
        request: Request, dataset_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> bytes:
    """Excel-экспорт датасета (Cowork-перенос C2): stdlib-OOXML, числа —
    числами (Excel суммирует), до 10000 строк."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_service import registry_for_user
    from backend.core.ontology.xlsx_export import rows_to_xlsx
    reg = registry_for_user(uid)
    rec = reg.get(dataset_id)
    if not rec:
        raise HTTPException(status_code=404, detail="датасет не найден")
    return rows_to_xlsx(rec.get("columns") or [], reg.load_rows(dataset_id),
                        sheet_name=rec.get("title") or "Данные")


@get("/verified")
async def verified_list_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Verified answers (Glean-перенос №4): отметки «проверено владельцем»
    на фактах. Отдельный стор — память/поиск не затрагиваются."""
    uid = _resolve_user(request, user_id)
    from backend.core.memory.verified_answers import verified_for_user
    marks = verified_for_user(uid).list()
    return {"verified": marks, "total": len(marks)}


@post("/verified")
async def verified_set_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Пометить: {fact_key, by, ttl_days?}. Снять: {fact_key, delete: true}."""
    uid = _resolve_user(request, user_id)
    from backend.core.memory.verified_answers import verified_for_user
    v = verified_for_user(uid)
    fact_key = data.get("fact_key")
    if not fact_key:
        return {"success": False, "error": "fact_key обязателен"}
    if data.get("delete"):
        return {"success": True, "deleted": v.unverify(fact_key)}
    try:
        return {"success": True,
                **v.verify(fact_key, by=data.get("by", ""),
                           ttl_days=int(data.get("ttl_days") or 90))}
    except ValueError as e:
        return {"success": False, "error": str(e)}


@get("/experts")
async def experts_route(
        request: Request,
        topic: str = Parameter(query="topic"),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Expert finding (идея Glean): «кто в компании знает про тему» —
    по связям графа и совместным упоминаниям. Нет сигналов → честно пусто."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.expert_finder import experts_for_user
    if not (topic or "").strip():
        return {"success": False, "error": "topic обязателен"}
    # Ловим ошибки сервиса и отдаём JSON вместо 500 plain-text (иначе фронт
    # падал на res.json() → «Unexpected token 'I', Internal S…»). Настоящую
    # причину логируем и кладём в error, чтобы её было видно в UI.
    try:
        return {"success": True, **(await experts_for_user(uid, topic.strip()))}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("experts_route failed for topic=%r", topic)
        return {"success": False, "error": f"expert finder error: {exc}", "experts": []}


@get("/object")
async def object360_route(
        request: Request,
        name: str = Parameter(query="name"),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Объектный 360-браузер: всё про сущность на одном экране — факты
    (версии), упоминания по источникам, события, связи графа, датасеты,
    где сущность живёт. Недоступный источник честно отсутствует."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.object360 import object360_for_user
    if not (name or "").strip():
        return {"success": False, "error": "name обязателен"}
    try:
        card = await object360_for_user(uid, name.strip())
        return {"success": True, **card}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("object360_route failed for name=%r", name)
        return {"success": False, "error": f"object360 error: {exc}"}


@delete("/datasets/{dataset_id:str}", status_code=200)
async def delete_dataset_route(
        request: Request, dataset_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.ontology.dataset_service import registry_for_user
    return {"deleted": registry_for_user(uid).delete(dataset_id)}


ontology_datasets_router = Router(
    path="/ontology",
    route_handlers=[
        register_dataset_route, list_datasets_route, get_dataset_route,
        ask_dataset_route, correct_dataset_route, refresh_dataset_route,
        workbook_dataset_route, dataset_versions_route,
        dataset_rollback_route,
        set_dataset_schedule_route, clear_dataset_schedule_route,
        sync_dataset_now_route, dataset_sync_status_route,
        delete_dataset_route, metrics_list_route, metric_summary_route,
        metrics_dashboard_route,
        object360_route, crm_providers_route, crm_import_route,
        experts_route, glossary_list_route, glossary_set_route,
        verified_list_route, verified_set_route,
        share_dataset_route, org_datasets_route, ask_org_dataset_route,
        export_dataset_xlsx_route,
    ],
    tags=["Data Ontology"],
)
