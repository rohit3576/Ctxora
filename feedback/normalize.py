"""Canonical SQL normalization for the flywheel (exact-comparison path)."""

import re

import sqlglot
from sqlglot import errors as sqerr


def normalize_sql(sql: str) -> str:
    """Canonical form for SQL comparison: reparsed, comments stripped, lowercase.

    Dedupe only — never executed, never a security gate. Input-level parse
    failures (ParseError and its tokenizer sibling TokenError) fall back to
    legacy whitespace-collapse; anything else is a bug and propagates.
    """
    try:
        return sqlglot.parse_one(sql).sql(comments=False).lower()
    except (sqerr.ParseError, sqerr.TokenError, TypeError, ValueError):
        return re.sub(r"\s+", " ", sql).strip().lower()
