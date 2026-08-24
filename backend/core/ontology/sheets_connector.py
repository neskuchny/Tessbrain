# -*- coding: utf-8 -*-
"""Google Sheets коннектор онтологии данных (№9: финансы = онтология цифр).

Малый бизнес живёт в таблицах — подключаем лист Google Sheets как датасет
тем же путём, что CRM/BI: register_from_crm(provider="sheets",
kind="sheet:<spreadsheet_id>" или "sheet:<id>#<gid>").

v1 — публичная ссылка (файл «доступен всем, у кого есть ссылка»):
CSV-экспорт без OAuth. Приватные листы через сервис-аккаунт — следующая
итерация. Учётные данные не нужны → env_check всегда ok.

Тестируется инжекцией fetch_fn(url) -> str (CSV-текст).
"""
from __future__ import annotations

import csv
import io
import logging
import re
from typing import List, Optional, Tuple

from backend.core.ontology.crm_connectors import CrmConnector, register

logger = logging.getLogger(__name__)

_SHEET_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def _service_account_token() -> Optional[str]:
    """OAuth-токен сервис-аккаунта для ПРИВАТНЫХ листов (опционально).

    env GOOGLE_SERVICE_ACCOUNT_FILE = путь к JSON-ключу; лист должен быть
    расшарен на email сервис-аккаунта (как обычному пользователю).
    google-auth уже в зависимостях (тянется langchain-google-genai).
    Нет env/библиотеки → None (работаем по публичной ссылке)."""
    import os
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not path:
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/drive.readonly"])
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        logger.warning(f"sheets: service account token failed: {e}")
        return None


async def _fetch_csv(url: str) -> str:
    import httpx
    headers = {}
    token = _service_account_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        r = await c.get(url, headers=headers)
        r.raise_for_status()
        return r.text


class GoogleSheetsConnector(CrmConnector):
    provider = "sheets"
    kinds = ("sheet:<spreadsheet_id>", "sheet:<spreadsheet_id>#<gid>")

    def env_check(self) -> Optional[str]:
        return None  # публичная ссылка — авторизация не нужна

    async def fetch(self, kind: str, fetch_fn=None, *,
                    limit: int = 200) -> Tuple[List[str], List[dict]]:
        if not kind.startswith("sheet:"):
            raise ValueError("kind должен быть sheet:<spreadsheet_id>[#gid]")
        ref = kind[len("sheet:"):].strip()
        # принимаем и полную ссылку на таблицу — вытащим id
        m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", ref)
        if m:
            ref = m.group(1) + ("#" + ref.split("#gid=")[1] if "#gid=" in ref else "")
        sheet_id, _, gid = ref.partition("#")
        if not _SHEET_ID.match(sheet_id):
            raise ValueError("некорректный spreadsheet_id (анти-инъекция)")
        gid = gid or "0"
        if not gid.isdigit():
            raise ValueError("gid должен быть числом")
        url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}"
               f"/export?format=csv&gid={gid}")
        text = await (fetch_fn or _fetch_csv)(url)
        if text.lstrip().lower().startswith(("<!doctype", "<html")):
            raise ValueError(
                "Google вернул HTML вместо CSV — лист не открыт по ссылке. "
                "Настройте доступ «все, у кого есть ссылка» и повторите.")
        reader = csv.reader(io.StringIO(text))
        table = [row for row in reader if any(c.strip() for c in row)]
        if len(table) < 2:
            raise ValueError("в листе нет данных (только заголовок или пусто)")
        columns = [c.strip() or f"col_{i}" for i, c in enumerate(table[0], 1)]
        rows = [dict(zip(columns, r)) for r in table[1:limit + 1]]
        logger.info(f"sheets: {sheet_id}#{gid} → {len(rows)} строк, "
                    f"{len(columns)} колонок")
        return columns, rows


# Регистрируем ЭКЗЕМПЛЯР (как все прочие коннекторы). Раньше здесь был
# декоратор @register на КЛАССЕ — в реестр попадал класс, и list_providers()/
# get_connector("sheets").fetch() падали unbound-method (env_check/fetch без
# self). Функционально коннектор поэтому не работал.
register(GoogleSheetsConnector())
