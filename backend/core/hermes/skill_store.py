# -*- coding: utf-8 -*-
"""Hermes-track skill-store (P12b) — процедурная память агента.

ПРОБЕЛ (см. карту переноса Hermes): у нас `core/skills/` — это
параметризованные промпт-шаблоны, создаются вручную. У Hermes скилл —
это **процедурная память**: `SKILL.md` с секциями When to Use /
Procedure / **Pitfalls / Verification**, автосоздаётся из успешных
траекторий, переиспользуется через progressive disclosure
(skills_list → skill_view). Этот модуль = тот самый store.

Дизайн (как P0–P11): чистый (только stdlib+yaml, без тяжёлых
backend.core.* импортов → юнит-тест без графа/БД), never-raises,
инъектируемые root/clock, bounded, per-user изоляция.

Раскладка на диске (паттерн как у rule_book):
  {root}/{user_id}/{category}/{name}/SKILL.md

Орг-слой (перенос из QM: «промоушен навыка на организацию с одобрением
админа»):
  {root}/_org/{org_id}/{category}/{name}/SKILL.md            — одобренные
  {root}/_org/{org_id}/_pending/{category}/{name}/SKILL.md   — предложения
Одобренный навык несёт shared_by (автор), approved_by, approved_at.
Предложение — только shared_by. list(user_id) дополняется одобренными
навыками организации пользователя (личный навык побеждает орг-навык при
конфликте имён); организация резолвится инъектируемым org_resolver, а без
него — ленивым импортом membership (модуль остаётся чистым: stdlib+yaml).

Формат SKILL.md (совместим по духу с Hermes / agentskills.io):
  ---
  name: <slug>
  description: <одна строка>
  version: <int>
  category: <slug>
  tags: [a, b]
  requires: [terminal]        # требуемые toolset'ы (опц.)
  platforms: [linux]          # опц.
  ---
  ## When to Use
  ...
  ## Procedure
  ...
  ## Pitfalls
  ...
  ## Verification
  ...
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MAX_SKILLS_PER_USER = 200          # bounded — не пухнуть бесконечно
_ORG_DIR = "_org"                  # служебный корень орг-слоя
_PENDING_DIR = "_pending"          # предложения, ждущие одобрения админа
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_SECTIONS = ("When to Use", "Procedure", "Pitfalls", "Verification")


def _slug(value: str, fallback: str = "skill") -> str:
    s = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return s or fallback


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SkillDoc:
    """Полный скилл (Level 1)."""
    name: str
    description: str = ""
    version: int = 1
    category: str = "general"
    tags: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    when_to_use: str = ""
    procedure: str = ""
    pitfalls: str = ""
    verification: str = ""
    created_at: str = ""
    updated_at: str = ""
    # орг-слой: автор, кто одобрил и когда (пустые для личных навыков)
    shared_by: str = ""
    approved_by: str = ""
    approved_at: str = ""

    def meta(self) -> "SkillMeta":
        return SkillMeta(
            name=self.name, description=self.description,
            category=self.category, tags=list(self.tags),
            version=self.version,
        )


@dataclass
class SkillMeta:
    """Лёгкая карточка (Level 0 — progressive disclosure)."""
    name: str
    description: str
    category: str
    tags: list[str]
    version: int

    def to_dict(self) -> dict:
        return {
            "name": self.name, "description": self.description,
            "category": self.category, "tags": self.tags,
            "version": self.version,
        }


# === сериализация / парсинг (never-raises) ==============================

def serialize(doc: SkillDoc) -> str:
    """SkillDoc → текст SKILL.md. Не raises."""
    try:
        import yaml

        front = {
            "name": doc.name,
            "description": doc.description,
            "version": int(doc.version),
            "category": doc.category,
            "tags": list(doc.tags),
            "requires": list(doc.requires),
        }
        if doc.platforms:
            front["platforms"] = list(doc.platforms)
        if doc.created_at:
            front["created_at"] = doc.created_at
        if doc.updated_at:
            front["updated_at"] = doc.updated_at
        if doc.shared_by:
            front["shared_by"] = doc.shared_by
        if doc.approved_by:
            front["approved_by"] = doc.approved_by
        if doc.approved_at:
            front["approved_at"] = doc.approved_at
        fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    except Exception as exc:
        logger.debug("skill_store.serialize frontmatter failed: %s", exc)
        fm = f"name: {doc.name}\ndescription: {doc.description}"
    body = (
        f"## When to Use\n{doc.when_to_use}".rstrip()
        + f"\n\n## Procedure\n{doc.procedure}".rstrip()
        + f"\n\n## Pitfalls\n{doc.pitfalls}".rstrip()
        + f"\n\n## Verification\n{doc.verification}".rstrip()
    )
    return f"---\n{fm}\n---\n# {doc.name}\n\n{body}\n"


def _extract_section(body: str, title: str) -> str:
    """Текст секции '## <title>' до следующего '## '. Не raises."""
    try:
        m = re.search(
            rf"^##\s+{re.escape(title)}\s*\n(.*?)(?=^##\s+|\Z)",
            body, re.MULTILINE | re.DOTALL,
        )
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def parse(text: str) -> SkillDoc:
    """Текст SKILL.md → SkillDoc. Никогда не raises (best-effort)."""
    doc = SkillDoc(name="")
    if not isinstance(text, str) or not text.strip():
        return doc
    front_raw, body = "", text
    try:
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
        if m:
            front_raw, body = m.group(1), m.group(2)
    except Exception:
        pass
    if front_raw:
        try:
            import yaml

            fm = yaml.safe_load(front_raw) or {}
            if isinstance(fm, dict):
                doc.name = str(fm.get("name", "") or "")
                doc.description = str(fm.get("description", "") or "")
                try:
                    doc.version = int(fm.get("version", 1))
                except (TypeError, ValueError):
                    doc.version = 1
                doc.category = str(fm.get("category", "general") or "general")
                for k in ("tags", "requires", "platforms"):
                    v = fm.get(k)
                    setattr(doc, k, [str(x) for x in v]
                            if isinstance(v, list) else [])
                doc.created_at = str(fm.get("created_at", "") or "")
                doc.updated_at = str(fm.get("updated_at", "") or "")
                doc.shared_by = str(fm.get("shared_by", "") or "")
                doc.approved_by = str(fm.get("approved_by", "") or "")
                doc.approved_at = str(fm.get("approved_at", "") or "")
        except Exception as exc:
            logger.debug("skill_store.parse frontmatter failed: %s", exc)
    doc.when_to_use = _extract_section(body, "When to Use")
    doc.procedure = _extract_section(body, "Procedure")
    doc.pitfalls = _extract_section(body, "Pitfalls")
    doc.verification = _extract_section(body, "Verification")
    return doc


# === store ==============================================================

class SkillStore:
    """Файловый per-user store с progressive disclosure. Не raises."""

    def __init__(
        self,
        root: str = "data/agent_skills",
        *,
        clock: Optional[Callable[[], str]] = None,
        max_skills: int = MAX_SKILLS_PER_USER,
        org_resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self._root = Path(root)
        self._clock = clock or _now_iso
        self._max = max_skills
        # user_id → org_id|None. None → ленивый импорт membership в list()
        # (модуль остаётся чистым для юнит-тестов без backend.core.*).
        self._org_resolver = org_resolver

    def _user_dir(self, user_id: str) -> Path:
        s = _slug(user_id or "anon", "anon")
        # ведущее подчёркивание зарезервировано под служебные каталоги
        # (_org) — user_id не должен уметь притвориться орг-слоем
        if s.startswith("_"):
            s = "u" + s
        return self._root / s

    def _skill_path(self, user_id: str, category: str, name: str) -> Path:
        return (self._user_dir(user_id) / _slug(category, "general")
                / _slug(name) / "SKILL.md")

    def _find(self, user_id: str, name: str) -> Optional[Path]:
        """Найти SKILL.md по name в любой категории. Не raises."""
        slug = _slug(name)
        try:
            base = self._user_dir(user_id)
            if not base.exists():
                return None
            for p in base.glob(f"*/{slug}/SKILL.md"):
                return p
        except Exception as exc:
            logger.debug("skill_store._find failed: %s", exc)
        return None

    def list_personal(self, user_id: str) -> list[SkillMeta]:
        """Level 0: только ЛИЧНЫЕ метаданные, bounded. Не raises."""
        out: list[SkillMeta] = []
        try:
            base = self._user_dir(user_id)
            if not base.exists():
                return out
            for p in sorted(base.glob("*/*/SKILL.md")):
                if len(out) >= self._max:
                    break
                try:
                    out.append(parse(p.read_text(encoding="utf-8")).meta())
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("skill_store.list_personal failed: %s", exc)
        return out

    def list(self, user_id: str) -> list[SkillMeta]:
        """Level 0: личные + одобренные орг-навыки организации
        пользователя (best-effort; без орги — только личные). При
        конфликте имён личный навык побеждает орг-навык. Не raises."""
        out = self.list_personal(user_id)
        try:
            org_id = self._resolve_org(user_id)
            if org_id:
                seen = {m.name for m in out}
                for m in self.list_org(org_id):
                    if len(out) >= self._max:
                        break
                    if m.name in seen:
                        continue  # личный затеняет орг-навык
                    seen.add(m.name)
                    out.append(m)
        except Exception as exc:
            logger.debug("skill_store.list org merge failed: %s", exc)
        return out

    def _resolve_org(self, user_id: str) -> Optional[str]:
        """org_id пользователя или None. Никогда не raises: без
        резолвера — ленивый импорт membership, недоступен → None
        (личные навыки работают в любом окружении)."""
        if self._org_resolver is not None:
            try:
                return self._org_resolver(user_id)
            except Exception as exc:
                logger.debug("skill_store org_resolver failed: %s", exc)
                return None
        try:
            from backend.core.ingest.membership import get_org_for_user
        except ImportError:
            return None
        try:
            return get_org_for_user(user_id)
        except Exception as exc:
            logger.debug("skill_store get_org_for_user failed: %s", exc)
            return None

    def _view_personal(self, user_id: str, name: str) -> Optional[SkillDoc]:
        """Только личный слой (для create/propose — без орг-фолбэка)."""
        p = self._find(user_id, name)
        if p is None:
            return None
        try:
            return parse(p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("skill_store._view_personal failed: %s", exc)
            return None

    def view(self, user_id: str, name: str) -> Optional[SkillDoc]:
        """Level 1: полный скилл. Личный слой, при промахе — одобренный
        орг-навык организации пользователя (раз list() его показывает,
        view() обязан его открывать). None если нигде нет. Не raises."""
        if self._find(user_id, name) is not None:
            return self._view_personal(user_id, name)
        try:
            org_id = self._resolve_org(user_id)
            if org_id:
                return self.view_org(org_id, name)
        except Exception as exc:
            logger.debug("skill_store.view org fallback failed: %s", exc)
        return None

    def exists(self, user_id: str, name: str) -> bool:
        return self._find(user_id, name) is not None

    def create(self, user_id: str, doc: SkillDoc) -> bool:
        """Создать/перезаписать скилл. False при ошибке/превышении
        лимита. Не raises."""
        if not isinstance(doc, SkillDoc) or not _slug(doc.name) or \
                _slug(doc.name) == "skill" and not doc.name:
            return False
        try:
            existing = self._view_personal(user_id, doc.name)
            # лимит — по ЛИЧНЫМ навыкам (орг-добор не в счёт)
            if existing is None and len(self.list_personal(user_id)) >= self._max:
                logger.warning("skill_store: max skills reached for user")
                return False
            now = self._clock()
            # created_at сохраняется при перезаписи (как в Hermes)
            if existing is not None and existing.created_at:
                doc.created_at = existing.created_at
            elif not doc.created_at:
                doc.created_at = now
            doc.updated_at = now
            path = self._skill_path(user_id, doc.category, doc.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(serialize(doc), encoding="utf-8")
            return True
        except Exception as exc:
            logger.debug("skill_store.create failed: %s", exc)
            return False

    def patch(self, user_id: str, name: str, old: str, new: str) -> bool:
        """Точечная замена подстроки в теле скилла (как Hermes patch).
        Не raises."""
        p = self._find(user_id, name)
        if not p or not old:
            return False
        try:
            txt = p.read_text(encoding="utf-8")
            if old not in txt:
                return False
            updated = parse(txt.replace(old, new, 1))
            updated.version += 1
            updated.updated_at = self._clock()
            p.write_text(serialize(updated), encoding="utf-8")
            return True
        except Exception as exc:
            logger.debug("skill_store.patch failed: %s", exc)
            return False

    # === орг-слой (перенос из QM: промоушен навыка с одобрением) =======

    def _org_dir(self, org_id: str) -> Path:
        return self._root / _ORG_DIR / _slug(org_id or "org", "org")

    def _org_skill_path(self, org_id: str, category: str, name: str) -> Path:
        return (self._org_dir(org_id) / _slug(category, "general")
                / _slug(name) / "SKILL.md")

    def _pending_path(self, org_id: str, category: str, name: str) -> Path:
        return (self._org_dir(org_id) / _PENDING_DIR
                / _slug(category, "general") / _slug(name) / "SKILL.md")

    @staticmethod
    def _find_under(base: Path, name: str) -> Optional[Path]:
        """SKILL.md по name в любой категории под base (глубина 2,
        служебные каталоги пропускаются). Не raises."""
        slug = _slug(name)
        try:
            if not base.exists():
                return None
            for p in sorted(base.glob(f"*/{slug}/SKILL.md")):
                if any(part.startswith("_") for part in
                       p.relative_to(base).parts[:-1]):
                    continue
                return p
        except Exception as exc:
            logger.debug("skill_store._find_under failed: %s", exc)
        return None

    def propose_to_org(self, user_id: str, org_id: str, name: str) -> bool:
        """Предложить СВОЙ личный навык в организацию (в _pending, ждёт
        одобрения админа). Перезапись собственного предложения допустима;
        чужого — отказ (False). Не raises."""
        if not (user_id and org_id and name):
            return False
        doc = self._view_personal(user_id, name)
        if doc is None:
            return False  # своего ЛИЧНОГО навыка с таким именем нет
        try:
            pend_base = self._org_dir(org_id) / _PENDING_DIR
            existing = self._find_under(pend_base, name)
            if existing is not None:
                prev = parse(existing.read_text(encoding="utf-8"))
                if prev.shared_by and prev.shared_by != user_id:
                    return False  # чужое предложение не перетираем
                existing.unlink()  # своё — обновляем (категория могла смениться)
                try:
                    existing.parent.rmdir()
                except OSError:
                    pass
            doc.shared_by = user_id
            doc.approved_by = ""
            doc.approved_at = ""
            doc.updated_at = self._clock()
            path = self._pending_path(org_id, doc.category, doc.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(serialize(doc), encoding="utf-8")
            return True
        except Exception as exc:
            logger.debug("skill_store.propose_to_org failed: %s", exc)
            return False

    def list_org(self, org_id: str,
                 include_pending: bool = False) -> list[SkillMeta]:
        """Одобренные орг-навыки (метаданные, bounded); с
        include_pending — плюс предложения. Не raises."""
        out: list[SkillMeta] = []
        if not org_id:
            return out
        try:
            base = self._org_dir(org_id)
            if not base.exists():
                return out
            for p in sorted(base.glob("*/*/SKILL.md")):
                if len(out) >= self._max:
                    break
                if any(part.startswith("_") for part in
                       p.relative_to(base).parts[:-1]):
                    continue  # _pending и прочие служебные — не сюда
                try:
                    out.append(parse(p.read_text(encoding="utf-8")).meta())
                except Exception:
                    continue
            if include_pending:
                for d in self.list_pending(org_id):
                    if len(out) >= self._max:
                        break
                    out.append(d.meta())
        except Exception as exc:
            logger.debug("skill_store.list_org failed: %s", exc)
        return out

    def list_pending(self, org_id: str) -> list[SkillDoc]:
        """Предложения организации (ПОЛНЫЕ доки — нужен shared_by),
        bounded. Не raises."""
        out: list[SkillDoc] = []
        if not org_id:
            return out
        try:
            base = self._org_dir(org_id) / _PENDING_DIR
            if not base.exists():
                return out
            for p in sorted(base.glob("*/*/SKILL.md")):
                if len(out) >= self._max:
                    break
                try:
                    out.append(parse(p.read_text(encoding="utf-8")))
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("skill_store.list_pending failed: %s", exc)
        return out

    def view_org(self, org_id: str, name: str) -> Optional[SkillDoc]:
        """Одобренный орг-навык по имени. None если нет. Не raises."""
        if not org_id:
            return None
        p = self._find_under(self._org_dir(org_id), name)
        if not p:
            return None
        try:
            return parse(p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("skill_store.view_org failed: %s", exc)
            return None

    def approve(self, org_id: str, category: str, name: str,
                approver_id: str) -> bool:
        """Одобрить предложение: перенести из _pending в орг-слой,
        проставив approved_by/approved_at (проверка, что approver —
        админ, — на вызывающей стороне). Не raises."""
        if not (org_id and name and approver_id):
            return False
        try:
            src = self._pending_path(org_id, category, name)
            if not src.exists():
                return False
            doc = parse(src.read_text(encoding="utf-8"))
            doc.approved_by = approver_id
            doc.approved_at = self._clock()
            doc.updated_at = doc.approved_at
            dst = self._org_skill_path(org_id, doc.category or category,
                                       doc.name or name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(serialize(doc), encoding="utf-8")
            src.unlink()
            try:
                src.parent.rmdir()
            except OSError:
                pass
            return True
        except Exception as exc:
            logger.debug("skill_store.approve failed: %s", exc)
            return False

    def reject(self, org_id: str, category: str, name: str) -> bool:
        """Отклонить предложение: удалить из _pending. Не raises."""
        if not (org_id and name):
            return False
        try:
            src = self._pending_path(org_id, category, name)
            if not src.exists():
                return False
            src.unlink()
            try:
                src.parent.rmdir()
            except OSError:
                pass
            return True
        except Exception as exc:
            logger.debug("skill_store.reject failed: %s", exc)
            return False

    def delete(self, user_id: str, name: str) -> bool:
        """Удалить скилл (файл). Не raises."""
        p = self._find(user_id, name)
        if not p:
            return False
        try:
            p.unlink()
            # подчистить пустую папку скилла
            try:
                p.parent.rmdir()
            except OSError:
                pass
            return True
        except Exception as exc:
            logger.debug("skill_store.delete failed: %s", exc)
            return False


__all__ = [
    "SkillDoc",
    "SkillMeta",
    "SkillStore",
    "serialize",
    "parse",
    "MAX_SKILLS_PER_USER",
]
