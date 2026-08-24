"""Shared pytest setup for unit tests.

Эти тесты не требуют PG/Neo4j/Qdrant/Redis. Только импорты модулей W1-W4
и pure-Python логика.

Что делает этот conftest:
1. Добавляет `tessent_brain/` в sys.path → `from backend.* import ...`.
2. Если PyJWT/cryptography сломаны в текущем окружении (типичная
   проблема dev-контейнеров без `_cffi_backend`), подсовывает minimal
   jwt-stub в sys.modules, чтобы каскадные импорты `backend.api.middleware`
   не падали при collection. В CI этот код no-op, потому что `import jwt`
   там работает нормально.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

# tessent_brain/ в sys.path → импорты вида `from backend....` работают.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _try_import_jwt() -> None:
    try:
        import jwt
    except BaseException:
        # PyJWT/cryptography сломан → подсовываем stub.
        # Тесты, которые реально тестируют JWT-логику (test_tenant_resolver),
        # сами строят JWT base64-руками без библиотеки.
        stub = ModuleType("jwt")

        class InvalidTokenError(Exception):
            pass

        class ExpiredSignatureError(InvalidTokenError):
            pass

        class InvalidSignatureError(InvalidTokenError):
            pass

        def _decode(token: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            # tenant_resolver._decode_jwt_claims_unsafe использует
            # options={"verify_signature": False}, тогда ему достаточно
            # base64-декода. Делаем минимально: парсим payload вручную.
            import base64
            import json
            try:
                _h, body, _s = token.split(".", 2)
                pad = "=" * (-len(body) % 4)
                return json.loads(base64.urlsafe_b64decode(body + pad))
            except Exception as exc:
                raise InvalidTokenError(str(exc)) from exc

        def _encode(payload: dict[str, Any], *args: Any, **kwargs: Any) -> str:
            import base64
            import json
            body = base64.urlsafe_b64encode(
                json.dumps(payload).encode()
            ).rstrip(b"=").decode("ascii")
            return f"e30.{body}."

        stub.InvalidTokenError = InvalidTokenError
        stub.ExpiredSignatureError = ExpiredSignatureError
        stub.InvalidSignatureError = InvalidSignatureError
        stub.decode = _decode
        stub.encode = _encode
        sys.modules["jwt"] = stub


_try_import_jwt()
