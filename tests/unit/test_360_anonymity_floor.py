# -*- coding: utf-8 -*-
"""Порог анонимности нельзя опустить запросом.

Найдено при сверке бизнес-документа с кодом. Документ обещал: «пока не
набралось 5 разных оценщиков, не раскрывается ничего». В коде `k` был
обычным query-параметром, а нормализация делала `max(1, int(k))` — то есть
запрос `?k=1` возвращал индивидуальные вклады с дословными комментариями,
начиная с первого оценщика. Анонимность, которую снимают параметром, — это
не анонимность.

Направление правки одно: поднять порог можно (это строже), опустить —
нельзя. DEFAULT_K стал жёстким полом.

Модуль грузится по пути файла: пакет backend.core.aggregation тянет
хранилище, а нормализация порога — чистая арифметика.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_core():
    name = "backend.core.aggregation.core"
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.aggregation"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "backend", "core", "aggregation", "core.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_core = _load_core()
_norm_k = _core._norm_k
DEFAULT_K = _core.DEFAULT_K
AggregateView = _core.AggregateView


def test_threshold_cannot_be_lowered():
    """Главный инвариант: k=1 больше не раскрывает вклады."""
    for attempt in (1, 2, 3, 4, 0, -10):
        assert _norm_k(attempt) == DEFAULT_K, (
            f"k={attempt} опустил порог до {_norm_k(attempt)} вместо {DEFAULT_K}"
        )
    print(f"✅ порог нельзя опустить ниже {DEFAULT_K}")


def test_threshold_can_be_raised():
    """Более строгий порог — безопасное направление, его разрешаем."""
    assert _norm_k(10) == 10
    assert _norm_k(100) == 100
    print("✅ порог можно поднять")


def test_missing_and_broken_k_fall_back_to_default():
    assert _norm_k(None) == DEFAULT_K
    assert _norm_k("нет") == DEFAULT_K
    assert _norm_k("") == DEFAULT_K
    assert _norm_k([]) == DEFAULT_K
    print("✅ отсутствующий и битый k → порог по умолчанию")


def test_default_is_five():
    """Документ обещает бизнесу именно 5 — число не должно тихо уехать."""
    assert DEFAULT_K == 5
    print("✅ порог по умолчанию = 5, как заявлено в документе")


def test_suppressed_view_hides_contributions():
    """Ниже порога наружу не уходит ни один вклад."""
    view = AggregateView(
        domain="review_360", aggregate_key="subj", distinct_count=3,
        k=5, suppressed=True,
        contributions=[{"comment": "тайна", "ratings": {"lead": 5}}],
    )
    out = view.to_dict()
    assert out["contributions"] == []
    assert out["suppressed"] is True
    assert "тайна" not in str(out)
    print("✅ подавленный срез не отдаёт вклады")


def test_visible_view_still_has_no_rater_identity():
    """Даже выше порога личности оценщиков в структуре нет."""
    view = AggregateView(
        domain="review_360", aggregate_key="subj", distinct_count=7,
        k=5, suppressed=False,
        contributions=[{"comment": "хорошо", "ratings": {"lead": 5}}],
    )
    out = view.to_dict()
    assert out["contributions"], "выше порога вклады должны отдаваться"
    assert not any(
        key in str(out) for key in
        ("contributor_user_id", "rater_user_id", "contributor", "rater")
    ), "в выдаче не должно быть полей с личностью оценщика"
    print("✅ в выдаче нет полей с личностью оценщика")


def test_comments_order_does_not_leak_submission_order():
    """Порядок подачи — побочный канал: кто отвечал первым, опознаётся."""
    src = open(os.path.join(ROOT, "backend/core/aggregation/review360.py"),
               encoding="utf-8").read()
    assert "sorted(" in src and "sha256" in src, (
        "комментарии обязаны перемешиваться по содержимому, а не идти в "
        "порядке подачи"
    )
    print("✅ комментарии перемешаны по содержимому, а не по времени подачи")


def test_manager_signal_uses_the_same_floor():
    """Сигнал наверх брал k из тела запроса мимо нормализации."""
    src = open(os.path.join(ROOT, "backend/core/aggregation/manager_signal.py"),
               encoding="utf-8").read()
    assert "max(1, int(k))" not in src, (
        "сигнал наверх снова опускает порог до единицы"
    )
    assert "_norm_k(k)" in src, "сигнал наверх обязан проходить через пол порога"
    print("✅ сигнал наверх использует тот же пол порога")


def test_route_checks_who_may_read():
    """Читать 360 могли все: эндпоинт проверял только валидность токена."""
    src = open(os.path.join(ROOT, "backend/api/routes/aggregation.py"),
               encoding="utf-8").read()
    assert "_assert_may_read_360" in src
    start = src.index("def _assert_may_read_360")
    body = src[start:src.index("\n\n\n", start)]
    assert "asker_uid == subject_user_id" in body, "сам человек — всегда"
    assert "is_manager_of" in body, "руководитель по цепочке подчинения"
    assert "_is_org_admin" in body, "администратор организации"
    assert "403" in body, "остальным — отказ"
    # Проверка вызывается до сборки ревью
    call = src.index("_assert_may_read_360(uid, subject_user_id)")
    build = src.index("await build_360_review(")
    assert call < build, "проверка прав обязана идти ДО сборки ревью"
    print("✅ 360 читают сам человек, его руководитель и админ — и только они")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты порога анонимности прошли.")
