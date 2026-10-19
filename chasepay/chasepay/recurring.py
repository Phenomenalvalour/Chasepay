"""Pro feature: invoices that create and send themselves every week or month."""
import logging
from datetime import date, timedelta

import clock
import reminders
import service

log = logging.getLogger("chasepay")


def next_date(d, every):
    return d + timedelta(days=7) if every == "weekly" else service.add_months(d, 1)


def generate_due(conn, today=None):
    today = today or clock.today()
    made = 0
    for r in conn.execute("SELECT * FROM recurring WHERE active=1 AND next_issue <= ?", (today.isoformat(),)).fetchall():
        user = conn.execute("SELECT * FROM users WHERE id=?", (r["user_id"],)).fetchone()
        if user["plan"] != "pro":
            continue
        inv_id = service.create_invoice(conn, user, r["customer_id"], r["description"], r["amount_cents"], r["currency"],
                                        today, today + timedelta(days=r["due_days"]), recurring_id=r["id"])
        nxt = date.fromisoformat(r["next_issue"])
        while nxt <= today:
            nxt = next_date(nxt, r["every"])
        conn.execute("UPDATE recurring SET next_issue=? WHERE id=?", (nxt.isoformat(), r["id"]))
        conn.commit()  # save first, so a failed email can never cause a duplicate invoice
        try:
            reminders.send_invoice(conn, inv_id)
            conn.commit()
        except Exception:
            log.exception("Recurring invoice %s created but not emailed", inv_id)
        made += 1
    return made
