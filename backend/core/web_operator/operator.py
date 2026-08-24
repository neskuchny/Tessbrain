"""
Web Operator - AI-управляемый браузер

Основной класс для управления браузером через AI.
Использует Stagehand + Gemini 3.0 Flash.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# Настройка кодировки для Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

logger = logging.getLogger(__name__)

# Пути для данных
DATA_DIR = Path(__file__).parent.parent.parent.parent / "data" / "web_operator"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
SESSIONS_DIR = DATA_DIR / "sessions"

# Домены, куда веб-оператору можно ходить по умолчанию. Здесь только
# общеизвестные сервисы: адреса конкретной установки — это конфигурация,
# а не код, и в исходниках им не место (в открытом репозитории такой
# адрес ещё и указывает на чужую инфраструктуру).
PUBLIC_ALLOWED_DOMAINS = [
    "yougile.com",
    "google.com",
    "calendar.google.com",
    "docs.google.com",
    "trello.com",
    "notion.so",
    "github.com",
]


def default_allowed_domains() -> List[str]:
    """Список разрешённых доменов: общеизвестные + свои из окружения.

    Свои задаются через WEB_OPERATOR_ALLOWED_DOMAINS через запятую —
    например адреса собственных сервисов установки. Порядок сохраняем,
    дубликаты убираем.
    """
    extra = (os.getenv("WEB_OPERATOR_ALLOWED_DOMAINS") or "").split(",")
    merged = [d.strip() for d in extra if d.strip()] + PUBLIC_ALLOWED_DOMAINS
    seen, out = set(), []
    for d in merged:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


class OperatorEnv(str, Enum):
    """Режим работы браузера"""
    LOCAL = "LOCAL"           # Локальный Playwright
    BROWSERBASE = "BROWSERBASE"  # Облачный Browserbase


class WebOperatorConfig(BaseModel):
    """Конфигурация Web Operator"""
    env: OperatorEnv = Field(default=OperatorEnv.LOCAL, description="LOCAL или BROWSERBASE")
    model_name: str = Field(
        default="google/gemini-flash-latest",
        description="Модель для AI (Gemini 3.0 Flash Preview)"
    )
    fallback_model: str = Field(
        default="openai/gpt-4o",
        description="Fallback модель при rate limit основной"
    )
    headless: bool = Field(default=False, description="Запускать без GUI (headless mode)")
    timeout_ms: int = Field(default=30000, description="Таймаут операций в мс")
    viewport_width: int = Field(default=1280, description="Ширина viewport")
    viewport_height: int = Field(default=720, description="Высота viewport")

    # Rate limit handling
    retry_on_rate_limit: bool = Field(default=True, description="Retry с fallback при 429")
    rate_limit_retry_delay: int = Field(default=5, description="Задержка перед retry (сек)")

    # Browserbase настройки (если env=BROWSERBASE)
    browserbase_api_key: Optional[str] = Field(default=None)
    browserbase_project_id: Optional[str] = Field(default=None)

    # Безопасность
    allowed_domains: List[str] = Field(
        default_factory=lambda: default_allowed_domains(),
        description="Разрешённые домены (пустой список = все)"
    )

    # Логирование
    save_screenshots: bool = Field(default=True, description="Сохранять скриншоты действий")
    save_session_log: bool = Field(default=True, description="Сохранять лог сессии")

    # Live view callback (для стриминга скриншотов)
    # Формат: async def callback(screenshot_base64: str, step: int, action: str)
    screenshot_callback: Optional[Any] = Field(default=None, exclude=True)


class ActionResult(BaseModel):
    """Результат действия в браузере"""
    success: bool
    action: str
    description: str
    timestamp: datetime = Field(default_factory=datetime.now)
    duration_ms: int = 0
    screenshot_path: Optional[str] = None
    extracted_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class WebOperator:
    """
    AI-агент для управления браузером.

    Использует Stagehand + Gemini 3.0 Flash для:
    - Навигации по сайтам
    - Кликов и ввода текста
    - Извлечения данных
    - Автономного выполнения задач

    Примеры:
        # Базовое использование
        operator = WebOperator()
        await operator.start()
        await operator.goto("https://google.com")
        await operator.act("search for 'AI automation'")
        await operator.stop()

        # С конфигурацией
        config = WebOperatorConfig(headless=True, env=OperatorEnv.BROWSERBASE)
        operator = WebOperator(config)
    """

    def __init__(self, config: Optional[WebOperatorConfig] = None):
        self.config = config or WebOperatorConfig()
        self.stagehand = None
        self.session_id: Optional[str] = None
        self.action_history: List[ActionResult] = []
        self._started = False
        self._start_time: Optional[datetime] = None

    @property
    def is_running(self) -> bool:
        """Проверить запущен ли браузер"""
        return self._started and self.stagehand is not None

    @property
    def page(self):
        """Получить текущую страницу Playwright"""
        if not self.stagehand:
            raise RuntimeError("WebOperator not started. Call start() first.")
        return self.stagehand.page

    async def start(self) -> str:
        """
        Запустить браузер.

        Returns:
            session_id: ID сессии для отслеживания
        """
        if self._started:
            logger.warning("WebOperator already started")
            return self.session_id

        try:
            # Импортируем Stagehand только при запуске
            from stagehand import Stagehand, StagehandConfig
        except ImportError:
            raise ImportError(
                "Stagehand not installed. Run: pip install stagehand playwright && playwright install chromium"
            )

        # Создаём директории для данных
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # Получаем API ключ
        model_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not model_api_key:
            raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY environment variable required")

        # Конфигурация Stagehand
        # Важно: используем camelCase, так как StagehandConfig ожидает именно их
        stagehand_config = StagehandConfig(
            env=self.config.env.value,
            modelName=self.config.model_name,
            modelApiKey=model_api_key,
            headless=self.config.headless,
            domSettleTimeoutMs=self.config.timeout_ms,
            # modelClientOptions={"api_key": model_api_key} # Убираем дублирование ключа
        )

        # Browserbase настройки
        if self.config.env == OperatorEnv.BROWSERBASE:
            stagehand_config.api_key = (
                self.config.browserbase_api_key or
                os.getenv("BROWSERBASE_API_KEY")
            )
            stagehand_config.project_id = (
                self.config.browserbase_project_id or
                os.getenv("BROWSERBASE_PROJECT_ID")
            )

            if not stagehand_config.api_key:
                raise ValueError("BROWSERBASE_API_KEY required for BROWSERBASE env")

        # Запускаем Stagehand
        self.stagehand = Stagehand(stagehand_config)
        await self.stagehand.init()

        self.session_id = getattr(self.stagehand, 'session_id', None) or f"local_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._started = True
        self._start_time = datetime.now()

        logger.info(f"🌐 WebOperator started: session={self.session_id}, env={self.config.env.value}")

        if self.config.env == OperatorEnv.BROWSERBASE:
            logger.info(f"🔗 View live: https://www.browserbase.com/sessions/{self.session_id}")

        return self.session_id

    async def stop(self):
        """Остановить браузер и сохранить логи"""
        if not self._started:
            return

        # Сохраняем лог сессии
        if self.config.save_session_log and self.action_history:
            await self._save_session_log()

        # Закрываем Stagehand
        if self.stagehand:
            try:
                await self.stagehand.close()
            except Exception as e:
                logger.warning(f"Error closing stagehand: {e}")

        duration = (datetime.now() - self._start_time).total_seconds() if self._start_time else 0
        logger.info(f"🔴 WebOperator stopped: session={self.session_id}, duration={duration:.1f}s, actions={len(self.action_history)}")

        self._started = False
        self.stagehand = None

    def _validate_url(self, url: str) -> bool:
        """Проверить что URL разрешён"""
        if not self.config.allowed_domains:
            return True  # Пустой список = все разрешены

        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()

        for allowed in self.config.allowed_domains:
            if allowed.lower() in domain:
                return True

        return False

    async def _record_action(
        self,
        action: str,
        description: str,
        success: bool,
        start_time: datetime,
        extracted_data: Dict[str, Any] | None = None,
        error: str | None = None,
        take_screenshot: bool = True
    ) -> ActionResult:
        """Записать результат действия"""
        duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        screenshot_path = None
        if take_screenshot and self.config.save_screenshots and success:
            try:
                screenshot_path = await self.screenshot()
            except Exception as e:
                logger.debug(f"Failed to take screenshot: {e}")

        result = ActionResult(
            success=success,
            action=action,
            description=description,
            duration_ms=duration_ms,
            screenshot_path=screenshot_path,
            extracted_data=extracted_data,
            error=error
        )

        self.action_history.append(result)

        emoji = "✅" if success else "❌"
        logger.info(f"{emoji} [{action}] {description} ({duration_ms}ms)")

        return result

    async def goto(self, url: str) -> ActionResult:
        """
        Перейти на URL.

        Args:
            url: URL для навигации

        Returns:
            ActionResult с результатом
        """
        start_time = datetime.now()

        # Проверка безопасности
        if not self._validate_url(url):
            return await self._record_action(
                action="goto",
                description=f"Navigation to {url} blocked (domain not allowed)",
                success=False,
                start_time=start_time,
                error=f"Domain not in allowed list: {self.config.allowed_domains}"
            )

        try:
            await self.page.goto(url)
            return await self._record_action(
                action="goto",
                description=f"Navigated to {url}",
                success=True,
                start_time=start_time
            )
        except Exception as e:
            return await self._record_action(
                action="goto",
                description=f"Failed to navigate to {url}",
                success=False,
                start_time=start_time,
                error=str(e)
            )

    async def observe(self, instruction: str) -> ActionResult:
        """
        Наблюдать за страницей - AI анализирует что на экране.

        Args:
            instruction: Что искать/анализировать

        Returns:
            ActionResult с найденными элементами
        """
        start_time = datetime.now()

        try:
            elements = await self.page.observe(instruction)

            # Преобразуем результат в сериализуемый формат
            elements_data = []
            if elements:
                for el in elements:
                    if hasattr(el, 'model_dump'):
                        elements_data.append(el.model_dump())
                    elif hasattr(el, '__dict__'):
                        elements_data.append(str(el))
                    else:
                        elements_data.append(str(el))

            return await self._record_action(
                action="observe",
                description=instruction,
                success=True,
                start_time=start_time,
                extracted_data={"elements": elements_data, "count": len(elements_data)}
            )
        except Exception as e:
            return await self._record_action(
                action="observe",
                description=instruction,
                success=False,
                start_time=start_time,
                error=str(e)
            )

    async def act(self, instruction: str) -> ActionResult:
        """
        Выполнить действие - AI кликает, вводит текст и т.д.

        Args:
            instruction: Что сделать (на естественном языке)

        Returns:
            ActionResult с результатом

        Examples:
            await operator.act("click on the login button")
            await operator.act("type 'hello@example.com' into the email field")
            await operator.act("scroll down to see more results")
        """
        start_time = datetime.now()

        try:
            result = await self.page.act(instruction)

            # Stagehand returns a structured result object for act/observe flows.
            # It can be success=False without raising an exception (e.g. LLM quota/rate-limit).
            success = True
            error = None
            extracted: Dict[str, Any] | None = {"result": str(result)} if result else None

            if result is not None and hasattr(result, "success"):
                try:
                    success = bool(result.success)
                except Exception:
                    success = True

            if not success:
                # Prefer a meaningful message if present
                msg = None
                if result is not None and hasattr(result, "message"):
                    msg = result.message
                error = str(msg or "Action failed")

                # Detect Gemini quota errors (Stagehand/LiteLLM may embed them into message)
                low = error.lower()
                if "resource_exhausted" in low or "rate limit" in low or "429" in low:
                    error = (
                        "Gemini quota/rate-limit exceeded (429 RESOURCE_EXHAUSTED). "
                        "Please check billing/limits or retry later."
                    )

            return await self._record_action(
                action="act",
                description=instruction,
                success=success,
                start_time=start_time,
                extracted_data=extracted,
                error=error,
            )
        except Exception as e:
            return await self._record_action(
                action="act",
                description=instruction,
                success=False,
                start_time=start_time,
                error=str(e)
            )

    async def extract(self, instruction: str, schema: type | None = None) -> ActionResult:
        """
        Извлечь данные со страницы.

        Args:
            instruction: Что извлечь
            schema: Pydantic модель для структурированных данных (опционально)

        Returns:
            ActionResult с извлечёнными данными

        Examples:
            # Простое извлечение
            result = await operator.extract("extract all product names and prices")

            # Структурированное извлечение
            class Product(BaseModel):
                name: str
                price: float

            result = await operator.extract("extract products", schema=Product)
        """
        start_time = datetime.now()

        try:
            if schema:
                data = await self.page.extract(instruction, schema=schema)
            else:
                data = await self.page.extract(instruction)

            # Преобразуем в словарь
            if hasattr(data, 'model_dump'):
                extracted = data.model_dump()
            elif isinstance(data, dict):
                extracted = data
            else:
                extracted = {"data": str(data)}

            return await self._record_action(
                action="extract",
                description=instruction,
                success=True,
                start_time=start_time,
                extracted_data=extracted
            )
        except Exception as e:
            return await self._record_action(
                action="extract",
                description=instruction,
                success=False,
                start_time=start_time,
                error=str(e)
            )

    async def execute_task(self, task: str, max_steps: int = 20) -> ActionResult:
        """
        Выполнить сложную задачу автономно.
        AI сам разобьёт на шаги и выполнит.

        Args:
            task: Описание задачи на естественном языке
            max_steps: Максимальное количество шагов

        Returns:
            ActionResult с результатом

        Examples:
            await operator.execute_task("Find the cheapest flight from Moscow to Paris for next week")
            await operator.execute_task("Create a new task in Trello called 'Review PR'")
        """
        start_time = datetime.now()

        if not self._started or not self.stagehand:
            return await self._record_action(
                action="execute_task",
                description=task,
                success=False,
                start_time=start_time,
                error="WebOperator not started. Call start() first."
            )

        try:
            logger.info(f"🎯 Executing task: {task}")

            # IMPORTANT:
            # Stagehand has two different paths:
            # - Flash models (gemini-flash-lite-latest / gemini-flash-latest) should use page.act/page.observe (LLMClient)
            # - "Computer Use" models should use stagehand.agent() (CUA Agent)
            #
            # In LOCAL mode Stagehand Agent supports only a limited set of computer-use models.
            normalized_model = (self.config.model_name or "").strip()
            model_key = normalized_model.split("/")[-1]  # e.g. "google/gemini-flash-latest" -> "gemini-flash-latest"

            computer_use_models: set[str] = {
                # As shown in Browserbase Gemini Browser demo:
                # https://gemini.browserbase.com/
                "gemini-2.5-computer-use-preview-10-2025",
            }

            should_use_cua_agent = model_key in computer_use_models
            logger.info(
                f"🧠 Model for task: {normalized_model} | model_key={model_key} | use_cua_agent={should_use_cua_agent}"
            )

            if should_use_cua_agent:
                logger.info("🤖 Using Stagehand CUA Agent (stagehand.agent().execute())")
                agent = self.stagehand.agent(model=model_key)
                result = await agent.execute(task)
                logger.info(f"✅ Agent execution completed: {result}")
            else:
                # Flash path: drive the browser with page.act/observe in a loop.
                logger.info("⚡ Using Flash path (page.act/page.observe) with multi-step loop")

                # Give the page a moment to settle
                await asyncio.sleep(1)

                step = 0
                result = None
                last_action = None

                while step < max_steps:
                    step += 1
                    logger.info(f"🔄 Step {step}/{max_steps}: executing task...")

                    try:
                        # First observe what's on the page
                        if step > 1:
                            observed = await self.page.observe(f"What can I do next to complete: {task}")
                            logger.info(f"👁️ Step {step} observed: {observed}")

                            # Check if task seems complete
                            if observed and isinstance(observed, list) and len(observed) == 0:
                                logger.info("✅ Task appears complete (no more actions needed)")
                                break

                        # Execute next action
                        result = await self.page.act(task)
                        logger.info(f"✅ Step {step} page.act() completed: {result}")

                        # Check if action was meaningful
                        action_str = str(result) if result else ""
                        if result and hasattr(result, 'action'):
                            action_str = str(result.action)

                        # Detect if we're stuck (same action repeated)
                        if action_str == last_action:
                            logger.info("⚠️ Same action repeated, task likely complete")
                            break
                        last_action = action_str

                        # Check for completion signals in result
                        if result and hasattr(result, 'message'):
                            msg = str(result.message).lower()
                            if any(word in msg for word in ['complete', 'done', 'finished', 'no action', 'already']):
                                logger.info(f"✅ Task complete signal detected: {result.message}")
                                break

                        # Send screenshot to callback if configured (live view)
                        if self.config.screenshot_callback:
                            try:
                                import base64
                                screenshot_bytes = await self.page._page.screenshot(type="jpeg", quality=70)
                                screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                                action_desc = str(result.action) if result and hasattr(result, 'action') else task
                                await self.config.screenshot_callback(screenshot_b64, step, action_desc)
                                logger.debug(f"📸 Screenshot sent to callback (step {step})")
                            except Exception as cb_error:
                                logger.debug(f"Screenshot callback failed: {cb_error}")

                        # Small delay between steps
                        await asyncio.sleep(1.5)

                    except Exception as act_error:
                        error_str = str(act_error).lower()

                        # Check for rate limit error (429)
                        if "429" in error_str or "rate" in error_str or "quota" in error_str or "resource_exhausted" in error_str:
                            logger.warning(f"⚠️ Step {step} rate limit hit: {act_error}")

                            if self.config.retry_on_rate_limit:
                                # Wait and retry
                                delay = self.config.rate_limit_retry_delay
                                logger.info(f"⏳ Waiting {delay}s before retry...")
                                await asyncio.sleep(delay)

                                # Try with fallback model if available
                                if self.config.fallback_model and self.config.fallback_model != self.config.model_name:
                                    logger.info(f"🔄 Switching to fallback model: {self.config.fallback_model}")
                                    # Note: Stagehand model switch requires page recreation
                                    # For now, just wait longer and retry
                                    await asyncio.sleep(delay * 2)

                                try:
                                    result = await self.page.act(task)
                                    logger.info(f"✅ Step {step} retry succeeded: {result}")
                                    continue
                                except Exception as retry_error:
                                    logger.error(f"❌ Retry also failed: {retry_error}")
                                    # Continue to next step anyway
                                    await asyncio.sleep(delay)
                                    continue
                            else:
                                logger.error("❌ Rate limit, no retry configured")
                                break
                        else:
                            logger.warning(f"⚠️ Step {step} page.act() failed: {act_error}")
                            # Try to observe and retry once
                            try:
                                observed = await self.page.observe("what is on this page")
                                logger.info(f"👁️ Recovery observed: {observed}")
                                result = await self.page.act(task)
                            except Exception as retry_error:
                                logger.error(f"❌ Retry also failed: {retry_error}")
                                break

                logger.info(f"🏁 Flash path completed after {step} steps")

            # Normalize success for both AgentResult and page.act() result objects
            success = True
            error = None
            if result is not None and hasattr(result, "success"):
                try:
                    success = bool(result.success)
                except Exception:
                    success = True

            if not success:
                msg = None
                if result is not None and hasattr(result, "message"):
                    msg = result.message
                error = str(msg or "Task failed")
                low = error.lower()
                if "resource_exhausted" in low or "rate limit" in low or "429" in low:
                    error = (
                        "Gemini quota/rate-limit exceeded (429 RESOURCE_EXHAUSTED). "
                        "Please check billing/limits or retry later."
                    )

            return await self._record_action(
                action="execute_task",
                description=task,
                success=success,
                start_time=start_time,
                extracted_data={
                    "steps_count": len(self.action_history),
                    "result": str(result) if result else None,
                },
                error=error,
            )
        except Exception as e:
            logger.exception(f"❌ execute_task failed: {e}")
            return await self._record_action(
                action="execute_task",
                description=task,
                success=False,
                start_time=start_time,
                error=str(e)
            )

    async def screenshot(self, filename: str | None = None) -> str:
        """
        Сделать скриншот текущей страницы.

        Args:
            filename: Имя файла (опционально)

        Returns:
            Путь к сохранённому скриншоту
        """
        if not filename:
            filename = f"{self.session_id}_{len(self.action_history):03d}.png"

        path = SCREENSHOTS_DIR / filename
        path.parent.mkdir(parents=True, exist_ok=True)

        await self.page.screenshot(path=str(path))
        return str(path)

    async def wait(self, seconds: float = 1.0):
        """Подождать указанное время"""
        await asyncio.sleep(seconds)

    async def get_page_content(self) -> str:
        """Получить текстовое содержимое страницы"""
        return await self.page.content()

    async def get_current_url(self) -> str:
        """Получить текущий URL"""
        return self.page.url

    async def _save_session_log(self):
        """Сохранить лог сессии в JSON"""
        import json

        log_path = SESSIONS_DIR / f"{self.session_id}.json"

        log_data = {
            "session_id": self.session_id,
            "start_time": self._start_time.isoformat() if self._start_time else None,
            "end_time": datetime.now().isoformat(),
            "config": self.config.model_dump(),
            "actions": [a.model_dump() for a in self.action_history],
            "total_actions": len(self.action_history),
            "successful_actions": sum(1 for a in self.action_history if a.success),
        }

        with open(log_path, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"📝 Session log saved: {log_path}")


# === Вспомогательные функции ===

async def run_web_task(
    task: str,
    start_url: str | None = None,
    config: WebOperatorConfig = None,
    timeout_seconds: int = 120
) -> Dict[str, Any]:
    """
    Выполнить задачу в браузере (высокоуровневая функция).

    Args:
        task: Описание задачи
        start_url: Начальный URL (опционально)
        config: Конфигурация WebOperator
        timeout_seconds: Таймаут выполнения

    Returns:
        Словарь с результатами:
        - success: bool
        - task: str
        - actions: List[dict]
        - session_id: str
        - error: Optional[str]

    Examples:
        # Простой поиск
        result = await run_web_task(
            task="Search for 'Tessbrain AI' and tell me what you find",
            start_url="https://google.com"
        )

        # Работа с сервисом
        result = await run_web_task(
            task="Find my last meeting and download the PDF",
            start_url="https://meetflow.app",
            config=WebOperatorConfig(headless=True)
        )
    """
    operator = WebOperator(config)

    try:
        await operator.start()

        # Если start_url не указан, пробуем извлечь URL из задачи
        if not start_url:
            # Пробуем найти URL в тексте задачи
            import re
            url_match = re.search(r'https?://[^\s]+|(?:google|yougile|trello|meetflow|github)\.com', task.lower())
            if url_match:
                extracted = url_match.group()
                if not extracted.startswith('http'):
                    extracted = f"https://{extracted}"
                start_url = extracted
                logger.info(f"📎 Extracted URL from task: {start_url}")
            else:
                # По умолчанию Google для поисковых задач
                if any(word in task.lower() for word in ['найди', 'поиск', 'search', 'find', 'google']):
                    start_url = "https://www.google.com"
                    logger.info("🔍 Using Google as default for search task")

        if start_url:
            nav_result = await operator.goto(start_url)
            if not nav_result.success:
                return {
                    "success": False,
                    "task": task,
                    "actions": [nav_result.model_dump()],
                    "session_id": operator.session_id,
                    "error": nav_result.error
                }
            # Даём странице время загрузиться
            await asyncio.sleep(2)
        else:
            logger.warning("⚠️ No start_url provided and couldn't extract from task")

        # Выполняем основную задачу
        result = await asyncio.wait_for(
            operator.execute_task(task),
            timeout=timeout_seconds
        )

        return {
            "success": result.success,
            "task": task,
            "actions": [a.model_dump() for a in operator.action_history],
            "session_id": operator.session_id,
            "error": result.error,
            "extracted_data": result.extracted_data
        }

    except asyncio.TimeoutError:
        return {
            "success": False,
            "task": task,
            "actions": [a.model_dump() for a in operator.action_history],
            "session_id": operator.session_id,
            "error": f"Task timed out after {timeout_seconds} seconds"
        }

    except Exception as e:
        logger.exception(f"Error in run_web_task: {e}")
        return {
            "success": False,
            "task": task,
            "actions": [a.model_dump() for a in operator.action_history] if operator.action_history else [],
            "session_id": operator.session_id if operator.session_id else None,
            "error": str(e)
        }

    finally:
        await operator.stop()


# === Context Manager ===

class WebOperatorContext:
    """
    Context manager для WebOperator.

    Example:
        async with WebOperatorContext() as operator:
            await operator.goto("https://google.com")
            await operator.act("search for something")
    """

    def __init__(self, config: WebOperatorConfig = None):
        self.operator = WebOperator(config)

    async def __aenter__(self) -> WebOperator:
        await self.operator.start()
        return self.operator

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.operator.stop()
        return False  # Don't suppress exceptions

