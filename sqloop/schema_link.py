"""Schema linking: pick the tables relevant to a question instead of dumping the
whole database schema.

Scores each table by overlap between the question and:
  1. the table's name + column names              (lexical, always on)
  2. sampled distinct VALUES in its text columns   (value linking, default on) --
     so a question that mentions a cell value ("singers from France") finds the
     table that actually stores it, which name/column matching alone cannot see.
  3. an optional pluggable SEMANTIC ranker          (off by default) -- bridges the
     vocabulary gap (question says "artist", column is "singer") the same way the
     memory module's dense-ranker hook does. Plug a real embedder via
     set_table_ranker(fn); zero-dep and fully lexical until then.

Keeps the matches, then adds their foreign-key neighbours so JOIN paths stay
visible. Falls back to the full schema for small databases or when nothing matches.

  SQLOOP_SCHEMA_LINK        = auto | off     (default auto)
  SQLOOP_SCHEMA_MIN_TABLES  = 5              (auto links only above this table count)
  SQLOOP_SCHEMA_VALUES      = on | off       (default on; value linking)
  SQLOOP_SCHEMA_VALUE_COLS  = 8              (text columns sampled per table)
  SQLOOP_SCHEMA_VALUE_ROWS  = 20             (distinct values sampled per column)
  SQLOOP_SCHEMA_SEM_TOPK    = 3              (tables added by the semantic ranker)

Pruning the schema *text* only changes what the generator SEES; the Executor still
runs against the full database, so this can never break execution -- it only makes
the prompt smaller and more focused (crucial for large real-world schemas).
"""

from __future__ import annotations

import functools
import os
import re
import sqlite3
from typing import Callable, Optional

from sqloop.db import default_db_path, get_schema

# Pluggable semantic table ranker (off by default), mirroring memory.set_dense_ranker:
#   (question: str, table_descriptions: list[str]) -> list[float] scores
_table_ranker: Optional[Callable[[str, list[str]], list[float]]] = None


def set_table_ranker(fn) -> None:
    """Plug a semantic ranker so 'artist' can match the 'singer' table, etc."""
    global _table_ranker
    _table_ranker = fn


def _toks(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _vtoks(s: str) -> set[str]:
    """Value tokens: drop short tokens and pure numbers (years etc.) to stay discriminative."""
    return {w for w in _toks(s) if len(w) >= 3 and any(ch.isalpha() for ch in w)}


def _is_text_decl(decl: str) -> bool:
    d = (decl or "").lower()
    return ("char" in d) or ("text" in d) or ("clob" in d) or (d == "")


@functools.lru_cache(maxsize=128)
def _value_index(db_path: str) -> dict:
    """Per-table token set from sampled distinct text cell values. Cached per db
    (the database doesn't change across questions in a run)."""
    max_cols = int(os.environ.get("SQLOOP_SCHEMA_VALUE_COLS", "8"))
    max_rows = int(os.environ.get("SQLOOP_SCHEMA_VALUE_ROWS", "20"))
    conn = sqlite3.connect(db_path)
    idx: dict[str, set] = {}
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            toks: set[str] = set()
            info = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
            text_cols = [c[1] for c in info if _is_text_decl(c[2])]
            for col in text_cols[:max_cols]:
                try:
                    rows = conn.execute(
                        f'SELECT DISTINCT "{col}" FROM "{t}" LIMIT {max_rows}').fetchall()
                except Exception:  # noqa: BLE001 - skip unreadable columns
                    continue
                for (v,) in rows:
                    if isinstance(v, str):
                        toks |= _vtoks(v)
            idx[t] = toks
    finally:
        conn.close()
    return idx


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
    val_idx = (_value_index(path)
               if os.environ.get("SQLOOP_SCHEMA_VALUES", "on").lower() != "off" else {})

    def score(t: str) -> int:
        name_col = _toks(t) | {w for c in cols[t] for w in _toks(c)}
        return len(qtok & name_col) + len(qtok & val_idx.get(t, set()))  # lexical + value

    selected = {t for t in tables if score(t) > 0}

    # Optional semantic ranker: add its top-k tables (recall-oriented union).
    if _table_ranker is not None:
        try:
            descs = [f"{t}: {', '.join(cols[t])}" for t in tables]
            sem = _table_ranker(question, descs)
            topk = int(os.environ.get("SQLOOP_SCHEMA_SEM_TOPK", "3"))
            order = sorted(range(len(tables)), key=lambda i: -sem[i])
            selected |= {tables[i] for i in order[:topk]}
        except Exception:  # noqa: BLE001 - semantic ranking is best-effort
            pass

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
