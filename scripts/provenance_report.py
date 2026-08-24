# -*- coding: utf-8 -*-
"""Отчёт покрытия провенанса в графе знаний.

Инвариант из синтеза Карпатого/Anthropic: «у каждого утверждения есть
источник — или явная пометка, что это вывод». Прежде чем ПРИНУЖДАТЬ к
этому на записи, нужно честно узнать текущее покрытие: какая доля узлов
и рёбер вообще несёт происхождение (meeting_id / source / origin /
source_doc / created_from), и какие типы — главные сироты.

Использование (офлайн, читает networkx-графы с диска, БД не нужна):
    python scripts/provenance_report.py [--user-id <uuid>]

Без --user-id — по всем per-user графам, найденным в data/.
Результат — таблица в stdout; ничего не изменяет (read-only).
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from collections import Counter

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Поля, считающиеся провенансом (узел/ребро знает, откуда оно)
_PROV_FIELDS = ("meeting_id", "source", "origin", "source_doc",
                "source_docs", "created_from", "provenance")


def _has_prov(attrs: dict) -> bool:
    return any(str(attrs.get(f) or "").strip() for f in _PROV_FIELDS
               if not isinstance(attrs.get(f), (list, set, dict))) or any(
        attrs.get(f) for f in _PROV_FIELDS
        if isinstance(attrs.get(f), (list, set, dict)))


def _graph_files(user_id: str | None) -> list[pathlib.Path]:
    try:
        from backend.core.store.tenant_paths import graph_path_for_user
        if user_id:
            p = pathlib.Path(graph_path_for_user(user_id))
            return [p] if p.exists() else []
    except Exception:
        pass
    data = _ROOT / "data"
    # per-user и org графы: любые *.graphml/*.gpickle/knowledge_graph*
    out: list[pathlib.Path] = []
    for pat in ("**/knowledge_graph*.gpickle", "**/knowledge_graph*.graphml",
                "**/*.gpickle"):
        out.extend(data.glob(pat))
    return sorted(set(out))


def _load_graph(path: pathlib.Path):
    import networkx as nx
    try:
        if path.suffix == ".graphml":
            return nx.read_graphml(path)
        import pickle
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        print(f"  ! {path.name}: не читается ({e})")
        return None


def report(user_id: str | None) -> int:
    files = _graph_files(user_id)
    if not files:
        print("Графов не найдено (data/ пуст или путь другой) — отчёт пуст, "
              "это честное состояние, а не ошибка.")
        return 0
    for path in files:
        g = _load_graph(path)
        if g is None:
            continue
        n_total = g.number_of_nodes()
        e_total = g.number_of_edges()
        n_prov = sum(1 for _, a in g.nodes(data=True) if _has_prov(a))
        e_prov = sum(1 for *_ , a in g.edges(data=True) if _has_prov(a))
        orphan_labels = Counter(
            str(a.get("_label") or a.get("label") or "?")
            for _, a in g.nodes(data=True) if not _has_prov(a))
        print(f"\n=== {path} ===")
        print(f"узлы: {n_prov}/{n_total} с провенансом "
              f"({(100 * n_prov / n_total) if n_total else 0:.0f}%)")
        print(f"рёбра: {e_prov}/{e_total} с провенансом "
              f"({(100 * e_prov / e_total) if e_total else 0:.0f}%)")
        if orphan_labels:
            print("узлы-сироты по типам (топ-10):")
            for lbl, cnt in orphan_labels.most_common(10):
                print(f"  {lbl:24s} {cnt}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", default=None)
    sys.exit(report(ap.parse_args().user_id))
