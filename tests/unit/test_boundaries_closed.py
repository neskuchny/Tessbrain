# -*- coding: utf-8 -*-
"""Закрытие границ из раздела 16: включения и push-уведомления шины.

Аудит бизнес-карты с владельцем: часть «выключено по умолчанию» была
выключена без причины, а «push-уведомлений из шины нет» — разрыв, который
несложно закрыть. Контракты:

  1. трасса происхождения включена по умолчанию (выключается env);
  2. слой групп доступа включён по умолчанию (strict), и это безопасно:
     непомеченный узел = INTERNAL, виден всем ролям; прячется только явно
     помеченное;
  3. синхронизация датасетов включена флагом в поставляемой конфигурации;
  4. дифф-детекция: неизменившийся источник не порождает версию и не
     запускает перестройку онтологии;
  5. webhook-канал: секрет виден один раз, хранится хэш; адрес проверяется
     периметром (в закрытом контуре внешний адрес отвергается);
  6. пуш НЕ несёт данных: длинные строки и вложенные структуры режутся;
  7. мёртвый адрес отключается после предела подряд ошибок;
  8. бот мессенджера подмешивает память компании (с лимитом времени,
     best-effort) — а не отвечает вслепую.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str, stub_pkgs=()):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.security",
                "backend.core.access", "backend.core.lineage",
                "backend.core.data_bus", "backend.core.store", *stub_pkgs):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# tenant_io стаб: file_lock/atomic_write_json без зависимостей.
_tio = types.ModuleType("backend.core.store.tenant_io")


class _Lock:
    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _atomic_write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


_tio.file_lock = _Lock
_tio.atomic_write_json = _atomic_write_json
sys.modules.setdefault("backend.core.store.tenant_io", _tio)

_lineage = _load("backend.core.lineage.lineage", "backend/core/lineage/lineage.py")
_mand = _load("backend.core.access.mandatory", "backend/core/access/mandatory.py")
_wh = _load("backend.core.data_bus.webhook_push",
            "backend/core/data_bus/webhook_push.py")


def _src(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


# ── 1-3. Включения ──────────────────────────────────────────────────────

def test_lineage_on_by_default():
    os.environ.pop("DATA_LINEAGE_ENABLED", None)
    assert _lineage.lineage_enabled_from_env() is True
    os.environ["DATA_LINEAGE_ENABLED"] = "off"
    assert _lineage.lineage_enabled_from_env() is False
    os.environ.pop("DATA_LINEAGE_ENABLED", None)
    print("✅ трасса происхождения включена, выключатель работает")


def test_mandatory_strict_by_default_and_safe():
    os.environ.pop("MANDATORY_ACCESS_MODE", None)
    assert _mand.mode_from_env() == _mand.EnforcementMode.STRICT
    # Безопасность умолчания: непомеченный узел виден рядовой роли.
    m = _mand.TeamMembership(user_id="u", clearance=2)
    ok, _ = _mand.can_see_node({}, m, mode=_mand.EnforcementMode.STRICT)
    assert ok, "узел без грифа обязан остаться видимым"
    # А явно помеченное — прячется: ради этого слой и включён.
    ok, reason = _mand.can_see_node({"access_level": 4}, m,
                                    mode=_mand.EnforcementMode.STRICT)
    assert not ok and "clearance" in reason
    ok, _ = _mand.can_see_node(
        {"access_level": 3, "access_groups": ["team:fin"]},
        _mand.TeamMembership(user_id="u", clearance=3,
                             team_ids=frozenset({"team:fin"})),
        mode=_mand.EnforcementMode.STRICT)
    assert ok, "член группы с допуском обязан видеть свой узел"
    os.environ["MANDATORY_ACCESS_MODE"] = "off"
    assert _mand.mode_from_env() == _mand.EnforcementMode.OFF
    os.environ.pop("MANDATORY_ACCESS_MODE", None)
    print("✅ группы доступа включены; непомеченное видно, помеченное прячется")


def test_dataset_sync_enabled_in_shipped_config():
    cfg = json.load(open(os.path.join(ROOT, "config/feature_flags.json"),
                         encoding="utf-8"))
    assert cfg.get("enable_dataset_sync") is True
    src = _src("backend/core/config/feature_flags.py")
    assert "enable_dataset_sync: bool = True" in src
    print("✅ синхронизация датасетов включена в поставляемой конфигурации")


# ── 4. Дифф-детекция обновления ─────────────────────────────────────────

def test_refresh_skips_rebuild_when_unchanged():
    src = _src("backend/core/ontology/dataset_service.py")
    fn = src[src.index("async def refresh_dataset"):]
    fn = fn[:fn.index("async def _fetch_url")]
    assert '"unchanged": True' in fn, "неизменившийся источник → честный ответ"
    # проверка стоит ДО дорогой перестройки и ДО записи версии
    assert fn.index("unchanged") < fn.index("build_dataset_ontology"), (
        "дифф обязан отсекать ДО перестройки онтологии"
    )
    print("✅ неизменившийся источник не плодит версии и не жжёт модель")


# ── 5. Webhook: секрет и периметр ───────────────────────────────────────

def test_webhook_secret_shown_once_stored_hashed():
    res = _wh.new_channel(tenant_id="t1", consumer_id="c1",
                          endpoint_url="https://partner.example.com/hook")
    assert res["ok"]
    ch, secret = res["channel"], res["secret"]
    assert secret and secret not in json.dumps(ch.to_dict()), (
        "секрет не должен храниться в канале"
    )
    import hashlib
    assert ch.secret_hash == hashlib.sha256(secret.encode()).hexdigest()
    sig = _wh.sign_payload(secret, b'{"event":"x"}')
    assert len(sig) == 64
    print("✅ секрет виден один раз, хранится хэшем, подпись работает")


def test_webhook_perimeter_rules():
    os.environ.pop("ENTERPRISE_MODE", None)
    assert _wh.validate_endpoint("https://partner.example.com/hook") is None
    assert _wh.validate_endpoint("http://crm.internal/hook") is None, (
        "http внутрь своей сети — допустим"
    )
    assert _wh.validate_endpoint("http://partner.example.com/hook"), (
        "открытый http наружу — нет"
    )
    os.environ["ENTERPRISE_MODE"] = "true"
    try:
        reason = _wh.validate_endpoint("https://partner.example.com/hook")
        assert reason and "закрытый контур" in reason
        assert _wh.validate_endpoint("http://10.0.0.5/hook") is None
    finally:
        os.environ.pop("ENTERPRISE_MODE", None)
    print("✅ адрес пуша проверяется периметром")


# ── 6. Пуш не несёт данных ──────────────────────────────────────────────

def test_push_payload_carries_no_data():
    s = _wh._sanitize_summary({
        "tasks": 7, "ok": True,
        "transcript": "х" * 5000,
        "nested": {"secret": "данные"},
        "label": "встреча отдела продаж",
    })
    assert s["tasks"] == 7 and s["ok"] is True
    assert len(s["transcript"]) <= 80, "длинный текст обязан быть срезан"
    assert "nested" not in s, "вложенные структуры не проходят"
    print("✅ в пуше только счётчики и короткие метки — данных нет")


def test_notify_disables_dead_channel():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp:
            store = _wh.WebhookStore("t1", path=os.path.join(tmp, "wh.json"))
            res = _wh.new_channel(tenant_id="t1", consumer_id="c1",
                                  endpoint_url="https://dead.example.com/h")
            ch = res["channel"]
            ch.consecutive_failures = _wh.MAX_CONSECUTIVE_FAILURES - 1
            store.save(ch)

            async def _fail(url, body, headers):
                return False, "ConnectError: dead"
            _wh._deliver, orig = _fail, _wh._deliver
            try:
                out = await _wh.notify("t1", "meeting_processed",
                                       {"tasks": 1}, store=store)
            finally:
                _wh._deliver = orig
            assert out["failed"] == 1
            saved = store.get(ch.id)
            assert saved and not saved.is_active, (
                "после предела подряд ошибок канал обязан отключиться"
            )
        return True
    assert asyncio.run(_run())
    print("✅ мёртвый адрес отключается сам, а не долбится вечно")


def test_notify_skips_unsubscribed_events():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp:
            store = _wh.WebhookStore("t1", path=os.path.join(tmp, "wh.json"))
            res = _wh.new_channel(tenant_id="t1", consumer_id="c1",
                                  endpoint_url="https://x.example.com/h",
                                  events=["dataset_refreshed"])
            store.save(res["channel"])
            calls = []

            async def _ok(url, body, headers):
                calls.append(url)
                return True, ""
            _wh._deliver, orig = _ok, _wh._deliver
            try:
                out = await _wh.notify("t1", "meeting_processed",
                                       {}, store=store)
            finally:
                _wh._deliver = orig
            assert out["sent"] == 0 and not calls, (
                "неподписанное событие не доставляется"
            )
        return True
    assert asyncio.run(_run())
    print("✅ доставка только по подписанным событиям")


# ── 7. Роуты и хук ──────────────────────────────────────────────────────

def test_webhook_routes_check_consumer_ownership():
    src = _src("backend/api/routes/data_bus.py")
    fn = src[src.index("async def admin_create_webhook"):]
    fn = fn[:fn.index("async def admin_list_webhooks")]
    assert "get_consumer" in fn and "tenant_id) != str(user_id)" in fn, (
        "канал заводится только на СВОЕГО потребителя"
    )
    assert "admin_create_webhook" in src[src.index("route_handlers"):]
    print("✅ webhook-канал только на своего потребителя; роуты подключены")


def test_worker_pushes_counts_not_content():
    src = _src("backend/workers/taskiq_app.py")
    idx = src.index("DATA_BUS_WEBHOOKS")
    block = src[idx - 200:idx + 900]
    assert "meeting_processed" in block and "len(" in block
    # В вызов notify уходят только счётчики: ни транскрипта, ни конспекта.
    call = block.split("_wh_notify(", 1)[1].split("})", 1)[0]
    assert "transcript" not in call and "summary\"" not in call, (
        "в пуш не должно попадать содержимое встречи"
    )
    print("✅ воркер шлёт счётчики, не содержимое встречи")


# ── 8. Бот мессенджера ──────────────────────────────────────────────────

def test_messenger_bot_searches_company_memory():
    src = _src("backend/core/messengers/chat_handler.py")
    # Контракт — «бот ищет по памяти компании», а не «зовёт конкретную
    # функцию». Раньше здесь стоял get_bm25_searcher; поиск переехал на
    # topic_fragments (гибрид с BM25-фолбэком), и проверка осталась
    # прибитой к имени старой функции. Проверяем факт поиска по памяти.
    assert "_memory_context" in src and "topic_fragments" in src
    fn = src[src.index("async def _memory_context"):]
    fn = fn[:fn.index("async def _get_router")]
    assert "wait_for" in fn and "timeout" in fn, (
        "поиск обязан быть ограничен временем — мессенджер не ждёт"
    )
    assert 'return ""' in fn, "не успели/нет индекса → пустой контекст, не ошибка"
    call = src[src.index("MESSENGER_MEMORY_SEARCH"):]
    assert "не выдумывай" in call[:900], (
        "промпт обязан запрещать выдумывать сверх фрагментов"
    )
    print("✅ бот подмешивает память компании: с лимитом времени, честно")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты закрытия границ прошли.")
