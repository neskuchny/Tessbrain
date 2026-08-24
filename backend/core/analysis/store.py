# -*- coding: utf-8 -*-
"""Storage для playbook'ов и run'ов (Postgres + in-memory fallback).

Паттерн зеркалит MessengerLinkService: при `postgres=None` (dev/тесты)
работает на in-memory dict'ах; с живым PG — пишет в таблицы
analysis_playbooks / analysis_runs (миграция 260).

Org-scoped: playbook'и принадлежат org (Wave 2). list/get фильтруются
по org_id. RBAC по access_level применяется на уровне route (P1 #5).

Все методы best-effort never-raise при сетевых сбоях БД: падают на
in-memory fallback с warning'ом (как остальной store-слой проекта).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from backend.core.analysis.models import AnalysisPlaybook, AnalysisRun

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(v: Any) -> Any:
    """ISO-строку → datetime для timestamp-колонок Postgres.

    asyncpg НЕ принимает str для timestamptz (DataError), а run.completed_at
    хранится ISO-строкой — иначе сохранение результата анализа падало и
    откатывалось в память (история терялась)."""
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except Exception:
        return None


# In-memory fallback ДОЛЖЕН быть общим на процесс: роуты создают новый
# PlaybookStore/RunStore на КАЖДЫЙ запрос (analysis.py:_playbook_store/_run_store),
# а раньше _mem был инстанс-атрибутом → без Postgres созданный run исчезал уже
# к следующему запросу (поллинг статуса → 404, весь анализ «молча не работал»).
# Общий модуль-уровневый dict переживает между запросами внутри процесса.
_PLAYBOOK_MEM: dict[str, "AnalysisPlaybook"] = {}
_RUN_MEM: dict[str, "AnalysisRun"] = {}


# === Playbook store ========================================================


class PlaybookStore:
    """CRUD над analysis_playbooks."""

    def __init__(self, *, postgres: Any = None) -> None:
        self.postgres = postgres
        self._mem = _PLAYBOOK_MEM  # общий на процесс (см. коммент выше)

    async def create(self, playbook: AnalysisPlaybook) -> AnalysisPlaybook:
        if not playbook.id:
            playbook.id = AnalysisPlaybook.new_id()
        playbook.created_at = playbook.created_at or _now_iso()
        playbook.updated_at = _now_iso()

        if self.postgres is None:
            self._mem[playbook.id] = playbook
            return playbook
        try:
            from sqlalchemy import text
            async with self.postgres.session() as session:
                await session.execute(
                    text("""
                        INSERT INTO public.analysis_playbooks
                          (id, org_id, name, analysis_type, description,
                           dimensions, company_context, reference_report,
                           output_template, created_by, access_level, version,
                           created_at, updated_at)
                        VALUES
                          (:id, :org_id, :name, :atype, :descr,
                           :dims, :cctx, :ref, :tmpl, :cby, :alvl, :ver,
                           now(), now())
                    """),
                    {
                        "id": playbook.id,
                        "org_id": playbook.org_id,
                        "name": playbook.name,
                        "atype": playbook.analysis_type,
                        "descr": playbook.description,
                        "dims": json.dumps([d.to_dict() for d in playbook.dimensions], ensure_ascii=False),
                        "cctx": json.dumps(playbook.company_context, ensure_ascii=False),
                        "ref": playbook.reference_report,
                        "tmpl": json.dumps(playbook.output_template, ensure_ascii=False),
                        "cby": playbook.created_by,
                        "ver": playbook.version,
                        "alvl": playbook.access_level,
                    },
                )
        except Exception as exc:
            logger.warning("playbook create failed (mem fallback): %s", exc)
            self._mem[playbook.id] = playbook
        return playbook

    async def get(self, playbook_id: str) -> Optional[AnalysisPlaybook]:
        if not playbook_id:
            return None
        if self.postgres is None:
            return self._mem.get(playbook_id)
        try:
            from sqlalchemy import text
            async with self.postgres.session() as session:
                res = await session.execute(
                    text("SELECT * FROM public.analysis_playbooks WHERE id = :id"),
                    {"id": playbook_id},
                )
                row = res.mappings().first() if hasattr(res, "mappings") else None
                if row:
                    return _playbook_from_row(dict(row))
                return None
        except Exception as exc:
            logger.warning("playbook get failed (mem fallback): %s", exc)
            return self._mem.get(playbook_id)

    async def list_for_org(self, org_id: Optional[str]) -> list[AnalysisPlaybook]:
        if self.postgres is None:
            return [p for p in self._mem.values() if p.org_id == org_id]
        try:
            from sqlalchemy import text
            async with self.postgres.session() as session:
                res = await session.execute(
                    text("""
                        SELECT * FROM public.analysis_playbooks
                        WHERE org_id IS NOT DISTINCT FROM :org_id
                        ORDER BY updated_at DESC
                    """),
                    {"org_id": org_id},
                )
                rows = res.mappings().all() if hasattr(res, "mappings") else []
                return [_playbook_from_row(dict(r)) for r in rows]
        except Exception as exc:
            logger.warning("playbook list failed (mem fallback): %s", exc)
            return [p for p in self._mem.values() if p.org_id == org_id]

    async def update(self, playbook: AnalysisPlaybook) -> AnalysisPlaybook:
        playbook.updated_at = _now_iso()
        playbook.version = int(playbook.version or 1) + 1  # инкремент при правке
        if self.postgres is None:
            self._mem[playbook.id] = playbook
            return playbook
        try:
            from sqlalchemy import text
            async with self.postgres.session() as session:
                await session.execute(
                    text("""
                        UPDATE public.analysis_playbooks SET
                          name=:name, analysis_type=:atype, description=:descr,
                          dimensions=:dims, company_context=:cctx,
                          reference_report=:ref, output_template=:tmpl,
                          access_level=:alvl, version=:ver, updated_at=now()
                        WHERE id=:id
                    """),
                    {
                        "id": playbook.id, "name": playbook.name,
                        "atype": playbook.analysis_type, "descr": playbook.description,
                        "dims": json.dumps([d.to_dict() for d in playbook.dimensions], ensure_ascii=False),
                        "cctx": json.dumps(playbook.company_context, ensure_ascii=False),
                        "ref": playbook.reference_report,
                        "tmpl": json.dumps(playbook.output_template, ensure_ascii=False),
                        "alvl": playbook.access_level,
                        "ver": playbook.version,
                    },
                )
        except Exception as exc:
            logger.warning("playbook update failed (mem fallback): %s", exc)
            self._mem[playbook.id] = playbook
        return playbook

    async def delete(self, playbook_id: str) -> bool:
        if self.postgres is None:
            return self._mem.pop(playbook_id, None) is not None
        try:
            from sqlalchemy import text
            async with self.postgres.session() as session:
                res = await session.execute(
                    text("DELETE FROM public.analysis_playbooks WHERE id=:id"),
                    {"id": playbook_id},
                )
                return int(getattr(res, "rowcount", 0) or 0) > 0
        except Exception as exc:
            logger.warning("playbook delete failed (mem fallback): %s", exc)
            return self._mem.pop(playbook_id, None) is not None


# === Run store =============================================================


class RunStore:
    """CRUD над analysis_runs."""

    def __init__(self, *, postgres: Any = None) -> None:
        self.postgres = postgres
        self._mem = _RUN_MEM  # общий на процесс (см. коммент выше)

    async def create(self, run: AnalysisRun) -> AnalysisRun:
        if not run.id:
            run.id = AnalysisRun.new_id()
        run.created_at = run.created_at or _now_iso()
        if self.postgres is None:
            self._mem[run.id] = run
            return run
        try:
            from sqlalchemy import text
            async with self.postgres.session() as session:
                await session.execute(
                    text("""
                        INSERT INTO public.analysis_runs
                          (id, playbook_id, playbook_version, org_id, user_id,
                           input_document_ids, client_context, status, steps_log,
                           extracted_facts, computed_metrics, final_report, error,
                           created_at)
                        VALUES
                          (:id, :pid, :pver, :org_id, :uid, :docs, :cctx, :status,
                           :steps, :facts, :metrics, :report, :err, now())
                    """),
                    _run_params(run),
                )
        except Exception as exc:
            logger.warning("run create failed (mem fallback): %s", exc)
            self._mem[run.id] = run
        return run

    async def get(self, run_id: str) -> Optional[AnalysisRun]:
        if not run_id:
            return None
        if self.postgres is None:
            return self._mem.get(run_id)
        try:
            from sqlalchemy import text
            async with self.postgres.session() as session:
                res = await session.execute(
                    text("SELECT * FROM public.analysis_runs WHERE id=:id"),
                    {"id": run_id},
                )
                row = res.mappings().first() if hasattr(res, "mappings") else None
                return _run_from_row(dict(row)) if row else None
        except Exception as exc:
            logger.warning("run get failed (mem fallback): %s", exc)
            return self._mem.get(run_id)

    async def list_for_org(
        self, org_id: Optional[str], *, limit: int = 50,
    ) -> list[AnalysisRun]:
        if self.postgres is None:
            runs = [r for r in self._mem.values() if r.org_id == org_id]
            runs.sort(key=lambda r: r.created_at, reverse=True)
            return runs[:limit]
        try:
            from sqlalchemy import text
            async with self.postgres.session() as session:
                res = await session.execute(
                    text("""
                        SELECT * FROM public.analysis_runs
                        WHERE org_id IS NOT DISTINCT FROM :org_id
                        ORDER BY created_at DESC LIMIT :lim
                    """),
                    {"org_id": org_id, "lim": limit},
                )
                rows = res.mappings().all() if hasattr(res, "mappings") else []
                return [_run_from_row(dict(r)) for r in rows]
        except Exception as exc:
            logger.warning("run list failed (mem fallback): %s", exc)
            runs = [r for r in self._mem.values() if r.org_id == org_id]
            runs.sort(key=lambda r: r.created_at, reverse=True)
            return runs[:limit]

    async def save(self, run: AnalysisRun) -> AnalysisRun:
        """Полностью перезаписать run (после каждого шага петли)."""
        if run.is_terminal() and not run.completed_at:
            run.completed_at = _now_iso()
        if self.postgres is None:
            self._mem[run.id] = run
            return run
        try:
            from sqlalchemy import text
            async with self.postgres.session() as session:
                await session.execute(
                    text("""
                        UPDATE public.analysis_runs SET
                          status=:status, steps_log=:steps,
                          extracted_facts=:facts, computed_metrics=:metrics,
                          final_report=:report, error=:err,
                          completed_at=:completed
                        WHERE id=:id
                    """),
                    {**_run_params(run), "completed": _parse_dt(run.completed_at)},
                )
        except Exception as exc:
            logger.warning("run save failed (mem fallback): %s", exc)
            self._mem[run.id] = run
        return run


# === Row mappers ===========================================================


def _maybe_json(v: Any, default: Any) -> Any:
    """PG может вернуть JSONB как dict/list (asyncpg) или str. Нормализуем."""
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return default
    return default


def _playbook_from_row(row: dict[str, Any]) -> AnalysisPlaybook:
    return AnalysisPlaybook.from_dict({
        "id": row.get("id"),
        "name": row.get("name"),
        "analysis_type": row.get("analysis_type"),
        "description": row.get("description"),
        "org_id": row.get("org_id"),
        "dimensions": _maybe_json(row.get("dimensions"), []),
        "company_context": _maybe_json(row.get("company_context"), {}),
        "reference_report": row.get("reference_report"),
        "output_template": _maybe_json(row.get("output_template"), {}),
        "created_by": row.get("created_by"),
        "access_level": row.get("access_level", 2),
        "version": row.get("version", 1),
        "created_at": str(row.get("created_at", "")),
        "updated_at": str(row.get("updated_at", "")),
    })


def _run_from_row(row: dict[str, Any]) -> AnalysisRun:
    return AnalysisRun.from_dict({
        "id": row.get("id"),
        "playbook_id": row.get("playbook_id"),
        "playbook_version": row.get("playbook_version", 1),
        "org_id": row.get("org_id"),
        "user_id": row.get("user_id"),
        "input_document_ids": _maybe_json(row.get("input_document_ids"), []),
        "client_context": row.get("client_context"),
        "status": row.get("status"),
        "steps_log": _maybe_json(row.get("steps_log"), []),
        "extracted_facts": _maybe_json(row.get("extracted_facts"), {}),
        "computed_metrics": _maybe_json(row.get("computed_metrics"), {}),
        "final_report": _maybe_json(row.get("final_report"), None),
        "error": row.get("error"),
        "created_at": str(row.get("created_at", "")),
        "completed_at": row.get("completed_at"),
    })


def _run_params(run: AnalysisRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "pid": run.playbook_id,
        "pver": run.playbook_version,
        "org_id": run.org_id,
        "uid": run.user_id,
        "docs": json.dumps(run.input_document_ids, ensure_ascii=False),
        "cctx": run.client_context,
        "status": run.status,
        "steps": json.dumps(run.steps_log, ensure_ascii=False),
        "facts": json.dumps(run.extracted_facts, ensure_ascii=False),
        "metrics": json.dumps(run.computed_metrics, ensure_ascii=False),
        "report": json.dumps(run.final_report, ensure_ascii=False) if run.final_report else None,
        "err": run.error,
    }
