"""Zelle / Venmo / check payments: instructions in the email, engineer marks paid, plans go out."""
import sqlite3

import pytest

from desktop import config, db, mailer, sender, watcher
from desktop.tests.test_desktop import activity_texts, attachments


def enable_zelle_venmo(stripe=True):
    s = config.load_settings()
    s["zelle"] = {"enabled": True, "recipient": "pat@acmecivil.test", "name": "Pat Lee"}
    s["venmo"] = {"enabled": True, "username": "AcmeCivil-Eng"}
    s["stripe_enabled"] = stripe
    config.save_settings(s)
    return s


def send(env, **over):
    args = dict(from_email="pat@acmecivil.test", to_email="client@example.com", price="1500",
                pdf_path=env["pdf"], client_name="Jo Smith")
    args.update(over)
    return sender.send_plans(**args)


def body(msg):
    return msg.get_body(("plain",)).get_content()


def test_zelle_only_needs_no_stripe_key(env):
    config.set_secret("stripe_secret_key", "")
    enable_zelle_venmo()
    assert config.enabled_methods() == ["zelle", "venmo"]
    tx_id = send(env, methods=["zelle"])
    tx = db.get_tx(tx_id)
    assert tx["ref_code"] == f"PLN-{tx_id}" and tx["methods"] == ["zelle"] and tx["link_id"] == ""
    (msg,) = env["smtp"].sent
    text = body(msg)
    assert "Send $1,500.00 to pat@acmecivil.test (Pat Lee)" in text and f"Memo: PLN-{tx_id}" in text
    assert "stripe.com" not in text and "venmo.com" not in text
    assert "Pay by: Zelle" in activity_texts(tx_id)[0]


def test_all_methods_in_one_email_with_venmo_link(env):
    enable_zelle_venmo()
    tx_id = send(env)  # defaults to every enabled method
    text = body(env["smtp"].sent[-1])
    assert "buy.stripe.com" in text and "Zelle" in text
    assert f"https://venmo.com/AcmeCivil-Eng?txn=pay&amount=1500.00&note=PLN-{tx_id}%20Lot%207%20Grading" in text


def test_venmo_link_encoding():
    assert mailer.venmo_link("@pat lee", 12345, "PLN-3 A&B") == \
        "https://venmo.com/pat%20lee?txn=pay&amount=123.45&note=PLN-3%20A%26B"


def test_mark_paid_delivers_once_and_closes_card_link(env):
    enable_zelle_venmo()
    tx_id = send(env)
    env["smtp"].sent.clear()
    assert sender.mark_paid(tx_id, "Zelle") is True
    tx = db.get_tx(tx_id)
    assert tx["status"] == "paid" and tx["paid_method"] == "Zelle" and tx["amount_paid_cents"] == 150000
    assert env["fake"].deactivated == ["plink_1"]
    (msg,) = env["smtp"].sent
    (name, data), = attachments(msg)
    assert data == env["pdf"].read_bytes()
    with pytest.raises(sender.SendError, match="already marked paid"):
        sender.mark_paid(tx_id, "Zelle")
    watcher.check_all()
    assert len(env["smtp"].sent) == 1
    assert "Paid $1,500.00 via Zelle (confirmed by you)" in activity_texts(tx_id)


def test_partial_payment_needs_explicit_release(env):
    enable_zelle_venmo(stripe=False)
    tx_id = send(env)
    with pytest.raises(sender.SendError, match="less than"):
        sender.mark_paid(tx_id, "Venmo", "1000")
    assert db.get_tx(tx_id)["status"] == "sent"
    assert sender.mark_paid(tx_id, "Venmo", "1000", allow_short=True)
    assert db.get_tx(tx_id)["amount_paid_cents"] == 100000


def test_watcher_skips_sends_without_stripe(env, monkeypatch):
    enable_zelle_venmo(stripe=False)
    send(env)
    monkeypatch.setattr("desktop.payments.poll", lambda link_id: pytest.fail("should not poll Stripe"))
    assert watcher.check_all()["checked"] == 0


def test_send_requires_a_method(env):
    config.set_secret("stripe_secret_key", "")
    with pytest.raises(sender.SendError, match="at least one way"):
        send(env)


def test_api_mark_paid_and_totals_by_method(env):
    from desktop.api import Api

    enable_zelle_venmo()
    a = Api()
    t1 = send(env)
    t2 = send(env, to_email="b@example.com")
    assert a.mark_paid(t1, "Zelle")["ok"]
    env["fake"].pay("plink_2", 150000)
    a.check_now()
    totals = a.transactions()["totals"]
    assert totals["paid"] == 2
    assert sorted(totals["by_method"]) == ["Stripe $1,500.00", "Zelle $1,500.00"]
    labels = {i["id"]: i["label"] for i in a.transactions()["items"]}
    assert labels[t1] == "Paid via Zelle · plans sent" and labels[t2] == "Paid via Stripe · plans sent"
    r = a.mark_paid(t1, "Zelle")
    assert not r["ok"]


def test_old_database_upgrades_in_place(env):
    path = config.db_path()
    path.unlink()
    conn = sqlite3.connect(path)  # the original v1 schema, without the new columns
    conn.executescript(db.SCHEMA.replace(""",
    ref_code TEXT NOT NULL DEFAULT '',
    methods TEXT NOT NULL DEFAULT '["stripe"]',  -- how the client may pay: stripe / zelle / venmo
    paid_method TEXT NOT NULL DEFAULT ''          -- Stripe, Zelle, Venmo, Check, Cash, Other""", ""))
    conn.execute("INSERT INTO transactions (created_at, title, client_email, from_email, price_cents, status) "
                 "VALUES ('2026-01-01', 'Old job', 'a@b.c', 'pat@acmecivil.test', 100, 'sent')")
    conn.commit()
    conn.close()
    db.init()
    (old,) = db.list_txs()
    assert old["methods"] == ["stripe"] and old["ref_code"] == "" and old["paid_method"] == ""
