"""ChasePay web app. Each function below with @app.get / @app.post is one page or action."""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import billing
import clock
import config
import db
import jobs
import mailer
import pdfgen
import reminders
import service
from money import SYMBOLS, fmt, parse_amount

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chasepay")
BASE = Path(__file__).parent


# ---------- startup ----------
def _secret():
    if os.environ.get("CHASEPAY_SECRET"):
        return os.environ["CHASEPAY_SECRET"]
    f = BASE / ".secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()


def _job_loop():
    while True:
        try:
            with db.connect() as conn:
                jobs.run_all(conn)
        except Exception:
            log.exception("Background job failed")
        time.sleep(3600)


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    if not os.environ.get("CHASEPAY_NO_THREAD"):
        threading.Thread(target=_job_loop, daemon=True).start()
    yield


# ---------- security ----------
def csrf_token(request):
    return request.session.setdefault("csrf", secrets.token_urlsafe(24))


async def verify_csrf(request: Request):
    """Every form carries a secret token tied to the visitor's session. This stops other websites
    from tricking a logged-in user's browser into submitting forms to us."""
    if request.method != "POST" or request.url.path == "/paystack/webhook":
        return
    form = await request.form()
    sent, expected = str(form.get("csrf", "")), request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        raise HTTPException(403, "Your session expired. Go back, refresh the page and try again.")


app = FastAPI(lifespan=lifespan, dependencies=[Depends(verify_csrf)], docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=_secret(), same_site="lax", max_age=60 * 60 * 24 * 30,
                   https_only=os.environ.get("CHASEPAY_HTTPS") == "1")


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["money"] = fmt

_fails = {}


def locked_out(key):
    now = time.time()
    _fails[key] = [t for t in _fails.get(key, []) if now - t < 900]
    return len(_fails[key]) >= 5


# ---------- helpers ----------
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200_000).hex()}"


def check_pw(pw, stored):
    return hmac.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)


def current_user(request, conn):
    uid = request.session.get("uid")
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None


def set_flash(request, msg):
    request.session["flash"] = msg


def page(request, name, user=None, status_code=200, **ctx):
    return templates.TemplateResponse(request, name, {
        "user": user, "today": clock.today(), "csrf": csrf_token(request),
        "flash": request.session.pop("flash", None), "config": config, **ctx}, status_code=status_code)


def go(url):
    return RedirectResponse(url, status_code=303)


def decorate(inv):
    d = dict(inv)
    delta = (datetime.fromisoformat(inv["due_date"]).date() - clock.today()).days
    d["balance_cents"] = inv["amount_cents"] - inv["paid_cents"]
    d["overdue"] = inv["status"] == "unpaid" and delta < 0
    d["days_late"] = -delta if delta < 0 else 0
    d["days_until"] = delta if delta >= 0 else 0
    return d


INVOICE_SQL = """SELECT i.*, c.name customer_name, c.email customer_email,
                 COALESCE((SELECT SUM(p.amount_cents) FROM payments p WHERE p.invoice_id = i.id), 0) paid_cents
                 FROM invoices i JOIN customers c ON c.id = i.customer_id"""


def chase_steps(conn, invoice_ids):
    out = {i: [] for i in invoice_ids}
    if invoice_ids:
        q = ",".join("?" * len(invoice_ids))
        for r in conn.execute(f"SELECT * FROM reminders WHERE invoice_id IN ({q}) ORDER BY send_on", invoice_ids):
            out[r["invoice_id"]].append(dict(r, label=reminders.LABELS[r["kind"]]))
    return out


def _get_invoice(conn, u, inv_id):
    return conn.execute(INVOICE_SQL + " WHERE i.id=? AND i.user_id=?", (inv_id, u["id"])).fetchone()


def _customer(conn, inv):
    return conn.execute("SELECT * FROM customers WHERE id=?", (inv["customer_id"],)).fetchone()


# ---------- public pages ----------
@app.get("/")
def home(request: Request):
    if request.session.get("uid"):
        return go("/invoices")
    return page(request, "landing.html")


@app.get("/health")
def health():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/privacy")
def privacy(request: Request):
    return page(request, "legal.html", doc="privacy")


@app.get("/terms")
def terms(request: Request):
    return page(request, "legal.html", doc="terms")


