"""Layer-2 SQL repairs: bounded AST-transform taxonomy (S1).

Each class is a parse -> mutate -> regenerate transform that only fires on
its trigger shape — unlike the plan's original "stop at first clean"
sketch, transforms are NOT validity-gated: value-cast, add-limit, and
strip-junk are quality repairs that must fire on already-valid SQL (v1
applied value-cast before validation for the same reason). The loop
applies one class per pass and stops at the fixpoint (nothing fires) or
the pass budget; the caller's hard rules remain the execution gate, so
fail-closed is preserved regardless of repair order.

A candidate that fails to re-parse is discarded and the loop stops on the
last known-good SQL — a half-transformed tree never reaches execution.

qualify-columns is deliberately absent: it lands with S2's qualify pass.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import sqlglot
from sqlglot import errors as sqerr
from sqlglot import exp

from config.settings import ColumnMapping
from database.contracts import Dialect

_logger = logging.getLogger("ctxora.repairs")

REPAIR_LIMIT: Final = 1000
_ERROR_FIXING_ORDER: Final = ("inline-cte-depth",)
_QUALITY_ORDER: Final = ("strip-junk", "value-cast", "add-limit")
REPAIR_CLASS_ORDER: Final = _ERROR_FIXING_ORDER + _QUALITY_ORDER
_AGGREGATE_NODES: Final = (exp.Avg, exp.Sum, exp.Min, exp.Max, exp.Median)


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Loop outcome: last-good SQL plus cumulative repair class names."""

    sql: str
    repairs_applied: tuple[str, ...]
    aborted: bool


def _parse(sql: str, dialect: Dialect) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, read=dialect.sqlglot_name)
    except (sqerr.ParseError, sqerr.TokenError):
        return None


def _parses_single(sql: str, dialect: Dialect) -> bool:
    """Verify with the same strictness the validator will apply."""
    try:
        statements = sqlglot.parse(sql, read=dialect.sqlglot_name)
    except (sqerr.ParseError, sqerr.TokenError):
        return False
    return len(statements) == 1 and statements[0] is not None


def _strip_junk(sql: str, dialect: Dialect, _mapping: ColumnMapping) -> str | None:
    if not any(mark in sql for mark in (";", "--", "/*")):
        return None
    try:
        statements = sqlglot.parse(sql, read=dialect.sqlglot_name)
    except (sqerr.ParseError, sqerr.TokenError):
        return None
    if len(statements) != 1 or statements[0] is None:
        return None
    return statements[0].sql(dialect=dialect.sqlglot_name, comments=False)


def _value_cast(sql: str, dialect: Dialect, mapping: ColumnMapping) -> str | None:
    ast = _parse(sql, dialect)
    if ast is None:
        return None
    value = mapping.value.lower()
    fired = False
    for aggregate in ast.find_all(*_AGGREGATE_NODES):
        arg = aggregate.this
        if isinstance(arg, exp.Column) and not arg.table and arg.name.lower() == value:
            cast = sqlglot.parse_one(dialect.cast_numeric(mapping.value))
            aggregate.set("this", cast)
            fired = True
    return ast.sql(dialect=dialect.sqlglot_name) if fired else None


def _add_limit(sql: str, dialect: Dialect, mapping: ColumnMapping) -> str | None:
    ast = _parse(sql, dialect)
    if ast is None or not isinstance(ast, exp.Select) or ast.args.get("limit") is not None:
        return None
    allowed = {mapping.table.lower()}
    tables = {table.name.lower() for table in ast.find_all(exp.Table)}
    if not (tables & allowed):
        return None
    ast.set("limit", exp.Limit(expression=exp.Literal.number(REPAIR_LIMIT)))
    return ast.sql(dialect=dialect.sqlglot_name)


def _inline_deepest_cte(sql: str, dialect: Dialect, _mapping: ColumnMapping) -> str | None:
    ast = _parse(sql, dialect)
    if ast is None or not isinstance(ast, exp.Select):
        return None
    with_node = ast.args.get("with")
    if with_node is None or with_node.recursive or len(with_node.expressions) <= 1:
        return None
    deepest = with_node.expressions[-1]
    name = deepest.alias_or_name.lower()
    body = deepest.this
    fired = False
    for table in list(ast.find_all(exp.Table)):
        if isinstance(table.this, exp.Identifier) and not table.db and table.name.lower() == name:
            table.replace(exp.Subquery(this=body.copy(), alias=table.args.get("alias")))
            fired = True
    if not fired:
        return None
    with_node.expressions.remove(deepest)
    if not with_node.expressions:
        ast.set("with", None)
    return ast.sql(dialect=dialect.sqlglot_name)


REPAIR_TRANSFORMS: Final[dict[str, Callable[[str, Dialect, ColumnMapping], str | None]]] = {
    "strip-junk": _strip_junk,
    "value-cast": _value_cast,
    "add-limit": _add_limit,
    "inline-cte-depth": _inline_deepest_cte,
}


def run_repair_loop(
    sql: str,
    dialect: Dialect,
    mapping: ColumnMapping,
    max_passes: int,
    hard_errors: Callable[[str], list[str]],
) -> RepairResult:
    """One repair class per pass until fixpoint or budget; fail-closed.

    Error-fixing classes (inline-cte-depth) run while hard errors exist;
    quality classes (strip-junk, value-cast, add-limit) run only once the
    statement is clean — a quality repair must never spend a pass that an
    error-fixing repair needs. Whatever this returns is re-judged by the
    validator's hard rules, so an aborted or partial repair can never
    smuggle SQL past Layer 1.
    """
    current = sql.strip()
    applied: list[str] = []
    for pass_number in range(max_passes):
        order = _ERROR_FIXING_ORDER if hard_errors(current) else _QUALITY_ORDER
        for class_name in order:
            candidate = REPAIR_TRANSFORMS[class_name](current, dialect, mapping)
            if candidate is None or candidate == current:
                continue
            if not _parses_single(candidate, dialect):
                _logger.warning(
                    "repair %s produced unparseable SQL; reverting to last good state",
                    class_name,
                )
                return RepairResult(current, tuple(applied), aborted=True)
            current = candidate
            applied.append(class_name)
            _logger.info("repair pass %d applied %s", pass_number + 1, class_name)
            break
        else:
            break
    return RepairResult(current, tuple(applied), aborted=False)
