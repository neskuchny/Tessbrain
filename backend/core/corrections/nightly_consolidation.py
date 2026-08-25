# -*- coding: utf-8 -*-
"""
Nightly Consolidation Service - Ночная консолидация данных.

Выполняет:
1. Применение pending исправлений (Two-Phase Corrections)
2. Temporal decay для связей графа (Hebbian Learning)
3. Консолидация памяти агентов (episodic → semantic)
4. Обновление снапшотов
5. Очистка устаревших данных

Запуск:
    python -m backend.core.corrections.nightly_consolidation

Или через cron:
    0 3 * * * cd /path/to/tessent_brain && python -m backend.core.corrections.nightly_consolidation
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.store.tenant_paths import (
    graph_path_for_user,
    graphs_dir,
)

logger = logging.getLogger(__name__)


def _persist_insights_to_store(user_id: str, insights: list, *, source: str) -> None:
    """NightInsight'ы → персистентная лента страницы инсайтов (best-effort).

    Аудит UI: раньше ночные находки уходили только в Telegram/очередь
    reactive_signals — страница инсайтов их не видела."""
    try:
        from backend.core.sleep.insight_store import InsightStore, stable_insight_id
        from backend.core.store.tenant_paths import insights_path_for_user
        rows = []
        for i in insights or []:
            try:
                d = {
                    "insight_type": i.insight_type.value,
                    "title": i.title,
                    "description": i.description,
                    "priority": i.priority.value,
                    "confidence": i.confidence,
                    "entities": getattr(i, "entities", []) or [],
                    "actions": getattr(i, "actions", []) or [],
                    "source": source,
                }
                # стабильный id по содержимому: proactive-скан выдаёт uuid4 на
                # каждый прогон — без этого одна и та же находка задваивалась бы
                d["insight_id"] = stable_insight_id(d)
                rows.append(d)
            except Exception:
                continue
        if rows:
            InsightStore(persist_path=insights_path_for_user(user_id)).add(rows)
    except Exception as exc:
        logger.warning("night insights: store persist failed for %s: %s",
                       user_id, exc)


async def _nightly_generate(user_id: str, llm, prompt: str, *,
                            temperature: float = 0.0, max_tokens: int = 0) -> str:
    """Ночной LLM-вызов с распределением по нагрузке.

    ЗА флагом enable_tiered_extraction → крупная модель тенанта (workload_policy
    'nightly' = premium) с 3-слойной Google-страховкой. По умолчанию/при сбое —
    прежний llm.generate (поведение 1:1). Всегда возвращает text (never-raise)."""
    async def _legacy(p: str) -> str:
        kw = {"prompt": p, "temperature": temperature}
        if max_tokens:
            kw["max_tokens"] = max_tokens
        r = await llm.generate(**kw)
        return r.get("text", "") if isinstance(r, dict) else str(r)

    try:
        from backend.core.config import flags as _flags
        if user_id and getattr(_flags, "enable_tiered_extraction", False):
            from backend.core.llm.workload_policy import generate_for_workload
            t = await generate_for_workload(user_id, "nightly", prompt,
                                            fallback_generate=_legacy)
            if t:
                return t
    except Exception as _e:  # noqa: BLE001
        logger.debug("nightly workload route fallback: %s", _e)
    return await _legacy(prompt)


class NightlyConsolidationService:
    """
    Сервис ночной консолидации.

    Запускается по расписанию (обычно в 3:00) и выполняет
    все фоновые задачи по обслуживанию системы.
    """

    def __init__(
        self,
        users_data_path: str = "data/users",
        log_path: str = "data/logs/nightly"
    ):
        """
        Args:
            users_data_path: Путь к данным пользователей
            log_path: Путь для логов консолидации
        """
        self.users_path = Path(users_data_path)
        self.log_path = Path(log_path)
        self.log_path.mkdir(parents=True, exist_ok=True)
        # Активен ли Neo4j-бэкенд (выставляется в _get_users). На Neo4j граф
        # лежит не в json-файлах, поэтому гейты graph_path.exists() ослабляются.
        self._neo4j_active = False

        self._stats = {
            "started_at": None,
            "completed_at": None,
            "users_processed": 0,
            "corrections_applied": 0,
            "edges_decayed": 0,
            "memories_consolidated": 0,
            "memories_compressed": 0,
            "memories_clustered": 0,
            "memories_archived": 0,
            "snapshots_updated": 0,
            "errors": [],
        }

    async def run(self, force: bool = False, only_user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Запустить ночную консолидацию.

        Идемпотентно: распределённый лок (Redis) гарантирует, что в нескольких
        репликах APScheduler не запустят джобу параллельно. Если лок уже взят —
        возвращаем `{skipped: True}`.

        Returns:
            Статистика выполнения
        """
        # Ручной запуск (кнопка) идёт МИМО распределённого лока: на одиночной
        # машине реплик нет, а упавший прежде прогон мог оставить лок висеть
        # (раньше TTL=4ч → кнопка молча «skipped: lock_held»). Пересечение с
        # плановым прогоном отсекается флагом scheduler._nightly_running.
        if force:
            return await self._run_locked(only_user_id)

        from backend.core.observability.dist_lock import try_acquire

        # TTL = 45 мин: типичный прогон короче; если процесс упал не освободив
        # лок, он истечёт быстро, и следующий запуск зайдёт (не 4 часа висим).
        async with try_acquire("nightly_consolidation", ttl_seconds=45 * 60) as got:
            if not got:
                logger.info("🌙 Nightly consolidation: lock held by another worker, skipping")
                return {"skipped": True, "reason": "lock_held"}
            return await self._run_locked(only_user_id)

    async def _run_locked(self, only_user_id: Optional[str] = None) -> Dict[str, Any]:
        self._stats["started_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("🌙 Starting nightly consolidation...")

        try:
            # only_user_id (ручной запуск кнопкой) → обрабатываем ТОЛЬКО этого
            # юзера, а не всех: агентство работает с одним клиентом за раз, гонять
            # снапшоты всех тенантов не нужно.
            if only_user_id:
                users = [only_user_id]
            else:
                users = await self._get_users()
            logger.info(f"Found {len(users)} users to process")

            # ИНКРЕМЕНТАЛЬНОСТЬ: у юзеров без новой активности с прошлого
            # успешного прогона всё уже консолидировано — пропускаем (иначе
            # каждая ночь = полный пересчёт ВСЕХ тенантов, и на десятках
            # клиентов окно 2:00-6:00 не хватает). Ручной запуск (only_user_id)
            # идёт всегда — пользователь явно попросил.
            if not only_user_id and len(users) > 1:
                users = await self._filter_users_with_new_activity(users)

            # ПАРАЛЛЕЛИЗМ: юзеры независимы (свой tenant, свои файлы) —
            # обрабатываем по TESSENT_NIGHTLY_CONCURRENCY за раз (default 3).
            # Раньше строго последовательно: 17 мин/юзер × N — на 10+ клиентах
            # ночь не влезала в окно и молча пропускалась.
            import os as _os
            try:
                _conc = max(1, int(_os.getenv("TESSENT_NIGHTLY_CONCURRENCY", "3")))
            except (TypeError, ValueError):
                _conc = 3
            _sem = asyncio.Semaphore(_conc)

            async def _one(user_id: str):
                async with _sem:
                    try:
                        # Метка расходов: всё, что тратит ночная консолидация
                        # по юзеру, атрибутируется agent_mode=nightly (иначе
                        # в экране затрат висит «unknown»).
                        from backend.core.llm.usage_tracker import UsageContext
                        async with UsageContext(agent_mode="nightly",
                                                request_type="consolidation",
                                                user_id=user_id):
                            await self._process_user(user_id)
                        self._stats["users_processed"] += 1
                        self._save_user_watermark(user_id)
                    except Exception as e:
                        logger.error(f"Error processing user {user_id}: {e}")
                        self._stats["errors"].append({
                            "user_id": user_id,
                            "error": str(e),
                        })

            await asyncio.gather(*(_one(u) for u in users))

            # W27: scheduled retention cleanup — раз за весь cycle, не per-user.
            try:
                retention_stats = await self._run_retention_cleanup()
                self._stats["retention"] = retention_stats
            except Exception as e:
                logger.error(f"Retention cleanup failed: {e}")
                self._stats["errors"].append({"retention": str(e)})

            self._stats["completed_at"] = datetime.now(timezone.utc).isoformat()

            # Сохраняем лог
            self._save_log()

            logger.info(
                f"✅ Nightly consolidation completed: "
                f"{self._stats['users_processed']} users, "
                f"{self._stats['corrections_applied']} corrections, "
                f"{self._stats['edges_decayed']} edges decayed"
            )

            return self._stats

        except Exception as e:
            logger.error(f"❌ Nightly consolidation failed: {e}")
            self._stats["errors"].append({"error": str(e)})
            self._stats["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._save_log()
            return self._stats

    @staticmethod
    def _looks_like_uuid(uid: str) -> bool:
        return bool(uid) and len(uid) == 36 and uid.count("-") == 4

    async def _get_users(self) -> List[str]:
        """Получить список пользователей.

        Раньше брали только из data/graphs/*.json (NetworkX) → на Neo4j-деплое
        файлов нет, список был пуст, и весь ночной прогон был no-op. Теперь
        мерджим: json-файлы + DISTINCT user_id/tenant_id из Neo4j.
        """
        users: set = set()

        # 1) NetworkX: файлы графов на диске
        graphs_path = graphs_dir()
        if graphs_path.exists():
            for graph_file in graphs_path.glob("*.json"):
                uid = graph_file.stem
                if self._looks_like_uuid(uid):
                    users.add(uid)

        # 2) Neo4j: перечисляем юзеров прямо из графа (и заодно детектим бэкенд)
        try:
            users.update(await self._enumerate_neo4j_users())
        except Exception as e:
            logger.debug(f"Neo4j user enumeration skipped: {e}")

        return sorted(users)

    async def _enumerate_neo4j_users(self) -> List[str]:
        """DISTINCT user_id/tenant_id из Neo4j. Ставит self._neo4j_active."""
        from backend.core.store.graph_builder import GraphBuilder

        gb = GraphBuilder(use_networkx=None)
        try:
            await gb.connect()
            if getattr(gb, "backend", None) != "neo4j" or not getattr(gb, "driver", None):
                self._neo4j_active = False
                return []
            self._neo4j_active = True
            found: set = set()
            async with gb.driver.session() as session:
                for prop in ("user_id", "tenant_id"):
                    try:
                        res = await session.run(
                            f"MATCH (n) WHERE n.{prop} IS NOT NULL "
                            f"RETURN DISTINCT n.{prop} AS uid LIMIT 10000"
                        )
                        async for rec in res:
                            uid = rec.get("uid")
                            if self._looks_like_uuid(str(uid or "")):
                                found.add(str(uid))
                    except Exception as e:
                        logger.debug(f"distinct {prop} query failed: {e}")
            return sorted(found)
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                logger.debug("graph close failed", exc_info=True)

    async def _curiosity_scan(self, user_id: str) -> Dict[str, Any]:
        """«Чего важного я НЕ знаю о компании?» — модуль самолюбопытства.

        Раз в TESSENT_CURIOSITY_DAYS (деф. 7, 0 = выключено) система читает
        СВОЙ снапшот компании, спрашивает LLM о слепых зонах (то, на что нет
        быстрого ответа по снапшотам: кто ведёт маркетинг? сколько денег
        привлекли? где это записано?) и отправляет ОДИН лучший вопрос в
        фоновое исследование. Находки вернутся через урожай (шаг 2.78) в
        ленту инсайтов — память дозаполняется сама, по капле.

        Гарды: не чаще периода (data/curiosity/<uid>.json), не повторяем уже
        заданные вопросы, не стартуем, если у юзера уже идёт исследование;
        стоимость ограничена бюджетом самого research (~$0.03-0.60)."""
        stats = {"asked": 0}
        try:
            days = int(os.getenv("TESSENT_CURIOSITY_DAYS", "7"))
        except (TypeError, ValueError):
            days = 7
        if days <= 0:
            return stats
        try:
            state_dir = Path("data") / "curiosity"
            state_dir.mkdir(parents=True, exist_ok=True)
            sf = state_dir / f"{user_id}.json"
            st = {}
            if sf.exists():
                try:
                    st = json.loads(sf.read_text(encoding="utf-8"))
                except Exception:
                    st = {}
            last = st.get("last_run", "")
            now = datetime.now(timezone.utc)
            if last:
                try:
                    delta = now - datetime.fromisoformat(last)
                    if delta.days < days:
                        return stats
                except ValueError:
                    pass
            # уже идёт исследование этого юзера → не мешаем
            from backend.core.research.batch_research import (
                _RUNNING_USER, start_research,
            )
            if user_id in _RUNNING_USER.values():
                return stats

            # что система УЖЕ знает — сводка снапшота компании
            known = ""
            try:
                from backend.core.store.tenant_paths import snapshots_dir_for_user
                comp = snapshots_dir_for_user(user_id) / "company" / "company.json"
                if comp.exists():
                    c = json.loads(comp.read_text(encoding="utf-8"))
                    known = json.dumps({
                        "name": c.get("name"), "mission": c.get("mission"),
                        "products": [p.get("name") for p in (c.get("products") or [])][:8],
                        "departments": [d.get("name") for d in (c.get("departments") or [])][:12],
                        "key_people": [p.get("name") for p in (c.get("key_people") or [])][:12],
                        "kpis": c.get("kpis"), "financial_kpis": bool(c.get("financial_kpis")),
                    }, ensure_ascii=False)[:2500]
            except Exception:
                pass
            if not known:
                return stats

            asked_before = st.get("questions", [])
            from backend.core.llm import get_llm_router
            llm = get_llm_router()
            if llm is None:
                return stats
            _prompt = (
                "Ты — память компании, оценивающая СВОИ слепые зоны.\n"
                f"Вот что ты уже знаешь (снапшот):\n{known}\n\n"
                f"Вопросы, которые ты уже исследовала раньше:\n"
                + ("\n".join(f"- {q}" for q in asked_before[-15:]) or "—")
                + "\n\nЧего ВАЖНОГО для управления компанией ты, судя по "
                  "снапшоту, НЕ знаешь или знаешь смутно (пустые отделы, "
                  "нет ответственных, нет финансовых цифр, нет статуса "
                  "ключевых продуктов)? Верни СТРОГО JSON: "
                  '{"questions": ["1-3 конкретных исследовательских '
                  'вопроса, НЕ повторяя уже заданные"]}')

            text = await _nightly_generate(user_id, llm, _prompt,
                                           temperature=0.3, max_tokens=300)
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", text)
            data = json.loads(m.group(0)) if m else {}
            qs = [str(q).strip() for q in (data.get("questions") or [])
                  if isinstance(q, str) and len(str(q).strip()) > 15]
            qs = [q for q in qs if q not in asked_before][:1]  # один за цикл
            if not qs:
                st["last_run"] = now.isoformat()
                sf.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                              encoding="utf-8")
                return stats
            question = qs[0]
            rid = start_research(user_id, question, mode="research")
            stats["asked"] = 1
            st["last_run"] = now.isoformat()
            st.setdefault("questions", []).append(question)
            st["last_research_id"] = rid
            sf.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            logger.info(f"  🧐 Любопытство: «{question[:90]}» → "
                        f"исследование {rid}")
        except Exception:
            logger.debug("curiosity scan failed", exc_info=True)
        return stats

    async def _competitor_radar(self, user_id: str) -> Dict[str, Any]:
        """Радар конкурентов: еженощный дифф их сайтов → инсайты.

        Список наблюдения (data/competitor_radar/<uid>.json): при первом
        запуске сайты конкурентов из снапшота компании резолвятся поиском
        (имя → официальный сайт, кешируется навсегда; ddgs опционален).
        Дальше каждую ночь: страница скачивается, текст хешируется; изменился
        → ОДИН LLM-вызов «что изменилось» → инсайт «конкурент задвигался»
        (цены/акции/новые продукты). Без изменений — 0 LLM. Кап: 3 диффа/ночь.
        URL можно добавить руками в поле "watch" файла состояния."""
        stats = {"checked": 0, "changed": 0}
        try:
            import hashlib
            import re as _re

            state_dir = Path("data") / "competitor_radar"
            state_dir.mkdir(parents=True, exist_ok=True)
            sf = state_dir / f"{user_id}.json"
            st: Dict[str, Any] = {}
            if sf.exists():
                try:
                    st = json.loads(sf.read_text(encoding="utf-8"))
                except Exception:
                    st = {}
            watch: list = list(st.get("watch") or [])

            # Первичный посев: конкуренты из снапшота компании → сайты
            if not st.get("seeded"):
                names: list = []
                try:
                    from backend.core.store.tenant_paths import (
                        snapshots_dir_for_user,
                    )
                    comp = (snapshots_dir_for_user(user_id) / "company"
                            / "company.json")
                    if comp.exists():
                        c = json.loads(comp.read_text(encoding="utf-8"))
                        names = [str(x) for x in (c.get("competitors") or [])
                                 if str(x).strip()][:5]
                except Exception:
                    pass
                try:
                    from ddgs import DDGS

                    def _resolve(name: str) -> str:
                        with DDGS() as d:
                            for r in d.text(f"{name} официальный сайт",
                                            max_results=3):
                                href = r.get("href") or ""
                                if href.startswith("http"):
                                    return href
                        return ""
                    for nm in names:
                        url = await asyncio.to_thread(_resolve, nm)
                        if url and url not in watch:
                            watch.append(url)
                            logger.info(f"  📡 Радар: «{nm}» → {url}")
                except Exception:
                    logger.debug("radar seeding skipped (нет ddgs?)",
                                 exc_info=True)
                st["seeded"] = True
                st["watch"] = watch

            if not watch:
                sf.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                              encoding="utf-8")
                return stats

            import httpx
            pages: Dict[str, Any] = st.setdefault("pages", {})
            diffs_left = 3   # кап LLM-вызовов за ночь
            rows = []
            async with httpx.AsyncClient(
                    timeout=25.0, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (TessentRadar)"}
            ) as client:
                for url in watch[:6]:
                    try:
                        r = await client.get(url)
                        if r.status_code != 200:
                            continue
                        text = _re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", r.text)
                        text = _re.sub(r"<[^>]+>", " ", text)
                        text = _re.sub(r"\s+", " ", text).strip()[:20000]
                    except Exception:
                        continue
                    stats["checked"] += 1
                    h = hashlib.sha256(text.encode()).hexdigest()[:16]
                    prev = pages.get(url) or {}
                    if not prev:
                        pages[url] = {"hash": h, "head": text[:1500],
                                      "checked_at": datetime.now(timezone.utc).isoformat()}
                        continue
                    if prev.get("hash") == h or diffs_left <= 0:
                        prev["checked_at"] = datetime.now(timezone.utc).isoformat()
                        continue
                    # Изменения → LLM формулирует суть (1 вызов на сайт)
                    diffs_left -= 1
                    from backend.core.llm import get_llm_router
                    llm = get_llm_router()
                    summary = ""
                    if llm is not None:
                        try:
                            from backend.core.llm.usage_tracker import UsageContext
                            async with UsageContext(request_type="competitor_radar"):
                                resp = await llm.generate(
                                    prompt=(
                                        f"Сайт конкурента {url} изменился. "
                                        "Сравни фрагменты и скажи СУТЬ изменений "
                                        "для маркетинга (новые продукты, цены, "
                                        "акции, позиционирование). 1-2 предложения, "
                                        "только по фактам; мелкую вёрстку/даты "
                                        "игнорируй; если по сути ничего — «косметика».\n\n"
                                        f"БЫЛО:\n{prev.get('head', '')[:1500]}\n\n"
                                        f"СТАЛО:\n{text[:1500]}"),
                                    temperature=0.2, max_tokens=200)
                            summary = (resp.get("text", "")
                                       if isinstance(resp, dict)
                                       else str(resp or "")).strip()
                        except Exception:
                            logger.debug("radar diff LLM failed", exc_info=True)
                    pages[url] = {"hash": h, "head": text[:1500],
                                  "checked_at": datetime.now(timezone.utc).isoformat()}
                    if summary and "космети" not in summary.lower():
                        stats["changed"] += 1
                        row = {
                            "insight_type": "competitor",
                            "title": f"Конкурент обновил сайт: {url[:60]}"[:90],
                            "description": f"{summary[:420]} ({url})",
                            "priority": "medium",
                            "confidence": 0.7,
                            "entities": [], "actions": [],
                            "source": "competitor_radar",
                        }
                        rows.append(row)
                        logger.info(f"  📡 Радар: {url} — {summary[:100]}")
            if rows:
                from backend.core.sleep.insight_store import (
                    InsightStore, stable_insight_id,
                )
                from backend.core.store.tenant_paths import insights_path_for_user
                for row in rows:
                    row["insight_id"] = stable_insight_id(row)
                InsightStore(persist_path=insights_path_for_user(user_id)).add(rows)
            st["watch"] = watch
            sf.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        except Exception:
            logger.debug("competitor radar failed", exc_info=True)
        return stats

    async def _harvest_research(self, user_id: str) -> Dict[str, Any]:
        """Урожай дневных исследований → лента инсайтов (ноль LLM-вызовов).

        Research сохраняет подтверждённые находки (с датой/встречей/цитатой)
        в data/research_harvest/<uid>/*.json. Ночью — не в момент чата! —
        они становятся инсайтами: находки с цитатой получают уверенность
        выше. Файл после обработки переименовывается в .done — идемпотентно.
        В граф ничего не пишется (защита от замусоривания): инсайты видны,
        ревьюируемы, и их дедупит stable_insight_id."""
        stats = {"harvested": 0}
        try:
            from backend.core.sleep.insight_store import (
                InsightStore, stable_insight_id,
            )
            from backend.core.store.tenant_paths import insights_path_for_user
            hdir = Path("data") / "research_harvest" / user_id
            if not hdir.exists():
                return stats
            rows = []
            for f in sorted(hdir.glob("*.json")):
                if f.name.endswith(".done.json"):
                    continue
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                q = (d.get("question") or "")[:100]
                for fd in (d.get("findings") or [])[:10]:
                    what = (fd.get("what") or "").strip()
                    if len(what) < 20:
                        continue
                    quote = (fd.get("quote") or "").strip()
                    desc = f"{fd.get('date', '')} ({fd.get('title', '')[:60]}): {what}"
                    if quote:
                        desc += f" Цитата: «{quote[:160]}»"
                    desc += f" — из исследования «{q}»"
                    row = {
                        "insight_type": "research",
                        "title": what[:90],
                        "description": desc[:500],
                        "priority": "medium",
                        "confidence": 0.8 if quote else 0.6,
                        "entities": [fd.get("who")] if fd.get("who") else [],
                        "actions": [],
                        "source": "research_harvest",
                    }
                    row["insight_id"] = stable_insight_id(row)
                    rows.append(row)
                try:
                    f.rename(f.with_suffix(".done.json"))
                except Exception:
                    logger.debug("harvest rename failed", exc_info=True)
            if rows:
                InsightStore(persist_path=insights_path_for_user(user_id)).add(rows)
                stats["harvested"] = len(rows)
                logger.info(f"  🌾 Research harvest: {len(rows)} находок из "
                            f"исследований → лента инсайтов")
        except Exception:
            logger.debug("research harvest failed", exc_info=True)
        return stats

    # ── Инкрементальность (watermarks) ────────────────────────────────────
    # Файл data/logs/nightly/watermarks.json: {user_id: iso_ts последнего
    # УСПЕШНОГО прогона}. Юзер обрабатывается, только если в его графе есть
    # активность новее watermark'а. Это первая ступень событийной модели
    # «смотрим на то, что пришло нового», а не пересчитываем всё каждую ночь.

    @property
    def _watermarks_path(self) -> Path:
        return self.log_path / "watermarks.json"

    def _load_watermarks(self) -> Dict[str, str]:
        try:
            return json.loads(self._watermarks_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_user_watermark(self, user_id: str) -> None:
        try:
            wm = self._load_watermarks()
            wm[user_id] = datetime.now(timezone.utc).isoformat()
            self._watermarks_path.write_text(
                json.dumps(wm, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("watermark save failed", exc_info=True)

    async def _latest_activity_ts(self, user_id: str) -> Optional[str]:
        """Самая свежая метка активности юзера (best-effort).

        Neo4j: max(created_at/updated_at) по узлам тенанта. NetworkX: mtime
        файла графа. None → определить не смогли (обрабатываем юзера, чтобы
        не потерять данные из-за сбоя детектора)."""
        try:
            from backend.core.store.graph_builder import GraphBuilder
            gb = GraphBuilder(use_networkx=None,
                              graph_storage_path=graph_path_for_user(user_id))
            await gb.connect()
            try:
                if getattr(gb, "backend", None) == "neo4j" and gb.driver:
                    async with gb.driver.session() as session:
                        res = await session.run(
                            "MATCH (n) WHERE n.tenant_id = $t OR n.user_id = $t "
                            "RETURN max(coalesce(n.updated_at, n.created_at)) AS ts",
                            {"t": user_id},
                        )
                        rec = await res.single()
                        return str(rec["ts"]) if rec and rec["ts"] else None
                p = Path(graph_path_for_user(user_id))
                if p.exists():
                    return datetime.fromtimestamp(
                        p.stat().st_mtime, tz=timezone.utc).isoformat()
                return None
            finally:
                await gb.close(save=False)
        except Exception as e:
            logger.debug(f"latest activity ts failed for {user_id}: {e}")
            return None

    async def _filter_users_with_new_activity(self, users: List[str]) -> List[str]:
        """Оставить юзеров, у которых есть активность новее их watermark'а."""
        wm = self._load_watermarks()
        keep: List[str] = []
        for uid in users:
            mark = wm.get(uid)
            if not mark:
                keep.append(uid)  # ни разу не консолидировался
                continue
            ts = await self._latest_activity_ts(uid)
            if ts is None or ts > mark:
                keep.append(uid)
        skipped = len(users) - len(keep)
        if skipped:
            logger.info(
                f"⏭️  Инкрементальность: {skipped} юзер(ов) без новой активности "
                f"пропущено, обрабатываем {len(keep)}")
        self._stats["users_skipped_no_activity"] = skipped
        return keep

    async def _graph_backend_healthy(self) -> bool:
        """True, если граф-бэкенд, ожидаемый системой, реально доступен.

        Защита от «пустого фолбэка». Если система настроена на Neo4j
        (USE_NETWORKX не выставлен, SDK установлен), но `connect()` не смог
        достучаться до БД — GraphBuilder ТИХО переключается на пустой
        NetworkX (`backend="networkx"`, Nodes: 0). Если в этот момент прогнать
        консолидацию, она:
          • дедупнет по пустому графу (0 clusters — впустую), и
          • перегенерирует снапшоты как persons=0, ЗАТЕРЕВ хорошие данные.
        Это ровно тот сценарий, что случался, пока Docker/Neo4j поднимался.

        Для честного NetworkX-деплоя (USE_NETWORKX=true) фолбэка нет —
        всегда True.
        """
        import os as _os
        if _os.environ.get("USE_NETWORKX", "").lower() in ("true", "1", "yes"):
            return True
        try:
            from backend.core.store.graph_builder import GraphBuilder
            gb = GraphBuilder(use_networkx=None)  # auto-detect
            await gb.connect()
            healthy = getattr(gb, "backend", None) == "neo4j"
            try:
                await gb.close(save=False)
            except Exception:
                logger.debug("health probe close failed", exc_info=True)
            # Neo4j активен → пригодится дальше (гейты graph_path.exists()).
            self._neo4j_active = healthy
            return healthy
        except Exception as e:
            logger.debug("graph backend health check failed: %s", e)
            return False

    async def _process_user(self, user_id: str):
        """Обработать данные одного пользователя."""
        logger.info(f"Processing user: {user_id}")

        # PRECONDITION: система настроена на Neo4j, но он сейчас лежит?
        # Тогда граф свалится на пустой NetworkX-фолбэк — прерываем прогон,
        # чтобы НЕ затереть снапшоты пустотой (persons=0) и не гонять дедуп
        # по пустому графу. Повтор — когда Neo4j поднимется.
        if not await self._graph_backend_healthy():
            logger.warning(
                "⏭️  Пропускаю консолидацию %s: Neo4j сконфигурирован, но "
                "недоступен (граф свалился бы на пустой NetworkX). Не затираю "
                "снапшоты. Повтори, когда Neo4j поднимется.",
                user_id,
            )
            self._stats["errors"].append({
                "user_id": user_id,
                "error": "neo4j_unavailable_skipped",
            })
            return

        # Тайминги шагов → в лог/статистику: без них не видно, какой шаг ест
        # ночь (а «мёртвые» no-op шаги тратят время незаметно). Пишутся в
        # self._stats["step_seconds"] и в сохранённый JSON-лог прогона.
        import time as _time
        step_secs: Dict[str, float] = self._stats.setdefault("step_seconds", {})

        async def _timed(step_name: str, coro):
            _t0 = _time.monotonic()
            try:
                return await coro
            finally:
                step_secs[step_name] = round(
                    step_secs.get(step_name, 0.0) + _time.monotonic() - _t0, 1)

        # 1. Применяем pending исправления
        corrections_stats = await _timed("corrections", self._apply_corrections(user_id))
        self._stats["corrections_applied"] += corrections_stats.get("applied", 0)

        # 2. Temporal decay для графа
        decay_stats = await _timed("temporal_decay", self._apply_temporal_decay(user_id))
        self._stats["edges_decayed"] += decay_stats.get("edges_decayed", 0)

        # 2.2 Снимок объёма памяти — ПОСЛЕ затухания, ДО достройки: меряем
        # то, что реально осталось. Наклон роста без ежедневных снимков не
        # посчитать, а без наклона невозможна политика забывания. Дёшево:
        # чтение своих же JSON, ни одного вызова модели. Never-raise.
        try:
            from backend.core.store.memory_footprint import record_snapshot
            _fp = record_snapshot(user_id)
            logger.info("  Memory footprint: %s nodes, %s edges, %.1f MB",
                        _fp.get("nodes_total"), _fp.get("edges_total"),
                        (_fp.get("total_bytes") or 0) / 1e6)
        except Exception:
            logger.debug("memory footprint snapshot failed", exc_info=True)

        # 2.5 Авто-склейка дублей сущностей (entity resolution). ДО регенерации
        # снапшотов — чтобы они строились по очищенному графу. auto_merge_duplicates
        # был готов, но ночью не гонялся → дубли накапливались.
        dedup_stats = await _timed("entity_dedup", self._dedup_entities(user_id))
        self._stats.setdefault("entities_merged", 0)
        self._stats["entities_merged"] += dedup_stats.get("merged", 0)
        self._stats.setdefault("dedup_on_review", 0)
        self._stats["dedup_on_review"] += dedup_stats.get("review", 0)

        # 2.7 Раскладка людей по отделам: у большинства Person-узлов поле
        # department пусто → иерархия показывает «Без отдела: 60 чел.», каша.
        # LLM распределяет по СУЩЕСТВУЮЩИМ отделам по роли/проектам/коллегам.
        dept_stats = await _timed("departments", self._assign_departments(user_id))
        self._stats.setdefault("departments_assigned", 0)
        self._stats["departments_assigned"] += dept_stats.get("assigned", 0)

        # 2.71 РАМКИ (two-layer из спеки «живой памяти»): каждый принцип/
        # регламент/идея получает applicable_when / not_applicable_when —
        # «голых принципов в памяти не существует». Заодно LLM помечает
        # не-правила (обрывки «кейс, когда...») → rule_book их не берёт.
        frame_stats = await _timed("memory_frames", self._frame_memory_items(user_id))
        self._stats.setdefault("memory_frames", 0)
        self._stats["memory_frames"] += frame_stats.get("framed", 0)

        # 2.72 Нити идей: одна идея из разных встреч — разные узлы Idea.
        # Дубли сливает дедуп (порог 0.90); здесь — СВЯЗЫВАНИЕ развития:
        # LLM группирует идеи в «нити», рёбра EVOLVED_INTO по хронологии
        # (узлы НЕ сливаются — видно, кто предложил и кто во что развил).
        idea_stats = await _timed("idea_threads", self._link_idea_evolution(user_id))
        self._stats.setdefault("idea_threads", 0)
        self._stats["idea_threads"] += idea_stats.get("threads", 0)

        # 2.75 Эволюция психопрофилей: профили пишутся на КАЖДУЮ встречу
        # (история наблюдений); синтезируем «как менялся человек» одним
        # LLM-вызовом по истории — для карточки 360 и таймлайна.
        psy_stats = await _timed("psych_evolution", self._synthesize_psych_evolution(user_id))
        self._stats.setdefault("psych_evolutions", 0)
        self._stats["psych_evolutions"] += psy_stats.get("synthesized", 0)

        # 2.77 Дежавю-детектор: свежие встречи сверяются с уроками прошлого
        # (Contradiction/Pattern/Principle). Совпадение → предупреждение в
        # ленту инсайтов: «похоже на X, в тот раз привело к Y — решайте иначе».
        dv_stats = await _timed("deja_vu", self._deja_vu_scan(user_id))
        self._stats.setdefault("deja_vu_warnings", 0)
        self._stats["deja_vu_warnings"] += dv_stats.get("warnings", 0)

        # 2.78 УРОЖАЙ ИССЛЕДОВАНИЙ: находки завершённых research-прогонов
        # (за которые уже заплачено токенами) вливаются в ленту инсайтов —
        # ноль LLM-вызовов, дедуп по stable_insight_id внутри стора. Система
        # «дообучается на том, что делала днём»: связи, найденные по вопросу
        # пользователя, не остаются похороненными в чате.
        rh_stats = await _timed("research_harvest",
                                self._harvest_research(user_id))
        self._stats.setdefault("research_harvested", 0)
        self._stats["research_harvested"] += rh_stats.get("harvested", 0)

        # 2.79 ЛЮБОПЫТСТВО (раз в TESSENT_CURIOSITY_DAYS, деф. 7): система
        # смотрит на свои снапшоты и спрашивает себя «чего важного я НЕ знаю
        # о компании?» → 1 вопрос уходит в фоновое исследование, находки
        # вернутся через урожай (2.78) следующей ночью. Слепые зоны памяти
        # закрываются сами, по капле, без роста чека.
        cu_stats = await _timed("curiosity", self._curiosity_scan(user_id))
        self._stats.setdefault("curiosity_questions", 0)
        self._stats["curiosity_questions"] += cu_stats.get("asked", 0)

        # 2.8 Карточки клиентов (B2B): по клиентским организациям из графа —
        # LLM-вердикт «статус отношений / договорённости / риски / следующий
        # шаг». B2C-режим: клиентов слишком много → только топ по активности.
        client_stats = await _timed("clients", self._update_client_snapshots(user_id))
        self._stats.setdefault("client_cards", 0)
        self._stats["client_cards"] += client_stats.get("cards", 0)

        # 2.85 ДОМЕННЫЕ СНАПШОТЫ (реестр: маркетинг/продажи/финансы/...):
        # дневные наработки агентов (Knowledge-буфер) + профильные решения
        # из встреч → живая картина каждой функции компании. Агент домена
        # читает свою целиком, смежные — кратко. Граф читается один раз,
        # максимум 1 LLM-вызов на домен и только при новых данных; домен
        # без материала не строится вовсе. См. core/sleep/domain_snapshots.py
        from backend.core.sleep.domain_snapshots import update_domain_snapshots
        dom_stats = await _timed("domain_snapshots",
                                 update_domain_snapshots(user_id))
        self._stats.setdefault("domain_snapshots", 0)
        self._stats["domain_snapshots"] += dom_stats.get("updated", 0)

        # 2.86 РАДАР КОНКУРЕНТОВ: следим за сайтами конкурентов из снапшота
        # компании (URL резолвятся один раз и кешируются). Изменился контент
        # → LLM формулирует «что изменилось» → инсайт в ленту. Без изменений
        # — 0 LLM-вызовов.
        radar_stats = await _timed("competitor_radar",
                                   self._competitor_radar(user_id))
        self._stats.setdefault("radar_changes", 0)
        self._stats["radar_changes"] += radar_stats.get("changed", 0)

        # 3. Консолидация памяти агентов
        memory_stats = await _timed("agent_memory", self._consolidate_agent_memory(user_id))
        self._stats["memories_consolidated"] += memory_stats.get("semantic_created", 0)

        # 4. Dream-inspired Memory Consolidation (саммаризация, кластеризация, архивация)
        consolidation_stats = await _timed("memory_consolidation", self._consolidate_memories(user_id))
        self._stats["memories_compressed"] += consolidation_stats.get("summaries_created", 0)
        self._stats["memories_clustered"] += consolidation_stats.get("clusters_created", 0)
        self._stats["memories_archived"] += consolidation_stats.get("memories_archived", 0)

        # 5. Обновление снапшотов
        snapshot_stats = await _timed("snapshots", self._update_snapshots(user_id))
        self._stats["snapshots_updated"] += snapshot_stats.get("updated", 0)

        # 6. Batch-извлечение предпочтений из истории чатов
        pref_stats = await _timed("chat_preferences", self._extract_chat_preferences(user_id))
        self._stats.setdefault("preferences_extracted", 0)
        self._stats["preferences_extracted"] += pref_stats.get("extracted", 0)

        # 7. Синтез мудрости (DIKW)
        wisdom_stats = await _timed("wisdom", self._synthesize_wisdom(user_id))
        self._stats.setdefault("wisdom_principles_created", 0)
        self._stats["wisdom_principles_created"] += wisdom_stats.get("created", 0)

        # 8. W7 user-adaptive: LLM-куратор обновляет профиль (style/lang/focus).
        curator_stats = await _timed("profile_curator", self._curate_user_profile(user_id))
        self._stats.setdefault("profiles_curated", 0)
        self._stats.setdefault("profile_updates_total", 0)
        if curator_stats.get("applied"):
            self._stats["profiles_curated"] += 1
            self._stats["profile_updates_total"] += curator_stats.get("applied", 0)

        # 8.5 Rule Book: сводим регламенты (Regulation-узлы из встреч) в
        # компактный набор правил, который чат подмешивает в system-prompt.
        # rule_book был готов, но не наполнялся и не читался.
        rb_stats = await _timed("rule_book", self._maintain_rule_book(user_id))
        self._stats.setdefault("rule_book_rules", 0)
        self._stats["rule_book_rules"] += rb_stats.get("rules", 0)

        # 9. Phase N «последняя миля»: ночные инсайты → reactive learning-loop.
        # NightProcessor находит инсайты по графу юзера, а submit_night_insights
        # прогоняет их через gate (load-aware) + калибровку и доставляет/паркует.
        insights_stats = await _timed("night_insights", self._deliver_night_insights(user_id))
        self._stats.setdefault("insights_submitted", 0)
        self._stats.setdefault("insights_skipped", 0)
        self._stats["insights_submitted"] += insights_stats.get("submitted", 0)
        self._stats["insights_skipped"] += insights_stats.get("skipped", 0)

        # 10. BWE crystal maintenance (за crystal_enrichment_enabled): фоновый
        # движок для кристаллов — кластеризует pending-решения и обновляет
        # обогащение/оси/decay-якорь. Это переводит decay/обогащение из
        # «ленивого на чтении» в проактивное ночное обслуживание.
        bwe_stats = await _timed("bwe_crystals", self._bwe_crystal_maintenance(user_id))
        self._stats.setdefault("bwe_patterns_updated", 0)
        self._stats["bwe_patterns_updated"] += bwe_stats.get("patterns_updated", 0)

        # 11. Проактивный скан слепых зон/рассинхронов (за proactive_scan_enabled):
        # система САМА находит пробелы по графу и доставляет как night-insights.
        proactive = await _timed("proactive_scan", self._proactive_skeleton_scan(user_id))
        self._stats.setdefault("proactive_insights", 0)
        self._stats["proactive_insights"] += proactive.get("submitted", 0)

        # 11.6 Расхождения единых метрик («на встречах 5 млн, по данным
        # 4.2 млн») → warning-инсайты. ДО фокуса дня: свежий алерт может
        # стать фокусом этой же ночью (warning — первый в ротации).
        mdiv = await _timed("metric_divergence",
                            self._metric_divergence_scan(user_id))
        self._stats.setdefault("metric_divergence_alerts", 0)
        self._stats["metric_divergence_alerts"] += mdiv.get("alerts", 0)

        # 12. Фокус дня: система САМА выбирает самое важное сейчас (риск /
        # дежавю-предупреждение / прогноз / клиент в зоне риска / инсайт),
        # чередуя типы по дням → баннер на главном экране. «Живая» система.
        focus_stats = await _timed("daily_focus", self._pick_daily_focus(user_id))
        self._stats.setdefault("daily_focus", 0)
        self._stats["daily_focus"] += 1 if focus_stats.get("picked") else 0

        # 12.5 Финансовый пульс (онтология цифр → память): финансовые
        # датасеты из реестра → KPI-узлы графа (временные ряды); аномалии
        # (скачок/просадка периода) → инсайты. «Система сама считает,
        # что в компании происходит и что не так».
        fin_stats = await _timed("finance_pulse", self._sync_finance_kpis(user_id))
        self._stats.setdefault("finance_kpis", 0)
        self._stats["finance_kpis"] += fin_stats.get("kpis", 0)
        self._stats.setdefault("finance_alerts", 0)
        self._stats["finance_alerts"] += fin_stats.get("alerts", 0)

        # 13. Отчёт дня: формат чередуется по дням недели (дайджест / люди /
        # клиенты / риски / проекты) — «система сама делает разные отчёты».
        # Сборка детерминированная из готовых данных (ноль LLM), доставка в
        # Telegram через mini Tess, если задан TESSENT_TESS_CHAT_ID.
        rep_stats = await _timed("daily_report", self._generate_daily_report(user_id))
        self._stats.setdefault("daily_reports", 0)
        self._stats["daily_reports"] += 1 if rep_stats.get("generated") else 0

        _slow = sorted(step_secs.items(), key=lambda kv: kv[1], reverse=True)[:5]
        logger.info(
            "  ⏱ %s: топ шагов по времени: %s", user_id[:8],
            ", ".join(f"{k}={v}s" for k, v in _slow))

    # Guardrail E: окно «свежести» десинка. Если последние факты по обеим целям
    # старше — конфликт, вероятно, уже неактуален (решён/устарел), не эмитим.
    DESYNC_STALE_DAYS = 120

    def _filter_stale_desyncs(self, desyncs: list, nodes: list) -> list:
        """Отсеять рассинхроны, чьи факты-источники устарели (временная привязка).

        Раньше десинк висел вечно: old_fact мог быть 5-летней давности, а
        система показывала его как активный конфликт. Здесь сопоставляем тексты
        целей с узлами графа и берём самую свежую дату; если по обеим целям
        свежих фактов нет — отбрасываем.
        """
        if not desyncs:
            return desyncs
        try:
            from datetime import timedelta

            # text(норм.) → самая свежая ISO-дата среди узлов с этим текстом
            def _norm(s):
                return str(s or "").strip().lower()

            text_ts: Dict[str, str] = {}
            for n in (nodes or []):
                if not isinstance(n, dict):
                    continue
                ts = n.get("updated_at") or n.get("created_at") or n.get("timestamp")
                if not ts:
                    continue
                for tk in ("statement", "goal", "description", "summary", "name"):
                    v = n.get(tk)
                    if v and str(v).strip():
                        key = _norm(v)
                        if key not in text_ts or str(ts) > text_ts[key]:
                            text_ts[key] = str(ts)
            if not text_ts:
                # Нет дат вообще — не режем (нечем судить), пропускаем как есть
                return desyncs

            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.DESYNC_STALE_DAYS)).isoformat()
            fresh = []
            for d in desyncs:
                ta = text_ts.get(_norm(d.get("goal_a")))
                tb = text_ts.get(_norm(d.get("goal_b")))
                newest = max([t for t in (ta, tb) if t], default=None)
                # Если дату нашли и она старше окна — устарело, пропускаем
                if newest is not None and newest < cutoff:
                    continue
                fresh.append(d)
            dropped = len(desyncs) - len(fresh)
            if dropped:
                logger.info(f"  Desync: {dropped} устаревших отсеяно (temporal anchoring)")
            return fresh
        except Exception as e:
            logger.debug(f"stale desync filter failed: {e}")
            return desyncs

    async def _validate_desyncs_llm(self, desyncs: list) -> list:
        """LLM-валидация рассинхронов (опц., за флагом conflict_llm_validation_enabled).

        Скелет ловит конфликт по ключевым словам направлений (↑/↓), что даёт
        ложные срабатывания: «увеличить сделки» vs «снизить риск» — разные
        метрики, не конфликт. LLM судит «реальный ли это конфликт?» и отсеивает
        шум. Только для metric-десинков; resource (soft) пропускаем как есть.
        Флаг выключен по умолчанию → нулевая стоимость, пока не включат.
        """
        if not desyncs:
            return desyncs
        try:
            from backend.config import settings as _settings
            if not getattr(_settings, "conflict_llm_validation_enabled", False):
                return desyncs
            from backend.core.llm import get_llm_router
            router = get_llm_router()
            if router is None:
                return desyncs
        except Exception:
            return desyncs

        validated = []
        for d in desyncs:
            if d.get("conflict_type") != "metric":
                validated.append(d)  # soft/resource — не трогаем
                continue
            try:
                prompt = (
                    "Две цели разных отделов. Это РЕАЛЬНЫЙ конфликт (одну метрику "
                    "тянут в противоположные стороны), или ложное совпадение слов "
                    "(разные метрики)? Ответь строго JSON "
                    '{"conflict": true|false, "confidence": 0..1}.\n'
                    f"Цель A ({d.get('department_a')}): {d.get('goal_a')}\n"
                    f"Цель B ({d.get('department_b')}): {d.get('goal_b')}\n"
                    f"Общие токены метрики: {d.get('shared')}"
                )
                verdict = await router.generate_json(
                    prompt, max_tokens=120, temperature=0.0
                )
                if not isinstance(verdict, dict):
                    verdict = {}
                if verdict.get("conflict") is True and float(verdict.get("confidence", 0)) >= 0.6:
                    validated.append(d)
                # иначе — отсеиваем как ложный
            except Exception as e:
                logger.debug(f"desync LLM validation failed: {e}")
                validated.append(d)  # при сбое не теряем сигнал
        dropped = len(desyncs) - len(validated)
        if dropped:
            logger.info(f"  Desync: {dropped} ложных отсеяно LLM-валидацией")
        return validated

    async def _proactive_skeleton_scan(self, user_id: str) -> Dict[str, Any]:
        """Проактивно: BlindSpotScanner.scan_from_graph + desync из графа →
        NightInsight → submit_night_insights (тот же канал, что night-insights:
        фильтр actionable+confidence, load-aware gate, доставка/паркинг).
        За флагом proactive_scan_enabled; graceful (не роняет ночь)."""
        stats = {"submitted": 0, "skipped": 0}
        try:
            from backend.config import settings as _settings
            if not getattr(_settings, "proactive_scan_enabled", False):
                return stats

            from backend.core.store.graph_builder import GraphBuilder
            from backend.core.store.tenant_paths import graph_path_for_user
            from backend.core.think.blind_spot_scanner import (
                BlindSpotScanner,
                proactive_insights,
            )
            from backend.core.think.cross_department_desync import (
                dept_goals_from_graph_nodes,
                find_desyncs,
            )

            gb = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))
            await gb.connect()
            nxg = getattr(gb, "nx_graph", None)
            if nxg is not None:
                # NetworkX: у узлов уже есть _label в атрибутах
                nodes = [d for _i, d in nxg.nodes(data=True)]
            else:
                # Neo4j: раньше nodes=[] (скан был мёртв). Фетчим целе-/паттерн-
                # несущие метки и проставляем _label, чтобы сканеры их увидели.
                nodes = []
                for _label in ("Pattern", "Contradiction", "Project", "KPI",
                               "Decision", "Goal", "Objective", "Initiative",
                               "Principle"):
                    try:
                        for n in await gb.find_nodes_by_label(_label, limit=500):
                            n["_label"] = _label
                            nodes.append(n)
                    except Exception:
                        continue
            try:
                await gb.close(save=False)
            except Exception:
                pass
            if not nodes:
                return stats

            blind = [b.to_dict() for b in BlindSpotScanner().scan_from_graph(nodes)]
            desyncs = [d.to_dict() for d in find_desyncs(dept_goals_from_graph_nodes(nodes))]

            # Guardrail E: временная привязка + отсев устаревших/ложных
            desyncs = self._filter_stale_desyncs(desyncs, nodes)
            desyncs = await self._validate_desyncs_llm(desyncs)

            insight_dicts = proactive_insights(blind, desyncs)
            if not insight_dicts:
                return stats
            # Временная метка обнаружения — на каждый инсайт (анти-«вечный флаг»)
            _detected_at = datetime.now(timezone.utc).isoformat()
            for _d in insight_dicts:
                _d["detected_at"] = _detected_at

            # REUSE 17 принципов / 8 патологий — обогащаем ПОЧЕМУ (claim-guard:
            # принцип появляется только при уверенном соответствии).
            if getattr(_settings, "principle_mapping_enabled", False):
                from backend.core.think.systems_principles import enrich_insights
                insight_dicts = enrich_insights(insight_dicts)

            # dict → NightInsight
            import uuid

            from backend.core.reactive.recommendations import submit_night_insights
            from backend.core.sleep.night_processor import (
                InsightPriority,
                InsightType,
                NightInsight,
            )

            insights = [
                NightInsight(
                    insight_id=str(uuid.uuid4()),
                    insight_type=InsightType(d["insight_type"]),
                    priority=InsightPriority(d["priority"]),
                    title=d["title"],
                    description=(
                        d["description"] + f"\n\nПочему (системно): {d['principle']['why']}"
                        if d.get("principle") else d["description"]
                    ),
                    evidence=d["evidence"], actions=d["actions"],
                    confidence=d["confidence"], is_actionable=True,
                    metadata={"source": "proactive_skeleton", "kind": d.get("kind"),
                              "principle": d.get("principle"),
                              "detected_at": d.get("detected_at")},
                )
                for d in insight_dicts
            ]
            res = await submit_night_insights(user_id=user_id, insights=insights, channels=["telegram"])
            # Аудит UI: те же инсайты — в персистентную ленту страницы
            # (раньше уходили только в Telegram и терялись для UI).
            _persist_insights_to_store(user_id, insights, source="proactive_skeleton")
            stats["submitted"] = int(res.get("submitted", 0))
            stats["skipped"] = int(res.get("skipped", 0))
            if stats["submitted"]:
                logger.info(f"  🔮 Proactive scan: {stats['submitted']} пробелов доставлено для {user_id[:8]}")
            return stats
        except Exception as e:
            logger.warning(f"Proactive skeleton scan failed for {user_id}: {e}")
            return stats

    async def _bwe_crystal_maintenance(self, user_id: str) -> Dict[str, Any]:
        """Ночное обслуживание BWE-кристаллов. За флагом crystal_enrichment_enabled;
        любой сбой не роняет ночную консолидацию (graceful)."""
        try:
            from backend.config import settings as _settings
            if not getattr(_settings, "crystal_enrichment_enabled", False):
                return {"patterns_updated": 0}
            from backend.core.wisdom.wisdom_engine import get_wisdom_engine
            engine = await get_wisdom_engine()
            n = await engine.recompute_patterns(user_id)
            if n:
                logger.info(f"  🔮 BWE crystals updated for {user_id}: {n}")
            return {"patterns_updated": n}
        except Exception as e:
            logger.warning(f"BWE crystal maintenance failed for {user_id}: {e}")
            return {"patterns_updated": 0}

    async def _extract_chat_preferences(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """
        Batch-извлечение предпочтений из истории чатов.

        Анализирует последние сообщения пользователя через LLM
        и обновляет профиль пользователя (UserProfileService).
        """
        stats = {"extracted": 0, "rules": 0}

        try:
            from backend.core.config import flags
            if not flags.enable_user_feedback_learning:
                return stats

            from backend.core.capture.agents.chat_preference_extractor import (
                get_chat_preference_extractor,
            )
            from backend.memory.user_profiles import UserProfileService

            profile_svc = UserProfileService()
            await profile_svc.initialize()

            # Получаем историю запросов за последние 24 часа
            history = await profile_svc.get_recent_queries(user_id, limit=50)

            if not history or len(history) < 5:
                return stats  # Слишком мало данных

            # Формируем messages для batch-анализа
            messages = [
                {"role": "user", "content": q.get("query", "")}
                for q in history if q.get("query")
            ]

            if len(messages) < 5:
                return stats

            # LLM batch-анализ
            try:
                from backend.core.llm.router import LLMRouter
                llm_router = LLMRouter()

                extractor = get_chat_preference_extractor()
                result = await extractor.extract_batch(messages, llm_router)

                if result.preferences or result.rules:
                    await extractor.apply_to_profile(result, user_id, profile_svc)
                    stats["extracted"] = len(result.preferences)
                    stats["rules"] = len(result.rules)

                    logger.info(
                        f"  Chat preferences: {stats['extracted']} prefs, "
                        f"{stats['rules']} rules for user {user_id[:8]}"
                    )
            except Exception as llm_err:
                logger.debug(f"  LLM batch preference extraction skipped: {llm_err}")

        except ImportError:
            logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.error(f"  Error extracting chat preferences: {e}")

        return stats

    async def _curate_user_profile(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """W7 hermes-style profile curation.

        Запускает LLM-куратора, который анализирует последние сессии
        пользователя и предлагает минимальные обновления профиля
        (стиль/язык/фокус/экспертиза). Изменения проходят whitelist
        полей и порог `min_confidence=0.6` внутри ProfileCurator.

        Идемпотентно: если предложений нет (or confidence низкий) —
        профиль не меняется. Если все предложения дублируют текущие
        значения — обновлений тоже нет.
        """
        stats: Dict[str, Any] = {"proposed": 0, "applied": 0}

        try:
            from backend.core.config import flags
            if not getattr(flags, "enable_profile_curator", False):
                return stats

            from backend.core.memory.profile_curator import (
                ProfileCurator,
                apply_proposals,
            )
            from backend.memory.user_profiles import UserProfileService

            profile_svc = UserProfileService()
            await profile_svc.initialize()

            history = await profile_svc.get_recent_queries(user_id, limit=50)
            if not history or len(history) < 5:
                return stats

            messages = [
                {"role": "user", "content": q.get("query", "")}
                for q in history if q.get("query")
            ]
            if len(messages) < 5:
                return stats

            try:
                from backend.core.llm.router import LLMRouter
                llm_router = LLMRouter()
            except Exception as router_err:
                logger.debug(f"  Profile curator: LLMRouter unavailable: {router_err}")
                return stats

            current_profile = await profile_svc.get_profile(user_id)
            current_dict = current_profile.to_dict()

            curator = ProfileCurator(llm_router=llm_router)
            proposals = await curator.curate(
                messages=messages,
                current_profile=current_dict,
            )
            stats["proposed"] = len(proposals)
            if not proposals:
                return stats

            new_dict = apply_proposals(current_dict, proposals)

            updates = {
                k: v for k, v in new_dict.items()
                if k not in current_dict or current_dict[k] != v
            }
            if not updates:
                return stats

            await profile_svc.update_profile(user_id, updates)
            stats["applied"] = len(updates)

            logger.info(
                f"  Profile curator: {stats['applied']} fields updated "
                f"({stats['proposed']} proposals) for user {user_id[:8]}"
            )

        except ImportError:
            logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.error(f"  Error curating user profile: {e}")

        return stats

    async def _synthesize_wisdom(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """Синтез мудрости (DIKW layer)."""
        stats = {"created": 0}

        try:
            # Импорты внутри метода
            from backend.core.capture.agents.synthesis_agent import SynthesisAgent
            from backend.core.store.graph_builder import GraphBuilder

            graph_path = Path(graph_path_for_user(user_id))
            if not graph_path.exists() and not getattr(self, "_neo4j_active", False):
                return stats

            # Подключаем граф
            graph = GraphBuilder(
                use_networkx=None,
                graph_storage_path=str(graph_path)
            )
            await graph.connect()

            # Для синтеза нам нужны данные.
            # Используем SnapshotGenerator чтобы собрать данные за последнее время
            # (или берем последние встречи из графа)

            # В данном примере используем данные из графа
            # TODO: Получать данные за последнюю неделю через GraphBuilder.get_recent_data()

            # Создаем SynthesisAgent
            agent = SynthesisAgent(graph_builder=graph)

            # 1. Собираем данные (пока мок, в реальности нужно выгружать из графа)
            # В будущем: data = await graph.get_weekly_data()
            data = {
                "projects": await graph.get_all_nodes_async(label="Project", limit=50),
                "tasks": await graph.get_all_nodes_async(label="Task", limit=100),
                "decisions": await graph.get_all_nodes_async(label="Decision", limit=50)
            }

            # 2. Анализируем паттерны
            analysis = await agent.analyze_patterns(
                all_modules_data={"main": data},
                meeting_id="nightly_synthesis"
            )

            # 3. Синтезируем принципы
            principles = await agent.synthesize_principles(analysis)

            # 4. Сохраняем в граф (guardrail D: с доказательной базой)
            import json as _json
            for p in principles:
                import uuid
                pid = f"principle_{uuid.uuid4().hex[:8]}"

                evidence = p.get("evidence", []) or []
                evidence_count = int(p.get("evidence_count", 0))

                await graph.create_principle(
                    principle_id=pid,
                    statement=p["statement"],
                    domain=p["domain"],
                    confidence=p["confidence"],
                    metadata={
                        "source": p["source"],
                        # Audit trail: чем подтверждён принцип и сколькими кейсами
                        "evidence_count": evidence_count,
                        "evidence": _json.dumps(evidence, ensure_ascii=False),
                        # Слабо подтверждённые помечаем для ревью человеком
                        "needs_validation": evidence_count < 2,
                    }
                )
                stats["created"] += 1

            logger.info(
                f"  Wisdom synthesis: {stats['created']} principles created "
                f"(grounded in evidence)"
            )

        except Exception as e:
            logger.error(f"  Error synthesizing wisdom: {e}")

        return stats

    async def _deliver_night_insights(
        self,
        user_id: str,
    ) -> Dict[str, Any]:
        """Phase N «последняя миля»: ночные инсайты юзера → reactive loop.

        Граф-глобальный NightProcessor запускается на ПЕРСОНАЛЬНОМ графе
        юзера (как _synthesize_wisdom / _apply_temporal_decay), поэтому
        найденные инсайты относятся именно к нему. submit_night_insights
        фильтрует (actionable + confidence>=0.5), прогоняет через delivery-gate
        (load-aware) и доставляет в Telegram либо паркует в reactive_signals.
        Calibration учит per-type пороги по фидбеку автоматически.

        Never-raise: best-effort, как остальные ночные шаги.
        """
        stats = {"submitted": 0, "skipped": 0}

        # Реактивный движок (доставка в Telegram) может быть выключен — но
        # ЛЕНТА инсайтОВ (страница UI) должна наполняться всегда. Раньше при
        # reactive_engine_enabled=False (дефолт) был ранний return → NightProcessor
        # не запускался вовсе, InsightStore пустой, страница «Инсайты» вечно
        # пустая. Теперь флаг гейтит только доставку в каналы.
        deliver_to_channels = True
        try:
            from backend.core.reactive.rollout import is_reactive_enabled_for_user
            deliver_to_channels = is_reactive_enabled_for_user(user_id)
        except Exception:
            # rollout недоступен — доставку не пытаемся, но ленту наполняем
            deliver_to_channels = False

        try:
            from backend.core.reactive.recommendations import submit_night_insights
            from backend.core.sleep.night_processor import NightProcessor
            from backend.core.store.graph_builder import GraphBuilder

            graph_path = Path(graph_path_for_user(user_id))
            if not graph_path.exists() and not getattr(self, "_neo4j_active", False):
                return stats

            graph = GraphBuilder(
                use_networkx=None,
                graph_storage_path=str(graph_path),
            )
            await graph.connect()

            # LLM-роутер опционален: NightProcessor без него выдаёт
            # эвристические инсайты (connections/patterns/activity), с ним —
            # ещё и LLM-synthesis. Не валим шаг если LLM недоступен.
            llm_router = None
            try:
                from backend.core.llm.router import LLMRouter
                llm_router = LLMRouter()
            except Exception as exc:
                logger.debug("  NightInsights: LLMRouter unavailable: %s", exc)

            processor = NightProcessor(graph_builder=graph, llm_router=llm_router,
                                       user_id=user_id)  # F2-fix: per-user EventLog
            result = await processor.run_night_processing()
            insights = getattr(result, "insights", None) or []
            if not insights:
                return stats

            # ПЕРСИСТЕНС инсайтов (CAPABILITY_READINESS B6): раньше жили только
            # в RAM + разовой отправке — истории не было. Сохраняем ДО отправки
            # в каналы (идемпотентно по insight_id). Best-effort: не роняет ночь.
            try:
                from backend.core.sleep.insight_store import InsightStore
                from backend.core.store.tenant_paths import insights_path_for_user
                store = InsightStore(persist_path=insights_path_for_user(user_id))
                persisted = store.add([i.to_dict() for i in insights])
                stats["persisted"] = persisted.get("added", 0)
                logger.info("  Night insights persisted: %s for user %s",
                            persisted, user_id)
            except Exception as exc:
                logger.debug("  Night insights persistence skipped: %s", exc)

            # Аудит UI: ночные (в т.ч. LLM-) инсайты — в персистентную ленту
            # страницы ВСЕГДА, независимо от каналов доставки.
            _persist_insights_to_store(user_id, insights, source="night_cycle")

            # Доставка в каналы (Telegram) — только при включённом reactive.
            if deliver_to_channels:
                persona = None
                try:
                    from backend.core.persona.store import get_persona_store
                    persona = await get_persona_store().get(user_id)
                except Exception:
                    persona = None

                submit_result = await submit_night_insights(
                    user_id=user_id,
                    insights=insights,
                    persona=persona,
                    channels=["telegram"],
                )
                stats["submitted"] = int(submit_result.get("submitted", 0))
                stats["skipped"] = int(submit_result.get("skipped", 0))
            logger.info(
                "  Night insights: %d в ленту, %d submitted, %d skipped for user %s",
                len(insights), stats["submitted"], stats["skipped"], user_id[:8],
            )

            # SIMA P2 звено A: авто-детектор предложений автоматизации (по сводке
            # компании) — за env-флагом AUTOMATION_DETECT_NIGHTLY (деф. ВЫКЛ),
            # чтобы не тратить токены и не спамить без явного согласия. Дедуп —
            # в store; never-raise.
            try:
                from backend.core.automations.opportunity_detector import (
                    nightly_detection_enabled, run_detection,
                )
                if nightly_detection_enabled():
                    det = await run_detection(user_id, max_items=3, source="night_detector")
                    stats["proposals_created"] = int(det.get("count", 0))
            except Exception as exc:
                logger.debug("  Automation proposal detection skipped: %s", exc)

            # SIMA Kanon: ночной пересчёт runnable-блоков + пуш «готово к
            # подтверждению: N» — за флагом KANON_NIGHTLY_PREPARE (деф. ВЫКЛ).
            # Ничего не запускает (запуск агента — только явный confirm).
            try:
                from backend.core.sima.kanon_nightly import (
                    nightly_prepare_enabled, run_for_user,
                )
                if nightly_prepare_enabled():
                    kn = await run_for_user(user_id)
                    stats["kanon_runnable"] = int(kn.get("runnable", 0))
            except Exception as exc:
                logger.debug("  Kanon nightly prepare skipped: %s", exc)

        except Exception as e:
            logger.error(f"  Error delivering night insights: {e}")

        return stats

    async def _apply_corrections(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """Применить pending исправления."""
        stats = {"applied": 0, "failed": 0}

        try:
            from backend.core.config import flags
            if not flags.enable_two_phase_corrections:
                return stats

            from backend.core.corrections import get_two_phase_manager
            from backend.core.store.graph_builder import GraphBuilder

            graph_path = Path(graph_path_for_user(user_id))
            if not graph_path.exists() and not getattr(self, "_neo4j_active", False):
                return stats

            graph = GraphBuilder(
                use_networkx=None,  # Auto-detect backend
                graph_storage_path=str(graph_path)
            )
            await graph.connect()

            manager = get_two_phase_manager(graph)
            result = await manager.apply_pending_corrections(apply_approved_only=True)

            stats["applied"] = result.get("applied", 0)
            stats["failed"] = result.get("failed", 0)

            logger.info(f"  Corrections: {stats['applied']} applied")

        except ImportError:
            logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.error(f"  Error applying corrections: {e}")

        return stats

    async def _apply_temporal_decay(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """Применить temporal decay к связям графа."""
        stats = {"edges_processed": 0, "edges_decayed": 0}

        try:
            from backend.core.config import flags
            if not flags.enable_temporal_decay:
                return stats

            from backend.core.memory import get_hebbian_learning
            from backend.core.store.graph_builder import GraphBuilder

            graph_path = Path(graph_path_for_user(user_id))
            if not graph_path.exists() and not getattr(self, "_neo4j_active", False):
                return stats

            graph = GraphBuilder(
                use_networkx=None,  # Auto-detect backend
                graph_storage_path=str(graph_path)
            )
            await graph.connect()

            hebbian = get_hebbian_learning(graph)
            result = await hebbian.apply_temporal_decay(decay_rate=0.01)

            stats["edges_processed"] = result.get("edges_processed", 0)
            stats["edges_decayed"] = result.get("edges_decayed", 0)
            stats["edges_removed"] = result.get("edges_removed", 0)

            logger.info(
                f"  Temporal decay: {stats['edges_decayed']} decayed, "
                f"{stats['edges_removed']} removed (provisional cleanup)"
            )

        except ImportError:
            logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.error(f"  Error applying temporal decay: {e}")

        return stats

    async def _maintain_rule_book(self, user_id: str) -> Dict[str, Any]:
        """Свести регламенты (Regulation-узлы) в Rule Book юзера.

        Источник — реальные регламенты, извлечённые из встреч (а не выдумка).
        Чат читает RuleBook.to_prompt() и следует им. Идемпотентно: add_rule
        дедупит/разрешает конфликты; при переполнении — ночная суммаризация.
        """
        stats = {"rules": 0, "summarized": False}
        try:
            from backend.core.memory.rule_book import Rule, get_rule_book_service
            from backend.core.store.graph_builder import GraphBuilder

            graph_path = Path(graph_path_for_user(user_id))
            if not graph_path.exists() and not getattr(self, "_neo4j_active", False):
                return stats
            graph = GraphBuilder(use_networkx=None, graph_storage_path=str(graph_path))
            await graph.connect()
            regs = await graph.get_all_nodes_async(label="Regulation")
            await graph.close(save=False)
            if not regs:
                return stats

            svc = get_rule_book_service()
            book = svc.load(user_id)
            added = 0
            for r in regs[:50]:
                # Two-layer: обрывки, помеченные фреймингом как не-правила
                # («кейс, когда встречи проводились неэффективно»), в Rule Book
                # не попадают; у настоящих правил рамка вплетается в текст.
                if r.get("frame_status") == "not_a_rule":
                    continue
                txt = (r.get("name") or r.get("description") or r.get("summary") or "").strip()
                if txt and len(txt) > 5:
                    aw = (r.get("applicable_when") or "").strip()
                    naw = (r.get("not_applicable_when") or "").strip()
                    rule_text = txt[:200]
                    if aw:
                        rule_text += f" (применимо: {aw[:100]}"
                        if naw:
                            rule_text += f"; НЕ применимо: {naw[:80]}"
                        rule_text += ")"
                    if book.add_rule(Rule(text=rule_text[:380], category="regulation", source="meeting")):
                        added += 1
            if book.needs_summarization():
                try:
                    from backend.core.llm.router import LLMRouter
                    await svc.summarize(book, llm_router=LLMRouter())
                    stats["summarized"] = True
                except Exception:
                    logger.debug("rule book summarize skipped", exc_info=True)
            svc.save(book)
            stats["rules"] = added
            if added:
                logger.info(f"  Rule book: +{added} regulation rule(s)")
        except Exception as e:
            logger.error(f"  Error maintaining rule book: {e}")
        return stats

    async def _dedup_entities(self, user_id: str) -> Dict[str, Any]:
        """Авто-склейка ОЧЕВИДНЫХ дублей людей/проектов в графе.

        Guardrail B: умный dedup. >= 0.95 сливаем сразу; полоса 0.80–0.94 —
        отдаём LLM (понимает «Катя»/«Екатерина», транслит, опечатки), сливаем
        только при уверенном подтверждении. Раньше авто-слияние было лишь при
        >= 0.95, поэтому варианты имён не сливались и дубли копились. На Neo4j
        поиск кандидатов раньше был заглушкой — dedup вообще не работал.
        """
        stats = {"merged": 0, "candidates": 0}
        try:
            from backend.core.store.entity_resolver import EntityResolver
            from backend.core.store.graph_view import merged_graph_view_for_user

            # ВАЖНО: тот же граф, что снапшоты/team360 (Neo4j-view), а НЕ
            # GraphBuilder(файл). Раньше дедуп смотрел в per-user файл (пустой),
            # находил <2 людей и LLM даже не звался (Entity dedup: 0 clusters за
            # 0мс), хотя снапшоты на Neo4j-view видят persons=323. Merges пишутся
            # в Neo4j через live-driver, так что сохраняются.
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)
            resolver = EntityResolver(graph_builder=graph, vector_indexer=None)
            # ВАЖНО: выставляем tenant-контекст. Без него внутренний
            # find_nodes_by_label скоупится на `tenant_id IS NULL` (фоновый
            # прогон = нет контекста) и НЕ видит людей юзера → 0 кандидатов,
            # 0 слияний (в логе entities_merged=0, хотя дублей полно).
            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            _tok = set_current_tenant(user_id)
            review_clusters: list = []
            try:
                # Repair составных узлов («Шитов / Кустова»), созданных старым
                # выбором keep-узла: возвращаем чистое имя из aliases, иначе
                # реальные люди навсегда спрятаны view-фильтром. Идемпотентно.
                try:
                    _rep = await resolver.repair_combined_persons(tenant_id=user_id)
                    if _rep:
                        stats["repaired_combined"] = _rep
                        logger.info(f"  🩹 Repaired combined persons: {_rep}")
                except Exception:
                    logger.debug("repair_combined_persons skipped", exc_info=True)
                # Content-aware кластерный дедуп: LLM группирует записи ОДНИМ
                # вызовом (учитывая активность — проекты/коллеги/отдел), а не N
                # попарных проверок. Высокую уверенность сливаем, среднюю —
                # копим на ревью пользователю (последний этап, без авто-мёрджа).
                # idea: сливаются только ПОЧТИ идентичные формулировки (строгий
                # порог не-персон 0.90) — дубли одной идеи из повторных
                # обсуждений; полноценная сшивка «идея развилась» — через
                # Batch Research (полное чтение транскриптов).
                # department/team/product: не-персонный матч строгий
                # (только почти идентичные имена, 0.90) — «Отдел маркетинга»
                # и «Маркетинг» НЕ сольются сами, а «Отдел маркетинга» и
                # «отдел маркетинга » — да. Путаница таймстемпов/диминутивов
                # тут не живёт, риск низкий.
                # company: контрагенты B2B (Entity c entity_type
                # company|organization) — свой матчер: юрформы/кавычки/
                # транслит — шум; «Альфа Банк»≠«Альфа Страхование»;
                # филиалы не авто-сливаются (confidence ≤0.7 → ревью)
                for etype in ("person", "project", "idea", "company",
                              "department", "team", "product"):
                    try:
                        r = await resolver.cluster_and_merge(
                            entity_type=etype, tenant_id=user_id,
                        )
                        stats["merged"] += r.get("merged", 0)
                        stats["candidates"] += r.get("clusters", 0)
                        for c in r.get("review", []):
                            c["entity_type"] = etype
                            review_clusters.append(c)
                    except Exception:
                        logger.debug(f"dedup {etype} skipped", exc_info=True)
            finally:
                reset_current_tenant(_tok)
            # save=False: слияния уже записаны в Neo4j через driver; файл вьюхи
            # перезаписывать не нужно.
            await graph.close(save=False)
            stats["review"] = len(review_clusters)
            self._save_dedup_review(user_id, review_clusters)
            logger.info(
                f"  Entity dedup: {stats['candidates']} clusters, "
                f"{stats['merged']} merged, {len(review_clusters)} on review"
            )
        except Exception as e:
            logger.error(f"  Error in entity dedup: {e}")
        return stats

    async def _assign_departments(self, user_id: str) -> Dict[str, Any]:
        """LLM-раскладка людей без department по существующим отделам.

        ОДИН вызов на батч ≤80 человек. Только СУЩЕСТВУЮЩИЕ отделы (из графа) —
        LLM не выдумывает новые. Неуверенные ('') не трогаем. Идемпотентно:
        обрабатываются только люди с пустым department."""
        stats = {"assigned": 0}
        try:
            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            from backend.core.sleep.enhanced_snapshot import _is_person_junk
            from backend.core.store.graph_view import merged_graph_view_for_user
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)
            _tok = set_current_tenant(user_id)
            try:
                depts = await graph.find_nodes_by_label(
                    "Department", limit=100, tenant_id=user_id, strict_tenant=True)
                dept_names = sorted({(d.get("name") or "").strip()
                                     for d in depts if (d.get("name") or "").strip()})
                if not dept_names:
                    return stats
                persons = await graph.find_nodes_by_label(
                    "Person", limit=2000, tenant_id=user_id, strict_tenant=False)
                todo = [p for p in persons
                        if p.get("id") and (p.get("name") or "").strip()
                        and not (p.get("department") or "").strip()
                        and not _is_person_junk(p.get("name"))]
                if not todo:
                    return stats
                from backend.core.llm import get_llm_router
                llm = get_llm_router()
                if llm is None:
                    return stats
                import json as _json
                for i in range(0, len(todo), 80):
                    batch = todo[i:i + 80]
                    listing = "\n".join(
                        f'{j}. "{p.get("name")}"'
                        + (f' — {p.get("role")}' if p.get("role") else "")
                        for j, p in enumerate(batch, 1))
                    prompt = (
                        "Распредели людей по отделам компании. Разрешённые отделы "
                        f"(ТОЛЬКО из этого списка): {', '.join(dept_names)}.\n"
                        "Если по роли/имени непонятно, к какому отделу человек "
                        "относится, или он внешний (гость/клиент/эксперт) — пустая "
                        'строка "". НЕ выдумывай отделы, НЕ угадывай.\n\n'
                        f"Люди:\n{listing}\n\n"
                        'Ответ СТРОГО JSON: {"1": "отдел или пусто", "2": "..."}'
                    )
                    try:
                        resp = await llm.generate(prompt=prompt, temperature=0.0,
                                                  max_tokens=2048)
                        text = resp.get("text", "") if isinstance(resp, dict) else str(resp)
                        m = text[text.find("{"):text.rfind("}") + 1]
                        mapping = _json.loads(m) if m else {}
                    except Exception as e:
                        logger.debug(f"dept assign LLM batch failed: {e}")
                        continue
                    for j, p in enumerate(batch, 1):
                        dept = (mapping.get(str(j)) or "").strip()
                        if dept and dept in dept_names:
                            try:
                                await graph.update_node(p["id"], {"department": dept})
                                stats["assigned"] += 1
                            except Exception:
                                logger.debug("dept update failed", exc_info=True)
            finally:
                reset_current_tenant(_tok)
                await graph.close(save=False)
            if stats["assigned"]:
                logger.info(f"  🏢 Departments: {stats['assigned']} чел. разложено по отделам")
        except Exception as e:
            logger.error(f"  Error assigning departments: {e}")
        return stats

    async def _frame_memory_items(self, user_id: str) -> Dict[str, Any]:
        """Two-layer (спека живой памяти, фаза 1): рамка каждому элементу.

        Regulation/Principle/Idea без applicable_when → батч-LLM проставляет
        {applicable_when, not_applicable_when} или {not_a_rule: true} для
        обрывков, которые правилом не являются. Идемпотентно: обработанные
        (есть applicable_when или frame_status) пропускаются. Кап 60/ночь."""
        stats = {"framed": 0, "not_rules": 0}
        try:
            import json as _json

            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            from backend.core.store.graph_view import merged_graph_view_for_user
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)
            _tok = set_current_tenant(user_id)
            try:
                from backend.core.llm import get_llm_router
                llm = get_llm_router()
                if llm is None:
                    return stats
                todo: list = []
                for label in ("Regulation", "Principle", "Idea"):
                    try:
                        nodes = await graph.find_nodes_by_label(
                            label, limit=300, tenant_id=user_id,
                            strict_tenant=False)
                    except Exception:
                        continue
                    for n in nodes:
                        txt = (n.get("name") or n.get("statement")
                               or n.get("summary") or "").strip()
                        if (n.get("id") and txt and len(txt) > 15
                                and not n.get("applicable_when")
                                and not n.get("frame_status")):
                            todo.append({"id": n["id"], "label": label,
                                         "text": txt[:220]})
                todo = todo[:60]
                if not todo:
                    return stats
                for start in range(0, len(todo), 20):
                    batch = todo[start:start + 20]
                    listing = "\n".join(
                        f'{j}. [{it["label"]}] {it["text"]}'
                        for j, it in enumerate(batch, 1))
                    prompt = (
                        "Для каждого элемента памяти компании определи РАМКУ "
                        "применимости — по самому тексту элемента, не додумывай "
                        "область за пределами сказанного. Если текст — не правило/"
                        "принцип/идея, а обрывок описания или кейс без вывода — "
                        "пометь not_a_rule.\n\n"
                        f"{listing}\n\n"
                        "Ответ СТРОГО JSON-объектом по номерам:\n"
                        '{"1": {"applicable_when": "когда применимо, кратко", '
                        '"not_applicable_when": "когда НЕ применимо/границы", '
                        '"not_a_rule": false}, "2": {...}}')
                    try:
                        resp = await llm.generate(prompt=prompt, temperature=0.0,
                                                  max_tokens=1600)
                        text = resp.get("text", "") if isinstance(resp, dict) else str(resp)
                        s_, e_ = text.find("{"), text.rfind("}")
                        mapping = _json.loads(text[s_:e_ + 1]) if s_ != -1 else {}
                    except Exception as e:
                        logger.debug(f"framing LLM batch failed: {e}")
                        continue
                    for j, it in enumerate(batch, 1):
                        fr = mapping.get(str(j))
                        if not isinstance(fr, dict):
                            continue
                        try:
                            if fr.get("not_a_rule"):
                                await graph.update_node(it["id"],
                                                        {"frame_status": "not_a_rule"})
                                stats["not_rules"] += 1
                            else:
                                await graph.update_node(it["id"], {
                                    "applicable_when": str(fr.get("applicable_when") or "")[:200],
                                    "not_applicable_when": str(fr.get("not_applicable_when") or "")[:200],
                                    "frame_status": "framed",
                                })
                                stats["framed"] += 1
                        except Exception:
                            logger.debug("frame update failed", exc_info=True)
            finally:
                reset_current_tenant(_tok)
                await graph.close(save=False)
            if stats["framed"] or stats["not_rules"]:
                logger.info(f"  🖼 Frames: {stats['framed']} с рамкой, "
                            f"{stats['not_rules']} помечено не-правилами")
        except Exception as e:
            logger.error(f"  Error framing memory items: {e}")
        return stats

    async def _link_idea_evolution(self, user_id: str) -> Dict[str, Any]:
        """Связать развитие идей: LLM группирует Idea-узлы в «нити» (одна
        идея, развивавшаяся на разных встречах), рёбра EVOLVED_INTO по
        хронологии + thread_id. НЕ сливает узлы: авторство каждого шага
        (Person-PROPOSED) сохраняется — видно «кто предложил, кто докрутил».
        Идемпотентно: идеи с thread_id пропускаются."""
        stats = {"threads": 0, "linked": 0}
        try:
            import json as _json

            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            from backend.core.store.graph_view import merged_graph_view_for_user
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)
            _tok = set_current_tenant(user_id)
            try:
                from backend.core.llm import get_llm_router
                llm = get_llm_router()
                if llm is None:
                    return stats
                ideas = await graph.find_nodes_by_label(
                    "Idea", limit=500, tenant_id=user_id, strict_tenant=False)
                todo = [i for i in ideas
                        if i.get("id") and (i.get("name") or "").strip()
                        and not i.get("thread_id")]
                if len(todo) < 2:
                    return stats
                # даты встреч для хронологии нитей
                mdates: Dict[str, str] = {}
                try:
                    for m in await graph.find_nodes_by_label(
                            "Meeting", limit=2000, tenant_id=user_id,
                            strict_tenant=False):
                        mid = str(m.get("meeting_id") or m.get("id") or "")
                        if mid:
                            mdates[mid] = str(m.get("date") or m.get("created_at") or "")
                except Exception:
                    logger.debug("idea threads: meeting dates unavailable")
                for i in todo:
                    i["_date"] = mdates.get(str(i.get("meeting_id") or ""), "")
                todo.sort(key=lambda i: i["_date"])

                for start in range(0, len(todo), 80):
                    batch = todo[start:start + 80]
                    listing = "\n".join(
                        f'{j}. [{(i["_date"] or "????")[:10]}] "{i.get("name")}"'
                        + (f' (автор: {i.get("author")})' if i.get("author") else "")
                        for j, i in enumerate(batch, 1))
                    prompt = (
                        "Идеи из встреч компании (хронологически). Сгруппируй "
                        "ТОЛЬКО те, что являются РАЗВИТИЕМ ОДНОЙ И ТОЙ ЖЕ идеи "
                        "(тема/механика совпадает, идея дорабатывалась). Разные "
                        "идеи об одном проекте — НЕ нить. Группы из 2-5 номеров.\n\n"
                        f"{listing}\n\n"
                        'Ответ СТРОГО JSON: [{"members": [номера], '
                        '"topic": "суть нити, 3-6 слов"}] или [].')
                    try:
                        text = await _nightly_generate(user_id, llm, prompt,
                                                       temperature=0.0, max_tokens=1200)
                        s_, e_ = text.find("["), text.rfind("]")
                        clusters = _json.loads(text[s_:e_ + 1]) if s_ != -1 else []
                    except Exception as e:
                        logger.debug(f"idea threads LLM batch failed: {e}")
                        continue
                    for c in clusters if isinstance(clusters, list) else []:
                        nums = [n for n in (c.get("members") or [])
                                if isinstance(n, int) and 1 <= n <= len(batch)]
                        if len(nums) < 2 or len(nums) > 5:
                            continue
                        chain = sorted((batch[n - 1] for n in set(nums)),
                                       key=lambda i: i["_date"])
                        tid = f"thread_{chain[0]['id']}"
                        for a, b in zip(chain, chain[1:]):
                            try:
                                await graph.create_relationship(
                                    from_id=a["id"], to_id=b["id"],
                                    rel_type="EVOLVED_INTO",
                                    properties={"topic": (c.get("topic") or "")[:80]})
                                stats["linked"] += 1
                            except Exception:
                                logger.debug("idea edge failed", exc_info=True)
                        for i in chain:
                            try:
                                await graph.update_node(i["id"], {"thread_id": tid})
                            except Exception:
                                logger.debug("idea thread_id failed", exc_info=True)
                        stats["threads"] += 1
            finally:
                reset_current_tenant(_tok)
                await graph.close(save=False)
            if stats["threads"]:
                logger.info(f"  💡 Idea threads: {stats['threads']} нитей, "
                            f"{stats['linked']} связей EVOLVED_INTO")
        except Exception as e:
            logger.error(f"  Error linking idea evolution: {e}")
        return stats

    async def _synthesize_psych_evolution(self, user_id: str) -> Dict[str, Any]:
        """Эволюция психопрофиля: по людям с 3+ профилями-наблюдениями —
        один LLM-вызов «как менялось поведение/роль/стиль» → свойство
        psych_evolution на Person-узле (читается 360/таймлайном).
        Идемпотентно: пропускаем, если число наблюдений не изменилось."""
        stats = {"synthesized": 0}
        try:
            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            from backend.core.sleep.enhanced_snapshot import _is_person_junk
            from backend.core.store.graph_view import merged_graph_view_for_user
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)
            _tok = set_current_tenant(user_id)
            try:
                from backend.core.llm import get_llm_router
                llm = get_llm_router()
                if llm is None:
                    return stats
                persons = await graph.find_nodes_by_label(
                    "Person", limit=2000, tenant_id=user_id, strict_tenant=True)
                # только заметные люди (по вовлечённости), кап 15 за ночь
                persons = sorted(
                    (p for p in persons
                     if p.get("id") and not _is_person_junk(p.get("name") or "")),
                    key=lambda p: -(p.get("total_mentions") or 0))[:15]
                for p in persons:
                    try:
                        rels = await graph.get_node_relationships(p["id"])
                        profs = []
                        for e in (rels.get("outgoing") or []) + (rels.get("incoming") or []):
                            other = e.get("target") or e.get("source")
                            if not other:
                                continue
                            n = await graph.get_node_by_id(other)
                            if n and (n.get("_label") or "") == "PsychologicalProfile":
                                profs.append(n)
                        if len(profs) < 3:
                            continue
                        profs.sort(key=lambda n: str(n.get("created_at") or ""))
                        # идемпотентность: то же число наблюдений — не пересинтезируем
                        if int(p.get("psych_evolution_n") or 0) == len(profs):
                            continue
                        lines = []
                        for pr in profs[-8:]:
                            lines.append(
                                f"{str(pr.get('created_at') or '')[:10]}: "
                                f"тип {pr.get('personality_type', '?')}; "
                                f"роль {pr.get('team_role', '?')}; "
                                f"стиль {str(pr.get('communication_style') or '')[:80]}")
                        prompt = (
                            f"Наблюдения за «{p.get('name')}» по встречам (от ранних "
                            "к поздним). Опиши в 3-4 предложениях, КАК МЕНЯЛСЯ человек: "
                            "роль, стиль, вовлечённость; что стабильно. По фактам, "
                            "без выдумок.\n\n" + "\n".join(lines))
                        text = (await _nightly_generate(user_id, llm, prompt,
                                                        temperature=0.2, max_tokens=300)).strip()
                        if text:
                            await graph.update_node(p["id"], {
                                "psych_evolution": text[:1000],
                                "psych_evolution_n": len(profs),
                            })
                            stats["synthesized"] += 1
                    except Exception:
                        logger.debug("psych evolution skipped for person", exc_info=True)
            finally:
                reset_current_tenant(_tok)
                await graph.close(save=False)
            if stats["synthesized"]:
                logger.info(f"  🧬 Psych evolution: {stats['synthesized']} чел.")
        except Exception as e:
            logger.error(f"  Error synthesizing psych evolution: {e}")
        return stats

    # Финансовые колонки: имя колонки → метрика (мост онтологии цифр в граф)
    _MONEY_COLS = ("выручка", "revenue", "доход", "income", "расход", "cost",
                   "затрат", "expense", "маржа", "margin", "прибыл", "profit",
                   "сумма", "amount", "оплат", "payment", "продаж", "sales")
    _DATE_COLS = ("месяц", "month", "дата", "date", "период", "period")

    async def _sync_finance_kpis(self, user_id: str) -> Dict[str, Any]:
        """Мост «онтология цифр → память»: по финансовым датасетам реестра
        строятся месячные ряды метрик → KPI-узлы графа (kpi_ds_<метрика>,
        series в props). Аномалия последнего периода (|Δ| ≥ 25% к предыдущему)
        → инсайт finance_pulse (питает ленту и «Фокус дня»)."""
        stats = {"kpis": 0, "alerts": 0}
        try:
            from backend.core.ontology.dataset_registry import to_number
            from backend.core.ontology.dataset_service import registry_for_user
            reg = registry_for_user(user_id)
            datasets = reg.list() or []
            if not datasets:
                return stats

            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            from backend.core.store.graph_view import merged_graph_view_for_user
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)
            _tok = set_current_tenant(user_id)
            alerts: list = []
            try:
                for ds in datasets[:10]:
                    cols = [str(c) for c in (ds.get("columns") or [])]
                    low = {c: c.lower() for c in cols}
                    date_col = next((c for c in cols if any(
                        k in low[c] for k in self._DATE_COLS)), None)
                    money_cols = [c for c in cols if any(
                        k in low[c] for k in self._MONEY_COLS)]
                    if not date_col or not money_cols:
                        continue
                    rows = reg.load_rows(ds.get("id") or ds.get("dataset_id") or "")
                    if not rows:
                        continue
                    for mc in money_cols[:4]:
                        # месячная агрегация: YYYY-MM → сумма
                        series: Dict[str, float] = {}
                        for r in rows:
                            v = to_number(r.get(mc))
                            if v is None:
                                continue
                            d = str(r.get(date_col) or "")
                            # нормализуем к YYYY-MM (терпим DD.MM.YYYY и ISO)
                            m = None
                            if len(d) >= 7 and d[4] in "-./" :
                                m = d[:7].replace(".", "-").replace("/", "-")
                            else:
                                import re as _re
                                mm = _re.search(r"(\d{2})[./-](\d{4})", d)
                                if mm:
                                    m = f"{mm.group(2)}-{mm.group(1)}"
                            if m:
                                series[m] = series.get(m, 0.0) + v
                        if len(series) < 2:
                            continue
                        months = sorted(series)
                        kpi_id = (f"kpi_ds_{(ds.get('id') or '')[:8]}_"
                                  + "".join(ch if ch.isalnum() else "_"
                                            for ch in mc.lower())[:30])
                        try:
                            import json as _json
                            await graph.create_node(
                                node_id=kpi_id, label="KPI",
                                properties={
                                    "name": mc,
                                    "kpi_id": kpi_id,
                                    "numeric_value": series[months[-1]],
                                    "period": months[-1],
                                    "series": _json.dumps(
                                        {m: series[m] for m in months[-12:]},
                                        ensure_ascii=False),
                                    "source_dataset": ds.get("title") or "",
                                    "tenant_id": user_id, "user_id": user_id,
                                },
                            )
                            stats["kpis"] += 1
                        except Exception:
                            logger.debug("finance KPI node failed", exc_info=True)
                        # пульс: скачок/просадка последнего периода
                        prev, cur = series[months[-2]], series[months[-1]]
                        if prev and abs(cur - prev) / abs(prev) >= 0.25:
                            direction = "вырос(ла)" if cur > prev else "просел(а)"
                            pct = abs(cur - prev) / abs(prev) * 100
                            alerts.append({
                                "title": f"📉 {mc}: {direction} на {pct:.0f}% "
                                         f"({months[-1]})",
                                "description": (
                                    f"«{mc}» из «{ds.get('title', '')}»: "
                                    f"{prev:,.0f} → {cur:,.0f} за {months[-1]}. "
                                    f"Проверьте причину."),
                            })
            finally:
                reset_current_tenant(_tok)
                await graph.close(save=False)

            if alerts:
                _persist_insights_to_store(
                    user_id,
                    [type("I", (), {
                        "insight_type": type("T", (), {"value": "risk"})(),
                        "title": a["title"], "description": a["description"],
                        "priority": type("P", (), {"value": "high"})(),
                        "confidence": 0.9, "entities": [], "actions": [],
                    })() for a in alerts],
                    source="finance_pulse")
                stats["alerts"] = len(alerts)
            if stats["kpis"] or stats["alerts"]:
                logger.info(f"  💰 Finance pulse: {stats['kpis']} KPI-рядов, "
                            f"{stats['alerts']} аномалий")
        except Exception as e:
            logger.error(f"  Error in finance pulse: {e}")
        return stats

    async def _generate_daily_report(self, user_id: str) -> Dict[str, Any]:
        """Отчёт дня с ротацией форматов по дням недели:
        пн=дайджест, вт=люди, ср=клиенты, чт=риски, пт=проекты (сб/вс — пн-формат).
        Детерминированная сборка из готовых данных (ноль LLM). Сохраняется в
        data/reports/daily/<uid>.json; в Telegram — если TESSENT_TESS_CHAT_ID."""
        stats = {"generated": False}
        try:
            import json as _json

            from backend.core.store.tenant_paths import snapshots_dir_for_user
            base = snapshots_dir_for_user(user_id)
            wd = datetime.now(timezone.utc).weekday()
            fmt = ["digest", "people", "clients", "risks", "projects",
                   "digest", "digest"][wd]
            titles = {"digest": "📰 Дайджест", "people": "👥 Люди недели",
                      "clients": "💼 Клиенты", "risks": "🔥 Риски",
                      "projects": "📁 Проекты"}
            lines: List[str] = [f"{titles[fmt]} · {datetime.now(timezone.utc).strftime('%d.%m.%Y')}"]

            def _read_dir(sub: str, cap: int = 8) -> list:
                out = []
                d = base / sub
                if d.exists():
                    for f in sorted(d.glob("*.json"),
                                    key=lambda x: x.stat().st_mtime,
                                    reverse=True)[:cap]:
                        try:
                            out.append(_json.loads(f.read_text(encoding="utf-8")))
                        except Exception:
                            continue
                return out

            if fmt == "people":
                for p in _read_dir("persons", 6):
                    s = (p.get("activity_summary") or "").strip()
                    if s:
                        lines.append(f"• {p.get('name', '')}: {s[:160]}")
            elif fmt == "clients":
                for c in _read_dir("clients", 8):
                    lines.append(f"• {c.get('name', '')} [{c.get('status', '?')}]: "
                                 f"{(c.get('summary') or '')[:140]}")
                    if c.get("next_step"):
                        lines.append(f"  → {c['next_step'][:120]}")
            elif fmt == "projects":
                for p in _read_dir("projects", 6):
                    s = (p.get("ai_summary") or p.get("description") or "").strip()
                    if s:
                        lines.append(f"• {p.get('name', '')} ({p.get('status', '?')}, "
                                     f"{p.get('progress', 0)}%): {s[:150]}")
            elif fmt == "risks":
                try:
                    from backend.core.sleep.insight_store import InsightStore
                    from backend.core.store.tenant_paths import insights_path_for_user
                    for it in InsightStore(
                            persist_path=insights_path_for_user(user_id)).list(limit=30):
                        t = str(it.get("insight_type") or it.get("type") or "").lower()
                        if "risk" in t or t == "warning" or it.get("source") == "deja_vu":
                            lines.append(f"• {it.get('title', '')}: "
                                         f"{(it.get('description') or '')[:150]}")
                        if len(lines) > 8:
                            break
                except Exception:
                    logger.debug("report risks unavailable", exc_info=True)
            else:  # digest: по чуть-чуть отовсюду
                cl = _read_dir("clients", 3)
                pr = _read_dir("projects", 3)
                if pr:
                    lines.append("Проекты:")
                    lines += [f"• {p.get('name', '')} — {p.get('status', '?')}, "
                              f"{p.get('progress', 0)}%" for p in pr]
                if cl:
                    lines.append("Клиенты:")
                    lines += [f"• {c.get('name', '')} [{c.get('status', '?')}]" for c in cl]
                try:
                    from backend.core.sleep.insight_store import InsightStore
                    from backend.core.store.tenant_paths import insights_path_for_user
                    top = InsightStore(
                        persist_path=insights_path_for_user(user_id)).list(limit=3)
                    if top:
                        lines.append("Главное из инсайтов:")
                        lines += [f"• {i.get('title', '')}" for i in top]
                except Exception:
                    logger.debug("report digest insights unavailable", exc_info=True)

            # МАРКЕТИНГ-ПУЛЬС (в любой формат дня): если этой ночью обновился
            # доменный снапшот маркетинга — «что сейчас в работе»; плюс
            # свежие сигналы радара конкурентов. Ноль LLM — чтение готового.
            try:
                from backend.core.sleep.domain_snapshots import snapshot_path
                mf = snapshot_path(user_id, "marketing")
                if mf.exists():
                    md = _json.loads(mf.read_text(encoding="utf-8"))
                    upd = str(md.get("updated_at") or "")
                    fresh = False
                    if upd:
                        delta = (datetime.now(timezone.utc)
                                 - datetime.fromisoformat(upd))
                        fresh = delta.days < 1
                    if fresh:
                        txt = str(md.get("markdown") or "")
                        m = txt.split("## Сейчас в работе", 1)
                        if len(m) > 1:
                            body = m[1].split("##", 1)[0].strip()
                            if body:
                                lines.append("📈 Маркетинг сейчас:")
                                lines += [ln for ln in body.splitlines()
                                          if ln.strip()][:4]
                from backend.core.sleep.insight_store import InsightStore
                from backend.core.store.tenant_paths import insights_path_for_user
                radar = [i for i in InsightStore(
                    persist_path=insights_path_for_user(user_id)).list(limit=20)
                    if i.get("source") == "competitor_radar"
                    and str(i.get("stored_at") or i.get("created_at") or "")[:10]
                    == datetime.now(timezone.utc).strftime("%Y-%m-%d")]
                if radar:
                    lines.append("📡 Конкуренты задвигались:")
                    lines += [f"• {i.get('description', '')[:150]}"
                              for i in radar[:3]]
            except Exception:
                logger.debug("marketing pulse skipped", exc_info=True)

            if len(lines) < 2:
                return stats  # данных нет — отчёт не плодим
            text = "\n".join(lines)
            rdir = Path("data") / "reports" / "daily"
            rdir.mkdir(parents=True, exist_ok=True)
            (rdir / f"{user_id}.json").write_text(_json.dumps(
                {"format": fmt, "title": titles[fmt], "text": text,
                 "date": datetime.now(timezone.utc).isoformat()},
                ensure_ascii=False, indent=2), encoding="utf-8")
            stats["generated"] = True
            logger.info(f"  📰 Daily report [{fmt}]: {len(lines) - 1} строк")

            # Доставка в Telegram (mini Tess) — только если чат задан явно.
            import os as _os
            chat_id = _os.getenv("TESSENT_TESS_CHAT_ID", "").strip()
            if chat_id:
                try:
                    from backend.integrations.tessent_brain_tools import (
                        TessentBrainTools,
                    )
                    tools = TessentBrainTools(user_id=user_id)
                    await tools.send_telegram_message(message=text, chat_id=chat_id)
                    logger.info("  📨 Daily report отправлен в Telegram")
                except Exception as te:
                    logger.warning(f"  Daily report TG delivery failed: {te}")
        except Exception as e:
            logger.error(f"  Error generating daily report: {e}")
        return stats

    async def _metric_divergence_scan(self, user_id: str) -> Dict[str, Any]:
        """Сверка единых метрик (metric_registry): где цифры со встреч
        расходятся с цифрами из подключённых данных сильнее порога —
        warning-инсайт в ленту (id детерминированный — не задваивается).
        Порог: TESSENT_METRIC_DIVERGENCE_PCT (дефолт 10)."""
        stats: Dict[str, Any] = {"alerts": 0}
        try:
            from backend.core.ontology.dataset_registry import ontology_enabled
            if not ontology_enabled():
                return stats
            import os
            from backend.core.ontology.metric_registry import (
                divergence_insights,
            )
            try:
                threshold = float(
                    os.getenv("TESSENT_METRIC_DIVERGENCE_PCT", "10"))
            except ValueError:
                threshold = 10.0
            insights = divergence_insights(user_id, threshold_pct=threshold)
            if not insights:
                return stats
            from backend.core.sleep.insight_store import (
                InsightStore,
                stable_insight_id,
            )
            from backend.core.store.tenant_paths import insights_path_for_user
            now = datetime.now(timezone.utc).isoformat()
            for i in insights:
                i["insight_id"] = stable_insight_id(i)
                i["created_at"] = now
            res = InsightStore(
                persist_path=insights_path_for_user(user_id)).add(insights)
            stats["alerts"] = res.get("added", len(insights)) \
                if isinstance(res, dict) else len(insights)
            logger.info(f"  ⚖️ Metric divergence: {len(insights)} расхождений "
                        f"(порог {threshold}%) → warning-инсайты")
            # Событие для автоматизаций: пользователь может построить
            # триггер «метрика разошлась → уведомить/поставить задачу».
            # Эмитим только НОВЫЕ алерты (new_ids) — без ночного спама.
            try:
                if isinstance(res, dict) and res.get("new_ids"):
                    new_ids = set(res["new_ids"])
                    from backend.core.automations.event_bus import (
                        EventType,
                        get_event_bus,
                    )
                    for i in insights:
                        if i.get("insight_id") not in new_ids:
                            continue
                        await get_event_bus().emit(
                            event_type=EventType.KPI_CHANGED,
                            payload={"kind": "divergence",
                                     "metric": i.get("metric"),
                                     "delta_pct": i.get("delta_pct"),
                                     "title": i.get("title"),
                                     "description": i.get("description")},
                            user_id=user_id,
                            source="metric_divergence_scan")
            except Exception:
                logger.debug("divergence event emit skipped", exc_info=True)
        except Exception as e:
            logger.error(f"  Error in metric divergence scan: {e}")
        return stats

    async def _pick_daily_focus(self, user_id: str) -> Dict[str, Any]:
        """Фокус дня: одна самая важная вещь сейчас, тип чередуется по дням
        (warning → risk → client → prediction → insight), чтобы система
        «жила», а не показывала одно и то же. Кандидаты — из уже готовых
        хранилищ (лента инсайтов, карточки клиентов, Scenario-узлы) — ноль
        LLM-вызовов. Пишется в data/focus/<uid>.json."""
        stats = {"picked": False}
        try:
            import json as _json

            buckets: Dict[str, list] = {"warning": [], "risk": [], "client": [],
                                        "prediction": [], "insight": []}
            # 1) лента инсайтов (включая дежавю-предупреждения)
            try:
                from backend.core.sleep.insight_store import InsightStore
                from backend.core.store.tenant_paths import insights_path_for_user
                for it in InsightStore(
                        persist_path=insights_path_for_user(user_id)).list(limit=40):
                    t = str(it.get("insight_type") or it.get("type") or "").lower()
                    src = str(it.get("source") or "")
                    entry = {"title": it.get("title", ""),
                             "description": (it.get("description") or "")[:280],
                             "priority": str(it.get("priority") or "medium")}
                    if src == "deja_vu" or t == "warning":
                        buckets["warning"].append(entry)
                    elif "risk" in t or "conflict" in t:
                        buckets["risk"].append(entry)
                    else:
                        buckets["insight"].append(entry)
            except Exception:
                logger.debug("focus: insights unavailable", exc_info=True)
            # 2) клиенты в зоне риска (карточки ночного шага 2.8)
            try:
                from backend.core.store.tenant_paths import snapshots_dir_for_user
                cdir = snapshots_dir_for_user(user_id) / "clients"
                if cdir.exists():
                    for f in cdir.glob("*.json"):
                        c = _json.loads(f.read_text(encoding="utf-8"))
                        if c.get("status") == "риск":
                            buckets["client"].append({
                                "title": f"Клиент в зоне риска: {c.get('name', '')}",
                                "description": (c.get("risks") or c.get("summary") or "")[:280],
                                "priority": "high"})
            except Exception:
                logger.debug("focus: clients unavailable", exc_info=True)
            # 3) прогнозы (Scenario/Prediction из графа)
            try:
                from backend.core.observability.tenant_context import (
                    reset_current_tenant, set_current_tenant,
                )
                from backend.core.store.graph_view import merged_graph_view_for_user
                gb = await merged_graph_view_for_user(user_id, use_networkx=None)
                tk = set_current_tenant(user_id)
                try:
                    for lbl in ("Scenario", "Prediction"):
                        for n in await gb.find_nodes_by_label(
                                lbl, limit=40, tenant_id=user_id, strict_tenant=False):
                            nm = (n.get("name") or "")[:120]
                            if nm:
                                buckets["prediction"].append({
                                    "title": f"Прогноз: {nm}",
                                    "description": (n.get("description") or "")[:280],
                                    "priority": "medium"})
                finally:
                    reset_current_tenant(tk)
                    await gb.close(save=False)
            except Exception:
                logger.debug("focus: predictions unavailable", exc_info=True)

            order = ["warning", "risk", "client", "prediction", "insight"]
            day = datetime.now(timezone.utc).toordinal()
            picked = None
            for shift in range(len(order)):
                bucket = order[(day + shift) % len(order)]
                cands = buckets.get(bucket) or []
                if cands:
                    cands.sort(key=lambda c: 0 if c.get("priority") == "high" else 1)
                    picked = {"type": bucket, **cands[0],
                              "date": datetime.now(timezone.utc).isoformat()}
                    break
            if picked:
                fdir = Path("data") / "focus"
                fdir.mkdir(parents=True, exist_ok=True)
                (fdir / f"{user_id}.json").write_text(
                    _json.dumps(picked, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                stats["picked"] = True
                logger.info(f"  🎯 Daily focus [{picked['type']}]: {picked['title'][:70]}")
        except Exception as e:
            logger.error(f"  Error picking daily focus: {e}")
        return stats

    async def _deja_vu_scan(self, user_id: str) -> Dict[str, Any]:
        """«У вас опять ситуация, похожая на X»: свежие встречи (последние
        3 дня, кап 5) сверяются с уроками прошлого — Contradiction (что
        конфликтовало), Pattern (что повторялось), Principle (что выведено).
        LLM судит совпадение; уверенное → предупреждение в ленту инсайтов
        с рекомендацией. Идемпотентно: deja_vu_checked на Meeting-узле."""
        stats = {"warnings": 0}
        try:
            import json as _json
            from datetime import timedelta

            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            from backend.core.store.graph_view import merged_graph_view_for_user
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)
            _tok = set_current_tenant(user_id)
            try:
                from backend.core.llm import get_llm_router
                llm = get_llm_router()
                if llm is None:
                    return stats

                cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
                meetings = await graph.find_nodes_by_label(
                    "Meeting", limit=2000, tenant_id=user_id, strict_tenant=False)
                recent = [m for m in meetings
                          if str(m.get("date") or m.get("created_at") or "") >= cutoff
                          and m.get("id") and not m.get("deja_vu_checked")][:5]
                if not recent:
                    return stats

                # Уроки прошлого (кап 30 строк)
                lessons: list = []
                for label, prefix in (("Contradiction", "Конфликт"),
                                      ("Pattern", "Паттерн"),
                                      ("Principle", "Принцип")):
                    try:
                        for n in (await graph.find_nodes_by_label(
                                label, limit=60, tenant_id=user_id,
                                strict_tenant=False)):
                            txt = (n.get("name") or n.get("statement")
                                   or n.get("description") or "").strip()
                            if txt and len(txt) > 15:
                                line = f"[{prefix}] {txt[:150]}"
                                if n.get("applicable_when"):
                                    line += f" (применимо: {n['applicable_when'][:80]})"
                                lessons.append(line)
                    except Exception:
                        continue
                lessons = lessons[:30]
                if not lessons:
                    return stats

                warnings: list = []
                for m in recent:
                    # содержание встречи: заголовок + связанные решения/задачи
                    parts = [f"Встреча: {m.get('title') or m.get('name') or ''}"]
                    try:
                        rels = await graph.get_node_relationships(m["id"])
                        cnt = 0
                        for e in (rels.get("outgoing") or []):
                            other = e.get("target")
                            if not other or cnt >= 8:
                                continue
                            n = await graph.get_node_by_id(other)
                            if n and (n.get("_label") or "") in ("Decision", "Task", "Topic"):
                                t = (n.get("text") or n.get("name") or "")[:110]
                                if t:
                                    parts.append(f"- {t}")
                                    cnt += 1
                    except Exception:
                        logger.debug("deja vu meeting content failed", exc_info=True)
                    try:
                        _prompt = (
                                "Сравни СВЕЖУЮ встречу с УРОКАМИ прошлого компании. "
                                "Если ситуация ЯВНО повторяет прошлый проблемный "
                                "паттерн/конфликт — верни предупреждение. Не "
                                "натягивай: сомневаешься → similar=false.\n\n"
                                "СВЕЖАЯ ВСТРЕЧА:\n" + "\n".join(parts)
                                + "\n\nУРОКИ ПРОШЛОГО:\n" + "\n".join(lessons)
                                + '\n\nСТРОГО JSON: {"similar": true|false, '
                                  '"lesson": "какой урок повторяется", '
                                  '"warning": "1-2 предложения: что было в тот раз", '
                                  '"recommendation": "как решить иначе"}')
                        text = await _nightly_generate(user_id, llm, _prompt,
                                                       temperature=0.0, max_tokens=250)
                        data = _json.loads(text[text.find("{"):text.rfind("}") + 1])
                        if isinstance(data, dict) and data.get("similar"):
                            warnings.append({
                                "insight_type": "warning",
                                "title": f"⚠️ Похожая ситуация: {str(data.get('lesson') or '')[:80]}",
                                "description": (
                                    f"Встреча «{(m.get('title') or '')[:60]}»: "
                                    f"{data.get('warning', '')} "
                                    f"Рекомендация: {data.get('recommendation', '')}"),
                                "priority": "high", "confidence": 0.7,
                                "entities": [], "actions":
                                    [str(data.get("recommendation") or "")[:150]],
                            })
                    except Exception:
                        logger.debug("deja vu judge failed", exc_info=True)
                    try:
                        await graph.update_node(m["id"], {"deja_vu_checked": True})
                    except Exception:
                        logger.debug("deja vu mark failed", exc_info=True)

                if warnings:
                    for w in warnings:
                        w["insight_id"] = ""  # проставит stable_insight_id
                    _persist_insights_to_store(
                        user_id,
                        [type("I", (), {
                            "insight_type": type("T", (), {"value": w["insight_type"]})(),
                            "title": w["title"], "description": w["description"],
                            "priority": type("P", (), {"value": w["priority"]})(),
                            "confidence": w["confidence"],
                            "entities": w["entities"], "actions": w["actions"],
                        })() for w in warnings],
                        source="deja_vu")
                    stats["warnings"] = len(warnings)
                    logger.info(f"  ⚠️ Deja vu: {stats['warnings']} предупреждений")
            finally:
                reset_current_tenant(_tok)
                await graph.close(save=False)
        except Exception as e:
            logger.error(f"  Error in deja vu scan: {e}")
        return stats

    def _client_revenue_map(self, user_id: str) -> Dict[str, float]:
        """Деньги клиентов из онтологии цифр: датасеты с колонкой-именем
        (клиент/контрагент/customer/компания) и денежной колонкой →
        {нормализованное имя: суммарная выручка}. Для карточек клиентов."""
        out: Dict[str, float] = {}
        try:
            from backend.core.ontology.dataset_registry import to_number
            from backend.core.ontology.dataset_service import registry_for_user
            _NAME_COLS = ("клиент", "контрагент", "customer", "client",
                          "компания", "company", "заказчик", "название")
            reg = registry_for_user(user_id)
            for ds in (reg.list() or [])[:10]:
                cols = [str(c) for c in (ds.get("columns") or [])]
                low = {c: c.lower() for c in cols}
                name_col = next((c for c in cols if any(
                    k in low[c] for k in _NAME_COLS)), None)
                money_col = next((c for c in cols if any(
                    k in low[c] for k in self._MONEY_COLS)), None)
                if not name_col or not money_col:
                    continue
                for r in reg.load_rows(ds.get("id") or ""):
                    nm = str(r.get(name_col) or "").strip().lower()
                    v = to_number(r.get(money_col))
                    if nm and v is not None:
                        out[nm] = out.get(nm, 0.0) + v
        except Exception:
            logger.debug("client revenue map failed", exc_info=True)
        return out

    async def _update_client_snapshots(self, user_id: str) -> Dict[str, Any]:
        """Карточки клиентов (B2B): Entity-организации клиентского типа →
        LLM-вердикт (статус/договорённости/риски/следующий шаг) по фактам
        из графа. Пишутся в snapshots_by_user/<uid>/clients/<id>.json.
        B2C-guard: карточки только по топ-20 самых связных клиентов."""
        stats = {"cards": 0, "skipped_b2c": 0}
        try:
            import json as _json

            from backend.core.observability.tenant_context import (
                reset_current_tenant, set_current_tenant,
            )
            from backend.core.store.graph_view import merged_graph_view_for_user
            from backend.core.store.tenant_paths import snapshots_dir_for_user
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)
            _tok = set_current_tenant(user_id)
            try:
                ents = await graph.find_nodes_by_label(
                    "Entity", limit=2000, tenant_id=user_id, strict_tenant=True)
                _CLIENT_TYPES = {"client", "customer", "company", "organization",
                                 "corporation", "клиент", "заказчик"}
                clients = [e for e in ents
                           if (e.get("entity_type") or "").lower() in _CLIENT_TYPES
                           and (e.get("name") or "").strip()]
                if not clients:
                    return stats
                # активность = число связей; B2C — берём только верхушку
                scored = []
                for c in clients:
                    try:
                        rels = await graph.get_node_relationships(c["id"])
                        deg = len(rels.get("outgoing") or []) + len(rels.get("incoming") or [])
                    except Exception:
                        deg, rels = 0, {"outgoing": [], "incoming": []}
                    scored.append((deg, c, rels))
                scored.sort(key=lambda x: -x[0])
                stats["skipped_b2c"] = max(0, len(scored) - 20)
                from backend.core.llm import get_llm_router
                llm = get_llm_router()
                out_dir = snapshots_dir_for_user(user_id) / "clients"
                out_dir.mkdir(parents=True, exist_ok=True)
                # деньги из онтологии цифр: имя клиента → суммарная выручка
                revenue_map = self._client_revenue_map(user_id)
                for deg, c, rels in scored[:20]:
                    if deg < 1:
                        continue
                    # факты: имена/типы соседей (встречи, решения, задачи, люди)
                    facts = [f"Клиент: {c.get('name')}",
                             f"Описание: {(c.get('description') or '')[:200]}"]
                    for e in ((rels.get("outgoing") or []) + (rels.get("incoming") or []))[:30]:
                        other = e.get("target") or e.get("source")
                        if not other or other == c["id"]:
                            continue
                        n = await graph.get_node_by_id(other)
                        if not n:
                            continue
                        lbl = n.get("_label", "")
                        nm = (n.get("name") or n.get("title") or n.get("text") or "")[:110]
                        if lbl in ("Meeting", "Decision", "Task", "Person", "Project") and nm:
                            facts.append(f"{lbl}: {nm}")
                    card = {"client_id": c["id"], "name": c.get("name"),
                            "degree": deg,
                            "updated": datetime.now(timezone.utc).isoformat()}
                    # выручка: точное имя или вхождение («ООО Ромашка» ⊃ «ромашка»)
                    _cn = str(c.get("name") or "").strip().lower()
                    _rev = revenue_map.get(_cn)
                    if _rev is None and _cn:
                        for k, v in revenue_map.items():
                            if _cn in k or k in _cn:
                                _rev = v
                                break
                    if _rev is not None:
                        card["revenue"] = round(_rev, 2)
                        facts.append(f"Выручка по данным таблиц: {_rev:,.0f}")
                    if llm is not None and len(facts) > 2:
                        try:
                            prompt = (
                                f"По фактам о клиенте «{c.get('name')}» верни СТРОГО JSON:\n"
                                '{"status": "активный|переговоры|риск|неактивный", '
                                '"summary": "2-3 предложения о состоянии отношений", '
                                '"agreements": "последние договорённости, кратко", '
                                '"risks": "риски или пусто", '
                                '"next_step": "следующий шаг или пусто"}\n'
                                "Только по фактам, без выдумок.\n\nФакты:\n"
                                + "\n".join(facts[:25]))
                            resp = await llm.generate(prompt=prompt, temperature=0.1,
                                                      max_tokens=400)
                            text = resp.get("text", "") if isinstance(resp, dict) else str(resp)
                            j = text[text.find("{"):text.rfind("}") + 1]
                            card.update(_json.loads(j))
                        except Exception:
                            logger.debug("client card LLM skipped", exc_info=True)
                    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                                      for ch in str(c["id"]))[:80]
                    (out_dir / f"{safe_id}.json").write_text(
                        _json.dumps(card, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    stats["cards"] += 1
            finally:
                reset_current_tenant(_tok)
                await graph.close(save=False)
            if stats["cards"]:
                logger.info(f"  💼 Client cards: {stats['cards']} "
                            f"(+{stats['skipped_b2c']} за пределами топ-20)")
        except Exception as e:
            logger.error(f"  Error updating client snapshots: {e}")
        return stats

    def _save_dedup_review(self, user_id: str, clusters: list) -> None:
        """Сохранить кластеры-кандидаты средней уверенности на ревью
        пользователю (последний этап дедупа — человек подтверждает слияние).
        Пишем per-user JSON; API/UI читает и предлагает слить/отклонить."""
        try:
            import json as _json
            review_dir = self.users_path.parent / "dedup_review"
            review_dir.mkdir(parents=True, exist_ok=True)
            path = review_dir / f"{user_id}.json"
            path.write_text(
                _json.dumps(
                    {"user_id": user_id, "clusters": clusters},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("save dedup review failed", exc_info=True)

    async def _consolidate_agent_memory(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """Консолидировать память агентов."""
        stats = {"episodic_processed": 0, "semantic_created": 0}

        try:
            from backend.core.config import flags
            if not flags.enable_agent_memory:
                return stats

            from backend.core.memory import get_agent_memory_manager

            # TODO: Add per-user memory support in tenant_paths
            # For now, assuming global memory or trying to find user-specific path
            memory_path = self.users_path.parent / "agent_memories"
            if not memory_path.exists():
                return stats

            manager = get_agent_memory_manager(str(memory_path))

            # Консолидируем для всех агентов
            all_stats = manager.get_stats()
            for agent_id in all_stats.get("by_agent", {}).keys():
                result = await manager.consolidate(
                    agent_id=agent_id,
                    min_access_count=3,
                    max_age_days=7
                )
                stats["episodic_processed"] += result.get("episodic_processed", 0)
                stats["semantic_created"] += result.get("semantic_created", 0)

            # Применяем decay
            await manager.apply_decay(decay_rate=0.01)

            logger.info(f"  Memory consolidation: {stats['semantic_created']} semantic created")

        except ImportError:
            logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.error(f"  Error consolidating memory: {e}")

        return stats

    async def _consolidate_memories(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """
        Dream-inspired Memory Consolidation.

        Включает:
        - Саммаризация (сжатие) памяти
        - DBSCAN кластеризация
        - Архивация старых данных
        - Creative Association Discovery
        """
        stats = {"summaries_created": 0, "clusters_created": 0, "memories_archived": 0}

        try:
            from backend.core.config import flags
            if not flags.enable_detailed_metrics:
                return stats

            from backend.core.memory import get_memory_consolidator
            from backend.core.store.graph_builder import GraphBuilder

            graph_path = Path(graph_path_for_user(user_id))
            if not graph_path.exists() and not getattr(self, "_neo4j_active", False):
                return stats

            graph = GraphBuilder(
                use_networkx=None,  # Auto-detect backend
                graph_storage_path=str(graph_path)
            )
            await graph.connect()

            consolidator = get_memory_consolidator(graph)
            result = await consolidator.consolidate(
                compress_memories=True,
                cluster_memories=True,
                archive_old=True,
                discover_associations=True
            )

            stats["summaries_created"] = result.get("summaries_created", 0)
            stats["clusters_created"] = result.get("clusters_created", 0)
            stats["memories_archived"] = result.get("memories_archived", 0)

            logger.info(
                f"  Memory consolidation (dream-inspired): "
                f"{stats['summaries_created']} summaries, "
                f"{stats['clusters_created']} clusters, "
                f"{stats['memories_archived']} archived"
            )

        except ImportError:
            logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.error(f"  Error in dream-inspired consolidation: {e}")

        return stats

    async def _update_snapshots(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """Обновить снапшоты."""
        stats = {"updated": 0}

        try:
            from backend.core.config import flags
            if not flags.enable_enhanced_snapshots:
                return stats

            from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
            from backend.core.store.graph_view import merged_graph_view_for_user

            # ВАЖНО: берём тот же граф пользователя, что и рабочий team360
            # (merged_graph_view_for_user). Раньше строился GraphBuilder по
            # per-user файловому пути с auto-detect — в нём людей/проектов не было
            # (persons=0 в логе), хотя team360 находит 40. Плюс прокидываем
            # user_id в генератор (иначе tenant-scoped выборка людей = 0).
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)

            # Per-user снапшоты: раньше писали в ОБЩУЮ data/snapshots, и при
            # консолидации нескольких юзеров последний (напр. Tessbrain) затирал
            # снапшоты предыдущих (КПД). Изолируем по тенанту.
            from backend.core.store.tenant_paths import snapshots_dir_for_user
            snapshot_path = snapshots_dir_for_user(user_id)

            generator = get_enhanced_snapshot_generator(graph, user_id=user_id)
            generator.user_id = user_id
            # set_storage_path перечитывает per-user оверрайды: worker-процесс
            # не видел правку CEO из API-процесса и ночной реген её перетирал.
            generator.set_storage_path(snapshot_path)

            # Обновляем ВСЕ снапшоты (company + persons + projects + products + departments + teams)
            try:
                all_stats = await generator.generate_all_snapshots()
                if all_stats:
                    stats["updated"] = sum(all_stats.values())
                    stats["details"] = all_stats

                # Генерируем еженедельный отчёт (по понедельникам) или ежедневный
                from datetime import datetime as dt
                if dt.now().weekday() == 0:  # Понедельник
                    try:
                        report = await generator.generate_periodic_report("weekly")
                        if report:
                            stats["weekly_report"] = True
                            logger.info("  📊 Weekly report generated")
                    except Exception as e:
                        logger.warning(f"  Weekly report failed: {e}")
            finally:
                try:
                    await graph.close(save=False)
                except Exception:
                    logger.debug("graph close failed", exc_info=True)

            logger.info(f"  Snapshots: {stats['updated']} updated ({all_stats})")

        except ImportError:
            logger.debug("suppressed exception", exc_info=True)
        except Exception as e:
            logger.error(f"  Error updating snapshots: {e}")

        return stats

    async def _run_retention_cleanup(self) -> Dict[str, Any]:
        """W27: вычистить устаревшие validation_results / executor handles."""
        try:
            from backend.config import get_settings
            from backend.core.retention.cleanup import run_retention_cycle
        except Exception as e:
            logger.debug(f"Retention helpers unavailable: {e}")
            return {"skipped": True, "reason": "import_failed"}

        s = get_settings()
        if not getattr(s, "retention_enabled", False):
            return {"skipped": True, "reason": "disabled"}

        postgres = None
        redis = None
        try:
            from backend.db.postgres import get_postgres
            postgres = await get_postgres()
        except Exception:
            postgres = None
        try:
            from backend.core.executors.store import _get_redis
            redis = await _get_redis()
        except Exception:
            redis = None

        result = await run_retention_cycle(
            postgres=postgres,
            redis=redis,
            validation_retention_days=getattr(s, "validation_retention_days", 90),
            executor_retention_days=getattr(s, "executor_retention_days", 30),
            enabled=True,
        )
        out = result.to_dict()
        logger.info(
            f"  Retention: {out['validation_deleted']} validation rows, "
            f"{out['executor_handles_purged']} handles purged"
        )
        return out

    def _save_log(self):
        """Сохранить лог консолидации."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = self.log_path / f"consolidation_{timestamp}.json"

        try:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
            logger.info(f"Log saved: {log_file}")
        except Exception as e:
            logger.error(f"Failed to save log: {e}")


async def run_nightly_consolidation(
    force: bool = False, only_user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Запустить ночную консолидацию. force=True — мимо распределённого лока
    (ручной запуск на одиночной машине). only_user_id — обработать только
    одного юзера (кнопка «Запустить консолидацию» для текущего аккаунта)."""
    service = NightlyConsolidationService()
    return await service.run(force=force, only_user_id=only_user_id)


# ============================================================================
# CLI
# ============================================================================

def main():
    """Entry point для CLI."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Tessbrain - Nightly Consolidation Service"
    )
    parser.add_argument(
        "--users-path",
        type=str,
        default="data/users",
        help="Путь к данным пользователей"
    )
    parser.add_argument(
        "--log-path",
        type=str,
        default="data/logs/nightly",
        help="Путь для логов"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Подробный вывод"
    )

    args = parser.parse_args()

    # Настраиваем логирование
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    # Запускаем
    service = NightlyConsolidationService(
        users_data_path=args.users_path,
        log_path=args.log_path
    )

    stats = asyncio.run(service.run())

    # Выводим результат
    logger.info("\n" + "="*60)
    logger.info("NIGHTLY CONSOLIDATION RESULTS")
    logger.info("="*60)
    logger.info(f"Started: {stats['started_at']}")
    logger.info(f"Completed: {stats['completed_at']}")
    logger.info(f"Users processed: {stats['users_processed']}")
    logger.info(f"Corrections applied: {stats['corrections_applied']}")
    logger.info(f"Edges decayed: {stats['edges_decayed']}")
    logger.info(f"Memories consolidated: {stats['memories_consolidated']}")
    logger.info(f"Snapshots updated: {stats['snapshots_updated']}")
    logger.info(f"Profiles curated: {stats.get('profiles_curated', 0)} "
                f"({stats.get('profile_updates_total', 0)} field updates)")
    logger.info(f"Errors: {len(stats['errors'])}")

    if stats['errors']:
        logger.info("\nErrors:")
        for err in stats['errors']:
            logger.info(f"  - {err}")


if __name__ == "__main__":
    main()

