"""Single-generation cross-engine transpilation (S4).

transpile_to() is a thin, total wrapper: exactly one statement transpiled,
or None (caller falls back to native generation and logs the divergence).
"""

import sqlglot
from sqlglot import errors as sqerr


def transpile_to(sql: str, write: str) -> str | None:
    """Transpile one postgres-grammar statement to the target dialect.

    None when transpilation raises or produces anything other than exactly
    one statement — a finding for the caller's fallback, never a guess.
    """
    try:
        statements = sqlglot.transpile(sql, read="postgres", write=write)
    except (sqerr.ParseError, sqerr.TokenError, sqerr.OptimizeError, TypeError, ValueError):
        return None
    return statements[0] if len(statements) == 1 else None
