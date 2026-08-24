"""
Outcome Linker — Слой 2 BWE

Связывает Decision Events с реальными исходами.
Без него система накапливает решения, но не учится на них.

Исправления vs v1:
- Проверка дубликатов перед созданием исхода
- Поиск по ВСЕМ решениям пользователя (не только последние 50)
- Включает решения С исходами (повторное ретро может добавить детали)
- Правильный порог LLM-верификации (≥ 0.55 — LLM, ≥ 0.72 — сразу)
- UTC-aware datetime
"""
from __future__ import annotations

import json
import logging
import math
from typing import Optional

from openai import AsyncOpenAI
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.wisdom.bwe_models import BWEDecisionEvent, BWEOutcomeEvent

logger = logging.getLogger(__name__)

MATCHING_PROMPT = """Найди прошлое решение компании, которое соответствует упомянутому в ретроспективе.

УПОМЯНУТОЕ:
"{outcome_mention}"

ПРОШЛЫЕ РЕШЕНИЯ:
{decisions_list}

Верни JSON:
{{"matched_decision_index": 0, "confidence": 0.85, "reasoning": "..."}}

Если нет совпадения: {{"matched_decision_index": -1, "confidence": 0.0, "reasoning": "не найдено"}}

Порог: confidence >= 0.65 для принятия. Индекс 0-based."""


