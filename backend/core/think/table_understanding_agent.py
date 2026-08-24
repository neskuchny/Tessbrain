# -*- coding: utf-8 -*-
"""
TableUnderstandingAgent — Столп C (docs/COMPANY_SELF_MODEL_DESIGN.md §2.C).

«Дают сырую таблицу → система понимает, ЧТО это, потому что знает компанию.»

Архитектура (TESSENT_BRAIN_ARCHITECTURE §0): СКЕЛЕТ + LLM.
- СКЕЛЕТ (детерминированный код, этот модуль): инференс типов колонок,
  schema-prior из снапшота компании, привязка значений колонок к известным
  сущностям, оценка уверенности и — главное — ЧЕСТНЫЙ вердикт «незнакомая
  таблица» (не выдумываем бизнес-смысл, если привязки нет).
- LLM (под скелетом): семантические ярлыки колонок + тип/бизнес-юнит/период,
  ТОЛЬКО когда скелет уже заземлил таблицу на компанию. Graceful: сбой LLM не
  ломает результат — остаётся скелетная заземлённость.

Модуль на уровне импорта — только stdlib → скелет тестируется везде. LLM-роутер
инъектируется (ленивый дефолт); снапшот/prior передаются. Это и есть дисциплина:
заземление — детерминированно и проверяемо; интерпретация — LLM, но не на веру.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(
    r"^\d{4}[-/.]\d{1,2}([-/.]\d{1,2})?$"      # 2026-01 / 2026-01-15
    r"|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$"      # 15.01.2026
)
_SAMPLE_CAP = 50  # сколько значений колонки держим для матчинга


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def _is_number(x: str) -> bool:
    s = x.strip().replace(" ", "").replace(" ", "").replace("%", "")
    # 1 234,56 / 1,234.56 — нормализуем разделители
    s = s.replace(",", ".") if s.count(",") == 1 and s.count(".") == 0 else s.replace(",", "")
    try:
        float(s)
        return True
    except ValueError:
        return False


def infer_column_type(values: list) -> str:
    """Тип колонки по сэмплу значений: numeric|date|categorical|text|empty.
    entity_ref НЕ выводится здесь — он определяется привязкой к prior (см. ground)."""
    vals = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if not vals:
        return "empty"
    if all(_is_number(v) for v in vals):
        return "numeric"
    if all(_DATE_RE.match(v) for v in vals):
        return "date"
    distinct = {_norm(v) for v in vals}
    if len(distinct) <= max(2, 0.3 * len(vals)):
        return "categorical"
    return "text"


_GRAPH_LABEL_TO_CATEGORY = {
    "person": "person", "team": "team", "department": "department",
    "project": "project", "product": "product", "kpi": "kpi",
}


def prior_from_graph_nodes(node_data_iter) -> dict:
    """Schema-prior НАПРЯМУЮ из узлов per-user графа — источник истины (лучше
    глобального снапшота-singleton). Берёт имена узлов по метке _label
    (Person/Product/Department/Project/Team/KPI; у всех обязателен 'name').
    Чистая функция: вход — итерируемое словарей данных узлов → тестируется
    без графа."""
    prior = {c: set() for c in ("product", "person", "department", "project", "team", "kpi")}
    for data in (node_data_iter or []):
        if not isinstance(data, dict):
            continue
        cat = _GRAPH_LABEL_TO_CATEGORY.get(_norm(data.get("_label") or data.get("label") or ""))
        if not cat:
            continue
        for nk in ("name", "title", "normalized_value", "full_name"):
            v = data.get(nk)
            if v and str(v).strip():
                prior[cat].add(_norm(v))
                break
    return prior


def _names_from(items) -> set:
    out: set = set()
    for it in (items or []):
        if isinstance(it, str) and it.strip():
            out.add(_norm(it))
        elif isinstance(it, dict):
            for nk in ("name", "title", "kpi", "metric", "full_name"):
                if it.get(nk):
                    out.add(_norm(it[nk]))
    return out


def build_schema_prior(snapshot: Optional[dict]) -> dict:
    """Schema-prior: нормализованные множества имён известных сущностей компании
    по категориям — «Розеттский камень» для интерпретации таблицы.

    Устойчив ко всем реальным формам снапшота:
    - простой (company_snapshot_agent): products ВЛОЖЕНЫ в company.products,
      team — список людей (dict с name/department), projects — список;
    - богатый (enhanced CompanySnapshot): products/departments/teams/key_people/
      kpis как List[Dict] с "name".
    Отделы извлекаются и из явного списка, и из поля department у людей."""
    snap = snapshot or {}
    company = snap.get("company") if isinstance(snap.get("company"), dict) else {}

    def names(*keys) -> set:
        out: set = set()
        for key in keys:
            out |= _names_from(snap.get(key))
            out |= _names_from(company.get(key))
        return out

    departments = names("departments")
    # Отдел может жить в записи человека (простой снапшот без departments-списка).
    for src in ("team", "key_people", "people"):
        for it in (snap.get(src) or []):
            if isinstance(it, dict) and it.get("department"):
                departments.add(_norm(it["department"]))

    return {
        "product": names("products"),
        # ВАЖНО: в простом снапшоте "team" = ЛЮДИ; в enhanced "teams" = орг-юниты,
        # а люди — "key_people". Поэтому person ← team/key_people/people, а
        # орг-юнит team ← "teams" (мн.ч.) отдельно, без коллизии.
        "person": names("team", "people", "key_people", "participants"),
        "team": names("teams"),
        "department": departments,
        "project": names("projects"),
        "kpi": names("kpis", "metrics"),
    }


def _entity_token_match(a: str, b: str) -> bool:
    """Консервативный alias-матч сущностей: один — ТОКЕН-подмножество другого
    («polis» ↔ «polis insurance» совпадают), но РАЗНЫЕ токены не сливаются
    («sales» ≠ «salesforce»). Повышает recall заземления без потери precision."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return a == b
    return ta <= tb or tb <= ta


