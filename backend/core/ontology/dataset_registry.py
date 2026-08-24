# -*- coding: utf-8 -*-
"""
Реестр датасетов — «палантир-модуль», часть 1 (docs/ru/DATA_ONTOLOGY.md).

Идея: подключили таблицу/выгрузку/CRM-данные → система ПРОФИЛИРУЕТ её
(типы, статистика, формат — детерминированно), ПОНИМАЕТ к чему она
относится в контексте компании (TableUnderstandingAgent: прайор из графа
знаний + LLM строго под скелетом), строит ОНТОЛОГИЮ (домены, привязки
колонок к сущностям, связи с другими датасетами) и хранит ССЫЛКУ на
источник, чтобы возвращаться за свежими данными.

Изоляция доступа: реестр и строки — per-user (tenant_paths), чужой
пользователь датасет не видит. Чистый stdlib.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import os
import re
import statistics
import uuid
from typing import Any, Dict, List, Optional

# Кап хранимых строк. Дефолт 5000 (сэмпл; источник остаётся истиной),
# для больших выгрузок поднимается без кода: TESSENT_DATASET_MAX_ROWS.
try:
    MAX_ROWS_STORED = max(100, int(os.getenv("TESSENT_DATASET_MAX_ROWS",
                                             "5000")))
except ValueError:
    MAX_ROWS_STORED = 5000
MAX_CELL_LEN = 500

SOURCE_KINDS = ("inline", "csv", "json", "url", "crm")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ──────────────────────────────────────────────────────────────────────
# Парсинг сырья → columns + rows(list[dict])
# ──────────────────────────────────────────────────────────────────────

def parse_tabular(content: str, *, fmt: Optional[str] = None
                  ) -> tuple[List[str], List[dict]]:
    """CSV / TSV / JSON(массив объектов или {rows:[...]}) → (columns, rows).

    Формат угадывается по содержимому, если fmt не задан. Ошибочные
    строки пропускаются (количество видно по профилю)."""
    text = (content or "").strip()
    if not text:
        return [], []

    if fmt == "json" or (not fmt and text[:1] in "[{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("rows") or data.get("data") or data.get("items") or []
            if isinstance(data, list) and data and isinstance(data[0], dict):
                cols: List[str] = []
                for r in data[:200]:
                    for k in r:
                        if k not in cols:
                            cols.append(str(k))
                rows = [{c: r.get(c) for c in cols} for r in data
                        if isinstance(r, dict)]
                return cols, rows[:MAX_ROWS_STORED]
        except (ValueError, TypeError):
            pass
        if fmt == "json":
            return [], []

    # CSV/TSV: разделитель — самый частый из ,;\t| в первой строке
    first = text.splitlines()[0]
    delim = max(",;\t|", key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    table = [row for row in reader if any(str(c).strip() for c in row)]
    if len(table) < 2:
        return [], []
    cols = [str(c).strip() or f"col{i}" for i, c in enumerate(table[0])]
    rows = []
    for raw in table[1:MAX_ROWS_STORED + 1]:
        rows.append({c: (str(raw[i])[:MAX_CELL_LEN] if i < len(raw) else None)
                     for i, c in enumerate(cols)})
    return cols, rows


# ──────────────────────────────────────────────────────────────────────
# Глубокий профиль колонок (детерминированно — «изучить цифры и формат»)
# ──────────────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"^-?\d{1,3}(?:[  ,]?\d{3})*(?:[.,]\d+)?%?$")


# ── Единицы измерения и масштаб («понимать, что это млн ₽, а не штуки») ──
# Словарь валют и масштабов — канонический, в money_units. Переехал туда,
# когда тот же словарь понадобился разбору бюджета проекта из встречи: два
# списка валют в разных файлах разошлись бы на первой же правке.
# Колонки, которые ПО СМЫСЛУ деньги (_MONEY_NAME_RE): если валюта не
# распознана, честно попросим уточнить, а не будем гадать.
from backend.core.ontology.money_units import (  # noqa: E402
    CURRENCY_PATTERNS as _CURRENCY_PATTERNS,
    MONEY_NAME_RE as _MONEY_NAME_RE,
    SCALE_PATTERNS as _SCALE_PATTERNS,
)


def infer_unit(name: str, values: List[Any]) -> dict:
    """Единица/валюта/масштаб колонки: из заголовка («Выручка, млн ₽»)
    и из самих значений («1 200 руб»). Ничего не выдумываем: не нашли —
    вернём пусто, а money-like колонка получит просьбу уточнить."""
    out: dict = {}
    header = str(name or "")
    for rx, mult, label in _SCALE_PATTERNS:
        if rx.search(header):
            out["scale"] = mult
            out["scale_label"] = label
            break
    for rx, code, sym in _CURRENCY_PATTERNS:
        if rx.search(header):
            out["unit"] = code
            out["unit_symbol"] = sym
            out["unit_source"] = "header"
            break
    if "unit" not in out:
        sample = [str(v) for v in values[:50] if v not in (None, "")]
        if sample:
            for rx, code, sym in _CURRENCY_PATTERNS:
                hits = sum(1 for s in sample if rx.search(s))
                if hits >= max(3, int(len(sample) * 0.6)):
                    out["unit"] = code
                    out["unit_symbol"] = sym
                    out["unit_source"] = "values"
                    break
    if "unit" not in out and _MONEY_NAME_RE.search(header):
        out["money_like"] = True   # деньги без подтверждённой валюты/масштаба
    return out


def unit_label(col_prof: dict) -> str:
    """Человеческий ярлык единицы: «млн ₽», «тыс $», «₽», «%» или ''."""
    if not col_prof:
        return ""
    if col_prof.get("dtype") == "percent":
        return "%"
    parts = []
    if col_prof.get("scale_label"):
        parts.append(col_prof["scale_label"])
    if col_prof.get("unit_symbol"):
        parts.append(col_prof["unit_symbol"])
    return " ".join(parts)


def to_number(v: Any) -> Optional[float]:
    """Число из ячейки: пробелы/неразрывные как разделители тысяч,
    запятая как десятичная, % срезается."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v or "").strip()
    if not s or not _NUM_RE.match(s):
        return None
    s = s.rstrip("%").replace(" ", "").replace(" ", "")
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def profile_column(name: str, values: List[Any]) -> dict:
    """Статистический профиль одной колонки (без LLM)."""
    non_null = [v for v in values if v not in (None, "")]
    nums = [n for n in (to_number(v) for v in non_null) if n is not None]
    distinct = {str(v) for v in non_null}
    prof: dict = {
        "name": name,
        "rows": len(values),
        "nulls": len(values) - len(non_null),
        "distinct": len(distinct),
        "samples": [str(v)[:80] for v in non_null[:5]],
    }
    if nums and len(nums) >= max(1, int(len(non_null) * 0.6)):
        pct = sum(1 for v in non_null if str(v).strip().endswith("%"))
        prof["dtype"] = "percent" if pct >= len(non_null) * 0.6 else "number"
        prof["stats"] = {
            "min": min(nums), "max": max(nums),
            "mean": round(statistics.fmean(nums), 4),
            "sum": round(sum(nums), 4),
        }
        if prof["dtype"] == "number":
            # единица/валюта/масштаб из заголовка и значений
            prof.update(infer_unit(name, non_null))
        # подозрение на смешанные единицы (1.5 «млн» рядом с 1 200 000):
        # отношение наибольшей и наименьшей ненулевой величины запредельно
        nonzero = [abs(n) for n in nums if n]
        if nonzero and max(nonzero) / min(nonzero) > 10000:
            prof["unit_warning"] = True
    elif non_null and all(_looks_date(str(v)) for v in non_null[:25]):
        prof["dtype"] = "date"
        svals = sorted(str(v) for v in non_null)
        prof["stats"] = {"min": svals[0], "max": svals[-1]}
    elif distinct and len(distinct) <= max(12, len(non_null) // 20):
        prof["dtype"] = "category"
        top: Dict[str, int] = {}
        for v in non_null:
            top[str(v)] = top.get(str(v), 0) + 1
        prof["categories"] = dict(sorted(top.items(), key=lambda kv: -kv[1])[:12])
    else:
        prof["dtype"] = "text"
    return prof


_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}|^\d{2}[./]\d{2}[./]\d{2,4}")


