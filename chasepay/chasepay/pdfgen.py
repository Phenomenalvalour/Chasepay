"""Invoice PDF."""
import re
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from money import fmt

INK = (0.08, 0.125, 0.17)
MUTED = (0.42, 0.47, 0.52)


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def render(inv, customer, user, paid_cents=0):
    accent = _rgb(user["brand_color"]) if user["plan"] == "pro" and re.fullmatch(r"#[0-9a-fA-F]{6}", user["brand_color"] or "") else (0.12, 0.31, 0.85)
    cur = inv["currency"]
    balance = max(inv["amount_cents"] - paid_cents, 0)
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    left, right = 56, w - 56
    y = h - 72

    c.setFillColorRGB(*INK)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(left, y, user["business_name"] or user["email"])
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(*MUTED)
    c.drawString(left, y - 16, user["email"])
    c.setFillColorRGB(*accent)
    c.setFont("Helvetica-Bold", 26)
    c.drawRightString(right, y, "INVOICE")
    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica", 10)
    c.drawRightString(right, y - 16, f"No. {inv['number']}")

    y -= 70
    c.setFont("Helvetica", 9)
    c.drawString(left, y, "Billed to")
    c.drawString(300, y, "Issued")
    c.drawString(420, y, "Due")
    c.setFillColorRGB(*INK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y - 16, customer["name"])
    c.drawString(300, y - 16, inv["issue_date"])
    c.drawString(420, y - 16, inv["due_date"])
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(*MUTED)
    c.drawString(left, y - 31, customer["email"])

    y -= 80
    c.setStrokeColorRGB(*INK)
    c.line(left, y, right, y)
    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica", 9)
    c.drawString(left, y - 16, "Description")
    c.drawRightString(right, y - 16, "Amount")
    c.setStrokeColorRGB(0.85, 0.87, 0.89)
    c.line(left, y - 24, right, y - 24)
    c.setFillColorRGB(*INK)
    c.setFont("Helvetica", 11)
    c.drawString(left, y - 44, inv["description"][:80])
    c.drawRightString(right, y - 44, fmt(inv["amount_cents"], cur))
    row = y - 64
    if paid_cents:
        c.setFillColorRGB(*MUTED)
        c.setFont("Helvetica", 10)
        c.drawString(left, row - 4, "Paid so far")
        c.drawRightString(right, row - 4, "-" + fmt(paid_cents, cur))
        row -= 22
    c.setStrokeColorRGB(*INK)
    c.line(left, row, right, row)
    c.setFillColorRGB(*INK)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(left, row - 24, "Total due" if not paid_cents else "Balance due")
    c.drawRightString(right, row - 24, fmt(balance, cur))

    if user["payment_details"]:
        y = row - 80
        c.setFillColorRGB(*MUTED)
        c.setFont("Helvetica", 9)
        c.drawString(left, y, "How to pay")
        c.setFillColorRGB(*INK)
        c.setFont("Helvetica", 10)
        for i, line in enumerate(user["payment_details"].splitlines()[:8]):
            c.drawString(left, y - 16 - i * 14, line[:90])
    c.showPage()
    c.save()
    return buf.getvalue()