def match_column_to_prior(values: list, prior: dict, min_overlap: float = 0.3) -> tuple:
    """Лучшая категория prior, с которой пересекаются ЗНАЧЕНИЯ колонки.
    Возвращает (entity_type, примеры) или (None, []). min_overlap — порог доли
    значений, совпавших с известными именами (защита от случайных совпадений).
    Матч alias-aware (токен-граничный) — ловит варианты, не сливая разные."""
    vals = {_norm(v) for v in values if v is not None and str(v).strip() != ""}
    if not vals:
        return None, []
    best_type, best_ratio, best_ex = None, min_overlap, []
    for etype, names in (prior or {}).items():
        if not names:
            continue
        matched = [v for v in vals if any(_entity_token_match(v, n) for n in names)]
        ratio = len(matched) / len(vals)
        if ratio > best_ratio:
            best_type, best_ratio, best_ex = etype, ratio, matched[:5]
    return best_type, best_ex


def header_is_known_kpi(column_name: str, prior: dict) -> bool:
    return _norm(column_name) in (prior or {}).get("kpi", set())


def assess_grounding(profiles: list, prior: dict) -> tuple:
    """(confidence, is_known). Заземлено, если ≥1 колонка привязалась к известной
    сущности ИЛИ заголовок = известный KPI. Иначе — ЧЕСТНО «незнакомая таблица»
    (низкая уверенность, бизнес-смысл не выдумываем)."""
    grounded = sum(1 for c in profiles if c.matched_entity_type)
    kpi_hits = sum(1 for c in profiles if header_is_known_kpi(c.name, prior))
    signal = grounded + kpi_hits
    if signal == 0:
        return 0.2, False
    total = max(1, len(profiles))
    return round(min(0.95, 0.4 + 0.5 * (signal / total)), 3), True


