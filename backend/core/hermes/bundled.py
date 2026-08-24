# -*- coding: utf-8 -*-
"""Hermes bundled skills (P12i-E) — стартовые навыки на «свежей установке».

Как у Hermes: на первом использовании в пустой стор копируются
bundled-скиллы из репозитория; `.bundled_manifest` фиксирует, что
сидирование уже было — повторно НЕ перезаписываем (особенно
пользовательские правки одноимённых навыков).

Дизайн (как весь P12): чистый stdlib, инъектируемые пути, never-raises,
идемпотентно. Без bundled-сидирования агент стартует «без памяти
навыков» (cold start) — это и есть пробел E из сверки с Hermes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .skill_store import SkillStore, parse

logger = logging.getLogger(__name__)

_MANIFEST = ".bundled_manifest"


def _default_bundled_dir() -> Path:
    return Path(__file__).parent / "bundled_skills"


def seed_bundled_skills(
    store: SkillStore,
    user_id: str,
    *,
    bundled_dir: Optional[str] = None,
) -> int:
    """Идемпотентно засеять bundled-скиллы в стор пользователя.

    Возвращает число добавленных. Никогда не raises.
    - Сидирует ТОЛЬКО один раз: маркер-файл `{root}/{user}/.bundled_
      manifest`. Повторный вызов → 0 (правки пользователя не трогаем).
    - Не перезаписывает уже существующий одноимённый навык.
    """
    try:
        bdir = Path(bundled_dir) if bundled_dir else _default_bundled_dir()
        if not bdir.exists():
            return 0
        # маркер per-user (рядом со скиллами пользователя)
        user_root = store._user_dir(user_id)  # noqa: SLF001 (внутр. контракт)
        marker = user_root / _MANIFEST
        try:
            if marker.exists():
                return 0
        except Exception:
            return 0

        added = 0
        for skill_md in sorted(bdir.glob("*/*/SKILL.md")):
            try:
                doc = parse(skill_md.read_text(encoding="utf-8"))
                if not doc.name:
                    continue
                if store.exists(user_id, doc.name):
                    continue  # не перетираем пользовательское
                if store.create(user_id, doc):
                    added += 1
            except Exception as exc:
                logger.debug("seed skill '%s' skipped: %s", skill_md, exc)
                continue

        # пишем маркер, даже если added==0 — чтобы не пытаться снова
        try:
            user_root.mkdir(parents=True, exist_ok=True)
            marker.write_text("bundled-seeded\n", encoding="utf-8")
        except Exception as exc:
            logger.debug("bundled manifest write skipped: %s", exc)
        return added
    except Exception as exc:
        logger.debug("seed_bundled_skills failed (safe): %s", exc)
        return 0


__all__ = ["seed_bundled_skills"]
