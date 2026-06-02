"""Execution-accuracy scoring for SQLoop.

Day 3 metric: run the predicted SQL and the gold SQL against the same database
and compare their result sets. This is a simplified version of Spider's official
execution accuracy:
- rows are compared as an order-insensitive multiset (sorted list of tuples),
  so queries without ORDER BY still match;
- values are stringified before comparison to avoid int/float/str mismatches;
- column ORDER is kept significant (a conservative choice -- may undercount when
  the model returns the right columns in a different order).

Good enough for a baseline; can be upgraded to the official test-suite later.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _run_sql(db_path: str | Path, sql: str) -> tuple[bool, list[tuple]]:
    """Execute SQL read-only; return (ok, rows). ok=False on any error."""
    if not sql or not sql.strip():
        return False, []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(sql).fetchall()
        return True, rows
    except Exception:
        return False, []
    finally:
        conn.close()


def _normalize(rows: list[tuple]) -> list[tuple]:
    """Order-insensitive, type-insensitive view of a result set."""
    return sorted(tuple(str(v) for v in row) for row in rows)


def execution_match(pred_sql: str, gold_sql: str, db_path: str | Path) -> bool:
    """True iff predicted SQL runs and yields the same result set as gold SQL."""
    ok_gold, gold_rows = _run_sql(db_path, gold_sql)
    if not ok_gold:
        # Gold should always run; if it doesn't, treat as non-scorable -> False.
        return False
    ok_pred, pred_rows = _run_sql(db_path, pred_sql)
    if not ok_pred:
        return False
    return _normalize(pred_rows) == _normalize(gold_rows)
