# -*- coding: utf-8 -*-
"""Run orchestrator — петля разбора (Фаза B: INGEST + EXTRACT).

Проходит run через фазы:
    INGEST     документы (по document_id) → текст → chunks
    EXTRACT    map-reduce по chunks: извлечь факты/цифры по dimensions
    COMPUTE    safe-calc над цифрами               (Фаза C — заглушка тут)
    ANALYZE    LLM-вывод по dimensions             (Фаза C — заглушка тут)
    ASSEMBLE   финальный отчёт по output_template  (Фаза D — базовый тут)

Каждая фаза пишет в steps_log («достал/посчитал/положил») и сохраняет run
после себя → процесс прозрачный и воспроизводимый, частичный прогресс
виден через GET /analysis/runs/{id} даже если упало в середине.

Зависимости инъектируются (RunDeps) — для тестов без живого LLM/Supabase:
    - document_loader(doc_id) -> {id, title, content, doc_type}
    - llm: объект с async generate_json(prompt, system_prompt) -> dict|list

map-reduce extract: chunks группируются в батчи (влезающие в LLM-context),
каждый батч обрабатывается ОДНИМ LLM-вызовом, который извлекает факты сразу
по всем dimensions (N_batches вызовов вместо N_batches × N_dims). reduce
сливает факты per-dimension.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from backend.core.analysis.models import AnalysisPlaybook, AnalysisRun, RunStatus

logger = logging.getLogger(__name__)

# Сколько символов chunks влезает в один extract-батч. Консервативно —
# чтобы prompt + dimensions instructions + ответ влезли в контекст.
EXTRACT_BATCH_CHARS = 12000


# === Dependency injection ==================================================


@dataclass
class RunDeps:
    """Инъецируемые зависимости движка (для тестов — fakes)."""
    # async fn(document_id) -> {"id","title","content","doc_type"} | None
    document_loader: Callable[[str], Awaitable[Optional[dict[str, Any]]]]
    # объект с async generate_json(prompt, system_prompt=...) -> dict|list
    llm: Any
    # async fn(run) -> None — персист после каждой фазы
    save_run: Callable[[AnalysisRun], Awaitable[Any]]


# === Default deps (реальные Supabase + LLMRouter) ==========================


async def _default_document_loader(
    document_id: str, *, owner_user_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Загрузить документ по id из Supabase documents table.

    owner_user_id ОБЯЗАТЕЛЕН: Supabase-клиент ходит с service-ключом (RLS
    обходится), поэтому фильтр user_id — единственная граница тенанта.
    Без владельца — отказ (fail-closed), иначе любой авторизованный
    пользователь мог бы прогнать чужой документ через разбор и прочитать
    его содержимое в отчёте."""
    if not owner_user_id:
        logger.warning(
            "document_loader: отказ — owner_user_id не передан (doc %s)",
            document_id)
        return None
    try:
        from backend.db.supabase_client import get_supabase_client
        sb = get_supabase_client()
        rows = await sb._request(
            "GET", "/rest/v1/documents",
            params={"id": f"eq.{document_id}",
                    "user_id": f"eq.{owner_user_id}",
                    "select": "id,title,content,doc_type", "limit": "1"},
        )
        if rows:
            return rows[0]
    except Exception as e:
        logger.warning("document_loader failed for %s: %s", document_id, e)
    return None


def _default_llm() -> Any:
    from backend.core.llm.router import get_llm_router
    return get_llm_router()


