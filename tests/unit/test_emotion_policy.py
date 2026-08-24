# -*- coding: utf-8 -*-
"""Политика эмоций: команде (public) — чистим чувствительное; лично (private) —
сохраняем всё. Guardrail: личное о людях не утекает в публичную версию."""
from __future__ import annotations

from backend.core.board.emotion_policy import apply_emotion_policy, audience_of


def test_private_keeps_everything():
    d = {"name": "Иван", "attention": "перегрузка",
         "highlights": [{"text": "релиз"}, {"text": "конфликт", "sensitive": True}]}
    out = apply_emotion_policy(d, "private")
    assert out["attention"] == "перегрузка"
    assert len(out["highlights"]) == 2


def test_public_strips_attention_and_sensitive():
    d = {"attention": "признаки перегрузки",
         "highlights": [{"text": "релиз"}, {"text": "х", "sensitive": True}]}
    out = apply_emotion_policy(d, "public")
    assert "attention" not in out
    assert [h["text"] for h in out["highlights"]] == ["релиз"]


def test_public_keyword_fallback():
    d = {"highlights": [{"text": "выгорание команды"}, {"text": "рост продаж"}]}
    out = apply_emotion_policy(d, "public")
    assert [h["text"] for h in out["highlights"]] == ["рост продаж"]


def test_public_softens_organism_pain():
    d = {"organs": [{"name": "Поддержка", "state": "pain", "note": "выгорание", "sensitive": True},
                    {"name": "Продажи", "state": "growth", "note": "план+"}]}
    out = apply_emotion_policy(d, "public")
    assert out["organs"][0]["state"] == "tension"
    assert out["organs"][0]["note"] == ""
    assert out["organs"][1]["note"] == "план+"  # несенситивное не трогаем


def test_public_does_not_mutate_original():
    d = {"attention": "x", "highlights": [{"text": "y", "sensitive": True}]}
    apply_emotion_policy(d, "public")
    assert d["attention"] == "x" and len(d["highlights"]) == 1  # оригинал цел


def test_audience_of():
    assert audience_of("private") == "private"
    assert audience_of("лично") == "private"
    assert audience_of("public") == "public"
    assert audience_of("") == "public"
