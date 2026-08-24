# -*- coding: utf-8 -*-
"""Виртуальная планёрка директоров — обсуждение вопроса ролями и решение.

Что это: пользователь задаёт вопрос → несколько «директоров» (CEO, маркетинг,
продажи, CTO, финансы) высказываются по данным компании → раунд(ы) взаимной
реакции → председатель сводит решение с протоколом разногласий → решение
СВЕРЯЕТСЯ с данными компании отдельным проходом («что противоречит, чего не
хватает»).

Честность превыше эффектности:
- каждый директор получает РЕАЛЬНЫЙ контекст (снапшот компании + поиск по
  мозгу + доменный снапшот своей функции) и инструкцию «чего нет в данных —
  так и скажи»;
- это СИМУЛЯЦИЯ обсуждения, а не мнение людей — дисклеймер зашит в результат;
- разногласия не сглаживаются: председатель обязан зафиксировать, кто против
  и почему.

Голоса ролей — промпты-как-данные в boardroom_prompts/*.md (добавить
директора = добавить .md + строку в DIRECTORS). Инструкции адаптируются под
стадию компании из снапшота (CEO стартапа ≠ CEO корпорации).

Стоимость: реплики директоров идут уровнем «chat» (быстрая модель), синтез и
сверка — «search_deep_synthesis» (сильная). Планёрка из 4 ролей × 2 раунда =
~10 вызовов; запускается только явным действием пользователя.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "boardroom_prompts")

# Каталог ролей. domain — фокус из реестра доменных снапшотов
# (backend/core/sleep/domain_snapshots.py); None = видит всё.
DIRECTORS: Dict[str, Dict[str, Any]] = {
    "ceo": {"name": "Генеральный директор", "icon": "🧭", "domain": None},
    "cmo": {"name": "Директор по маркетингу", "icon": "📣", "domain": "marketing"},
    "sales": {"name": "Директор по продажам", "icon": "💼", "domain": "sales"},
    "cto": {"name": "Технический директор", "icon": "⚙️", "domain": "tech"},
    "cfo": {"name": "Финансовый директор", "icon": "💰", "domain": "finance"},
}

_DEFAULT_CAST = ["ceo", "cmo", "sales", "cto"]
_MAX_ROUNDS = 3


def _load_prompt(name: str) -> str:
    try:
        with open(os.path.join(_PROMPTS_DIR, f"{name}.md"), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.error("boardroom prompt not found: %s", name)
        return ""


async def _company_header(user_id: str) -> str:
    """Короткая шапка «что за компания» — для адаптации роли под стадию.

    CEO стартапа и CEO корпорации принимают решения по-разному; стадию берём
    из снапшота компании, а не спрашиваем пользователя."""
    try:
        from backend.core.sleep.enhanced_snapshot import (
            get_enhanced_snapshot_generator,
        )
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
            snap = await gen.get_company_snapshot(force_regenerate=False)
            d = snap.to_dict() if hasattr(snap, "to_dict") else {}
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
        bits = []
        for label, key in [("Компания", "name"), ("Отрасль", "industry"),
                           ("Стадия", "stage"), ("Размер", "size")]:
            v = str(d.get(key) or "").strip()
            if v:
                bits.append(f"{label}: {v}")
        return "; ".join(bits)
    except Exception as e:
        logger.warning("boardroom: шапка компании недоступна: %s", e)
        return ""


async def _brain_context(user_id: str, question: str, *,
                         use_brain: bool, days_back: int) -> str:
    """Общий контекст планёрки из знаний компании.

    use_brain=True — паттерн узла ask_brain: снапшот + поиск по мозгу под
    вопрос + блок чисел. use_brain=False — только снапшот (директора «по
    памяти», без исследования)."""
    parts: List[str] = []
    try:
        from backend.core.reports.report_context import _company_snapshot_text
        snap = await _company_snapshot_text(user_id)
        if snap:
            parts.append(snap)
    except Exception:
        logger.warning("boardroom: снапшот недоступен", exc_info=True)

    if use_brain:
        try:
            from backend.core.search.bm25_searcher import get_bm25_searcher
            hits = get_bm25_searcher(user_id=user_id).search(question, top_k=8)
            frags = []
            for h in hits or []:
                t = str((h.get("text") if isinstance(h, dict) else getattr(h, "text", "")) or "").strip()
                if t:
                    frags.append(f"  • {t[:600]}")
            if frags:
                parts.append("## НАЙДЕНО В ПАМЯТИ КОМПАНИИ ПО ВОПРОСУ\n"
                             + "\n".join(frags))
        except Exception:
            logger.warning("boardroom: поиск по мозгу пропущен", exc_info=True)
        try:
            from backend.core.ontology.numbers_context import numbers_block
            nb = numbers_block(user_id, question)
            nb_text = (nb or {}).get("text") if isinstance(nb, dict) else str(nb or "")
            if nb_text:
                parts.append("## ЦИФРЫ КОМПАНИИ\n" + str(nb_text))
        except Exception:
            logger.debug("boardroom: numbers block пропущен", exc_info=True)
        try:
            from backend.core.reports.report_context import _graph_facts_text
            facts, _ = await _graph_facts_text(user_id, days_back)
            if facts:
                parts.append(facts)
        except Exception:
            logger.warning("boardroom: факты графа пропущены", exc_info=True)
    return "\n\n".join(parts)


def _domain_context(user_id: str, domain: Optional[str]) -> str:
    if not domain:
        return ""
    try:
        from backend.core.sleep.domain_snapshots import read_domain_snapshot
        body = read_domain_snapshot(user_id, domain, max_chars=6000)
        return f"## НАКОПЛЕННАЯ КАРТИНА ТВОЕЙ ФУНКЦИИ\n{body}" if body.strip() else ""
    except Exception:
        return ""


async def _speak(user_id: str, system_prompt: str, user_prompt: str,
                 *, heavy: bool = False) -> str:
    from backend.core.llm.workload_policy import generate_for_workload
    workload = "search_deep_synthesis" if heavy else "chat"
    got = await generate_for_workload(
        user_id, workload, f"{system_prompt}\n\n---\n\n{user_prompt}")
    return (got or "").strip()


def _lang_tail(lang: str) -> str:
    """Хвост «отвечай на языке интерфейса» к системному промпту.

    Промпты планёрки написаны по-русски, и без этой строки англоязычный
    пользователь видел переведённый интерфейс с русскими репликами
    директоров. Пусто/ru → пустая строка, промпт байт-в-байт прежний."""
    from backend.core.llm.lang import lang_instruction
    return lang_instruction(lang)


def _transcript_block(replies: Dict[str, str],
                      participants: Optional[Dict[str, Dict[str, Any]]] = None
                      ) -> str:
    parts = []
    for pid, text in replies.items():
        meta = (participants or {}).get(pid) or DIRECTORS.get(pid) or {}
        parts.append(f"=== {meta.get('icon', '')} {meta.get('name', pid)} ===\n{text}")
    return "\n\n".join(parts)


async def _role_calibration(user_id: str, did: str) -> str:
    """Глубокая калибровка роли под компанию (см. role_calibration.py).

    Отдельная обёртка, чтобы тесты могли подменить её, не трогая модуль."""
    try:
        from backend.core.boardroom.role_calibration import get_role_calibration
        return await get_role_calibration(
            user_id, did, DIRECTORS[did]["name"],
            domain=DIRECTORS[did].get("domain"))
    except Exception:
        logger.warning("калибровка роли %s пропущена", did, exc_info=True)
        return ""


async def _build_participants(user_id: str, cast: List[str],
                              person_ids: List[str], header: str,
                              lang: str = ""
                              ) -> Dict[str, Dict[str, Any]]:
    """Участники планёрки: абстрактные роли и/или слепки живых людей.

    Роль — это НЕ «шаблон + строка про отрасль»: к базовому голосу
    добавляется КАЛИБРОВКА под конкретную компанию (о чём эта роль реально
    беспокоится здесь, что применимо, что вредно) — её строит и кэширует
    role_calibration.py из профиля компании.

    Слепок — полноценный участник: его системный промпт строится из
    PersonSnapshot (решения, мнения, манера речи), а не из ролевого шаблона.
    Это сценарий «CEO собирает комнату из слепков СВОИХ директоров»."""
    base_rules = _load_prompt("common_rules") + _lang_tail(lang)
    out: Dict[str, Dict[str, Any]] = {}
    for did in cast:
        role_p = _load_prompt(did) or f"Ты — {DIRECTORS[did]['name']} компании."
        calib = await _role_calibration(user_id, did)
        out[did] = {
            "kind": "role",
            "name": DIRECTORS[did]["name"],
            "icon": DIRECTORS[did]["icon"],
            "domain": DIRECTORS[did].get("domain"),
            "system": "\n\n".join(p for p in (
                role_p,
                f"О компании: {header}" if header else "",
                f"КАЛИБРОВКА ТВОЕЙ РОЛИ ПОД ЭТУ КОМПАНИЮ:\n{calib}" if calib else "",
                base_rules) if p),
        }
    for pid in person_ids:
        try:
            from backend.core.twin.profile import load_twin, twin_system_prompt
            snap, profile, voice = await load_twin(user_id, pid)
        except Exception as e:
            logger.warning("boardroom: слепок %s не загрузился: %s", pid, e)
            continue
        if snap is None:
            logger.info("boardroom: слепка «%s» нет — пропускаем", pid)
            continue
        # слепку, как и ролям, нужен контекст компании — иначе ему не из
        # чего рассуждать о вопросах шире собственных встреч. Категория
        # решает кадрирование: внешний человек — приглашённый участник,
        # а не «коллега по планёрке»
        try:
            from backend.core.twin.profile import person_category
            _cat = await person_category(user_id, pid,
                                         getattr(snap, "name", ""))
        except Exception:
            _cat = ""
        sysp = twin_system_prompt(snap.name, profile, voice,
                                  setting="boardroom",
                                  company_context=header or "",
                                  category=_cat)
        out[f"twin:{pid}"] = {
            "kind": "twin",
            "name": f"{snap.name} (слепок)",
            "icon": "🧬",
            "domain": None,
            "system": "\n\n".join(p for p in (sysp, base_rules) if p),
        }
    return out


async def run_boardroom(user_id: str, question: str, *,
                        director_ids: Optional[List[str]] = None,
                        person_ids: Optional[List[str]] = None,
                        rounds: int = 2,
                        use_brain: bool = True,
                        days_back: int = 30,
                        lang: str = "",
                        save: bool = True) -> Dict[str, Any]:
    """Провести планёрку. Возврат: решение + протокол + сверка + статистика.

    director_ids — абстрактные роли из каталога; person_ids — слепки живых
    сотрудников (комната из слепков СВОИХ директоров). Можно смешивать.
    lang — язык интерфейса: на нём участники и отвечают."""
    question = (question or "").strip()
    if not question:
        return {"status": "error", "message": "Пустой вопрос."}

    person_ids = [p for p in (person_ids or []) if str(p or "").strip()]
    cast = [d for d in (director_ids if director_ids is not None else
                        ([] if person_ids else _DEFAULT_CAST))
            if d in DIRECTORS]
    rounds = max(1, min(int(rounds or 1), _MAX_ROUNDS))

    header = await _company_header(user_id)
    participants = await _build_participants(user_id, cast, person_ids,
                                            header, lang)
    if len(participants) < 2:
        return {"status": "error",
                "message": ("Нужно минимум два участника: роли из каталога "
                            "и/или слепки сотрудников, по которым накоплены "
                            "данные.")}

    shared_ctx = await _brain_context(user_id, question,
                                      use_brain=use_brain, days_back=days_back)
    if not shared_ctx.strip():
        return {"status": "no_data",
                "message": "В мозге компании нет данных для обсуждения — "
                           "планёрка на пустом контексте была бы выдумкой."}

    # Переговоры, а не параллельный залп: реплики идут ПО ОЧЕРЕДИ, каждый
    # следующий видит всё, что уже прозвучало (и в прошлых раундах, и в
    # текущем), и ОБЯЗАН отреагировать — согласие без аргументов запрещено.
    # Первый спикер ротируется по раундам, чтобы один голос не якорил всех.
    transcript: List[Dict[str, Any]] = []
    order = list(participants.keys())

    async def _run_round(r: int, instruction: str) -> Dict[str, str]:
        start = (r - 1) % len(order)
        speak_order = order[start:] + order[:start]
        round_replies: Dict[str, str] = {}
        for pid in speak_order:
            prev_proto = "\n\n".join(
                f"--- РАУНД {t['round']} ---\n"
                f"{_transcript_block(t['replies'], participants)}"
                for t in transcript)
            parts = [f"ВОПРОС ПЛАНЁРКИ: {question}",
                     f"ДАННЫЕ КОМПАНИИ:\n{shared_ctx}"]
            if prev_proto:
                parts.append(f"ПРОТОКОЛ ПРЕДЫДУЩЕГО РАУНДА:\n{prev_proto}")
            if round_replies:
                parts.append("УЖЕ ПРОЗВУЧАЛО В ЭТОМ РАУНДЕ:\n"
                             + _transcript_block(round_replies, participants))
            extra = _domain_context(user_id, participants[pid].get("domain"))
            if extra:
                parts.append(extra)
            if prev_proto or round_replies:
                parts.append(
                    "ТЫ ГОВОРИШЬ ПОСЛЕ КОЛЛЕГ. Говори только по существу "
                    "СВОЕЙ роли:\n"
                    "- не согласен — скажи прямо: «так делать нельзя, потому "
                    "что…», с опорой на данные;\n"
                    "- видишь у коллеги слабое место, важное для твоей зоны — "
                    "назови его;\n"
                    "- видишь третий вариант, лучше обоих — предложи;\n"
                    "- согласен и добавить нечего, или вопрос вне твоей зоны — "
                    "ответь ровно одной строкой: ВОЗДЕРЖИВАЮСЬ. Это нормальный "
                    "ответ; НЕ выдумывай возражений ради возражений и не "
                    "поддакивай ради вежливости.")
            parts.append(instruction)
            try:
                reply = await _speak(user_id, participants[pid]["system"],
                                     "\n\n".join(parts))
            except Exception as e:
                logger.warning("boardroom: реплика %s не удалась: %s", pid, e)
                reply = ""
            # Право промолчать: «воздерживаюсь» — нормальный ответ роли, чьей
            # зоны вопрос не касается. В протокол попадает одной строкой —
            # коллеги видят, что человек слышал и сознательно не возражает.
            low = reply.strip().lower()
            # роль вправе промолчать на любом языке ответа — распознаём и
            # русскую, и английскую форму (иначе при lang=en «I abstain»
            # уходило в протокол как обычная реплика)
            if (low.startswith("воздержива") or low.startswith("воздержусь")
                    or low.startswith("abstain") or low.startswith("i abstain")):
                reply = ("Воздерживаюсь: по этому вопросу мне добавить нечего."
                         if not lang or lang == "ru" else
                         "Abstaining: I have nothing to add on this question.")
            if reply:
                round_replies[pid] = reply
        return round_replies

    for r in range(1, rounds + 1):
        instruction = (
            "Выскажи позицию своей роли: 1) как ты видишь ситуацию по данным; "
            "2) что предлагаешь; 3) какие риски видишь; 4) что нужно "
            "проверить прежде чем решать. Кратко, по делу." if r == 1 else
            "Раунд сближения: что меняешь в своей позиции после услышанного, "
            "где готов уступить, где стоишь насмерть и почему.")
        round_replies = await _run_round(r, instruction)
        if not round_replies:
            return {"status": "error",
                    "message": "Ни один директор не смог ответить (сбой моделей)."}
        transcript.append({"round": r, "replies": round_replies})

    # Дожим разногласий: если участники НЕ сошлись — один дополнительный
    # раунд строго по спорным пунктам (консенсус или мотивированный отказ,
    # а не «замяли и забыли»).
    contested: List[str] = []
    try:
        proto_txt = "\n\n".join(
            f"--- РАУНД {t['round']} ---\n"
            f"{_transcript_block(t['replies'], participants)}"
            for t in transcript)
        raw = await _speak(
            user_id,
            "Ты — секретарь планёрки. Отвечай СТРОГО JSON, без пояснений.",
            ("Прочитай стенограмму и определи, остались ли НЕРАЗРЕШЁННЫЕ "
             "разногласия (кто-то возразил, и возражение не снято ни "
             "уступкой, ни контраргументом, ни третьим вариантом).\n"
             'Формат: {"resolved": true|false, "contested": ["пункт", ...]}\n\n'
             f"СТЕНОГРАММА:\n{proto_txt}"))
        import json as _json
        cleaned = raw.strip().strip("`").replace("json\n", "", 1)
        parsed = _json.loads(cleaned[cleaned.find("{"):cleaned.rfind("}") + 1])
        if parsed.get("resolved") is False:
            contested = [str(c) for c in (parsed.get("contested") or []) if str(c).strip()][:5]
    except Exception:
        logger.debug("boardroom: consensus check неубедителен — без доп. раунда",
                     exc_info=True)

    if contested:
        extra_replies = await _run_round(
            len(transcript) + 1,
            "РАУНД РАЗРЕШЕНИЯ РАЗНОГЛАСИЙ. Спорные пункты:\n"
            + "\n".join(f"  • {c}" for c in contested)
            + "\nПо КАЖДОМУ спорному пункту: уступаешь, стоишь на своём "
              "(почему), или предлагаешь третий вариант. Ничего не замалчивать.")
        if extra_replies:
            transcript.append({"round": len(transcript) + 1,
                               "replies": extra_replies,
                               "resolution_round": True})

    # Председатель: решение + протокол разногласий
    chair_sys = (_load_prompt("chairman") or
                 "Ты — председатель планёрки. Сведи обсуждение в решение."
                 ) + _lang_tail(lang)
    full_protocol = "\n\n".join(
        f"--- РАУНД {t['round']} ---\n{_transcript_block(t['replies'], participants)}"
        for t in transcript)
    contested_note = ("\n\nСПОРНЫЕ ПУНКТЫ, зафиксированные секретарём:\n"
                      + "\n".join(f"  • {c}" for c in contested)
                      if contested else "")
    decision = await _speak(
        user_id, chair_sys,
        f"ВОПРОС: {question}\n\nПОЛНЫЙ ПРОТОКОЛ ОБСУЖДЕНИЯ:\n{full_protocol}"
        f"{contested_note}",
        heavy=True)

    # Сверка решения с данными компании — то, ради чего планёрка не «сочиняет»:
    # отдельный проход ищет противоречия решения фактам и пробелы данных.
    verification = ""
    try:
        verification = await _speak(
            user_id,
            "Ты — независимый аудитор решений. Твоя задача — НЕ соглашаться, "
            "а проверять. Отвечай только по данным." + _lang_tail(lang),
            (f"РЕШЕНИЕ ПЛАНЁРКИ:\n{decision}\n\nДАННЫЕ КОМПАНИИ:\n{shared_ctx}\n\n"
             "Проверь: 1) что в решении ПРОТИВОРЕЧИТ данным компании (с "
             "цитатой факта); 2) какие важные факты решение игнорирует; "
             "3) каких данных не хватает, чтобы решение считать обоснованным. "
             "Если противоречий нет — так и скажи."),
            heavy=True)
    except Exception:
        logger.warning("boardroom: сверка решения не удалась", exc_info=True)

    result = {
        "status": "success",
        "id": str(uuid.uuid4()),
        "question": question,
        "cast": [{"id": pid, "name": meta["name"], "icon": meta["icon"],
                  "kind": meta["kind"]}
                 for pid, meta in participants.items()],
        "rounds": transcript,
        "contested_points": contested,
        "decision": decision,
        "verification": verification,
        "disclaimer": ("Это симуляция обсуждения на основе данных компании, "
                       "а не мнение реальных людей. Решения проверяйте с "
                       "командой."),
        "used_brain": use_brain,
        "context_chars": len(shared_ctx),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    _save_result = save
    if _save_result:
        try:
            from backend.core.reports.methodology_service import (
                report_store_for_user,
            )
            store = report_store_for_user(user_id)
            store.add_report({
                "id": result["id"],
                "report_type": "boardroom",
                "title": f"Планёрка: {question[:80]}",
                "icon": "🏛️",
                "content_text": (f"# Планёрка: {question}\n\n## Решение\n"
                                 f"{decision}\n\n## Сверка с данными\n"
                                 f"{verification}\n\n_{result['disclaimer']}_"),
                "summary": decision[:500],
                "sub_reports": {f"round_{t['round']}_{d}": txt
                                for t in transcript
                                for d, txt in t["replies"].items()},
                "context_key": "boardroom",
                "created_at": result["created_at"],
            })
        except Exception:
            logger.warning("boardroom: не сохранилось в историю отчётов",
                           exc_info=True)

    return result


async def ask_director(user_id: str, director_id: str, question: str, *,
                       history: Optional[List[Dict[str, str]]] = None,
                       use_brain: bool = True,
                       days_back: int = 30,
                       lang: str = "") -> Dict[str, Any]:
    """Диалог с ОДНИМ директором (не планёрка): вопрос → ответ роли по данным.

    history — прошлые реплики этого разговора [{"who": "you"|"director",
    "text": ...}], чтобы диалог был связным. Контекст компании собирается
    заново под каждый вопрос (use_brain=True — с поиском по мозгу)."""
    question = (question or "").strip()
    if not question:
        return {"status": "error", "message": "Пустой вопрос."}
    if director_id not in DIRECTORS:
        return {"status": "error",
                "message": f"Нет такого директора: {director_id}"}

    header = await _company_header(user_id)
    ctx = await _brain_context(user_id, question,
                               use_brain=use_brain, days_back=days_back)
    if not ctx.strip():
        return {"status": "no_data",
                "message": "В мозге компании нет данных — директору не на "
                           "что опереться, отвечать из головы он не будет."}

    role_p = _load_prompt(director_id) or f"Ты — {DIRECTORS[director_id]['name']}."
    calib = await _role_calibration(user_id, director_id)
    sysp = "\n\n".join(p for p in (
        role_p, f"О компании: {header}" if header else "",
        f"КАЛИБРОВКА ТВОЕЙ РОЛИ ПОД ЭТУ КОМПАНИЮ:\n{calib}" if calib else "",
        _load_prompt("common_rules")) if p) + _lang_tail(lang)
    extra = _domain_context(user_id, DIRECTORS[director_id].get("domain"))

    hist_txt = ""
    for h in (history or [])[-12:]:
        who = "ТЫ" if (h or {}).get("who") == "director" else "СОБЕСЕДНИК"
        t = str((h or {}).get("text") or "").strip()
        if t:
            hist_txt += f"{who}: {t}\n"

    user_p = (f"ДАННЫЕ КОМПАНИИ:\n{ctx}\n\n"
              + (f"{extra}\n\n" if extra else "")
              + (f"ПРЕДЫДУЩИЙ РАЗГОВОР:\n{hist_txt}\n" if hist_txt else "")
              + f"ВОПРОС К ТЕБЕ: {question}\n\n"
              "Ответь как твоя роль: по данным, кратко, честно про пробелы.")

    answer = await _speak(user_id, sysp, user_p)
    if not answer:
        return {"status": "error", "message": "Модель недоступна."}
    return {
        "status": "success",
        "director": {"id": director_id,
                     "name": DIRECTORS[director_id]["name"],
                     "icon": DIRECTORS[director_id]["icon"]},
        "answer": answer,
        "used_brain": use_brain,
        "disclaimer": ("Это симуляция роли на данных компании, а не человек."),
    }


async def ask_directors(user_id: str, director_ids: List[str], question: str, *,
                        history: Optional[List[Dict[str, str]]] = None,
                        use_brain: bool = True,
                        days_back: int = 30,
                        lang: str = "") -> Dict[str, Any]:
    """Спросить НЕСКОЛЬКИХ директоров сразу — мини-обсуждение в чате.

    Не параллельный залп: реплики по очереди, каждый следующий видит уже
    сказанное и обязан отреагировать (несогласие — прямо и с причиной).
    В конце короткий итог: к чему пришли, кто остался против. Один директор →
    обычный ask_director."""
    ids = [d for d in (director_ids or []) if d in DIRECTORS]
    if len(ids) == 1:
        return await ask_director(user_id, ids[0], question,
                                  history=history, use_brain=use_brain,
                                  days_back=days_back, lang=lang)
    if len(ids) < 2:
        return {"status": "error",
                "message": "Выберите директоров из каталога."}

    question = (question or "").strip()
    if not question:
        return {"status": "error", "message": "Пустой вопрос."}

    header = await _company_header(user_id)
    ctx = await _brain_context(user_id, question,
                               use_brain=use_brain, days_back=days_back)
    if not ctx.strip():
        return {"status": "no_data",
                "message": "В мозге компании нет данных — обсуждение на "
                           "пустом контексте было бы выдумкой."}

    participants = await _build_participants(user_id, ids, [], header, lang)

    hist_txt = ""
    for h in (history or [])[-12:]:
        who = str((h or {}).get("who") or "you")
        t = str((h or {}).get("text") or "").strip()
        if t:
            hist_txt += f"{'ВЫ' if who == 'you' else who.upper()}: {t}\n"

    voices: Dict[str, str] = {}
    for pid in ids:
        parts = [f"ВОПРОС: {question}", f"ДАННЫЕ КОМПАНИИ:\n{ctx}"]
        if hist_txt:
            parts.append(f"ПРЕДЫДУЩИЙ РАЗГОВОР:\n{hist_txt}")
        extra = _domain_context(user_id, participants[pid].get("domain"))
        if extra:
            parts.append(extra)
        if voices:
            parts.append("КОЛЛЕГИ УЖЕ ВЫСКАЗАЛИСЬ:\n"
                         + _transcript_block(voices, participants))
            parts.append(
                "Говори только по существу своей роли: не согласен — скажи "
                "прямо и почему («так делать нельзя, потому что…»); видишь "
                "третий вариант — предложи. Добавить нечего или вопрос вне "
                "твоей зоны — ответь одной строкой: ВОЗДЕРЖИВАЮСЬ. Не "
                "выдумывай возражений ради возражений.")
        parts.append("Ответь как твоя роль: по данным, кратко, честно про "
                     "пробелы.")
        try:
            reply = await _speak(user_id, participants[pid]["system"],
                                 "\n\n".join(parts))
        except Exception as e:
            logger.warning("ask_directors: %s не ответил: %s", pid, e)
            reply = ""
        low = (reply or "").strip().lower()
        if (low.startswith("воздержива") or low.startswith("воздержусь")
                or low.startswith("abstain") or low.startswith("i abstain")):
            reply = ("Воздерживаюсь: по этому вопросу мне добавить нечего."
                     if not lang or lang == "ru" else
                     "Abstaining: I have nothing to add on this question.")
        if reply:
            voices[pid] = reply
    if not voices:
        return {"status": "error", "message": "Модели недоступны."}

    summary = ""
    try:
        summary = await _speak(
            user_id,
            "Ты — секретарь обсуждения. Пиши кратко и честно."
            + _lang_tail(lang),
            (f"ВОПРОС: {question}\n\nОБСУЖДЕНИЕ:\n"
             f"{_transcript_block(voices, participants)}\n\n"
             "Итог в 3-6 предложениях: к чему пришли, кто остался против и "
             "почему, что осталось непроверенным. Разногласия не сглаживай."
             + _lang_tail(lang)))
    except Exception:
        logger.debug("ask_directors: итог пропущен", exc_info=True)

    return {
        "status": "success",
        "mode": "panel",
        "voices": [{"id": pid,
                    "name": participants[pid]["name"],
                    "icon": participants[pid]["icon"],
                    "answer": txt}
                   for pid, txt in voices.items()],
        "summary": summary,
        "used_brain": use_brain,
        "disclaimer": "Это симуляция ролей на данных компании, а не люди.",
    }
