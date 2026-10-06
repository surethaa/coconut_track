# Coconut Mill Ledger (Flask)

An installable web app for a coconut mill: **Dashboard**, five data sets (**Coconut, Copra, Shell,
Labor & transport, Coir**), **Reports** with PROFIT / LOSS, date-filtered **CSV downloads**, and an
**automatic monthly statement email**. People sign in with their **email and password**.

Profit or loss = (copra + shell + coir) − amount invested in coconuts − (labor + transport).

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
python app.py               # open http://localhost:5000
```

The first time you open it you are asked to **create the owner account** (email + password). That
account is the admin. Data lives in one SQLite file (`DATABASE_PATH`, default `mill.db`) — **back it up**.

## Accounts and roles

| | Admin | Staff |
|---|---|---|
| View dashboard, data, reports | yes | yes |
| Add and edit records, download CSVs | yes | yes |
| Delete records | yes | no |
| Add / remove users, reset passwords, change roles | yes | no |
| Set statement email, send a statement now | yes | no |
| Change own password | yes | yes |

- Admins add people under **Settings → Users** (email + a temporary password to hand over privately).
- **Forgot password** emails a one-hour, single-use reset link. This needs `SMTP_*` and `BASE_URL`. Without
  email set up, an admin resets the password from **Settings → Users**.
- Every record stores who entered it (the "Entered by" column in CSV downloads).
- Five wrong logins lock that email + IP for 5 minutes.

## Install it as an app

The site is a Progressive Web App, so people can put it on their phone or desktop like a normal app:

- **Android (Chrome):** menu → *Install app*, or the *Install app* button in the sidebar.
- **iPhone (Safari):** Share → *Add to Home Screen*.
- **Desktop (Chrome/Edge):** the install icon in the address bar.

Installing needs the site to be served over **HTTPS** (localhost is exempt for testing). The app still needs
an internet connection to load and save records; offline it shows a friendly "you're offline" screen rather
than stale numbers.

## Monthly email

1. Fill in the `SMTP_*` values in `.env` (for Gmail, create an *App password*).
2. **Settings → Monthly statement email** (defaults to the owner's email).
3. On **day 1 of every month at 06:00** (`SCHEDULER_TZ`, default Asia/Kolkata) the previous month's
   statement is emailed (text + a summary CSV). It retries daily on days 2–5 if it failed or the server was
   off, and never sends a month twice.
4. To test: **Monthly statement → Send this statement now**.

The scheduler runs inside the app process, so the app must be running on a server and only **one worker**
may run (the Dockerfile does this). Prefer system cron? Set `ENABLE_SCHEDULER=0` and add:

```
0 6 1 * * cd /path/to/app && .venv/bin/flask --app app send-statement
```

## Deploy

```bash
docker build -t coconut-mill .
docker run -d -p 8000:8000 --env-file .env -v mill-data:/data --name mill coconut-mill
```

Put it behind HTTPS (Caddy, nginx, or your host's TLS), set `BASE_URL` to that address, `COOKIE_SECURE=1`,
and `TRUST_PROXY=1` if a proxy sits in front. Set a fixed `SECRET_KEY` — if it changes, everyone is logged
out and pending reset links stop working.

## Security notes

- Passwords are stored hashed (never in plain text). Forms use CSRF tokens; a strict Content-Security-Policy
  is on; CSV exports neutralise spreadsheet formulas.
- Password-reset emails use `BASE_URL` (not the request's Host header) so links cannot be hijacked.
- Login throttling is in memory (fine for one worker; it resets on restart).
- No two-factor login and no full audit log of edits/deletes — worth adding if several people share the ledger.

## Tests

```bash
pip install pytest aiosmtpd
pytest
```

## Files

`app.py` (accounts, routes, data sets, dashboard, summary, email, scheduler) · `templates/` · `static/`
(CSS, JS, icons, `manifest.webmanifest`, `sw.js`) · `tests/` · `Dockerfile`
