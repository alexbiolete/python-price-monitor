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

SCHEMA_VERSION = 19

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
    # v4: how a listing's price adds up (eBay: price + shipping + customs)
    ("listings", "price_detail", "VARCHAR(300) NULL", "TEXT"),
    # v5: listings pinned in the dashboard, in their order (lower first; NULL: not pinned)
    ("listings", "pin_order", "INT NULL", "INTEGER"),
    # v6: ads re-checked on their own page when a search stops showing them; inactive_at: when the
    # ad was found sold or removed (NULL: active), checked_at: the last such check
    ("listings", "inactive_at", "DATETIME NULL", "TEXT"),
    ("listings", "checked_at", "DATETIME NULL", "TEXT"),
    # v7: worth-knowing facts that aren't scam signs and add no points (risk.py), e.g. a seller with
    # no reviews yet: a JSON list of sentences, NULL when none
    ("listings", "risk_notes", "TEXT NULL", "TEXT"),
    # v8: ads outside a search's budget are stored too (for the price estimate); price_ron is the price
    # in lei the budget compares, so readers filter on it (min_budget <= price_ron <= max_budget; v15
    # renamed them from min_price/max_price)
    ("listings", "price_ron", "DOUBLE NULL", "REAL"),
    # v10: live exchange rates (rates.py; JSON {"date", "source", "rates": {currency: lei}}) and when
    # they were last fetched; the dashboard's table shows prices in lei unless prices_in_ron is 0
    ("settings", "exchange_rates", "TEXT NULL", "TEXT"),
    ("settings", "exchange_rates_at", "DATETIME NULL", "TEXT"),
    ("settings", "prices_in_ron", "TINYINT(1) NOT NULL DEFAULT 1", "INTEGER NOT NULL DEFAULT 1"),
    # v11: listings excluded in the dashboard (left out of price estimates, listed last); the monitor
    # keeps them like pinned ones and never clears it, but sets it on a new ad with the same photo as an
    # excluded one on its platform (a repost under a new id) (v12 renamed it from ignored_at)
    ("listings", "excluded_at", "DATETIME NULL", "TEXT"),
    # v13: the highest risk level emailed ("none", "low", "medium", "high": all); above it, listings
    # are stored and shown, not emailed
    ("settings", "email_max_risk", "VARCHAR(10) NOT NULL DEFAULT 'high'", "TEXT NOT NULL DEFAULT 'high'"),
    # v14: one email for all products a check found something for (1), or one per product (0)
    ("settings", "merge_emails", "TINYINT(1) NOT NULL DEFAULT 1", "INTEGER NOT NULL DEFAULT 1"),
    # v15: whether the dashboard lists a search's listings (1) or only gathers them (0: still stored, for
    # the price estimate; emails are notify_new/notify_price_drops). And its prices, min_price/max_price:
    # what a real one of the product can cost, broader than the budget (min_budget/max_budget, which these
    # two names held before v15). Ads outside them (games, cases, parts) aren't stored unless
    # store_outside_prices is 1; NULL: no bound
    ("searches", "show_listings", "TINYINT(1) NOT NULL DEFAULT 1", "INTEGER NOT NULL DEFAULT 1"),
    ("searches", "min_price", "DOUBLE NULL", "REAL"),
    ("searches", "max_price", "DOUBLE NULL", "REAL"),
    ("searches", "store_outside_prices", "TINYINT(1) NOT NULL DEFAULT 0", "INTEGER NOT NULL DEFAULT 0"),
    # v16: why a price changed: 'rate' when only the platform's currency conversion moved it (a seller
    # pricing in another currency, shown converted: Vinted's foreign sellers); NULL: the seller changed it
    ("price_history", "reason", "VARCHAR(10) NULL", "TEXT"),
    # v17: when it was marked suspicious in the dashboard (shown as a likely scam whatever its signs; its price
    # drops aren't emailed); NULL: not marked. The monitor never changes it
    ("listings", "marked_suspicious_at", "DATETIME NULL", "TEXT"),
    # v18: the good signs a listing shows (risk.good_signs: an old seller account, a well-rated seller,
    # buyer protection, an offer to meet), a JSON list of sentences; NULL: none
    ("listings", "risk_good", "TEXT NULL", "TEXT"),
    # v19: the good signs' level ("low": some, "medium": good, "high": very good; NULL: none) and points
    ("listings", "good_level", "VARCHAR(10) NULL", "TEXT"),
    ("listings", "good_score", "INT NULL", "INTEGER"),
]

