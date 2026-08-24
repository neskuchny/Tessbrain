# -*- coding: utf-8 -*-
"""
Compact Rule Book — компактный набор правил для каждого пользователя/компании.

Принцип работы:
1. Собирает правила из разных источников:
   - ChatPreferenceExtractor (из диалогов)
   - RegulationExtractor (из встреч)
   - UserProfileService (из предпочтений)
   - Явные указания пользователя ("запомни: ...")
2. Ночью — суммаризирует все правила в один компактный документ (≤2000 токенов)
3. Новые правила merge-ятся со старыми, конфликтные разрешаются (новое побеждает)
4. Rule Book используется как system prompt при генерации ответов

Файлы:
- backend/core/memory/rule_book.py (этот файл)
- Связан с: chat_preference_extractor.py, user_profiles.py, nightly_consolidation.py
- Хранение: data/rule_books/{user_id}.json (per-user)

Стоимость LLM: ~$0.02-0.05 за суммаризацию (раз в день ночью)
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Максимальный размер Rule Book (в символах, ~2000 токенов)
MAX_RULE_BOOK_CHARS = 6000
# Максимальное количество правил до суммаризации
MAX_RULES_BEFORE_SUMMARIZE = 30


@dataclass
class Rule:
    """Одно правило в Rule Book."""
    text: str
    category: str = "general"  # communication, format, content, context, regulation
    source: str = "user"  # user, meeting, system, inferred
    priority: int = 5  # 1-10, чем выше — тем важнее
    created_at: str = ""
    last_confirmed: str = ""  # Когда правило было подтверждено последний раз

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.last_confirmed:
            self.last_confirmed = self.created_at


@dataclass
class RuleBook:
    """
    Компактный набор правил для одного пользователя.

    Содержит:
    - rules: список отдельных правил (до MAX_RULES_BEFORE_SUMMARIZE)
    - summary: суммаризированный текст всех правил (≤MAX_RULE_BOOK_CHARS)
    - context: контекст пользователя (роль, отдел, проекты)

    При превышении лимита правил — ночной процесс суммаризирует через LLM.
    """
    user_id: str = ""
    rules: List[Rule] = field(default_factory=list)
    summary: str = ""
    context: Dict[str, str] = field(default_factory=dict)
    version: int = 1
    last_updated: str = ""
    last_summarized: str = ""

    def add_rule(self, rule: Rule) -> bool:
        """
        Добавить правило. Если конфликтует с существующим — заменяет.

        Returns:
            True если правило добавлено/обновлено
        """
        # Проверяем на дубликат/конфликт
        for i, existing in enumerate(self.rules):
            if existing.category == rule.category and existing.text.lower().strip() == rule.text.lower().strip():
                # Обновляем время подтверждения
                self.rules[i].last_confirmed = datetime.now(timezone.utc).isoformat()
                return False

            # Конфликт в той же категории (напр. "кратко" vs "подробно")
            if existing.category == rule.category and self._is_conflicting(existing, rule):
                self.rules[i] = rule  # Новое правило побеждает
                logger.info(f"Rule conflict resolved: '{existing.text}' → '{rule.text}'")
                return True

        self.rules.append(rule)
        self.last_updated = datetime.now(timezone.utc).isoformat()
        return True

    def _is_conflicting(self, old: Rule, new: Rule) -> bool:
        """Проверить, конфликтуют ли два правила."""
        conflicts = [
            ({"кратко", "коротко", "brief"}, {"подробно", "детально", "detailed"}),
            ({"формально", "formal"}, {"неформально", "casual"}),
            ({"русск", "ru"}, {"english", "en"}),
        ]

        old_lower = old.text.lower()
        new_lower = new.text.lower()

        for group_a, group_b in conflicts:
            old_in_a = any(kw in old_lower for kw in group_a)
            old_in_b = any(kw in old_lower for kw in group_b)
            new_in_a = any(kw in new_lower for kw in group_a)
            new_in_b = any(kw in new_lower for kw in group_b)

            if (old_in_a and new_in_b) or (old_in_b and new_in_a):
                return True

        return False

    def needs_summarization(self) -> bool:
        """Проверить, нужна ли суммаризация."""
        return len(self.rules) > MAX_RULES_BEFORE_SUMMARIZE

    def to_prompt(self) -> str:
        """
        Получить Rule Book как текст для system prompt.

        Если есть суммаризированная версия — возвращает её.
        Иначе — собирает из отдельных правил.
        """
        if self.summary:
            parts = [self.summary]
        else:
            if not self.rules:
                return ""

            parts = ["ПРАВИЛА ВЗАИМОДЕЙСТВИЯ С ПОЛЬЗОВАТЕЛЕМ:"]
            # Сортируем по приоритету (высший первый)
            sorted_rules = sorted(self.rules, key=lambda r: r.priority, reverse=True)
            for rule in sorted_rules[:20]:
                parts.append(f"- {rule.text}")

        if self.context:
            parts.append("\nКОНТЕКСТ ПОЛЬЗОВАТЕЛЯ:")
            for key, value in self.context.items():
                parts.append(f"- {key}: {value}")

        result = "\n".join(parts)

        # Обрезаем если слишком длинный
        if len(result) > MAX_RULE_BOOK_CHARS:
            result = result[:MAX_RULE_BOOK_CHARS - 50] + "\n[...обрезано...]"

        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "rules": [asdict(r) for r in self.rules],
            "summary": self.summary,
            "context": self.context,
            "version": self.version,
            "last_updated": self.last_updated,
            "last_summarized": self.last_summarized,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuleBook":
        rules = [Rule(**r) for r in data.get("rules", [])]
        return cls(
            user_id=data.get("user_id", ""),
            rules=rules,
            summary=data.get("summary", ""),
            context=data.get("context", {}),
            version=data.get("version", 1),
            last_updated=data.get("last_updated", ""),
            last_summarized=data.get("last_summarized", ""),
        )


class RuleBookService:
    """
    Сервис управления Rule Book'ами пользователей.

    Использование:
    ```python
    service = RuleBookService()

    # Получить Rule Book
    book = service.load(user_id)

    # Добавить правило
    book.add_rule(Rule(text="Отвечай кратко", category="communication"))
    service.save(book)

    # Получить prompt для LLM
    prompt = book.to_prompt()

    # Ночная суммаризация (если накопилось >30 правил)
    await service.summarize(book, llm_router)
    ```
    """

    def __init__(self, storage_dir: str = "data/rule_books"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def load(self, user_id: str) -> RuleBook:
        """Загрузить Rule Book пользователя."""
        path = self.storage_dir / f"{user_id}.json"

        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return RuleBook.from_dict(data)
            except Exception as e:
                logger.error(f"Error loading rule book for {user_id}: {e}")

        return RuleBook(user_id=user_id)

    def save(self, book: RuleBook):
        """Сохранить Rule Book."""
        path = self.storage_dir / f"{book.user_id}.json"

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(book.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error saving rule book for {book.user_id}: {e}")

    async def summarize(self, book: RuleBook, llm_router=None) -> bool:
        """
        Суммаризировать Rule Book через LLM.

        Берёт все правила, контекст, и создаёт компактный текст
        (≤2000 токенов), который используется как system prompt.

        Args:
            book: Rule Book для суммаризации
            llm_router: LLM роутер

        Returns:
            True если суммаризация успешна
        """
        if not llm_router or not book.rules:
            return False

        rules_text = "\n".join(
            f"- [{r.category}] {r.text} (приоритет: {r.priority})"
            for r in sorted(book.rules, key=lambda r: r.priority, reverse=True)
        )

        context_text = "\n".join(
            f"- {k}: {v}" for k, v in book.context.items()
        ) if book.context else "Нет контекста"

        old_summary = book.summary or "Нет предыдущего саммари"

        prompt = f"""Ты — система управления правилами для AI-ассистента.

