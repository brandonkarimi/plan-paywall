"""SQLite storage: one row per transaction plus an append-only activity log."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from desktop import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    client_name TEXT NOT NULL DEFAULT '',
    client_email TEXT NOT NULL,
    from_email TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'usd',
    status TEXT NOT NULL DEFAULT 'draft',      -- draft | sent | paid
    original_name TEXT NOT NULL DEFAULT '',
    final_path TEXT NOT NULL DEFAULT '',
    preview_path TEXT NOT NULL DEFAULT '',
    share_link TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    pages INTEGER,
    size_bytes INTEGER,
    sheets TEXT NOT NULL DEFAULT '[]',
    sent_at TEXT,
    paid_at TEXT,
    delivered_at TEXT,
    amount_paid_cents INTEGER,
    delivery_error TEXT NOT NULL DEFAULT '',
    ref_code TEXT NOT NULL DEFAULT '',
    methods TEXT NOT NULL DEFAULT '[]',        -- how the client may pay: zelle / venmo
    paid_method TEXT NOT NULL DEFAULT '',      -- Zelle, Venmo, Check, Cash, Other
    invoice_path TEXT NOT NULL DEFAULT '',     -- optional invoice PDF sent with the preview
    email_subject TEXT NOT NULL DEFAULT '',    -- the preview email's wording as written for this send
    email_body TEXT NOT NULL DEFAULT ''        -- ('' = the defaults from Settings)
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_tx ON activity(tx_id, id);
-- Payment emails found in the engineer's inbox (each processed once).
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    found_at TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',          -- Zelle | Venmo
    amount_cents INTEGER NOT NULL DEFAULT 0,
    payer TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,                     -- released | review | confirmed | dismissed | ignored (not a payment)
    tx_id INTEGER,
    note TEXT NOT NULL DEFAULT ''             -- why it needs the engineer's confirmation
);
-- How far each payment-alert inbox has been read (IMAP UIDs), so only new mail is downloaded.
CREATE TABLE IF NOT EXISTS inbox_state (
    email TEXT PRIMARY KEY,
    uidvalidity TEXT NOT NULL DEFAULT '',
    last_uid INTEGER NOT NULL DEFAULT 0
);
"""

# Columns added after the first release; older databases get them on startup.
UPGRADE_COLUMNS = {
    "ref_code": "TEXT NOT NULL DEFAULT ''",
    "methods": "TEXT NOT NULL DEFAULT '[]'",
    "paid_method": "TEXT NOT NULL DEFAULT ''",
    "invoice_path": "TEXT NOT NULL DEFAULT ''",
    "email_subject": "TEXT NOT NULL DEFAULT ''",
    "email_body": "TEXT NOT NULL DEFAULT ''",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    with _lock:
        conn = sqlite3.connect(config.db_path(), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init():
    with connect() as c:
        c.executescript(SCHEMA)
        existing = {row["name"] for row in c.execute("PRAGMA table_info(transactions)")}
        for name, ddl in UPGRADE_COLUMNS.items():
            if name not in existing:
                c.execute(f"ALTER TABLE transactions ADD COLUMN {name} {ddl}")
        if c.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Test versions up to 1.3.0-beta.7 wrongly skipped every Wells Fargo Zelle email (its fine print mentions
            # "payment requests"). Forget those skips and re-read the inbox so the payments get matched after all.
            c.execute("DELETE FROM payments WHERE status = 'ignored'")
            c.execute("DELETE FROM inbox_state")
            c.execute("PRAGMA user_version = 1")


def _row(r):
    if r is None:
        return None
    d = dict(r)
    d["sheets"] = json.loads(d.get("sheets") or "[]")
    d["methods"] = json.loads(d.get("methods") or "[]")
    return d


def _encode(fields):
    out = dict(fields)
    for key in ("sheets", "methods"):
        if key in out and not isinstance(out[key], str):
            out[key] = json.dumps(out[key])
    return out


def create_tx(**fields) -> int:
    fields = _encode({"created_at": now(), **fields})
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with connect() as c:
        return c.execute(f"INSERT INTO transactions ({cols}) VALUES ({marks})", list(fields.values())).lastrowid


def update_tx(tx_id, **fields):
    if not fields:
        return
    fields = _encode(fields)
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE transactions SET {sets} WHERE id = ?", [*fields.values(), tx_id])


def get_tx(tx_id):
    with connect() as c:
        return _row(c.execute("SELECT * FROM transactions WHERE id = ?", [tx_id]).fetchone())


def list_txs(statuses=None):
    sql, args = "SELECT * FROM transactions WHERE status != 'draft'", []
    if statuses:
        sql = f"SELECT * FROM transactions WHERE status IN ({', '.join('?' for _ in statuses)})"
        args = list(statuses)
    with connect() as c:
        return [_row(r) for r in c.execute(sql + " ORDER BY id DESC", args).fetchall()]


def delete_tx(tx_id):
    with connect() as c:
        c.execute("DELETE FROM activity WHERE tx_id = ?", [tx_id])
        c.execute("DELETE FROM transactions WHERE id = ?", [tx_id])


def delete_all() -> int:
    """Remove every transaction and its activity. Ids keep counting up (AUTOINCREMENT), so reference codes
    like PLN-7 are never reused for a different project. Returns how many transactions were removed."""
    with connect() as c:
        count = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        c.execute("DELETE FROM activity")
        c.execute("DELETE FROM transactions")
    return count


def log(tx_id, text):
    with connect() as c:
        c.execute("INSERT INTO activity (tx_id, ts, text) VALUES (?, ?, ?)", [tx_id, now(), text[:500]])


def activity(tx_id):
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT ts, text FROM activity WHERE tx_id = ? ORDER BY id DESC", [tx_id]).fetchall()]


# ------------------------------------------------------------------ payments found in the inbox
def payment_seen(message_id) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM payments WHERE message_id = ?", [message_id]).fetchone() is not None


def add_payment(**fields) -> int:
    fields = {"found_at": now(), **fields}
    cols = ", ".join(fields)
    with connect() as c:
        return c.execute(f"INSERT INTO payments ({cols}) VALUES ({', '.join('?' for _ in fields)})",
                         list(fields.values())).lastrowid


def update_payment(payment_id, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE payments SET {sets} WHERE id = ?", [*fields.values(), payment_id])


def get_payment(payment_id):
    with connect() as c:
        r = c.execute("SELECT * FROM payments WHERE id = ?", [payment_id]).fetchone()
        return dict(r) if r else None


def list_payments(status):
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM payments WHERE status = ? ORDER BY id DESC", [status])]


def last_payment_id() -> int:
    with connect() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM payments").fetchone()[0]


def payments_after(payment_id):
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM payments WHERE id > ? ORDER BY id", [payment_id])]


def inbox_state(email):
    with connect() as c:
        r = c.execute("SELECT * FROM inbox_state WHERE email = ?", [email]).fetchone()
        return dict(r) if r else None


def set_inbox_state(email, uidvalidity, last_uid):
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO inbox_state (email, uidvalidity, last_uid) VALUES (?, ?, ?)",
                  [email, uidvalidity, last_uid])
