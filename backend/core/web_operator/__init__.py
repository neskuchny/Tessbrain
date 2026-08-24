"""
Web Operator - AI-управляемый браузер для Tessbrain

Использует Stagehand + Gemini 3.0 Flash для автоматизации
браузерных задач без необходимости API.

Примеры использования:
    from backend.core.web_operator import WebOperator, run_web_task

    # Простой запуск задачи
    result = await run_web_task(
        task="Найди последнюю встречу и скачай PDF",
        start_url="https://meetflow.app"
    )

    # Полный контроль
    operator = WebOperator()
    await operator.start()
    await operator.goto("https://google.com")
    await operator.act("search for 'Tessbrain AI'")
    data = await operator.extract("extract search results")
    await operator.stop()
"""

from .actions import (
    ActionType,
    WebAction,
)
from .operator import (
    ActionResult,
    WebOperator,
    WebOperatorConfig,
    run_web_task,
)

__all__ = [
    "ActionResult",
    "ActionType",
    "WebAction",
    "WebOperator",
    "WebOperatorConfig",
    "run_web_task",
]

