"""Role-typed pipelines — генераторы артефактов под каждую роль.

Phase B Coffee scenario, шаг 2. Один pipeline на роль:
- SalesPipeline → черновик письма следующего шага
- DevPipeline → ТЗ с архитектурным эскизом для claude_code_cli / cursor_cli
- PMPipeline → список Jira-карточек + roadmap-фрагмент
- FounderPipeline → стратегический бриф + 3-5 вопросов на подумать
- DesignPipeline / HRPipeline / OtherPipeline — для редких ролей,
  отдают универсальный summary без специфичной структуры

Все наследуют RolePipeline base.

Регистрация в DEFAULT_PIPELINE_FOR_ROLE происходит в orchestrator.py.
"""
from backend.core.coffee.role_pipelines.base import (
    RolePipeline,
    RolePipelineOutput,
)
from backend.core.coffee.role_pipelines.design import DesignPipeline
from backend.core.coffee.role_pipelines.dev import DevPipeline
from backend.core.coffee.role_pipelines.founder import FounderPipeline
from backend.core.coffee.role_pipelines.hr import HRPipeline
from backend.core.coffee.role_pipelines.other import OtherPipeline
from backend.core.coffee.role_pipelines.pm import PMPipeline
from backend.core.coffee.role_pipelines.sales import SalesPipeline

__all__ = [
    "DesignPipeline",
    "DevPipeline",
    "FounderPipeline",
    "HRPipeline",
    "OtherPipeline",
    "PMPipeline",
    "RolePipeline",
    "RolePipelineOutput",
    "SalesPipeline",
]
