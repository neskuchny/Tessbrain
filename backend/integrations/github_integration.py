# -*- coding: utf-8 -*-
"""
GitHub Integration — PRs, issues, CI/CD статусы, поиск кода.

Использует GitHub REST API v3.
Аутентификация: Personal Access Token (Fine-grained).

Credentials (из user_integrations — пока нет провайдера, используем API-ключ через env):
    Добавляем провайдер "github" в INTEGRATION_PROVIDERS.
    - api_token: ghp_... (Personal Access Token)
    - default_owner: github-org-or-user (опционально)
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


@register_integration
class GitHubIntegration(BaseIntegration):
    integration_id = "github"
    integration_name = "GitHub"
    category = "code"
    keywords = [
        # Русский
        "github", "гитхаб", "пул реквест",
        "issue", "баг", "создай issue",
        "ci", "cd", "пайплайн", "деплой",
        "коммит", "ветка", "бранч", "мерж",
        "репозитори", "код",
        # English
        "pull request", "pr", "merge request",
        "github issue", "github pr", "ci status",
        "pipeline", "workflow", "deploy", "release",
        "commit", "branch", "merge", "repo",
        "code search", "github search",
        # Транслит
        "githab", "pul rekvest",
    ]

    def _headers(self) -> Dict[str, str]:
        token = self.get_key("api_token")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _api(self, method: str, path: str, body: Optional[Dict] = None,
                   params: Optional[Dict] = None) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method, f"{GITHUB_API}{path}",
                headers=self._headers(),
                json=body,
                params=params,
            )
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                    msg = err.get("message", str(err))
                except Exception:
                    msg = resp.text[:500]
                logger.warning(f"[GitHub] {method} {path} → {resp.status_code}: {msg}")
                return {"error": True, "status": resp.status_code, "message": msg}
            if resp.status_code == 204:
                return {"success": True}
            return resp.json()

    def _parse_repo(self, repo: str) -> str:
        """Парсит repo из формата 'owner/repo' или добавляет default_owner."""
        if "/" in repo:
            return repo
        owner = self.get_key("default_owner")
        if owner:
            return f"{owner}/{repo}"
        return repo

    # ── Tool implementations ──────────────────────────────────────

    async def list_prs(self, repo: str, state: str = "open") -> str:
        full_repo = self._parse_repo(repo)
        result = await self._api("GET", f"/repos/{full_repo}/pulls", params={"state": state, "per_page": 30})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        prs = []
        for pr in result:
            prs.append({
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "author": pr.get("user", {}).get("login", ""),
                "created_at": pr.get("created_at", ""),
                "updated_at": pr.get("updated_at", ""),
                "url": pr.get("html_url", ""),
                "draft": pr.get("draft", False),
                "mergeable_state": pr.get("mergeable_state", ""),
            })
        return json.dumps({"success": True, "repo": full_repo, "count": len(prs), "pull_requests": prs}, ensure_ascii=False)

    async def get_pr(self, repo: str, pr_number: int) -> str:
        full_repo = self._parse_repo(repo)
        result = await self._api("GET", f"/repos/{full_repo}/pulls/{pr_number}")
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "number": result["number"],
            "title": result["title"],
            "body": (result.get("body") or "")[:2000],
            "state": result["state"],
            "author": result.get("user", {}).get("login", ""),
            "merged": result.get("merged", False),
            "mergeable": result.get("mergeable"),
            "additions": result.get("additions", 0),
            "deletions": result.get("deletions", 0),
            "changed_files": result.get("changed_files", 0),
            "url": result.get("html_url", ""),
        }, ensure_ascii=False)

    async def list_issues(self, repo: str, state: str = "open", labels: str = "") -> str:
        full_repo = self._parse_repo(repo)
        params: Dict[str, Any] = {"state": state, "per_page": 30}
        if labels:
            params["labels"] = labels
        result = await self._api("GET", f"/repos/{full_repo}/issues", params=params)
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        issues = []
        for issue in result:
            if issue.get("pull_request"):  # Skip PRs (API returns them in issues)
                continue
            issues.append({
                "number": issue["number"],
                "title": issue["title"],
                "state": issue["state"],
                "author": issue.get("user", {}).get("login", ""),
                "assignees": [a["login"] for a in issue.get("assignees", [])],
                "labels": [l["name"] for l in issue.get("labels", [])],
                "created_at": issue.get("created_at", ""),
                "url": issue.get("html_url", ""),
            })
        return json.dumps({"success": True, "repo": full_repo, "count": len(issues), "issues": issues}, ensure_ascii=False)

    async def create_issue(self, repo: str, title: str, body: str = "",
                           labels: str = "", assignees: str = "") -> str:
        full_repo = self._parse_repo(repo)
        data: Dict[str, Any] = {"title": title}
        if body:
            data["body"] = body
        if labels:
            data["labels"] = [l.strip() for l in labels.split(",")]
        if assignees:
            data["assignees"] = [a.strip() for a in assignees.split(",")]

        result = await self._api("POST", f"/repos/{full_repo}/issues", data)
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "number": result.get("number"),
            "title": title,
            "url": result.get("html_url", ""),
        }, ensure_ascii=False)

    async def get_ci_status(self, repo: str, ref: str = "main") -> str:
        """Получить статус CI для ветки/коммита."""
        full_repo = self._parse_repo(repo)
        # Получаем workflow runs
        result = await self._api("GET", f"/repos/{full_repo}/actions/runs", params={"branch": ref, "per_page": 5})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        runs = []
        for run in result.get("workflow_runs", []):
            runs.append({
                "id": run["id"],
                "name": run.get("name", ""),
                "status": run.get("status", ""),
                "conclusion": run.get("conclusion", ""),
                "branch": run.get("head_branch", ""),
                "created_at": run.get("created_at", ""),
                "url": run.get("html_url", ""),
            })
        return json.dumps({"success": True, "repo": full_repo, "ref": ref, "runs": runs}, ensure_ascii=False)

    async def search_code(self, query: str) -> str:
        """Поиск по коду в GitHub."""
        result = await self._api("GET", "/search/code", params={"q": query, "per_page": 20})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        items = []
        for item in result.get("items", []):
            items.append({
                "name": item.get("name", ""),
                "path": item.get("path", ""),
                "repo": item.get("repository", {}).get("full_name", ""),
                "url": item.get("html_url", ""),
                "score": item.get("score", 0),
            })
        return json.dumps({"success": True, "total": result.get("total_count", 0), "results": items}, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "github_list_prs",
                "description": "List pull requests for a GitHub repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository (owner/repo or just repo name)"},
                        "state": {"type": "string", "description": "State filter: open, closed, all (default: open)"},
                    },
                    "required": ["repo"],
                },
            },
            {
                "name": "github_get_pr",
                "description": "Get details of a specific pull request.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository (owner/repo)"},
                        "pr_number": {"type": "integer", "description": "PR number"},
                    },
                    "required": ["repo", "pr_number"],
                },
            },
            {
                "name": "github_list_issues",
                "description": "List issues for a GitHub repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository (owner/repo)"},
                        "state": {"type": "string", "description": "State: open, closed, all (default: open)"},
                        "labels": {"type": "string", "description": "Filter by labels, comma-separated (optional)"},
                    },
                    "required": ["repo"],
                },
            },
            {
                "name": "github_create_issue",
                "description": "Create a new issue in a GitHub repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository (owner/repo)"},
                        "title": {"type": "string", "description": "Issue title"},
                        "body": {"type": "string", "description": "Issue body/description (optional)"},
                        "labels": {"type": "string", "description": "Labels, comma-separated (optional)"},
                        "assignees": {"type": "string", "description": "Assignees, comma-separated (optional)"},
                    },
                    "required": ["repo", "title"],
                },
            },
            {
                "name": "github_get_ci_status",
                "description": "Get CI/CD workflow run status for a branch.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository (owner/repo)"},
                        "ref": {"type": "string", "description": "Branch or commit SHA (default: main)"},
                    },
                    "required": ["repo"],
                },
            },
            {
                "name": "github_search_code",
                "description": "Search code across GitHub repositories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (e.g. 'className repo:owner/repo')"},
                    },
                    "required": ["query"],
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "GitHub not configured. Add api_token in integrations."}, ensure_ascii=False)

        dispatch = {
            "github_list_prs": lambda: self.list_prs(
                repo=str(kwargs.get("repo", "")),
                state=str(kwargs.get("state", "open")),
            ),
            "github_get_pr": lambda: self.get_pr(
                repo=str(kwargs.get("repo", "")),
                pr_number=int(kwargs.get("pr_number", 0)),
            ),
            "github_list_issues": lambda: self.list_issues(
                repo=str(kwargs.get("repo", "")),
                state=str(kwargs.get("state", "open")),
                labels=str(kwargs.get("labels", "")),
            ),
            "github_create_issue": lambda: self.create_issue(
                repo=str(kwargs.get("repo", "")),
                title=str(kwargs.get("title", "")),
                body=str(kwargs.get("body", "")),
                labels=str(kwargs.get("labels", "")),
                assignees=str(kwargs.get("assignees", "")),
            ),
            "github_get_ci_status": lambda: self.get_ci_status(
                repo=str(kwargs.get("repo", "")),
                ref=str(kwargs.get("ref", "main")),
            ),
            "github_search_code": lambda: self.search_code(query=str(kwargs.get("query", ""))),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown GitHub tool: {tool_name}"}, ensure_ascii=False)
