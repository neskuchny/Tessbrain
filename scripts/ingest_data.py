import argparse
import asyncio
import logging
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
        self.graph = GraphBuilder(use_networkx=True)
        self.indexer = VectorIndexer()
        self.llm_router = LLMRouter()
        self.orchestrator = None # Lazy init
        self.supabase = None # Lazy init

    async def initialize(self):
        logger.info("🚀 Initializing Ingest Manager...")
        await self.graph.connect()
        await self.indexer.connect()

        # Init Orchestrator
        self.orchestrator = CaptureOrchestrator(
            llm_router=self.llm_router
        )

    async def ingest_files(self, folder_path: str, access_group: str):
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

                # Mock meeting structure
                meeting_data = {
                    "id": f"doc_{file_path.stem}",
                    "transcription_text": content,
                    "summary": "",
                    "date": datetime.now().isoformat(),
                    "title": file_path.stem
                }

                results = await self.orchestrator.process_meeting(
                    meeting_data["transcription_text"],
                    meeting_context=meeting_data
                )

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

    args = parser.parse_args()

    manager = IngestManager()
    await manager.initialize()

    try:
        if args.source == "files":
            await manager.ingest_files(args.target, args.group)
        elif args.source == "supabase":
            await manager.ingest_supabase(args.target, args.group)
    finally:
        await manager.close()

if __name__ == "__main__":
    asyncio.run(main())

