# -*- coding: utf-8 -*-
"""
Linear Integration — issues, projects, cycles.

Использует Linear GraphQL API.
Аутентификация: API Key (Bearer).

Credentials (из user_integrations):
    - api_key: Linear API key (lin_api_...)
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

LINEAR_API = "https://api.linear.app/graphql"


@register_integration
class LinearIntegration(BaseIntegration):
    integration_id = "linear"
    integration_name = "Linear"
    category = "tasks"
    keywords = [
        # Русский
        "linear", "линеар", "issue", "тикет linear",
        "создай issue", "мои тикеты linear",
        "цикл", "cycle", "проект linear",
        # English
        "linear issue", "linear project", "linear cycle",
        "create issue", "my issues", "assign issue",
        "backlog", "in progress", "sprint linear",
    ]

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": self.get_key("api_key"),
            "Content-Type": "application/json",
        }

    async def _graphql(self, query: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(LINEAR_API, headers=self._headers(), json=body)
            if resp.status_code >= 400:
                return {"error": True, "status": resp.status_code, "message": resp.text[:500]}
            data = resp.json()
            if data.get("errors"):
                return {"error": True, "message": data["errors"][0].get("message", str(data["errors"]))}
            return data.get("data", {})

    # ── Tool implementations ──────────────────────────────────────

    async def search_issues(self, query: str, limit: int = 20) -> str:
        """Поиск issues."""
        result = await self._graphql("""
            query($term: String!, $first: Int) {
                issueSearch(term: $term, first: $first) {
                    nodes {
                        id identifier title priority priorityLabel
                        state { name color }
                        assignee { name email }
                        team { name }
                        createdAt updatedAt
                        url
                    }
                }
            }
        """, {"term": query, "first": min(limit, 50)})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        issues = []
        for node in result.get("issueSearch", {}).get("nodes", []):
            issues.append({
                "id": node.get("id", ""),
                "identifier": node.get("identifier", ""),
                "title": node.get("title", ""),
                "priority": node.get("priorityLabel", ""),
                "state": node.get("state", {}).get("name", ""),
                "assignee": (node.get("assignee") or {}).get("name", ""),
                "team": node.get("team", {}).get("name", ""),
                "url": node.get("url", ""),
                "updated": node.get("updatedAt", ""),
            })

        return json.dumps({"success": True, "count": len(issues), "issues": issues}, ensure_ascii=False)

    async def my_issues(self, limit: int = 20) -> str:
        """Мои assigned issues."""
        result = await self._graphql("""
            query($first: Int) {
                viewer {
                    assignedIssues(first: $first, orderBy: updatedAt) {
                        nodes {
                            id identifier title priority priorityLabel
                            state { name }
                            team { name }
                            dueDate
                            url
                            updatedAt
                        }
                    }
                }
            }
        """, {"first": min(limit, 50)})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        issues = []
        for node in result.get("viewer", {}).get("assignedIssues", {}).get("nodes", []):
            issues.append({
                "identifier": node.get("identifier", ""),
                "title": node.get("title", ""),
                "priority": node.get("priorityLabel", ""),
                "state": node.get("state", {}).get("name", ""),
                "team": node.get("team", {}).get("name", ""),
                "due_date": node.get("dueDate", ""),
                "url": node.get("url", ""),
            })

        return json.dumps({"success": True, "count": len(issues), "issues": issues}, ensure_ascii=False)

    async def create_issue(self, title: str, description: str = "",
                           team_id: str = "", priority: int = 0) -> str:
        """Создать issue."""
        # Если team_id не указан — берём первую команду
        if not team_id:
            teams = await self._graphql("query { teams { nodes { id name } } }")
            team_nodes = teams.get("teams", {}).get("nodes", [])
            if team_nodes:
                team_id = team_nodes[0]["id"]
            else:
                return json.dumps({"success": False, "error": "No teams found in Linear"}, ensure_ascii=False)

        input_data: Dict[str, Any] = {
            "title": title,
            "teamId": team_id,
        }
        if description:
            input_data["description"] = description
        if priority:
            input_data["priority"] = priority

        result = await self._graphql("""
            mutation($input: IssueCreateInput!) {
                issueCreate(input: $input) {
                    success
                    issue {
                        id identifier title url
                        state { name }
                        team { name }
                    }
                }
            }
        """, {"input": input_data})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        issue = result.get("issueCreate", {}).get("issue", {})
        return json.dumps({
            "success": True,
            "identifier": issue.get("identifier", ""),
            "title": issue.get("title", ""),
            "url": issue.get("url", ""),
            "team": issue.get("team", {}).get("name", ""),
        }, ensure_ascii=False)

    async def list_projects(self, limit: int = 20) -> str:
        """Список проектов."""
        result = await self._graphql("""
            query($first: Int) {
                projects(first: $first, orderBy: updatedAt) {
                    nodes {
                        id name description state
                        progress
                        startDate targetDate
                        url
                        teams { nodes { name } }
                    }
                }
            }
        """, {"first": min(limit, 50)})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        projects = []
        for node in result.get("projects", {}).get("nodes", []):
            teams = [t.get("name", "") for t in node.get("teams", {}).get("nodes", [])]
            projects.append({
                "id": node.get("id", ""),
                "name": node.get("name", ""),
                "state": node.get("state", ""),
                "progress": node.get("progress", 0),
                "start_date": node.get("startDate", ""),
                "target_date": node.get("targetDate", ""),
                "teams": teams,
                "url": node.get("url", ""),
            })

        return json.dumps({"success": True, "count": len(projects), "projects": projects}, ensure_ascii=False)

    async def current_cycle(self) -> str:
        """Текущий активный цикл (sprint)."""
        result = await self._graphql("""
            query {
                cycles(filter: { isActive: { eq: true } }, first: 5) {
                    nodes {
                        id number name
                        startsAt endsAt
                        progress
                        team { name }
                        issues { nodes { identifier title state { name } } }
                    }
                }
            }
        """)
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        cycles = []
        for node in result.get("cycles", {}).get("nodes", []):
            issues = []
            for iss in node.get("issues", {}).get("nodes", [])[:20]:
                issues.append({
                    "identifier": iss.get("identifier", ""),
                    "title": iss.get("title", ""),
                    "state": iss.get("state", {}).get("name", ""),
                })
            cycles.append({
                "number": node.get("number", 0),
                "name": node.get("name", ""),
                "team": node.get("team", {}).get("name", ""),
                "starts_at": node.get("startsAt", ""),
                "ends_at": node.get("endsAt", ""),
                "progress": node.get("progress", 0),
                "issues_count": len(issues),
                "issues": issues,
            })

        return json.dumps({"success": True, "cycles": cycles}, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "linear_search_issues",
                "description": "Search Linear issues by text. Use for: find issue, search tickets, найди тикет в linear.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "linear_my_issues",
                "description": "List issues assigned to me. Use for: my issues, мои задачи в linear, what's assigned.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                },
            },
            {
                "name": "linear_create_issue",
                "description": "Create a new Linear issue. Use for: create issue, new ticket, создай тикет в linear.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Issue title"},
                        "description": {"type": "string", "description": "Issue description (markdown)"},
                        "team_id": {"type": "string", "description": "Team ID (optional, uses first team)"},
                        "priority": {"type": "integer", "description": "0=none, 1=urgent, 2=high, 3=medium, 4=low"},
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "linear_projects",
                "description": "List Linear projects. Use for: show projects, проекты linear, project status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                },
            },
            {
                "name": "linear_current_cycle",
                "description": "Get current active cycle (sprint) with issues. Use for: current sprint, текущий цикл, what's in sprint.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Linear not configured. Add API key in integrations."}, ensure_ascii=False)

        dispatch = {
            "linear_search_issues": lambda: self.search_issues(
                query=str(kwargs.get("query", "")),
                limit=int(kwargs.get("limit", 20)),
            ),
            "linear_my_issues": lambda: self.my_issues(limit=int(kwargs.get("limit", 20))),
            "linear_create_issue": lambda: self.create_issue(
                title=str(kwargs.get("title", "")),
                description=str(kwargs.get("description", "")),
                team_id=str(kwargs.get("team_id", "")),
                priority=int(kwargs.get("priority", 0)),
            ),
            "linear_projects": lambda: self.list_projects(limit=int(kwargs.get("limit", 20))),
            "linear_current_cycle": lambda: self.current_cycle(),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown Linear tool: {tool_name}"}, ensure_ascii=False)
