"""Pro subscriptions through Paystack (works for Nigerian businesses; Stripe does not).
Flow: user clicks Upgrade -> we ask Paystack for a checkout link -> user pays on Paystack's page
-> Paystack calls our /paystack/webhook -> we verify the signature and switch the user to Pro."""
import hashlib
import hmac
import json
import os
import urllib.request


def configured():
    return bool(os.environ.get("PAYSTACK_SECRET_KEY") and os.environ.get("PAYSTACK_PLAN_CODE"))


def start_checkout(email, user_id, callback_url):
    body = json.dumps({
        "email": email,
        "plan": os.environ["PAYSTACK_PLAN_CODE"],
        "callback_url": callback_url,
        "metadata": {"product": "chasepay_pro", "user_id": user_id},
    }).encode()
    req = urllib.request.Request(
        "https://api.paystack.co/transaction/initialize", data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ['PAYSTACK_SECRET_KEY']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)["data"]["authorization_url"]


def valid_signature(raw_body: bytes, header: str) -> bool:
    secret = os.environ.get("PAYSTACK_SECRET_KEY", "").encode()
    if not secret or not header:
        return False
    expected = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, header)


def handle_event(conn, event):
    """Returns the plan that was applied ('pro' / 'free') or None if the event was ignored."""
    kind, data = event.get("event"), event.get("data", {})
    email = (data.get("customer") or {}).get("email", "").lower()
    meta = data.get("metadata") or {}
    if kind == "charge.success" and isinstance(meta, dict) and meta.get("product") == "chasepay_pro":
        uid = meta.get("user_id")
        row = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone() if uid else None
        row = row or conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            conn.execute("UPDATE users SET plan='pro' WHERE id=?", (row["id"],))
            return "pro"
    if kind == "subscription.disable" and email:
        conn.execute("UPDATE users SET plan='free' WHERE email=?", (email,))
        return "free"
    return None
