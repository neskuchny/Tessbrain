# -*- coding: utf-8 -*-
"""Batch Research Mode — исследование по ВСЕМУ корпусу, а не по топ-K чанков.

Зачем: обычный чат отвечает по ~16 фрагментам — вопросы «как менялось со
временем», «системно по всем конфликтам», «какой вклад каждого» отвечаются
по выборке, и обрезка происходит молча. Здесь — честный проход:

  Фаза 0 РАЗВЕДКА: система сначала выясняет, ГДЕ лежат данные (встречи по
      месяцам, снапшоты, сущности из вопроса в графе) и СТРОИТ ПЛАН
      исследования (один LLM-вызов), и только потом исследует.
  Фаза 1 ОТБОР: встречи фильтруются по периоду/ключевым словам плана;
      если больше лимита — ранжирование по релевантности.
  Фаза 2 ЧТЕНИЕ: каждая отобранная встреча читается ЦЕЛИКОМ (полный
      транскрипт из Supabase), LLM извлекает находки под вопрос.
  Фаза 3 СИНТЕЗ: находки сортируются хронологически → итоговый отчёт
      с покрытием («проанализировано N встреч из M найденных»).

Долгий (минуты) → запускается фоном, прогресс и результат — в
data/research/<user_id>/<research_id>.json (переживает рестарт).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_MEETINGS = int(os.getenv("TESSENT_RESEARCH_MAX_MEETINGS", "30"))
_TRANSCRIPT_CHARS = int(os.getenv("TESSENT_RESEARCH_TRANSCRIPT_CHARS", "9000"))
_CONCURRENCY = int(os.getenv("TESSENT_RESEARCH_CONCURRENCY", "3"))
# Жёсткий $-потолок на ОДНО исследование (реальный счётчик по job_id из
# usage_tracker, не оценка). Дошли до потолка — чтение останавливается,
# отчёт собирается из уже найденного с честной пометкой.
_BUDGET_USD = float(os.getenv("TESSENT_RESEARCH_BUDGET_USD", "0.60"))


def _research_dir(user_id: str) -> Path:
    p = Path("data") / "research" / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_state(user_id: str, research_id: str, state: Dict[str, Any]) -> None:
    try:
        (_research_dir(user_id) / f"{research_id}.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("research state save failed", exc_info=True)


def load_state(user_id: str, research_id: str) -> Optional[Dict[str, Any]]:
    try:
        f = _research_dir(user_id) / f"{research_id}.json"
        if not f.exists():
            return None
        state = json.loads(f.read_text(encoding="utf-8"))
        # Восстановление после рестарта: статус «в работе», но задачи в
        # процессе нет и файл давно не обновлялся → честно «interrupted»
        # (иначе фронт поллил бы вечно).
        if state.get("status") in ("starting", "discovering", "reading",
                                   "synthesizing", "verifying") \
                and research_id not in _RUNNING:
            import time as _time
            if _time.time() - f.stat().st_mtime > 120:
                state["status"] = "failed"
                state["error"] = ("Прервано (перезапуск сервера). "
                                  "Запустите исследование заново.")
                _save_state(user_id, research_id, state)
        return state
    except Exception:
        logger.debug("research state load failed", exc_info=True)
    return None


def list_research(user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    out = []
    try:
        files = sorted(_research_dir(user_id).glob("*.json"),
                       key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
        for f in files:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                out.append({"research_id": d.get("research_id"),
                            "question": d.get("question", "")[:160],
                            "status": d.get("status"),
                            "started_at": d.get("started_at"),
                            "progress": d.get("progress", "")})
            except Exception:
                continue
    except Exception:
        logger.debug("research list failed", exc_info=True)
    return out


def _md_to_html(report_md: str) -> str:
    """Мини-конвертер markdown → HTML без зависимостей: заголовки, жирный,
    курсив-строки, списки, цитаты (>), разделители, абзацы, бейджи."""
    import html as _html
    lines_out: List[str] = []
    in_list = False
    for raw in (report_md or "").split("\n"):
        line = _html.escape(raw.rstrip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        # «[не подтверждено находками]» — заметная оранжевая пометка
        line = line.replace("[не подтверждено находками]",
                            '<span class="warn">не подтверждено находками</span>')
        # «цитаты в ёлочках» — визуально выделяем
        line = re.sub(r"«(.{10,240}?)»", r'<span class="q">«\1»</span>', line)
        stripped = line.strip()
        if re.fullmatch(r"_(.+)_", stripped):
            stripped = f"<em class='basis'>{stripped[1:-1]}</em>"
            line = stripped
        if stripped.startswith(("- ", "• ", "* ")):
            if not in_list:
                lines_out.append("<ul>")
                in_list = True
            lines_out.append(f"<li>{stripped[2:]}</li>")
            continue
        if in_list:
            lines_out.append("</ul>")
            in_list = False
        if stripped.startswith("&gt; "):
            lines_out.append(f"<blockquote>{stripped[5:]}</blockquote>")
        elif stripped.startswith("### "):
            lines_out.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            lines_out.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            lines_out.append(f"<h2>{stripped[2:]}</h2>")
        elif stripped.startswith("---"):
            lines_out.append("<hr/>")
        elif stripped:
            lines_out.append(f"<p>{stripped}</p>")
    if in_list:
        lines_out.append("</ul>")
    return "\n".join(lines_out)


def _render_report_html(question: str, report_md: str, dt: str,
                        badges: Optional[List[str]] = None,
                        appendix_html: str = "") -> str:
    """Markdown-отчёт → самодостаточный HTML-документ.

    badges — строка фактов покрытия под заголовком («31 встреча прочитана»,
    «$0.012»); appendix_html — готовый HTML приложения «Источники и покрытие»
    (сворачиваемые списки, чтобы было видно, ЧТО прочитано, а что нет)."""
    import html as _html
    body = _md_to_html(report_md)
    q = _html.escape(question)
    badge_html = ""
    if badges:
        badge_html = ('<div class="badges">'
                      + "".join(f'<span class="badge">{_html.escape(b)}</span>'
                                for b in badges)
                      + "</div>")
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Исследование — {q[:80]}</title>
<style>
 :root {{ --ink:#1a2233; --mut:#7a86a8; --acc:#4f5ed7; --line:#e3e8f4; }}
 body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
        max-width: 880px; margin: 0 auto; padding: 36px 28px 60px;
        color: var(--ink); line-height: 1.7; background: #fbfcff; }}
 .head {{ background: linear-gradient(120deg, #4f5ed7 0%, #6a7ae8 100%);
        color: #fff; border-radius: 14px; padding: 22px 26px; margin-bottom: 22px;
        box-shadow: 0 6px 18px rgba(79,94,215,.18); }}
 .head h1 {{ font-size: 21px; margin: 0 0 6px; line-height: 1.35; }}
 .meta {{ color: #dfe4ff; font-size: 12.5px; }}
 .badges {{ margin: 14px 0 4px; display: flex; flex-wrap: wrap; gap: 8px; }}
 .badge {{ background: #eef1fd; color: #3a49b8; border: 1px solid #d8ddf8;
        border-radius: 999px; padding: 3px 12px; font-size: 12.5px; }}
 h2 {{ font-size: 18px; margin: 30px 0 10px; color: #2c3a6b;
      border-left: 4px solid var(--acc); padding-left: 10px; }}
 h3 {{ font-size: 15.5px; margin-top: 20px; color: #38466f; }}
 li {{ margin: 6px 0; }}
 hr {{ border: none; border-top: 1px solid var(--line); margin: 26px 0; }}
 p {{ margin: 9px 0; }}
 blockquote {{ margin: 10px 0; padding: 8px 14px; border-left: 3px solid #b9c3ee;
        background: #f2f4fd; border-radius: 0 8px 8px 0; color: #3c4667; }}
 .q {{ color: #345; background: #eef4ea; border-radius: 4px; padding: 0 3px; }}
 .warn {{ background: #fff1dc; color: #9a6200; border: 1px solid #f2d9ab;
        border-radius: 4px; padding: 0 6px; font-size: 12.5px; }}
 .basis {{ color: var(--mut); font-size: 13px; }}
 details {{ border: 1px solid var(--line); border-radius: 10px;
        padding: 10px 16px; margin: 10px 0; background: #fff; }}
 details summary {{ cursor: pointer; font-weight: 600; color: #38466f; }}
 details li {{ font-size: 13.5px; color: #43507a; }}
 .apx-note {{ color: var(--mut); font-size: 13px; }}
 @media print {{ body {{ margin: 12px; background: #fff; }}
        .head {{ box-shadow: none; }} details {{ page-break-inside: avoid; }} }}
 .pdfbtn {{ position: fixed; top: 18px; right: 18px; background: #4f5ed7;
        color: #fff; border: none; border-radius: 10px; padding: 9px 16px;
        font-size: 13.5px; cursor: pointer; box-shadow: 0 4px 12px rgba(79,94,215,.3); }}
 .pdfbtn:hover {{ background: #3a49b8; }}
 @media print {{ .pdfbtn {{ display: none; }} }}
</style></head><body>
<button class="pdfbtn" onclick="window.print()">⬇️ Сохранить в PDF</button>
<div class="head"><h1>🔬 {q}</h1>
<div class="meta">Tessbrain · Batch Research · {dt}</div></div>
{badge_html}
{body}
{appendix_html}
</body></html>"""


