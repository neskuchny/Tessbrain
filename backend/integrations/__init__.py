# -*- coding: utf-8 -*-
"""
Integrations module - подключение внешних систем.

Включает:
- MeetFlow Bridge - интеграция с MeetFlow агентами
- Supabase Sync - синхронизация с базой данных
"""

from .meetflow_bridge import MeetFlowBridge
from .tessent_brain_tools import TessentBrainTools

__all__ = ["MeetFlowBridge", "TessentBrainTools"]

