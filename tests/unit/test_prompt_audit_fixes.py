# -*- coding: utf-8 -*-
"""Фиксы аудита промптов визуальных отчётов vs концепция.

Дыры из аудита: (1) утечка sensitive в публичный мульти-портрет;
(2) «тишина» не защищена, мем навязывал юмор безусловно; (3) нити ×N только
у инфографики; (4) метафора/организм без severity→размер; (5) лимит текста
в картинке только у инфографики."""
from backend.core.board.comic_strip import build_comic_prompt
from backend.core.board.company_organism import build_organism_prompt
from backend.core.board.emotion_policy import apply_emotion_policy
from backend.core.board.meme_card import _EXTRACT_PROMPT as MEME_EXTRACT
from backend.core.board.meme_card import build_meme_prompt
from backend.core.board.metaphor_scene import build_metaphor_prompt
from backend.core.board.viz_lang import all_text_note


def test_emotion_policy_strips_nested_characters():
    """P0: мульти-портрет — sensitive-поля вложены в characters[]."""
    data = {"title": "Команда", "characters": [
        {"name": "Иван", "attention": "признаки перегрузки",
         "highlights": [{"text": "закрыл релиз"},
                        {"text": "конфликт с Петром", "sensitive": True}]},
        {"name": "Мария", "highlights": [{"text": "рост продаж"}]},
    ]}
    pub = apply_emotion_policy(data, "public")
    ivan = pub["characters"][0]
    assert "attention" not in ivan                      # личный блок срезан
    assert [h["text"] for h in ivan["highlights"]] == ["закрыл релиз"]
    assert pub["characters"][1]["highlights"]            # чужое не задето
    # личная версия — всё на месте
    priv = apply_emotion_policy(data, "private")
    assert priv["characters"][0]["attention"] == "признаки перегрузки"
    assert len(priv["characters"][0]["highlights"]) == 2


def test_meme_humor_conditional_and_no_person_jokes():
    assert "НИКОГДА" in MEME_EXTRACT and "над конкретными людьми" in MEME_EXTRACT
    assert "ровный" in MEME_EXTRACT                      # тишина в экстракции
    p = build_meme_prompt({"top": "А", "bottom": "Б", "scene": "офис"})
    assert "только если сам материал даёт повод" in p    # юмор не безусловный
    assert "не направлена на конкретного человека" in p
    assert "с юмором, но по делу" not in p               # старая формула убрана


def test_metaphor_and_organism_severity_mapping():
    m = build_metaphor_prompt({"metaphor": "корабль", "title": "Т",
                               "meaning": "смысл", "elements": []})
    assert "ИНТЕНСИВНОСТЬ = СЕРЬЁЗНОСТЬ" in m
    o = build_organism_prompt({"title": "Пульс", "organs": [
        {"name": "Продажи", "state": "pain", "note": "срыв срока"}]})
    assert "РАЗМЕР = СЕРЬЁЗНОСТЬ" in o
    assert "ВСЕ органы в норме" in o                     # тишина организма
    assert "ничего не выдумывай" in o                    # факты святы в картинке
    assert "2-4 ключевых слов" in o                      # кап на заметку


def test_all_text_note_caps_label_length_not_density():
    """Референсы пользователя — ПЛОТНЫЕ карты с короткими подписями: кап на
    длину каждой подписи, а не на их количество."""
    ru = all_text_note("ru")
    assert "КАЖДАЯ подпись КОРОТКАЯ" in ru and "2-5 слов" in ru
    assert "плотная карта читаема" in ru
    assert "Текста НЕМНОГО" not in ru      # старый кап убивал плотность
    en = all_text_note("en")
    assert "EVERY label is SHORT" in en and "dense map stays readable" in en


def test_comic_face_equals_tone_and_bubble_cap():
    p = build_comic_prompt({"title": "Неделя", "panels": [
        {"caption": "Старт", "line": "Поехали", "mood": "up"}]})
    assert "ЛИЦО В КАЖДОМ КАДРЕ = ТОН" in p
    assert "максимум 6 слов" in p


def test_extractions_request_threads():
    """Нити ×N теперь просят все форматы (движок вплетает их универсально)."""
    from backend.core.board.character_portrait import (
        _EXTRACT_PROMPT as CHAR)
    from backend.core.board.comic_strip import _EXTRACT_PROMPT as COMIC
    from backend.core.board.company_organism import _EXTRACT_PROMPT as ORG
    from backend.core.board.metaphor_scene import _EXTRACT_PROMPT as MET
    for name, pr in (("comic", COMIC), ("organism", ORG),
                     ("metaphor", MET), ("character", CHAR)):
        assert '"threads"' in pr, f"{name}: нет поля threads"
        assert "не выдумывай" in pr.lower(), name
