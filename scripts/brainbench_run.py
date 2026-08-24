#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BrainBench (gbrain-evals): наш поиск на ЧУЖОЙ линейке.

Зачем. Сравнивать наши числа с числами gbrain в лоб нельзя — метрики
разные. Единственный честный способ узнать «кто лучше» — прогнать наш
стек на их харнессе: тот же корпус, те же запросы, те же формулы P@5 /
R@5. Их харнесс открыт (github.com/garrytan/gbrain-evals) и явно
приглашает чужие системы через интерфейс адаптера.

Что это. Точный Python-порт их раннера (buildQueries + precision/recall
из eval/runner/{multi-adapter,types}.ts) поверх НАШИХ продовых модулей:
bm25_searcher, rrf_fusion, enumerative_detect. Плюс детерминированный
граф из markdown-ссылок страниц — рёбра без единого вызова модели
(перенос приёма gbrain: у них extract тоже без LLM).

ИСТОРИЯ ЧИСЛА (три акта, каждый честнее предыдущего).
Акт 1: первая версия адаптера показала 0.729 — но держалось это на двух
правилах, захардкоженных В ЭТОМ СКРИПТЕ и отсутствующих в проде («вопрос
с Who → только соседи-person», «перечислительный вопрос → только граф,
без добора»). Все 145 запросов корпуса порождены четырьмя шаблонами
«Who …?», золото всегда из людей — правила были подогнаны под генератор
вопросов. Абляции: без них 0.26–0.40, BM25 не добавлял ничего.
Акт 2: правила сняты, адаптер приведён к проду как есть → 0.240 / 0.806.
Акт 3: сами правила оказались не глупостью, а продуктовыми пробелами —
«кто…» и по-русски всегда спрашивает про людей, а полный структурный
ответ и вправду не надо разбавлять. Поэтому они вернулись, но КАК ФИЧИ
ПРОДУКТА, с русскими тестами и выключателями:
  - enumerative_detect: реляционные вопросы без слова «все» («кто был на
    встрече», «who attended…») — RELATIONAL_DETECT, default on;
  - answer_type: «кто/who» → тип ответа person — чистый модуль;
  - confident_graph: полный структурный ответ без добора —
    CONFIDENT_GRAPH, default OFF до проверки на своих данных.
Этот скрипт больше не содержит НИ ОДНОГО собственного правила ранжирования:
он вызывает те же модули, что прод, и печатает лесенку «вчера / сегодня /
с флагом», чтобы вклад каждой фичи был виден отдельно.

Границы честности:
- адаптер видит ТОЛЬКО публичные поля страницы (slug/type/title/
  compiled_truth/timeline) — _facts вычищаются до передачи, как их
  sanitizePage; gold живёт только в скорере;
- в адаптере НЕТ ни одного правила, придуманного под этот корпус:
  каналы, веса и условие перечислительности взяты из продового
  оркестратора без изменений;
- это ПОРТ их харнесса, не их раннер: сравнение с их публикацией несёт
  риск расхождения порта — поэтому рядом всегда бежит их же baseline
  (BM25-only), портированный из того же файла: если наш порт врёт, он
  врёт обоим одинаково. Сверено с их настоящим раннером: их адаптер
  grep-only даёт 17.1/62.4, наш BM25-порт — 16.1/59.7, расхождение
  порта ~1–3 пункта;
- векторного канала в этом прогоне нет (нет модели эмбеддингов в
  окружении) — это написано в выводе, а не умолчано;
- реляционные паттерны детектора и правило «кто → person» писались от
  русского языка продукта (тесты — на русских вопросах), а этот корпус
  используется как ВАЛИДАЦИЯ на чужих данных, не как источник правил.
  Риск остаётся и напечатан: подтверждением обобщения станет только
  прогон на наших русских наборах без регрессии.

Запуск:
  git clone --depth 1 https://github.com/garrytan/gbrain-evals /tmp/ge
  python scripts/brainbench_run.py --corpus /tmp/ge/eval/data/world-v1
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import types
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP_K = 5


