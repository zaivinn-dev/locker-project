import os
import sqlite3
from pathlib import Path


def _db_path() -> Path:
    env_path = os.getenv("LOCKER_DB_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parent.parent / "locker.sqlite3"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              full_name TEXT NOT NULL,
              address TEXT,
              contact_number TEXT,
              age INTEGER,
              category TEXT,
              payment_status TEXT NOT NULL DEFAULT 'unpaid',
              paid_at TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        # Create payments table to track all revenue
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              member_id INTEGER NOT NULL,
              amount INTEGER NOT NULL,
              payment_type TEXT NOT NULL,  -- 'initial', 'renewal', 'penalty', etc.
              payment_date TEXT NOT NULL DEFAULT (datetime('now')),
              notes TEXT,
              FOREIGN KEY (member_id) REFERENCES members(id)
            )
            """
        )

        # Lightweight migration for existing databases: add columns if they don't exist.
        for col_def in [
            ("address", "TEXT"),
            ("contact_number", "TEXT"),
            ("age", "INTEGER"),
            ("category", "TEXT"),
            ("locker_id", "INTEGER"),
            ("payment_status", "TEXT"),
            ("paid_at", "TEXT"),
            ("fingerprint_uid", "TEXT"),
            ("rfid_uid", "TEXT"),
            ("expiry_date", "TEXT"),  # Add expiry date for membership renewals
            ("member_type", "TEXT DEFAULT 'regular'"),  # regular|guest
        ]:
            try:
                conn.execute(f"ALTER TABLE members ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                # Column probably already exists; ignore.
                pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lockers (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              label TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'available'
            )
            """
        )
        # Initialize with 4 physical lockers only
        for i in range(1, 5):
            conn.execute(
                "INSERT OR IGNORE INTO lockers (id, label) VALUES (?, ?)",
                (i, f"Locker {i}"),
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor_type TEXT NOT NULL, -- member|guest|admin|system
              actor_ref TEXT,           -- e.g. member_id or rfid uid
              action TEXT NOT NULL,     -- e.g. unlock_requested|unlocked|denied
              detail TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

