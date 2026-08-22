"""PG-backed query callable for the knowledge store."""

from config.settings import Settings
from database.metadata import MetadataConnection
from knowledge.store import Query


def metadata_query(settings: Settings) -> Query:
    """Build a (sql, params) -> rows callable over fresh connections."""

    def query(sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        statement = sql.encode("utf-8")
        with MetadataConnection(settings) as conn, conn.cursor() as cur:
            cur.execute(statement, params)
            rows = cur.fetchall()
        return [tuple(row) for row in rows]

    return query
