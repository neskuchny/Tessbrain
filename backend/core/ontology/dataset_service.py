# -*- coding: utf-8 -*-
"""
Оркестратор онтологии данных — «палантир-модуль», часть 3.

build_dataset_ontology: профиль (детерминированный) + заземление колонок
в контексте компании (TableUnderstandingAgent: прайор из графа знаний,
LLM строго под скелетом) + связи с другими датасетами → онтология.

ask_dataset: вопрос → план (LLM cheap-tier; без LLM — фолбэк-парсер) →
детерминированное исполнение → честная оценка → находка в память
датасета. Цифры считает код; LLM никогда не считает и не пишет код.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from backend.core.ontology.dataset_registry import DatasetRegistry, unit_label
from backend.core.ontology.query_engine import (
    OPS,
    assess_result,
    execute_plan,
    parse_question_fallback,
    validate_plan,
)

logger = logging.getLogger(__name__)


def _tua():
    """Модуль table_understanding_agent. Прямой импорт тянет
    think/__init__ (autogen и пр.) — при его отсутствии грузим файл
    модуля напрямую: сам агент — чистый stdlib."""
    try:
        from backend.core.think import table_understanding_agent as m
        return m
    except Exception:
        import importlib.util
        import os
        import sys
        name = "_tua_standalone"
        if name in sys.modules:
            return sys.modules[name]
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "think", "table_understanding_agent.py")
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m     # dataclasses требуют модуль в sys.modules
        spec.loader.exec_module(m)
        return m


def registry_for_user(user_id: str) -> DatasetRegistry:
    from backend.core.store.tenant_paths import datasets_index_path_for_user
    return DatasetRegistry(datasets_index_path_for_user(user_id))


# ──────────────────────────────────────────────────────────────────────
# Онтология: заземление колонок в контексте компании
# ──────────────────────────────────────────────────────────────────────

async def _company_prior(user_id: str) -> dict:
    """Прайор из графа знаний (имена людей/проектов/KPI). Best-effort:
    нет графа → пустой прайор → честное «не заземлено»."""
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        prior_from_graph_nodes = _tua().prior_from_graph_nodes
        gb = GraphBuilder(use_networkx=None,
                          graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        if gb.connected and gb.nx_graph:
            return prior_from_graph_nodes(
                d for _n, d in gb.nx_graph.nodes(data=True))
    except Exception as e:
        logger.debug(f"ontology prior unavailable: {e}")
    return {}


async def build_dataset_ontology(user_id: str, columns: List[str],
                                 rows: List[dict],
                                 prior: Optional[dict] = None) -> dict:
    """Онтология датасета: per-column заземление (known/entity/kpi) +
    интерпретация таблицы (тип/период) когда заземлена."""
    mod = _tua()
    TableUnderstandingAgent = mod.TableUnderstandingAgent
    header_is_known_kpi = mod.header_is_known_kpi
    if prior is None:
        prior = await _company_prior(user_id)
    agent = TableUnderstandingAgent()
    understanding = await agent.understand(columns, rows, prior=prior)
    u = understanding.to_dict() if hasattr(understanding, "to_dict") else {}

    grounding: dict = {}
    for cp in u.get("columns") or []:
        name = cp.get("name", "")
        known_kpi = header_is_known_kpi(name, prior) if prior else False
        grounding[name] = {
            "known": bool(cp.get("matched_entity_type")) or known_kpi,
            "entity_type": cp.get("matched_entity_type"),
            "kpi": name if known_kpi else None,
            "examples": (cp.get("matched_examples") or [])[:3],
        }
    return {
        "is_known": u.get("is_known", False),
        "confidence": u.get("confidence", 0.0),
        "table_type": u.get("table_type"),
        "time_period": u.get("time_period"),
        "grounding": grounding,
        "notes": u.get("notes") or [],
    }


# ──────────────────────────────────────────────────────────────────────
# Вопрос → план → цифры → оценка
# ──────────────────────────────────────────────────────────────────────

async def _plan_from_llm(question: str, dataset: dict,
                         others: Optional[List[dict]] = None) -> Optional[dict]:
    """Вопрос → структурный план через ДЕШЁВУЮ модель. Только план —
    никакого кода и никаких вычислений со стороны LLM.

    others — другие таблицы юзера [{dataset_id, title, columns}]: модель
    может добавить в план "join", если вопрос требует данных из двух таблиц."""
    try:
        from backend.core.llm.router import (
            ModelTier,
            TaskType,
            get_llm_router,
        )
        profile = dataset.get("profile") or {}
        cols_desc = "\n".join(
            f"- {p['name']} ({p['dtype']}; примеры: {', '.join(p.get('samples', [])[:3])})"
            for p in profile.get("columns", []))
        join_desc = ""
        if others:
            lines = "\n".join(
                f"- id={o['dataset_id']} «{o['title']}»: колонки: "
                + ", ".join(o.get("columns") or [])
                for o in others[:5])
            join_desc = (
                "\nДругие таблицы пользователя (можно присоединить LEFT JOIN, "
                "если вопрос требует их данных):\n" + lines +
                '\nДля join добавь в план: "join": {"dataset": "<id>", '
                '"left_on": "<колонка этой таблицы>", '
                '"right_on": "<колонка той таблицы>"}. '
                "После join доступны колонки обеих таблиц.\n")
        prompt = (
            "Переведи вопрос пользователя в JSON-план запроса к таблице.\n"
            f"Колонки таблицы:\n{cols_desc}\n{join_desc}\n"
            f"Операции: {', '.join(OPS)}. Фильтры: eq|ne|gt|lt|gte|lte|contains.\n"
            f"Вопрос: {question}\n\n"
            'Верни ТОЛЬКО JSON: {"op": "...", "column": "...", '
            '"group_by": null, "limit": 10, "filters": '
            '[{"column": "...", "op": "eq", "value": "..."}]}. '
            "Используй ТОЛЬКО существующие колонки. join добавляй ТОЛЬКО "
            "когда без второй таблицы не ответить.")
        # TaskType.ANALYSIS не существует (AttributeError на КАЖДОМ вызове):
        # «Ask» по датасету всегда падал в «LLM недоступна» и понимал только
        # шаблонные вопросы. REASONING — ближайший реальный тип.
        plan = await get_llm_router().generate_json(
            prompt=prompt, task_type=TaskType.REASONING,
            model_tier=ModelTier.STANDARD, temperature=0.0)
        return plan if isinstance(plan, dict) else None
    except Exception as e:
        # warning, не debug: молчаливый фолбэк прятал реальную причину
        logger.warning(f"dataset ask: LLM-план недоступен: {e}")
        return None


async def ask_dataset(user_id: str, dataset_id: str, question: str,
                      registry: Optional[DatasetRegistry] = None) -> dict:
    """Главный вход «спросить таблицу»: понять → посчитать → оценить.

    Возвращает {answer: цифры, plan, assessment, llm_plan_used} и пишет
    находку в память датасета. registry — опциональная подмена источника
    (org-shared, см. org_datasets); по умолчанию личный реестр."""
    reg = registry or registry_for_user(user_id)
    dataset = reg.get(dataset_id)
    if not dataset:
        return {"success": False, "error": "датасет не найден"}
    rows = reg.load_rows(dataset_id)
    columns = dataset.get("columns") or []

    profile = dataset.get("profile") or {}
    date_cols = set(profile.get("date_columns") or [])

    def _resolve_month(p: Optional[dict]) -> Optional[dict]:
        # «по месяцам» → первая date-колонка датасета
        if p and p.get("group_by") == "__month__":
            if not date_cols:
                return None
            p = {**p, "group_by": sorted(date_cols)[0]}
        return p

    # Другие таблицы юзера — LLM может предложить LEFT JOIN
    others = [{"dataset_id": d.get("dataset_id"), "title": d.get("title"),
               "columns": d.get("columns") or []}
              for d in reg.list() if d.get("dataset_id") != dataset_id]

    plan_raw = _resolve_month(await _plan_from_llm(question, dataset, others))
    llm_used = plan_raw is not None

    # LEFT JOIN со второй таблицей: строки склеиваются ДО валидации плана,
    # чтобы колонки обеих таблиц были легальны для column/group_by/filters.
    join_note = None
    join_spec = (plan_raw or {}).get("join") if isinstance(plan_raw, dict) else None
    if isinstance(join_spec, dict):
        try:
            from backend.core.ontology.query_engine import join_rows
            ref = str(join_spec.get("dataset") or "").strip()
            other = reg.get(ref) or next(
                (d for d in reg.list()
                 if str(d.get("title", "")).strip().lower() == ref.lower()),
                None)
            l_on = str(join_spec.get("left_on") or "")
            r_on = str(join_spec.get("right_on") or "")
            if (other and other.get("dataset_id") != dataset_id
                    and l_on in columns
                    and r_on in (other.get("columns") or [])):
                right_rows = reg.load_rows(other["dataset_id"])
                rows, added, matched = join_rows(rows, right_rows, l_on, r_on)
                columns = list(columns) + added
                join_note = (f"LEFT JOIN «{other.get('title')}» по "
                             f"{l_on}={r_on}: пар найдено {matched} из "
                             f"{len(rows)} строк (при нескольких совпадениях "
                             "берётся первое)")
            else:
                join_note = "join из плана отклонён (таблица/колонки не найдены)"
        except Exception as e:
            logger.warning(f"dataset join failed: {e}")
            join_note = f"join не выполнен: {e}"

    plan, errors = (validate_plan(plan_raw, columns)
                    if plan_raw else (None, ["LLM недоступна"]))
    if plan is None:
        fallback = _resolve_month(parse_question_fallback(question, columns))
        if fallback:
            plan, errors = validate_plan(fallback, columns)
            llm_used = False
    if plan is None:
        # DSL не справился → воркбук: LLM пишет pandas-код, песочница
        # исполняет (deny-list, урезанные builtins, таймаут). Ответ придёт
        # с кодом и пометкой «вычислено кодом».
        try:
            from backend.core.ontology.workbook import (
                pandas_available,
                run_workbook,
                workbook_enabled,
            )
            if workbook_enabled() and pandas_available():
                wb = await run_workbook(user_id, dataset_id, question,
                                        registry=reg)
                if wb.get("success"):
                    return wb
                errors = errors + [f"воркбук: {wb.get('error')}"]
        except Exception as e:
            logger.warning(f"workbook fallback failed: {e}")
        return {"success": False,
                "error": "не понял вопрос к таблице: " + "; ".join(errors),
                "hint": "примеры: «сколько строк», «сумма <колонка>», "
                        "«сумма <колонка> по <колонка>», «топ 5 по <колонка>»",
                "columns": columns}

    exec_out = execute_plan(plan, rows, date_columns=date_cols)
    # Оценка/сверка с контекстом — best-effort: если они падают, ЦИФРА всё
    # равно возвращается (раньше throw тут ронял весь ответ).
    try:
        assessment = assess_result(plan, exec_out, dataset)
    except Exception as e:
        logger.warning(f"assess_result failed: {e}")
        assessment = {"confidence": 0.5, "notes": []}
    try:
        assessment.setdefault("notes", [])
        if join_note:
            assessment["notes"].insert(0, join_note)
        assessment["context_checks"] = _context_checks(reg, dataset, plan, exec_out)
        for c in assessment["context_checks"]:
            assessment["notes"].append(c["note"])
    except Exception as e:
        logger.warning(f"context_checks failed: {e}")
        assessment.setdefault("context_checks", [])
    reg.add_finding(dataset_id, {
        "question": question, "plan": plan,
        "result_preview": str(exec_out.get("result"))[:300],
        "confidence": assessment["confidence"],
    })
    return {"success": True, "question": question, "plan": plan,
            "llm_plan_used": llm_used,
            "answer": exec_out.get("result"),
            "rows_used": exec_out.get("rows_used"),
            "assessment": assessment,
            "dataset": {"id": dataset_id, "title": dataset.get("title"),
                        "data_as_of": dataset.get("refreshed_at")}}


def _scalar(x) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _context_checks(reg, dataset: dict, plan: dict, exec_out: dict) -> list:
    """Сверка цифры с контекстом (детерминированно):
    1) история: тот же план уже считали → дельта со прошлым значением;
    2) связанные датасеты: те же колонки есть в другом датасете → та же
       агрегация там → расхождение в % («CRM и отчёт сходятся?»)."""
    checks: list = []
    result = _scalar(exec_out.get("result"))

    # 1) история находок этого датасета
    if result is not None:
        import json as _json
        plan_key = _json.dumps(plan, sort_keys=True, ensure_ascii=False)
        for f in reversed(dataset.get("findings") or []):
            if _json.dumps(f.get("plan") or {}, sort_keys=True,
                           ensure_ascii=False) != plan_key:
                continue
            try:
                prev = float(f.get("result_preview"))
            except (TypeError, ValueError):
                break
            if prev:
                delta = round((result - prev) / abs(prev) * 100, 1)
                checks.append({
                    "kind": "history", "previous": prev,
                    "previous_at": f.get("at"), "delta_pct": delta,
                    "note": (f"этот же вопрос считали {f.get('at', '')[:10]}: "
                             f"было {prev:g}, сейчас {result:g} "
                             f"({'+' if delta >= 0 else ''}{delta}%)")})
            break

    # 2) связанные датасеты: одинаковые имена колонок плана
    if result is not None and plan.get("op") in ("sum", "avg", "count",
                                                 "group_sum"):
        needed = {c for c in (plan.get("column"), plan.get("group_by")) if c}

        def _col_unit(ds: dict, col: Optional[str]) -> tuple:
            for p in ((ds.get("profile") or {}).get("columns") or []):
                if p.get("name") == col:
                    return (p.get("unit"), p.get("scale"))
            return (None, None)

        my_unit = _col_unit(dataset, plan.get("column"))
        for rel in (dataset.get("relations") or [])[:3]:
            other = reg.get(rel.get("dataset_id", ""))
            if not other:
                continue
            other_cols = set(other.get("columns") or [])
            if not needed.issubset(other_cols):
                continue
            # Не сравниваем несопоставимое: разные валюты/масштабы у той же
            # колонки → вместо ложного «РАСХОЖДЕНИЕ 99%» честная пометка.
            o_unit = _col_unit(other, plan.get("column"))
            if my_unit != o_unit and any(my_unit) and any(o_unit):
                checks.append({
                    "kind": "unit_mismatch",
                    "other_dataset": other.get("title"),
                    "note": (f"сверка с «{other.get('title')}» пропущена: "
                             f"единицы колонки различаются "
                             f"({my_unit} vs {o_unit})")})
                continue
            try:
                o_rows = reg.load_rows(other["dataset_id"])
                o_dates = set((other.get("profile") or {}).get("date_columns") or [])
                o_out = execute_plan(plan, o_rows, date_columns=o_dates)
            except Exception:
                continue
            o_res = _scalar(o_out.get("result"))
            if o_res is None:
                continue
            if result == 0 and o_res == 0:
                continue
            base = max(abs(result), abs(o_res))
            diff = round(abs(result - o_res) / base * 100, 1) if base else 0.0
            verdict = ("сходится" if diff <= 2
                       else f"РАСХОЖДЕНИЕ {diff}%")
            checks.append({
                "kind": "cross_dataset",
                "other_dataset": other.get("title"),
                "other_result": o_res, "diff_pct": diff,
                "note": (f"сверка с «{other.get('title')}»: там тот же "
                         f"расчёт даёт {o_res:g} — {verdict}")})
    return checks


# ──────────────────────────────────────────────────────────────────────
# Корректировка от пользователя («система поняла не так — я поправил»)
# ──────────────────────────────────────────────────────────────────────

def _sync_dataset_metrics(user_id: str, rec: Optional[dict],
                          rows: Optional[List[dict]] = None) -> None:
    """Питатель единых метрик: KPI-заземлённые числовые колонки → ряды в
    metric_registry (мост к цифрам из встреч). Never-raise."""
    if not rec:
        return
    try:
        from backend.core.ontology.metric_registry import (
            ingest_dataset_metrics,
        )
        if rows is None:
            rows = registry_for_user(user_id).load_rows(
                rec.get("dataset_id") or "")
        n = ingest_dataset_metrics(user_id, rec, rows or [])
        if n:
            logger.info(f"📈 Metrics: {n} точек из датасета "
                        f"«{rec.get('title')}» в единый реестр")
    except Exception as e:
        logger.debug(f"metric ingest (dataset) skipped: {e}")


def _index_dataset_summary(user_id: str, rec: dict) -> None:
    """Саммари датасета → поисковые индексы мозга. Never-raise.

    До этого таблицы были невидимы для чата/deep-search: сырые строки в
    индексы не попадают (осознанно), но и САММАРИ не индексировалось —
    поиск «какие у нас есть данные по расходам» не находил датасет.
    Индексируем компактный текст: название, колонки с типами/единицами,
    заземление на сущности компании, размер и свежесть."""
    if not rec:
        return
    dataset_id = rec.get("dataset_id") or ""
    try:
        profile = rec.get("profile") or {}
        grounding = (rec.get("ontology") or {}).get("grounding") or {}
        col_bits = []
        for p in (profile.get("columns") or [])[:30]:
            bit = f"{p.get('name')} ({p.get('dtype')}"
            u = unit_label(p)
            if u:
                bit += f", {u}"
            g = grounding.get(p.get("name")) or {}
            if g.get("known"):
                bit += f" → {g.get('entity_type') or g.get('kpi')}"
            bit += ")"
            col_bits.append(bit)
        text = (
            f"Датасет «{rec.get('title')}» ({profile.get('rows_total', 0)} строк, "
            f"обновлён {rec.get('refreshed_at', '')[:10]}). "
            f"Колонки: {'; '.join(col_bits)}. "
            "Таблица доступна в разделе Знания → Данные; по ней можно задавать "
            "числовые вопросы (суммы, топы, динамика по месяцам, join).")
        doc_id = f"dataset_{dataset_id}"
        meta = {"type": "dataset", "dataset_id": dataset_id,
                "title": rec.get("title"), "source": "data_ontology"}

        # BM25 — лёгкий и надёжный, индекс на юзера
        from backend.core.search.bm25_searcher import get_bm25_searcher
        bm25 = get_bm25_searcher(user_id=user_id)
        bm25.add_document(doc_id, text, metadata=meta)
        bm25.save_index()

        # Вектор — best-effort в коллекцию reports (её читает deep-search)
        try:
            import asyncio as _aio
            from backend.core.store.tenant_paths import (
                vector_index_path_for_user,
            )
            from backend.core.store.vector_indexer import VectorIndexer

            async def _vec():
                vi = VectorIndexer(
                    use_qdrant=None,
                    storage_path=vector_index_path_for_user(user_id),
                    namespace=user_id)
                await vi.connect()
                try:
                    await vi.upsert_vector_public(
                        "reports", doc_id, text, metadata=meta)
                finally:
                    await vi.close()

            loop = _aio.get_event_loop()
            if loop.is_running():
                _aio.ensure_future(_vec())
            else:
                loop.run_until_complete(_vec())
        except Exception:
            logger.debug("dataset vector index skipped", exc_info=True)
        logger.info(f"🔎 Датасет «{rec.get('title')}» проиндексирован в поиск")
    except Exception as e:
        logger.debug(f"dataset summary index skipped: {e}")


def rollback_dataset(user_id: str, dataset_id: str, version: int) -> dict:
    """Откат строк датасета к версии N (история refresh'ей). Метрики и
    поисковый индекс пересобираются под откатанные данные. Сам откат
    обратим: текущее состояние архивируется как новая версия."""
    reg = registry_for_user(user_id)
    rec = reg.rollback(dataset_id, version)
    if rec is None:
        return {"success": False,
                "error": f"версия {version} не найдена или пуста"}
    rows = reg.load_rows(dataset_id)
    _sync_dataset_metrics(user_id, rec, rows)
    _index_dataset_summary(user_id, rec)
    return {"success": True, "dataset": _slim(rec),
            "rows_total": len(rows),
            "versions": reg.list_versions(dataset_id)}


def correct_dataset_column(user_id: str, dataset_id: str, column: str,
                           patch: dict) -> dict:
    """Ручная правка колонки: entity_type/kpi (к чему относится), unit/
    scale (валюта и масштаб: ₽/$, тыс/млн), dtype, label. Правка сильнее
    авто-грундинга, переживает refresh, дальше все ответы считаются с ней.
    Снять правку — прислать пустое значение поля."""
    reg = registry_for_user(user_id)
    # удобные алиасы масштаба и валюты от человека («млн», «₽»)
    scale_alias = {"тыс": 1_000, "млн": 1_000_000, "млрд": 1_000_000_000}
    if isinstance(patch.get("scale"), str):
        s = patch["scale"].strip().lower()
        if s in scale_alias:
            patch["scale_label"] = s
            patch["scale"] = scale_alias[s]
        elif s in ("", "1", "нет"):
            patch["scale"] = None
            patch["scale_label"] = None
        else:
            try:
                patch["scale"] = float(s)
            except ValueError:
                return {"success": False,
                        "error": f"scale «{s}» не понят: тыс|млн|млрд|число"}
    unit_alias = {"₽": ("RUB", "₽"), "руб": ("RUB", "₽"), "rub": ("RUB", "₽"),
                  "$": ("USD", "$"), "usd": ("USD", "$"),
                  "€": ("EUR", "€"), "eur": ("EUR", "€")}
    if isinstance(patch.get("unit"), str) and patch["unit"].strip():
        code, sym = unit_alias.get(patch["unit"].strip().lower(),
                                   (patch["unit"].strip().upper(),
                                    patch["unit"].strip()))
        patch["unit"], patch["unit_symbol"] = code, sym
    rec = reg.set_column_override(dataset_id, column, patch)
    if rec is None:
        return {"success": False,
                "error": f"датасет или колонка «{column}» не найдены"}
    # корректировка могла заземлить колонку на KPI → пересобрать метрики
    _sync_dataset_metrics(user_id, rec)
    _index_dataset_summary(user_id, rec)
    return {"success": True, "dataset": _slim(rec),
            "override": (rec.get("overrides") or {}).get(column)}


# ──────────────────────────────────────────────────────────────────────
# Регистрация с онтологией + refresh по ссылке
# ──────────────────────────────────────────────────────────────────────

async def register_from_crm(user_id: str, *, provider: str, kind: str,
                            title: Optional[str] = None,
                            limit: int = 200,
                            fetch_fn=None) -> dict:
    """Подключить таблицу из CRM нативно: AmoCRM/Bitrix24/HubSpot/
    Pipedrive (см. crm_connectors). Возвращает зарегистрированный
    датасет; refresh потом тянет свежие данные тем же коннектором."""
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector(provider)
    if conn is None:
        return {"success": False, "error": f"провайдер {provider} не зарегистрирован"}
    # *_for_user: ключи из карточки «Интеграций» пользователя, фолбэк — env
    # (для старых коннекторов дефолт делегирует в env-версии — без изменений).
    err = await conn.env_check_for_user(user_id)
    if err:
        return {"success": False, "error": err}
    try:
        columns, raw_rows = await conn.fetch_for_user(
            user_id, kind, fetch_fn, limit=limit)
    except Exception as e:
        return {"success": False, "error": f"{provider} {kind}: {e}"}
    if not raw_rows:
        return {"success": False, "error": "CRM вернула пустой ответ"}
    rows = [{c: r.get(c) for c in columns} for r in raw_rows]
    ontology = await build_dataset_ontology(user_id, columns, rows)
    rec = registry_for_user(user_id).register(
        title=title or f"{provider}:{kind}",
        columns=columns, rows=rows,
        source={"kind": "crm", "provider": provider,
                "deck": kind, "limit": limit},
        ontology=ontology)
    _sync_dataset_metrics(user_id, rec, rows)
    _index_dataset_summary(user_id, rec)
    return {"success": True, "dataset": _slim(rec)}


async def register_dataset(user_id: str, *, title: str,
                           content: Optional[str] = None,
                           url: Optional[str] = None,
                           fmt: Optional[str] = None,
                           content_b64: Optional[str] = None) -> dict:
    """Зарегистрировать датасет из сырья, по URL или из бинарного файла
    (content_b64 + fmt='xlsx'): парсинг → профиль → онтология (контекст
    компании) → связи с другими датасетами."""
    from backend.core.ontology.dataset_registry import parse_tabular
    source: dict[str, Any] = {"kind": "inline"}
    if content_b64:
        import base64
        from backend.core.ontology.xlsx_import import parse_xlsx
        try:
            raw = base64.b64decode(content_b64)
        except Exception:
            return {"success": False, "error": "content_b64 повреждён"}
        columns, rows = parse_xlsx(raw)
        if not columns or not rows:
            return {"success": False,
                    "error": "не удалось разобрать .xlsx (нужен первый лист "
                             "с заголовками в первой строке)"}
        source = {"kind": "csv", "format": "xlsx"}
        ontology = await build_dataset_ontology(user_id, columns, rows)
        rec = registry_for_user(user_id).register(
            title=title, columns=columns, rows=rows,
            source=source, ontology=ontology)
        _sync_dataset_metrics(user_id, rec, rows)
        _index_dataset_summary(user_id, rec)
        return {"success": True, "dataset": _slim(rec)}
    if url:
        content = await _fetch_url(url)
        source = {"kind": "url", "location": url}
    columns, rows = parse_tabular(content or "", fmt=fmt)
    if not columns or not rows:
        return {"success": False,
                "error": "не удалось разобрать таблицу (CSV/TSV/JSON-массив)"}
    ontology = await build_dataset_ontology(user_id, columns, rows)
    rec = registry_for_user(user_id).register(
        title=title, columns=columns, rows=rows,
        source=source, ontology=ontology)
    _sync_dataset_metrics(user_id, rec, rows)
    _index_dataset_summary(user_id, rec)
    return {"success": True, "dataset": _slim(rec)}


async def refresh_dataset(user_id: str, dataset_id: str,
                          fetch_fn=None) -> dict:
    """Сходить в источник по ссылке за свежими данными и перестроить
    профиль+онтологию (для kind=url; inline освежается повторной загрузкой)."""
    from backend.core.ontology.dataset_registry import parse_tabular
    reg = registry_for_user(user_id)
    rec = reg.get(dataset_id)
    if not rec:
        return {"success": False, "error": "датасет не найден"}
    src = rec.get("source") or {}
    if src.get("kind") == "crm":
        from backend.core.ontology.crm_connectors import get_connector
        conn = get_connector(src.get("provider", ""))
        if conn is None:
            return {"success": False, "error": "CRM-провайдер исчез"}
        try:
            columns, raw = await conn.fetch_for_user(
                user_id, src.get("deck", ""), fetch_fn,
                limit=int(src.get("limit", 200)))
        except Exception as e:
            return {"success": False, "error": str(e)}
        rows = [{c: r.get(c) for c in columns} for r in raw]
    elif src.get("kind") == "url" and src.get("location"):
        content = await _fetch_url(src["location"])
        columns, rows = parse_tabular(content or "")
    else:
        return {"success": False,
                "error": "refresh поддержан только для kind=url и kind=crm"}
    if not columns:
        return {"success": False, "error": "источник вернул не таблицу"}

    # Дифф-детекция: источники не отдают «изменённое с даты», поэтому забор
    # всегда полный — но если данные НЕ изменились, дорогая часть не нужна:
    # не перестраиваем онтологию (LLM-вызовы), не пишем версию в историю
    # (кап 5 версий съедался пустыми обновлениями по расписанию), не трогаем
    # метрики. Регулярный синк по расписанию без этого превращал историю
    # версий в ленту одинаковых копий.
    try:
        import hashlib as _h
        import json as _j
        _old_rows = reg.load_rows(dataset_id)
        _old_cols = rec.get("columns") or []
        def _fp(cols, rws):
            return _h.sha256(_j.dumps([cols, rws], ensure_ascii=False,
                                      sort_keys=True, default=str)
                             .encode("utf-8")).hexdigest()
        if _old_rows and _fp(_old_cols, _old_rows) == _fp(columns, rows):
            reg.update(dataset_id, {"last_sync_unchanged_at":
                                    datetime.now(timezone.utc).isoformat()})
            return {"success": True, "unchanged": True,
                    "dataset": _slim(reg.get(dataset_id))}
    except Exception:
        logger.debug("dataset diff-check failed — обновляем как раньше",
                     exc_info=True)

    ontology = await build_dataset_ontology(user_id, columns, rows)
    rec = reg.update(dataset_id, {"columns": columns, "ontology": ontology},
                     rows=rows)
    _sync_dataset_metrics(user_id, rec, rows)
    _index_dataset_summary(user_id, rec)

    # Push шины: подписчикам уходит «датасет обновился» — счётчики без данных.
    try:
        from backend.core.data_bus.webhook_push import notify as _wh_notify
        await _wh_notify(user_id, "dataset_refreshed",
                         {"dataset": str(rec.get("name") or dataset_id)[:60],
                          "rows": len(rows)})
    except Exception:
        logger.debug("webhook push (dataset) skipped", exc_info=True)
    return {"success": True, "dataset": _slim(rec)}


async def _fetch_url(url: str) -> str:
    import httpx
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("только http(s) URL")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text


def _slim(rec: Optional[dict]) -> Optional[dict]:
    if not rec:
        return rec
    out = dict(rec)
    out.pop("findings", None)
    return out


# ──────────────────────────────────────────────────────────────────────
# Авто-роутинг чата: вопрос про цифры → нужный датасет (без LLM-детекта)
# ──────────────────────────────────────────────────────────────────────

_NUMERIC_INTENT = ("сколько", "сумм", "средн", "максим", "миним", "топ",
                   "всего", "количеств", "выручк", "динамик", "по месяцам")


def _significant_tokens(text: str) -> set:
    import re as _re
    return {t for t in _re.split(r"[^\wа-яё]+", (text or "").lower())
            if len(t) > 3}


async def try_metric_route(user_id: str, question: str) -> Optional[dict]:
    """Вопрос про ЕДИНУЮ метрику («какая выручка?», «а что по данным?») →
    карточка metric_registry: последние цифры из встреч И из таблиц +
    сверка. Консервативно: имя метрики должно встретиться в вопросе;
    метрика без точек / не найдена → None (чат идёт обычным путём)."""
    q = (question or "").lower()
    if not q or not any(w in q for w in _NUMERIC_INTENT):
        return None
    try:
        from backend.core.ontology.metric_registry import metrics_for_user
        reg = metrics_for_user(user_id)
        metrics = reg.list_metrics()
    except Exception:
        return None
    best = None
    for m in metrics:
        name = (m.get("name") or "").lower()
        if len(name) > 3 and name in q and m.get("points"):
            if best is None or m["points"] > best["points"]:
                best = m
    if not best:
        return None
    s = reg.summary(best["name"])
    if not s:
        return None
    unit = f" {best['unit']}" if best.get("unit") else ""
    lines = [f"Метрика «{best['name']}» (единый реестр: встречи + данные):"]
    latest = s.get("latest") or {}
    src_ru = {"meeting": "из встреч", "dataset": "из данных",
              "manual": "введено вручную"}
    for stype, p in latest.items():
        when = str(p.get("at") or "")[:10]
        per = f" за {p['period']}" if p.get("period") else ""
        lines.append(f"• {src_ru.get(stype, stype)}: "
                     f"{p['value']:g}{unit}{per} ({when})")
    plan = s.get("plan") or {}
    if plan.get("note"):
        lines.append(f"🎯 {plan['note']}")
    if s.get("note"):
        lines.append("")
        lines.append(f"⚖️ {s['note']}")
    lines.append("")
    lines.append(f"Точек в истории: {s.get('points_total', 0)} — "
                 "у каждой цифры видно происхождение (встреча/таблица)")
    return {"text": "\n".join(lines), "metric": best["name"],
            "summary": s}


async def try_dataset_route(user_id: str, question: str) -> Optional[dict]:
    """Понять, что вопрос чата — про подключённый датасет, и ответить
    цифрами через план→код. КОНСЕРВАТИВНО (хайджекать чат нельзя):

    нужен числовой интент И совпадение с конкретным датасетом (токен
    названия ИЛИ имя колонки в вопросе). Не уверены / план не понят →
    None, чат идёт обычным путём. Детект детерминированный, без LLM."""
    q = (question or "").lower()
    if not q or not any(w in q for w in _NUMERIC_INTENT):
        return None
    try:
        reg = registry_for_user(user_id)
        items = reg.list()
    except Exception:
        return None
    if not items:
        return None

    q_tokens = _significant_tokens(q)
    best, best_score = None, 0
    for slim in items:
        score = 0
        title_tokens = _significant_tokens(slim.get("title", ""))
        score += 2 * len(title_tokens & q_tokens)
        for col in slim.get("columns") or []:
            if len(col) > 2 and col.lower() in q:
                score += 3
        if score > best_score:
            best, best_score = slim, score
    if not best or best_score < 3:
        return None        # совпадение слабое — не вмешиваемся

    res = await ask_dataset(user_id, best["dataset_id"], question)
    if not res.get("success"):
        return None        # план не понят — пусть отвечает обычный чат

    a = res.get("assessment") or {}
    import json as _json
    answer = res.get("answer")
    unit = a.get("unit")
    answer_str = (f"{answer:g}{' ' + unit if unit else ''}"
                  if isinstance(answer, (int, float))
                  else _json.dumps(answer, ensure_ascii=False, indent=1))
    lines = [f"По датасету «{best.get('title')}» "
             f"(данные на {str(res['dataset'].get('data_as_of', ''))[:10]}):",
             "", answer_str, ""]
    lines.append(f"Уверенность: {a.get('confidence')} · учтено строк: "
                 f"{res.get('rows_used')} · план: "
                 f"{'LLM' if res.get('llm_plan_used') else 'правила'}")
    for n in (a.get("notes") or [])[:4]:
        lines.append(f"• {n}")
    return {"text": "\n".join(lines), "dataset_id": best["dataset_id"],
            "dataset_title": best.get("title"), "raw": res}
