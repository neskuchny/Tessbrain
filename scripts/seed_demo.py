"""Загрузка готовых демо-данных в память — без единого LLM-вызова.

Демо-корпуса (demo/gelion — русский, demo/helion — английский) лежат в репо
в двух видах:
  - transcripts/  — исходные стенограммы (источник истины, их проверяет
    валидатор ground-truth);
  - extracted/    — уже прогнанные через полный конвейер извлечения
    результаты (JSON на каждую встречу), созданные командой
    `python scripts/ingest_data.py --source files --target demo/<corpus>/transcripts \
        --group public --dump-extractions demo/<corpus>/extracted`.

Этот скрипт заливает extracted/ в граф и векторный индекс напрямую, минуя
LLM: не нужен API-ключ, не нужен VPN, ничего не стоит. Ключ понадобится
только для чата/отчётов — просмотр графа, встреч и поиск работают сразу.

Использование (zero-infra, без Docker):
    USE_NETWORKX=true USE_QDRANT=false python scripts/seed_demo.py --corpus demo/gelion
    # Windows PowerShell:
    #   $env:USE_NETWORKX="true"; $env:USE_QDRANT="false"
    #   python scripts/seed_demo.py --corpus demo/gelion

Эмбеддинги считаются локальной моделью (multilingual-e5-base, скачается при
первом запуске). Если модель недоступна — векторная часть тихо выключится,
граф и встречи всё равно загрузятся.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from backend.core.store.graph_builder import GraphBuilder
from backend.core.store.vector_indexer import VectorIndexer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def seed(corpus: str, group_override: str = None) -> int:
    extracted = Path(corpus) / "extracted"
    files = sorted(extracted.glob("*.json"))
    if not files:
        logger.error(f"❌ No extraction dumps in {extracted}. "
                     f"Run ingest_data.py with --dump-extractions first, "
                     f"or check the corpus path.")
        return 1

    # Тот же путь сохранения, что и в scripts/ingest_data.py — только без
    # CaptureOrchestrator и LLMRouter. Хранилище выбирается как в API
    # (USE_NETWORKX / наличие Neo4j), чтобы данные легли туда, откуда
    # бекенд их читает.
    graph = GraphBuilder()
    indexer = VectorIndexer()
    await graph.connect()
    await indexer.connect()

    # Досейка знаний из дампов: extract уже оплачен при их создании,
    # save_extracted только пишет готовое в граф и индекс. llm_router=None
    # легален — LLM-фазы здесь не вызываются.
    from backend.core.capture.agents.knowledge_extraction_orchestrator import (
        KnowledgeExtractionOrchestrator,
    )
    knowledge_saver = KnowledgeExtractionOrchestrator(
        llm_router=None, graph_builder=graph, vector_indexer=indexer)

    ok = 0
    try:
        for fp in files:
            payload = json.loads(fp.read_text(encoding="utf-8"))
            meeting = payload["meeting"]
            results = payload["results"]
            group = group_override or payload.get("access_group", "public")

            logger.info(f"📄 Seeding {meeting['id']} (group={group})")

            # 1. Сырой документ в векторный индекс (как шаг 1 ингеста)
            text = meeting.get("transcription_text", "")
            if text:
                await indexer.add_document(text, {
                    "source": "file",
                    "filename": payload.get("source_file", fp.stem),
                    "path": payload.get("source_file", fp.stem),
                    "title": meeting.get("title", fp.stem),
                }, group)

            # 2. Узел встречи — до записи, конвенцией capture-пути
            #    (meeting_<id>): на него ссылаются рёбра знаний. MERGE в
            #    save_meeting_results найдёт его по meeting_id и обогатит.
            await graph.create_node(
                node_id=f"meeting_{meeting['id']}",
                label="Meeting",
                properties={
                    "meeting_id": meeting["id"],
                    "title": meeting.get("title", ""),
                    "date": meeting.get("date", ""),
                },
                access_group=group,
            )

            # 3. Готовые извлечения в граф и индекс
            await graph.save_meeting_results(
                meeting_id=meeting["id"],
                results=results,
                meeting_metadata=meeting,
                access_group=group,
            )
            await indexer.index_meeting_results(
                meeting_id=meeting["id"],
                results=results,
                meeting_metadata=meeting,
                access_group=group,
            )

            # 4. Знания из дампа (антипаттерны, практики, workflow…) — в
            #    первом прогоне они извлекались, но не попадали в граф:
            #    оркестратор работал без graph_builder.
            ke = results.get("knowledge_extraction") or {}
            if ke.get("total_extracted"):
                await knowledge_saver.save_extracted(meeting["id"], ke, text)
            ok += 1
    finally:
        await graph.close(save=True)
        await indexer.close()

    logger.info(f"✅ Seeded {ok}/{len(files)} meetings from {extracted}")
    return 0


async def main():
    parser = argparse.ArgumentParser(
        description="Seed pre-extracted demo data (no LLM calls needed)")
    parser.add_argument("--corpus", required=True,
                        help="Corpus folder, e.g. demo/gelion or demo/helion")
    parser.add_argument("--group", default=None,
                        help="Override access group (default: as recorded in dumps)")
    args = parser.parse_args()
    raise SystemExit(await seed(args.corpus, args.group))


if __name__ == "__main__":
    asyncio.run(main())
