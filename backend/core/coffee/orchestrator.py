"""CoffeeOrchestrator — top-level entry для Coffee scenario.

Phase B Coffee scenario, шаг 4 (объединяющий).

API:
    orchestrator = get_coffee_orchestrator()
    result = await orchestrator.prepare_for_meeting(
        meeting_id="...",
        meeting_data={...},     # transcript + summary + tasks + decisions + participants
        deliver: bool = True,    # сразу слать или только сгенерировать
    )
    # result.artifacts: list[RolePipelineOutput]
    # result.deliveries: list[DeliveryResult]

Flow:
1. infer_roles → определить роль каждого участника
2. Для каждого участника:
   a. Загрузить его Persona (если есть)
   b. Получить pipeline для роли (DEFAULT_PIPELINE_FOR_ROLE)
   c. Сгенерировать артефакт
3. Если deliver=True:
   a. pick_channels → упорядоченный список каналов
   b. deliver_artifact на первый канал; если упал — следующий
4. Логируем + возвращаем CoffeeResult

SLA: target ≤5 минут на одну встречу с 3-5 участниками.
LLM-вызовов: ~1 на участника (если pipeline уровня RAG не нужен).

Best-effort: если pipeline одной роли упал — продолжаем с другими.
Никаких raise.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from backend.core.coffee.delivery import (
    DeliveryResult,
    deliver_artifact,
    pick_channels,
)
from backend.core.coffee.role_inference import Role, RoleAssignment, infer_roles
from backend.core.coffee.role_pipelines import (
    DesignPipeline,
    DevPipeline,
    FounderPipeline,
    HRPipeline,
    OtherPipeline,
    PMPipeline,
    RolePipeline,
    RolePipelineOutput,
    SalesPipeline,
)
from backend.core.coffee.storage import save_artifact, update_delivery_status

logger = logging.getLogger(__name__)


# Mapping role → pipeline class. Добавление новой роли (см. role_inference.Role)
# требует регистрации pipeline здесь.
DEFAULT_PIPELINE_FOR_ROLE: dict[Role, type[RolePipeline]] = {
    Role.FOUNDER: FounderPipeline,
    Role.SALES: SalesPipeline,
    Role.DEV: DevPipeline,
    Role.PM: PMPipeline,
    Role.DESIGN: DesignPipeline,   # Phase B.2: design-brief + figma-links
    Role.HR: HRPipeline,           # Phase B.2: HR-pack для interview/onboarding
    Role.OTHER: OtherPipeline,
}


@dataclass
class CoffeeResult:
    """Результат подготовки coffee scenario для одной встречи."""

    meeting_id: str
    artifacts: list[RolePipelineOutput] = field(default_factory=list)
    artifact_ids: list[Optional[str]] = field(default_factory=list)
    # ↑ index-parallel к artifacts: artifact_ids[i] это UUID из coffee_artifacts
    # (None если save_artifact упал). Phase B.2 — нужно для update_delivery_status
    # после доставки.
    deliveries: list[DeliveryResult] = field(default_factory=list)
    duration_ms: int = 0
    success_count: int = 0
    failure_count: int = 0
    pending_count: int = 0
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "duration_ms": self.duration_ms,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "pending_count": self.pending_count,
            "generated_at": self.generated_at,
            "artifacts": [
                {
                    "artifact_id": self.artifact_ids[i] if i < len(self.artifact_ids) else None,
                    "artifact_type": a.artifact_type,
                    "role": a.role.value if hasattr(a.role, "value") else str(a.role),
                    "participant_id": a.participant_id,
                    "participant_name": a.participant_name,
                    "title": a.title,
                    "content_markdown_preview": a.content_markdown[:500],
                    "recommended_channels": list(a.recommended_channels),
                    "recommended_executor": a.recommended_executor,
                    "success": a.success,
                    "generated_at": a.generated_at,
                    "generation_duration_ms": a.generation_duration_ms,
                }
                for i, a in enumerate(self.artifacts)
            ],
            "deliveries": [
                {
                    "success": d.success,
                    "channel": d.channel.value if hasattr(d.channel, "value") else str(d.channel),
                    "delivered_at": d.delivered_at,
                    "external_ref": d.external_ref,
                    "error_message": d.error_message,
                }
                for d in self.deliveries
            ],
        }


class CoffeeOrchestrator:
    """Top-level orchestrator. Singleton; держит mapping role→pipeline."""

    def __init__(
        self,
        pipeline_map: Optional[dict[Role, type[RolePipeline]]] = None,
    ) -> None:
        self.pipeline_map = pipeline_map or DEFAULT_PIPELINE_FOR_ROLE
        # Pipeline инстансы кэшируем (stateless по сути)
        self._cached_pipelines: dict[Role, RolePipeline] = {}

    def _get_pipeline(self, role: Role) -> RolePipeline:
        cls = self.pipeline_map.get(role, OtherPipeline)
        if cls not in [type(p) for p in self._cached_pipelines.values()]:
            self._cached_pipelines[role] = cls()
        return self._cached_pipelines.setdefault(role, cls())

    async def prepare_for_meeting(
        self,
        *,
        meeting_id: str,
        meeting_data: dict[str, Any],
        deliver: bool = True,
        llm_router: Any = None,
        only_user_ids: Optional[list[str]] = None,
    ) -> CoffeeResult:
        """Сгенерировать (и опционально доставить) coffee-артефакты для встречи.

        Args:
            meeting_id: для логов / результата
            meeting_data: {transcript?, summary?, participants[], tasks[],
                           decisions[], key_topics[], title}
            deliver: True — отправлять артефакты по каналам сразу;
                False — только генерация, доставку триггерит UI
            llm_router: если None — pipeline'ы используют fallback тексты
            only_user_ids: если задан — обрабатываем только указанных
                участников (для re-generation одного человека)
        """
        start = time.perf_counter()
        result = CoffeeResult(meeting_id=meeting_id)

        participants = meeting_data.get("participants") or []

        # Участник встречи → аккаунт сотрудника. Без этого шага доставка
        # уходила на случайный uuid4, который participant_agent генерирует
        # при отсутствии id, и артефакт садился в pending для ВСЕХ, кроме
        # владельца ингеста. Резолвим по подтверждённой сшивке «это я» или
        # точному совпадению имени в организации; не разрешилось — оставляем
        # как есть, но с внятной причиной (см. participant_resolve).
        org_id = str(meeting_data.get("org_id") or "").strip()
        if not org_id:
            try:
                from backend.core.ingest.membership import get_org_for_user
                owner = str(meeting_data.get("owner_user_id")
                            or meeting_data.get("user_id") or "").strip()
                org_id = str(get_org_for_user(owner) or "") if owner else ""
            except Exception:
                org_id = ""
        if org_id:
            from backend.core.coffee.participant_resolve import (
                resolve_participant_account,
                unresolved_reason,
            )
            enriched = []
            for p in participants:
                if not isinstance(p, dict):
                    continue
                if str(p.get("user_id") or "").strip():
                    enriched.append(p)
                    continue
                res = resolve_participant_account(p, org_id)
                p = dict(p)
                if res.get("user_id"):
                    p["user_id"] = res["user_id"]
                    p["_resolve_basis"] = res["basis"]
                else:
                    p["_resolve_basis"] = res["basis"]
                    p["_unresolved_reason"] = unresolved_reason(
                        res["basis"], str(p.get("name") or ""))
                enriched.append(p)
            participants = enriched

        if only_user_ids:
            wanted = set(only_user_ids)
            participants = [
                p for p in participants
                if isinstance(p, dict)
                and (str(p.get("user_id") or p.get("id") or "") in wanted)
            ]

        meeting_context = {
            "title": meeting_data.get("title"),
            "summary": meeting_data.get("summary"),
            "project_id": meeting_data.get("project_id"),
        }

        # Шаг 1: roles
        assignments = await infer_roles(
            meeting_id=meeting_id,
            participants=participants,
            meeting_context=meeting_context,
            llm_router=llm_router,
        )

        # Шаг 2: pipeline per participant — параллельно для скорости
        async def _process_one(a: RoleAssignment) -> Optional[RolePipelineOutput]:
            pipeline = self._get_pipeline(a.role)
            persona = await _load_persona_quietly(a.participant_id)
            t0 = time.perf_counter()
            try:
                out = await pipeline.generate(
                    role_assignment=a,
                    meeting_data=meeting_data,
                    persona=persona,
                    llm_router=llm_router,
                )
                out.generation_duration_ms = int((time.perf_counter() - t0) * 1000)
                return out
            except Exception as exc:
                logger.warning(
                    "[coffee] pipeline %s failed for participant %s: %s",
                    a.role.value, a.participant_id, exc,
                )
                return None

        outputs_raw = await asyncio.gather(
            *[_process_one(a) for a in assignments],
            return_exceptions=False,
        )
        artifacts = [o for o in outputs_raw if o is not None]
        result.artifacts = artifacts

        # Шаг 2.5: persistent storage (Phase B.2)
        # Сохраняем каждый артефакт в coffee_artifacts. save_artifact сам
        # помечает прошлые pending superseded. None в artifact_ids = БД
        # недоступна, продолжаем работать без persistence.
        artifact_ids: list[Optional[str]] = []
        meeting_user_id = None
        if isinstance(meeting_data, dict):
            meeting_user_id = meeting_data.get("user_id") or meeting_data.get("tenant_id")
        for output in artifacts:
            aid = await save_artifact(
                pipeline_output=output,
                meeting_id=meeting_id,
                user_id=output.participant_id,
                tenant_id=meeting_user_id,
            )
            artifact_ids.append(aid)
        result.artifact_ids = artifact_ids

        # Шаг 3: deliver (опционально) — gated through Reactive engine
        if deliver:
            # Phase L: prefetch'аем personas + gate decisions ПАРАЛЛЕЛЬНО.
            # Раньше gate каждого артефакта блокировал loop последовательно;
            # для multi-participant встречи (5+ юзеров) это 5x ускоряет.
            personas = await asyncio.gather(
                *[_load_persona_quietly(o.participant_id) for o in artifacts],
                return_exceptions=False,
            )
            gate_priorities = [_coffee_priority_for_artifact(o) for o in artifacts]
            gate_decisions = await _bulk_gate_quietly(
                [(o.participant_id, gate_priorities[i],
                  f"coffee_{o.artifact_type}", personas[i])
                 for i, o in enumerate(artifacts)]
            )

            for idx, output in enumerate(artifacts):
                persona = personas[idx]
                gate = gate_decisions[idx]
                gate_priority = gate_priorities[idx]
                aid = artifact_ids[idx] if idx < len(artifact_ids) else None

                if gate is not None and not gate.deliver_now:
                    # Парковка: артефакт уже в БД (coffee_artifacts), плюс
                    # пишем сигнал чтобы scheduler/UI знал про отложенную доставку.
                    await _enqueue_quietly(
                        user_id=output.participant_id,
                        signal_type=f"coffee_{output.artifact_type}",
                        title=output.title,
                        body_markdown=output.content_markdown,
                        payload={
                            "artifact_id": aid,
                            "recommended_channels": output.recommended_channels,
                            "recommended_executor": output.recommended_executor,
                            "meeting_id": meeting_id,
                        },
                        priority=gate_priority,
                        source_kind="coffee_artifact",
                        source_id=aid,
                        valid_until_minutes=12 * 60,  # 12 ч окно актуальности
                    )
                    result.pending_count += 1
                    continue

                # Phase H: reorder каналы по выученному engagement
                # (где юзер реально реагирует — туда первым). Best-effort.
                channel_engagement = await _channel_engagement_quietly(
                    user_id=output.participant_id,
                    signal_type=f"coffee_{output.artifact_type}",
                )
                channels = pick_channels(
                    pipeline_output=output, persona=persona,
                    channel_engagement=channel_engagement,
                )
                delivered = False
                last_error = ""
                for ch in channels:
                    if not ch or ch == "pending":
                        continue
                    dr = await deliver_artifact(
                        pipeline_output=output,
                        channel=ch,
                        user_id=output.participant_id,
                    )
                    result.deliveries.append(dr)
                    if dr.success:
                        delivered = True
                        result.success_count += 1
                        # Phase B.2: фиксируем delivered в БД
                        if aid:
                            await update_delivery_status(
                                artifact_id=aid,
                                delivered_channel=dr.channel.value if hasattr(dr.channel, "value") else str(dr.channel),
                                delivered_external_ref=dr.external_ref,
                                success=True,
                            )
                        break
                    last_error = dr.error_message or last_error
                if not delivered:
                    result.failure_count += 1
                    result.pending_count += 1
                    if aid:
                        await update_delivery_status(
                            artifact_id=aid,
                            success=False,
                            error_message=last_error or "all channels failed",
                        )
                    # Audit-фикс: артефакт пытался доставиться напрямую и упал
                    # на ВСЕХ каналах. Без этого enqueue он становится orphan'ом
                    # в status='pending' — drain не увидит, retry не случится.
                    # Кидаем в очередь чтобы reactive_drain_task его подобрал.
                    await _enqueue_quietly(
                        user_id=output.participant_id,
                        signal_type=f"coffee_{output.artifact_type}",
                        title=output.title,
                        body_markdown=output.content_markdown,
                        payload={
                            "artifact_id": aid,
                            "recommended_channels": output.recommended_channels,
                            "recommended_executor": output.recommended_executor,
                            "meeting_id": meeting_id,
                            "retry_after_direct_failure": True,
                            "last_error": last_error or "all channels failed",
                        },
                        priority=gate_priority,
                        source_kind="coffee_artifact",
                        source_id=aid,
                        valid_until_minutes=12 * 60,
                    )

        result.duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "[coffee] meeting=%s artifacts=%d success_delivers=%d pending=%d in %dms",
            meeting_id, len(artifacts), result.success_count,
            result.pending_count, result.duration_ms,
        )
        return result


async def _load_persona_quietly(user_id: str) -> Optional[Any]:
    """Best-effort Persona load. Возвращает None при любом сбое."""
    try:
        from backend.core.persona.store import get_persona_store
        return await get_persona_store().get(user_id)
    except Exception:
        return None


def _coffee_priority_for_artifact(output: RolePipelineOutput) -> int:
    """Маппинг artifact_type → priority для Reactive gate.

    - strategic_brief / hr_pack: 60 (важно, но не критично)
    - tz_document: 70 (часто нужен в ходе спринта)
    - jira_cards / email_draft: 55
    - design_brief / summary: 45 (можно отложить до low-load окна)
    """
    pri_map = {
        "strategic_brief": 60,
        "hr_pack": 60,
        "tz_document": 70,
        "jira_cards": 55,
        "email_draft": 55,
        "design_brief": 45,
        "summary": 45,
    }
    return pri_map.get(getattr(output, "artifact_type", ""), 50)


async def _bulk_gate_quietly(
    triples: list[tuple[str, int, str, Optional[Any]]],
) -> list[Optional[Any]]:
    """Phase L: best-effort bulk_gate. Возвращает list[Optional[GateDecision]]
    того же размера. None если Reactive layer недоступен.
    """
    if not triples:
        return []
    try:
        from backend.core.reactive.delivery_gate import GateRequest, bulk_gate
        reqs = [
            GateRequest(user_id=u, priority=p, signal_type=st, persona=pers)
            for (u, p, st, pers) in triples
        ]
        decisions = await bulk_gate(reqs)
        return list(decisions)
    except Exception as exc:
        logger.debug("[coffee] bulk_gate unavailable: %s", exc)
        return [None] * len(triples)


async def _enqueue_quietly(**kwargs: Any) -> Optional[str]:
    """Best-effort signal_queue.enqueue_signal."""
    try:
        from backend.core.reactive.signal_queue import enqueue_signal
        return await enqueue_signal(**kwargs)
    except Exception as exc:
        logger.debug("[coffee] enqueue_signal unavailable: %s", exc)
        return None


async def _channel_engagement_quietly(
    *, user_id: str, signal_type: str,
) -> Optional[dict[str, float]]:
    """Phase H: best-effort выученный channel-engagement. None при сбое."""
    try:
        from backend.core.reactive.calibration import get_channel_engagement
        eng = await get_channel_engagement(user_id, signal_type)
        return eng or None
    except Exception as exc:
        logger.debug("[coffee] channel engagement unavailable: %s", exc)
        return None


_default_orchestrator: Optional[CoffeeOrchestrator] = None


def get_coffee_orchestrator() -> CoffeeOrchestrator:
    global _default_orchestrator
    if _default_orchestrator is None:
        _default_orchestrator = CoffeeOrchestrator()
    return _default_orchestrator


async def prepare_coffee_for_meeting(
    *,
    meeting_id: str,
    meeting_data: dict[str, Any],
    deliver: bool = True,
    llm_router: Any = None,
) -> CoffeeResult:
    """Module-level helper для taskiq job'ов и API."""
    return await get_coffee_orchestrator().prepare_for_meeting(
        meeting_id=meeting_id,
        meeting_data=meeting_data,
        deliver=deliver,
        llm_router=llm_router,
    )


__all__ = [
    "CoffeeOrchestrator",
    "CoffeeResult",
    "get_coffee_orchestrator",
    "prepare_coffee_for_meeting",
]