def _load_module(name: str, relpath: str):
    for pkg in ("backend", "backend.core", "backend.core.search"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = m
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Корпус (порт loadCorpus) ────────────────────────────────────────────

def load_corpus(corpus_dir: str) -> List[dict]:
    pages = []
    for fname in sorted(os.listdir(corpus_dir)):
        if not fname.endswith(".json") or fname.startswith("_"):
            continue
        with open(os.path.join(corpus_dir, fname), encoding="utf-8") as f:
            p = json.load(f)
        if isinstance(p.get("timeline"), list):
            p["timeline"] = "\n".join(p["timeline"])
        if isinstance(p.get("compiled_truth"), list):
            p["compiled_truth"] = "\n\n".join(p["compiled_truth"])
        p["title"] = str(p.get("title") or "")
        p["compiled_truth"] = str(p.get("compiled_truth") or "")
        p["timeline"] = str(p.get("timeline") or "")
        pages.append(p)
    return pages


def sanitize_page(p: dict) -> dict:
    """Порт sanitizePage: адаптер НЕ видит _facts и frontmatter."""
    return {k: p.get(k) for k in
            ("slug", "type", "title", "compiled_truth", "timeline")}


# ── Запросы (точный порт buildQueries) ──────────────────────────────────

def build_queries(pages: List[dict]) -> List[dict]:
    existing = {p["slug"] for p in pages}

    def flt(slugs):
        return [s for s in (slugs or []) if s in existing]
    queries = []
    counter = [0]

    def qid():
        counter[0] += 1
        return f"q-{counter[0]:04d}"

    def add(text, relevant):
        if relevant:
            queries.append({"id": qid(), "text": text,
                            "gold": sorted(set(relevant))})

    for p in pages:
        f = p.get("_facts") or {}
        if f.get("type") == "meeting":
            add(f"Who attended {p['title']}?", flt(f.get("attendees")))
    for p in pages:
        f = p.get("_facts") or {}
        if f.get("type") == "company":
            add(f"Who works at {p['title']}?",
                flt((f.get("employees") or []) + (f.get("founders") or [])))
    for p in pages:
        f = p.get("_facts") or {}
        if f.get("type") == "company":
            add(f"Who invested in {p['title']}?", flt(f.get("investors")))
    for p in pages:
        f = p.get("_facts") or {}
        if f.get("type") == "company":
            add(f"Who advises {p['title']}?", flt(f.get("advisors")))
    return queries


# ── Метрики (порт precisionAtK / recallAtK) ─────────────────────────────

def precision_at_k(ranked: List[str], relevant: set, k: int) -> float:
    top = ranked[:k]
    if not top:
        return 0.0
    return sum(1 for s in top if s in relevant) / len(top)


def recall_at_k(ranked: List[str], relevant: set, k: int) -> float:
    if not relevant:
        return 0.0
    top = ranked[:k]
    return sum(1 for s in top if s in relevant) / len(relevant)


# ── Адаптер 1: их baseline (BM25-only, наш продовый BM25) ───────────────

class Bm25OnlyAdapter:
    """Контроль порта: их EXT-1 baseline на нашем BM25. Если порт врёт —
    врёт и здесь, и сравнение с их публикацией это покажет."""
    name = "bm25-only (наш BM25Searcher)"

    def init(self, raw_pages: List[dict]) -> None:
        B = _load_module("backend.core.search.bm25_searcher",
                         "backend/core/search/bm25_searcher.py")
        self.bm25 = B.BM25Searcher()
        for p in raw_pages:
            self.bm25.add_document(
                doc_id=p["slug"],
                text=f"{p['title']}\n{p['compiled_truth']}\n{p['timeline']}",
                metadata={"type": p.get("type")})

    def query(self, q: dict) -> List[str]:
        hits = self.bm25.search(q["text"], top_k=TOP_K * 3)
        return [h.doc_id for h in hits]


# ── Адаптер 2: наш стек (BM25 + граф из ссылок + RRF + детектор) ────────

_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:people|companies|meetings|concepts|deal|projects)/[a-z0-9\-]+)\)")