У пользователя накопилось {len(book.rules)} правил взаимодействия.
Создай КОМПАКТНОЕ саммари (максимум 30 строк) — набор правил для AI-ассистента.

ПРЕДЫДУЩЕЕ САММАРИ:
{old_summary}

ТЕКУЩИЕ ПРАВИЛА:
{rules_text}

КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ:
{context_text}

ТРЕБОВАНИЯ:
1. Объедини похожие правила
2. Новые правила важнее старых (если конфликтуют)
3. Результат должен быть компактным — максимум 30 строк
4. Формат: набор пунктов "- ..." группированных по категориям
5. Пиши на русском

Ответь ТОЛЬКО текстом правил, без заголовков и преамбулы."""

        try:
            summary = await llm_router.generate(
                prompt=prompt,
                system_prompt="Ты система суммаризации правил.",
                temperature=0.2,
                max_tokens=1000,
            )

            book.summary = summary.strip()
            book.last_summarized = datetime.now(timezone.utc).isoformat()
            book.version += 1

            # Очищаем старые правила с низким приоритетом (оставляем топ-10)
            if len(book.rules) > MAX_RULES_BEFORE_SUMMARIZE:
                book.rules = sorted(book.rules, key=lambda r: r.priority, reverse=True)[:10]

            self.save(book)
            logger.info(
                f"Rule Book summarized for {book.user_id}: "
                f"{len(book.rules)} rules → summary ({len(summary)} chars)"
            )
            return True

        except Exception as e:
            logger.error(f"Rule Book summarization failed: {e}")
            return False


# Singleton
_rule_book_service: Optional[RuleBookService] = None

def get_rule_book_service() -> RuleBookService:
    """Получить singleton сервиса Rule Book."""
    global _rule_book_service
    if _rule_book_service is None:
        _rule_book_service = RuleBookService()
    return _rule_book_service
