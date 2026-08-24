# -*- coding: utf-8 -*-
"""Опыт компании: уверенность, петля обучения и честность выдачи.

Аудит раздела «Опыт компании как актив» нашёл четыре дефекта, каждый из
которых искажает смысл того, что слой обещает:

1. Уверенность считалась по числу РЕШЕНИЙ в кластере (`known >= N or
   total >= N`). Паттерн из десяти решений, о судьбе которых не известно
   ничего, объявлялся «high» — при том что комментарий над самой строкой
   гласил «на основе числа случаев С известными исходами».
2. Петля «решение → исход → распределение» была разомкнута: пересчёт
   статистики вызывался только после кластеризации новых решений, а
   типовой случай обратный — решение кластеризовали в марте, исход
   появился в июне. Привязка исхода до распределения не доходила.
3. Сбой поиска в индексе молча превращался в ответ «в архиве компании нет
   похожих паттернов» — утверждение о содержимом архива, к которому не
   было доступа.
4. При пустой базе в ТЗ и чат уходила строка «Зафиксируйте решение в
   Tessbrain для накопления паттернов» под заголовком «критерии из опыта
   прошлых решений»: рекламная подпись, выданная за опыт компании.

Тесты проверяют арифметику и контракты по исходникам — сам движок требует
Postgres, Qdrant и OpenAI, которых в песочнице нет.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _confidence(known: int, total: int, hi: int = 10, med: int = 3) -> str:
    """Копия правила из _update_pattern_stats. Сверяется с оригиналом
    тестом test_confidence_rule_matches_source ниже."""
    if known >= hi:
        return "high"
    if known >= med:
        return "medium"
    return "low"


# ── 1. Уверенность привязана к исходам ──────────────────────────────────

def test_no_outcomes_means_low_confidence():
    """Главный случай: десять обсуждений, ноль известных исходов."""
    assert _confidence(known=0, total=10) == "low"
    assert _confidence(known=0, total=100) == "low"
    print("✅ кластер без единого исхода — низкая уверенность")


def test_confidence_grows_with_known_outcomes():
    assert _confidence(known=3, total=3) == "medium"
    assert _confidence(known=10, total=10) == "high"
    assert _confidence(known=2, total=50) == "low"
    print("✅ уверенность растёт от исходов, а не от числа обсуждений")


def test_confidence_rule_matches_source():
    """Ветка `or total >= ...` не должна вернуться."""
    src = _src("backend/core/wisdom/pattern_encoder.py")
    start = src.index("known = dist[\"success\"]")
    block = src[start:start + 500]
    assert "or total >=" not in block, (
        "уверенность снова считается по числу решений, а не по исходам"
    )
    assert "if known >= self.MIN_HIGH_CONFIDENCE:" in block
    assert "elif known >= self.MIN_MEDIUM_CONFIDENCE:" in block
    print("✅ правило уверенности в исходнике опирается только на known")


# ── 2. Петля обучения замкнута ──────────────────────────────────────────

def test_refresh_pattern_stats_exists():
    src = _src("backend/core/wisdom/pattern_encoder.py")
    assert "async def refresh_pattern_stats(" in src, (
        "нужен пересчёт статистики БЕЗ кластеризации новых решений"
    )
    start = src.index("async def refresh_pattern_stats(")
    body = src[start:src.index("async def encode_incremental(", start)]
    assert "_update_pattern_stats" in body, "пересчёт обязан обновлять статистику"
    assert "BWEOutcomeEvent" in body, "пересчёт обязан читать исходы"
    print("✅ есть пересчёт статистики без новых решений")


def test_link_outcome_refreshes_stats():
    """После привязки исхода распределение обязано обновиться."""
    src = _src("backend/core/wisdom/wisdom_engine.py")
    start = src.index("async def link_outcome(")
    body = src[start:src.index("async def query(", start)]
    assert "refresh_pattern_stats" in body, (
        "привязка исхода не доходит до распределения паттерна"
    )
    print("✅ привязка исхода пересчитывает распределение")


def test_nightly_recompute_refreshes_stats():
    src = _src("backend/core/wisdom/wisdom_engine.py")
    start = src.index("async def recompute_patterns(")
    body = src[start:start + 1600]
    assert "refresh_pattern_stats" in body, (
        "ночной прогон без новых решений не обновлял распределение"
    )
    print("✅ ночной прогон пересчитывает распределение")


# ── 3. Недоступный индекс ≠ пустой архив ────────────────────────────────

def test_index_failure_raises_instead_of_empty():
    src = _src("backend/core/wisdom/wisdom_index.py")
    assert "class WisdomIndexUnavailable" in src
    start = src.index("Qdrant search failed")
    tail = src[start:start + 300]
    assert "raise WisdomIndexUnavailable" in tail, (
        "сбой поиска снова маскируется под пустой результат"
    )
    print("✅ сбой индекса поднимает отдельное исключение")


def test_engine_reports_unavailable_archive_honestly():
    src = _src("backend/core/wisdom/wisdom_engine.py")
    assert "except WisdomIndexUnavailable" in src
    start = src.index("except WisdomIndexUnavailable")
    block = src[start:start + 900]
    assert "недоступен" in block, "нужен честный текст про недоступность"
    assert "сбой доступа к хранилищу" in block, (
        "ответ обязан отличать сбой доступа от отсутствия опыта"
    )
    assert "отсутствие опыта" in block
    # И не должен утверждать обратное
    assert "нет похожих паттернов" not in block
    print("✅ при недоступном архиве система не утверждает, что он пуст")


# ── 4. Пустая база не подмешивает рекламу в ТЗ ──────────────────────────

def test_brief_is_empty_without_patterns():
    src = _src("backend/core/wisdom/wisdom_engine.py")
    start = src.index("async def build_wisdom_brief(")
    body = src[start:start + 2500]
    assert 'if not getattr(resp, "similar_patterns", None):' in body, (
        "бриф обязан быть пустым, когда паттернов нет"
    )
    assert 'brief.append(f"💡 ОПЫТ КОМПАНИИ: {resp.recommendation}")' not in body, (
        "рекламная строка снова выдаётся за опыт компании"
    )
    print("✅ при пустой базе блок «опыт компании» не выдаётся вовсе")


# ── 5. Исход нельзя записать к чужому решению ───────────────────────────

def test_outcome_checks_decision_owner():
    src = _src("backend/core/wisdom/outcome_linker.py")
    start = src.index("async def link_outcome_to_decision(")
    body = src[start:start + 3000]
    assert "BWEDecisionEvent.user_id" in body, (
        "нужна проверка владельца решения перед записью исхода"
    )
    assert "str(owner_uid) != str(user_id)" in body
    # Проверка обязана стоять до создания записи
    check = body.index("owner_uid")
    create = body.index("outcome = BWEOutcomeEvent(")
    assert check < create, "проверка владельца обязана идти до создания исхода"
    print("✅ исход нельзя привязать к чужому решению")


def test_outcome_dedup_scoped_to_user():
    src = _src("backend/core/wisdom/outcome_linker.py")
    start = src.index("# Проверка дубликатов")
    block = src[start:start + 500]
    assert "BWEOutcomeEvent.user_id == user_id" in block, (
        "дедуп исходов обязан быть в границах одного владельца данных"
    )
    print("✅ дедуп исходов ограничен своим владельцем данных")


# ── 6. Включение слоя и автонаполнение ─────────────────────────────────

def test_wisdom_flags_enabled_by_default():
    """Решение владельца продукта: слой включён, а не спрятан за флагами."""
    src = _src("backend/config.py")
    for flag in ("crystal_enrichment_enabled: bool = True",
                 "wisdom_in_tasker_enabled: bool = True",
                 "wisdom_in_search_enabled: bool = True"):
        assert flag in src, f"флаг выключен: {flag}"
    print("✅ затухание, wisdom-в-чате и wisdom-в-ТЗ включены по умолчанию")


def test_meeting_worker_feeds_bwe():
    """Слой наполняется при обработке встречи, а не только ручным API."""
    src = _src("backend/workers/taskiq_app.py")
    assert "get_wisdom_engine" in src, "воркер не вызывает слой опыта"
    start = src.index("BWE_AUTO_INGEST")
    block = src[start - 200:start + 1800]
    assert "process_meeting" in block
    assert "is_retrospective" in block, "ретро-эвристика обязана передаваться"
    assert "except Exception" in block, "сбой опыта не должен ронять встречу"
    print("✅ обработка встречи наполняет слой опыта (BWE_AUTO_INGEST)")


# ── 7. Привязка исходов укреплена ───────────────────────────────────────

def test_direct_threshold_raised():
    src = _src("backend/core/wisdom/outcome_linker.py")
    assert "DIRECT_THRESHOLD = 0.85" in src, (
        "порог прямого принятия снова опущен — 0.72 путает соседние темы"
    )
    print("✅ прямое принятие только при почти дословном совпадении (0.85)")


def test_same_meeting_excluded_from_candidates():
    src = _src("backend/core/wisdom/outcome_linker.py")
    assert "exclude_meeting_id" in src
    start = src.index("def _find_matching_decision")
    body = src[start:start + 2500]
    assert "meeting_id != exclude_meeting_id" in body, (
        "исход снова может подтверждать решение из той же встречи"
    )
    print("✅ решения той же встречи не считаются кандидатами")


# ── 8. Случаи, а не упоминания ──────────────────────────────────────────

def test_distribution_dedupes_cases():
    src = _src("backend/core/wisdom/pattern_encoder.py")
    start = src.index("async def _update_pattern_stats")
    body = src[start:start + 3500]
    assert "cases" in body and "setdefault" in body, (
        "распределение снова считается по упоминаниям, а не по случаям"
    )
    assert 'if "failure" in types:' in body.split('if not outcomes:')[1], (
        "при противоречивых свидетельствах провал обязан весить больше успеха"
    )
    print("✅ распределение по случаям, провал приоритетнее при противоречии")


def test_decay_anchor_moves_only_on_new_outcomes():
    src = _src("backend/core/wisdom/pattern_encoder.py")
    assert "_known_changed" in src, (
        "якорь затухания снова омолаживается любым пересчётом"
    )
    print("✅ якорь затухания переставляется только при новых исходах")


def test_llm_context_uses_decayed_confidence_and_examples():
    src = _src("backend/core/wisdom/wisdom_engine.py")
    start = src.index("conf_str = details.get(")
    block = src[start - 600:start + 1600]
    assert "с учётом давности" in block, (
        "«мудрец» снова видит незатухшую уверенность"
    )
    assert "recent_decisions" in block, (
        "в контекст обязаны идти примеры реальных исходов, не только счётчики"
    )
    print("✅ мудрец видит состаренную уверенность и примеры исходов")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты честности слоя опыта прошли.")
