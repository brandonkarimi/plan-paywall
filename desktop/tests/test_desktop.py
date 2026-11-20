import hashlib
import smtplib
import sqlite3

import pytest

from desktop import config, db, sender


def send(env, **over):
    args = dict(from_email="pat@acmecivil.test", to_email="client@example.com", price="1500",
                pdf_path=env["pdf"], client_name="Jo Smith")
    args.update(over)
    return sender.send_plans(**args)


def attachments(msg):
    return [(p.get_filename(), p.get_content()) for p in msg.iter_attachments()]


def body(msg):
    return msg.get_body(("plain",)).get_content()


def log_texts(tx_id):
    return [a["text"] for a in reversed(db.activity(tx_id))]


# ------------------------------------------------------------------ sending
def test_send_emails_preview_and_instructions_but_never_the_final(env):
    tx_id = send(env)
    tx = db.get_tx(tx_id)
    original = env["pdf"].read_bytes()
    assert tx["status"] == "sent" and tx["title"] == "Lot 7 Grading" and tx["ref_code"] == f"PLN-{tx_id}"
    assert tx["methods"] == ["zelle", "venmo"] and tx["sha256"] == hashlib.sha256(original).hexdigest()

    (msg,) = env["smtp"].sent
    assert msg["To"] == "client@example.com" and msg["Reply-To"] == "pat@acmecivil.test"
    text = body(msg)
    assert text.startswith("Hello Jo Smith,")
    assert "Here is a preview of your plans for Lot 7 Grading. To receive the full PDF, please pay $1,500.00." in text
    assert "Zelle: send $1,500.00 to pat@acmecivil.test (Pat Lee)" in text
    assert "Venmo: send $1,500.00 to @AcmeCivil-Eng" in text and "venmo.com" not in text  # no Venmo button
    assert "Once payment is received, the full PDF will be emailed to you." in text
    for detail in (tx["sha256"], "fingerprint", "Sheets:", "Memo"):  # no extra details any more
        assert detail not in text
    (name, data), = attachments(msg)
    assert name.startswith("PREVIEW") and data != original
    assert log_texts(tx_id) == ["Preview sent to client@example.com"]


def test_send_with_one_method(env):
    send(env, methods=["zelle"])
    text = body(env["smtp"].sent[0])
    assert "Zelle" in text and "Venmo" not in text


@pytest.mark.parametrize("over, message", [
    ({"to_email": "nope"}, "client's email"),
    ({"price": "0"}, "greater than"),
    ({"price": "abc"}, "price like"),
    ({"from_email": "other@x.test"}, "isn't set up"),
    ({"methods": []}, "how the client can pay"),
])
def test_send_validation(env, over, message):
    with pytest.raises(sender.SendError, match=message):
        send(env, **over)
    assert db.list_txs() == [] and env["smtp"].sent == []


def test_rejects_non_pdf(env):
    bad = env["tmp"] / "notes.pdf"
    bad.write_bytes(b"hello")
    with pytest.raises(sender.SendError, match="isn't a PDF"):
        send(env, pdf_path=bad)


def test_large_pdf_needs_share_link(env, monkeypatch):
    monkeypatch.setattr(config, "ATTACH_LIMIT_BYTES", 100)
    with pytest.raises(sender.NeedShareLink):
        send(env)
    tx_id = send(env, share_link="https://dropbox.com/s/abc")
    sender.mark_paid(tx_id, "Zelle")
    msg = env["smtp"].sent[-1]
    assert attachments(msg) == [] and "https://dropbox.com/s/abc" in body(msg)


def test_failed_email_rolls_back(env):
    env["smtp"].fail_with = smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(sender.SendError, match="Email login failed"):
        send(env)
    assert db.list_txs(["draft", "sent"]) == []


