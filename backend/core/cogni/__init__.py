# -*- coding: utf-8 -*-
"""CogniLayer — когнитивные измерения профиля и адаптация вывода под человека.

Ф1.1: ``dimensions`` формализует слот ``extended["cogni"]`` персоны
(витки/световой конус/эквалайзер/фаза) со схемой, нормализацией и аксессорами.
Ф1.2: ``adapt`` — единый сервис адаптации вывода под человека (Persona →
CogniFrame → директива/преамбула), общий для чата, ТЗ, отчётов, трансляции.
"""
from backend.core.cogni.dimensions import (  # noqa: F401
    COGNI_SCHEMA_VERSION,
    EQUALIZER_AXES,
    PHASE_STATES,
    VITKI_DOMAINS,
    apply_to_persona,
    default_cogni,
    derive_from_persona,
    get_cogni,
    merge_cogni,
    normalize_cogni,
    set_equalizer_axis,
    set_light_cone,
    set_phase_state,
    set_vitok,
)
from backend.core.cogni.adapt import (  # noqa: F401
    CogniFrame,
    adapt_system_prompt,
    frame_block,
    frame_directive,
    frame_preamble,
    load_frame,
)
