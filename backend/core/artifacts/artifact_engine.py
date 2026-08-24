# -*- coding: utf-8 -*-
"""
Artifact Engine — генерация артефактов по СХЕМАМ-ДАННЫМ из контекста компании
(docs/CAPABILITY_READINESS.md, универсальная операция №4).

ЗАЧЕМ. Резюме сотрудника, отчёт по команде, КП, подготовка к переговорам —
один класс задач: «собери контекст о сущностях → изложи по структуре для
аудитории». Пайплайн под каждый тип не масштабируется. Здесь тип артефакта =
СХЕМА (данные): новый тип добавляется конфигурацией, без нового кода.

РАЗДЕЛЕНИЕ (скелет+LLM):
- СКЕЛЕТ (код, детерминированно): валидация схемы, сборка контекста из досье
  (entity_dossier.to_text — даты + карта покрытия), вычисление data_gaps
  (какие секции не обеспечены источниками — БЕЗ LLM), сборка промпта.
- LLM (инъектируется callable): только повествование по готовому контексту.
  Промпт наследует валидированные принципы analysis-режима: компилируй из
  разрозненных сигналов, отделяй факты от выводов, помечай выводы, не выдумывай
  при пустых данных (LoCoMo/LongMemEval - см. BENCHMARK_PUBLIC_LOCOMO_HOTPOT.md).

ЧЕСТНОСТЬ (§8): data_gaps вычисляются в коде и ЯВНО передаются модели и
вызывающему — артефакт не делает вид, что данных больше, чем есть.

Чистый stdlib + инъекция LLM (тестируется с моком, без инфры).
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ArtifactSection:
    """Секция артефакта: что в ней должно быть и какие источники досье критичны."""
    key: str
    title: str
    instruction: str            # содержательное требование к секции
    requires: Tuple[str, ...] = ()  # источники досье, без которых секция = gap


@dataclass(frozen=True)
class ArtifactSchema:
    """Тип артефакта как ДАННЫЕ. Новый тип = новый экземпляр, не код."""
    name: str
    description: str
    audience: str               # для кого пишем (тон/фокус)
    sections: Tuple[ArtifactSection, ...]
    language: str = "ru"

    @staticmethod
    def from_dict(d: dict) -> "ArtifactSchema":
        """Схема из словаря/JSON — типы артефактов можно хранить как конфиги."""
        sections = tuple(
            ArtifactSection(key=s["key"], title=s["title"],
                            instruction=s.get("instruction", ""),
                            requires=tuple(s.get("requires", ())))
            for s in d.get("sections", ()))
        if not d.get("name") or not sections:
            raise ValueError("schema requires 'name' and non-empty 'sections'")
        return ArtifactSchema(name=d["name"], description=d.get("description", ""),
                              audience=d.get("audience", ""), sections=sections,
                              language=d.get("language", "ru"))


@dataclass(frozen=True)
class Artifact:
    """Результат: текст + детерминированные метаданные честности."""
    schema_name: str
    text: str
    generated_at: str
    data_gaps: Tuple[str, ...] = ()       # секции без обеспеченных источников
    coverage: dict = field(default_factory=dict)  # entity → {source: status}


def compute_data_gaps(schema: ArtifactSchema, dossiers: Dict[str, "object"]) -> List[str]:
    """Какие секции схемы НЕ обеспечены данными (детерминированно, без LLM).

    Секция = gap, если НИ ОДНО досье не даёт ok ни по одному из её requires.
    Секции без requires считаются обеспеченными (пишутся из общего контекста).
    """
    ok_sources = set()
    for d in dossiers.values():
        for src, status in d.coverage().items():
            if status == "ok":
                ok_sources.add(src)
    gaps = []
    for s in schema.sections:
        if s.requires and not (set(s.requires) & ok_sources):
            gaps.append(s.key)
    return gaps


# Промпт-основа — наследует валидированные принципы analysis-режима.
_SYSTEM_BASE = (
    "Ты — аналитик Tessbrain. Создай артефакт «{name}» для аудитории: "
    "{audience}. Пиши на языке: {language}.\n"
    "ПРИНЦИПЫ:\n"
    "- Используй ТОЛЬКО предоставленный контекст (досье с датами и картой покрытия).\n"
    "- КОМПИЛИРУЙ выводы из разрозненных сигналов разных источников и дат — "
    "не отказывайся преждевременно.\n"
    "- Рассуждай о ПОРЯДКЕ и ИЗМЕНЕНИИ во времени, опирайся на даты; «сейчас» = "
    "самые свежие фрагменты.\n"
    "- ЧЕСТНОСТЬ: факты утверждай; выводы помечай («вероятно», «судя по обсуждениям»).\n"
    "- Для секций, помеченных как ПРОБЕЛ ДАННЫХ, честно напиши, каких данных не "
    "хватает — НЕ выдумывай содержимое.\n"
    "СТРУКТУРА (обязательные секции):\n{sections}"
)


def compose_prompt(schema: ArtifactSchema, dossiers: Dict[str, "object"],
                   *, extra_context: str = "", request: str = "") -> Tuple[str, str]:
    """Скелет: детерминированная сборка (system, user) промпта из схемы и досье."""
    gaps = set(compute_data_gaps(schema, dossiers))
    sec_lines = []
    for i, s in enumerate(schema.sections, 1):
        mark = "  [ПРОБЕЛ ДАННЫХ — честно укажи, чего не хватает]" if s.key in gaps else ""
        sec_lines.append(f"{i}. {s.title} — {s.instruction}{mark}")
    system = _SYSTEM_BASE.format(name=schema.name, audience=schema.audience or "—",
                                 language=schema.language, sections="\n".join(sec_lines))
    parts = []
    for entity, d in dossiers.items():
        parts.append(d.to_text())
    if extra_context:
        parts.append(f"=== ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ ===\n{extra_context}")
    user = "\n\n".join(parts) or "(контекст пуст)"
    if request:
        user = f"ЗАПРОС: {request}\n\n{user}"
    return system, user


def generate_artifact(schema: ArtifactSchema, dossiers: Dict[str, "object"],
                      llm: Callable[[str, str], str], *,
                      extra_context: str = "", request: str = "") -> Artifact:
    """Сгенерировать артефакт. llm(system, user) -> text — инъекция (мок в тестах)."""
    system, user = compose_prompt(schema, dossiers,
                                  extra_context=extra_context, request=request)
    text = llm(system, user)
    return Artifact(
        schema_name=schema.name,
        text=text or "",
        generated_at=_now_iso(),
        data_gaps=tuple(compute_data_gaps(schema, dossiers)),
        coverage={e: d.coverage() for e, d in dossiers.items()},
    )


# ---------------------------------------------------------------------------
# СХЕМЫ КАК ДАННЫЕ — примеры целевых сценариев продукта = конфиги, не код.
# Новый тип задачи = новый dict здесь ЛИБО ArtifactSchema.from_dict(json).
# ---------------------------------------------------------------------------

BUILTIN_SCHEMAS: Dict[str, ArtifactSchema] = {
    "person_summary": ArtifactSchema.from_dict({
        "name": "Резюме работы сотрудника",
        "description": "Как работал человек: задачи, динамика, сильные/слабые стороны",
        "audience": "руководитель",
        "sections": [
            {"key": "overview", "title": "Кто и чем занимался",
             "instruction": "роль, проекты, основные активности по датам",
             "requires": ["mentions", "events"]},
            {"key": "delivery", "title": "Выполнение задач",
             "instruction": "что выполнено/не выполнено, динамика во времени",
             "requires": ["events"]},
            {"key": "strengths_weaknesses", "title": "Где хорош / где слаб",
             "instruction": "компилируй из сигналов; выводы помечай как выводы"},
            {"key": "recommendations", "title": "Рекомендации",
             "instruction": "что улучшить, какая поддержка нужна"},
        ],
    }),
    "team_report": ArtifactSchema.from_dict({
        "name": "Отчёт о работе команды",
        "description": "Проблемы, достижения, динамика команды за период",
        "audience": "руководитель",
        "sections": [
            {"key": "status", "title": "Текущее состояние",
             "instruction": "по самым свежим данным; не путай со старыми",
             "requires": ["mentions", "events"]},
            {"key": "achievements", "title": "Достижения", "instruction":
                "конкретика с датами", "requires": ["events"]},
            {"key": "problems", "title": "Проблемы и узкие места",
             "instruction": "включая зреющие конфликты и противоречия"},
            {"key": "trend", "title": "Динамика",
             "instruction": "как было раньше vs сейчас, где перелом"},
        ],
    }),
    "commercial_proposal": ArtifactSchema.from_dict({
        "name": "Коммерческое предложение",
        "description": "КП под конкретного клиента из истории взаимодействия",
        "audience": "клиент (внешний документ)",
        "sections": [
            {"key": "client_needs", "title": "Понимание ваших задач",
             "instruction": "что клиенту важно — из истории встреч/обсуждений",
             "requires": ["mentions", "events", "facts"]},
            {"key": "fit", "title": "Точки соприкосновения",
             "instruction": "их потребности × наши сильные стороны; конкретно"},
            {"key": "offer", "title": "Предложение",
             "instruction": "что предлагаем, ценность, ожидаемый результат"},
            {"key": "next_steps", "title": "Следующие шаги",
             "instruction": "конкретный реалистичный план"},
        ],
    }),
    # Перевод коммуникации под получателя (целевой сценарий «руководитель
    # говорит идеологию — сотрудник должен понять НА СВОЁМ языке»). Досье
    # получателя (persona: метафоры/лексика/стиль/known_topics) — вход;
    # исходный спич — в request/extra_context. КЛЮЧЕВОЕ: смысл сохраняется,
    # меняется только подача; контроль смысла — отдельной секцией (честность).
    "speech_translation": ArtifactSchema.from_dict({
        "name": "Перевод сообщения под получателя",
        "description": "Пересказ сообщения на языке/метафорах/примерах получателя",
        "audience": "конкретный сотрудник (см. досье получателя)",
        "sections": [
            {"key": "translated", "title": "Сообщение для получателя",
             "instruction": ("перескажи ИСХОДНОЕ СООБЩЕНИЕ на языке получателя. "
                             "ОБЯЗАТЕЛЬНО: (а) возьми ОБЛАСТИ МЕТАФОР получателя из "
                             "досье и объясни ключевые идеи ЧЕРЕЗ НИХ (если у него "
                             "«стройка» — через фундамент/объекты/смету; если «спорт» "
                             "— через матчи/тренировки), (б) замени термины, которых "
                             "он не знает (см. досье), на его понятия, (в) приведи "
                             "пример из ЕГО работы (из досье). СМЫСЛ и ключевые тезисы "
                             "сохрани полностью — меняется только подача. Не упрощай "
                             "до искажения, не добавляй новых обещаний"),
             "requires": ["persona", "mentions"]},
            {"key": "meaning_check", "title": "Контроль смысла",
             "instruction": ("списком: какой тезис оригинала каким фрагментом "
                             "перевода передан; что НЕ удалось передать без потерь "
                             "— честно укажи")},
        ],
    }),
}
