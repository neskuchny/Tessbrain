# -*- coding: utf-8 -*-
"""Kanon контроль-луп (SIMA P3): «ведём агента за ручку и проверяем работу».

Раньше связь была разомкнута: после прогона кодинг-агента (confirm_handoff)
никто не сверял результат с Kanon-контрактами — приёмка была только ручной.
Здесь замыкаем: сразу после прогона верифицируем блок(и) проекта против
workspace, пишем вердикты (канва красит точки), а при провале приёмки —
собираем РЕФАЙН-ТЗ (контракт + конкретные непройденные проверки) и заводим
новый PENDING-handoff. Человеческий гейт сохраняется (refine — тоже PENDING,
подтверждается отдельно); полностью автономный ре-прогон — только за
env-флагом KANON_AUTO_REFINE (по умолчанию ВЫКЛ), с жёстким лимитом итераций.

Вызывается из task_analysis.confirm_handoff по завершении прогона, если у
handoff есть kanon-метаданные (project_id [+ block_id], run_commands,
iteration). Never-raise — контроль-луп не должен ронять сам handoff.
"""
from __future__ import annotations

import os

import structlog

logger = structlog.get_logger(__name__)


def max_refine_iterations() -> int:
    """Жёсткий предел рефайн-раундов (защита от бесконечного цикла/трат)."""
    try:
        return max(0, min(int(os.getenv("KANON_MAX_REFINE", "2") or 2), 5))
    except Exception:
        return 2


def auto_refine_enabled() -> bool:
    """Полностью автономный ре-прогон рефайна (без повторного подтверждения) —
    только по явному env-флагу. По умолчанию ВЫКЛ: refine ждёт confirm."""
    return os.getenv("KANON_AUTO_REFINE", "").strip().lower() in ("on", "1", "true", "yes")


def visual_accept_enabled() -> bool:
    """Визуальная приёмка HTML-результатов (скриншоты desktop+mobile →
    мультимодальный критик). Включена по умолчанию: при отсутствии
    Playwright или мультимодальной модели валидатор честно возвращает
    tool_missing, и вердикт просто не учитывается — деградация мягкая.
    KANON_VISUAL_ACCEPT=off выключает целиком."""
    return os.getenv("KANON_VISUAL_ACCEPT", "on").strip().lower() in (
        "on", "1", "true", "yes")


def _collect_html_artifacts(folder: str, cap: int = 5) -> list:
    """HTML-файлы рабочей папки как артефакты для визуальной приёмки.

    Служебные каталоги workspace (atlas/, node_modules, .git) пропускаются:
    там HTML либо не результат, либо чужой.
    """
    out: list = []
    try:
        import pathlib
        root = pathlib.Path(folder)
        if not root.is_dir():
            return out
        skip_parts = {"node_modules", ".git", "atlas", "dist", "build"}
        for f in sorted(root.rglob("*.htm*")):
            if skip_parts.intersection(set(p.name for p in f.parents)):
                continue
            out.append({"name": f.name, "kind": "file", "path": str(f)})
            if len(out) >= cap:
                break
    except Exception:
        logger.debug("kanon_loop: html artifact scan skipped", exc_info=True)
    return out


