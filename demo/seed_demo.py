"""Seed the demo tenant: metadata + synthetic telemetry.

Usage:
    uv run python -m demo.seed_demo            # metadata (PG) + telemetry
    QUERYPULSE_ADAPTER=postgres uv run python -m demo.seed_demo

Telemetry goes to ClickHouse (default adapter in defaults.yaml) or to the
metadata Postgres when QUERYPULSE_ADAPTER=postgres (simplest local demo).
"""

import datetime
import os
import random
from collections.abc import Sequence

import psycopg

from config.settings import Settings, get_settings
from database.metadata import MetadataConnection, bootstrap_schema

TENANT = "demo"
TABLE = "demo_telemetry"

TRUCKS = ("truck-101", "truck-102", "truck-103")

KEYS: dict[str, tuple[float, float, str]] = {
    "speed": (40.0, 90.0, "km/h"),
    "engine.rpm": (900.0, 2400.0, "rpm"),
    "engine.coolantTemp": (70.0, 95.0, "C"),
    "fuel.level": (20.0, 100.0, "%"),
    "battery.voltage": (11.8, 14.6, "V"),
}

ALIASES: Sequence[tuple[str, str]] = (
    ("rpm", "engine.rpm"),
    ("revs", "engine.rpm"),
    ("engine speed", "engine.rpm"),
    ("speed", "speed"),
    ("coolant temperature", "engine.coolantTemp"),
    ("engine temp", "engine.coolantTemp"),
    ("fuel", "fuel.level"),
    ("battery", "battery.voltage"),
    ("voltage", "battery.voltage"),
)

RULES: Sequence[str] = (
    "CTE bounds: when joining 3+ metrics via CTEs, add a timestamp filter inside EACH CTE.",
    "Latest value: use argMax-style latest per device, never unbounded ORDER BY.",
    "Numeric math on the value column always goes through the null-safe cast.",
)

EXAMPLES: Sequence[tuple[str, str]] = (
    (
        "What was the average RPM of truck-102 yesterday?",
        (
            "SELECT device_id, avg(toFloat64OrNull(value)) AS avg_rpm FROM demo_telemetry "
            "WHERE key = 'engine.rpm' AND device_id = 'truck-102' "
            "AND timestamp >= now() - INTERVAL 1 DAY GROUP BY device_id"
        ),
    ),
    (
        "Latest battery voltage per truck?",
        (
            "SELECT device_id, argMax(toFloat64OrNull(value), timestamp) AS latest_v "
            "FROM demo_telemetry WHERE key = 'battery.voltage' GROUP BY device_id"
        ),
    ),
    (
        "Max speed of the fleet today?",
        (
            "SELECT max(toFloat64OrNull(value)) AS max_speed FROM demo_telemetry "
            "WHERE key = 'speed' AND timestamp >= now() - INTERVAL 1 DAY"
        ),
    ),
)

SCHEMA_COLUMNS: Sequence[tuple[str, str, str, str]] = (
    (TABLE, "timestamp", "DateTime", "reading time"),
    (TABLE, "device_id", "String", "truck identifier"),
    (TABLE, "key", "String", "metric name"),
    (TABLE, "value", "String", "raw reading; cast numerically"),
)


def seed_metadata(settings: Settings) -> None:
    """Create schema + insert demo knowledge rows (idempotent)."""
    bootstrap_schema(settings)
    with MetadataConnection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sql_agent_tenants (tenant_name, display_name, eav_rules_text) "
                "VALUES (%s, %s, %s) ON CONFLICT (tenant_name) DO NOTHING",
                (
                    TENANT,
                    "Demo Fleet",
                    "Each row is one (timestamp, device_id, key, value) reading.",
                ),
            )
            cur.execute("SELECT id FROM sql_agent_tenants WHERE tenant_name = %s", (TENANT,))
            tenant_row = cur.fetchone()
            tenant_id = tenant_row[0] if tenant_row else 0

            for key, (_low, _high, unit) in KEYS.items():
                cur.execute(
                    "INSERT INTO sql_agent_telemetry_registry "
                    "(tenant_id, canonical_key, physical_key, description, "
                    "datatype, unit, aggregation, provenance) "
                    "VALUES (%s,%s,%s,%s,'numeric',%s,%s,'seed') "
                    "ON CONFLICT DO NOTHING",
                    (
                        tenant_id,
                        key,
                        key,
                        key.replace(".", " "),
                        unit,
                        "latest" if key in ("battery.voltage", "fuel.level") else "average",
                    ),
                )
            for alias, canonical in ALIASES:
                cur.execute(
                    "INSERT INTO sql_agent_aliases (tenant_id, alias, canonical_key) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (tenant_id, alias, canonical),
                )
            for number, rule in enumerate(RULES, start=1):
                cur.execute(
                    "INSERT INTO sql_agent_business_rules (tenant_id, rule_number, rule_text) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (tenant_id, number, rule),
                )
            for question, sql in EXAMPLES:
                cur.execute(
                    "INSERT INTO sql_agent_sql_examples "
                    "(tenant_id, question, sql_query, query_category, status) "
                    "VALUES (%s,%s,%s,'telemetry','approved') "
                    "ON CONFLICT DO NOTHING",
                    (tenant_id, question, sql),
                )
            for table, column, datatype, description in SCHEMA_COLUMNS:
                cur.execute(
                    "INSERT INTO sql_agent_schema_columns (tenant_id, table_name, "
                    "column_name, datatype, description) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (tenant_id, table, column, datatype, description),
                )
            cur.execute(
                "INSERT INTO sql_agent_table_metadata (tenant_id, table_name, table_type, "
                "purpose, time_column) VALUES (%s,%s,'eav','telemetry readings',"
                "'timestamp') ON CONFLICT DO NOTHING",
                (tenant_id, TABLE),
            )
        conn.commit()
    print(f"metadata seeded for tenant '{TENANT}'")


