import hashlib
import hmac
import json
import os
import re
import tempfile
from datetime import timedelta

import pytest

os.environ["CHASEPAY_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CHASEPAY_NO_THREAD"] = "1"
os.environ["PAYSTACK_SECRET_KEY"] = "sk_test_x"
os.environ["PAYSTACK_PLAN_CODE"] = "PLN_x"

from fastapi.testclient import TestClient  # noqa: E402

import billing  # noqa: E402
import clock  # noqa: E402
import db  # noqa: E402
import recurring  # noqa: E402
import reminders  # noqa: E402
from app import app  # noqa: E402


class Client:
    """Wraps TestClient and attaches the CSRF token the way a browser form would."""
    def __init__(self):
        self.c = TestClient(app)
        self.c.__enter__()

    def token(self):
        return re.search(r'name="csrf" value="([^"]+)"', self.c.get("/login").text).group(1)

    def post(self, url, data=None, **kw):
        return self.c.post(url, data={**(data or {}), "csrf": self.token()}, **kw)

    def get(self, url, **kw):
        return self.c.get(url, **kw)


me_client = None


@pytest.fixture(scope="module")
def me():
    global me_client
    c = me_client = Client()
    assert c.post("/signup", {"email": "a@b.com", "password": "password1", "business_name": "Ada Designs"}).status_code == 200
    c.post("/settings", {"business_name": "Ada Designs", "payment_details": "Bank X\n0123456789"})
    c.post("/customers", {"name": "Tunde", "email": "tunde@client.com"})
    return c


def iso(days):
    return (clock.today() + timedelta(days=days)).isoformat()


def new_inv(c, desc="Job", amount="100", days=10, send=True, cur="USD"):
    data = {"customer_id": 1, "description": desc, "amount": amount, "currency": cur, "due_date": iso(days)}
    if send:
        data["send_now"] = "1"
    return c.post("/invoices/new", data)


def lid():
    with db.connect() as conn:
        return conn.execute("SELECT MAX(id) FROM invoices").fetchone()[0]


def pay_all():
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM invoices WHERE status='unpaid' AND id>1")]
    for i in ids:
        me_client.post(f"/invoices/{i}/paid")


def set_plan(plan):
    with db.connect() as conn:
        conn.execute("UPDATE users SET plan=? WHERE email='a@b.com'", (plan,))


def test_free_plan_gets_basic_reminders_only(me):
    r = new_inv(me, "Website design", "750")
    assert "Invoice #1001" in r.text
    assert me.get("/invoices/1/pdf").content.startswith(b"%PDF")
    with db.connect() as conn:
        kinds = [x["kind"] for x in conn.execute("SELECT kind FROM reminders WHERE invoice_id=1 ORDER BY send_on")]
        assert kinds == ["due_today", "late_7"]
        assert reminders.run_due(conn) == 0
        assert reminders.run_due(conn, today=clock.today() + timedelta(days=10)) == 1
        assert reminders.run_due(conn, today=clock.today() + timedelta(days=10)) == 0   # never twice
        assert reminders.run_due(conn, today=clock.today() + timedelta(days=17)) == 1
        bodies = [m["body"] for m in conn.execute("SELECT body FROM outbox")]
        assert any("due today" in b for b in bodies) and any("Bank X" in b for b in bodies)
        assert any("/i/" in b for b in bodies)


def test_pro_plan_gets_full_sequence_and_unlimited(me):
    set_plan("pro")
    for i in range(5):
        new_inv(me, f"Pro job {i}")
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM reminders WHERE invoice_id=2").fetchone()[0]
        assert n == 4
    pay_all()


def test_free_limit_blocks_fourth(me):
    set_plan("free")
    for i in range(3):
        new_inv(me, f"Free {i}", send=False)
    r = new_inv(me, "One too many", send=False)
    assert r.status_code == 400 and "Free plan" in r.text
    pay_all()


def test_part_payments_and_paid_cancels_reminders(me):
    new_inv(me, "Logo", "1,000", cur="NGN")
    i = lid()
    me.post(f"/invoices/{i}/payments", {"amount": "400", "note": "transfer"})
    page = me.get(f"/invoices/{i}").text
    assert "₦600.00" in page
    me.post(f"/invoices/{i}/payments", {"amount": "9999"})   # more than balance: refused
    assert "₦600.00" in me.get(f"/invoices/{i}").text
    me.post(f"/invoices/{i}/payments", {"amount": "600"})
    assert "Paid" in me.get(f"/invoices/{i}").text
    with db.connect() as conn:
        assert reminders.run_due(conn, today=clock.today() + timedelta(days=60)) == 0
        assert {r["status"] for r in conn.execute("SELECT status FROM reminders WHERE invoice_id=?", (i,))} == {"cancelled"}


