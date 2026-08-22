"""Telemetry store factory: config adapter name -> concrete store."""

from config.settings import AppConfig, Settings
from database.clickhouse_store import ClickHouseStore
from database.contracts import TelemetryStore
from database.postgres_store import PostgresStore


def build_telemetry_store(app_config: AppConfig, settings: Settings) -> TelemetryStore:
    """Build the configured telemetry adapter."""
    telemetry = app_config.stores.telemetry
    events = app_config.stores.events
    events_table = events.mapping.table if events.enabled and events.mapping else None
    match telemetry.adapter:
        case "clickhouse":
            return ClickHouseStore(
                mapping=telemetry.mapping, settings=settings, events_table_template=events_table
            )
        case "postgres":
            return PostgresStore(
                mapping=telemetry.mapping, settings=settings, events_table_template=events_table
            )
