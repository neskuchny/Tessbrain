# -*- coding: utf-8 -*-
"""
Интеграция с CallInsight Pro (звонки/чаты колл-центра) —
docs/CALLINSIGHT_INTEGRATION.md. Домен customer_comms отделён от
внутренней памяти: транскрипты/аудио НЕ копируются, supersede store не
трогается. У нас живут только:
1) события их webhooks (сырой лог + проекция в insights/event_log с
   provenance/_source — их AI-выводы помечены llm_narrative и не
   становятся ground truth);
2) live-карточка клиента (snapshot/timeline тянутся их REST в момент
   показа; их API недоступен → честное «данные недоступны», не пустота).

Дедупликация: их конверт несёт event_id (uuid), ретраи при 5xx — повтор
не проецируется второй раз (UNIQUE(source, event_id), миграция 260).

Identity получателя артефактов (в чей event_log/insights класть):
1) data.agent_email → external_identity_mapping('callinsight', email);
2) иначе орг-уровень: mapping('callinsight', 'org:<meetflow_org_id>') —
   owner проставляет в админке «Daily Pulse — доступы» один раз.
Нет получателя → только сырой лог (как у Daily Pulse).

Чистые функции (подпись, нормализация телефона, проектор) тестируются
без БД и сети. БД/HTTP-слой best-effort.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

EVENT_KINDS = (
    "deal_at_risk",
    "complaint_detected",
    "coach_recommendation_created",
    "coach_recommendation_outcome",
    "top_objection_emerged",
    "digest_daily",
)


# ──────────────────────────────────────────────────────────────────────
# Подпись и телефон (чистые функции)
# ──────────────────────────────────────────────────────────────────────

def verify_signature(secret: Optional[str], raw_body: bytes,
                     header: Optional[str]) -> bool:
    """X-CIP-Signature: hex(HMAC-SHA256(secret, body)). Принимаем и их
    голый hex, и формат `sha256=<hex>` — на случай унификации."""
    if not secret or not header:
        return False
    try:
        expected = hmac.new(str(secret).encode("utf-8"), raw_body,
                            hashlib.sha256).hexdigest()
        got = str(header).strip()
        if got.lower().startswith("sha256="):
            got = got[7:]
        return hmac.compare_digest(expected, got.lower())
    except Exception:
        return False


def secret_from_env() -> Optional[str]:
    return os.environ.get("CALLINSIGHT_WEBHOOK_SECRET") or None


_DIGITS = re.compile(r"\D+")


def normalize_phone(value: Any) -> Optional[str]:
    """Общий ключ матчинга клиента: все нецифры вырезать, последние
    10 цифр (контракт с CallInsight, их §4). Меньше 5 цифр → None
    (мусор, а не телефон)."""
    digits = _DIGITS.sub("", str(value or ""))
    if len(digits) < 5:
        return None
    return digits[-10:]


# ──────────────────────────────────────────────────────────────────────
# Проектор: их конверт → наши примитивы (без сайд-эффектов)
# ──────────────────────────────────────────────────────────────────────

def _stable_id(*parts) -> str:
    canon = "|".join(str(p) for p in parts)
    return hashlib.sha1(canon.encode("utf-8", errors="ignore")).hexdigest()[:16]


_SOURCE_BY_EVENT = {
    # маркер жалобы нашёл их LLM в транскрипте — нарратив
    "complaint_detected": "llm_narrative",
    # скоринг риска: LLM-оценка поверх метрик
    "deal_at_risk": "mixed",
    "coach_recommendation_created": "llm_narrative",
    # рефлексия закрыта по факту движения метрики
    "coach_recommendation_outcome": "measured",
    # подсчёт упоминаний возражения
    "top_objection_emerged": "measured",
    "digest_daily": "mixed",
}


def _provenance(event: str, envelope: dict) -> dict:
    return {
        "source": "callinsight",
        "domain": "customer_comms",
        "event": event,
        "event_id": str(envelope.get("event_id") or ""),
        "_source": _SOURCE_BY_EVENT.get(event, "llm_narrative"),
        "received_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
    }


def project_event(event: str, envelope: dict) -> Dict[str, Any]:
    """Конверт CallInsight (§5.3 их дока) → {events, insights, errors}.
    insight_id детерминирован от event_id — ретрай не задвоит."""
    out: Dict[str, Any] = {"events": [], "insights": [], "errors": []}
    if event not in EVENT_KINDS:
        out["errors"].append(f"event {event!r} вне известных {EVENT_KINDS}")
        return out
    prov = _provenance(event, envelope)
    data = envelope.get("data") or {}
    links = envelope.get("links") or {}
    at = str(envelope.get("occurred_at") or "")
    eid = str(envelope.get("event_id") or "")
    phone = normalize_phone(data.get("customer_phone"))
    agent = str(data.get("agent") or "")
    deep_link = links.get("ui")

    def _ev(kind: str, payload: dict) -> None:
        out["events"].append({
            "actor": agent or "callcenter", "kind": kind, "at": at,
            "payload": {**payload, "customer_phone": phone,
                        "deep_link": deep_link, "provenance": prov}})

    if event == "complaint_detected":
        _ev("cc_complaint", {
            "call_id": data.get("call_id"),
            "marker_phrase": data.get("marker_phrase"),
            "excerpt": data.get("transcript_excerpt")})
        out["insights"].append({
            "insight_id": _stable_id("cip", eid or ("complaint",
                                     data.get("call_id"), at)),
            "title": f"Жалоба клиента в звонке ({agent or 'КЦ'})",
            "description": (str(data.get("marker_phrase") or "") + " — «" +
                            str(data.get("transcript_excerpt") or "")[:300]
                            + "»"),
            "priority": "high",
            "source": "callinsight",
            "customer_phone": phone,
            "meta": {"deep_link": deep_link, "call_id": data.get("call_id")},
            "provenance": prov,
        })
    elif event == "deal_at_risk":
        _ev("cc_deal_at_risk", {
            "call_id": data.get("call_id"),
            "priority": data.get("priority"),
            "key_insight": data.get("key_insight")})
        their_pri = str(data.get("priority") or "").lower()
        out["insights"].append({
            "insight_id": _stable_id("cip", eid or ("at_risk",
                                     data.get("call_id"), at)),
            "title": f"Сделка к спасению: клиент {phone or '?'}",
            "description": str(data.get("key_insight") or ""),
            "priority": "high" if their_pri in ("high", "critical", "1")
                        else "medium",
            "source": "callinsight",
            "customer_phone": phone,
            "meta": {"deep_link": deep_link, "agent": agent},
            "provenance": prov,
        })
    elif event == "coach_recommendation_created":
        target = data.get("target") or {}
        _ev("cc_coach_rec", {
            "rec_id": data.get("rec_id"),
            "title": data.get("title"),
            "focus_metric": data.get("focus_metric"),
            "target": target})
        out["insights"].append({
            "insight_id": _stable_id("cip", eid or ("coach",
                                     data.get("rec_id"))),
            "title": str(data.get("title") or "Совет coach (CallInsight)"),
            "description": str(data.get("body_md") or ""),
            "priority": "medium",
            "source": "callinsight",
            "insight_type": "coaching",
            "meta": {"deep_link": deep_link,
                     "rec_id": data.get("rec_id"),
                     "focus_metric": data.get("focus_metric"),
                     "target": target},
            "provenance": prov,
        })
    elif event == "coach_recommendation_outcome":
        # measured-замыкание петли: задачу/совет закрыл факт метрики
        _ev("cc_coach_outcome", {
            "rec_id": data.get("rec_id"),
            "verdict": data.get("verdict"),
            "metric_before": data.get("metric_before"),
            "metric_after": data.get("metric_after")})
    elif event == "top_objection_emerged":
        # v2 (2026-06-14): событие теперь реально эмитится и несёт mentions_prev
        # — авторитетная дельта от источника (посчитана по ISO-неделе).
        # event_id детерминирован по неделе → дневные повторы дедупятся на
        # ingest (store_raw_event_dedup). Это «закрывает» мост 4а (дельта).
        prev = data.get("mentions_prev")
        _ev("cc_objection", {
            "objection": data.get("objection_name"),
            "mentions": data.get("mentions_count"),
            "mentions_prev": prev,
            "sample_call_ids": data.get("sample_call_ids")})
        prev_txt = f" (было {prev})" if prev is not None else ""
        out["insights"].append({
            "insight_id": _stable_id("cip", eid or ("objection",
                                     data.get("objection_name"))),
            "title": f"Топ-возражение «{data.get('objection_name', '?')}» "
                     f"набирает обороты",
            "description": (f"Упоминаний: {data.get('mentions_count', '?')}"
                            f"{prev_txt}. Сигнал для продукта/маркетинга."),
            "priority": "medium",
            "source": "callinsight",
            "meta": {"deep_link": deep_link,
                     "mentions_prev": prev,
                     "sample_call_ids": data.get("sample_call_ids")},
            "provenance": prov,
        })
    elif event == "digest_daily":
        _ev("cc_digest", {"digest": data})
    return out


# ──────────────────────────────────────────────────────────────────────
# БД-слой: сырой лог с дедупом по event_id (best-effort)
# ──────────────────────────────────────────────────────────────────────

async def store_raw_event_dedup(event: str, envelope: dict, *,
                                signature_ok: bool) -> bool:
    """Записать сырое в integration_events. Возвращает True, если запись
    новая (проецировать), False — дубликат по event_id (ретрай их
    воркера, проекцию пропустить). Нет БД → True (best-effort: лучше
    обработать, чем потерять)."""
    event_id = str(envelope.get("event_id") or "") or None
    try:
        import json as _json

        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("INSERT INTO integration_events "
                     "(source, event, org_ref, team_ref, user_ref, "
                     "occurred_at, payload, signature_ok, event_id) VALUES "
                     "('callinsight', :e, :org, '', :u, "
                     "CAST(:t AS timestamptz), CAST(:p AS jsonb), :sig, :eid) "
                     "ON CONFLICT (source, event_id) WHERE event_id IS NOT NULL "
                     "DO NOTHING"),
                {"e": event,
                 "org": str(envelope.get("meetflow_org_id")
                            or envelope.get("tenant_id") or ""),
                 "u": str((envelope.get("data") or {}).get("agent") or ""),
                 "t": envelope.get("occurred_at") or
                      datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "p": _json.dumps(envelope, ensure_ascii=False),
                 "sig": bool(signature_ok), "eid": event_id})
            return res.rowcount > 0
    except Exception as e:
        logger.debug(f"store_raw_event_dedup skipped: {e}")
        return True


async def resolve_receiver(envelope: dict) -> Optional[str]:
    """В чей event_log/insights класть проекцию: agent_email →
    'org:<meetflow_org_id>' (owner проставляет маппинг в админке)."""
    from backend.core.integrations.identity import get_mapping, is_email
    data = envelope.get("data") or {}
    email = str(data.get("agent_email") or "").strip().lower()
    if email and is_email(email):
        m = await get_mapping("callinsight", email)
        if m:
            return str(m["tessent_user_id"])
    org = str(envelope.get("meetflow_org_id")
              or envelope.get("tenant_id") or "").strip()
    if org:
        m = await get_mapping("callinsight", f"org:{org}")
        if m:
            return str(m["tessent_user_id"])
    return None


# ──────────────────────────────────────────────────────────────────────
# Live-карточка клиента: их REST (snapshot/timeline), best-effort
# ──────────────────────────────────────────────────────────────────────

def _api_base() -> Optional[str]:
    # CallInsight живёт на той же платформе (synlabs), что и MeetFlow, и
    # логинится через MeetFlow. База: своя → calls → как у MeetFlow.
    return ((os.environ.get("CALLINSIGHT_API_URL")
             or os.environ.get("CALLS_API_URL")
             or os.environ.get("MEETFLOW_API_URL")
             or "").rstrip("/")) or None


def _ci_auth_headers() -> Optional[Dict[str, str]]:
    """Заголовки авторизации для CallInsight. CallInsight логинится через ТОТ
    ЖЕ MeetFlow, поэтому при отсутствии собственного ключа используем
    MeetFlow-токен (Bearer) — единый логин платформы. Нет ни того, ни
    другого → None (вызов честно вернёт «не настроено»)."""
    ci_key = os.environ.get("CALLINSIGHT_API_KEY")
    if ci_key:
        return {"X-API-Key": ci_key}
    mf_key = os.environ.get("MEETFLOW_API_KEY")
    if mf_key:
        return {"Authorization": f"Bearer {mf_key}"}
    return None


async def fetch_customer_card(phone: str, *, fetch=None) -> dict:
    """Snapshot + timeline из CallInsight. Невидимое ≠ несуществующее:
    недоступный API → {"available": false, "error": ...} — UI обязан
    показать «данные звонков недоступны», а не пустую карточку.

    fetch — инжекция для тестов: async (url, headers) → (status, json)."""
    norm = normalize_phone(phone)
    if not norm:
        return {"available": False, "error": "невалидный телефон",
                "phone_norm": None}
    base = _api_base()
    headers = _ci_auth_headers()
    if not base or not headers:
        return {"available": False, "phone_norm": norm,
                "error": "API звонков не настроен (нужен CALLINSIGHT_API_URL "
                         "или MEETFLOW_API_URL + ключ CALLINSIGHT_API_KEY / "
                         "MEETFLOW_API_KEY)"}
    if fetch is None:
        async def fetch(url, headers):
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=headers)
                return r.status_code, (r.json() if r.content else None)
    out: Dict[str, Any] = {"available": True, "phone_norm": norm,
                           "snapshot": None, "timeline": []}
    try:
        st, body = await fetch(f"{base}/api/customers/{norm}/snapshot",
                               headers)
        if st == 200:
            out["snapshot"] = body
        elif st == 404:
            out["snapshot"] = None      # клиента у них нет — это факт
        else:
            out["available"] = False
            out["error"] = f"snapshot HTTP {st}"
            return out
        st, body = await fetch(f"{base}/api/customers/{norm}/timeline",
                               headers)
        if st == 200:
            out["timeline"] = body or []
    except Exception as e:
        return {"available": False, "phone_norm": norm, "error": str(e)}
    return out


# ──────────────────────────────────────────────────────────────────────
# CallInsight API v4 (API_CHANGES_v4.md): биллинг, дневной отчёт,
# AI-поиск клиентов, лаборатория персон. Единый HTTP-хелпер с обработкой
# нового 402 quota_exceeded («интеграциям стоит обрабатывать 402
# наравне с 429») — сообщение честно объясняет тариф и остаток.
# ──────────────────────────────────────────────────────────────────────

def _quota_error(body) -> str:
    d = body if isinstance(body, dict) else {}
    reason = {"no_plan": "тариф не подключён",
              "period_expired": "период тарифа истёк",
              "minutes_exceeded": "минуты тарифа исчерпаны"}.get(
        str(d.get("reason") or ""), "квота исчерпана")
    used, limit = d.get("used"), d.get("limit")
    tail = f" ({used}/{limit} мин, план {d.get('plan')})" if used is not None else ""
    return f"CallInsight: {reason}{tail} — пополните пакет минут (вкладка Биллинг)"


async def _ci_request(method: str, path: str, *, json_body=None,
                      params=None, timeout: float = 25.0) -> dict:
    """GET/POST к CallInsight. Возвращает {"ok": bool, "data"|"error"}."""
    base = _api_base()
    headers = _ci_auth_headers()
    if not base or not headers:
        return {"ok": False,
                "error": ("API звонков не настроен (CALLINSIGHT_API_URL/"
                          "CALLS_API_URL + ключ CALLINSIGHT_API_KEY или "
                          "MEETFLOW_API_KEY)")}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, f"{base}{path}",
                                     headers=headers, json=json_body,
                                     params=params)
        body = r.json() if r.content else None
        if r.status_code == 402:
            return {"ok": False, "error": _quota_error(body), "status": 402}
        if r.status_code == 429:
            return {"ok": False, "status": 429,
                    "error": "CallInsight: слишком много запросов — повторите позже"}
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code,
                    "error": f"CallInsight HTTP {r.status_code}: {str(body)[:200]}"}
        return {"ok": True, "data": body}
    except Exception as e:
        return {"ok": False, "error": f"CallInsight недоступен: {e}"}


async def fetch_billing_usage() -> dict:
    """Расход минут за месяц (v4: GET /api/billing/usage)."""
    return await _ci_request("GET", "/api/billing/usage")


async def fetch_daily_report(refresh: bool = False,
                             cached_only: bool = False) -> dict:
    """Дневной отчёт по звонкам (v4: персистится; cached_only не тратит LLM)."""
    params = {}
    if refresh:
        params["refresh"] = "true"
    if cached_only:
        params["cached_only"] = "true"
    return await _ci_request("GET", "/api/analytics/daily-report",
                             params=params or None, timeout=60.0)


async def llm_search_customers(query: str) -> dict:
    """AI-подбор клиентов под предложение (v4: POST /api/customers/llm-search)."""
    return await _ci_request("POST", "/api/customers/llm-search",
                             json_body={"query": query}, timeout=60.0)


async def list_personas() -> dict:
    """Сохранённые цифровые персоны тенанта (v4 Лаборатория)."""
    return await _ci_request("GET", "/api/personas")


async def simulate_personas(persona_ids: list, content: str,
                            kind: str = "offer") -> dict:
    """Реакция персон на текст (v4): resonance 0-100, возражения, эмоция,
    что убедит, rewrite_suggestion. kind: message|offer|creative|landing."""
    return await _ci_request("POST", "/api/personas/simulate",
                             json_body={"persona_ids": persona_ids,
                                        "content": content, "kind": kind},
                             timeout=90.0)


async def compare_personas(persona_ids: list, content_a: str,
                           content_b: str, kind: str = "offer") -> dict:
    """A/B двух текстов на персонах (v4): winner по каждой + сводка."""
    return await _ci_request("POST", "/api/personas/compare",
                             json_body={"persona_ids": persona_ids,
                                        "content_a": content_a,
                                        "content_b": content_b,
                                        "kind": kind},
                             timeout=120.0)


# ──────────────────────────────────────────────────────────────────────
# Отчёт «Голос клиента» — детерминированная агрегация (без LLM)
# ──────────────────────────────────────────────────────────────────────

def _in_period(at: str, since: Optional[str], until: Optional[str]) -> bool:
    a = str(at or "")
    if since and a < since:
        return False
    if until and a > until:
        return False
    return True


def voice_of_customer(events: list, *, since: Optional[str] = None,
                      until: Optional[str] = None) -> dict:
    """Еженедельный срез «что говорит клиент» из накопленных cc_*-событий
    event_log. Все числа — подсчёт кодом (measured); цитаты/советы — вывод
    их LLM (llm_narrative), помечены. Дельта возражений: период vs всё,
    что до него (видно «что выросло»)."""
    cc = []
    for d in events or []:
        kind = str(d.get("kind") or "")
        prov = (d.get("payload") or {}).get("provenance") or {}
        if kind.startswith("cc_") or prov.get("source") == "callinsight":
            cc.append(d)
    cc.sort(key=lambda d: str(d.get("at") or ""))

    complaints, at_risk = [], []
    objections_now: Dict[str, int] = {}
    objections_before: Dict[str, int] = {}
    # имя возражения → 'at' последнего события с авторитетным mentions_prev
    objection_authoritative: Dict[str, str] = {}
    per_agent: Dict[str, Dict[str, int]] = {}
    outcomes = {"worked": 0, "worsened": 0, "implicit": 0}
    advice_count = 0

    def _agent_bump(agent: str, field: str) -> None:
        if not agent:
            return
        row = per_agent.setdefault(agent, {"complaints": 0, "at_risk": 0,
                                           "coach_advice": 0})
        row[field] += 1

    for d in cc:
        kind = str(d.get("kind") or "")
        pl = d.get("payload") or {}
        at = str(d.get("at") or "")
        agent = str(d.get("actor") or "")
        inside = _in_period(at, since, until)
        if kind == "cc_complaint" and inside:
            complaints.append({"at": at, "agent": agent,
                               "marker": pl.get("marker_phrase"),
                               "excerpt": pl.get("excerpt"),
                               "deep_link": pl.get("deep_link"),
                               "_source": "llm_narrative"})
            _agent_bump(agent, "complaints")
        elif kind == "cc_deal_at_risk" and inside:
            at_risk.append({"at": at, "agent": agent,
                            "customer_phone": pl.get("customer_phone"),
                            "key_insight": pl.get("key_insight"),
                            "deep_link": pl.get("deep_link"),
                            "_source": "llm_narrative"})
            _agent_bump(agent, "at_risk")
        elif kind == "cc_objection":
            name = str(pl.get("objection") or "").strip()
            if name:
                try:
                    now_cnt = int(pl.get("mentions") or 0)
                except (TypeError, ValueError):
                    now_cnt = 0
                prev_raw = pl.get("mentions_prev")
                if prev_raw is not None:
                    # v2: дельта авторитетна (источник посчитал now/prev по
                    # ISO-неделе). Последнее по времени событие на возражение
                    # перекрывает накопление — не суммируем повторы недель.
                    if inside:
                        try:
                            prev_cnt = int(prev_raw)
                        except (TypeError, ValueError):
                            prev_cnt = 0
                        at_e = str(d.get("at") or "")
                        if at_e >= objection_authoritative.get(name, ""):
                            objection_authoritative[name] = at_e
                            objections_now[name] = now_cnt
                            objections_before[name] = prev_cnt
                elif name not in objection_authoritative:
                    # legacy без mentions_prev — накопление по периодам
                    bucket = objections_now if inside else objections_before
                    bucket[name] = bucket.get(name, 0) + (now_cnt or 1)
        elif kind == "cc_coach_rec" and inside:
            advice_count += 1
            _agent_bump(agent, "coach_advice")
        elif kind == "cc_coach_outcome" and inside:
            verdict = str(pl.get("verdict") or "").lower()
            if verdict in outcomes:
                outcomes[verdict] += 1

    objection_rows = []
    for name in sorted(set(objections_now) | set(objections_before),
                       key=lambda n: -(objections_now.get(n, 0))):
        now, before = objections_now.get(name, 0), objections_before.get(name, 0)
        objection_rows.append({
            "objection": name, "mentions": now, "mentions_before": before,
            "trend": "new" if before == 0 and now > 0 else
                     ("up" if now > before else
                      ("down" if now < before else "flat"))})

    return {
        "since": since, "until": until,
        "complaints": {"total": len(complaints),
                       "items": complaints[-20:], "_source": "measured"},
        "deals_at_risk": {"total": len(at_risk),
                          "items": at_risk[-20:], "_source": "measured"},
        "objections": {"rows": objection_rows[:20], "_source": "measured"},
        "coach": {"advice_given": advice_count, "outcomes": outcomes,
                  "_source": "measured"},
        "per_agent": per_agent,
        "_note": ("числа посчитаны кодом из событий CallInsight; цитаты и "
                  "формулировки советов — вывод их LLM (llm_narrative), "
                  "не подтверждённый факт"),
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
    }


def voice_of_customer_markdown(report: dict) -> str:
    """Markdown-рендер отчёта (для страницы отчётов/выгрузки). Чистая
    функция поверх voice_of_customer — никаких новых вычислений."""
    lines = ["# Голос клиента (по данным звонков КЦ)", ""]
    period = " — ".join(p for p in (report.get("since"),
                                    report.get("until")) if p)
    if period:
        lines.append(f"Период: {period}")
        lines.append("")
    c = report.get("complaints") or {}
    lines.append(f"## Жалобы: {c.get('total', 0)}")
    for item in (c.get("items") or [])[-5:]:
        lines.append(f"- {str(item.get('at') or '')[:10]} "
                     f"({item.get('agent') or 'КЦ'}): "
                     f"«{str(item.get('excerpt') or item.get('marker') or '')[:120]}»")
    lines.append("")
    d = report.get("deals_at_risk") or {}
    lines.append(f"## Сделки к спасению: {d.get('total', 0)}")
    for item in (d.get("items") or [])[-5:]:
        lines.append(f"- {item.get('customer_phone') or '?'}: "
                     f"{str(item.get('key_insight') or '')[:120]}")
    lines.append("")
    lines.append("## Возражения (дельта к прошлым периодам)")
    trend_mark = {"new": "🆕", "up": "↑", "down": "↓", "flat": "→"}
    for row in (report.get("objections") or {}).get("rows") or []:
        lines.append(f"- {trend_mark.get(row['trend'], '')} "
                     f"{row['objection']}: {row['mentions']} "
                     f"(было {row['mentions_before']})")
    lines.append("")
    coach = report.get("coach") or {}
    oc = coach.get("outcomes") or {}
    lines.append(f"## Coach: советов {coach.get('advice_given', 0)}, "
                 f"сработало {oc.get('worked', 0)}, "
                 f"ухудшилось {oc.get('worsened', 0)}")
    pa = report.get("per_agent") or {}
    if pa:
        lines.append("")
        lines.append("## По менеджерам")
        lines.append("| Менеджер | Жалобы | Сделки к спасению | Советы |")
        lines.append("|---|---|---|---|")
        for agent, row in sorted(pa.items()):
            lines.append(f"| {agent} | {row['complaints']} | "
                         f"{row['at_risk']} | {row['coach_advice']} |")
    lines.append("")
    lines.append(f"> {report.get('_note', '')}")
    return "\n".join(lines)


def integration_enabled() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(get_feature_flags().enable_callinsight_integration)
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────
# Мост 4б: их KB-секции → наша база знаний с verified-flow
# (по их v1 §9: GET /api/kb/sections, дневная дельта; verified=true ⇔
# человек правил — попадает в наш граф/верифицированные факты как human;
# verified=false ⇔ AI-черновик — кладём как llm_narrative-инсайт, в
# supersede store НЕ пишем, owner подтверждает явно)
# ──────────────────────────────────────────────────────────────────────

# KB-секции, которые имеют ценность для нашей базы знаний компании
# (исключаем money/pipeline — слишком чувствительные/не наш домен)
KB_SECTIONS_ACCEPTED = ("overview", "products", "services",
                        "objections_handling", "target_audience",
                        "competitors", "kpis")


async def fetch_kb_sections(since: Optional[str] = None, *,
                            fetch=None) -> Dict[str, Any]:
    """GET {CALLINSIGHT_API_URL}/api/kb/sections?since=YYYY-MM-DD.
    Возвращает {"available": bool, "items": [...], "as_of": ..., "error": ?}.
    Их API недоступен → available=false (UI пишет «недоступно», не пусто).

    fetch — инжекция для тестов: async (url, headers, params) → (st, body)."""
    base = _api_base()
    headers = _ci_auth_headers()
    if not base or not headers:
        return {"available": False, "items": [],
                "error": "API звонков не настроен (CALLINSIGHT_API_URL/"
                         "MEETFLOW_API_URL + ключ)"}
    params = {"since": since} if since else {}
    if fetch is None:
        async def fetch(url, headers, params):
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(url, headers=headers, params=params)
                return r.status_code, (r.json() if r.content else None)
    try:
        st, body = await fetch(f"{base}/api/kb/sections", headers, params)
        if st != 200 or not isinstance(body, dict):
            return {"available": False, "items": [],
                    "error": f"HTTP {st}"}
        return {"available": True,
                "items": list(body.get("items") or []),
                "as_of": body.get("as_of")}
    except Exception as e:
        return {"available": False, "items": [], "error": str(e)}


def project_kb_sections(items: list) -> Dict[str, Any]:
    """KB-секции их компании → наши примитивы.

    verified=true (правил человек) → verified-факт `kb_section:<name>` +
    провенанс `_source=human`. Это попадает в наш контекст и Context API.

    verified=false (AI-черновик) → инсайт priority=medium с пометкой
    `_source=llm_narrative` для ленты — owner может подтвердить, тогда
    отдельным шагом перейдёт в verified.

    Чистая функция: возвращает {verified_facts:[{key,answer,by,at}],
    insights:[...], skipped:[...]}. БД/файлы не трогает."""
    out: Dict[str, Any] = {"verified_facts": [], "insights": [],
                           "skipped": []}
    for it in items or []:
        section = str(it.get("section") or "").strip().lower()
        if section not in KB_SECTIONS_ACCEPTED:
            out["skipped"].append({"section": section,
                                   "reason": "вне allowlist"})
            continue
        content = str(it.get("content_md") or "").strip()
        if not content:
            out["skipped"].append({"section": section,
                                   "reason": "пустая content_md"})
            continue
        fact_key = f"kb_section:{section}"
        if it.get("verified") is True:
            out["verified_facts"].append({
                "fact_key": fact_key,
                "answer": content,
                "verified_by": str(it.get("updated_by") or "callinsight"),
                "verified_at": str(it.get("updated_at") or ""),
                "source": "callinsight",
                "_source": "human",
            })
        else:
            out["insights"].append({
                "insight_id": _stable_id("cip_kb", section,
                                         it.get("updated_at")),
                "title": f"Черновик KB-секции «{section}» от CallInsight",
                "description": content[:600],
                "priority": "medium",
                "source": "callinsight",
                "insight_type": "kb_draft",
                "meta": {"section": section,
                         "updated_at": it.get("updated_at"),
                         "owner_action": "подтвердить или отклонить"},
                "provenance": {"source": "callinsight",
                               "domain": "company_knowledge",
                               "event": "kb_draft",
                               "_source": "llm_narrative"},
            })
    return out


async def apply_kb_projection(user_id: str,
                              projection: Dict[str, Any]) -> Dict[str, int]:
    """Раскидать KB-проекцию по сторам. verified_facts → VerifiedAnswers
    (отдельный стор, supersede НЕ трогается); insights → InsightStore.
    Возвращает счётчики применённого."""
    facts_pushed = insights_pushed = 0
    try:
        from backend.core.memory.verified_answers import verified_for_user
        vstore = verified_for_user(user_id)
        for f in projection.get("verified_facts") or []:
            try:
                vstore.verify(f["fact_key"],
                              by=f"callinsight:{f['verified_by']}")
                facts_pushed += 1
            except Exception as e:
                logger.debug(f"kb verify skipped {f['fact_key']}: {e}")
    except Exception as e:
        logger.debug(f"verified store unavailable: {e}")
    try:
        from backend.core.sleep.insight_store import InsightStore
        from backend.core.store.tenant_paths import insights_path_for_user
        ins = InsightStore(persist_path=insights_path_for_user(user_id))
        drafts = projection.get("insights") or []
        if drafts:
            r = ins.add(drafts)
            insights_pushed = r.get("added", 0)
    except Exception as e:
        logger.debug(f"kb insights push skipped: {e}")
    return {"verified_facts": facts_pushed,
            "draft_insights": insights_pushed}
