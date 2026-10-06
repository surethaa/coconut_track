"""Coconut Mill Ledger — Flask + SQLite.

Five data sets (coconut, copra, shell, labor & transport, coir), a profit/loss
summary, date-filtered CSV downloads, and an automatic monthly statement email.
"""
import calendar
import csv
import io
import math
import os
import re
import secrets
import smtplib
import sqlite3
import time
import datetime as dt
from email.message import EmailMessage
from functools import wraps

import click
from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, g, redirect,
                   render_template, request, send_from_directory, session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash as _gen_password_hash

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = dt.timedelta(days=30)

DB_PATH = os.environ.get("DATABASE_PATH", "mill.db")
CUR = os.environ.get("CURRENCY_SYMBOL", "₹")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "COOKIE_SECURE", "1" if BASE_URL.startswith("https") else "0") == "1"
if os.environ.get("TRUST_PROXY") == "1":  # behind nginx/Caddy: use the real client IP + scheme
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Some Python builds (notably the one macOS ships at /usr/bin/python3, and some
# python.org 3.9 installers) link against an OpenSSL without scrypt support, which is
# Werkzeug's current default hashing method. Pinning to pbkdf2:sha256 avoids that
# entirely -- it works on every platform and is still a strong, salted hash.
def generate_password_hash(password):
    return _gen_password_hash(password, method="pbkdf2:sha256")


DUMMY_HASH = generate_password_hash("not-a-real-password")

SCHEMA = """
CREATE TABLE IF NOT EXISTS coconut (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    farmer TEXT NOT NULL, date TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'count',
    qty REAL NOT NULL DEFAULT 0, unit_price REAL NOT NULL DEFAULT 0,
    processed REAL NOT NULL DEFAULT 0,
    created_by TEXT, created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')));
CREATE TABLE IF NOT EXISTS copra (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
    kg REAL NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0,
    created_by TEXT, created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')));
CREATE TABLE IF NOT EXISTS shell (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
    tons REAL NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0,
    created_by TEXT, created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')));
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
    labor REAL NOT NULL DEFAULT 0, transport REAL NOT NULL DEFAULT 0,
    created_by TEXT, created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')));
CREATE TABLE IF NOT EXISTS coir (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
    loads REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0,
    created_by TEXT, created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'staff', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("coconut", "copra", "shell", "expenses", "coir"):  # upgrade older databases
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if "created_by" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN created_by TEXT")


def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
def money(n):
    return f"{'-' if n < 0 else ''}{CUR}{abs(n):,.2f}"


def num(n, d=0):
    return f"{n:,.{d}f}"


def money0(n):
    return f"{'-' if n < 0 else ''}{CUR} {abs(n):,.0f}"


def dmy(text):
    try:
        return dt.date.fromisoformat(text).strftime("%d-%m-%Y")
    except (TypeError, ValueError):
        return text or ""


app.jinja_env.filters["money"] = money
app.jinja_env.filters["money0"] = money0
app.jinja_env.filters["dmy"] = dmy


# --------------------------------------------------------------------------
# The five data sets
# --------------------------------------------------------------------------
def F(name, label, type_="number", **kw):
    return dict(name=name, label=label, type=type_, **kw)


DATASETS = {
    "coconut": dict(
        title="Coconut", table="coconut", total_label="Total amount (quantity × unit price)",
        fields=[F("farmer", "Farmer name", "text", required=True),
                F("date", "Date", "date", required=True),
                F("unit", "Measured in", "select", options=["count", "kg"]),
                F("qty", "Quantity", step="any"),
                F("unit_price", "Unit price", step="0.01"),
                F("processed", "Processed coconuts (count)", step="1")],
        amount=lambda r: r["qty"] * r["unit_price"],
        cols=[("Farmer", lambda r: r["farmer"], "l"), ("Date", lambda r: r["date"], "l"),
              ("Coconuts", lambda r: num(r["qty"], 1 if r["unit"] == "kg" else 0), ""),
              ("Unit", lambda r: r["unit"], ""),
              ("Unit price", lambda r: money(r["unit_price"]), ""),
              ("Total amount", lambda r: money(r["amount"]), ""),
              ("Processed", lambda r: num(r["processed"]), "")],
        csv=[("Farmer name", "farmer"), ("Date", "date"), ("Coconut quantity", "qty"),
             ("Unit", "unit"), ("Unit price", "unit_price"), ("Total amount", "amount"),
             ("Processed coconuts", "processed"), ("Entered by", "created_by")]),
    "copra": dict(
        title="Copra", table="copra", total_label="Total copra price",
        fields=[F("date", "Date", "date", required=True),
                F("kg", "Copra (kg)", step="0.1"), F("price", "Price (per kg)", step="0.01")],
        amount=lambda r: r["kg"] * r["price"],
        cols=[("Date", lambda r: r["date"], "l"), ("Copra (kg)", lambda r: num(r["kg"], 1), ""),
              ("Price per kg", lambda r: money(r["price"]), ""),
              ("Total copra price", lambda r: money(r["amount"]), "")],
        csv=[("Date", "date"), ("Copra (kg)", "kg"), ("Price per kg", "price"),
             ("Total copra price", "amount"), ("Entered by", "created_by")]),
    "shell": dict(
        title="Shell", table="shell", total_label="Total shell price",
        fields=[F("date", "Date", "date", required=True),
                F("tons", "Shell (tons)", step="0.01"), F("price", "Price (per ton)", step="0.01")],
        amount=lambda r: r["tons"] * r["price"],
        cols=[("Date", lambda r: r["date"], "l"), ("Shell (tons)", lambda r: num(r["tons"], 2), ""),
              ("Price per ton", lambda r: money(r["price"]), ""),
              ("Total shell price", lambda r: money(r["amount"]), "")],
        csv=[("Date", "date"), ("Shell (tons)", "tons"), ("Price per ton", "price"),
             ("Total shell price", "amount"), ("Entered by", "created_by")]),
    "expenses": dict(
        title="Labor & transport", table="expenses", total_label="Total expenses (labor + transport)",
        fields=[F("date", "Date", "date", required=True),
                F("labor", "Labor cost (weekly)", step="0.01"),
                F("transport", "Transport cost", step="0.01")],
        amount=lambda r: r["labor"] + r["transport"],
        cols=[("Date", lambda r: r["date"], "l"), ("Labor (weekly)", lambda r: money(r["labor"]), ""),
              ("Transport", lambda r: money(r["transport"]), ""),
              ("Total", lambda r: money(r["amount"]), "")],
        csv=[("Date", "date"), ("Labor cost (weekly)", "labor"), ("Transport cost", "transport"),
             ("Total expenses", "amount"), ("Entered by", "created_by")]),
    "coir": dict(
        title="Coir", table="coir", total_label=None,
        fields=[F("date", "Date", "date", required=True),
                F("loads", "Number of loads", step="1"), F("amount", "Total amount", step="0.01")],
        amount=lambda r: r["amount"],
        cols=[("Date", lambda r: r["date"], "l"), ("Number of loads", lambda r: num(r["loads"]), ""),
              ("Total amount", lambda r: money(r["amount"]), "")],
        csv=[("Date", "date"), ("Number of loads", "loads"), ("Total amount", "amount"), ("Entered by", "created_by")]),
}


def get_cfg(key):
    if key not in DATASETS:
        abort(404)
    return DATASETS[key]


def valid_date(text):
    try:
        return len(text) == 10 and dt.date.fromisoformat(text) is not None
    except (TypeError, ValueError):
        return False


def fetch_rows(conn, key, start="", end=""):
    cfg = DATASETS[key]
    where, params = [], []
    if start:
        where.append("date >= ?")
        params.append(start)
    if end:
        where.append("date <= ?")
        params.append(end)
    sql = f"SELECT * FROM {cfg['table']}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date, id"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r["amount"] = cfg["amount"](r) if key != "coir" else r["amount"]
    return rows


def parse_form(cfg, form):
    values, errors = {}, []
    for f in cfg["fields"]:
        raw = (form.get(f["name"]) or "").strip()
        if f["type"] == "text":
            if f.get("required") and not raw:
                errors.append(f"{f['label']} is required.")
            values[f["name"]] = raw[:200]
        elif f["type"] == "date":
            if not valid_date(raw):
                errors.append(f"{f['label']} must be a valid date.")
            values[f["name"]] = raw
        elif f["type"] == "select":
            if raw not in f["options"]:
                errors.append(f"{f['label']} is not valid.")
            values[f["name"]] = raw
        else:
            try:
                val = float(raw) if raw else 0.0
                if val < 0 or val != val or val in (float("inf"),):
                    raise ValueError
            except ValueError:
                errors.append(f"{f['label']} must be a positive number.")
                val = 0.0
            values[f["name"]] = val
    return values, errors


# --------------------------------------------------------------------------
# Summary and statement
# --------------------------------------------------------------------------
def summarize(conn, start="", end=""):
    coco = fetch_rows(conn, "coconut", start, end)
    copra = fetch_rows(conn, "copra", start, end)
    shell = fetch_rows(conn, "shell", start, end)
    exp = fetch_rows(conn, "expenses", start, end)
    coir = fetch_rows(conn, "coir", start, end)
    s = {
        "coco_rows": coco,
        "count": sum(r["qty"] for r in coco if r["unit"] != "kg"),
        "kg": sum(r["qty"] for r in coco if r["unit"] == "kg"),
        "invested": sum(r["amount"] for r in coco),
        "processed": sum(r["processed"] for r in coco),
        "copra_kg": sum(r["kg"] for r in copra), "copra": sum(r["amount"] for r in copra),
        "shell_tons": sum(r["tons"] for r in shell), "shell": sum(r["amount"] for r in shell),
        "labor": sum(r["labor"] for r in exp), "transport": sum(r["transport"] for r in exp),
        "coir_loads": sum(r["loads"] for r in coir), "coir": sum(r["amount"] for r in coir),
    }
    s["expenses"] = s["labor"] + s["transport"]
    s["income"] = s["copra"] + s["shell"] + s["coir"]
    s["profit"] = round(s["income"] - s["invested"] - s["expenses"], 2)
    s["state"] = "even" if abs(s["profit"]) < 0.005 else ("profit" if s["profit"] > 0 else "loss")
    cost = s["invested"] + s["expenses"]
    s["pct"] = (abs(s["profit"]) / cost * 100) if cost > 0 else None
    s["empty"] = not (coco or copra or shell or exp or coir)
    return s


def foot_pairs(key, rows):
    if key == "coconut":
        count = sum(r["qty"] for r in rows if r["unit"] != "kg")
        kg = sum(r["qty"] for r in rows if r["unit"] == "kg")
        return [("Purchased", f"{num(count)} count · {num(kg, 1)} kg"),
                ("Amount invested", money(sum(r["amount"] for r in rows))),
                ("Processed", num(sum(r["processed"] for r in rows)))]
    if key == "copra":
        return [("Copra", f"{num(sum(r['kg'] for r in rows), 1)} kg"),
                ("Total copra amount", money(sum(r["amount"] for r in rows)))]
    if key == "shell":
        return [("Shell", f"{num(sum(r['tons'] for r in rows), 2)} tons"),
                ("Total shell amount", money(sum(r["amount"] for r in rows)))]
    if key == "expenses":
        return [("Labor", money(sum(r["labor"] for r in rows))),
                ("Transport", money(sum(r["transport"] for r in rows))),
                ("Total expenses", money(sum(r["amount"] for r in rows)))]
    return [("Loads", num(sum(r["loads"] for r in rows))),
            ("Total coir amount", money(sum(r["amount"] for r in rows)))]


def month_bounds(month):
    year, mon = (int(x) for x in month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}", \
        dt.date(year, mon, 1).strftime("%B %Y")


def valid_month(text):
    try:
        y, m = text.split("-")
        return len(y) == 4 and 1 <= int(m) <= 12 and int(y) > 1900
    except (ValueError, AttributeError):
        return False


def statement_text(conn, month):
    start, end, name = month_bounds(month)
    s = summarize(conn, start, end)
    if s["empty"]:
        return f"Coconut mill - monthly statement\n{name}\n\nNo records for this month.\n"
    farmers = "\n".join(
        f"  {r['date']}  {r['farmer']}: {num(r['qty'], 1 if r['unit'] == 'kg' else 0)} "
        f"{'kg' if r['unit'] == 'kg' else 'coconuts'} @ {money(r['unit_price'])} = {money(r['amount'])}"
        for r in s["coco_rows"]) or "  (none)"
    word = {"profit": "PROFIT", "loss": "LOSS", "even": "BREAK-EVEN"}[s["state"]]
    return f"""Coconut mill - monthly statement
{name}

1. COCONUT PURCHASES
{farmers}
  Coconuts purchased: {num(s['count'])} count, {num(s['kg'], 1)} kg
  Coconuts processed: {num(s['processed'])}
  Total amount invested: {money(s['invested'])}

2. COPRA
  {num(s['copra_kg'], 1)} kg - total copra amount {money(s['copra'])}

3. SHELL
  {num(s['shell_tons'], 2)} tons - total shell amount {money(s['shell'])}

4. LABOR AND TRANSPORT
  Labor (weekly): {money(s['labor'])}
  Transport: {money(s['transport'])}
  Total expenses: {money(s['expenses'])}

5. COIR
  {num(s['coir_loads'])} loads - total coir amount {money(s['coir'])}

RESULT
  {word}: {money(abs(s['profit']))}
  (copra + shell + coir {money(s['income'])} - invested {money(s['invested'])} - expenses {money(s['expenses'])})
"""


# --------------------------------------------------------------------------
# Email + scheduler
# --------------------------------------------------------------------------
def smtp_configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def send_email(to, subject, body, attachments=()):
    if not smtp_configured():
        raise RuntimeError("SMTP is not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM.")
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, data, mimetype in attachments:
        maintype, subtype = mimetype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if os.environ.get("SMTP_SSL") == "1":
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        if os.environ.get("SMTP_SSL") != "1" and os.environ.get("SMTP_STARTTLS", "1") == "1":
            server.starttls()
        if user:
            server.login(user, password or "")
        server.send_message(msg)


def summary_csv_bytes(conn, month):
    start, end, _ = month_bounds(month)
    s = summarize(conn, start, end)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Item", "Value"])
    for label, val in [
        ("Coconuts purchased (count)", s["count"]), ("Coconuts purchased (kg)", s["kg"]),
        ("Total amount invested", f"{s['invested']:.2f}"), ("Total copra amount", f"{s['copra']:.2f}"),
        ("Total shell amount", f"{s['shell']:.2f}"), ("Coir loads", s["coir_loads"]),
        ("Total coir amount", f"{s['coir']:.2f}"), ("Labor cost", f"{s['labor']:.2f}"),
        ("Transport cost", f"{s['transport']:.2f}"), ("Total expenses", f"{s['expenses']:.2f}"),
        ("Total income (copra + shell + coir)", f"{s['income']:.2f}"),
        ("Profit" if s["profit"] >= 0 else "Loss", f"{abs(s['profit']):.2f}")]:
        w.writerow([label, val])
    return buf.getvalue().encode("utf-8")


def send_monthly_statement(conn, month):
    """Send the statement for `month` to the registered email. Returns the address."""
    to = get_setting(conn, "registered_email")
    if not to:
        raise RuntimeError("No registered email is saved yet (Settings page).")
    _, _, name = month_bounds(month)
    send_email(to, f"Coconut mill statement - {name}", statement_text(conn, month),
               [(f"statement-{month}.csv", summary_csv_bytes(conn, month), "text/csv")])
    set_setting(conn, "last_sent_month", month)
    set_setting(conn, "last_sent_at", dt.datetime.now().isoformat(timespec="seconds"))
    return to


def scheduled_job():
    """Runs on day 1 of each month: sends the previous month's statement once."""
    today = dt.date.today()
    prev = (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    conn = connect()
    try:
        if get_setting(conn, "last_sent_month") == prev:
            return
        if not get_setting(conn, "registered_email"):
            app.logger.warning("Monthly statement skipped: no registered email.")
            return
        to = send_monthly_statement(conn, prev)
        app.logger.info("Monthly statement for %s sent to %s", prev, to)
    except Exception:
        app.logger.exception("Monthly statement failed; it will retry on the next run.")
    finally:
        conn.close()


def start_scheduler():
    if os.environ.get("ENABLE_SCHEDULER", "1") != "1":
        return None
    from apscheduler.schedulers.background import BackgroundScheduler
    tz = os.environ.get("SCHEDULER_TZ", "Asia/Kolkata")
    sched = BackgroundScheduler(timezone=tz)
    # Day 1 at 06:00, and a catch-up run every day at 06:30 in case the server was off
    # (scheduled_job skips if that month was already sent).
    sched.add_job(scheduled_job, "cron", day=1, hour=6, minute=0, id="monthly", misfire_grace_time=3600)
    sched.add_job(scheduled_job, "cron", day="2-5", hour=6, minute=30, id="monthly_retry",
                  misfire_grace_time=3600)
    sched.start()
    return sched


# --------------------------------------------------------------------------
# Accounts, sessions, CSRF, security headers
# --------------------------------------------------------------------------
PUBLIC = {"login", "setup", "forgot", "reset", "static", "manifest", "service_worker", "offline"}
_FAILS = {}  # login throttling: key -> [failure count, locked-until timestamp]


def lock_remaining(key):
    rec = _FAILS.get(key)
    if rec and rec[1] > time.time():
        return int(rec[1] - time.time())
    return 0


def register_fail(key, limit=5, lock_seconds=300):
    rec = _FAILS.setdefault(key, [0, 0])
    rec[0] += 1
    if rec[0] >= limit:
        rec[0], rec[1] = 0, time.time() + lock_seconds


def valid_email(text):
    return bool(text) and len(text) <= 200 and bool(EMAIL_RE.match(text))


def user_count(conn):
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user or g.user["role"] != "admin":
            abort(403)
        return fn(*a, **kw)
    return wrapper


@app.before_request
def guard():
    g.user = None
    ep = request.endpoint
    if ep is None:
        return None
    conn = db()
    uid = session.get("uid")
    if uid:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        g.user = dict(row) if row else None
        if g.user is None:
            session.pop("uid", None)
    if ep != "static":
        if user_count(conn) == 0:
            if ep not in ("setup", "manifest", "service_worker", "offline"):
                return redirect(url_for("setup"))
        elif ep == "setup":
            return redirect(url_for("login"))
        elif ep not in PUBLIC and g.user is None:
            return redirect(url_for("login", next=request.path))
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not secrets.compare_digest(sent.encode(), session.get("csrf", "").encode()):
            abort(400, "Invalid or missing form token. Reload the page and try again.")
    return None


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    return resp


@app.context_processor
def inject_globals():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return dict(csrf=session["csrf"], datasets=DATASETS, cur=CUR, user=g.get("user"),
                today=dt.date.today().isoformat())


def start_session(user_id):
    session.clear()
    session["uid"] = user_id
    session["csrf"] = secrets.token_hex(16)
    session.permanent = True


def safe_next():
    nxt = request.args.get("next", "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard")


def check_new_password(pw, confirm=None):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if len(pw) > 200:
        return "Password is too long."
    if confirm is not None and pw != confirm:
        return "The two passwords do not match."
    return None


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run only: create the owner (admin) account."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        err = None if valid_email(email) else "Enter a valid email address."
        err = err or check_new_password(pw, request.form.get("confirm", ""))
        if err:
            flash(err, "error")
        else:
            conn = db()
            if user_count(conn) == 0:
                cur = conn.execute("INSERT INTO users(email, password_hash, role) VALUES(?,?, 'admin')",
                                   (email, generate_password_hash(pw)))
                conn.commit()
                start_session(cur.lastrowid)
                set_setting(conn, "registered_email", email)  # default statement recipient
                return redirect(url_for("dashboard"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        key = f"{email}|{request.remote_addr}"
        wait = lock_remaining(key)
        if wait:
            flash(f"Too many failed attempts. Try again in about {wait // 60 + 1} minute(s).", "error")
        else:
            row = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            ok = check_password_hash(row["password_hash"] if row else DUMMY_HASH, pw) and row is not None
            if ok:
                _FAILS.pop(key, None)
                nxt = safe_next()
                start_session(row["id"])
                return redirect(nxt)
            register_fail(key)
            flash("Incorrect email or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def reset_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="password-reset")


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    can_email = smtp_configured() and bool(BASE_URL)
    if request.method == "POST":
        key = f"forgot|{request.remote_addr}"
        if lock_remaining(key):
            flash("Too many requests. Please try again later.", "error")
        else:
            register_fail(key, limit=5, lock_seconds=900)
            email = request.form.get("email", "").strip().lower()
            row = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if row and can_email:
                token = reset_serializer().dumps({"u": row["id"], "h": row["password_hash"][-12:]})
                link = f"{BASE_URL}{url_for('reset', token=token)}"
                try:
                    send_email(row["email"], "Reset your Coconut Mill Ledger password",
                               f"Use this link to choose a new password (valid for 1 hour):\n\n{link}\n\n"
                               "If you did not ask for this, ignore this email.")
                except Exception:
                    app.logger.exception("Could not send password reset email")
            # Same answer whether or not the account exists (no account enumeration).
            flash("If that account exists and email is set up on the server, a reset link is on its way.", "ok")
            return redirect(url_for("login"))
    return render_template("forgot.html", can_email=can_email)


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    try:
        data = reset_serializer().loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        flash("That reset link is invalid or has expired.", "error")
        return redirect(url_for("forgot"))
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (data.get("u"),)).fetchone()
    if row is None or row["password_hash"][-12:] != data.get("h"):  # already used
        flash("That reset link is invalid or has expired.", "error")
        return redirect(url_for("forgot"))
    if request.method == "POST":
        pw = request.form.get("password", "")
        err = check_new_password(pw, request.form.get("confirm", ""))
        if err:
            flash(err, "error")
        else:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), row["id"]))
            conn.commit()
            flash("Password updated. Please log in.", "ok")
            return redirect(url_for("login"))
    return render_template("reset.html", token=token)


# --------------------------------------------------------------------------
# App (PWA) routes
# --------------------------------------------------------------------------
@app.route("/manifest.webmanifest")
def manifest():
    resp = send_from_directory(app.static_folder, "manifest.webmanifest",
                               mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/sw.js")
def service_worker():
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/offline")
def offline():
    return render_template("offline.html")


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
# (dashboard route is defined below, after the summary helpers)


def date_args():
    start, end = request.args.get("start", ""), request.args.get("end", "")
    return (start if valid_date(start) else ""), (end if valid_date(end) else "")


def csv_safe(v):
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def csv_response(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(c) for c in r])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def add_months(year, month, delta):
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def short_num(n):
    if n >= 1e6:
        return f"{n / 1e6:g}M"
    if n >= 1e3:
        return f"{n / 1e3:g}K"
    return f"{n:g}"


def short_net(n):
    """Compact label for a monthly net figure: 206770 -> 207K, 4210 -> 4.2K, 850 -> 850."""
    if n >= 1e6:
        return f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1e4:
        return f"{n / 1e3:.0f}K"
    if n >= 1e3:
        return f"{n / 1e3:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{n:.0f}"


def nice_max(v):
    if v <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


def monthly_series(conn, end_date, n=6):
    out = []
    for i in range(n - 1, -1, -1):
        y, m = add_months(end_date.year, end_date.month, -i)
        first = dt.date(y, m, 1)
        last = dt.date(y, m, calendar.monthrange(y, m)[1])
        s = summarize(conn, first.isoformat(), last.isoformat())
        out.append(dict(label=first.strftime("%b"), income=s["income"],
                        cost=s["invested"] + s["expenses"], net=s["profit"]))
    return out


def chart_svg(series):
    """Grouped bars per month: income (green) vs costs (blue), net profit/loss printed underneath."""
    W, H, left, right, top, bottom = 600, 270, 50, 12, 14, 56
    pw, ph = W - left - right, H - top - bottom
    peak = max([x["income"] for x in series] + [x["cost"] for x in series] + [0])
    ymax = nice_max(peak)
    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="Monthly income versus costs, with net profit or loss">']
    for i in range(5):
        y = top + ph - ph * i / 4
        parts.append(f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="ax" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{short_num(ymax * i / 4)}</text>')
    gw = pw / len(series)
    bw = gw * 0.26
    for i, x in enumerate(series):
        cx = left + gw * i + gw / 2
        for j, (key, cls, name) in enumerate((("income", "bar-in", "Income"), ("cost", "bar-out", "Costs"))):
            h = ph * x[key] / ymax
            bx = cx - bw - 2 if j == 0 else cx + 2
            parts.append(f'<rect class="{cls}" x="{bx:.1f}" y="{top + ph - h:.1f}" width="{bw:.1f}" '
                         f'height="{h:.1f}" rx="2"><title>{x["label"]} {name}: {money0(x[key])}</title></rect>')
        parts.append(f'<text class="ax" x="{cx:.1f}" y="{top + ph + 18}" text-anchor="middle">{x["label"]}</text>')
        sign = "+" if x["net"] > 0 else ("−" if x["net"] < 0 else "")
        cls = "net-pos" if x["net"] > 0 else ("net-neg" if x["net"] < 0 else "net-zero")
        parts.append(f'<text class="net {cls}" x="{cx:.1f}" y="{top + ph + 36}" text-anchor="middle">'
                     f'{sign}{short_net(abs(x["net"]))}</text>')
    if peak == 0:
        parts.append(f'<text class="ax" x="{W / 2}" y="{top + ph / 2}" text-anchor="middle">No data for these months yet</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def recent_transactions(conn, limit=6):
    items = []
    spec = [("coconut", "Coconut purchase", "out", lambda r: r["farmer"]),
            ("copra", "Copra sale", "in", lambda r: f"{num(r['kg'], 1)} kg"),
            ("shell", "Shell sale", "in", lambda r: f"{num(r['tons'], 2)} tons"),
            ("coir", "Coir sale", "in", lambda r: f"{num(r['loads'])} loads")]
    for key, label, direction, detail in spec:
        for r in fetch_rows(conn, key)[-limit:]:
            items.append(dict(date=r["date"], id=r["id"], made=r.get("created_at") or "", type=label,
                              detail=detail(r), amount=r["amount"], direction=direction))
    for r in fetch_rows(conn, "expenses")[-limit:]:
        if r["labor"] > 0:
            items.append(dict(date=r["date"], id=r["id"], made=r.get("created_at") or "", type="Labor",
                              detail="", amount=r["labor"], direction="out"))
        if r["transport"] > 0:
            items.append(dict(date=r["date"], id=r["id"], made=r.get("created_at") or "", type="Transport",
                              detail="", amount=r["transport"], direction="out"))
    items.sort(key=lambda x: (x["date"], x["made"], x["id"]), reverse=True)
    return items[:limit]


def pct_change(cur, prev):
    if prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100


@app.route("/")
def dashboard():
    conn = db()
    today = dt.date.today()
    start, end = date_args()
    if "start" not in request.args and "end" not in request.args:
        start, end = (today - dt.timedelta(days=29)).isoformat(), today.isoformat()
    cur = summarize(conn, start, end)
    prev = None
    if start and end and start <= end:
        d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        span = (d1 - d0).days + 1
        pe = d0 - dt.timedelta(days=1)
        prev = summarize(conn, (pe - dt.timedelta(days=span - 1)).isoformat(), pe.isoformat())
    p = lambda key: prev[key] if prev else None
    kpis = [
        dict(label="Total Coconuts", value=num(cur["count"]),
             sub=f"+ {num(cur['kg'], 1)} kg" if cur["kg"] else "", change=pct_change(cur["count"], p("count")), cls=""),
        dict(label="Copra Produced", value=f"{num(cur['copra_kg'], 1)} kg", sub="",
             change=pct_change(cur["copra_kg"], p("copra_kg")), cls=""),
        dict(label="Total Revenue", value=money0(cur["income"]), sub="",
             change=pct_change(cur["income"], p("income")), cls=""),
        dict(label="Net Loss" if cur["state"] == "loss" else "Net Profit", value=money0(cur["profit"]), sub="",
             change=pct_change(cur["profit"], p("profit")), cls="neg" if cur["state"] == "loss" else "pos"),
    ]
    chart_end = dt.date.fromisoformat(end) if end else today
    return render_template("dashboard.html", kpis=kpis, cur=cur, start=start, end=end,
                           chart=chart_svg(monthly_series(conn, chart_end)),
                           recent=recent_transactions(conn),
                           has_prev=prev is not None,
                           presets=dashboard_presets(today))


def dashboard_presets(today):
    first = today.replace(day=1)
    return [("This month", first.isoformat(), today.isoformat()),
            ("Last 30 days", (today - dt.timedelta(days=29)).isoformat(), today.isoformat()),
            ("Last 90 days", (today - dt.timedelta(days=89)).isoformat(), today.isoformat()),
            ("This year", today.replace(month=1, day=1).isoformat(), today.isoformat())]


@app.route("/d/<key>", methods=["GET", "POST"])
def dataset(key):
    cfg = get_cfg(key)
    conn = db()
    start, end = date_args()
    values, edit_id = {}, request.args.get("edit", type=int)
    if request.method == "POST":
        values, errors = parse_form(cfg, request.form)
        rid = request.form.get("id", type=int)
        if errors:
            for e in errors:
                flash(e, "error")
            edit_id = rid
        else:
            names = [f["name"] for f in cfg["fields"]]
            if rid:
                conn.execute(f"UPDATE {cfg['table']} SET " + ", ".join(f"{n}=?" for n in names) +
                             " WHERE id=?", [values[n] for n in names] + [rid])
                flash("Record updated.", "ok")
            else:
                conn.execute(f"INSERT INTO {cfg['table']} ({', '.join(names)}, created_by) VALUES "
                             f"({', '.join('?' for _ in names)}, ?)", [values[n] for n in names] + [g.user["email"]])
                flash("Record saved.", "ok")
            conn.commit()
            return redirect(url_for("dataset", key=key, start=start, end=end))
    elif edit_id:
        row = conn.execute(f"SELECT * FROM {cfg['table']} WHERE id=?", (edit_id,)).fetchone()
        if row is None:
            abort(404)
        values = dict(row)
    rows = fetch_rows(conn, key, start, end)
    view = [dict(id=r["id"], cells=[c[1](r) for c in cfg["cols"]]) for r in rows]
    return render_template("dataset.html", key=key, cfg=cfg, rows=view, values=values,
                           edit_id=edit_id, start=start, end=end, foot=foot_pairs(key, rows) if rows else [])


@app.route("/d/<key>/<int:rid>/delete", methods=["POST"])
@admin_required
def delete_record(key, rid):
    cfg = get_cfg(key)
    conn = db()
    conn.execute(f"DELETE FROM {cfg['table']} WHERE id=?", (rid,))
    conn.commit()
    flash("Record deleted.", "ok")
    return redirect(url_for("dataset", key=key, start=request.form.get("start", ""),
                            end=request.form.get("end", "")))


@app.route("/d/<key>/csv")
def dataset_csv(key):
    cfg = get_cfg(key)
    start, end = date_args()
    rows = fetch_rows(db(), key, start, end)
    header = [h for h, _ in cfg["csv"]]
    body = [[f"{r[f]:.2f}" if f == "amount" else r[f] for _, f in cfg["csv"]] for r in rows]
    return csv_response(f"{key}-{start or 'start'}_to_{end or 'latest'}.csv", header, body)


@app.route("/summary")
def summary():
    start, end = date_args()
    return render_template("summary.html", s=summarize(db(), start, end), start=start, end=end)


@app.route("/summary/csv")
def summary_csv():
    start, end = date_args()
    s = summarize(db(), start, end)
    rows = [["Coconuts purchased (count)", s["count"]], ["Coconuts purchased (kg)", s["kg"]],
            ["Total amount invested", f"{s['invested']:.2f}"], ["Total copra amount", f"{s['copra']:.2f}"],
            ["Total shell amount", f"{s['shell']:.2f}"], ["Coir loads", s["coir_loads"]],
            ["Total coir amount", f"{s['coir']:.2f}"], ["Labor cost", f"{s['labor']:.2f}"],
            ["Transport cost", f"{s['transport']:.2f}"], ["Total expenses", f"{s['expenses']:.2f}"],
            ["Total income (copra + shell + coir)", f"{s['income']:.2f}"],
            ["Profit" if s["profit"] >= 0 else "Loss", f"{abs(s['profit']):.2f}"]]
    return csv_response(f"summary-{start or 'start'}_to_{end or 'latest'}.csv", ["Item", "Value"], rows)


@app.route("/statement")
def statement():
    conn = db()
    month = request.args.get("month", "")
    if not valid_month(month):
        month = dt.date.today().strftime("%Y-%m")
    return render_template("statement.html", month=month, text=statement_text(conn, month),
                           email=get_setting(conn, "registered_email"), smtp_ok=smtp_configured(),
                           last_month=get_setting(conn, "last_sent_month"),
                           last_at=get_setting(conn, "last_sent_at"))


@app.route("/statement/download")
def statement_download():
    month = request.args.get("month", "")
    if not valid_month(month):
        abort(400)
    return Response(statement_text(db(), month), mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="statement-{month}.txt"'})


@app.route("/statement/send", methods=["POST"])
@admin_required
def statement_send():
    month = request.form.get("month", "")
    if not valid_month(month):
        abort(400)
    try:
        to = send_monthly_statement(db(), month)
        flash(f"Statement for {month} sent to {to}.", "ok")
    except Exception as exc:  # show the reason (SMTP errors, missing email) to the user
        flash(f"Could not send: {exc}", "error")
    return redirect(url_for("statement", month=month))


@app.route("/settings")
def settings():
    conn = db()
    users = [dict(r) for r in conn.execute("SELECT id, email, role, created_at FROM users ORDER BY id")]
    return render_template("settings.html", email=get_setting(conn, "registered_email"),
                           smtp_ok=smtp_configured(), users=users,
                           scheduler_on=os.environ.get("ENABLE_SCHEDULER", "1") == "1",
                           tz=os.environ.get("SCHEDULER_TZ", "Asia/Kolkata"),
                           last_month=get_setting(conn, "last_sent_month"),
                           last_at=get_setting(conn, "last_sent_at"))


@app.route("/settings/email", methods=["POST"])
@admin_required
def settings_email():
    email = request.form.get("registered_email", "").strip().lower()
    if email and not valid_email(email):
        flash("Enter a valid email address.", "error")
    else:
        set_setting(db(), "registered_email", email)
        flash("Statement email saved.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/password", methods=["POST"])
def settings_password():
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
    pw = request.form.get("new_password", "")
    if not check_password_hash(row["password_hash"], request.form.get("current_password", "")):
        flash("Your current password is not correct.", "error")
    else:
        err = check_new_password(pw, request.form.get("confirm", ""))
        if err:
            flash(err, "error")
        else:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), row["id"]))
            conn.commit()
            flash("Password changed.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/users/add", methods=["POST"])
@admin_required
def user_add():
    conn = db()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "staff")
    pw = request.form.get("password", "")
    err = None if valid_email(email) else "Enter a valid email address."
    err = err or (None if role in ("admin", "staff") else "Choose a role.")
    err = err or check_new_password(pw)
    if not err and conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        err = "That email already has an account."
    if err:
        flash(err, "error")
    else:
        conn.execute("INSERT INTO users(email, password_hash, role) VALUES(?,?,?)",
                     (email, generate_password_hash(pw), role))
        conn.commit()
        flash(f"Account created for {email}. Share the temporary password with them privately.", "ok")
    return redirect(url_for("settings"))


