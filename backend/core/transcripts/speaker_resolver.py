# -*- coding: utf-8 -*-
"""Self-introduction detection в транскриптах (issue #112 T-6).

Извлекает mapping `Speaker N → имя` из транскрипта на основе явных
само-представлений и обращений по имени. Полностью deterministic
(regex + морфология), без LLM-вызовов.

Зачем: диаризация даёт нам `Speaker 1, Speaker 2, ...` (анонимные),
но не имена. Этот модуль закрывает значительную часть identity
resolution — если кто-то явно представился ИЛИ к нему обратились,
mapping становится почти гарантированным (high confidence).

Паттерны:
  - «Привет, меня зовут Максим» → Speaker N (текущий) → Максим
  - «Я Максим» / «Я — Максим» → Speaker N → Максим
  - «Hi, my name is John» / «I'm John»
  - «Максим, что думаешь?» (обращение) → следующий Speaker = Максим
    (если ответ начинается с подтверждения «я думаю»)

Точки интеграции:
  - decision_extractor.py: новый `speakers_resolved` параметр в промпт
  - task_agent.py: то же
  - graph_builder._create_decisions/tasks: если speaker есть в mapping
    → attribution_confidence повышается до medium даже если LLM сказал low

Возвращаем confidence на каждый mapping:
  - 0.9: явное само-представление
  - 0.7: ответ на обращение по имени
  - 0.5: упоминание в контексте speaker'а (более слабый сигнал)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Само-представления — русский и английский
# Группа (\w[\w\-]+(?:\s+\w[\w\-]+)?) ловит как «Максим» так и «Максим Иванов»
_SELF_INTRO_PATTERNS = [
    # «меня зовут Максим» / «меня зовут Максим Иванов»
    (re.compile(
        r"(?:меня\s+зовут|зовут\s+меня)\s+([А-ЯЁA-Z][а-яёa-z]+"
        r"(?:[\s\-][А-ЯЁA-Z][а-яёa-z]+)?)",
        re.IGNORECASE | re.UNICODE,
    ), 0.9),
    # «я Максим» / «Я Максим» / «я — Максим» / «это я, Максим»
    # Capture name требует capital — иначе ловит 'я думаю', 'я хочу' и т.п.
    # Используем (?-i:) inline flag чтобы выключить IGNORECASE на имя.
    (re.compile(
        r"(?:^|\s)(?:[яЯ]|[ЭэEe]то\s+я,?)\s+[—–-]?\s*"
        r"(?-i:([А-ЯЁA-Z][а-яёa-z]+(?:[\s\-][А-ЯЁA-Z][а-яёa-z]+)?))\b",
        re.UNICODE,
    ), 0.85),
    # English: "my name is John" / "I'm John" / "I am John"
    (re.compile(
        r"\b(?:my\s+name\s+is|I'?m|I\s+am)\s+([A-Z][a-z]+"
        r"(?:[\s\-][A-Z][a-z]+)?)\b",
        re.IGNORECASE,
    ), 0.85),
    # «Здравствуйте, Максим» (само-приветствие)
    (re.compile(
        r"^(?:привет|здравствуйте|добрый\s+день|hi|hello),?\s+"
        r"(?:меня\s+зовут\s+)?([А-ЯЁA-Z][а-яёa-z]+)",
        re.IGNORECASE | re.UNICODE,
    ), 0.75),
]

# Обращение по имени: «Максим, что думаешь?» — слабее, но всё равно сигнал
# Используется только если следующий Speaker сразу же отвечает.
_ADDRESS_PATTERN = re.compile(
    r"^([А-ЯЁA-Z][а-яёa-z]+)\s*,\s*"
    r"(?:что\s+думаешь|что\s+скажешь|твой\s+ответ|твоё\s+мнение|"
    r"расскажи|подскажи|поясни)",
    re.IGNORECASE | re.UNICODE,
)

# Stop-list — слова которые матчатся как имена, но именами не являются.
# Часто встречаются в качестве "false positives" в русской речи.
_NAME_STOPLIST = frozenset({
    "так", "вот", "ну", "это", "там", "тут", "сейчас", "вчера", "завтра",
    "сегодня", "значит", "хорошо", "плохо", "yes", "no", "ok", "okay",
    "well", "right", "sure", "thank", "thanks", "ah", "oh", "hmm",
})


def _normalize_name(name: str) -> str:
    """Нормализовать имя — strip + capitalize первой буквы каждого слова."""
    parts = re.split(r"\s+", name.strip())
    return " ".join(p[:1].upper() + p[1:] for p in parts if p)


def _is_valid_name(name: str) -> bool:
    """Простая валидация: не stop-word, длина >= 2, начинается с буквы."""
    if not name or len(name) < 2:
        return False
    if name.lower() in _NAME_STOPLIST:
        return False
    # Не должно быть число или иной мусор
    return bool(re.match(r"^[А-ЯЁA-Za-zа-яё][А-ЯЁA-Za-zа-яё\s\-]+$", name))


def _parse_speaker_segments(transcript: str) -> List[Tuple[Optional[str], str]]:
    """Разбить транскрипт на сегменты по speaker labels.

    Возвращает [(speaker_id, text), ...]. speaker_id=None для пред-первого
    блока (если он есть, например преамбула).

    Поддерживает форматы:
      'Speaker 1: ...'
      'Speaker 1\n...'
      'SPEAKER_2: ...'
      'S1: ...'
    """
    # Расщепляем по началу строки, где есть Speaker N: ...
    # (re.split с group capturing сохранит speaker_id в результате)
    parts = re.split(
        r"(?:^|\n)\s*(?:[Ss][Pp][Ee][Aa][Kk][Ee][Rr][\s_]?(\d+)|S(\d+))"
        r"\s*[:\-—]\s*",
        transcript,
    )
    # parts: [pre_text, speaker1_id_or_None, speaker1_id_alt_or_None, text1,
    #         speaker2_id_or_None, speaker2_id_alt_or_None, text2, ...]
    segments: List[Tuple[Optional[str], str]] = []
    if not parts:
        return segments
    # Первый элемент — текст до первого Speaker N: (если есть)
    pre = parts[0].strip()
    if pre:
        segments.append((None, pre))
    # Дальше — тройки (id, id_alt, text)
    i = 1
    while i + 2 < len(parts):
        sid = parts[i] or parts[i + 1]
        text = parts[i + 2]
        if sid and text:
            segments.append((f"Speaker {sid}", text.strip()))
        i += 3
    return segments


def extract_speaker_introductions(
    transcript: str,
) -> Dict[str, Dict[str, Any]]:
    """Извлечь mapping Speaker N → {name, confidence, source}.

    Args:
        transcript: Сырой текст транскрипта со Speaker N: метками.
                    Если меток нет — возвращается пустой dict.

    Returns:
        {"Speaker 1": {"name": "Максим", "confidence": 0.9,
                       "source": "self_intro"},
         "Speaker 2": {"name": "Иван", "confidence": 0.7,
                       "source": "address_response"}}
    """
    if not transcript or not transcript.strip():
        return {}

    segments = _parse_speaker_segments(transcript)
    if not segments:
        return {}

    mapping: Dict[str, Dict[str, Any]] = {}

    # Первый проход: само-представления
    for speaker_id, text in segments:
        if speaker_id is None:
            continue
        # Проверяем первые 200 символов сегмента — само-представление
        # обычно в начале реплики.
        check_text = text[:200]
        for pattern, confidence in _SELF_INTRO_PATTERNS:
            m = pattern.search(check_text)
            if not m:
                continue
            name = _normalize_name(m.group(1))
            if not _is_valid_name(name):
                continue
            # Не перезаписываем более сильный сигнал более слабым
            existing = mapping.get(speaker_id)
            if existing and existing["confidence"] >= confidence:
                continue
            mapping[speaker_id] = {
                "name": name,
                "confidence": confidence,
                "source": "self_intro",
            }
            break

    # Второй проход: обращение по имени → следующий speaker отвечает
    # 'Максим, что думаешь?' → next speaker имеет имя 'Максим'
    for i in range(len(segments) - 1):
        speaker_id_a, text_a = segments[i]
        speaker_id_b, _ = segments[i + 1]
        if speaker_id_a is None or speaker_id_b is None:
            continue
        if speaker_id_a == speaker_id_b:
            continue  # тот же самый говорит дальше — не обращение
        m = _ADDRESS_PATTERN.match(text_a.strip())
        if not m:
            continue
        addressed_name = _normalize_name(m.group(1))
        if not _is_valid_name(addressed_name):
            continue
        # Если speaker_b уже само-представился — не downgrade'им
        existing = mapping.get(speaker_id_b)
        if existing and existing["confidence"] >= 0.7:
            continue
        mapping[speaker_id_b] = {
            "name": addressed_name,
            "confidence": 0.7,
            "source": "address_response",
        }

    return mapping


def resolve_speakers_for_extraction(
    transcript: str,
    known_participants: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Convenience helper для decision_extractor/task_agent.

    Возвращает простой dict Speaker N → name, готовый для подмешивания
    в LLM-промпт как participants_hint.

    Если known_participants задан — фильтруем: оставляем только тех, кто
    в списке (защита от случайного матча 'Здравствуйте, Иван' когда
    Ивана не было на встрече).
    """
    introductions = extract_speaker_introductions(transcript)
    if not introductions:
        return {}

    resolved: Dict[str, str] = {}
    known_lower = {p.lower() for p in (known_participants or [])}

    for speaker_id, info in introductions.items():
        name = info["name"]
        # Если есть white-list — проверяем match (case-insensitive,
        # допускаем частичное совпадение по первому имени).
        if known_lower:
            first_token = name.split()[0].lower()
            matched = any(
                first_token == p.split()[0].lower()
                or first_token in p.lower()
                for p in known_participants or []
            )
            if not matched:
                continue
        resolved[speaker_id] = name
    return resolved
