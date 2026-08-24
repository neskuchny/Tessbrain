# -*- coding: utf-8 -*-
"""Доменная модель «Аналитический разбор» (Analysis Playbook + Run).

Контекст. Сотрудники регулярно получают большие документы (отчёты на
100 страниц с цифрами/таблицами/фактами) и должны выдать структурный
вывод — с точки зрения компании, по фиксированному набору параметров,
с расчётами. Это повторяемый процесс, который меняется только данными
(новый клиент/документ), но не методикой.

Две сущности:

  AnalysisPlaybook — МЕТОДИКА разбора (настраивается раз, гоняется N раз):
    - dimensions[]: параметры анализа (на что смотрим + ПОЧЕМУ + как считаем
      + с чем сравниваем)
    - company_context: что подмешивать (snapshot/rule_book/custom)
    - reference_report: пример идеального отчёта (few-shot)
    - output_template: структура вывода

  AnalysisRun — ПРОГОН на конкретных документах:
    - input_document_ids + опц. client_context
    - петля: parse → extract → compute → analyze → assemble
    - steps_log делает процесс прозрачным/воспроизводимым

Первый таргет — due-diligence/аудит: микс количественных метрик
(долговая нагрузка, тренд выручки) и качественных факторов (юр-риски,
red flags, соответствие критериям).
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

# === Enums ==================================================================


class DimensionKind(str, Enum):
    """Тип параметра анализа."""
    QUANTITATIVE = "quantitative"  # цифры + computation + benchmark
    QUALITATIVE = "qualitative"    # факты/риски, LLM-оценка без расчёта


class RunStatus(str, Enum):
    """Фазы петли обработки. Порядок = порядок выполнения."""
    PENDING = "pending"
    PARSING = "parsing"        # INGEST: документы → текст/таблицы → chunks
    EXTRACTING = "extracting"  # EXTRACT: факты/цифры по dimensions
    COMPUTING = "computing"    # COMPUTE: Python-расчёты
    ANALYZING = "analyzing"    # ANALYZE: LLM-вывод по dimensions
    ASSEMBLING = "assembling"  # ASSEMBLE: финальный отчёт
    DONE = "done"
    FAILED = "failed"


# Терминальные статусы (run больше не двигается).
TERMINAL_STATUSES = frozenset({RunStatus.DONE.value, RunStatus.FAILED.value})


# === Dimension =============================================================


@dataclass
class AnalysisDimension:
    """Один параметр анализа в playbook'е.

    Пример (due-diligence, долговая нагрузка):
        key="debt_load", label="Долговая нагрузка",
        rationale="Высокий долг → риск дефолта при росте ставки",
        extraction_hint="Найди обязательства, кредиты, займы, debt, equity",
        kind="quantitative",
        computation="debt / equity",
        inputs=["debt", "equity"],
        benchmark={"red_flag": 2.0, "warning": 1.5},   # debt/equity > N
        weight=2.0
    """
    key: str
    label: str
    rationale: str = ""                       # ПОЧЕМУ этот параметр важен
    extraction_hint: str = ""                 # что искать в документе
    kind: str = DimensionKind.QUALITATIVE.value
    # Для quantitative: safe-calc выражение над извлечёнными inputs.
    computation: Optional[str] = None
    # Опц.: произвольный Python-код над `data` (извлечённые факты) — для
    # сложных расчётов, что не выразить формулой (цикл/группировка). Работает
    # ТОЛЬКО при ANALYSIS_CODE_EXEC=on (изолированный раннер). Результат — в
    # переменной `result`. Приоритетнее computation.
    code: Optional[str] = None
    inputs: list[str] = field(default_factory=list)  # имена нужных метрик
    # Пороги для интерпретации computed-значения. Семантика — в playbook'е
    # (обычно: значение выше red_flag = красный флаг).
    benchmark: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AnalysisDimension":
        return cls(
            key=str(d.get("key", "")),
            label=str(d.get("label", "")),
            rationale=str(d.get("rationale", "")),
            extraction_hint=str(d.get("extraction_hint", "")),
            kind=str(d.get("kind", DimensionKind.QUALITATIVE.value)),
            computation=d.get("computation"),
            code=d.get("code"),
            inputs=list(d.get("inputs", []) or []),
            benchmark=dict(d.get("benchmark", {}) or {}),
            weight=float(d.get("weight", 1.0) or 1.0),
        )


# === Playbook ==============================================================


@dataclass
class AnalysisPlaybook:
    """Методика разбора — настраивается раз, переиспользуется."""
    id: str
    name: str
    analysis_type: str = "due_diligence"     # due_diligence | financial | custom
    description: str = ""
    org_id: Optional[str] = None             # Wave 2 org-scoped
    dimensions: list[AnalysisDimension] = field(default_factory=list)
    # company_context: {"use_snapshot": bool, "use_rule_book": bool,
    #                   "custom": "Мы — PE-фонд, оцениваем по ..."}
    company_context: dict[str, Any] = field(default_factory=dict)
    reference_report: Optional[str] = None   # few-shot пример идеального отчёта
    # output_template: {"blocks": [{"name","label","description"}]}
    output_template: dict[str, Any] = field(default_factory=dict)
    created_by: str = ""
    access_level: int = 2                     # P1 #5 RBAC (INTERNAL по умолч.)
    version: int = 1                          # инкремент при каждом update
    created_at: str = ""
    updated_at: str = ""

    @staticmethod
    def new_id() -> str:
        return f"apb_{uuid.uuid4().hex[:16]}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["dimensions"] = [dim.to_dict() if isinstance(dim, AnalysisDimension)
                           else dim for dim in self.dimensions]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AnalysisPlaybook":
        dims_raw = d.get("dimensions", []) or []
        dims = [
            AnalysisDimension.from_dict(x) if isinstance(x, dict) else x
            for x in dims_raw
        ]
        return cls(
            id=str(d.get("id") or cls.new_id()),
            name=str(d.get("name", "")),
            analysis_type=str(d.get("analysis_type", "due_diligence")),
            description=str(d.get("description", "")),
            org_id=d.get("org_id"),
            dimensions=dims,
            company_context=dict(d.get("company_context", {}) or {}),
            reference_report=d.get("reference_report"),
            output_template=dict(d.get("output_template", {}) or {}),
            created_by=str(d.get("created_by", "")),
            access_level=int(d.get("access_level", 2) or 2),
            version=int(d.get("version", 1) or 1),
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
        )


# === Run ===================================================================


@dataclass
class AnalysisRun:
    """Один прогон playbook'а на конкретных документах."""
    id: str
    playbook_id: str
    # Версия playbook'а на момент запуска — для воспроизводимости:
    # «этот отчёт сделан по версии N методики». Методику могли изменить
    # после прогона; run остаётся привязан к снапшоту версии.
    playbook_version: int = 1
    org_id: Optional[str] = None
    user_id: str = ""
    input_document_ids: list[str] = field(default_factory=list)
    client_context: Optional[str] = None     # что обсуждалось с клиентом
    status: str = RunStatus.PENDING.value
    # steps_log: [{phase, action, detail, ts}] — петля «достал/посчитал/положил»
    steps_log: list[dict[str, Any]] = field(default_factory=list)
    # extracted_facts: {dimension_key: [{value, source_doc, quote, page?}]}
    extracted_facts: dict[str, Any] = field(default_factory=dict)
    # computed_metrics: {dimension_key: {value, benchmark_status, formula}}
    computed_metrics: dict[str, Any] = field(default_factory=dict)
    # hypotheses: реверсивный разбор — гипотезы выводов/рисков, выдвинутые ДО
    # детального извлечения (top-down): [{claim, why, check}]. Пусто, если
    # методика не включила reverse_analysis.
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    # model_tier: standard (экономичная) | premium (тяжёлая) — на этот прогон
    model_tier: str = "standard"
    # final_report: {markdown, sections[], score?, verdict?}
    final_report: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str = ""
    completed_at: Optional[str] = None

    @staticmethod
    def new_id() -> str:
        return f"arun_{uuid.uuid4().hex[:16]}"

    def add_step(self, phase: str, action: str, detail: Any = None) -> None:
        """Записать шаг петли — для прозрачности/воспроизводимости."""
        self.steps_log.append({
            "phase": phase,
            "action": action,
            "detail": detail,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AnalysisRun":
        return cls(
            id=str(d.get("id") or cls.new_id()),
            playbook_id=str(d.get("playbook_id", "")),
            playbook_version=int(d.get("playbook_version", 1) or 1),
            org_id=d.get("org_id"),
            user_id=str(d.get("user_id", "")),
            input_document_ids=list(d.get("input_document_ids", []) or []),
            client_context=d.get("client_context"),
            status=str(d.get("status", RunStatus.PENDING.value)),
            steps_log=list(d.get("steps_log", []) or []),
            extracted_facts=dict(d.get("extracted_facts", {}) or {}),
            computed_metrics=dict(d.get("computed_metrics", {}) or {}),
            hypotheses=list(d.get("hypotheses", []) or []),
            model_tier=str(d.get("model_tier") or "standard"),
            final_report=d.get("final_report"),
            error=d.get("error"),
            created_at=str(d.get("created_at", "")),
            completed_at=d.get("completed_at"),
        )
