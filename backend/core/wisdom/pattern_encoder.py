"""
JEPA-inspired Pattern Encoder — Слой 3 BWE

Инкрементальная кластеризация Decision Events в абстрактные паттерны.

Ключевые улучшения vs v1:
- Инкрементальная кластеризация: обрабатываем только новые/изменённые решения
- Fix pattern_id: flush перед присвоением pattern_id (исправлен race condition)
- UTC-aware datetime
- Меньше LLM: имена кэшируются, differentiators — только при наличии обоих типов исходов
- Правильный подсчёт outcome_distribution (по решениям, не по исходам)
- Пометка нуждающихся в обновлении анализа (needs_analysis_update)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from openai import AsyncOpenAI
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.wisdom.bwe_models import BWEDecisionEvent, BWEOutcomeEvent, BWEWisdomPattern

logger = logging.getLogger(__name__)


def decay_confidence_score(
    confidence_score: Optional[float],
    last_calibrated_at: Optional[datetime],
    dist: Optional[dict],
    now: Optional[datetime] = None,
    decay_rate: float = 0.02,
    access_count: int = 0,
) -> Optional[float]:
    """Time-decay confidence_score с contradiction-multiplier (порт из
    Tessbrain modules/apoptosis.py, адаптация под ленивое чтение).

    Tessbrain: `pc · (1 − decay_rate · mult · …)^days_stale` в ночном job.
    У нас нет фонового движка BWE, поэтому decay применяется на ЧТЕНИИ:
    хранимый confidence_score остаётся «свежим», наружу отдаётся состаренный.

    - mult = 1 + contradiction_ratio·2  (противоречивые кристаллы стареют
      вдвое быстрее; contradiction_ratio = F/(S+F) из outcome_distribution).
    - Гранулярность — недели (бизнес-решения редки), decay_rate=0.02/нед.
    - Пол 0.05. NULL confidence_score или last_calibrated_at → НЕ угасать
      (back-compat: некалиброванный кристалл не «стареет», он просто новый).
    """
    if confidence_score is None or last_calibrated_at is None:
        return confidence_score
    now = now or datetime.now(timezone.utc)
    # last_calibrated_at может прийти naive — трактуем как UTC.
    if last_calibrated_at.tzinfo is None:
        last_calibrated_at = last_calibrated_at.replace(tzinfo=timezone.utc)
    delta_days = (now - last_calibrated_at).total_seconds() / 86400.0
    if delta_days <= 0:
        return confidence_score
    weeks = delta_days / 7.0
    d = dist or {}
    s = d.get("success", 0)
    f = d.get("failure", 0)
    contradiction_ratio = f / (s + f) if (s + f) > 0 else 0.0
    mult = 1.0 + contradiction_ratio * 2.0
    # B2 retrieval-as-learning: часто извлекаемые кристаллы забываются медленнее
    # (сопротивление ∈ (0,1]; больше access → меньше эффективная скорость decay).
    resistance = 1.0 / (1.0 + math.log1p(max(0, access_count or 0)) * 0.3)
    decayed = confidence_score * (1.0 - decay_rate * mult * resistance) ** weeks
    return round(max(0.05, decayed), 4)


PATTERN_NAME_PROMPT = """Дай короткое snake_case название бизнес-паттерну (3-5 слов).

Примеры:
- Ценовое давление конкурентов → pricing_pressure_response
- Выбор скорость vs качество → speed_quality_tradeoff
- Найм или аутсорс → hiring_vs_outsource

Ситуации, образующие паттерн:
{situations}

Верни JSON: {{"pattern_name": "short_snake_case_name"}}"""

DIFFERENTIATORS_PROMPT = """Найди ключевые факторы успеха и провала в бизнес-паттерне.

Паттерн: {pattern_name}

Успешные случаи:
{success_cases}

Неуспешные случаи:
{failure_cases}

