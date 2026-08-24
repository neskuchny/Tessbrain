"""
Wisdom Engine — Слой 5 BWE (главный фасад)

Wisdom Query Engine: получает новую ситуацию, навигирует пространство паттернов,
возвращает ответ с уверенностью — «как мудрец, а не поисковик».

Ключевые улучшения vs v1:
- Все модели берутся из settings (не захардкожены)
- Транзакционная безопасность (один session context)
- Идемпотентность по meeting_id (дедупликация)
- Sync Qdrant orphans при полной синхронизации
- Правильный threshold для поиска (из константы)
- Переиспользует get_qdrant() из db слоя
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from openai import AsyncOpenAI
from sqlalchemy import func, select, update

from backend.config import settings
from backend.core.wisdom.bwe_models import (
    BWEDecisionEvent,
    BWEOutcomeEvent,
    BWEWisdomPattern,
    DecisionEventRead,
    OutcomeEventRead,
    ProcessMeetingResult,
    SimilarPatternResult,
    WisdomQuery,
    WisdomResponse,
)
from backend.core.wisdom.decision_extractor import _VALID_TENSIONS, DecisionExtractor
from backend.core.wisdom.outcome_linker import OutcomeLinker
from backend.core.wisdom.pattern_encoder import PatternEncoder, decay_confidence_score
from backend.core.wisdom.wisdom_index import WisdomIndex, WisdomIndexUnavailable

logger = logging.getLogger(__name__)

# Module D: классификация ситуации в tension-типы (тот же словарь, что и у
# DecisionExtractor — оси запроса и решений общие). Phase 2 E: многогипотезность —
# просим primary + альтернативные переформулировки, берём моду (устойчивее
# одной классификации; порт alternative_formulations из query_interpreter).
TENSION_INFER_PROMPT = """Определи тип бизнес-напряжения в ситуации.

Ситуация: {situation}
Контекст: {context}

Рассмотри ситуацию с НЕСКОЛЬКИХ сторон (переформулируй её 2-3 раза) и для
каждой формулировки выбери тип из списка (или "other"):
{tensions}

Верни JSON:
{{"tension_type": "<основной_тип>", "alternatives": ["<тип_2>", "<тип_3>"]}}
— tension_type: самая точная формулировка; alternatives: типы для других
ракурсов (могут повторять основной)."""

WISDOM_RESPONSE_PROMPT = """Ты — корпоративный мудрец с доступом к истории решений компании.

Текущая ситуация: {situation}
Виток компании: {vitek}

Найденные похожие паттерны из истории:
{patterns_context}

