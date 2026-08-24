# -*- coding: utf-8 -*-
"""Шина данных: тенант из ключа, владение consumer'ом, рабочий /dialog.

Аудит раздела «Шина данных» нашёл три дефекта:

1. Cross-tenant доступ: /data-bus/query|snapshot принимали user_id
   query-параметром, и сервис использовал `user_id or consumer.tenant_id`.
   Guard соответствия токену на X-API-Key не распространяется — потребитель
   тенанта A с валидным ключом мог передать user_id=<чужой tenant> и искать
   по чужому графу.

2. Admin-операции шли по одному consumer_id без сверки владельца: зная
   чужой dbc_-идентификатор, можно было деактивировать чужого потребителя,
   а через ротацию — ПОЛУЧИТЬ НОВЫЙ СЕКРЕТ чужого ключа.

3. /dialog читал res["results"], которого сервис никогда не возвращал
   (узлы лежат в "data") — всегда перетирал ответ на «данных не найдено».

Тесты контрактные + прямой прогон _tenant_for: сервис целиком тянет
хранилища, недоступные в песочнице.
"""
from __future__ import annotations

import os
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _extract_tenant_for():
    """Вырезать _tenant_for из исходника и выполнить изолированно."""
    src = _src("backend/core/data_bus/bus_service.py")
    start = src.index("    def _tenant_for(")
    end = src.index("    async def _query_inner(")
    body = src[start:end]
    # снять один уровень отступа
    lines = [ln[4:] if ln.startswith("    ") else ln
             for ln in body.splitlines()]
    ns: dict = {"Optional": type(None), "logger": types.SimpleNamespace(
        warning=lambda *a, **k: None)}
    exec("from typing import Optional\n" + "\n".join(lines), ns)
    return ns["_tenant_for"]


_tenant_for = _extract_tenant_for()


class _Consumer:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id


# ── 1. Тенант всегда из ключа ───────────────────────────────────────────

def test_foreign_user_id_is_ignored():
    """Главный случай: параметр не расширяет доступ."""
    c = _Consumer("tenant-a")
    assert _tenant_for(c, "tenant-b") == "tenant-a", (
        "user_id из запроса не должен переключать тенант"
    )
    print("✅ чужой user_id игнорируется — тенант всегда из ключа")


def test_matching_or_absent_user_id_ok():
    c = _Consumer("tenant-a")
    assert _tenant_for(c, "tenant-a") == "tenant-a"
    assert _tenant_for(c, None) == "tenant-a"
    assert _tenant_for(c, "") == "tenant-a"
    print("✅ совпадающий или пустой user_id — норма")


def test_no_or_fallback_remains_in_source():
    src = _src("backend/core/data_bus/bus_service.py")
    code_only = "\n".join(ln for ln in src.splitlines()
                          if not ln.lstrip().startswith("#")
                          and "Раньше стояло" not in ln)
    assert "user_id or consumer.tenant_id" not in code_only, (
        "уязвимый паттерн вернулся в исполняемый код"
    )
    assert src.count("self._tenant_for(consumer, user_id)") >= 3, (
        "все пути (query, recon, snapshot) должны идти через _tenant_for"
    )
    print("✅ уязвимый паттерн `user_id or tenant` устранён во всех путях")


# ── 2. Владение consumer'ом ─────────────────────────────────────────────

def test_admin_ops_check_ownership():
    src = _src("backend/core/data_bus/bus_service.py")
    assert "async def _owned_consumer(" in src
    start = src.index("async def _owned_consumer(")
    body = src[start:start + 1600]
    assert "consumer.tenant_id) != str(tenant_id)" in body, (
        "сверка владельца обязана сравнивать тенант consumer'а"
    )
    for op in ("async def deactivate_consumer(", "async def rotate_api_key("):
        op_start = src.index(op)
        op_body = src[op_start:op_start + 900]
        assert "_owned_consumer(" in op_body, (
            f"{op} снова работает без проверки владения"
        )
    print("✅ деактивация и ротация требуют владения consumer'ом")


def test_routes_pass_verified_tenant():
    src = _src("backend/api/routes/data_bus.py")
    assert "service.deactivate_consumer(consumer_id, tenant_id=user_id)" in src
    assert "service.rotate_api_key(consumer_id, tenant_id=user_id)" in src
    # user_id на admin-роутере подтверждён токеном
    assert "enforce_user_id_matches_token" in src
    print("✅ роуты передают подтверждённый токеном тенант")


# ── 3. /dialog работает ─────────────────────────────────────────────────

def test_dialog_reads_data_key():
    src = _src("backend/api/routes/data_bus.py")
    start = src.index('"""Диалог агент↔агент')
    body = src[start:start + 3000]
    assert 'res.get("data")' in body, (
        "/dialog обязан читать ключ, который сервис реально возвращает"
    )
    assert 'res.get("results")' not in body, (
        "чтение несуществующего results вернулось — сценарий снова мёртв"
    )
    assert 'response_format="both"' in body, (
        "для контекста диалога нужны и данные, и синтез"
    )
    print("✅ /dialog читает data и просит формат both")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты шины данных прошли.")
