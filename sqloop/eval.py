"""Execution-accuracy scoring for SQLoop (Spider-style, with confidence intervals).

execution_match follows the spirit of Spider's official execution accuracy:
- run predicted and gold SQL against the same database and compare result sets;
- order matters ONLY when the gold query has an ORDER BY (otherwise multiset);
- column ORDER is NOT significant -- we accept any column permutation of the
  prediction that reproduces the gold result (matches the official eval, which
  permutes columns rather than requiring identical SELECT order);
- numbers are normalised (6 == 6.0, floats rounded) so trivial type/format
  differences don't cause false negatives.

wilson_ci gives a 95% confidence interval for an accuracy of k/n correct, so the
self-improvement curve can be reported with error bars instead of bare points.
"""

from __future__ import annotations

import math
import sqlite3
from itertools import permutations
from pathlib import Path

_MAX_PERM_COLS = 5  # cap column-permutation search to keep it cheap


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


def _cell(v):
    """Normalise a cell: 6 and 6.0 compare equal; floats rounded; else string."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        f = float(v)
        return int(f) if f.is_integer() else round(f, 6)
    return str(v)


def _norm_rows(rows: list[tuple]) -> list[tuple]:
    return [tuple(_cell(c) for c in row) for row in rows]


def _cols_match(pred: list[tuple], gold: list[tuple], order_matters: bool) -> bool:
    """True if some column permutation of `pred` reproduces `gold`."""
    if len(pred) != len(gold):
        return False
    if not gold:
        return True
    ncols = len(gold[0])
    if any(len(r) != ncols for r in pred):
        return False

    def eq(a: list[tuple], b: list[tuple]) -> bool:
        return a == b if order_matters else sorted(a, key=repr) == sorted(b, key=repr)

    if ncols > _MAX_PERM_COLS:  # too many columns to permute; compare as-is
        return eq(pred, gold)
    for perm in permutations(range(ncols)):
        permuted = [tuple(r[i] for i in perm) for r in pred]
        if eq(permuted, gold):
            return True
    return False


def execution_match(pred_sql: str, gold_sql: str, db_path: str | Path) -> bool:
    """True iff predicted SQL runs and yields the same result set as gold SQL."""
    ok_gold, gold_rows = _run_sql(db_path, gold_sql)
    if not ok_gold:
        return False  # gold should always run; non-scorable -> False
    ok_pred, pred_rows = _run_sql(db_path, pred_sql)
    if not ok_pred:
        return False
    order_matters = "order by" in (gold_sql or "").lower()
    return _cols_match(_norm_rows(pred_rows), _norm_rows(gold_rows), order_matters)


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes out of n (returns (lo, hi))."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))
