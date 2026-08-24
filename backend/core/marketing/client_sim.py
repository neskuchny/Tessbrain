# -*- coding: utf-8 -*-
"""Симуляция клиентов, сегментов и партнёров.

Три круга собеседников, все — из РЕАЛЬНЫХ данных мозга, ничего не выдумываем:

1. **Реальные клиенты** — Client-узлы графа тенанта (из встреч и CRM).
   Досье собирается из свойств узла, его связей и снапшотов привязанных
   людей. С клиентом можно «поговорить» (симуляция от его лица с железными
   правилами честности) и прогнать оффер через панель реакций.
2. **Сегменты** — группировка реальных клиентов по РЕАЛЬНЫМ атрибутам
   (industry/category/segment/status). Клиенты без атрибутов честно
   попадают в «без атрибутов», а не рассовываются по выдуманным группам.
3. **Гипотезные группы рынка** — для выбранного рынка LLM строит
   потенциальные группы клиентов ПОД НАШ ПРОДУКТ (продукт берётся из
   снапшота компании — реальный). Каждая группа явно помечена
   origin="hypothesis": это генератор гипотез, мерило — реальные продажи.

Партнёры: человек, с которым были встречи, — это Person-узел со слепком
(twin). С ним можно провести симуляцию переговоров по продажам и собрать
пакет подготовки (концепция продукта под партнёра, КП, условия, план
переговоров) — с обязательной разметкой [из данных]/[гипотеза].

Дисклеймер везде один: симуляция на основе накопленных данных, не мнение
реального человека. Решения проверяются реальным контактом.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DISCLAIMER = ("Это симуляция на основе накопленных данных о клиенте/рынке, "
              "а не мнение реального человека. Проверяйте реальным контактом.")

_SEGMENT_ATTRS = ("industry", "segment", "category", "status", "type")
_MAX_PANEL = 6
_HISTORY_CAP = 16


# ── Стор (гипотезные группы + история симуляций) ────────────────────────

def _store_dir() -> Path:
    p = Path("data/client_sim")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(user_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:64] or "anon"
    return _store_dir() / f"{safe}.json"


def _load(user_id: str) -> Dict[str, Any]:
    try:
        return json.loads(_path(user_id).read_text(encoding="utf-8"))
    except Exception:
        return {"market_groups": [], "simulations": []}


def _save(user_id: str, data: Dict[str, Any]) -> None:
    data["market_groups"] = (data.get("market_groups") or [])[-30:]
    data["simulations"] = (data.get("simulations") or [])[-40:]
    tmp = _path(user_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    import os
    os.replace(tmp, _path(user_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Реальные клиенты из графа ────────────────────────────────────────────

def _node_str(n: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = n.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


async def list_clients(user_id: str) -> Dict[str, Any]:
    """Client-узлы графа тенанта + сегменты по реальным атрибутам.

    Пустой граф → честный пустой список (не выдумываем клиентов)."""
    from backend.core.store.graph_view import merged_graph_view_for_user

    clients: List[Dict[str, Any]] = []
    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    try:
        _tid = user_id or None
        nodes = await gb.get_all_nodes_async(label="Client", limit=1000,
                                             tenant_id=_tid,
                                             strict_tenant=bool(_tid)) or []
        for n in nodes:
            nid = _node_str(n, "id")
            name = _node_str(n, "name", "title")
            if not (nid and name):
                continue
            c = {"id": nid, "name": name}
            for attr in _SEGMENT_ATTRS + ("description", "size", "stage"):
                v = _node_str(n, attr)
                if v:
                    c[attr] = v
            clients.append(c)
    except Exception:
        logger.warning("client_sim: клиенты из графа недоступны",
                       exc_info=True)
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass

    # сегменты по первому заполненному реальному атрибуту
    segments: Dict[str, List[str]] = {}
    for c in clients:
        key = ""
        for attr in _SEGMENT_ATTRS:
            if c.get(attr):
                key = f"{attr}: {c[attr]}"
                break
        segments.setdefault(key or "без атрибутов", []).append(c["id"])
    return {"clients": clients,
            "segments": [{"name": k, "client_ids": v, "count": len(v)}
                         for k, v in sorted(segments.items(),
                                            key=lambda kv: -len(kv[1]))]}


async def _node_edges(gb, node_id: str) -> List[Dict[str, str]]:
    """Рёбра узла (обе стороны): [{other_id, type, direction}]. Тип ребра
    в networkx лежит в ключе `_type` (не `type`) — проверено graph_export."""
    out: List[Dict[str, str]] = []
    nx_g = getattr(gb, "nx_graph", None)
    if nx_g is not None:
        try:
            for a, b, data in nx_g.edges(data=True):
                if str(a) == node_id:
                    out.append({"other_id": str(b), "direction": "out",
                                "type": str((data or {}).get("_type")
                                            or (data or {}).get("type") or "")})
                elif str(b) == node_id:
                    out.append({"other_id": str(a), "direction": "in",
                                "type": str((data or {}).get("_type")
                                            or (data or {}).get("type") or "")})
            return out[:60]
        except Exception:
            logger.debug("client_sim: nx edges failed", exc_info=True)
    driver = getattr(gb, "driver", None)
    if driver is None:
        return out
    try:
        # AsyncDriver — только async with / async for (см. graph_export)
        async with driver.session() as s:
            result = await s.run(
                "MATCH (a)-[r]-(b) WHERE a.id = $nid "
                "RETURN b.id AS o, type(r) AS ty, "
                "startNode(r).id = $nid AS is_out LIMIT 60",
                {"nid": node_id})
            async for rec in result:
                out.append({"other_id": str(rec["o"]),
                            "direction": "out" if rec["is_out"] else "in",
                            "type": str(rec["ty"] or "")})
    except Exception:
        logger.debug("client_sim: neo4j edges failed", exc_info=True)
    return out


async def client_dossier(user_id: str, client_id: str) -> Dict[str, Any]:
    """Досье клиента: свойства узла + соседи по графу + снапшоты привязанных
    людей (ТОЛЬКО из кэша — без генерации, это read-only путь). Без LLM."""
    from backend.core.sleep.enhanced_snapshot import (
        get_enhanced_snapshot_generator,
    )
    from backend.core.store.graph_view import merged_graph_view_for_user

    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    try:
        _tid = user_id or None
        node = None
        names: Dict[str, Dict[str, str]] = {}
        for label in ("Client", "Person", "Product", "Project", "Task",
                      "Department", "Company"):
            try:
                for n in (await gb.get_all_nodes_async(
                        label=label, limit=2000, tenant_id=_tid,
                        strict_tenant=bool(_tid)) or []):
                    nid = _node_str(n, "id")
                    if not nid:
                        continue
                    names[nid] = {"name": _node_str(n, "name", "title"),
                                  "label": label}
                    if nid == client_id and label == "Client":
                        node = n
            except Exception:
                logger.debug("client_sim: nodes %s unavailable", label,
                             exc_info=True)
        if node is None:
            return {"status": "error",
                    "message": "клиент не найден в графе этого аккаунта"}

        edges = await _node_edges(gb, client_id)
        related = []
        person_ids = []
        for e in edges:
            other = names.get(e["other_id"])
            if not other or not other["name"]:
                continue
            related.append({"name": other["name"], "kind": other["label"],
                            "relation": e["type"] or "связан",
                            "direction": e["direction"]})
            if other["label"] == "Person":
                person_ids.append(e["other_id"])

        # снапшоты контактных лиц — строго из кэша (никакой генерации)
        contacts = []
        try:
            gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
            cached = getattr(gen, "_person_snapshots", None) or {}
            for pid in person_ids[:5]:
                snap = cached.get(pid)
                if snap is None:
                    continue
                contacts.append({
                    "name": str(getattr(snap, "name", "") or ""),
                    "role": str(getattr(snap, "role", "") or ""),
                    "strengths": list(getattr(snap, "strengths", None)
                                      or [])[:3]})
        except Exception:
            logger.debug("client_sim: contact snapshots skipped",
                         exc_info=True)
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass

    dossier: Dict[str, Any] = {"status": "success", "id": client_id,
                               "name": _node_str(node, "name", "title")}
    for attr in _SEGMENT_ATTRS + ("description", "size", "stage",
                                  "problem", "goal", "facts"):
        v = node.get(attr)
        if v:
            dossier[attr] = v
    if related:
        dossier["related"] = related[:20]
    if contacts:
        dossier["contacts"] = contacts
    return dossier


def _dossier_card(d: Dict[str, Any]) -> str:
    """Карточка клиента для промпта — только реально заполненные поля."""
    lines = [f"КЛИЕНТ: {d.get('name')}"]
    for attr, title in (("industry", "Отрасль"), ("segment", "Сегмент"),
                        ("category", "Категория"), ("status", "Статус"),
                        ("stage", "Стадия сделки"), ("size", "Размер"),
                        ("description", "Описание"), ("problem", "Проблема"),
                        ("goal", "Цель")):
        if d.get(attr):
            lines.append(f"- {title}: {str(d[attr])[:300]}")
    facts = d.get("facts")
    if isinstance(facts, list) and facts:
        lines.append("- Факты: " + "; ".join(str(f)[:150] for f in facts[:5]))
    for r in (d.get("related") or [])[:10]:
        lines.append(f"- Связь: {r['relation']} → {r['kind']} «{r['name']}»")
    for c in (d.get("contacts") or [])[:3]:
        s = f"- Контактное лицо: {c['name']}"
        if c.get("role"):
            s += f" ({c['role']})"
        lines.append(s)
    return "\n".join(lines)


# ── Продукт компании (реальная опора для гипотез) ────────────────────────

async def _company_context(user_id: str) -> str:
    """Продукт/рынок/модель из снапшота компании. Пусто — честно пусто."""
    from backend.core.sleep.enhanced_snapshot import (
        get_enhanced_snapshot_generator,
    )
    from backend.core.store.graph_view import merged_graph_view_for_user

    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    try:
        gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
        snap = await gen.get_company_snapshot()
    except Exception:
        logger.debug("client_sim: company snapshot unavailable",
                     exc_info=True)
        return ""
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass
    if snap is None:
        return ""
    parts = []
    if getattr(snap, "name", ""):
        parts.append(f"Компания: {snap.name}")
    if getattr(snap, "industry", ""):
        parts.append(f"Отрасль: {snap.industry}")
    for p in (getattr(snap, "products", None) or [])[:4]:
        if isinstance(p, dict) and p.get("name"):
            s = f"Продукт: {p['name']}"
            if p.get("description"):
                s += f" — {str(p['description'])[:200]}"
            if p.get("target_audience"):
                s += f" (ЦА: {str(p['target_audience'])[:120]})"
            parts.append(s)
    for attr, title in (("business_model", "Бизнес-модель"),
                        ("target_market", "Целевой рынок"),
                        ("revenue_model", "Модель выручки")):
        v = getattr(snap, attr, "")
        if v:
            parts.append(f"{title}: {str(v)[:200]}")
    return "\n".join(parts)


# ── Гипотезные группы рынка ──────────────────────────────────────────────

async def build_market_groups(user_id: str, *, market: str,
                              product: str = "") -> Dict[str, Any]:
    """Потенциальные группы клиентов под наш продукт на выбранном рынке.

    Продукт — из снапшота компании (реальный) либо из явного описания.
    Группы — ГИПОТЕЗЫ (origin=hypothesis), сохраняются и дальше могут
    участвовать в панели реакций наравне с реальными сегментами."""
    market = (market or "").strip()
    if len(market) < 3:
        return {"status": "error", "message": "опишите рынок (мин. 3 символа)"}
    company = await _company_context(user_id)
    if not company and not (product or "").strip():
        return {"status": "error",
                "message": ("в мозге нет данных о продукте компании — "
                            "опишите продукт явно в поле product")}

    from backend.core.llm.router import get_llm_router
    llm = get_llm_router()
    prompt = (
        "Ты — аналитик рынка. По РЕАЛЬНОМУ описанию продукта компании "
        "построй потенциальные группы клиентов на заданном рынке.\n"
        "Правила честности: группы — гипотезы для проверки, не факты. "
        "Не приписывай группам выдуманную статистику и численность. "
        "У каждой группы укажи, ЧЕМ проверить гипотезу (какой реальный "
        "контакт/эксперимент подтвердит).\n"
        'Ответь ТОЛЬКО JSON: {"groups": [{"name": "название группы", '
        '"who": "кто это (должность/тип компании/контекст)", '
        '"pains": ["боли, которые закрывает наш продукт"], '
        '"buying_trigger": "что заставляет купить", '
        '"objections": ["вероятные возражения"], '
        '"channel": "где их искать", '
        '"validate_by": "как проверить гипотезу реальным контактом", '
        '"fit_1_5": 1-5}]}\n\n'
        f"РЫНОК: {market[:500]}\n\n"
        f"ПРОДУКТ КОМПАНИИ (из данных мозга):\n{company[:3000]}\n"
        + (f"\nДОП. ОПИСАНИЕ ПРОДУКТА ОТ ПОЛЬЗОВАТЕЛЯ:\n{product[:1500]}"
           if (product or "").strip() else ""))
    # язык пользователя: значения переводим, ключи схемы — нет
    from backend.core.llm.lang import lang_instruction, resolve_answer_lang
    prompt += lang_instruction(await resolve_answer_lang(user_id),
                               json_values=True)
    try:
        data = await llm.generate_json(prompt=prompt, temperature=0.4)
    except Exception as e:
        return {"status": "error", "message": f"LLM недоступен: {e}"}

    groups = []
    for g in ((data or {}).get("groups") or [])[:8]:
        if not isinstance(g, dict) or not str(g.get("name") or "").strip():
            continue
        try:
            fit = max(1, min(5, int(g.get("fit_1_5") or 3)))
        except (TypeError, ValueError):
            fit = 3
        groups.append({
            "id": str(uuid.uuid4())[:8],
            "origin": "hypothesis",           # ничего не выдаём за факт
            "market": market[:200],
            "name": str(g.get("name"))[:120],
            "who": str(g.get("who") or "")[:400],
            "pains": [str(p)[:200] for p in (g.get("pains") or [])[:5]],
            "buying_trigger": str(g.get("buying_trigger") or "")[:300],
            "objections": [str(o)[:200]
                           for o in (g.get("objections") or [])[:5]],
            "channel": str(g.get("channel") or "")[:200],
            "validate_by": str(g.get("validate_by") or "")[:300],
            "fit_1_5": fit,
            "created_at": _now(),
        })
    if not groups:
        return {"status": "error",
                "message": "модель не вернула ни одной группы — попробуйте "
                           "конкретнее описать рынок"}
    store = _load(user_id)
    store["market_groups"] = (store.get("market_groups") or []) + groups
    _save(user_id, store)
    return {"status": "success", "groups": groups, "disclaimer": DISCLAIMER}


def list_market_groups(user_id: str) -> List[dict]:
    return _load(user_id).get("market_groups") or []


def delete_market_group(user_id: str, group_id: str) -> bool:
    store = _load(user_id)
    items = store.get("market_groups") or []
    kept = [g for g in items if g.get("id") != group_id]
    if len(kept) == len(items):
        return False
    store["market_groups"] = kept
    _save(user_id, store)
    return True


def _group_card(g: Dict[str, Any]) -> str:
    lines = [f"ГРУППА (гипотеза): {g.get('name')}",
             f"- Кто: {g.get('who') or '—'}"]
    if g.get("pains"):
        lines.append("- Боли: " + "; ".join(g["pains"]))
    if g.get("buying_trigger"):
        lines.append(f"- Триггер покупки: {g['buying_trigger']}")
    if g.get("objections"):
        lines.append("- Возражения: " + "; ".join(g["objections"]))
    return "\n".join(lines)


# ── Панель реакций: оффер → клиенты/сегменты/группы ─────────────────────

async def simulate_offer(user_id: str, *, offer: str,
                         client_ids: Optional[List[str]] = None,
                         group_ids: Optional[List[str]] = None
                         ) -> Dict[str, Any]:
    """Реакция панели (реальные клиенты и/или гипотезные группы) на оффер.

    Карточки собираются из реальных досье; сама реакция — симуляция LLM
    с обязательным скептиком (иначе панель льстит автору)."""
    offer = (offer or "").strip()
    if len(offer) < 10:
        return {"status": "error", "message": "оффер слишком короткий"}

    cards: List[str] = []
    members: List[Dict[str, str]] = []
    for cid in (client_ids or [])[:_MAX_PANEL]:
        d = await client_dossier(user_id, cid)
        if d.get("status") == "success":
            cards.append(_dossier_card(d))
            members.append({"id": cid, "name": d["name"], "kind": "client"})
    groups = {g["id"]: g for g in list_market_groups(user_id)}
    for gid in (group_ids or [])[:_MAX_PANEL]:
        g = groups.get(gid)
        if g:
            cards.append(_group_card(g))
            members.append({"id": gid, "name": g["name"], "kind": "group"})
    if not cards:
        return {"status": "error",
                "message": ("не выбран ни один клиент/группа с данными — "
                            "панели не из чего собраться")}

    from backend.core.llm.router import get_llm_router
    llm = get_llm_router()
    prompt = (
        "Панель реакций B2B. Ниже карточки клиентов/групп (собраны из "
        "РЕАЛЬНЫХ данных компании) и ПРЕДЛОЖЕНИЕ. Для КАЖДОЙ карточки дай "
        "честную реакцию лица, принимающего решение: скепсис, «нам не "
        "надо», «дорого», «уже есть подрядчик» — валидные реакции.\n"
        "Опирайся только на то, что есть в карточке; чего не знаешь о "
        "клиенте — не выдумывай, а отрази как открытый вопрос.\n"
        'Ответь ТОЛЬКО JSON: {"reactions": [{"name": "имя как в карточке", '
        '"first_reaction": "первая реакция от первого лица", '
        '"interest_1_5": 1-5, "objections": ["возражения"], '
        '"open_questions": ["что о клиенте неизвестно и влияет на исход"], '
        '"what_would_close": "что должно быть в оффере, чтобы купил", '
        '"next_step": "реалистичный следующий шаг"}], '
        '"skeptic": {"weakest_point": "самое слабое место оффера", '
        '"fix_first": "что чинить первым"}}\n\n'
        f"ПРЕДЛОЖЕНИЕ:\n{offer[:3000]}\n\n"
        "КАРТОЧКИ:\n" + "\n\n".join(cards))
    from backend.core.llm.lang import lang_instruction, resolve_answer_lang
    prompt += lang_instruction(await resolve_answer_lang(user_id),
                               json_values=True)
    try:
        data = await llm.generate_json(prompt=prompt, temperature=0.5)
    except Exception as e:
        return {"status": "error", "message": f"LLM недоступен: {e}"}

    reactions = []
    for r in ((data or {}).get("reactions") or [])[:len(cards)]:
        if not isinstance(r, dict):
            continue
        try:
            interest = max(1, min(5, int(r.get("interest_1_5") or 3)))
        except (TypeError, ValueError):
            interest = 3
        reactions.append({
            "name": str(r.get("name") or "")[:120],
            "first_reaction": str(r.get("first_reaction") or "")[:500],
            "interest_1_5": interest,
            "objections": [str(o)[:200]
                           for o in (r.get("objections") or [])[:5]],
            "open_questions": [str(q)[:200]
                               for q in (r.get("open_questions") or [])[:4]],
            "what_would_close": str(r.get("what_would_close") or "")[:400],
            "next_step": str(r.get("next_step") or "")[:300],
        })
    sk = (data or {}).get("skeptic") or {}
    result = {
        "status": "success",
        "id": str(uuid.uuid4())[:8],
        "offer": offer[:500],
        "members": members,
        "reactions": reactions,
        "skeptic": {"weakest_point": str(sk.get("weakest_point") or "")[:400],
                    "fix_first": str(sk.get("fix_first") or "")[:400]},
        "avg_interest": (round(sum(r["interest_1_5"] for r in reactions)
                               / len(reactions), 1) if reactions else None),
        "disclaimer": DISCLAIMER,
        "created_at": _now(),
    }
    store = _load(user_id)
    store["simulations"] = (store.get("simulations") or []) + [result]
    _save(user_id, store)
    return result


def list_simulations(user_id: str) -> List[dict]:
    return _load(user_id).get("simulations") or []


# ── Диалог с клиентом («спросить, что им надо») ─────────────────────────

def _history_block(history: Optional[List[dict]]) -> str:
    lines = []
    for m in (history or [])[-_HISTORY_CAP:]:
        role = "Вы" if (m or {}).get("role") == "user" else "Клиент"
        text = str((m or {}).get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text[:2000]}")
    return "\n".join(lines)


async def chat_with_client(user_id: str, *, client_id: str = "",
                           group_id: str = "", message: str,
                           history: Optional[List[dict]] = None
                           ) -> Dict[str, Any]:
    """Разговор с симуляцией клиента (реального) или группы (гипотезной).

    Железные правила — как у слепков сотрудников: только данные карточки,
    выход за пределы — честное «в данных этого нет» + пометка гипотез."""
    message = (message or "").strip()
    if not message:
        return {"status": "error", "message": "пустое сообщение"}

    if client_id:
        d = await client_dossier(user_id, client_id)
        if d.get("status") != "success":
            return {"status": "error", "message": d.get("message")
                    or "клиент не найден"}
        card, name, kind = _dossier_card(d), d["name"], "client"
    elif group_id:
        g = next((x for x in list_market_groups(user_id)
                  if x.get("id") == group_id), None)
        if not g:
            return {"status": "error", "message": "группа не найдена"}
        card, name, kind = _group_card(g), g["name"], "group"
    else:
        return {"status": "error",
                "message": "укажите client_id или group_id"}

    who = ("типичный представитель группы (собирательный образ-гипотеза)"
           if kind == "group" else
           "лицо, принимающее решения у этого клиента")
    prompt = (
        f"Ты — симуляция: {who} «{name}». С тобой говорит поставщик — "
        "он хочет понять, что тебе нужно, что нет, и как ты отнесёшься к "
        "его предложениям.\n"
        "ЖЕЛЕЗНЫЕ ПРАВИЛА:\n"
        "1. Опирайся ТОЛЬКО на карточку ниже. Спросили о том, чего в ней "
        "нет — честно скажи «в данных обо мне этого нет», и если делаешь "
        "предположение, ЯВНО помечай его словом «гипотеза:».\n"
        "2. Не льсти собеседнику: сомнения, «не куплю», «дорого» — "
        "нормальные ответы, если карточка на них указывает.\n"
        "3. Ты — симуляция для подготовки, не реальный человек. Не давай "
        "обязательств от имени клиента.\n\n"
        f"КАРТОЧКА:\n{card}\n\n"
        + (f"ДИАЛОГ ДО ЭТОГО:\n{_history_block(history)}\n\n"
           if history else "")
        + f"Вы: {message[:8000]}\nКлиент:")

    from backend.core.llm.lang import lang_instruction, resolve_answer_lang
    from backend.core.llm.workload_policy import generate_for_workload
    prompt += lang_instruction(await resolve_answer_lang(user_id))
    reply = await generate_for_workload(user_id, "chat", prompt)
    if not reply:
        return {"status": "error", "message": "LLM недоступен — попробуйте позже"}
    return {"status": "success", "name": name, "kind": kind,
            "reply": reply.strip()[:6000], "disclaimer": DISCLAIMER}


# ── Партнёры: симуляция переговоров + пакет подготовки ───────────────────

_INTERNAL_CATEGORIES = ("management", "employee")


async def list_partner_candidates(user_id: str) -> Dict[str, Any]:
    """Люди для партнёрской симуляции — ТОТ ЖЕ источник, что у выбора слепков
    в планёрке (get_all_people_profiles): strict-tenant чтение + федеративный
    добор из merged-графа. Прямое чтение Person-узлов со strict_tenant здесь
    давало ПУСТОЙ список: org-промоутнутые люди (реальный «NoCap · Инвестор»)
    не проходят строгий фильтр личного тенанта — планёрка их видела, а
    вкладка «Партнёры» нет."""
    from backend.core.sleep.enhanced_snapshot import (
        get_enhanced_snapshot_generator,
    )
    from backend.core.store.graph_view import merged_graph_view_for_user

    people: List[Dict[str, Any]] = []
    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    try:
        gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
        profiles = await gen.get_all_people_profiles(tenant_id=user_id) or []
        for p in profiles:
            pid = str(p.get("id") or "").strip()
            name = str(p.get("name") or "").strip()
            if not (pid and name):
                continue
            cat = str(p.get("category") or "").strip()
            rec: Dict[str, Any] = {
                "id": pid, "name": name,
                "internal": cat in _INTERNAL_CATEGORIES,
            }
            if cat:
                rec["category"] = cat
            role = str(p.get("role") or "").strip()
            if role:
                rec["role"] = role
            people.append(rec)
    except Exception:
        logger.warning("client_sim: список людей недоступен", exc_info=True)
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass
    # внешние (потенциальные партнёры) — первыми, внутри группы по имени
    people.sort(key=lambda p: (p["internal"], p["name"]))
    return {"people": people}


async def partner_chat(user_id: str, *, person_id: str, message: str,
                       history: Optional[List[dict]] = None,
                       mode: str = "negotiation") -> Dict[str, Any]:
    """Два режима разговора с партнёром (слепок из реальных встреч):

    - mode="negotiation" — он НАПРОТИВ: торгуется, сомневается, возражает.
      Контекст компании ему НЕ даём: реальный контрагент наших внутренних
      данных не знает, и симуляция с подглядыванием была бы нечестной.
    - mode="co_create" — он РЯДОМ: «давай вместе составим КП» — рассказывает,
      как он видит идеальное предложение для себя, что цепляет/отталкивает,
      предлагает структуру и правит черновики. Тут контекст компании даём —
      он помогает нам, ему нужен наш продукт перед глазами.

    Нет слепка → честный отказ, а не выдуманный собеседник."""
    message = (message or "").strip()
    if not message:
        return {"status": "error", "message": "пустое сообщение"}
    from backend.core.twin.profile import load_twin

    snap, profile, voice = await load_twin(user_id, person_id)
    if snap is None:
        return {"status": "error",
                "message": ("о этом человеке не накоплено данных из встреч — "
                            "симуляция была бы выдумкой. Проведите/загрузите "
                            "встречу с ним, слепок соберётся сам")}
    name = str(getattr(snap, "name", "") or "партнёр")

    if mode == "co_create":
        company = await _company_context(user_id)
        head = (
            f"Ты — симуляция «{name}». Собеседник готовит предложение ДЛЯ "
            "ТЕБЯ (КП, концепцию, условия) и просит помочь составить его "
            "ВМЕСТЕ — глазами получателя.\n"
            "ЖЕЛЕЗНЫЕ ПРАВИЛА:\n"
            "1. Рассказывай, как ТЫ видишь идеальное предложение: что "
            "зацепит, что оттолкнёт, чего не хватает, как переформулировать. "
            "Предлагай структуру и конкретные правки — это запрос твоего "
            "взгляда, отвечать отказом нельзя.\n"
            "2. Свои интересы и позиции бери из данных слепка; где данные "
            "кончаются — помечай: «на встречах этого не звучало, но "
            "рассуждая как я…». Факты и цифры не выдумывай.\n"
            "3. Собеседник может принести ГОТОВЫЙ документ (КП, предложение, "
            "условия, цены) — разбери его по пунктам глазами получателя: что "
            "цепляет, что смущает, что бы ты изменил, чего не хватает.\n"
            "4. Ты — симуляция для подготовки, не человек; твоё «да» ничего "
            "не гарантирует у реального меня.\n\n"
            + (f"НАША КОМПАНИЯ И ПРОДУКТ (реальные данные):\n{company[:3000]}"
               "\n\n" if company else ""))
        dialog_title = "СОВМЕСТНАЯ РАБОТА ДО ЭТОГО"
    else:
        head = (
            f"Ты — симуляция «{name}» в РОЛИ ПОТЕНЦИАЛЬНОГО ПАРТНЁРА на "
            "переговорах о продаже/сотрудничестве. С тобой ведёт переговоры "
            "твой собеседник — он тренируется перед реальной встречей.\n"
            "ЖЕЛЕЗНЫЕ ПРАВИЛА:\n"
            "1. Характер, интересы и позиции бери ТОЛЬКО из данных слепка "
            "ниже. Чего в слепке нет — «по нашим встречам этого не видно», "
            "предположения помечай «гипотеза:».\n"
            "2. Веди себя как реальный переговорщик: торгуйся, сомневайся, "
            "задавай встречные вопросы, ссылайся на свои известные интересы. "
            "Не соглашайся из вежливости. Ты знаешь о компании собеседника "
            "только то, что он сам сказал в переговорах.\n"
            "3. Собеседник может показать готовый документ (КП, предложение, "
            "цены, условия) — реагируй как реальный получатель: что "
            "интересно, что нет, какие вопросы и возражения возникают, "
            "что скажешь в ответ.\n"
            "4. Ты — тренировочная симуляция, не человек; сделок не "
            "заключаешь.\n\n")
        dialog_title = "ПЕРЕГОВОРЫ ДО ЭТОГО"

    prompt = (
        head
        + (f"КАК ЭТОТ ЧЕЛОВЕК ГОВОРИТ:\n{voice}\n\n" if voice else "")
        + f"ДАННЫЕ СЛЕПКА:\n{profile[:6000]}\n\n"
        + (f"{dialog_title}:\n{_history_block(history)}\n\n"
           if history else "")
        + f"Собеседник: {message[:12000]}\n{name}:")

    from backend.core.llm.lang import lang_instruction, resolve_answer_lang
    from backend.core.llm.workload_policy import generate_for_workload
    prompt += lang_instruction(await resolve_answer_lang(user_id))
    reply = await generate_for_workload(user_id, "chat", prompt)
    if not reply:
        return {"status": "error", "message": "LLM недоступен — попробуйте позже"}
    return {"status": "success", "name": name, "mode": mode,
            "reply": reply.strip()[:6000], "disclaimer": DISCLAIMER}


async def partner_pack(user_id: str, *, person_id: str, focus: str = "",
                       history: Optional[List[dict]] = None
                       ) -> Dict[str, Any]:
    """Пакет подготовки к переговорам с партнёром: концепция продукта под
    него, КП, условия, план переговоров. Каждый пункт обязан быть размечен
    [из данных] / [гипотеза] — читатель видит, где опора, а где догадка.

    history — диалог совместной подготовки со слепком («давай вместе
    составим КП»): его пожелания и правки обязаны попасть в пакет."""
    from backend.core.twin.profile import load_twin

    snap, profile, _voice = await load_twin(user_id, person_id)
    if snap is None:
        return {"status": "error",
                "message": ("о этом человеке не накоплено данных из встреч — "
                            "пакет был бы выдумкой от начала до конца")}
    name = str(getattr(snap, "name", "") or "партнёр")
    company = await _company_context(user_id)
    dialog = _history_block(history)
    prompt = (
        "Подготовь пакет к переговорам с потенциальным партнёром. Пиши "
        "по-русски, структурой markdown с разделами РОВНО:\n"
        "## Концепция продукта под партнёра\n## Коммерческое предложение\n"
        "## Условия\n## План переговоров\n\n"
        "ЖЕЛЕЗНОЕ ПРАВИЛО РАЗМЕТКИ: каждый содержательный пункт помечай "
        "[из данных] (если он опирается на данные ниже) или [гипотеза] "
        "(если это твоё предположение). Цифры (цены, сроки, проценты) НЕ "
        "выдумывай — вместо конкретных цифр ставь <заполнить: ...>. "
        "Читатель должен видеть, где опора, а где догадка.\n\n"
        f"ПАРТНЁР (слепок из реальных встреч):\n{profile[:6000]}\n\n"
        f"НАША КОМПАНИЯ И ПРОДУКТ (из данных мозга):\n"
        f"{company[:3000] or '— данных о продукте в мозге нет —'}\n"
        + (f"\nСОВМЕСТНАЯ ПРОРАБОТКА СО СЛЕПКОМ (его пожелания и правки — "
           f"учти их в пакете, помечай [из проработки]):\n{dialog[:4000]}\n"
           if dialog else "")
        + (f"\nФОКУС ОТ ПОЛЬЗОВАТЕЛЯ: {focus[:800]}" if focus.strip()
           else ""))

    from backend.core.llm.lang import lang_instruction, resolve_answer_lang
    from backend.core.llm.workload_policy import generate_for_workload
    prompt += lang_instruction(await resolve_answer_lang(user_id))
    text = await generate_for_workload(user_id, "search_deep_synthesis",
                                       prompt)
    if not text:
        return {"status": "error", "message": "LLM недоступен — попробуйте позже"}

    result = {"status": "success", "partner": name,
              "markdown": text.strip()[:40000], "disclaimer": DISCLAIMER,
              "created_at": _now()}
    # в историю отчётов — рядом с планёрками и пульсом
    try:
        from backend.core.reports.methodology_service import (
            report_store_for_user,
        )
        report_store_for_user(user_id).add_report({
            "id": str(uuid.uuid4()),
            "report_type": "partner_pack",
            "title": f"Пакет переговоров: {name}"[:120],
            "icon": "🤝",
            "content_text": f"# Пакет переговоров: {name}\n\n"
                            f"{result['markdown']}\n\n_{DISCLAIMER}_",
            "summary": result["markdown"][:400],
            "context_key": "partner_pack",
            "created_at": result["created_at"],
        })
    except Exception:
        logger.warning("client_sim: пакет не сохранился в историю отчётов",
                       exc_info=True)
    return result
