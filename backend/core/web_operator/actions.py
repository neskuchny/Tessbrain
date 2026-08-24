"""
Web Operator Actions - библиотека готовых действий

Предоставляет высокоуровневые действия для типичных задач.
"""
import asyncio
import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    """Типы действий"""
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    WAIT = "wait"
    EXTRACT = "extract"
    SCREENSHOT = "screenshot"
    LOGIN = "login"
    FORM_FILL = "form_fill"
    DOWNLOAD = "download"


class WebAction(BaseModel):
    """Описание действия для выполнения"""
    type: ActionType
    target: Optional[str] = Field(default=None, description="Целевой элемент или URL")
    value: Optional[str] = Field(default=None, description="Значение для ввода")
    options: Dict[str, Any] = Field(default_factory=dict, description="Дополнительные параметры")


class ActionSequence:
    """
    Последовательность действий для выполнения.

    Позволяет создавать цепочки действий декларативно.

    Example:
        sequence = ActionSequence()
        sequence.goto("https://example.com")
        sequence.click("login button")
        sequence.type("email field", "user@example.com")
        sequence.type("password field", "secret")
        sequence.click("submit button")

        await sequence.execute(operator)
    """

    def __init__(self):
        self.actions: List[WebAction] = []

    def goto(self, url: str) -> "ActionSequence":
        """Добавить навигацию"""
        self.actions.append(WebAction(type=ActionType.NAVIGATE, target=url))
        return self

    def click(self, target: str) -> "ActionSequence":
        """Добавить клик"""
        self.actions.append(WebAction(type=ActionType.CLICK, target=target))
        return self

    def type(self, target: str, value: str) -> "ActionSequence":
        """Добавить ввод текста"""
        self.actions.append(WebAction(type=ActionType.TYPE, target=target, value=value))
        return self

    def scroll(self, direction: str = "down", amount: int = 500) -> "ActionSequence":
        """Добавить скролл"""
        self.actions.append(WebAction(
            type=ActionType.SCROLL,
            target=direction,
            options={"amount": amount}
        ))
        return self

    def wait(self, seconds: float = 1.0) -> "ActionSequence":
        """Добавить ожидание"""
        self.actions.append(WebAction(type=ActionType.WAIT, options={"seconds": seconds}))
        return self

    def extract(self, instruction: str, schema: type = None) -> "ActionSequence":
        """Добавить извлечение данных"""
        self.actions.append(WebAction(
            type=ActionType.EXTRACT,
            target=instruction,
            options={"schema": schema.__name__ if schema else None}
        ))
        return self

    def screenshot(self, filename: str | None = None) -> "ActionSequence":
        """Добавить скриншот"""
        self.actions.append(WebAction(type=ActionType.SCREENSHOT, target=filename))
        return self

    async def execute(self, operator) -> List[Dict[str, Any]]:
        """
        Выполнить все действия последовательно.

        Args:
            operator: WebOperator instance

        Returns:
            Список результатов каждого действия
        """
        results = []

        for action in self.actions:
            result = await self._execute_action(operator, action)
            results.append(result.model_dump() if hasattr(result, 'model_dump') else result)

            # Прерываем при ошибке
            if hasattr(result, 'success') and not result.success:
                logger.warning(f"Action failed: {action.type} - {action.target}")
                break

        return results

    async def _execute_action(self, operator, action: WebAction):
        """Выполнить одно действие"""
        if action.type == ActionType.NAVIGATE:
            return await operator.goto(action.target)

        elif action.type == ActionType.CLICK:
            return await operator.act(f"click on {action.target}")

        elif action.type == ActionType.TYPE:
            return await operator.act(f"type '{action.value}' into {action.target}")

        elif action.type == ActionType.SCROLL:
            direction = action.target or "down"
            return await operator.act(f"scroll {direction}")

        elif action.type == ActionType.WAIT:
            await asyncio.sleep(action.options.get("seconds", 1.0))
            return {"success": True, "action": "wait"}

        elif action.type == ActionType.EXTRACT:
            return await operator.extract(action.target)

        elif action.type == ActionType.SCREENSHOT:
            path = await operator.screenshot(action.target)
            return {"success": True, "action": "screenshot", "path": path}

        else:
            return {"success": False, "error": f"Unknown action type: {action.type}"}


# === Готовые сценарии ===

