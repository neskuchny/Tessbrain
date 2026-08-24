# -*- coding: utf-8 -*-
"""Негативная память дедупа: пары «это РАЗНЫЕ сущности, не сливать».

Проблема, которую закрывает: пользователь отклонил предложение слияния
(«Катя (CEO)» ≠ «Катя (продюсер)») — но раньше это решение нигде не
записывалось, и следующей ночью дедуп предлагал ту же пару снова. Теперь
отклонение — это знание: пара + опциональный комментарий человека
(«это два разных человека, CEO — Постовалова, продюсер — Кожемякина»).

Ключ пары — ПОЛНОЕ нормализованное имя узла (нижний регистр, схлопнутые
пробелы), С квалификаторами: «катя (ceo)» ≠ «катя (продюсер)». Нельзя
нормализовать до голого имени — иначе негатив «катя ≠ катя» заблокировал
бы ВСЕ слияния любых Кать.

Использование:
- резолвер: пост-фильтр кластеров (выкинуть члена, негативного к канону)
  и блок «ИЗВЕСТНЫЕ НЕ-ДУБЛИ» в LLM-промпт кластеризации;
- ревью: отклонение/частичное подтверждение пишет негативы.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_MAX_PAIRS = 500


def _dir() -> Path:
    p = Path("data") / "dedup_negative"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(user_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id or "")[:64] or "anon"
    return _dir() / f"{safe}.json"


def _norm(name: str) -> str:
    """Полное имя, нижний регистр, схлопнутые пробелы — квалификаторы
    сохраняются («катя (ceo)» ≠ «катя»)."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _pair_key(a: str, b: str) -> Tuple[str, str]:
    na, nb = _norm(a), _norm(b)
    return (na, nb) if na <= nb else (nb, na)


def load_negatives(user_id: str) -> List[dict]:
    try:
        return json.loads(_path(user_id).read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(user_id: str, items: List[dict]) -> None:
    _path(user_id).write_text(
        json.dumps(items[-_MAX_PAIRS:], ensure_ascii=False, indent=1),
        encoding="utf-8")


def add_negative_pairs(user_id: str, canonical: str, names: List[str],
                       comment: str = "", entity_type: str = "person") -> int:
    """Записать «canonical ≠ каждое из names». Дубли пар не плодим,
    комментарий обновляется свежим (человек мог уточнить)."""
    if not user_id or not canonical:
        return 0
    items = load_negatives(user_id)
    index = {(_pair_key(it.get("a", ""), it.get("b", ""))): it
             for it in items}
    added = 0
    for nm in names:
        if not nm or _norm(nm) == _norm(canonical):
            continue
        key = _pair_key(canonical, nm)
        if key[0] == key[1]:
            continue
        rec = index.get(key)
        if rec is None:
            rec = {"a": key[0], "b": key[1], "entity_type": entity_type,
                   "comment": (comment or "").strip()[:300],
                   "at": datetime.utcnow().isoformat()}
            items.append(rec)
            index[key] = rec
            added += 1
        elif comment:
            rec["comment"] = comment.strip()[:300]
            rec["at"] = datetime.utcnow().isoformat()
    _save(user_id, items)
    return added


def negative_pairs_set(user_id: str) -> Set[Tuple[str, str]]:
    return {_pair_key(it.get("a", ""), it.get("b", ""))
            for it in load_negatives(user_id)}


def is_negative(user_id: str, a: str, b: str,
                pairs: Optional[Set[Tuple[str, str]]] = None) -> bool:
    """Пара помечена человеком как «разные»? pairs — прекешированный сет
    (для циклов), иначе читаем файл."""
    if pairs is None:
        pairs = negative_pairs_set(user_id)
    return _pair_key(a, b) in pairs


def negatives_prompt_block(user_id: str, entity_type: str = "person",
                           limit: int = 20) -> str:
    """Блок для LLM-промпта кластеризации: подтверждённые человеком
    НЕ-дубли с его комментариями — чтобы LLM не предлагал их снова."""
    items = [it for it in load_negatives(user_id)
             if it.get("entity_type", "person") == entity_type][-limit:]
    if not items:
        return ""
    lines = []
    for it in items:
        line = f'- «{it.get("a")}» ≠ «{it.get("b")}»'
        if it.get("comment"):
            line += f' ({it["comment"]})'
        lines.append(line)
    return ("\nПОДТВЕРЖДЁННЫЕ ЧЕЛОВЕКОМ НЕ-ДУБЛИ (никогда не объединяй "
            "эти пары):\n" + "\n".join(lines) + "\n")
