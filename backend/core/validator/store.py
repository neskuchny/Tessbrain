"""ValidationResultService — Postgres persistence для validation reports (W23).

Дополняет Redis-based TaskHandle store: validator results живут постоянно
для аудита, аналитики и human-review queue.

Дизайн:
- Best-effort: при отсутствии Postgres методы возвращают пустые/None
  и логируют — caller продолжает работу
- Никаких raises на read/write
- artifacts_summary не leak'ает bytes (только metadata) — для UI listing
- aggregate_score хранится как numeric(4,2) — точно сохраняется при
  round-trip
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.core.validator.aggregator import AggregatedReport, ValidationDecision

logger = logging.getLogger(__name__)


def _new_id() -> str:
    return f"vr_{uuid.uuid4().hex[:16]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summarize_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Минимальный сериализатор: name + kind + size — без content/bytes."""
    out: list[dict[str, Any]] = []
    for a in artifacts or []:
        if not isinstance(a, dict):
            continue
        out.append({
            "name": a.get("name") or "unnamed",
            "kind": a.get("kind") or "file",
            "size_bytes": int(a.get("size_bytes") or len(str(a.get("content", "")))),
            "truncated": bool(a.get("truncated", False)),
        })
        if len(out) >= 100:
            break
    return out


@dataclass
class StoredValidation:
    """Read-projection — то что отдаём из БД в API."""
    id: str
    user_id: str
    decision: str
    aggregate_score: float
    blocker_seen: bool
    rationale: str
    reports: list[dict[str, Any]] = field(default_factory=list)
    artifacts_summary: list[dict[str, Any]] = field(default_factory=list)
    issue_count: int = 0
    task_handle_id: Optional[str] = None
    task_type: Optional[str] = None
    tenant_id: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    human_decision: Optional[str] = None
    human_notes: Optional[str] = None
    created_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Any) -> "StoredValidation":
        get = (row.get if hasattr(row, "get")
               else lambda k, default=None: row[k] if k in row else default)
        reports = get("reports") or []
        if isinstance(reports, str):
            try:
                reports = json.loads(reports)
            except json.JSONDecodeError:
                reports = []
        artifacts = get("artifacts_summary") or []
        if isinstance(artifacts, str):
            try:
                artifacts = json.loads(artifacts)
            except json.JSONDecodeError:
                artifacts = []
        return cls(
            id=str(get("id")),
            user_id=str(get("user_id")),
            decision=str(get("decision") or ""),
            aggregate_score=float(get("aggregate_score") or 0),
            blocker_seen=bool(get("blocker_seen", False)),
            rationale=str(get("rationale") or ""),
            reports=list(reports),
            artifacts_summary=list(artifacts),
            issue_count=int(get("issue_count") or 0),
            task_handle_id=get("task_handle_id"),
            task_type=get("task_type"),
            tenant_id=get("tenant_id"),
            reviewed_by=get("reviewed_by"),
            reviewed_at=str(get("reviewed_at")) if get("reviewed_at") else None,
            human_decision=get("human_decision"),
            human_notes=get("human_notes"),
            created_at=str(get("created_at")) if get("created_at") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "task_handle_id": self.task_handle_id,
            "task_type": self.task_type,
            "decision": self.decision,
            "aggregate_score": self.aggregate_score,
            "blocker_seen": self.blocker_seen,
            "rationale": self.rationale,
            "reports": list(self.reports),
            "artifacts_summary": list(self.artifacts_summary),
            "issue_count": self.issue_count,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "human_decision": self.human_decision,
            "human_notes": self.human_notes,
            "created_at": self.created_at,
        }


_INSERT_SQL = """
INSERT INTO public.validation_results
    (id, user_id, task_handle_id, task_type,
     decision, aggregate_score, blocker_seen, rationale,
     reports, artifacts_summary, issue_count)
VALUES (:id, :user_id, :task_handle_id, :task_type,
        :decision, :aggregate_score, :blocker_seen, :rationale,
        CAST(:reports AS JSONB), CAST(:artifacts_summary AS JSONB), :issue_count)
"""


