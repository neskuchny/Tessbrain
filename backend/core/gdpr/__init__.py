"""GDPR — right to erasure cascade (Art. 17).

См. erasure.erase_user — каскадное удаление всех user-owned данных по тенанту.
"""
from backend.core.gdpr.erasure import (
    ERASURE_SPECS,
    SKIPPED_TABLES,
    erase_user,
)

__all__ = ["ERASURE_SPECS", "SKIPPED_TABLES", "erase_user"]
