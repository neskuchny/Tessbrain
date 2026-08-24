# -*- coding: utf-8 -*-
"""Исследование аудитории (Mark → «Исследования»): вывод продукта на рынок.

Конвейер: бриф → сегменты-гипотезы (прямые/косвенные/доноры) → полевой
сбор (веб, t.me/s, VK, отзовики) → корпус реальных текстов → лексикон
(частоты считает КОД) → инсайты с ПРОВЕРЯЕМЫМИ цитатами → отчёт-документ.

Дисциплина честности (та же, что rate_origin в медиаплане и гейт в МОРМ):
- каждый инсайт помечен origin='field' ТОЛЬКО если его цитата дословно
  найдена в собранном корпусе (проверяет код, не LLM); иначе —
  origin='hypothesis' («домысел, проверить»);
- сегменты до сбора — гипотезы, не выводы; чего не собрали — пишем прямо;
- симуляция/стратегия — кандидат, мерило — реальные метрики (фаза 3).

Раны живут в data/research/<user_id>.json; исполняются asyncio-таской
в процессе (paттерн knowledge_sync: не блокируем HTTP на минуты).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_SEGMENTS = 7
_MAX_ITEMS = 40           # фрагментов корпуса на ран
_CORPUS_PROMPT_CAP = 45000
_QUOTE_MIN = 20           # минимум символов совпадения для origin='field'

_STOPWORDS = set("""
это как что для того чтобы если или его она они оно там тут еще ещё уже
только очень можно нужно надо есть быть был была были будет по на не мы вы
они наш ваш свой этот тот все всё при чем чём тем кто где когда почему
потому просто даже более менее самый может однако также тоже из-за них нас
вас вам нам мне мой моя мое так вот бы же ли до от за под над про без
""".split())


# ── Стор ранов (JSON per-user, как handoffs) ────────────────────────────

def _store_dir() -> Path:
    p = Path("data/research")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _runs_path(user_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:64] or "anon"
    return _store_dir() / f"{safe}.json"


def list_runs(user_id: str) -> List[dict]:
    try:
        return json.loads(_runs_path(user_id).read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_runs(user_id: str, runs: List[dict]) -> None:
    _runs_path(user_id).write_text(
        json.dumps(runs[-30:], ensure_ascii=False, indent=1),
        encoding="utf-8")


def get_run(user_id: str, run_id: str) -> Optional[dict]:
    return next((r for r in list_runs(user_id) if r.get("id") == run_id), None)


def _update_run(user_id: str, run_id: str, **fields) -> None:
    runs = list_runs(user_id)
    for r in runs:
        if r.get("id") == run_id:
            r.update(fields)
            r["updated_at"] = datetime.utcnow().isoformat()
    _save_runs(user_id, runs)


def _log_stage(user_id: str, run_id: str, stage: str, detail: str) -> None:
    runs = list_runs(user_id)
    for r in runs:
        if r.get("id") == run_id:
            r["stage"] = stage
            r.setdefault("stages_log", []).append(
                {"stage": stage, "detail": detail,
                 "at": datetime.utcnow().isoformat()})
    _save_runs(user_id, runs)
    logger.info(f"research {run_id} [{stage}] {detail}")


# ── Лексикон: считает код, не LLM ────────────────────────────────────────

def build_lexicon(texts: List[str], top: int = 25) -> dict:
    """Частотный словарь аудитории: униграммы и биграммы (слова ≥4 симв.,
    без стоп-слов). Это «какими словами люди сами это называют»."""
    uni: Counter = Counter()
    bi: Counter = Counter()
    for t in texts:
        words = [w for w in re.findall(r"[а-яёa-z]{4,}", (t or "").lower())
                 if w not in _STOPWORDS]
        uni.update(words)
        bi.update(f"{a} {b}" for a, b in zip(words, words[1:])
                  if a != b)
    return {
        "unigrams": [{"term": w, "count": c} for w, c in uni.most_common(top)],
        "bigrams": [{"term": w, "count": c} for w, c in bi.most_common(top)
                    if c >= 2],
    }


# ── Проверка цитат: origin='field' назначает КОД ────────────────────────

def _norm_text(s: str) -> str:
    return re.sub(r"[^а-яёa-z0-9]+", " ", (s or "").lower()).strip()


# Разбор длинной цитаты на перекрывающиеся окна.
_WIN = 40            # длина окна сравнения
_STRIDE = 20         # шаг окна (окна перекрываются вдвое)
_MIN_COVERAGE = 0.5  # доля окон, которая обязана найтись


def _windows_found(q: str, corpus_norm: str) -> List[bool]:
    """Какие окна цитаты нашлись в корпусе — по порядку."""
    windows = [q[i:i + _WIN]
               for i in range(0, max(1, len(q) - _WIN + 1), _STRIDE)]
    return [len(w) >= _QUOTE_MIN and w in corpus_norm for w in windows]


def verify_quote(quote: str, corpus_norm: str) -> bool:
    """Цитата дословно (по нормализованному тексту) есть в корпусе?

    Точное вхождение — да. Иначе для длинной цитаты допускаем, что модель
    выбросила кусок из СЕРЕДИНЫ (обрезку с краёв ловит обычная проверка
    вхождения: обрезанная с конца цитата — это префикс исходной). Но тогда
    требуем два условия сразу: НАЧАЛО И КОНЕЦ цитаты найдены в корпусе, и
    найдено не меньше половины её кусков.

    Раньше здесь стояло `q[15:60] in corpus_norm`: хватало одного окна в 45
    символов, а вся остальная цитата любой длины могла быть дописана
    моделью — метка «подтверждено дословной цитатой» ставилась всё равно.
    Это подрывало то единственное, ради чего проверка существует:
    origin='field' назначает КОД, и метка должна что-то значить.
    """
    q = _norm_text(quote)
    if len(q) < _QUOTE_MIN:
        return False
    if q in corpus_norm:
        return True
    if len(q) <= 60:
        return False
    found = _windows_found(q, corpus_norm)
    if not found or not (found[0] and found[-1]):
        # Дописанный хвост (или начало) — самый частый способ выдумать
        # цитату вокруг настоящего фрагмента.
        return False
    return sum(found) / len(found) >= _MIN_COVERAGE


def mark_insights_origin(insights: List[dict], corpus_norm: str) -> List[dict]:
    out = []
    for it in insights or []:
        if not isinstance(it, dict):
            continue
        it = dict(it)
        it["origin"] = ("field" if verify_quote(it.get("quote") or "",
                                                corpus_norm)
                        else "hypothesis")
        out.append(it)
    return out


# ── LLM-этапы ────────────────────────────────────────────────────────────

async def propose_segments(llm, *, product: str, market: str,
                           extra: str) -> List[dict]:
    """Сегменты-гипотезы: прямые, косвенные и ДОНОРЫ (у кого уже есть
    сформированное поведение рядом — критично для новых продуктов)."""
    prompt = (
        "Ты — исследователь рынка. Продукт выводится на рынок:\n"
        f"ПРОДУКТ: {product}\nРЫНОК/ГЕО: {market or 'не указан'}\n"
        + (f"КОНТЕКСТ: {extra[:3000]}\n" if extra else "")
        + "\nПредложи сегменты-ГИПОТЕЗЫ аудитории (это гипотезы для полевой "
        "проверки, не выводы). Обязательно включи сегменты-доноры: люди, у "
        "которых УЖЕ есть сформированное поведение рядом с продуктом (для "
        "нового продукта прямой проблемы может не существовать — "
        "привязываемся к понятным им проблемам).\n"
        'Ответь ТОЛЬКО JSON: {"segments": [{"name": "...", '
        '"type": "direct|indirect|donor", "who": "кто это", '
        '"pains_hypothesis": "какие боли предполагаем", '
        '"queries": ["2-4 поисковых запроса, как ИЩУТ ЭТИ ЛЮДИ (их язык)"], '
        '"tg_channels": ["публичные тг-каналы, где они могут быть (если '
        'уверен), без @"], '
        '"why": "почему сегмент перспективен"}]}\n'
        f"Не больше {_MAX_SEGMENTS} сегментов. Запросы — на языке рынка.")
    data = await llm.generate_json(prompt=prompt, temperature=0.4)
    segs = (data or {}).get("segments") if isinstance(data, dict) else None
    out = []
    for s in (segs or [])[:_MAX_SEGMENTS]:
        if isinstance(s, dict) and s.get("name"):
            out.append({
                "name": str(s["name"])[:80],
                "type": (s.get("type") if s.get("type") in
                         ("direct", "indirect", "donor") else "direct"),
                "who": str(s.get("who") or "")[:300],
                "pains_hypothesis": str(s.get("pains_hypothesis") or "")[:300],
                "queries": [str(q)[:100] for q in (s.get("queries") or [])[:4]],
                "tg_channels": [str(c)[:40] for c in
                                (s.get("tg_channels") or [])[:3]],
                "why": str(s.get("why") or "")[:300],
            })
    return out


async def extract_insights(llm, *, product: str, corpus_items: List[dict],
                           segments: List[dict]) -> dict:
    """Инсайты из корпуса: боли, язык, триггеры, возражения — КАЖДЫЙ с
    цитатой (код потом проверит цитату по корпусу и разметит origin)."""
    parts, used = [], 0
    for i, it in enumerate(corpus_items):
        chunk = f"[S{i+1} | {it.get('kind')} | {it.get('url')}]\n{it.get('text', '')}"
        if used + len(chunk) > _CORPUS_PROMPT_CAP:
            break
        parts.append(chunk)
        used += len(chunk)
    seg_names = ", ".join(s["name"] for s in segments) or "—"
    prompt = (
        f"Продукт: {product}. Сегменты-гипотезы: {seg_names}.\n"
        "Ниже — ПОЛЕВОЙ КОРПУС: реальные тексты людей и конкурентов "
        "(посты, отзывы, лендинги). Извлеки из него:\n"
        "1. pains — боли/задачи, о которых люди РЕАЛЬНО пишут\n"
        "2. language — их слова/обороты для этих тем (как говорят САМИ)\n"
        "3. triggers — что цепляет/нравится\n"
        "4. objections — возражения, страхи, на что жалуются\n"
        "5. competitors_say — как говорят конкуренты (штампы, обещания)\n"
        "6. donot — как НЕ надо заходить на эту аудиторию\n\n"
        "ЖЁСТКОЕ ПРАВИЛО: у каждого пункта — ДОСЛОВНАЯ цитата из корпуса "
        "(поле quote, копируй как есть) и source (маркер [S#]). Пункт без "
        "дословной цитаты можно дать, но quote тогда пустой — он попадёт в "
        "гипотезы. НЕ выдумывай цитат: их сверит код.\n"
        'Ответь ТОЛЬКО JSON: {"pains": [{"text": "...", "quote": "...", '
        '"source": "S1", "segment": "к какому сегменту (или пусто)"}], '
        '"language": [...], "triggers": [...], "objections": [...], '
        '"competitors_say": [...], "donot": [...]} — те же поля у всех.\n\n'
        "КОРПУС:\n" + "\n\n".join(parts))
    data = await llm.generate_json(prompt=prompt, temperature=0.2)
    return data if isinstance(data, dict) else {}


# ── Сбор корпуса ─────────────────────────────────────────────────────────

async def collect_corpus(segments: List[dict], *, product: str,
                         tg_channels: List[str], vk_groups: List[str],
                         urls: List[str], user_id: Optional[str] = None,
                         collectors=None) -> tuple[List[dict], List[str]]:
    """Полевой сбор по плану сегментов + явным источникам из брифа.
    collectors — инъекция для тестов. Возвращает (items, notes)."""
    from backend.core.marketing import collectors as C
    c = collectors or C
    items: List[dict] = []
    notes: List[str] = []
    seen_urls: set = set()

    def _add(new: List[dict], seg: Optional[str] = None) -> None:
        for it in new:
            if len(items) >= _MAX_ITEMS:
                return
            u = it.get("url") or ""
            key = (u, (it.get("text") or "")[:80])
            if key in seen_urls:
                continue
            seen_urls.add(key)
            if seg:
                it = {**it, "segment": seg}
            items.append(it)

    # Явные источники из брифа — первыми (владелец знает, где его люди)
    for ch in tg_channels[:5]:
        got = await c.telegram_channel_posts(ch)
        _add(got)
        if not got:
            notes.append(f"t.me/{ch}: не удалось собрать (канал закрыт/пуст?)")
    for g in vk_groups[:5]:
        got = await c.vk_group_posts(g, user_id=user_id)
        _add(got)
        if not got:
            notes.append(f"vk.com/{g}: не собрано (нужен сервисный ключ VK "
                         "— вкладка «Интеграции» → VK, или группа закрыта)")
    for u in urls[:6]:
        text = await c.fetch_page(u)
        if text:
            _add([{"kind": "web", "url": u, "title": u, "text": text}])
        else:
            notes.append(f"{u}: страница не прочиталась")

    # Отзовики по продукту
    _add(await c.review_search(product))

    # Поисковые запросы сегментов (язык аудитории)
    for s in segments:
        if len(items) >= _MAX_ITEMS:
            break
        for q in (s.get("queries") or [])[:2]:
            _add(await c.collect_web(q, max_results=3), seg=s["name"])
        for ch in (s.get("tg_channels") or [])[:2]:
            _add(await c.telegram_channel_posts(ch), seg=s["name"])

    if not items:
        notes.append("корпус пуст: ни один источник не отдал текстов — "
                     "отчёт будет ГИПОТЕЗАМИ без полевой опоры")
    return items, notes


# ── Отчёт ────────────────────────────────────────────────────────────────

def _fmt_insights(title: str, items: List[dict]) -> List[str]:
    if not items:
        return []
    md = [f"## {title}", ""]
    for it in items:
        icon = "📍" if it.get("origin") == "field" else "💡"
        seg = f" _(сегмент: {it['segment']})_" if it.get("segment") else ""
        md.append(f"- {icon} {it.get('text', '')}{seg}")
        if it.get("origin") == "field" and it.get("quote"):
            md.append(f"  > «{str(it['quote'])[:300]}» — {it.get('source', '')}")
    md.append("")
    return md


def render_report_md(*, product: str, market: str, segments: List[dict],
                     corpus_items: List[dict], lexicon: dict,
                     insights: dict, notes: List[str]) -> str:
    n_field = sum(1 for k in ("pains", "language", "triggers", "objections",
                              "competitors_say", "donot")
                  for it in (insights.get(k) or [])
                  if it.get("origin") == "field")
    kinds = Counter(it.get("kind") for it in corpus_items)
    md = [f"# Исследование аудитории: {product}", "",
          f"**Рынок:** {market or '—'} · **корпус:** {len(corpus_items)} "
          f"фрагментов ({', '.join(f'{k}: {v}' for k, v in kinds.items()) or 'пусто'})"
          f" · **инсайтов с полевыми цитатами:** {n_field}", "",
          "> 📍 — подтверждено дословной цитатой из поля (проверил код) · "
          "💡 — гипотеза LLM, требует проверки", ""]

    md += ["## Сегменты-гипотезы", ""]
    t_ru = {"direct": "прямой", "indirect": "косвенный", "donor": "донор"}
    for s in segments:
        md += [f"### {s['name']} ({t_ru.get(s['type'], s['type'])})",
               f"- **Кто:** {s['who']}",
               f"- **Гипотеза боли:** {s['pains_hypothesis']}",
               f"- **Почему перспективен:** {s['why']}", ""]

    if lexicon.get("unigrams"):
        md += ["## Лексикон аудитории (частоты — код, не LLM)", "",
               "Слова: " + ", ".join(
                   f"**{u['term']}** ({u['count']})"
                   for u in lexicon["unigrams"][:20]), ""]
        if lexicon.get("bigrams"):
            md += ["Обороты: " + ", ".join(
                f"«{b['term']}» ({b['count']})"
                for b in lexicon["bigrams"][:12]), ""]

    md += _fmt_insights("Боли и задачи (как о них пишут)", insights.get("pains") or [])
    md += _fmt_insights("Язык аудитории", insights.get("language") or [])
    md += _fmt_insights("Триггеры (что цепляет)", insights.get("triggers") or [])
    md += _fmt_insights("Возражения и страхи", insights.get("objections") or [])
    md += _fmt_insights("Как говорят конкуренты", insights.get("competitors_say") or [])
    md += _fmt_insights("Как НЕ надо заходить", insights.get("donot") or [])

    if corpus_items:
        md += ["## Где нашли этих людей (источники корпуса)", ""]
        seen = set()
        for it in corpus_items:
            u = it.get("url") or ""
            if u and u not in seen:
                seen.add(u)
                md.append(f"- [{it.get('kind')}] {u}")
        md.append("")
    if notes:
        md += ["## Ограничения честно", ""]
        md += [f"- {n}" for n in notes]
        md.append("")
    md += ["---",
           "_Сегменты и 💡-инсайты — гипотезы; мерило — реакция реальных "
           "людей (кампании/метрики). Симуляция персон — фаза 2._"]
    return "\n".join(md)


# ── Оркестратор ──────────────────────────────────────────────────────────

def start_research(user_id: str, *, product: str, market: str = "",
                   extra: str = "", tg_channels: Optional[List[str]] = None,
                   vk_groups: Optional[List[str]] = None,
                   urls: Optional[List[str]] = None) -> dict:
    """Создать ран и запустить фоном. Возвращает карточку рана."""
    run = {
        "id": f"res_{uuid.uuid4().hex[:10]}",
        "product": (product or "").strip()[:200],
        "market": (market or "").strip()[:120],
        "extra": (extra or "").strip()[:4000],
        "tg_channels": [str(c).strip().lstrip("@")[:40]
                        for c in (tg_channels or []) if str(c).strip()][:5],
        "vk_groups": [str(g).strip()[:60] for g in (vk_groups or [])
                      if str(g).strip()][:5],
        "urls": [str(u).strip()[:300] for u in (urls or [])
                 if str(u).strip().startswith("http")][:6],
        "status": "running", "stage": "queued", "stages_log": [],
        "created_at": datetime.utcnow().isoformat(),
    }
    runs = list_runs(user_id)
    runs.append(run)
    _save_runs(user_id, runs)
    return run


async def execute_research(user_id: str, run_id: str, *, llm=None,
                           collectors=None) -> dict:
    """Исполнить ран (вызывается фоном из роута или напрямую в тестах)."""
    run = get_run(user_id, run_id)
    if not run:
        return {"success": False, "error": "run not found"}
    if llm is None:
        from backend.core.llm.router import get_llm_router
        llm = get_llm_router()
    try:
        # 1. Сегменты-гипотезы
        _log_stage(user_id, run_id, "segments", "строю карту сегментов-гипотез")
        segments = await propose_segments(
            llm, product=run["product"], market=run["market"],
            extra=run["extra"])
        if not segments:
            raise RuntimeError("LLM не предложил сегментов")
        _log_stage(user_id, run_id, "segments",
                   f"{len(segments)} сегментов: "
                   + ", ".join(s["name"] for s in segments))

        # 2. Полевой сбор
        _log_stage(user_id, run_id, "collect",
                   "собираю корпус: веб, t.me/s, отзовики"
                   + (", VK" if (run.get("vk_groups")) else ""))
        corpus_items, notes = await collect_corpus(
            segments, product=run["product"],
            tg_channels=run.get("tg_channels") or [],
            vk_groups=run.get("vk_groups") or [],
            urls=run.get("urls") or [], user_id=user_id,
            collectors=collectors)
        _log_stage(user_id, run_id, "collect",
                   f"корпус: {len(corpus_items)} фрагментов")

        # 3. Лексикон — код
        _log_stage(user_id, run_id, "language", "считаю лексикон аудитории")
        lexicon = build_lexicon([it.get("text") or "" for it in corpus_items])

        # 4. Инсайты с цитатами; origin проверяет код
        insights: Dict[str, Any] = {}
        if corpus_items:
            _log_stage(user_id, run_id, "insights",
                       "извлекаю боли/язык/триггеры с цитатами")
            raw = await extract_insights(
                llm, product=run["product"], corpus_items=corpus_items,
                segments=segments)
            corpus_norm = _norm_text(
                " ".join(it.get("text") or "" for it in corpus_items))
            for k in ("pains", "language", "triggers", "objections",
                      "competitors_say", "donot"):
                insights[k] = mark_insights_origin(raw.get(k) or [],
                                                   corpus_norm)
        else:
            notes.append("инсайты не извлекались: нет корпуса")

        # 5. Отчёт + сохранение
        _log_stage(user_id, run_id, "report", "собираю отчёт")
        md = render_report_md(
            product=run["product"], market=run["market"], segments=segments,
            corpus_items=corpus_items, lexicon=lexicon, insights=insights,
            notes=notes)
        document_id = await _save_report_doc(user_id, run, md, insights,
                                             segments)
        _save_preset(user_id, run["product"], lexicon, insights)

        _update_run(user_id, run_id, status="done", stage="done",
                    report_markdown=md, document_id=document_id,
                    segments=segments, corpus_count=len(corpus_items),
                    sources=[{k: it.get(k) for k in ("kind", "url", "segment")}
                             for it in corpus_items],
                    # тексты корпуса (обрезанные) — субстрат для персон
                    # фазы 2: персона сшивается из реальных цитат, не из
                    # головы LLM
                    corpus=[{"kind": it.get("kind"), "url": it.get("url"),
                             "segment": it.get("segment"),
                             "text": (it.get("text") or "")[:1200]}
                            for it in corpus_items])
        return {"success": True, "run_id": run_id}
    except Exception as e:
        logger.exception(f"research {run_id} failed")
        _update_run(user_id, run_id, status="failed", error=str(e)[:500])
        return {"success": False, "error": str(e)}


async def _save_report_doc(user_id: str, run: dict, md: str,
                           insights: dict, segments: List[dict]) -> Optional[str]:
    try:
        from backend.core.documents.document_writer_agent import GeneratedDocument
        from backend.core.documents import doc_store
        now = datetime.utcnow()
        n_field = sum(1 for k in insights for it in (insights.get(k) or [])
                      if isinstance(it, dict) and it.get("origin") == "field")
        doc = GeneratedDocument(
            document_id=f"research_{uuid.uuid4().hex[:10]}",
            title=f"Исследование аудитории: {run['product']}"[:120],
            document_type="research", version="1.0",
            created_at=now, updated_at=now,
            summary=f"{len(segments)} сегментов, корпус "
                    f"{run.get('corpus_count', '—')}, "
                    f"{n_field} полевых инсайтов",
            content_markdown=md, content_html="",
            topic=run["product"], keywords=[run["product"], "исследование",
                                            "аудитория"],
            source_meetings=[], status="draft",
            sections=[{"kind": "audience_research",
                       "segments": segments, "insights": insights}],
            word_count=len(md.split()), user_id=user_id)
        doc.folder = "Исследования"
        await doc_store.save_document(doc, user_id)
        return doc.document_id
    except Exception as e:
        logger.warning(f"research doc save failed: {e}")
        return None


async def append_doc_section(user_id: str, document_id: str,
                             md_block: str) -> bool:
    """Дописать секцию в документ исследования (размер сегментов,
    факт-гейт) — версия растёт, содержимое не переписывается."""
    try:
        from backend.core.documents.document_writer_agent import GeneratedDocument
        from backend.core.documents import doc_store
        row = await doc_store.get_document(document_id, user_id)
        if not row:
            return False
        try:
            ver = f"{float(row.get('version') or 1.0) + 0.1:.1f}"
        except (TypeError, ValueError):
            ver = "1.1"
        now = datetime.utcnow()

        def _dt(v):
            try:
                return datetime.fromisoformat(
                    str(v).replace("Z", "+00:00")).replace(tzinfo=None)
            except (TypeError, ValueError):
                return now
        doc = GeneratedDocument(
            document_id=row["document_id"], title=row.get("title") or "",
            document_type=row.get("document_type") or "research",
            version=ver, created_at=_dt(row.get("created_at")),
            updated_at=now, summary=(row.get("summary") or "")[:500],
            content_markdown=(row.get("content_markdown") or "")
                             + "\n\n" + md_block,
            content_html="", topic=row.get("topic") or "",
            keywords=row.get("keywords") or [],
            source_meetings=row.get("source_meetings") or [],
            sections=row.get("sections") or [],
            status=row.get("status") or "draft",
            word_count=len((row.get("content_markdown") or "").split()),
            user_id=user_id)
        doc.folder = row.get("folder") or "Исследования"
        await doc_store.save_document(doc, user_id)
        return True
    except Exception as e:
        logger.warning(f"append_doc_section failed: {e}")
        return False


def _save_preset(user_id: str, product: str, lexicon: dict,
                 insights: dict) -> None:
    """Заготовка «Аудитория: X» — тон/язык для контента Марка и документов
    (upsert по названию)."""
    try:
        from backend.core.documents.fill_engine import (
            list_context_presets, save_context_preset)
        field_pains = [it["text"] for it in (insights.get("pains") or [])
                       if it.get("origin") == "field"][:6]
        terms = [u["term"] for u in (lexicon.get("unigrams") or [])[:15]]
        if not field_pains and not terms:
            return
        text = ("ЯЗЫК И БОЛИ АУДИТОРИИ (из полевого исследования):\n"
                + (f"Слова аудитории: {', '.join(terms)}\n" if terms else "")
                + "".join(f"- Боль: {p}\n" for p in field_pains))
        title = f"Аудитория: {product}"[:120]
        existing = next((p for p in list_context_presets(user_id)
                         if p.get("title") == title), None)
        save_context_preset(user_id, title=title, text=text,
                            preset_id=existing.get("id") if existing else None)
    except Exception as e:
        logger.debug(f"research preset skipped: {e}")
