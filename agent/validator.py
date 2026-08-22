"""SQL validator: generic read-only rules + dialect patterns + one repair pass.

Layer 1 (hard rejects): statement must start with SELECT/WITH, forbidden
verbs (dialect-supplied), only allowlisted tables, CTE depth <= 5.
Layer 2 (auto-repair): bare aggregates over the EAV value column get the
dialect's null-safe numeric cast, once, then re-validate.
"""

import re
from dataclasses import dataclass
from typing import Final

from config.settings import ColumnMapping
from database.contracts import Dialect

_MAX_CTE_DEPTH: Final = 5
_AGGREGATES: Final = ("avg", "sum", "min", "max", "median")
_TABLE_REF: Final = re.compile(r"\b(?:FROM|JOIN)\s+`?\"?([A-Za-z_][\w.]*)", re.IGNORECASE)
_CTE_NAMES: Final = re.compile(r"(\w+)\s+AS\s*\(", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating (and possibly repairing) one SQL statement."""

    valid: bool
    errors: tuple[str, ...]
    normalized_sql: str
    repairs_applied: tuple[str, ...]


class SQLValidator:
    """Validate generated SQL against safety rules before execution."""

    def __init__(
        self, dialect: Dialect, mapping: ColumnMapping, allowed_tables: tuple[str, ...]
    ) -> None:
        """Bind dialect, column mapping, and the tenant's table allowlist."""
        self.dialect: Dialect = dialect
        self.mapping: ColumnMapping = mapping
        self.allowed_tables: tuple[str, ...] = allowed_tables

    def validate(self, sql: str) -> ValidationResult:
        """Run hard rules, then one repair pass, then re-run hard rules."""
        repaired = self._repair_value_casts(sql)
        repairs = [] if repaired == sql.strip() else ["value-cast"]
        errors = self._hard_errors(repaired)
        return ValidationResult(
            valid=not errors,
            errors=tuple(errors),
            normalized_sql=repaired,
            repairs_applied=tuple(repairs),
        )

    def _hard_errors(self, sql: str) -> list[str]:
        stripped = sql.strip().rstrip(";")
        errors: list[str] = []

        for pattern in self.dialect.readonly_violation_patterns():
            if re.search(pattern, stripped, flags=re.IGNORECASE):
                verb = pattern.removeprefix("\\b")
                errors.append(f"forbidden statement: {verb}")
                return errors

        head = stripped.split(maxsplit=1)[0].upper() if stripped else ""
        if head not in ("SELECT", "WITH"):
            errors.append(f"statement must start with SELECT or WITH, got {head!r}")
            return errors

        if len(_CTE_NAMES.findall(stripped)) > _MAX_CTE_DEPTH:
            errors.append(f"CTE depth exceeds {_MAX_CTE_DEPTH}")

        cte_names = {name.lower() for name in _CTE_NAMES.findall(stripped)}
        referenced = {match.lower() for match in _TABLE_REF.findall(stripped)}
        allowed = {table.lower() for table in self.allowed_tables}
        errors.extend(
            f"table not allowed: {table}"
            for table in sorted(referenced - cte_names)
            if table not in allowed
        )
        return errors

    def _repair_value_casts(self, sql: str) -> str:
        cast = self.dialect.cast_numeric(self.mapping.value)
        if cast in sql:
            return sql.strip().rstrip(";")

        repaired = sql
        for aggregate in _AGGREGATES:
            bare = re.compile(
                rf"\b{aggregate}\(\s*{re.escape(self.mapping.value)}\s*\)", re.IGNORECASE
            )
            repaired = bare.sub(f"{aggregate}({cast})", repaired)
        return repaired.strip().rstrip(";")
