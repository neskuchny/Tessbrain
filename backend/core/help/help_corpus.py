# -*- coding: utf-8 -*-
"""
Корпус справки «Гид по Tessbrain» — изолирован от памяти компании.

Очень простая структура (как и просил владелец): папки
`data/help_corpus/<section>/<subsection>.md` + `manifest.json` = граф
разделов→подразделов. Каждый .md — человеческим языком: что это, зачем,
польза, как пользоваться (куда кликать), что на входе → что на выходе.

Поиск — BM25 (без модели эмбеддингов, надёжно в любом окружении) по чанкам
(разбивка по `##`-блокам). Для узких вопросов — чанки, для общих — ещё и
целый документ раздела. Векторов сознательно нет: маленькому курируемому
корпусу BM25 достаточно (и эмбеддинги могут быть недоступны офлайн).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Запросы, при которых стоит добавить обзор целого раздела (широкий вопрос).
_BROAD_HINTS = (
    "что такое", "что это", "зачем", "чем полезна", "чем полезен", "как работает",
    "обзор", "расскажи", "для чего", "что умеет", "что ты умеешь", "чем ты",
    "что тут", "что здесь", "не понимаю", "overview", "what is", "how does",
)

_MAX_CHUNK = 1800


def _corpus_dir() -> Path:
    env = os.getenv("HELP_CORPUS_DIR")
    if env:
        return Path(env)
    # backend/core/help/help_corpus.py → parents[2] == backend/
    return Path(__file__).resolve().parents[2] / "data" / "help_corpus"


def _chunk_markdown(text: str) -> list[str]:
    """Разбить документ по `##`-заголовкам; крупные блоки дорезать."""
    text = (text or "").strip()
    if not text:
        return []
    # Отрезаем frontmatter (--- ... ---) если есть.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:].strip()
    # Разбивка по заголовкам второго уровня, заголовок остаётся с блоком.
    parts = re.split(r"\n(?=## )", text)
    chunks: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= _MAX_CHUNK:
            chunks.append(p)
        else:
            # длинный блок — дорезаем существующим чанкером
            try:
                from backend.core.documents.document_indexer import (
                    split_text_into_chunks,
                )
                for c in split_text_into_chunks(p, _MAX_CHUNK, 200):
                    if c.get("text", "").strip():
                        chunks.append(c["text"].strip())
            except Exception:
                chunks.append(p[:_MAX_CHUNK])
    return chunks


class HelpCorpus:
    def __init__(self) -> None:
        self._loaded = False
        self.docs: dict[str, dict[str, Any]] = {}   # slug -> {meta, text}
        self.chunks: list[dict[str, Any]] = []       # {doc_id, text, meta}
        self._bm25 = None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        base = _corpus_dir()
        manifest = base / "manifest.json"
        entries: list[dict[str, Any]] = []
        if manifest.exists():
            try:
                entries = json.loads(manifest.read_text(encoding="utf-8")).get("docs", [])
            except Exception as e:
                logger.warning("help_corpus: bad manifest: %s", e)
        else:
            logger.warning("help_corpus: no manifest at %s", manifest)

        from backend.core.search.bm25_searcher import BM25Searcher
        self._bm25 = BM25Searcher()   # in-memory, без persistence
        for ent in entries:
            slug = str(ent.get("slug") or "").strip()
            rel = str(ent.get("file") or "").strip()
            if not slug or not rel:
                continue
            fp = base / rel
            if not fp.exists():
                logger.warning("help_corpus: missing file %s", fp)
                continue
            try:
                raw = fp.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("help_corpus: read %s failed: %s", fp, e)
                continue
            meta = {
                "slug": slug,
                "section": ent.get("section", ""),
                "subsection": ent.get("subsection", ""),
                "title": ent.get("title", slug),
                "lang": ent.get("lang", "ru"),
            }
            self.docs[slug] = {"meta": meta, "text": raw}
            for i, ch in enumerate(_chunk_markdown(raw)):
                doc_id = f"{slug}#{i}"
                cmeta = {**meta, "chunk_index": i}
                self.chunks.append({"doc_id": doc_id, "text": ch, "meta": cmeta})
                try:
                    self._bm25.add_document(doc_id, f"{meta['title']}\n{ch}", cmeta)
                except Exception as e:
                    logger.debug("help_corpus: bm25 add failed for %s: %s", doc_id, e)
        logger.info("help_corpus loaded: %d docs, %d chunks",
                    len(self.docs), len(self.chunks))

    def _is_broad(self, query: str) -> bool:
        q = (query or "").lower()
        if any(h in q for h in _BROAD_HINTS):
            return True
        return len(q.split()) <= 4

    def retrieve(self, query: str, *, k: int = 4) -> dict[str, Any]:
        """Вернуть {chunks:[{text,meta}], parents:[{slug,title,text}]}.

        chunks — лучшие фрагменты; parents — обзоры целых разделов (для
        широких вопросов) по slug'ам найденных чанков."""
        self._load()
        out_chunks: list[dict[str, Any]] = []
        if self._bm25 is not None and query.strip():
            try:
                results = self._bm25.search(query, top_k=k)
            except Exception as e:
                logger.warning("help_corpus search failed: %s", e)
                results = []
            for r in results:
                out_chunks.append({"text": getattr(r, "text", ""),
                                   "meta": getattr(r, "metadata", {}) or {},
                                   "score": getattr(r, "score", 0.0)})

        parents: list[dict[str, Any]] = []
        if self._is_broad(query):
            seen = set()
            for c in out_chunks:
                slug = c["meta"].get("slug")
                if slug and slug not in seen and slug in self.docs:
                    seen.add(slug)
                    d = self.docs[slug]
                    parents.append({"slug": slug, "title": d["meta"]["title"],
                                    "text": d["text"]})
                if len(parents) >= 2:
                    break
        return {"chunks": out_chunks, "parents": parents}

    def list_sections(self) -> list[dict[str, Any]]:
        self._load()
        return [d["meta"] for d in self.docs.values()]

    def stats(self) -> dict[str, int]:
        self._load()
        return {"docs": len(self.docs), "chunks": len(self.chunks)}


_CORPUS: Optional[HelpCorpus] = None


def get_help_corpus() -> HelpCorpus:
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = HelpCorpus()
    return _CORPUS


def _reset_for_tests() -> None:
    global _CORPUS
    _CORPUS = None


__all__ = ["HelpCorpus", "get_help_corpus"]
