"""PersonaAggregator — заполняет Persona из существующих источников.

Phase A.1 MVP подход:
В коде уже работают 32 capture-агента (`backend/core/capture/agents/`),
которые на каждой встрече извлекают participants / decisions / opinions /
psychological_analyzer / communication_style и т.п. Но их выходы НЕ
сохраняются с user-attribution — они уходят в граф / extraction-results
и теряются по контексту «кто из участников какой».

MVP-стратегия (этот файл):
1. Берём recent meetings user'а через Supabase REST (то же что
   `tessent_brain_tools.get_recent_meetings` — без новых зависимостей).
2. Из каждого `meeting.transcription_text` + extraction-полей (`participants`,
   `analysis_summary`, `keywords`) выводим **поведенческие** метрики
   (avg_meetings_per_day, peak_hours_utc) — это работает сразу, не требует
   LLM.
3. Для **коммуникативно-когнитивной** подфункции — опционально вызываем
   `CommunicationStyleAgent` на топ-3 свежих встречах, если установлен
   `LLMRouter`. Иначе пропускаем (Persona всё равно полезна).
4. Подфункции `development` и `strengths_weaknesses` пока строятся
   эвристически из проектных тегов и keywords; полноценное извлечение
   — Phase A.2 (мы там переключим capture-агентов на persist через
   новую таблицу `persona_observations`).

Provenance каждого заполненного поля идёт через `record_provenance`.

API:
    aggregator = PersonaAggregator()
    persona = await aggregator.build_for_user(user_id, tenant_id=None,
                                              llm_router=optional)

Метод never-raise (best-effort). Если данных не хватает — возвращает
Persona с теми полями, которые удалось собрать.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from backend.core.persona.models import (
    AreaAssessment,
    ChannelPreference,
    Persona,
    PersonaProvenanceEntry,
    ProvenanceSource,
)
from backend.core.persona.store import get_persona_store

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Source 1: Supabase MeetFlow REST (тот же что для get_recent_meetings)
# ─────────────────────────────────────────────────────────────────


async def _fetch_user_meetings(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """Получить recent meetings из Supabase. Возвращает [] при недоступности."""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_KEY", "")
    if not (supabase_url and supabase_key and user_id):
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{supabase_url}/rest/v1/meetings",
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                },
                params={
                    "select": "id,title,created_at,user_timezone,audio_duration_seconds,metadata,transcription_text",
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                    "limit": str(limit),
                },
            )
            if resp.status_code != 200:
                logger.warning("persona: Supabase meetings %s", resp.status_code)
                return []
            return resp.json() or []
    except Exception as exc:
        logger.warning("persona: meetings fetch failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────
# Behavioral aggregation (без LLM)
# ─────────────────────────────────────────────────────────────────


def _aggregate_behavioral(persona: Persona, meetings: list[dict[str, Any]]) -> list[str]:
    """Заполняет behavioral подфункцию из метаданных встреч. Возвращает
    список field_path'ов которые были обновлены (для provenance)."""
    updated: list[str] = []
    if not meetings:
        return updated

    # avg_meetings_per_day — за последние 30 дней
    now = datetime.now(timezone.utc)
    days_seen: set[str] = set()
    hours_counter: Counter[int] = Counter()
    durations: list[float] = []
    for m in meetings:
        created = m.get("created_at")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except Exception:
            continue
        delta_days = (now - dt).days
        if delta_days < 0 or delta_days > 30:
            continue
        days_seen.add(dt.date().isoformat())
        hours_counter[dt.hour] += 1
        dur = m.get("audio_duration_seconds")
        if isinstance(dur, (int, float)) and dur > 0:
            durations.append(float(dur))

    if days_seen:
        avg = len(meetings) / max(len(days_seen), 1)
        persona.behavioral.avg_meetings_per_day = round(avg, 2)
        updated.append("behavioral.avg_meetings_per_day")

    if hours_counter:
        # peak — топ-3 часа по количеству встреч
        peaks = [h for h, _ in hours_counter.most_common(3)]
        persona.behavioral.peak_hours_utc = sorted(peaks)
        updated.append("behavioral.peak_hours_utc")

        # low_load — часы, которых в данных НЕТ (или один раз) в рабочее окно 8-22
        active = set(hours_counter.keys())
        low = [h for h in range(8, 22) if h not in active or hours_counter[h] <= 1]
        # Возвращаем максимум 4 окна — переусложнять не надо
        persona.behavioral.low_load_windows_utc = low[:4]
        updated.append("behavioral.low_load_windows_utc")

    return updated


