# -*- coding: utf-8 -*-
"""
Корпоративный словарь синонимов/акронимов (перенос идеи Glean,
docs/GLEAN_ADOPTION.md №3): «КП» = «коммерческое предложение»,
«ДЗ» = «дебиторская задолженность», прозвища людей и проектов.

Зачем: поиск и заземление промахиваются, когда компания говорит на
своём жаргоне. Словарь расширяет запрос алиасами ДО поиска —
детерминированно, без LLM (term↔aliases в обе стороны).

Per-user JSON (tenant_paths) + file_lock/atomic — как остальные сторы.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

MAX_TERMS = 500


def _norm(s: str) -> str:
    return str(s or "").strip().lower()


class CompanyGlossary:
    """{канонический термин: [алиасы]} с двунаправленным расширением."""

    def __init__(self, persist_path: str):
        self._path = persist_path

    def _load(self) -> Dict[str, List[str]]:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f).get("terms", {})
        except (OSError, ValueError):
            return {}

    def list(self) -> Dict[str, List[str]]:
        return self._load()

    def set_term(self, term: str, aliases: List[str]) -> dict:
        """Создать/заменить термин. Пустые алиасы выбрасываются."""
        term = str(term or "").strip()
        clean = [str(a).strip() for a in aliases or [] if str(a).strip()]
        if not term or not clean:
            raise ValueError("term и хотя бы один alias обязательны")
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            terms = self._load()
            if term not in terms and len(terms) >= MAX_TERMS:
                raise ValueError(f"словарь полон ({MAX_TERMS} терминов)")
            terms[term] = clean
            atomic_write_json(self._path, {"terms": terms})
        return {"term": term, "aliases": clean, "total": len(terms)}

    def delete_term(self, term: str) -> bool:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            terms = self._load()
            if term not in terms:
                return False
            del terms[term]
            atomic_write_json(self._path, {"terms": terms})
            return True

    # -- расширение запроса ----------------------------------------------
    def expand_query(self, query: str) -> dict:
        """Найти в запросе термины/алиасы словаря и вернуть расширение.

        Двунаправленно: «сделай КП» → +«коммерческое предложение»;
        «коммерческое предложение» → +«КП». Возвращает
        {expanded: bool, additions: [...], query_expanded: str} — сам
        запрос НЕ переписывается (additions добавляются к поиску отдельно,
        синтез видит исходную формулировку)."""
        q = _norm(query)
        if not q:
            return {"expanded": False, "additions": [],
                    "query_expanded": query}
        words = set(re.split(r"[^\wа-яё]+", q))
        additions: List[str] = []
        for term, aliases in self._load().items():
            t = _norm(term)
            alias_norms = [_norm(a) for a in aliases]
            # термин в запросе → добавить алиасы
            if t in words or (len(t) > 3 and t in q):
                additions.extend(a for a in aliases
                                 if _norm(a) not in q)
            # алиас в запросе → добавить термин и братьев-алиасов
            elif any(a in words or (len(a) > 3 and a in q)
                     for a in alias_norms):
                if t not in q:
                    additions.append(term)
                additions.extend(a for a in aliases if _norm(a) not in q)
        # дедуп с сохранением порядка
        seen = set()
        additions = [a for a in additions
                     if not (_norm(a) in seen or seen.add(_norm(a)))]
        return {
            "expanded": bool(additions),
            "additions": additions[:8],
            "query_expanded": (query + " " + " ".join(additions[:8])).strip()
            if additions else query,
        }


def glossary_for_user(user_id: str) -> CompanyGlossary:
    from backend.core.store.tenant_paths import glossary_path_for_user
    return CompanyGlossary(glossary_path_for_user(user_id))
