# -*- coding: utf-8 -*-
"""
Feature Flags - Система управления функциональностью.

Позволяет постепенно включать новые функции без риска сломать production.

Использование:
```python
from backend.core.config import flags

if flags.enable_graph_weights:
    # Используем веса узлов
    node_weight = node.get("weight", 1.0)
```

Конфигурация:
1. Через ENV переменные: TESSENT_ENABLE_GRAPH_WEIGHTS=true
2. Через JSON файл: config/feature_flags.json
3. Программно: flags.enable_graph_weights = True
"""
import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class FeatureFlags:
    """
    Флаги функций для постепенного включения новых возможностей.

    Все флаги по умолчанию выключены для безопасности.
    Для продакшена рекомендуется включить через config/feature_flags.json
    или через ENV переменные (TESSENT_ENABLE_<FLAG_NAME>=true).

    ╔═══════════════════════════════════════════════════════════════════╗
    ║  СПРАВОЧНИК ФЛАГОВ                                              ║
    ╠═══════════════════════════════════════════════════════════════════╣
    ║                                                                   ║
    ║  enable_graph_weights                                             ║
    ║  ─────────────────────                                            ║
    ║  Что делает: Добавляет к узлам графа поля weight, access_count,  ║
    ║  frequency_score, а к рёбрам — weight, co_occurrences,           ║
    ║  description. Позволяет ранжировать узлы по важности.            ║
    ║  Где используется: graph_builder.py, reasoning_engine.py         ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: нет (только больше свойств на узлах)               ║
    ║                                                                   ║
    ║  enable_graphiti                                                   ║
    ║  ────────────────                                                  ║
    ║  Что делает: Включает Graphiti — temporal knowledge graph.       ║
    ║  Хранит эпизоды (встречи, события) с временными метками.         ║
    ║  Позволяет запросы "что было в январе?", "что изменилось?"       ║
    ║  Где используется: graphiti_client.py, knowledge_sync.py         ║
    ║  Зависимости: Neo4j, пакет graphiti-core                         ║
    ║  Доп. расход: нет (только дополнительная запись в Neo4j)         ║
    ║                                                                   ║
    ║  enable_frequency_tracking                                        ║
    ║  ─────────────────────────                                        ║
    ║  Что делает: Считает частоту упоминаний каждой сущности.         ║
    ║  Чем чаще упоминается — тем выше приоритет при поиске.           ║
    ║  Где используется: knowledge_sync.py, graph_builder.py           ║
    ║  Зависимости: enable_graph_weights (для хранения score)          ║
    ║  Доп. расход: нет                                                 ║
    ║                                                                   ║
    ║  enable_hebbian_learning                                          ║
    ║  ───────────────────────                                          ║
    ║  Что делает: "Нейроны, которые вместе активируются, связываются  ║
    ║  вместе." Когда две сущности упоминаются в одном контексте —     ║
    ║  связь между ними усиливается (weight растёт).                   ║
    ║  Где используется: knowledge_sync.py, memory_consolidator.py     ║
    ║  Зависимости: enable_graph_weights                                ║
    ║  Доп. расход: нет                                                 ║
    ║                                                                   ║
    ║  enable_temporal_decay                                            ║
    ║  ────────────────────                                              ║
    ║  Что делает: Связи, которые давно не упоминались, затухают       ║
    ║  (weight уменьшается). Имитирует "забывание" неактуальной        ║
    ║  информации. Применяется ночью в NightlyConsolidation.           ║
    ║  Где используется: nightly_consolidation.py, decay_manager.py    ║
    ║  Зависимости: enable_graph_weights, enable_hebbian_learning      ║
    ║  Доп. расход: нет                                                 ║
    ║                                                                   ║
    ║  enable_emotional_salience                                        ║
    ║  ─────────────────────────                                        ║
    ║  Что делает: Анализирует эмоциональную значимость информации.    ║
    ║  Срочные/негативные факты получают более высокий приоритет.       ║
    ║  "Проект горит!" важнее чем "обновили документацию".             ║
    ║  Где используется: capture/orchestrator.py, knowledge_sync.py    ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: ~0.001$/встречу (мини-LLM вызов)                   ║
    ║                                                                   ║
    ║  enable_enhanced_snapshots                                        ║
    ║  ─────────────────────────                                        ║
    ║  Что делает: Включает полные снэпшоты компании/сотрудников/      ║
    ║  проектов. NoCap-style бизнес-профили с LLM-обогащением.         ║
    ║  Добавляет CompanySnapshot в контекст чат-ответов.               ║
    ║  Где используется: enhanced_snapshot.py, chat.py, snapshots.py   ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: ~0.02$/генерацию (LLM enrichment)                  ║
    ║                                                                   ║
    ║  enable_agent_memory                                              ║
    ║  ───────────────────                                               ║
    ║  Что делает: Персистентная память для агентов системы.            ║
    ║  4 типа: episodic (события), semantic (факты), procedural        ║
    ║  (как делать), meta (о себе). Консолидируется ночью.             ║
    ║  Где используется: agent_memory.py, nightly_consolidation.py     ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: нет                                                 ║
    ║                                                                   ║
    ║  enable_validation_agent                                          ║
    ║  ───────────────────────                                          ║
    ║  Что делает: Перед сохранением в граф — валидация извлечённых     ║
    ║  данных. Проверяет типы сущностей, консистентность связей,       ║
    ║  дедупликацию. Снижает мусор в графе.                            ║
    ║  Где используется: knowledge_sync.py                              ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: ~0.005$/встречу                                    ║
    ║                                                                   ║
    ║  enable_two_phase_corrections                                     ║
    ║  ────────────────────────────                                     ║
    ║  Что делает: Двухфазные исправления. Днём коррекции              ║
    ║  буферизуются (не применяются сразу). Ночью — review и           ║
    ║  применение одобренных. Защищает от случайных изменений.         ║
    ║  Где используется: two_phase_manager.py,                          ║
    ║                     nightly_consolidation.py                      ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: нет                                                 ║
    ║                                                                   ║
    ║  enable_user_feedback_learning                                    ║
    ║  ─────────────────────────────                                    ║
    ║  Что делает: Обучение на обратной связи пользователя.            ║
    ║  Если пользователь исправил ответ → обновить граф.               ║
    ║  Если поставил 👍 → усилить связи. Если 👎 → пересмотреть.      ║
    ║  Где используется: user_profiles.py, chat.py                     ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: нет                                                 ║
    ║                                                                   ║
    ║  enable_external_data_filtering                                   ║
    ║  ──────────────────────────────                                   ║
    ║  Что делает: Фильтрация данных, не относящихся к компании.       ║
    ║  Если на встрече обсуждали погоду или личные дела —               ║
    ║  эти данные не попадают в граф знаний.                            ║
    ║  Где используется: knowledge_sync.py, orchestrator.py            ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: ~0.001$/встречу                                    ║
    ║                                                                   ║
    ║  enable_graph_audit_log                                           ║
    ║  ──────────────────────                                            ║
    ║  Что делает: Логирование ВСЕХ изменений графа в аудит-лог.      ║
    ║  Кто создал узел, когда, что изменилось. Для compliance          ║
    ║  и отслеживания истории изменений.                                ║
    ║  Где используется: graph_builder.py                               ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: нет (дополнительная запись в файл/БД)              ║
    ║                                                                   ║
    ║  enable_detailed_metrics                                          ║
    ║  ───────────────────────                                          ║
    ║  Что делает: Детальные метрики и телеметрия. Время обработки     ║
    ║  каждого этапа, количество токенов, качество извлечения.         ║
    ║  Также включает dream-inspired memory consolidation ночью.       ║
    ║  Где используется: везде (metrics collection points)             ║
    ║  Зависимости: нет                                                 ║
    ║  Доп. расход: нет                                                 ║
    ║                                                                   ║
    ╚═══════════════════════════════════════════════════════════════════╝
    """

    # === ЭТАП 1: Расширение графа ===
    # Добавляет weight/access_count/frequency_score к узлам, weight/co_occurrences к рёбрам.
    # Используется для ранжирования узлов по важности при поиске.
    enable_graph_weights: bool = False

    # === ЭТАП 2: Интеграция Graphiti ===
    # Temporal knowledge graph: хранит эпизоды с временными метками.
    # Позволяет запросы по времени ("что обсуждали в январе?").
    # Требует: Neo4j, pip install graphiti-core
    enable_graphiti: bool = False

    # === ЭТАП 3: Усиление памяти ===
    # Подсчёт частоты упоминаний сущностей. Чаще упоминается → выше приоритет.
    enable_frequency_tracking: bool = False

    # Hebbian Learning: совместное упоминание усиливает связь (weight растёт).
    # "Neurons that fire together wire together."
    enable_hebbian_learning: bool = False

    # Temporal decay: неупоминаемые связи затухают (weight уменьшается ночью).
    # Имитация "забывания" неактуальной информации.
    enable_temporal_decay: bool = False

    # === ЭТАП 4: Эмоциональная приоритизация ===
    # Срочные/негативные факты получают более высокий приоритет.
    # "Проект горит!" > "Обновили документацию". Доп. расход: ~0.001$/встречу.
    enable_emotional_salience: bool = False

    # === ЭТАП 5: Расширенные снапшоты ===
    # NoCap-style бизнес-профили: CompanySnapshot/PersonSnapshot/ProjectSnapshot.
    # LLM-обогащение. CompanySnapshot используется как контекст для чат-ответов.
    # Доп. расход: ~0.02$/генерацию.
    enable_enhanced_snapshots: bool = False

    # === ЭТАП 6: Агентская память ===
    # 4 типа памяти: episodic (события), semantic (факты),
    # procedural (как делать), meta (о себе). Консолидируется ночью.
    enable_agent_memory: bool = False

    # === ЭТАП 7: Валидация и исправления ===
    # Валидация извлечённых данных перед сохранением (типы, связи, дедупликация).
    # Снижает мусор в графе. Доп. расход: ~0.005$/встречу.
    enable_validation_agent: bool = False

    # Двухфазные исправления: днём коррекции буферизуются,
    # ночью — review и применение одобренных. Защита от ошибок.
    enable_two_phase_corrections: bool = False

    # === Дополнительные флаги ===
    # Обучение на feedback: 👍 усиливает связи, 👎 пересматривает,
    # исправление ответа → обновляет граф.
    enable_user_feedback_learning: bool = False

    # W7: Профиль-куратор (LLM-driven), запускается в nightly_consolidation.
    # Анализирует последние сессии и предлагает обновления профиля
    # (стиль, язык, текущий фокус) с порогом confidence>=0.6.
    # Доп. расход: 1 LLM-вызов/юзер/ночь. Опционально — рекомендуется ON в проде.
    enable_profile_curator: bool = False

    # Фильтрация данных не о компании (погода, личное).
    # Доп. расход: ~0.001$/встречу.
    enable_external_data_filtering: bool = False

    # Запись ЧАТА в память (пара user_message+assistant_response → vector_indexer
    # .add_document, как «беседа»). Третий контекст памяти после встреч и
    # документов (см. docs/ru/MEMORY_DESIGN_PRINCIPLES.md §6). Недеструктивно — без
    # lossy-извлечения; полный текст сохраняется как retrievable документ.
    # Fire-and-forget, не блокирует ответ чата. Default False — продукт
    # байт-в-байт прежний при выключенном флаге.
    enable_chat_memory_ingestion: bool = False

    # Кросс-источниковые упоминания (MEMORY_DESIGN_PRINCIPLES.md §6 №5):
    # при индексации сущностей встречи дополнительно фиксируем «упоминание
    # сущности из источника» в per-user cross_source_mentions store. Это собирает
    # мета-факт «где/когда/как часто упоминали сущность» через встречи/документы/
    # чаты. Best-effort (не роняет индексацию), идемпотентно (повтор встречи не
    # задваивает). Default False — индексация байт-в-байт прежняя.
    enable_cross_source_mentions: bool = False

    # Доменные события встречи в EventLog (MEMORY_DESIGN_PRINCIPLES.md §6 №4):
    # при индексации встречи фиксируем структурные decisions/tasks как датированные
    # события (decision_made/task_created) → реальный СЧЁТ во времени («сколько
    # решений в Q3», «сколько задач»). Non-lossy (из уже-структурных данных),
    # идемпотентно (event_id = kind::meeting::id), best-effort. Default False.
    enable_event_log_ingestion: bool = False

    # Контекст-зависимая гранулярность чанков (MEMORY_DESIGN_PRINCIPLES.md §6 №6,
    # §2 «не один размер на всех»): document_indexer берёт chunk_size/overlap из
    # granularity_policy по context (chat мелко / document широко / meeting средне).
    # Профиль 'document' == текущие DEFAULT (1500/200) → при context='document'
    # поведение ИДЕНТИЧНО. Ценность — при индексации чат-контента (context='chat').
    # Default False — поведение прежнее.
    enable_context_granularity: bool = False

    # Abstention в QUICK-чате (MEMORY_DESIGN_PRINCIPLES.md §6 №7, §8 — ОСТОРОЖНО):
    # ТОЛЬКО для search_depth='quick' И пустого ретрива И НЕ-синтез-вопроса —
    # честное «нет в памяти» вместо возможной галлюцинации. Синтез-вопросы (кого
    # нанять, стратегия, ...) ЖЁСТКО защищены: всегда компилируются, не абстенят.
    # НЕ для standard/deep/ТЗ. Default False — чат байт-в-байт прежний.
    enable_quick_abstention: bool = False

    # Зеркалирование BWE evolution_log в SupersedeStore (MEMORY_DESIGN_PRINCIPLES
    # §6 №3, §7 — недеструктивный supersede). pattern_encoder уже пишет свой
    # evolution_log (порт concept_crystallizer) — мы лишь дублируем в наш стор
    # с унифицированным API (current/history/count_distinct_values/total_mentions).
    # Best-effort: BWE-логика НЕ трогается, ошибка зеркала подавляется. Default
    # False — BWE байт-в-байт прежняя.
    enable_supersede_mirror: bool = False

    # Темпоральный буст в ретриве (MEMORY_DESIGN_PRINCIPLES.md §6, пункт №2).
    # Темпоральные поля (date/decided_at/deadline/created_at/indexed_at) УЖЕ лежат
    # в graph/vector payload, но не используются в hybrid_search. После RRF
    # применяется небольшой буст по близости даты документа к now (или as_of).
    # Гарантии гардрейла §1: buster ≤+20%, релевантность не доминируется; на
    # документах без дат буст=0; оригиналы не мутируются. Default False —
    # поведение прежнее. Полезно для «когда решили X», recency, «последнее по Y».
    enable_temporal_retrieval_boost: bool = False

    # Answer-mode router в чат-синтезе (MEMORY_DESIGN_PRINCIPLES §8; замер —
    # docs/ru/BENCHMARK_PUBLIC_LOCOMO_HOTPOT.md / BENCHMARK_ANALYSIS_MODE.md).
    # Для вопросов про ДИНАМИКУ/ИСТОРИЮ/мультисессию/синтез («когда пошло не так»,
    # «вся история проекта», «как идёт сейчас», «кого нанять») усиливает синтез
    # инструкцией: соединяй факты ПО СЕССИЯМ/датам, рассуждай о ПОРЯДКЕ и
    # ИЗМЕНЕНИИ во времени, компилируй ответ (не отказывайся преждевременно),
    # но отделяй факты от выводов и не выдумывай при пустых данных. Для прямых
    # фактических lookup'ов режим не меняется. Замер на ДЕШЁВОЙ модели:
    # multi-session 0.143→0.714, temporal +0.27, HotpotQA multi-hop +0.20.
    # Только дополняет system_prompt синтеза; ретрив/данные не трогает.
    # Surface: response_metadata['answer_mode']. Default False — синтез прежний.
    enable_answer_mode_router: bool = False

    # Автономное ВЫПОЛНЕНИЕ задач (аудит: было TODO-заглушкой). Когда true —
    # execution_mode='autonomous' реально исполняет инструменты из tool_registry
    # через TaskExecutorAgent. ОПАСНО (реальные побочные действия) — default OFF:
    # autonomous деградирует в supervised (детальный план + ожидание подтверждения,
    # без выполнения). supervised работает всегда. См. docs/ru/PRODUCTION_HARDENING.md.
    enable_autonomous_execution: bool = False

    # Service-аутентификация (аудит S1, БЕЗОПАСНОСТЬ): когда true — сервисные
    # endpoints (/compose; /chat при service-вызове) ТРЕБУЮТ валидный
    # service-JWT (подпись TESSENT_SERVICE_JWT_SECRET), а user_id берётся из
    # claim токена, НЕ из произвольного query/body. Закрывает дыру «любой с
    # ?user_id=X получает данные X». См. docs/ru/PRODUCTION_HARDENING.md S1.
    # issue #107 T-2: дефолт переведён на True (раньше OFF). Внешние клиенты
    # (AI-Coach, Report-Automation, standup_dailypulse, Analytics) должны
    # начать слать service-JWT. Откатить локально: TESSENT_ENABLE_SERVICE_AUTH=false.
    enable_service_auth: bool = True

    # M4 (аудит): SQLite-бэкенд для append-only сторов (event_log,
    # cross_source_mentions). Решает O(n²)-запись (пересериализация всего JSON
    # на каждое событие) и конкурентность процессов (WAL вместо flock).
    # При первом включении старый JSON автоматически импортируется в
    # data/<store>/{user_id}.sqlite3 (JSON не удаляется — страховка).
    # ВАЖНО: после включения новые записи идут только в SQLite — откат флага
    # без экспорта потеряет записи sqlite-периода. Default OFF.
    enable_sqlite_stores: bool = False

    # Ops-рубильник запуска кодинг-агента ПОСЛЕ подтверждения пользователя.
    # Сам по себе НИЧЕГО не запускает: handoff всегда создаёт PENDING-запись,
    # исполнение происходит только через явный confirm пользователя
    # (POST /task-analysis/handoff/{id}/confirm). Флаг false → confirm
    # отвечает отказом и отдаёт команду для ручного запуска. Включать в
    # доверенной среде, где у агента (claude/cursor/codex) есть доступ к репо.
    # Дефолт ON: запуск и так гейтится явным confirm per-task; выключить —
    # ENABLE_CODING_HANDOFF_EXEC=false.
    enable_coding_handoff_exec: bool = True

    # Режим «собери артефакт кодом» (презентация/КП/таблица) БЕЗ git-репо:
    # кодинг-агент (Claude Code/Codex) запускается по подписке в ИЗОЛИРОВАННОЙ
    # временной папке, создаёт файл-результат (html/pptx/xlsx/pdf/…), система
    # собирает эти файлы и отдаёт человеку. Отдельный рубильник (НЕ путать с
    # enable_coding_handoff_exec — тот про запуск в существующем репозитории):
    # исполнение произвольной сборки в scratch-папке несёт свой профиль риска,
    # поэтому по умолчанию OFF. Гейт подтверждения человеком сохраняется.
    # Включать в доверенной среде: TESSENT_ENABLE_HANDOFF_ARTIFACT_MODE=true.
    enable_handoff_artifact_mode: bool = False

    # ПРОАКТИВНЫЙ цикл инсайтов: планировщик раз в PROACTIVE_SCAN_HOURS
    # (env, default 6ч) сам прогоняет сканеры (blind spots, рассинхроны,
    # аномалии, сигнатуры встреч) по каждому пользователю и складывает
    # НОВЫЕ находки в ленту инсайтов — система подсказывает то, чего
    # человек не видит, без его участия. Идемпотентно (дубли не плодятся).
    enable_proactive_insight_scan: bool = False

    # Доставка проактивных инсайтов в Telegram: когда true — НОВЫЕ находки
    # проактивного скана уходят через reactive-гейт в каналы (telegram).
    # Гейт сам решает, что слать (confidence/калибровка/нагрузка) и не
    # повторяется (анти-повтор по source_id). Default OFF — не спамить, пока
    # канал не настроен. Требует enable_proactive_insight_scan.
    # Включено по решению владельца продукта (2026-08-13): найденный ночью
    # инсайт, который никто не увидел, — не сигнал, а запись в журнале.
    enable_proactive_telegram_delivery: bool = True

    # Наблюдатель Ф1: структурный снапшот каждой встречи при синке
    # (blockers/tensions/explicit_asks/... — core/observer/meeting_snapshot).
    # Один дешёвый LLM-вызов на встречу, кэш навсегда. Несущий примитив для
    # проактивного агента (фронты/outcome) и досок.
    # ВКЛЮЧЕНО по умолчанию решением владельца (21.07.2026); выключение —
    # env ENABLE_MEETING_SNAPSHOTS=false.
    enable_meeting_snapshots: bool = True

    # Наблюдатель Ф2: фоновый цикл агента (скрин фронтов по снапшотам →
    # ротация → живая зацепка в память агента + ленту инсайтов). Требует
    # enable_meeting_snapshots (без снапшотов циклы вхолостую). Каденс —
    # OBSERVER_CYCLE_DAYS (env, default 2 дня на пользователя).
    # ВКЛЮЧЕНО по умолчанию решением владельца (21.07.2026).
    enable_observer_agent: bool = True

    # Наблюдатель Ф3: зацепки и follow-up'ы («я проверил») → Telegram через
    # reactive-гейт (нагрузка/калибровка/дедуп). Кнопок в TG нет — реакции
    # на главной. ВКЛЮЧЕНО по умолчанию решением владельца (21.07.2026);
    # гейт сам бережёт от спама, без TG-привязки тихо no-op.
    enable_observer_telegram_delivery: bool = True

    # Kanon Protocol в SIMA (перенос методологии из sima_atlas, см.
    # docs/SIMA_KANON.md): контракты блоков из существующих полей SIMA,
    # tri-state приёмка (pass/fail/inconclusive, детерминированные
    # коллекторы), cascade-верификация с desync, экспорт Kanon-workspace
    # для кодинг-агентов (Claude Code/Cursor/Codex). Запуск агента —
    # ТОЛЬКО через двухфазный handoff с явным подтверждением пользователя.
    # Включён по умолчанию ПОСЛЕ харденинга Ф0.1: исполнение проверок —
    # shell=False всегда, shlex-разбор с отказом при shell-метасимволах,
    # allowlist известных инструментов (вне списка → inconclusive,
    # не исполняется). RCE-путь закрыт — отказ по флагу больше не нужен.
    enable_sima_kanon: bool = True

    # Онтология данных («палантир-модуль», docs/ru/DATA_ONTOLOGY.md):
    # /api/v1/ontology/datasets/* — регистрация таблиц/выгрузок (inline или
    # по URL), детерминированный профиль, заземление колонок в контексте
    # компании (TableUnderstandingAgent + граф), связи между датасетами,
    # вопросы цифрами (LLM строит ТОЛЬКО план, считает код), честная
    # оценка результата. Default OFF: endpoints отвечают отказом.
    enable_data_ontology: bool = False

    # Регулярный inbound-sync датасетов (A1): тянуть данные из подключённых
    # CRM/BI/Sheets по расписанию (interval/daily/weekly), а не по кнопке.
    # Планировщик обходит датасеты с refresh_schedule и зовёт refresh_dataset,
    # пишет наблюдаемость (last_at/status/rows/error).
    # ВКЛЮЧЕНО 2026-08-13 (аудит бизнес-карты, раздел 16): расписания
    # задавались, но авто-прогон не бежал — «синхронизация есть» была
    # правдой только наполовину. Прогон трогает лишь датасеты, у которых
    # владелец сам задал расписание; без расписания поведение прежнее.
    # Откат: false.
    enable_dataset_sync: bool = True

    # Корпоративный словарь синонимов/акронимов (Glean-перенос №3,
    # docs/GLEAN_ADOPTION.md): запрос расширяется алиасами компании
    # («КП»↔«коммерческое предложение») ПЕРЕД поиском. Детерминированно,
    # без LLM; пустой словарь → no-op. Default OFF — поиск байт-в-байт
    # прежний.
    enable_company_glossary: bool = False

    # Экспертный роутинг чата (Glean-перенос №2 в безопасной форме,
    # docs/GLEAN_ADOPTION.md): вопрос «кто знает / у кого узнать про X»
    # детектится детерминированно → ответ живыми экспертами (граф +
    # co-occurrence + РОЛЬ). Ролевое ранжирование живёт только здесь —
    # общая выдача поиска не трогается вообще. Нет экспертов / не
    # кто-вопрос → обычный путь чата. Default OFF.
    enable_expert_routing: bool = False

    # Единая модель прав на ресурсы (docs/ru/ACCESS_CONTROL_DESIGN.md,
    # по образцу MeetFlow): таблица resource_permissions (user/team-гранты
    # read|write|admin + сроки), каскад по родителям, потолок ролей команд,
    # permission_audit. Флаг гейтит API управления грантами
    # (/permissions/*); сама проверка аддитивна и безопасна всегда:
    # нет грантов на ресурс → прежняя логика «моё/не моё».
    # Default ON (мульти-аккаунт P1: кнопка «Поделиться» должна работать
    # из коробки; выключение флага возвращает 403 на /permissions/* и
    # чистое owner-only поведение — без миграций и потери данных).
    enable_resource_permissions: bool = True

    # Интеграция с Daily Pulse (docs/INTEGRATION_TESSENT_RESPONSE.md):
    # ingest webhook /integrations/daily-pulse/events (HMAC) +
    # context API /integrations/context. События попадают в event_log
    # и инсайты с пометкой _source=measured|llm_narrative|human (защита
    # от циклов LLM-on-LLM, их §4.2). Default OFF: маршруты отвечают
    # 403, прежнее поведение системы не меняется.
    enable_daily_pulse_integration: bool = False

    # Интеграция с CallInsight Pro (docs/CALLINSIGHT_INTEGRATION.md):
    # webhooks звонков КЦ (X-CIP-Signature, дедуп по event_id) → инсайты/
    # event_log с _source; live-карточка клиента (snapshot/timeline их REST);
    # context-by-customer для их анализа. Домен customer_comms отделён от
    # внутренней памяти: транскрипты НЕ копируются, supersede store не
    # трогается. Default OFF: маршруты отвечают 403.
    enable_callinsight_integration: bool = False

    # Строгая аутентификация /chat (аудит, финальный шаг S1): когда true —
    # запрос БЕЗ валидного Bearer (сервисного или пользовательского JWT)
    # отклоняется. Тот же флаг управляет decode_claims_guarded (issue #107 T-1):
    # на ON невалидная подпись → {} → 401 (подделка отвергается); на OFF —
    # passthrough без проверки (compat).
    # issue #107 T-2: дефолт переведён на True (раньше OFF). Фронтенд должен
    # гарантированно слать токен; внешние клиенты — подписанные service-JWT.
    # Откатить локально: TESSENT_ENABLE_STRICT_CHAT_AUTH=false.
    enable_strict_chat_auth: bool = True

    # A2A API: внешние агенты (Mark001, MeetFlow, кастомные) общаются с мозгом
    # через REST (CAPABILITY_READINESS C13). Endpoints: GET /a2a/agents,
    # POST /a2a/route, POST /a2a/call/{name}. Ленивая инициализация —
    # экосистема (setup_tessent_ecosystem) поднимается при ПЕРВОМ запросе,
    # не при старте сервиса. Default OFF: все endpoints → 403.
    enable_a2a_api: bool = False

    # Композитор задач-артефактов в веб-чате (CAPABILITY_READINESS C11):
    # strategy='compose' в ChatRequest → POST /compose-конвейер из чата.
    # Доступно из бота с C5; теперь и из веб-интерфейса. Default OFF:
    # стратегия compose → fallback на enhanced_search.
    enable_chat_compose_strategy: bool = False

    # Table-aware индексация документов (CAPABILITY_READINESS C10): при
    # детекте таблицы в чанке прогоняем СКЕЛЕТ TableUnderstandingAgent
    # (без LLM) и кладём в metadata: tabular, is_known, confidence,
    # column_types, matched_entity_types — поиск теперь видит, что цифры
    # «наши» (KPI компании) или сотрудники «наши». Default OFF: индексация
    # байт-в-байт прежняя.
    enable_table_aware_indexing: bool = False

    # Wisdom → критерии приёмки ТЗ (CAPABILITY_READINESS C9): опыт прошлых
    # решений (build_wisdom_brief) обогащает acceptance_criteria блоком
    # company_wisdom (soft/hard/calibration). Без доп. LLM-вызова — берём готовый
    # бриф. Default OFF: ТЗ байт-в-байт прежние. Включается отдельно от
    # wisdom_in_search_enabled — разные точки врезки.
    enable_wisdom_in_specs: bool = False

    # Персонализация ответов чата под пользователя (CAPABILITY_READINESS B4):
    # get_personalized_prompt из user_profiles (стиль/детализация/известные
    # темы) добавляется в синтез. Меняет ПОДАЧУ, не содержание; факты и
    # честность не трогаются. Код существовал, но не вызывался нигде —
    # подключено за флагом. Default OFF — синтез байт-в-байт прежний.
    enable_chat_personalization: bool = False

    # Composer API (POST /compose) — универсальный композитор для внешних
    # потребителей (mini tess / телеграм-бот / агенты): произвольная задача →
    # план → досье → артефакт → опц. оценка. Память планов (PlanMemory)
    # подключена: повторные задачи делаются запомненным способом. Default OFF —
    # endpoint отвечает 403, поведение системы не меняется.
    enable_composer_api: bool = False

    # Аудит-лог всех изменений графа: кто, когда, что изменил.
    # Для compliance и истории изменений.
    enable_graph_audit_log: bool = False

    # Детальные метрики: время этапов, токены, качество.
    # Также включает dream-inspired memory consolidation ночью.
    enable_detailed_metrics: bool = False

    # Адаптивный тиринг извлечения встречи (docs/MODEL_TIERING_RUNBOOK.md):
    # вместо ~30 вызовов дробим по tier'у модели — комбо-извлечение в 1-3
    # запроса на крупных моделях (транскрипт летит 1×, не ×30). Модуль
    # backend/core/capture/tiered_extraction.py. Default OFF: прогон встречи
    # байт-в-байт прежний, пока не включат явно. Включать после замера
    # качества харнессом scripts/bench_meeting_models.py.
    enable_tiered_extraction: bool = False

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "FeatureFlags":
        """
        Загрузка флагов из ENV и JSON файла.

        Приоритет:
        1. ENV переменные (высший)
        2. JSON файл
        3. Значения по умолчанию (низший)

        Args:
            config_path: Путь к JSON файлу конфигурации

        Returns:
            Экземпляр FeatureFlags
        """
        flags = cls()

        # 1. Загрузка из JSON файла
        # Проверяем несколько путей: относительно CWD, относительно tessent_brain/, абсолютный
        if config_path:
            json_path = Path(config_path)
        else:
            candidates = [
                Path("config/feature_flags.json"),
                Path(__file__).parent.parent.parent.parent / "config" / "feature_flags.json",
                Path("tessent_brain/config/feature_flags.json"),
            ]
            json_path = next((p for p in candidates if p.exists()), candidates[0])
        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    json_flags = json.load(f)

                for key, value in json_flags.items():
                    if hasattr(flags, key):
                        setattr(flags, key, bool(value))

                logger.info(f"✅ Feature flags loaded from {json_path}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to load feature flags from {json_path}: {e}")

        # 2. Загрузка из ENV (переопределяет JSON)
        for field_name in flags.__dataclass_fields__:
            env_name = f"TESSENT_{field_name.upper()}"
            env_value = os.getenv(env_name)

            if env_value is not None:
                bool_value = env_value.lower() in ("true", "1", "yes", "on")
                setattr(flags, field_name, bool_value)
                logger.debug(f"Feature flag {field_name} set from ENV: {bool_value}")

        # Логируем активные флаги
        active_flags = [name for name in flags.__dataclass_fields__ if getattr(flags, name)]
        if active_flags:
            logger.info(f"🚀 Active feature flags: {', '.join(active_flags)}")
        else:
            logger.info("ℹ️ No feature flags enabled")

        return flags

    def save(self, config_path: Optional[str] = None) -> bool:
        """
        Сохранить текущие флаги в JSON файл.

        Args:
            config_path: Путь к JSON файлу

        Returns:
            True если успешно сохранено
        """
        json_path = Path(config_path) if config_path else Path("config/feature_flags.json")

        try:
            json_path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
            }

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.info(f"✅ Feature flags saved to {json_path}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to save feature flags: {e}")
            return False

    def enable_all(self) -> None:
        """Включить все флаги (для тестирования)."""
        for name in self.__dataclass_fields__:
            setattr(self, name, True)
        logger.warning("⚠️ All feature flags enabled!")

    def disable_all(self) -> None:
        """Выключить все флаги."""
        for name in self.__dataclass_fields__:
            setattr(self, name, False)
        logger.info("ℹ️ All feature flags disabled")

    def get_status(self) -> dict:
        """Получить статус всех флагов."""
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }

    def __repr__(self) -> str:
        active = [name for name in self.__dataclass_fields__ if getattr(self, name)]
        return f"FeatureFlags(active={active})"


@lru_cache(maxsize=1)
def get_feature_flags() -> FeatureFlags:
    """
    Получить глобальный экземпляр флагов (с кешированием).

    Returns:
        Singleton экземпляр FeatureFlags
    """
    return FeatureFlags.load()


# Глобальный экземпляр для удобного импорта
# Использование: from backend.core.config import flags
flags = get_feature_flags()

