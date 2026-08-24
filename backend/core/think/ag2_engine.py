import asyncio
import logging
import os
from typing import Any, Dict

from autogen import ConversableAgent, GroupChat, GroupChatManager, UserProxyAgent, register_function

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AG2ReasoningEngine:
    """
    Экспериментальный движок на базе Microsoft AutoGen (AG2).
    Реализует мульти-агентную систему для глубокого анализа.
    """

    def __init__(self, tools_instance, llm_config: Dict[str, Any]):
        """
        Args:
            tools_instance: Экземпляр TessentBrainTools
            llm_config: Конфигурация для LLM (OpenAI/Gemini)
        """
        self.tools = tools_instance
        self.llm_config = llm_config
        self.agents = {}

        # Инициализация агентов
        self._init_agents()

    def _init_agents(self):
        """Создание агентов"""

        # Общий контекст для всех агентов
        company_context = """
ВАЖНО: Ты работаешь с ВНУТРЕННЕЙ базой знаний компании Tessbrain.
Это НЕ интернет-поиск. У тебя есть доступ к:
- Транскриптам встреч компании
- Задачам и решениям
- Информации о сотрудниках и проектах
- Графу знаний компании

Когда пользователь спрашивает "чем занимается компания?" — ищи в базе знаний информацию о проектах, задачах и деятельности.
НЕ СПРАШИВАЙ название компании — это НАША компания, информация о которой уже в базе.

КРИТИЧНОЕ ПРАВИЛО (anti-hallucination):
- НЕЛЬЗЯ добавлять факты, которых нет в результатах инструментов/источниках.
- Если данных недостаточно — так и скажи: "в базе знаний недостаточно данных", и перечисли, какие данные нужны.
- Запрещено ссылаться на внешние события/новости/интернет (например, "банкротство", "рынок", "новости") если этого нет в базе знаний.
"""

        # 1. User Proxy - инициатор
        self.agents["user_proxy"] = UserProxyAgent(
            name="User_Proxy",
            system_message="Human admin.",
            code_execution_config=False,
            human_input_mode="NEVER",
        )

        # 2. Planner - планировщик
        self.agents["planner"] = ConversableAgent(
            name="Planner",
            system_message=f"""{company_context}

Ты — Старший Аналитик.
Твоя задача — составить план расследования для ответа на вопрос пользователя.
Разбей вопрос на подзадачи.
Определи, какую информацию нужно найти (проекты, люди, задачи, встречи).
Не ищи информацию сам, только давай указания Исследователю (Researcher).
Когда информации достаточно, дай команду Аналитику (Analyst) составить отчет.

ПЕРВЫЙ ШАГ ВСЕГДА: попроси Исследователя использовать search_knowledge() для поиска по базе знаний.
ВАЖНО: не предлагай несуществующие инструменты (например, find_related_documents). Используй только те инструменты, которые реально доступны Исследователю.
""",
            llm_config=self.llm_config,
        )

        # 3. Researcher - исследователь (с инструментами)
        self.agents["researcher"] = ConversableAgent(
            name="Researcher",
            system_message=f"""{company_context}

Ты — Эксперт по поиску в базе знаний Tessbrain.
Твоя задача — выполнять поиск информации по запросу Планировщика.

ДОСТУПНЫЕ ИНСТРУМЕНТЫ:
1. search_knowledge(query) — семантический поиск по всей базе (встречи, задачи, решения)
2. get_project_status(project_name) — статус конкретного проекта
3. get_person_info(person_name) — информация о сотруднике
4. get_anomalies() — список аномалий и рисков

ВСЕГДА начинай с search_knowledge() для общих вопросов!
Если инструмент вернул мало данных, попробуй переформулировать запрос.
Сообщай о найденных фактах кратко и четко.
НЕ выдумывай факты: только то, что реально найдено инструментами.
""",
            llm_config=self.llm_config,
        )

        # 4. Critic - критик
        self.agents["critic"] = ConversableAgent(
            name="Critic",
            system_message=f"""{company_context}

Ты — Критик.
Проверяй результаты поиска Исследователя.

ВАЖНЫЕ ПРАВИЛА:
1. Если Исследователь нашёл ХОТЬ КАКИЕ-ТО данные (задачи, проекты, встречи, сотрудники) —
   это ДОСТАТОЧНО для ответа! Передай управление Аналитику (Analyst).
2. НЕ требуй идеальных данных. Работай с тем, что есть.
3. МАКСИМУМ 1 дополнительный запрос на поиск. После этого — ОБЯЗАТЕЛЬНО передай Аналитику.
4. Если данные найдены, скажи: "Данные достаточны. Analyst, составь отчёт."

ЗАПРЕЩЕНО:
- Бесконечно требовать "больше данных"
- Возвращать задачу Планировщику более 1 раза
- Блокировать работу Аналитика
""",
            llm_config=self.llm_config,
        )

        # 5. Analyst - аналитик
        self.agents["analyst"] = ConversableAgent(
            name="Analyst",
            system_message=f"""{company_context}

Ты — Бизнес-аналитик. Когда к тебе обращаются, ты ОБЯЗАН дать ПОЛНЫЙ структурированный ответ.

КРИТИЧЕСКИ ВАЖНО:
- НЕЛЬЗЯ отвечать просто "OK" или "TERMINATE"
- Ты ДОЛЖЕН написать РАЗВЁРНУТЫЙ ответ на РУССКОМ языке
- Используй ВСЕ данные из сообщений Исследователя (search_knowledge результаты)

АЛГОРИТМ:
1. Прочитай ВСЕ предыдущие сообщения
2. Найди результаты search_knowledge() — там есть конкретные данные
3. Извлеки из них: задачи, проекты, сотрудников, KPI, системы
4. Составь структурированный ответ

ОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТВЕТА (на русском языке):

## Ответ на вопрос пользователя

[2-3 предложения с прямым ответом]

## Найденные данные

### Задачи
- [Перечисли найденные задачи]

### Проекты и системы
- [Перечисли найденные проекты и системы — ТОЛЬКО названия из данных поиска]

### Сотрудники
- [Перечисли найденных людей]

### KPI и метрики
- [Перечисли найденные KPI]

## Выводы

[Твои выводы на основе данных]

---
TERMINATE
""",
            llm_config=self.llm_config,
        )

        # Регистрация инструментов для Researcher
        # Оборачиваем методы класса в функции, чтобы AutoGen мог их использовать
        # (AutoGen иногда капризничает с методами инстансов)

        # TODO: Реализовать регистрацию инструментов
        # Пока просто заглушка, так как register_function требует глобальных функций или методов

    async def register_tools(self):
        """Регистрация инструментов для Researcher"""
        researcher = self.agents["researcher"]
        user_proxy = self.agents["user_proxy"] # Инструменты исполняются здесь или в researcher

        # Врапперы для методов tools
        # AutoGen 0.2+ поддерживает async функции
        # Добавляем задержку между вызовами чтобы избежать Gemini rate limit

        async def search_knowledge(query: str) -> str:
            await asyncio.sleep(0.5)  # Задержка 0.5 сек перед запросом
            return await self.tools.search_knowledge(query)

        async def get_project_status(project_name: str) -> str:
            await asyncio.sleep(0.5)
            return await self.tools.get_project_status(project_name)

        async def get_person_info(person_name: str) -> str:
            await asyncio.sleep(0.5)
            return await self.tools.get_person_info(person_name)

        async def get_anomalies() -> str:
            await asyncio.sleep(0.5)
            return await self.tools.get_anomalies_and_risks()

        # Регистрация
        register_function(
            search_knowledge,
            caller=researcher,
            executor=user_proxy, # Инструменты выполняются в user_proxy (он умеет выполнять код/функции)
            name="search_knowledge",
            description="Семантический поиск по базе знаний (встречи, задачи, решения)."
        )

        register_function(
            get_project_status,
            caller=researcher,
            executor=user_proxy,
            name="get_project_status",
            description="Получить статус проекта, включая задачи и участников."
        )

        register_function(
            get_person_info,
            caller=researcher,
            executor=user_proxy,
            name="get_person_info",
            description="Узнать, чем занимается сотрудник."
        )

        register_function(
            get_anomalies,
            caller=researcher,
            executor=user_proxy,
            name="get_anomalies",
            description="Получить список аномалий и рисков."
        )

    def _extract_search_results(self, messages: list) -> str | None:
        """
        Извлекает результаты search_knowledge() из сообщений.
        Если Analyst не успел ответить, но search_knowledge() вернул данные -
        собираем ВСЕ ответы и синтезируем единый результат.

        AG2 tool results могут быть:
        1. В отдельных сообщениях (tool_responses key)
        2. В content сообщений User_Proxy обёрнутых ***** Response from calling tool (ID) *****
        3. В content с несколькими JSON блоками через разделители
        """
        import re

        logger.info(f"AG2: _extract_search_results called with {len(messages) if messages else 0} messages")

        all_answers = []
        all_sources = []

        for msg in messages:
            if not isinstance(msg, dict):
                continue

            # Проверяем tool_responses в сообщении (AG2 формат)
            tool_responses = msg.get("tool_responses", [])
            if tool_responses:
                for tr in tool_responses:
                    self._try_extract_answer(tr.get("content", ""), all_answers, all_sources)
                continue

            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                continue

            # Если в content несколько JSON блоков через разделители
            if "Response from calling tool" in content:
                # Разделяем по маркерам ***** Response from calling tool (XXXX) *****
                parts = re.split(r'\*{3,}\s*Response from calling tool.*?\*{3,}', content)
                for part in parts:
                    part = part.strip().strip('*').strip('-').strip()
                    if part:
                        self._try_extract_answer(part, all_answers, all_sources)
            elif '"answer":' in content and '"query":' in content:
                self._try_extract_answer(content, all_answers, all_sources)

        if not all_answers:
            logger.info("AG2: No search_knowledge answers found in messages")
            return None

        # Синтезируем единый ответ из всех найденных данных
        result = "## Результаты анализа базы знаний\n\n"

        # Объединяем ответы, убирая дубликаты
        seen = set()
        for answer_data in all_answers:
            query = answer_data.get("query", "")
            answer = answer_data.get("answer", "")
            if answer and answer not in seen:
                seen.add(answer)
                if query:
                    result += f"### {query}\n\n"
                result += f"{answer}\n\n"

        # Добавляем уникальные источники
        if all_sources:
            result += "\n**Ключевые источники:**\n"
            seen_sources = set()
            for src in all_sources[:10]:
                src_name = src.get("name", "")
                src_type = src.get("type", "")
                if src_name and src_name not in seen_sources:
                    seen_sources.add(src_name)
                    result += f"- [{src_type}] {src_name}\n"

        logger.info(f"AG2: Extracted {len(all_answers)} search_knowledge answers, total length={len(result)}")
        return result

    def _try_extract_answer(self, text: str, all_answers: list, all_sources: list):
        """Пытается извлечь JSON ответ search_knowledge из текста."""
        import json

        if not text or not isinstance(text, str):
            return

        # Пробуем найти JSON в тексте
        json_start = text.find('{')
        if json_start < 0:
            return

        # Ищем соответствующую закрывающую скобку с учётом вложенности
        depth = 0
        json_end = -1
        for i in range(json_start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    json_end = i + 1
                    break

        if json_end <= json_start:
            return

        try:
            json_str = text[json_start:json_end]
            data = json.loads(json_str)

            answer = data.get("answer", "")
            if answer and len(answer) > 30:
                all_answers.append(data)
                sources = data.get("sources", [])
                for src in sources:
                    if src.get("name"):
                        all_sources.append(src)
        except (json.JSONDecodeError, KeyError):
            pass

    async def _synthesize_from_search_results(self, query: str, search_results: str) -> str | None:
        """
        Синтезирует финальный ответ из search_results через LLM.
        Используется как fallback когда Analyst не успел ответить, но данные есть.
        """
        try:
            # Через LLM-роутер (не прямой genai) — иначе air-gap/локальный
            # Qwen не обслуживал этот fallback и он молча падал в None.
            from backend.core.llm.router import get_llm_router

            prompt = f"""Ты — бизнес-аналитик. На основе найденных данных из базы знаний компании дай структурированный ответ на вопрос пользователя.

ВОПРОС: {query}

НАЙДЕННЫЕ ДАННЫЕ:
{search_results[:8000]}

ПРАВИЛА:
- Отвечай на РУССКОМ языке
- Используй ТОЛЬКО данные из раздела "НАЙДЕННЫЕ ДАННЫЕ"
- НЕ выдумывай факты
- Если данных недостаточно, честно скажи что известно и что нет
- Структурируй ответ с заголовками и списками

Дай развёрнутый ответ:"""

            text = await get_llm_router().generate(prompt=prompt,
                                                   temperature=0.3,
                                                   max_tokens=2048)
            if text and len(text) > 50:
                logger.info(f"AG2: Synthesized answer from search results, length={len(text)}")
                return text

        except Exception as e:
            logger.warning(f"AG2: Failed to synthesize from search results: {e}")

        return None

    async def run(self, query: str) -> str:
        """Запуск цикла рассуждения"""

        await self.register_tools()

        # Создаем групповой чат с ограничением переходов
        # Это предотвращает зацикливание Critic
        allowed_transitions = {
            self.agents["user_proxy"]: [self.agents["planner"], self.agents["researcher"]],
            self.agents["planner"]: [self.agents["researcher"], self.agents["analyst"]],
            self.agents["researcher"]: [self.agents["critic"], self.agents["analyst"]],
            self.agents["critic"]: [self.agents["analyst"], self.agents["planner"]],  # Critic ДОЛЖЕН передать Analyst
            self.agents["analyst"]: [self.agents["user_proxy"]],  # Analyst завершает
        }

        groupchat = GroupChat(
            agents=[
                self.agents["user_proxy"],
                self.agents["planner"],
                self.agents["researcher"],
                self.agents["critic"],
                self.agents["analyst"]
            ],
            messages=[],
            max_round=int(os.getenv("AG2_MAX_ROUNDS", "10")), # Уменьшаем до 10, чтобы не упираться в rate limit
            speaker_selection_method="auto",
            allowed_or_disallowed_speaker_transitions=allowed_transitions,
            speaker_transitions_type="allowed"
        )

        manager = GroupChatManager(
            groupchat=groupchat,
            llm_config=self.llm_config
        )

        # Запускаем чат
        logger.info(f"[AG2] Starting GroupChat for query: {query}")

        # В AutoGen 0.2 initiate_chat может быть синхронным или асинхронным
        # Используем a_initiate_chat если доступен

        result = None
        error_occurred = False

        try:
            result = await self.agents["user_proxy"].a_initiate_chat(
                manager,
                message=query
            )
            logger.info(f"AG2 result type: {type(result)}")

        except TypeError as e:
            # Ошибка "object of type 'NoneType' has no len()" - баг в AutoGen Gemini клиенте
            # Происходит когда Gemini возвращает пустой ответ (rate limiting, content filter)
            # Но к этому моменту уже могут быть полезные сообщения в groupchat
            if "NoneType" in str(e) and "len" in str(e):
                logger.warning(f"AG2 Gemini API returned empty response (rate limit or content filter): {e}")
                error_occurred = True
            else:
                raise
        except Exception as e:
            logger.error(f"AG2 Error: {e}")
            import traceback
            logger.error(f"AG2 Traceback: {traceback.format_exc()}")
            error_occurred = True

        # Функция для извлечения ответа из сообщений
        def extract_response_from_messages(messages: list, require_analyst: bool = True) -> str | None:
            """
            Извлекает финальный ответ из сообщений.

            Args:
                messages: Список сообщений из groupchat
                require_analyst: Если True, возвращает только ответ от Analyst.
                                 Если False, возвращает ответ от любого агента.
            """
            if not messages or not isinstance(messages, list):
                return None

            # Сначала ищем ответ от Analyst (это должен быть финальный ответ!)
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("name") == "Analyst":
                    content = msg.get("content")
                    if content and isinstance(content, str) and len(content) > 50:
                        return content.replace("TERMINATE", "").strip()

            # Если require_analyst=True и Analyst не нашли - возвращаем None
            # чтобы система знала, что полный цикл не завершён
            if require_analyst:
                return None

            # Fallback: ищем ответ от Critic (если он подтвердил готовность)
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("name") == "Critic":
                    content = msg.get("content")
                    if content and isinstance(content, str) and len(content) > 50:
                        return content.replace("TERMINATE", "").strip()

            # Fallback 2: ищем ответ от Researcher (содержит данные поиска)
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("name") == "Researcher":
                    content = msg.get("content")
                    if content and isinstance(content, str) and len(content) > 100:
                        # Researcher может содержать tool calls — пропускаем их
                        if "tool_calls" not in str(content) and "search_knowledge" not in content:
                            return content.replace("TERMINATE", "").strip()

            # Fallback 3: ищем синтезированный ответ от Planner
            # Planner может дать хороший ответ если он уже проанализировал данные
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("name") == "Planner":
                    content = msg.get("content")
                    if content and isinstance(content, str) and len(content) > 200:
                        # Проверяем что это не просто план, а синтезированный ответ
                        # План обычно содержит "Исследователь", "Задача:", "Шаг"
                        content_lower = content.lower()
                        is_plan = any(kw in content_lower for kw in [
                            "исследователь", "задача:", "шаг 1", "шаг 2",
                            "план:", "действие:", "действия:", "researcher"
                        ])
                        if not is_plan:
                            return content.replace("TERMINATE", "").strip()

            # Последний fallback: любое осмысленное сообщение
            for msg in reversed(messages):
                if isinstance(msg, dict):
                    content = msg.get("content")
                    name = msg.get("name", "")
                    # Пропускаем User_Proxy
                    if name == "User_Proxy":
                        continue
                    if content and isinstance(content, str) and len(content) > 100:
                        return content.replace("TERMINATE", "").strip()
            return None

        # Пробуем извлечь ответ из разных источников
        response = None

        # Получаем все сообщения - ВСЕГДА берём из groupchat, т.к. он содержит все сообщения
        all_messages = []

        # Сначала пробуем из groupchat (самый надёжный источник)
        if hasattr(manager, 'groupchat') and hasattr(manager.groupchat, 'messages'):
            all_messages = manager.groupchat.messages
            logger.info(f"AG2: Got {len(all_messages)} messages from groupchat")

        # Если groupchat пустой, пробуем из result.chat_history
        if not all_messages and result is not None:
            chat_history = getattr(result, 'chat_history', None)
            if chat_history:
                all_messages = chat_history
                logger.info(f"AG2: Got {len(all_messages)} messages from chat_history")

        # Логируем какие агенты отвечали
        agent_names = [msg.get("name") for msg in all_messages if isinstance(msg, dict)]
        logger.info(f"AG2: Agents who responded: {agent_names}")

        # 1. Сначала пробуем получить ответ от Analyst (полный цикл)
        response = extract_response_from_messages(all_messages, require_analyst=True)

        if response:
            logger.info(f"AG2: Got response from Analyst, length={len(response)}")
            return response

        # 2. Если Analyst не ответил, но есть ошибка - цикл прервался раньше
        if error_occurred:
            # Ищем результаты search_knowledge() - они содержат реальные данные!
            search_results = self._extract_search_results(all_messages)
            if search_results:
                logger.info("AG2: Found search_knowledge results, synthesizing answer...")
                # Пробуем синтезировать ответ через LLM
                synthesized = await self._synthesize_from_search_results(query, search_results)
                if synthesized:
                    return synthesized
                # Если LLM недоступен, отдаём raw results
                return search_results

            # Пробуем получить хоть что-то полезное (НО не план Planner'а)
            fallback_response = extract_response_from_messages(all_messages, require_analyst=False)
            if fallback_response:
                logger.warning("AG2: Analyst didn't respond, using fallback. Error occurred during chat.")
                return f"{fallback_response}\n\n---\n*Примечание: Анализ был прерван из-за ошибки API. Это промежуточный результат.*"
            return "Произошла ошибка при обработке запроса (Gemini API rate limit). Агенты не успели завершить анализ. Попробуйте через несколько секунд."

        # 3. Проверяем result.summary
        if result is not None:
            summary = getattr(result, 'summary', None)
            if summary and isinstance(summary, str) and len(summary) > 50:
                logger.info(f"AG2: Using result.summary, length={len(summary)}")
                return summary

        # 4. Если нет ошибки, но Analyst не ответил - возможно max_rounds исчерпан
        fallback_response = extract_response_from_messages(all_messages, require_analyst=False)
        if fallback_response:
            logger.warning("AG2: Analyst didn't respond, using fallback (max_rounds reached?)")
            return fallback_response

        return "Не удалось получить ответ от аналитика. Попробуйте переформулировать вопрос."

