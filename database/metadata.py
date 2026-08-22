"""Metadata store access: connections, readiness, schema bootstrap."""

from pathlib import Path
from types import TracebackType
from typing import Final

import psycopg
from psycopg import Connection

from config.settings import Settings

_SCHEMA_DIR: Final = Path(__file__).parent.parent
SCHEMA_FILES: Final = (
    _SCHEMA_DIR / "knowledge" / "schema.sql",
    _SCHEMA_DIR / "memory" / "schema.sql",
    _SCHEMA_DIR / "onboarding" / "schema.sql",
    _SCHEMA_DIR / "feedback" / "schema.sql",
    _SCHEMA_DIR / "rag" / "schema.sql",
)


def conninfo(settings: Settings, *, readonly: bool = False) -> str:
    """Build a psycopg conninfo string from settings."""
    options = "-c default_transaction_read_only=on" if readonly else ""
    return (
        f"host={settings.metadata_db_host} port={settings.metadata_db_port} "
        f"dbname={settings.metadata_db_name} user={settings.metadata_db_user} "
        f"password={settings.metadata_db_password} options='{options}'"
    )


class MetadataConnection:
    """One metadata DB connection as a context manager (commit/close on exit)."""

    def __init__(self, settings: Settings) -> None:
        """Remember which settings to connect with."""
        self._settings: Settings = settings
        self._conn: Connection | None = None

    def __enter__(self) -> Connection:
        """Open the connection."""
        self._conn = psycopg.connect(conninfo(self._settings), connect_timeout=5)
        return self._conn

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Delegate to psycopg's transaction/close semantics."""
        if self._conn is not None:
            self._conn.__exit__(exc_type, exc, tb)
            self._conn.close()


def check_metadata_db(settings: Settings) -> tuple[bool, str]:
    """Run SELECT 1 against the metadata DB; (ok, detail)."""
    try:
        with MetadataConnection(settings) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except psycopg.OperationalError as exc:
        return (False, str(exc).splitlines()[0] if str(exc) else "unreachable")
    return (True, "ok")


def bootstrap_schema(settings: Settings) -> None:
    """Apply every schema.sql (knowledge, memory, onboarding) idempotently."""
    ddls = [path.read_text().encode("utf-8") for path in SCHEMA_FILES]
    with MetadataConnection(settings) as conn:
        with conn.cursor() as cur:
            # Prerequisite, not schema: knowledge/rag columns use VECTOR(n).
            # Ships disabled in fresh pgvector databases (CI, new volumes).
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            for ddl in ddls:
                cur.execute(ddl)
        conn.commit()
