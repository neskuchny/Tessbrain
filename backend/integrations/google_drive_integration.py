# -*- coding: utf-8 -*-
"""
Google Drive Integration — поиск, список файлов, скачивание.

Использует Google Drive API v3 через REST.
Аутентификация: OAuth2 (access_token + refresh_token).

Credentials (из user_integrations):
    - oauth_token: JSON с access_token, refresh_token, client_id, client_secret
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"

MIME_TYPE_LABELS = {
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.folder": "Folder",
    "application/pdf": "PDF",
    "image/png": "PNG Image",
    "image/jpeg": "JPEG Image",
    "text/plain": "Text File",
}


@register_integration
class GoogleDriveIntegration(BaseIntegration):
    integration_id = "google_drive"
    integration_name = "Google Drive"
    category = "productivity"
    keywords = [
        # Русский
        "google drive", "гугл диск", "диск", "файл",
        "найди файл", "список файлов", "скачай файл",
        "папк", "загрузк", "облак",
        # English
        "drive", "file", "folder", "search files",
        "list files", "download file", "google file",
        "shared drive", "my drive",
        # Транслит
        "disk", "fayl", "papka",
    ]

    _access_token: str = ""

    async def _ensure_token(self) -> bool:
        if self._access_token:
            return True

        oauth_str = self.get_key("oauth_token")
        if not oauth_str:
            return False

        try:
            oauth = json.loads(oauth_str)
        except json.JSONDecodeError:
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
                    self._access_token = resp.json()["access_token"]
                    return True
        return False

    async def _api(self, method: str, path: str, params: Optional[Dict] = None,
                   body: Optional[Dict] = None) -> Dict[str, Any]:
        if not await self._ensure_token():
            return {"error": True, "message": "Google Drive OAuth not configured or token expired"}

        headers = {"Authorization": f"Bearer {self._access_token}"}
        url = f"{DRIVE_API}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, params=params, json=body)
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get("error", {}).get("message", resp.text[:300])
                except Exception:
                    msg = resp.text[:500]
                return {"error": True, "status": resp.status_code, "message": msg}
            if resp.status_code == 204:
                return {"success": True}
            return resp.json()

    # ── Tool implementations ──────────────────────────────────────

    async def search_files(self, query: str, max_results: int = 20) -> str:
        """Поиск файлов по имени или типу."""
        # Преобразуем простой запрос в Drive query
        drive_query = f"name contains '{query}' and trashed = false"
        result = await self._api("GET", "/files", params={
            "q": drive_query,
            "pageSize": min(max_results, 100),
            "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink,owners)",
            "orderBy": "modifiedTime desc",
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        files = []
        for f in result.get("files", []):
            mime = f.get("mimeType", "")
            files.append({
                "id": f.get("id", ""),
                "name": f.get("name", ""),
                "type": MIME_TYPE_LABELS.get(mime, mime),
                "size": f.get("size", ""),
                "modified": f.get("modifiedTime", ""),
                "url": f.get("webViewLink", ""),
            })

        return json.dumps({
            "success": True,
            "query": query,
            "count": len(files),
            "files": files,
        }, ensure_ascii=False)

    async def list_files(self, folder_id: str = "root", max_results: int = 30) -> str:
        """Список файлов в папке."""
        q = f"'{folder_id}' in parents and trashed = false"
        result = await self._api("GET", "/files", params={
            "q": q,
            "pageSize": min(max_results, 100),
            "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink)",
            "orderBy": "folder,name",
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        files = []
        for f in result.get("files", []):
            mime = f.get("mimeType", "")
            files.append({
                "id": f.get("id", ""),
                "name": f.get("name", ""),
                "type": MIME_TYPE_LABELS.get(mime, mime),
                "is_folder": mime == "application/vnd.google-apps.folder",
                "size": f.get("size", ""),
                "modified": f.get("modifiedTime", ""),
                "url": f.get("webViewLink", ""),
            })

        return json.dumps({
            "success": True,
            "folder_id": folder_id,
            "count": len(files),
            "files": files,
        }, ensure_ascii=False)

    async def get_file_info(self, file_id: str) -> str:
        """Получить подробную информацию о файле."""
        result = await self._api("GET", f"/files/{file_id}", params={
            "fields": "id,name,mimeType,size,modifiedTime,createdTime,webViewLink,webContentLink,owners,permissions,description",
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        mime = result.get("mimeType", "")
        owners = [o.get("displayName", o.get("emailAddress", "")) for o in result.get("owners", [])]

        return json.dumps({
            "success": True,
            "id": result.get("id", ""),
            "name": result.get("name", ""),
            "type": MIME_TYPE_LABELS.get(mime, mime),
            "size": result.get("size", ""),
            "created": result.get("createdTime", ""),
            "modified": result.get("modifiedTime", ""),
            "owners": owners,
            "description": result.get("description", ""),
            "view_url": result.get("webViewLink", ""),
            "download_url": result.get("webContentLink", ""),
        }, ensure_ascii=False)

    async def read_file_content(self, file_id: str) -> str:
        """Прочитать текстовое содержимое файла (для Google Docs/Sheets — экспорт в text)."""
        # Сначала узнаем тип файла
        info = await self._api("GET", f"/files/{file_id}", params={"fields": "mimeType,name"})
        if info.get("error"):
            return json.dumps({"success": False, "error": info["message"]}, ensure_ascii=False)

        mime = info.get("mimeType", "")
        name = info.get("name", "")

        if not await self._ensure_token():
            return json.dumps({"error": "OAuth not configured"}, ensure_ascii=False)

        headers = {"Authorization": f"Bearer {self._access_token}"}

        async with httpx.AsyncClient(timeout=30) as client:
            # Google Workspace files need export
            if mime.startswith("application/vnd.google-apps."):
                export_mime = "text/plain"
                if "spreadsheet" in mime:
                    export_mime = "text/csv"
                resp = await client.get(
                    f"{DRIVE_API}/files/{file_id}/export",
                    headers=headers,
                    params={"mimeType": export_mime},
                )
            else:
                resp = await client.get(
                    f"{DRIVE_API}/files/{file_id}",
                    headers=headers,
                    params={"alt": "media"},
                )

            if resp.status_code >= 400:
                return json.dumps({"success": False, "error": f"Read failed: {resp.status_code}"}, ensure_ascii=False)

            content = resp.text[:10000]

        return json.dumps({
            "success": True,
            "file_id": file_id,
            "name": name,
            "content": content,
            "content_length": len(resp.text),
            "truncated": len(resp.text) > 10000,
        }, ensure_ascii=False)

    async def upload_file(self, filename: str, content: bytes,
                          mime_type: str = "application/octet-stream",
                          folder_id: str = "",
                          make_link_public: bool = True) -> Dict[str, Any]:
        """Загрузить файл на Google Drive и вернуть ссылку.

        Multipart-upload (metadata + media). make_link_public=True →
        best-effort permission «anyone with link: reader» (в корп. Workspace
        может быть запрещено политикой — тогда ссылка останется приватной,
        это не ошибка). Требует у OAuth-токена write-скоуп
        (drive / drive.file); readonly-токен вернёт 403 с понятным текстом."""
        if not await self._ensure_token():
            return {"error": True,
                    "message": "Google Drive OAuth not configured or token expired"}

        import mimetypes as _mt
        mime = mime_type or _mt.guess_type(filename)[0] or "application/octet-stream"
        metadata: Dict[str, Any] = {"name": filename}
        if folder_id:
            metadata["parents"] = [folder_id]

        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    "https://www.googleapis.com/upload/drive/v3/files",
                    headers=headers,
                    params={"uploadType": "multipart",
                            "fields": "id,name,webViewLink"},
                    files={
                        "metadata": (None, json.dumps(metadata),
                                     "application/json; charset=UTF-8"),
                        "file": (filename, content, mime),
                    },
                )
                if resp.status_code >= 400:
                    try:
                        msg = resp.json().get("error", {}).get(
                            "message", resp.text[:300])
                    except Exception:
                        msg = resp.text[:300]
                    if resp.status_code == 403:
                        msg += (" (похоже, у токена только чтение — "
                                "переподключите Google Drive со скоупом записи)")
                    return {"error": True, "status": resp.status_code,
                            "message": msg}
                data = resp.json()
                file_id = data.get("id", "")

                # best-effort публичная ссылка
                if make_link_public and file_id:
                    perm = await self._api(
                        "POST", f"/files/{file_id}/permissions",
                        body={"role": "reader", "type": "anyone"})
                    if perm.get("error"):
                        logger.debug("drive public permission skipped: %s",
                                     perm.get("message"))

                return {"success": True, "file_id": file_id,
                        "name": data.get("name", filename),
                        "url": data.get("webViewLink")
                        or f"https://drive.google.com/file/d/{file_id}/view"}
        except Exception as e:
            logger.warning(f"Drive upload failed: {e}")
            return {"error": True, "message": str(e)}

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "drive_search",
                "description": "Search files in Google Drive by name. Use for: find file, search drive, найди файл на диске.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (file name or keywords)"},
                        "max_results": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "drive_list",
                "description": "List files in a Google Drive folder. Use for: list files, show folder, что в папке, список файлов.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "folder_id": {"type": "string", "description": "Folder ID (default: root = My Drive)"},
                        "max_results": {"type": "integer", "description": "Max results (default 30)"},
                    },
                },
            },
            {
                "name": "drive_file_info",
                "description": "Get detailed info about a file (size, owner, dates, URL). Use for: file info, инфо о файле.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string", "description": "Google Drive file ID"},
                    },
                    "required": ["file_id"],
                },
            },
            {
                "name": "drive_read",
                "description": "Read text content of a file from Google Drive. Use for: read file, прочитай файл, содержимое файла.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string", "description": "Google Drive file ID"},
                    },
                    "required": ["file_id"],
                },
            },
            {
                "name": "drive_upload",
                "description": "Upload a server-side file to Google Drive and get a link. Use for: upload to drive, загрузи на диск, сохрани на гугл диск.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file on the Tessbrain server"},
                        "folder_id": {"type": "string", "description": "Target Drive folder ID (optional)"},
                    },
                    "required": ["file_path"],
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Google Drive not configured. Add OAuth token in integrations."}, ensure_ascii=False)

        dispatch = {
            "drive_search": lambda: self.search_files(
                query=str(kwargs.get("query", "")),
                max_results=int(kwargs.get("max_results", 20)),
            ),
            "drive_list": lambda: self.list_files(
                folder_id=str(kwargs.get("folder_id", "root")),
                max_results=int(kwargs.get("max_results", 30)),
            ),
            "drive_file_info": lambda: self.get_file_info(str(kwargs.get("file_id", ""))),
            "drive_read": lambda: self.read_file_content(str(kwargs.get("file_id", ""))),
            "drive_upload": lambda: self._upload_tool(
                str(kwargs.get("file_path", "")),
                str(kwargs.get("folder_id", ""))),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown Drive tool: {tool_name}"}, ensure_ascii=False)

    async def _upload_tool(self, file_path: str, folder_id: str = "") -> str:
        """Tool-обёртка над upload_file: читает файл с сервера, возвращает JSON."""
        import os as _os
        if not file_path or not _os.path.isfile(file_path):
            return json.dumps({"error": f"file not found: {file_path}"},
                              ensure_ascii=False)
        try:
            with open(file_path, "rb") as f:
                content = f.read()
        except OSError as e:
            return json.dumps({"error": f"cannot read file: {e}"},
                              ensure_ascii=False)
        res = await self.upload_file(_os.path.basename(file_path), content,
                                     folder_id=folder_id)
        return json.dumps(res, ensure_ascii=False)


async def upload_files_to_drive(user_id: str, file_paths: List[str],
                                folder_id: str = "") -> Dict[str, Any]:
    """Загрузить файлы пользователя на его Google Drive → [{file, url}].

    Для handoff-пайплайна («готовые артефакты → диск → ссылка в задачу»).
    Never-raise: по-файловые ошибки в errors; нет интеграции → сразу error."""
    import os as _os
    integ = GoogleDriveIntegration(user_id)
    if not await integ.load_credentials():
        return {"status": "error", "message": "Google Drive не подключён",
                "links": [], "errors": []}
    links, errors = [], []
    for fp in file_paths:
        name = _os.path.basename(str(fp))
        try:
            with open(fp, "rb") as f:
                content = f.read()
            res = await integ.upload_file(name, content, folder_id=folder_id)
            if res.get("success"):
                links.append({"file": name, "url": res.get("url")})
            else:
                errors.append({"file": name, "error": res.get("message")})
        except Exception as e:
            errors.append({"file": name, "error": str(e)})
    status = "success" if links and not errors else \
             ("partial" if links else "error")
    return {"status": status, "links": links, "errors": errors}
