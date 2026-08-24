"""Generic k-anonymous aggregator (P2 §6 + п.7).

Копит вклады от distinct user'ов в (tenant, domain, aggregate_key), подавляет
любой раскрытие ниже k contributors, эмитит ровно один раз (emit-once guard).

Конвенции как в reactive/signal_queue: Postgres, apply_tenant=False, text()
SQL, best-effort never-raise.

ПРИВАТНОСТЬ (критично): contributor_user_id хранится только для distinct-count.
Ни одна функция этого модуля НЕ кладёт contributor_user_id в возвращаемый
AggregateView. Наружу идут лишь payload-вклады и счётчики.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_K = 5

DOMAIN_MANAGER_SIGNAL = "manager_signal"
DOMAIN_REVIEW_360 = "review_360"


@dataclass
class AggregateView:
    """Агрегированный взгляд на bucket. Без contributor-идентичностей."""

    domain: str
    aggregate_key: str
    distinct_count: int
    k: int
    suppressed: bool                      # True → ниже порога, payload'ы скрыты
    contributions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "aggregate_key": self.aggregate_key,
            "distinct_count": self.distinct_count,
            "k": self.k,
            "suppressed": self.suppressed,
            # contributions раскрываются ТОЛЬКО когда suppressed=False
            "contributions": [] if self.suppressed else self.contributions,
        }


def _norm_k(k: Optional[int]) -> int:
    """Порог анонимности. Поднять можно, опустить — нет.

    Раньше здесь стоял `max(1, int(k))`, а `k` приходил из query-параметра.
    То есть обещание «пока не набралось 5 разных оценщиков, не
    раскрывается ничего» снималось запросом `?k=1`: выдача возвращала
    индивидуальные вклады с дословными комментариями, начиная с первого
    оценщика. Анонимность, которую можно отключить параметром, — это не
    анонимность, а её изображение.

    DEFAULT_K — жёсткий пол. Клиент может попросить более строгий порог
    (это безопасное направление), но не более мягкий.
    """
    try:
        return max(DEFAULT_K, int(k)) if k is not None else DEFAULT_K
    except (TypeError, ValueError):
        return DEFAULT_K


async def contribute(
    *,
    tenant_id: str,
    domain: str,
    aggregate_key: str,
    contributor_user_id: str,
    payload: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """Записать/обновить вклад одного юзера. Upsert по
    (tenant, domain, aggregate_key, contributor) — повтор того же юзера НЕ
    увеличивает distinct. Возвращает текущий distinct_count или None при ошибке.
    """
    if not (tenant_id and domain and aggregate_key and contributor_user_id):
        return None
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            await session.execute(
                text("""
                    INSERT INTO public.aggregation_contributions
                        (id, tenant_id, domain, aggregate_key,
                         contributor_user_id, payload)
                    VALUES
                        (CAST(:id AS UUID), CAST(:tid AS UUID), :dom, :akey,
                         CAST(:cuid AS UUID), CAST(:pl AS JSONB))
                    ON CONFLICT (tenant_id, domain, aggregate_key,
                                 contributor_user_id)
                    DO UPDATE SET payload = EXCLUDED.payload,
                                  updated_at = now()
                """),
                {
                    "id": str(uuid.uuid4()),
                    "tid": tenant_id,
                    "dom": domain,
                    "akey": aggregate_key,
                    "cuid": contributor_user_id,
                    "pl": json.dumps(payload or {}, ensure_ascii=False, default=str),
                },
            )
        return await distinct_count(
            tenant_id=tenant_id, domain=domain, aggregate_key=aggregate_key,
        )
    except Exception as exc:
        logger.warning("aggregation.contribute failed: %s", exc)
        return None


async def distinct_count(
    *, tenant_id: str, domain: str, aggregate_key: str,
) -> int:
    """Число РАЗНЫХ контрибьюторов в bucket'е. Best-effort → 0."""
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            r = await session.execute(
                text("""
                    SELECT COUNT(DISTINCT contributor_user_id)
                    FROM public.aggregation_contributions
                    WHERE tenant_id = CAST(:tid AS UUID)
                      AND domain = :dom AND aggregate_key = :akey
                """),
                {"tid": tenant_id, "dom": domain, "akey": aggregate_key},
            )
            row = r.first()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception as exc:
        logger.warning("aggregation.distinct_count failed: %s", exc)
        return 0


