import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.errors
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore


def _db_path() -> Path:
    env_path = os.getenv("LOCKER_DB_PATH")
    if env_path:
        return Path(env_path)

    current_dir = Path(__file__).resolve().parent
    candidate_paths = [
        current_dir / "locker.sqlite3",
        current_dir / "locker.db",
        current_dir.parent / "locker.sqlite3",
        current_dir.parent / "locker.db",
    ]

    for path in candidate_paths:
        if path.exists():
            return path

    return current_dir.parent / "locker.sqlite3"


def _database_url() -> Optional[str]:
    return os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")


class DBConnection:
    def __init__(self, conn: Any, is_postgres: bool):
        self._conn = conn
        self._is_postgres = is_postgres
        self._cursor = None

    def __enter__(self) -> "DBConnection":
        if self._is_postgres:
            self._cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            self._conn.row_factory = sqlite3.Row
            self._cursor = self._conn.cursor()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self._conn.rollback()
                return False
            self._conn.commit()
        finally:
            if self._cursor is not None:
                self._cursor.close()
            if self._conn is not None:
                self._conn.close()
        return False

    def _translate_sql(self, sql: str, params: Optional[Iterable[Any]] = None) -> str:
        if not self._is_postgres:
            return sql

        translated = sql
        translated = translated.replace("datetime('now')", "CURRENT_TIMESTAMP")
        translated = translated.replace("AUTOINCREMENT", "")

        if "INSERT OR IGNORE INTO" in translated:
            translated = translated.replace("INSERT OR IGNORE INTO", "INSERT INTO")
            if " ON CONFLICT DO NOTHING" not in translated:
                translated = translated.strip()
                if translated.endswith(")"):
                    translated += " ON CONFLICT DO NOTHING"

        translated = re.sub(r"\?", "%s", translated)
        return translated

    def execute(self, sql: str, params: Optional[Iterable[Any]] = None):
        params = params or ()
        sql = self._translate_sql(sql, params)
        try:
            return self._cursor.execute(sql, params)
        except Exception as exc:
            if self._is_postgres and isinstance(exc, psycopg2.errors.DuplicateColumn):
                raise
            raise

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]):
        sql = self._translate_sql(sql)
        return self._cursor.executemany(sql, seq_of_params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def commit(self):
        self._conn.commit()

    def close(self):
        if self._cursor is not None:
            self._cursor.close()
        if self._conn is not None:
            self._conn.close()


def connect() -> DBConnection:
    database_url = _database_url()
    if database_url:
        if psycopg2 is None:
            raise RuntimeError("PostgreSQL support requires psycopg2-binary. Install it before using DATABASE_URL.")
        conn = psycopg2.connect(database_url)
        return DBConnection(conn, is_postgres=True)

    sqlite_conn = sqlite3.connect(
        _db_path(),
        timeout=60,
        check_same_thread=False,
    )
    sqlite_conn.execute("PRAGMA journal_mode=WAL;")
    sqlite_conn.execute("PRAGMA synchronous = NORMAL;")
    sqlite_conn.execute("PRAGMA foreign_keys = ON;")
    sqlite_conn.execute("PRAGMA busy_timeout = 60000;")
    return DBConnection(sqlite_conn, is_postgres=False)


def _drop_guest_rfid_uid_unique_constraint(conn: DBConnection) -> None:
    if conn._is_postgres:
        try:
            constraint = conn.execute(
                """
                SELECT con.conname
                  FROM pg_constraint con
                  JOIN pg_class rel ON rel.oid = con.conrelid
                  WHERE rel.relname = 'guest_rfid_cards'
                    AND con.contype = 'u'
                    AND pg_get_constraintdef(con.oid) ILIKE '%rfid_uid%'
                """
            ).fetchone()
            if constraint and constraint["conname"]:
                conn.execute(f'ALTER TABLE guest_rfid_cards DROP CONSTRAINT "{constraint["conname"]}"')
        except Exception:
            pass
        return

    indexes = conn.execute("PRAGMA index_list('guest_rfid_cards')").fetchall()
    for idx in indexes:
        if idx["unique"]:
            index_info = conn.execute(f"PRAGMA index_info('{idx['name']}')").fetchall()
            if len(index_info) == 1 and index_info[0]["name"] == "rfid_uid":
                try:
                    conn.execute(f"DROP INDEX IF EXISTS \"{idx['name']}\"")
                except sqlite3.OperationalError as exc:
                    # SQLite will refuse to drop an index if it is tied to a UNIQUE constraint.
                    # Rebuild the table without the unique constraint instead.
                    if "UNIQUE or PRIMARY KEY constraint" in str(exc):
                        conn.execute("PRAGMA foreign_keys = OFF")
                        conn.execute("ALTER TABLE guest_rfid_cards RENAME TO guest_rfid_cards_old")
                        conn.execute(
                            """
                            CREATE TABLE guest_rfid_cards (
                              id INTEGER PRIMARY KEY AUTOINCREMENT,
                              guest_id INTEGER NOT NULL,
                              rfid_uid TEXT NOT NULL,
                              status TEXT NOT NULL DEFAULT 'ACTIVE',
                              issue_time TEXT NOT NULL DEFAULT (datetime('now')),
                              expires_at TEXT NOT NULL,
                              expected_return_time TEXT,
                              actual_return_time TEXT,
                              checkout_admin_id INTEGER,
                              return_admin_id INTEGER,
                              checkout_notes TEXT,
                              return_notes TEXT,
                              FOREIGN KEY (guest_id) REFERENCES members(id),
                              FOREIGN KEY (checkout_admin_id) REFERENCES members(id),
                              FOREIGN KEY (return_admin_id) REFERENCES members(id)
                            )
                            """
                        )
                        conn.execute(
                            "INSERT INTO guest_rfid_cards (id, guest_id, rfid_uid, status, issue_time, expires_at, expected_return_time, actual_return_time, checkout_admin_id, return_admin_id, checkout_notes, return_notes) "
                            "SELECT id, guest_id, rfid_uid, status, issue_time, expires_at, expected_return_time, actual_return_time, checkout_admin_id, return_admin_id, checkout_notes, return_notes "
                            "FROM guest_rfid_cards_old"
                        )
                        conn.execute("DROP TABLE guest_rfid_cards_old")
                        conn.execute("PRAGMA foreign_keys = ON")
                    else:
                        raise


def _column_exists_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "duplicate column" in message or "already exists" in message


def init_db() -> None:
    database_url = _database_url()
    is_postgres = bool(database_url)

    with connect() as conn:
        if is_postgres:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                  id SERIAL PRIMARY KEY,
                  full_name TEXT NOT NULL,
                  address TEXT,
                  contact_number TEXT,
                  age INTEGER,
                  category TEXT,
                  payment_status TEXT NOT NULL DEFAULT 'unpaid',
                  paid_at TEXT,
                  status TEXT NOT NULL DEFAULT 'pending',
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                  id SERIAL PRIMARY KEY,
                  member_id INTEGER NOT NULL,
                  amount INTEGER NOT NULL,
                  payment_type TEXT NOT NULL,
                  payment_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  notes TEXT,
                  FOREIGN KEY (member_id) REFERENCES members(id)
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lockers (
                  id SERIAL PRIMARY KEY,
                  label TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'available'
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admins (
                  id SERIAL PRIMARY KEY,
                  username TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL DEFAULT 'Admin',
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  created_by TEXT
                )
                """
            )

            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_members_fingerprint_uid ON members(fingerprint_uid)"
            )

            for i in range(1, 5):
                conn.execute(
                    "INSERT INTO lockers (id, label) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (i, f"Locker {i}"),
                )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS access_logs (
                  id SERIAL PRIMARY KEY,
                  actor_type TEXT NOT NULL,
                  actor_ref TEXT,
                  action TEXT NOT NULL,
                  detail TEXT,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guest_rfid_cards (
                  id SERIAL PRIMARY KEY,
                  guest_id INTEGER NOT NULL,
                  rfid_uid TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'ACTIVE',
                  issue_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  expires_at TEXT NOT NULL,
                  expected_return_time TEXT,
                  actual_return_time TEXT,
                  checkout_admin_id INTEGER,
                  return_admin_id INTEGER,
                  checkout_notes TEXT,
                  return_notes TEXT,
                  FOREIGN KEY (guest_id) REFERENCES members(id),
                  FOREIGN KEY (checkout_admin_id) REFERENCES members(id),
                  FOREIGN KEY (return_admin_id) REFERENCES members(id)
                )
                """
            )
        else:
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

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  member_id INTEGER NOT NULL,
                  amount INTEGER NOT NULL,
                  payment_type TEXT NOT NULL,
                  payment_date TEXT NOT NULL DEFAULT (datetime('now')),
                  notes TEXT,
                  FOREIGN KEY (member_id) REFERENCES members(id)
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lockers (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  label TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'available'
                )
                """
            )

            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_members_fingerprint_uid ON members(fingerprint_uid)"
            )

            for i in range(1, 5):
                conn.execute(
                    "INSERT OR IGNORE INTO lockers (id, label) VALUES (?, ?)",
                    (i, f"Locker {i}"),
                )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admins (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL DEFAULT 'Admin',
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TEXT NOT NULL DEFAULT (datetime('now')),
                  created_by TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS access_logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  actor_type TEXT NOT NULL,
                  actor_ref TEXT,
                  action TEXT NOT NULL,
                  detail TEXT,
                  created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guest_rfid_cards (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  guest_id INTEGER NOT NULL,
                  rfid_uid TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'ACTIVE',
                  issue_time TEXT NOT NULL DEFAULT (datetime('now')),
                  expires_at TEXT NOT NULL,
                  expected_return_time TEXT,
                  actual_return_time TEXT,
                  checkout_admin_id INTEGER,
                  return_admin_id INTEGER,
                  checkout_notes TEXT,
                  return_notes TEXT,
                  locker_id INTEGER,
                  FOREIGN KEY (guest_id) REFERENCES members(id),
                  FOREIGN KEY (checkout_admin_id) REFERENCES members(id),
                  FOREIGN KEY (return_admin_id) REFERENCES members(id)
                )
                """
            )

        for col_def in [
            ("locker_id", "INTEGER"),
            ("actual_return_time", "TEXT"),
            ("return_admin_id", "INTEGER"),
            ("return_notes", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guest_rfid_cards ADD COLUMN {col_def[0]} {col_def[1]}")
            except Exception as exc:
                if not _column_exists_error(exc):
                    raise

        _drop_guest_rfid_uid_unique_constraint(conn)

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
            ("expiry_date", "TEXT"),
            ("member_type", "TEXT DEFAULT 'regular'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE members ADD COLUMN {col_def[0]} {col_def[1]}")
            except Exception as exc:
                if not _column_exists_error(exc):
                    raise

        if is_postgres:
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_guest_cards_status_expiry ON guest_rfid_cards(status, expires_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_guest_cards_rfid ON guest_rfid_cards(rfid_uid)"
                )
            except Exception:
                pass
        else:
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_guest_cards_status_expiry ON guest_rfid_cards(status, expires_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_guest_cards_rfid ON guest_rfid_cards(rfid_uid)"
                )
            except Exception:
                pass

        conn.commit()

