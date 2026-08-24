"""Регрессия контракта инсайтов SIMA (Фаза 0.3).

Бэкенд сериализует ответы в camelCase (_to_camel_dict), поэтому ключ
источника у инсайта — dataSourceId, а связь с блоком — linkedBlockId.
Раньше и роут (/data-sources), и фронт читали snake_case → инсайты всегда
были пусты («не грузит контекст из встреч»). Тест фиксирует camelCase и
что группировка инсайтов по источнику НЕ теряет их."""
import sys

sys.path.insert(0, ".")

from backend.db.sima_client import _snake_to_camel, _to_camel_dict  # noqa: E402


def test_insight_keys_are_camel():
    assert _snake_to_camel("data_source_id") == "dataSourceId"
    assert _snake_to_camel("linked_block_id") == "linkedBlockId"
    assert _snake_to_camel("mime_type") == "mimeType"
    assert _snake_to_camel("insights_count") == "insightsCount"


def test_grouping_by_camel_source_id_keeps_insights():
    """Точный сценарий роута get_data_sources: инсайты (camelCase) должны
    сгруппироваться по источнику, а не упасть под None."""
    raw_insight = {"id": "i1", "data_source_id": "src-1",
                   "linked_block_id": "b1", "title": "x"}
    insight = _to_camel_dict(raw_insight)  # как отдаёт get_data_insights
    sources = [{"id": "src-1", "insights": []}]

    # Логика роута (после фикса): читаем dataSourceId, фолбэк на snake
    by_source = {}
    ds_id = insight.get("dataSourceId") or insight.get("data_source_id")
    by_source.setdefault(str(ds_id), []).append(insight)
    for s in sources:
        s["insights"] = by_source.get(str(s.get("id")), [])

    assert sources[0]["insights"], "инсайт должен попасть под свой источник"
    assert sources[0]["insights"][0]["linkedBlockId"] == "b1"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