class TessentAdapter:
    """Наш продовый механизм, перенесённый на их корпус. Правил ранжирования
    здесь НЕТ — только вызовы продовых модулей:

    - типизированные рёбра — ДЕТЕРМИНИРОВАННО из markdown-ссылок страниц
      (ни одного вызова модели; перенос приёма gbrain, у них extract
      links тоже без LLM);
    - реляционный/перечислительный запрос распознаёт НАШ enumerative_detect
      (те же регэкспы, что в проде; за RELATIONAL_DETECT) → графовый канал
      получает больший вес;
    - «кто → person» решает НАШ answer_type; полный структурный ответ без
      добора — НАШ confident_graph (за CONFIDENT_GRAPH, в проде default
      OFF) — та же связка, что в hybrid_search_orchestrator;
    - слияние каналов — НАШ RRFFusion с теми же весами, что в проде
      (BALANCED: bm25=1.0, graph=0.8; при срабатывании детектора 1.3).

    Тай-брейк документирован: соседи сущности подаются в RRF в порядке
    возрастания слага. RRF берёт ранг из порядка входа (enumerate в
    rrf_fusion.fuse), а обход множества в Python зависит от рандомизации
    хешей — без сортировки один и тот же прогон давал 0.727…0.730.
    """
    name = "tessent (bm25+graph+rrf, без вектора)"
    # None → флаги читаются из окружения (режим --serve, их раннер).
    # Словарь → лесенка вариантов в run(): каждый вариант выставляет свои.
    env: Optional[Dict[str, str]] = None

    def init(self, raw_pages: List[dict]) -> None:
        if self.env is not None:
            os.environ.update(self.env)
        B = _load_module("backend.core.search.bm25_searcher",
                         "backend/core/search/bm25_searcher.py")
        self._R = _load_module("backend.core.search.rrf_fusion",
                               "backend/core/search/rrf_fusion.py")
        self._det = _load_module("backend.core.search.enumerative_detect",
                                 "backend/core/search/enumerative_detect.py")
        self._ans = _load_module("backend.core.search.answer_type",
                                 "backend/core/search/answer_type.py")
        self._conf = _load_module("backend.core.search.confident_graph",
                                  "backend/core/search/confident_graph.py")
        self.bm25 = B.BM25Searcher()
        self.by_slug: Dict[str, dict] = {}
        self.title_to_slug: Dict[str, str] = {}
        # slug → set(slug): ненаправленные рёбра «страница упоминает страницу»
        self.edges: Dict[str, set] = {}
        for p in raw_pages:
            slug = p["slug"]
            self.by_slug[slug] = p
            self.title_to_slug.setdefault(p["title"].strip().lower(), slug)
            text = f"{p['title']}\n{p['compiled_truth']}\n{p['timeline']}"
            self.bm25.add_document(doc_id=slug, text=text,
                                   metadata={"type": p.get("type")})
            for _name, target in _LINK_RE.findall(text):
                if target == slug:
                    continue
                self.edges.setdefault(slug, set()).add(target)
                self.edges.setdefault(target, set()).add(slug)

    def _seed(self, query_text: str) -> Optional[str]:
        """Сущность запроса: самое длинное название страницы в тексте."""
        qt = query_text.lower()
        best, best_len = None, 0
        for title, slug in self.title_to_slug.items():
            if title and title in qt and len(title) > best_len:
                best, best_len = slug, len(title)
        return best

    def _graph_channel(self, q: dict) -> List[Dict[str, Any]]:
        """Соседи сущности запроса. Никакой фильтрации по типу: продовый
        графовый канал тоже её не делает, а вывести «нужны люди» из того,
        что вопрос начинается с Who, значит подстроиться под генератор
        вопросов этого корпуса."""
        seed = self._seed(q["text"])
        if not seed:
            return []
        return [{"id": slug, "score": 1.0}
                for slug in sorted(self.edges.get(seed, set()))]

    def query(self, q: dict) -> List[str]:
        bm25_hits = [{"id": h.doc_id, "score": h.score}
                     for h in self.bm25.search(q["text"], top_k=TOP_K * 3)]
        graph_hits = self._graph_channel(q)

        verdict = self._det.detect(q["text"])
        graph_weight = 1.3 if verdict else 0.8

        fusion = self._R.RRFFusion(k=60)
        if bm25_hits:
            fusion.add_results("bm25", bm25_hits, weight=1.0,
                               id_field="id", score_field="score")
        if graph_hits:
            fusion.add_results("graph", graph_hits, weight=graph_weight,
                               id_field="id", score_field="score")
        fused = fusion.fuse(top_k=TOP_K * 3)

        # Уверенный структурный ответ — та же связка, что в оркестраторе:
        # детектор + тип ответа + узлы нужного типа → сузить выдачу до
        # структурного множества. Логика и гарантии — в confident_graph.
        if self._conf.confident_enabled() and verdict:
            exp = self._ans.expected_answer_type(q["text"])
            if exp == self._ans.PERSON:
                typed_ids = [
                    h["id"] for h in graph_hits
                    if (self.by_slug.get(h["id"]) or {}).get("type") == "person"]
                picked = self._conf.pick_confident(
                    fused, typed_ids,
                    verdict_truthy=True, expected_type=exp)
                if picked is not None:
                    fused = picked

        return [r.doc_id if hasattr(r, "doc_id") else r["id"] for r in fused]


