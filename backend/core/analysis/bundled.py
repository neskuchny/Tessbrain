# -*- coding: utf-8 -*-
"""Готовые playbook-шаблоны (методики из коробки).

Как bundled skills: сотрудник не настраивает методику с нуля, а берёт
готовый шаблон (DD, финразбор, оценка КП) и кастомизирует под себя.
Это даёт «детерминированные процессы из коробки».

Шаблоны — чистые dict'ы (без id/org_id), которые превращаются в playbook
при выборе. Каждый — рабочий пример с dimensions/benchmark/output_template.
"""
from __future__ import annotations

from typing import Any

# Каждый шаблон — то что можно подать в POST /analysis/playbooks.
BUNDLED_PLAYBOOKS: dict[str, dict[str, Any]] = {
    "exploratory_generic": {
        "name": "Свободный разбор документа (без методики)",
        "analysis_type": "exploratory",
        "description": ("Когда у клиента нет готовой методологии: агент сам "
                        "изучает документ(ы), вытаскивает суть, цифры, риски, "
                        "скрытые связи и делает вывод под запрос клиента."),
        "company_context": {
            "use_snapshot": True,
            # Реверсивный разбор ВКЛЮЧЁН: гипотезы по скелету документа +
            # сверка цифр с метриками компании — ядро свободного анализа.
            "reverse_analysis": True,
            # План «что считать/что читать»: ИИ сам предлагает динамические
            # измерения и формулы (см. planning.py), считает safe-calc.
            "auto_plan": True,
            "custom": ("Конкретный запрос клиента (что именно анализировать и "
                       "что ему важно) приходит в client_context — приоритезируй "
                       "выводы под него."),
        },
        "dimensions": [
            {"key": "essence", "label": "Суть и ключевые тезисы",
             "rationale": "О чём документ, главные смыслы",
             "extraction_hint": "основные тезисы, цели, предмет, выводы документа",
             "kind": "qualitative"},
            {"key": "key_figures", "label": "Ключевые цифры и показатели",
             "rationale": "Числа, на которых строится оценка",
             "extraction_hint": "любые числа: суммы, проценты, метрики, сроки, "
                                "объёмы, цены, доли — с тем, что они означают",
             "kind": "quantitative"},
            {"key": "strengths", "label": "Сильные стороны / что хорошо",
             "rationale": "Что в документе выглядит сильно/выгодно",
             "extraction_hint": "преимущества, плюсы, сильные аргументы, выгоды",
             "kind": "qualitative"},
            {"key": "risks", "label": "Риски, слабые места, опасности",
             "rationale": "Что плохо, на что обратить внимание, red flags",
             "extraction_hint": "риски, противоречия, пробелы, завышенные "
                                "ожидания, юридические/финансовые опасности",
             "kind": "qualitative"},
            {"key": "hidden", "label": "Скрытое и неочевидное",
             "rationale": "Связи и выводы, не видные сразу",
             "extraction_hint": "неявные зависимости, причины-следствия, "
                                "несоответствия между разделами/документами",
             "kind": "qualitative"},
            {"key": "verdict", "label": "Вывод и как реагировать",
             "rationale": "Что это значит для клиента и что делать",
             "extraction_hint": "итоговая оценка, рекомендации, следующие шаги",
             "kind": "qualitative"},
        ],
        "output_template": {
            "blocks": [
                {"name": "summary", "label": "Главное за 30 секунд",
                 "description": "Суть + ключевой вывод одним абзацем под запрос клиента"},
                {"name": "figures", "label": "Цифры и расчёты",
                 "description": "Ключевые показатели; где есть формулы — посчитать"},
                {"name": "strengths_risks", "label": "Сильное и рискованное",
                 "description": "Что хорошо vs что опасно/слабо"},
                {"name": "hidden", "label": "Скрытое и взаимосвязи",
                 "description": "Неочевидные выводы, противоречия между документами"},
                {"name": "verdict", "label": "Вывод и рекомендации",
                 "description": "Что важно клиенту и как реагировать"},
            ],
        },
        "reference_report": (
            "# Разбор: <документ>\n\n"
            "## Главное за 30 секунд\n<суть + вывод под запрос клиента>\n\n"
            "## Цифры и расчёты\n<показатели; расчёты где возможны>\n\n"
            "## Сильное и рискованное\n<плюсы vs риски/опасности>\n\n"
            "## Скрытое и взаимосвязи\n<неочевидное, противоречия>\n\n"
            "## Вывод и рекомендации\n<что важно клиенту и что делать>"
        ),
    },

    "due_diligence_standard": {
        "name": "Due Diligence (стандарт)",
        "analysis_type": "due_diligence",
        "description": "Базовый DD таргета: долг, рост, юр-риски, fit",
        "company_context": {
            "use_snapshot": True,
            "custom": "Оцениваем компанию-цель для инвестиции/покупки. "
                      "Высокий долг и активные юр-риски — стоп-факторы.",
        },
        "dimensions": [
            {
                "key": "debt_load", "label": "Долговая нагрузка",
                "rationale": "Высокий долг → риск дефолта, снижает цену сделки",
                "extraction_hint": "обязательства, кредиты, займы, debt, equity, капитал",
                "kind": "quantitative",
                "computation": "debt / equity", "inputs": ["debt", "equity"],
                "benchmark": {"direction": "higher_worse", "red_flag": 2.0, "warning": 1.5},
            },
            {
                "key": "revenue_growth", "label": "Рост выручки",
                "rationale": "Растущая выручка оправдывает мультипликатор",
                "extraction_hint": "выручка, revenue, оборот, динамика по годам",
                "kind": "quantitative",
                "computation": "(rev_current - rev_prev) / rev_prev",
                "inputs": ["rev_current", "rev_prev"],
                "benchmark": {"direction": "higher_better", "red_flag": 0.0, "warning": 0.05},
            },
            {
                "key": "legal_risks", "label": "Юридические риски",
                "rationale": "Активные иски могут обнулить сделку",
                "extraction_hint": "судебные иски, претензии, споры, арбитраж",
                "kind": "qualitative",
            },
            {
                "key": "fit", "label": "Соответствие критериям",
                "rationale": "Должна подходить под инвест-мандат",
                "extraction_hint": "отрасль, размер, бизнес-модель, география",
                "kind": "qualitative",
            },
        ],
        "output_template": {
            "blocks": [
                {"name": "summary", "label": "Резюме и рекомендация",
                 "description": "GO/NO-GO + ключевой вывод одним абзацем"},
                {"name": "financials", "label": "Финансовые показатели",
                 "description": "долг и выручка с интерпретацией"},
                {"name": "risks", "label": "Риски", "description": "юридические и прочие red flags"},
                {"name": "recommendation", "label": "Итоговая рекомендация",
                 "description": "чёткая позиция с условиями"},
            ],
        },
        "reference_report": (
            "# Due Diligence: <Компания>\n\n"
            "**Рекомендация:** GO / NO-GO / GO с условиями\n\n"
            "## Резюме\n2-3 предложения с ключевым выводом.\n\n"
            "## Финансовые показатели\nДолг + выручка с интерпретацией.\n\n"
            "## Риски\nЮридические и прочие.\n\n"
            "## Итоговая рекомендация\nЧёткая позиция."
        ),
    },

    "financial_quarter": {
        "name": "Квартальный финразбор",
        "analysis_type": "financial",
        "description": "Маржа, runway, рост — здоровье бизнеса по отчёту",
        "company_context": {"use_snapshot": True},
        "dimensions": [
            {
                "key": "margin", "label": "Валовая маржа",
                "rationale": "Низкая маржа → проблемы юнит-экономики",
                "extraction_hint": "выручка, себестоимость, cost, revenue",
                "kind": "quantitative",
                "computation": "gross = rev - cost\nmargin = gross / rev\nmargin",
                "inputs": ["rev", "cost"],
                "benchmark": {"direction": "higher_better", "red_flag": 0.05, "warning": 0.10},
            },
            {
                "key": "runway", "label": "Runway (месяцев)",
                "rationale": "Сколько месяцев проживём без нового раунда",
                "extraction_hint": "денежные средства, cash, burn rate, расходы в месяц",
                "kind": "quantitative",
                "computation": "cash / monthly_burn",
                "inputs": ["cash", "monthly_burn"],
                "benchmark": {"direction": "higher_better", "red_flag": 6, "warning": 12},
            },
        ],
        "output_template": {
            "blocks": [
                {"name": "health", "label": "Здоровье бизнеса",
                 "description": "общий вывод по финансам"},
                {"name": "metrics", "label": "Ключевые метрики",
                 "description": "маржа, runway с интерпретацией"},
                {"name": "actions", "label": "Рекомендации",
                 "description": "что делать на основе цифр"},
            ],
        },
    },

    "vendor_proposal": {
        "name": "Оценка коммерческого предложения",
        "analysis_type": "custom",
        "description": "Сравнение КП поставщика по цене/срокам/условиям",
        "dimensions": [
            {
                "key": "price", "label": "Цена",
                "rationale": "Соответствие бюджету",
                "extraction_hint": "стоимость, цена, сумма, бюджет",
                "kind": "quantitative",
                "computation": "price", "inputs": ["price"],
            },
            {
                "key": "timeline", "label": "Сроки",
                "rationale": "Укладывается ли в дедлайн",
                "extraction_hint": "срок, дедлайн, недель, месяцев, поставка",
                "kind": "qualitative",
            },
            {
                "key": "terms", "label": "Условия",
                "rationale": "Гарантии, оплата, ответственность",
                "extraction_hint": "гарантия, предоплата, штрафы, SLA, условия",
                "kind": "qualitative",
            },
        ],
        "output_template": {
            "blocks": [
                {"name": "verdict", "label": "Вердикт", "description": "go/no-go по КП"},
                {"name": "details", "label": "Разбор по критериям",
                 "description": "цена/сроки/условия"},
            ],
        },
    },
}


def list_templates() -> list[dict[str, Any]]:
    """Краткий список доступных шаблонов (для UI выбора)."""
    return [
        {
            "template_id": tid,
            "name": tpl["name"],
            "analysis_type": tpl.get("analysis_type"),
            "description": tpl.get("description", ""),
            "dimensions_count": len(tpl.get("dimensions", [])),
        }
        for tid, tpl in BUNDLED_PLAYBOOKS.items()
    ]


def get_template(template_id: str) -> dict[str, Any] | None:
    """Полный шаблон по id (для создания playbook'а из него)."""
    tpl = BUNDLED_PLAYBOOKS.get(template_id)
    return dict(tpl) if tpl else None
