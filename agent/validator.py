"""SQL validator: sqlglot-AST read-only rules + bounded repair passes.

Layer 1 (hard rejects, fail-closed, in order): parse with the engine's
sqlglot dialect (tokenizer and parser errors both reject), single
statement only, root allowlist (Select/Union/Intersect/Except after a
bounded Subquery unwrap), mutation-node deny walk (Insert/Update/Delete/
Drop/Alter/Create/Into/Lock plus best-effort admin nodes), dangerous-
function deny (short named list with last-segment matching; FROM/JOIN
targets must be plain identifier tables), CTE-aware table allowlist
(qualified names are never allowed), CTE depth <= 5.
With the qualify flag (S2), after the mutation walk the statement is
re-parsed and sqlglot-qualified against the tenant's EAV schema; every
later check then reasons over the fully-resolved AST: the table
allowlist walks scope sources (base tables are exp.Table, CTEs are
Scopes — shadowing is exact, not heuristic), unresolvable columns and
schema-unknown tables reject with typed errors, and star selects can be
denied by config. Qualify failure is a finding, not an inconvenience.
Layer 2 (auto-repair): with repair_v2 off, the single regex value-cast
pass (v1 behavior, byte-identical). With repair_v2 on, the bounded AST
transform taxonomy from agent/repairs.py — one class per pass, stop at
the first clean re-validation, revert on a broken transform.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, cast

import sqlglot
from sqlglot import errors as sqerr
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope

from agent.repairs import run_repair_loop
from config.settings import ColumnMapping
from database.contracts import Dialect

_MAX_CTE_DEPTH: Final = 5
# sqlglot's optimizer submodules ship without full type information; the
# callable signature below is the observed, load-bearing contract.
_qualify: Final[Callable[..., exp.Expression]] = cast("Callable[..., exp.Expression]", qualify)
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
        self,
        dialect: Dialect,
        mapping: ColumnMapping,
        allowed_tables: tuple[str, ...],
        repair_v2: bool = False,
        repair_passes: int = 1,
        qualify: bool = False,
        deny_star_selects: bool = False,
        extra_schemas: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        """Bind dialect, column mapping, table allowlist, and repair mode."""
        self.dialect: Dialect = dialect
        self.mapping: ColumnMapping = mapping
        self.allowed_tables: tuple[str, ...] = allowed_tables
        self.repair_v2: bool = repair_v2
        self.repair_passes: int = repair_passes
        self.qualify: bool = qualify
        self.deny_star_selects: bool = deny_star_selects
        self.extra_schemas: Mapping[str, Mapping[str, str]] = extra_schemas or {}

    def validate(self, sql: str) -> ValidationResult:
        """Run hard rules, then repairs, then hard rules again."""
        if not self.repair_v2:
            repaired = self._repair_value_casts(sql)
            repairs = [] if repaired == sql.strip() else ["value-cast"]
            errors = self._hard_errors(repaired)
            return ValidationResult(
                valid=not errors,
                errors=tuple(errors),
                normalized_sql=repaired,
                repairs_applied=tuple(repairs),
            )
        looped = run_repair_loop(
            sql,
            self.dialect,
            self.mapping,
            self.repair_passes,
            self._hard_errors,
        )
        errors = self._hard_errors(looped.sql)
        return ValidationResult(
            valid=not errors,
            errors=tuple(errors),
            normalized_sql=looped.sql,
            repairs_applied=looped.repairs_applied,
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

        check_root = root
        if self.qualify:
            gate = self._qualify_gate(sql, root)
            if isinstance(gate, str):
                return [gate]
            check_root = gate

        errors = self._function_errors(check_root)
        errors.extend(self._table_errors(check_root))
        if len(list(check_root.find_all(exp.CTE))) > _MAX_CTE_DEPTH:
            errors.append(f"CTE depth exceeds {_MAX_CTE_DEPTH}")
        return errors

    def _qualify_gate(self, sql: str, root: exp.Expression) -> exp.Expression | str:
        """Qualified checking tree, or one typed error string.

        The qualified tree is a checking copy only — normalized_sql (the
        statement that executes) stays the repaired original, so the flag
        never changes execution semantics.
        """
        if self.deny_star_selects and any(isinstance(node, exp.Star) for node in root.walk()):
            return "qualify: star-select"
        try:
            ast = sqlglot.parse_one(sql, read=self.dialect.sqlglot_name)
            return _qualify(ast, dialect=self.dialect.sqlglot_name, schema=self._schema())
        except sqerr.OptimizeError as exc:
            return self._classify_qualify_error(root, str(exc))
        except Exception as exc:  # noqa: BLE001 (fail-closed: qualify failure is a finding)
            detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            return f"qualify: unsupported: {detail}"

    def _schema(self) -> dict[str, dict[str, str]]:
        schema = {
            self.mapping.table: {
                self.mapping.timestamp: "datetime",
                self.mapping.entity_id: "text",
                self.mapping.key: "text",
                self.mapping.value: "text",
            }
        }
        for table, columns in self.extra_schemas.items():
            schema[table] = dict(columns)
        return schema

    def _classify_qualify_error(self, root: exp.Expression, message: str) -> str:
        """Type one qualify failure: unknown tables outrank column issues."""
        schema = self._schema()
        unknown = sorted(
            {
                table.name.lower()
                for table in self._scope_tables(root)
                if table.name.lower() not in {name.lower() for name in schema}
                and table.name.lower() not in self._cte_names(root)
            }
        )
        if unknown:
            return f"qualify: schema-unknown-table: {', '.join(unknown)}"
        detail = message.splitlines()[0] if message else "could not be resolved"
        return f"qualify: unresolvable-column: {detail}"

    def _scope_tables(self, root: exp.Expression) -> list[exp.Table]:
        """Every real-table source across scopes (CTEs excluded by shape)."""
        try:
            scope_root = build_scope(root)
            if scope_root is None:
                return list(root.find_all(exp.Table))
            return [
                source
                for scope in scope_root.traverse()
                for source in scope.sources.values()
                if isinstance(source, exp.Table)
            ]
        except Exception:  # noqa: BLE001 (scope build failure falls back to the AST walk)
            return list(root.find_all(exp.Table))

    @staticmethod
    def _cte_names(root: exp.Expression) -> set[str]:
        return {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}

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
        if self.qualify:
            tables = self._scope_tables(root)
            cte_names: set[str] = set()
        else:
            tables = list(root.find_all(exp.Table))
            cte_names = self._cte_names(root)
        referenced: set[str] = set()
        for table in tables:
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