# ------------------------------------------------------------------ marking paid
def test_mark_paid_delivers_final_once(env):
    tx_id = send(env)
    env["smtp"].sent.clear()
    assert sender.mark_paid(tx_id, "Zelle") is True
    tx = db.get_tx(tx_id)
    assert tx["status"] == "paid" and tx["paid_method"] == "Zelle" and tx["amount_paid_cents"] == 150000
    (msg,) = env["smtp"].sent
    (name, data), = attachments(msg)
    assert name == "Lot 7 Grading.pdf" and data == env["pdf"].read_bytes()
    assert body(msg).startswith("Hello Jo Smith,\n\nThank you for your payment. The full plans for Lot 7 Grading are attached.")
    with pytest.raises(sender.SendError, match="already marked paid"):
        sender.mark_paid(tx_id, "Zelle")
    assert len(env["smtp"].sent) == 1
    assert log_texts(tx_id)[1:] == ["Marked as paid: $1,500.00 by Zelle", "Full plans emailed to client@example.com"]


def test_partial_payment_needs_explicit_release(env):
    tx_id = send(env)
    with pytest.raises(sender.SendError, match="less than"):
        sender.mark_paid(tx_id, "Venmo", "1000")
    assert db.get_tx(tx_id)["status"] == "sent"
    assert sender.mark_paid(tx_id, "Venmo", "1000", allow_short=True)
    assert db.get_tx(tx_id)["amount_paid_cents"] == 100000


def test_failed_delivery_is_reported_and_resend_fixes_it(env):
    tx_id = send(env)
    env["smtp"].fail_with = smtplib.SMTPServerDisconnected("down")
    assert sender.mark_paid(tx_id, "Check") is False
    tx = db.get_tx(tx_id)
    assert tx["status"] == "paid" and not tx["delivered_at"] and "down" in tx["delivery_error"]
    env["smtp"].fail_with = None
    sender.resend(tx_id)
    assert db.get_tx(tx_id)["delivered_at"] and db.get_tx(tx_id)["delivery_error"] == ""


def test_resend(env):
    tx_id = send(env)
    sender.resend(tx_id)
    assert env["smtp"].sent[-1]["Subject"] == "Preview: Lot 7 Grading"
    sender.mark_paid(tx_id, "Cash")
    sender.resend(tx_id)
    assert env["smtp"].sent[-1]["Subject"] == "Plans: Lot 7 Grading"
    assert "Full plans emailed again to client@example.com" in log_texts(tx_id)
    assert "Preview emailed again to client@example.com" in log_texts(tx_id)


# ------------------------------------------------------------------ window bridge & storage
def test_api_accounts_totals_and_csv(env, tmp_path):
    from desktop.api import Api, write_csv

    a = Api()
    assert a.save_account({"preset": "gmail", "email": "Office@AcmeCivil.test", "password": "abcd efgh ijkl mnop"})["ok"]
    assert config.smtp_password("office@acmecivil.test") == "abcdefghijklmnop"  # Gmail app-password spaces stripped
    assert not a.save_account({"preset": "custom", "email": "x@y.test", "password": "p"})["ok"]  # needs a host

    t1 = send(env, from_email="office@acmecivil.test")
    t2 = send(env, to_email="b@example.com")
    send(env, to_email="c@example.com", price="200")
    assert a.mark_paid(t1, "Zelle")["ok"] and a.mark_paid(t2, "Venmo")["ok"]
    assert not a.mark_paid(t1, "Zelle")["ok"]
    data = a.transactions()
    assert data["totals"]["paid"] == 2 and data["totals"]["collected"] == "$3,000.00"
    assert data["totals"]["waiting"] == "$200.00"
    assert sorted(data["totals"]["by_method"]) == ["Venmo $1,500.00", "Zelle $1,500.00"]
    labels = {i["id"]: i["label"] for i in data["items"]}
    assert labels[t1] == "Paid by Zelle"
    item = next(i for i in data["items"] if i["id"] == t1)
    assert item["pay_by_text"] == "Zelle or Venmo" and item["plans_text"] == "3 pages"

    out = tmp_path / "log.csv"
    write_csv(out)
    content = out.read_text(encoding="utf-8-sig")
    assert content.startswith("Date,Time,Project,Client name,Client email,Amount,Status,What happened")
    assert "Marked as paid: $1,500.00 by Zelle" in content and "PLN" not in content

    assert a.save_settings({"venmo": {"enabled": True, "username": "@new-handle"}})["ok"]
    assert config.load_settings()["venmo"]["username"] == "new-handle"
    assert not a.save_settings({"zelle": {"enabled": True, "recipient": ""}})["ok"]


