"""SQLite storage. One small file, easy to back up with the container."""
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT DEFAULT '',
    address TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    client_id INTEGER REFERENCES clients(id),
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('staff','owner','billing','member')),
    password_hash TEXT,
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    totp_last_step INTEGER NOT NULL DEFAULT 0,
    backup_codes TEXT NOT NULL DEFAULT '[]',
    session_version INTEGER NOT NULL DEFAULT 1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    last_login TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL CHECK (purpose IN ('invite','reset')),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    created_by INTEGER NOT NULL REFERENCES users(id),
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','waiting','resolved','closed')),
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('low','normal','high','urgent')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    internal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    number TEXT NOT NULL UNIQUE,
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','paid','void')),
    notes TEXT NOT NULL DEFAULT '',
    paid_at TEXT,
    payment_method TEXT DEFAULT '',
    payment_ref TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 1,
    unit_cents INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    client_id INTEGER REFERENCES clients(id),
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Other',
    visibility TEXT NOT NULL DEFAULT 'everyone' CHECK (visibility IN ('everyone','billing')),
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_articles (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL DEFAULT '',
    published INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    ticket_id INTEGER REFERENCES tickets(id),
    booked_by INTEGER NOT NULL REFERENCES users(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested','confirmed','cancelled','completed')),
    location TEXT NOT NULL DEFAULT '',
    onsite_contact TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    staff_notes TEXT NOT NULL DEFAULT '',
    cancel_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS time_off (
    id INTEGER PRIMARY KEY,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ticket_ratings (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL UNIQUE REFERENCES tickets(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 3),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    message_id INTEGER REFERENCES ticket_messages(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    uploaded_by INTEGER NOT NULL REFERENCES users(id),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    storage TEXT NOT NULL DEFAULT 'local' CHECK (storage IN ('local','s3')),
    storage_key TEXT NOT NULL,
    internal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monthly_reports (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    month TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    data_json TEXT,
    sent_at TEXT,
    sent_by INTEGER REFERENCES users(id),
    UNIQUE (client_id, month)
);

CREATE INDEX IF NOT EXISTS idx_attachments_ticket ON attachments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_visits_time ON visits(status, start_utc);
CREATE INDEX IF NOT EXISTS idx_tickets_client ON tickets(client_id, status);
CREATE INDEX IF NOT EXISTS idx_messages_ticket ON ticket_messages(ticket_id);
CREATE INDEX IF NOT EXISTS idx_invoices_client ON invoices(client_id, status);
CREATE INDEX IF NOT EXISTS idx_users_client ON users(client_id);
"""


# Columns added after the first release. New and existing databases both get them on startup.
MIGRATIONS = [
    ("clients", "snipeit_company_id", "INTEGER"),
    ("tickets", "asset_id", "INTEGER"),
    ("tickets", "asset_tag", "TEXT NOT NULL DEFAULT ''"),
    ("tickets", "asset_label", "TEXT NOT NULL DEFAULT ''"),
    ("tickets", "resolved_at", "TEXT"),
    ("documents", "storage", "TEXT NOT NULL DEFAULT 'local'"),
    ("users", "staff_admin", "INTEGER NOT NULL DEFAULT 0"),
]

# Keep tickets.resolved_at accurate however the status changes (staff, client, or a reply reopening it).
TRIGGERS = """
DROP TRIGGER IF EXISTS ticket_resolved_at;
CREATE TRIGGER IF NOT EXISTS ticket_resolved_at_v2 AFTER UPDATE OF status ON tickets
WHEN NEW.status IN ('resolved','closed') AND OLD.status NOT IN ('resolved','closed') AND NEW.resolved_at IS NULL
BEGIN
    UPDATE tickets SET resolved_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS ticket_reopened AFTER UPDATE OF status ON tickets
WHEN NEW.status NOT IN ('resolved','closed') AND OLD.status IN ('resolved','closed')
BEGIN
    UPDATE tickets SET resolved_at = NULL WHERE id = NEW.id;
END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                if (table, column) == ("users", "staff_admin"):  # everyone on staff before roles existed is an Admin
                    conn.execute("UPDATE users SET staff_admin = 1 WHERE role = 'staff'")
                if (table, column) == ("tickets", "resolved_at"):  # best guess for requests resolved before this existed
                    conn.execute("UPDATE tickets SET resolved_at = updated_at WHERE status IN ('resolved','closed')")
        conn.executescript(TRIGGERS)


def all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def run(sql: str, params: tuple = ()) -> int:
    """Execute a write and return the new row id (or affected row count for updates)."""
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def audit(user_id: int | None, action: str, detail: str = "", ip: str = "") -> None:
    run(
        "INSERT INTO audit_log (user_id, action, detail, ip, created_at) VALUES (?,?,?,?,?)",
        (user_id, action, detail, ip, now()),
    )


def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    run("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def invoice_total(invoice_id: int) -> int:
    row = one(
        "SELECT COALESCE(SUM(CAST(ROUND(quantity * unit_cents) AS INTEGER)), 0) AS t "
        "FROM invoice_items WHERE invoice_id = ?",
        (invoice_id,),
    )
    return int(row["t"]) if row else 0


def next_invoice_number() -> str:
    row = one("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM invoices")
    n = row["n"] if row else 1
    return f"{config.INVOICE_PREFIX}-{n:04d}"
