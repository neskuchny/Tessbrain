"""Unit-тесты для core.validator.store (W23)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from backend.core.validator.aggregator import (
    AggregatedReport,
    ValidationDecision,
)
from backend.core.validator.base import (
    Issue,
    IssueSeverity,
    ValidatorReport,
)
from backend.core.validator.store import (
    QuotaConfig,
    QuotaExceeded,
    StoredValidation,
    ValidationResultService,
    _summarize_artifacts,
    assert_within_quota,
)


def _run(coro):
    return asyncio.run(coro)


# === _summarize_artifacts ==============================================

def test_summarize_strips_content() -> None:
    """Bytes/content не должны попасть в summary."""
    out = _summarize_artifacts([
        {"name": "a.png", "kind": "binary", "size_bytes": 5000, "content": b"big"},
        {"name": "b.md", "kind": "file", "content": "hello"},
    ])
    assert len(out) == 2
    assert "content" not in out[0]
    assert "content" not in out[1]


def test_summarize_falls_back_to_content_length() -> None:
    out = _summarize_artifacts([
        {"name": "x.txt", "kind": "file", "content": "12345"},
    ])
    assert out[0]["size_bytes"] == 5


def test_summarize_filters_invalid() -> None:
    out = _summarize_artifacts([
        {"name": "ok.md"},
        "not a dict",
        None,
    ])
    assert len(out) == 1


def test_summarize_caps_at_100() -> None:
    big = [{"name": f"f{i}.md"} for i in range(200)]
    out = _summarize_artifacts(big)
    assert len(out) == 100


# === StoredValidation.from_row =========================================

def test_from_row_basic_dict() -> None:
    row = {
        "id": "v_abc",
        "user_id": "u-1",
        "decision": "auto_approve",
        "aggregate_score": 8.5,
        "blocker_seen": False,
        "rationale": "ok",
        "reports": [{"validator": "structural", "score": 9}],
        "artifacts_summary": [{"name": "x.md"}],
        "issue_count": 2,
        "tenant_id": "t-1",
        "created_at": "2026-01-01T00:00:00Z",
    }
    s = StoredValidation.from_row(row)
    assert s.id == "v_abc"
    assert s.aggregate_score == 8.5
    assert len(s.reports) == 1
    assert s.tenant_id == "t-1"


def test_from_row_parses_string_jsonb() -> None:
    """Postgres драйверы могут отдать JSONB как str — должны раcпарсить."""
    row = {
        "id": "v_x",
        "user_id": "u",
        "decision": "reject",
        "aggregate_score": 0,
        "rationale": "",
        "reports": '[{"validator":"x","score":0}]',
        "artifacts_summary": "[]",
    }
    s = StoredValidation.from_row(row)
    assert len(s.reports) == 1
    assert s.reports[0]["validator"] == "x"


def test_from_row_handles_invalid_jsonb_string() -> None:
    """Invalid JSONB string → empty list, не raises."""
    row = {
        "id": "v_x",
        "user_id": "u",
        "decision": "reject",
        "aggregate_score": 0,
        "rationale": "",
        "reports": "{not json",
    }
    s = StoredValidation.from_row(row)
    assert s.reports == []


def test_to_dict_round_trip() -> None:
    s = StoredValidation(
        id="v_1", user_id="u-1", decision="auto_approve",
        aggregate_score=9.0, blocker_seen=False, rationale="ok",
        issue_count=1,
    )
    d = s.to_dict()
    assert d["id"] == "v_1"
    assert d["aggregate_score"] == 9.0
    assert d["decision"] == "auto_approve"


# === ValidationResultService без postgres (no-op) =====================

def _make_aggregated(score: float = 9.0, decision=ValidationDecision.AUTO_APPROVE):
    return AggregatedReport(
        decision=decision,
        aggregate_score=score,
        reports=[
            ValidatorReport(
                validator="structural",
                score=score,
                issues=[Issue(IssueSeverity.WARNING, "fmt", "minor")],
            ),
        ],
        rationale="ok",
    )


def test_save_no_postgres_returns_none() -> None:
    svc = ValidationResultService()
    out = _run(svc.save(
        user_id="u-1",
        aggregated=_make_aggregated(),
        artifacts=[{"name": "x.md", "content": "hello"}],
    ))
    assert out is None


def test_get_no_postgres_returns_none() -> None:
    svc = ValidationResultService()
    out = _run(svc.get(validation_id="v_x"))
    assert out is None


def test_get_empty_id_returns_none() -> None:
    svc = ValidationResultService(postgres=object())
    out = _run(svc.get(validation_id=""))
    assert out is None


def test_list_no_postgres_returns_empty() -> None:
    svc = ValidationResultService()
    out = _run(svc.list_for_user(user_id="u-1"))
    assert out == []


def test_count_no_postgres_returns_zero() -> None:
    svc = ValidationResultService()
    n = _run(svc.count_since(since=datetime.now(timezone.utc)))
    assert n == 0


def test_mark_reviewed_no_postgres_returns_false() -> None:
    svc = ValidationResultService()
    ok = _run(svc.mark_reviewed(
        validation_id="v_x", reviewer_user_id="u",
        human_decision="approve",
    ))
    assert ok is False


def test_mark_reviewed_rejects_unknown_decision() -> None:
    svc = ValidationResultService(postgres=object())
    ok = _run(svc.mark_reviewed(
        validation_id="v_x", reviewer_user_id="u",
        human_decision="garbage",
    ))
    assert ok is False


# === Per-tenant quota ==================================================

class _FakeService:
    def __init__(self, count_value: int) -> None:
        self.postgres = object()
        self._count = count_value
        self.calls = 0

    async def count_since(self, *, since):
        self.calls += 1
        return self._count


def test_quota_passes_when_under_limit() -> None:
    svc = _FakeService(count_value=5)
    config = QuotaConfig(max_runs_per_minute=30, max_runs_per_hour=200, max_runs_per_day=2000)
    # Должен не raise.
    _run(assert_within_quota(service=svc, config=config))


def test_quota_raises_per_minute() -> None:
    svc = _FakeService(count_value=999)
    config = QuotaConfig(max_runs_per_minute=10, max_runs_per_hour=999999, max_runs_per_day=9999999)
    with pytest.raises(QuotaExceeded) as exc:
        _run(assert_within_quota(service=svc, config=config))
    assert exc.value.scope == "per_minute"


def test_quota_raises_per_hour() -> None:
    """Если minute OK но hour превышен — raise per_hour."""
    # Делаем service который возвращает фиксированное число — тест слабый,
    # но scope=per_minute может тоже trigger'нуться. Берём limit big.
    svc = _FakeService(count_value=500)
    config = QuotaConfig(
        max_runs_per_minute=10000,   # passes
        max_runs_per_hour=100,         # exceeded (500 >= 100)
        max_runs_per_day=10000,
    )
    with pytest.raises(QuotaExceeded) as exc:
        _run(assert_within_quota(service=svc, config=config))
    assert exc.value.scope == "per_hour"


def test_quota_skips_zero_or_negative_limit() -> None:
    """Limit ≤ 0 → не проверяем (отключено)."""
    svc = _FakeService(count_value=10_000)
    config = QuotaConfig(max_runs_per_minute=0, max_runs_per_hour=0, max_runs_per_day=0)
    # Все три скипнуты — не raises.
    _run(assert_within_quota(service=svc, config=config))


def test_quota_skips_when_no_postgres() -> None:
    """Fail-open: без postgres validation НЕ блокируется (caller не теряет работу)."""
    svc = ValidationResultService()  # postgres=None
    config = QuotaConfig(max_runs_per_minute=1)
    # Должен не raise — postgres None → skip.
    _run(assert_within_quota(service=svc, config=config))


def test_quota_exceeded_to_str() -> None:
    e = QuotaExceeded(scope="per_minute", limit=30, used=31)
    assert "per_minute" in str(e)
    assert "31/30" in str(e)
    assert e.scope == "per_minute"
    assert e.used == 31
