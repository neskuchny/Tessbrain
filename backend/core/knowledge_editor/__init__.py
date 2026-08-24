# -*- coding: utf-8 -*-
"""
Natural Language Knowledge Editor - Редактор знаний на естественном языке.

Позволяет пользователю редактировать данные в графе через текстовые команды в чате.
"""

from .intent_parser import EditAction, EditIntent, IntentParser
from .nl_editor import NLKnowledgeEditor, get_nl_editor

__all__ = [
    "EditAction",
    "EditIntent",
    "IntentParser",
    "NLKnowledgeEditor",
    "get_nl_editor",
]


