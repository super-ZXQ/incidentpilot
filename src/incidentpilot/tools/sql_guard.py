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

# Reference database schema; customer names and arbitrary schemas are not exposed.
ALLOWED_SCHEMA = {
    "orders": {"id", "customer_id", "status", "total_cents", "created_at"},
    "order_items": {"id", "order_id", "sku", "qty", "price_cents"},
    "customers": {"id"},
}
ALLOWED_FUNCTIONS = {"COUNT", "SUM", "MIN", "MAX", "AVG", "COALESCE", "CAST", "LOWER", "UPPER", "LENGTH", "ABS"}


class SQLValidationError(ValueError):
    pass


def validate_readonly_sql(query: str) -> dict[str, Any]:
    """Validate that query is a safe read-only SELECT-like statement."""
    if not query or not query.strip():
        raise SQLValidationError("empty query")
    if len(query) > 8192:
        raise SQLValidationError("query too long")

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
    if sum(1 for _ in tree.walk()) > 200:
        raise SQLValidationError("query too complex")
    # CTE scope resolution is intentionally unsupported in this small readonly
    # interface: reject instead of guessing alias/column provenance.
    if tree.find(exp.CTE) or tree.find(exp.With):
        raise SQLValidationError("CTE queries are unsupported")
    tables = list(tree.find_all(exp.Table))
    if len(tables) > 4:
        raise SQLValidationError("too many table references")
    for table in tables:
        if table.catalog or table.db not in {"", "public"} or table.name not in ALLOWED_SCHEMA:
            raise SQLValidationError("table or schema is not allowlisted")
    aliases = {table.alias_or_name: table.name for table in tables}
    for column in tree.find_all(exp.Column):
        if column.table:
            name = aliases.get(column.table)
            allowed_columns = ALLOWED_SCHEMA.get(name, set())
        else:
            allowed_columns = set().union(*(ALLOWED_SCHEMA[table.name] for table in tables))
        if column.name not in allowed_columns:
            raise SQLValidationError("unknown or forbidden column")
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise SQLValidationError("wildcard projections are forbidden")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SQLValidationError(f"forbidden statement node: {type(node).__name__}")
        if node.args.get("into") or node.args.get("locks") or node.args.get("lock"):
            raise SQLValidationError("SELECT INTO or locking SELECT is forbidden")
        if isinstance(node, exp.Func):
            function = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
            if function not in ALLOWED_FUNCTIONS:
                raise SQLValidationError(f"function not allowlisted: {function}")
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

    for limit in tree.find_all(exp.Limit):
        expression = limit.expression
        if not isinstance(expression, exp.Literal) or not expression.is_int or not 1 <= int(expression.this) <= 100:
            raise SQLValidationError("LIMIT must be an integer between 1 and 100")
    # Limit execution as well as fetching: fetchmany alone does not bound DB work.
    safe = tree.copy().limit(100) if tree.args.get("limit") is None else tree
    return {"ok": True, "statement_type": type(tree).__name__, "query": safe.sql(dialect="postgres")}
