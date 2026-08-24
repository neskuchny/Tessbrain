"""Ingest helpers: dedup, org-aware upsert и promote из personal в org-graph.

Этот пакет — тонкий слой ПОВЕРХ существующего knowledge_sync. Не заменяет
basic flow `_process_single_meeting`, а оборачивает его двумя hook'ами:

- PRE: `meeting_upsert.find_or_create_episode` — проверка ledger ДО processing.
       Если эпизод уже обработан в org-канале другим участником — skip,
       reuse существующий результат.
- POST: `meeting_promote.promote_to_org` — после успешной обработки в
        personal-графе скопировать Meeting+Decision+Task в org-graph, если
        пользователь принадлежит организации и встреча из corp-источника.

Это сохраняет совместимость с Mini Tess (личный коуч пишет per-user без
изменений) и с solo-юзерами без org.
"""
