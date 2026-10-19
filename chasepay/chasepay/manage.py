"""Owner commands. Run from the project folder:
    python manage.py run-jobs                  send due reminders + create due recurring invoices now
    python manage.py set-plan you@mail.com pro give someone Pro (or 'free' to remove it)
    python manage.py users                     list accounts
    python manage.py backup                    copy the database to backups/
"""
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import db
import jobs


def main(argv):
    db.init_db()
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "run-jobs":
        with db.connect() as conn:
            made, sent = jobs.run_all(conn)
        print(f"Recurring invoices created: {made}. Reminders sent: {sent}.")
    elif cmd == "set-plan" and len(argv) == 4 and argv[3] in ("pro", "free"):
        with db.connect() as conn:
            n = conn.execute("UPDATE users SET plan=? WHERE email=?", (argv[3], argv[2].lower())).rowcount
        print("Updated." if n else "No account with that email.")
    elif cmd == "users":
        with db.connect() as conn:
            for u in conn.execute("SELECT email, plan, created_at FROM users ORDER BY id"):
                print(u["email"], u["plan"], u["created_at"])
    elif cmd == "backup":
        Path("backups").mkdir(exist_ok=True)
        dest = Path("backups") / f"chasepay-{datetime.now():%Y%m%d-%H%M}.db"
        src = sqlite3.connect(db.db_path())
        out = sqlite3.connect(dest)
        src.backup(out)
        out.close(); src.close()
        print(f"Backup saved to {dest}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