async def _visual_accept(spec_text: str, folder: str) -> dict:
    """Прогнать визуальную приёмку. Возвращает
    {"applicable": bool, "verdict": "pass"|"fail"|"inconclusive", ...}.

    Правила соответствуют духу Kanon: визуальный вердикт НЕ создаёт pass —
    он может только уронить в рефайн (blocker/error от критика) или честно
    сказать «проверить не смогли» (нет инструмента → inconclusive, не
    влияет). «Нет доказательств ≠ готово» распространяется и на глаза.
    """
    out: dict = {"applicable": False, "verdict": "inconclusive"}
    try:
        artifacts = _collect_html_artifacts(folder)
        if not artifacts:
            return out  # нечего рендерить — визуальная приёмка неприменима
        from backend.core.validator.visual import VisualDiffValidator
        report = await VisualDiffValidator().validate(
            task_description=spec_text[:2000],
            tz_markdown=spec_text[:6000],
            artifacts=artifacts,
        )
        meta = report.metadata or {}
        out["applicable"] = bool(meta.get("applicable", True))
        out["score"] = report.score
        out["summary"] = (report.summary or "")[:400]
        if meta.get("tool_missing") or report.error:
            out["verdict"] = "inconclusive"
            out["tool_missing"] = meta.get("tool_missing") or ""
            return out
        issues = [
            {"severity": getattr(i.severity, "value", str(i.severity)),
             "message": str(getattr(i, "message", ""))[:300]}
            for i in (report.issues or [])
        ]
        out["issues"] = issues[:10]
        hard = [i for i in issues
                if i["severity"] in ("blocker", "error")]
        out["verdict"] = "fail" if hard else "pass"
    except Exception as e:
        logger.debug("kanon_loop: visual accept skipped: %s", e)
        out["verdict"] = "inconclusive"
        out["error"] = str(e)[:200]
    return out


def _build_refine_spec(original_spec: str, failing: list[dict]) -> str:
    """Рефайн-ТЗ: исходное ТЗ + конкретные НЕпройденные проверки приёмки."""
    parts = [(original_spec or "").strip(), "", "---", "",
             "## Приёмка НЕ пройдена — доработай реализацию", ""]
    for r in failing:
        parts.append(f"### Блок «{r.get('name')}» — вердикт: {r.get('verdict')}")
        for e in r.get("evidence", []):
            if e.get("verdict") != "pass":
                crit = e.get("raw") or e.get("target") or e.get("kind") or "проверка"
                parts.append(f"- [{e.get('verdict')}] {crit} — {e.get('detail', '')}")
        # семантические замечания судьи (todo_to_pass) — тоже в правки
        sem = r.get("semantic") or {}
        for todo in (sem.get("todo_to_pass") or [])[:8]:
            parts.append(f"- [судья] {todo}")
        parts.append("")
    parts.append("Исправь так, чтобы перечисленные проверки приёмки прошли "
                 "(file/contains/cmd в контрактах блоков). narrative.md блока "
                 "дополняй (append-only). Не ломай уже прошедшие блоки.")
    return "\n".join(parts).strip()