class YesterdayAdapter(TessentAdapter):
    """Прод до сегодняшних фич: реляционный детектор выключен."""
    name = "  вчера: без реляционного детектора"
    env = {"RELATIONAL_DETECT": "off", "CONFIDENT_GRAPH": "off"}


class ProdDefaultAdapter(TessentAdapter):
    """Прод с сегодняшними дефолтами: детектор on, уверенный ответ OFF."""
    name = "  сегодня (дефолты прода)"
    env = {"RELATIONAL_DETECT": "on", "CONFIDENT_GRAPH": "off"}


class ConfidentOnAdapter(TessentAdapter):
    """Плюс CONFIDENT_GRAPH=on — флаг, в проде выключенный по умолчанию
    до проверки на своих данных."""
    name = "  плюс CONFIDENT_GRAPH=on (флаг)"
    env = {"RELATIONAL_DETECT": "on", "CONFIDENT_GRAPH": "on"}


# ── Прогон ──────────────────────────────────────────────────────────────

def run(corpus_dir: str) -> Dict[str, Any]:
    pages = load_corpus(corpus_dir)
    queries = build_queries(pages)
    public_pages = [sanitize_page(p) for p in pages]

    # Лесенка: вклад каждой фичи виден отдельной строкой.
    adapters = [Bm25OnlyAdapter(), YesterdayAdapter(),
                ProdDefaultAdapter(), ConfidentOnAdapter()]

    results: Dict[str, Any] = {"corpus": corpus_dir, "pages": len(pages),
                               "queries": len(queries), "top_k": TOP_K,
                               "adapters": {}}
    for adapter in adapters:
        adapter.init(public_pages)          # только публичные поля
        p_sum = r_sum = 0.0
        for q in queries:
            ranked = adapter.query({"id": q["id"], "text": q["text"]})
            relevant = set(q["gold"])       # gold — только у скорера
            p_sum += precision_at_k(ranked, relevant, TOP_K)
            r_sum += recall_at_k(ranked, relevant, TOP_K)
        n = len(queries) or 1
        results["adapters"][adapter.name] = {
            "P@5": round(p_sum / n, 4), "R@5": round(r_sum / n, 4)}
    return results


