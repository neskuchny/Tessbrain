"""
Тестовый скрипт для Web Operator

Запуск:
    cd tessent_brain
    python -m backend.core.web_operator.test_operator
"""
import asyncio
import os
import sys

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from backend.core.web_operator import WebOperator, WebOperatorConfig, run_web_task
from backend.core.web_operator.operator import OperatorEnv


async def test_basic_navigation():
    """Тест базовой навигации"""
    print("\n" + "="*50)
    print("🧪 Тест 1: Базовая навигация")
    print("="*50)

    config = WebOperatorConfig(
        env=OperatorEnv.LOCAL,
        headless=False,  # Показываем браузер для отладки
        allowed_domains=["google.com", "wikipedia.org"]
    )

    operator = WebOperator(config)

    try:
        session_id = await operator.start()
        print(f"✅ Браузер запущен: {session_id}")

        # Навигация
        result = await operator.goto("https://www.google.com")
        print(f"📍 Навигация: {result.success} - {result.description}")

        # Наблюдение
        result = await operator.observe("find the search input field")
        print(f"👁️ Наблюдение: {result.success} - найдено элементов: {result.extracted_data}")

        # Действие
        result = await operator.act("type 'Tessbrain AI automation' into the search field")
        print(f"⌨️ Ввод: {result.success}")

        # Ждём немного
        await operator.wait(1)

        # Действие - поиск
        result = await operator.act("press Enter or click search button")
        print(f"🔍 Поиск: {result.success}")

        await operator.wait(2)

        # Извлечение
        result = await operator.extract("extract the titles of the first 3 search results")
        print(f"📊 Извлечение: {result.success}")
        if result.extracted_data:
            print(f"   Данные: {result.extracted_data}")

        # Скриншот
        screenshot_path = await operator.screenshot("test_google_search.png")
        print(f"📸 Скриншот: {screenshot_path}")

        print(f"\n✅ Тест завершён. Всего действий: {len(operator.action_history)}")

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await operator.stop()


async def test_run_web_task():
    """Тест высокоуровневой функции run_web_task"""
    print("\n" + "="*50)
    print("🧪 Тест 2: run_web_task")
    print("="*50)

    result = await run_web_task(
        task="Search for 'AI browser automation' and tell me what you find",
        start_url="https://www.google.com",
        config=WebOperatorConfig(
            env=OperatorEnv.LOCAL,
            headless=False
        ),
        timeout_seconds=60
    )

    print(f"✅ Успех: {result['success']}")
    print(f"📋 Задача: {result['task']}")
    print(f"🔢 Действий: {len(result.get('actions', []))}")

    if result.get('error'):
        print(f"❌ Ошибка: {result['error']}")

    if result.get('extracted_data'):
        print(f"📊 Данные: {result['extracted_data']}")


async def test_blocked_domain():
    """Тест блокировки неразрешённого домена"""
    print("\n" + "="*50)
    print("🧪 Тест 3: Блокировка домена")
    print("="*50)

    config = WebOperatorConfig(
        env=OperatorEnv.LOCAL,
        headless=True,
        allowed_domains=["google.com"]  # Только Google разрешён
    )

    operator = WebOperator(config)

    try:
        await operator.start()

        # Попытка перейти на неразрешённый домен
        result = await operator.goto("https://facebook.com")

        print(f"🚫 Блокировка: {not result.success}")
        print(f"   Сообщение: {result.description}")
        print(f"   Ошибка: {result.error}")

        if not result.success:
            print("✅ Домен правильно заблокирован!")
        else:
            print("❌ Домен НЕ был заблокирован!")

    finally:
        await operator.stop()


async def main():
    """Запуск всех тестов"""
    print("🚀 Запуск тестов Web Operator")
    print("="*50)

    # Проверяем наличие API ключа
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("❌ GEMINI_API_KEY или GOOGLE_API_KEY не установлен!")
        print("   Установите переменную окружения и попробуйте снова.")
        return

    print(f"✅ API ключ найден: {api_key[:10]}...")

    # Проверяем наличие Stagehand
    try:
        import stagehand
        print("✅ Stagehand установлен")
    except ImportError:
        print("❌ Stagehand не установлен!")
        print("   Выполните: pip install stagehand playwright")
        print("   Затем: playwright install chromium")
        return

    # Запускаем тесты
    try:
        await test_basic_navigation()
    except Exception as e:
        print(f"❌ Тест 1 провален: {e}")

    # await test_run_web_task()  # Раскомментировать для полного теста
    # await test_blocked_domain()  # Раскомментировать для теста безопасности


if __name__ == "__main__":
    asyncio.run(main())

