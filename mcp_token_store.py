# -*- coding: utf-8 -*-
"""Долгоживущие MCP-токены Tessbrain (tess_mcp_…), привязанные к user_id.

Зачем: у Tessbrain нет своего PAT, а Supabase-JWT короткоживущий и не годится для
постоянного коннектора claude.ai. На экране согласия OAuth пользователь один раз
вставляет свой Supabase-токен → мы валидируем его через API (/auth/me) → и
ВЫПУСКАЕМ стабильный tess_mcp_-токен, привязанный к его user_id. Коннектор держит
именно его. На каждый MCP-вызов сервер меняет tess_mcp_ → user_id и исполняет от
его имени (service-auth + user_id).

Хранилище — простой JSON-файл (по умолчанию data/mcp_tokens.json); один процесс
remote-сервера. Ничего секретного, кроме самих токенов: файл держите с правами 600.
Never-raise на чтении.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "mcp_tokens.json"
_PREFIX = "tess_mcp_"


def _store_path() -> Path:
    return Path(os.environ.get("MCP_TOKENS_FILE", "").strip() or _DEFAULT_PATH)


def _load() -> Dict[str, Any]:
    p = _store_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save(data: Dict[str, Any]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def mint(user_id: str, *, label: str = "claude.ai") -> str:
    """Выпустить новый tess_mcp_-токен для пользователя. Возврат — сам токен."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id required")
    token = _PREFIX + secrets.token_hex(24)
    data = _load()
    data[token] = {"user_id": uid, "label": label, "created_at": int(time.time())}
    _save(data)
    return token


def validate(token: str) -> Optional[str]:
    """tess_mcp_-токен → user_id (или None, если неизвестен). Never-raise."""
    t = str(token or "").strip()
    if not t.startswith(_PREFIX):
        return None
    rec = _load().get(t)
    if not isinstance(rec, dict):
        return None
    uid = str(rec.get("user_id") or "").strip()
    return uid or None


def revoke(token: str) -> bool:
    data = _load()
    if token in data:
        del data[token]
        _save(data)
        return True
    return False


def list_for_user(user_id: str) -> list:
    uid = str(user_id or "").strip()
    return [{"token": k[:16] + "…", "created_at": v.get("created_at"),
             "label": v.get("label")}
            for k, v in _load().items()
            if isinstance(v, dict) and str(v.get("user_id") or "") == uid]


def find_by_prefix(user_id: str, prefix: str) -> Optional[str]:
    """Префикс из списка → полный токен ЭТОГО пользователя (иначе None).

    Наружу секрет не отдаётся, поэтому UI оперирует префиксами; отзыв обязан
    искать только среди своих токенов, чтобы чужой нельзя было погасить,
    угадав начало."""
    uid = str(user_id or "").strip()
    head = str(prefix or "").strip()
    if not uid or len(head) < 12:
        return None
    for token, rec in _load().items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("user_id") or "") == uid and token.startswith(head):
            return token
    return None


__all__ = ["mint", "validate", "revoke", "list_for_user", "find_by_prefix"]


def _cli() -> int:
    """CLI: выпустить/отозвать/показать tess_mcp-токены (для Claude Code без веб-флоу).
        python -m mcp_token_store mint <user_id>
        python -m mcp_token_store list <user_id>
        python -m mcp_token_store revoke <token>
    """
    import sys
    a = sys.argv[1:]
    if not a:
        print(_cli.__doc__)
        return 2
    cmd = a[0]
    if cmd == "mint" and len(a) >= 2:
        print(mint(a[1], label=(a[2] if len(a) > 2 else "cli")))
        return 0
    if cmd == "revoke" and len(a) >= 2:
        print("revoked" if revoke(a[1]) else "not found")
        return 0
    if cmd == "list" and len(a) >= 2:
        for row in list_for_user(a[1]):
            print(row)
        return 0
    print(_cli.__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