def _looks_date(s: str) -> bool:
    return bool(_DATE_RE.match(s.strip()))


def deep_profile(columns: List[str], rows: List[dict]) -> dict:
    """Профиль всей таблицы: per-column статистика + сводка."""
    profs = [profile_column(c, [r.get(c) for r in rows]) for c in columns]
    seen: Dict[str, int] = {}
    for r in rows:
        key = json.dumps([str(r.get(c)) for c in columns], ensure_ascii=False)
        seen[key] = seen.get(key, 0) + 1
    duplicates = sum(n - 1 for n in seen.values() if n > 1)
    return {
        "rows_total": len(rows),
        "duplicate_rows": duplicates,
        "columns_total": len(columns),
        "columns": profs,
        "numeric_columns": [p["name"] for p in profs
                            if p["dtype"] in ("number", "percent")],
        "percent_columns": [p["name"] for p in profs if p["dtype"] == "percent"],
        "date_columns": [p["name"] for p in profs if p["dtype"] == "date"],
        "category_columns": [p["name"] for p in profs if p["dtype"] == "category"],
    }


# ──────────────────────────────────────────────────────────────────────
# Связи между датасетами (онтологические рёбра, без LLM)
# ──────────────────────────────────────────────────────────────────────

def find_relations(profile: dict, others: List[dict]) -> List[dict]:
    """Связи с уже зарегистрированными датасетами:
    1) одинаковое имя колонки; 2) пересечение значений категорий/сэмплов
    (общие сущности). Это рёбра онтологии «таблица ↔ таблица»."""
    out: List[dict] = []
    my_cols = {p["name"].lower(): p for p in profile.get("columns", [])}
    for other in others:
        oprof = other.get("profile") or {}
        hits = []
        for op in oprof.get("columns", []):
            name = op["name"].lower()
            if name in my_cols:
                hits.append({"column": op["name"], "via": "same_column"})
                continue
            mine_vals = set()
            for p in profile.get("columns", []):
                mine_vals |= {s.lower() for s in p.get("samples", [])}
                mine_vals |= {k.lower() for k in (p.get("categories") or {})}
            other_vals = ({s.lower() for s in op.get("samples", [])}
                          | {k.lower() for k in (op.get("categories") or {})})
            common = mine_vals & other_vals - {"", "0", "1", "-"}
            if len(common) >= 3:
                hits.append({"column": op["name"], "via": "shared_values",
                             "examples": sorted(common)[:3]})
        if hits:
            out.append({"dataset_id": other["dataset_id"],
                        "title": other.get("title", ""),
                        "links": hits[:5]})
    return out


