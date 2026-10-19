"""Email sending. With no SMTP settings it runs in dry-run mode:
messages are saved to the outbox table so you can see exactly what would go out."""
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("chasepay")


def send(conn, user, to_addr, subject, body, pdf_bytes=None, pdf_name="invoice.pdf", reply_to=True):
    mode = "dry-run"
    host = os.environ.get("SMTP_HOST")
    if host:
        msg = EmailMessage()
        sender = os.environ.get("SMTP_FROM", user["email"])
        who = user["business_name"] or user["email"]
        msg["From"] = formataddr((f"{who} via ChasePay", sender)) if reply_to else formataddr(("ChasePay", sender))
        if reply_to:
            msg["Reply-To"] = user["email"]
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        if pdf_bytes:
            msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_name)
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
        mode = "sent"
    else:
        log.warning("DRY-RUN EMAIL to %s | %s\n%s", to_addr, subject, body)
    conn.execute(
        "INSERT INTO outbox (user_id, to_addr, subject, body, mode) VALUES (?,?,?,?,?)",
        (user["id"], to_addr, subject, body, mode),
    )
    return mode