def serve() -> int:
    """Режим моста для их НАСТОЯЩЕГО раннера (bun/TypeScript).

    Их харнесс ждёт адаптер на TypeScript. Переписать наш стек на TS
    значило бы мерить чужую копию нашей системы: расхождение копии никто
    не проверит, и весь смысл прогона на чужой линейке пропадёт. Поэтому
    TS-адаптер тонкий — он поднимает этот процесс и разговаривает с ним
    строками JSON, а ищут наши продовые модули.

    Протокол (по строке JSON в обе стороны, чтобы не тащить зависимостей):
        →  {"cmd":"init","pages":[<публичные страницы>]}
        ←  {"ok":true,"pages":240}
        →  {"cmd":"query","text":"Who attended X?","top_k":15}
        ←  {"ok":true,"docs":["slug", ...]}
    EOF на входе — выход. Ни сети, ни файлов, ни состояния между
    запусками: детерминизм, которого требует их CONTRIBUTING.
    """
    adapter = TessentAdapter()
    ready = False
    # readline(), а НЕ «for line in sys.stdin»: итерация по файловому
    # объекту читает с упреждением и в диалоговом протоколе встаёт —
    # ответа ждёт клиент, а мы ждём, пока добьётся внутренний буфер.
    while True:
        line = sys.stdin.readline()
        if not line:                       # EOF — клиент закрыл канал
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            cmd = msg.get("cmd")
            if cmd == "init":
                adapter.init([sanitize_page(p) for p in msg.get("pages") or []])
                ready = True
                out = {"ok": True, "pages": len(adapter.by_slug)}
            elif cmd == "query":
                if not ready:
                    raise RuntimeError("query до init")
                docs = adapter.query({"id": msg.get("id") or "",
                                      "text": msg.get("text") or ""})
                k = int(msg.get("top_k") or TOP_K * 3)
                out = {"ok": True, "docs": docs[:k]}
            elif cmd == "name":
                out = {"ok": True, "name": adapter.name}
            else:
                raise RuntimeError(f"неизвестная команда: {cmd!r}")
        except Exception as exc:                      # noqa: BLE001
            # Ошибку возвращаем в канал, а не роняем мост: их раннер
            # должен увидеть причину, а не «процесс умер».
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true",
                    help="мост для их TypeScript-раннера (JSON построчно)")
    ap.add_argument("--corpus", required=False,
                    help="путь к gbrain-evals/eval/data/world-v1")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.serve:
        return serve()
    if not args.corpus:
        ap.error("нужен --corpus (или --serve для моста)")
    if not os.path.isdir(args.corpus):
        print(f"корпус не найден: {args.corpus}\n"
              "клонируйте: git clone --depth 1 "
              "https://github.com/garrytan/gbrain-evals", file=sys.stderr)
        return 2
    res = run(args.corpus)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"BrainBench (порт харнесса gbrain-evals) — корпус "
          f"{res['pages']} страниц, {res['queries']} реляционных запросов, "
          f"top-{res['top_k']}")
    for name, m in res["adapters"].items():
        print(f"  {name:44s} P@5 {m['P@5']:.3f}   R@5 {m['R@5']:.3f}")
    print("\nОговорки:")
    print("  · это наш порт их харнесса, не их раннер. Контроль: их адаптер "
          "grep-only\n    в их настоящем раннере даёт 17.1/62.4, наш "
          "BM25-порт — 16.1/59.7.")
    print("  · векторного канала в прогоне нет (нет модели эмбеддингов) — "
          "у gbrain он есть.")
    print("  · правила писались от живых вопросов к продукту (он двуязычный: "
          "русский и\n    английский), этот корпус — валидация; обобщение "
          "подтвердит только прогон\n    на наших наборах без регрессии.")
    print("  · CONFIDENT_GRAPH в проде по умолчанию ВЫКЛЮЧЕН: режим меняет "
          "состав выдачи,\n    включим после проверки на своих данных.")
    print("  · для справки, их публикация на этом же корпусе и тех же 145 "
          "запросах:\n    gbrain 0.491/0.979, vector-grep-rrf-fusion "
          "0.178/0.651, grep-only 0.171/0.624,\n    vector 0.108/0.407.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