def admin_count(conn):
    return conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]


@app.route("/settings/users/<int:uid>/delete", methods=["POST"])
@admin_required
def user_delete(uid):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        abort(404)
    if row["id"] == g.user["id"]:
        flash("You cannot remove your own account.", "error")
    elif row["role"] == "admin" and admin_count(conn) <= 1:
        flash("There must be at least one admin.", "error")
    else:
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        flash(f"Removed {row['email']}.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/users/<int:uid>/password", methods=["POST"])
@admin_required
def user_reset_password(uid):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        abort(404)
    pw = request.form.get("password", "")
    err = check_new_password(pw)
    if err:
        flash(err, "error")
    else:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
        conn.commit()
        flash(f"Password reset for {row['email']}. Share it with them privately.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/users/<int:uid>/role", methods=["POST"])
@admin_required
def user_role(uid):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    role = request.form.get("role", "")
    if row is None:
        abort(404)
    if role not in ("admin", "staff"):
        abort(400)
    if row["role"] == "admin" and role != "admin" and admin_count(conn) <= 1:
        flash("There must be at least one admin.", "error")
    else:
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        conn.commit()
        flash(f"{row['email']} is now {role}.", "ok")
    return redirect(url_for("settings"))


@app.cli.command("send-statement")
@click.option("--month", default="", help="YYYY-MM (default: previous month)")
def cli_send_statement(month):
    """Send a monthly statement now (use from cron if you disable the built-in scheduler)."""
    if not month:
        month = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    if not valid_month(month):
        raise click.BadParameter("Use YYYY-MM")
    conn = connect()
    try:
        click.echo(f"Sent to {send_monthly_statement(conn, month)}")
    finally:
        conn.close()


init_db()
# Started once per process. Run with gunicorn --workers 1 (see Dockerfile) or `python app.py`.
# If you use `flask run --debug`, set ENABLE_SCHEDULER=0 to avoid a duplicate scheduler.
_scheduler = start_scheduler()

if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", "5000")))
