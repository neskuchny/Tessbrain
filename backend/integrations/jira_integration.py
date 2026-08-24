# -*- coding: utf-8 -*-
"""
Jira Integration — полный CRUD для задач (issues), проектов и спринтов.

Использует Jira REST API v3 (Cloud) / v2 (Server).
Аутентификация: email + API Token (Basic Auth).

Credentials (из user_integrations):
    - domain: your-company.atlassian.net
    - email: your@email.com
    - api_token: API Token из id.atlassian.com
    - default_project: PROJECT_KEY (опционально)
"""

import base64
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)


@register_integration
class JiraIntegration(BaseIntegration):
    integration_id = "jira"
    integration_name = "Jira"
    category = "tasks"
    keywords = [
        # Русский
        "jira", "джира", "жира", "тикет", "issue",
        "создай тикет", "создай задачу в jira",
        "перевести в jira", "статус задачи jira",
        "спринт jira", "бэклог jira",
        # English
        "jira issue", "jira ticket", "create issue",
        "jira sprint", "jira backlog", "jira board",
        "transition issue", "move issue", "assign issue",
        "jql", "jira search", "my jira issues",
        # Транслит
        "dzhira", "tiket",
    ]

    def _auth_header(self) -> str:
        email = self.get_key("email")
        token = self.get_key("api_token")
        creds = base64.b64encode(f"{email}:{token}".encode()).decode()
        return f"Basic {creds}"

    def _base_url(self) -> str:
        domain = self.get_key("domain", "").rstrip("/")
        if not domain.startswith("http"):
            domain = f"https://{domain}"
        return domain

    async def _api(self, method: str, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self._base_url()}/rest/api/3{path}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, json=body)
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                    messages = err.get("errorMessages", []) or [err.get("message", "")]
                    msg = "; ".join(messages) if messages else str(err)
                except Exception:
                    msg = resp.text[:500]
                logger.warning(f"[Jira] {method} {path} → {resp.status_code}: {msg}")
                return {"error": True, "status": resp.status_code, "message": msg}
            if resp.status_code == 204:
                return {"success": True}
            return resp.json()

    # ── Tool implementations ──────────────────────────────────────

    async def search_issues(self, jql: str, max_results: int = 20) -> str:
        """Поиск задач через JQL."""
        body = {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["summary", "status", "assignee", "priority", "created", "updated", "issuetype", "project"],
        }
        result = await self._api("POST", "/search", body)
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        issues = []
        for issue in result.get("issues", []):
            fields = issue.get("fields", {})
            assignee = fields.get("assignee")
            issues.append({
                "key": issue["key"],
                "summary": fields.get("summary", ""),
                "status": fields.get("status", {}).get("name", ""),
                "assignee": assignee.get("displayName", "") if assignee else "",
                "priority": fields.get("priority", {}).get("name", ""),
                "type": fields.get("issuetype", {}).get("name", ""),
                "project": fields.get("project", {}).get("key", ""),
                "updated": fields.get("updated", ""),
            })
        return json.dumps({"success": True, "total": result.get("total", 0), "issues": issues}, ensure_ascii=False)

    async def get_my_issues(self, status: str = "") -> str:
        """Получить мои задачи."""
        jql = "assignee = currentUser() ORDER BY updated DESC"
        if status:
            jql = f'assignee = currentUser() AND status = "{status}" ORDER BY updated DESC'
        return await self.search_issues(jql)

    async def create_issue(self, project: str, summary: str, description: str = "",
                           issue_type: str = "Task", assignee: str = "", priority: str = "") -> str:
        """Создать задачу."""
        if not project:
            project = self.get_key("default_project")
        if not project:
            return json.dumps({"success": False, "error": "No project specified"}, ensure_ascii=False)

        fields: Dict[str, Any] = {
            "project": {"key": project},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
            }
        if assignee:
            fields["assignee"] = {"accountId": assignee}
        if priority:
            fields["priority"] = {"name": priority}

        result = await self._api("POST", "/issue", {"fields": fields})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "key": result.get("key", ""),
            "id": result.get("id", ""),
            "url": f"{self._base_url()}/browse/{result.get('key', '')}",
        }, ensure_ascii=False)

    async def update_issue(self, issue_key: str, summary: str = "", assignee: str = "",
                           priority: str = "", comment: str = "") -> str:
        """Обновить задачу (поля + опциональный комментарий)."""
        fields: Dict[str, Any] = {}
        if summary:
            fields["summary"] = summary
        if assignee:
            fields["assignee"] = {"accountId": assignee}
        if priority:
            fields["priority"] = {"name": priority}

        if fields:
            result = await self._api("PUT", f"/issue/{issue_key}", {"fields": fields})
            if result.get("error"):
                return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        if comment:
            await self.add_comment(issue_key, comment)

        return json.dumps({"success": True, "key": issue_key, "updated_fields": list(fields.keys())}, ensure_ascii=False)

    async def transition_issue(self, issue_key: str, transition_name: str) -> str:
        """Перевести задачу в другой статус (In Progress, Done, Review и т.д.)."""
        # Получаем доступные transitions
        result = await self._api("GET", f"/issue/{issue_key}/transitions")
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        transitions = result.get("transitions", [])
        target = None
        transition_lower = transition_name.lower()
        for t in transitions:
            if t["name"].lower() == transition_lower or t["to"]["name"].lower() == transition_lower:
                target = t
                break

        if not target:
            available = [f"{t['name']} → {t['to']['name']}" for t in transitions]
            return json.dumps({
                "success": False,
                "error": f"Transition '{transition_name}' not found",
                "available_transitions": available,
            }, ensure_ascii=False)

        result = await self._api("POST", f"/issue/{issue_key}/transitions", {"transition": {"id": target["id"]}})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "key": issue_key,
            "new_status": target["to"]["name"],
        }, ensure_ascii=False)

    async def add_comment(self, issue_key: str, body_text: str) -> str:
        """Добавить комментарий к задаче."""
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": body_text}]}],
            }
        }
        result = await self._api("POST", f"/issue/{issue_key}/comment", body)
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({"success": True, "key": issue_key, "comment_id": result.get("id", "")}, ensure_ascii=False)

    async def get_projects(self) -> str:
        """Список проектов."""
        result = await self._api("GET", "/project/search?maxResults=50")
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        projects = [
            {"key": p["key"], "name": p["name"], "type": p.get("projectTypeKey", "")}
            for p in result.get("values", [])
        ]
        return json.dumps({"success": True, "count": len(projects), "projects": projects}, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "jira_search_issues",
                "description": "Search Jira issues using JQL query. Example: 'project = PROJ AND status = \"In Progress\"'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string", "description": "JQL query string"},
                        "max_results": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["jql"],
                },
            },
            {
                "name": "jira_get_my_issues",
                "description": "Get current user's Jira issues, optionally filtered by status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Filter by status name (optional)"},
                    },
                },
            },
            {
                "name": "jira_create_issue",
                "description": "Create a new Jira issue (Task, Bug, Story, etc.).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project key (e.g. PROJ). Uses default if empty."},
                        "summary": {"type": "string", "description": "Issue title/summary"},
                        "description": {"type": "string", "description": "Issue description (optional)"},
                        "issue_type": {"type": "string", "description": "Issue type: Task, Bug, Story, Epic (default: Task)"},
                        "assignee": {"type": "string", "description": "Assignee account ID (optional)"},
                        "priority": {"type": "string", "description": "Priority: Highest, High, Medium, Low, Lowest (optional)"},
                    },
                    "required": ["summary"],
                },
            },
            {
                "name": "jira_update_issue",
                "description": "Update a Jira issue fields and optionally add a comment.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Issue key (e.g. PROJ-123)"},
                        "summary": {"type": "string", "description": "New summary (optional)"},
                        "assignee": {"type": "string", "description": "New assignee account ID (optional)"},
                        "priority": {"type": "string", "description": "New priority (optional)"},
                        "comment": {"type": "string", "description": "Comment to add (optional)"},
                    },
                    "required": ["issue_key"],
                },
            },
            {
                "name": "jira_transition_issue",
                "description": "Transition a Jira issue to a new status (e.g. In Progress, Done, Review).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Issue key (e.g. PROJ-123)"},
                        "transition_name": {"type": "string", "description": "Target status name (e.g. In Progress, Done)"},
                    },
                    "required": ["issue_key", "transition_name"],
                },
            },
            {
                "name": "jira_add_comment",
                "description": "Add a comment to a Jira issue.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Issue key (e.g. PROJ-123)"},
                        "body_text": {"type": "string", "description": "Comment text"},
                    },
                    "required": ["issue_key", "body_text"],
                },
            },
            {
                "name": "jira_get_projects",
                "description": "List all Jira projects accessible to the user.",
                "parameters": {"type": "object", "properties": {}},
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Jira not configured. Add domain, email, api_token in integrations."}, ensure_ascii=False)

        dispatch = {
            "jira_search_issues": lambda: self.search_issues(
                jql=str(kwargs.get("jql", "")),
                max_results=int(kwargs.get("max_results", 20)),
            ),
            "jira_get_my_issues": lambda: self.get_my_issues(
                status=str(kwargs.get("status", "")),
            ),
            "jira_create_issue": lambda: self.create_issue(
                project=str(kwargs.get("project", "")),
                summary=str(kwargs.get("summary", "")),
                description=str(kwargs.get("description", "")),
                issue_type=str(kwargs.get("issue_type", "Task")),
                assignee=str(kwargs.get("assignee", "")),
                priority=str(kwargs.get("priority", "")),
            ),
            "jira_update_issue": lambda: self.update_issue(
                issue_key=str(kwargs.get("issue_key", "")),
                summary=str(kwargs.get("summary", "")),
                assignee=str(kwargs.get("assignee", "")),
                priority=str(kwargs.get("priority", "")),
                comment=str(kwargs.get("comment", "")),
            ),
            "jira_transition_issue": lambda: self.transition_issue(
                issue_key=str(kwargs.get("issue_key", "")),
                transition_name=str(kwargs.get("transition_name", "")),
            ),
            "jira_add_comment": lambda: self.add_comment(
                issue_key=str(kwargs.get("issue_key", "")),
                body_text=str(kwargs.get("body_text", "")),
            ),
            "jira_get_projects": lambda: self.get_projects(),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown Jira tool: {tool_name}"}, ensure_ascii=False)
