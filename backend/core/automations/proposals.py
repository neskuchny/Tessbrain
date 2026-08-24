# -*- coding: utf-8 -*-
"""Предложения автоматизации (SIMA P2, звенья A/B/C бизнес-цепочки).

Tessbrain замечает потребность в автоматизации → сохраняет её как СТРУКТУРНЫЙ
объект (не только Markdown-отчёт) → показывает пользователю с «Принять/
Отклонить». На «Принять» из полей предложения ДЕТЕРМИНИРОВАННО собирается ТЗ
(без выдумки) и заводится проект в SIMA через seed_project_from_spec (звено D).

Таблица: automation_proposals (миграция 266). Доступ — общий PostgresClient.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

import structlog
from sqlalchemy import text

from backend.db.postgres import get_postgres

logger = structlog.get_logger(__name__)

_ALLOWED_STATUS = {"pending", "accepted", "dismissed"}


def _row(r: Any) -> dict:
    """DB-строка → camelCase-словарь для фронта."""
    m = dict(r._mapping)
    return {
        "id": str(m.get("id") or ""),
        "userId": str(m.get("user_id") or ""),
        "title": m.get("title") or "",
        "problem": m.get("problem") or "",
        "proposedAction": m.get("proposed_action") or "",
        "expectedBenefit": m.get("expected_benefit") or "",
        "evidence": m.get("evidence") or "",
        "source": m.get("source") or "manual",
        "status": m.get("status") or "pending",
        "simaProjectId": str(m["sima_project_id"]) if m.get("sima_project_id") else None,
        "createdAt": m.get("created_at").isoformat() if m.get("created_at") else None,
    }


def build_spec_text(p: dict) -> str:
    """Детерминированное ТЗ из полей предложения — честно, без выдумки.

    Пустые поля просто пропускаются (не заполняем водой). Это ТЗ уходит в
    seed_project_from_spec, где LLM раскладывает его в блоки/связи.
    """
    title = (p.get("title") or "").strip() or "Автоматизация процесса"
    parts: list[str] = [f"# ТЗ: {title}", ""]
    if (p.get("problem") or "").strip():
        parts += ["## Проблема / зачем автоматизировать", p["problem"].strip(), ""]
    if (p.get("proposedAction") or p.get("proposed_action") or "").strip():
        parts += ["## Что предлагается сделать",
                  (p.get("proposedAction") or p.get("proposed_action")).strip(), ""]
    if (p.get("expectedBenefit") or p.get("expected_benefit") or "").strip():
        parts += ["## Ожидаемый результат / польза",
                  (p.get("expectedBenefit") or p.get("expected_benefit")).strip(), ""]
    if (p.get("evidence") or "").strip():
        parts += ["## Обоснование (данные компании)", p["evidence"].strip(), ""]
    parts += [
        "## Требования к продукту",
        "- Реши описанную проблему; закрой перечисленные шаги процесса.",
        "- Опиши вход/выход и ключевые сценарии.",
        "- Учти интеграции с существующими системами компании, если упомянуты.",
    ]
    return "\n".join(parts).strip()


async def create_proposal(
    user_id: str,
    title: str,
    *,
    problem: str = "",
    proposed_action: str = "",
    expected_benefit: str = "",
    evidence: str = "",
    source: str = "manual",
) -> Optional[dict]:
    """Создать предложение (детектор или ручное). Дедуп: одинаковый title в
    статусе pending на пользователя не плодится (частичный уникальный индекс) —
    возвращаем существующее.
    """
    title = (title or "").strip()
    if not title:
        return None
    pg = await get_postgres()
    pid = str(uuid.uuid4())
    async with pg.session() as session:
        res = await session.execute(
            text("""
                INSERT INTO automation_proposals
                    (id, user_id, title, problem, proposed_action,
                     expected_benefit, evidence, source)
                VALUES (:id, :uid, :title, :problem, :action, :benefit, :ev, :src)
                ON CONFLICT (user_id, title) WHERE status = 'pending'
                DO NOTHING
                RETURNING *
            """),
            {"id": pid, "uid": user_id, "title": title, "problem": problem or None,
             "action": proposed_action or None, "benefit": expected_benefit or None,
             "ev": evidence or None, "src": source or "manual"},
        )
        row = res.fetchone()
        if row:
            return {**_row(row), "_created": True}  # реально вставили новое
        # дубль pending — вернём существующий (не новое)
        res2 = await session.execute(
            text("""SELECT * FROM automation_proposals
                    WHERE user_id = :uid AND title = :title AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1"""),
            {"uid": user_id, "title": title},
        )
        row2 = res2.fetchone()
        return {**_row(row2), "_created": False} if row2 else None


_MISSING_TABLE_WARNED = False


def _is_missing_table(exc: BaseException) -> bool:
    """True, если ошибка = «таблицы automation_proposals нет» (миграция 266
    не применена). asyncpg.UndefinedTableError → SQLSTATE 42P01."""
    sqlstate = getattr(getattr(exc, "orig", exc), "sqlstate", "") or ""
    if sqlstate == "42P01":
        return True
    return "automation_proposals" in str(exc) and "does not exist" in str(exc)


async def list_proposals(user_id: str, status: str = "pending", limit: int = 50) -> list[dict]:
    """Список предложений пользователя по статусу (свежие сверху).

    Мягко деградирует, если таблицы ещё нет (миграция 266 не применена):
    возвращаем пустой список вместо 500 — «Очередь задач» показывает «пусто»,
    а не роняет UI и не засыпает лог трейсбеками на каждый опрос. Фича
    включается применением `python -m backend.db.migrate`.
    """
    global _MISSING_TABLE_WARNED
    pg = await get_postgres()
    try:
        async with pg.session() as session:
            res = await session.execute(
                text("""SELECT * FROM automation_proposals
                        WHERE user_id = :uid AND status = :st
                        ORDER BY created_at DESC LIMIT :lim"""),
                {"uid": user_id, "st": status, "lim": max(1, min(limit, 200))},
            )
            return [_row(r) for r in res.fetchall()]
    except Exception as exc:  # noqa: BLE001 — классифицируем ниже
        if not _is_missing_table(exc):
            raise
        if not _MISSING_TABLE_WARNED:
            logger.warning(
                "automation_proposals table missing — feature disabled. "
                "Apply migrations: python -m backend.db.migrate",
            )
            _MISSING_TABLE_WARNED = True
        return []


async def get_proposal(proposal_id: str, user_id: str) -> Optional[dict]:
    pg = await get_postgres()
    async with pg.session() as session:
        res = await session.execute(
            text("""SELECT * FROM automation_proposals
                    WHERE id = :id AND user_id = :uid"""),
            {"id": proposal_id, "uid": user_id},
        )
        row = res.fetchone()
        return _row(row) if row else None


async def mark_accepted(proposal_id: str, user_id: str, sima_project_id: str) -> Optional[dict]:
    """Пометить принятым + связать с созданным проектом SIMA. Атомарно:
    переводим только из pending (защита от двойного accept)."""
    pg = await get_postgres()
    async with pg.session() as session:
        res = await session.execute(
            text("""UPDATE automation_proposals
                    SET status = 'accepted', sima_project_id = :pid, updated_at = now()
                    WHERE id = :id AND user_id = :uid AND status = 'pending'
                    RETURNING *"""),
            {"id": proposal_id, "uid": user_id, "pid": sima_project_id},
        )
        row = res.fetchone()
        return _row(row) if row else None


async def mark_dismissed(proposal_id: str, user_id: str) -> Optional[dict]:
    pg = await get_postgres()
    async with pg.session() as session:
        res = await session.execute(
            text("""UPDATE automation_proposals
                    SET status = 'dismissed', updated_at = now()
                    WHERE id = :id AND user_id = :uid AND status = 'pending'
                    RETURNING *"""),
            {"id": proposal_id, "uid": user_id},
        )
        row = res.fetchone()
        return _row(row) if row else None
