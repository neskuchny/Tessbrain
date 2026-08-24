# -*- coding: utf-8 -*-
"""Сборка транскрипта из локального захвата.

Главное, что здесь проверяется: реплика, услышанная с двух устройств,
попадает в транскрипт один раз и с правильным автором. Без этого запись
встречи двумя участниками задваивала бы половину разговора.
"""
from __future__ import annotations

from backend.core.capture.local_session import (
    Segment,
    Track,
    build_session,
    merge_tracks,
    parse_session,
    resolve_names,
    to_transcript_text,
)


def seg(start, end, text, speaker="", device=""):
    return {"start": start, "end": end, "text": text,
            "speaker": speaker, "device": device}


# ── разбор входа ────────────────────────────────────────────────────


def test_parse_basic_session():
    tracks = parse_session({"tracks": [
        {"kind": "own", "owner_user_id": "u-1", "owner_name": "Иван",
         "device_id": "mac-1",
         "segments": [seg(0.0, 2.0, "Начнём с бюджета")]},
        {"kind": "system", "device_id": "mac-1",
         "segments": [seg(2.5, 5.0, "Согласен", "Speaker 1")]},
    ]})
    assert len(tracks) == 2
    assert tracks[0].kind == "own" and tracks[0].owner_name == "Иван"
    assert tracks[1].segments[0].speaker_label == "Speaker 1"


def test_parse_skips_garbage_without_raising():
    tracks = parse_session({"tracks": [
        "не словарь",
        {"kind": "own", "segments": [
            {"text": ""},                       # пустой текст
            {"text": "ok", "start": "abc"},     # кривое время
            seg(1.0, 2.0, "нормальная реплика"),
        ]},
    ]})
    assert len(tracks) == 1
    assert [s.text for s in tracks[0].segments] == ["нормальная реплика"]


def test_parse_empty_payload():
    assert parse_session({}) == []
    assert parse_session(None) == []


def test_reversed_timestamps_are_fixed():
    tracks = parse_session({"tracks": [
        {"kind": "own", "segments": [seg(5.0, 2.0, "задом наперёд")]}]})
    s = tracks[0].segments[0]
    assert s.start == 2.0 and s.end == 5.0


def test_unknown_kind_treated_as_system():
    tracks = parse_session({"tracks": [
        {"kind": "чужое", "segments": [seg(0, 1, "x")]}]})
    assert tracks[0].kind == "system"


# ── атрибуция своей дорожки ─────────────────────────────────────────


def test_own_track_author_is_certain():
    """Микрофон владельца — единственное место с полной уверенностью."""
    tracks = parse_session({"tracks": [
        {"kind": "own", "owner_user_id": "u-1", "owner_name": "Иван",
         "segments": [seg(0.0, 2.0, "Моя реплика")]}]})
    merged = merge_tracks(tracks)
    assert merged[0].author_name == "Иван"
    assert merged[0].author_user_id == "u-1"
    assert merged[0].confidence == 1.0


def test_system_track_has_no_author_yet():
    tracks = parse_session({"tracks": [
        {"kind": "system", "segments": [seg(0.0, 2.0, "Чужая", "Speaker 1")]}]})
    merged = merge_tracks(tracks)
    assert merged[0].author_name == ""
    assert merged[0].confidence == 0.0


def test_merged_output_is_chronological():
    tracks = parse_session({"tracks": [
        {"kind": "own", "owner_name": "Иван",
         "segments": [seg(10.0, 12.0, "третья")]},
        {"kind": "system",
         "segments": [seg(0.0, 2.0, "первая", "Speaker 1"),
                      seg(5.0, 6.0, "вторая", "Speaker 1")]},
    ]})
    merged = merge_tracks(tracks)
    assert [s.text for s in merged] == ["первая", "вторая", "третья"]


# ── дедупликация: встречу пишут двое ────────────────────────────────


def _two_devices():
    """Иван и Пётр оба с приложением. Реплика Петра слышна у Ивана в
    системной дорожке — и наоборот."""
    return {"tracks": [
        # устройство Ивана
        {"kind": "own", "owner_user_id": "u-1", "owner_name": "Иван",
         "device_id": "mac-1",
         "segments": [seg(0.0, 3.0, "Предлагаю поднять цены на двадцать")]},
        {"kind": "system", "device_id": "mac-1",
         "segments": [seg(4.0, 7.0,
                          "Не согласен это оттолкнёт крупных клиентов",
                          "Speaker 1")]},
        # устройство Петра
        {"kind": "own", "owner_user_id": "u-2", "owner_name": "Пётр",
         "device_id": "win-2",
         "segments": [seg(4.1, 7.2,
                          "Не согласен это оттолкнёт крупных клиентов")]},
        {"kind": "system", "device_id": "win-2",
         "segments": [seg(0.1, 3.1,
                          "Предлагаю поднять цены на двадцать",
                          "Speaker 1")]},
    ]}


def test_two_devices_no_duplicates():
    """Каждая реплика попадает в транскрипт ровно один раз."""
    merged = merge_tracks(parse_session(_two_devices()))
    assert len(merged) == 2, [s.text for s in merged]


def test_two_devices_everyone_named():
    """Оба участника с приложением — имена известны без диаризации."""
    merged = merge_tracks(parse_session(_two_devices()))
    names = {s.author_name for s in merged}
    assert names == {"Иван", "Пётр"}
    assert all(s.confidence == 1.0 for s in merged)