def _parse_json_blob(text: str) -> Any:
    """Вытащить JSON из ответа LLM (объект или массив, терпим обвязку)."""
    if not text:
        return None
    for opener, closer in (("{", "}"), ("[", "]")):
        s, e = text.find(opener), text.rfind(closer)
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except Exception:
                continue
    return None


class BatchResearcher:
    def __init__(self, user_id: str, question: str, research_id: Optional[str] = None,
                 mode: str = "research", depth: str = "standard"):
        self.user_id = user_id
        self.question = (question or "").strip()
        # mode: research (отчёт по фактам) | advisor (факты + СОБСТВЕННЫЕ
        # предложения системы, размечены отдельно — «советник», не архивариус)
        self.mode = mode if mode in ("research", "advisor") else "research"
        # depth: standard | deep. Deep = «глубокое исследование»: ×2 встреч,
        # ×2 длина транскрипта, ×3 бюджет, до 2 раундов рефлексии, длиннее
        # синтез. Явно включается пользователем (дороже и дольше).
        self.depth = depth if depth in ("standard", "deep") else "standard"
        _deep = self.depth == "deep"
        self.max_meetings = _MAX_MEETINGS * (2 if _deep else 1)
        self.transcript_chars = _TRANSCRIPT_CHARS * (2 if _deep else 1)
        self.budget_usd = _BUDGET_USD * (3 if _deep else 1)
        self.reflection_rounds = 2 if _deep else 1
        self.synth_tokens = 6000 if _deep else 3600
        self.research_id = research_id or f"res_{uuid.uuid4().hex[:10]}"
        self.state: Dict[str, Any] = {
            "research_id": self.research_id,
            "question": self.question,
            "depth": self.depth,
            "status": "starting",
            "progress": "",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

    def _progress(self, status: str, progress: str) -> None:
        self.state["status"] = status
        self.state["progress"] = progress
        _save_state(self.user_id, self.research_id, self.state)
        logger.info(f"🔬 Research {self.research_id}: {progress}")

    def _spent_usd(self) -> float:
        """Реально потрачено на ЭТО исследование (по job_id из usage_tracker)."""
        try:
            from backend.core.llm.quota import _daily_metric
            return float(_daily_metric("", scope="job",
                                       subject_id=self.research_id,
                                       metric="total_cost"))
        except Exception:
            return 0.0

    def _budget_ok(self) -> bool:
        return self._spent_usd() < self.budget_usd

    # ── Фаза 0: разведка + план ────────────────────────────────────────
    async def _discover(self, llm) -> Dict[str, Any]:
        """Инвентаризация данных → LLM-план исследования."""
        inv: Dict[str, Any] = {"meetings_by_month": {}, "snapshots": {},
                               "matched_entities": []}
        meetings: List[Dict[str, Any]] = []
        try:
            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            from backend.core.store.graph_view import merged_graph_view_for_user
            gb = await merged_graph_view_for_user(self.user_id, use_networkx=None)
            tok = set_current_tenant(self.user_id)
            try:
                nodes = await gb.find_nodes_by_label(
                    "Meeting", limit=2000, tenant_id=self.user_id,
                    strict_tenant=False)
                for m in nodes:
                    mid = (m.get("meeting_id") or m.get("id") or "")
                    date = str(m.get("date") or m.get("created_at") or "")
                    title = (m.get("title") or m.get("name") or "")
                    if not mid:
                        continue
                    meetings.append({"id": str(mid), "date": date, "title": title})
                    month = date[:7] if len(date) >= 7 else "????-??"
                    inv["meetings_by_month"][month] = inv["meetings_by_month"].get(month, 0) + 1
                # сущности из вопроса (имена с заглавной, 2+ слов или кавычки)
                cand = set(re.findall(r"«([^»]{3,60})»|\"([^\"]{3,60})\"", self.question))
                flat = {c for pair in cand for c in pair if c}
                flat |= set(re.findall(r"[А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁA-Z][а-яёa-z]+)?",
                                       self.question))
                unmatched: List[str] = []
                for name in list(flat)[:8]:
                    try:
                        hits = await gb.search_nodes(query=name, limit=3,
                                                     tenant_id=self.user_id)
                        if hits:
                            for h in hits:
                                inv["matched_entities"].append({
                                    "name": h.get("name") or h.get("title"),
                                    "label": h.get("_label") or "",
                                    "id": h.get("id")})
                        else:
                            unmatched.append(name)
                    except Exception:
                        unmatched.append(name)
                # Фаззи-фолбэк по людям: «Пустовалова» в вопросе против
                # «Постовалова» в графе (опечатка транскрипции) — точный поиск
                # даёт 0, и граф-обход не запускается вовсе. Сверяем токены
                # имени через _name_matches (диминутивы/фаззи/транслит).
                if unmatched:
                    try:
                        from backend.core.store.entity_resolver import _name_matches
                        persons = await gb.find_nodes_by_label(
                            "Person", limit=2000, tenant_id=self.user_id,
                            strict_tenant=True)
                        for name in unmatched:
                            for p in persons or []:
                                pn = (p.get("name") or "").strip()
                                if pn and _name_matches(name, pn):
                                    inv["matched_entities"].append({
                                        "name": pn, "label": "Person",
                                        "id": p.get("id")})
                                    logger.info(
                                        f"research: фаззи-матч сущности "
                                        f"«{name}» → «{pn}»")
                                    break
                    except Exception:
                        logger.debug("fuzzy entity match failed", exc_info=True)
            finally:
                reset_current_tenant(tok)
                await gb.close(save=False)
        except Exception as e:
            logger.warning(f"research discover graph failed: {e}")

        try:
            from backend.core.store.tenant_paths import snapshots_dir_for_user
            base = snapshots_dir_for_user(self.user_id)
            for sub in ("persons", "projects", "clients", "departments"):
                d = base / sub
                if d.exists():
                    inv["snapshots"][sub] = len(list(d.glob("*.json")))
        except Exception:
            logger.debug("research snapshot inventory failed", exc_info=True)

        # ПЛАН: где искать и как (тот самый шаг «сначала пойми, где данные»)
        months = ", ".join(f"{k}:{v}" for k, v in sorted(inv["meetings_by_month"].items()))
        ents = "; ".join(f"{e['name']} ({e['label']})" for e in inv["matched_entities"][:10])
        plan_prompt = (
            "Ты планируешь исследование по памяти компании. Вопрос:\n"
            f"«{self.question}»\n\n"
            f"Доступные данные:\n- Встречи по месяцам: {months or 'нет'}\n"
            f"- Снапшоты: {json.dumps(inv['snapshots'], ensure_ascii=False)}\n"
            f"- Найденные в графе сущности из вопроса: {ents or 'нет'}\n\n"
            "Верни СТРОГО JSON:\n"
            '{"period_from": "YYYY-MM или null", "period_to": "YYYY-MM или null", '
            + ('"keywords": ["10-16 ключевых слов, ОБЯЗАТЕЛЬНО включая '
               'СИНОНИМЫ и словоформы (например для оплаты: зарплата, бонус, '
               'вознаграждение, фикс, процент, доля, выплата)"], '
               if self.depth == "deep" else
               '"keywords": ["3-8 ключевых слов/фраз для отбора встреч"], ')
            + '"use_snapshots": true|false, '
            '"aspects": ["2-5 аспектов, на которые отвечать по каждой встрече"]}'
        )
        plan = None
        try:
            resp = await llm.generate(prompt=plan_prompt, temperature=0.1, max_tokens=400)
            plan = _parse_json_blob(resp.get("text", "") if isinstance(resp, dict) else str(resp))
        except Exception as e:
            logger.warning(f"research plan LLM failed: {e}")
        if not isinstance(plan, dict):
            plan = {"period_from": None, "period_to": None,
                    "keywords": [w for w in re.findall(r"[а-яёa-z]{4,}", self.question.lower())][:6],
                    "use_snapshots": True, "aspects": ["что относится к вопросу"]}
        plan["inventory"] = inv
        plan["all_meetings"] = meetings
        return plan

    # ── Фаза 0.5: обход ГРАФА от сущностей вопроса ────────────────────
    # Сильные рёбра (авторство/решения/развитие идеи) — идём глубже (2-й хоп);
    # слабые (RELATES_TO/MENTIONS) — берём соседа, но дальше не идём. Это
    # «смотрим на связь, чтобы понять, нужно ли туда идти». Ноль LLM-вызовов,
    # ограниченное число запросов к графу → дёшево и быстро.
    _STRONG_EDGES = {"PROPOSED", "EVOLVED_INTO", "DECIDED", "MADE_DECISION",
                     "HAS_IDEA", "ASSIGNED_TO", "LEADS", "OWNS", "RESPONSIBLE_FOR",
                     "PARTICIPATED_IN"}
    _FACT_LABELS = {"Idea", "Decision", "Task", "Contradiction", "Regulation"}

    async def _graph_walk(self, plan: Dict[str, Any]):
        """Вернуть (boost: meeting_id→вес, facts: строки фактов из графа).
        Факты из графа попадают в синтез даже если их встреча не вошла в
        лимит полного чтения — «найти всё, не читая всё»."""
        boost: Dict[str, float] = {}
        facts: List[str] = []
        ents = (plan.get("inventory") or {}).get("matched_entities") or []
        seeds = [e["id"] for e in ents if e.get("id")][:8]
        if not seeds:
            # Диагностика в прогресс: пользователь должен видеть, что граф
            # пропущен и почему (сущности вопроса не найдены в графе).
            self._progress("discovering",
                           "🕸 Граф: сущности вопроса не найдены в графе — "
                           "иду по чанкам и встречам")
            return boost, facts
        try:
            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            from backend.core.store.graph_view import merged_graph_view_for_user
            gb = await merged_graph_view_for_user(self.user_id, use_networkx=None)
            tok = set_current_tenant(self.user_id)
            visited: set = set()
            fetches = 0

            async def _visit(nid: str, depth: int):
                nonlocal fetches
                if nid in visited or fetches > 250 or depth > 2:
                    return
                visited.add(nid)
                try:
                    rels = await gb.get_node_relationships(nid)
                except Exception:
                    return
                edges = ([("out", r) for r in rels.get("outgoing") or []]
                         + [("in", r) for r in rels.get("incoming") or []])[:100]
                for direction, e in edges:
                    other = e.get("target") if direction == "out" else e.get("source")
                    rtype = str(e.get("type") or "").upper()
                    if not other or other in visited or fetches > 250:
                        continue
                    fetches += 1
                    try:
                        n = await gb.get_node_by_id(other)
                    except Exception:
                        continue
                    if not n:
                        continue
                    lbl = n.get("_label") or ""
                    if lbl == "Meeting":
                        mid = str(n.get("meeting_id") or n.get("id") or "")
                        if mid:
                            boost[mid] = boost.get(mid, 0.0) + (
                                2.0 if rtype in self._STRONG_EDGES else 0.7)
                        continue
                    if lbl in self._FACT_LABELS:
                        nm = (n.get("name") or n.get("text") or n.get("summary") or "")[:160]
                        if nm and len(facts) < 60:
                            _who = n.get("author") or n.get("decision_maker") or ""
                            facts.append(
                                f"[{lbl}] {nm}"
                                + (f" (автор: {_who})" if _who else "")
                                + (f" [ребро: {rtype}]" if rtype else ""))
                        # у фактов есть meeting_id — их встречи тоже кандидаты
                        fmid = str(n.get("meeting_id") or "")
                        if fmid:
                            boost[fmid] = boost.get(fmid, 0.0) + 1.5
                    # глубже — ТОЛЬКО по сильным рёбрам и содержательным узлам
                    if rtype in self._STRONG_EDGES and lbl in (
                            "Idea", "Project", "Decision", "Person"):
                        await _visit(other, depth + 1)

            try:
                for sid in seeds:
                    await _visit(sid, 1)
            finally:
                reset_current_tenant(tok)
                await gb.close(save=False)
        except Exception as e:
            logger.debug(f"research graph walk failed: {e}")
        return boost, facts

    # ── Фаза 1: отбор по УЛИКАМ (чанки), не по заголовкам ─────────────
    async def _collect_evidence(self, plan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """BM25-поиск по всему корпусу чанков: у какой встречи сколько улик.
        Возвращает meeting_id → {score, snippets[]}. Это «ничего не упустить»:
        встреча без ключевого слова в заголовке, но с обсуждением в середине —
        находится по чанку."""
        evidence: Dict[str, Dict[str, Any]] = {}
        doc_snips: List[str] = []   # чанки НЕ из встреч: документы/шаблоны
        seen_doc: set = set()
        try:
            from backend.core.search.bm25_searcher import get_bm25_searcher
            bm25 = get_bm25_searcher(user_id=self.user_id)
            _kw_cap = 12 if self.depth == "deep" else 6
            queries = [self.question] + [k for k in (plan.get("keywords") or []) if k][:_kw_cap]
            for q in queries:
                try:
                    for r in bm25.search(query=q, top_k=80):
                        meta = getattr(r, "metadata", None) or {}
                        mid = str(meta.get("meeting_id") or "")
                        txt = (getattr(r, "text", "") or "")[:260]
                        if not mid:
                            # Документ/шаблон/чат из корпуса — тоже источник:
                            # «а он читает документы, если надо?» — да, их
                            # релевантные фрагменты идут в синтез и в отчёт.
                            if txt and len(doc_snips) < 12:
                                _ttl = str(meta.get("title") or
                                           meta.get("type") or "документ")[:60]
                                key = txt[:80]
                                if key not in seen_doc:
                                    seen_doc.add(key)
                                    doc_snips.append(f"[документ: {_ttl}] {txt}")
                            continue
                        ev = evidence.setdefault(mid, {"score": 0.0, "snippets": []})
                        ev["score"] += float(getattr(r, "score", 0) or 0)
                        if txt and len(ev["snippets"]) < 3:
                            ev["snippets"].append(txt)
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"research evidence collection failed: {e}")

        # СЕМАНТИКА: вектор-поиск ПО СМЫСЛУ, не только по словам — иначе
        # «нематериальные условия» не находятся рядом с «бонусами», если нет
        # общих слов. Только при прогретой модели эмбеддингов (холодная
        # загрузка модели уже роняла воркер — не рискуем и в фоне).
        try:
            from backend.core.store.vector_indexer import (
                _EMBEDDING_MODEL_CACHE, VectorIndexer)
            if _EMBEDDING_MODEL_CACHE:
                from backend.core.store.tenant_paths import (
                    vector_index_path_for_user,
                )
                vi = VectorIndexer(
                    storage_path=vector_index_path_for_user(self.user_id),
                    namespace=self.user_id)
                await vi.connect()
                sem_hits = 0
                for q in ([self.question]
                          + [k for k in (plan.get("keywords") or []) if k][:2]):
                    try:
                        hits = await vi.search(query=q, limit=30,
                                               score_threshold=0.55)
                    except Exception:
                        continue
                    for r in hits or []:
                        pl = r.get("payload") or {}
                        mid = str(pl.get("meeting_id")
                                  or (pl.get("metadata") or {}).get("meeting_id")
                                  or "")
                        txt = (pl.get("text") or pl.get("content") or "")[:260]
                        if not mid:
                            continue
                        ev = evidence.setdefault(mid, {"score": 0.0, "snippets": []})
                        # смысловое совпадение ценнее словесного
                        ev["score"] += float(r.get("score") or 0) * 2.0
                        if txt and len(ev["snippets"]) < 3:
                            ev["snippets"].append(txt)
                        sem_hits += 1
                logger.info(f"research: семантический поиск добавил "
                            f"{sem_hits} улик (по смыслу)")
            else:
                logger.info("research: модель эмбеддингов холодная — "
                            "семантика пропущена, только BM25 по словам")
        except Exception:
            logger.debug("research semantic evidence failed", exc_info=True)

        plan["doc_snippets"] = doc_snips
        return evidence

    async def _select_meetings(self, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        meetings = plan.get("all_meetings") or []
        pf, pt = plan.get("period_from"), plan.get("period_to")
        if pf:
            meetings = [m for m in meetings if not m["date"] or m["date"][:7] >= pf]
        if pt:
            meetings = [m for m in meetings if not m["date"] or m["date"][:7] <= pt]
        kws = [k.lower() for k in (plan.get("keywords") or []) if k]
        evidence = await self._collect_evidence(plan)

        graph_boost = plan.get("graph_boost") or {}

        def _score(m):
            t = (m.get("title") or "").lower()
            title_hits = sum(1 for k in kws if k in t)
            ev = evidence.get(m["id"], {})
            return (float(ev.get("score", 0)) + title_hits * 2.0
                    + float(graph_boost.get(m["id"], 0)) * 1.5)

        for m in meetings:
            m["_evidence_score"] = _score(m)
            m["_snippets"] = evidence.get(m["id"], {}).get("snippets", [])
        # кандидаты = встречи хоть с какими-то уликами; если улик нет вообще
        # (BM25 пуст) — фолбэк на все встречи периода
        cand = [m for m in meetings if m["_evidence_score"] > 0] or meetings
        cand.sort(key=lambda m: -m["_evidence_score"])
        selected = cand[:self.max_meetings]
        selected.sort(key=lambda m: m.get("date") or "")
        plan["total_matched"] = len(cand)
        # хвост за лимитом — для рефлексии («дочитать, если не хватило»)
        plan["unselected"] = cand[self.max_meetings:self.max_meetings + 40]
        return selected

    async def _worth_full_read(self, llm, meeting: Dict[str, Any]) -> bool:
        """Дешёвый гейт ПЕРЕД полным чтением: по уликам-чанкам понятно,
        что обсуждают не то → полную встречу не читаем (экономия токенов).
        Сильные улики (верх рейтинга) идут без гейта."""
        snippets = meeting.get("_snippets") or []
        if not snippets:
            return True  # улик нет (фолбэк-отбор) — решит полное чтение
        try:
            resp = await llm.generate(
                prompt=(
                    f"Вопрос исследования: «{self.question}»\n"
                    f"Фрагменты из встречи «{meeting.get('title', '')}»:\n- "
                    + "\n- ".join(snippets)
                    + '\n\nСтоит ли читать эту встречу целиком для ответа? '
                      'Ответь СТРОГО JSON: {"read": true|false}'),
                temperature=0.0, max_tokens=20)
            data = _parse_json_blob(resp.get("text", "") if isinstance(resp, dict) else str(resp))
            return bool(data.get("read", True)) if isinstance(data, dict) else True
        except Exception:
            return True  # сомневаемся — читаем (лучше лишнее, чем упустить)

    # ── Фаза 2: полное чтение встречи ─────────────────────────────────
    async def _read_meeting(self, llm, sb, meeting: Dict[str, Any],
                            aspects: List[str]) -> Optional[Dict[str, Any]]:
        try:
            text = await sb.get_meeting_transcript(meeting["id"])
        except Exception:
            text = None
        if not text or len(text) < 200:
            return None
        if len(text) > self.transcript_chars:
            half = self.transcript_chars // 2
            text = text[:half] + "\n[...середина опущена...]\n" + text[-half:]
        prompt = (
            f"Вопрос исследования: «{self.question}»\n"
            f"Аспекты: {'; '.join(aspects)}\n\n"
            f"Встреча «{meeting.get('title', '')}» от {meeting.get('date', '')[:10]}. "
            "Прочитай транскрипт и верни СТРОГО JSON:\n"
            '{"relevant": true|false, "findings": [{"who": "кто (если известно)", '
            '"what": "что сказано/решено/произошло по вопросу, кратко", '
            '"quote": "короткая цитата-доказательство или пусто"}], '
            '"leads": ["если сказано, что вопрос решат/обсудят на ДРУГОЙ '
            'встрече — её дата в формате YYYY-MM-DD (вычисли от даты этой '
            'встречи) или название"]}\n'
            "Только факты из транскрипта, без выдумок. Нерелевантно — "
            '{"relevant": false, "findings": []}.\n\n'
            f"Транскрипт:\n{text}"
        )
        try:
            resp = await llm.generate(prompt=prompt, temperature=0.0, max_tokens=900)
            data = _parse_json_blob(resp.get("text", "") if isinstance(resp, dict) else str(resp))
            if isinstance(data, dict) and data.get("relevant") and data.get("findings"):
                return {"meeting_id": meeting["id"], "title": meeting.get("title", ""),
                        "date": meeting.get("date", "")[:10],
                        "findings": data["findings"][:8],
                        "leads": [str(x)[:80] for x in (data.get("leads") or [])][:4]}
        except Exception as e:
            logger.debug(f"research read meeting failed: {e}")
        return None

    # ── Фаза 3: снапшот-контекст + синтез ─────────────────────────────
    def _snapshot_context(self, plan: Dict[str, Any]) -> str:
        """Снапшоты сущностей вопроса — ВСЕГДА пробуем (раньше только по флагу
        плана и точному совпадению имени → почти никогда не срабатывало).
        Матчим по вхождению: сущность плана ⊆ имя снапшота или наоборот,
        плюс ключевые слова вопроса. Компанию добавляем всегда."""
        try:
            from backend.core.store.tenant_paths import snapshots_dir_for_user
            base = snapshots_dir_for_user(self.user_id)
            wanted = {(e.get("name") or "").strip().lower()
                      for e in (plan.get("inventory", {}).get("matched_entities") or [])}
            wanted |= {k.strip().lower() for k in (plan.get("keywords") or []) if len(k) > 3}
            wanted.discard("")
            parts: List[str] = []
            # компания — общий контекст для любого вопроса
            try:
                comp = base / "company" / "company.json"
                if comp.exists():
                    s = json.loads(comp.read_text(encoding="utf-8"))
                    summ = s.get("ai_summary") or s.get("description") or ""
                    if summ:
                        parts.append(f"[Снапшот компании {s.get('name', '')}] "
                                     f"{str(summ)[:400]}")
            except Exception:
                pass
            for sub in ("persons", "projects", "clients", "products"):
                d = base / sub
                if not d.exists():
                    continue
                for f in d.glob("*.json"):
                    try:
                        s = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    nm = (s.get("name") or "").strip()
                    nml = nm.lower()
                    if nm and any(w in nml or nml in w for w in wanted):
                        blk = f"[Снапшот {nm}] "
                        for k in ("ai_summary", "activity_summary", "summary", "status"):
                            if s.get(k):
                                blk += f"{str(s[k])[:400]} "
                                break
                        parts.append(blk)
                    if len(parts) >= 9:
                        break
            return "\n".join(parts[:9])
        except Exception:
            return ""

    async def _method_brief(self) -> str:
        """Методология анализа из РЕГЛАМЕНТОВ компании — если пользователь
        загрузил документ «как анализировать правду и вымысел / как думать
        через Tessbrain», его принципы подмешиваются в синтез и проверку
        (тот же паттерн, что фирменный стиль в композиторе)."""
        try:
            from backend.core.templates import get_template_registry
            registry = get_template_registry()
            for q in ("как анализировать правду и вымысел",
                      "методология анализа мышление tessent",
                      "принципы анализа данных и выводов"):
                try:
                    found = registry.search(query=q, limit=2)
                    if hasattr(found, "__await__"):
                        found = await found
                except Exception:
                    continue
                for t in found or []:
                    d = t.to_dict() if hasattr(t, "to_dict") else dict(t)
                    text = (d.get("content") or d.get("body")
                            or d.get("description") or "")
                    title = str(d.get("title") or d.get("name") or "")
                    low = (title + " " + str(text)[:200]).lower()
                    if text and any(w in low for w in
                                    ("анализ", "правд", "вымыс", "мышлен",
                                     "tessent", "критическ")):
                        return (f"МЕТОДОЛОГИЯ АНАЛИЗА (из регламента "
                                f"«{title}») — строго следуй ей при выводах:\n"
                                f"{str(text)[:1500]}\n\n")
        except Exception:
            logger.debug("method brief unavailable", exc_info=True)
        return ""

    async def run(self) -> Dict[str, Any]:
        # Весь прогон — под cost_scope(job_id): каждый LLM-вызов атрибутируется
        # на research_id, реальный расход считается usage_tracker'ом, а
        # _budget_ok() режет чтение при достижении потолка. Плюс глобальный
        # per-job cap из квот, если включён.
        from backend.core.llm.usage_tracker import cost_scope
        async with cost_scope(job_id=self.research_id, user_id=self.user_id,
                              surface="research",
                              # agent_mode обязателен: без него все вызовы
                              # research в экране расходов падают в «unknown»
                              agent_mode="research",
                              request_type=("deep_research"
                                            if self.depth == "deep"
                                            else "research")):
            return await self._run_inner()

    async def _run_inner(self) -> Dict[str, Any]:
        from backend.core.llm import get_llm_router
        llm = get_llm_router()
        if llm is None:
            self.state.update(status="failed", error="LLM недоступен")
            _save_state(self.user_id, self.research_id, self.state)
            return self.state

        self._progress("discovering", "🔎 Разведка: ищу, где лежат данные…")
        plan = await self._discover(llm)
        self.state["plan"] = {k: plan.get(k) for k in
                              ("period_from", "period_to", "keywords",
                               "use_snapshots", "aspects")}

        # Обход графа от сущностей вопроса: сильные рёбра → глубже, слабые —
        # только сосед. Даёт (а) кандидатов-встречи, которых нет в чанках,
        # (б) готовые факты (решения/идеи/задачи) для синтеза без чтения.
        self._progress("discovering", "🕸 Обхожу граф связей от сущностей вопроса…")
        graph_boost, graph_facts = await self._graph_walk(plan)
        plan["graph_boost"] = graph_boost
        if graph_facts:
            self._progress("discovering",
                           f"🕸 Граф: {len(graph_facts)} фактов, "
                           f"{len(graph_boost)} встреч-кандидатов по связям")

        selected = await self._select_meetings(plan)
        total_matched = plan.get("total_matched", len(selected))
        if not selected:
            self.state.update(status="done",
                              report="По плану исследования не нашлось ни одной "
                                     "подходящей встречи. Уточни период или "
                                     "формулировку.",
                              coverage={"matched": 0, "analyzed": 0})
            _save_state(self.user_id, self.research_id, self.state)
            return self.state

        from backend.db.supabase_client import get_supabase_client
        sb = get_supabase_client()

        self._progress("reading",
                       f"📖 Читаю встречи целиком: 0/{len(selected)} "
                       f"(подошло {total_matched}, беру {len(selected)})")
        sem = asyncio.Semaphore(_CONCURRENCY)
        done_cnt = 0
        results: List[Optional[Dict[str, Any]]] = []

        # верхняя половина по уликам читается без гейта; нижняя — через
        # дешёвую проверку по чанкам («обсуждают не то → не читаем»)
        strong_cut = sorted((m.get("_evidence_score", 0) for m in selected),
                            reverse=True)[max(0, len(selected) // 2 - 1)] if selected else 0
        skipped_by_gate = 0
        gate_skipped_meetings: List[Dict[str, Any]] = []   # для приложения
        budget_stopped_meetings: List[Dict[str, Any]] = []
        read_no_findings: List[Dict[str, Any]] = []  # прочитаны, находок нет

        stopped_by_budget = 0

        async def _one(m):
            nonlocal done_cnt, skipped_by_gate, stopped_by_budget
            async with sem:
                if not self._budget_ok():
                    stopped_by_budget += 1
                    budget_stopped_meetings.append(m)
                    done_cnt += 1
                    return None
                if m.get("_evidence_score", 0) < strong_cut:
                    if not await self._worth_full_read(llm, m):
                        skipped_by_gate += 1
                        gate_skipped_meetings.append(m)
                        done_cnt += 1
                        return None
                r = await self._read_meeting(llm, sb, m, plan.get("aspects") or [])
                if r is None:
                    read_no_findings.append(m)
                done_cnt += 1
                if done_cnt % 3 == 0 or done_cnt == len(selected):
                    self._progress("reading",
                                   f"📖 Читаю встречи: {done_cnt}/{len(selected)} "
                                   f"(${self._spent_usd():.3f})")
                return r

        results = list(await asyncio.gather(*(_one(m) for m in selected)))
        findings = [r for r in results if r]
        findings.sort(key=lambda r: r.get("date") or "")

        # ── РЕФЛЕКСИЯ: хватило ли найденного? Обдуманный доп. раунд ──────
        # Система смотрит на находки и решает, надо ли дочитать хвост
        # кандидатов (максимум ОДИН доп. раунд ≤10 встреч, только в бюджете).
        unread = plan.get("unselected") or []
        for _round in range(self.reflection_rounds):
            if not (findings and unread and self._budget_ok()):
                break
            try:
                digest = "\n".join(
                    f"- {f['date']}: {f['findings'][0].get('what', '')[:100]}"
                    for f in findings[:20])
                titles = "\n".join(
                    f"{j}. [{(m.get('date') or '')[:10]}] {m.get('title', '')[:80]}"
                    for j, m in enumerate(unread[:20], 1))
                resp = await llm.generate(
                    prompt=(
                        f"Вопрос: «{self.question}»\nУже найдено:\n{digest}\n\n"
                        f"Непрочитанные встречи-кандидаты:\n{titles}\n\n"
                        "Достаточно ли находок для ПОЛНОГО ответа? Если нет — "
                        "какие номера встреч дочитать (максимум 10)? "
                        'СТРОГО JSON: {"enough": true|false, "read_more": [номера]}'),
                    temperature=0.0, max_tokens=100)
                data = _parse_json_blob(
                    resp.get("text", "") if isinstance(resp, dict) else str(resp))
                extra_nums = ([] if not isinstance(data, dict) or data.get("enough")
                              else [n for n in (data.get("read_more") or [])
                                    if isinstance(n, int) and 1 <= n <= min(20, len(unread))][:10])
                if not extra_nums:
                    break  # находок достаточно — второй раунд не нужен
                extra = [unread[n - 1] for n in sorted(set(extra_nums))]
                self._progress("reading",
                               f"🔁 Рефлексия: дочитываю ещё {len(extra)} встреч…")
                selected = selected + extra
                extra_res = list(await asyncio.gather(*(_one(m) for m in extra)))
                findings += [r for r in extra_res if r]
                findings.sort(key=lambda r: r.get("date") or "")
                unread = [m for m in unread if m not in extra]
            except Exception:
                logger.debug("research reflection skipped", exc_info=True)
                break

        # ── ЧТЕНИЕ ПО СЛЕДУ: находки ссылаются на другие встречи ─────────
        # «Вопрос решим на партнёрской встрече 1 апреля» — сама встреча могла
        # не пройти отбор по ключевым словам. Идём по ссылке: находим встречу
        # по дате (±3 дня) или названию и читаем её ПРИНУДИТЕЛЬНО (без гейта).
        try:
            lead_strs: List[str] = []
            for f in findings:
                lead_strs += f.get("leads") or []
            if lead_strs and self._budget_ok():
                from datetime import date as _date
                read_ids = {m["id"] for m in selected}
                lead_meetings: List[Dict[str, Any]] = []
                for ls in lead_strs[:10]:
                    mdate = re.search(r"\d{4}-\d{2}-\d{2}", ls)
                    for m in plan.get("all_meetings") or []:
                        if m["id"] in read_ids:
                            continue
                        if mdate and (m.get("date") or "")[:10]:
                            try:
                                d1 = _date.fromisoformat(mdate.group(0))
                                d2 = _date.fromisoformat(m["date"][:10])
                                if abs((d1 - d2).days) <= 3:
                                    lead_meetings.append(m)
                            except ValueError:
                                continue
                        elif not mdate and len(ls) >= 6 \
                                and ls.lower() in (m.get("title") or "").lower():
                            lead_meetings.append(m)
                uniq, _seen = [], set()
                for m in lead_meetings:
                    if m["id"] not in _seen:
                        _seen.add(m["id"])
                        uniq.append(m)
                uniq = uniq[:6]
                if uniq:
                    self._progress(
                        "reading",
                        f"🧭 Иду по следу: находки ссылаются на "
                        f"{len(uniq)} встреч — читаю их…")
                    for m in uniq:
                        m["_evidence_score"] = 1e9  # принудительно, без гейта
                    selected = selected + uniq
                    lead_res = list(await asyncio.gather(
                        *(_one(m) for m in uniq)))
                    findings += [r for r in lead_res if r]
                    findings.sort(key=lambda r: r.get("date") or "")
        except Exception:
            logger.debug("research lead-following skipped", exc_info=True)

        self._progress("synthesizing", "🧠 Синтезирую отчёт…")
        chrono = []
        for f in findings:
            for fd in f["findings"]:
                line = f"{f['date']} ({f['title'][:60]}): "
                if fd.get("who"):
                    line += f"{fd['who']} — "
                line += str(fd.get("what") or "")[:220]
                if fd.get("quote"):
                    line += f' | цитата: «{str(fd["quote"])[:140]}»'
                chrono.append(line)
        snap_ctx = self._snapshot_context(plan)
        graph_ctx = "\n".join(graph_facts[:40])

        # ФРАГМЕНТЫ из встреч, которые НЕ читались целиком (гейт/лимит/бюджет):
        # их чанки-улики всё равно идут в синтез как слабые сигналы — так
        # исследование «ничего не теряет», даже не потратившись на полное
        # чтение. В отчёте такие данные помечаются как [фрагмент].
        unread_all = (gate_skipped_meetings + budget_stopped_meetings
                      + (plan.get("unselected") or []))
        chunk_ctx_lines: List[str] = []
        for m in unread_all:
            for sn in (m.get("_snippets") or [])[:2]:
                chunk_ctx_lines.append(
                    f"[фрагмент, {(m.get('date') or '')[:10]}] "
                    f"{m.get('title', '')[:60]}: {sn[:200]}")
            if len(chunk_ctx_lines) >= 30:
                break
        chunk_ctx = "\n".join(chunk_ctx_lines)
        doc_ctx = "\n".join(plan.get("doc_snippets") or [])

        method_ctx = await self._method_brief()
        synth_prompt = (
            method_ctx
            + f"Вопрос исследования: «{self.question}»\n\n"
            "Ниже — ХРОНОЛОГИЧЕСКИЕ находки из полного чтения встреч "
            "(каждая строка: дата, встреча, кто, что, цитата). "
            + (f"Плюс ФАКТЫ ИЗ ГРАФА ЗНАНИЙ (идеи/решения/задачи со связями "
               f"и авторами):\n{graph_ctx}\n\n" if graph_ctx else "")
            + (f"Плюс СНАПШОТЫ сущностей (агрегированное состояние):\n"
               f"{snap_ctx}\n\n" if snap_ctx else "")
            + (f"Плюс ФРАГМЕНТЫ из встреч, прочитанных только частично — "
               f"слабые сигналы, при использовании помечай [фрагмент]:\n"
               f"{chunk_ctx}\n\n" if chunk_ctx else "")
            + (f"Плюс ФРАГМЕНТЫ ДОКУМЕНТОВ из базы знаний (при использовании "
               f"помечай [документ]):\n{doc_ctx}\n\n" if doc_ctx else "")
            + ("ВАЖНО ПРО ЧЕСТНОСТЬ: если ПРЯМОГО ответа на вопрос в данных "
               "нет — скажи это ПЕРВОЙ строкой раздела «Ответ» открытым "
               "текстом («Прямых данных по X в встречах не найдено»), объясни "
               "где искали, а смежные темы вынеси в отдельный раздел "
               "«## Смежные находки (не прямой ответ)» и не выдавай их за "
               "ответ на вопрос.\n\n")
            + ("Ты — бизнес-советник компании. Напиши ответ-рекомендацию на "
               "русском (markdown) СТРОГО в двух разделах:\n"
               "## 📊 Что говорят ваши данные\n"
               "— только факты из находок/графа/снапшотов, с датами и именами.\n"
               "## 💡 Предложения Tessbrain\n"
               "— твои СОБСТВЕННЫЕ идеи и модели (мотивация, управление, "
               "развитие продукта): каждая начинается с «💡» и заканчивается "
               "строкой «_основано на: <какие находки натолкнули>_». Смелые "
               "гипотезы можно — но помечай «⚗️ гипотеза». Не выдавай "
               "предложения за факты.\n\n"
               if self.mode == "advisor" else
               "Ты — исследователь-аналитик компании. Напиши ПОЛНОЦЕННОЕ "
               "исследование на русском (markdown), а не тезисный ответ. "
               "Телеграфный стиль запрещён: каждый раздел — связные абзацы "
               "и развёрнутые пункты. Структура:\n"
               "## Ответ\n— прямой ответ на вопрос, 4-8 предложений с сутью "
               "и главными цифрами/именами.\n"
               "## Обоснование\n— КАЖДЫЙ вывод отдельным развёрнутым пунктом "
               "(3-5 предложений): что установлено, дата и название "
               "встречи-источника, цитата «...» где есть, И ЧТО ЭТО ЗНАЧИТ "
               "для бизнеса (интерпретация, следствия). Вывод без источника "
               "не пиши.\n"
               "## Хронология\n— как тема развивалась по датам: не список "
               "событий, а связный рассказ с переломными моментами.\n"
               "## Выводы и что делать\n— 3-6 практических следствий из "
               "исследования: на что обратить внимание, какие решения "
               "напрашиваются (помечай их как рекомендации, не факты).\n"
               "## Противоречия и пробелы\n— где данные расходятся, чего не "
               "хватает для полной уверенности, что стоит уточнить.\n"
               + ("## Разбор ключевых источников\n— для КАЖДОЙ встречи с "
                  "находками: 2-4 предложения — что именно там прозвучало "
                  "по вопросу, кто говорил, в каком контексте.\n"
                  if self.depth == "deep" else "")
               + "Если вопрос про вклад/проценты — дай ОЦЕНКУ с явной пометкой "
               "«оценка по данным встреч» и обоснованием по находкам. "
               "Факты — только по находкам/графу/снапшотам; интерпретации "
               "и рекомендации разрешены, но отделяй их от фактов.\n\n")
            + "\n".join(chrono[:120])
        )
        try:
            resp = await llm.generate(prompt=synth_prompt, temperature=0.2,
                                      max_tokens=self.synth_tokens)
            report = (resp.get("text", "") if isinstance(resp, dict)
                      else str(resp or "")).strip()
        except Exception as e:
            report = f"Синтез не удался: {e}. Сырые находки сохранены."

        # ── САМОПРОВЕРКА: отчёт против ВСЕХ источников ────────────────────
        # Критично: проверяем не только против находок полного чтения, но и
        # против графа/снапшотов/фрагментов — раньше им передавались только
        # находки, и всё взятое из графа/снапшотов честно помечалось
        # «не подтверждено», хотя источник был.
        all_evidence = "\n".join(chrono[:100])
        if graph_ctx:
            all_evidence += f"\n\nФАКТЫ ГРАФА (валидный источник):\n{graph_ctx}"
        if snap_ctx:
            all_evidence += f"\n\nСНАПШОТЫ (валидный источник):\n{snap_ctx}"
        if chunk_ctx:
            all_evidence += (f"\n\nФРАГМЕНТЫ встреч (валидный источник, "
                             f"слабее полного чтения):\n{chunk_ctx}")
        if report and all_evidence.strip() and self._budget_ok():
            try:
                self._progress("verifying", "🔍 Проверяю отчёт против находок…")
                vresp = await llm.generate(
                    prompt=(
                        "Проверь ОТЧЁТ строго против ИСТОЧНИКОВ (находки "
                        "полного чтения + факты графа + снапшоты + фрагменты — "
                        "ВСЕ валидны). Помечай «[не подтверждено находками]» "
                        "ТОЛЬКО утверждения, которых нет НИ В ОДНОМ источнике."
                        + (" Раздел «💡 Предложения Tessbrain» НЕ проверяй — это "
                           "намеренные идеи системы, их не трогай."
                           if self.mode == "advisor" else
                           " Разделы «Выводы и что делать» и рекомендации не "
                           "проверяй — это намеренные интерпретации.")
                        + " Числа/проценты-оценки оставь, но убедись, "
                        "что помечены как оценка. Верни исправленный отчёт "
                        "целиком (markdown), без комментариев о проверке.\n\n"
                        f"ИСТОЧНИКИ:\n{all_evidence[:12000]}"
                        + f"\n\nОТЧЁТ:\n{report[:9000]}"),
                    temperature=0.0, max_tokens=3800)
                vtext = (vresp.get("text", "") if isinstance(vresp, dict)
                         else str(vresp or "")).strip()
                # защита от деградации: принимаем только сопоставимый по длине
                if vtext and len(vtext) > len(report) * 0.5:
                    report = vtext
            except Exception:
                logger.debug("research verification skipped", exc_info=True)

        # ── ДОКАЗАТЕЛЬНЫЙ ДОЧИТ: спасаем «не подтверждено» через чанки ────
        # Для утверждений, оставшихся без опоры, система САМА ищет
        # подтверждения по BM25-чанкам всего корпуса (вопрос пользователя:
        # «он же может найти данные об этом по графам/связям/чанкам»).
        _unconf_mark = "[не подтверждено находками]"
        if report.count(_unconf_mark) and self._budget_ok():
            try:
                claims = [l.strip() for l in report.split("\n")
                          if _unconf_mark in l][:10]
                self._progress("verifying",
                               f"🔎 Доказательный дочит: ищу подтверждения "
                               f"для {len(claims)} утверждений в чанках…")
                from backend.core.search.bm25_searcher import get_bm25_searcher
                bm25 = get_bm25_searcher(user_id=self.user_id)
                title_by_id = {m["id"]: (m.get("title", ""), (m.get("date") or "")[:10])
                               for m in (plan.get("all_meetings") or [])}
                rescue_chunks: List[str] = []
                for cl in claims:
                    q = cl.replace(_unconf_mark, "")[:200]
                    try:
                        for r in bm25.search(query=q, top_k=3):
                            meta = getattr(r, "metadata", None) or {}
                            mid = str(meta.get("meeting_id") or "")
                            ttl, dte = title_by_id.get(mid, ("", ""))
                            rescue_chunks.append(
                                f"[чанк{', ' + dte if dte else ''}"
                                f"{', ' + ttl[:50] if ttl else ''}] "
                                f"{(getattr(r, 'text', '') or '')[:220]}")
                    except Exception:
                        continue
                if rescue_chunks:
                    rresp = await llm.generate(
                        prompt=(
                            "В ОТЧЁТЕ есть утверждения с пометкой "
                            f"«{_unconf_mark}». Ниже — ЧАНКИ из корпуса встреч, "
                            "найденные по этим утверждениям. Для каждого "
                            "помеченного утверждения реши: если чанк его "
                            "подтверждает — убери пометку и добавь в конце "
                            "«(подтверждено фрагментом: <дата, встреча>)»; "
                            "если нет — оставь пометку как есть. Ничего "
                            "другого не меняй. Верни отчёт целиком.\n\n"
                            f"ЧАНКИ:\n" + "\n".join(rescue_chunks[:30])
                            + f"\n\nОТЧЁТ:\n{report[:9000]}"),
                        temperature=0.0, max_tokens=3800)
                    rtext = (rresp.get("text", "") if isinstance(rresp, dict)
                             else str(rresp or "")).strip()
                    if rtext and len(rtext) > len(report) * 0.5:
                        report = rtext
            except Exception:
                logger.debug("research rescue pass skipped", exc_info=True)

        coverage = {
            "matched": total_matched,
            "selected": len(selected),
            "skipped_by_gate": skipped_by_gate,
            "analyzed": len(findings),
            "empty_or_irrelevant": len(selected) - skipped_by_gate - len(findings),
        }
        coverage["graph_facts"] = len(graph_facts)
        coverage["stopped_by_budget"] = stopped_by_budget
        coverage["cost_usd"] = round(self._spent_usd(), 4)
        _read_total = (len({f["meeting_id"] for f in findings})
                       + len(read_no_findings))
        coverage["read_total"] = _read_total
        report += (f"\n\n---\n📊 Покрытие: подошло встреч — {coverage['matched']}, "
                   f"прочитано целиком — {_read_total} "
                   f"(с находками — {coverage['analyzed']}, остальные "
                   f"обсуждали другое), "
                   f"отсеяно по фрагментам без полного чтения — {skipped_by_gate}, "
                   f"фактов из графа связей — {len(graph_facts)}. "
                   f"💰 Стоимость: ${coverage['cost_usd']:.3f} "
                   f"(бюджет ${self.budget_usd:.2f}).")
        if stopped_by_budget:
            report += (f" ⚠️ {stopped_by_budget} встреч не прочитаны — "
                       f"достигнут бюджет исследования.")
        if total_matched > len(selected):
            report += (f" ⚠️ {total_matched - len(selected)} встреч за пределами "
                       f"лимита {self.max_meetings} не читались.")

        # ПРИЛОЖЕНИЕ «Источники и покрытие» — ответ на «как мы уверены, что
        # ничего не потеряли»: полные списки прочитанного/отсеянного/
        # непрочитанного (сворачиваемые), топ граф-фактов и снапшоты.
        import html as _h

        def _mlist(ms, extra=None):
            items = []
            for m in ms:
                t = _h.escape(f"{(m.get('date') or '')[:10]} — "
                              f"{m.get('title', '')[:90]}")
                if extra:
                    t += f" <span class='apx-note'>{_h.escape(extra(m))}</span>"
                items.append(f"<li>{t}</li>")
            return "<ul>" + "".join(items) + "</ul>" if items else ""

        read_ok = {f["meeting_id"]: len(f["findings"]) for f in findings}
        read_meetings = [m for m in selected if m["id"] in read_ok]
        total_read = len(read_meetings) + len(read_no_findings)
        leftover = (plan.get("unselected") or [])
        apx = ["<hr/><h2>📚 Источники и покрытие</h2>"]
        if read_meetings:
            apx.append(f"<details open><summary>Прочитаны целиком, есть "
                       f"находки — {len(read_meetings)} встреч</summary>"
                       + _mlist(read_meetings,
                                lambda m: f"находок: {read_ok.get(m['id'], 0)}")
                       + "</details>")
        if read_no_findings:
            apx.append(f"<details><summary>Прочитаны целиком, находок по "
                       f"вопросу нет — {len(read_no_findings)} (обсуждали "
                       f"другое)</summary>"
                       + _mlist(read_no_findings) + "</details>")
        if gate_skipped_meetings:
            apx.append(f"<details><summary>Отсеяны по фрагментам без полного "
                       f"чтения — {len(gate_skipped_meetings)} (по чанкам "
                       f"обсуждают не то)</summary>"
                       + _mlist(gate_skipped_meetings) + "</details>")
        if budget_stopped_meetings or leftover:
            apx.append(f"<details><summary>Не читались — "
                       f"{len(budget_stopped_meetings) + len(leftover)} "
                       f"(лимит {self.max_meetings} встреч/бюджет; их фрагменты "
                       f"учтены в синтезе)</summary>"
                       + _mlist(budget_stopped_meetings + leftover)
                       + "</details>")
        if graph_facts:
            gitems = "".join(f"<li>{_h.escape(g[:180])}</li>"
                             for g in graph_facts[:15])
            apx.append(f"<details><summary>Факты из графа знаний — "
                       f"{len(graph_facts)} (топ-15)</summary>"
                       f"<ul>{gitems}</ul></details>")
        if snap_ctx:
            sitems = "".join(f"<li>{_h.escape(s[:200])}</li>"
                             for s in snap_ctx.split("\n")[:9])
            apx.append(f"<details><summary>Снапшоты сущностей — использованы "
                       f"в синтезе</summary><ul>{sitems}</ul></details>")
        if doc_ctx:
            ditems = "".join(f"<li>{_h.escape(d[:200])}</li>"
                             for d in doc_ctx.split("\n")[:12])
            apx.append(f"<details><summary>Документы из базы знаний — "
                       f"фрагменты в синтезе</summary><ul>{ditems}</ul></details>")
        appendix_html = "\n".join(apx)

        badges = [
            f"📖 прочитано целиком {total_read} из {total_matched} подходящих, "
            f"с находками — {len(read_meetings)}",
            f"🕸 граф: {len(graph_facts)} фактов",
            f"📸 снапшоты: {len(snap_ctx.splitlines()) if snap_ctx else 0}",
            f"📄 документы: {len((plan.get('doc_snippets') or []))} фрагм.",
            f"🧩 фрагменты непрочитанных: {len(chunk_ctx_lines)}",
            (f"🔬 глубокое исследование · " if self.depth == "deep" else "")
            + f"💰 ${coverage['cost_usd']:.3f}",
        ]

        # Отчёт — ещё и ДОКУМЕНТОМ (.html: открывается двойным кликом в любом
        # браузере, из браузера печатается в PDF — стили print уже заданы).
        # Отдаётся и по /research/{id}/report для кнопки «Открыть отчёт».
        report_file = ""
        try:
            f = _research_dir(self.user_id) / f"{self.research_id}.html"
            f.write_text(_render_report_html(
                self.question, report,
                self.state.get("started_at", "")[:16].replace("T", " "),
                badges=badges, appendix_html=appendix_html),
                encoding="utf-8")
            report_file = str(f)
        except Exception:
            logger.debug("research html save failed", exc_info=True)

        self.state.update(status="done", report=report, coverage=coverage,
                          findings=findings, report_file=report_file,
                          completed_at=datetime.now(timezone.utc).isoformat())
        _save_state(self.user_id, self.research_id, self.state)

        # УРОЖАЙ для ночной консолидации: подтверждённые находки (дата/
        # встреча/цитата) складываются в research_harvest — ночью, не сейчас,
        # они станут инсайтами (ноль LLM). Токены, потраченные на исследование,
        # не пропадают: система дообучается на собственных отчётах.
        try:
            hdir = Path("data") / "research_harvest" / self.user_id
            hdir.mkdir(parents=True, exist_ok=True)
            harvest = {
                "question": self.question, "mode": self.mode,
                "completed_at": self.state["completed_at"],
                "findings": [
                    {"date": f.get("date"), "title": f.get("title"),
                     "who": fd.get("who"), "what": fd.get("what"),
                     "quote": fd.get("quote")}
                    for f in findings for fd in f.get("findings", [])
                ][:20],
            }
            if harvest["findings"]:
                (hdir / f"{self.research_id}.json").write_text(
                    json.dumps(harvest, ensure_ascii=False, indent=2),
                    encoding="utf-8")
        except Exception:
            logger.debug("research harvest save failed", exc_info=True)

        self._progress("done", "✅ Готово")
        return self.state


_RUNNING: Dict[str, asyncio.Task] = {}
_RUNNING_USER: Dict[str, str] = {}  # research_id → user_id


def start_research(user_id: str, question: str, mode: str = "research",
                   depth: str = "standard") -> str:
    """Запустить исследование фоном; вернуть research_id сразу.

    Продакшн-гард: одно активное исследование на пользователя — повторный
    запуск возвращает id уже идущего (фронт продолжит поллить его)."""
    for rid, uid in list(_RUNNING_USER.items()):
        if uid == user_id and rid in _RUNNING:
            logger.info(f"🔬 Research already running for {user_id[:8]}: {rid}")
            return rid
    r = BatchResearcher(user_id, question, mode=mode, depth=depth)
    _save_state(user_id, r.research_id, r.state)

    async def _wrapped():
        try:
            await r.run()
        except Exception as e:
            logger.error(f"research {r.research_id} crashed: {e}")
            r.state.update(status="failed", error=str(e))
            _save_state(user_id, r.research_id, r.state)
        finally:
            _RUNNING.pop(r.research_id, None)
            _RUNNING_USER.pop(r.research_id, None)

    _RUNNING[r.research_id] = asyncio.get_event_loop().create_task(_wrapped())
    _RUNNING_USER[r.research_id] = user_id
    return r.research_id
