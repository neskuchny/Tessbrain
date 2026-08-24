# -*- coding: utf-8 -*-
"""Экспорт в Google Sheets / Docs через сервисный аккаунт (write!).

До сих пор система умела Google только ЧИТАТЬ (public CSV). Этот модуль —
запись: «медиаплан → сразу в Google Sheets», «обоснование → в Google Docs».

Настройка (один раз): в Google Cloud создать сервисный аккаунт, включить
Sheets API + Docs API + Drive API, скачать JSON-ключ и положить его целиком
в env GOOGLE_SERVICE_ACCOUNT_JSON (или путь в GOOGLE_SERVICE_ACCOUNT_FILE).
Файлы создаются на диске сервисного аккаунта и расшариваются по ссылке
(anyone-with-link reader) — ссылка возвращается пользователю.

Без ключа модуль честно отвечает configured=False с подсказкой.
JWT RS256 подписывается локально (cryptography уже в проде) — без
google-auth зависимостей.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCOPES = ("https://www.googleapis.com/auth/spreadsheets "
           "https://www.googleapis.com/auth/documents "
           "https://www.googleapis.com/auth/drive")
_TOKEN_URL = "https://oauth2.googleapis.com/token"

_token_cache: Dict[str, Any] = {"token": None, "exp": 0}


def _sa_creds() -> Optional[dict]:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        if path and os.path.exists(path):
            raw = open(path, encoding="utf-8").read()
    if not raw:
        return None
    try:
        creds = json.loads(raw)
        return creds if creds.get("client_email") and creds.get("private_key") else None
    except ValueError:
        return None


def is_configured() -> bool:
    return _sa_creds() is not None


def config_hint() -> str:
    return ("Google-экспорт не настроен: создайте сервисный аккаунт в "
            "Google Cloud (Sheets+Docs+Drive API), JSON-ключ → env "
            "GOOGLE_SERVICE_ACCOUNT_JSON")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")




def _sign_rs256(private_key_pem: str, data: bytes) -> bytes:
    """RS256-подпись JWT. Вынесена отдельно: в тестовом окружении
    cryptography паникует (pyo3) — тесты подменяют эту функцию,
    в проде работает реальная подпись."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None)
    return key.sign(data, padding.PKCS1v15(), hashes.SHA256())


async def _access_token(http=None) -> str:
    """OAuth2-токен сервисного аккаунта (JWT RS256 → token endpoint),
    кэш до истечения. http — инъекция для тестов (httpx-совместимый)."""
    now = int(time.time())
    if _token_cache["token"] and _token_cache["exp"] - 60 > now:
        return _token_cache["token"]
    creds = _sa_creds()
    if not creds:
        raise RuntimeError(config_hint())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({
        "iss": creds["client_email"], "scope": _SCOPES,
        "aud": _TOKEN_URL, "iat": now, "exp": now + 3600,
    }).encode())
    signing_input = f"{header}.{claims}".encode()
    sig = _sign_rs256(creds["private_key"], signing_input)
    assertion = f"{header}.{claims}.{_b64url(sig)}"

    if http is None:
        import httpx
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(_TOKEN_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion})
    else:
        resp = await http.post(_TOKEN_URL, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion})
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["exp"] = now + int(data.get("expires_in", 3600))
    return _token_cache["token"]


async def _api(method: str, url: str, *, token: str, json_body=None,
               http=None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if http is None:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, headers=headers,
                                        json=json_body)
    else:
        resp = await http.request(method, url, headers=headers, json=json_body)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


async def _share_anyone(file_id: str, token: str, http=None) -> None:
    """Доступ по ссылке (reader) — иначе файл виден только сервис-аккаунту."""
    try:
        await _api("POST",
                   f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                   token=token, http=http,
                   json_body={"role": "reader", "type": "anyone"})
    except Exception as e:
        logger.warning(f"google share failed (файл у сервис-аккаунта): {e}")


async def export_table_to_sheets(*, title: str, columns: List[str],
                                 rows: List[dict], http=None) -> dict:
    """Создать Google Sheet с таблицей. rows: list[dict] по columns.
    Возвращает {url, spreadsheet_id}. Числа-строки конвертируются."""
    token = await _access_token(http=http)
    created = await _api(
        "POST", "https://sheets.googleapis.com/v4/spreadsheets",
        token=token, http=http,
        json_body={"properties": {"title": title[:100] or "Tessbrain"}})
    sid = created["spreadsheetId"]

    def _cell(v):
        if v in (None, ""):
            return ""
        try:
            return float(v)
        except (TypeError, ValueError):
            return str(v)
    values = [columns] + [[_cell(r.get(c)) for c in columns] for r in rows]
    await _api(
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/A1"
        "?valueInputOption=USER_ENTERED",
        token=token, http=http, json_body={"values": values})
    await _share_anyone(sid, token, http=http)
    return {"spreadsheet_id": sid,
            "url": f"https://docs.google.com/spreadsheets/d/{sid}"}


async def export_text_to_doc(*, title: str, text: str, http=None) -> dict:
    """Создать Google Doc с текстом. Возвращает {url, document_id}."""
    token = await _access_token(http=http)
    created = await _api("POST", "https://docs.googleapis.com/v1/documents",
                         token=token, http=http,
                         json_body={"title": title[:100] or "Tessbrain"})
    did = created["documentId"]
    await _api("POST",
               f"https://docs.googleapis.com/v1/documents/{did}:batchUpdate",
               token=token, http=http,
               json_body={"requests": [{"insertText": {
                   "location": {"index": 1}, "text": text[:200000]}}]})
    await _share_anyone(did, token, http=http)
    return {"document_id": did,
            "url": f"https://docs.google.com/document/d/{did}"}