# ─────────────────────────────────────────────────────────────────
# Development aggregation: проекты + темы из заголовков встреч
# ─────────────────────────────────────────────────────────────────


def _aggregate_development(persona: Persona, meetings: list[dict[str, Any]]) -> list[str]:
    """Извлекает recent_learning и active_goals из заголовков и keywords."""
    if not meetings:
        return []
    titles = [str(m.get("title") or "") for m in meetings if m.get("title")]
    if not titles:
        return []
    # Heuristic: топовые слова > 3 букв из заголовков как "recent topics"
    word_counter: Counter[str] = Counter()
    for t in titles:
        for w in t.lower().split():
            w = w.strip(".,;:!?()[]\"'")
            if len(w) > 3:
                word_counter[w] += 1
    top_topics = [w for w, c in word_counter.most_common(8) if c >= 2]
    if top_topics:
        persona.development.recent_learning = top_topics
        return ["development.recent_learning"]
    return []


# ─────────────────────────────────────────────────────────────────
# Communication (опционально, требует LLM)
# ─────────────────────────────────────────────────────────────────


async def _aggregate_cogni_from_observations(
    user_id: str,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """CogniLayer Ф1.3: свести cogni-наблюдения из встреч в один профиль.

    Читает persona_observations вида cogni_dimensions_agent (пишутся мостом
    core/cogni/observe.py с атрибуцией к аккаунту) и сливает их по уверенности;
    при равной уверенности побеждает более свежее наблюдение. Возвращает
    (cogni-dict | None, meeting_ids для provenance). Never-raise."""
    try:
        from backend.core.cogni.dimensions import merge_cogni
        from backend.core.cogni.observe import (
            AGENT_TYPE as _COGNI_AGENT,
            cogni_from_observation_payload,
        )
        from backend.core.persona.observations import list_observations
    except Exception:
        logger.debug("cogni observations imports skipped", exc_info=True)
        return None, []
    try:
        rows = await list_observations(
            user_id=user_id, agent_types=[_COGNI_AGENT], limit=30)
    except Exception:
        logger.debug("cogni observations read skipped", exc_info=True)
        return None, []
    if not rows:
        return None, []

    merged: Optional[dict[str, Any]] = None
    meeting_ids: list[str] = []
    # rows идут от свежих к старым; складываем от старых к свежим, чтобы при
    # равной уверенности (merge отдаёт ничьи incoming'у) побеждало новое
    for row in reversed(rows):
        cogni = cogni_from_observation_payload(
            row.get("payload") or {}, row.get("meeting_id"))
        merged = cogni if merged is None else merge_cogni(merged, cogni)
        if row.get("meeting_id"):
            meeting_ids.append(str(row["meeting_id"]))
    return merged, meeting_ids[-10:]


async def _aggregate_communication_from_observations(
    persona: Persona,
    user_id: str,
) -> list[str]:
    """Phase A.4: читает persisted observations из public.persona_observations.

    Capture-агенты пишут в эту таблицу при ингесте встречи (см.
    backend/core/persona/observations.py). Здесь мы агрегируем готовые
    результаты — БЕЗ realtime LLM-вызовов.

    Возвращает список обновлённых field_path'ов (для provenance).
    """
    from collections import Counter
    from backend.core.persona.observations import list_observations

    updated: list[str] = []
    observations = await list_observations(
        user_id=user_id,
        agent_types=["communication_style_agent"],
        limit=20,
    )
    if not observations:
        return updated

    # Agregate по 20 свежим observations: mode для speech_pace и jargon
    pace_counter: Counter[str] = Counter()
    jargon_counter: Counter[str] = Counter()
    used_mids: list[str] = []
    for obs in observations:
        payload = obs.get("payload") or {}
        if isinstance(payload, dict):
            pace = payload.get("speech_pace")
            jargon = payload.get("use_of_jargon")
            if pace:
                pace_counter[str(pace)] += 1
            if jargon:
                jargon_counter[str(jargon)] += 1
            if obs.get("meeting_id"):
                used_mids.append(obs["meeting_id"])

    if pace_counter:
        mode_pace, _ = pace_counter.most_common(1)[0]
        persona.communication_cognitive.speech_patterns.append({
            "field": "speech_pace",
            "value": mode_pace,
            "meeting_ids": used_mids[:10],
            "confidence": 0.85,  # выше чем realtime (больше observations)
        })
        updated.append("communication_cognitive.speech_patterns")

    if jargon_counter:
        mode_jargon, _ = jargon_counter.most_common(1)[0]
        register_map = {
            "heavy": "tech_business",
            "moderate": "tech_business_mix",
            "minimal": "casual",
        }
        persona.communication_cognitive.vocabulary_register = register_map.get(
            mode_jargon, "balanced",
        )
        updated.append("communication_cognitive.vocabulary_register")

    return updated


async def _aggregate_communication(
    persona: Persona,
    meetings: list[dict[str, Any]],
    llm_router: Any = None,
) -> list[str]:
    """Best-effort: дёргает CommunicationStyleAgent на топ-3 свежих встречах.
    Если LLM не задан — пропускает (Persona всё равно полезна без этого).

    Phase A.4: это fallback для legacy-сценария когда observations таблица
    пустая (нет meeting_pipeline или старые встречи без capture-ингеста).
    В норме `_aggregate_communication_from_observations` достаёт уже
    persisted-данные без LLM-вызовов.
    """
    if not (meetings and llm_router):
        return []
    try:
        from backend.core.capture.agents.communication_style_agent import (
            CommunicationStyleAgent,
        )
    except Exception:
        logger.debug("CommunicationStyleAgent unavailable", exc_info=True)
        return []

    # Берём 3 свежих встречи с непустым транскриптом и >300 символов
    candidates: list[dict[str, Any]] = []
    for m in meetings[:5]:
        tr = m.get("transcription_text") or ""
        if isinstance(tr, str) and len(tr) > 300:
            candidates.append(m)
        if len(candidates) >= 3:
            break
    if not candidates:
        return []

    # Конструктор и метод — по реальной сигнатуре агента: __init__(llm_router=…),
    # analyze(transcript, participants, meeting_context) → dict с ключом
    # communication_profiles. Раньше здесь звались несуществующие
    # llm_client=/extract() — TypeError вылетал ВНЕ try и ронял всю сборку
    # персоны, так что fallback-ветка не работала ни разу с момента написания.
    agent = CommunicationStyleAgent(llm_router=llm_router)
    aggregated_pace: list[str] = []
    aggregated_jargon: list[str] = []
    used_meeting_ids: list[str] = []
    for m in candidates:
        try:
            analysis = await agent.analyze(
                transcript=m["transcription_text"],
                participants=m.get("participants") or [],
                meeting_context={"meeting_id": m.get("id"), "title": m.get("title")},
            )
            profiles = (analysis or {}).get("communication_profiles") or []
        except Exception as exc:
            logger.debug("communication_style_agent failed: %s", exc)
            continue
        used_meeting_ids.append(str(m.get("id", "")))
        # profiles — list[CommunicationProfile] либо list[dict] (зависит от LLM)
        for p in (profiles or []):
            if isinstance(p, dict):
                pace = p.get("speech_pace")
                jargon = p.get("use_of_jargon")
            else:
                pace = getattr(p, "speech_pace", None)
                jargon = getattr(p, "use_of_jargon", None)
            if pace:
                aggregated_pace.append(str(pace))
            if jargon:
                aggregated_jargon.append(str(jargon))

    updated: list[str] = []
    if aggregated_pace:
        # mode (самый частый) — простая агрегация на 3 встречах
        most_common_pace, _ = Counter(aggregated_pace).most_common(1)[0]
        # speech_patterns как «свидетельство», без поля как такового
        persona.communication_cognitive.speech_patterns.append({
            "field": "speech_pace",
            "value": most_common_pace,
            "meeting_ids": used_meeting_ids,
            "confidence": 0.7,
        })
        updated.append("communication_cognitive.speech_patterns")
    if aggregated_jargon:
        # У нас в модели есть vocabulary_register — мапим heavy/moderate/minimal
        mode_jargon, _ = Counter(aggregated_jargon).most_common(1)[0]
        register_map = {
            "heavy": "tech_business",
            "moderate": "tech_business_mix",
            "minimal": "casual",
        }
        persona.communication_cognitive.vocabulary_register = register_map.get(
            mode_jargon, "balanced"
        )
        updated.append("communication_cognitive.vocabulary_register")

    return updated


# ─────────────────────────────────────────────────────────────────
# Top-level orchestrator
# ─────────────────────────────────────────────────────────────────


def _safe_source(raw: str) -> ProvenanceSource:
    """str source_type → ProvenanceSource enum, дефолт EXTERNAL."""
    try:
        return ProvenanceSource(raw)
    except Exception:
        return ProvenanceSource.EXTERNAL


class PersonaAggregator:
    """MVP-агрегатор. Phase A.1.

    Идемпотентен: повторный build_for_user перезатирает поля свежими данными
    (provenance тоже обновляется). Manual override'ы из UI (Phase A.2)
    приоритизируются — будем уважать ProvenanceSource.MANUAL и не
    перезаписывать.
    """

    async def build_for_user(
        self,
        user_id: str,
        tenant_id: Optional[str] = None,
        llm_router: Any = None,
        meetings_limit: int = 30,
    ) -> Persona:
        """Построить/обновить Persona для user_id.

        - Читает recent meetings через Supabase REST
        - Заполняет behavioral + development без LLM
        - Если llm_router задан — обогащает communication_cognitive
        - Сохраняет в PersonaStore + записывает provenance
        """
        store = get_persona_store()
        persona = await store.get(user_id) or Persona(user_id=user_id)

        meetings = await _fetch_user_meetings(user_id, limit=meetings_limit)
        persona.source_meetings_count = len(meetings)

        updated_paths: list[tuple[str, str, list[str]]] = []
        # (field_path, source_actor, meeting_ids_evidence)

        for path in _aggregate_behavioral(persona, meetings):
            updated_paths.append((path, "persona_aggregator_behavioral",
                                  [str(m.get("id")) for m in meetings[:10] if m.get("id")]))

        for path in _aggregate_development(persona, meetings):
            updated_paths.append((path, "persona_aggregator_development",
                                  [str(m.get("id")) for m in meetings[:10] if m.get("id")]))

        # Phase A.4: сначала пробуем persisted observations (faster, free)
        comm_paths = await _aggregate_communication_from_observations(persona, user_id)
        for path in comm_paths:
            updated_paths.append((path, "communication_style_agent[persisted]", []))

        # Fallback: если в observations пусто — realtime LLM-вызов (legacy)
        if not comm_paths:
            for path in await _aggregate_communication(persona, meetings, llm_router):
                updated_paths.append((path, "communication_style_agent[realtime]", []))

        # CogniLayer Ф1: пересчитать когнитивные измерения (extended["cogni"])
        # из источников и ЗАМЕНИТЬ слот. Раньше здесь был merge со старым
        # значением слота — это давало «храповик»: при равной уверенности
        # ничья уходила старому, а кап агента 0.7 делает равные уверенности
        # нормой → новые наблюдения никогда не заменяли старые, а удалённые
        # (напр. ошибочно атрибутированные) строки жили в персоне вечно.
        # Замена чинит оба случая; авторитетные значения не теряются — блок
        # идёт ДО reapply_external, который накатывает их поверх каждый билд.
        cogni_actor: Optional[str] = None
        cogni_meeting_ids: list[str] = []
        try:
            from backend.core.cogni.dimensions import (
                apply_to_persona, derive_from_persona, merge_cogni,
            )
            base = derive_from_persona(persona)
            obs_cogni, cogni_meeting_ids = await _aggregate_cogni_from_observations(
                user_id)
            if obs_cogni is not None:
                base = merge_cogni(base, obs_cogni)
                cogni_actor = "cogni_dimensions_agent[persisted]"
            else:
                cogni_actor = "cogni_derive"
            apply_to_persona(persona, base)
        except Exception:
            cogni_actor = None
            logger.debug("cogni aggregation skipped", exc_info=True)

        # Phase A.5: авторитетные значения (manual override + Mini Tess /me/self)
        # ПОБЕЖДАЮТ агрегацию из встреч — применяем последними. Это структурно
        # чинит старый баг «rebuild затирает manual override» (provenance
        # MANUAL раньше не уважался) и делает push профиля сотрудника stable.
        external_applied: list[tuple[str, str, str]] = []
        try:
            from backend.core.persona.external import reapply_external
            external_applied = await reapply_external(persona, user_id)
        except Exception:
            logger.debug("reapply_external skipped", exc_info=True)

        # Confidence — простая функция от количества meetings (плато 30+)
        persona.confidence = min(1.0, persona.source_meetings_count / 30.0)
        persona.updated_at = datetime.now(timezone.utc).isoformat()

        await store.upsert(persona, tenant_id=tenant_id)

        # extended.cogni перезаписан авторитетным значением → provenance
        # остаётся за external-записью (её пишет цикл ниже), нашу не пишем.
        if cogni_actor and not any(fp == "extended.cogni"
                                   for fp, _, _ in external_applied):
            try:
                await store.record_provenance(
                    user_id,
                    PersonaProvenanceEntry(
                        field_path="extended.cogni",
                        source_type=(ProvenanceSource.CAPTURE_AGENT
                                     if cogni_meeting_ids
                                     else ProvenanceSource.INFERRED),
                        agent_or_actor=cogni_actor,
                        meeting_ids=cogni_meeting_ids,
                        confidence=0.6 if cogni_meeting_ids else 0.5,
                    ),
                    tenant_id=tenant_id,
                )
            except Exception:
                logger.debug("cogni provenance skipped", exc_info=True)

        # Provenance — пишем по одной записи на каждое обновлённое поле.
        # Авторитетные поля исключаем из CAPTURE_AGENT-набора, чтобы их
        # provenance не «перебивался» агрегацией того же поля.
        external_paths = {fp for fp, _, _ in external_applied}
        for field_path, actor, meeting_ids in updated_paths:
            if field_path in external_paths:
                continue
            try:
                await store.record_provenance(
                    user_id,
                    PersonaProvenanceEntry(
                        field_path=field_path,
                        source_type=ProvenanceSource.CAPTURE_AGENT,
                        agent_or_actor=actor,
                        meeting_ids=meeting_ids,
                        confidence=0.7,  # MVP — заниженная, поднимем после A.2
                    ),
                    tenant_id=tenant_id,
                )
            except Exception:
                logger.debug("provenance record skipped for %s", field_path, exc_info=True)

        # Provenance авторитетных полей — с их реальным source_type.
        for field_path, source_type, actor in external_applied:
            try:
                await store.record_provenance(
                    user_id,
                    PersonaProvenanceEntry(
                        field_path=field_path,
                        source_type=_safe_source(source_type),
                        agent_or_actor=actor,
                        confidence=1.0,
                    ),
                    tenant_id=tenant_id,
                )
            except Exception:
                logger.debug("ext provenance skipped for %s", field_path, exc_info=True)

        return persona


_default_aggregator: Optional[PersonaAggregator] = None


def get_persona_aggregator() -> PersonaAggregator:
    global _default_aggregator
    if _default_aggregator is None:
        _default_aggregator = PersonaAggregator()
    return _default_aggregator


__all__ = ["PersonaAggregator", "get_persona_aggregator"]