Верни JSON:
{{
  "context_differentiators": ["фактор успеха: ...", "фактор провала: ...", "условие: ..."],
  "vitek_sensitivity": "как паттерн работает на разных витках (если видно из данных)"
}}"""

# Crystal V2 (за флагом crystal_enrichment_enabled): то же, что выше, плюс
# причинно-логическое обогащение по ARC-методологии — границы применимости
# (not_law) и честные пробелы (open_holes). Старые ключи не меняются, поэтому
# парсер ниже совместим с обоими промптами.
DIFFERENTIATORS_PROMPT_V2 = """Найди факторы успеха и провала в бизнес-паттерне И очерти границы его применимости.

Паттерн: {pattern_name}

Успешные случаи:
{success_cases}

Неуспешные случаи:
{failure_cases}

Верни JSON:
{{
  "context_differentiators": ["фактор успеха: ...", "фактор провала: ...", "условие: ..."],
  "vitek_sensitivity": "как паттерн работает на разных витках (если видно из данных)",
  "not_law": ["паттерн НЕ применим, когда ...", "ломается при ..."],
  "open_holes": ["в данных мало про ...", "неясно поведение при ..."]
}}

Правила:
- not_law: где паттерн ЛОМАЕТСЯ — условия, при которых он давал провал или
  неприменим. Это НЕ универсальный закон, а его границы.