class _TieredLLM:
    """Обёртка llm-депа: все generate_json уходят на выбранный тир модели.

    Прогон может попросить premium («тяжёлая» модель) — полезно для сложных
    инвестиционных/юридических документов; standard — экономичный дефолт."""

    def __init__(self, inner: Any, tier: str):
        self._inner = inner
        self._tier = tier

    async def generate_json(self, prompt: str, **kwargs: Any) -> Any:
        try:
            from backend.core.llm.router import ModelTier
            kwargs.setdefault("model_tier",
                              ModelTier.PREMIUM if self._tier == "premium"
                              else ModelTier.STANDARD)
        except Exception:
            pass
        return await self._inner.generate_json(prompt, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _tenant_model_enabled() -> bool:
    """Флаг: гнать разбор через тенантскую модель (DeepSeek v4 Pro). Дефолт ON;
    выключается ANALYSIS_TENANT_DEEPSEEK=off без правки кода (откат на Gemini)."""
    return os.getenv("ANALYSIS_TENANT_DEEPSEEK", "on").strip().lower() in (
        "1", "on", "true", "yes")


async def _tenant_deepseek_client(user_id: Optional[str]) -> Any:
    """DeepSeek v4 Pro на ключе тенанта («Интеграции») или платформы (env
    DEEPSEEK_API_KEY). None, если ключа нет / клиент не поднялся — вызывающий
    тогда фолбэчит на дефолтный роутер (Gemini). Реальный клиент, без заглушек."""
    key = ""
    if user_id:
        try:
            from backend.api.routes.integrations import get_user_integration_keys
            keys = await get_user_integration_keys(user_id, "deepseek") or {}
            key = str(keys.get("api_key") or "").strip()
        except Exception:
            logger.debug("analysis: deepseek integration key lookup failed", exc_info=True)
    if not key:
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return None
    try:
        from backend.core.llm.profile_client import ProfileBackedClient
        c = ProfileBackedClient(provider="deepseek", base_url=None,
                                model="deepseek-v4-pro", api_key=key)
        return c if getattr(c, "enabled", False) else None
    except Exception:
        logger.debug("analysis: deepseek client build failed", exc_info=True)
        return None


class _TenantJsonLLM:
    """generate_json уходит на тенантскую модель (DeepSeek v4 Pro), а не на
    дефолтный Gemini. Ключ — из «Интеграций»/env. При отсутствии ключа, ошибке
    или пустом/битом JSON — ФОЛБЭК на внутренний роутер (текущий Gemini), чтобы
    прогон не падал. Клиент резолвится лениво и кэшируется на прогон."""

    def __init__(self, inner: Any, user_id: Optional[str]):
        self._inner = inner
        self._uid = user_id
        self._client: Any = None
        self._resolved = False

    async def _ds(self) -> Any:
        if not self._resolved:
            self._resolved = True
            self._client = await _tenant_deepseek_client(self._uid)
        return self._client

    async def generate_json(self, prompt: str, **kwargs: Any) -> Any:
        c = await self._ds()
        if c is not None:
            try:
                out = await c.generate_json(
                    prompt,
                    system_prompt=kwargs.get("system_prompt"),
                    temperature=float(kwargs.get("temperature", 0.2) or 0.2),
                    max_tokens=int(kwargs.get("max_tokens", 8192) or 8192))
                if out not in (None, {}, []):
                    return out
            except Exception as e:
                logger.warning(
                    "analysis: DeepSeek generate_json failed → Gemini fallback: %s", e)
        return await self._inner.generate_json(prompt, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _default_deps() -> RunDeps:
    from backend.core.analysis.store import RunStore
    pg = None
    try:
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
    except Exception:
        pg = None
    store = RunStore(postgres=pg)

    async def _save(run: AnalysisRun) -> Any:
        return await store.save(run)

    return RunDeps(
        document_loader=_default_document_loader,
        llm=_default_llm(),
        save_run=_save,
    )


# === Chunking ==============================================================


def _chunk_text(text: str) -> list[str]:
    """Разбить текст на chunks. Переиспользует document_indexer splitter."""
    try:
        from backend.core.documents.document_indexer import split_text_into_chunks
        return split_text_into_chunks(text)
    except Exception:
        # Fallback: грубое деление по EXTRACT_BATCH_CHARS.
        return [text[i:i + 1500] for i in range(0, len(text), 1500)] or [""]


def _batch_chunks(chunks: list[str], *, batch_chars: int = EXTRACT_BATCH_CHARS) -> list[str]:
    """Сгруппировать chunks в батчи, каждый ≤ batch_chars (для одного LLM-вызова)."""
    batches: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for ch in chunks:
        if cur and cur_len + len(ch) > batch_chars:
            batches.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(ch)
        cur_len += len(ch)
    if cur:
        batches.append("\n\n".join(cur))
    return batches


# === Phase: INGEST =========================================================


async def _phase_ingest(
    run: AnalysisRun, deps: RunDeps,
) -> list[str]:
    """Загрузить документы, разбить на chunks. Возвращает плоский список chunks.

    Документы, которые не загрузились, логируются в steps_log и пропускаются.
    """
    run.status = RunStatus.PARSING.value
    all_chunks: list[str] = []
    loaded = 0
    for doc_id in run.input_document_ids:
        doc = await deps.document_loader(doc_id)
        if not doc:
            run.add_step("parsing", "document not found", {"document_id": doc_id})
            continue
        content = doc.get("content") or ""
        if not content.strip():
            run.add_step("parsing", "empty document", {"document_id": doc_id})
            continue
        chunks = _chunk_text(content)
        # Помечаем chunks источником для provenance в extract.
        title = doc.get("title") or doc_id
        tagged = [f"[doc: {title}]\n{c}" for c in chunks]
        all_chunks.extend(tagged)
        loaded += 1
        run.add_step("parsing", "document chunked", {
            "document_id": doc_id, "title": title,
            "chars": len(content), "chunks": len(chunks),
        })
    run.add_step("parsing", "ingest complete", {
        "documents_loaded": loaded, "total_chunks": len(all_chunks),
    })
    return all_chunks


# === Phase: SKELETON (реверсивный разбор — top-down) =======================


async def _phase_skeleton(
    run: AnalysisRun, playbook: AnalysisPlaybook,
    chunks: list[str], deps: RunDeps,
) -> None:
    """Реверсивный проход: ДО детального извлечения быстро смотрим на СКЕЛЕТ
    документа (оглавление/начало/конец) и выдвигаем ГИПОТЕЗЫ выводов и рисков.

    Дальше analyze проверяет эти гипотезы против извлечённых фактов (top-down),
    а не только собирает выводы снизу вверх. Включается флагом методики
    company_context.reverse_analysis. 1 дешёвый LLM-вызов на выборке чанков.
    """
    if not chunks:
        return
    # Выборка: начало + середина + конец (скелет документа), ~6 чанков.
    n = len(chunks)
    sample_idx = sorted({0, 1, n // 2, n - 2, n - 1, *range(min(2, n))})
    sample = "\n---\n".join(chunks[i] for i in sample_idx if 0 <= i < n)[:14000]
    dims = ", ".join(d.label for d in playbook.dimensions) or "ключевые аспекты"
    # Цифры компании (единые метрики план/факт + таблицы онтологии):
    # гипотезы сверяются с РЕАЛЬНЫМИ показателями, а не только с текстом
    # документа. Never-raise, за флагом онтологии.
    numbers_ctx = ""
    try:
        from backend.core.ontology.numbers_context import numbers_block
        if run.user_id:
            nb = numbers_block(run.user_id, dims + " " + playbook.name)
            if nb.get("text"):
                numbers_ctx = ("\n\nДЛЯ СВЕРКИ — " + nb["text"][:4000]
                               + "\nЕсли цифры документа расходятся с этими — "
                                 "это кандидат в гипотезу-риск.")
    except Exception:
        logger.debug("analysis numbers_block skipped", exc_info=True)
    prompt = (
        "Ты — старший аналитик. Перед детальным разбором посмотри на СКЕЛЕТ "
        "документа (фрагменты начала, середины и конца) и выдвинь ГИПОТЕЗЫ: "
        "какие выводы и риски ВЕРОЯТНЫ по этому документу — чтобы потом "
        "проверить их фактами. Ориентируйся на параметры методики: " + dims + ".\n\n"
        "Верни СТРОГО JSON-массив (3-6 гипотез):\n"
        '[{"claim": "<вероятный вывод/риск>", "why": "<на чём основана '
        'гипотеза>", "check": "<что нужно проверить в деталях>"}]\n'
        "Это ГИПОТЕЗЫ, не факты — их предстоит подтвердить или опровергнуть.\n\n"
        "СКЕЛЕТ ДОКУМЕНТА:\n" + sample + numbers_ctx
    )
    try:
        raw = await deps.llm.generate_json(
            prompt, system_prompt="Ты выдвигаешь проверяемые гипотезы. Только JSON-массив.",
            temperature=0.3,
        )
    except Exception as e:
        run.add_step("parsing", "skeleton pass failed (skip)", {"error": str(e)[:200]})
        return
    items = raw if isinstance(raw, list) else (raw.get("hypotheses") if isinstance(raw, dict) else None)
    hyps: list[dict[str, Any]] = []
    for it in (items or []):
        if isinstance(it, dict) and it.get("claim"):
            hyps.append({
                "claim": str(it.get("claim", ""))[:300],
                "why": str(it.get("why", ""))[:300],
                "check": str(it.get("check", ""))[:300],
            })
    run.hypotheses = hyps[:6]
    run.add_step("parsing", "reverse skeleton: hypotheses", {"count": len(run.hypotheses)})


def _reverse_enabled(playbook: AnalysisPlaybook) -> bool:
    cc = playbook.company_context or {}
    return bool(cc.get("reverse_analysis"))


# === Phase: EXTRACT (map-reduce) ===========================================


def _build_extract_prompt(playbook: AnalysisPlaybook, batch_text: str) -> str:
    """Промпт для извлечения фактов по всем dimensions из одного батча."""
    dim_lines = []
    for d in playbook.dimensions:
        hint = d.extraction_hint or d.label
        kind = "число/метрика" if d.kind == "quantitative" else "факт/риск"
        dim_lines.append(f'  - "{d.key}" ({d.label}, {kind}): {hint}')
    dims_block = "\n".join(dim_lines)

    return (
        "Ты — аналитик. Из ФРАГМЕНТА ДОКУМЕНТА извлеки релевантные факты "
        "по каждому параметру. Для количественных параметров извлекай ЧИСЛА "
        "(с единицами). Для качественных — краткие факты/риски с цитатой.\n\n"
        f"ПАРАМЕТРЫ:\n{dims_block}\n\n"
        "Верни СТРОГО JSON-объект вида:\n"
        '{ "<param_key>": [ {"value": <число или строка>, '
        '"quote": "<краткая цитата из текста>", "unit": "<ед.изм. или null>"} ] }\n'
        "Если по параметру в этом фрагменте ничего нет — пустой массив. "
        "Не выдумывай: бери только то, что есть в тексте.\n\n"
        f"ФРАГМЕНТ ДОКУМЕНТА:\n{batch_text}"
    )


def _coerce_facts(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Нормализовать ответ LLM в {key: [fact,...]}."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(raw, dict):
        return out
    for key, items in raw.items():
        if not isinstance(items, list):
            continue
        clean = []
        for it in items:
            if isinstance(it, dict) and ("value" in it or "quote" in it):
                clean.append({
                    "value": it.get("value"),
                    "quote": str(it.get("quote", ""))[:500],
                    "unit": it.get("unit"),
                })
        if clean:
            out[str(key)] = clean
    return out


async def _phase_extract(
    run: AnalysisRun, playbook: AnalysisPlaybook,
    chunks: list[str], deps: RunDeps,
) -> dict[str, list[dict[str, Any]]]:
    """Map-reduce извлечение фактов по dimensions из всех chunks."""
    run.status = RunStatus.EXTRACTING.value
    if not playbook.dimensions:
        run.add_step("extracting", "no dimensions in playbook", None)
        return {}

    batches = _batch_chunks(chunks)
    run.add_step("extracting", "batched chunks", {
        "batches": len(batches), "chunks": len(chunks),
    })

    merged: dict[str, list[dict[str, Any]]] = {d.key: [] for d in playbook.dimensions}

    for idx, batch in enumerate(batches):
        prompt = _build_extract_prompt(playbook, batch)
        try:
            # temperature=0: извлечение фактов/ЦИФР должно быть
            # детерминированным (на дефолтных 0.7 числа «плавали» между
            # прогонами одного документа).
            raw = await deps.llm.generate_json(
                prompt,
                system_prompt="Ты извлекаешь структурированные факты. Только JSON.",
                temperature=0.0,
            )
        except Exception as e:
            run.add_step("extracting", "batch llm failed (skip)", {
                "batch": idx, "error": str(e)[:200],
            })
            continue
        facts = _coerce_facts(raw)
        added = 0
        for key, items in facts.items():
            if key in merged:
                merged[key].extend(items)
                added += len(items)
        run.add_step("extracting", "batch extracted", {
            "batch": idx, "facts_added": added,
        })

    # Дедуп фактов внутри dimension: один и тот же факт мог всплыть в разных
    # батчах (перекрытие/повтор) — без дедупа compute суммировал бы дубли
    # (двойной счёт). Ключ дедупа: (нормализованное value, начало цитаты).
    def _fact_key(f: dict[str, Any]) -> tuple:
        val = str(f.get("value", "")).strip().lower()
        quote = str(f.get("quote", "")).strip().lower()[:80]
        return (val, quote)

    deduped: dict[str, list[dict[str, Any]]] = {}
    dropped = 0
    for key, items in merged.items():
        if not items:
            continue
        seen: set = set()
        uniq: list[dict[str, Any]] = []
        for f in items:
            fk = _fact_key(f)
            if fk in seen:
                dropped += 1
                continue
            seen.add(fk)
            uniq.append(f)
        deduped[key] = uniq

    result = deduped
    run.extracted_facts = result
    if dropped:
        run.add_step("extracting", "deduped facts", {"dropped_duplicates": dropped})
    run.add_step("extracting", "extract complete", {
        "dimensions_with_facts": len(result),
        "total_facts": sum(len(v) for v in result.values()),
    })
    return result


# === Phase: ASSEMBLE (базовый — Фаза D обогатит) ===========================


def _assemble_basic_report(
    run: AnalysisRun, playbook: AnalysisPlaybook,
) -> dict[str, Any]:
    """Собрать базовый markdown-отчёт из extracted_facts + computed_metrics.

    Фаза D заменит это на LLM-генерацию по output_template + reference few-shot.
    Здесь — детерминированная выжимка, чтобы run доходил до done с видимым
    результатом уже после Фазы B.
    """
    lines: list[str] = [f"# Аналитический разбор: {playbook.name}", ""]
    if run.client_context:
        lines += [f"**Контекст клиента:** {run.client_context}", ""]
    lines += [f"_Документов проанализировано: {len(run.input_document_ids)}_", ""]

    for d in playbook.dimensions:
        lines.append(f"## {d.label}")
        if d.rationale:
            lines.append(f"_{d.rationale}_")
        facts = run.extracted_facts.get(d.key, [])
        metric = run.computed_metrics.get(d.key)
        if metric is not None:
            status = metric.get("benchmark_status", "")
            lines.append(f"- **Расчёт:** {metric.get('value')} {('('+status+')') if status else ''}")
        if facts:
            for f in facts[:10]:
                val = f.get("value")
                unit = f.get("unit") or ""
                quote = f.get("quote", "")
                lines.append(f"- {val} {unit} — «{quote}»".rstrip())
        elif metric is None:
            lines.append("- _данные не найдены в документах_")
        lines.append("")

    markdown = "\n".join(lines)
    return {
        "markdown": markdown,
        "dimensions": [d.key for d in playbook.dimensions],
        "generated_by": "phase_b_basic",
    }


# === Orchestrator ==========================================================


async def run_pipeline(
    run: AnalysisRun, playbook: AnalysisPlaybook, deps: RunDeps,
) -> AnalysisRun:
    """Прогнать run через петлю. never-raises: ошибка → status=failed + error."""
    # Привязка LLM-контекста к пользователю: раньше analysis-слой нигде не
    # ставил контекст → все разборы списывались на user_id=None ('unknown'),
    # а юзерская квота (assert_within_quota) не применялась. run исполняется
    # в фоне (своя copy-контекста), поэтому ставим ВНУТРИ петли.
    try:
        from backend.core.llm.router import set_llm_context
        set_llm_context(user_id=run.user_id or "", agent_mode="analysis")
    except Exception:
        logger.debug("set_llm_context skipped", exc_info=True)
    try:
        # INGEST
        chunks = await _phase_ingest(run, deps)
        await deps.save_run(run)
        if not chunks:
            run.status = RunStatus.FAILED.value
            run.error = "no content extracted from documents"
            await deps.save_run(run)
            return run

        # Тир модели на этот прогон (standard экономичный / premium тяжёлый):
        # оборачиваем llm-деп, чтобы все фазы ушли на выбранный тир. Это фолбэк-
        # роутер (Gemini), поэтому тир применяем ВНУТРИ, до тенантской обёртки.
        if getattr(run, "model_tier", "standard") == "premium":
            deps = RunDeps(document_loader=deps.document_loader,
                           llm=_TieredLLM(deps.llm, "premium"),
                           save_run=deps.save_run)

        # Разбор гоним ТЕНАНТСКОЙ моделью (DeepSeek v4 Pro) вместо дефолтного
        # Gemini — как и стандарт платформы. Обёртка внешняя: сперва DeepSeek,
        # при любой проблеме (нет ключа / ошибка / битый JSON) — фолбэк на
        # роутер выше. За флагом ANALYSIS_TENANT_DEEPSEEK (дефолт ON).
        if _tenant_model_enabled():
            deps = RunDeps(document_loader=deps.document_loader,
                           llm=_TenantJsonLLM(deps.llm, run.user_id),
                           save_run=deps.save_run)

        # SKELETON (реверсивный проход — top-down гипотезы) — опционально
        if _reverse_enabled(playbook):
            await _phase_skeleton(run, playbook, chunks, deps)
            await deps.save_run(run)

        # PLAN — ИИ сам решает, что ещё читать и что считать в ЭТОМ документе
        # (динамические измерения + safe-calc формулы; см. planning.py)
        try:
            from backend.core.analysis.planning import phase_plan, plan_enabled
            if plan_enabled(playbook):
                playbook = await phase_plan(run, playbook, chunks, deps)
                await deps.save_run(run)
        except Exception as e:
            logger.warning("plan phase failed for %s: %s", run.id, e)
            run.add_step("parsing", "plan failed (skip)", {"error": str(e)[:200]})

        # EXTRACT
        await _phase_extract(run, playbook, chunks, deps)
        await deps.save_run(run)

        # NUMERIC AUDIT — сходятся ли СОБСТВЕННЫЕ расчёты документа
        # (LLM находит заявленные вычисления, пересчитывает КОД)
        try:
            from backend.core.analysis.numeric_audit import phase_numeric_audit
            await phase_numeric_audit(run, chunks, deps)
            await deps.save_run(run)
        except Exception as e:
            logger.warning("numeric audit failed for %s: %s", run.id, e)
            run.add_step("computing", "numeric audit failed (skip)",
                         {"error": str(e)[:200]})

        # COMPUTE + ANALYZE — Фаза C (заглушка: пропускаем, метрики пустые).
        # Хук оставлен явно, чтобы Фаза C встроилась без изменения orchestrator'а.
        try:
            from backend.core.analysis.compute import run_compute_phase
            run.status = RunStatus.COMPUTING.value
            await run_compute_phase(run, playbook)
            await deps.save_run(run)
        except Exception as e:
            # Раньше — logger.debug: сбой расчёта молча маскировался под
            # «метрик нет». Теперь явный след в steps_log, чтобы пустые
            # computed_metrics не читались как «данных не было».
            logger.warning("compute phase failed for %s: %s", run.id, e)
            run.add_step("computing", "compute failed", {"error": str(e)[:200]})

        try:
            from backend.core.analysis.analyze import run_analyze_phase
            run.status = RunStatus.ANALYZING.value
            await run_analyze_phase(run, playbook, deps)
            await deps.save_run(run)
        except Exception as e:
            logger.warning("analyze phase failed for %s: %s", run.id, e)
            run.add_step("analyzing", "analyze failed", {"error": str(e)[:200]})

        # ASSEMBLE — базовый (Фаза D обогатит через template + few-shot).
        run.status = RunStatus.ASSEMBLING.value
        report = None
        try:
            from backend.core.analysis.assemble import assemble_report
            report = await assemble_report(run, playbook, deps)
        except Exception as e:
            logger.debug("assemble phase not available yet, using basic: %s", e)
        if report is None:
            report = _assemble_basic_report(run, playbook)
        # Честная шапка «что проанализировано» — В ЛЮБОМ отчёте (и LLM, и
        # basic). Инцидент: пользователь запустил прогон по 5k-конспекту, а
        # 143k-PDF с цифрами загрузил ПОСЛЕ старта — отчёт писал «данных о
        # стоимости нет», и было не видно, что главный документ в прогон
        # не попал. Теперь список источников с объёмами — первой строкой.
        try:
            _src_lines = []
            for st in (run.steps_log or []):
                _d = st.get("detail") or {}
                if not isinstance(_d, dict):
                    continue
                if st.get("action") == "document chunked":
                    _src_lines.append(
                        f"- {_d.get('title')} — {int(_d.get('chars') or 0):,}".replace(",", " ")
                        + " знаков")
                elif st.get("action") == "document not found":
                    _src_lines.append(f"- ⚠️ не найден: {_d.get('document_id')}")
                elif st.get("action") == "empty document":
                    _src_lines.append(f"- ⚠️ пустой: {_d.get('document_id')}")
            if _src_lines and isinstance(report, dict):
                _hdr = ("## Проанализировано\n" + "\n".join(_src_lines)
                        + "\n\n_Если нужного документа нет в списке — он не "
                          "был выбран в прогон; перезапустите анализ, отметив "
                          "его._\n\n")
                for _k in ("markdown", "report", "text", "content"):
                    if isinstance(report.get(_k), str):
                        report[_k] = _hdr + report[_k]
                        break
                report["sources_analyzed"] = _src_lines
        except Exception:
            logger.debug("sources header skipped", exc_info=True)
        run.final_report = report
        run.add_step("assembling", "report assembled", {
            "generated_by": report.get("generated_by"),
        })

        run.status = RunStatus.DONE.value
        await deps.save_run(run)
        return run
    except Exception as e:
        logger.warning("run_pipeline failed for %s: %s", run.id, e)
        run.status = RunStatus.FAILED.value
        run.error = str(e)[:500]
        run.add_step("failed", "pipeline error", {"error": str(e)[:200]})
        try:
            await deps.save_run(run)
        except Exception:
            logger.debug("save after failure suppressed", exc_info=True)
        return run


async def start_analysis_run(
    *, user_id: str, playbook_id: str, document_ids: list[str],
    client_context: str = "", org_id: Optional[str] = None,
) -> Optional[str]:
    """Создать AnalysisRun и запустить в фоне. Возвращает run_id или None.

    Переиспользуется АВТОМАТИЗАЦИЯМИ (запуск методики по расписанию/событию):
    результат ложится в ту же историю runs, что и ручные разборы.
    """
    from backend.core.analysis.models import AnalysisRun, RunStatus
    from backend.core.analysis.store import PlaybookStore, RunStore
    try:
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
    except Exception:
        pg = None
    pb = await PlaybookStore(postgres=pg).get(playbook_id)
    if pb is None:
        logger.warning("start_analysis_run: playbook %s not found", playbook_id)
        return None
    docs = [str(d) for d in (document_ids or []) if str(d).strip()]
    if not docs:
        logger.warning("start_analysis_run: no document_ids")
        return None
    run = AnalysisRun(
        id=AnalysisRun.new_id(), playbook_id=playbook_id,
        playbook_version=pb.version, org_id=org_id or pb.org_id,
        user_id=user_id, input_document_ids=docs,
        client_context=client_context or None, status=RunStatus.PENDING.value,
    )
    run.add_step("created", "run queued by automation", {"documents": len(docs)})
    saved = await RunStore(postgres=pg).create(run)
    await dispatch_run_background(saved.id)
    return saved.id


async def dispatch_run(run_id: str, *, deps: Optional[RunDeps] = None) -> None:
    """Точка входа из route. Загружает run + playbook, гонит петлю.

    Синхронно в рамках запроса для MVP (run на 100 страниц = минуты;
    для prod вынести в taskiq/background worker — отдельный тикет).
    """
    if deps is None:
        deps = await _default_deps()

    # Загрузить run + playbook.
    from backend.core.analysis.store import PlaybookStore, RunStore
    pg = None
    try:
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
    except Exception:
        pg = None
    run_store = RunStore(postgres=pg)
    pb_store = PlaybookStore(postgres=pg)

    run = await run_store.get(run_id)
    if run is None:
        logger.warning("dispatch_run: run %s not found", run_id)
        return
    # Дефолтный загрузчик документов привязывается к владельцу run'а:
    # документы читаются service-ключом, и без этого фильтра прогон мог бы
    # прочитать документ другого тенанта по угаданному id.
    if deps.document_loader is _default_document_loader:
        import functools
        deps = RunDeps(
            document_loader=functools.partial(
                _default_document_loader, owner_user_id=run.user_id),
            llm=deps.llm, save_run=deps.save_run,
        )
    playbook = await pb_store.get(run.playbook_id)
    if playbook is None:
        run.status = RunStatus.FAILED.value
        run.error = "playbook not found"
        await run_store.save(run)
        return

    await run_pipeline(run, playbook, deps)


# === Background dispatch ====================================================

# Держим ссылки на fire-and-forget задачи, иначе event loop может
# garbage-collect'нуть task до завершения (asyncio docs рекомендуют).
_BACKGROUND_TASKS: set = set()


async def dispatch_run_background(run_id: str) -> str:
    """Запустить run в фоне, не блокируя caller'а (HTTP-запрос).

    Стратегия:
      1. По умолчанию — asyncio.create_task (in-process): исполняется всегда,
         даже если отдельный taskiq-worker не запущен. Не durable, но не
         блокирует HTTP на минуты (паттерн как в knowledge_sync).
      2. taskiq (durable, Redis) — ТОЛЬКО при ANALYSIS_DISPATCH=taskiq, т.е.
         когда деплой реально поднимает `taskiq worker`. Иначе задача просто
         повисла бы в Redis без потребителя (run застревает в «В очереди»).

    Returns: "taskiq" | "asyncio" — каким каналом запущено.
    """
    # 1. taskiq — durable, но требует запущенного worker'а. Включаем явно.
    import os
    if os.getenv("ANALYSIS_DISPATCH", "asyncio").strip().lower() == "taskiq":
        try:
            from backend.workers.taskiq_app import broker  # type: ignore
            analysis_task = getattr(
                __import__("backend.workers.taskiq_app", fromlist=["analysis_run_task"]),
                "analysis_run_task", None,
            )
            if broker is not None and analysis_task is not None:
                await analysis_task.kiq(run_id)
                return "taskiq"
        except Exception as e:
            logger.debug("taskiq dispatch unavailable, using asyncio: %s", e)

    # 2. По умолчанию: in-process asyncio task — исполняется здесь и сейчас.
    import asyncio

    async def _runner():
        try:
            await dispatch_run(run_id)
        except Exception as e:
            logger.warning("background run %s failed: %s", run_id, e)

    task = asyncio.create_task(_runner())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return "asyncio"
