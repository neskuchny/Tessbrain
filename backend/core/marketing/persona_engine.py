# -*- coding: utf-8 -*-
"""Персоны-симуляции (Mark «Исследования», фаза 2).

Из корпуса исследования собираются карточки персон — НЕ выдуманные
маркетинговые аватары, а сшитые из реальных цитат и паттернов корпуса
(боли персоны проходят ту же проверку цитат, что инсайты: field/
hypothesis назначает код). Персоны сохраняются как переиспользуемые
«аудитории».

Панель реакций: оффер/пост/лендинг → каждая персона реагирует НА ЯЗЫКЕ
корпуса (что цепляет, что отталкивает, возражения, «как со мной надо
говорить») + обязательный скептик-адверсарий, который атакует и посыл,
и оптимизм панели — симулированные персоны без него льстят.

Честно: симуляция — ГЕНЕРАТОР ГИПОТЕЗ, не измерение. Мерило — реакция
реальных людей (CTR/конверсия кампании, фаза 3).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.marketing import research_engine as _re
from backend.core.marketing.research_engine import (
    _norm_text,
    get_run,
    verify_quote,
)

logger = logging.getLogger(__name__)

_MAX_PERSONAS = 6
_CORPUS_PROMPT_CAP = 40000


# ── Стор персон (переиспользуемые «аудитории») ──────────────────────────

def _personas_path(user_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:64] or "anon"
    return _re._store_dir() / f"personas_{safe}.json"


def list_personas(user_id: str) -> List[dict]:
    try:
        return json.loads(_personas_path(user_id).read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_personas(user_id: str, personas: List[dict]) -> None:
    _personas_path(user_id).write_text(
        json.dumps(personas[-40:], ensure_ascii=False, indent=1),
        encoding="utf-8")


def delete_persona(user_id: str, persona_id: str) -> bool:
    items = list_personas(user_id)
    kept = [p for p in items if p.get("id") != persona_id]
    if len(kept) == len(items):
        return False
    _save_personas(user_id, kept)
    return True


# ── Сборка персон из корпуса ─────────────────────────────────────────────

def _corpus_prompt(corpus: List[dict]) -> str:
    parts, used = [], 0
    for i, it in enumerate(corpus):
        chunk = (f"[S{i+1} | {it.get('kind')} | "
                 f"{it.get('segment') or 'без сегмента'}]\n{it.get('text', '')}")
        if used + len(chunk) > _CORPUS_PROMPT_CAP:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts)


async def build_personas(user_id: str, *, run_id: str, llm=None) -> dict:
    """Персоны из корпуса завершённого исследования. Боли каждой персоны
    проходят проверку цитат кодом (field/hypothesis)."""
    if llm is None:
        from backend.core.llm.router import get_llm_router
        llm = get_llm_router()
    run = get_run(user_id, run_id)
    if not run:
        return {"success": False, "error": "исследование не найдено"}
    if run.get("status") != "done":
        return {"success": False, "error": "исследование ещё не завершено"}
    corpus = run.get("corpus") or []
    if not corpus:
        return {"success": False,
                "error": "в ране нет корпуса (старый формат или пустой сбор) "
                         "— перезапустите исследование"}
    segments = run.get("segments") or []
    seg_names = ", ".join(s.get("name", "") for s in segments) or "—"

    prompt = (
        f"Продукт: {run.get('product')}. Сегменты: {seg_names}.\n"
        "Ниже — полевой корпус (реальные тексты аудитории и конкурентов). "
        f"Собери {min(_MAX_PERSONAS, max(3, len(segments)))} ПЕРСОН — "
        "собирательные портреты РЕАЛЬНЫХ людей из корпуса, не выдуманные "
        "аватары. Требования:\n"
        "1. Каждая персона привязана к сегменту.\n"
        "2. pains — с ДОСЛОВНЫМИ цитатами из корпуса (код их сверит; "
        "без цитаты — пометится гипотезой).\n"
        "3. lexicon — слова/обороты, которыми ЭТА персона говорит "
        "(из корпуса).\n"
        "4. Не приукрашивай: скепсис, лень, недоверие — если они видны "
        "в корпусе, они должны быть в персоне.\n"
        'Ответь ТОЛЬКО JSON: {"personas": [{"name": "имя + роль '
        '(например «Марина, руководитель агентства 12 чел»)", '
        '"segment": "...", "portrait": "кто это, контекст жизни/работы", '
        '"goals": ["чего хочет"], '
        '"pains": [{"text": "...", "quote": "дословная цитата или пусто", '
        '"source": "S#"}], '
        '"lexicon": ["слова и обороты этой персоны"], '
        '"objections": ["что её оттолкнёт / чему не поверит"], '
        '"media": ["где обитает: каналы/площадки из корпуса"], '
        '"tone": "как с ней говорить (стиль)"}]}\n\n'
        "КОРПУС:\n" + _corpus_prompt(corpus))
    try:
        data = await llm.generate_json(prompt=prompt, temperature=0.4)
    except Exception as e:
        return {"success": False, "error": f"LLM недоступен: {e}"}
    raw = (data or {}).get("personas") if isinstance(data, dict) else None
    if not raw:
        return {"success": False, "error": "не удалось собрать персоны"}

    corpus_norm = _norm_text(" ".join(it.get("text") or "" for it in corpus))
    personas: List[dict] = []
    for p in raw[:_MAX_PERSONAS]:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        pains = []
        for pain in (p.get("pains") or [])[:6]:
            if not isinstance(pain, dict):
                continue
            pains.append({
                "text": str(pain.get("text") or "")[:300],
                "quote": str(pain.get("quote") or "")[:300],
                "source": str(pain.get("source") or "")[:20],
                "origin": ("field" if verify_quote(pain.get("quote") or "",
                                                   corpus_norm)
                           else "hypothesis"),
            })
        personas.append({
            "id": f"pers_{uuid.uuid4().hex[:10]}",
            "run_id": run_id,
            "product": run.get("product"),
            "name": str(p["name"])[:120],
            "segment": str(p.get("segment") or "")[:80],
            "portrait": str(p.get("portrait") or "")[:600],
            "goals": [str(g)[:200] for g in (p.get("goals") or [])[:5]],
            "pains": pains,
            "lexicon": [str(w)[:60] for w in (p.get("lexicon") or [])[:12]],
            "objections": [str(o)[:200] for o in (p.get("objections") or [])[:6]],
            "media": [str(m)[:120] for m in (p.get("media") or [])[:6]],
            "tone": str(p.get("tone") or "")[:300],
            "field_pains": sum(1 for x in pains if x["origin"] == "field"),
            "created_at": datetime.utcnow().isoformat(),
        })
    if not personas:
        return {"success": False, "error": "не удалось собрать персоны"}

    # upsert: старые персоны этого рана заменяются новыми
    existing = [p for p in list_personas(user_id)
                if p.get("run_id") != run_id]
    _save_personas(user_id, existing + personas)
    return {"success": True, "personas": personas}


def _simulations_path(user_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:64] or "anon"
    return _re._store_dir() / f"simulations_{safe}.json"


def list_simulations(user_id: str) -> List[dict]:
    """История панелей реакций (полные результаты, новые в конце)."""
    try:
        return json.loads(
            _simulations_path(user_id).read_text(encoding="utf-8"))
    except Exception:
        return []


# ── Панель реакций ───────────────────────────────────────────────────────

def _persona_card(p: dict) -> str:
    pains = "; ".join(
        f"{x['text']}" + (f" («{x['quote']}»)" if x.get("quote")
                          and x.get("origin") == "field" else "")
        for x in (p.get("pains") or []))
    return (f"ПЕРСОНА: {p.get('name')}\nСегмент: {p.get('segment')}\n"
            f"Портрет: {p.get('portrait')}\n"
            f"Цели: {'; '.join(p.get('goals') or [])}\n"
            f"Боли: {pains}\n"
            f"Её лексикон: {', '.join(p.get('lexicon') or [])}\n"
            f"Что оттолкнёт: {'; '.join(p.get('objections') or [])}\n"
            f"Тон для неё: {p.get('tone')}")


async def simulate_reactions(user_id: str, *, persona_ids: List[str],
                             message: str, llm=None) -> dict:
    """Панель реакций: посыл → реакция каждой персоны (на её языке) +
    скептик-адверсарий против оптимизма панели."""
    if llm is None:
        from backend.core.llm.router import get_llm_router
        llm = get_llm_router()
    message = (message or "").strip()
    if len(message) < 10:
        return {"success": False, "error": "посыл слишком короткий"}
    all_p = list_personas(user_id)
    panel = [p for p in all_p if p.get("id") in set(persona_ids)][:6]
    if not panel:
        return {"success": False, "error": "выберите персоны для панели"}

    cards = "\n\n".join(_persona_card(p) for p in panel)
    prompt = (
        "Панель реакций на маркетинговый посыл. Ниже персоны (собраны из "
        "реальных текстов аудитории) и ПОСЫЛ. Для КАЖДОЙ персоны дай "
        "честную реакцию ОТ ПЕРВОГО ЛИЦА, её лексиконом (см. карточку). "
        "Не льсти автору посыла: скепсис, «не понял», «закрыл вкладку» — "
        "валидные реакции, если персона так себя ведёт.\n"
        'Ответь ТОЛЬКО JSON: {"reactions": [{"persona": "имя как в '
        'карточке", "first_impression": "первая мысль (её словами)", '
        '"hooks": ["что зацепило"], "repels": ["что оттолкнуло/непонятно"], '
        '"objections": ["возражения"], "trust_1_5": 1-5, '
        '"would_respond": true/false, '
        '"say_it_better": "как переформулировать посыл ДЛЯ НЕЁ (её '
        'словами)"}]}\n\n'
        f"ПОСЫЛ:\n{message[:3000]}\n\nПЕРСОНЫ:\n{cards}")
    try:
        data = await llm.generate_json(prompt=prompt, temperature=0.5)
    except Exception as e:
        return {"success": False, "error": f"LLM недоступен: {e}"}
    reactions = []
    raw = (data or {}).get("reactions") if isinstance(data, dict) else None
    for r in (raw or [])[:len(panel)]:
        if not isinstance(r, dict):
            continue
        try:
            trust = max(1, min(5, int(r.get("trust_1_5") or 3)))
        except (TypeError, ValueError):
            trust = 3
        reactions.append({
            "persona": str(r.get("persona") or "")[:120],
            "first_impression": str(r.get("first_impression") or "")[:400],
            "hooks": [str(h)[:200] for h in (r.get("hooks") or [])[:5]],
            "repels": [str(h)[:200] for h in (r.get("repels") or [])[:5]],
            "objections": [str(h)[:200] for h in (r.get("objections") or [])[:5]],
            "trust_1_5": trust,
            "would_respond": bool(r.get("would_respond")),
            "say_it_better": str(r.get("say_it_better") or "")[:500],
        })

    # Скептик-адверсарий: отдельный вызов, атакует посыл И панель
    skeptic = None
    try:
        sk_prompt = (
            "Ты — скептик-адверсарий в исследовании. Ниже маркетинговый "
            "посыл и реакции симулированных персон. Твоя работа — атаковать: "
            "(1) где посыл слаб/неправдоподобен/непонятен холодному "
            "человеку; (2) где панель персон ЛЬСТИТ (симуляции склонны к "
            "оптимизму); (3) что в реальности пойдёт не так.\n"
            'Ответь ТОЛЬКО JSON: {"attacks": ["удар по посылу"], '
            '"panel_flattery": ["где реакции подозрительно добрые"], '
            '"reality_check": "что скорее всего будет в реальной кампании", '
            '"fix_first": "ОДНА правка посыла, которую сделать первой"}\n\n'
            f"ПОСЫЛ:\n{message[:3000]}\n\n"
            f"РЕАКЦИИ ПАНЕЛИ:\n{json.dumps(reactions, ensure_ascii=False)[:6000]}")
        sk = await llm.generate_json(prompt=sk_prompt, temperature=0.3)
        if isinstance(sk, dict):
            skeptic = {
                "attacks": [str(a)[:300] for a in (sk.get("attacks") or [])[:5]],
                "panel_flattery": [str(a)[:300]
                                   for a in (sk.get("panel_flattery") or [])[:4]],
                "reality_check": str(sk.get("reality_check") or "")[:500],
                "fix_first": str(sk.get("fix_first") or "")[:400],
            }
    except Exception as e:
        logger.warning(f"skeptic failed: {e}")

    # Скептик — не украшение, а единственная защита от главной ловушки
    # синтетических панелей: они льстят автору. Если он не отработал,
    # результат обязан сказать это вслух, иначе панель без критики
    # выглядит как панель с критикой — самый опасный вид тишины.
    skeptic_missing = not (skeptic and (skeptic.get("attacks")
                                        or skeptic.get("panel_flattery")))

    result = {
        "success": True,
        "message": message[:500],
        "reactions": reactions,
        "skeptic": skeptic,
        "skeptic_missing": skeptic_missing,
        "skeptic_warning": (
            "Скептик не отработал — панель НЕ была оспорена. Синтетические "
            "панели склонны льстить автору, поэтому этим реакциям верить "
            "нельзя: повторите прогон."
        ) if skeptic_missing else "",
        "avg_trust": (round(sum(r["trust_1_5"] for r in reactions)
                            / len(reactions), 1) if reactions else None),
        "would_respond": sum(1 for r in reactions if r["would_respond"]),
        "panel_size": len(reactions),
        "disclaimer": "Симуляция — генератор гипотез, не измерение: "
                      "мерило — реакция реальных людей (кампания/метрики).",
        "at": datetime.utcnow().isoformat(),
    }
    # история симуляций: ПОЛНЫЙ результат (реакции+скептик) — раньше
    # хранилась выжимка, и панель «не сохранялась» для пользователя
    try:
        hist_path = _simulations_path(user_id)
        try:
            hist = json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            hist = []
        hist.append({**result,
                     "panel": [p.get("name") for p in panel]})
        hist_path.write_text(json.dumps(hist[-20:], ensure_ascii=False,
                                        indent=1), encoding="utf-8")
    except Exception:
        logger.debug("simulation history save failed", exc_info=True)
    return result
