import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from backend.core.capture.orchestrator import CaptureOrchestrator
from backend.core.llm.router import LLMRouter
from backend.core.store.graph_builder import GraphBuilder
from backend.core.store.vector_indexer import VectorIndexer
from backend.db.supabase_client import SupabaseClient

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class IngestManager:
    def __init__(self):
        # Хранилище выбирается так же, как в API: USE_NETWORKX / наличие
        # Neo4j. Иначе ингест писал бы в NetworkX-файл, а бекенд с поднятым
        # Neo4j читал бы пустой граф — «данные есть, в интерфейсе пусто».
        self.graph = GraphBuilder()
        self.indexer = VectorIndexer()
        self.llm_router = LLMRouter()
        self.orchestrator = None # Lazy init
        self.supabase = None # Lazy init

    async def initialize(self):
        logger.info("🚀 Initializing Ingest Manager...")
        await self.graph.connect()
        await self.indexer.connect()

        # Init Orchestrator. Сторы обязательны: без graph_builder слой
        # knowledge extraction отрабатывает (LLM-вызовы идут, деньги
        # тратятся), но узлы знаний и рёбра к встрече не пишутся никуда —
        # «Memory reinforcement skipped: no graph_builder» в логе.
        self.orchestrator = CaptureOrchestrator(
            llm_router=self.llm_router,
            graph_builder=self.graph,
            vector_indexer=self.indexer,
        )

    async def ingest_files(self, folder_path: str, access_group: str,
                           dump_dir: str = None):
        """Ingest local files from a folder"""
        path = Path(folder_path)
        if not path.exists():
            logger.error(f"❌ Folder not found: {folder_path}")
            return

        files = list(path.glob("**/*.txt")) + list(path.glob("**/*.md"))
        logger.info(f"📂 Found {len(files)} files in {folder_path}")

        for file_path in files:
            logger.info(f"📄 Processing file: {file_path.name}")
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # 1. Index raw document
                metadata = {
                    "source": "file",
                    "filename": file_path.name,
                    "path": str(file_path),
                    "title": file_path.stem
                }
                await self.indexer.add_document(content, metadata, access_group)

                # 2. Extract entities and update graph (if it looks like meeting notes or structured text)
                # We treat every file as a "Meeting" node for now to reuse the pipeline,
                # or create a special "Document" node logic.
                # Let's reuse process_meeting but map it to Document logic later.

                # Дата встречи: из префикса имени файла (2026-03-09_...),
                # иначе — момент ингеста. Иначе весь корпус штампуется одним
                # днём и таймлайн/темпоральные запросы врут.
                date_match = re.match(r"(\d{4}-\d{2}-\d{2})", file_path.stem)
                meeting_date = (date_match.group(1) if date_match
                                else datetime.now().isoformat())

                # Mock meeting structure
                meeting_data = {
                    "id": f"doc_{file_path.stem}",
                    "transcription_text": content,
                    "summary": "",
                    "date": meeting_date,
                    "title": file_path.stem
                }

                # Узел встречи — ДО конвейера: knowledge/template-слои
                # линкуют извлечённое на meeting_<id> прямо во время
                # process_meeting (тот же приём и по той же причине, что в
                # knowledge_sync.py — иначе рёбра «знание → встреча» молча
                # отбрасываются). save_meeting_results потом обогатит этот
                # же узел: MERGE по meeting_id находит его.
                await self.graph.create_node(
                    node_id=f"meeting_{meeting_data['id']}",
                    label="Meeting",
                    properties={
                        "meeting_id": meeting_data["id"],
                        "title": meeting_data["title"],
                        "date": meeting_data["date"],
                    },
                    access_group=access_group,
                )

                results = await self.orchestrator.process_meeting(
                    meeting_data["transcription_text"],
                    meeting_context=meeting_data
                )

                # Дамп извлечений: сырой результат конвейера в JSON, чтобы
                # scripts/seed_demo.py мог загрузить память без LLM-вызовов.
                if dump_dir:
                    dump_path = Path(dump_dir)
                    dump_path.mkdir(parents=True, exist_ok=True)
                    with open(dump_path / f"{meeting_data['id']}.json", "w",
                              encoding="utf-8") as f:
                        json.dump({
                            "meeting": meeting_data,
                            "access_group": access_group,
                            "source_file": file_path.name,
                            "results": results,
                        }, f, ensure_ascii=False, indent=2, default=str)

                # Save to Graph with Access Group
                await self.graph.save_meeting_results(
                    meeting_id=meeting_data["id"],
                    results=results,
                    meeting_metadata=meeting_data,
                    access_group=access_group
                )

                # Index Entities with Access Group
                await self.indexer.index_meeting_results(
                    meeting_id=meeting_data["id"],
                    results=results,
                    meeting_metadata=meeting_data,
                    access_group=access_group
                )

            except Exception as e:
                logger.error(f"❌ Failed to process {file_path.name}: {e}")

    async def ingest_supabase(self, user_id: str, access_group: str):
        """Ingest meetings from Supabase"""
        if not self.supabase:
            self.supabase = SupabaseClient()
            # No explicit connect needed for http client

        logger.info(f"☁️ Fetching meetings for user {user_id}...")
        try:
            meetings = await self.supabase.get_meetings(user_id=user_id, limit=50)
            logger.info(f"📥 Found {len(meetings)} meetings")

            for meeting in meetings:
                meeting_id = meeting.get("id")
                title = meeting.get("title", "Untitled")
                logger.info(f"🔄 Processing meeting: {title} ({meeting_id})")

                transcript = meeting.get("transcription_text", "")
                if not transcript:
                    logger.warning(f"⚠️ No transcript for meeting {meeting_id}, skipping.")
                    continue

                # Process
                results = await self.orchestrator.process_meeting(
                    transcript,
                    meeting_context=meeting
                )

                # Save to Graph
                await self.graph.save_meeting_results(
                    meeting_id=str(meeting_id),
                    results=results,
                    meeting_metadata=meeting,
                    access_group=access_group
                )

                # Save to Vector DB
                await self.indexer.index_meeting_results(
                    meeting_id=str(meeting_id),
                    results=results,
                    meeting_metadata=meeting,
                    access_group=access_group
                )

                logger.info(f"✅ Processed {title}")

        except Exception as e:
            logger.error(f"❌ Supabase ingestion failed: {e}")

    async def close(self):
        await self.graph.close(save=True)
        await self.indexer.close()

async def main():
    parser = argparse.ArgumentParser(description="Ingest data into Tessbrain with Access Control")
    parser.add_argument("--source", choices=["files", "supabase"], required=True, help="Data source")
    parser.add_argument("--target", required=True, help="Folder path (for files) or User ID (for supabase)")
    parser.add_argument("--group", required=True, help="Access Group (e.g. 'public', 'management', 'project_x')")
    parser.add_argument("--dump-extractions", default=None, metavar="DIR",
                        help="Also write raw extraction results as JSON per meeting "
                             "(for scripts/seed_demo.py — reload without LLM calls)")

    args = parser.parse_args()

    manager = IngestManager()
    await manager.initialize()

    try:
        if args.source == "files":
            await manager.ingest_files(args.target, args.group,
                                       dump_dir=args.dump_extractions)
        elif args.source == "supabase":
            await manager.ingest_supabase(args.target, args.group)
    finally:
        await manager.close()

if __name__ == "__main__":
    asyncio.run(main())