@app.get("/i/{token}")
def public_invoice(request: Request, token: str):
    with db.connect() as conn:
        inv = conn.execute(INVOICE_SQL + " WHERE i.public_token=?", (token,)).fetchone()
        if not inv:
            return page(request, "notfound.html", status_code=404)
        owner = conn.execute("SELECT * FROM users WHERE id=?", (inv["user_id"],)).fetchone()
    resp = page(request, "public_invoice.html", inv=decorate(inv), owner=owner, token=token)
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.get("/i/{token}/pdf")
def public_pdf(token: str):
    with db.connect() as conn:
        inv = conn.execute(INVOICE_SQL + " WHERE i.public_token=?", (token,)).fetchone()
        if not inv:
            return Response(status_code=404)
        owner = conn.execute("SELECT * FROM users WHERE id=?", (inv["user_id"],)).fetchone()
        data = pdfgen.render(inv, _customer(conn, inv), owner, inv["paid_cents"])
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="invoice-{inv["number"]}.pdf"'})


# ---------- accounts ----------
@app.get("/signup")
def signup_form(request: Request):
    return page(request, "auth.html", mode="signup")


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...), business_name: str = Form("")):
    email = email.strip().lower()
    if "@" not in email or len(password) < 8:
        return page(request, "auth.html", status_code=400, mode="signup", email=email, business_name=business_name,
                    error="Use a valid email and a password of at least 8 characters.")
    with db.connect() as conn:
        try:
            cur = conn.execute("INSERT INTO users (email, pw, business_name) VALUES (?,?,?)",
                               (email, hash_pw(password), business_name.strip()))
        except sqlite3.IntegrityError:
            return page(request, "auth.html", status_code=400, mode="signup", email=email, business_name=business_name,
                        error="An account with that email already exists. Log in instead.")
        request.session["uid"] = cur.lastrowid
    set_flash(request, "Welcome. Start by adding your payment details in Settings, then add a customer.")
    return go("/invoices")


@app.get("/login")
def login_form(request: Request):
    return page(request, "auth.html", mode="login")


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    key = f"{request.client.host if request.client else '?'}|{email}"
    if locked_out(key):
        return page(request, "auth.html", status_code=429, mode="login", email=email,
                    error="Too many attempts. Wait 15 minutes, or reset your password.")
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not check_pw(password, u["pw"]):
        _fails.setdefault(key, []).append(time.time())
        return page(request, "auth.html", status_code=400, mode="login", email=email,
                    error="That email and password don't match.")
    _fails.pop(key, None)
    request.session.clear()
    request.session["uid"] = u["id"]
    return go("/invoices")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return go("/")


@app.get("/forgot")
def forgot_form(request: Request):
    return page(request, "forgot.html")


@app.post("/forgot")
def forgot(request: Request, email: str = Form(...)):
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        if u:
            raw = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?,?,?)",
                         (hashlib.sha256(raw.encode()).hexdigest(), u["id"], (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)).isoformat()))
            mailer.send(conn, u, u["email"], "Reset your ChasePay password",
                        f"Use this link within one hour to choose a new password:\n\n{config.BASE_URL}/reset/{raw}\n\n"
                        "If you didn't ask for this, ignore this email.", reply_to=False)
    return page(request, "forgot.html", sent=True)


def _valid_reset(conn, raw):
    return conn.execute("SELECT * FROM password_resets WHERE token_hash=? AND used=0 AND expires_at > ?",
                        (hashlib.sha256(raw.encode()).hexdigest(), datetime.now(timezone.utc).replace(tzinfo=None).isoformat())).fetchone()


@app.get("/reset/{token}")
def reset_form(request: Request, token: str):
    with db.connect() as conn:
        ok = _valid_reset(conn, token)
    return page(request, "reset.html", token=token, valid=bool(ok))


@app.post("/reset/{token}")
def reset(request: Request, token: str, password: str = Form(...)):
    with db.connect() as conn:
        r = _valid_reset(conn, token)
        if not r:
            return page(request, "reset.html", status_code=400, token=token, valid=False)
        if len(password) < 8:
            return page(request, "reset.html", status_code=400, token=token, valid=True,
                        error="Use a password of at least 8 characters.")
        conn.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(password), r["user_id"]))
        conn.execute("UPDATE password_resets SET used=1 WHERE user_id=?", (r["user_id"],))
    set_flash(request, "Password changed. Log in with your new password.")
    return go("/login")


