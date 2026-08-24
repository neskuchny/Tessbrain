# -*- coding: utf-8 -*-
"""
Методологии-отчёты — перенос из meetflow report_automation в мозг.

ЧТО ПЕРЕНЕСЕНО (и почему именно так):
- Промпты методологий (methodology_prompts/*.md) — КАК ДАННЫЕ, по нашему же
  принципу artifact_engine (schemas-as-data): правка отчёта = правка .md,
  без кода. Источник: report_automation/prompts/ (automation_audit 185 строк,
  business_dna_map 345, triple_* и др.).
- «Тройной» отчёт (triple): три линзы параллельно (системная теория /
  TSP-самоподобие / wisdom) + human-синтез — доставляется только синтез,
  под-отчёты сохраняются.
- Rolling-память (context_evolution): после каждого отчёта summary мерджится
  в накопленный контекст автоматизации (≤400 слов) и подмешивается в
  следующий прогон — «отчёт помнит, что было раньше».
- Динамический режим: предыдущий summary инжектится с требованием секции
  «Динамика изменений» (↑/↓/→).

ЧТО НЕ ПЕРЕНЕСЕНО: свой HTTP-клиент Gemini (у нас LLMRouter с tier'ами и
fallback'ами), Telegram-доставка (у нас страница инсайтов + опц. бот),
своя БД (per-user JSON-стор через tenant_paths, как у остальных примитивов).

Замечание по бюджету: транскрипт каждой встречи режется до 5000 символов
(как в оригинале) — методологии работают на дешёвой модели (standard tier).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "methodology_prompts")

# Типы отчётов (порт REPORT_TYPES из config.py оригинала)
METHODOLOGIES: Dict[str, Dict[str, str]] = {
    "project_summary": {"name": "Саммари по проекту", "icon": "📊",
                        "description": "Общий обзор прогресса и статуса"},
    "project_progress": {"name": "Движение по проекту", "icon": "📈",
                         "description": "Динамика задач и целей"},
    "key_decisions": {"name": "Ключевые решения", "icon": "✅",
                      "description": "Решения и договорённости со встреч"},
    "employee_analysis": {"name": "Анализ работы сотрудников", "icon": "👥",
                          "description": "Вклад, перегрузка, bus factor"},
    "consultant_analysis": {"name": "Консультант-анализ", "icon": "🎩",
                            "description": "Big-4-style разбор бизнеса"},
    "automation_audit": {"name": "Аудит автоматизации и ИИ", "icon": "🤖",
                         "description": "Индекс зрелости, что автоматизировать срочно, ROI"},
    "business_dna_map": {"name": "DNA-карта бизнеса", "icon": "🧬",
                         "description": "7 слоёв: Identity→Ecosystem→…→Health"},
    "triple": {"name": "Тройной анализ", "icon": "🔺",
               "description": "Системная теория + TSP + Wisdom → human-синтез"},
    "custom": {"name": "Произвольный запрос", "icon": "✍️",
               "description": "Своя формулировка анализа"},
    # Ролевые отчёты: «что произошло за период глазами конкретного директора».
    # Фокус (домены) режет знания по релевантности роли; CEO видит всё.
    "ceo_weekly": {"name": "Неделя глазами CEO", "icon": "🧭",
                   "description": "Вся компания: движение к целям, риски, решения"},
    "marketing_weekly": {"name": "Неделя маркетинга", "icon": "📣",
                         "description": "Кампании, каналы, лиды — глазами директора по маркетингу"},
    "sales_weekly": {"name": "Неделя продаж", "icon": "💼",
                     "description": "Сделки, клиенты, выручка — глазами директора по продажам"},
    "tech_weekly": {"name": "Неделя технологий", "icon": "⚙️",
                    "description": "Разработка, релизы, техдолг — глазами CTO"},
}

# Фокус ролевого отчёта → домены реестра (backend/core/sleep/domain_snapshots).
# Ключевые слова берутся из того же реестра, править можно без кода —
# через config/domain_snapshots.json.
ROLE_FOCUS: Dict[str, List[str]] = {
    "ceo_weekly": [],                       # вся компания
    "marketing_weekly": ["marketing"],
    "sales_weekly": ["sales"],
    "tech_weekly": ["tech"],
}


def _load_prompt(name: str) -> str:
    try:
        with open(os.path.join(_PROMPTS_DIR, f"{name}.md"), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.error("methodology prompt not found: %s", name)
        return ""


def _system_prompt(report_type: str) -> str:
    body = _load_prompt(report_type if report_type in METHODOLOGIES else "custom")
    rules = _load_prompt("formatting_rules")
    return f"{body}\n\n{rules}" if rules else body


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# Стор отчётов: per-user JSON (паттерн остальных примитивов памяти)
# ============================================================================

class ReportStore:
    """Отчёты + rolling-контексты одного пользователя."""

    MAX_REPORTS = 200

    def __init__(self, persist_path: str):
        self._path = persist_path
        self._data: dict = {"reports": [], "contexts": {}}
        self._load()

    def _locked(self, fn):
        from backend.core.store.tenant_io import file_lock
        with file_lock(self._path):
            self._load()
            return fn()

    def add_report(self, report: dict) -> None:
        def _impl():
            self._data["reports"].append(report)
            if len(self._data["reports"]) > self.MAX_REPORTS:
                self._data["reports"] = self._data["reports"][-self.MAX_REPORTS:]
            self._save()
        self._locked(_impl)

    def list_reports(self, *, report_type: Optional[str] = None,
                     limit: int = 50) -> List[dict]:
        """Свежие первыми; полный текст не включается (см. get_report)."""
        out = []
        for r in reversed(self._data["reports"]):
            if report_type and r.get("report_type") != report_type:
                continue
            slim = {k: v for k, v in r.items()
                    if k not in ("content_text", "sub_reports")}
            out.append(slim)
            if len(out) >= limit:
                break
        return out

    def get_report(self, report_id: str) -> Optional[dict]:
        for r in self._data["reports"]:
            if r.get("id") == report_id:
                return r
        return None

    def get_context(self, context_key: str) -> str:
        return str(self._data["contexts"].get(context_key) or "")

    def set_context(self, context_key: str, value: str) -> None:
        def _impl():
            self._data["contexts"][context_key] = value
            self._save()
        self._locked(_impl)

    def _save(self) -> None:
        try:
            from backend.core.store.tenant_io import atomic_write_json
            atomic_write_json(self._path, self._data)
        except Exception:
            logger.warning("ReportStore save failed", exc_info=True)

    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self._data = {"reports": d.get("reports", []),
                                  "contexts": d.get("contexts", {})}
        except Exception:
            self._data = {"reports": [], "contexts": {}}


def report_store_for_user(user_id: str) -> ReportStore:
    from backend.core.store.tenant_paths import _DATA_ROOT, _require_uuid
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return ReportStore(str(d / f"{user_id}.json"))


# ============================================================================
# Границы периода
# ============================================================================
#
# prepare_meetings_context() и MAX_TRANSCRIPT_CHARS=5000 удалены намеренно.
# Они склеивали контекст отчёта из СЫРЫХ транскриптов 25 последних встреч,
# каждый обрезанный до 5000 символов. Это неверно вдвойне: 25 встреч не
# описывают компанию (их сотни), а сырой обрезанный транскрипт выбрасывает
# всё, что мозг из него уже извлёк. Контекст теперь собирает
# report_context.build_company_context() — из знаний и без обрезки.


async def fetch_meetings(user_id: str, *, project_id: Optional[str] = None,
                         folder_id: Optional[str] = None,
                         days_back: int = 30, limit: int = 2000) -> List[Dict]:
    """Встречи периода — для границ периода и счётчиков (без транскриптов).

    Транскрипты здесь больше не нужны: содержательный контекст даёт
    build_company_context(). Тянуть их означало бы качать мегабайты текста
    ради двух дат."""
    from datetime import timedelta

    from backend.db.supabase_client import get_supabase_client
    supabase = get_supabase_client()
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    params = {
        "user_id": f"eq.{user_id}",
        "created_at": f"gte.{since}",
        "select": "id,meeting_id,title,created_at",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if project_id:
        params["project_id"] = f"eq.{project_id}"
    if folder_id:
        params["folder_id"] = f"eq.{folder_id}"
    rows = await supabase._request("GET", "/rest/v1/meetings", params=params)
    return list(rows or [])


# ============================================================================
# Генерация
# ============================================================================

async def _llm(system_prompt: str, user_prompt: str, *, model_tier: str,
               max_tokens: int = 4000, lang: str = "") -> str:
    """lang — язык ЧИТАТЕЛЯ отчёта. Пусто/ru — промпт прежний байт-в-байт.

    Сознательно НЕ применяется к накопленному контексту (context_evolution):
    он копится между прогонами и служит входом для следующих отчётов —
    переключение языка на полпути смешало бы две версии одной памяти."""
    from backend.core.llm.lang import lang_instruction
    from backend.core.llm.router import LLMRouter, ModelTier
    system_prompt = f"{system_prompt}{lang_instruction(lang)}"
    router = LLMRouter()
    tier = ModelTier.PREMIUM if model_tier == "premium" else ModelTier.STANDARD
    return await router.generate(prompt=user_prompt, system_prompt=system_prompt,
                                 model_tier=tier, max_tokens=max_tokens)


def _context_key(report_type: str, project_id: Optional[str],
                 folder_id: Optional[str]) -> str:
    scope = f"{report_type}::{project_id or '*'}::{folder_id or '*'}"
    return hashlib.sha1(scope.encode()).hexdigest()[:16]


async def generate_methodology_report(
    user_id: str,
    report_type: str,
    *,
    project_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    days_back: int = 30,
    custom_prompt: Optional[str] = None,
    dynamic: bool = True,
    model_tier: str = "standard",
    lang: str = "",
) -> Dict[str, Any]:
    """Сгенерировать отчёт по методологии и сохранить в стор.

    dynamic=True: сравнение с предыдущим отчётом того же scope (секция
    «Динамика изменений») + rolling-память (context_evolution).
    """
    if report_type not in METHODOLOGIES:
        raise ValueError(f"unknown report_type: {report_type}")

    store = report_store_for_user(user_id)
    ckey = _context_key(report_type, project_id, folder_id)
    accumulated = store.get_context(ckey)
    prev = store.list_reports(report_type=report_type, limit=5)
    prev_summary = ""
    for p in prev:
        if p.get("context_key") == ckey and p.get("summary"):
            prev_summary = p["summary"]
            break

    # Контекст — из ЗНАНИЙ компании (снапшот + граф + саммари ВСЕХ встреч
    # периода), а не из сырых транскриптов 25 последних встреч, обрезанных до
    # 5000 символов. 25 случайных встреч не описывают компанию, а сырой
    # транскрипт игнорирует всё, что мозг уже из него извлёк. Обрезок нет:
    # избыточный объём сворачивается map-reduce'ом. См. report_context.py.
    from backend.core.reports.report_context import build_company_context
    _ctx = await build_company_context(user_id, days_back=days_back,
                                       project_id=project_id, folder_id=folder_id,
                                       domains=ROLE_FOCUS.get(report_type) or None)
    meetings_ctx = _ctx.get("text") or ""
    ctx_stats = _ctx.get("stats") or {}

    if not meetings_ctx.strip():
        # Знаний нет вовсе — честный отказ вместо отчёта «из головы».
        return {"status": "no_data",
                "message": (f"За {days_back} дн. в выбранном scope нет данных "
                            "для анализа: ни снапшота компании, ни фактов в "
                            "графе, ни саммари встреч.")}

    meetings = await fetch_meetings(user_id, project_id=project_id,
                                    folder_id=folder_id, days_back=days_back)
    dates = [m.get("created_at", "")[:10] for m in meetings if m.get("created_at")]
    period = f"Период: {min(dates)} — {max(dates)}" if dates else f"Период: {days_back} дн."

    if report_type == "triple":
        content, sub_reports = await _generate_triple(
            meetings_ctx, period, accumulated, model_tier, lang)
    else:
        sub_reports = None
        system_prompt = _system_prompt(report_type)
        if accumulated:
            system_prompt += ("\n\nНАКОПЛЕННЫЙ КОНТЕКСТ ПРОШЛЫХ ОТЧЁТОВ "
                              "(rolling-память):\n" + accumulated)
        if dynamic and prev_summary:
            system_prompt = system_prompt.replace("{{dynamic_context}}", f"""
