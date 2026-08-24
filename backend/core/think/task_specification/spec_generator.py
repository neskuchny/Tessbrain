# -*- coding: utf-8 -*-
"""
Specification Generator Agent - генерация детального ТЗ.

Создает:
- Полное техническое задание
- Функциональные и нефункциональные требования
- План выполнения
- Критерии приемки
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .base_agent import BaseTaskAgent


class SpecificationGeneratorAgent(BaseTaskAgent):
    """Агент генерации технического задания."""

    def __init__(self, storage=None, llm_router=None, output_dir: str | None = None):
        super().__init__("SpecGenerator", storage, llm_router)
        self.output_dir = Path(output_dir) if output_dir else Path("task_specifications")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self,
        task_analysis: Dict[str, Any],
        context_data: Dict[str, Any],
        task_id: str
    ) -> Dict[str, Any]:
        """
        Генерация комплексного технического задания.

        Args:
            task_analysis: Результат анализа задачи
            context_data: Найденный контекст
            task_id: ID задачи

        Returns:
            Полное техническое задание
        """
        self.log_start(f"Генерация ТЗ для задачи: {task_id}")

        # 1. Извлечение параметров
        spec_params = self._extract_spec_params(task_analysis, context_data)

        # 2. Определение типа спецификации
        spec_type = self._determine_spec_type(spec_params)

        # 3. Генерация основной спецификации
        main_spec = await self._generate_main_spec(spec_params, spec_type)

        # 4. Генерация плана выполнения
        execution_plan = await self._generate_execution_plan(spec_params, main_spec)

        # 5. Генерация критериев приемки
        acceptance_criteria = await self._generate_acceptance_criteria(spec_params, main_spec)

        # 5а. C9: обогатить критерии опытом компании (wisdom paterns).
        # Без доп. LLM-вызова — переиспользуем build_wisdom_brief. Best-effort:
        # любая ошибка → criteria как есть.
        try:
            from backend.core.config import flags
            if getattr(flags, "enable_wisdom_in_specs", False):
                from .wisdom_to_criteria import (
                    enrich_criteria_with_wisdom,
                    fetch_wisdom_brief_safe,
                )
                wisdom_brief = await fetch_wisdom_brief_safe(
                    user_id=spec_params.get("user_id", ""),
                    situation=spec_params.get("original_task", ""),
                )
                acceptance_criteria = enrich_criteria_with_wisdom(
                    acceptance_criteria, wisdom_brief)
        except Exception:
            pass  # criteria как есть

        # 6. Генерация рисков и митигаций
        risk_plan = await self._generate_risk_plan(spec_params)

        # 7. Сборка финального документа
        specification = {
            "specification_id": f"SPEC_{task_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "task_id": task_id,
            "spec_type": spec_type,
            "main_specification": main_spec,
            "execution_plan": execution_plan,
            "acceptance_criteria": acceptance_criteria,
            "risk_management": risk_plan,
            "source_data": {
                "task_analysis_summary": self._summarize_analysis(task_analysis),
                "context_summary": self._summarize_context(context_data)
            },
            "metadata": {
                "agent": self.name,
                "timestamp": self.get_timestamp(),
                "version": "1.0.0",
                "estimated_complexity": spec_params.get("complexity", "medium")
            }
        }

        # 8. Генерация Markdown версии
        markdown = self._generate_markdown(specification)
        specification["markdown"] = markdown

        # 9. Сохранение файлов
        file_paths = await self._save_specification(specification, task_id)
        specification["file_paths"] = file_paths

        self.log_complete(f"ТЗ сгенерировано: {specification['specification_id']}")
        return specification

    def _extract_spec_params(
        self,
        task_analysis: Dict[str, Any],
        context_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Извлечение параметров для спецификации."""
        analysis = task_analysis.get("analysis", {})
        context_analysis = context_data.get("analysis", {})

        return {
            "original_task": task_analysis.get("original_task", ""),
            "understanding": analysis.get("understanding", {}),
            "classification": analysis.get("classification", {}),
            "complexity": analysis.get("complexity", {}).get("level", "medium"),
            "stakeholders": analysis.get("stakeholders", {}),
            "risks": analysis.get("risks", []),
            "dependencies": analysis.get("dependencies", {}),
            "recommended_approach": analysis.get("recommended_approach", {}),
            "context_insights": context_analysis.get("key_insights", []),
            "success_factors": context_analysis.get("success_factors", []),
            "contextual_requirements": context_analysis.get("contextual_requirements", {}),
            # C9: user_id для wisdom-обогащения acceptance_criteria. Берём из
            # task_analysis (исходный orchestrator кладёт user_id в analysis),
            # либо из context_data — best-effort.
            "user_id": (task_analysis.get("user_id")
                        or analysis.get("user_id")
                        or context_data.get("user_id", "")),
        }

    def _determine_spec_type(self, params: Dict[str, Any]) -> str:
        """Определение типа спецификации."""
        task_type = params.get("classification", {}).get("task_type", "").lower()

        type_mapping = {
            "development": "software_development",
            "analysis": "analytical_study",
            "content": "content_creation",
            "planning": "project_planning",
            "design": "design_specification",
            "marketing": "marketing_campaign",
            "research": "research_specification"
        }

        return type_mapping.get(task_type, "general_task")

    async def _get_company_context(self, user_id: str = "") -> str:
        """Получить контекст компании из Enhanced Snapshot (строго per-user)."""
        try:
            from backend.core.config import flags
            if not flags.enable_enhanced_snapshots:
                return ""

            from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
            generator = get_enhanced_snapshot_generator(user_id=user_id)
            snapshot = await generator.get_company_snapshot()

            if snapshot:
                return snapshot.to_text(max_length=2000)
        except Exception as e:
            self.log_error(f"Не удалось получить контекст компании: {e}")

        return ""

    async def _generate_main_spec(
        self,
        params: Dict[str, Any],
        spec_type: str
    ) -> Dict[str, Any]:
        """Генерация основной спецификации."""

        # НОВОЕ: Получаем контекст компании (снапшот тенанта, не общий)
        company_context = await self._get_company_context(params.get("user_id", ""))

        company_section = ""
        if company_context:
            company_section = f"""
КОНТЕКСТ КОМПАНИИ:
{company_context}

Учитывай этот контекст при создании ТЗ - используй терминологию компании,
учитывай текущие приоритеты и ограничения.
"""

        system_prompt = f"""Ты — ведущий архитектор решений.
Твоя задача — создать детальное техническое задание.
{company_section}
ПРИНЦИПЫ:
1. КОНКРЕТНОСТЬ — каждый пункт должен быть измеримым
2. ПОЛНОТА — учитывай все аспекты задачи
3. ПРАКТИЧНОСТЬ — ТЗ должно быть готово к выполнению
4. ЯСНОСТЬ — понятно всем участникам

Возвращай ТОЛЬКО валидный JSON без markdown-разметки."""

        params_json = json.dumps(params, ensure_ascii=False, indent=2)

        prompt = f"""Создай техническое задание и верни JSON:

ТИП СПЕЦИФИКАЦИИ: {spec_type}

ПАРАМЕТРЫ:
{params_json}

Верни JSON со структурой:
{{
    "project_overview": {{
        "title": "название проекта",
        "description": "детальное описание",
        "objectives": ["цели проекта"],
        "scope": "границы проекта",
        "out_of_scope": ["что НЕ входит"]
    }},
    "requirements": {{
        "functional": [
            {{
                "id": "FR-001",
                "title": "название требования",
                "description": "описание",
                "priority": "high|medium|low",
                "acceptance_criteria": ["критерии"]
            }}
        ],
        "non_functional": [
            {{
                "category": "performance|security|usability|etc",
                "requirement": "описание",
                "metric": "метрика",
                "target_value": "целевое значение"
            }}
        ],
        "business": [
            {{
                "requirement": "бизнес-требование",
                "justification": "обоснование"
            }}
        ]
    }},
    "solution_approach": {{
        "methodology": "методология",
        "architecture": "архитектура решения",
        "key_components": [
            {{
                "component": "компонент",
                "description": "описание",
                "responsibilities": ["обязанности"]
            }}
        ],
        "technology_stack": {{
            "primary": ["основные технологии"],
            "supporting": ["вспомогательные"],
            "justification": "обоснование выбора"
        }}
    }},
    "deliverables": [
        {{
            "name": "название результата",
            "description": "описание",
            "format": "формат",
            "quality_criteria": ["критерии качества"]
        }}
    ],
    "constraints": {{
        "technical": ["технические ограничения"],
        "business": ["бизнес-ограничения"],
        "resource": ["ресурсные ограничения"],
        "assumptions": ["предположения"]
    }}
}}"""

        return await self.generate_llm_response(prompt, system_prompt)

    async def _generate_execution_plan(
        self,
        params: Dict[str, Any],
        main_spec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Генерация плана выполнения."""

        system_prompt = """Ты — эксперт по управлению проектами.
Создай реалистичный план выполнения.

Возвращай ТОЛЬКО валидный JSON без markdown-разметки."""

        approach = params.get("recommended_approach", {})
        deliverables = main_spec.get("deliverables", [])

        prompt = f"""Создай план выполнения и верни JSON:

РЕКОМЕНДУЕМЫЙ ПОДХОД:
{json.dumps(approach, ensure_ascii=False, indent=2)}

РЕЗУЛЬТАТЫ (DELIVERABLES):
{json.dumps(deliverables, ensure_ascii=False, indent=2)}

СЛОЖНОСТЬ: {params.get('complexity', 'medium')}

Верни JSON со структурой:
{{
    "phases": [
        {{
            "id": "PHASE-1",
            "name": "название фазы",
            "description": "описание",
            "duration_days": 5,
            "activities": ["активности"],
            "deliverables": ["результаты фазы"],
            "dependencies": ["зависимости"],
            "resources": ["ресурсы"]
        }}
    ],
    "timeline": {{
        "total_duration_days": 20,
        "start_conditions": ["условия старта"],
        "milestones": [
            {{
                "name": "веха",
                "phase": "PHASE-1",
                "criteria": "критерий достижения"
            }}
        ],
        "critical_path": ["критический путь"]
    }},
    "resources": {{
        "human": [
            {{
                "role": "роль",
                "skills": ["навыки"],
                "allocation": "100%",
                "phases": ["PHASE-1"]
            }}
        ],
        "technical": ["технические ресурсы"],
        "budget_estimate": {{
            "category": "low|medium|high",
            "breakdown": ["статьи расходов"]
        }}
    }},
    "monitoring": {{
        "progress_tracking": "метод отслеживания",
        "reporting_frequency": "частота отчетов",
        "escalation_triggers": ["триггеры эскалации"]
    }}
}}"""

        return await self.generate_llm_response(prompt, system_prompt)

    async def _generate_acceptance_criteria(
        self,
        params: Dict[str, Any],
        main_spec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Генерация критериев приемки."""

        system_prompt = """Ты — эксперт по тестированию и контролю качества.
Создай комплексные критерии приемки.

Возвращай ТОЛЬКО валидный JSON без markdown-разметки."""

        requirements = main_spec.get("requirements", {})
        deliverables = main_spec.get("deliverables", [])

        prompt = f"""Создай критерии приемки и верни JSON:

ТРЕБОВАНИЯ:
{json.dumps(requirements, ensure_ascii=False, indent=2)}

РЕЗУЛЬТАТЫ:
{json.dumps(deliverables, ensure_ascii=False, indent=2)}

Верни JSON со структурой:
{{
    "functional_acceptance": [
        {{
            "id": "AC-001",
            "requirement_ref": "FR-001",
            "description": "описание критерия",
            "test_method": "метод проверки",
            "expected_result": "ожидаемый результат"
        }}
    ],
    "quality_acceptance": [
        {{
            "aspect": "аспект качества",
            "measurement": "метод измерения",
            "threshold": "пороговое значение",
            "validation": "способ валидации"
        }}
    ],
    "business_acceptance": [
        {{
            "criterion": "бизнес-критерий",
            "success_indicator": "индикатор успеха",
            "measurement_period": "период измерения"
        }}
    ],
    "user_acceptance": {{
        "scenarios": [
            {{
                "name": "сценарий",
                "steps": ["шаги"],
                "expected_outcome": "ожидаемый результат"
            }}
        ],
        "usability_criteria": ["критерии юзабилити"]
    }},
    "sign_off": {{
        "approvers": ["утверждающие"],
        "documentation": ["необходимая документация"],
        "final_checklist": ["финальный чеклист"]
    }}
}}"""

        return await self.generate_llm_response(prompt, system_prompt)

    async def _generate_risk_plan(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Генерация плана управления рисками."""

        system_prompt = """Ты — эксперт по управлению рисками.
Создай план управления рисками.

Возвращай ТОЛЬКО валидный JSON без markdown-разметки."""

        risks = params.get("risks", [])

        prompt = f"""Создай план управления рисками и верни JSON:

ИДЕНТИФИЦИРОВАННЫЕ РИСКИ:
{json.dumps(risks, ensure_ascii=False, indent=2)}

СЛОЖНОСТЬ ПРОЕКТА: {params.get('complexity', 'medium')}

Верни JSON со структурой:
{{
    "risk_register": [
        {{
            "id": "RISK-001",
            "description": "описание риска",
            "category": "technical|business|resource|external",
            "probability": "low|medium|high",
            "impact": "low|medium|high",
            "risk_score": 1-9,
            "mitigation_strategy": "стратегия снижения",
            "contingency_plan": "план на случай реализации",
            "owner": "ответственный",
            "status": "active|mitigated|closed"
        }}
    ],
    "risk_monitoring": {{
        "review_frequency": "частота пересмотра",
        "early_warning_indicators": ["индикаторы раннего предупреждения"],
        "escalation_procedure": "процедура эскалации"
    }},
    "risk_response_strategies": {{
        "avoid": ["риски для избежания"],
        "mitigate": ["риски для снижения"],
        "transfer": ["риски для передачи"],
        "accept": ["риски для принятия"]
    }}
}}"""

        return await self.generate_llm_response(prompt, system_prompt)

    def _generate_markdown(self, spec: Dict[str, Any]) -> str:
        """Генерация Markdown версии ТЗ."""
        lines = []

        # Заголовок
        lines.append("# Техническое Задание")
        lines.append(f"\n**ID:** {spec['specification_id']}")
        lines.append(f"**Дата:** {spec['metadata']['timestamp']}")
        lines.append(f"**Тип:** {spec['spec_type']}")
        lines.append("")

        # Обзор проекта
        main = spec.get("main_specification", {})
        overview = main.get("project_overview", {})
        if overview:
            lines.append("## 1. Обзор проекта")
            lines.append(f"\n**Название:** {overview.get('title', 'N/A')}")
            lines.append(f"\n**Описание:** {overview.get('description', 'N/A')}")

            if overview.get("objectives"):
                lines.append("\n### Цели:")
                for obj in overview["objectives"]:
                    lines.append(f"- {obj}")

            lines.append(f"\n**Границы:** {overview.get('scope', 'N/A')}")

            if overview.get("out_of_scope"):
                lines.append("\n### Вне границ проекта:")
                for item in overview["out_of_scope"]:
                    lines.append(f"- {item}")
            lines.append("")

        # Требования
        requirements = main.get("requirements", {})
        if requirements:
            lines.append("## 2. Требования")

            # Функциональные
            if requirements.get("functional"):
                lines.append("\n### 2.1 Функциональные требования")
                for req in requirements["functional"]:
                    lines.append(f"\n**{req.get('id', 'N/A')}:** {req.get('title', 'N/A')}")
                    lines.append(f"- Описание: {req.get('description', 'N/A')}")
                    lines.append(f"- Приоритет: {req.get('priority', 'N/A')}")

            # Нефункциональные
            if requirements.get("non_functional"):
                lines.append("\n### 2.2 Нефункциональные требования")
                for req in requirements["non_functional"]:
                    lines.append(f"- **{req.get('category', 'N/A')}:** {req.get('requirement', 'N/A')}")
            lines.append("")

        # План выполнения
        plan = spec.get("execution_plan", {})
        if plan.get("phases"):
            lines.append("## 3. План выполнения")
            for phase in plan["phases"]:
                lines.append(f"\n### {phase.get('id', 'N/A')}: {phase.get('name', 'N/A')}")
                lines.append(f"- Длительность: {phase.get('duration_days', 'N/A')} дней")
                lines.append(f"- Описание: {phase.get('description', 'N/A')}")

            timeline = plan.get("timeline", {})
            if timeline:
                lines.append(f"\n**Общая длительность:** {timeline.get('total_duration_days', 'N/A')} дней")
            lines.append("")

        # Критерии приемки
        acceptance = spec.get("acceptance_criteria", {})
        if acceptance:
            lines.append("## 4. Критерии приемки")

            if acceptance.get("functional_acceptance"):
                lines.append("\n### 4.1 Функциональные критерии")
                for ac in acceptance["functional_acceptance"]:
                    lines.append(f"- **{ac.get('id', 'N/A')}:** {ac.get('description', 'N/A')}")
            lines.append("")

        # Риски
        risks = spec.get("risk_management", {})
        if risks.get("risk_register"):
            lines.append("## 5. Управление рисками")
            for risk in risks["risk_register"]:
                lines.append(f"\n**{risk.get('id', 'N/A')}:** {risk.get('description', 'N/A')}")
                lines.append(f"- Вероятность: {risk.get('probability', 'N/A')}")
                lines.append(f"- Влияние: {risk.get('impact', 'N/A')}")
                lines.append(f"- Митигация: {risk.get('mitigation_strategy', 'N/A')}")
            lines.append("")

        # Метаданные
        lines.append("---")
        lines.append(f"\n*Сгенерировано: {spec['metadata']['timestamp']}*")
        lines.append(f"*Версия: {spec['metadata']['version']}*")

        return "\n".join(lines)

    async def _save_specification(
        self,
        spec: Dict[str, Any],
        task_id: str
    ) -> Dict[str, str]:
        """Сохранение спецификации в файлы."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"SPEC_{task_id}_{timestamp}"

        paths = {}

        try:
            # JSON версия
            json_path = self.output_dir / f"{base_name}.json"
            with open(json_path, 'w', encoding='utf-8') as f:
                # Убираем markdown из JSON для компактности
                spec_copy = {k: v for k, v in spec.items() if k != 'markdown'}
                json.dump(spec_copy, f, ensure_ascii=False, indent=2)
            paths["json"] = str(json_path)

            # Markdown версия
            md_path = self.output_dir / f"{base_name}.md"
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(spec.get("markdown", ""))
            paths["markdown"] = str(md_path)

            self.log_info(f"Файлы сохранены: {json_path}, {md_path}")

        except Exception as e:
            self.log_error(f"Ошибка сохранения файлов: {e}")

        return paths

    def _summarize_analysis(self, analysis: Dict[str, Any]) -> str:
        """Краткое резюме анализа."""
        a = analysis.get("analysis", {})
        return f"Тип: {a.get('classification', {}).get('task_type', 'N/A')}, Сложность: {a.get('complexity', {}).get('level', 'N/A')}"

    def _summarize_context(self, context: Dict[str, Any]) -> str:
        """Краткое резюме контекста."""
        stats = context.get("stats", {})
        return f"Источников: {stats.get('total_sources', 0)}, Инсайтов: {stats.get('stakeholder_insights', 0)}"

