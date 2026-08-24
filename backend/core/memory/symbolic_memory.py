# -*- coding: utf-8 -*-
"""Символическая память с drill-down — перенос идеи TencentDB-Agent-Memory (G1+G2).

Длинные tool-run'ы и цепочки рассуждений раздувают контекст многословными
логами. Приём из TencentDB-Agent-Memory: свернуть сырьё в компактный
СИМВОЛИЧЕСКИЙ граф — по одной строке-символу на шаг с коротким `node_id`, а
полное сырьё убрать в сторону. Агент рассуждает по символам (−~60% токенов),
а к деталям «доныривает» по id (drill-down). Каждый вышестоящий уровень
абстракции хранит ЯВНЫЙ указатель на нижний (сквозной provenance, G2):
    сцена  ▸  [шаги]  ▸  сырьё
    [s1] план закупок ▸3
      [n1] search(склад) ⟨2480c⟩
      [n2] compute(остатки) ⟨1120c⟩
      [n3] draft(заказ) ⟨640c⟩

Модуль ЧИСТО аддитивный и без I/O/сети: структура данных + детерминированные
алгоритмы, покрыт юнит-тестами. Ничего в живых путях не меняет — его можно
подключать точечно (напр. `AutoSession.symbolic_view()`), не трогая горячие
пути. Никаких Date.now/random — вывод детерминирован (id по счётчику).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_WS = re.compile(r"\s+")


def one_line(text: str, limit: int) -> str:
    """Первая непустая строка: схлопнутые пробелы, усечение до limit символов."""
    for raw_line in (text or "").splitlines():
        line = _WS.sub(" ", raw_line).strip()
        if line:
            if len(line) <= limit:
                return line
            return line[: max(1, limit - 1)].rstrip() + "…"
    return ""


@dataclass
class _Node:
    node_id: str
    kind: str                     # "step" (L0/L1 атом) | "scene" (L2+ агрегат)
    symbol: str                   # однострочный символ для рассуждения
    raw: str = ""                 # полное сырьё (только у атомов)
    children: List[str] = field(default_factory=list)  # указатели вниз (drill-down)
    meta: Dict = field(default_factory=dict)


class SymbolicMemory:
    """Сворачивает многословные шаги в символический граф с drill-down.

    add(raw)          → атом (L0-сырьё + L1-символ), возвращает node_id
    group(ids, label) → узел-сцена с явными указателями вниз (G2)
    render()          → компактный символический вид (то, чем рассуждает агент)
    expand(node_id)   → drill-down к сырью атома / собранному сырью сцены
    trace(node_id)    → цепочка указателей вниз до листьев-атомов
    token_savings()   → грубая оценка экономии символов
    """

    def __init__(self, *, symbol_chars: int = 80):
        self._symbol_chars = max(16, int(symbol_chars))
        self._nodes: Dict[str, _Node] = {}
        self._order: List[str] = []   # порядок узлов в верхнем потоке
        self._seq: Dict[str, int] = {}  # счётчик id на префикс (n… атомы, s… сцены)

    def _next_id(self, prefix: str) -> str:
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}{self._seq[prefix]}"

    # ── построение ──────────────────────────────────────────────────────
    def add(self, raw, *, label: str = "", kind: str = "step",
            meta: Optional[Dict] = None) -> str:
        """Добавить атом (полное сырьё + однострочный символ). → node_id."""
        raw = raw if isinstance(raw, str) else str(raw)
        nid = self._next_id("n")
        symbol = one_line(label, self._symbol_chars) or one_line(raw, self._symbol_chars)
        if not symbol:
            symbol = "∅"
        # маркер объёма — сразу видно, что за символом много деталей
        if len(raw) > self._symbol_chars:
            symbol = f"{symbol} ⟨{len(raw)}c⟩"
        self._nodes[nid] = _Node(
            nid, "step" if kind != "scene" else "scene",
            symbol, raw=raw, meta=dict(meta or {}))
        self._order.append(nid)
        return nid

    def group(self, child_ids: List[str], *, label: str) -> str:
        """Собрать вышестоящий узел-сцену с ЯВНЫМИ указателями вниз (G2).

        Дети убираются из верхнего потока (их представляет сцена, вставляемая
        на позицию первого ребёнка), но остаются в графе — доступ через
        expand/trace. Порядок детей сохраняется как передан."""
        valid = [c for c in child_ids if c in self._nodes]
        if not valid:
            raise ValueError("group: нет валидных child_ids")
        sid = self._next_id("s")
        symbol = one_line(label, self._symbol_chars) or f"сцена ({len(valid)})"
        symbol = f"{symbol} ▸{len(valid)}"
        self._nodes[sid] = _Node(sid, "scene", symbol, children=list(valid))
        # заменяем детей в верхнем потоке сценой (на месте первого ребёнка)
        child_set = set(valid)
        new_order: List[str] = []
        inserted = False
        for x in self._order:
            if x in child_set:
                if not inserted:
                    new_order.append(sid)
                    inserted = True
                # сам ребёнок уходит из верхнего потока
            else:
                new_order.append(x)
        if not inserted:            # дети уже были сгруппированы под другой сценой
            new_order.append(sid)
        self._order = new_order
        return sid

    # ── чтение ──────────────────────────────────────────────────────────
    def render(self) -> str:
        """Компактный символический вид (то, чем рассуждает агент)."""
        out: List[str] = []
        for nid in self._order:
            self._render_node(nid, 0, out, set())
        return "\n".join(out)

    def _render_node(self, nid: str, depth: int, out: List[str],
                     seen: set) -> None:
        node = self._nodes.get(nid)
        if not node or nid in seen:
            return
        seen.add(nid)
        out.append(f"{'  ' * depth}[{nid}] {node.symbol}")
        for c in node.children:
            self._render_node(c, depth + 1, out, seen)

    def expand(self, node_id: str) -> str:
        """Drill-down: сырьё атома или собранное сырьё всех атомов сцены."""
        node = self._nodes.get(node_id)
        if node is None:
            raise KeyError(node_id)
        if node.kind == "scene":
            return "\n".join(self.expand(c) for c in node.children)
        return node.raw

    def trace(self, node_id: str) -> List[str]:
        """Цепочка указателей вниз до листьев-атомов (сквозной provenance)."""
        node = self._nodes.get(node_id)
        if node is None:
            raise KeyError(node_id)
        if node.kind != "scene":
            return [node_id]
        out: List[str] = []
        for c in node.children:
            out.extend(self.trace(c))
        return out

    def get(self, node_id: str) -> Optional[_Node]:
        return self._nodes.get(node_id)

    def token_savings(self) -> Dict[str, int]:
        """Грубая оценка: символы полного сырья vs символического вида."""
        full = sum(len(n.raw) for n in self._nodes.values() if n.kind != "scene")
        symbolic = len(self.render())
        saved = max(0, full - symbolic)
        pct = int(round(100 * saved / full)) if full else 0
        return {"full_chars": full, "symbolic_chars": symbolic,
                "saved_chars": saved, "saved_pct": pct}
