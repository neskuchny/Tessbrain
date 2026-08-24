# -*- coding: utf-8 -*-
"""Офисная задача под приёмкой: исполнил → проверили → доработал → человек.

Чем это «лучше, чем у него». Петля OpenWorker (и подобных): описал
результат → агент сделал → человек посмотрел. Слабое место — середина:
между «сделал» и «посмотрел» нет машинной проверки, и человек читает
КАЖДЫЙ результат целиком, включая явный брак. Здесь между исполнителем
и человеком стоит та же трёхисходная приёмка, что в слое агентов и в
Kanon: «нет доказательств ≠ готово».

  задача + проверки → исполнитель → приёмка:
      fail        → замечания ДОСЛОВНО в новое задание, ещё попытка
                    (ограниченно — MAX_RETURNS, не бесконечный цикл)
      pass        → к человеку с пометкой «проверки прошли»
      inconclusive→ к человеку с пометкой «проверить нечем — читайте сами»

  Финал ВСЕГДА за человеком: петля отбраковывает и доводит, но не
  принимает. Автоприёмки «молча в прод» здесь нет и не будет.

Проверки — текстовые (contains / regex / min_len), те же и с той же
семантикой, что в слое внешних агентов: битый regex отбрасывается, без
проверок вердикт честно «не доказано», а не «принято». Файлы и работающий
код эта приёмка не запускает — для кода есть feedback_loop с валидаторами.

Всё инъектируемо (backend, sleep) → юнит-тест без сети и реального
исполнителя.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from backend.core.executors.base import (
    ExecutorBackend,
    TaskResult,
    TaskStatus,
    TaskSubmission,
)

logger = logging.getLogger(__name__)

MAX_RETURNS = 2          # доработок после первой попытки
POLL_INTERVAL_S = 5.0
MAX_WAIT_S = 3600.0

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_INCONCLUSIVE = "inconclusive"


# ── Приёмка (та же семантика, что в слое агентов) ───────────────────────

def normalize_acceptance(raw: Optional[List[dict]]) -> List[dict]:
    """Отобрать валидные проверки. Битый regex молча отбрасывается —
    сломанная проверка не должна ронять задачу. Кап 20."""
    out: List[dict] = []
    for c in (raw or [])[:20]:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("kind") or "").strip().lower()
        if kind == "contains" and str(c.get("target") or "").strip():
            out.append({"kind": "contains",
                        "target": str(c["target"]).strip()})
        elif kind == "min_len":
            try:
                n = int(c.get("n"))
            except (TypeError, ValueError):
                continue
            if n > 0:
                out.append({"kind": "min_len", "n": n})
        elif kind == "regex":
            pat = str(c.get("pattern") or "")
            try:
                re.compile(pat)
            except re.error:
                continue
            if pat:
                out.append({"kind": "regex", "pattern": pat})
    return out


def verify_text(result_text: str, acceptance: List[dict]) -> dict:
    """Трёхисходный вердикт по тексту результата.

    Нет проверок → inconclusive с объяснением, НЕ pass: «нечем проверять»
    и «проверено» — разные утверждения.
    """
    text = result_text or ""
    if not acceptance:
        return {"verdict": VERDICT_INCONCLUSIVE, "checks": [],
                "note": "проверок приёмки нет — принять может только человек"}
    checks = []
    failed = 0
    for c in acceptance:
        ok, detail = True, ""
        if c["kind"] == "contains":
            ok = c["target"].lower() in text.lower()
            detail = c["target"]
        elif c["kind"] == "min_len":
            ok = len(text.strip()) >= c["n"]
            detail = f"минимум {c['n']} символов"
        elif c["kind"] == "regex":
            ok = re.search(c["pattern"], text) is not None
            detail = c["pattern"]
        checks.append({"kind": c["kind"], "detail": detail,
                       "verdict": VERDICT_PASS if ok else VERDICT_FAIL})
        failed += 0 if ok else 1
    return {"verdict": VERDICT_FAIL if failed else VERDICT_PASS,
            "checks": checks, "failed": failed}


def build_remarks(verdict: dict) -> str:
    """Замечания для доработки — конкретные, из проваленных проверок."""
    lines = [f"- не выполнено ({c['kind']}): {c['detail']}"
             for c in verdict.get("checks", [])
             if c.get("verdict") == VERDICT_FAIL]
    return "\n".join(lines)


def _refined_task(original: str, remarks: str, attempt: int) -> str:
    return (f"{original}\n\n## Замечания приёмки (доработка {attempt})\n"
            f"Предыдущий результат не прошёл проверки:\n{remarks}\n"
            f"Исправь ровно это, не ломая остального.")


# ── Петля ───────────────────────────────────────────────────────────────

@dataclass
class OfficeAttempt:
    attempt: int
    handle_id: str
    executor_success: bool
    verdict: str
    remarks: str = ""
    error: str = ""


@dataclass
class OfficeRunReport:
    """Итог петли. status — что делать человеку:
    awaiting_human — результат есть (pass или не доказано), решает человек;
    returned_exhausted — доработки исчерпаны, последний результат к человеку;
    failed — исполнитель не дал результата вовсе."""
    status: str
    final_text: str = ""
    final_verdict: dict = field(default_factory=dict)
    attempts: List[OfficeAttempt] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "final_text": self.final_text,
                "final_verdict": self.final_verdict,
                "attempts": [a.__dict__ for a in self.attempts]}


async def _wait_result(backend: ExecutorBackend, handle,
                       *, poll_s: float, max_wait_s: float,
                       sleep: Callable[[float], Any]) -> Optional[TaskResult]:
    waited = 0.0
    while waited < max_wait_s:
        status = await backend.get_status(handle)
        if status in (TaskStatus.DONE, TaskStatus.FAILED,
                      TaskStatus.CANCELLED):
            return await backend.get_result(handle)
        await sleep(poll_s)
        waited += poll_s
    return None


async def run_office_task(
    *,
    task_text: str,
    acceptance: Optional[List[dict]] = None,
    backend: ExecutorBackend,
    metadata: Optional[dict] = None,
    max_returns: int = MAX_RETURNS,
    poll_s: float = POLL_INTERVAL_S,
    max_wait_s: float = MAX_WAIT_S,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> OfficeRunReport:
    """Прогнать офисную задачу через исполнителя под приёмкой.

    Возвращает отчёт с историей попыток. Никогда не raises на «нормальных»
    сбоях исполнителя — они видны в attempts и в статусе failed.
    """
    checks = normalize_acceptance(acceptance)
    report = OfficeRunReport(status="failed")
    current_task = task_text

    for attempt in range(1, max_returns + 2):   # первая + доработки
        try:
            handle = await backend.submit(TaskSubmission(
                tz_markdown=current_task, task_type="office",
                metadata=dict(metadata or {})))
        except Exception as exc:
            report.attempts.append(OfficeAttempt(
                attempt=attempt, handle_id="", executor_success=False,
                verdict="", error=f"{type(exc).__name__}: {exc}"))
            return report

        result = await _wait_result(backend, handle, poll_s=poll_s,
                                    max_wait_s=max_wait_s, sleep=sleep)
        if result is None or not result.success:
            report.attempts.append(OfficeAttempt(
                attempt=attempt, handle_id=handle.id,
                executor_success=False, verdict="",
                error=(result.error_message if result else "таймаут ожидания")
                or "исполнитель не вернул результат"))
            return report

        text = ""
        for a in result.artifacts:
            if a.get("kind") == "text" and a.get("content"):
                text = str(a["content"])
                break
        text = text or result.summary or ""

        verdict = verify_text(text, checks)
        remarks = build_remarks(verdict)
        report.attempts.append(OfficeAttempt(
            attempt=attempt, handle_id=handle.id, executor_success=True,
            verdict=verdict["verdict"], remarks=remarks))
        report.final_text = text
        report.final_verdict = verdict

        if verdict["verdict"] in (VERDICT_PASS, VERDICT_INCONCLUSIVE):
            report.status = "awaiting_human"
            return report
        if attempt >= max_returns + 1:
            # Доработки исчерпаны — последний результат идёт к человеку
            # с замечаниями, а не в мусор: решает он.
            report.status = "returned_exhausted"
            return report
        current_task = _refined_task(task_text, remarks, attempt)

    return report


__all__ = [
    "MAX_RETURNS",
    "OfficeAttempt",
    "OfficeRunReport",
    "build_remarks",
    "normalize_acceptance",
    "run_office_task",
    "verify_text",
]
