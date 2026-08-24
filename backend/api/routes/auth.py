# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Authentication Routes
API для аутентификации через Supabase Auth
"""
import logging
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel, EmailStr

try:
    # supabase-py v2 uses `supabase_auth` package for GoTrue
    from supabase_auth.errors import AuthApiError
except Exception:  # pragma: no cover
    # Fallback to keep server bootable even if dependency layout changes
    class AuthApiError(Exception):
        """Fallback AuthApiError."""

        def __init__(self, message: str = "Auth error"):
            self.message = message
            super().__init__(message)

logger = logging.getLogger(__name__)

# JWT Configuration (must match backend.api.middleware.auth_middleware).
# Никаких hardcoded дефолтов — секреты берутся только из окружения.
JWT_SECRET = (
    os.environ.get("JWT_SECRET")
    or os.environ.get("SUPABASE_JWT_SECRET")
    or os.environ.get("SECRET_KEY")
    # JWT_SECRET_KEY — имя из pydantic-настроек (settings.jwt_secret_key).
    # Без него login подписывал токен ПУСТЫМ ключом (env JWT_SECRET не задан),
    # а verify_user_token проверял по settings.jwt_secret_key → mismatch → 403.
    or os.environ.get("JWT_SECRET_KEY")
    or ""
)
LEGACY_JWT_SECRET = os.environ.get("LEGACY_JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

if not JWT_SECRET:
    logger.warning(
        "JWT_SECRET is not configured; signed tokens will fail verification. "
        "Set JWT_SECRET / SUPABASE_JWT_SECRET / SECRET_KEY env var."
    )


def _decode_jwt(token: str) -> dict[str, Any]:
    """Decode JWT with current secret, with a legacy fallback for backward compatibility."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidSignatureError:
        if LEGACY_JWT_SECRET and LEGACY_JWT_SECRET != JWT_SECRET:
            return jwt.decode(token, LEGACY_JWT_SECRET, algorithms=[JWT_ALGORITHM])
        raise


# === Models ===

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None


class UserResponse(BaseModel):
    id: str
    email: str
    name: Optional[str] = None


class AuthResponse(BaseModel):
    success: bool = True
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    user: UserResponse


class RefreshRequest(BaseModel):
    refresh_token: str


# === Helper Functions ===

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Создать JWT токен"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=JWT_EXPIRATION_HOURS))
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_supabase_auth_client():
    """Получить Supabase Auth клиент"""
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
        if url and key:
            return create_client(url, key)
        else:
            logger.error(f"Missing Supabase env vars: URL={bool(url)}, KEY={bool(key)}")
    except Exception as e:
        logger.error(f"Failed to create Supabase client: {e}")
    return None


