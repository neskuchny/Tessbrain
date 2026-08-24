# -*- coding: utf-8 -*-
"""Экран чтения психоаналитики: контроль доступа — контрактные тесты.

Слой PsychInsight хранится с грифом и не индексируется в поиск; роут
/psych-insights — единственный предусмотренный способ его читать, значит
весь контроль доступа обязан жить в нём. Тесты фиксируют контракт по
исходнику (импорт роута тянет litestar-стек, недоступный в песочнице):

  - персональная психоаналитика: сам / руководитель по цепочке / админ,
    отказ по умолчанию (fail-closed), включая случай сбоя проверки;
  - групповая динамика: только руководители, «сам»-доступа нет —
    динамика касается многих людей сразу;
  - выдача не собирается до проверки прав;
  - чувствительный слой грузится в UI по явному клику, а не автоматически.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_person_endpoint_checks_access_before_loading():
    src = _src("backend/api/routes/psych_insights.py")
    handler = src[src.index("async def person_insights"):]
    check = handler.index("_assert_may_read_person_insights")
    load = handler.index("_load_insights")
    assert check < load, "проверка прав обязана идти ДО загрузки данных"
    print("✅ права проверяются до загрузки данных")


def test_access_circle_is_self_manager_admin():
    src = _src("backend/api/routes/psych_insights.py")
    start = src.index("def _assert_may_read_person_insights")
    body = src[start:src.index("def _require_manager")]
    assert "owner_uid == asker_uid" in body, "сам человек видит своё"
    assert "is_manager_of" in body, "руководитель по цепочке подчинения"
    assert "_is_org_admin" in body, "администратор организации"
    # Fail-closed: и финальный отказ, и отказ при сбое проверки
    assert body.count("HTTPException") >= 2
    assert body.count("403") >= 2, (
        "сбой проверки прав обязан давать 403, а не пропуск"
    )
    print("✅ круг доступа: сам / руководитель / админ, fail-closed")


def test_meeting_dynamics_is_manager_only():
    src = _src("backend/api/routes/psych_insights.py")
    handler = src[src.index("async def meeting_dynamics"):]
    assert "_require_manager(uid)" in handler.split("_load_insights")[0], (
        "динамика встречи — только руководителям, проверка до загрузки"
    )
    guard = src[src.index("def _require_manager"):src.index("async def _load_insights")]
    assert '"founder", "admin", "manager"' in guard
    print("✅ групповая динамика — только руководителям")


def test_router_is_registered_in_app():
    app_src = _src("backend/api/app.py")
    assert "psych_insights" in app_src
    assert "psych_insights.router" in app_src
    print("✅ роутер подключён в приложение")


def test_response_carries_disclaimer():
    src = _src("backend/api/routes/psych_insights.py")
    assert "гипотезы, а не" in src and "диагнозы" in src.replace("\n", " "), (
        "выдача обязана нести дисклеймер «гипотезы, а не диагнозы»"
    )
    print("✅ выдача несёт дисклеймер")


def test_ui_block_is_lazy_and_shows_denial():
    src = _src("frontend/components/PsychInsightsBlock.tsx")
    assert '"idle"' in src and "onClick={load}" in src, (
        "чувствительный слой грузится по явному клику, не автоматически"
    )
    assert "403" in src and "denied" in src, (
        "отказ в доступе показывается честно, а не глотается"
    )
    panel = _src("frontend/components/SnapshotsPanel.tsx")
    assert "PsychInsightsBlock" in panel, "блок подключён в карточку человека"
    print("✅ UI: ленивая загрузка, честный показ отказа, блок в карточке")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты экрана психоаналитики прошли.")
