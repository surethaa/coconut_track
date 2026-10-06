import datetime as dt
import importlib
import os
import re
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

OWNER = ("owner@example.com", "owner-pass-123")


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_PATH", os.path.join(tmp, "test.db"))
    monkeypatch.setenv("ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("CURRENCY_SYMBOL", "$")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("BASE_URL", "https://mill.example.com")
    for k in ("SMTP_HOST", "SMTP_FROM", "SMTP_PORT", "SMTP_STARTTLS"):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    mod.app.config["TESTING"] = True
    mod.app.config["SESSION_COOKIE_SECURE"] = False
    c = mod.app.test_client()
    c.mod = mod
    return c


def token(c, path):
    html = c.get(path, follow_redirects=True).get_data(as_text=True)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def post(c, path, page, **data):
    data["csrf"] = token(c, page)
    return c.post(path, data=data, follow_redirects=True)


def setup_owner(c):
    return post(c, "/setup", "/setup", email=OWNER[0], password=OWNER[1], confirm=OWNER[1])


def login(c, email, pw):
    return post(c, "/login", "/login", email=email, password=pw)


def logout(c):
    post(c, "/logout", "/")


def add_staff(c, email="staff@example.com", pw="staff-pass-123", role="staff"):
    return post(c, "/settings/users/add", "/settings", email=email, password=pw, role=role)


def seed(c, d1="2026-08-03", d2="2026-08-10"):
    post(c, "/d/coconut", "/d/coconut", farmer="Ravi", date=d1, unit="count", qty="20000", unit_price="10", processed="18000")
    post(c, "/d/coconut", "/d/coconut", farmer="Mani", date=d2, unit="kg", qty="500", unit_price="20", processed="0")
    post(c, "/d/copra", "/d/copra", date=d1, kg="3000", price="100")
    post(c, "/d/shell", "/d/shell", date=d1, tons="1.8", price="1000")
    post(c, "/d/expenses", "/d/expenses", date=d1, labor="5000", transport="2000")
    post(c, "/d/coir", "/d/coir", date=d1, loads="4", amount="10000")


# ---------------- accounts ----------------
def test_first_run_redirects_to_setup_then_creates_admin(client):
    r = client.get("/")
    assert r.status_code == 302 and "/setup" in r.headers["Location"]
    html = setup_owner(client).get_data(as_text=True)
    assert "Dashboard" in html and OWNER[0] in html
    assert client.get("/setup").status_code == 302  # setup is closed once a user exists


def test_setup_validation(client):
    r = post(client, "/setup", "/setup", email="not-an-email", password="short", confirm="short")
    assert "valid email" in r.get_data(as_text=True)
    r = post(client, "/setup", "/setup", email="a@b.co", password="longenough1", confirm="different1")
    assert "do not match" in r.get_data(as_text=True)


def test_login_with_email_and_wrong_password(client):
    setup_owner(client)
    logout(client)
    assert client.get("/d/coconut").status_code == 302
    bad = login(client, OWNER[0], "wrong-password")
    assert "Incorrect email or password" in bad.get_data(as_text=True)
    ok = login(client, OWNER[0].upper(), OWNER[1])  # email is case-insensitive
    assert "Dashboard" in ok.get_data(as_text=True)


def test_login_lockout_after_repeated_failures(client):
    setup_owner(client)
    logout(client)
    for _ in range(5):
        login(client, OWNER[0], "nope-nope-nope")
    r = login(client, OWNER[0], OWNER[1])  # even the right password is refused while locked
    assert "Too many failed attempts" in r.get_data(as_text=True)


def test_csrf_blocks_post_without_token(client):
    setup_owner(client)
    assert client.post("/d/copra", data={"date": "2026-08-05", "kg": "1", "price": "1"}).status_code == 400


def test_staff_permissions(client):
    setup_owner(client)
    add_staff(client)
    seed(client)
    logout(client)
    login(client, "staff@example.com", "staff-pass-123")
    page = client.get("/d/coconut").get_data(as_text=True)
    assert "Ravi" in page and "Delete" not in page  # no delete button for staff
    assert post(client, "/d/copra", "/d/copra", date="2026-08-20", kg="5", price="2").status_code == 200
    t = token(client, "/d/copra")
    assert client.post("/d/copra/1/delete", data={"csrf": t}).status_code == 403
    assert client.post("/settings/users/add", data={"csrf": t, "email": "x@y.co", "password": "abcdefgh1", "role": "admin"}).status_code == 403
    assert client.post("/settings/email", data={"csrf": t, "registered_email": "x@y.co"}).status_code == 403
    assert "Add a user" not in client.get("/settings").get_data(as_text=True)


def test_admin_user_management_rules(client):
    setup_owner(client)
    add_staff(client)
    assert "staff@example.com" in client.get("/settings").get_data(as_text=True)
    assert "already has an account" in add_staff(client).get_data(as_text=True)
    assert "cannot remove your own" in post(client, "/settings/users/1/delete", "/settings").get_data(as_text=True)
    assert "at least one admin" in post(client, "/settings/users/1/role", "/settings", role="staff").get_data(as_text=True)
    post(client, "/settings/users/2/password", "/settings", password="brand-new-pass1")
    logout(client)
    assert "Dashboard" in login(client, "staff@example.com", "brand-new-pass1").get_data(as_text=True)
    logout(client)
    login(client, *OWNER)
    post(client, "/settings/users/2/delete", "/settings")
    assert "staff@example.com" not in client.get("/settings").get_data(as_text=True)


def test_change_own_password(client):
    setup_owner(client)
    bad = post(client, "/settings/password", "/settings", current_password="nope", new_password="another-pass1", confirm="another-pass1")
    assert "not correct" in bad.get_data(as_text=True)
    post(client, "/settings/password", "/settings", current_password=OWNER[1], new_password="another-pass1", confirm="another-pass1")
    logout(client)
    assert "Dashboard" in login(client, OWNER[0], "another-pass1").get_data(as_text=True)


def test_password_reset_flow(client, monkeypatch):
    sent = []
    setup_owner(client)
    logout(client)
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("SMTP_FROM", "mill@example.com")
    monkeypatch.setattr(client.mod, "send_email", lambda to, subject, body, attachments=(): sent.append((to, body)))
    r = post(client, "/forgot", "/forgot", email=OWNER[0])
    assert "reset link is on its way" in r.get_data(as_text=True)
    assert sent and sent[0][0] == OWNER[0]
    link = re.search(r"https://mill\.example\.com(/reset/\S+)", sent[0][1]).group(1)
    assert "Choose a new password" in client.get(link).get_data(as_text=True)
    done = post(client, link, link, password="reset-pass-999", confirm="reset-pass-999")
    assert "Password updated" in done.get_data(as_text=True)
    assert "invalid or has expired" in client.get(link, follow_redirects=True).get_data(as_text=True)  # single use
    assert "Dashboard" in login(client, OWNER[0], "reset-pass-999").get_data(as_text=True)
    logout(client)
    before = len(sent)
    r = post(client, "/forgot", "/forgot", email="ghost@example.com")
    assert "reset link is on its way" in r.get_data(as_text=True) and len(sent) == before


def test_bad_reset_token(client):
    setup_owner(client)
    logout(client)
    r = client.get("/reset/not-a-real-token", follow_redirects=True)
    assert "invalid or has expired" in r.get_data(as_text=True)


# ---------------- app (PWA) ----------------
def test_pwa_assets_are_public_and_correct(client):
    r = client.get("/manifest.webmanifest")  # public, even before setup / when logged out
    assert r.status_code == 200 and "manifest+json" in r.mimetype
    data = r.get_json(force=True)
    assert data["display"] == "standalone" and data["start_url"] == "/"
    assert any(i["sizes"] == "512x512" for i in data["icons"])
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.mimetype and "no-cache" in sw.headers["Cache-Control"]
    assert client.get("/offline").status_code == 200
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_security_headers_and_no_inline_handlers(client):
    r = client.get("/offline")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    setup_owner(client)
    for path in ("/", "/d/coconut", "/settings", "/summary", "/statement"):
        html = client.get(path).get_data(as_text=True)
        assert " onclick=" not in html and " onsubmit=" not in html, path


# ---------------- dashboard ----------------
def test_dashboard_shows_kpis_chart_transactions_inventory(client):
    setup_owner(client)
    today = dt.date.today().isoformat()
    seed(client, d1=today, d2=today)
    html = client.get("/").get_data(as_text=True)
    for text in ("Total Coconuts", "Copra Produced", "Total Revenue", "Net Profit",
                 "Monthly Profit / Loss", "Recent Transactions", "Inventory Overview"):
        assert text in html
    assert "$ 311,800" in html            # revenue = 300000 + 1800 + 10000
    assert "$ 94,800" in html             # net profit
    assert "<svg" in html and "Coir sale" in html and "Coconut purchase" in html and "Transport" in html
    assert "20,000" in html and "18,000" in html   # coconuts and processed in inventory


def test_dashboard_percent_change_and_loss(client):
    setup_owner(client)
    today = dt.date.today()
    post(client, "/d/copra", "/d/copra", date=(today - dt.timedelta(days=45)).isoformat(), kg="100", price="10")  # 1000 (previous window)
    post(client, "/d/copra", "/d/copra", date=today.isoformat(), kg="150", price="10")                            # 1500 (+50%)
    assert "▲ 50%" in client.get("/").get_data(as_text=True)
    post(client, "/d/coconut", "/d/coconut", farmer="X", date=today.isoformat(), unit="count", qty="1000", unit_price="10", processed="0")
    assert "Net Loss" in client.get("/").get_data(as_text=True)


def test_dashboard_range_and_all_time(client):
    setup_owner(client)
    post(client, "/d/copra", "/d/copra", date="2020-01-05", kg="10", price="10")
    def revenue_card(path):
        html = client.get(path).get_data(as_text=True)
        return re.search(r'Total Revenue</div>\s*<div class="kv[^"]*">([^<]+)<', html).group(1)

    assert revenue_card("/") == "$ 0"                  # 2020 record is outside the default last-30-days range
    assert revenue_card("/?start=&end=") == "$ 100"    # "All time" includes it


# ---------------- data ----------------
def test_profit_calculation_includes_coir(client):
    setup_owner(client)
    seed(client)
    html = client.get("/summary").get_data(as_text=True)
    assert "PROFIT: $94,800.00" in html and "10,000.00" in html


def test_loss_is_labelled(client):
    setup_owner(client)
    post(client, "/d/coconut", "/d/coconut", farmer="A", date="2026-08-01", unit="count", qty="100", unit_price="10", processed="0")
    assert "LOSS: $1,000.00" in client.get("/summary").get_data(as_text=True)


def test_date_filter_csv_and_entered_by(client):
    setup_owner(client)
    seed(client)
    r = client.get("/d/coconut/csv?start=2026-08-05&end=2026-08-31")
    text = r.get_data(as_text=True)
    assert "Mani" in text and "Ravi" not in text and r.mimetype == "text/csv"
    assert "Entered by" in text and OWNER[0] in text


def test_csv_formula_injection_is_neutralised(client):
    setup_owner(client)
    post(client, "/d/coconut", "/d/coconut", farmer="=1+1", date="2026-08-01", unit="count", qty="1", unit_price="1", processed="0")
    assert "'=1+1" in client.get("/d/coconut/csv").get_data(as_text=True)


def test_validation_rejects_bad_input(client):
    setup_owner(client)
    html = post(client, "/d/copra", "/d/copra", date="not-a-date", kg="-5", price="abc").get_data(as_text=True)
    assert "valid date" in html and "positive number" in html
    assert "No records" in client.get("/d/copra").get_data(as_text=True)


def test_edit_and_delete_as_admin(client):
    setup_owner(client)
    post(client, "/d/copra", "/d/copra", date="2026-08-05", kg="10", price="10")
    post(client, "/d/copra", "/d/copra?edit=1", id="1", date="2026-08-05", kg="20", price="10")
    assert "$200.00" in client.get("/d/copra").get_data(as_text=True)
    post(client, "/d/copra/1/delete", "/d/copra")
    assert "No records" in client.get("/d/copra").get_data(as_text=True)


def test_statement_text(client):
    setup_owner(client)
    seed(client)
    text = client.get("/statement?month=2026-08").get_data(as_text=True)
    assert "PROFIT" in text and "Ravi" in text and "COIR" in text


def test_monthly_email_is_really_sent(client, monkeypatch):
    from aiosmtpd.controller import Controller

    received = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            received.append(envelope)
            return "250 OK"

    ctl = Controller(Handler(), hostname="127.0.0.1", port=8026)
    ctl.start()
    try:
        monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("SMTP_PORT", "8026")
        monkeypatch.setenv("SMTP_FROM", "mill@example.com")
        monkeypatch.setenv("SMTP_STARTTLS", "0")
        setup_owner(client)  # owner email becomes the default statement recipient
        seed(client)
        r = post(client, "/statement/send", "/statement?month=2026-08", month="2026-08")
        assert f"sent to {OWNER[0]}" in r.get_data(as_text=True)
        assert received and received[0].rcpt_tos == [OWNER[0]]
        body = received[0].content.decode()
        assert "PROFIT" in body and "statement-2026-08.csv" in body

        m = client.mod
        prev = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
        conn = m.connect()
        m.set_setting(conn, "last_sent_month", prev)
        conn.close()
        before = len(received)
        m.scheduled_job()
        assert len(received) == before  # already sent for that month -> skipped
    finally:
        ctl.stop()
