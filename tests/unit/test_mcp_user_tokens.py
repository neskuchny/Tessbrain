# -*- coding: utf-8 -*-
"""Персональные токены подключения (MCP): выпуск, отзыв, изоляция аккаунтов.

Разрыв, который это закрывает: токен можно было получить ТОЛЬКО командой на
сервере (`python -m mcp_token_store mint <user_id>`), то есть подключить
Tessbrain к Claude/Cursor мог лишь администратор — и то, зная чужой UUID. А сам
hosted-эндпоинт /api/v1/mcp принимал только ОДИН общий env-токен, из-за чего
любой подключившийся работал бы от имени TESSENT_USER_ID.

Проверяем: стор изолирует аккаунты (в т.ч. на отзыве по префиксу), секрет
наружу не течёт, и /api/v1/mcp исполняет вызов от имени владельца токена.
"""
import importlib
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_TOKENS_FILE", str(tmp_path / "mcp_tokens.json"))
    import mcp_token_store
    return importlib.reload(mcp_token_store)


# ── Стор ────────────────────────────────────────────────────────────────────

def test_mint_validate_roundtrip(store):
    tok = store.mint("user-a", label="Claude")
    assert tok.startswith("tess_mcp_")
    assert store.validate(tok) == "user-a"
    assert store.validate("tess_mcp_nope") is None
    assert store.validate("") is None
    assert store.validate("совсем не токен") is None


def test_list_never_leaks_the_secret(store):
    tok = store.mint("user-a")
    rows = store.list_for_user("user-a")
    assert len(rows) == 1
    shown = rows[0]["token"]
    assert shown.endswith("…") and shown[:-1] in tok
    assert tok not in shown, "секрет утёк в список"
    # чужой аккаунт не видит ничего
    assert store.list_for_user("user-b") == []


def test_revoke_by_prefix_is_scoped_to_owner(store):
    a = store.mint("user-a")
    b = store.mint("user-b")
    # владелец находит свой по префиксу
    assert store.find_by_prefix("user-a", a[:16]) == a
    # чужой по тому же префиксу — нет (иначе токен гасился бы угадыванием)
    assert store.find_by_prefix("user-b", a[:16]) is None
    # слишком короткий префикс не принимается
    assert store.find_by_prefix("user-a", "tess_mcp_") is None
    assert store.revoke(a) is True
    assert store.validate(a) is None
    assert store.validate(b) == "user-b", "отзыв задел чужой токен"


# ── Эндпоинт /api/v1/mcp ────────────────────────────────────────────────────

@pytest.fixture()
def http(store, monkeypatch):
    monkeypatch.setenv("TESSENT_API_TOKEN", "svc-token")
    monkeypatch.setenv("TESSENT_MCP_TOKEN", "static-admin-token")
    monkeypatch.setenv("TESSENT_USER_ID", "env-user")
    from backend.api.routes import mcp_http
    return importlib.reload(mcp_http)


def test_personal_token_resolves_to_its_owner(http, store):
    tok = store.mint("user-a")
    assert http._resolve_user(tok) == "user-a"


def test_static_env_token_still_works(http):
    assert http._resolve_user("static-admin-token") == "env-user"


def test_unknown_and_empty_tokens_are_rejected(http):
    assert http._resolve_user("tess_mcp_deadbeef") is None
    assert http._resolve_user("") is None
    assert http._resolve_user("Bearer") is None


def test_revoked_token_stops_working(http, store):
    tok = store.mint("user-a")
    assert http._resolve_user(tok) == "user-a"
    store.revoke(tok)
    assert http._resolve_user(tok) is None


def test_call_runs_as_the_token_owner(http, store, monkeypatch):
    """Ключевое: вызов исполняется от имени ВЛАДЕЛЬЦА токена, а не от env-юзера.

    Без этого один сервер отдавал бы всем подключившимся данные TESSENT_USER_ID.
    """
    import asyncio

    import mcp_server

    tok = store.mint("user-a")
    seen = {}

    def _fake_handle(msg):
        seen["user"] = mcp_server._effective_user()
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    monkeypatch.setattr(mcp_server, "handle_message", _fake_handle)
    monkeypatch.setattr(mcp_server, "USER_ID", "env-user")

    class _Req:
        headers = {"authorization": f"Bearer {tok}"}

    handler = http.mcp_endpoint.fn      # разворачиваем декоратор Litestar
    asyncio.run(handler(_Req(), data={"jsonrpc": "2.0", "id": 1,
                                      "method": "tools/list"}))
    assert seen["user"] == "user-a", \
        f"вызов ушёл от чужого имени: {seen.get('user')}"
    # контекст не протёк за пределы запроса
    assert mcp_server._req_user.get() is None


def test_tools_pass_request_user_not_env(monkeypatch):
    """t_compose/t_generate_report клали в тело env-USER_ID, игнорируя
    пользователя запроса — отчёт генерировался бы по чужой компании."""
    import mcp_server

    monkeypatch.setattr(mcp_server, "USER_ID", "env-user")
    bodies = []

    def _fake_api(method, path, *, params=None, body=None, timeout=300.0):
        bodies.append(body or {})
        return {"status": "success", "report": {"content_text": "ok"},
                "artifact_text": "ok"}

    monkeypatch.setattr(mcp_server, "_api", _fake_api)
    ctx = mcp_server.set_request_user("user-a")
    try:
        mcp_server.t_compose({"request": "сделай"})
        mcp_server.t_generate_report({"report_type": "project_summary"})
    finally:
        mcp_server._req_user.reset(ctx)

    assert bodies and all(b.get("user_id") == "user-a" for b in bodies), bodies
