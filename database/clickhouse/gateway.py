"""Typed boundary over the untyped clickhouse-connect library.

Everything inside this package accepts the untyped library; everything
outside it sees strict types. The store never imports clickhouse_connect.
"""

from config.settings import Settings


class ClickHouseGatewayError(Exception):
    """One failed ClickHouse call, pre-classified as connection or query."""

    def __init__(self, kind: str, detail: str) -> None:
        """Classify the failure and carry the first error line."""
        self.kind: str = kind
        self.detail: str = detail
        super().__init__(detail)


def run_query(
    settings: Settings, sql: str, clickhouse_settings: dict[str, int | str]
) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
    """Execute one query; return (column_names, raw_rows).

    Raises:
        ClickHouseGatewayError: classified as connection or query failure.
    """
    import clickhouse_connect
    from clickhouse_connect.driver.exceptions import ClickHouseError

    try:
        client = clickhouse_connect.get_client(
            host=settings.telemetry_db_host or "localhost",
            port=settings.telemetry_db_port,
            username=settings.telemetry_db_user or "default",
            password=settings.telemetry_db_password or "",
            database=settings.telemetry_db_name,
        )
        result = client.query(sql, settings=clickhouse_settings)
    except ClickHouseError as exc:
        raise _classified(str(exc)) from exc

    columns = tuple(str(name) for name in result.column_names)
    rows = tuple(tuple(row) for row in result.result_rows)
    return columns, rows


def _classified(message: str) -> ClickHouseGatewayError:
    lowered = message.lower()
    markers = ("connection", "timeout", "unreachable", "refused", "resolve", "dns")
    kind = "connection" if any(marker in lowered for marker in markers) else "query"
    return ClickHouseGatewayError(kind, message.splitlines()[0] if message else "unknown")
