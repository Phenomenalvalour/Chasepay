"""All settings come from environment variables, so the same code runs on your laptop and on a server."""
import os

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
FREE_LIMIT = 3                         # unpaid invoices allowed on the Free plan
FREE_STEPS = ("due_today", "late_7")   # reminders Free users get; Pro gets all four
PRO_PRICE_LABEL = "$9/month"
