"""Create a tiny local SQLite database for Day 1 smoke tests.

Run: uv run python seed_db.py
Produces: data/sqloop_test.db with `users` and `orders` tables + sample rows.
This is throwaway scaffolding; Day 2 replaces it with the real Spider databases.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "sqloop_test.db"

SCHEMA = """
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    country TEXT NOT NULL,
    age     INTEGER
);

CREATE TABLE orders (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id),
    amount    REAL NOT NULL,
    status    TEXT NOT NULL
);
"""

USERS = [
    (1, "Alice", "US", 30),
    (2, "Bob", "US", 25),
    (3, "Carol", "UK", 41),
    (4, "Dan", "DE", 38),
    (5, "Eve", "UK", 29),
]

ORDERS = [
    (1, 1, 120.0, "paid"),
    (2, 1, 80.0, "paid"),
    (3, 2, 50.0, "pending"),
    (4, 3, 200.0, "paid"),
    (5, 4, 30.0, "cancelled"),
    (6, 5, 95.0, "paid"),
]


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?)", USERS)
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ORDERS)
        conn.commit()
    finally:
        conn.close()
    print(f"Seeded {DB_PATH} — {len(USERS)} users, {len(ORDERS)} orders.")


if __name__ == "__main__":
    main()
