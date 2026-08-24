# -*- coding: utf-8 -*-
"""Язык ответа модели = язык интерфейса пользователя.

Промпты системы написаны по-русски, и без явной инструкции модель отвечает
по-русски даже англоязычному пользователю: интерфейс переведён, а директор
на планёрке говорит по-русски. Инструкция добавляется ХВОСТОМ к системному
промпту — сами промпты остаются как есть.

Пусто или "ru" → пустая строка: поведение для русского тенанта не меняется
байт-в-байт (тот же промпт, тот же кэш).
"""
from __future__ import annotations

# Языки интерфейса + распространённые соседи. Неизвестный код не выдумываем
# в название — передаём модели как есть (она поймёт ISO-код лучше, чем наш
# кривой перевод названия).
LANG_NAMES = {"en": "English", "ru": "русский", "de": "Deutsch",
              "es": "español", "fr": "français", "it": "italiano",
              "pt": "português", "zh": "中文", "ja": "日本語"}


def _request_lang() -> str:
    """Локаль текущего HTTP-запроса или "" — у фоновой джобы запроса нет."""
    try:
        from backend.core.i18n import _current_locale
        loc = _current_locale.get()
        if loc is not None:
            return str(getattr(loc, "value", loc))
    except Exception:
        pass
    return ""


async def _persona_lang(user_id: str) -> str:
    """Сохранённый язык человека (Persona) или ""."""
    try:
        from backend.core.persona.store import get_persona_store
        persona = await get_persona_store().get(user_id)
        cc = getattr(persona, "communication_cognitive", None)
        pref = getattr(cc, "preferred_language", None)
        if isinstance(pref, str) and pref.strip():
            return pref.strip().lower()
    except Exception:
        pass
    return ""


async def resolve_answer_lang(user_id: str, *,
                              prefer_persona: bool = False) -> str:
    """Язык, на котором писать текст для человека.

    По умолчанию (prefer_persona=False) — язык ТОГО, КТО СПРОСИЛ: локаль
    запроса → сохранённый язык человека → дефолт стенда. Ночная джоба
    запроса не имеет, поэтому там сразу работает второй шаг: иначе
    англоязычный руководитель получал бы русский пульс в Telegram.

    prefer_persona=True — когда текст пишется НЕ ДЛЯ ТОГО, кто нажал кнопку
    (персональная версия стратегии для каждого сотрудника): там читатель
    один, а запрос сделал другой человек, и его локаль читателю не указ.
    Возврат — код языка ("ru", "en", …), никогда не пусто."""
    req, persona = _request_lang(), await _persona_lang(user_id)
    for got in ((persona, req) if prefer_persona else (req, persona)):
        if got:
            return got
    try:
        from backend.config import get_settings
        d = str(getattr(get_settings(), "default_locale", "") or "").strip()
        if d:
            return d.lower()
    except Exception:
        pass
    return "ru"


def lang_instruction(lang: str | None, *, json_values: bool = False) -> str:
    """Хвост системного промпта «отвечай на языке пользователя».

    json_values=True — для промптов, которые обязаны вернуть строгий JSON:
    переводить в них можно ТОЛЬКО значения, ключи и скопированные из данных
    имена собственные обязаны остаться как есть (иначе ломается разбор
    ответа и сопоставление с исходными карточками).

    Возврат "" для пустого/русского — вызывающий код может приклеивать
    результат без условий."""
    code = str(lang or "").strip().lower()
    if not code or code == "ru":
        return ""
    name = LANG_NAMES.get(code, code)
    if json_values:
        return (f"\n\nЯЗЫК ОТВЕТА: значения в JSON пиши на языке {name} "
                f"({code}) — это язык пользователя. КЛЮЧИ JSON оставь ровно "
                "такими, как в схеме выше, и не переводи имена собственные, "
                "скопированные из исходных данных.")
    return (f"\n\nЯЗЫК ОТВЕТА: отвечай пользователю на языке {name} ({code}) — "
            "это язык его интерфейса. Инструкции выше даны по-русски, но "
            "ответ должен быть целиком на языке пользователя.")
