"""Access to the Spider text-to-SQL dataset (data/spider/).

Layout (after unzip):
  data/spider/dev.json                       -- 1034 dev examples
  data/spider/tables.json                    -- schemas
  data/spider/database/<db_id>/<db_id>.sqlite-- executable dbs
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

SPIDER_DIR = Path(__file__).resolve().parent.parent / "data" / "spider"
DEV_JSON = SPIDER_DIR / "dev.json"
DB_DIR = SPIDER_DIR / "database"


@functools.lru_cache(maxsize=1)
def load_dev() -> list[dict]:
    """Load and cache the Spider dev split (list of {db_id, question, query, ...})."""
    with open(DEV_JSON, encoding="utf-8") as f:
        return json.load(f)


def dev_examples(limit: int | None = None) -> list[dict]:
    """Return the first `limit` dev examples (or all when limit is None)."""
    data = load_dev()
    return data[:limit] if limit else data


def db_path_for(db_id: str) -> Path:
    """Path to the sqlite file for a given Spider db_id."""
    return DB_DIR / db_id / f"{db_id}.sqlite"