class OutcomeLinker:
    """
    Связывает решения с исходами.

    Стратегия матчинга (от быстрого к медленному):
    1. Если decision_event_id явный → напрямую создаём исход
    2. Если нет → cosine similarity по situation_embedding
       - sim >= 0.85 → принимаем без LLM (почти дословное совпадение)
       - sim >= 0.55 → LLM верификация топ-3 кандидатов
       - sim < 0.55 → не линкуем (ситуация слишком разная)
       Решения из той же встречи, что и исход, кандидатами не считаются:
       исход не может подтверждать решение из того же разговора.
    """

    # Порог прямого принятия поднят 0.72 → 0.85. На коротких русских
    # бизнес-фразах text-embedding-3-small даёт ≥0.72 для «нанять подрядчика
    # на фронт» против «нанять подрядчика на дизайн» — привязка исхода не к
    # тому решению искажает распределение навсегда и молча. 0.85 оставляет
    # прямой путь только почти-дословным совпадениям; всё остальное — через
    # LLM-подтверждение (дёшево: работает только на ретроспективах).
    DIRECT_THRESHOLD = 0.85    # выше → принимаем без LLM
    LLM_THRESHOLD = 0.55       # выше → LLM верифицирует
    MAX_CANDIDATES = 200       # максимум решений для поиска

    def __init__(self, client: AsyncOpenAI, chat_model: str, embedding_model: str):
        self.client = client
        self.chat_model = chat_model
        self.embedding_model = embedding_model

    async def link_outcome_to_decision(
        self,
        session: AsyncSession,
        user_id: str,
        outcome_data: dict,
        decision_event_id: Optional[str] = None,
    ) -> Optional[BWEOutcomeEvent]:
        """
        Создать OutcomeEvent и привязать к Decision Event.

        Проверяет дубликаты перед созданием.

        Returns:
            Созданный BWEOutcomeEvent или None
        """
        if not decision_event_id:
            decision_event_id = await self._find_matching_decision(
                session=session,
                user_id=user_id,
                decision_summary=outcome_data.get("decision_summary", ""),
                exclude_meeting_id=outcome_data.get("meeting_id"),
            )

        if not decision_event_id:
            logger.warning(
                f"[OutcomeLinker] No matching decision for: "
                f"'{outcome_data.get('decision_summary', '')[:60]}'"
            )
            return None

        # Решение обязано принадлежать тому, кто пишет исход.
        # decision_event_id приходит из тела запроса (POST /wisdom/link-outcome),
        # и без этой проверки можно было записать исход к ЧУЖОМУ решению и
        # выставить ему outcome_linked=True — то есть подмешать свой «опыт»
        # в чужую компанию. Автоматический путь (матчинг выше) и так ищет
        # только свои решения, страдал именно явный вызов.
        owner = await session.execute(
            select(BWEDecisionEvent.user_id).where(
                BWEDecisionEvent.id == decision_event_id)
        )
        owner_uid = owner.scalars().first()
        if owner_uid is None:
            logger.warning("[OutcomeLinker] decision %s not found",
                           str(decision_event_id)[:8])
            return None
        if str(owner_uid) != str(user_id):
            logger.warning(
                "[OutcomeLinker] отказ: решение %s принадлежит другому "
                "владельцу данных", str(decision_event_id)[:8])
            return None

        # Проверка дубликатов (тот же источник + тот же тип исхода)
        existing = await session.execute(
            select(BWEOutcomeEvent).where(
                BWEOutcomeEvent.user_id == user_id,
                BWEOutcomeEvent.decision_event_id == decision_event_id,
                BWEOutcomeEvent.outcome_type == outcome_data.get("outcome_type", "unknown"),
                BWEOutcomeEvent.source_type == outcome_data.get("source_type", "retrospective"),
            )
        )
        if existing.scalars().first():
            logger.debug(f"[OutcomeLinker] Outcome already exists for decision {str(decision_event_id)[:8]}")
            return None

        delay_days = outcome_data.get("delay_days")
        if not delay_days and outcome_data.get("delay_description"):
            delay_days = self._parse_delay_days(outcome_data["delay_description"])

        outcome = BWEOutcomeEvent(
            user_id=user_id,
            decision_event_id=decision_event_id,
            meeting_id=outcome_data.get("meeting_id"),
            outcome_type=outcome_data.get("outcome_type", "unknown"),
            outcome_description=outcome_data.get("outcome_description", ""),
            delay_days=delay_days,
            causal_factors=outcome_data.get("causal_factors", []),
            impact_magnitude=float(outcome_data.get("impact_magnitude", 0.5)),
            source_type=outcome_data.get("source_type", "retrospective"),
        )
        session.add(outcome)

        # Обновляем флаг outcome_linked и нужно пересчитать pattern analysis
        await session.execute(
            update(BWEDecisionEvent)
            .where(BWEDecisionEvent.id == decision_event_id)
            .values(outcome_linked=True)
        )

        await session.flush()
        logger.info(
            f"[OutcomeLinker] Linked '{outcome.outcome_type}' "
            f"to decision {str(decision_event_id)[:8]}"
        )
        return outcome

    async def _find_matching_decision(
        self,
        session: AsyncSession,
        user_id: str,
        decision_summary: str,
        exclude_meeting_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Найти Decision Event по косинусному сходству эмбеддингов.

        Поиск по ВСЕМ решениям пользователя с эмбеддингами.
        (Не ограничиваем outcome_linked=False — ретро может дать новый контекст)
        """
        if not decision_summary.strip():
            return None

        conds = [
            BWEDecisionEvent.user_id == user_id,
            BWEDecisionEvent.situation_embedding.isnot(None),
        ]
        # Исход не может подтверждать решение из ТОЙ ЖЕ встречи: «решили и
        # тут же рассказали, чем кончилось» — это одно обсуждение, а не
        # решение с проверенным исходом. До этого фильтра решения текущей
        # встречи (уже сброшенные flush'ем) были видны линкеру.
        if exclude_meeting_id:
            conds.append(or_(
                BWEDecisionEvent.meeting_id.is_(None),
                BWEDecisionEvent.meeting_id != exclude_meeting_id,
            ))
        result = await session.execute(
            select(BWEDecisionEvent)
            .where(*conds)
            .order_by(BWEDecisionEvent.created_at.desc())
            .limit(self.MAX_CANDIDATES)
        )
        decisions = result.scalars().all()

        if not decisions:
            return None

        query_emb = await self._embed(decision_summary)
        if not query_emb:
            return None

        scored = []
        for dec in decisions:
            emb = dec.situation_embedding
            if not emb:
                continue
            sim = self._cosine_similarity(query_emb, emb)
            scored.append((sim, dec))

        if not scored:
            return None

        scored.sort(reverse=True, key=lambda x: x[0])
        best_sim, best_dec = scored[0]

        # Прямое принятие при высоком сходстве
        if best_sim >= self.DIRECT_THRESHOLD:
            logger.debug(f"[OutcomeLinker] Direct match sim={best_sim:.3f} for '{decision_summary[:50]}'")
            return str(best_dec.id)

        # LLM-верификация при среднем сходстве
        if best_sim >= self.LLM_THRESHOLD:
            top = scored[:3]
            decisions_list = "\n".join([
                f"{i}. Ситуация: {dec.situation[:120]} | Решение: {dec.decision[:80]}"
                for i, (_, dec) in enumerate(top)
            ])
            idx = await self._llm_verify_match(
                outcome_mention=decision_summary,
                decisions_list=decisions_list,
            )
            if 0 <= idx < len(top):
                return str(top[idx][1].id)

        logger.debug(f"[OutcomeLinker] No match, best_sim={best_sim:.3f}")
        return None

    async def _llm_verify_match(self, outcome_mention: str, decisions_list: str) -> int:
        """LLM верификация. Возвращает 0-based индекс или -1."""
        prompt = MATCHING_PROMPT.format(
            outcome_mention=outcome_mention[:400],
            decisions_list=decisions_list,
        )
        try:
            resp = await self.client.chat.completions.create(
                model=self.chat_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=150,
            )
            data = json.loads(resp.choices[0].message.content)
            idx = int(data.get("matched_decision_index", -1))
            conf = float(data.get("confidence", 0.0))
            return idx if conf >= 0.65 else -1
        except Exception as e:
            logger.debug(f"[OutcomeLinker] LLM verification failed: {e}")
            return -1

    async def _embed(self, text: str) -> list[float]:
        try:
            resp = await self.client.embeddings.create(
                model=self.embedding_model,
                input=text[:2000],
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.debug(f"[OutcomeLinker] Embedding failed: {e}")
            return []

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _parse_delay_days(text: str) -> Optional[int]:
        t = text.lower()
        import re
        try:
            m = re.search(r"(\d+)\s*(?:день|дня|дней|дн)", t)
            if m:
                return int(m.group(1))
            m = re.search(r"(\d+)\s*(?:недел)", t)
            if m:
                return int(m.group(1)) * 7
            m = re.search(r"(\d+)\s*(?:месяц|мес)", t)
            if m:
                return int(m.group(1)) * 30
            m = re.search(r"(\d+)\s*(?:квартал)", t)
            if m:
                return int(m.group(1)) * 90
        except Exception:
            logger.debug("suppressed exception", exc_info=True)
        return None
