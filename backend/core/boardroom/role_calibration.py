# -*- coding: utf-8 -*-
"""Калибровка роли директора под КОНКРЕТНУЮ компанию.

Строки «Отрасль: ремонт квартир; Стадия: стартап» мало: у CMO ремонтной
компании и CMO SaaS-стартапа разные заботы, применимые практики и типичные
ошибки. Калибровка — это развёрнутый бриф, который сильная модель ОДИН РАЗ
строит из профиля компании (снапшот целиком + доменная картина функции):

  1. о чём этот директор реально беспокоится именно здесь (из данных);
  2. какие практики роли здесь применимы;
  3. что здесь НЕ работает или вредно — типичные ошибки переноса практик из
     чужого масштаба/отрасли;
  4. отраслевые тонкости (регуляторика, сезонность, каналы) — если из данных
     компании это не следует, помечается как [отраслевое допущение].

Кэш: data/boardroom_calibration/<user_id>/<role>.json. Инвалидация — по
отпечатку профиля компании (профиль изменился → бриф пересобирается) и по
возрасту (14 дней). Пустой профиль компании → пустая калибровка: роль
работает в общем виде, а не на выдуманной специфике. Never-raise.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_TTL_SECONDS = 14 * 86400


def _dir() -> Path:
    return Path(os.environ.get("BOARDROOM_CALIBRATION_DIR", "").strip()
                or "data/boardroom_calibration")


def _path(user_id: str, role_id: str) -> Path:
    safe = "".join(c for c in role_id if c.isalnum() or c in "-_")
    return _dir() / user_id / f"{safe}.json"


def _fingerprint(company_text: str) -> str:
    return hashlib.sha1(company_text.encode("utf-8")).hexdigest()[:16]


async def get_role_calibration(user_id: str, role_id: str, role_name: str,
                               domain: Optional[str] = None) -> str:
    """Бриф «эта роль в этой компании». Кэшируется; сбой → ""."""
    try:
        from backend.core.reports.report_context import _company_snapshot_text
        company = await _company_snapshot_text(user_id)
    except Exception:
        company = ""
    if not (company or "").strip():
        return ""      # без профиля компании калибровка была бы выдумкой

    fp = _fingerprint(company)
    p = _path(user_id, role_id)
    try:
        if p.exists():
            cached = json.loads(p.read_text(encoding="utf-8"))
            fresh = (cached.get("fingerprint") == fp
                     and time.time() - float(cached.get("created_at") or 0)
                     < _TTL_SECONDS)
            if fresh and str(cached.get("text") or "").strip():
                return cached["text"]
    except Exception:
        logger.debug("calibration cache read skipped", exc_info=True)

    domain_text = ""
    if domain:
        try:
            from backend.core.sleep.domain_snapshots import read_domain_snapshot
            domain_text = read_domain_snapshot(user_id, domain, max_chars=8000)
        except Exception:
            pass

    prompt = (
        f"Ты настраиваешь роль «{role_name}» под конкретную компанию для "
        "виртуального совета директоров. Ниже — реальный профиль компании"
        + (" и накопленная картина этой функции" if domain_text else "") +
        ". Составь КАЛИБРОВКУ РОЛИ — плотный бриф без воды:\n\n"
        "## О чём ты реально беспокоишься в ЭТОЙ компании\n"
        "5-8 конкретных пунктов ИЗ ДАННЫХ: живые проблемы, цели, риски, "
        "узкие места, относящиеся к твоей функции.\n\n"
        "## Что здесь применимо из практик твоей роли\n"
        "С учётом отрасли, стадии и размера: какие подходы уместны именно "
        "при таком масштабе и типе бизнеса.\n\n"
        "## Что здесь НЕ работает или вредно\n"
        "Типичные ошибки переноса практик из чужого масштаба/отрасли: что "
        "выглядит правильным «по учебнику», но для этой компании вредно "
        "(например, процессы корпорации в стартапе, бренд-кампании там, где "
        "решает локальная репутация).\n\n"
        "## Отраслевые тонкости\n"
        "Регуляторика, сезонность, каналы, специфика клиентов. Что не "
        "следует из данных компании, а известно про отрасль в целом — "
        "помечай [отраслевое допущение].\n\n"
        "ПРАВИЛА: опирайся на данные компании; не выдумывай факты о ней; "
        "пиши во втором лице («ты беспокоишься о…»), кратко и предметно.\n\n"
        f"ПРОФИЛЬ КОМПАНИИ:\n{company}\n"
        + (f"\nКАРТИНА ФУНКЦИИ:\n{domain_text}\n" if domain_text else ""))

    try:
        from backend.core.llm.workload_policy import generate_for_workload
        text = (await generate_for_workload(
            user_id, "search_deep_synthesis", prompt) or "").strip()
    except Exception as e:
        logger.warning("calibration generation failed for %s: %s", role_id, e)
        return ""
    if not text:
        return ""

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"text": text, "fingerprint": fp, "created_at": time.time(),
             "role": role_id}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        logger.debug("calibration cache write skipped", exc_info=True)
    logger.info("🎯 Калибровка роли %s пересобрана (%d симв)", role_id, len(text))
    return text
