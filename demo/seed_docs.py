"""Seed demo documents (RAG golden-set corpus) into the rag store.

Usage:
    uv run python -m demo.seed_docs            # real embeddings (LLM_API_KEY + PG up)
    uv run python -m demo.seed_docs --hash     # deterministic hash embeddings (offline)
"""

import argparse
from pathlib import Path

from config.settings import RagConfig, Settings, get_settings, load_app_config
from database.metadata import bootstrap_schema
from knowledge.pg import metadata_query
from llm.client import LLMClient
from llm.openai_compat import OpenAICompatibleClient
from rag.contracts import RagStore
from rag.fake import HashEmbedLLM
from rag.ingest import ingest
from rag.store import PGRagStore

DOCS_DIR = Path(__file__).parent / "docs"
# Families grouped oldest-first: R4e will supersede same-family older versions
# on upload, so seeding order fixes which revision ends ACTIVE.
DOC_FILES = (
    "door-sensor-ds200-manual-v1.md",
    "door-sensor-ds200-manual-v2.md",
    "refrigeration-ru500-manual-v2.0.md",
    "refrigeration-ru500-manual-v2.3.md",
    "gps-tracker-gt800-manual-v1.0.md",
    "gps-tracker-gt800-manual-v1.1.md",
)
HASH_EMBEDDING_MODEL = "hash-embed-1536"


def seed_documents(
    store: RagStore, llm: LLMClient, config: RagConfig, tenant: str, embedding_model: str
) -> None:
    """Ingest every demo document (idempotent via content-hash dedupe)."""
    for name in DOC_FILES:
        record = ingest(
            store,
            llm,
            config,
            tenant,
            name,
            (DOCS_DIR / name).read_bytes(),
            embedding_model=embedding_model,
        )
        print(
            f"{name}: {record.chunk_count} chunks / {record.total_pages} pages"
            f" (document {record.id})"
        )


def main() -> None:
    """Bootstrap the rag schema, then seed demo docs for one tenant."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--hash", action="store_true", help="use the offline hash embedder")
    args = parser.parse_args()

    settings: Settings = get_settings()
    bootstrap_schema(settings)
    store = PGRagStore(metadata_query(settings))
    llm: LLMClient = HashEmbedLLM() if args.hash else OpenAICompatibleClient(settings)
    embedding_model = HASH_EMBEDDING_MODEL if args.hash else settings.embedding_model
    seed_documents(store, llm, load_app_config().rag, args.tenant, embedding_model)


if __name__ == "__main__":
    main()
