"""The heart of ChasePay: the follow-up sequence."""
import logging
from datetime import date, timedelta

import clock
import config
import db
import mailer
import pdfgen
from money import fmt

log = logging.getLogger("chasepay")

# (days relative to the due date, kind)
SEQUENCE = [(-3, "upcoming"), (0, "due_today"), (3, "late_3"), (7, "late_7")]
LABELS = {
    "upcoming": "3 days before due",
    "due_today": "On the due date",
    "late_3": "3 days late",
    "late_7": "7 days late",
}


def schedule(conn, invoice_id, due, plan="pro", today=None):
    """Create reminder rows for one invoice. Steps already in the past are marked 'skipped'.
    Free users only get the steps listed in config.FREE_STEPS."""
    today = today or clock.today()
    due = date.fromisoformat(due) if isinstance(due, str) else due
    conn.execute("DELETE FROM reminders WHERE invoice_id=? AND status IN ('scheduled','skipped')", (invoice_id,))
    for offset, kind in SEQUENCE:
        if plan != "pro" and kind not in config.FREE_STEPS:
            continue
        send_on = due + timedelta(days=offset)
        status = "scheduled" if send_on >= today else "skipped"
        conn.execute(
            "INSERT INTO reminders (invoice_id, kind, send_on, status) VALUES (?,?,?,?)",
            (invoice_id, kind, send_on.isoformat(), status),
        )


def _link(inv):
    return f"\n\nView this invoice online: {config.BASE_URL}/i/{inv['public_token']}" if inv["public_token"] else ""


def build_message(kind, inv, customer, user, balance=None):
    balance = inv["amount_cents"] if balance is None else balance
    amount = fmt(balance, inv["currency"])
    num = inv["number"]
    who = user["business_name"] or user["email"]
    hi = f"Hi {customer['name']},\n\n"
    sign = f"\n\nThank you,\n{who}"
    if kind == "upcoming":
        subject = f"Invoice #{num} is due in 3 days"
        body = f"{hi}Your invoice #{num} for {amount} ({inv['description']}) is due in 3 days, on {inv['due_date']}.{sign}"
    elif kind == "due_today":
        subject = f"Invoice #{num} is due today"
        body = f"{hi}Your invoice of {amount} (#{num}) is due today.{sign}"
    elif kind == "late_3":
        subject = f"Checking in on invoice #{num}"
        body = f"{hi}Just checking in regarding the outstanding {amount} invoice (#{num}), which was due on {inv['due_date']}. If you've already paid, thank you and please ignore this.{sign}"
    else:
        subject = f"Reminder: invoice #{num} is unpaid"
        body = f"{hi}This is a reminder that invoice #{num} ({amount}) remains unpaid. It was due on {inv['due_date']}. Please let me know when to expect payment.{sign}"
    if user["payment_details"]:
        body += f"\n\nHow to pay:\n{user['payment_details']}"
    return subject, body + _link(inv)


def send_invoice(conn, inv_id):
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (inv["customer_id"],)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id=?", (inv["user_id"],)).fetchone()
    paid = db.paid_total(conn, inv_id)
    amount = fmt(inv["amount_cents"] - paid, inv["currency"])
    who = user["business_name"] or user["email"]
    body = (
        f"Hi {customer['name']},\n\nPlease find attached invoice #{inv['number']} for {amount} "
        f"({inv['description']}), due on {inv['due_date']}.\n\nThank you,\n{who}"
    )
    if user["payment_details"]:
        body += f"\n\nHow to pay:\n{user['payment_details']}"
    body += _link(inv)
    mode = mailer.send(
        conn, user, customer["email"], f"Invoice #{inv['number']} from {who}", body,
        pdf_bytes=pdfgen.render(inv, customer, user, paid), pdf_name=f"invoice-{inv['number']}.pdf",
    )
    conn.execute("UPDATE invoices SET sent_at=? WHERE id=?", (clock.now_iso(), inv_id))
    return mode


def run_due(conn, today=None, user_id=None):
    """Send every scheduled reminder that is due. Safe to run as often as you like:
    each reminder is 'claimed' before sending, so it can never go out twice."""
    today = (today or clock.today()).isoformat()
    sql = """SELECT r.id rid, r.kind, i.id iid FROM reminders r
             JOIN invoices i ON i.id = r.invoice_id
             WHERE r.status='scheduled' AND r.send_on <= ?
               AND i.status='unpaid' AND i.reminders_on=1 AND i.sent_at IS NOT NULL"""
    args = [today]
    if user_id:
        sql += " AND i.user_id=?"
        args.append(user_id)
    sent = 0
    for row in conn.execute(sql, args).fetchall():
        claimed = conn.execute("UPDATE reminders SET status='sending' WHERE id=? AND status='scheduled'", (row["rid"],)).rowcount
        conn.commit()
        if not claimed:
            continue
        inv = conn.execute("SELECT * FROM invoices WHERE id=?", (row["iid"],)).fetchone()
        customer = conn.execute("SELECT * FROM customers WHERE id=?", (inv["customer_id"],)).fetchone()
        user = conn.execute("SELECT * FROM users WHERE id=?", (inv["user_id"],)).fetchone()
        balance = inv["amount_cents"] - db.paid_total(conn, inv["id"])
        subject, body = build_message(row["kind"], inv, customer, user, balance)
        try:
            mailer.send(conn, user, customer["email"], subject, body)
        except Exception:
            log.exception("Reminder %s failed; will retry on the next run", row["rid"])
            conn.execute("UPDATE reminders SET status='scheduled' WHERE id=?", (row["rid"],))
            conn.commit()
            continue
        conn.execute("UPDATE reminders SET status='sent', sent_at=? WHERE id=?", (clock.now_iso(), row["rid"]))
        conn.commit()
        sent += 1
    return sent
