# -*- coding: utf-8 -*-
"""Офисный исполнитель: петля с приёмкой и место OpenWorker в системе.

Разбор OpenWorker показал дыру: все наши исполнители — кодинг-агенты,
а типичная задача после таскера — офисная («собери бриф», «письмо с
цифрами»). Решение: OpenWorker как ИСПОЛНИТЕЛЬ (не профиль модели!) и
петля «исполнил → проверили → доработал → человек» поверх любого backend.

Контракты:
  1. петля: провал приёмки возвращает задачу с ДОСЛОВНЫМИ замечаниями
     и даёт доработку; исчерпание доработок — к человеку, не в мусор;
  2. без проверок вердикт «не доказано», к человеку — не автопринятие;
  3. pass — тоже к человеку: финальное «принято» петля не ставит никогда;
  4. сбой исполнителя — честный failed, без выдуманного результата;
  5. офисные типы задач маршрутизируются в openworker только когда он
     поднят; явная карта сильнее;
  6. openworker не в списке внешних (свой сервис), но в закрытом контуре
     обязан быть по внутреннему адресу;
  7. как модель его завести нельзя: адаптер — исполнитель с задачей,
     submit пишет handle в store, ошибка HTTP не становится успехом.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.executors",
                "backend.core.executors.backends", "backend.core.security"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_base = _load("backend.core.executors.base", "backend/core/executors/base.py")
_loop = _load("backend.core.executors.office_loop",
              "backend/core/executors/office_loop.py")


def _src(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


class FakeBackend(_base.ExecutorBackend):
    """Исполнитель по сценарию: список текстов (или None = сбой)."""
    name = "fake"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.received: list = []
        self._results: dict = {}

    async def submit(self, submission):
        self.received.append(submission.tz_markdown)
        h = _base.TaskHandle.new(backend=self.name)
        out = self.outputs.pop(0) if self.outputs else None
        if out is None:
            self._results[h.id] = _base.TaskResult(
                handle_id=h.id, status=_base.TaskStatus.FAILED,
                success=False, error_message="исполнитель упал")
        else:
            self._results[h.id] = _base.TaskResult(
                handle_id=h.id, status=_base.TaskStatus.DONE, success=True,
                artifacts=[{"name": "r.md", "kind": "text", "content": out}])
        return h

    async def get_status(self, handle):
        return self._results[handle.id].status

    async def get_result(self, handle):
        return self._results[handle.id]


async def _noop_sleep(_s):
    return None


def _run(coro):
    return asyncio.run(coro)


# ── 1-4. Петля ──────────────────────────────────────────────────────────

def test_fail_returns_with_verbatim_remarks_then_pass():
    be = FakeBackend(["текст без нужного", "бриф: история, суммы и риски"])
    rep = _run(_loop.run_office_task(
        task_text="Собери бриф по клиенту: история, суммы, риски.",
        acceptance=[{"kind": "contains", "target": "риски"}],
        backend=be, sleep=_noop_sleep))
    assert rep.status == "awaiting_human"
    assert len(rep.attempts) == 2
    assert rep.attempts[0].verdict == "fail"
    # замечание дословно попало во ВТОРОЕ задание
    assert "риски" in be.received[1] and "Замечания приёмки" in be.received[1]
    assert rep.attempts[1].verdict == "pass"
    print("✅ провал возвращает с дословными замечаниями, доработка проходит")


def test_exhausted_returns_go_to_human_not_trash():
    be = FakeBackend(["мимо 1", "мимо 2", "мимо 3"])
    rep = _run(_loop.run_office_task(
        task_text="Задача с проверкой, которую исполнитель не пройдёт никогда.",
        acceptance=[{"kind": "contains", "target": "недостижимое"}],
        backend=be, max_returns=2, sleep=_noop_sleep))
    assert rep.status == "returned_exhausted"
    assert len(rep.attempts) == 3, "первая попытка + две доработки"
    assert rep.final_text == "мимо 3", (
        "последний результат отдаётся человеку, а не выбрасывается"
    )
    assert rep.final_verdict["verdict"] == "fail"
    print("✅ исчерпание доработок — к человеку с последним результатом")


def test_no_checks_means_inconclusive_awaiting_human():
    be = FakeBackend(["какой-то результат работы"])
    rep = _run(_loop.run_office_task(
        task_text="Задача без формализованных проверок приёмки вообще.",
        acceptance=None, backend=be, sleep=_noop_sleep))
    assert rep.status == "awaiting_human"
    assert rep.final_verdict["verdict"] == "inconclusive"
    assert "человек" in rep.final_verdict.get("note", "")
    print("✅ без проверок — «не доказано», к человеку, не автопринятие")


def test_pass_still_goes_to_human():
    be = FakeBackend(["готовый бриф с рисками и суммами, достаточно длинный"])
    rep = _run(_loop.run_office_task(
        task_text="Собери бриф по клиенту: история, суммы, риски.",
        acceptance=[{"kind": "contains", "target": "риск"},
                    {"kind": "min_len", "n": 20}],
        backend=be, sleep=_noop_sleep))
    assert rep.status == "awaiting_human", (
        "финальное «принято» петля не ставит никогда"
    )
    assert rep.final_verdict["verdict"] == "pass"
    print("✅ прошедшая приёмка всё равно ждёт человека")


def test_executor_crash_is_honest_failure():
    be = FakeBackend([None])
    rep = _run(_loop.run_office_task(
        task_text="Задача, на которой исполнитель падает сразу же.",
        acceptance=[{"kind": "min_len", "n": 5}],
        backend=be, sleep=_noop_sleep))
    assert rep.status == "failed" and rep.final_text == ""
    assert rep.attempts[0].error
    print("✅ сбой исполнителя — честный failed, не выдуманный результат")


def test_bad_regex_dropped_from_acceptance():
    checks = _loop.normalize_acceptance([
        {"kind": "regex", "pattern": r"\d{2,}"},
        {"kind": "regex", "pattern": "[битый("},
        {"kind": "contains", "target": ""},
    ])
    assert len(checks) == 1 and checks[0]["kind"] == "regex"
    print("✅ битый regex и пустые проверки отброшены")


# ── 5. Маршрутизация ────────────────────────────────────────────────────

def test_office_types_route_to_openworker_only_when_up():
    src = _src("backend/core/executors/registry.py")
    blk = src[src.index("_OFFICE_TASK_TYPES"):]
    assert '"office"' in blk[:200] and '"brief"' in blk[:200]
    fn = src[src.index("def resolve_backend_name_for_task_type"):]
    fn = fn[:fn.index("def validate_enterprise")]
    assert "OPENWORKER_BASE_URL" in fn, (
        "маршрут в openworker только когда он поднят"
    )
    assert fn.index("EXECUTOR_TASK_ROUTES") < fn.index("_OFFICE_TASK_TYPES"), (
        "явная карта сильнее дефолтного маршрута"
    )
    print("✅ офисные типы → openworker, только если он поднят; карта сильнее")


# ── 6. Периметр ─────────────────────────────────────────────────────────

def test_openworker_not_external_but_needs_internal_url_in_air_gap():
    src = _src("backend/core/executors/registry.py")
    ext = src[src.index("_EXTERNAL_BACKENDS = {"):]
    ext = ext[:ext.index("}")]
    assert "openworker" not in ext, "свой сервис — не внешний backend"
    fn = src[src.index("def validate_enterprise"):]
    assert 'backend_name == "openworker"' in fn
    assert "is_internal_url" in fn and "OPENWORKER_BASE_URL" in fn
    cfg = _src("backend/config.py")
    assert 'backend == "openworker"' in cfg, (
        "валидатор Settings обязан проверять openworker в контуре"
    )
    assert '"codex_cli"' in cfg[cfg.index("uses managed cloud API") - 400:
                                cfg.index("uses managed cloud API")], (
        "codex_cli тоже облачный — обязан быть запрещён в контуре"
    )
    print("✅ openworker свой, но в контуре только по внутреннему адресу")


# ── 7. Адаптер — исполнитель, не модель ─────────────────────────────────

def test_adapter_is_executor_not_model_profile():
    src = _src("backend/core/executors/backends/openworker.py")
    assert "ExecutorBackend" in src and "TaskSubmission" in src
    # ошибка HTTP или пустой ответ не становятся успехом
    assert "HTTP {resp.status_code}" in src.replace("f\"", "\"") or \
           "HTTP " in src
    assert "пустой ответ исполнителя" in src
    assert 'success=ok' in src and "TaskStatus.FAILED" in src
    # и он НЕ добавлен в поставщики моделей
    prof = _src("backend/core/llm/profile_client.py")
    assert "openworker" not in prof.lower(), (
        "OpenWorker как профиль модели дал бы его инструментам путь "
        "в обход сит — он должен быть только исполнителем"
    )
    print("✅ адаптер — исполнитель с задачей, не поставщик модели")


def test_route_exists_and_never_autocloses():
    src = _src("backend/api/routes/executor.py")
    fn = src[src.index('@post("/office")'):]
    fn = fn[:fn.index('@post("/auto")')]
    assert "run_office_task" in fn
    assert "не ставит" in fn or "НЕ ставит" in fn, (
        "роут обязан говорить: финальное «принято» не ставится петлёй"
    )
    assert "audit_log.emit" in fn, "запуск исполнителя виден в аудите"
    assert "office" in src[src.index("route_handlers"):]
    print("✅ роут подключён, аудит пишется, автопринятия нет")




# ── 8. Хранилище прогонов и финал человека ──────────────────────────────

def _office_store(tmp):
    import types as _t
    tio = _t.ModuleType("backend.core.store.tenant_io")

    class _L:
        def __init__(self, *_a, **_k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _aw(path, data):
        import json as _j
        with open(path, "w", encoding="utf-8") as f:
            _j.dump(data, f, ensure_ascii=False)
    tio.file_lock, tio.atomic_write_json = _L, _aw
    sys.modules["backend.core.store.tenant_io"] = tio
    if "backend.core.store" not in sys.modules:
        m = types.ModuleType("backend.core.store")
        m.__path__ = [os.path.join(ROOT, "backend", "core", "store")]
        sys.modules["backend.core.store"] = m
    st = _load("backend.core.executors.office_store",
               "backend/core/executors/office_store.py")
    return st.OfficeRunStore("u1", path=os.path.join(tmp, "runs.json")), st


def test_run_persisted_and_closed_only_by_human():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store, st = _office_store(tmp)
        run = store.add(task_text="Собери бриф", backend="openworker",
                        report={"status": "awaiting_human",
                                "final_text": "бриф готов",
                                "final_verdict": {"verdict": "pass"},
                                "attempts": [{"attempt": 1,
                                              "verdict": "pass"}]})
        assert run["status"] == st.STATUS_AWAITING
        # финал человека
        res = store.close(run["id"], closed_by="u-boss", approve=True)
        assert res["ok"] and res["run"]["status"] == st.STATUS_CLOSED
        assert res["run"]["closed_by"] == "u-boss"
        # закрытый не переоткрывается
        again = store.close(run["id"], closed_by="u-2", approve=False)
        assert not again["ok"] and "уже закрыт" in again["error"]
    print("✅ прогон сохраняется, финал за человеком, закрытый не переоткрыть")


def test_failed_run_cannot_be_accepted():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store, st = _office_store(tmp)
        run = store.add(task_text="Задача", backend="openworker",
                        report={"status": "failed"})
        res = store.close(run["id"], closed_by="u", approve=True)
        assert not res["ok"] and "нечего" in res["error"], (
            "принять несуществующий результат нельзя"
        )
    print("✅ провал исполнителя принять нельзя — нечего")


def test_ui_zone_wired():
    page = _src("frontend/app/[locale]/page.tsx")
    assert "'office'" in page and "OfficeTasksPanel" in page, (
        "зона исполнителей обязана быть подключена во вкладки"
    )
    panel = _src("frontend/components/OfficeTasksPanel.tsx")
    assert "/api/v1/executor/office" in panel
    assert "runs/${id}/close" in panel or "/close" in panel
    assert "Принять" in panel and "Отклонить" in panel, (
        "финал человека — кнопки в интерфейсе, а не только API"
    )
    assert "не доказано" in panel, (
        "зона обязана объяснять смысл «без проверок — не доказано»"
    )
    print("✅ зона в интерфейсе: вкладка, запуск, финал человека")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты офисного исполнителя прошли.")
