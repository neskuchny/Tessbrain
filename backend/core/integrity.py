# -*- coding: utf-8 -*-
"""Проверка целостности ядра Tessbrain (анти-тампер, слой поверх компиляции).

Зачем: сделать «вскрыть и переписать под себя» дороже. Любая правка
crown-jewels (вырезать проверку лицензии, подменить промпты, снять
watermark) меняет sha256 файла → подпись манифеста не сходится → в
enterprise-образе (`TESSENT_INTEGRITY_REQUIRED=on`) приложение НЕ стартует.

Чтобы обойти, вору нужно ЛИБО подделать подпись манифеста (нужен наш
приватный ключ — его нет в поставке), ЛИБО пропатчить сам этот модуль —
а он в enterprise-образе скомпилирован в .so (crown_jewels.txt →
compile_crown_jewels.sh), т.е. не текстовый .py. Два замка вместо нуля.

ЧЕСТНО: на чужом root с достаточной решимостью снимается всё (как у
любого софта). Это ТРЕНИЕ + tamper-evidence (watermark deployment_id в
логах остаётся), а не абсолютная стена. Основная защита — гибрид+договор
(docs/ENTERPRISE_PORTABILITY.md).

Манифест: `deploy/onprem/integrity_manifest.json`
{version, files:{rel_path: sha256}} + отделённая Ed25519-подпись (тот же
ключ, что лицензия). Собирается на нашей сборке (scripts/
make_integrity_manifest.py), signature кладётся в env/файл образа.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "deploy" / "onprem" / "integrity_manifest.json"


def integrity_required() -> bool:
    return os.getenv("TESSENT_INTEGRITY_REQUIRED", "").strip().lower() in (
        "on", "1", "true", "yes")


def _sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _module_path(rel: str) -> Optional[Path]:
    """Разрешить путь модуля: сперва .py, иначе — скомпилированный .so
    (enterprise-образ) — целостность .so проверяем так же по хэшу."""
    p = _REPO_ROOT / rel
    if p.is_file():
        return p
    # crown-jewel скомпилирован: backend/…/x.py → backend/…/x.*.so
    stem = p.with_suffix("")
    matches = list(stem.parent.glob(stem.name + ".*.so"))
    return matches[0] if matches else None


def compute_hashes(files: list[str]) -> dict[str, str]:
    """sha256 по списку rel-путей (для генератора манифеста и проверки)."""
    out: dict[str, str] = {}
    for rel in files:
        mp = _module_path(rel)
        if mp is None:
            continue
        digest = _sha256_file(mp)
        if digest:
            out[rel] = digest
    return out


def _verify_manifest_signature(manifest_bytes: bytes, signature: bytes) -> bool:
    # Тот же Ed25519-ключ, что у лицензии (один корень доверия).
    try:
        from backend.core.licensing import _verify_signature
        return _verify_signature(manifest_bytes, signature)
    except Exception:
        return False


def verify_integrity(manifest_path: Optional[Path] = None,
                     signature_b64: Optional[str] = None) -> dict[str, Any]:
    """Сверить хэши crown-jewels с подписанным манифестом.
    Возвращает {ok, reason, tampered:[...], missing:[...], checked}.
    Never-raise."""
    manifest_path = manifest_path or _MANIFEST_PATH
    signature_b64 = (signature_b64 if signature_b64 is not None
                     else os.getenv("TESSENT_INTEGRITY_SIG", ""))
    out: dict[str, Any] = {"ok": False, "reason": None,
                           "tampered": [], "missing": [], "checked": 0}
    try:
        manifest_bytes = Path(manifest_path).read_bytes()
    except Exception:
        out["reason"] = "манифест целостности не найден"
        return out

    # Подпись обязательна: иначе вор просто перегенерил бы манифест под свои
    # правки. Без подписи — фейл.
    if not signature_b64.strip():
        out["reason"] = "подпись манифеста (TESSENT_INTEGRITY_SIG) не задана"
        return out
    import base64
    try:
        sig = base64.urlsafe_b64decode(
            signature_b64.strip() + "=" * (-len(signature_b64.strip()) % 4))
    except Exception:
        out["reason"] = "подпись манифеста повреждена"
        return out
    if not _verify_manifest_signature(manifest_bytes, sig):
        out["reason"] = "подпись манифеста не прошла проверку (правка манифеста?)"
        return out

    try:
        manifest = json.loads(manifest_bytes)
        expected: dict[str, str] = manifest.get("files", {})
    except Exception:
        out["reason"] = "манифест не JSON"
        return out

    actual = compute_hashes(list(expected.keys()))
    for rel, exp_hash in expected.items():
        if rel not in actual:
            out["missing"].append(rel)
        elif actual[rel] != exp_hash:
            out["tampered"].append(rel)
    out["checked"] = len(expected)
    if out["missing"] or out["tampered"]:
        out["reason"] = (f"целостность нарушена: изменено {len(out['tampered'])}, "
                         f"отсутствует {len(out['missing'])} модулей ядра")
        return out
    out["ok"] = True
    return out


def enforce_integrity_on_startup() -> Optional[dict[str, Any]]:
    """TESSENT_INTEGRITY_REQUIRED=on → нарушенная целостность валит boot;
    иначе no-op (наш SaaS/dev). Возвращает результат проверки для лога."""
    if not integrity_required():
        return None
    res = verify_integrity()
    if not res["ok"]:
        raise RuntimeError(
            f"Tessbrain integrity check failed: {res['reason']}. "
            f"tampered={res['tampered']} missing={res['missing']}. "
            "Ядро изменено — запуск заблокирован.")
    logger.info("Tessbrain integrity OK: %d модулей ядра совпали с манифестом",
                res["checked"])
    return res