# ──────────────────────────────────────────────────────────────────────
# Типизация ячеек при записи: числа хранятся ЧИСЛАМИ
# ──────────────────────────────────────────────────────────────────────

def typed_rows(rows: List[dict], profile: dict) -> List[dict]:
    """Ячейки number-колонок → float при персисте (Foundry-принцип:
    типизированное хранение, а не «парсим строку на каждый запрос»).
    Нераспознанная ячейка остаётся строкой (честно видна как пропуск).
    percent/date не трогаем: их текст несёт формат («12%», «2026-06-01»)."""
    num_cols = [p["name"] for p in profile.get("columns", [])
                if p.get("dtype") == "number"]
    if not num_cols:
        return rows
    out = []
    for r in rows:
        r2 = dict(r)
        for c in num_cols:
            n = to_number(r2.get(c))
            if n is not None:
                r2[c] = n
        out.append(r2)
    return out


# ──────────────────────────────────────────────────────────────────────
# Наложение ручных корректировок на профиль и онтологию
# ──────────────────────────────────────────────────────────────────────

def _apply_column_overrides(rec: dict) -> None:
    """Наложить rec['overrides'] на profile.columns и ontology.grounding.
    Идемпотентно; правка пользователя сильнее авто-вывода."""
    overrides = rec.get("overrides") or {}
    if not overrides:
        return
    profile = rec.setdefault("profile", {})
    by_name = {p.get("name"): p for p in profile.get("columns", [])}
    grounding = rec.setdefault("ontology", {}).setdefault("grounding", {})
    for col, ov in overrides.items():
        prof = by_name.get(col)
        if prof is not None:
            for k in ("dtype", "unit", "unit_symbol", "scale",
                      "scale_label", "label"):
                if ov.get(k) not in (None, ""):
                    prof[k] = ov[k]
            if ov.get("unit") or ov.get("scale"):
                prof.pop("money_like", None)   # единица подтверждена
        g = grounding.setdefault(col, {})
        if ov.get("entity_type"):
            g["entity_type"] = ov["entity_type"]
            g["known"] = True
        if ov.get("kpi"):
            g["kpi"] = ov["kpi"]
            g["known"] = True
        if ov.get("entity_type") or ov.get("kpi"):
            g["corrected_by_user"] = True
    # производные списки могли поменяться после правок dtype
    profs = profile.get("columns", [])
    if profs:
        profile["numeric_columns"] = [p["name"] for p in profs
                                      if p.get("dtype") in ("number", "percent")]
        profile["percent_columns"] = [p["name"] for p in profs
                                      if p.get("dtype") == "percent"]
        profile["date_columns"] = [p["name"] for p in profs
                                   if p.get("dtype") == "date"]
        profile["category_columns"] = [p["name"] for p in profs
                                       if p.get("dtype") == "category"]


