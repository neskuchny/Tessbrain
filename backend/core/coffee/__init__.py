"""Coffee scenario — флагман видения (Phase B).

«Пока человек идёт от переговорной до стола, Tessbrain уже подготовил для него
черновики выходов в зависимости от роли. Для аккаунт-менеджера — письмо
подрядчику. Для разработчика — техническое задание с архитектурным эскизом,
готовое к передаче в IDE. Для продакта — описание новой стратегической
инициативы.»
                        (docs/tessent_core/TESSENT — функциональная спецификация)

Этот пакет реализует:
- Определение РОЛИ каждого участника в контексте встречи (RoleInference)
- Запуск ROLE-SPECIFIC генератора артефакта (RolePipeline subclasses)
- DELIVERY ROUTING — куда отправить артефакт через какой канал (использует
  Persona.behavioral.channel_preferences)
- TOP-LEVEL ORCHESTRATOR — собирает всё вместе

Подробная архитектура: docs/COFFEE_ARCHITECTURE.md.
"""
from backend.core.coffee.delivery import (
    DeliveryChannel,
    DeliveryRecipient,
    deliver_artifact,
    pick_channels,
)
from backend.core.coffee.orchestrator import (
    CoffeeOrchestrator,
    CoffeeResult,
    get_coffee_orchestrator,
    prepare_coffee_for_meeting,
)
from backend.core.coffee.role_inference import (
    Role,
    RoleAssignment,
    infer_roles,
)
from backend.core.coffee.role_pipelines.base import (
    RolePipeline,
    RolePipelineOutput,
)
from backend.core.coffee.storage import (
    list_artifacts,
    save_artifact,
    update_delivery_status,
)

__all__ = [
    "CoffeeOrchestrator",
    "CoffeeResult",
    "DeliveryChannel",
    "DeliveryRecipient",
    "Role",
    "RoleAssignment",
    "RolePipeline",
    "RolePipelineOutput",
    "deliver_artifact",
    "get_coffee_orchestrator",
    "infer_roles",
    "list_artifacts",
    "pick_channels",
    "prepare_coffee_for_meeting",
    "save_artifact",
    "update_delivery_status",
]
