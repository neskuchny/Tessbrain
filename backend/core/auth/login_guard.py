# -*- coding: utf-8 -*-
"""Замедление подбора пароля — счётчик НЕУДАЧНЫХ входов на учётную запись.

ЗАЧЕМ ОТДЕЛЬНО ОТ RATE-LIMIT. Общий лимит считает по адресу. Против двух
самых частых атак этого мало:

· Перебор пароля. Адрес меняется дёшево — прокси, мобильная сеть,
  ботнет. Лимит «10 в минуту с адреса» превращается в «10 в минуту с
  адреса, а адресов тысяча».

· Подстановка паролей (credential stuffing). Берут слитую где-то базу
  «почта-пароль» и пробуют у нас. Люди повторяют пароли, поэтому часть
  подходит С ПЕРВОГО раза. Тут перебора по одному аккаунту вообще нет —
  зато есть много попыток по разным аккаунтам с одного места, и это ловит
  как раз адресный лимит. Один счётчик другой не заменяет; нужны оба.

Здесь — счётчик на УЧЁТНУЮ ЗАПИСЬ: сколько неудачных попыток по этой почте
за окно. Считаются только неудачные; успешный вход счётчик обнуляет.

ЧЕСТНАЯ ОГОВОРКА ПРО ОБРАТНУЮ СТОРОНУ. Любой счётчик на аккаунт даёт
атакующему рычаг: долбить чужую почту заведомо неверным паролем, чтобы
владелец не мог войти. Поэтому здесь НЕТ блокировки — только временная
задержка, окно короткое, порог с запасом (по умолчанию 10 попыток за 15
минут), и снимается всё само. Постоянных замков не ставим никогда.

Почта в ключ не пишется — только её отпечаток (sha256). В журнал попадает
тоже отпечаток: по логам rate-limit'а не должно быть видно, кто у нас
зарегистрирован.

Redis недоступен → пропускаем (как и общий rate-limit): защита от подбора
не должна ронять вход целиком. Настройки:

    LOGIN_MAX_FAILURES         сколько неудач до задержки (0 — выключить)
    LOGIN_FAILURE_WINDOW_SEC   длина окна в секундах
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX = 10
_DEFAULT_WINDOW = 15 * 60


def _config() -> tuple[int, int]:
    def _int(name: str, default: int) -> int:
        raw = (os.getenv(name, "") or "").strip()
        if not raw:
            return default
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning("login_guard: %s=%r не число, беру %s", name, raw, default)
            return default

    return _int("LOGIN_MAX_FAILURES", _DEFAULT_MAX), _int(
        "LOGIN_FAILURE_WINDOW_SEC", _DEFAULT_WINDOW)


def account_key(email: str) -> str:
    """Отпечаток учётной записи. Сама почта нигде не сохраняется."""
    normalized = (email or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


async def _redis():
    try:
        from backend.db.redis_client import get_redis
        redis = await get_redis()
        if not await redis.health_check():
            return None
        return redis.client
    except Exception as exc:
        logger.debug("login_guard: Redis недоступен: %s", exc)
        return None


async def check(email: str) -> tuple[bool, int]:
    """(можно_пробовать, через_сколько_секунд_можно).

    Ничего не записывает — только смотрит. Считать неудачу нужно явным
    вызовом `record_failure` уже после ответа провайдера, иначе счётчик
    рос бы и от успешных входов.
    """
    limit, window = _config()
    if limit <= 0 or not email:
        return True, 0
    client = await _redis()
    if client is None:
        return True, 0
    key = f"login:fail:{account_key(email)}"
    try:
        raw = await client.get(key)
        used = int(raw) if raw else 0
        if used < limit:
            return True, 0
        ttl = await client.ttl(key)
        return False, max(1, int(ttl) if ttl and ttl > 0 else window)
    except Exception as exc:
        logger.warning("login_guard: ошибка чтения счётчика, пропускаю: %s", exc)
        return True, 0


async def record_failure(email: str) -> int:
    """Учесть неудачную попытку. Возвращает текущее число неудач в окне."""
    limit, window = _config()
    if limit <= 0 or not email:
        return 0
    client = await _redis()
    if client is None:
        return 0
    key = f"login:fail:{account_key(email)}"
    try:
        used = int(await client.incr(key))
        if used == 1:
            # Окно скользит от ПЕРВОЙ неудачи: серия попыток не может
            # бесконечно продлевать себе срок.
            await client.expire(key, window)
        if used == limit:
            fingerprint = key.rsplit(":", 1)[-1][:8]
            logger.warning(
                "login_guard: учётная запись %s… достигла %s неудач за %sс — "
                "вход придержан", fingerprint, limit, window)
        return used
    except Exception as exc:
        logger.warning("login_guard: ошибка записи счётчика: %s", exc)
        return 0


async def record_success(email: str) -> None:
    """Успешный вход — счётчик обнуляем."""
    if not email:
        return
    client = await _redis()
    if client is None:
        return
    try:
        await client.delete(f"login:fail:{account_key(email)}")
    except Exception as exc:
        logger.debug("login_guard: не удалось сбросить счётчик: %s", exc)


def retry_message(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return (f"Слишком много неудачных попыток входа. "
            f"Попробуйте снова через {minutes} мин.")


__all__ = ["account_key", "check", "record_failure", "record_success", "retry_message"]