async def ensure_user(user_id: str, email: str, name: Optional[str] = None):
    """Убедиться что пользователь есть в public.users"""
    try:
        from backend.db.supabase_client import get_supabase_client
        supabase = get_supabase_client()

        # Проверяем существует ли пользователь
        existing = await supabase._request("GET", "/rest/v1/users", params={
            "id": f"eq.{user_id}",
            "select": "id"
        })

        if not existing:
            # Создаём пользователя. Схема public.users в разных проектах
            # отличается: у живой БД MeetFlow НЕТ колонок name/created_at
            # (PGRST204 «Could not find the 'name' column») → создание падало
            # 400-кой (не фатально, но юзер в таблице не заводился). Пробуем
            # полный набор; при ошибке про несуществующую колонку — повтор
            # минимальным (id+email есть всегда).
            full = {
                "id": user_id,
                "email": email,
                "name": name or email.split("@")[0],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                await supabase._request("POST", "/rest/v1/users", json_data=full)
                logger.info(f"Created user {email} in public.users")
            except Exception as col_err:
                # httpx кидает «Client error '400 Bad Request' for url …» БЕЗ
                # текста PGRST204/column (он только в r.text) — старая проверка
                # по str(e) не матчилась и ретрай не срабатывал. Читаем тело.
                body = ""
                resp = getattr(col_err, "response", None)
                if resp is not None:
                    try:
                        body = resp.text or ""
                    except Exception:
                        body = ""
                msg = (str(col_err) + " " + body).lower()
                if "409" in msg or "duplicate" in msg or "23505" in msg:
                    # email уже есть (строка заведена ранее/другим id) —
                    # юзер существует, это не ошибка
                    logger.info(f"User {email} already in public.users (email exists)")
                elif ("could not find" in msg or "pgrst204" in msg
                        or "column" in msg or "400" in msg):
                    try:
                        await supabase._request("POST", "/rest/v1/users",
                                                json_data={"id": user_id, "email": email})
                        logger.info(f"Created user {email} in public.users (id+email)")
                    except Exception as e2:
                        m2 = str(e2).lower()
                        r2 = getattr(e2, "response", None)
                        if r2 is not None:
                            try:
                                m2 += " " + (r2.text or "").lower()
                            except Exception:
                                pass
                        if "409" in m2 or "duplicate" in m2 or "23505" in m2:
                            logger.info(f"User {email} already in public.users (email exists)")
                        else:
                            raise
                else:
                    raise
    except Exception as e:
        logger.warning(f"Could not ensure user in public.users: {e}")


# === Route Handlers ===

@post("/login")
async def login(data: LoginRequest) -> AuthResponse:
    """Аутентификация пользователя по email и паролю.

    Перед проверкой пароля смотрим счётчик неудачных попыток по этой
    учётной записи (login_guard). Общий rate-limit считает по адресу, а
    адрес при переборе меняется дёшево — счётчик на аккаунт закрывает
    именно это. Блокировки нет, только временная задержка: иначе чужую
    учётку можно было бы запереть, долбя её заведомо неверным паролем.
    """
    from backend.core.auth import login_guard

    allowed, retry_after = await login_guard.check(data.email)
    if not allowed:
        logger.info("login: попытка придержана (слишком много неудач подряд)")
        raise HTTPException(
            status_code=429,
            detail=login_guard.retry_message(retry_after),
            headers={"Retry-After": str(retry_after)},
        )

    logger.info(f"Attempting login for user: {data.email}")
    try:
        client = get_supabase_auth_client()
        if not client:
            logger.error("Supabase client is None. Check env vars.")
            raise HTTPException(status_code=500, detail="Auth service unavailable (client init failed)")

        # Используем sign_in_with_password для Supabase GoTrue API
        logger.debug("Calling client.auth.sign_in_with_password...")
        try:
            auth_response = client.auth.sign_in_with_password({
                "email": data.email,
                "password": data.password,
            })
        except AuthApiError:
            # Пробрасываем AuthApiError как есть - он будет обработан ниже
            raise
        except Exception as auth_exc:
            logger.error(f"Error during sign_in_with_password: {auth_exc}")
            # Если это похоже на ошибку авторизации, возвращаем 401
            if "Invalid login credentials" in str(auth_exc) or "400" in str(auth_exc):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            raise auth_exc

        if not auth_response or not auth_response.session:
            logger.warning("No session returned from Supabase Auth")
            raise HTTPException(status_code=401, detail="Invalid credentials")

        user_id = auth_response.user.id
        email = auth_response.user.email
        name = auth_response.user.user_metadata.get("name", email)

        # Ensure user exists in our public.users table
        await ensure_user(user_id, email, name)

        # Generate JWT token for our backend
        access_token = create_access_token(
            data={"sub": user_id, "email": email, "name": name}
        )
        refresh_token = auth_response.session.refresh_token

        logger.info(f"User {email} logged in successfully.")
        await login_guard.record_success(data.email)
        return AuthResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=UserResponse(id=user_id, email=email, name=name),
        )
    except AuthApiError as e:
        # Invalid credentials should be 401, not 500
        msg = getattr(e, "message", None) or str(e) or "Invalid credentials"
        logger.info(f"Supabase Auth login failed for {data.email}: {msg}")
        await login_guard.record_failure(data.email)
        raise HTTPException(status_code=401, detail=msg)
    except HTTPException as e:
        # Неверный пароль учитываем — именно эти попытки и считает guard.
        # Ошибки конфигурации (500) и наш собственный 429 в счётчик не идут:
        # иначе сломанный Supabase «запирал» бы всех подряд, а придержанная
        # попытка продлевала бы себе срок.
        if getattr(e, "status_code", None) == 401:
            await login_guard.record_failure(data.email)
        raise
    except Exception as e:
        error_trace = traceback.format_exc()
        logger.error(f"Login error: {e}\n{error_trace}")
        # Внутренняя ошибка наружу текстом не уходит — по ней видно
        # устройство системы (какая библиотека, какое поле, какой адрес).
        raise HTTPException(status_code=500, detail="Internal server error")


