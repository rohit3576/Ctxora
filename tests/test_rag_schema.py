"""Locks for rag/schema.sql: the R4c migration stays additive and idempotent.

The schema file crosses a trust boundary as raw SQL, so these content locks
keep the migration properties testable without PostgreSQL: every R4c column
must appear both in the CREATE TABLE shape (fresh installs) and as an
ADD COLUMN IF NOT EXISTS (in-place upgrades), and the parent/family indexes
that 4d/4e rely on must exist.
"""

from pathlib import Path

SCHEMA = Path(__file__).parent.parent / "rag" / "schema.sql"

DOCUMENT_COLUMNS = ("doc_family", "doc_version", "metadata")
CHUNK_COLUMNS = ("parent_id", "chunk_kind", "metadata")
R4C_INDEXES = ("idx_rag_chunks_parent", "idx_rag_documents_family")


def _schema() -> str:
    return SCHEMA.read_text(encoding="utf-8")


class TestFreshInstallShape:
    def test_document_columns_in_create_table(self) -> None:
        for column in DOCUMENT_COLUMNS:
            assert f"{column} " in _schema(), f"rag_documents missing {column}"

    def test_chunk_columns_in_create_table(self) -> None:
        for column in CHUNK_COLUMNS:
            assert f"{column} " in _schema(), f"rag_chunks missing {column}"

    def test_parent_id_is_self_referencing_foreign_key(self) -> None:
        assert "parent_id UUID REFERENCES rag_chunks(id)" in _schema()


class TestInPlaceMigration:
    def test_document_columns_have_additive_alters(self) -> None:
        for column in DOCUMENT_COLUMNS:
            assert f"ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS {column}" in _schema(), (
                f"missing in-place ALTER for rag_documents.{column}"
            )

    def test_chunk_columns_have_additive_alters(self) -> None:
        for column in CHUNK_COLUMNS:
            assert f"ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS {column}" in _schema(), (
                f"missing in-place ALTER for rag_chunks.{column}"
            )


class TestIndexes:
    def test_r4c_indexes_exist(self) -> None:
        for name in R4C_INDEXES:
            assert f"CREATE INDEX IF NOT EXISTS {name}" in _schema(), name
