"""Read-only SQL validation using sqlglot AST + forbidden keyword defense-in-depth."""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

ALLOWED_STATEMENT_TYPES = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
)

FORBIDDEN_NODES = tuple(
    node
    for node in (
        getattr(exp, "Insert", None),
        getattr(exp, "Update", None),
        getattr(exp, "Delete", None),
        getattr(exp, "Drop", None),
        getattr(exp, "Alter", None),
        getattr(exp, "Create", None),
        getattr(exp, "Grant", None),
        getattr(exp, "TruncateTable", None),
        getattr(exp, "Truncate", None),
        getattr(exp, "Copy", None),
        getattr(exp, "Command", None),
    )
    if node is not None
)

FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "create",
    "grant",
    "revoke",
    "copy",
    "call",
    "do",
    "vacuum",
    "reindex",
    "cluster",
    "refresh",
    "execute",
    "prepare",
    "listen",
    "notify",
)

FORBIDDEN_FUNCTIONS = {
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "dblink",
    "dblink_exec",
    "lo_import",
    "lo_export",
    "set_config",
    "current_setting",
}


class SQLValidationError(ValueError):
    pass


def validate_readonly_sql(query: str) -> dict[str, Any]:
    """Validate that query is a safe read-only SELECT-like statement."""
    if not query or not query.strip():
        raise SQLValidationError("empty query")

    lowered = query.lower()
    stripped = " ".join(lowered.split())
    hard_forbidden = {
        "insert", "update", "delete", "drop", "alter", "truncate", "create", "grant", "revoke"
    }
    for kw in FORBIDDEN_KEYWORDS:
        if (
            f" {kw} " in f" {stripped} " or stripped.startswith(kw + " ")
        ) and kw in hard_forbidden:
            raise SQLValidationError(f"forbidden keyword: {kw}")

    try:
        statements = sqlglot.parse(query, read="postgres")
    except Exception as exc:
        raise SQLValidationError(f"sql parse error: {exc}") from exc

    if not statements:
        raise SQLValidationError("no statements parsed")
    if len(statements) != 1:
        raise SQLValidationError("multiple statements are not allowed")

    tree = statements[0]
    if tree is None:
        raise SQLValidationError("empty statement")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SQLValidationError(f"forbidden statement node: {type(node).__name__}")
        if isinstance(node, exp.Anonymous):
            name = (node.name or "").lower()
            if name in FORBIDDEN_FUNCTIONS:
                raise SQLValidationError(f"forbidden function: {name}")

    if tree.args.get("into") is not None:
        raise SQLValidationError("SELECT INTO is forbidden")
    if tree.args.get("locks") or tree.args.get("lock"):
        raise SQLValidationError("locking SELECT is forbidden")

    if not isinstance(tree, ALLOWED_STATEMENT_TYPES):
        # allow WITH ... SELECT
        if (
            isinstance(tree, exp.With)
            and any(isinstance(s, ALLOWED_STATEMENT_TYPES) for s in tree.expressions)
        ) or (
            isinstance(tree, exp.Subquery) and isinstance(tree.this, ALLOWED_STATEMENT_TYPES)
        ):
            pass
        else:
            raise SQLValidationError(f"statement type not allowed: {type(tree).__name__}")

    return {"ok": True, "statement_type": type(tree).__name__}