# ---------- settings, billing, account ----------
@app.get("/settings")
def settings_form(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
    return go("/login") if not u else page(request, "settings.html", u)


@app.post("/settings")
def settings_save(request: Request, business_name: str = Form(""), payment_details: str = Form(""),
                  brand_color: str = Form("#1f4fd8")):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        color = brand_color if re.fullmatch(r"#[0-9a-fA-F]{6}", brand_color) and u["plan"] == "pro" else u["brand_color"]
        conn.execute("UPDATE users SET business_name=?, payment_details=?, brand_color=? WHERE id=?",
                     (business_name.strip(), payment_details.strip(), color, u["id"]))
    set_flash(request, "Settings saved.")
    return go("/settings")


@app.get("/account/export")
def account_export(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        data = {"account": {k: u[k] for k in ("email", "business_name", "payment_details", "plan", "created_at")}}
        for t in ("customers", "invoices", "recurring"):
            data[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t} WHERE user_id=?", (u["id"],))]
        data["payments"] = [dict(r) for r in conn.execute(
            "SELECT p.* FROM payments p JOIN invoices i ON i.id=p.invoice_id WHERE i.user_id=?", (u["id"],))]
    return Response(json.dumps(data, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="chasepay-export.json"'})


@app.post("/account/delete")
def account_delete(request: Request, password: str = Form(...)):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        if not check_pw(password, u["pw"]):
            set_flash(request, "Wrong password. Your account was not deleted.")
            return go("/settings")
        uid = u["id"]
        conn.execute("DELETE FROM payments WHERE invoice_id IN (SELECT id FROM invoices WHERE user_id=?)", (uid,))
        conn.execute("DELETE FROM reminders WHERE invoice_id IN (SELECT id FROM invoices WHERE user_id=?)", (uid,))
        for t in ("invoices", "recurring", "customers", "outbox", "password_resets"):
            conn.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    request.session.clear()
    return go("/")


@app.get("/billing")
def billing_page(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
    return go("/login") if not u else page(request, "billing.html", u, configured=billing.configured())


@app.post("/billing/upgrade")
def billing_upgrade(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
    if not u:
        return go("/login")
    if not billing.configured():
        set_flash(request, "Online payment isn't switched on yet. Email us to upgrade.")
        return go("/billing")
    try:
        return go(billing.start_checkout(u["email"], u["id"], f"{config.BASE_URL}/billing"))
    except Exception:
        log.exception("Paystack checkout failed")
        set_flash(request, "We couldn't reach the payment page. Try again in a minute.")
        return go("/billing")


@app.post("/paystack/webhook")
async def paystack_webhook(request: Request):
    raw = await request.body()
    if not billing.valid_signature(raw, request.headers.get("x-paystack-signature", "")):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    with db.connect() as conn:
        result = billing.handle_event(conn, json.loads(raw))
    log.info("Paystack webhook handled: %s", result)
    return {"ok": True}


# ---------- customers ----------
@app.get("/customers")
def customers(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        rows = conn.execute("""SELECT c.*, (SELECT COUNT(*) FROM invoices i WHERE i.customer_id=c.id) n
                               FROM customers c WHERE user_id=? ORDER BY name""", (u["id"],)).fetchall()
    return page(request, "customers.html", u, customers=rows)


@app.post("/customers")
def customer_add(request: Request, name: str = Form(...), email: str = Form(...)):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        if name.strip() and "@" in email:
            conn.execute("INSERT INTO customers (user_id, name, email) VALUES (?,?,?)", (u["id"], name.strip(), email.strip()))
        else:
            set_flash(request, "Enter a name and a valid email.")
    return go("/customers")


@app.post("/customers/{cid}/edit")
def customer_edit(request: Request, cid: int, name: str = Form(...), email: str = Form(...)):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        if name.strip() and "@" in email:
            conn.execute("UPDATE customers SET name=?, email=? WHERE id=? AND user_id=?", (name.strip(), email.strip(), cid, u["id"]))
            set_flash(request, "Customer updated.")
    return go("/customers")


@app.post("/customers/{cid}/delete")
def customer_delete(request: Request, cid: int):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        used = conn.execute("SELECT COUNT(*) FROM invoices WHERE customer_id=?", (cid,)).fetchone()[0]
        if used:
            set_flash(request, "This customer has invoices, so it can't be deleted.")
        else:
            conn.execute("DELETE FROM customers WHERE id=? AND user_id=?", (cid, u["id"]))
    return go("/customers")


# ---------- invoices ----------
@app.get("/invoices")
def invoices(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        rows = [decorate(r) for r in conn.execute(
            INVOICE_SQL + " WHERE i.user_id=? ORDER BY (i.status!='unpaid'), i.due_date", (u["id"],))]
        steps = chase_steps(conn, [r["id"] for r in rows])
    for r in rows:
        r["steps"] = steps[r["id"]]
    open_rows = [r for r in rows if r["status"] == "unpaid"]
    totals = {}
    for r in open_rows:
        totals[r["currency"]] = totals.get(r["currency"], 0) + r["balance_cents"]
    return page(request, "invoices.html", u, invoices=rows, open_count=len(open_rows),
                overdue_count=sum(r["overdue"] for r in open_rows), totals=totals)


@app.get("/invoices/new")
def invoice_new_form(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        custs = conn.execute("SELECT * FROM customers WHERE user_id=? ORDER BY name", (u["id"],)).fetchall()
    return page(request, "invoice_new.html", u, customers=custs, currencies=list(SYMBOLS))


@app.post("/invoices/new")
def invoice_create(request: Request, customer_id: int = Form(...), description: str = Form(...),
                   amount: str = Form(...), currency: str = Form("USD"), due_date: str = Form(...),
                   send_now: str = Form("")):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        custs = conn.execute("SELECT * FROM customers WHERE user_id=? ORDER BY name", (u["id"],)).fetchall()

        def fail(msg):
            return page(request, "invoice_new.html", u, status_code=400, customers=custs, currencies=list(SYMBOLS),
                        error=msg, form=dict(customer_id=customer_id, description=description, amount=amount,
                                             currency=currency, due_date=due_date))
        try:
            cents = parse_amount(amount)
        except ValueError as e:
            return fail(str(e))
        try:
            due = datetime.fromisoformat(due_date).date()
        except ValueError:
            return fail("Choose a valid due date.")
        if currency not in SYMBOLS:
            return fail("Choose a supported currency.")
        if not conn.execute("SELECT 1 FROM customers WHERE id=? AND user_id=?", (customer_id, u["id"])).fetchone():
            return fail("Choose a customer.")
        try:
            inv_id = service.create_invoice(conn, u, customer_id, description, cents, currency, clock.today(), due)
        except service.LimitError as e:
            return fail(str(e))
        conn.commit()
        if send_now:
            try:
                reminders.send_invoice(conn, inv_id)
            except Exception:
                log.exception("Could not email invoice %s", inv_id)
                set_flash(request, "Invoice created, but the email could not be sent. Check your email settings, then press Email invoice.")
        return go(f"/invoices/{inv_id}")


@app.get("/invoices/{inv_id}")
def invoice_detail(request: Request, inv_id: int):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        inv = _get_invoice(conn, u, inv_id)
        if not inv:
            return page(request, "notfound.html", u, status_code=404)
        steps = chase_steps(conn, [inv_id])[inv_id]
        pays = conn.execute("SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (inv_id,)).fetchall()
    return page(request, "invoice_detail.html", u, inv=decorate(inv), steps=steps, payments=pays)


@app.get("/invoices/{inv_id}/pdf")
def invoice_pdf(request: Request, inv_id: int):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        inv = _get_invoice(conn, u, inv_id)
        if not inv:
            return Response(status_code=404)
        data = pdfgen.render(inv, _customer(conn, inv), u, inv["paid_cents"])
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="invoice-{inv["number"]}.pdf"'})


@app.post("/invoices/{inv_id}/send")
def invoice_send(request: Request, inv_id: int):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        if _get_invoice(conn, u, inv_id):
            try:
                reminders.send_invoice(conn, inv_id)
                set_flash(request, "Invoice emailed.")
            except Exception:
                log.exception("Could not email invoice %s", inv_id)
                set_flash(request, "The email could not be sent. Check your email settings.")
    return go(f"/invoices/{inv_id}")


@app.post("/invoices/{inv_id}/payments")
def invoice_payment(request: Request, inv_id: int, amount: str = Form(...), note: str = Form("")):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        inv = _get_invoice(conn, u, inv_id)
        if inv and inv["status"] == "unpaid":
            try:
                cents = parse_amount(amount)
                if cents > inv["amount_cents"] - inv["paid_cents"]:
                    raise ValueError("That is more than the balance still owed.")
                service.record_payment(conn, inv_id, cents, note)
                set_flash(request, "Payment recorded.")
            except ValueError as e:
                set_flash(request, str(e))
    return go(f"/invoices/{inv_id}")


@app.post("/invoices/{inv_id}/paid")
def invoice_paid(request: Request, inv_id: int):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        inv = _get_invoice(conn, u, inv_id)
        if inv and inv["status"] == "unpaid":
            service.record_payment(conn, inv_id, inv["amount_cents"] - inv["paid_cents"], "Marked as paid")
    return go(f"/invoices/{inv_id}")


@app.post("/invoices/{inv_id}/void")
def invoice_void(request: Request, inv_id: int):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        if _get_invoice(conn, u, inv_id):
            service.void_invoice(conn, inv_id)
            set_flash(request, "Invoice cancelled. No more reminders will be sent.")
    return go(f"/invoices/{inv_id}")


@app.post("/invoices/{inv_id}/reminders")
def invoice_toggle_reminders(request: Request, inv_id: int):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        if _get_invoice(conn, u, inv_id):
            conn.execute("UPDATE invoices SET reminders_on = 1 - reminders_on WHERE id=?", (inv_id,))
    return go(f"/invoices/{inv_id}")


@app.post("/reminders/run")
def reminders_run(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        n = reminders.run_due(conn, user_id=u["id"])
    set_flash(request, f"{n} reminder{'' if n == 1 else 's'} sent. See Sent mail.")
    return go("/invoices")


# ---------- recurring (Pro) ----------
@app.get("/recurring")
def recurring_page(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        custs = conn.execute("SELECT * FROM customers WHERE user_id=? ORDER BY name", (u["id"],)).fetchall()
        rows = conn.execute("""SELECT r.*, c.name customer_name FROM recurring r JOIN customers c ON c.id=r.customer_id
                               WHERE r.user_id=? ORDER BY r.active DESC, r.next_issue""", (u["id"],)).fetchall()
    return page(request, "recurring.html", u, customers=custs, rows=rows, currencies=list(SYMBOLS))


@app.post("/recurring")
def recurring_add(request: Request, customer_id: int = Form(...), description: str = Form(...), amount: str = Form(...),
                  currency: str = Form("USD"), every: str = Form("monthly"), first_date: str = Form(...), due_days: int = Form(14)):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        if u["plan"] != "pro":
            return go("/billing")
        try:
            cents = parse_amount(amount)
            first = datetime.fromisoformat(first_date).date()
            assert every in ("weekly", "monthly") and currency in SYMBOLS and 0 <= due_days <= 120
            assert conn.execute("SELECT 1 FROM customers WHERE id=? AND user_id=?", (customer_id, u["id"])).fetchone()
        except (ValueError, AssertionError):
            set_flash(request, "Check the customer, amount, dates and schedule, then try again.")
            return go("/recurring")
        conn.execute("""INSERT INTO recurring (user_id, customer_id, description, amount_cents, currency, every, due_days, next_issue)
                        VALUES (?,?,?,?,?,?,?,?)""", (u["id"], customer_id, description.strip(), cents, currency, every, due_days, first.isoformat()))
        set_flash(request, "Recurring invoice saved.")
    return go("/recurring")


@app.post("/recurring/{rid}/stop")
def recurring_stop(request: Request, rid: int):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        conn.execute("UPDATE recurring SET active=0 WHERE id=? AND user_id=?", (rid, u["id"]))
    return go("/recurring")


@app.get("/outbox")
def outbox(request: Request):
    with db.connect() as conn:
        u = current_user(request, conn)
        if not u:
            return go("/login")
        rows = conn.execute("SELECT * FROM outbox WHERE user_id=? ORDER BY id DESC LIMIT 100", (u["id"],)).fetchall()
    return page(request, "outbox.html", u, messages=rows)
