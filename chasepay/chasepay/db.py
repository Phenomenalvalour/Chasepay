"""Database layer: SQLite, one file, zero setup."""
import os
import secrets
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    pw TEXT NOT NULL,
    business_name TEXT NOT NULL DEFAULT '',
    payment_details TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    number INTEGER NOT NULL,
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unpaid',
    sent_at TEXT,
    paid_at TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    kind TEXT NOT NULL,
    send_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL,
    paid_on TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    every TEXT NOT NULL,
    due_days INTEGER NOT NULL DEFAULT 14,
    next_issue TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def db_path():
    return os.environ.get("CHASEPAY_DB", "chasepay.db")


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first version. Existing databases get upgraded automatically.
ADDED_COLUMNS = {
    "users": [("brand_color", "TEXT NOT NULL DEFAULT '#1f4fd8'")],
    "invoices": [("public_token", "TEXT"), ("reminders_on", "INTEGER NOT NULL DEFAULT 1"), ("recurring_id", "INTEGER")],
}


def init_db():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        for table, cols in ADDED_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for r in conn.execute("SELECT id FROM invoices WHERE public_token IS NULL").fetchall():
            conn.execute("UPDATE invoices SET public_token=? WHERE id=?", (secrets.token_urlsafe(16), r["id"]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invoice_token ON invoices(public_token)")


def paid_total(conn, invoice_id):
    return conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
