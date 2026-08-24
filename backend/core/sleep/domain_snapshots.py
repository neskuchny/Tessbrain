# -*- coding: utf-8 -*-
"""Доменные снапшоты — живая картина ФУНКЦИИ компании (маркетинг, продажи,
финансы...), а не узла-отдела из графа (отделы в графе шумные — 162 записи
у реального клиента, большинство мусор экстракции; функциональных доменов
конечное число, и они не зависят от чистоты графа).

Дисциплина памяти та же, что везде: ДНЁМ агенты пишут наработки в
Knowledge(category=<домен>) — это буфер; НОЧЬЮ (шаг консолидации 2.85)
буфер + профильные решения из встреч трансформируются в снапшот: что в
работе, договорённости, что сработало/нет, хроника, открытые вопросы.
«Обучение — это и разделение»: эхо задач в снапшот не попадает; «память —
это и забывание»: устаревшее LLM сворачивает в хронику или выбрасывает.

Потребители:
- агент домена читает СВОЙ снапшот целиком в контекст каждой задачи
  (Mark → marketing, см. `_build_mark_company_context`);
- смежные агенты читают ЧУЖИЕ снапшоты кратко (0 LLM — файлы уже сварены);
- шина данных может открыть снапшот домена наружу как срез;
- модуль любопытства видит пробелы («в продажах нет цифр за квартал»).

Гард чека: 1 LLM-вызов на домен за ночь и ТОЛЬКО при новых данных;
домен без материала не строится вовсе («для всех, где есть жизнь»).

Реестр доменов правится без кода: `config/domain_snapshots.json`
(поверх DEFAULT_DOMAINS, тот же паттерн, что model_pricing.json).
Добавить агенту новый домен = добавить запись реестра.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ── Реестр доменов ─────────────────────────────────────────────────────────
# buffer_category — категория Knowledge-узлов дневного буфера (агент домена
#   пишет туда свои наработки; у Mark это "marketing").
# keywords — отбор профильных решений из встреч (Decision-узлы).
# sections — СТРОГИЕ разделы markdown-снапшота (структура ответа LLM).
DEFAULT_DOMAINS: Dict[str, Dict[str, Any]] = {
    "marketing": {
        "title": "Маркетинг",
        "buffer_category": "marketing",
        "keywords": ["маркет", "реклам", "продвиж", "кампан", "контент",
                     "smm", "лид", "воронк", "партнер", "партнёр", "бренд",
                     "таргет", "seo", "конкурент", "пиар", "pr "],
        "sections": ["Сейчас в работе", "Каналы и активности",
                     "Партнёрства и договорённости",
                     "Что сработало / не сработало",
                     "Хроника (с датами)", "Открытые вопросы"],
    },
    "sales": {
        "title": "Продажи",
        "buffer_category": "sales",
        "keywords": ["продаж", "сделк", "клиент", "лид", "воронк", "оффер",
                     "кп ", "коммерческое предложение", "договор", "контракт",
                     "оплат", "чек", "выручк", "скидк", "тариф"],
        "sections": ["Сейчас в работе", "Сделки и клиенты",
                     "Что закрывается / что буксует",
                     "Возражения и уроки",
                     "Хроника (с датами)", "Открытые вопросы"],
    },
    "finance": {
        "title": "Финансы",
        "buffer_category": "finance",
        "keywords": ["финанс", "бюджет", "выручк", "прибыл", "расход",
                     "оплат", "инвест", "кассов", "рентабельн",
                     "себестоимост", "дебиторк", "маржа"],
        "sections": ["Ключевые цифры", "Сейчас в работе",
                     "Решения по деньгам", "Риски",
                     "Хроника (с датами)", "Открытые вопросы"],
        # финансам подмешиваются цифры KPI из снапшота компании
        "include_company_kpis": True,
    },
    "tech": {
        "title": "Технологии и продукт",
        "buffer_category": "tech",
        "keywords": ["разработ", "техническ", "архитектур", "баг", "релиз",
                     "деплой", "интеграц", "api", "инфраструктур", "продукт",
                     "фич", "mvp", "тестир", "код", "сервер", "безопасн"],
        "sections": ["Сейчас в работе", "Релизы и изменения",
                     "Технические решения", "Долги и риски",
                     "Хроника (с датами)", "Открытые вопросы"],
    },
}

_CONFIG_FILE = Path("config") / "domain_snapshots.json"


def load_domains() -> Dict[str, Dict[str, Any]]:
    """Реестр: дефолты + переопределения/новые домены из config-файла."""
    domains = {k: dict(v) for k, v in DEFAULT_DOMAINS.items()}
    try:
        if _CONFIG_FILE.exists():
            override = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(override, dict):
                for key, cfg in override.items():
                    if not isinstance(cfg, dict):
                        continue
                    base = domains.get(key, {})
                    base.update(cfg)
                    domains[key] = base
    except Exception:
        logger.debug("domain_snapshots config skipped", exc_info=True)
    return domains


def snapshot_path(user_id: str, domain: str) -> Path:
    return Path("data") / "domain_snapshots" / str(user_id) / f"{domain}.json"


def read_domain_snapshot(user_id: str, domain: str,
                         max_chars: int = 2500) -> str:
    """Markdown снапшота домена для контекста агента (0 LLM, чтение файла).
    Fallback на старый путь маркетинга (data/marketing_snapshots/<uid>.json)
    — история, наработанная до реестра, не теряется."""
    if not user_id:
        return ""
    for path in (snapshot_path(user_id, domain),
                 (Path("data") / "marketing_snapshots" / f"{user_id}.json")
                 if domain == "marketing" else None):
        if path is None or not path.exists():
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            md = str(d.get("markdown") or "").strip()
            if md:
                return md[:max_chars]
        except Exception:
            continue
    return ""


def _quality_buffer(nodes: list, category: str) -> list:
    """Наработки домена из Knowledge-буфера с фильтром качества (эхо
    обогащённых задач и короткие реплики диалога — не материал)."""
    works = []
    for n in nodes or []:
        if not isinstance(n, dict) or (n.get("category") or "") != category:
            continue
        content = str(n.get("content") or "").strip()
        lowc = content[:300].lower()
        if (len(content) < 200 or lowc.startswith("[контекст")
                or "[задача]" in lowc):
            continue
        works.append(n)
    works.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    return works


def _company_kpi_material(user_id: str) -> str:
    """Цифры KPI из ГОТОВОГО снапшота компании (чтение файла, 0 LLM)."""
    try:
        from backend.core.store.tenant_paths import snapshots_dir_for_user
        comp = snapshots_dir_for_user(user_id) / "company" / "company.json"
        if not comp.exists():
            return ""
        c = json.loads(comp.read_text(encoding="utf-8"))
        kpis = (c.get("financial_kpis") or []) + (c.get("kpis") or [])
        rows = []
        for k in kpis[:12]:
            if isinstance(k, dict) and k.get("name"):
                rows.append(f"{k.get('name')}={k.get('current_value')}")
        return "; ".join(rows)
    except Exception:
        return ""


async def update_domain_snapshots(user_id: str) -> Dict[str, Any]:
    """Ночное обновление снапшотов ВСЕХ доменов реестра, где есть материал.

    Граф читается ОДИН раз на все домены (Knowledge + Decision), дальше —
    фильтры по домену и максимум 1 LLM-вызов на домен (гейт «есть новое»).
    """
    stats: Dict[str, Any] = {"updated": 0, "domains": []}
    domains = load_domains()
    if not domains:
        return stats
    try:
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            nodes = await gb.find_nodes_by_label(
                "Knowledge", limit=300, tenant_id=user_id, strict_tenant=True)
            decisions = await gb.find_nodes_by_label(
                "Decision", limit=400, tenant_id=user_id, strict_tenant=True)
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
    except Exception:
        logger.debug("domain snapshots: graph unavailable", exc_info=True)
        return stats

    from backend.core.llm import get_llm_router
    llm = get_llm_router()
    if llm is None:
        return stats

    for domain, cfg in domains.items():
        try:
            updated = await _update_one_domain(
                user_id, domain, cfg, nodes or [], decisions or [], llm)
            if updated:
                stats["updated"] += 1
                stats["domains"].append(domain)
        except Exception:
            logger.debug(f"domain snapshot {domain} failed", exc_info=True)
    return stats


async def _update_one_domain(user_id: str, domain: str, cfg: Dict[str, Any],
                             nodes: list, decisions: list, llm) -> bool:
    keywords = tuple(cfg.get("keywords") or ())
    title = str(cfg.get("title") or domain)

    def _is_domain(n: Dict[str, Any]) -> bool:
        t = (f"{n.get('name', '')} "
             f"{str(n.get('content') or n.get('description') or '')[:300]}"
             ).lower()
        return any(k in t for k in keywords)

    works = _quality_buffer(nodes, str(cfg.get("buffer_category") or domain))
    dom_decisions = [d for d in decisions
                     if isinstance(d, dict) and _is_domain(d)]
    dom_decisions.sort(key=lambda a: a.get("created_at") or "", reverse=True)

    kpi_line = _company_kpi_material(user_id) if cfg.get(
        "include_company_kpis") else ""

    # домен без жизни не строится вовсе
    if not works and not dom_decisions:
        return False

    sf = snapshot_path(user_id, domain)
    prev: Dict[str, Any] = {}
    if sf.exists():
        try:
            prev = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    elif domain == "marketing":
        # миграция со старого пути — прежний снапшот не теряем
        old = Path("data") / "marketing_snapshots" / f"{user_id}.json"
        if old.exists():
            try:
                prev = json.loads(old.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
    last_upd = str(prev.get("updated_at") or "")

    # новые данные с прошлого обновления? нет → не тратим вызов
    if last_upd:
        def _newer(items):
            return any((i.get("created_at") or "") > last_upd for i in items)
        if not _newer(works) and not _newer(dom_decisions):
            return False

    material = []
    if kpi_line:
        material.append(f"[KPI компании (актуальные цифры)] {kpi_line}")
    for w in works[:12]:
        material.append(
            f"[наработка агента, {str(w.get('created_at') or '')[:10]}] "
            f"{w.get('name', '')}: {str(w.get('content'))[:600]}")
    for d in dom_decisions[:15]:
        material.append(
            f"[решение из встречи, {str(d.get('created_at') or '')[:10]}] "
            f"{d.get('name', '')}: "
            f"{str(d.get('description') or d.get('content') or '')[:300]}")

    sections = [str(s) for s in (cfg.get("sections") or []) if str(s).strip()]
    sections_md = "\n".join(f"## {s}" for s in sections) or "## Сводка"
    prev_md = str(prev.get("markdown") or "")[:3000]

    from backend.core.llm.usage_tracker import UsageContext
    async with UsageContext(request_type=f"domain_snapshot_{domain}"):
        resp = await llm.generate(
            prompt=(
                f"Ты — хроникёр направления «{title}» компании. Обнови "
                f"СНАПШОТ ДОМЕНА — живую картину «что происходит в "
                f"направлении сейчас и какая была история». Только факты из "
                "материала, ничего не выдумывай. Устаревшее из прежнего "
                "снапшота сворачивай в строку хроники или выбрасывай.\n\n"
                f"ПРЕЖНИЙ СНАПШОТ:\n{prev_md or '—'}\n\n"
                "НОВЫЙ МАТЕРИАЛ (наработки агентов и решения из встреч):\n"
                + "\n".join(material)[:9000] +
                "\n\nВерни markdown ≤2500 символов СТРОГО с разделами:\n"
                + sections_md),
            temperature=0.2, max_tokens=1600)
    md = (resp.get("text", "") if isinstance(resp, dict)
          else str(resp or "")).strip()
    if len(md) < 200:
        return False

    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({
        "domain": domain,
        "title": title,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "markdown": md[:6000],
        "sources": {"buffer": len(works), "decisions": len(dom_decisions),
                    "kpis": bool(kpi_line)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"  📈 Снапшот домена «{title}» обновлён "
                f"({len(works)} наработок, {len(dom_decisions)} решений)")
    return True
