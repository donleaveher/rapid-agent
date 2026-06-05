"""Schema linking: pick the tables relevant to a question instead of dumping the
whole database schema.

Lexically scores each table by overlap between the question and the table's
name + column names, keeps the matches, then adds their foreign-key neighbours so
JOIN paths stay visible. Falls back to the full schema for small databases (where
linking buys nothing) or when nothing matches. Adaptive, like the memory module:

  SQLOOP_SCHEMA_LINK = auto | off            (default auto)
  SQLOOP_SCHEMA_MIN_TABLES = 5               (auto links only above this table count)

Pruning the schema *text* only changes what the generator SEES; the Executor still
runs against the full database, so this can never break execution -- it only makes
the prompt smaller and more focused (crucial for large real-world schemas).
"""

from __future__ import annotations

import os
import re
import sqlite3

from sqloop.db import default_db_path, get_schema


def _toks(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _introspect(db_path):
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        create = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"))
        cols, fks = {}, {}
        for t in tables:
            cols[t] = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]
            fks[t] = {r[2] for r in conn.execute(f'PRAGMA foreign_key_list("{t}")')}
    finally:
        conn.close()
    return tables, create, cols, fks


def linked_schema(db_path: str, question: str) -> str:
    """Return the CREATE statements for the question-relevant tables (+FK neighbours)."""
    path = db_path or str(default_db_path())
    mode = os.environ.get("SQLOOP_SCHEMA_LINK", "auto").lower()
    min_tables = int(os.environ.get("SQLOOP_SCHEMA_MIN_TABLES", "5"))

    tables, create, cols, fks = _introspect(path)
    if mode == "off" or len(tables) <= min_tables:
        return get_schema(path)

    qtok = _toks(question)
    scored = {t: len(qtok & (_toks(t) | {w for c in cols[t] for w in _toks(c)})) for t in tables}
    selected = {t for t, s in scored.items() if s > 0}
    if not selected:  # nothing matched -> don't risk hiding the answer
        return get_schema(path)

    # add foreign-key neighbours (both directions) so join paths are visible
    neighbours = set()
    for t in selected:
        neighbours |= fks.get(t, set())
    for t in tables:
        if fks.get(t, set()) & selected:
            neighbours.add(t)
    selected |= {t for t in neighbours if t in create}

    ordered = [t for t in tables if t in selected]  # preserve original order
    return "\n\n".join(create[t] for t in ordered)
