# -*- coding: utf-8 -*-
"""
Consulting Analyzer — аналитические фреймворки уровня McKinsey/BCG.

Превращает Tessbrain во внутреннего гениального аудитора,
способного провести анализ любого аспекта бизнеса.

Фреймворки:
1. Organizational Health — здоровье команды, токсичные паттерны, burnout, лидерство
2. Strategic Analysis — ключевые решения, пивоты, дрейф стратегии, невыполненные инициативы
3. Process Efficiency — bottlenecks, узкие места, waste, time loss
4. Risk Assessment — скрытые риски, ignored warnings, зависимости
5. Capability Gap Analysis — какие компетенции нужны, а каких нет
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """Результат одного аналитического фреймворка."""
    framework: str = ""
    title: str = ""
    findings: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)
    confidence: float = 0.0
    data_sources: int = 0


class ConsultingAnalyzer:
    """
    Набор аналитических фреймворков для корпоративного анализа.

    Работает с данными из графа знаний — обходит ноды и связи,
    агрегирует информацию и формирует структурированные выводы.
    """

    def __init__(self, graph_builder=None, llm_router=None):
        self.graph = graph_builder
        self.llm = llm_router

    async def analyze(self, query: str, frameworks: Optional[List[str]] = None) -> List[AnalysisResult]:
        """
        Запустить анализ по релевантным фреймворкам.

        Args:
            query: Запрос пользователя
            frameworks: Список фреймворков (если None — автоопределение)
        """
        if not frameworks:
            frameworks = self._detect_frameworks(query)

        results = []
        for fw in frameworks:
            try:
                if fw == "org_health":
                    results.append(await self.organizational_health())
                elif fw == "strategic":
                    results.append(await self.strategic_analysis())
                elif fw == "process":
                    results.append(await self.process_efficiency())
                elif fw == "risk":
                    results.append(await self.risk_assessment())
                elif fw == "capability_gap":
                    results.append(await self.capability_gap_analysis())
                elif fw == "swot":
                    results.append(await self.swot_analysis())
                elif fw == "proactive":
                    results.append(await self.proactive_insights())
            except Exception as e:
                logger.warning(f"Framework {fw} failed: {e}")

        return results

    def _detect_frameworks(self, query: str) -> List[str]:
        """Определить релевантные фреймворки по запросу."""
        q = query.lower()
        frameworks = []

        # Organizational Health
        if any(w in q for w in [
            "сотрудник", "команд", "выгоран", "burnout", "токсич", "лидер",
            "кто нужен", "не хватает", "нанять", "уволить", "мотивац",
            "эффективность команд", "конфликт", "настроен", "engagement"
        ]):
            frameworks.append("org_health")

        # Strategic Analysis
        if any(w in q for w in [
            "стратег", "решени", "пивот", "дрейф", "направлен",
            "куда движ", "ключев", "приоритет", "что изменил",
            "не выполнен", "инициатив", "цели", "план", "миссия"
        ]):
            frameworks.append("strategic")

        # Process Efficiency
        if any(w in q for w in [
            "процесс", "оптимизи", "узкое место", "bottleneck", "эффективн",
            "автоматиз", "ии", "ai", "внедри", "ускорить", "сократить",
            "потери", "waste", "время", "задержк"
        ]):
            frameworks.append("process")

        # Risk Assessment
        if any(w in q for w in [
            "риск", "опасн", "угроз", "проблем", "что может пойти",
            "зависимост", "слабое место", "уязвимост"
        ]):
            frameworks.append("risk")

        # Capability Gap
        if any(w in q for w in [
            "компетенц", "навык", "skill", "gap", "не хватает",
            "нужен специалист", "кого нанять", "обучение"
        ]):
            frameworks.append("capability_gap")

        # SWOT
        if any(w in q for w in [
            "swot", "сильные сторон", "слабые сторон", "возможност",
            "угроз", "конкурент", "позицион", "преимуществ"
        ]):
            frameworks.append("swot")

        # Proactive
        if any(w in q for w in [
            "что упускаем", "на что обратить", "инсайт", "проактив",
            "незаметн", "что не видим", "что важно", "обзор"
        ]):
            frameworks.append("proactive")

        # Если ничего не подошло — анализируем всё
        if not frameworks:
            frameworks = ["org_health", "process", "risk", "proactive"]

        return frameworks

    # ═══════════════════════════════════════════════════════════
    # FRAMEWORK 1: Organizational Health
    # ═══════════════════════════════════════════════════════════

    async def organizational_health(self) -> AnalysisResult:
        """
        Анализ организационного здоровья.

        Анализирует:
        - Нагрузку на людей (перегрузки, недозагрузки)
        - Заблокированные задачи (системные проблемы)
        - Паттерны проблем по отделам/командам
        - Психологические профили (если есть)
        - Конфликты и противоречия
        """
        result = AnalysisResult(
            framework="org_health",
            title="Организационное здоровье"
        )

        if not self.graph:
            return result

        # 1. Нагрузка по людям
        try:
            persons = await self.graph.find_nodes_by_label(label="Person", limit=100)

            overloaded = []
            underloaded = []
            blocked_people = []

            for person in persons:
                pid = person.get("id")
                if not pid:
                    continue

                rels = await self.graph.get_node_relationships(pid)
                tasks = []
                blocked = 0

                for direction in ("outgoing", "incoming"):
                    for rel in rels.get(direction, []):
                        other_id = rel.get("target" if direction == "outgoing" else "source", "")
                        if not other_id:
                            continue
                        other = await self.graph.get_node_by_id(other_id)
                        if other and other.get("_label") == "Task":
                            tasks.append(other)
                            if other.get("status") in ("blocked", "stuck", "on_hold"):
                                blocked += 1

                name = person.get("name", "N/A")
                task_count = len(tasks)

                if task_count > 7:
                    overloaded.append({"name": name, "tasks": task_count, "blocked": blocked})
                elif task_count == 0:
                    underloaded.append({"name": name})

                if blocked >= 2:
                    blocked_people.append({"name": name, "blocked": blocked, "total": task_count})

            if overloaded:
                overloaded.sort(key=lambda x: x["tasks"], reverse=True)
                result.findings.append(f"⚠️ Перегруженные сотрудники ({len(overloaded)}):")
                for p in overloaded[:5]:
                    bl = f", ❌ {p['blocked']} заблокированы" if p["blocked"] else ""
                    result.findings.append(f"  - {p['name']}: {p['tasks']} задач{bl}")

            if blocked_people:
                result.findings.append(f"🚧 Сотрудники с заблокированными задачами ({len(blocked_people)}):")
                for p in blocked_people[:5]:
                    result.findings.append(f"  - {p['name']}: {p['blocked']}/{p['total']} задач заблокированы")

            result.metrics["total_persons"] = len(persons)
            result.metrics["overloaded"] = len(overloaded)
            result.metrics["underloaded"] = len(underloaded)
            result.metrics["blocked_people"] = len(blocked_people)
            result.data_sources += len(persons)

        except Exception as e:
            logger.warning(f"Org health: workload analysis failed: {e}")

        # 2. Психологические профили (если есть)
        try:
            profiles = await self.graph.find_nodes_by_label(label="PsychologicalProfile", limit=50)
            if profiles:
                result.findings.append(f"\n👥 Психологические профили ({len(profiles)}):")
                for prof in profiles[:5]:
                    name = prof.get("name", "N/A")
                    traits = prof.get("personality_traits", "")
                    result.findings.append(f"  - {name}: {str(traits)[:200]}")
                result.data_sources += len(profiles)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        # 3. Противоречия (конфликты в данных)
        try:
            contradictions = await self.graph.find_nodes_by_label(label="Contradiction", limit=20)
            if contradictions:
                result.findings.append(f"\n🔀 Противоречия и конфликты ({len(contradictions)}):")
                for c in contradictions[:5]:
                    result.findings.append(f"  - БЫЛО: {c.get('existing_fact', '?')[:150]}")
                    result.findings.append(f"    СТАЛО: {c.get('new_fact', '?')[:150]}")
                result.data_sources += len(contradictions)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        # Рекомендации (через LLM если доступен)
        if self.llm and result.findings:
            try:
                prompt = f"""На основе анализа организационного здоровья:

{chr(10).join(result.findings[:20])}

Сформулируй 3-5 конкретных рекомендаций:
1. Что критически нужно исправить прямо сейчас
2. Что улучшить в среднесрочной перспективе
3. Что мониторить

Ответь списком рекомендаций, кратко и конкретно."""

                recs = await self.llm.generate(prompt=prompt, max_tokens=500)
                result.recommendations = [r.strip() for r in recs.split("\n") if r.strip() and not r.strip().startswith("#")]
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        result.confidence = min(1.0, result.data_sources / 20.0)
        return result

    # ═══════════════════════════════════════════════════════════
    # FRAMEWORK 2: Strategic Analysis
    # ═══════════════════════════════════════════════════════════

    async def strategic_analysis(self) -> AnalysisResult:
        """
        Стратегический анализ — ключевые решения, пивоты, дрейф.

        Анализирует:
        - Decision ноды: какие решения принимались, кем, когда
        - Невыполненные задачи (created but never completed)
        - Изменения в проектах (delta)
        - Сценарии (Scenario ноды)
        """
        result = AnalysisResult(
            framework="strategic",
            title="Стратегический анализ"
        )

        if not self.graph:
            return result

        # 1. Ключевые решения
        try:
            decisions = await self.graph.find_nodes_by_label(label="Decision", limit=100)
            if decisions:
                result.findings.append(f"📋 Ключевые решения ({len(decisions)}):")
                # Сортируем по дате (если есть)
                decisions_sorted = sorted(
                    decisions,
                    key=lambda d: d.get("date", d.get("created_at", "")),
                    reverse=True
                )
                for d in decisions_sorted[:10]:
                    text = d.get("text", d.get("title", d.get("description", "N/A")))
                    made_by = d.get("made_by", "")
                    date = d.get("date", "")
                    confidence = d.get("confidence", "")
                    line = f"  - {text[:200]}"
                    if made_by:
                        line += f" [{made_by}]"
                    if date:
                        line += f" ({date})"
                    if confidence:
                        line += f" (уверенность: {confidence})"
                    result.findings.append(line)
                result.data_sources += len(decisions)
        except Exception as e:
            logger.warning(f"Strategic: decisions failed: {e}")

        # 2. Невыполненные задачи (инициативы которые не завершились)
        try:
            tasks = await self.graph.find_nodes_by_label(label="Task", limit=200)
            stale_tasks = []
            for t in tasks:
                status = t.get("status", "")
                if status in ("pending", "todo", "backlog"):
                    stale_tasks.append(t)

            if stale_tasks:
                result.findings.append(f"\n⏳ Невыполненные инициативы ({len(stale_tasks)}):")
                for t in stale_tasks[:10]:
                    name = t.get("name", t.get("title", "N/A"))
                    assignee = t.get("assignee", "не назначен")
                    priority = t.get("priority", "")
                    line = f"  - {name[:200]} (исполнитель: {assignee})"
                    if priority:
                        line += f" [приоритет: {priority}]"
                    result.findings.append(line)
                result.data_sources += len(stale_tasks)
        except Exception as e:
            logger.warning(f"Strategic: stale tasks failed: {e}")

        # 3. Сценарии
        try:
            scenarios = await self.graph.find_nodes_by_label(label="Scenario", limit=20)
            if scenarios:
                result.findings.append(f"\n🎯 Рассматриваемые сценарии ({len(scenarios)}):")
                for s in scenarios[:5]:
                    name = s.get("name", s.get("description", "N/A"))
                    conditions = s.get("conditions", "")
                    result.findings.append(f"  - {name[:200]}")
                    if conditions:
                        result.findings.append(f"    Условия: {conditions[:200]}")
                result.data_sources += len(scenarios)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        result.confidence = min(1.0, result.data_sources / 15.0)
        return result

    # ═══════════════════════════════════════════════════════════
    # FRAMEWORK 3: Process Efficiency
    # ═══════════════════════════════════════════════════════════

    async def process_efficiency(self) -> AnalysisResult:
        """
        Анализ эффективности процессов — bottlenecks, waste, возможности ИИ.

        Анализирует:
        - Заблокированные задачи (системные bottlenecks)
        - Задачи с самым высоким приоритетом, но не выполненные
        - Повторяющиеся проблемы (frequency_score)
        - Зависимости между задачами/проектами
        """
        result = AnalysisResult(
            framework="process",
            title="Эффективность процессов"
        )

        if not self.graph:
            return result

        # 1. Bottlenecks: заблокированные задачи
        try:
            tasks = await self.graph.find_nodes_by_label(label="Task", limit=200)

            blocked = [t for t in tasks if t.get("status") in ("blocked", "stuck", "on_hold")]
            high_prio_pending = [
                t for t in tasks
                if t.get("priority") in ("high", "critical", "urgent")
                and t.get("status") in ("pending", "todo")
            ]

            completed = [t for t in tasks if t.get("status") in ("completed", "done")]

            result.metrics["total_tasks"] = len(tasks)
            result.metrics["blocked"] = len(blocked)
            result.metrics["high_priority_pending"] = len(high_prio_pending)
            result.metrics["completed"] = len(completed)
            result.metrics["completion_rate"] = round(len(completed) / max(len(tasks), 1) * 100, 1)

            if blocked:
                # Группируем по причинам
                reasons = {}
                for t in blocked:
                    reason = t.get("blocked_reason", t.get("description", "не указана"))[:100]
                    reasons.setdefault(reason, []).append(t.get("name", t.get("title", "N/A")))

                result.findings.append(f"🚧 Bottlenecks: {len(blocked)} задач заблокированы:")
                for reason, task_names in list(reasons.items())[:5]:
                    result.findings.append(f"  Причина: {reason}")
                    for tn in task_names[:3]:
                        result.findings.append(f"    - {tn[:100]}")

            if high_prio_pending:
                result.findings.append(f"\n🔴 Критичные задачи не начаты ({len(high_prio_pending)}):")
                for t in high_prio_pending[:5]:
                    result.findings.append(f"  - {t.get('name', t.get('title', 'N/A'))[:200]}")

            result.data_sources += len(tasks)
        except Exception as e:
            logger.warning(f"Process: tasks failed: {e}")

        # 2. Повторяющиеся проблемы (горячие темы)
        try:
            entities = await self.graph.find_nodes_by_label(label="Entity", limit=100)
            frequent = [
                e for e in entities
                if (e.get("frequency_score", 0) or 0) > 3
            ]
            frequent.sort(key=lambda x: x.get("frequency_score", 0), reverse=True)

            if frequent:
                result.findings.append("\n🔥 Повторяющиеся темы/проблемы:")
                for f in frequent[:8]:
                    result.findings.append(
                        f"  - {f.get('name', 'N/A')} "
                        f"(обсуждалась {f.get('frequency_score', 0)} раз)"
                    )
                result.data_sources += len(frequent)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        # 3. Зависимости между проектами
        try:
            projects = await self.graph.find_nodes_by_label(label="Project", limit=30)
            dep_chains = []

            for proj in projects[:15]:
                pid = proj.get("id")
                if not pid:
                    continue
                rels = await self.graph.get_node_relationships(pid)

                deps = []
                for direction in ("outgoing", "incoming"):
                    for rel in rels.get(direction, []):
                        rel_type = rel.get("relation", rel.get("type", ""))
                        if rel_type in ("DEPENDS_ON", "BLOCKED_BY"):
                            other_id = rel.get("target" if direction == "outgoing" else "source", "")
                            if other_id:
                                other = await self.graph.get_node_by_id(other_id)
                                if other:
                                    deps.append({
                                        "name": other.get("name", other_id),
                                        "type": rel_type,
                                        "status": other.get("status", "?"),
                                    })

                if deps:
                    dep_chains.append({
                        "project": proj.get("name", "N/A"),
                        "dependencies": deps,
                    })

            if dep_chains:
                result.findings.append("\n🔗 Зависимости между проектами:")
                for dc in dep_chains[:5]:
                    result.findings.append(f"  {dc['project']}:")
                    for dep in dc["dependencies"][:3]:
                        status_icon = "✅" if dep["status"] in ("completed", "done") else "🔴"
                        result.findings.append(f"    {dep['type']} → {dep['name']} {status_icon} [{dep['status']}]")
                result.data_sources += len(dep_chains)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        result.confidence = min(1.0, result.data_sources / 20.0)
        return result

    # ═══════════════════════════════════════════════════════════
    # FRAMEWORK 4: Risk Assessment
    # ═══════════════════════════════════════════════════════════

    async def risk_assessment(self) -> AnalysisResult:
        """
        Оценка рисков — скрытые риски, ignored warnings, зависимости.
        """
        result = AnalysisResult(
            framework="risk",
            title="Оценка рисков"
        )

        if not self.graph:
            return result

        # 1. Явные риски
        try:
            risks = await self.graph.find_nodes_by_label(label="Risk", limit=50)
            if risks:
                # Сортируем по severity
                severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
                risks_sorted = sorted(
                    risks,
                    key=lambda r: severity_order.get(r.get("impact", "medium"), 2)
                )

                result.findings.append(f"⚠️ Зафиксированные риски ({len(risks)}):")
                for r in risks_sorted[:10]:
                    name = r.get("description", r.get("name", "N/A"))
                    prob = r.get("probability", "?")
                    impact = r.get("impact", "?")
                    status = r.get("status", "open")
                    mitigation = r.get("mitigation", "")

                    icon = "🔴" if impact in ("critical", "high") else "🟡" if impact == "medium" else "🟢"
                    line = f"  {icon} {name[:200]}"
                    line += f"\n     Вероятность: {prob}, Влияние: {impact}, Статус: {status}"
                    if mitigation:
                        line += f"\n     Митигация: {mitigation[:200]}"
                    result.findings.append(line)

                result.metrics["total_risks"] = len(risks)
                result.metrics["critical_risks"] = len([r for r in risks if r.get("impact") in ("critical", "high")])
                result.data_sources += len(risks)
        except Exception as e:
            logger.warning(f"Risk: explicit risks failed: {e}")

        # 2. Скрытые риски — задачи которые обсуждались но не решены
        try:
            tasks = await self.graph.find_nodes_by_label(label="Task", limit=200)
            stale_critical = [
                t for t in tasks
                if t.get("priority") in ("high", "critical")
                and t.get("status") in ("pending", "todo", "backlog")
            ]
            if stale_critical:
                result.findings.append(f"\n🕳️ Скрытые риски — критичные задачи не начаты ({len(stale_critical)}):")
                for t in stale_critical[:5]:
                    result.findings.append(f"  - {t.get('name', t.get('title', 'N/A'))[:200]}")
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        result.confidence = min(1.0, result.data_sources / 10.0)
        return result

    # ═══════════════════════════════════════════════════════════
    # FRAMEWORK 5: Capability Gap Analysis
    # ═══════════════════════════════════════════════════════════

    async def capability_gap_analysis(self) -> AnalysisResult:
        """
        Анализ пробелов в компетенциях.

        Определяет:
        - Задачи без исполнителя (нет компетенций?)
        - Постоянно блокируемые задачи определённого типа
        - Темы, о которых часто говорят, но не делают
        """
        result = AnalysisResult(
            framework="capability_gap",
            title="Анализ пробелов в компетенциях"
        )

        if not self.graph:
            return result

        # 1. Задачи без исполнителя
        try:
            tasks = await self.graph.find_nodes_by_label(label="Task", limit=200)

            unassigned = [
                t for t in tasks
                if not t.get("assignee") or t.get("assignee", "").lower() in ("", "не назначен", "none", "n/a")
            ]

            if unassigned:
                result.findings.append(f"❓ Задачи без исполнителя ({len(unassigned)}) — возможно, нет компетенций:")
                for t in unassigned[:10]:
                    name = t.get("name", t.get("title", "N/A"))
                    priority = t.get("priority", "")
                    prio_icon = "🔴" if priority in ("high", "critical") else ""
                    result.findings.append(f"  - {prio_icon} {name[:200]}")
                result.data_sources += len(unassigned)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        # 2. Постоянно блокируемые задачи — паттерн отсутствия навыков
        try:
            tasks = await self.graph.find_nodes_by_label(label="Task", limit=200)
            blocked = [t for t in tasks if t.get("status") in ("blocked", "stuck")]

            if blocked:
                # Группируем по типу/описанию чтобы найти паттерн
                keywords_freq = {}
                for t in blocked:
                    desc = (t.get("name", "") + " " + t.get("description", "")).lower()
                    words = desc.split()
                    for w in words:
                        if len(w) > 4:
                            keywords_freq[w] = keywords_freq.get(w, 0) + 1

                # Самые частые слова в blocked tasks
                frequent_blocked = sorted(keywords_freq.items(), key=lambda x: x[1], reverse=True)[:10]

                if frequent_blocked:
                    result.findings.append("\n🔍 Повторяющиеся темы в заблокированных задачах:")
                    for word, count in frequent_blocked:
                        if count >= 2:
                            result.findings.append(f"  - '{word}' ({count} раз) — возможная область недостающей компетенции")
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        # 3. Knowledge ноды — что компания уже знает
        try:
            knowledge = await self.graph.find_nodes_by_label(label="Knowledge", limit=50)
            if knowledge:
                categories = {}
                for k in knowledge:
                    cat = k.get("knowledge_category", k.get("category", "Общее"))
                    categories[cat] = categories.get(cat, 0) + 1

                result.findings.append(f"\n📚 Области знаний компании ({len(knowledge)} записей):")
                for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True)[:8]:
                    result.findings.append(f"  - {cat}: {count} записей")
                result.data_sources += len(knowledge)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        # LLM рекомендации
        if self.llm and result.findings:
            try:
                prompt = f"""На основе анализа пробелов в компетенциях компании:

{chr(10).join(result.findings[:20])}

Определи:
1. Каких специалистов критически не хватает (и почему)
2. Что можно решить обучением текущих сотрудников
3. Где нужно именно нанимать нового человека
4. Приоритеты (что нанять первым)

Ответь конкретно, с обоснованием из данных."""

                recs = await self.llm.generate(prompt=prompt, max_tokens=600)
                result.recommendations = [r.strip() for r in recs.split("\n") if r.strip() and not r.strip().startswith("#")]
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        result.confidence = min(1.0, result.data_sources / 15.0)
        return result

    # ═══════════════════════════════════════════════════════════
    # FRAMEWORK 6: SWOT & Competitive Analysis
    # ═══════════════════════════════════════════════════════════

    async def swot_analysis(self) -> AnalysisResult:
        """
        SWOT-анализ компании на основе данных из графа.

        Strengths: проекты completed, KPI рост, позитивные решения
        Weaknesses: blocked tasks, risks, bottlenecks
        Opportunities: обсуждаемые но не начатые инициативы
        Threats: high-impact risks, зависимости, конкуренция
        """
        result = AnalysisResult(
            framework="swot",
            title="SWOT-анализ"
        )

        if not self.graph:
            return result

        strengths = []
        weaknesses = []
        opportunities = []
        threats = []

        try:
            # Strengths: завершённые задачи, растущие KPI
            tasks = await self.graph.find_nodes_by_label(label="Task", limit=200)
            completed = [t for t in tasks if t.get("status") in ("completed", "done")]
            total = len(tasks)
            if total > 0:
                rate = len(completed) / total * 100
                if rate > 60:
                    strengths.append(f"Высокий процент выполнения задач: {rate:.0f}% ({len(completed)}/{total})")

            kpis = await self.graph.find_nodes_by_label(label="KPI", limit=50)
            growing_kpis = [k for k in kpis if k.get("trend") in ("up", "рост")]
            if growing_kpis:
                for k in growing_kpis[:3]:
                    strengths.append(f"Рост KPI: {k.get('name', 'N/A')} — {k.get('numeric_value', '?')} {k.get('unit', '')}")

            # Knowledge база
            knowledge = await self.graph.find_nodes_by_label(label="Knowledge", limit=50)
            if len(knowledge) > 10:
                strengths.append(f"Богатая база знаний: {len(knowledge)} записей")

            # Weaknesses: blocked, высокая нагрузка
            blocked = [t for t in tasks if t.get("status") in ("blocked", "stuck")]
            if blocked:
                weaknesses.append(f"{len(blocked)} задач заблокированы")

            risks = await self.graph.find_nodes_by_label(label="Risk", limit=30)
            open_risks = [r for r in risks if r.get("status") != "mitigated"]
            if open_risks:
                weaknesses.append(f"{len(open_risks)} открытых рисков")

            contradictions = await self.graph.find_nodes_by_label(label="Contradiction", limit=10)
            if contradictions:
                weaknesses.append(f"{len(contradictions)} противоречий в данных")

            # Opportunities: pending high-priority tasks, scenarios
            high_prio_pending = [
                t for t in tasks
                if t.get("priority") in ("high", "critical")
                and t.get("status") in ("pending", "todo", "backlog")
            ]
            if high_prio_pending:
                for t in high_prio_pending[:3]:
                    opportunities.append(f"Инициатива: {t.get('name', t.get('title', 'N/A'))[:100]}")

            scenarios = await self.graph.find_nodes_by_label(label="Scenario", limit=10)
            for s in scenarios[:3]:
                opportunities.append(f"Сценарий: {s.get('name', s.get('description', 'N/A'))[:100]}")

            # Threats: high-impact risks
            critical_risks = [r for r in risks if r.get("impact") in ("critical", "high")]
            for r in critical_risks[:3]:
                threats.append(f"Риск: {r.get('description', r.get('name', 'N/A'))[:100]}")

            result.data_sources = total + len(kpis) + len(risks) + len(knowledge)

        except Exception as e:
            logger.warning(f"SWOT: data collection failed: {e}")

        # Форматируем
        if strengths:
            result.findings.append("💪 STRENGTHS (Сильные стороны):")
            for s in strengths:
                result.findings.append(f"  + {s}")

        if weaknesses:
            result.findings.append("\n⚠️ WEAKNESSES (Слабые стороны):")
            for w in weaknesses:
                result.findings.append(f"  - {w}")

        if opportunities:
            result.findings.append("\n🚀 OPPORTUNITIES (Возможности):")
            for o in opportunities:
                result.findings.append(f"  → {o}")

        if threats:
            result.findings.append("\n🔴 THREATS (Угрозы):")
            for t in threats:
                result.findings.append(f"  ⚡ {t}")

        result.metrics["strengths"] = len(strengths)
        result.metrics["weaknesses"] = len(weaknesses)
        result.metrics["opportunities"] = len(opportunities)
        result.metrics["threats"] = len(threats)
        result.confidence = min(1.0, result.data_sources / 30.0)

        return result

    # ═══════════════════════════════════════════════════════════
    # FRAMEWORK 7: Proactive Insights Generator
    # ═══════════════════════════════════════════════════════════

    async def proactive_insights(self, meeting_data: Optional[Dict[str, Any]] = None) -> AnalysisResult:
        """
        Генерация проактивных инсайтов — что система заметила сама.

        Как в сценарии "ГОНКА" — Tessbrain говорит:
        "У НордИнвест за последний год было 3 похожих тендера"

        Анализирует:
        - Связи между текущими задачами и прошлым опытом
        - Паттерны, которые повторяются
        - Незамеченные проблемы и возможности
        """
        result = AnalysisResult(
            framework="proactive",
            title="Проактивные инсайты"
        )

        if not self.graph:
            return result

        try:
            # 1. Задачи которые стоят давно (никто не заметил?)
            tasks = await self.graph.find_nodes_by_label(label="Task", limit=200)
            stale = [
                t for t in tasks
                if t.get("status") in ("pending", "todo")
                and t.get("priority") in ("high", "critical")
            ]
            if stale:
                result.findings.append("💡 Незамеченные задачи высокого приоритета:")
                for t in stale[:5]:
                    result.findings.append(f"  - {t.get('name', t.get('title', 'N/A'))[:200]}")

            # 2. Темы с высокой frequency, но без решений
            entities = await self.graph.find_nodes_by_label(label="Entity", limit=100)
            hot = [e for e in entities if (e.get("frequency_score", 0) or 0) > 5]

            decisions = await self.graph.find_nodes_by_label(label="Decision", limit=100)
            decision_topics = set()
            for d in decisions:
                text = (d.get("text", "") + " " + d.get("title", "")).lower()
                decision_topics.add(text)

            unresolved_hot = []
            for h in hot:
                name = h.get("name", "").lower()
                # Проверяем, есть ли решение по этой теме
                has_decision = any(name in dt for dt in decision_topics if len(name) > 3)
                if not has_decision:
                    unresolved_hot.append(h)

            if unresolved_hot:
                result.findings.append("\n🔥 Частые темы БЕЗ принятых решений:")
                for h in unresolved_hot[:5]:
                    result.findings.append(
                        f"  - {h.get('name', 'N/A')} "
                        f"(обсуждалась {h.get('frequency_score', 0)} раз, решения не зафиксировано)"
                    )

            # 3. KPI с негативным трендом
            kpis = await self.graph.find_nodes_by_label(label="KPI", limit=50)
            declining = [k for k in kpis if k.get("trend") in ("down", "падение", "снижение")]
            if declining:
                result.findings.append("\n📉 KPI со снижающимся трендом:")
                for k in declining[:5]:
                    result.findings.append(
                        f"  - {k.get('name', 'N/A')}: {k.get('numeric_value', '?')} {k.get('unit', '')} ↓"
                    )

            result.data_sources = len(tasks) + len(entities) + len(kpis)
        except Exception as e:
            logger.warning(f"Proactive insights failed: {e}")

        result.confidence = min(1.0, result.data_sources / 30.0)
        return result

    def format_results(self, results: List[AnalysisResult]) -> str:
        """Форматирование результатов анализа в Markdown."""
        parts = []

        for r in results:
            parts.append(f"\n## {r.title}")
            parts.append(f"*Фреймворк: {r.framework}, уверенность: {r.confidence:.0%}, источников: {r.data_sources}*\n")

            if r.findings:
                parts.append("### Находки")
                parts.extend(r.findings)

            if r.metrics:
                parts.append("\n### Метрики")
                for k, v in r.metrics.items():
                    parts.append(f"- {k}: **{v}**")

            if r.recommendations:
                parts.append("\n### Рекомендации")
                parts.extend(r.recommendations)

        return "\n".join(parts)


# Singleton
_consulting_instance: Optional[ConsultingAnalyzer] = None


def get_consulting_analyzer(graph_builder=None, llm_router=None) -> ConsultingAnalyzer:
    """Получить экземпляр ConsultingAnalyzer."""
    global _consulting_instance
    if _consulting_instance is None:
        _consulting_instance = ConsultingAnalyzer(graph_builder, llm_router)
    else:
        if graph_builder:
            _consulting_instance.graph = graph_builder
        if llm_router:
            _consulting_instance.llm = llm_router
    return _consulting_instance