def test_void_and_pause(me):
    new_inv(me, "Void me"); a = lid()
    new_inv(me, "Pause me"); b = lid()
    me.post(f"/invoices/{a}/void")
    me.post(f"/invoices/{b}/reminders")
    with db.connect() as conn:
        assert reminders.run_due(conn, today=clock.today() + timedelta(days=60)) == 0
    me.post(f"/invoices/{b}/void")


def test_recurring_pro_only(me):
    set_plan("free")
    me.post("/recurring", {"customer_id": 1, "description": "Care plan", "amount": "50", "currency": "USD",
                           "every": "monthly", "first_date": iso(0), "due_days": 14}, follow_redirects=False)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recurring").fetchone()[0] == 0
    set_plan("pro")
    me.post("/recurring", {"customer_id": 1, "description": "Care plan", "amount": "50", "currency": "USD",
                           "every": "monthly", "first_date": iso(0), "due_days": 14})
    with db.connect() as conn:
        assert recurring.generate_due(conn) == 1
        assert recurring.generate_due(conn) == 0        # not twice
        inv = conn.execute("SELECT * FROM invoices WHERE recurring_id=1").fetchone()
        assert inv["sent_at"] is not None
        nxt = conn.execute("SELECT next_issue FROM recurring").fetchone()[0]
        assert nxt > clock.today().isoformat()
    set_plan("free")


def test_public_page(me):
    with db.connect() as conn:
        tok = conn.execute("SELECT public_token FROM invoices WHERE id=1").fetchone()[0]
    anon = TestClient(app)
    r = anon.get(f"/i/{tok}")
    assert r.status_code == 200 and "Bank X" in r.text and "noindex" in r.text
    assert anon.get(f"/i/{tok}/pdf").content.startswith(b"%PDF")
    assert anon.get("/i/doesnotexist").status_code == 404


def test_csrf_and_auth_and_isolation(me):
    assert me.c.post("/customers", data={"name": "X", "email": "x@x.com"}).status_code == 403   # no token
    anon = TestClient(app)
    assert anon.get("/invoices", follow_redirects=False).status_code == 303
    other = Client()
    other.post("/signup", {"email": "z@z.com", "password": "password2"})
    assert other.get("/invoices/1").status_code == 404
    assert other.get("/invoices/1/pdf").status_code == 404


def test_password_reset(me):
    c = Client()
    c.post("/forgot", {"email": "a@b.com"})
    with db.connect() as conn:
        body = conn.execute("SELECT body FROM outbox WHERE subject LIKE 'Reset%' ORDER BY id DESC").fetchone()[0]
    token = re.search(r"/reset/(\S+)", body).group(1)
    assert c.post(f"/reset/{token}", {"password": "newpassword9"}, follow_redirects=False).status_code == 303
    assert c.post(f"/reset/{token}", {"password": "again12345"}).status_code == 400      # single use
    assert c.post("/login", {"email": "a@b.com", "password": "newpassword9"}, follow_redirects=False).status_code == 303


def test_login_lockout():
    c = Client()
    for _ in range(5):
        c.post("/login", {"email": "ghost@x.com", "password": "wrong"})
    assert c.post("/login", {"email": "ghost@x.com", "password": "wrong"}).status_code == 429


def test_paystack_webhook(me):
    body = json.dumps({"event": "charge.success", "data": {"customer": {"email": "a@b.com"},
                       "metadata": {"product": "chasepay_pro", "user_id": 1}}}).encode()
    assert me.c.post("/paystack/webhook", content=body, headers={"x-paystack-signature": "bad"}).status_code == 401
    sig = hmac.new(b"sk_test_x", body, hashlib.sha512).hexdigest()
    assert me.c.post("/paystack/webhook", content=body, headers={"x-paystack-signature": sig}).status_code == 200
    with db.connect() as conn:
        assert conn.execute("SELECT plan FROM users WHERE id=1").fetchone()[0] == "pro"
        off = {"event": "subscription.disable", "data": {"customer": {"email": "a@b.com"}}}
        assert billing.handle_event(conn, off) == "free"


def test_export_and_delete(me):
    r = me.get("/account/export")
    assert r.status_code == 200 and "Tunde" in r.text
    other = Client()
    other.post("/signup", {"email": "del@x.com", "password": "password3"})
    other.post("/account/delete", {"password": "password3"})
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE email='del@x.com'").fetchone() is None
    assert me.get("/health").json() == {"ok": True}
