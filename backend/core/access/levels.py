"""Access levels для org-graph узлов (P1 #5 + P2 мульти-аккаунта).

Числовая шкала видимости узла внутри org. Большее значение = более ограниченный
доступ. Mapping role → max_access_level определяет «что видит каждая роль».

Шкала (выровнена со schema.py 1–5; 0 — служебный «всем»):
    0 PUBLIC        — видно всем members org (объявления, общие шаблоны)
    1 INTERNAL_LOW  — большинство дефолтных work-узлов (старый default)
    2 INTERNAL      — стандартный default (graph_builder.create_node)
    3 CONFIDENTIAL  — manager+ (планы команды, перформанс)
    4 RESTRICTED    — admin (HR, legal, finance)
    5 TOP_SECRET    — только founder/генеральный (board strategy, M&A).
      Раньше уровень был задокументирован в schema.py, но не реализован —
      RBAC-фильтр обрезался на 4 и шкалы расходились. Теперь founder=5:
      «генеральный видит весь орг-мозг».

Отделы (P2): узел может нести `department` (промоушен ставит отдел автора).
Для членов ДРУГОГО отдела эффективный порог = max(access_level, CONFIDENTIAL):
чужой отдел читается только с уровня руководителя и выше. Свой отдел, узлы
без отдела, владелец/автор промоушена — обычные правила.

Personal-узлы (которые принадлежат самому user'у — `user_id == caller_id`)
НЕ фильтруются по access_level: ты всегда видишь свои данные, даже если
кто-то ошибочно поставил CONFIDENTIAL на твой draft. Это уровень
«personal-graph как owner», не «org-graph через role».
"""
from __future__ import annotations

from enum import IntEnum
from typing import Optional

from backend.core.ingest import membership


class AccessLevel(IntEnum):
    PUBLIC = 0
    INTERNAL_LOW = 1
    INTERNAL = 2          # graph_builder.create_node дефолт
    CONFIDENTIAL = 3
    RESTRICTED = 4
    TOP_SECRET = 5        # только founder («генеральный видит всё»)


# Role → max access_level. Узел c level > этого значения скрыт от роли.
# contractor намеренно жёстко урезан — внешние подрядчики видят только
# то что explicitly помечено PUBLIC.
ROLE_MAX_ACCESS_LEVEL: dict[str, int] = {
    membership.ROLE_CONTRACTOR: AccessLevel.INTERNAL_LOW,  # 1
    membership.ROLE_EMPLOYEE:   AccessLevel.INTERNAL,      # 2
    membership.ROLE_MANAGER:    AccessLevel.CONFIDENTIAL,  # 3
    membership.ROLE_ADMIN:      AccessLevel.RESTRICTED,    # 4
    membership.ROLE_FOUNDER:    AccessLevel.TOP_SECRET,    # 5 — видит всё
}

# Если role неизвестна — самый закрытый дефолт (видим только PUBLIC).
# Это fail-safe: лучше показать меньше, чем leak'нуть.
UNKNOWN_ROLE_MAX = AccessLevel.PUBLIC


def max_access_level_for_user(user_id: str) -> int:
    """Вернуть максимальный access_level, доступный user'у в его org.

    None / solo-user / не-член org → UNKNOWN_ROLE_MAX (только PUBLIC).
    Это не блокирует personal-flow: personal-узлы фильтр НЕ применяет
    (см. graph_view._merge_org_into_personal — owner-check).

    Кастомные роли (enterprise): роль вне встроенной пятёрки резолвится в
    base-роль org'а; опциональный max_access_level кастомной роли может
    только СУЗИТЬ клиренс base (никогда не расширить). Неизвестная роль —
    по-прежнему fail-closed PUBLIC.
    """
    if not user_id:
        return int(UNKNOWN_ROLE_MAX)
    role = membership.get_user_org_role(user_id)
    if role is None:
        return int(UNKNOWN_ROLE_MAX)
    if role in ROLE_MAX_ACCESS_LEVEL:
        return int(ROLE_MAX_ACCESS_LEVEL[role])
    # кастомная роль организации → клиренс base с опциональным сужением
    try:
        org_id = membership.get_org_for_user(user_id)
        base = membership.resolve_base_role(role, org_id)
        if base is None:
            return int(UNKNOWN_ROLE_MAX)
        level = int(ROLE_MAX_ACCESS_LEVEL.get(base, UNKNOWN_ROLE_MAX))
        from backend.core.ingest import org_store
        spec = org_store.get_custom_roles(org_id or "").get(role) or {}
        cap = spec.get("max_access_level")
        if cap is not None:
            level = min(level, int(cap))
        return level
    except Exception:
        return int(UNKNOWN_ROLE_MAX)


def department_for_user(user_id: str) -> Optional[str]:
    """Отдел пользователя в его org (или None). Обёртка для батч-фильтров."""
    try:
        return membership.get_user_org_department(user_id)
    except Exception:
        return None


def node_visible_to(node_attrs: dict, *, user_id: str,
                    max_level: Optional[int] = None,
                    viewer_department: Optional[str] = ...) -> bool:
    """Видим ли узел этому user'у?

    Правила:
      1. Если node owner == user_id → видим (свой узел независимо от уровня)
      2. Узел, promoted из personal этого user'а → видим (свой decision)
      3. Иначе access_level узла должен быть ≤ max_level user'а
      4. Узел без access_level считается AccessLevel.INTERNAL (дефолт)
      5. Отделы: узел с `department`, отличным от отдела зрителя, требует
         эффективный уровень max(access_level, CONFIDENTIAL) — чужой отдел
         читается только руководителями и выше. Founder/admin проходят.

    Args:
        node_attrs: dict из nx_graph.nodes[node_id]
        user_id: caller
        max_level: предвычисленный max_access_level (для batch-фильтра без
                   повторных lookup'ов membership на каждый узел)
        viewer_department: предвычисленный отдел зрителя; Ellipsis (не
                   передан) → узнаём через membership; None → «без отдела».
    """
    if not isinstance(node_attrs, dict):
        return False

    # Owner-check: свой узел всегда видим.
    owner = node_attrs.get("user_id") or node_attrs.get("created_by")
    if owner and owner == user_id:
        return True

    # Promoted-from check: узел, который был promoted из personal этого user'а,
    # тоже его (origin == user_id). Это важно для Wave 1 promote: «свой
    # decision» не должен прятаться от меня после promote в org.
    if node_attrs.get("promoted_from_user_id") == user_id:
        return True

    if max_level is None:
        max_level = max_access_level_for_user(user_id)

    node_level = node_attrs.get("access_level")
    if node_level is None:
        node_level = int(AccessLevel.INTERNAL)
    try:
        node_level_int = int(node_level)
    except (TypeError, ValueError):
        # corrupted access_level → fail-closed (не показываем)
        return False

    # Отдел: контент чужого отдела требует уровень руководителя и выше.
    node_dept = str(node_attrs.get("department") or "").strip().lower()
    if node_dept:
        if viewer_department is ...:
            viewer_department = department_for_user(user_id)
        viewer_dept = str(viewer_department or "").strip().lower()
        if viewer_dept != node_dept:
            node_level_int = max(node_level_int, int(AccessLevel.CONFIDENTIAL))

    return node_level_int <= max_level
