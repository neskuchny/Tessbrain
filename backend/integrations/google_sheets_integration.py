# -*- coding: utf-8 -*-
"""
Google Sheets Integration — чтение, запись и добавление данных в Google Таблицы.

Использует Google Sheets API v4 через REST.
Аутентификация: OAuth2 (access_token + refresh_token) ИЛИ Service Account.

Credentials (из user_integrations):
    - oauth_token: JSON с access_token, refresh_token, client_id, client_secret
    ИЛИ
    - service_account_json: JSON ключ сервисного аккаунта
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"


@register_integration
class GoogleSheetsIntegration(BaseIntegration):
    integration_id = "google_sheets"
    integration_name = "Google Sheets"
    category = "productivity"
    keywords = [
        # Русский
        "google sheets", "гугл таблиц", "таблиц", "spreadsheet",
        "ячейк", "строк", "столбец", "колонк",
        "формул", "эксель", "excel", "xlsx",
        "запиши в таблицу", "прочитай таблицу", "добавь в таблицу",
        # English
        "spreadsheet", "worksheet", "cell", "row", "column",
        "read sheet", "write sheet", "append row",
        "google spreadsheet", "gsheet",
        # Транслит
        "tablitsa", "yacheyka",
    ]

    _access_token: str = ""

    async def _ensure_token(self) -> bool:
        """Получить или обновить access_token."""
        if self._access_token:
            return True

        oauth_str = self.get_key("oauth_token")
        if not oauth_str:
            return False

        try:
            oauth = json.loads(oauth_str)
        except json.JSONDecodeError:
            logger.warning("[GoogleSheets] Invalid oauth_token JSON")
            return False

        if oauth.get("access_token"):
            self._access_token = oauth["access_token"]
            return True

        if oauth.get("refresh_token") and oauth.get("client_id") and oauth.get("client_secret"):
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(TOKEN_URL, data={
                    "grant_type": "refresh_token",
                    "refresh_token": oauth["refresh_token"],
                    "client_id": oauth["client_id"],
                    "client_secret": oauth["client_secret"],
                })
                if resp.status_code == 200:
                    data = resp.json()
                    self._access_token = data["access_token"]
                    return True
                logger.warning(f"[GoogleSheets] Token refresh failed: {resp.text[:200]}")
        return False

    async def _api(self, method: str, url: str, body: Optional[Dict] = None,
                   params: Optional[Dict] = None) -> Dict[str, Any]:
        if not await self._ensure_token():
            return {"error": True, "message": "Google Sheets OAuth not configured or token expired"}

        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, json=body, params=params)
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                    msg = err.get("error", {}).get("message", str(err))
                except Exception:
                    msg = resp.text[:500]
                return {"error": True, "status": resp.status_code, "message": msg}
            if resp.status_code == 204:
                return {"success": True}
            return resp.json()

    # ── Tool implementations ──────────────────────────────────────

    async def read_range(self, spreadsheet_id: str, range_: str = "Sheet1",
                         value_render: str = "FORMATTED_VALUE") -> str:
        """Прочитать диапазон ячеек."""
        url = f"{SHEETS_API}/{spreadsheet_id}/values/{range_}"
        result = await self._api("GET", url, params={"valueRenderOption": value_render})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        values = result.get("values", [])
        return json.dumps({
            "success": True,
            "spreadsheet_id": spreadsheet_id,
            "range": result.get("range", range_),
            "rows_count": len(values),
            "values": values[:100],  # Ограничиваем вывод
        }, ensure_ascii=False)

    async def write_range(self, spreadsheet_id: str, range_: str,
                          values: List[List[Any]]) -> str:
        """Записать данные в диапазон ячеек."""
        url = f"{SHEETS_API}/{spreadsheet_id}/values/{range_}"
        body = {
            "range": range_,
            "majorDimension": "ROWS",
            "values": values,
        }
        result = await self._api("PUT", url, body=body,
                                 params={"valueInputOption": "USER_ENTERED"})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "updated_range": result.get("updatedRange", ""),
            "updated_rows": result.get("updatedRows", 0),
            "updated_cells": result.get("updatedCells", 0),
        }, ensure_ascii=False)

    async def append_rows(self, spreadsheet_id: str, range_: str = "Sheet1",
                          values: List[List[Any]] | None = None) -> str:
        """Добавить строки в конец таблицы."""
        if not values:
            return json.dumps({"success": False, "error": "No values to append"}, ensure_ascii=False)

        url = f"{SHEETS_API}/{spreadsheet_id}/values/{range_}:append"
        body = {
            "range": range_,
            "majorDimension": "ROWS",
            "values": values,
        }
        result = await self._api("POST", url, body=body,
                                 params={"valueInputOption": "USER_ENTERED",
                                         "insertDataOption": "INSERT_ROWS"})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        updates = result.get("updates", {})
        return json.dumps({
            "success": True,
            "updated_range": updates.get("updatedRange", ""),
            "updated_rows": updates.get("updatedRows", 0),
        }, ensure_ascii=False)

    async def get_spreadsheet_info(self, spreadsheet_id: str) -> str:
        """Получить информацию о таблице (листы, заголовки)."""
        url = f"{SHEETS_API}/{spreadsheet_id}"
        result = await self._api("GET", url, params={"fields": "spreadsheetId,properties.title,sheets.properties"})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        sheets = []
        for s in result.get("sheets", []):
            props = s.get("properties", {})
            sheets.append({
                "title": props.get("title", ""),
                "sheet_id": props.get("sheetId", 0),
                "row_count": props.get("gridProperties", {}).get("rowCount", 0),
                "column_count": props.get("gridProperties", {}).get("columnCount", 0),
            })

        return json.dumps({
            "success": True,
            "spreadsheet_id": result.get("spreadsheetId", ""),
            "title": result.get("properties", {}).get("title", ""),
            "sheets": sheets,
        }, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "sheets_read",
                "description": "Read data from a Google Sheets range. Returns rows as arrays. Use for: read spreadsheet, get table data, прочитай таблицу.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {"type": "string", "description": "Google Sheets ID (from URL)"},
                        "range": {"type": "string", "description": "Range to read, e.g. 'Sheet1!A1:D10' or 'Sheet1'"},
                    },
                    "required": ["spreadsheet_id"],
                },
            },
            {
                "name": "sheets_write",
                "description": "Write data to a Google Sheets range. Replaces existing values. Use for: update spreadsheet, write to table, запиши в таблицу.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {"type": "string", "description": "Google Sheets ID"},
                        "range": {"type": "string", "description": "Target range, e.g. 'Sheet1!A1:C3'"},
                        "values": {"type": "array", "description": "2D array of values, e.g. [['Name','Score'],['Alice','95']]",
                                   "items": {"type": "array", "items": {"type": "string"}}},
                    },
                    "required": ["spreadsheet_id", "range", "values"],
                },
            },
            {
                "name": "sheets_append",
                "description": "Append rows to the end of a Google Sheet. Use for: add row, add data, добавь строку, добавь в таблицу.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {"type": "string", "description": "Google Sheets ID"},
                        "range": {"type": "string", "description": "Sheet name or range, default 'Sheet1'"},
                        "values": {"type": "array", "description": "Rows to append, e.g. [['Alice','95'],['Bob','88']]",
                                   "items": {"type": "array", "items": {"type": "string"}}},
                    },
                    "required": ["spreadsheet_id", "values"],
                },
            },
            {
                "name": "sheets_info",
                "description": "Get spreadsheet metadata: title, sheets list, sizes. Use for: info about spreadsheet, list sheets, какие листы в таблице.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {"type": "string", "description": "Google Sheets ID"},
                    },
                    "required": ["spreadsheet_id"],
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Google Sheets not configured. Add OAuth token in integrations."}, ensure_ascii=False)

        dispatch = {
            "sheets_read": lambda: self.read_range(
                spreadsheet_id=str(kwargs.get("spreadsheet_id", "")),
                range_=str(kwargs.get("range", "Sheet1")),
            ),
            "sheets_write": lambda: self.write_range(
                spreadsheet_id=str(kwargs.get("spreadsheet_id", "")),
                range_=str(kwargs.get("range", "")),
                values=kwargs.get("values", []),
            ),
            "sheets_append": lambda: self.append_rows(
                spreadsheet_id=str(kwargs.get("spreadsheet_id", "")),
                range_=str(kwargs.get("range", "Sheet1")),
                values=kwargs.get("values", []),
            ),
            "sheets_info": lambda: self.get_spreadsheet_info(
                spreadsheet_id=str(kwargs.get("spreadsheet_id", "")),
            ),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown Sheets tool: {tool_name}"}, ensure_ascii=False)
