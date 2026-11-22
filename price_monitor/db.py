"""Database access: MySQL when MYSQL_DATABASE is set, otherwise a local SQLite file.

The monitor owns this schema (it creates the tables). Other apps, such as the
codename-dojo dashboard, read and write the same tables; see README.md for the
contract. All timestamps are stored as UTC.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3

# Columns added after the first version: (table, column, MySQL type, SQLite type).
# Added on start when missing, so an existing database upgrades itself.
ADDED_COLUMNS = [
    # v2: how old an ad may be (searches.max_age_days; NULL: settings.default_max_age_days)
    ("listings", "posted_at", "DATETIME NULL", "TEXT"),
    ("searches", "max_age_days", "INT NULL", "INTEGER"),
    ("settings", "default_max_age_days", "INT NOT NULL DEFAULT 30", "INTEGER NOT NULL DEFAULT 30"),
    # v3: possible-scam flags on listings (risk.py), and the searches' order (lower first)
    ("listings", "risk_level", "VARCHAR(10) NULL", "TEXT"),
    ("listings", "risk_score", "INT NULL", "INTEGER"),
    ("listings", "risk_reasons", "TEXT NULL", "TEXT"),
    ("listings", "risk_signals", "TEXT NULL", "TEXT"),
    ("searches", "sort_order", "INT NOT NULL DEFAULT 100", "INTEGER NOT NULL DEFAULT 100"),
]

MYSQL_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS searches (
        id INT AUTO_INCREMENT PRIMARY KEY,
        platform VARCHAR(50) NOT NULL,
        name VARCHAR(200) NOT NULL,
        query_url TEXT NOT NULL,
        min_price DOUBLE NULL,
        max_price DOUBLE NULL,
        include_keywords TEXT NULL,
        exclude_keywords TEXT NULL,
        options TEXT NULL,
        provider_state TEXT NULL,
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        interval_minutes INT NULL,
        notify_new TINYINT(1) NOT NULL DEFAULT 1,
        notify_price_drops TINYINT(1) NOT NULL DEFAULT 1,
        run_requested_at DATETIME NULL,
        baseline_at DATETIME NULL,
        last_run_at DATETIME NULL,
        last_status VARCHAR(20) NULL,
        last_error TEXT NULL,
        last_result_count INT NULL,
        last_match_count INT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_searches_platform (platform)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS listings (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        search_id INT NOT NULL,
        platform VARCHAR(50) NOT NULL,
        external_id VARCHAR(64) NOT NULL,
        title VARCHAR(500) NOT NULL,
        url TEXT NOT NULL,
        image_url TEXT NULL,
        price DOUBLE NOT NULL,
        currency VARCHAR(8) NOT NULL,
        location VARCHAR(200) NULL,
        first_seen DATETIME NOT NULL,
        last_seen DATETIME NOT NULL,
        UNIQUE KEY uq_listings_search_external (search_id, external_id),
        INDEX idx_listings_first_seen (first_seen),
        CONSTRAINT fk_listings_search FOREIGN KEY (search_id) REFERENCES searches(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS price_history (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        listing_id BIGINT NOT NULL,
        price DOUBLE NOT NULL,
        currency VARCHAR(8) NOT NULL,
        seen_at DATETIME NOT NULL,
        INDEX idx_price_history_listing (listing_id),
        CONSTRAINT fk_price_history_listing FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS settings (
        id TINYINT PRIMARY KEY,
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        default_interval_minutes INT NOT NULL DEFAULT 10,
        email_to VARCHAR(500) NULL,
        run_requested_at DATETIME NULL,
        last_tick_at DATETIME NULL,
        providers TEXT NULL,
        schema_version INT NOT NULL DEFAULT 1
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "INSERT IGNORE INTO settings (id, schema_version) VALUES (1, %d)" % SCHEMA_VERSION,
]

SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        name TEXT NOT NULL,
        query_url TEXT NOT NULL,
        min_price REAL, max_price REAL,
        include_keywords TEXT, exclude_keywords TEXT, options TEXT, provider_state TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        interval_minutes INTEGER,
        notify_new INTEGER NOT NULL DEFAULT 1,
        notify_price_drops INTEGER NOT NULL DEFAULT 1,
        run_requested_at TEXT, baseline_at TEXT, last_run_at TEXT,
        last_status TEXT, last_error TEXT,
        last_result_count INTEGER, last_match_count INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        external_id TEXT NOT NULL,
        title TEXT NOT NULL, url TEXT NOT NULL, image_url TEXT,
        price REAL NOT NULL, currency TEXT NOT NULL, location TEXT,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        UNIQUE (search_id, external_id)
    )""",
    """CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
        price REAL NOT NULL, currency TEXT NOT NULL, seen_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1,
        default_interval_minutes INTEGER NOT NULL DEFAULT 10,
        email_to TEXT, run_requested_at TEXT, last_tick_at TEXT, providers TEXT,
        schema_version INTEGER NOT NULL DEFAULT 1
    )""",
    "INSERT OR IGNORE INTO settings (id, schema_version) VALUES (1, %d)" % SCHEMA_VERSION,
]


def utcnow():
    """Current UTC time, naive, to the second (what DATETIME columns hold)."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def to_db_time(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def from_db_time(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.strptime(str(value)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")


def json_list(value):
    if not value:
        return []
    data = json.loads(value) if isinstance(value, str) else value
    return [str(x) for x in data] if isinstance(data, list) else []


def json_object(value):
    if not value:
        return {}
    data = json.loads(value) if isinstance(value, str) else value
    return data if isinstance(data, dict) else {}


class Db:
    """Thin wrapper: queries use '?' placeholders (translated for MySQL);
    rows support row["column"] and dict(row) in both backends."""

    def __init__(self, sqlite_path):
        database = os.getenv("MYSQL_DATABASE")
        if database:
            import pymysql
            self.kind = "mysql"
            self.conn = pymysql.connect(
                host=os.getenv("MYSQL_HOST") or "localhost",
                port=int(os.getenv("MYSQL_PORT") or 3306),
                user=os.getenv("MYSQL_USER"),
                password=os.getenv("MYSQL_PASSWORD"),
                database=database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                init_command="SET time_zone = '+00:00'",
            )
            schema = MYSQL_SCHEMA
        else:
            self.kind = "sqlite"
            self.conn = sqlite3.connect(Path(sqlite_path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            schema = SQLITE_SCHEMA
        for statement in schema:
            self.execute(statement)
        self._add_missing_columns()
        self.execute("UPDATE settings SET schema_version = ? WHERE id = 1 AND schema_version < ?",
                     (SCHEMA_VERSION, SCHEMA_VERSION))
        self.commit()

    def _has_column(self, table, column):
        if self.kind == "mysql":
            row = self.one("SELECT COUNT(*) AS n FROM information_schema.columns"
                           " WHERE table_schema = DATABASE() AND table_name = ? AND column_name = ?", (table, column))
            return row["n"] > 0
        return any(r["name"] == column for r in self.all(f"PRAGMA table_info({table})"))

    def _add_missing_columns(self):
        for table, column, mysql_type, sqlite_type in ADDED_COLUMNS:
            if not self._has_column(table, column):
                self.execute(f"ALTER TABLE {table} ADD COLUMN {column} "
                             f"{mysql_type if self.kind == 'mysql' else sqlite_type}")

    def execute(self, sql, params=()):
        if self.kind == "mysql":
            sql = sql.replace("?", "%s")
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur

    def one(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, params=()):
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()