def test_api_send_reports_errors_instead_of_raising(env):
    from desktop.api import Api

    r = Api().send({"from_email": "pat@acmecivil.test", "to_email": "bad", "price": "10", "pdf_path": str(env["pdf"])})
    assert r == {"ok": False, "error": "Enter the client's email address."}


def test_old_database_upgrades_in_place(env):
    path = config.db_path()
    path.unlink()
    conn = sqlite3.connect(path)  # first-release table, without the newer columns
    conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                 "title TEXT NOT NULL, client_name TEXT NOT NULL DEFAULT '', client_email TEXT NOT NULL, "
                 "from_email TEXT NOT NULL, price_cents INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'usd', "
                 "status TEXT NOT NULL DEFAULT 'draft', original_name TEXT NOT NULL DEFAULT '', "
                 "final_path TEXT NOT NULL DEFAULT '', preview_path TEXT NOT NULL DEFAULT '', "
                 "share_link TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '', pages INTEGER, "
                 "size_bytes INTEGER, sheets TEXT NOT NULL DEFAULT '[]', sent_at TEXT, paid_at TEXT, "
                 "delivered_at TEXT, amount_paid_cents INTEGER, delivery_error TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO transactions (created_at, title, client_email, from_email, price_cents, status) "
                 "VALUES ('2026-01-01', 'Old job', 'a@b.c', 'pat@acmecivil.test', 100, 'sent')")
    conn.commit()
    conn.close()
    db.init()
    (old,) = db.list_txs()
    assert old["methods"] == [] and old["ref_code"] == "" and old["paid_method"] == ""


def test_reset_log_requires_confirmation_and_never_reuses_refs(env):
    from desktop.api import Api

    a = Api()
    t1 = send(env)
    send(env, to_email="b@example.com")
    folder = config.data_dir() / "files" / str(t1)
    assert folder.is_dir()

    assert not a.reset_log("delete it")["ok"]
    assert len(db.list_txs()) == 2

    r = a.reset_log("DELETE")
    assert r["ok"] and "Cleared 2 transactions" in r["message"]
    assert db.list_txs() == [] and db.activity(t1) == [] and not folder.exists()
    assert a.transactions()["totals"]["sent"] == 0

    t3 = send(env, to_email="c@example.com")
    assert t3 == 3 and db.get_tx(t3)["ref_code"] == "PLN-3"  # numbering continues; PLN-1 is never reused


def test_greeting_without_client_name(env):
    send(env, client_name="")
    assert body(env["smtp"].sent[0]).startswith("Hello,\n")


def test_old_log_entries_are_shown_in_plain_wording():
    from desktop.api import friendly

    assert friendly("Sent to a@b.com from x@y.com ($10.00, 2 pages). Pay by: Venmo. Ref PLN-1") == "Preview sent to a@b.com"
    assert friendly("Sent to a@b.com from x@y.com (amount on invoice, 3 pages, invoice attached). Pay by: Zelle. Ref PLN-4")         == "Preview and invoice sent to a@b.com"
    assert friendly("Paid $1,500.00 via Zelle (confirmed by you)") == "Marked as paid: $1,500.00 by Zelle"
    assert friendly("Final plans re-sent to a@b.com") == "Full plans emailed again to a@b.com"
    assert friendly("Something new") == "Something new"


def test_both_methods_say_whichever_you_prefer(env):
    send(env)  # Zelle and Venmo both enabled
    text = body(env["smtp"].sent[0])
    assert "You can pay with Zelle or Venmo, whichever you prefer:\n  • Zelle: send $1,500.00" in text
    assert "  • Venmo: send $1,500.00 to @AcmeCivil-Eng" in text
    send(env, methods=["venmo"], to_email="b@example.com")
    single = body(env["smtp"].sent[1])
    assert "whichever you prefer" not in single and "Venmo: send $1,500.00 to @AcmeCivil-Eng" in single