_LIST_SQL = """
SELECT id, user_id, task_handle_id, task_type, decision, aggregate_score,
       blocker_seen, rationale, reports, artifacts_summary, issue_count,
       reviewed_by, reviewed_at, human_decision, human_notes,
       tenant_id, created_at
FROM public.validation_results
WHERE user_id = :user_id
"""

_GET_SQL = """
SELECT id, user_id, task_handle_id, task_type, decision, aggregate_score,
       blocker_seen, rationale, reports, artifacts_summary, issue_count,
       reviewed_by, reviewed_at, human_decision, human_notes,
       tenant_id, created_at
FROM public.validation_results
WHERE id = :id
"""

_COUNT_RECENT_SQL = """
SELECT count(*) AS c
FROM public.validation_results
WHERE created_at > :since
"""


class ValidationResultService:
    """Persistence layer над validation reports.

    Caller передаёт `postgres` — PostgresClient или совместимый объект с
    `.session()` async context-manager.
    Если postgres=None — все методы no-op (best-effort), удобно для unit
    тестов.
    """

    def __init__(self, *, postgres: Any = None) -> None:
        self.postgres = postgres

    async def save(
        self,
        *,
        user_id: str,
        aggregated: AggregatedReport,
        artifacts: list[dict[str, Any]],
        task_handle_id: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> Optional[str]:
        """Сохранить result. Возвращает id или None при failure.

        Никогда не raises — failure logged, caller получает None.
        """
        record_id = _new_id()
        params = {
            "id": record_id,
            "user_id": user_id,
            "task_handle_id": task_handle_id,
            "task_type": task_type,
            "decision": aggregated.decision.value,
            "aggregate_score": float(aggregated.aggregate_score),
            "blocker_seen": bool(aggregated.blocker_seen),
            "rationale": aggregated.rationale or "",
            "reports": json.dumps(
                [r.to_dict() for r in aggregated.reports],
                ensure_ascii=False,
            ),
            "artifacts_summary": json.dumps(
                _summarize_artifacts(artifacts),
                ensure_ascii=False,
            ),
            "issue_count": len(aggregated.all_issues),
        }
        if self.postgres is None:
            logger.debug("ValidationResultService: postgres unset, skipping save")
            return None
        try:
            async with self.postgres.session() as session:
                await session.execute(_INSERT_SQL, params)
        except Exception as exc:
            logger.warning("ValidationResultService.save failed: %s", exc)
            return None
        return record_id

    async def get(self, *, validation_id: str) -> Optional[StoredValidation]:
        if not validation_id or self.postgres is None:
            return None
        try:
            async with self.postgres.session() as session:
                result = await session.execute(_GET_SQL, {"id": validation_id})
                row = await _first_row(result)
        except Exception as exc:
            logger.warning("ValidationResultService.get failed: %s", exc)
            return None
        if row is None:
            return None
        return StoredValidation.from_row(row)

    async def list_for_user(
        self,
        *,
        user_id: str,
        decision: Optional[ValidationDecision] = None,
        limit: int = 50,
        offset: int = 0,
        unreviewed_only: bool = False,
    ) -> list[StoredValidation]:
        if self.postgres is None:
            return []
        sql = _LIST_SQL
        params: dict[str, Any] = {"user_id": user_id}
        if decision is not None:
            sql += " AND decision = :decision"
            params["decision"] = decision.value
        if unreviewed_only:
            sql += " AND reviewed_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        params["lim"] = max(1, min(int(limit), 200))
        params["off"] = max(0, int(offset))
        try:
            async with self.postgres.session() as session:
                result = await session.execute(sql, params)
                rows = await _all_rows(result)
        except Exception as exc:
            logger.warning("ValidationResultService.list_for_user failed: %s", exc)
            return []
        return [StoredValidation.from_row(r) for r in rows]

    async def count_since(self, *, since: datetime) -> int:
        """Counted by RLS — сколько runs было в текущем tenant с момента since."""
        if self.postgres is None:
            return 0
        try:
            async with self.postgres.session() as session:
                result = await session.execute(_COUNT_RECENT_SQL, {"since": since})
                row = await _first_row(result)
        except Exception as exc:
            logger.warning("ValidationResultService.count_since failed: %s", exc)
            return 0
        if row is None:
            return 0
        get = row.get if hasattr(row, "get") else lambda k, d=None: row[k]
        return int(get("c", 0) or 0)

    async def mark_reviewed(
        self,
        *,
        validation_id: str,
        reviewer_user_id: str,
        human_decision: str,
        human_notes: str = "",
    ) -> bool:
        """Записать human review результат. Доступные decisions проверяются
        caller'ом."""
        if self.postgres is None:
            return False
        if human_decision not in {"approve", "reject", "escalate"}:
            return False
        try:
            async with self.postgres.session() as session:
                await session.execute(
                    """
                    UPDATE public.validation_results
                       SET reviewed_by = :reviewer,
                           reviewed_at = now(),
                           human_decision = :decision,
                           human_notes = :notes
                     WHERE id = :id
                    """,
                    {
                        "reviewer": reviewer_user_id,
                        "decision": human_decision,
                        "notes": human_notes[:2000],
                        "id": validation_id,
                    },
                )
        except Exception as exc:
            logger.warning("ValidationResultService.mark_reviewed failed: %s", exc)
            return False
        return True


async def _first_row(result: Any) -> Optional[Any]:
    """Compat-обёртка для разных asyncpg/sqlalchemy result shapes."""
    # sqlalchemy result: scalars/all/first
    if hasattr(result, "first"):
        try:
            row = result.first()
            if hasattr(row, "_mapping"):
                return dict(row._mapping)
            return row
        except Exception:
            pass
    if hasattr(result, "fetchone"):
        try:
            row = await result.fetchone() if hasattr(result.fetchone, "__await__") else result.fetchone()
            return dict(row) if row else None
        except Exception:
            return None
    return None


async def _all_rows(result: Any) -> list[Any]:
    if hasattr(result, "mappings"):
        try:
            return [dict(m) for m in result.mappings().all()]
        except Exception:
            pass
    if hasattr(result, "all"):
        try:
            rows = result.all()
            return [
                dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
                for r in rows
            ]
        except Exception:
            return []
    return []


# === Per-tenant quota ====================================================

@dataclass(frozen=True)
class QuotaConfig:
    """Per-tenant validation quota."""
    max_runs_per_minute: int = 30
    max_runs_per_hour: int = 200
    max_runs_per_day: int = 2000


class QuotaExceeded(Exception):
    """Raised когда tenant превысил allowed validation rate."""

    def __init__(self, scope: str, limit: int, used: int) -> None:
        self.scope = scope
        self.limit = limit
        self.used = used
        super().__init__(
            f"validation quota exceeded ({scope}): {used}/{limit}"
        )


async def assert_within_quota(
    *,
    service: ValidationResultService,
    config: QuotaConfig,
) -> None:
    """Проверить что не превышены лимиты в текущем tenant'е.

    Использует RLS — `count_since` отдаёт count по текущему tenant'у
    автоматически. Если postgres недоступен — quota silently passes
    (fail-open, чтобы не блокировать validate когда БД временно лежит).
    """
    if service.postgres is None:
        return
    now = datetime.now(timezone.utc)

    checks = (
        ("per_minute", config.max_runs_per_minute, timedelta(minutes=1)),
        ("per_hour", config.max_runs_per_hour, timedelta(hours=1)),
        ("per_day", config.max_runs_per_day, timedelta(days=1)),
    )
    for scope, limit, window in checks:
        if limit <= 0:
            continue
        count = await service.count_since(since=now - window)
        if count >= limit:
            raise QuotaExceeded(scope=scope, limit=limit, used=count)


__all__ = [
    "QuotaConfig",
    "QuotaExceeded",
    "StoredValidation",
    "ValidationResultService",
    "assert_within_quota",
]
