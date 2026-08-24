# -*- coding: utf-8 -*-
"""SIMA: рабочая кнопка проверки и доступ к чужому проекту.

Аудит раздела «SIMA» нашёл три дефекта:

1. Кнопка «Проверить» на экране реализации слала только projectId, а
   хендлер требовал projectId + blockId + workspace — то есть проверить
   результат из интерфейса было нельзя ВООБЩЕ. Работала только
   автоматическая приёмка внутри цикла после прогона агента, а вопрос
   «а сейчас-то починилось?» из UI задать было нечем.
2. POST /ai/economics читал чужой проект по одному projectId: остальные
   SIMA-хендлеры зовут _require_project_access, этот не звал.
3. DELETE /data-sources удалял источник по id вообще без проверки
   владельца — деструктивная операция без единого замка.

Плюс роут автоописания связи был объявлен, но не зарегистрирован в
роутере: интерфейс дважды звал его и получал 404.

Тесты контрактные: сам роутер тянет litestar и БД, которых в песочнице нет.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── 1. Кнопка проверки работает ─────────────────────────────────────────

def test_verify_requires_only_project_id():
    src = _src("backend/api/routes/sima.py")
    start = src.index('async def kanon_verify(')
    body = src[start:start + 4000]
    assert 'if not project_id:' in body
    assert '"projectId, blockId, workspace обязательны"' not in body, (
        "кнопка проверки снова требует поля, которых интерфейс не шлёт"
    )
    print("✅ проверка запускается по одному projectId")


def test_verify_falls_back_to_default_workspace():
    src = _src("backend/api/routes/sima.py")
    start = src.index('async def kanon_verify(')
    body = src[start:start + 4000]
    assert "if not workspace:" in body
    assert "sima-projects" in body, "нужна папка проекта по умолчанию"
    print("✅ без workspace берётся папка проекта по умолчанию")


def test_verify_whole_project_when_no_block():
    src = _src("backend/api/routes/sima.py")
    start = src.index('async def kanon_verify(')
    body = src[start:start + 5000]
    assert "if not block_id:" in body
    assert "ready_for_handoff" in body, (
        "без blockId проверяются блоки с готовым контрактом"
    )
    assert '"counts"' in body, "нужна сводка вердиктов для интерфейса"
    assert "Нет блоков с проверяемым контрактом" in body, (
        "пустой случай обязан объясняться, а не молчать"
    )
    print("✅ без blockId проверяется весь проект, со сводкой вердиктов")


def test_ui_shows_verify_summary():
    src = _src("frontend/sima/components/Implementation/ImplementationPanel.tsx")
    assert "d?.counts" in src, "интерфейс обязан показывать сводку проверки"
    assert "нет доказательств" in src, (
        "inconclusive обязан читаться как «нет доказательств», а не как успех"
    )
    print("✅ интерфейс показывает сводку: прошло / упало / нет доказательств")


# ── 2. Доступ к проекту ─────────────────────────────────────────────────

def test_economics_checks_project_access():
    src = _src("backend/api/routes/sima.py")
    start = src.index('async def economics_route(')
    body = src[start:start + 1500]
    assert "_require_project_access" in body, (
        "экономика чужого проекта снова читается по одному projectId"
    )
    # Проверка обязана идти до чтения проекта
    check = body.index("_require_project_access")
    read = body.index("db.get_project(project_id)")
    assert check < read, "проверка доступа обязана идти до чтения проекта"
    print("✅ экономика проекта требует доступа к проекту")


def test_delete_data_source_checks_write_access():
    src = _src("backend/api/routes/sima.py")
    start = src.index('async def delete_data_source_route(')
    body = src[start:start + 1800]
    assert "_require_project_access" in body and "write=True" in body, (
        "удаление источника снова без проверки владельца"
    )
    assert "projectId обязателен" in body
    assert "не принадлежит этому проекту" in body, (
        "источник обязан проверяться на принадлежность проекту"
    )
    check = body.index("_require_project_access")
    delete = body.index("await delete_data_source(id)")
    assert check < delete, "проверка обязана идти до удаления"
    print("✅ удаление источника требует права записи на проект")


# ── 3. Роут автоописания связи зарегистрирован ──────────────────────────

def test_connection_description_route_registered():
    src = _src("backend/api/routes/sima.py")
    assert "async def generate_connection_description_route(" in src
    router = src[src.index("sima_router = Router("):]
    assert "generate_connection_description_route" in router, (
        "роут объявлен, но не зарегистрирован — интерфейс получает 404"
    )
    print("✅ роут автоописания связи зарегистрирован в роутере")


def test_ui_calls_registered_route():
    """Тот, кто его зовёт, должен звать существующий путь."""
    ui = _src("frontend/sima/components/BlockDetails/ConnectionDetailsPanel.tsx")
    assert "generate-connection-description" in ui
    src = _src("backend/api/routes/sima.py")
    assert '@post("/ai/generate-connection-description")' in src
    print("✅ путь, который зовёт интерфейс, существует на сервере")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты SIMA прошли.")