Дай ответ: что из этого можно вывести? При каких условиях какие решения работали?
Укажи уровень уверенности честно. Если данных мало — скажи об этом.
Ответ: 3-5 предложений, конкретно, с числами случаев."""

MIN_SEARCH_SCORE = 0.50  # единая константа для поиска


class WisdomEngine:
    """
    Главный фасад BWE. Координирует все слои.
    """

    def __init__(
        self,
        openai_client: AsyncOpenAI,
        wisdom_index: WisdomIndex,
        session_factory,
        chat_model: str,
        embedding_model: str,
    ):
        self.client = openai_client
        self.index = wisdom_index
        self.session_factory = session_factory
        self.chat_model = chat_model
        self.embedding_model = embedding_model

        self.extractor = DecisionExtractor(
            client=openai_client,
            chat_model=chat_model,
            embedding_model=embedding_model,
        )
        self.linker = OutcomeLinker(
            client=openai_client,
            chat_model=chat_model,
            embedding_model=embedding_model,
        )
        self.encoder = PatternEncoder(
            client=openai_client,
            chat_model=chat_model,
            cluster_threshold=getattr(settings, "bwe_cluster_threshold", 0.72),
            crystal_enrichment=getattr(settings, "crystal_enrichment_enabled", False),
        )

    async def process_meeting(
        self,
        user_id: str,
        transcript: str,
        meeting_id: Optional[str] = None,
        meeting_source: Optional[str] = None,
        company_vitek: int = 1,
        market_context: str = "",
        is_retrospective: bool = False,
    ) -> ProcessMeetingResult:
        """
        Обработать транскрипт встречи.

        Идемпотентен: повторная обработка того же meeting_id пропускается.
        Транзакционен: всё в одной сессии.
        """
        decision_events_created: list[BWEDecisionEvent] = []
        outcome_events_created: list[BWEOutcomeEvent] = []
        patterns_updated = 0
        skipped_duplicates = 0

        async with self.session_factory() as session:
            # ── Дедупликация по meeting_id ────────────────────────────
            if meeting_id:
                existing_count = await session.scalar(
                    select(func.count(BWEDecisionEvent.id))
                    .where(
                        BWEDecisionEvent.user_id == user_id,
                        BWEDecisionEvent.meeting_id == meeting_id,
                    )
                )
                if existing_count and existing_count > 0:
                    logger.info(
                        f"[WisdomEngine] Meeting {meeting_id} already processed "
                        f"({existing_count} decisions exist)"
                    )
                    return ProcessMeetingResult(
                        meeting_id=meeting_id,
                        decisions_extracted=0,
                        outcomes_linked=0,
                        patterns_updated=0,
                        skipped_duplicates=existing_count,
                    )

            # ── Слой 1: Извлечение Decision Events ───────────────────
            raw_decisions = await self.extractor.extract_decisions(
                transcript=transcript,
                company_vitek=company_vitek,
                market_context=market_context,
            )

            # Батчевые эмбеддинги — один API-вызов для всех решений
            embeddings: list[list[float]] = []
            if raw_decisions:
                embeddings = await self.extractor.batch_generate_situation_embeddings(raw_decisions)

            for i, raw_dec in enumerate(raw_decisions):
                emb = embeddings[i] if i < len(embeddings) else []
                decision = BWEDecisionEvent(
                    user_id=user_id,
                    meeting_id=meeting_id,
                    meeting_source=meeting_source,
                    situation=raw_dec.get("situation", ""),
                    tension_type=raw_dec.get("tension_type"),
                    options_considered=raw_dec.get("options_considered") or [],
                    decision=raw_dec.get("decision", ""),
                    rationale=raw_dec.get("rationale"),
                    company_vitek=company_vitek,
                    market_context=raw_dec.get("market_context"),
                    decision_confidence=raw_dec.get("decision_confidence", "medium"),
                    epistemic_type=raw_dec.get("epistemic_type", "claimed"),
                    predicted_outcome=raw_dec.get("predicted_outcome", "uncertain"),
                    situation_embedding=emb if emb else None,
                )
                session.add(decision)
                decision_events_created.append(decision)

            await session.flush()

            # ── Слой 2: Исходы из ретроспективы ─────────────────────
            if is_retrospective:
                raw_outcomes = await self.extractor.extract_outcomes_from_retrospective(transcript)
                for raw_out in raw_outcomes:
                    raw_out["meeting_id"] = meeting_id
                    outcome = await self.linker.link_outcome_to_decision(
                        session=session,
                        user_id=user_id,
                        outcome_data=raw_out,
                    )
                    if outcome:
                        emb = await self.extractor.generate_outcome_embedding(
                            outcome_type=outcome.outcome_type,
                            outcome_description=outcome.outcome_description,
                            causal_factors=outcome.causal_factors or [],
                        )
                        outcome.outcome_embedding = emb if emb else None
                        outcome_events_created.append(outcome)

            await session.commit()

        # ── Слой 3: Инкрементальная кластеризация (отдельная сессия) ─
        if decision_events_created or (is_retrospective and outcome_events_created):
            async with self.session_factory() as pattern_session:
                try:
                    changed_patterns = await self.encoder.encode_incremental(
                        session=pattern_session,
                        user_id=user_id,
                    )
                    await pattern_session.commit()
                    patterns_updated = len(changed_patterns)

                    # Синхронизируем изменённые паттерны в Qdrant
                    for pattern in changed_patterns:
                        await self.index.index_pattern(pattern)
                except Exception as e:
                    logger.error(f"[WisdomEngine] Pattern clustering failed: {e}")
                    await pattern_session.rollback()

        logger.info(
            f"[WisdomEngine] Meeting processed: user={user_id} meeting={meeting_id} "
            f"decisions={len(decision_events_created)} outcomes={len(outcome_events_created)} "
            f"patterns={patterns_updated}"
        )

        return ProcessMeetingResult(
            meeting_id=meeting_id,
            decisions_extracted=len(decision_events_created),
            outcomes_linked=len(outcome_events_created),
            patterns_updated=patterns_updated,
            skipped_duplicates=skipped_duplicates,
            decision_events=[_to_decision_read(d) for d in decision_events_created],
            outcome_events=[_to_outcome_read(o) for o in outcome_events_created],
        )

    async def link_outcome(
        self,
        user_id: str,
        decision_event_id: str,
        outcome_type: str,
        outcome_description: str,
        causal_factors: Optional[list[str]] = None,
        delay_days: Optional[int] = None,
        impact_magnitude: float = 0.5,
        meeting_id: Optional[str] = None,
    ) -> Optional[OutcomeEventRead]:
        """Вручную привязать исход к решению и обновить паттерн."""
        async with self.session_factory() as session:
            outcome = await self.linker.link_outcome_to_decision(
                session=session,
                user_id=user_id,
                outcome_data={
                    "decision_event_id": decision_event_id,
                    "meeting_id": meeting_id,
                    "outcome_type": outcome_type,
                    "outcome_description": outcome_description,
                    "causal_factors": causal_factors or [],
                    "delay_days": delay_days,
                    "impact_magnitude": impact_magnitude,
                    "source_type": "retrospective",
                },
                decision_event_id=decision_event_id,
            )
            if not outcome:
                return None

            emb = await self.extractor.generate_outcome_embedding(
                outcome_type=outcome_type,
                outcome_description=outcome_description,
                causal_factors=causal_factors or [],
            )
            outcome.outcome_embedding = emb if emb else None
            await session.commit()
            result = _to_outcome_read(outcome)

        # Обновляем статистику паттерна.
        # encode_incremental одного мало: он выходит на пустом списке новых
        # некластеризованных решений, а тут типовой случай ровно такой —
        # решение приняли и кластеризовали давно, исход появился сейчас.
        # Поэтому после кластеризации ЯВНО пересчитываем распределение
        # исходов, иначе привязка исхода не доходит до паттерна вовсе.
        async with self.session_factory() as ps:
            try:
                changed = await self.encoder.encode_incremental(session=ps, user_id=user_id)
                refreshed = await self.encoder.refresh_pattern_stats(
                    session=ps, user_id=user_id)
                await ps.commit()
                by_id = {str(p.id): p for p in (list(changed) + list(refreshed))}
                for p in by_id.values():
                    await self.index.index_pattern(p)
            except Exception as e:
                logger.error(f"[WisdomEngine] Pattern update after link_outcome failed: {e}")

        return result

    async def query(self, wisdom_query: WisdomQuery) -> WisdomResponse:
        """
        Запрос к Wisdom Engine.

        Алгоритм:
        0. (Module D) Если crystal-обогащение включено и tension_type не задан
           явно — выводим его из текста ситуации, чтобы veto-gate работал.
        1. Кодируем ситуацию → вектор (учитывает tension_type)
        2. Ищем ближайшие паттерны в Qdrant (с payload filter)
        3. Загружаем детали из PostgreSQL
        4. Формируем wisdom narrative через LLM
        """
        from backend.config import settings as _settings
        crystal_on = getattr(_settings, "crystal_enrichment_enabled", False)

        # Module D (query understanding): tension_type как ось запроса. Выводим
        # ТОЛЬКО когда caller не дал осмысленного значения — большинство
        # вызовов (persona) знают axis из своего контекста и его не надо гадать.
        # company_vitek НЕ выводим из текста: это свойство компании, а не
        # одного предложения — оставляем как передал caller.
        if crystal_on and (not wisdom_query.tension_type or wisdom_query.tension_type == "other"):
            inferred = await self._infer_tension(
                wisdom_query.situation, wisdom_query.market_context or ""
            )
            if inferred and inferred != "other":
                wisdom_query = wisdom_query.model_copy(update={"tension_type": inferred})
                logger.info(
                    f"[WisdomEngine] Module D inferred tension_type='{inferred}' "
                    f"for user {wisdom_query.user_id}"
                )

        # Кодируем запрос с учётом tension_type
        query_emb = await self.extractor.generate_query_embedding(
            situation=wisdom_query.situation,
            tension_type=wisdom_query.tension_type or "",
            context=wisdom_query.market_context or "",
        )
        if not query_emb:
            return WisdomResponse(
                query_situation=wisdom_query.situation,
                similar_patterns=[],
                wisdom_narrative="Не удалось закодировать ситуацию. Проверьте подключение к OpenAI.",
                overall_confidence="low",
                total_cases_analyzed=0,
            )

        # Поиск в Wisdom Index (с Qdrant payload filter по user_id)
        try:
            raw_patterns = await self.index.search_similar_patterns(
                query_embedding=query_emb,
                user_id=wisdom_query.user_id,
                limit=wisdom_query.max_patterns,
                min_score=_settings.bwe_search_threshold,
            )
            # Второй канал — лексический (arXiv 2508.21038: одновекторный
            # поиск ограничен размерностью; BWE был единственным местом,
            # где канал один). Дополняет, не заменяет: векторные кандидаты
            # не трогаются и остаются первыми; сбой канала → просто нет
            # добавки. Выключатель WISDOM_LEXICAL_CHANNEL=off.
            try:
                import os as _os
                if _os.getenv("WISDOM_LEXICAL_CHANNEL", "on").strip().lower() in (
                        "1", "on", "true", "yes"):
                    raw_patterns = await self._augment_lexical(
                        raw_patterns, wisdom_query)
            except Exception:
                logger.debug("[WisdomEngine] lexical channel skipped",
                             exc_info=True)
        except WisdomIndexUnavailable as e:
            # «Не смогли посмотреть» ≠ «посмотрели, там пусто». Про архив,
            # к которому не было доступа, ничего утверждать нельзя.
            logger.error(f"[WisdomEngine] wisdom index unavailable: {e}")
            return WisdomResponse(
                query_situation=wisdom_query.situation,
                similar_patterns=[],
                wisdom_narrative=(
                    "Архив опыта компании сейчас недоступен — сказать, есть ли "
                    "похожие случаи, нельзя. Это сбой доступа к хранилищу, а не "
                    "отсутствие опыта."
                ),
                overall_confidence="low",
                total_cases_analyzed=0,
            )

        if not raw_patterns:
            return WisdomResponse(
                query_situation=wisdom_query.situation,
                similar_patterns=[],
                wisdom_narrative=(
                    "Ситуация нового типа — в архиве компании нет похожих паттернов. "
                    "Уверенность очень низкая. Рекомендую зафиксировать это решение в Tessbrain "
                    "— система начнёт накапливать паттерны для будущих похожих случаев."
                ),
                overall_confidence="low",
                total_cases_analyzed=0,
                recommendation="Зафиксируйте решение в Tessbrain для накопления паттернов.",
            )

        # Детали паттернов из PostgreSQL
        similar_patterns: list[SimilarPatternResult] = []
        total_cases = 0
        context_parts: list[str] = []
        # crystal_on вычислен в начале query(). Axis-veto активен только при нём;
        # OFF → фильтрации нет, поведение query() байт-в-байт прежнее.
        excluded_by_axis = 0
        # C (Phase 1): мета принятых кристаллов для типизации конфликтов
        # кристалл-vs-кристалл (mutual/temporal/granularity).
        crystal_meta: list[dict] = []

        async with self.session_factory() as session:
            for raw_p in raw_patterns:
                details = await self.index.get_pattern_details(
                    session=session,
                    pattern_uuid=raw_p["pattern_uuid"],
                    user_id=wisdom_query.user_id,
                )
                if not details:
                    continue

                # Veto-gate (Module C): кристалл сверяется с запросом по осям.
                # Hard-veto (исключение) — только однозначная фаза-вне-диапазона
                # и, опционально, not_law-срабатывание. Мягкие расхождения
                # (tension-mismatch, not_law без hard-флага) НЕ исключают, а
                # копятся в distinguishing_axes для прозрачности. OFF → gate
                # не вызывается, поведение прежнее.
                distinguishing_axes: list[str] = []
                if crystal_on:
                    excluded, distinguishing_axes = self._evaluate_veto(details, wisdom_query)
                    if excluded:
                        excluded_by_axis += 1
                        continue

                dist = details.get("outcome_distribution", {})
                n = details.get("total_cases", 0)
                total_cases += n

                # A (Phase 1): time-decay confidence_score на ЧТЕНИИ. Хранимое
                # значение остаётся свежим; наружу отдаём состаренное по
                # last_calibrated_at + contradiction-multiplier. OFF → не трогаем.
                effective_score = details.get("confidence_score")
                if crystal_on and effective_score is not None:
                    lc_raw = details.get("last_calibrated_at")
                    lc_dt = None
                    if lc_raw:
                        try:
                            lc_dt = datetime.fromisoformat(lc_raw)
                        except (ValueError, TypeError):
                            lc_dt = None
                    effective_score = decay_confidence_score(
                        effective_score, lc_dt, dist,
                        access_count=details.get("access_count", 0),
                    )

                similar_patterns.append(SimilarPatternResult(
                    pattern_id=details["id"],
                    pattern_name=details["pattern_name"],
                    similarity_score=round(raw_p["score"], 3),
                    outcome_distribution=dist,
                    confidence=details.get("confidence", "low"),
                    context_differentiators=details.get("context_differentiators"),
                    vitek_sensitivity=details.get("vitek_sensitivity"),
                    total_cases=n,
                    recent_decisions=details.get("recent_decisions", []),
                    distinguishing_axes=distinguishing_axes,
                    # Crystal-поля (аддитивно; None/[] для до-миграционных паттернов).
                    primary_tension=details.get("primary_tension"),
                    vitek_phase_min=details.get("vitek_phase_min"),
                    vitek_phase_max=details.get("vitek_phase_max"),
                    confidence_score=effective_score,
                    not_law=details.get("not_law") or [],
                    open_holes=details.get("open_holes") or [],
                ))

                if crystal_on:
                    crystal_meta.append({
                        "name": details["pattern_name"],
                        "dist": dist,
                        "cases": n,
                        "last_calibrated_at": details.get("last_calibrated_at"),
                        "open_holes": details.get("open_holes") or [],
                        "primary_tension": details.get("primary_tension"),
                    })

                success = dist.get("success", 0)
                failure = dist.get("failure", 0)
                diff_str = ""
                if details.get("context_differentiators"):
                    diff_str = "Факторы: " + "; ".join(details["context_differentiators"][:2])

                # Прозрачность Module C: если кристалл прошёл gate, но условия
                # запроса отличаются — даём LLM повод оговориться в нарративе.
                axes_str = ""
                if distinguishing_axes:
                    axes_str = "\n  ⚠️ Применять осторожно: " + "; ".join(distinguishing_axes[:3])

                # Уверенность для LLM — СОСТАРЕННАЯ, если затухание включено.
                # Раньше сюда шла хранимая строка, и «мудрец» видел свежую
                # уверенность у паттерна трёхлетней давности — затухание
                # работало только в JSON-поле, которое никто не читал.
                conf_str = details.get("confidence", "low")
                if effective_score is not None:
                    conf_str = f"{conf_str} (с учётом давности: {effective_score:.2f})"

                # Реальные примеры исходов — чтобы «вот чем это заканчивалось»
                # было содержанием, а не только счётчиками. До двух примеров.
                examples = ""
                recents = details.get("recent_decisions") or []
                ex_lines = []
                for r in recents[:2]:
                    if isinstance(r, dict):
                        d_txt = str(r.get("decision") or r.get("situation") or "")[:100]
                        o_txt = str(r.get("outcome") or r.get("outcome_type") or "")[:80]
                        if d_txt:
                            ex_lines.append(f"    – {d_txt}" + (f" → {o_txt}" if o_txt else ""))
                if ex_lines:
                    examples = "\n  Примеры:\n" + "\n".join(ex_lines)

                context_parts.append(
                    f"Паттерн '{details['pattern_name']}' (схожесть {raw_p['score']:.0%}):\n"
                    f"  {n} случаев | Успехов: {success} | Провалов: {failure}\n"
                    f"  {diff_str}\n"
                    f"  Уверенность: {conf_str}"
                    f"{axes_str}"
                    f"{examples}"
                )

        if crystal_on and excluded_by_axis:
            logger.info(
                f"[WisdomEngine] Axis-veto excluded {excluded_by_axis} pattern(s) "
                f"out of phase {wisdom_query.company_vitek} for user {wisdom_query.user_id}"
            )

        # B2 retrieval-as-learning: отмечаем выданные кристаллы (access_count++,
        # last_accessed_at) — это их сопротивление будущему decay. Best-effort:
        # сбой инкремента не влияет на ответ. coalesce — для до-миграционных NULL.
        if crystal_on and similar_patterns:
            try:
                ids = [p.pattern_id for p in similar_patterns]
                async with self.session_factory() as bump_session:
                    await bump_session.execute(
                        update(BWEWisdomPattern)
                        .where(BWEWisdomPattern.id.in_(ids))
                        .values(
                            access_count=func.coalesce(BWEWisdomPattern.access_count, 0) + 1,
                            last_accessed_at=datetime.now(timezone.utc),
                        )
                    )
                    await bump_session.commit()
            except Exception as e:
                logger.debug(f"[WisdomEngine] access_count bump failed: {e}")

        # C (Phase 1): типизация конфликтов кристалл-vs-кристалл. Среди топически
        # связанных результатов ищем пары с расходящимися исходами и объясняем
        # причину (mutual/temporal/granularity) — даём LLM повод не выдавать
        # ложно-уверенный ответ, когда история компании по теме расходится.
        if crystal_on and len(crystal_meta) > 1:
            conflicts = self._detect_crystal_conflicts(crystal_meta)
            if conflicts:
                context_parts.insert(0, "⚠️ История по этой теме расходится:\n  " + "\n  ".join(conflicts))
                logger.info(
                    f"[WisdomEngine] Detected {len(conflicts)} crystal conflict(s) "
                    f"for user {wisdom_query.user_id}"
                )

        # F (Phase 2): gap→VOI. Открытые дыры релевантных кристаллов сортируем
        # по ценности информации (voi = 1 + log1p(cases)·0.3) и показываем топ —
        # чтобы ответ честно подсказал, что стоит доуточнить в первую очередь.
        if crystal_on and crystal_meta:
            holes = self._prioritize_open_holes(crystal_meta)
            if holes:
                lines = [f"• {h['query']} (VOI {h['voi']})" for h in holes[:2]]
                context_parts.append("📭 Стоит доуточнить (по убыванию ценности):\n  " + "\n  ".join(lines))

        overall_confidence = self._compute_confidence(similar_patterns, total_cases)

        wisdom_text = await self._generate_wisdom_narrative(
            situation=wisdom_query.situation,
            vitek=wisdom_query.company_vitek,
            patterns_context="\n\n".join(context_parts),
        )

        return WisdomResponse(
            query_situation=wisdom_query.situation,
            similar_patterns=similar_patterns,
            wisdom_narrative=wisdom_text,
            overall_confidence=overall_confidence,
            total_cases_analyzed=total_cases,
        )

    async def _augment_lexical(self, raw_patterns: list,
                               wisdom_query: "WisdomQuery") -> list:
        """Дополнить векторную выдачу лексическими кандидатами из PG.

        Паттернов у пользователя немного (это кластеры, не события) —
        честное пересечение слов считается за миллисекунды. Кандидаты
        идут ПОСЛЕ векторных с channel="lexical"; уже найденное вектором
        не трогается. Никогда не raises — вызывающий обёрнут в try.
        """
        from sqlalchemy import select as _select

        from backend.core.wisdom.bwe_models import BWEWisdomPattern
        from backend.core.wisdom.wisdom_lexical import (
            merge_lexical_candidates,
        )
        async with self.session_factory() as session:
            rows = (await session.execute(
                _select(BWEWisdomPattern).where(
                    BWEWisdomPattern.user_id == wisdom_query.user_id)
            )).scalars().all()
        all_patterns = []
        for p in rows:
            diffs = p.context_differentiators or []
            text = " ".join([
                str(p.pattern_name or ""),
                str(p.vitek_sensitivity or ""),
                " ".join(str(d) for d in diffs if d),
            ])
            dist = p.outcome_distribution or {}
            all_patterns.append({
                "pattern_uuid": str(p.id),
                "pattern_name": p.pattern_name,
                "text": text,
                "confidence": p.confidence or "low",
                "outcome_distribution": dist,
                "total_cases": sum(int(v) for v in dist.values()
                                   if isinstance(v, (int, float))),
            })
        merged = merge_lexical_candidates(
            raw_patterns, all_patterns, wisdom_query.situation)
        added = len(merged) - len(raw_patterns)
        if added:
            logger.info("[WisdomEngine] lexical channel added %d candidate(s)",
                        added)
        return merged

    async def sync_patterns_from_existing_meetings(
        self,
        user_id: str,
        meeting_transcripts: list[dict],
        company_vitek: int = 1,
    ) -> dict:
        """Ретроспективная обработка существующих встреч."""
        total_decisions = 0
        total_outcomes = 0
        processed = 0

        for meeting in meeting_transcripts:
            transcript = meeting.get("text", "")
            if not transcript or len(transcript.strip()) < 80:
                continue
            result = await self.process_meeting(
                user_id=user_id,
                transcript=transcript,
                meeting_id=meeting.get("id"),
                meeting_source=meeting.get("source"),
                company_vitek=company_vitek,
                is_retrospective=meeting.get("is_retrospective", False),
            )
            total_decisions += result.decisions_extracted
            total_outcomes += result.outcomes_linked
            processed += 1

        # Финальная полная синхронизация Qdrant (удаляет orphans)
        async with self.session_factory() as session:
            await self.index.sync_all_patterns(session=session, user_id=user_id)

        return {
            "processed_meetings": processed,
            "total_decisions": total_decisions,
            "total_outcomes": total_outcomes,
        }

    async def _generate_wisdom_narrative(
        self,
        situation: str,
        vitek: int,
        patterns_context: str,
    ) -> str:
        """LLM генерация wisdom ответа."""
        prompt = WISDOM_RESPONSE_PROMPT.format(
            situation=situation[:400],
            vitek=vitek,
            patterns_context=patterns_context[:1800],
        )
        try:
            resp = await self.client.chat.completions.create(
                model=self.chat_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.25,
                max_tokens=350,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"[WisdomEngine] Narrative generation failed: {e}")
            n_patterns = len(patterns_context.split("Паттерн '")) - 1
            return f"Найдено {n_patterns} похожих паттернов. Детали доступны в полях similar_patterns."

    @staticmethod
    def _vitek_phase_excludes(details: dict, company_vitek: int) -> bool:
        """True, если кристалл валидирован только на диапазоне витков
        [vitek_phase_min, vitek_phase_max], а запрос вне него.

        Консервативно: исключаем ТОЛЬКО когда обе границы заданы. Кристаллы
        без диапазона (NULL — до миграции 250 или ещё не денормализованы)
        НИКОГДА не вето — back-compat и отсутствие ложных отсечений.
        """
        lo = details.get("vitek_phase_min")
        hi = details.get("vitek_phase_max")
        if lo is None or hi is None:
            return False
        return company_vitek < lo or company_vitek > hi

    # Стоп-слова для грубого матчинга not_law против ситуации (RU+EN).
    _NOT_LAW_STOPWORDS = frozenset({
        "паттерн", "применим", "неприменим", "работает", "ломается", "когда",
        "если", "при", "это", "как", "что", "для", "или", "без", "над", "под",
        "the", "and", "for", "not", "when", "with", "this", "that", "pattern",
    })

    # Длина префикса для грубого матча словоформ (рус. морфология:
    # «регуляторных»/«регуляторные» → общий корень «регул»).
    _NOT_LAW_PREFIX = 5

    @classmethod
    def _word_prefixes(cls, text: str) -> set:
        """Префиксы содержательных слов (len>4, не стоп-слова) — чтобы матч
        переживал склонения/спряжения без полноценного стеммера."""
        return {
            w[: cls._NOT_LAW_PREFIX]
            for w in re.findall(r"\w+", text.lower())
            if len(w) > 4 and w not in cls._NOT_LAW_STOPWORDS
        }

    @classmethod
    def _not_law_hits(cls, not_law: list, situation: str) -> list:
        """Грубо определить, какие границы not_law похоже задеты ситуацией.

        Эвристика (дёшево, детерминированно, БЕЗ LLM): сравниваем префиксы
        содержательных слов границы и ситуации — это ловит словоформы
        («регуляторных» vs «регуляторные»), которые точный матч пропускает.
        Сигнал-подсказка, поэтому по умолчанию НЕ hard-veto (см. _evaluate_veto):
        префиксный матч даёт recall ценой возможных ложных срабатываний.
        """
        if not not_law or not situation:
            return []
        sit_prefixes = cls._word_prefixes(situation)
        hits = []
        for rule in not_law:
            if not isinstance(rule, str):
                continue
            if cls._word_prefixes(rule) & sit_prefixes:
                hits.append(rule)
        return hits

    # Tier 3 G: self-monitoring режима здоровья (порт equilibrium_monitor.py).
    # amplitude = приток решений за окно / нормировку; пороги — как у Tessbrain.
    HEALTH_WINDOW_DAYS = 14
    HEALTH_NORMALIZATION = 30.0  # 30 решений за 14 дней = amplitude 1.0

    @classmethod
    def _classify_health_regime(cls, amplitude: float) -> str:
        """Режим здоровья корпуса знаний по амплитуде притока (пороги Tessbrain):
        frozen <0.1 (знания застыли), healthy 0.1–0.6, stressed 0.6–0.9,
        chaotic >0.9 (турбулентность → заморозить авто-выводы, нужен review)."""
        if amplitude < 0.1:
            return "frozen"
        if amplitude <= 0.6:
            return "healthy"
        if amplitude <= 0.9:
            return "stressed"
        return "chaotic"

    async def recompute_patterns(self, user_id: str) -> int:
        """Фоновое обслуживание кристаллов (ночной движок): кластеризовать
        pending-решения и обновить обогащение/оси/decay-якорь
        (encode_incremental с crystal-флагом), проиндексировать изменённые в
        Qdrant. Возвращает число изменённых паттернов.
        """
        indexed = 0
        async with self.session_factory() as session:
            changed = await self.encoder.encode_incremental(session=session, user_id=user_id)
            # Плюс пересчёт по исходам, привязанным к уже кластеризованным
            # решениям: без него ночной прогон при отсутствии новых решений
            # не обновлял распределение вообще (см. refresh_pattern_stats).
            refreshed = await self.encoder.refresh_pattern_stats(
                session=session, user_id=user_id)
            await session.commit()
            # Индексируем при ОТКРЫТОЙ сессии (после commit атрибуты ленивы).
            _seen = {str(p.id): p for p in (list(changed or []) + list(refreshed or []))}
            for p in _seen.values():
                try:
                    await self.index.index_pattern(p)
                    indexed += 1
                except Exception as e:
                    logger.debug(f"[WisdomEngine] index_pattern failed: {e}")
        return indexed

    async def build_wisdom_brief(
        self,
        user_id: str,
        situation: str,
        company_vitek: int = 1,
        market_context: str = "",
    ) -> list:
        """Единый «бриф опыта прошлых решений» для ТЗ, чата и др. поверхностей
        (DRY). Возвращает строки-инсайты: рекомендация + границы not_law +
        предупреждение об observer-drift. Ось tension выводит Module D.
        Любой сбой → [] (потребитель не падает).
        """
        try:
            if not situation or not situation.strip():
                return []
            wq = WisdomQuery(
                user_id=user_id,
                situation=situation[:1000],
                tension_type=None,
                company_vitek=int(company_vitek or 1),
                market_context=market_context or "",
                max_patterns=5,
            )
            resp = await self.query(wisdom_query=wq)

            brief: list = []
            # Блок «опыт компании» выдаётся ТОЛЬКО когда опыт есть.
            # Раньше здесь первым шло resp.recommendation, а оно заполняется
            # ровно в противоположном случае — когда паттернов не нашлось.
            # В результате при пустой базе в ТЗ и чат уходила строка
            # «Зафиксируйте решение в Tessbrain для накопления паттернов» под
            # заголовком «критерии из опыта прошлых решений»: рекламная
            # подпись, выданная за накопленный опыт компании.
            if not getattr(resp, "similar_patterns", None):
                return []
            if getattr(resp, "wisdom_narrative", None):
                brief.append(f"💡 ОПЫТ КОМПАНИИ: {resp.wisdom_narrative[:300]}")

            seen: set = set()
            for p in (getattr(resp, "similar_patterns", None) or [])[:5]:
                for nl in (getattr(p, "not_law", None) or [])[:2]:
                    if nl and nl not in seen:
                        seen.add(nl)
                        brief.append(f"⚠️ ГРАНИЦА (из прошлых решений): {nl}")

            try:
                drift = await self.compute_observer_drift(user_id=user_id, tension_type=wq.tension_type)
                if drift.get("evaluated", 0) >= 3 and drift.get("drift_index", 0.0) >= 0.3:
                    brief.append(
                        f"⚠️ КАЛИБРОВКА: по похожим решениям исходы исторически "
                        f"переоценивались (drift {drift['drift_index']}). Закладывай "
                        f"консервативные оценки."
                    )
            except Exception as e:
                logger.debug(f"[WisdomEngine] drift in brief skipped: {e}")
            return brief
        except Exception as e:
            logger.warning(f"[WisdomEngine] build_wisdom_brief failed: {e}")
            return []

    async def compute_intelligence_health(self, user_id: str) -> dict:
        """Tier 3 G (on-demand, без фонового движка): классифицировать режим
        здоровья знаний пользователя по притоку решений за окно. Возвращает
        режим + амплитуду + рекомендацию политики.
        """
        window_start = datetime.now(timezone.utc) - timedelta(days=self.HEALTH_WINDOW_DAYS)
        async with self.session_factory() as session:
            n = (await session.execute(
                select(func.count(BWEDecisionEvent.id)).where(
                    BWEDecisionEvent.user_id == user_id,
                    BWEDecisionEvent.created_at >= window_start,
                )
            )).scalar() or 0
        amplitude = round(min(1.0, n / self.HEALTH_NORMALIZATION), 4)
        regime = self._classify_health_regime(amplitude)
        rec = {
            "frozen": "Знания застывают: новых решений мало. Приоритизируй закрытие "
                      "open_holes (gap→VOI) и фиксируй свежие решения в Tessbrain.",
            "healthy": "Нормальный приток знаний — режим без особых мер.",
            "stressed": "Высокий приток: усиль контроль конфликтов кристаллов, "
                        "повышай строгость синтеза.",
            "chaotic": "Турбулентность: заморозь авто-выводы, нужен человеческий review.",
        }[regime]
        return {
            "regime": regime,
            "amplitude": amplitude,
            "decisions_in_window": int(n),
            "window_days": self.HEALTH_WINDOW_DAYS,
            "recommendation": rec,
        }

    @staticmethod
    def _observer_drift_report(pairs: list) -> dict:
        """D1 observer-effect: дрейф ожидание−факт. pairs: list[(predicted,
        actual)], predicted ∈ {success,failure,uncertain}, actual ∈
        {success,failure,partial,unknown}.

        Учитываются только пары с ЯВНЫМ ожиданием (success/failure) и
        ОДНОЗНАЧНЫМ фактом (success/failure); uncertain/partial/unknown
        пропускаются (нет утверждения / двусмысленно).

        drift_index ∈ [−1, 1] = (optimism − pessimism)/evaluated:
          + → систематическая ПЕРЕОЦЕНКА (встреча обещала успех, по факту провал)
              = сигнатура observer-effect (встречи как перформанс);
          − → системный пессимизм; 0 → калибровано/нет данных.
        """
        evaluated = optimism = pessimism = calibrated = 0
        for pred, actual in pairs:
            if pred not in ("success", "failure"):
                continue
            if actual == "success":
                a = "success"
            elif actual == "failure":
                a = "failure"
            else:
                continue  # partial/unknown — двусмысленно
            evaluated += 1
            if pred == a:
                calibrated += 1
            elif pred == "success":  # ожидали успех, получили провал
                optimism += 1
            else:  # ожидали провал, получили успех
                pessimism += 1
        if evaluated == 0:
            return {"evaluated": 0, "drift_index": 0.0, "optimism_rate": 0.0,
                    "pessimism_rate": 0.0, "calibration_rate": 0.0}
        return {
            "evaluated": evaluated,
            "drift_index": round((optimism - pessimism) / evaluated, 3),
            "optimism_rate": round(optimism / evaluated, 3),
            "pessimism_rate": round(pessimism / evaluated, 3),
            "calibration_rate": round(calibrated / evaluated, 3),
        }

    async def compute_observer_drift(self, user_id: str, tension_type: Optional[str] = None) -> dict:
        """D1: измерить observer-drift по решениям пользователя — расхождение
        между ожиданием на встрече (predicted_outcome) и фактом
        (BWEOutcomeEvent). Опционально по конкретной оси (tension_type).
        """
        async with self.session_factory() as session:
            q = select(BWEDecisionEvent).where(
                BWEDecisionEvent.user_id == user_id,
                BWEDecisionEvent.outcome_linked.is_(True),
            )
            if tension_type:
                q = q.where(BWEDecisionEvent.tension_type == tension_type)
            decisions = (await session.execute(q)).scalars().all()
            if not decisions:
                return self._observer_drift_report([])

            dec_ids = [d.id for d in decisions]
            outcomes = (await session.execute(
                select(BWEOutcomeEvent).where(BWEOutcomeEvent.decision_event_id.in_(dec_ids))
            )).scalars().all()

            # Лучший известный факт на решение (success > failure > partial).
            actual_by_dec: dict = {}
            prio = {"success": 3, "failure": 2, "partial": 1, "unknown": 0}
            for o in outcomes:
                k = str(o.decision_event_id)
                if prio.get(o.outcome_type, 0) > prio.get(actual_by_dec.get(k, "unknown"), 0):
                    actual_by_dec[k] = o.outcome_type

            pairs = [
                (getattr(d, "predicted_outcome", "uncertain") or "uncertain",
                 actual_by_dec.get(str(d.id), "unknown"))
                for d in decisions
            ]
            report = self._observer_drift_report(pairs)
            report["tension_type"] = tension_type
            return report

    @staticmethod
    def _prioritize_open_holes(crystal_meta: list) -> list:
        """Phase 2 F: собрать open_holes релевантных кристаллов и упорядочить по
        ценности информации. VOI = 1 + log1p(cases)·0.3 (формула gap_detector.py
        из Tessbrain; cases — число опорных решений = «downstream-вес»). Для каждой
        дыры — конкретный search-query. Дедуп по тексту дыры (берём макс. VOI).
        """
        import math

        best: dict = {}
        for c in crystal_meta:
            cases = c.get("cases", 0) or 0
            voi = round(1.0 + math.log1p(cases) * 0.3, 3)
            tension = c.get("primary_tension")
            for hole in (c.get("open_holes") or []):
                if not isinstance(hole, str) or not hole.strip():
                    continue
                key = hole.strip().lower()
                query = f"{hole.strip()}" + (f" (в контексте {tension})" if tension else "")
                if key not in best or voi > best[key]["voi"]:
                    best[key] = {"hole": hole.strip(), "voi": voi, "query": query}
        return sorted(best.values(), key=lambda h: h["voi"], reverse=True)

    @staticmethod
    def _aggregate_tension_hypotheses(primary: str, alternatives: list) -> str:
        """Phase 2 E: мода по [primary + alternatives] с приоритетом primary
        при равенстве. Невалидные/'other' отбрасываются; если валидных нет —
        'other'. Чистая функция → тестируется без LLM."""
        from collections import Counter

        def _clean(x):
            t = (x or "").lower().strip()
            return t if t in _VALID_TENSIONS and t != "other" else None

        votes = [t for t in ([_clean(primary)] + [_clean(a) for a in (alternatives or [])]) if t]
        if not votes:
            return "other"
        counts = Counter(votes)
        top = counts.most_common(1)[0][1]
        tied = [t for t, c in counts.items() if c == top]
        # tiebreak: primary, если он среди лидеров; иначе первый по счёту
        p = _clean(primary)
        return p if p in tied else tied[0]

    async def _infer_tension(self, situation: str, market_context: str = "") -> str:
        """Module D (Phase 2 E): вывести tension_type из текста ситуации —
        многогипотезно (primary + альтернативные ракурсы → мода). Один LLM-вызов,
        temperature=0. Любая ошибка/нет валидных → 'other' (no-op для veto).
        Вызывается ТОЛЬКО когда caller не задал tension_type и crystal включён.
        """
        if not situation:
            return "other"
        prompt = TENSION_INFER_PROMPT.format(
            situation=situation[:500],
            context=(market_context or "")[:200],
            tensions="\n".join(f"- {t}" for t in sorted(_VALID_TENSIONS)),
        )
        try:
            resp = await self.client.chat.completions.create(
                model=self.chat_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=64,
            )
            data = json.loads(resp.choices[0].message.content)
            return self._aggregate_tension_hypotheses(
                data.get("tension_type"), data.get("alternatives") or []
            )
        except Exception as e:
            logger.debug(f"[WisdomEngine] tension inference failed: {e}")
            return "other"

    @staticmethod
    def _outcome_lean(dist: dict) -> Optional[str]:
        """'success' / 'failure' / None — в какую сторону кренятся исходы
        кристалла. None, если исходов нет или баланс."""
        s = (dist or {}).get("success", 0)
        f = (dist or {}).get("failure", 0)
        if s > f:
            return "success"
        if f > s:
            return "failure"
        return None

    @classmethod
    def _classify_crystal_conflict(cls, a: dict, b: dict) -> Optional[str]:
        """Типизировать конфликт пары кристаллов (порт conflict_detector.py).

        Срабатывает только при ПРОТИВОПОЛОЖНОМ крене исходов (один success-,
        другой failure-leaning) — иначе это не конфликт, а сосуществование.
        Тип (детерминированно, без LLM):
          - temporal: один заметно новее (>90 дн.) → новый опыт пересматривает старый;
          - granularity: один охватывает ≥3× больше случаев → общий vs частный;
          - mutual: сопоставимы по свежести и объёму → история реально расколота.
        Возвращает строку-объяснение или None.
        """
        lean_a, lean_b = cls._outcome_lean(a["dist"]), cls._outcome_lean(b["dist"])
        if not lean_a or not lean_b or lean_a == lean_b:
            return None  # нет противоречия исходов

        na, nb = a["name"], b["name"]
        win = na if lean_a == "success" else nb  # кто «за успех»
        lose = nb if win == na else na

        # temporal: разница свежести калибровки > 90 дней
        da, db = cls._parse_dt(a.get("last_calibrated_at")), cls._parse_dt(b.get("last_calibrated_at"))
        if da and db:
            gap_days = abs((da - db).total_seconds()) / 86400.0
            if gap_days > 90:
                newer = na if da > db else nb
                older = nb if newer == na else na
                return f"temporal: '{newer}' (новее) пересматривает '{older}' — опыт изменился"

        # granularity: один охватывает ≥3× больше случаев
        ca, cb = a.get("cases", 0), b.get("cases", 0)
        if ca and cb and (ca >= 3 * cb or cb >= 3 * ca):
            general = na if ca > cb else nb
            specific = nb if general == na else na
            return f"granularity: '{general}' общий, '{specific}' — частный случай, не противоречие"

        # mutual: сопоставимы → история реально расколота
        return f"mutual: '{win}' давал успех, '{lose}' — провал в похожих условиях"

    @classmethod
    def _detect_crystal_conflicts(cls, meta: list) -> list:
        """Попарно типизировать конфликты среди принятых кристаллов (O(k²),
        k обычно ≤5). Возвращает список строк-объяснений."""
        out: list = []
        for i in range(len(meta)):
            for j in range(i + 1, len(meta)):
                note = cls._classify_crystal_conflict(meta[i], meta[j])
                if note:
                    out.append(note)
        return out

    @staticmethod
    def _parse_dt(raw):
        """ISO-строка → datetime (или None)."""
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None

    @classmethod
    def _evaluate_veto(cls, details: dict, wisdom_query) -> tuple:
        """Veto-gate Module C: сверяет кристалл с запросом по осям.

        Возвращает (excluded, distinguishing_axes):
          - excluded — hard-veto: фаза-вне-диапазона (всегда) или
            not_law-срабатывание (только при флаге crystal_notlaw_hard_veto).
          - distinguishing_axes — человекочитаемые расхождения для прозрачности
            (попадают в ответ и нарратив), НЕ обязательно ведут к исключению.
        """
        from backend.config import settings as _settings

        axes: list = []
        excluded = False

        # 1. Фаза компании — единственный безусловный hard-veto (однозначен).
        if cls._vitek_phase_excludes(details, wisdom_query.company_vitek):
            lo, hi = details.get("vitek_phase_min"), details.get("vitek_phase_max")
            axes.append(f"фаза {wisdom_query.company_vitek} вне диапазона кристалла [{lo}–{hi}]")
            excluded = True

        # 2. Расхождение tension — мягкий сигнал (эмбеддинг уже учитывает
        #    tension_type, жёсткое отсечение было бы слишком агрессивным).
        q_tension = getattr(wisdom_query, "tension_type", None)
        p_tension = details.get("primary_tension")
        if q_tension and p_tension and q_tension != p_tension:
            axes.append(f"tension расходится: запрос '{q_tension}' vs кристалл '{p_tension}'")

        # 3. not_law-срабатывание — сигнал; hard-veto только за opt-in флагом.
        hits = cls._not_law_hits(details.get("not_law") or [], getattr(wisdom_query, "situation", ""))
        for h in hits:
            axes.append(f"сработало ограничение not_law: {h}")
        if hits and getattr(_settings, "crystal_notlaw_hard_veto", False):
            excluded = True

        return excluded, axes

    @staticmethod
    def _compute_confidence(
        patterns: list[SimilarPatternResult],
        total_cases: int,
    ) -> str:
        if not patterns or total_cases == 0:
            return "low"
        top_score = patterns[0].similarity_score
        if top_score >= 0.80 and total_cases >= 10:
            return "high"
        if top_score >= 0.65 and total_cases >= 3:
            return "medium"
        return "low"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _to_decision_read(d: BWEDecisionEvent) -> DecisionEventRead:
    return DecisionEventRead(
        id=str(d.id),
        user_id=d.user_id,
        meeting_id=d.meeting_id,
        meeting_source=d.meeting_source,
        situation=d.situation,
        tension_type=d.tension_type,
        options_considered=d.options_considered,
        decision=d.decision,
        rationale=d.rationale,
        company_vitek=d.company_vitek,
        market_context=d.market_context,
        decision_confidence=d.decision_confidence,
        pattern_id=d.pattern_id,
        outcome_linked=d.outcome_linked,
        created_at=d.created_at,
    )


def _to_outcome_read(o: BWEOutcomeEvent) -> OutcomeEventRead:
    return OutcomeEventRead(
        id=str(o.id),
        user_id=o.user_id,
        decision_event_id=str(o.decision_event_id),
        meeting_id=o.meeting_id,
        outcome_type=o.outcome_type,
        outcome_description=o.outcome_description,
        delay_days=o.delay_days,
        causal_factors=o.causal_factors,
        impact_magnitude=o.impact_magnitude,
        source_type=o.source_type,
        created_at=o.created_at,
    )


# ─────────────────────────────────────────────
# Singleton Factory
# ─────────────────────────────────────────────

_wisdom_engine: Optional[WisdomEngine] = None


async def get_wisdom_engine() -> WisdomEngine:
    """Получить singleton WisdomEngine, используя настройки из settings."""
    global _wisdom_engine
    if _wisdom_engine is None:
        from qdrant_client import QdrantClient

        from backend.config import settings
        from backend.db.postgres import Base, get_postgres

        # BWE использует собственную модель (bwe_chat_model) — по умолчанию gpt-4o-mini
        # Намного дешевле gpt-4, хорошо справляется со структурированным extraction
        chat_model = settings.bwe_chat_model
        embedding_model = settings.embedding_model

        # Enterprise-переносимость: base_url переопределяем (OPENAI_BASE_URL /
        # openai_base_url) — BWE больше не прибит к api.openai.com и может
        # работать через локальный vLLM/прокси в закрытом контуре.
        _bwe_kwargs = {"api_key": settings.openai_api_key}
        _base = getattr(settings, "openai_base_url", "") or __import__("os").getenv("OPENAI_BASE_URL", "")
        if _base:
            _bwe_kwargs["base_url"] = _base
        openai_client = AsyncOpenAI(**_bwe_kwargs)

        qdrant_client = QdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            api_key=settings.qdrant_api_key or None,
            https=settings.qdrant_https,
            timeout=30,
        )

        wisdom_index = WisdomIndex(
            qdrant_client=qdrant_client,
            embedding_dim=settings.embedding_dimension,
        )

        postgres = await get_postgres()

        # Регистрируем ORM-модели BWE и создаём таблицы
        from backend.core.wisdom import bwe_models as _bwe_models_module
        async with postgres.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        _wisdom_engine = WisdomEngine(
            openai_client=openai_client,
            wisdom_index=wisdom_index,
            session_factory=postgres.async_session,
            chat_model=chat_model,
            embedding_model=embedding_model,
        )

        # Создаём Qdrant коллекцию при старте
        await wisdom_index.ensure_collection()

        logger.info(
            f"[WisdomEngine] Initialized: chat={chat_model} emb={embedding_model}"
        )

    return _wisdom_engine