def test_own_wins_over_system_copy():
    """Побеждает own: звук чище и автор точен."""
    merged = merge_tracks(parse_session(_two_devices()))
    for s in merged:
        assert s.track_kind == "own"
        assert s.speaker_label == "", "анонимная копия не должна остаться"


def test_external_participant_survives_dedup():
    """Внешний человек есть только в системной дорожке — его не потерять."""
    payload = _two_devices()
    payload["tracks"][1]["segments"].append(
        seg(10.0, 13.0, "А что с юридической стороной вопроса", "Speaker 2"))
    merged = merge_tracks(parse_session(payload))
    texts = [s.text for s in merged]
    assert "А что с юридической стороной вопроса" in texts
    ext = next(s for s in merged if s.speaker_label == "Speaker 2")
    assert ext.author_name == ""


def test_similar_time_different_words_kept():
    """Люди говорят одновременно — это две разные реплики, не дубль."""
    payload = {"tracks": [
        {"kind": "own", "owner_name": "Иван", "device_id": "mac-1",
         "segments": [seg(0.0, 3.0, "Предлагаю поднять цены на двадцать")]},
        {"kind": "system", "device_id": "win-2",
         "segments": [seg(0.1, 3.1,
                          "Совершенно другая мысль про сроки поставки",
                          "Speaker 1")]},
    ]}
    assert len(merge_tracks(parse_session(payload))) == 2


def test_same_device_echo_not_dropped():
    """В пределах одного устройства own и system — разные люди, не эхо."""
    payload = {"tracks": [
        {"kind": "own", "owner_name": "Иван", "device_id": "mac-1",
         "segments": [seg(0.0, 3.0, "Предлагаю поднять цены на двадцать")]},
        {"kind": "system", "device_id": "mac-1",
         "segments": [seg(0.0, 3.0, "Предлагаю поднять цены на двадцать",
                          "Speaker 1")]},
    ]}
    assert len(merge_tracks(parse_session(payload))) == 2


def test_clock_skew_tolerated():
    """Часы устройств расходятся — дедупликация всё равно срабатывает."""
    payload = {"tracks": [
        {"kind": "own", "owner_name": "Пётр", "device_id": "win-2",
         "segments": [seg(10.0, 13.0, "Нужно пересмотреть сроки поставки")]},
        {"kind": "system", "device_id": "mac-1",
         "segments": [seg(11.5, 14.5, "Нужно пересмотреть сроки поставки",
                          "Speaker 1")]},
    ]}
    merged = merge_tracks(parse_session(payload))
    assert len(merged) == 1
    assert merged[0].author_name == "Пётр"


# ── имена из разговора, не из голоса ────────────────────────────────


def test_self_introduction_resolves_name():
    segs = [
        Segment(0.0, 3.0, "Здравствуйте, меня зовут Максим",
                speaker_label="Speaker 1"),
        Segment(3.5, 6.0, "Очень приятно", speaker_label="Speaker 2"),
    ]
    resolve_names(segs)
    assert segs[0].author_name == "Максим"
    assert segs[0].confidence > 0


def test_attendee_list_filters_wrong_guesses():
    """Имени нет среди участников — скорее ошибка разбора, чем новый человек."""
    segs = [Segment(0.0, 3.0, "Меня зовут Максим", speaker_label="Speaker 1")]
    resolve_names(segs, attendee_names=["Иван", "Пётр"])
    assert segs[0].author_name == "", "не приписываем постороннее имя"


def test_attendee_list_confirms_right_guess():
    segs = [Segment(0.0, 3.0, "Меня зовут Максим", speaker_label="Speaker 1")]
    resolve_names(segs, attendee_names=["Максим", "Иван"])
    assert segs[0].author_name == "Максим"


def test_own_track_name_not_overwritten():
    segs = [Segment(0.0, 3.0, "Меня зовут Максим", author_name="Иван",
                    author_user_id="u-1", confidence=1.0)]
    resolve_names(segs)
    assert segs[0].author_name == "Иван", "аккаунт точнее разбора текста"


# ── вывод ───────────────────────────────────────────────────────────


def test_transcript_keeps_unknown_speaker_visible():
    """Неопознанный остаётся «Speaker N» — честнее правдоподобной подмены."""
    text = to_transcript_text([
        Segment(0.0, 2.0, "Первая", author_name="Иван"),
        Segment(2.0, 4.0, "Вторая", speaker_label="Speaker 2"),
    ])
    assert text == "Иван: Первая\nSpeaker 2: Вторая"


def test_build_session_reports_attribution_rate():
    result = build_session(_two_devices())
    assert result["status"] == "success"
    st = result["stats"]
    assert st["segments"] == 2
    assert st["own_tracks"] == 2
    assert st["attributed_certain"] == 2
    assert st["attribution_rate"] == 1.0
    assert st["unknown_speakers"] == 0


def test_build_session_counts_unknown_honestly():
    payload = {"tracks": [
        {"kind": "own", "owner_name": "Иван",
         "segments": [seg(0.0, 2.0, "Своя реплика")]},
        {"kind": "system",
         "segments": [seg(3.0, 5.0, "Чужая реплика", "Speaker 1")]},
    ]}
    st = build_session(payload)["stats"]
    assert st["attributed_named"] == 1
    assert st["unknown_speakers"] == 1
    assert st["attribution_rate"] == 0.5


def test_build_session_empty_input():
    r = build_session({})
    assert r["stats"]["segments"] == 0
    assert r["stats"]["attribution_rate"] == 0.0
    assert r["transcript"] == ""
