# -*- coding: utf-8 -*-
"""Подрядчик спрашивает мозг своими словами — в границах владельца.

Вопрос был: «почему нельзя спросить мозг, просто с ограничениями откуда
информацию брать, а откуда нельзя?». Труба-то была: /external/query
аутентифицирует подрядчика, режет по разрешённым ему категориям, маскирует
чувствительное и пишет аудит — но отдавала СТРУКТУРУ, связного ответа не было.

process_question() достраивает последний шаг, не создавая нового пути к
данным. Здесь проверяем ровно это: модель видит только то, что уже прошло
фильтр; выключено по умолчанию; ответ маскируется ещё раз; всё попадает в
аудит; отказы (ключ, лимит) до модели не доходят.
"""
import asyncio
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.external_access.access_gateway import (  # noqa: E402
    AccessGateway,
    Contractor,
    ExternalAccessResponse,
)
from backend.core.external_access.security_filter import ContractorType  # noqa: E402


class _Gate(AccessGateway):
    """Шлюз без конфигов на диске: подменяем только вход в process_request."""

    def __init__(self, tmp_path, resp: ExternalAccessResponse):
        self.config_path = tmp_path / "contractors.json"
        self.contractors = {
            "c1": Contractor(id="c1", name="Консалтинг",
                             type=ContractorType.CONSULTANT,
                             api_key_hash="x",
                             allowed_categories={"company_overview"}),
        }
        from backend.core.external_access.audit_logger import AuditLogger
        from backend.core.external_access.security_filter import get_security_filter
        self.security_filter = get_security_filter()
        self.data_aggregator = None
        self.audit_logger = AuditLogger(storage_path=str(tmp_path / "audit"))
        self._request_counts = {}
        self._resp = resp
        self.seen = {}

    async def process_request(self, **kw):     # noqa: D401 - подменяем вход
        self.seen["request"] = kw
        return self._resp


def _ok_resp(data):
    return ExternalAccessResponse(
        request_id="r1", contractor_id="c1", contractor_type="consultant",
        data=data, categories_accessed=["company_overview"],
        items_returned=1, items_filtered=3, audit_log_id="a1",
        processing_time_ms=1)


def _fail_resp(err):
    r = _ok_resp({})
    r.error = err
    return r


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("ENABLE_EXTERNAL_ASK", "1")


# ── Флаг ────────────────────────────────────────────────────────────────────

def test_disabled_by_default(tmp_path, monkeypatch):
    """Без явного разрешения владельца связного ответа нет — но и молчаливой
    работы тоже: статус honest 'disabled', данные отдаются как есть."""
    monkeypatch.delenv("ENABLE_EXTERNAL_ASK", raising=False)
    g = _Gate(tmp_path, _ok_resp({"company_overview": "Компания делает X"}))
    r = asyncio.run(g.process_question(api_key="k", question="чем занимаемся?",
                                       user_id="u-1"))
    assert r.data["answer_status"] == "disabled"
    assert r.data["company_overview"] == "Компания делает X"


def test_flag_parsing(monkeypatch):
    for val, want in [("1", True), ("on", True), ("TRUE", True), ("yes", True),
                      ("0", False), ("", False), ("нет", False)]:
        monkeypatch.setenv("ENABLE_EXTERNAL_ASK", val)
        assert AccessGateway.ask_enabled() is want, val


# ── Границы данных ──────────────────────────────────────────────────────────

def test_model_sees_only_filtered_data(tmp_path, on, monkeypatch):
    """Ключевое: в промпт уходит РОВНО то, что вернул фильтр. Скрытого нет."""
    seen = {}

    async def _fake_llm(uid, workload, prompt, **kw):
        seen["prompt"] = prompt
        seen["user"] = uid
        return "Компания занимается X."

    monkeypatch.setattr("backend.core.llm.workload_policy.generate_for_workload",
                        _fake_llm)
    g = _Gate(tmp_path, _ok_resp({"company_overview": "Компания делает X",
                                  "budget": "[СУММА СКРЫТА]"}))
    r = asyncio.run(g.process_question(api_key="k", question="чем занимаетесь?",
                                       user_id="u-1"))
    assert r.data["answer_status"] == "ok"
    assert "Компания делает X" in seen["prompt"]
    assert "[СУММА СКРЫТА]" in seen["prompt"]
    assert "чем занимаетесь?" in seen["prompt"]
    # расход относится на владельца данных, а не на подрядчика
    assert seen["user"] == "u-1"