def generate_rows(hours: int = 24) -> list[tuple[datetime.datetime, str, str, str]]:
    """Deterministic synthetic telemetry: hourly samples per truck per key."""
    rng = random.Random(42)
    now = datetime.datetime.now(tz=datetime.UTC).replace(minute=0, second=0, microsecond=0)
    rows: list[tuple[datetime.datetime, str, str, str]] = []
    for hour in range(hours):
        ts = now - datetime.timedelta(hours=hours - hour)
        for truck in TRUCKS:
            for key, (low, high, _unit) in KEYS.items():
                value = round(rng.uniform(low, high), 2)
                rows.append((ts, truck, key, str(value)))
    return rows


def seed_telemetry_postgres(
    settings: Settings, rows: list[tuple[datetime.datetime, str, str, str]]
) -> None:
    """Write telemetry rows into a Postgres demo table."""
    with (
        psycopg.connect(
            f"host={settings.metadata_db_host} port={settings.metadata_db_port} "
            f"dbname={settings.metadata_db_name} user={settings.metadata_db_user} "
            f"password={settings.metadata_db_password}"
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} ("
            "timestamp TIMESTAMPTZ NOT NULL, device_id TEXT NOT NULL, "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_key ON {TABLE} (key, timestamp)")
        cur.executemany(f"INSERT INTO {TABLE} VALUES (%s,%s,%s,%s)", rows)
        conn.commit()
    print(f"{len(rows)} telemetry rows inserted into postgres {TABLE}")


def seed_telemetry_clickhouse(
    settings: Settings, rows: list[tuple[datetime.datetime, str, str, str]]
) -> None:
    """Write telemetry rows into a ClickHouse MergeTree table."""
    try:
        import clickhouse_connect  # noqa: PLC0415 (optional extra, lazy by design)
    except ImportError as exc:
        msg = "clickhouse extra missing: uv sync --extra clickhouse"
        raise SystemExit(msg) from exc

    client = clickhouse_connect.get_client(
        host=settings.telemetry_db_host or "localhost",
        port=settings.telemetry_db_port,
        username=settings.telemetry_db_user or "default",
        password=settings.telemetry_db_password or "",
        database=settings.telemetry_db_name,
    )
    client.command(
        f"CREATE TABLE IF NOT EXISTS {TABLE} (timestamp DateTime64(3), device_id String, "
        "key LowCardinality(String), value String) ENGINE = MergeTree "
        "ORDER BY (device_id, key, timestamp)"
    )
    client.insert(TABLE, rows, column_names=["timestamp", "device_id", "key", "value"])
    print(f"{len(rows)} telemetry rows inserted into clickhouse {TABLE}")


def main() -> None:
    """Seed metadata then telemetry per adapter."""
    settings = get_settings()
    seed_metadata(settings)
    rows = generate_rows()
    adapter = os.environ.get("QUERYPULSE_ADAPTER", "clickhouse")
    match adapter:
        case "postgres":
            seed_telemetry_postgres(settings, rows)
        case "clickhouse":
            seed_telemetry_clickhouse(settings, rows)
        case _:
            msg = f"unknown QUERYPULSE_ADAPTER: {adapter}"
            raise SystemExit(msg)


if __name__ == "__main__":
    main()
