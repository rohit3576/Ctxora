"""SQL validator: sqlglot-AST read-only rules + one repair pass.

Layer 1 (hard rejects, fail-closed, in order): parse with the engine's
sqlglot dialect (tokenizer and parser errors both reject), single
statement only, root allowlist (Select/Union/Intersect/Except after a
bounded Subquery unwrap), mutation-node deny walk (Insert/Update/Delete/
Drop/Alter/Create/Into/Lock plus best-effort admin nodes), dangerous-
function deny (short named list with last-segment matching; FROM/JOIN
targets must be plain identifier tables), CTE-aware table allowlist
(qualified names are never allowed), CTE depth <= 5.
Layer 2 (auto-repair): bare aggregates over the EAV value column get the
dialect's null-safe numeric cast, once, then re-validate.
"""

import re
from dataclasses import dataclass
from typing import Final

import sqlglot
from sqlglot import errors as sqerr
from sqlglot import exp

from config.settings import ColumnMapping
from database.contracts import Dialect

_MAX_CTE_DEPTH: Final = 5
_MAX_ROOT_UNWRAP: Final = 4
_AGGREGATES: Final = ("avg", "sum", "min", "max", "median")

_ROOT_NODES: Final = (exp.Select, exp.Union, exp.Intersect, exp.Except)
# Into: SELECT ... INTO has a Select root. Lock: FOR UPDATE / FOR SHARE —
# the verb regexes (removed in a later phase) were their only previous block.
_MUTATION_NODES: Final = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Into,
    exp.Lock,
)
# Admin nodes: include when the pinned sqlglot version has them, silently
# drop otherwise (non-Select roots are rejected by the allowlist anyway).
_BEST_EFFORT_MUTATION_NODES: Final = tuple(
    node
    for node in (
        getattr(exp, name, None)
        for name in ("Grant", "Revoke", "TruncateTable", "Merge", "Copy", "Command")
    )
    if node is not None
)
_DENY_MUTATION_WALK: Final = _MUTATION_NODES + _BEST_EFFORT_MUTATION_NODES
# File/table-reading functions; last-segment matching also hits qualified forms.
_DENY_FUNCTIONS: Final = frozenset(("read_csv", "pg_read_file", "pg_read_binary_file", "s3"))


def _unwrap_root(node: exp.Expression | None) -> exp.Expression | None:
    """Peel up to _MAX_ROOT_UNWRAP Subquery wrappers off the statement root."""
    root: exp.Expression | None = node
    for _ in range(_MAX_ROOT_UNWRAP):
        if not isinstance(root, exp.Subquery):
            break
        inner = root.this
        if not isinstance(inner, exp.Expression):
            break
        root = inner
    return root


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
        statements = self._parse(sql)
        if statements is None:
            return ["unparseable statement"]
        if len(statements) != 1:
            return ["multi-statement input rejected"]

        root = _unwrap_root(statements[0])
        if root is None:
            return ["unparseable statement"]
        if not isinstance(root, _ROOT_NODES):
            return [f"forbidden statement: {type(root).__name__}"]

        for node in root.walk():
            if isinstance(node, _DENY_MUTATION_WALK):
                return [f"forbidden statement: {type(node).__name__}"]

        errors = self._function_errors(root)
        errors.extend(self._table_errors(root))
        if len(list(root.find_all(exp.CTE))) > _MAX_CTE_DEPTH:
            errors.append(f"CTE depth exceeds {_MAX_CTE_DEPTH}")
        return errors

    def _function_errors(self, root: exp.Expression) -> list[str]:
        errors = [
            f"forbidden function: {name}"
            for name in (str(node.name) for node in root.walk() if isinstance(node, exp.Anonymous))
            if name.rsplit(".", 1)[-1].lower() in _DENY_FUNCTIONS
        ]
        errors.extend(
            "forbidden function: table-function"
            for table in root.find_all(exp.Table)
            if not isinstance(table.this, exp.Identifier)
        )
        return errors

    def _table_errors(self, root: exp.Expression) -> list[str]:
        errors: list[str] = []
        cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
        referenced: set[str] = set()
        for table in root.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                continue
            if table.db or table.catalog:
                qualifier = table.db or table.catalog
                errors.append(f"table not allowed: {qualifier}.{table.name}")
                continue
            referenced.add(table.name.lower())

        allowed = {table.lower() for table in self.allowed_tables}
        errors.extend(
            f"table not allowed: {name}"
            for name in sorted(referenced - cte_names)
            if name not in allowed
        )
        return errors

    def _parse(self, sql: str) -> list[sqlglot.Expression | None] | None:
        try:
            return sqlglot.parse(sql, read=self.dialect.sqlglot_name)
        except (sqerr.ParseError, sqerr.TokenError):
            return None

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