async def aggregate_view(
    *,
    tenant_id: str,
    domain: str,
    aggregate_key: str,
    k: Optional[int] = None,
) -> AggregateView:
    """Собрать агрегат. Если distinct < k → suppressed=True, contributions
    скрыты (k-anonymity). Иначе раскрывает payload'ы БЕЗ contributor-id.
    Never-raise → при ошибке возвращает suppressed-view с count=0.
    """
    kk = _norm_k(k)
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            rows = await session.execute(
                text("""
                    SELECT payload
                    FROM public.aggregation_contributions
                    WHERE tenant_id = CAST(:tid AS UUID)
                      AND domain = :dom AND aggregate_key = :akey
                    ORDER BY created_at ASC
                """),
                {"tid": tenant_id, "dom": domain, "akey": aggregate_key},
            )
            payloads: list[dict[str, Any]] = []
            for r in rows:
                pl = r[0]
                if isinstance(pl, str):
                    try:
                        pl = json.loads(pl)
                    except Exception:
                        pl = {}
                payloads.append(pl or {})
        # distinct count считаем отдельно (вкладов == distinct из-за UNIQUE upsert,
        # но домен может позволять не-upsert в будущем — берём честный distinct).
        dcount = await distinct_count(
            tenant_id=tenant_id, domain=domain, aggregate_key=aggregate_key,
        )
        suppressed = dcount < kk
        return AggregateView(
            domain=domain,
            aggregate_key=aggregate_key,
            distinct_count=dcount,
            k=kk,
            suppressed=suppressed,
            contributions=[] if suppressed else payloads,
        )
    except Exception as exc:
        logger.warning("aggregation.aggregate_view failed: %s", exc)
        return AggregateView(
            domain=domain, aggregate_key=aggregate_key,
            distinct_count=0, k=kk, suppressed=True, contributions=[],
        )


async def try_emit_once(
    *,
    tenant_id: str,
    domain: str,
    aggregate_key: str,
    distinct_count: int,
    target_user_id: Optional[str] = None,
    emit_ref: Optional[str] = None,
) -> bool:
    """Атомарно «застолбить» эмит ключа. Возвращает True ТОЛЬКО первому
    вызову (INSERT ... ON CONFLICT DO NOTHING → rowcount). Защита от двойного
    эмита при гонке/повторных contribute. Never-raise → False при ошибке.
    """
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            res = await session.execute(
                text("""
                    INSERT INTO public.aggregation_emissions
                        (tenant_id, domain, aggregate_key, distinct_count,
                         target_user_id, emit_ref)
                    VALUES
                        (CAST(:tid AS UUID), :dom, :akey, :dc,
                         CAST(:tgt AS UUID), :ref)
                    ON CONFLICT (tenant_id, domain, aggregate_key) DO NOTHING
                """),
                {
                    "tid": tenant_id, "dom": domain, "akey": aggregate_key,
                    "dc": int(distinct_count), "tgt": target_user_id,
                    "ref": emit_ref,
                },
            )
        return (getattr(res, "rowcount", 0) or 0) > 0
    except Exception as exc:
        logger.warning("aggregation.try_emit_once failed: %s", exc)
        return False


async def release_emit(
    *, tenant_id: str, domain: str, aggregate_key: str,
) -> None:
    """Снять claim эмита (если последующая доставка провалилась), чтобы
    следующий contribute мог повторить. Best-effort, never-raise."""
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            await session.execute(
                text("""
                    DELETE FROM public.aggregation_emissions
                    WHERE tenant_id = CAST(:tid AS UUID)
                      AND domain = :dom AND aggregate_key = :akey
                """),
                {"tid": tenant_id, "dom": domain, "akey": aggregate_key},
            )
    except Exception as exc:
        logger.warning("aggregation.release_emit failed: %s", exc)
