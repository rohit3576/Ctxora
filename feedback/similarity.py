"""Structural similarity + correction deltas for the flywheel (S3).

structural_signature(): a qualified shape signature — alias renames, column
reorder, formatting, and qualifier churn collapse to one hash; literal and
filter changes keep signatures apart. similar(): exact normalize_sql
equality first (cheap), signature equality second. correction_delta():
labeled role deltas between the SQL a user corrected and the SQL that
succeeded — what the correction teaches, for review and prompt surfacing.

None of this is a security gate: signatures only decide flywheel dedupe
and decay, and delta mining is wrapped non-blocking at the call site.
"""

import hashlib
import json
from collections import Counter
from typing import Final, TypeAlias

import sqlglot
from sqlglot import errors as sqerr
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from feedback.normalize import normalize_sql

Shape: TypeAlias = tuple[object, ...]
_AGG_BASE: Final = exp.AggFunc
_TIME_WINDOW_MARKS: Final = ("interval",)


_STATEMENT_ROOTS: Final = (exp.Select, exp.Union, exp.Intersect, exp.Subquery)


def _parse_qualified(sql: str) -> exp.Expression | None:
    try:
        ast = sqlglot.parse_one(sql)
        if not isinstance(ast, _STATEMENT_ROOTS):
            return None
        return qualify(ast)
    except (sqerr.ParseError, sqerr.TokenError, sqerr.OptimizeError, TypeError, ValueError):
        return None


def _conjuncts(condition: exp.Expression | None) -> list[exp.Expression]:
    """Flatten one WHERE condition tree into its AND conjuncts."""
    if condition is None:
        return []
    parts: list[exp.Expression] = []

    def flatten(node: exp.Expression) -> None:
        if isinstance(node, exp.And):
            flatten(node.this)
            flatten(node.expression)
        else:
            parts.append(node)

    flatten(condition)
    return parts


def _shape(node: exp.Expression) -> Shape:
    """Order-insensitive structural key for one AST node.

    Aliases and table qualifiers drop; commutative lists (output columns,
    GROUP BY, WHERE conjuncts) sort.
    """
    if isinstance(node, exp.Alias):
        return _shape(node.this)
    if isinstance(node, exp.Column):
        return ("col", node.name)
    if isinstance(node, exp.Literal):
        return ("lit", bool(node.is_string), str(node.this))
    if isinstance(node, exp.Table):
        return ("table", node.db, node.name)
    if isinstance(node, exp.Where):
        conjunct_shapes: tuple[Shape, ...] = tuple(
            sorted((_shape(part) for part in _conjuncts(node.this)), key=repr)
        )
        return ("where", conjunct_shapes)
    if isinstance(node, (exp.Select, exp.Group)) and isinstance(node.args.get("expressions"), list):
        children = tuple(
            sorted(
                _shape(child)
                for child in node.args["expressions"]
                if isinstance(child, exp.Expression)
            )
        )
        rest = {
            name: _shape(value)
            for name, value in node.args.items()
            if name != "expressions" and isinstance(value, exp.Expression)
        }
        return (type(node).__name__, "exprs", children, tuple(sorted(rest.items())))
    children = {
        name: _shape(value)
        for name, value in node.args.items()
        if isinstance(value, exp.Expression)
    }
    return (type(node).__name__, tuple(sorted(children.items())))


def structural_signature(sql: str) -> str:
    """Stable shape hash for one SQL statement.

    Unparseable input gets a fallback marker no parseable signature equals.
    """
    ast = _parse_qualified(sql)
    if ast is None:
        return "unparseable:" + normalize_sql(sql)
    payload = json.dumps(_shape(ast), default=repr)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def similar(a: str, b: str) -> bool:
    """Exact -> structural -> different (threshold-free by design)."""
    if normalize_sql(a) == normalize_sql(b):
        return True
    return structural_signature(a) == structural_signature(b)


def _output_columns(ast: exp.Expression) -> list[str]:
    select = ast if isinstance(ast, exp.Select) else next(ast.find_all(exp.Select), None)
    if select is None or not isinstance(select.args.get("expressions"), list):
        return []
    return sorted(
        expr.alias_or_name
        for expr in select.args["expressions"]
        if isinstance(expr, exp.Expression)
    )


def _filters(ast: exp.Expression) -> tuple[list[str], list[str]]:
    """(plain_filters, time_windows) as sorted lowercase renders."""
    wheres = list(ast.find_all(exp.Where))
    conjuncts = [part for where in wheres for part in _conjuncts(where.this)]
    renders = sorted(part.sql(comments=False).lower() for part in conjuncts)
    plain = [r for r in renders if not any(mark in r for mark in _TIME_WINDOW_MARKS)]
    windows = [r for r in renders if any(mark in r for mark in _TIME_WINDOW_MARKS)]
    return plain, windows


def _aggregations(ast: exp.Expression) -> Counter[str]:
    return Counter(type(node).__name__ for node in ast.find_all(_AGG_BASE))


def _tables(ast: exp.Expression) -> list[str]:
    return sorted({table.name for table in ast.find_all(exp.Table)})


def _pair_changes(before: Counter[str], after: Counter[str]) -> list[str]:
    removed = sorted((before - after).elements())
    added = sorted((after - before).elements())
    changes = [f"{old} -> {new}" for old, new in zip(removed, added, strict=False)]
    changes.extend(f"+ {name}" for name in added[len(removed) :])
    changes.extend(f"- {name}" for name in removed[len(added) :])
    return changes


def correction_delta(previous_sql: str, corrected_sql: str) -> dict[str, list[str]]:
    """Labeled deltas between the corrected-away SQL and the SQL that worked."""
    before, after = _parse_qualified(previous_sql), _parse_qualified(corrected_sql)
    if before is None or after is None:
        return {}

    before_filters, before_windows = _filters(before)
    after_filters, after_windows = _filters(after)
    delta: dict[str, list[str]] = {}
    if added := sorted(set(after_filters) - set(before_filters)):
        delta["added_filters"] = added
    if removed := sorted(set(before_filters) - set(after_filters)):
        delta["removed_filters"] = removed
    if agg_changes := _pair_changes(_aggregations(before), _aggregations(after)):
        delta["aggregation_changes"] = agg_changes
    if columns_added := sorted(set(_output_columns(after)) - set(_output_columns(before))):
        delta["added_columns"] = columns_added
    if columns_removed := sorted(set(_output_columns(before)) - set(_output_columns(after))):
        delta["removed_columns"] = columns_removed
    if windows_added := sorted(set(after_windows) - set(before_windows)):
        delta["added_time_windows"] = windows_added
    if windows_removed := sorted(set(before_windows) - set(after_windows)):
        delta["removed_time_windows"] = windows_removed
    if tables_added := sorted(set(_tables(after)) - set(_tables(before))):
        delta["added_tables"] = tables_added
    if tables_removed := sorted(set(_tables(before)) - set(_tables(after))):
        delta["removed_tables"] = tables_removed
    return delta