@dataclass
class ColumnProfile:
    name: str
    inferred_type: str
    sample_values: list = field(default_factory=list)        # для матчинга (cap)
    matched_entity_type: Optional[str] = None                # product|person|... (скелет)
    matched_examples: list = field(default_factory=list)
    semantic_label: Optional[str] = None                     # из LLM (под скелетом)

    def display(self) -> dict:
        return {
            "name": self.name,
            "type": self.inferred_type,
            "matched_entity_type": self.matched_entity_type,
            "matched_examples": self.matched_examples,
            "semantic_label": self.semantic_label,
            "sample": self.sample_values[:5],
        }


@dataclass
class TableUnderstanding:
    is_known: bool
    confidence: float
    columns: list
    table_type: Optional[str] = None
    business_unit: Optional[str] = None
    time_period: Optional[str] = None
    detected_kpis: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    ungrounded_claims: list = field(default_factory=list)  # утверждения LLM, не подтверждённые фактами

    def to_dict(self) -> dict:
        return {
            "is_known": self.is_known,
            "confidence": self.confidence,
            "table_type": self.table_type,
            "business_unit": self.business_unit,
            "time_period": self.time_period,
            "detected_kpis": self.detected_kpis,
            "columns": [c.display() for c in self.columns],
            "notes": self.notes,
            "ungrounded_claims": self.ungrounded_claims,
        }


def verify_business_claims(business_unit, detected_kpis, prior: dict) -> dict:
    """Claim-guard: проверить утверждения LLM против ФАКТОВ компании (prior).
    Усиление измеренного в Gate-0 выигрыша — анти-галлюцинация: чистый LLM
    выдумывает business_unit/KPI, которых в компании НЕТ; скелет их отбрасывает.

    Возвращает {business_unit (или None если не подтверждён), detected_kpis
    (только подтверждённые), ungrounded (список отвергнутых с причиной)}."""
    p = prior or {}
    orgs = (p.get("department") or set()) | (p.get("team") or set())
    known_kpis = p.get("kpi") or set()
    ungrounded: list = []

    bu = business_unit
    if bu and orgs and _norm(bu) not in orgs:
        ungrounded.append(f"business_unit='{bu}' — нет среди отделов/команд компании")
        bu = None  # не утверждаем выдуманное

    grounded_kpis: list = []
    for k in (detected_kpis or []):
        if not k:
            continue
        if known_kpis and _norm(k) not in known_kpis:
            ungrounded.append(f"KPI '{k}' — нет среди известных KPI компании")
        else:
            grounded_kpis.append(k)

    return {"business_unit": bu, "detected_kpis": grounded_kpis, "ungrounded": ungrounded}


_INTERPRET_SYSTEM = (
    "Ты — аналитик данных, который ЗНАЕТ эту компанию. По снапшоту компании и "
    "профилю таблицы определи, ЧТО это за таблица. НЕ выдумывай: если данные не "
    "вяжутся с компанией — скажи честно. Отвечай строго JSON."
)


