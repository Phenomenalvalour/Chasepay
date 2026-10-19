"""Business rules shared by the web pages and the background jobs."""
import calendar
import secrets
from datetime import date

import clock
import config
import db
import reminders


class LimitError(Exception):
    pass


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def create_invoice(conn, user, customer_id, description, cents, currency, issue, due, recurring_id=None):
    if user["plan"] != "pro":
        active = conn.execute("SELECT COUNT(*) FROM invoices WHERE user_id=? AND status='unpaid'", (user["id"],)).fetchone()[0]
        if active >= config.FREE_LIMIT:
            raise LimitError(f"The Free plan allows {config.FREE_LIMIT} unpaid invoices. Mark one as paid or upgrade to Pro.")
    number = (conn.execute("SELECT MAX(number) FROM invoices WHERE user_id=?", (user["id"],)).fetchone()[0] or 1000) + 1
    cur = conn.execute(
        """INSERT INTO invoices (user_id, customer_id, number, description, amount_cents, currency,
                                 issue_date, due_date, public_token, recurring_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (user["id"], customer_id, number, description.strip(), cents, currency,
         issue.isoformat(), due.isoformat(), secrets.token_urlsafe(16), recurring_id))
    reminders.schedule(conn, cur.lastrowid, due, user["plan"], today=issue)
    return cur.lastrowid


def record_payment(conn, inv_id, cents, note="", on=None):
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    conn.execute("INSERT INTO payments (invoice_id, amount_cents, paid_on, note) VALUES (?,?,?,?)",
                 (inv_id, cents, (on or clock.today()).isoformat(), note.strip()))
    if db.paid_total(conn, inv_id) >= inv["amount_cents"]:
        conn.execute("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (clock.now_iso(), inv_id))
        conn.execute("UPDATE reminders SET status='cancelled' WHERE invoice_id=? AND status='scheduled'", (inv_id,))


def void_invoice(conn, inv_id):
    conn.execute("UPDATE invoices SET status='void' WHERE id=? AND status='unpaid'", (inv_id,))
    conn.execute("UPDATE reminders SET status='cancelled' WHERE invoice_id=? AND status='scheduled'", (inv_id,))
