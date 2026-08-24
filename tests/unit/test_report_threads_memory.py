# -*- coding: utf-8 -*-
"""Память образов и хвостов между визуальными отчётами (§9.1 VISUAL_REPORTS).

До фикса: seen_count копился, но (а) промпт не требовал визуального
НАКОПЛЕНИЯ тянущегося хвоста, (б) стор рос вечно (resolved/исчезнувшие
сюжеты жили бесконечно), (в) не было памяти сцены — серия могла повторяться
буквально."""
import backend.core.board.report_threads as rt

_UID = "11111111-1111-4111-8111-111111111111"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rt, "_path",
        lambda uid, scope: str(tmp_path / f"{uid}__{scope}.json"))


def test_tail_accumulates_across_reports(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    t = [{"title": "Риск задержки клиента", "status": "continuing"}]

    r1 = rt.reconcile(_UID, "weekly", t)
    assert r1[0]["is_new"] and r1[0]["seen_count"] == 1
    sym = r1[0]["symbol"]

    r2 = rt.reconcile(_UID, "weekly", t)
    r3 = rt.reconcile(_UID, "weekly", t)
    assert r3[0]["seen_count"] == 3
    assert r3[0]["symbol"] == sym            # символ стабилен через отчёты
    assert r3[0]["first_seen"] == r1[0]["first_seen"]

    # промпт требует визуального НАКОПЛЕНИЯ с меткой ×3
    block = rt.threads_prompt_block(r3, lang_ru=True)
    assert "ТЯНЕТСЯ 3-й отчёт" in block and "×3" in block
    # первый отчёт — без накопления
    assert "×1" not in rt.threads_prompt_block(r1, lang_ru=True)


def test_resolved_shown_once_then_forgotten(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    rt.reconcile(_UID, "s", [{"title": "Баг оплаты", "status": "continuing"}])
    r = rt.reconcile(_UID, "s", [{"title": "Баг оплаты", "status": "resolved"}])
    assert r[0]["status"] == "resolved"      # финал показан в этом отчёте
    # в следующем отчёте сюжет НЕ восстанавливается как старый
    r2 = rt.reconcile(_UID, "s", [{"title": "Баг оплаты", "status": "continuing"}])
    assert r2[0]["is_new"] and r2[0]["seen_count"] == 1


def test_missing_threads_age_out(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    rt.reconcile(_UID, "s", [{"title": "Старая тема", "status": "continuing"}])
    other = [{"title": "Другое", "status": "continuing"}]
    for _ in range(3):                        # 3 отчёта без упоминания
        rt.reconcile(_UID, "s", other)
    r = rt.reconcile(_UID, "s", [{"title": "Старая тема", "status": "continuing"}])
    assert r[0]["is_new"]                     # забыта → как новая


def test_scene_memory_anti_repeat(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert rt.previous_scene(_UID, "weekly") == ""
    assert rt.anti_repeat_block("", lang_ru=True) == ""   # серии нет → пусто

    rt.remember_scene(_UID, "weekly", "infographic/neon: изометрия, бирюза")
    prev = rt.previous_scene(_UID, "weekly")
    assert "неон" in prev or "neon" in prev
    block = rt.anti_repeat_block(prev, lang_ru=True)
    assert "ЗАМЕТНО другую композицию" in block and prev in block

    # хранится максимум 3 сцены, последняя — актуальная
    for i in range(5):
        rt.remember_scene(_UID, "weekly", f"сцена {i}")
    assert rt.previous_scene(_UID, "weekly") == "сцена 4"


def test_scenes_and_threads_share_store(monkeypatch, tmp_path):
    """Сцены и нити живут в одном файле scope — не затирают друг друга."""
    _isolate(monkeypatch, tmp_path)
    rt.reconcile(_UID, "w", [{"title": "Тема", "status": "continuing"}])
    rt.remember_scene(_UID, "w", "сцена А")
    r = rt.reconcile(_UID, "w", [{"title": "Тема", "status": "continuing"}])
    assert r[0]["seen_count"] == 2            # нить пережила запись сцены
    assert rt.previous_scene(_UID, "w") == "сцена А"