# Columns renamed since: (table, old name, new name), renamed in place on start, before the above
RENAMED_COLUMNS = [
    ("listings", "ignored_at", "excluded_at"),  # v12
    # v15: the range to buy in was min_price/max_price; those names are now the broader price limits
    # (added above, after the rename)
    ("searches", "min_price", "min_budget"),
    ("searches", "max_price", "max_budget"),
]

MYSQL_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS searches (
        id INT AUTO_INCREMENT PRIMARY KEY,
        platform VARCHAR(50) NOT NULL,
        name VARCHAR(200) NOT NULL,
        query_url TEXT NOT NULL,
        min_budget DOUBLE NULL,
        max_budget DOUBLE NULL,
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
    # v9: when an ad was found sold or removed ("inactive"), or back up after that ("active")
    """CREATE TABLE IF NOT EXISTS status_history (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        listing_id BIGINT NOT NULL,
        status VARCHAR(10) NOT NULL,
        seen_at DATETIME NOT NULL,
        INDEX idx_status_history_listing (listing_id),
        CONSTRAINT fk_status_history_listing FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE CASCADE
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
        min_budget REAL, max_budget REAL,
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
    """CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
        status TEXT NOT NULL, seen_at TEXT NOT NULL
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
            self.path = Path(sqlite_path)
            self.conn = sqlite3.connect(self.path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            schema = SQLITE_SCHEMA
        for statement in schema:
            self.execute(statement)
        self._rename_columns()
        self._add_missing_columns()
        version = (self.one("SELECT schema_version FROM settings WHERE id = 1") or {"schema_version": SCHEMA_VERSION})
        if version["schema_version"] < 16:
            self._mark_rate_changes()
        if version["schema_version"] < 9:
            # Ads already found inactive before status_history: their one known change
            self.execute("INSERT INTO status_history (listing_id, status, seen_at)"
                         " SELECT id, 'inactive', inactive_at FROM listings WHERE inactive_at IS NOT NULL")
        self.execute("UPDATE settings SET schema_version = ? WHERE id = 1 AND schema_version < ?",
                     (SCHEMA_VERSION, SCHEMA_VERSION))
        self.commit()

    def _has_column(self, table, column):
        if self.kind == "mysql":
            row = self.one("SELECT COUNT(*) AS n FROM information_schema.columns"
                           " WHERE table_schema = DATABASE() AND table_name = ? AND column_name = ?", (table, column))
            return row["n"] > 0
        return any(r["name"] == column for r in self.all(f"PRAGMA table_info({table})"))

    def _mark_rate_changes(self):
        """v16: price changes recorded before 'reason' existed that were only a currency conversion moving
        (rate_change: small, on a platform that converts), marked 'rate'"""
        from .rates import RATE_PLATFORMS, rate_change
        marks = ", ".join("?" for _ in RATE_PLATFORMS)
        rows = self.all(f"SELECT h.id, h.listing_id, h.price, h.currency FROM price_history h JOIN listings l"
                        f" ON l.id = h.listing_id WHERE l.platform IN ({marks}) ORDER BY h.listing_id, h.seen_at, h.id",
                        tuple(RATE_PLATFORMS))
        before = {}
        for r in rows:
            prev = before.get(r["listing_id"])
            if prev and prev["currency"] == r["currency"] and rate_change(prev["price"], r["price"]):
                self.execute("UPDATE price_history SET reason = 'rate' WHERE id = ?", (r["id"],))
            before[r["listing_id"]] = r

    def _rename_columns(self):
        for table, old, new in RENAMED_COLUMNS:
            if self._has_column(table, old) and not self._has_column(table, new):
                self.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")

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

    def try_lock(self):
        """One check at a time on this database (cron and a manual run can overlap): True if this
        connection got the lock, False if another check holds it."""
        if self.kind == "mysql":
            return bool(self.one("SELECT GET_LOCK(CONCAT(DATABASE(), '.tick'), 0) AS ok")["ok"])
        import fcntl
        self._lock_file = open(f"{self.path}.lock", "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._lock_file.close()
            return False

    def unlock(self):
        if self.kind == "mysql":
            self.execute("SELECT RELEASE_LOCK(CONCAT(DATABASE(), '.tick'))")
        elif getattr(self, "_lock_file", None):
            self._lock_file.close()

    def close(self):
        self.conn.close()
