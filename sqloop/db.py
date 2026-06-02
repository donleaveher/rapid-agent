"""SQLite access for SQLoop: schema introspection + the Executor tool.

`execute_sql` is the Executor in the task plane. It is wrapped as an ADK
FunctionTool, so each call shows up as its own span in Phoenix. The database it
runs against is chosen per turn via session state ("db_path"), so the same
pipeline serves any Spider database.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from google.adk.tools import ToolContext

# Day 1 throwaway DB; used when no per-turn db_path is set in state.
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sqloop_test.db"


def default_db_path() -> Path:
    """Fallback database path (env override or the Day 1 test DB)."""
    return Path(os.environ.get("SQLOOP_DB_PATH", DEFAULT_DB_PATH))


def get_schema(db_path: str | Path | None = None) -> str:
    """Return the CREATE TABLE statements for every table in the database."""
    path = Path(db_path) if db_path else default_db_path()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND sql IS NOT NULL "
            "ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return "\n\n".join(r[0] for r in rows)


def execute_sql(sql: str, tool_context: ToolContext) -> str:
    """Execute a single read-only SQLite SELECT and return the result as text.

    Args:
      sql: One SQLite SELECT statement. No INSERT/UPDATE/DELETE/DDL.

    Returns:
      A text table (header + rows), "(no rows)" when empty, or a line that
      starts with "SQL_ERROR:" when the query fails or is not a SELECT.
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped.lower().startswith(("select", "with")):
        return "SQL_ERROR: only SELECT/WITH queries are allowed."

    db_path = tool_context.state.get("db_path") or str(default_db_path())
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(stripped)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    except Exception as exc:  # surfaced to the agent so it can repair
        return f"SQL_ERROR: {exc}"
    finally:
        conn.close()

    if not rows:
        return "(no rows)"

    header = " | ".join(cols)
    body = "\n".join(" | ".join(str(v) for v in row) for row in rows[:50])
    suffix = f"\n... ({len(rows)} rows total)" if len(rows) > 50 else ""
    return f"{header}\n{body}{suffix}"