@post("/register")
async def register(data: RegisterRequest) -> AuthResponse:
    """Регистрация нового пользователя."""
    try:
        client = get_supabase_auth_client()
        if not client:
            raise HTTPException(status_code=500, detail="Auth service unavailable")

        # Регистрация через Supabase Auth
        auth_response = client.auth.sign_up({
            "email": data.email,
            "password": data.password,
            "options": {
                "data": {"name": data.name or data.email.split("@")[0]}
            }
        })

        if not auth_response or not auth_response.user:
            raise HTTPException(status_code=400, detail="Registration failed")

        user_id = auth_response.user.id
        email = auth_response.user.email
        name = data.name or email.split("@")[0]

        # Ensure user exists in our public.users table
        await ensure_user(user_id, email, name)

        # Generate JWT token
        access_token = create_access_token(
            data={"sub": user_id, "email": email, "name": name}
        )

        refresh_token = None
        if auth_response.session:
            refresh_token = auth_response.session.refresh_token

        logger.info(f"User {email} registered successfully.")
        return AuthResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=UserResponse(id=user_id, email=email, name=name),
        )
    except AuthApiError as e:
        msg = getattr(e, "message", None) or str(e) or "Registration failed"
        logger.info(f"Supabase Auth registration failed for {data.email}: {msg}")
        raise HTTPException(status_code=400, detail=msg)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@post("/refresh")
async def refresh_token(data: RefreshRequest) -> AuthResponse:
    """Обновить токен доступа."""
    try:
        client = get_supabase_auth_client()
        if not client:
            raise HTTPException(status_code=500, detail="Auth service unavailable")

        auth_response = client.auth.refresh_session(data.refresh_token)

        if not auth_response or not auth_response.session:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        user_id = auth_response.user.id
        email = auth_response.user.email
        name = auth_response.user.user_metadata.get("name", email)

        access_token = create_access_token(
            data={"sub": user_id, "email": email, "name": name}
        )

        return AuthResponse(
            access_token=access_token,
            refresh_token=auth_response.session.refresh_token,
            token_type="bearer",
            user=UserResponse(id=user_id, email=email, name=name),
        )
    except AuthApiError as e:
        msg = getattr(e, "message", None) or str(e) or "Invalid refresh token"
        logger.info(f"Supabase Auth refresh failed: {msg}")
        raise HTTPException(status_code=401, detail=msg)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Refresh error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@post("/logout")
async def logout(
    authorization: Optional[str] = Parameter(header="Authorization", default=None)
) -> dict[str, Any]:
    """Выход из системы."""
    try:
        client = get_supabase_auth_client()
        if client:
            client.auth.sign_out()
        return {"status": "success", "message": "Logged out successfully"}
    except Exception as e:
        logger.error(f"Logout error: {e}")
        return {"status": "success", "message": "Logged out"}


@get("/me")
async def get_current_user(
    authorization: Optional[str] = Parameter(header="Authorization", default=None)
) -> dict[str, Any]:
    """Получить информацию о текущем пользователе."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.split(" ")[1]

    try:
        payload = _decode_jwt(token)
        return {
            "status": "success",
            "user": {
                "id": payload.get("sub"),
                "email": payload.get("email"),
                "name": payload.get("name")
            }
        }
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


@get("/verify")
async def verify_token(
    authorization: Optional[str] = Parameter(header="Authorization", default=None)
) -> dict[str, Any]:
    """Проверить валидность JWT токена (для фронтенда)."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"valid": False}
    token = authorization.split(" ", 1)[1]
    try:
        payload = _decode_jwt(token)
        return {
            "valid": True,
            "user": {"id": payload.get("sub"), "email": payload.get("email"), "name": payload.get("name")},
        }
    except Exception:
        return {"valid": False}


# === Router ===

router = Router(
    path="/auth",
    route_handlers=[
        login,
        register,
        refresh_token,
        logout,
        get_current_user,
        verify_token,
    ],
    tags=["Authentication"],
)