class LoginScenario:
    """
    Сценарий логина на сайт.

    Example:
        login = LoginScenario(
            login_url="https://app.example.com/login",
            email_field="email input",
            password_field="password input",
            submit_button="login button"
        )

        success = await login.execute(operator, "user@example.com", "password123")
    """

    def __init__(
        self,
        login_url: str,
        email_field: str = "email field",
        password_field: str = "password field",
        submit_button: str = "login button",
        success_indicator: str = "dashboard or user menu"
    ):
        self.login_url = login_url
        self.email_field = email_field
        self.password_field = password_field
        self.submit_button = submit_button
        self.success_indicator = success_indicator

    async def execute(self, operator, email: str, password: str) -> bool:
        """
        Выполнить логин.

        Returns:
            True если логин успешен
        """
        # Переходим на страницу логина
        await operator.goto(self.login_url)
        await asyncio.sleep(1)  # Ждём загрузки

        # Вводим креденшелы
        await operator.act(f"type '{email}' into {self.email_field}")
        await operator.act(f"type '{password}' into {self.password_field}")

        # Нажимаем кнопку
        await operator.act(f"click on {self.submit_button}")
        await asyncio.sleep(2)  # Ждём авторизации

        # Проверяем успешность
        result = await operator.observe(f"check if we see {self.success_indicator}")

        return result.success and result.extracted_data


class FormFillScenario:
    """
    Сценарий заполнения формы.

    Example:
        form = FormFillScenario()
        form.add_field("name", "John Doe")
        form.add_field("email", "john@example.com")
        form.add_dropdown("country", "Russia")

        await form.execute(operator)
    """

    def __init__(self, submit_button: str = "submit button"):
        self.fields: List[Dict[str, Any]] = []
        self.submit_button = submit_button

    def add_field(self, field_name: str, value: str) -> "FormFillScenario":
        """Добавить текстовое поле"""
        self.fields.append({
            "type": "text",
            "field": field_name,
            "value": value
        })
        return self

    def add_dropdown(self, field_name: str, value: str) -> "FormFillScenario":
        """Добавить выпадающий список"""
        self.fields.append({
            "type": "dropdown",
            "field": field_name,
            "value": value
        })
        return self

    def add_checkbox(self, field_name: str, checked: bool = True) -> "FormFillScenario":
        """Добавить чекбокс"""
        self.fields.append({
            "type": "checkbox",
            "field": field_name,
            "value": checked
        })
        return self

    async def execute(self, operator, submit: bool = True) -> bool:
        """
        Заполнить форму.

        Args:
            operator: WebOperator
            submit: Нажать кнопку отправки

        Returns:
            True если успешно
        """
        for field in self.fields:
            if field["type"] == "text":
                await operator.act(f"type '{field['value']}' into {field['field']}")

            elif field["type"] == "dropdown":
                await operator.act(f"select '{field['value']}' in {field['field']} dropdown")

            elif field["type"] == "checkbox":
                action = "check" if field["value"] else "uncheck"
                await operator.act(f"{action} the {field['field']} checkbox")

        if submit:
            await operator.act(f"click on {self.submit_button}")

        return True


class DataExtractionScenario:
    """
    Сценарий извлечения данных с нескольких страниц.

    Example:
        extractor = DataExtractionScenario(
            base_url="https://example.com/products",
            item_selector="product card",
            fields=["name", "price", "rating"],
            pagination="next page button"
        )

        data = await extractor.execute(operator, max_pages=5)
    """

    def __init__(
        self,
        base_url: str,
        item_selector: str,
        fields: List[str],
        pagination: str | None = None,
        max_items_per_page: int = 20
    ):
        self.base_url = base_url
        self.item_selector = item_selector
        self.fields = fields
        self.pagination = pagination
        self.max_items_per_page = max_items_per_page

    async def execute(self, operator, max_pages: int = 1) -> List[Dict[str, Any]]:
        """
        Извлечь данные.

        Returns:
            Список извлечённых элементов
        """
        all_data = []

        await operator.goto(self.base_url)

        for page in range(max_pages):
            # Извлекаем данные с текущей страницы
            fields_str = ", ".join(self.fields)
            result = await operator.extract(
                f"extract {fields_str} from all {self.item_selector} elements on the page"
            )

            if result.success and result.extracted_data:
                items = result.extracted_data.get("data", [])
                if isinstance(items, list):
                    all_data.extend(items)
                else:
                    all_data.append(items)

            # Переходим на следующую страницу
            if self.pagination and page < max_pages - 1:
                nav_result = await operator.act(f"click on {self.pagination}")
                if not nav_result.success:
                    break
                await asyncio.sleep(1)  # Ждём загрузки

        return all_data

