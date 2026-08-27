"""Integration: R4c schema migration — fresh install and in-place upgrade.

Run explicitly: CTXORA_IT=1 uv run pytest tests/integration
Requires: docker compose up -d (pgvector image, superuser compose user).

Fresh install: bootstrap a scratch database and verify the R4c shape.
In-place: rebuild a legacy (pre-R4c) shape, insert legacy rows, re-run
bootstrap, and verify the columns arrive and the rows survive.
"""

import os

import psycopg
import pytest

from config.settings import Settings
from database.metadata import bootstrap_schema, conninfo

pytestmark = pytest.mark.skipif(
    os.environ.get("CTXORA_IT") != "1",
    reason="integration tests run only with CTXORA_IT=1",
)

SCRATCH_DB = "ctxora_r4c_it"

DOCUMENT_R4C_COLUMNS = ("doc_family", "doc_version", "metadata")
CHUNK_R4C_COLUMNS = ("parent_id", "chunk_kind", "metadata")
R4C_INDEXES = ("idx_rag_chunks_parent", "idx_rag_documents_family")


def _admin_conninfo(settings: Settings) -> str:
    return (
        f"host={settings.metadata_db_host} port={settings.metadata_db_port} "
        f"dbname=postgres user={settings.metadata_db_user} "
        f"password={settings.metadata_db_password}"
    )


def _scratch(settings: Settings) -> Settings:
    return settings.model_copy(update={"metadata_db_name": SCRATCH_DB})


def _recreate_scratch(settings: Settings) -> None:
    with psycopg.connect(_admin_conninfo(settings), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {SCRATCH_DB}")


def _drop_scratch(settings: Settings) -> None:
    with psycopg.connect(_admin_conninfo(settings), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")


def _columns(settings: Settings, table: str) -> set[str]:
    with psycopg.connect(conninfo(_scratch(settings))) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        return {row[0] for row in cur.fetchall()}


def _indexes(settings: Settings) -> set[str]:
    with psycopg.connect(conninfo(_scratch(settings))) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public'"
            " AND tablename IN ('rag_documents', 'rag_chunks')"
        )
        return {row[0] for row in cur.fetchall()}


def _downgrade_to_legacy(settings: Settings) -> None:
    with psycopg.connect(conninfo(_scratch(settings))) as conn, conn.cursor() as cur:
        for column in DOCUMENT_R4C_COLUMNS:
            cur.execute(f"ALTER TABLE rag_documents DROP COLUMN {column}")
        for column in CHUNK_R4C_COLUMNS:
            cur.execute(f"ALTER TABLE rag_chunks DROP COLUMN {column}")
        for name in R4C_INDEXES:
            cur.execute(f"DROP INDEX IF EXISTS {name}")
        conn.commit()


def test_fresh_install_has_r4c_shape() -> None:
    settings = Settings()
    _recreate_scratch(settings)
    try:
        bootstrap_schema(_scratch(settings))

        assert set(DOCUMENT_R4C_COLUMNS) <= _columns(settings, "rag_documents")
        assert set(CHUNK_R4C_COLUMNS) <= _columns(settings, "rag_chunks")
        assert set(R4C_INDEXES) <= _indexes(settings)
    finally:
        _drop_scratch(settings)


def test_in_place_migration_preserves_legacy_rows() -> None:
    settings = Settings()
    _recreate_scratch(settings)
    scratch = _scratch(settings)
    try:
        bootstrap_schema(scratch)
        _downgrade_to_legacy(settings)
        with psycopg.connect(conninfo(scratch)) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rag_documents (id, tenant, filename, file_hash)"
                " VALUES (gen_random_uuid(), 'r4c-it', 'legacy.md', 'legacy-hash')"
            )
            cur.execute(
                "INSERT INTO rag_chunks (id, document_id, chunk_text)"
                " SELECT gen_random_uuid(), id, 'legacy body' FROM rag_documents"
                " WHERE tenant = 'r4c-it'"
            )
        assert not set(DOCUMENT_R4C_COLUMNS) & _columns(settings, "rag_documents")
        assert not set(CHUNK_R4C_COLUMNS) & _columns(settings, "rag_chunks")

        bootstrap_schema(scratch)

        assert set(DOCUMENT_R4C_COLUMNS) <= _columns(settings, "rag_documents")
        assert set(CHUNK_R4C_COLUMNS) <= _columns(settings, "rag_chunks")
        assert set(R4C_INDEXES) <= _indexes(settings)
        with psycopg.connect(conninfo(scratch)) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), count(doc_family), count(doc_version), count(metadata)"
                " FROM rag_documents WHERE tenant = 'r4c-it'"
            )
            assert cur.fetchone() == (1, 0, 0, 0)
            cur.execute(
                "SELECT chunk_text, parent_id, chunk_kind FROM rag_chunks"
                " WHERE document_id IN"
                " (SELECT id FROM rag_documents WHERE tenant = 'r4c-it')"
            )
            assert cur.fetchall() == [("legacy body", None, None)]

        bootstrap_schema(scratch)
    finally:
        _drop_scratch(settings)
