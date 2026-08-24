# -*- coding: utf-8 -*-
"""Единый объект «Метрика» — мост между контурами цифр (Foundry-принцип).

Проблема, которую решает: «выручка», произнесённая на планёрке (KPI-узел
графа из транскрипта), и «Выручка» из подключённой таблицы были ДВУМЯ
разными объектами, которые никогда не сверялись. Здесь они сливаются в
одну сущность с историей значений и происхождением КАЖДОЙ точки:

    metrics(name, unit, scale, category)
    metric_points(value, period, at, source_type: meeting|dataset|manual,
                  source_id, detail)

Хранение — типизированный per-user SQLite (тот же паттерн, что temporal
tracker): числа числами, честные SQL-агрегации, без лимита JSON-сэмпла.
Питатели: KPI из встреч (knowledge_sync) и колонки датасетов,
заземлённые на KPI (dataset_service). Сводка summary() даёт главный
wow-эффект: «на встрече заявляли X, данные показывают Y — расхождение Z%».

Никогда не роняет вызывающий код: питатели вызывают через try/except.
Чистый stdlib (sqlite3 + re + json).
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, List, Optional

from backend.core.ontology.dataset_registry import (
    _CURRENCY_PATTERNS,
    _SCALE_PATTERNS,
    to_number,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def parse_value_with_unit(text: Any) -> dict:
    """Понять цифру из живой речи/ячейки: «4,2 млн ₽» →
    {value: 4200000.0, unit: 'RUB', unit_symbol: '₽', scale: 1000000}.

    Возвращает {} если числа нет. value — уже В БАЗОВЫХ единицах
    (масштаб применён): метрика хранит сопоставимые числа."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return {"value": float(text)}
    s = str(text or "").strip()
    if not s:
        return {}
    out: dict = {}
    scale = 1.0
    for rx, mult, label in _SCALE_PATTERNS:
        if rx.search(s):
            scale = float(mult)
            out["scale"] = mult
            out["scale_label"] = label
            break
    for rx, code, sym in _CURRENCY_PATTERNS:
        if rx.search(s):
            out["unit"] = code
            out["unit_symbol"] = sym
            break
    # само число: срезаем всё, кроме числовой части
    m = re.search(r"-?\d[\d  .,]*", s)
    if not m:
        return {}
    num = to_number(m.group(0).strip())
    if num is None:
        return {}
    out["value"] = num * scale
    return out


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    metric_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    name_norm  TEXT NOT NULL UNIQUE,
    unit       TEXT,
    category   TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_points (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id   TEXT NOT NULL REFERENCES metrics(metric_id),
    value       REAL NOT NULL,
    period      TEXT,             -- '2026-05' / '2026-Q2' / NULL
    at          TEXT NOT NULL,    -- когда цифра актуальна (ISO)
    source_type TEXT NOT NULL CHECK (source_type IN
                                     ('meeting','dataset','manual')),
    source_id   TEXT,             -- meeting_id / dataset_id
    kind        TEXT NOT NULL DEFAULT 'fact'
                CHECK (kind IN ('fact','plan')),  -- план/факт first-class
    detail      TEXT,             -- lineage: сырой текст/колонка/план
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_points_metric ON metric_points(metric_id, at);
CREATE INDEX IF NOT EXISTS idx_points_source ON metric_points(source_type, source_id);
"""


class MetricRegistry:
    """Per-user реестр метрик на SQLite. Слияние по нормализованному имени."""

    def __init__(self, db_path: str):
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # миграция ранних БД без kind (ALTER идемпотентен через PRAGMA)
            cols = {r["name"] for r in
                    c.execute("PRAGMA table_info(metric_points)")}
            if "kind" not in cols:
                c.execute("ALTER TABLE metric_points ADD COLUMN kind TEXT "
                          "NOT NULL DEFAULT 'fact'")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # ── метрики ─────────────────────────────────────────────────────────
    def upsert_metric(self, name: str, *, unit: Optional[str] = None,
                      category: Optional[str] = None) -> dict:
        """Найти по нормализованному имени или создать. Unit заполняется
        первым известным значением и НЕ перетирается молча другим."""
        nn = _norm(name)
        if not nn:
            raise ValueError("metric name is empty")
        with self._conn() as c:
            row = c.execute("SELECT * FROM metrics WHERE name_norm=?",
                            (nn,)).fetchone()
            if row:
                if unit and not row["unit"]:
                    c.execute("UPDATE metrics SET unit=? WHERE metric_id=?",
                              (unit, row["metric_id"]))
                    row = c.execute("SELECT * FROM metrics WHERE metric_id=?",
                                    (row["metric_id"],)).fetchone()
                return dict(row)
            mid = uuid.uuid4().hex[:12]
            c.execute(
                "INSERT INTO metrics(metric_id,name,name_norm,unit,category,"
                "created_at) VALUES (?,?,?,?,?,?)",
                (mid, str(name).strip(), nn, unit, category, _now_iso()))
            return dict(c.execute("SELECT * FROM metrics WHERE metric_id=?",
                                  (mid,)).fetchone())

    def get(self, name: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM metrics WHERE name_norm=?",
                            (_norm(name),)).fetchone()
            return dict(row) if row else None

    # ── точки ───────────────────────────────────────────────────────────
    def add_point(self, name: str, value: float, *, source_type: str,
                  source_id: Optional[str] = None,
                  period: Optional[str] = None, at: Optional[str] = None,
                  unit: Optional[str] = None, kind: str = "fact",
                  detail: Optional[Any] = None) -> dict:
        m = self.upsert_metric(name, unit=unit)
        with self._conn() as c:
            c.execute(
                "INSERT INTO metric_points(metric_id,value,period,at,"
                "source_type,source_id,kind,detail,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (m["metric_id"], float(value), period, at or _now_iso(),
                 source_type, source_id, kind,
                 json.dumps(detail, ensure_ascii=False) if detail is not None
                 else None,
                 _now_iso()))
        return m

    def replace_source_points(self, source_type: str, source_id: str,
                              points: List[dict]) -> int:
        """Идемпотентный питатель: снести старые точки источника и записать
        свежие (refresh датасета не плодит дубли). points: [{name, value,
        period?, at?, unit?, detail?}]."""
        with self._conn() as c:
            c.execute("DELETE FROM metric_points WHERE source_type=? "
                      "AND source_id=?", (source_type, source_id))
        n = 0
        for p in points:
            try:
                self.add_point(
                    p["name"], float(p["value"]), source_type=source_type,
                    source_id=source_id, period=p.get("period"),
                    at=p.get("at"), unit=p.get("unit"),
                    kind=p.get("kind") or "fact",
                    detail=p.get("detail"))
                n += 1
            except Exception as e:
                logger.debug(f"metric point skipped: {e}")
        return n

    # ── чтение ──────────────────────────────────────────────────────────
    def list_metrics(self) -> List[dict]:
        with self._conn() as c:
            rows = c.execute("""
                SELECT m.metric_id, m.name, m.unit, m.category,
                       COUNT(p.id) AS points,
                       COUNT(DISTINCT p.source_type) AS source_types,
                       MAX(p.at) AS last_at
                FROM metrics m LEFT JOIN metric_points p
                     ON p.metric_id = m.metric_id
                GROUP BY m.metric_id ORDER BY last_at DESC""").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                last = c.execute(
                    "SELECT value, source_type FROM metric_points "
                    "WHERE metric_id=? ORDER BY at DESC, id DESC LIMIT 1",
                    (r["metric_id"],)).fetchone()
                if last:
                    d["last_value"] = last["value"]
                    d["last_source"] = last["source_type"]
                out.append(d)
            return out

    def series(self, name: str, source_type: Optional[str] = None,
               kind: Optional[str] = None) -> List[dict]:
        m = self.get(name)
        if not m:
            return []
        q = ("SELECT value, period, at, source_type, source_id, kind, detail "
             "FROM metric_points WHERE metric_id=?")
        args: list = [m["metric_id"]]
        if source_type:
            q += " AND source_type=?"
            args.append(source_type)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        with self._conn() as c:
            return [dict(r) for r in
                    c.execute(q + " ORDER BY at, id", args).fetchall()]

    def summary(self, name: str) -> Optional[dict]:
        """Карточка метрики: последняя цифра из каждого источника, сверка
        «встреча vs данные» (по общему периоду, иначе по последним) и
        выполнение плана (план из встречи vs самый свежий факт)."""
        m = self.get(name)
        if not m:
            return None
        all_pts = self.series(name)
        pts = [p for p in all_pts if p.get("kind") != "plan"]   # факты
        plans = [p for p in all_pts if p.get("kind") == "plan"]
        by_src: dict = {}
        for p in pts:
            by_src[p["source_type"]] = p          # последняя по времени
        out: dict = {"metric": m, "points_total": len(all_pts),
                     "latest": by_src, "delta": None, "note": None,
                     "plan": None}
        # ── план vs факт: план — first-class точка (kind='plan') ──
        if plans:
            plan = plans[-1]                      # самый свежий план
            # факт для сверки: тот же период, приоритет данным; иначе
            # последний известный факт
            fact = None
            for p in reversed(pts):
                if plan.get("period") and p.get("period") == plan["period"]:
                    fact = p
                    if p["source_type"] == "dataset":
                        break
            if fact is None and pts:
                fact = pts[-1]
            plan_block: dict = {"value": plan["value"],
                                "period": plan.get("period"),
                                "at": plan.get("at")}
            if fact and plan["value"]:
                completion = round(fact["value"] / plan["value"] * 100, 1)
                plan_block["fact_value"] = fact["value"]
                plan_block["fact_source"] = fact["source_type"]
                plan_block["completion_pct"] = completion
                unit = f" {m['unit']}" if m.get("unit") else ""
                per = f" за {plan['period']}" if plan.get("period") else ""
                plan_block["note"] = (
                    f"план{per}: {plan['value']:g}{unit}, факт: "
                    f"{fact['value']:g}{unit} — выполнение {completion}%")
            out["plan"] = plan_block
        mt, ds = by_src.get("meeting"), by_src.get("dataset")
        if mt and ds:
            # сверка по общему периоду, если есть; иначе последние значения
            mt_c, ds_c = mt, ds
            periods_m = {p["period"]: p for p in pts
                         if p["source_type"] == "meeting" and p["period"]}
            for p in reversed(pts):
                if (p["source_type"] == "dataset" and p["period"]
                        and p["period"] in periods_m):
                    ds_c, mt_c = p, periods_m[p["period"]]
                    break
            base = max(abs(mt_c["value"]), abs(ds_c["value"]))
            if base:
                delta = round((ds_c["value"] - mt_c["value"]) / base * 100, 1)
                out["delta"] = {
                    "meeting_value": mt_c["value"], "dataset_value": ds_c["value"],
                    "period": ds_c.get("period") if ds_c.get("period") == mt_c.get("period") else None,
                    "pct": delta,
                }
                unit = f" {m['unit']}" if m.get("unit") else ""
                scope = (f"за {ds_c['period']}" if out["delta"]["period"]
                         else "последние значения")
                verdict = ("сходятся" if abs(delta) <= 2 else
                           f"РАСХОЖДЕНИЕ {abs(delta)}%")
                out["note"] = (
                    f"«{m['name']}» ({scope}): на встречах — "
                    f"{mt_c['value']:g}{unit}, по данным — "
                    f"{ds_c['value']:g}{unit} → {verdict}")
        return out


    def divergences(self, threshold_pct: float = 10.0) -> List[dict]:
        """Метрики, где «встреча vs данные» расходятся сильнее порога.
        Возвращает summary-карточки с delta — сырьё для алертов."""
        out: List[dict] = []
        for m in self.list_metrics():
            if (m.get("source_types") or 0) < 2:
                continue        # сверять нечего — один источник
            try:
                s = self.summary(m["name"])
            except Exception as e:
                logger.debug(f"divergence check skipped for {m['name']}: {e}")
                continue
            d = (s or {}).get("delta")
            if d and abs(d.get("pct") or 0) >= threshold_pct:
                out.append(s)
        out.sort(key=lambda s: -abs(s["delta"]["pct"]))
        return out


class MetricRegistryPG(MetricRegistry):
    """Postgres-бэкенд реестра (D1e/B3): те же публичные методы, но общие
    таблицы с фильтром по user_id. Storage-примитивы (upsert_metric/get/
    add_point/replace_source_points/list_metrics/series) переопределены под
    Postgres; чистая логика (summary/divergences/parse) наследуется от
    MetricRegistry без изменений. Sync (общий движок access_repo)."""

    def __init__(self, user_id: str):
        # НЕ вызываем super().__init__ — там создание sqlite-файла/схемы.
        from backend.core.store.tenant_paths import _require_uuid
        self._uid = _require_uuid(user_id, "user_id")

    def _session(self):
        from backend.core.ingest import access_repo
        return access_repo.pg_session()

    def _metric_row(self, s, mid: str) -> Optional[dict]:
        from sqlalchemy import text
        r = s.execute(text(
            "SELECT metric_id,name,name_norm,unit,category,created_at "
            "FROM metric_registry WHERE user_id=:u AND metric_id=:m"),
            {"u": self._uid, "m": mid}).fetchone()
        return dict(r._mapping) if r else None

    def upsert_metric(self, name: str, *, unit: Optional[str] = None,
                      category: Optional[str] = None) -> dict:
        from sqlalchemy import text
        nn = _norm(name)
        if not nn:
            raise ValueError("metric name is empty")
        with self._session() as s:
            r = s.execute(text(
                "SELECT metric_id,name,name_norm,unit,category,created_at "
                "FROM metric_registry WHERE user_id=:u AND name_norm=:n"),
                {"u": self._uid, "n": nn}).fetchone()
            if r:
                d = dict(r._mapping)
                if unit and not d.get("unit"):
                    s.execute(text(
                        "UPDATE metric_registry SET unit=:un "
                        "WHERE user_id=:u AND metric_id=:m"),
                        {"un": unit, "u": self._uid, "m": d["metric_id"]})
                    d["unit"] = unit
                return d
            mid = uuid.uuid4().hex[:12]
            s.execute(text(
                "INSERT INTO metric_registry(user_id,metric_id,name,name_norm,"
                "unit,category,created_at) VALUES (:u,:m,:nm,:nn,:un,:cat,:ts)"),
                {"u": self._uid, "m": mid, "nm": str(name).strip(), "nn": nn,
                 "un": unit, "cat": category, "ts": _now_iso()})
            return {"metric_id": mid, "name": str(name).strip(), "name_norm": nn,
                    "unit": unit, "category": category, "created_at": _now_iso()}

    def get(self, name: str) -> Optional[dict]:
        from sqlalchemy import text
        with self._session() as s:
            r = s.execute(text(
                "SELECT metric_id,name,name_norm,unit,category,created_at "
                "FROM metric_registry WHERE user_id=:u AND name_norm=:n"),
                {"u": self._uid, "n": _norm(name)}).fetchone()
            return dict(r._mapping) if r else None

    def add_point(self, name: str, value: float, *, source_type: str,
                  source_id: Optional[str] = None,
                  period: Optional[str] = None, at: Optional[str] = None,
                  unit: Optional[str] = None, kind: str = "fact",
                  detail: Optional[Any] = None) -> dict:
        from sqlalchemy import text
        m = self.upsert_metric(name, unit=unit)
        with self._session() as s:
            s.execute(text(
                "INSERT INTO metric_point(user_id,metric_id,value,period,at,"
                "source_type,source_id,kind,detail,created_at) VALUES "
                "(:u,:m,:v,:p,:at,:st,:sid,:k,:d,:ts)"),
                {"u": self._uid, "m": m["metric_id"], "v": float(value),
                 "p": period, "at": at or _now_iso(), "st": source_type,
                 "sid": source_id, "k": kind,
                 "d": json.dumps(detail, ensure_ascii=False)
                 if detail is not None else None, "ts": _now_iso()})
        return m

    def replace_source_points(self, source_type: str, source_id: str,
                              points: List[dict]) -> int:
        from sqlalchemy import text
        with self._session() as s:
            s.execute(text(
                "DELETE FROM metric_point WHERE user_id=:u AND source_type=:st "
                "AND source_id=:sid"),
                {"u": self._uid, "st": source_type, "sid": source_id})
        n = 0
        for p in points:
            try:
                self.add_point(
                    p["name"], float(p["value"]), source_type=source_type,
                    source_id=source_id, period=p.get("period"),
                    at=p.get("at"), unit=p.get("unit"),
                    kind=p.get("kind") or "fact", detail=p.get("detail"))
                n += 1
            except Exception as e:  # noqa: BLE001
                logger.debug(f"metric point skipped: {e}")
        return n

    def list_metrics(self) -> List[dict]:
        from sqlalchemy import text
        with self._session() as s:
            rows = s.execute(text(
                "SELECT m.metric_id, m.name, m.unit, m.category, "
                "COUNT(p.id) AS points, "
                "COUNT(DISTINCT p.source_type) AS source_types, "
                "MAX(p.at) AS last_at "
                "FROM metric_registry m LEFT JOIN metric_point p "
                "  ON p.user_id=m.user_id AND p.metric_id=m.metric_id "
                "WHERE m.user_id=:u "
                "GROUP BY m.metric_id, m.name, m.unit, m.category "
                "ORDER BY last_at DESC NULLS LAST"),
                {"u": self._uid}).fetchall()
            out = []
            for r in rows:
                d = dict(r._mapping)
                last = s.execute(text(
                    "SELECT value, source_type FROM metric_point "
                    "WHERE user_id=:u AND metric_id=:m "
                    "ORDER BY at DESC, id DESC LIMIT 1"),
                    {"u": self._uid, "m": d["metric_id"]}).fetchone()
                if last:
                    d["last_value"] = last._mapping["value"]
                    d["last_source"] = last._mapping["source_type"]
                out.append(d)
            return out

    def series(self, name: str, source_type: Optional[str] = None,
               kind: Optional[str] = None) -> List[dict]:
        from sqlalchemy import text
        m = self.get(name)
        if not m:
            return []
        q = ("SELECT value, period, at, source_type, source_id, kind, detail "
             "FROM metric_point WHERE user_id=:u AND metric_id=:m")
        params: dict = {"u": self._uid, "m": m["metric_id"]}
        if source_type:
            q += " AND source_type=:st"
            params["st"] = source_type
        if kind:
            q += " AND kind=:k"
            params["k"] = kind
        with self._session() as s:
            rows = s.execute(text(q + " ORDER BY at, id"), params).fetchall()
            return [dict(r._mapping) for r in rows]


def dashboard_for_user(user_id: str, *, max_points: int = 24) -> List[dict]:
    """Дашборд трендов (B3): по каждой метрике — компактные ряды для графика
    одним запросом. Pure поверх реестра — работает на sqlite и Postgres.

    Ряд: точки по оси x (period, иначе дата из at), раздельно по сериям:
      fact_dataset / fact_meeting — факты по источникам;
      plan — плановые точки (kind='plan').
    Плюс сводка: unit, последняя цифра, delta сверки «встречи vs данные»,
    выполнение плана (из summary). Метрики без точек пропускаются.
    """
    reg = metrics_for_user(user_id)
    out: List[dict] = []
    for m in reg.list_metrics():
        if not (m.get("points") or 0):
            continue
        name = m["name"]
        pts = reg.series(name)
        # ось X: period приоритетно (сопоставимые периоды), иначе дата ISO
        by_x: dict = {}
        order: List[str] = []
        for p in pts:
            x = p.get("period") or str(p.get("at") or "")[:10]
            if not x:
                continue
            if x not in by_x:
                by_x[x] = {"x": x}
                order.append(x)
            key = ("plan" if p.get("kind") == "plan"
                   else f"fact_{p.get('source_type') or 'manual'}")
            by_x[x][key] = p.get("value")
        series = [by_x[x] for x in sorted(order)][-max_points:]
        summary = None
        try:
            summary = reg.summary(name)
        except Exception:
            logger.debug("dashboard summary skipped for %s", name, exc_info=True)
        out.append({
            "name": name,
            "unit": m.get("unit"),
            "last_value": m.get("last_value"),
            "last_source": m.get("last_source"),
            "points": m.get("points"),
            "source_types": m.get("source_types"),
            "series": series,
            "delta": (summary or {}).get("delta"),
            "plan": (summary or {}).get("plan"),
            "note": (summary or {}).get("note"),
        })
    return out


def divergence_insights(user_id: str,
                        threshold_pct: float = 10.0) -> List[dict]:
    """Готовые warning-инсайты по расхождениям метрик — для InsightStore
    (лента + «фокус дня»). Чистая функция: id детерминированный по
    содержимому, повторный ночной прогон не задваивает; изменившаяся
    дельта — новый инсайт (так и надо: ситуация изменилась)."""
    reg = metrics_for_user(user_id)
    insights: List[dict] = []
    for s in reg.divergences(threshold_pct):
        m, d = s["metric"], s["delta"]
        unit = f" {m['unit']}" if m.get("unit") else ""
        scope = f"за {d['period']}" if d.get("period") else "последние значения"
        insights.append({
            "insight_type": "warning",
            "title": (f"Метрика «{m['name']}»: слова расходятся с данными "
                      f"на {abs(d['pct'])}%")[:120],
            "description": (
                f"{scope}: на встречах звучало {d['meeting_value']:g}{unit}, "
                f"по подключённым данным — {d['dataset_value']:g}{unit}. "
                "Либо данные устарели, либо ожидания завышены — стоит "
                "разобраться, откуда разрыв."),
            "priority": "high" if abs(d["pct"]) >= 25 else "medium",
            "source": "metric_divergence",
            "metric": m["name"],
            "delta_pct": d["pct"],
        })
    return insights


def metrics_db_path_for_user(user_id: str) -> str:
    from backend.core.store.tenant_paths import _DATA_ROOT, _require_uuid
    user_id = _require_uuid(user_id, "user_id")
    return str(Path(_DATA_ROOT) / "metrics" / f"{user_id}.db")


def _metrics_pg_enabled() -> bool:
    """Postgres-бэкенд метрик включён И БД доступна? Иначе — sqlite (fallback)."""
    try:
        from backend.config import settings
        if getattr(settings, "metric_store_backend", "sqlite") != "postgres":
            return False
        from backend.core.ingest import access_repo
        if access_repo.engine_ready():
            return True
        logger.error("metric_registry: Postgres недоступен — откат на sqlite")
    except Exception:
        logger.debug("metric_registry backend check failed", exc_info=True)
    return False


def metrics_for_user(user_id: str) -> MetricRegistry:
    if _metrics_pg_enabled():
        return MetricRegistryPG(user_id)
    return MetricRegistry(metrics_db_path_for_user(user_id))


# ──────────────────────────────────────────────────────────────────────
# Питатель 1: KPI из встреч (вызывается из knowledge_sync, never-raise)
# ──────────────────────────────────────────────────────────────────────

def ingest_meeting_kpis(user_id: str, meeting_id: str,
                        kpis: List[dict], enhanced_kpis: List[dict],
                        at_iso: Optional[str] = None) -> int:
    """Цифры KPI, произнесённые на встрече → точки метрик. «4,2 млн ₽»
    парсится в 4200000.0 RUB — цифра из речи становится числом."""
    reg = metrics_for_user(user_id)
    points: List[dict] = []
    for k in (kpis or []):
        name = (k.get("name") or "").strip()
        parsed = parse_value_with_unit(k.get("value"))
        if not name or "value" not in parsed:
            continue
        points.append({"name": name, "value": parsed["value"],
                       "unit": parsed.get("unit"), "at": at_iso,
                       "detail": {"raw": k.get("value"),
                                  "trend": k.get("trend") or None}})
    for k in (enhanced_kpis or []):
        name = (k.get("metric_name") or "").strip()
        if not name:
            continue
        vals = k.get("values") or {}
        period = k.get("time_period") or None
        parsed = parse_value_with_unit(vals.get("current_value"))
        unit = parsed.get("unit") or (vals.get("unit") or None)
        if "value" in parsed:
            points.append({"name": name, "value": parsed["value"],
                           "unit": unit, "at": at_iso, "period": period,
                           "detail": {"raw": vals.get("current_value")}})
        # план/цель — first-class точка (kind='plan'): summary() посчитает
        # выполнение против самого свежего факта
        target = parse_value_with_unit(vals.get("target_value"))
        if "value" in target:
            points.append({"name": name, "value": target["value"],
                           "unit": unit or target.get("unit"),
                           "at": at_iso, "period": period, "kind": "plan",
                           "detail": {"raw": vals.get("target_value")}})
    return reg.replace_source_points("meeting", meeting_id, points)


# ──────────────────────────────────────────────────────────────────────
# Питатель 2: колонки датасетов, заземлённые на KPI (dataset_service)
# ──────────────────────────────────────────────────────────────────────

def ingest_dataset_metrics(user_id: str, rec: dict,
                           rows: List[dict]) -> int:
    """Колонка таблицы, узнанная как KPI компании (авто-грундинг или
    корректировка пользователя) → ряд точек метрики. Есть date-колонка →
    помесячные суммы (динамика); нет → одна суммарная точка. Значения
    нормализуются масштабом колонки (млн → ×1e6): сопоставимые числа."""
    from backend.core.ontology.query_engine import execute_plan
    grounding = (rec.get("ontology") or {}).get("grounding") or {}
    profile = rec.get("profile") or {}
    by_col = {p.get("name"): p for p in profile.get("columns") or []}
    date_cols = sorted(profile.get("date_columns") or [])
    at = rec.get("refreshed_at") or _now_iso()
    points: List[dict] = []
    for col, g in grounding.items():
        kpi_name = (g or {}).get("kpi")
        prof = by_col.get(col) or {}
        if not kpi_name or prof.get("dtype") != "number":
            continue
        mult = float(prof.get("scale") or 1)
        unit = prof.get("unit")
        detail = {"dataset_id": rec.get("dataset_id"),
                  "dataset": rec.get("title"), "column": col}
        if date_cols:
            out = execute_plan(
                {"op": "group_sum", "column": col, "group_by": date_cols[0],
                 "filters": [], "limit": 100},
                rows, date_columns=set(date_cols))
            for period, val in (out.get("result") or {}).items():
                points.append({"name": kpi_name, "value": float(val) * mult,
                               "period": period, "at": at, "unit": unit,
                               "detail": {**detail, "agg": "group_sum"}})
        else:
            out = execute_plan({"op": "sum", "column": col, "filters": [],
                                "limit": 10}, rows)
            if isinstance(out.get("result"), (int, float)):
                points.append({"name": kpi_name,
                               "value": float(out["result"]) * mult,
                               "at": at, "unit": unit,
                               "detail": {**detail, "agg": "sum"}})
    reg = metrics_for_user(user_id)
    return reg.replace_source_points("dataset", rec.get("dataset_id") or "?",
                                     points)
