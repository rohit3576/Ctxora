"""Document version ordering for the family lifecycle (R4e).

doc_version strings ("1", "v2", "2.0", "1.1") become comparable tuples:
the optional v/V prefix is dropped, dot-separated components become ints
(non-numeric components count as 0), and trailing zeros are stripped so
"1" == "1.0" and "2" < "2.3" < "2.10".
"""

_VERSION_PREFIXES: tuple[str, ...] = ("v", "V")


def version_key(version: str) -> tuple[int, ...]:
    """Total-order key for one doc_version string; empty/odd input is (0,)."""
    cleaned = version
    for prefix in _VERSION_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    components = [int(part) if part.isdigit() else 0 for part in cleaned.split(".")]
    while components and components[-1] == 0 and len(components) > 1:
        components.pop()
    return tuple(components) if components else (0,)
