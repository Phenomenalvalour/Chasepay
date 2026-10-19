# ChasePay

Create an invoice. Send it. Chase the payment automatically.

## Run it on your computer

    pip install -r requirements.txt
    uvicorn app:app --reload

Open http://127.0.0.1:8000. With no email settings, every email is saved under "Sent mail" (dry run).

## Test it

    python -m pytest

## Owner commands

    python manage.py run-jobs                    send due reminders + create due recurring invoices now
    python manage.py set-plan you@mail.com pro   give someone Pro (or free)
    python manage.py users                       list accounts
    python manage.py backup                      copy the database into backups/

## Deploy

See the Founder's Guide. Short version: push this folder to GitHub, create a Render web service from
render.yaml (or any Docker host), fill in the variables listed in .env.example, attach a persistent disk
for the database, point your domain at it.

## Files

- app.py: every web page and button
- reminders.py: the chase sequence (read this first)
- service.py: invoice rules (limits, payments, voiding)
- recurring.py, jobs.py: scheduled work
- billing.py: Paystack subscriptions for Pro
- mailer.py, pdfgen.py, money.py, clock.py, config.py, db.py: supporting pieces
- templates/ and static/: the screens and styling
- manage.py: owner commands
- test_chasepay.py: automated checks
- Dockerfile, render.yaml, .env.example: deployment