class TableUnderstandingAgent:
    """Оркестратор столпа C. Скелет заземляет; LLM интерпретирует под скелетом."""

    def __init__(self, llm_router=None):
        self._llm = llm_router  # инъекция для тестируемости; ленивый дефолт ниже

    def profile_columns(self, columns: list, rows: list) -> list:
        """Профиль колонок (тип + сэмпл). rows — list[dict] или list[list]."""
        col_values: dict = {c: [] for c in columns}
        for r in rows:
            if isinstance(r, dict):
                for c in columns:
                    col_values[c].append(r.get(c))
            else:
                for i, c in enumerate(columns):
                    col_values[c].append(r[i] if i < len(r) else None)
        return [
            ColumnProfile(
                name=c,
                inferred_type=infer_column_type(col_values[c]),
                sample_values=col_values[c][:_SAMPLE_CAP],
            )
            for c in columns
        ]

    def ground(self, profiles: list, prior: dict) -> tuple:
        """Скелет: привязать колонки к известным сущностям + оценить заземление."""
        for c in profiles:
            etype, examples = match_column_to_prior(c.sample_values, prior)
            if etype:
                c.matched_entity_type = etype
                c.matched_examples = examples
                c.inferred_type = "entity_ref"
        return assess_grounding(profiles, prior)

    async def understand(
        self,
        columns: list,
        rows: list,
        snapshot: Optional[dict] = None,
        prior: Optional[dict] = None,
        snapshot_text: str = "",
    ) -> TableUnderstanding:
        """Главный вход. Скелет → (если заземлено) LLM-интерпретация. Graceful."""
        profiles = self.profile_columns(columns, rows)
        prior = prior if prior is not None else build_schema_prior(snapshot)
        confidence, is_known = self.ground(profiles, prior)
        result = TableUnderstanding(is_known=is_known, confidence=confidence, columns=profiles)

        if not is_known:
            result.notes.append(
                "Незнакомая таблица: значения не привязались к известным сущностям "
                "компании. Бизнес-смысл не выдумываем — нужна ручная разметка/контекст."
            )
            return result

        # LLM-интерпретация ПОД скелетом — только для заземлённой таблицы. Graceful.
        try:
            enrich = await self._llm_interpret(profiles, snapshot_text or "")
            if isinstance(enrich, dict):
                result.table_type = enrich.get("table_type")
                result.time_period = enrich.get("time_period")
                # Claim-guard: проверяем business_unit/KPI LLM против фактов компании
                # (усиление Gate-0 выигрыша — не пропускаем выдуманное).
                checked = verify_business_claims(
                    enrich.get("business_unit"), enrich.get("detected_kpis") or [], prior
                )
                result.business_unit = checked["business_unit"]
                result.detected_kpis = checked["detected_kpis"]
                if checked["ungrounded"]:
                    result.ungrounded_claims = checked["ungrounded"]
                    result.notes.append(
                        "⚠️ Отвергнуты неподтверждённые догадки LLM: "
                        + "; ".join(checked["ungrounded"])
                    )
                labels = enrich.get("column_labels") or {}
                for c in profiles:
                    if c.name in labels:
                        c.semantic_label = labels[c.name]
        except Exception as e:
            logger.debug(f"[TableUnderstanding] LLM interpret skipped: {e}")
            result.notes.append("LLM-интерпретация недоступна — отдан скелетный профиль.")
        return result

    async def _llm_interpret(self, profiles: list, snapshot_text: str) -> dict:
        llm = self._llm
        if llm is None:
            from backend.core.llm.router import get_llm_router
            llm = get_llm_router()
        cols_desc = "\n".join(
            f"- {c.name}: тип={c.inferred_type}"
            + (f", привязано к {c.matched_entity_type} (напр. {c.matched_examples})" if c.matched_entity_type else "")
            + f", примеры={c.sample_values[:3]}"
            for c in profiles
        )
        prompt = (
            f"СНАПШОТ КОМПАНИИ:\n{snapshot_text[:2000]}\n\n"
            f"ПРОФИЛЬ ТАБЛИЦЫ (колонки уже частично привязаны к сущностям компании):\n{cols_desc}\n\n"
            "Верни JSON:\n"
            '{"table_type":"что это (напр. sales_pipeline|budget|headcount|...)",'
            '"business_unit":"какой отдел/команда (из снапшота)",'
            '"time_period":"период, если виден",'
            '"detected_kpis":["известные KPI в таблице"],'
            '"column_labels":{"имя_колонки":"семантический ярлык"}}'
        )
        system_prompt = _INTERPRET_SYSTEM
        try:
            from backend.config import get_settings
            if get_settings().human_thinking_enabled:
                from backend.core.think.human_thinking import with_thinking_discipline
                system_prompt = with_thinking_discipline(system_prompt, "interpret")
        except Exception as e:
            logger.debug(f"[TableUnderstanding] thinking-discipline skipped: {e}")
        return await llm.generate_json(
            prompt=prompt, system_prompt=system_prompt, temperature=0.1, max_tokens=600
        )