async def verify_after_run(user_id: str, rec: dict, folder: str) -> dict:
    """После прогона агента: верифицировать блок(и) → вердикты → при провале
    подготовить refine-handoff. Возвращает summary для ответа confirm_handoff.
    Never-raise (при любой ошибке — пустой/частичный summary)."""
    try:
        kmeta = rec.get("kanon") or {}
        project_id = kmeta.get("project_id")
        if not project_id or not folder:
            return {}

        from backend.core.sima import kanon_service as ks
        from backend.core.store.tenant_paths import sima_kanon_path_for_user
        from backend.db.sima_client import get_sima_db

        db = await get_sima_db()
        project = await db.get_project(project_id)
        if not project:
            return {}
        blocks = project.get("blocks") or []
        connections = project.get("connections") or []
        idx = {str(b.get("id")): b for b in blocks}
        run_commands = bool(kmeta.get("run_commands"))
        block_id = kmeta.get("block_id")

        # Что верифицируем: конкретный блок (batch-handoff) ИЛИ все блоки с
        # проверяемым контрактом (project-handoff).
        if block_id and str(block_id) in idx:
            targets = [idx[str(block_id)]]
        else:
            targets = []
            for b in blocks:
                contract = ks.derive_contract(b, connections, idx)
                if ks.implementation_status(contract, b).get("ready_for_handoff"):
                    targets.append(b)
        if not targets:
            return {"accepted": None,
                    "reason": "нет блоков с проверяемым контрактом — верифицировать нечего"}

        # Опц. семантический судья (Kanon R-7.94): ловит «детерминированно
        # прошло, но по смыслу неверно». НЕ создаёт pass (спека Kanon) — только
        # помогает уронить в рефайн и даёт todo_to_pass.
        use_judge = bool(kmeta.get("judge")) or ks.semantic_judge_enabled()
        results = []
        for b in targets:
            r = ks.verify_block(b, connections, idx, folder, run_commands=run_commands)
            if use_judge and r.get("verdict") != ks.FAIL:
                try:
                    r["semantic"] = await ks.semantic_judge(b, connections, idx, folder)
                except Exception:
                    logger.debug("kanon_loop: semantic_judge skipped", exc_info=True)
            results.append(r)
        try:
            ks.KanonStore(sima_kanon_path_for_user(user_id)).record(project_id, results)
        except Exception:
            logger.debug("kanon_loop: verdict record skipped", exc_info=True)

        # Визуальная приёмка результата целиком (не по-блочно): рендерим
        # HTML рабочей папки на desktop+mobile и отдаём мультимодальному
        # критику вместе с ТЗ. Вердикт не создаёт pass — только роняет в
        # рефайн при blocker/error. Нет Playwright/модели → inconclusive,
        # не влияет.
        visual = None
        if visual_accept_enabled():
            visual = await _visual_accept(rec.get("spec_text", ""), folder)

        def _needs_refine(r: dict) -> bool:
            # детерминированный не-pass ИЛИ семантический hard-fail
            if r.get("verdict") != ks.PASS:
                return True
            return (r.get("semantic") or {}).get("verdict") == ks.FAIL

        failing = [r for r in results if _needs_refine(r)]
        if visual and visual.get("verdict") == "fail":
            # Визуальный провал — отдельная запись в списке замечаний,
            # чтобы рефайн-ТЗ содержал претензии критика дословно.
            failing.append({
                "block_id": "__visual__",
                "name": "Визуальная приёмка (скриншоты)",
                "verdict": "fail",
                # Поле называется evidence — его читает _build_refine_spec,
                # чтобы претензии критика дословно попали в рефайн-ТЗ.
                "evidence": [
                    {"kind": "visual", "target": i.get("severity", ""),
                     "verdict": "fail", "detail": i.get("message", "")}
                    for i in (visual.get("issues") or [])
                    if i.get("severity") in ("blocker", "error")
                ],
            })
        summary = {
            "accepted": len(failing) == 0,
            "verified": len(results),
            "visual": visual,
            # По блокам, без визуальной записи: она в failing не блок.
            "passed": sum(1 for r in results if not _needs_refine(r)),
            "failing": [{"block_id": r.get("block_id"), "name": r.get("name"),
                         "verdict": r.get("verdict"),
                         "semantic": (r.get("semantic") or {}).get("verdict")}
                        for r in failing],
        }
        if summary["accepted"]:
            logger.info("kanon_loop: all blocks accepted", project=project_id, n=len(results))
            return summary

        iteration = int(kmeta.get("iteration", 0) or 0)
        if iteration >= max_refine_iterations():
            summary["refine"] = f"достигнут предел рефайнов ({max_refine_iterations()}) — нужна ручная доработка"
            return summary

        # Готовим refine-handoff (PENDING) — гейт человека сохраняется.
        from backend.core.tasks.task_analysis import coding_handoff
        refine_spec = _build_refine_spec(rec.get("spec_text", ""), failing)
        refine = await coding_handoff(
            user_id,
            {"title": (rec.get("task_title") or "SIMA") + f" — рефайн {iteration + 1}",
             "description": ""},
            agent=rec.get("agent", "claude"), repo_path=folder, spec_text=refine_spec,
            kanon={"project_id": project_id, "block_id": block_id,
                   "run_commands": run_commands, "iteration": iteration + 1},
        )
        summary["refine_handoff_id"] = refine.get("id")
        summary["iteration"] = iteration + 1

        # Полностью автономный ре-прогон — только за флагом.
        if auto_refine_enabled():
            try:
                from backend.core.tasks.task_analysis import confirm_handoff
                auto = await confirm_handoff(user_id, refine.get("id"), repo_path=folder)
                summary["auto_refine"] = {"handoff_id": refine.get("id"),
                                          "status": auto.get("status")}
            except Exception as e:
                summary["auto_refine_error"] = str(e)
        return summary
    except Exception as e:
        logger.warning("kanon_loop: verify_after_run failed: %s", e)
        return {}