━━━ КОНТЕКСТ ПРЕДЫДУЩЕГО ОТЧЁТА ━━━
{prev_summary}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ВАЖНО: это динамический отчёт. Сравни с предыдущим, добавь раздел
«## 📊 Динамика изменений» (что улучшилось ↑ / ухудшилось ↓ / без изменений →),
выдели новые тренды и оцени: движение вперёд или стагнация.""")
        else:
            system_prompt = system_prompt.replace("{{dynamic_context}}", "")
        if report_type == "custom" and custom_prompt:
            system_prompt = system_prompt.replace("{{custom_prompt}}", custom_prompt)

        _cover = (f"Охват данных: профиль компании "
                  f"{'есть' if ctx_stats.get('has_snapshot') else 'отсутствует'}, "
                  f"фактов из графа знаний: {ctx_stats.get('graph_facts_total', 0)}, "
                  f"встреч в периоде: {ctx_stats.get('meetings_in_period', 0)} "
                  f"(с саммари: {ctx_stats.get('meetings_with_summary', 0)}).")
        user_prompt = (
            "Проанализируй знания компании и создай отчёт.\n\n"
            f"{period}\n{_cover}\n\n"
            "Это НЕ стенограммы: перед тобой то, что система уже извлекла из "
            "всех встреч — профиль компании, датированные решения, достижения "
            "и провалы, цели, риски, KPI и саммари встреч периода. Опирайся на "
            "эти факты; если чего-то нет — так и скажи, не додумывай.\n\n"
            f"ЗНАНИЯ О КОМПАНИИ:\n{meetings_ctx}\n\n"
            "Создай структурированный отчёт согласно инструкциям.")
        content = await _llm(system_prompt, user_prompt,
                             model_tier=model_tier, lang=lang)

    # Арифметический аудит СОБСТВЕННЫХ расчётов отчёта. LLM только выписывает
    # заявленные вычисления с цитатой — пересчитывает их КОД (допуск 1%).
    # Без этого «отчёт уровня McKinsey» мог сложить 40+35+23 и написать «итого
    # 120», и никто бы не заметил.
    numeric_audit: Optional[Dict[str, Any]] = None
    try:
        from backend.core.analysis.numeric_audit import audit_text, format_audit
        from backend.core.llm.router import LLMRouter
        numeric_audit = await audit_text(content, LLMRouter().generate_json)
        block = format_audit(numeric_audit)
        if numeric_audit.get("mismatches"):
            content += ("\n\n---\n\n## ⚠️ Проверка расчётов\n\n"
                        "Числа ниже пересчитаны кодом, а не моделью.\n\n" + block)
            logger.warning("отчёт %s: расхождений в расчётах — %d",
                           report_type, len(numeric_audit["mismatches"]))
    except Exception:
        logger.warning("numeric audit пропущен", exc_info=True)

    # Summary (для динамики следующего отчёта + истории)
    summary = ""
    try:
        summary = await _llm(
            "Создай краткое summary (5-7 предложений) основных выводов отчёта. "
            "Ключевые факты, числа, решения, тренды. Без воды.",
            content, model_tier="standard", max_tokens=500, lang=lang)
    except Exception:
        # Обрубок content[:500] здесь резал отчёт посреди слова и выдавал это
        # за summary. Берём целые первые абзацы — короткие, но не рваные.
        logger.warning("summary LLM failed — берём вводные абзацы", exc_info=True)
        _acc: List[str] = []
        for _para in content.split("\n\n"):
            if not _para.strip():
                continue
            _acc.append(_para.strip())
            if sum(len(p) for p in _acc) >= 400:
                break
        summary = "\n\n".join(_acc)

    # Rolling-память (context_evolution)
    try:
        evo_system = _load_prompt("context_evolution")
        if evo_system and summary:
            new_ctx = await _llm(
                evo_system,
                f"ТЕКУЩИЙ КОНТЕКСТ:\n{accumulated or '(пусто — первый отчёт)'}\n\n"
                f"SUMMARY НОВОГО ОТЧЁТА:\n{summary}\n\n"
                "Создай ОБНОВЛЁННЫЙ контекст (макс 400 слов).",
                model_tier="standard", max_tokens=800)
            if new_ctx:
                store.set_context(ckey, new_ctx)
    except Exception:
        logger.warning("context evolution failed", exc_info=True)

    report = {
        "id": str(uuid.uuid4()),
        "report_type": report_type,
        "title": METHODOLOGIES[report_type]["name"],
        "icon": METHODOLOGIES[report_type]["icon"],
        "content_text": content,
        "summary": summary,
        "sub_reports": sub_reports,
        "context_key": ckey,
        "scope": {"project_id": project_id, "folder_id": folder_id,
                  "days_back": days_back},
        "meetings_count": len(meetings),
        "context_stats": ctx_stats,      # честный охват: откуда взялись данные
        "numeric_audit": numeric_audit,  # что пересчитал код
        "dynamic": dynamic,
        "model_tier": model_tier,
        "created_at": _now_iso(),
    }
    store.add_report(report)
    return {"status": "success", "report": report}


async def _generate_triple(meetings_ctx: str, period: str,
                           accumulated: str, model_tier: str,
                           lang: str = ""):
    """Тройной отчёт: 3 линзы → human-синтез (порт generate_triple_report)."""
    import asyncio
    lenses = {
        "systems": _load_prompt("triple_systems"),
        "tsp": _load_prompt("triple_tsp"),
        "wisdom": _load_prompt("triple_wisdom"),
    }
    base_user = (f"{period}\n\n"
                 "Это НЕ стенограммы: перед тобой то, что система уже извлекла "
                 "из всех встреч — профиль компании, датированные решения, "
                 "достижения и провалы, цели, риски, KPI и саммари встреч.\n\n"
                 f"ЗНАНИЯ О КОМПАНИИ:\n{meetings_ctx}\n\n"
                 "Проведи анализ согласно своей методологии.")

    async def _one(name: str, sys_p: str) -> tuple:
        sp = sys_p + (f"\n\nНАКОПЛЕННЫЙ КОНТЕКСТ:\n{accumulated}" if accumulated else "")
        try:
            return name, await _llm(sp, base_user,
                                    model_tier=model_tier, lang=lang)
        except Exception as e:
            logger.warning("triple lens %s failed: %s", name, e)
            return name, ""

    results = dict(await asyncio.gather(*[_one(n, p) for n, p in lenses.items()]))
    human_sys = _load_prompt("triple_human")
    combined = "\n\n".join(
        f"=== АНАЛИЗ «{n.upper()}» ===\n{t}" for n, t in results.items() if t)
    content = await _llm(
        human_sys or "Синтезируй три анализа в один связный человеческий отчёт (~700 слов).",
        combined or base_user, model_tier=model_tier, lang=lang)
    return content, results
