"""The background work: run by a timer inside the web app, or by `python manage.py run-jobs`."""
import recurring
import reminders


def run_all(conn):
    made = recurring.generate_due(conn)
    sent = reminders.run_due(conn)
    return made, sent
