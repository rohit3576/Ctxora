"""Row value normalization: DB-native values -> JSON-safe scalars."""

import datetime
import decimal
import uuid

from database.contracts import JsonScalar


def to_json_scalar(value: object) -> JsonScalar:
    """Convert one database-native value to a JSON-safe scalar."""
    match value:
        case None:
            return None
        case bool():
            return value
        case int():
            return value
        case float():
            return value
        case str():
            return value
        case decimal.Decimal():
            return float(value)
        case datetime.datetime():
            return value.isoformat()
        case datetime.date():
            return value.isoformat()
        case uuid.UUID():
            return str(value)
        case bytes():
            return value.decode("utf-8", errors="replace")
        case other:
            return str(other)
