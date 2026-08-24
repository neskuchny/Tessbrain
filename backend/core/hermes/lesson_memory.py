# -*- coding: utf-8 -*-
"""Hermes lesson-memory (P12d) — память удач/неудач с заземлением на граф.

ПРОБЕЛ (карта переноса, разрыв #2): Hermes копит «уроки» в MEMORY.md
(lessons learned) и подмешивает их в будущие задачи. У нас этого как
механизма не было (feedback-loop не персистил исходы).

P12d: атомарные уроки (success/failure/correction) per-user, recall по
релевантности → инжект в задачу. **Наш апгрейд над Hermes**: урок
несёт `graph_refs` (узлы графа) и `pipeline_id` (P7 lineage) — урок не
просто текст, а заземлён на источник.

Дизайн (как P0–P12c): чистый (stdlib only), детерминированный (без
LLM → юнит-тест без сети), never-raises, инъектируемые root/clock,
bounded, per-user изоляция (паттерн как rule_book: data/<x>/{uid}.json).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_LESSONS_PER_USER = 300
_WORD_RE = re.compile(r"[a-zа-я0-9]+", re.IGNORECASE)
_KINDS = ("success", "failure", "correction")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 2}


@dataclass
class Lesson:
    text: str
    kind: str = "success"                       # success|failure|correction
    tags: list[str] = field(default_factory=list)
    task: str = ""                              # краткое описание задачи
    graph_refs: list[str] = field(default_factory=list)  # узлы графа
    pipeline_id: str = ""                       # P7 lineage
    created_at: str = ""
    updated_at: str = ""
    hits: int = 0                               # сколько раз переиспользован

    @property
    def id(self) -> str:
        return hashlib.sha1(
            f"{self.kind}|{_norm(self.text)}".encode("utf-8")
        ).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "text": self.text, "kind": self.kind,
            "tags": list(self.tags), "task": self.task,
            "graph_refs": list(self.graph_refs),
            "pipeline_id": self.pipeline_id,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "hits": self.hits,
        }
        return d

    @staticmethod
    def from_dict(d: dict) -> "Lesson":
        return Lesson(
            text=str(d.get("text", "")),
            kind=str(d.get("kind", "success")),
            tags=[str(t) for t in d.get("tags", []) if isinstance(d.get("tags"), list)],
            task=str(d.get("task", "")),
            graph_refs=[str(g) for g in d.get("graph_refs", [])
                        if isinstance(d.get("graph_refs"), list)],
            pipeline_id=str(d.get("pipeline_id", "")),
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
            hits=int(d.get("hits", 0) or 0),
        )


class LessonStore:
    """Per-user файловый стор уроков. Никогда не raises."""

    def __init__(
        self,
        root: str = "data/agent_lessons",
        *,
        clock: Optional[Callable[[], str]] = None,
        max_lessons: int = MAX_LESSONS_PER_USER,
    ) -> None:
        self._root = Path(root)
        self._clock = clock or _now_iso
        self._max = max_lessons

    def _path(self, user_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", user_id or "anon") or "anon"
        return self._root / f"{safe}.json"

    def all(self, user_id: str) -> list[Lesson]:
        try:
            p = self._path(user_id)
            if not p.exists():
                return []
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
            return [Lesson.from_dict(x) for x in raw if isinstance(x, dict)]
        except Exception as exc:
            logger.debug("lesson_store.all failed: %s", exc)
            return []

    def _save(self, user_id: str, lessons: list[Lesson]) -> bool:
        try:
            p = self._path(user_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps([x.to_dict() for x in lessons],
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            logger.debug("lesson_store._save failed: %s", exc)
            return False

    def _prune(self, lessons: list[Lesson]) -> list[Lesson]:
        if len(lessons) <= self._max:
            return lessons
        # держим самые ценные: больше hits, затем свежее
        ranked = sorted(
            lessons, key=lambda x: (x.hits, x.updated_at or x.created_at),
            reverse=True,
        )
        return ranked[: self._max]

    def add(self, user_id: str, lesson: Lesson) -> bool:
        """Добавить/слить урок (дедуп по kind+норм.текст). Не raises."""
        if not isinstance(lesson, Lesson) or not lesson.text.strip():
            return False
        if lesson.kind not in _KINDS:
            lesson.kind = "success"
        try:
            now = self._clock()
            lessons = self.all(user_id)
            by_id = {x.id: x for x in lessons}
            if lesson.id in by_id:
                ex = by_id[lesson.id]
                ex.hits += 1
                ex.updated_at = now
                ex.tags = sorted(set(ex.tags) | set(lesson.tags))
                ex.graph_refs = sorted(set(ex.graph_refs) |
                                       set(lesson.graph_refs))
                if lesson.pipeline_id:
                    ex.pipeline_id = lesson.pipeline_id
            else:
                lesson.created_at = now
                lesson.updated_at = now
                lessons.append(lesson)
            return self._save(user_id, self._prune(lessons))
        except Exception as exc:
            logger.debug("lesson_store.add failed: %s", exc)
            return False

    def recall(
        self, user_id: str, query: str, *, limit: int = 5,
    ) -> list[Lesson]:
        """Топ релевантных уроков по пересечению токенов (+бонус за
        hits/failure). Детерминированно, не raises."""
        try:
            q = _tokens(query)
            if not q:
                return []
            scored: list[tuple[float, Lesson]] = []
            for ls in self.all(user_id):
                base = _tokens(ls.text) | _tokens(" ".join(ls.tags)) | _tokens(ls.task)
                if not base:
                    continue
                overlap = len(q & base) / float(len(q))
                if overlap <= 0:
                    continue
                score = overlap + 0.05 * min(ls.hits, 10)
                if ls.kind == "failure":
                    score += 0.1  # провалы важно не повторить
                scored.append((score, ls))
            scored.sort(key=lambda t: t[0], reverse=True)
            return [ls for _s, ls in scored[: max(1, limit)]]
        except Exception as exc:
            logger.debug("lesson_store.recall failed: %s", exc)
            return []


def to_prompt(lessons: list[Lesson]) -> str:
    """Компактный блок для инжекта в задачу. Не raises."""
    if not lessons:
        return ""
    lines = ["Уроки из прошлого опыта (учти их):"]
    for ls in lessons:
        mark = {"failure": "⚠️", "correction": "✏️"}.get(ls.kind, "✓")
        ref = f" [граф:{','.join(ls.graph_refs[:3])}]" if ls.graph_refs else ""
        lines.append(f"- {mark} {ls.text.strip()}{ref}")
    return "\n".join(lines)


# === извлечение из траектории / исхода (детерминированно) ==============

_ID_KEYS = ("node_id", "entity_id", "id", "snapshot_id", "meeting_id")


def extract_graph_refs(steps: Any) -> tuple[list[str], str]:
    """Best-effort: id-подобные ссылки и pipeline_id из шагов. Не raises."""
    refs: list[str] = []
    pipeline_id = ""
    try:
        for s in steps if isinstance(steps, (list, tuple)) else []:
            args = getattr(s, "args", None)
            if args is None and isinstance(s, dict):
                args = s.get("args")
            if not isinstance(args, dict):
                continue
            for k, v in args.items():
                if k == "pipeline_id" and v:
                    pipeline_id = str(v)
                elif k in _ID_KEYS and isinstance(v, (str, int)) and v:
                    refs.append(str(v))
        # uniq, bounded
        seen: list[str] = []
        for r in refs:
            if r not in seen:
                seen.append(r)
            if len(seen) >= 10:
                break
        return seen, pipeline_id
    except Exception as exc:
        logger.debug("extract_graph_refs failed: %s", exc)
        return [], ""


def lesson_from_outcome(
    *,
    success: bool,
    task: str,
    tools: list[str],
    summary: str = "",
    graph_refs: Optional[list[str]] = None,
    pipeline_id: str = "",
) -> Optional[Lesson]:
    """Детерминированный урок из исхода. None если нечего фиксировать."""
    task = (task or "").strip()
    if not task:
        return None
    uniq_tools = []
    for t in tools or []:
        if t and t != "done" and t not in uniq_tools:
            uniq_tools.append(t)
    if success:
        text = (
            f"Задача «{task[:120]}» решена связкой инструментов: "
            f"{', '.join(uniq_tools) or 'n/a'}."
        )
        kind = "success"
    else:
        text = (
            f"Подход к задаче «{task[:120]}» НЕ достиг цели "
            f"(инструменты: {', '.join(uniq_tools) or 'n/a'}). "
            f"{summary.strip()}".strip()
        )
        kind = "failure"
    return Lesson(
        text=text, kind=kind, tags=uniq_tools[:6], task=task[:160],
        graph_refs=list(graph_refs or []), pipeline_id=pipeline_id,
    )


def maybe_capture_lesson(
    *,
    success: bool,
    task: str,
    steps: Any,
    store: LessonStore,
    user_id: str,
    summary: str = "",
    enabled: bool = False,
) -> Optional[str]:
    """Зафиксировать урок из траектории. Возвращает lesson.id или None.
    НИКОГДА не raises (память не должна ронять ответ агента)."""
    if not enabled:
        return None
    try:
        norm = []
        for s in steps if isinstance(steps, (list, tuple)) else []:
            tool = getattr(s, "tool", None)
            if tool is None and isinstance(s, dict):
                tool = s.get("tool")
            if tool:
                norm.append(str(tool))
        refs, pid = extract_graph_refs(steps)
        ls = lesson_from_outcome(
            success=success, task=task, tools=norm, summary=summary,
            graph_refs=refs, pipeline_id=pid,
        )
        if ls is None:
            return None
        return ls.id if store.add(user_id, ls) else None
    except Exception as exc:
        logger.debug("maybe_capture_lesson failed (safe): %s", exc)
        return None


def maybe_recall_block(
    user_id: str,
    query: str,
    *,
    store: LessonStore,
    enabled: bool = False,
    limit: int = 5,
) -> str:
    """Блок релевантных уроков для инжекта в сообщение. '' если выкл/
    нет. Не raises."""
    if not enabled:
        return ""
    try:
        return to_prompt(store.recall(user_id, query, limit=limit))
    except Exception as exc:
        logger.debug("maybe_recall_block failed: %s", exc)
        return ""


__all__ = [
    "Lesson",
    "LessonStore",
    "to_prompt",
    "extract_graph_refs",
    "lesson_from_outcome",
    "maybe_capture_lesson",
    "maybe_recall_block",
    "MAX_LESSONS_PER_USER",
]