- open_holes: честные пробелы — чего в данных не хватает для уверенности.
- Если данных недостаточно — верни пустые списки, НЕ выдумывай."""


class PatternEncoder:
    """
    Инкрементальный кодировщик паттернов.

    Алгоритм:
    1. Загружаем только решения без pattern_id (новые) + паттерны пользователя
    2. Пытаемся добавить новые решения в существующие паттерны
    3. Создаём новые паттерны для не-матчнутых решений
    4. Обновляем центроиды изменившихся паттернов
    5. Пересчитываем confidence и outcome_distribution
    6. Асинхронно генерируем differentiators для паттернов с флагом
    """

    MIN_MEDIUM_CONFIDENCE = 3
    MIN_HIGH_CONFIDENCE = 10

    def __init__(
        self,
        client: AsyncOpenAI,
        chat_model: str,
        cluster_threshold: float = 0.72,
        crystal_enrichment: bool = False,
    ):
        self.client = client
        self.chat_model = chat_model
        self.CLUSTER_THRESHOLD = cluster_threshold
        # Когда True — differentiators дополняются not_law/open_holes (V2-промпт).
        # Default False сохраняет прежнее поведение для существующих вызовов.
        self.crystal_enrichment = crystal_enrichment

    async def refresh_pattern_stats(
        self,
        session: AsyncSession,
        user_id: str,
        pattern_ids: Optional[list] = None,
    ) -> list[BWEWisdomPattern]:
        """Пересчитать распределение исходов и уверенность БЕЗ кластеризации.

        Зачем отдельный метод. Петля обучения слоя — «решение → исход →
        обновлённое распределение» — была разомкнута ровно здесь:
        `encode_incremental` выходит на `return []`, если новых
        некластеризованных решений нет, а `_update_pattern_stats` вызывается
        только ниже этого выхода. Значит привязка исхода к УЖЕ
        кластеризованному решению (обычный случай: решение приняли в марте,
        ретроспектива прошла в июне) распределение не меняла: паттерн знал
        «4 случая, исходы неизвестны» и после появления исходов продолжал
        так думать. Догоняло только случайно — если в тот же кластер
        прилетало новое решение — или ручным полным пересбором, который
        сносит паттерны вместе с историей эволюции.

        pattern_ids=None → все паттерны пользователя.
        """
        conds = [BWEWisdomPattern.user_id == user_id]
        if pattern_ids:
            conds.append(BWEWisdomPattern.id.in_([str(p) for p in pattern_ids]))
        result = await session.execute(select(BWEWisdomPattern).where(*conds))
        patterns = result.scalars().all()
        if not patterns:
            return []

        outcome_result = await session.execute(
            select(BWEOutcomeEvent).where(BWEOutcomeEvent.user_id == user_id)
        )
        outcomes_by_decision: dict = defaultdict(list)
        for o in outcome_result.scalars().all():
            outcomes_by_decision[str(o.decision_event_id)].append(o)

        for pattern in patterns:
            await self._update_pattern_stats(
                session=session,
                pattern=pattern,
                outcomes_by_decision=outcomes_by_decision,
            )
        return list(patterns)

    async def encode_incremental(
        self,
        session: AsyncSession,
        user_id: str,
    ) -> list[BWEWisdomPattern]:
        """
        Инкрементальная кластеризация: только новые решения (без pattern_id).

        Значительно быстрее полной перекластеризации.
        Возвращает список изменённых паттернов.
        """
        # Загружаем новые решения (без pattern_id) с эмбеддингами.
        conds = [
            BWEDecisionEvent.user_id == user_id,
            BWEDecisionEvent.situation_embedding.isnot(None),
            BWEDecisionEvent.pattern_id.is_(None),
        ]
        # B1 bi-temporal (за crystal-флагом): не кластеризуем решения с истёкшим
        # контекстом — кристалл не должен строиться на больше-не-истинном.
        if self.crystal_enrichment:
            conds.append(or_(
                BWEDecisionEvent.valid_until.is_(None),
                BWEDecisionEvent.valid_until >= datetime.now(timezone.utc),
            ))
        new_result = await session.execute(
            select(BWEDecisionEvent).where(*conds).order_by(BWEDecisionEvent.created_at)
        )
        new_decisions = new_result.scalars().all()

        if not new_decisions:
            logger.debug(f"[PatternEncoder] No new decisions to cluster for user {user_id}")
            return []

        # Загружаем существующие паттерны
        pattern_result = await session.execute(
            select(BWEWisdomPattern)
            .where(BWEWisdomPattern.user_id == user_id)
        )
        existing_patterns = pattern_result.scalars().all()

        # Загружаем все исходы для пользователя
        [d.id for d in new_decisions]
        outcome_result = await session.execute(
            select(BWEOutcomeEvent)
            .where(BWEOutcomeEvent.user_id == user_id)
        )
        all_outcomes = outcome_result.scalars().all()
        outcomes_by_decision: dict = defaultdict(list)
        for o in all_outcomes:
            outcomes_by_decision[str(o.decision_event_id)].append(o)

        changed_patterns: list[BWEWisdomPattern] = []

        for dec in new_decisions:
            emb = dec.situation_embedding
            if not emb:
                continue

            matched_pattern = self._find_best_pattern(emb, existing_patterns)

            if matched_pattern:
                # Добавляем в существующий паттерн
                instances = matched_pattern.instances or []
                dec_id_str = str(dec.id)
                if dec_id_str not in instances:
                    instances.append(dec_id_str)
                    matched_pattern.instances = instances
                    # Обновляем центроид (incremental running mean)
                    matched_pattern.prototype_vector = self._update_centroid(
                        current=matched_pattern.prototype_vector,
                        new_vec=emb,
                        n=len(instances),
                    )
                    matched_pattern.needs_analysis_update = True
                    if matched_pattern not in changed_patterns:
                        changed_patterns.append(matched_pattern)
            else:
                # Создаём новый паттерн
                pattern_name = await self._generate_pattern_name(
                    situations=[dec.situation[:100]]
                )
                new_pattern = BWEWisdomPattern(
                    user_id=user_id,
                    pattern_name=pattern_name,
                    prototype_vector=list(emb),
                    instances=[str(dec.id)],
                    outcome_distribution={"success": 0, "failure": 0, "partial": 0, "unknown": 0},
                    confidence="low",
                    needs_analysis_update=False,
                )
                session.add(new_pattern)
                # ВАЖНО: flush перед присвоением pattern_id — иначе id = None
                await session.flush()
                existing_patterns.append(new_pattern)
                changed_patterns.append(new_pattern)

            # Присваиваем pattern_id решению ПОСЛЕ flush
            await session.flush()
            pattern_for_dec = matched_pattern or changed_patterns[-1]
            dec.pattern_id = str(pattern_for_dec.id)

        # Обновляем confidence и outcome_distribution для изменённых паттернов
        for pattern in changed_patterns:
            await self._update_pattern_stats(
                session=session,
                pattern=pattern,
                outcomes_by_decision=outcomes_by_decision,
            )

        # Асинхронно генерируем differentiators для паттернов с флагом
        patterns_needing_analysis = [
            p for p in changed_patterns
            if p.needs_analysis_update and self._has_both_outcomes(p)
        ]
        if patterns_needing_analysis:
            # Кап + ограниченная параллельность: раньше был неограниченный
            # asyncio.gather — при много новых решений это сотни одновременных
            # LLM-вызовов (флуд, rate-limit, память). TESSENT_BWE_MAX_PATTERNS
            # ограничивает число за прогон (деф. 30, 0 = без лимита); семафор —
            # не больше 4 параллельно. Остаток доберётся в следующие прогоны.
            import os as _os
            try:
                _cap = int(_os.getenv("TESSENT_BWE_MAX_PATTERNS", "30"))
            except (TypeError, ValueError):
                _cap = 30
            subset = (patterns_needing_analysis if _cap <= 0
                      else patterns_needing_analysis[:_cap])
            if len(subset) < len(patterns_needing_analysis):
                logger.info(
                    f"[PatternEncoder] {len(patterns_needing_analysis)} patterns "
                    f"need analysis; capping to {len(subset)} this run "
                    f"(TESSENT_BWE_MAX_PATTERNS)"
                )
            _sem = asyncio.Semaphore(4)

            async def _bounded(pat):
                async with _sem:
                    await self._update_differentiators(
                        session, pat, outcomes_by_decision)

            await asyncio.gather(*[_bounded(p) for p in subset])

        await session.flush()
        logger.info(
            f"[PatternEncoder] Processed {len(new_decisions)} new decisions "
            f"→ {len(changed_patterns)} patterns updated for user {user_id}"
        )
        return changed_patterns

    async def full_recluster(
        self,
        session: AsyncSession,
        user_id: str,
    ) -> list[BWEWisdomPattern]:
        """
        Полная перекластеризация. Используется только при ручном запросе
        или при изменении порогов кластеризации.

        ВНИМАНИЕ: дорогая операция O(n²) по числу решений.
        """
        # Сбрасываем все pattern_id
        all_dec_result = await session.execute(
            select(BWEDecisionEvent)
            .where(
                BWEDecisionEvent.user_id == user_id,
                BWEDecisionEvent.situation_embedding.isnot(None),
            )
            .order_by(BWEDecisionEvent.created_at)
        )
        all_decisions = all_dec_result.scalars().all()

        for dec in all_decisions:
            dec.pattern_id = None
        await session.flush()

        # Удаляем все старые паттерны
        old_pattern_result = await session.execute(
            select(BWEWisdomPattern)
            .where(BWEWisdomPattern.user_id == user_id)
        )
        for p in old_pattern_result.scalars().all():
            await session.delete(p)
        await session.flush()

        # Инкрементальная кластеризация по-новой
        return await self.encode_incremental(session=session, user_id=user_id)

    def _find_best_pattern(
        self,
        embedding: list[float],
        patterns: list[BWEWisdomPattern],
    ) -> Optional[BWEWisdomPattern]:
        """Найти лучший существующий паттерн для нового решения."""
        best_sim = 0.0
        best_pattern = None
        for p in patterns:
            if not p.prototype_vector:
                continue
            sim = self._cosine_similarity(embedding, p.prototype_vector)
            if sim > best_sim:
                best_sim = sim
                best_pattern = p
        return best_pattern if best_sim >= self.CLUSTER_THRESHOLD else None

    async def _update_pattern_stats(
        self,
        session: AsyncSession,
        pattern: BWEWisdomPattern,
        outcomes_by_decision: dict,
    ) -> None:
        """Пересчитать confidence и outcome_distribution для паттерна."""
        instances = pattern.instances or []
        total = len(instances)

        # ── Дедуп «случаев»: одно РЕШЕНИЕ, обсуждённое на трёх встречах, —
        # это один случай, а не три. Без этого «из 4 похожих случаев 3
        # закрылись успешно» могло описывать один случай, упомянутый
        # четырежды. Ключ случая — нормализованный текст решения; исходы
        # упоминаний одного случая объединяются.
        instance_rows = []
        if instances:
            res = await session.execute(
                select(BWEDecisionEvent).where(
                    BWEDecisionEvent.id.in_([str(i) for i in instances])))
            instance_rows = res.scalars().all()
        cases: dict = {}
        found_ids = set()
        for dec in instance_rows:
            found_ids.add(str(dec.id))
            key = " ".join(str(dec.decision or dec.situation or dec.id)
                           .lower().split())[:200]
            cases.setdefault(key, []).extend(
                outcomes_by_decision.get(str(dec.id), []))
        # id из instances, не найденные в БД (устаревшие ссылки), считаем
        # отдельными случаями без исходов — не теряем их молча.
        for dec_id in instances:
            if str(dec_id) not in found_ids:
                cases.setdefault(f"__missing__{dec_id}", [])

        # Подсчёт per-case. Приоритет провала над успехом: если у одного
        # случая зафиксированы и успех, и провал — это противоречивые
        # свидетельства, и считать их успехом значило бы систематически
        # завышать оптимизм ровно там, где данные спорят. Провал дороже.
        dist = {"success": 0, "failure": 0, "partial": 0, "unknown": 0}
        for outcomes in cases.values():
            if not outcomes:
                dist["unknown"] += 1
            else:
                types = {o.outcome_type for o in outcomes}
                if "failure" in types:
                    dist["failure"] += 1
                elif "partial" in types:
                    dist["partial"] += 1
                elif "success" in types:
                    dist["success"] += 1
                else:
                    dist["unknown"] += 1

        _prev_dist = dict(pattern.outcome_distribution or {})
        pattern.outcome_distribution = dist

        # Confidence на основе числа случаев С ИЗВЕСТНЫМИ ИСХОДАМИ.
        #
        # Здесь стояло `known >= N or total >= N`, где total — просто число
        # решений в кластере. То есть паттерн из десяти решений, о судьбе
        # которых не известно НИЧЕГО, объявлялся "high". Это противоречило
        # и комментарию над самой строкой, и смыслу всего слоя: уверенность
        # обязана расти от подтверждённых исходов, а не от того, сколько раз
        # похожую тему обсуждали. Ветка `or total` убрана.
        known = dist["success"] + dist["failure"] + dist["partial"]
        if known >= self.MIN_HIGH_CONFIDENCE:
            pattern.confidence = "high"
        elif known >= self.MIN_MEDIUM_CONFIDENCE:
            pattern.confidence = "medium"
        else:
            pattern.confidence = "low"

        # Crystal (за флагом): численный confidence_score + денормализация осей.
        # OFF → ничего из этого не выполняется, лишнего запроса decisions нет.
        if self.crystal_enrichment:
            # Асимметричная уверенность: считается из распределения исходов,
            # без обращения к БД (дёшево).
            pattern.confidence_score = self._asymmetric_confidence(dist)
            # A (Phase 1): якорь свежести для time-decay переставляем ТОЛЬКО
            # когда состав ИЗВЕСТНЫХ исходов изменился. Раньше условие было
            # `known > 0` при любом пересчёте — новое решение, капнувшее в
            # кластер, омолаживало якорь без единого нового исхода, и вывод
            # трёхлетней давности не старел, пока тему продолжали обсуждать.
            _known_changed = any(
                dist.get(k, 0) != _prev_dist.get(k, 0)
                for k in ("success", "failure", "partial")
            )
            if known > 0 and (_known_changed or pattern.last_calibrated_at is None):
                pattern.last_calibrated_at = datetime.now(timezone.utc)
            # Оси: доминирующий tension_type + диапазон company_vitek по
            # решениям паттерна. Один запрос на паттерн (как в differentiators).
            await self._denormalize_axes(session, pattern, instances)

    @staticmethod
    def _asymmetric_confidence(dist: dict) -> float:
        """Численная уверенность с асимметрией: подтверждение поднимает
        медленно, опровержение роняет быстро. Stateless (зависит только от
        распределения), поэтому безопасно при полном пересчёте.

        Формула — сглаживание с приором и штрафом за провалы:
            pos = S + 0.5·P + α       (α=1: приор «скорее работает»)
            neg = λ·F + β             (λ=2: провал весит вдвое; β=1)
            score = pos / (pos + neg)
        Свойства: 0 данных → 0.5; +1 success → ~0.67 (медленно);
        +1 failure → 0.25 (резко); каждый провал тянет сильнее успеха.
        """
        s = dist.get("success", 0)
        f = dist.get("failure", 0)
        p = dist.get("partial", 0)
        alpha, beta, lam = 1.0, 1.0, 2.0
        pos = s + 0.5 * p + alpha
        neg = lam * f + beta
        return round(pos / (pos + neg), 4)

    async def _denormalize_axes(
        self,
        session: AsyncSession,
        pattern: BWEWisdomPattern,
        instances: list,
    ) -> None:
        """Денормализовать «оси» из связанных Decision Events:
        primary_tension (доминирующий tension_type) и диапазон company_vitek.

        НЕ новая таксономия — копирует уже существующие поля Decision Events
        в паттерн ради быстрого фильтра поиска (search_with_axis).
        """
        if not instances:
            return
        dec_result = await session.execute(
            select(BWEDecisionEvent)
            .where(BWEDecisionEvent.id.in_(instances))
        )
        decisions = dec_result.scalars().all()
        if not decisions:
            return

        # Доминирующий tension_type (мода; пустые/None игнорируем).
        tension_counts: dict = defaultdict(int)
        viteks: list = []
        for d in decisions:
            t = getattr(d, "tension_type", None)
            if t:
                tension_counts[t] += 1
            v = getattr(d, "company_vitek", None)
            if v is not None:
                viteks.append(v)

        if tension_counts:
            pattern.primary_tension = max(tension_counts.items(), key=lambda kv: kv[1])[0]
        if viteks:
            pattern.vitek_phase_min = min(viteks)
            pattern.vitek_phase_max = max(viteks)

    async def _update_differentiators(
        self,
        session: AsyncSession,
        pattern: BWEWisdomPattern,
        outcomes_by_decision: dict,
    ) -> None:
        """Сгенерировать context_differentiators и vitek_sensitivity через LLM."""
        instances = pattern.instances or []

        # Загружаем нужные Decision Events
        dec_result = await session.execute(
            select(BWEDecisionEvent)
            .where(BWEDecisionEvent.id.in_(instances[:20]))
        )
        decisions = {str(d.id): d for d in dec_result.scalars().all()}

        success_cases = []
        failure_cases = []
        for dec_id in instances:
            dec = decisions.get(dec_id)
            if not dec:
                continue
            outcomes = outcomes_by_decision.get(dec_id, [])
            types = {o.outcome_type for o in outcomes}
            outcome_desc = next((o.outcome_description for o in outcomes), "")
            # Phase 2 D: помечаем эпистемику источника, чтобы LLM при выводе
            # not_law/факторов весил наблюдения выше утверждений/прогнозов.
            epi = getattr(dec, "epistemic_type", None) or "claimed"
            epi_tag = "" if epi == "claimed" else f"[{epi}] "
            case_text = f"{epi_tag}Ситуация: {dec.situation[:80]} | Решение: {dec.decision[:60]} | Исход: {outcome_desc[:80]}"
            if "success" in types:
                success_cases.append(case_text)
            elif "failure" in types:
                failure_cases.append(case_text)

        if not success_cases or not failure_cases:
            pattern.needs_analysis_update = False
            return

        # V2-промпт (за флагом) просит больше — ему нужен и больший бюджет токенов.
        template = DIFFERENTIATORS_PROMPT_V2 if self.crystal_enrichment else DIFFERENTIATORS_PROMPT
        max_tokens = 500 if self.crystal_enrichment else 300
        prompt = template.format(
            pattern_name=pattern.pattern_name,
            success_cases="\n".join(success_cases[:3]),
            failure_cases="\n".join(failure_cases[:3]),
        )
        try:
            resp = await self.client.chat.completions.create(
                model=self.chat_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
            )
            data = json.loads(resp.choices[0].message.content)
            new_diff = data.get("context_differentiators", [])
            # B (Phase 1): evolution_log — версионируем СДВИГ понимания. Если
            # differentiators реально изменились vs прошлый прогон — дописываем
            # "v{n}: …" (порт concept_crystallizer.evolution_log). Только за
            # флагом; первая генерация (пустой old) не логируется как «эволюция».
            if self.crystal_enrichment:
                old_diff = pattern.context_differentiators or []
                if old_diff and new_diff and new_diff != old_diff:
                    log = list(pattern.evolution_log or [])
                    head = "; ".join(new_diff[:2])[:80]
                    log.append(f"v{len(log) + 1} @ {datetime.now(timezone.utc).date()}: {head}")
                    pattern.evolution_log = log

                    # Зеркалирование в SupersedeStore (MEMORY_DESIGN_PRINCIPLES §6 №3).
                    # Best-effort: BWE-логика не падает при ошибке зеркала.
                    # За флагом OFF → дублирование выключено.
                    try:
                        from backend.core.config.feature_flags import get_feature_flags
                        if get_feature_flags().enable_supersede_mirror:
                            from backend.core.memory.bwe_supersede_mirror import (
                                mirror_pattern_change,
                            )
                            mirror_pattern_change(
                                user_id=str(pattern.user_id),
                                pattern_id=str(pattern.id),
                                new_value=new_diff,
                                dimension="differentiators",
                            )
                    except Exception as e:
                        logger.debug(f"supersede-mirror skipped (non-critical): {e}")
            pattern.context_differentiators = new_diff
            if data.get("vitek_sensitivity"):
                pattern.vitek_sensitivity = data["vitek_sensitivity"]
            # Crystal-обогащение: пишем только при включённом флаге И только
            # непустые значения — чтобы повторный прогон с обеднённым ответом
            # LLM не затёр ранее извлечённые границы (как с vitek_sensitivity).
            if self.crystal_enrichment:
                if data.get("not_law"):
                    pattern.not_law = data["not_law"]
                if data.get("open_holes"):
                    pattern.open_holes = data["open_holes"]
        except Exception as e:
            logger.debug(f"[PatternEncoder] Differentiators failed for {pattern.pattern_name}: {e}")
        finally:
            pattern.needs_analysis_update = False

    async def _generate_pattern_name(self, situations: list[str]) -> str:
        """Генерировать имя паттерна через LLM."""
        sample = "\n".join(f"- {s[:100]}" for s in situations[:5])
        try:
            resp = await self.client.chat.completions.create(
                model=self.chat_model,
                messages=[{"role": "user", "content": PATTERN_NAME_PROMPT.format(situations=sample)}],
                temperature=0.2,
                response_format={"type": "json_object"},
                max_tokens=40,
            )
            data = json.loads(resp.choices[0].message.content)
            return data.get("pattern_name") or f"pattern_{str(uuid.uuid4())[:8]}"
        except Exception:
            return f"pattern_{str(uuid.uuid4())[:8]}"

    @staticmethod
    def _has_both_outcomes(pattern: BWEWisdomPattern) -> bool:
        dist = pattern.outcome_distribution or {}
        return dist.get("success", 0) > 0 and dist.get("failure", 0) > 0

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    @staticmethod
    def _update_centroid(current: list[float], new_vec: list[float], n: int) -> list[float]:
        """Инкрементальное обновление центроида (running mean)."""
        if n <= 1 or not current:
            return list(new_vec)
        return [current[i] * (n - 1) / n + new_vec[i] / n for i in range(len(current))]