def test_question_goes_through_the_same_gate(tmp_path, on, monkeypatch):
    """Вопрос обязан пройти обычный process_request — с категориями и
    владельцем, иначе появился бы второй, необойдённый путь к данным."""
    async def _fake_llm(uid, workload, prompt, **kw):
        return "ответ"

    monkeypatch.setattr("backend.core.llm.workload_policy.generate_for_workload",
                        _fake_llm)
    g = _Gate(tmp_path, _ok_resp({"company_overview": "X"}))
    asyncio.run(g.process_question(api_key="k", question="q",
                                   categories=["company_overview"],
                                   user_id="u-1", ip_address="1.2.3.4"))
    req = g.seen["request"]
    assert req["api_key"] == "k" and req["user_id"] == "u-1"
    assert req["categories"] == ["company_overview"]
    assert req["ip_address"] == "1.2.3.4"


def test_answer_is_masked_again(tmp_path, on, monkeypatch):
    """Даже если модель вписала в пересказ сумму — маскировка её снимет."""
    async def _fake_llm(uid, workload, prompt, **kw):
        return "Выручка: 5000000 руб, всё отлично"

    monkeypatch.setattr("backend.core.llm.workload_policy.generate_for_workload",
                        _fake_llm)
    g = _Gate(tmp_path, _ok_resp({"company_overview": "X"}))
    r = asyncio.run(g.process_question(api_key="k", question="q", user_id="u-1"))
    assert "5000000 руб" not in r.data["answer"], r.data["answer"]
    assert "СКРЫТ" in r.data["answer"]


# ── Отказы ──────────────────────────────────────────────────────────────────

def test_auth_failure_never_reaches_the_model(tmp_path, on, monkeypatch):
    called = []

    async def _fake_llm(*a, **k):
        called.append(1)
        return "не должно случиться"

    monkeypatch.setattr("backend.core.llm.workload_policy.generate_for_workload",
                        _fake_llm)
    g = _Gate(tmp_path, _fail_resp("Authentication failed"))
    r = asyncio.run(g.process_question(api_key="bad", question="q", user_id="u-1"))
    assert r.error == "Authentication failed"
    assert not called, "неавторизованный запрос дошёл до модели"


def test_rate_limit_never_reaches_the_model(tmp_path, on, monkeypatch):
    called = []

    async def _fake_llm(*a, **k):
        called.append(1)
        return "нет"

    monkeypatch.setattr("backend.core.llm.workload_policy.generate_for_workload",
                        _fake_llm)
    g = _Gate(tmp_path, _fail_resp("Rate limit exceeded"))
    r = asyncio.run(g.process_question(api_key="k", question="q", user_id="u-1"))
    assert r.error == "Rate limit exceeded" and not called


def test_model_failure_degrades_honestly(tmp_path, on, monkeypatch):
    """Модель недоступна → данные всё равно отдаём, но не притворяемся."""
    async def _boom(*a, **k):
        raise RuntimeError("провайдер лежит")

    monkeypatch.setattr("backend.core.llm.workload_policy.generate_for_workload",
                        _boom)
    g = _Gate(tmp_path, _ok_resp({"company_overview": "X"}))
    r = asyncio.run(g.process_question(api_key="k", question="q", user_id="u-1"))
    assert r.data["answer_status"] == "unavailable"
    assert r.data["company_overview"] == "X"


# ── Аудит ───────────────────────────────────────────────────────────────────

def test_answer_is_audited(tmp_path, on, monkeypatch):
    """Владелец должен видеть не только «скачали данные», но и «спросили»."""
    async def _fake_llm(*a, **k):
        return "краткий ответ"

    monkeypatch.setattr("backend.core.llm.workload_policy.generate_for_workload",
                        _fake_llm)
    g = _Gate(tmp_path, _ok_resp({"company_overview": "X"}))
    logged = []
    orig = g.audit_logger.log

    async def _spy(**kw):
        logged.append(kw)
        return await orig(**kw)

    g.audit_logger.log = _spy
    asyncio.run(g.process_question(api_key="k", question="как дела с наймом?",
                                   user_id="u-1"))
    assert logged, "ответ подрядчику не попал в аудит"
    assert logged[0]["query"].startswith("[ask] ")
    assert "как дела с наймом?" in logged[0]["query"]
    assert logged[0]["contractor_id"] == "c1"


def test_consultant_categories_are_narrow():
    """Консультанту по умолчанию доступны только обзорные категории."""
    from backend.core.external_access.security_filter import SecurityFilter
    cats = SecurityFilter.ALLOWED_CATEGORIES[ContractorType.CONSULTANT]
    assert cats == {"company_overview", "processes", "team_structure"}
    assert "salary" not in cats and "financial" not in cats