# ──────────────────────────────────────────────────────────────────────
# Реестр (per-user, file_lock + atomic_write_json)
# ──────────────────────────────────────────────────────────────────────

class DatasetRegistry:
    """Зарегистрированные датасеты пользователя: индекс + строки.

    index: {dataset_id: запись без rows}; строки — отдельным файлом
    рядом ({dataset_id}.rows.json), чтобы индекс оставался лёгким."""

    def __init__(self, index_path: str):
        self._path = index_path
        self._dir = os.path.dirname(index_path)

    # -- низкий уровень ------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _rows_path(self, dataset_id: str) -> str:
        return os.path.join(self._dir, f"{dataset_id}.rows.json")

    def load_rows(self, dataset_id: str) -> List[dict]:
        try:
            with open(self._rows_path(dataset_id), encoding="utf-8") as f:
                return json.load(f).get("rows", [])
        except (OSError, ValueError):
            return []

    # -- операции --------------------------------------------------------
    def register(self, *, title: str, columns: List[str], rows: List[dict],
                 source: Optional[dict] = None,
                 ontology: Optional[dict] = None) -> dict:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        source = dict(source or {"kind": "inline"})
        if source.get("kind") not in SOURCE_KINDS:
            raise ValueError(f"source.kind must be one of {SOURCE_KINDS}")
        dataset_id = uuid.uuid4().hex[:12]
        profile = deep_profile(columns, rows)
        rows = typed_rows(rows, profile)
        with file_lock(self._path):
            data = self._load()
            relations = find_relations(profile, list(data.values()))
            # рёбра онтологии двусторонние: связанные датасеты тоже
            # узнают о новом (для кросс-сверок в обе стороны)
            for rel in relations:
                other = data.get(rel["dataset_id"])
                if other is not None:
                    other.setdefault("relations", []).append({
                        "dataset_id": dataset_id, "title": title,
                        "links": rel.get("links", [])})
            rec = {
                "dataset_id": dataset_id,
                "title": title or f"Датасет {dataset_id}",
                "source": source,
                "columns": columns,
                "profile": profile,
                "ontology": ontology or {},
                "relations": relations,
                "findings": [],
                "registered_at": _now_iso(),
                "refreshed_at": _now_iso(),
            }
            data[dataset_id] = rec
            atomic_write_json(self._rows_path(dataset_id), {"rows": rows})
            atomic_write_json(self._path, data)
        return rec

    # Сколько прошлых версий строк храним на датасет (диск не резиновый)
    MAX_VERSIONS = 5

    def _version_path(self, dataset_id: str, version: int) -> str:
        return os.path.join(self._dir, f"{dataset_id}.v{version}.rows.json")

    @staticmethod
    def _numeric_sums(profile: dict, rows: List[dict]) -> dict:
        """Суммы числовых колонок — компактный «отпечаток» версии для диффа."""
        sums: dict = {}
        for name in (profile or {}).get("numeric_columns") or []:
            vals = [to_number(r.get(name)) for r in rows]
            ns = [v for v in vals if v is not None]
            if ns:
                sums[name] = round(sum(ns), 4)
        return sums

    def update(self, dataset_id: str, patch: dict,
               rows: Optional[List[dict]] = None) -> Optional[dict]:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            data = self._load()
            rec = data.get(dataset_id)
            if not rec:
                return None
            rec.update(patch)
            if rows is not None:
                # ── Версионирование: прежние строки уходят в архив ДО
                # перезаписи (Foundry-принцип: refresh не уничтожает
                # историю). Откат через rollback() тоже проходит здесь,
                # поэтому он сам обратим.
                old_rows = self.load_rows(dataset_id)
                old_profile = rec.get("profile") or {}
                if old_rows:
                    versions = rec.setdefault("versions", [])
                    ver = (versions[-1]["version"] + 1) if versions else 1
                    atomic_write_json(self._version_path(dataset_id, ver),
                                      {"rows": old_rows})
                    versions.append({
                        "version": ver,
                        "saved_at": rec.get("refreshed_at") or _now_iso(),
                        "rows_total": len(old_rows),
                        "numeric_sums": self._numeric_sums(old_profile,
                                                           old_rows),
                    })
                    # кап истории: старые файлы удаляем
                    while len(versions) > self.MAX_VERSIONS:
                        drop = versions.pop(0)
                        try:
                            os.remove(self._version_path(
                                dataset_id, drop["version"]))
                        except OSError:
                            pass

                rec["profile"] = deep_profile(rec.get("columns", []), rows)
                rows = typed_rows(rows, rec["profile"])
                rec["refreshed_at"] = _now_iso()
                atomic_write_json(self._rows_path(dataset_id), {"rows": rows})

                # Дифф «что изменилось» — как delta_summary у снапшотов
                if old_rows:
                    new_sums = self._numeric_sums(rec["profile"], rows)
                    old_sums = (rec.get("versions") or [{}])[-1].get(
                        "numeric_sums") or {}
                    sums_diff = {}
                    for col in set(old_sums) | set(new_sums):
                        o, n = old_sums.get(col), new_sums.get(col)
                        if o != n:
                            entry = {"old": o, "new": n}
                            if o and n is not None:
                                entry["delta_pct"] = round((n - o) / o * 100, 1)
                            sums_diff[col] = entry
                    rec["last_diff"] = {
                        "at": rec["refreshed_at"],
                        "rows": {"old": len(old_rows), "new": len(rows)},
                        "sums": sums_diff,
                    }
            # Ручные корректировки пользователя ПЕРЕЖИВАЮТ refresh: авто-
            # профиль/онтология перестроились — правки накладываем заново.
            _apply_column_overrides(rec)
            atomic_write_json(self._path, data)
            return rec

    def list_versions(self, dataset_id: str) -> List[dict]:
        rec = self.get(dataset_id) or {}
        return list(rec.get("versions") or [])

    def load_version_rows(self, dataset_id: str, version: int) -> List[dict]:
        try:
            with open(self._version_path(dataset_id, version),
                      encoding="utf-8") as f:
                return json.load(f).get("rows", [])
        except (OSError, ValueError):
            return []

    def rollback(self, dataset_id: str, version: int) -> Optional[dict]:
        """Откатить строки к версии N. Текущее состояние при этом само
        архивируется (update снапшотит перед перезаписью) — откат обратим."""
        rows = self.load_version_rows(dataset_id, version)
        if not rows:
            return None
        return self.update(dataset_id,
                           {"rolled_back_from": version}, rows=rows)

    # Разрешённые поля корректировки колонки. «Система поняла не так —
    # пользователь поправил» — правка сильнее авто-грундинга и переживает
    # refresh/переобучение онтологии.
    OVERRIDE_KEYS = ("entity_type", "kpi", "unit", "unit_symbol",
                     "scale", "scale_label", "dtype", "label", "comment")

    def set_column_override(self, dataset_id: str, column: str,
                            override: dict) -> Optional[dict]:
        """Корректировка колонки от пользователя. Пустые значения в patch
        снимают соответствующее поле правки; пустой словарь — снимает
        правку целиком."""
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        clean = {k: v for k, v in (override or {}).items()
                 if k in self.OVERRIDE_KEYS}
        with file_lock(self._path):
            data = self._load()
            rec = data.get(dataset_id)
            if not rec or column not in (rec.get("columns") or []):
                return None
            overrides = rec.setdefault("overrides", {})
            cur = overrides.get(column) or {}
            for k, v in clean.items():
                if v in (None, ""):
                    cur.pop(k, None)
                else:
                    cur[k] = v
            if cur:
                cur["corrected_at"] = _now_iso()
                overrides[column] = cur
            else:
                overrides.pop(column, None)
            _apply_column_overrides(rec)
            atomic_write_json(self._path, data)
            return rec

    def add_finding(self, dataset_id: str, finding: dict) -> bool:
        """Находка по датасету (вопрос → цифры → оценка) — память модуля:
        потом отвечаем быстрее и видим историю обращений."""
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            data = self._load()
            rec = data.get(dataset_id)
            if not rec:
                return False
            finding = {**finding, "at": _now_iso()}
            rec.setdefault("findings", []).append(finding)
            rec["findings"] = rec["findings"][-50:]
            atomic_write_json(self._path, data)
            return True

    def get(self, dataset_id: str) -> Optional[dict]:
        return self._load().get(dataset_id)

    def list(self) -> List[dict]:
        out = []
        for rec in self._load().values():
            slim = {k: v for k, v in rec.items()
                    if k not in ("profile", "findings")}
            slim["rows_total"] = (rec.get("profile") or {}).get("rows_total", 0)
            slim["findings_count"] = len(rec.get("findings") or [])
            out.append(slim)
        out.sort(key=lambda r: r.get("refreshed_at", ""), reverse=True)
        return out

    def delete(self, dataset_id: str) -> bool:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            data = self._load()
            if dataset_id not in data:
                return False
            del data[dataset_id]
            atomic_write_json(self._path, data)
        try:
            os.remove(self._rows_path(dataset_id))
        except OSError:
            pass
        return True


def ontology_enabled() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(get_feature_flags().enable_data_ontology)
    except Exception:
        return False
