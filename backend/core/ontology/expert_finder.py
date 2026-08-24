# -*- coding: utf-8 -*-
"""
Expert finding (перенос идеи Glean, docs/GLEAN_ADOPTION.md):
«документа/данных нет → вот кто в компании об этом знает».

Детерминированно и честно (без LLM): эксперт предлагается только по
РЕАЛЬНЫМ сигналам — (а) человек связан с темой в графе знаний,
(б) человек упоминается в тех же встречах/документах, что и тема
(co-occurrence по source_id в cross_source_mentions). Нет сигналов →
пустой список, никого не выдумываем.

Чистая функция find_experts тестируема без сторов; прод-обёртка
experts_for_user собирает входы по tenant_paths (изоляция per-user).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_TOP_K = 3


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def topic_tokens(topic: str) -> Set[str]:
    """Значимые токены темы (>3 символов, без стоп-частей вопроса)."""
    stop = {"какой", "какая", "какие", "сколько", "почему", "когда",
            "знает", "проект", "проекта", "проекту", "расскажи", "есть"}
    return {t for t in re.split(r"[^\wа-яё]+", _norm(topic))
            if len(t) > 3 and t not in stop}


def find_experts(topic: str, *,
                 persons: Set[str],
                 mention_records: Optional[List[dict]] = None,
                 graph_links: Optional[List[Tuple[str, str]]] = None,
                 person_roles: Optional[Dict[str, str]] = None
                 ) -> List[dict]:
    """Топ-эксперты по теме из двух детерминированных сигналов.

    persons         — известные люди компании (из графа);
    mention_records — [{entity, source_id}] все упоминания;
    graph_links     — [(person, related_node_name)] рёбра человек↔узел.

    Скоринг: ребро графа с узлом темы = 2, совместное упоминание в одном
    источнике = 1 за источник. Возвращает [{name, score, evidence:[...]}]."""
    tokens = topic_tokens(topic)
    if not tokens or not persons:
        return []
    persons_norm = {_norm(p): p for p in persons}

    def is_topic(text: Any) -> bool:
        # русская морфология: «миграции» должна находить «миграция БД» —
        # сравниваем стем-префиксы слов (длина-2, минимум 4 символа)
        low = _norm(text)
        if not low:
            return False
        words = re.split(r"[^\wа-яё]+", low)
        for t in tokens:
            for w in words:
                if len(w) <= 3:
                    continue
                # общий префикс покрывает корень: «маркетинг»↔«маркетолог»
                # (общие 6), «миграции»↔«миграция»; «проект»↔«проблема»
                # (общие 3) — нет
                common = 0
                for a, b in zip(t, w):
                    if a != b:
                        break
                    common += 1
                if common >= max(4, min(len(t), len(w)) - 4):
                    return True
        return False

    score: Dict[str, float] = {}
    evidence: Dict[str, List[str]] = {}

    # сигнал 1: связи в графе знаний (человек ↔ узел темы)
    for person, related in graph_links or []:
        pkey = persons_norm.get(_norm(person))
        if pkey and is_topic(related) and not is_topic(person):
            score[pkey] = score.get(pkey, 0) + 2
            evidence.setdefault(pkey, []).append(
                f"связан с «{related}» в графе знаний")

    # сигнал 2: co-occurrence в источниках (одна встреча/документ)
    if mention_records:
        topic_sources = {m.get("source_id") for m in mention_records
                         if is_topic(m.get("entity"))}
        topic_sources.discard(None)
        if topic_sources:
            seen: Dict[str, Set[str]] = {}
            for m in mention_records:
                pkey = persons_norm.get(_norm(m.get("entity")))
                src = m.get("source_id")
                if pkey and src in topic_sources and not is_topic(pkey):
                    seen.setdefault(pkey, set()).add(src)
            for pkey, sources in seen.items():
                score[pkey] = score.get(pkey, 0) + len(sources)
                evidence.setdefault(pkey, []).append(
                    f"упоминается вместе с темой в {len(sources)} "
                    f"источник(ах)")

    # сигнал 3: РОЛЬ человека совпадает с темой («маркетинг» → маркетолог).
    # Ролевое ранжирование живёт ТОЛЬКО здесь — общая выдача не трогается.
    for pname, role in (person_roles or {}).items():
        pkey = persons_norm.get(_norm(pname))
        if pkey and role and is_topic(role):
            score[pkey] = score.get(pkey, 0) + 2
            evidence.setdefault(pkey, []).append(
                f"роль «{role}» совпадает с темой")

    ranked = sorted(score.items(), key=lambda kv: -kv[1])[:_TOP_K]
    out = []
    for name, sc in ranked:
        if sc <= 0:
            continue
        item = {"name": name, "score": sc,
                "evidence": evidence.get(name, [])}
        role = (person_roles or {}).get(name)
        if role:
            item["role"] = role
        out.append(item)
    return out


async def experts_for_user(user_id: str, topic: str) -> dict:
    """Прод-сборка: люди и связи из графа + упоминания из
    cross_source_mentions. Любой источник best-effort."""
    persons: Set[str] = set()
    person_roles: Dict[str, str] = {}
    graph_links: List[Tuple[str, str]] = []
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        gb = GraphBuilder(use_networkx=None,
                          graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        if gb.connected and getattr(gb, "driver", None):
            # Neo4j: раньше код читал ТОЛЬКО gb.nx_graph (None на Neo4j) →
            # persons пусто → «сигналов нет — честно» ВСЕГДА (та же бага,
            # что была в Объект 360). Один Cypher: люди + их соседи.
            async with gb.driver.session() as session:
                res = await session.run(
                    """
                    MATCH (p:Person)
                    WHERE p.tenant_id = $t OR p.user_id = $t
                    OPTIONAL MATCH (p)--(n)
                    RETURN p.name AS person, p.role AS role,
                           collect(DISTINCT coalesce(n.name, n.title, n.text))[..40] AS nbs
                    LIMIT 1500
                    """,
                    {"t": user_id},
                )
                async for rec in res:
                    nm = str(rec.get("person") or "").strip()
                    if not nm:
                        continue
                    persons.add(nm)
                    if rec.get("role"):
                        person_roles[nm] = str(rec["role"])
                    for nb in (rec.get("nbs") or []):
                        if nb:
                            graph_links.append((nm, str(nb)))
        elif gb.connected and gb.nx_graph:
            g = gb.nx_graph
            node_name = {}
            for nid, d in g.nodes(data=True):
                nm = d.get("name") or d.get("title") or str(nid)
                node_name[nid] = nm
                if _norm(d.get("_label") or d.get("label")) == "person":
                    persons.add(str(nm))
                    role = d.get("role") or d.get("position") or d.get("job_title")
                    if role:
                        person_roles[str(nm)] = str(role)
            for nid, d in g.nodes(data=True):
                if str(node_name.get(nid)) in persons:
                    for nb in g.neighbors(nid):
                        graph_links.append(
                            (str(node_name[nid]), str(node_name.get(nb, nb))))
    except Exception as e:
        logger.debug(f"experts graph unavailable: {e}")

    mention_records: List[dict] = []
    try:
        from backend.core.memory.cross_source_mentions import (
            CrossSourceMentions,
        )
        from backend.core.store.tenant_paths import (
            cross_source_mentions_path_for_user,
        )
        store = CrossSourceMentions(
            persist_path=cross_source_mentions_path_for_user(user_id))
        for m in store.mentions():
            d = m.to_dict() if hasattr(m, "to_dict") else dict(m)
            mention_records.append({"entity": d.get("entity"),
                                    "source_id": d.get("source_id")})
            # имена людей встречаются и в mentions без графа
    except Exception as e:
        logger.debug(f"experts mentions unavailable: {e}")

    # если граф пуст, люди могут быть известны только из mentions — но
    # тогда отличить человека от проекта нечем; честно требуем граф
    experts = find_experts(topic, persons=persons,
                           mention_records=mention_records,
                           graph_links=graph_links,
                           person_roles=person_roles)
    return {"topic": topic, "experts": experts,
            "honest_note": ("эксперты найдены по реальным связям и "
                            "совместным упоминаниям; никого не выдумано"
                            if experts else
                            "сигналов о знатоках темы в памяти компании "
                            "нет — честно")}


# ──────────────────────────────────────────────────────────────────────
# Роутинг чата: «у кого узнать про X?» → живые эксперты вместо синтеза
# (Glean-перенос №2 в безопасной форме: ролевое ранжирование включается
# ТОЛЬКО на кто-вопросах, общая выдача не трогается вообще)
# ──────────────────────────────────────────────────────────────────────

_WHO_PATTERNS = (
    r"кто\s+(знает|разбирается|занимается|отвечает|шарит|в\s+курсе|видел|вёл|ведет|ведёт)",
    r"у\s+кого\s+(узнать|спросить|уточнить)",
    r"кого\s+(спросить|подключить)",
    r"кому\s+(написать|задать)",
    r"кто\s+эксперт",
    r"who\s+(knows|owns|to\s+ask)",
)


def is_who_question(text: str) -> Optional[str]:
    """Детерминированный детект «кто/у кого»-вопроса. Возвращает тему
    (хвост после паттерна) или None — чат не хайджекается без уверенности."""
    low = _norm(text)
    for pat in _WHO_PATTERNS:
        m = re.search(pat, low)
        if m:
            topic = re.sub(r"^[\s?.!,]*(про|по|о|об|в|на|за)\s+", "",
                           low[m.end():].strip(" ?.!,"))
            # тема может стоять и до паттерна («по миграции кто шарит?»)
            if len(topic_tokens(topic)) == 0:
                topic = low[:m.start()].strip(" ?.!,")
            return topic if topic_tokens(topic) else None
    return None


async def try_expert_route(user_id: str, question: str) -> Optional[dict]:
    """Хук для чата: кто-вопрос → эксперты с ролями и доказательствами.
    Не кто-вопрос / экспертов нет → None (обычный путь чата)."""
    topic = is_who_question(question)
    if not topic:
        return None
    res = await experts_for_user(user_id, topic)
    experts = res.get("experts") or []
    if not experts:
        return None        # честно молчим — пусть отвечает обычный поиск
    lines = [f"По теме «{topic}» в компании знают:"]
    for e in experts:
        role = f" ({e['role']})" if e.get("role") else ""
        lines.append(f"• {e['name']}{role} — " + "; ".join(e["evidence"]))
    lines.append("")
    lines.append("Подобрано по реальным связям графа, совместным "
                 "упоминаниям и ролям — никого не выдумано.")
    return {"text": "\n".join(lines), "topic": topic, "experts": experts}
