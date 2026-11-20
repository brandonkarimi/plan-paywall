import hashlib
import smtplib

import pytest

from desktop import config, db, sender, watcher


def send(env, **over):
    args = dict(from_email="pat@acmecivil.test", to_email="client@example.com", price="1500",
                pdf_path=env["pdf"], client_name="Jo Smith")
    args.update(over)
    return sender.send_plans(**args)


def attachments(msg):
    return [(p.get_filename(), p.get_content()) for p in msg.iter_attachments()]


def activity_texts(tx_id):
    return [a["text"] for a in reversed(db.activity(tx_id))]


# ------------------------------------------------------------------ sending
def test_send_emails_preview_and_pay_link_but_never_the_final(env):
    tx_id = send(env)
    tx = db.get_tx(tx_id)
    assert tx["status"] == "sent" and tx["title"] == "Lot 7 Grading"
    assert tx["link_id"] == "plink_1" and tx["link_url"].startswith("https://buy.stripe.com/")
    original = env["pdf"].read_bytes()
    assert tx["sha256"] == hashlib.sha256(original).hexdigest()

    (msg,) = env["smtp"].sent
    assert msg["To"] == "client@example.com" and "pat@acmecivil.test" in msg["From"]
    assert msg["Reply-To"] == "pat@acmecivil.test"
    body = msg.get_body(("plain",)).get_content()
    assert tx["link_url"] in body and tx["sha256"] in body and "$1,500.00" in body and "Hi Jo Smith," in body
    (name, data), = attachments(msg)
    assert name.startswith("PREVIEW") and data != original
    assert activity_texts(tx_id)[0].startswith("Sent to client@example.com from pat@acmecivil.test ($1,500.00, 3 pages)")


@pytest.mark.parametrize("over, message", [
    ({"to_email": "nope"}, "client's email"),
    ({"price": "0.10"}, r"at least \$0\.50"),
    ({"price": "abc"}, "price like"),
    ({"from_email": "other@x.test"}, "isn't set up"),
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


def test_requires_stripe_key(env):
    config.set_secret("stripe_secret_key", "")
    with pytest.raises(sender.SendError, match="Stripe key"):
        send(env, methods=["stripe"])


def test_large_pdf_needs_share_link(env, monkeypatch):
    monkeypatch.setattr(config, "ATTACH_LIMIT_BYTES", 100)
    with pytest.raises(sender.NeedShareLink):
        send(env)
    tx_id = send(env, share_link="https://onedrive.live.com/x")
    assert db.get_tx(tx_id)["share_link"] == "https://onedrive.live.com/x"


def test_failed_email_rolls_back_and_disables_link(env):
    env["smtp"].fail_with = smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(sender.SendError, match="Email login failed"):
        send(env)
    assert db.list_txs(["draft", "sent"]) == []
    assert env["fake"].deactivated == ["plink_1"]


# ------------------------------------------------------------------ watching Stripe
def test_paid_delivers_final_once_and_closes_link(env):
    tx_id = send(env)
    env["smtp"].sent.clear()
    watcher.check_all()
    assert env["smtp"].sent == []  # nobody paid yet

    env["fake"].pay("plink_1", 150000)
    notes = []
    summary = watcher.check_all(lambda t, m: notes.append(t))
    tx = db.get_tx(tx_id)
    assert tx["status"] == "paid" and tx["delivered_at"] and tx["amount_paid_cents"] == 150000
    assert summary["paid"] == 1 and summary["delivered"] == 1
    assert env["fake"].deactivated == ["plink_1"]
    assert notes == ["PAID: Lot 7 Grading"]
    (msg,) = env["smtp"].sent
    (name, data), = attachments(msg)
    assert name == "Lot 7 Grading.pdf" and data == env["pdf"].read_bytes()

    watcher.check_all()
    assert len(env["smtp"].sent) == 1  # never delivered twice
    log = activity_texts(tx_id)
    assert "Paid $1,500.00 by Stripe" in log and "Final plans emailed to client@example.com" in log


def test_bank_transfer_pending_then_paid(env):
    tx_id = send(env)
    env["smtp"].sent.clear()
    env["fake"].pay("plink_1", 150000, state="pending")
    watcher.check_all()
    watcher.check_all()
    assert db.get_tx(tx_id)["status"] == "pending"
    assert [m["Subject"] for m in env["smtp"].sent] == ["Bank transfer received: Lot 7 Grading"]
    env["fake"].pay("plink_1", 150000)
    watcher.check_all()
    assert db.get_tx(tx_id)["status"] == "paid"


def test_bank_transfer_failure_issues_new_link(env):
    tx_id = send(env)
    env["smtp"].sent.clear()
    env["fake"].pay("plink_1", 150000, state="failed")
    watcher.check_all()
    watcher.check_all()
    tx = db.get_tx(tx_id)
    assert tx["status"] == "sent" and tx["link_id"] == "plink_2" and tx["old_link_ids"] == ["plink_1"]
    (msg,) = env["smtp"].sent
    assert "didn't go through" in msg["Subject"] and "plink_2" in msg.get_body(("plain",)).get_content()


def test_underpayment_never_delivers(env):
    tx_id = send(env)
    env["smtp"].sent.clear()
    env["fake"].pay("plink_1", 100)
    watcher.check_all()
    watcher.check_all()
    assert db.get_tx(tx_id)["status"] == "sent" and env["smtp"].sent == []
    assert sum("mismatch" in t for t in activity_texts(tx_id)) == 1


def test_failed_delivery_retries_and_logs_once(env):
    tx_id = send(env)
    env["smtp"].sent.clear()
    env["fake"].pay("plink_1", 150000)
    env["smtp"].fail_with = smtplib.SMTPServerDisconnected("down")
    watcher.check_all()
    watcher.check_all()
    tx = db.get_tx(tx_id)
    assert tx["status"] == "paid" and not tx["delivered_at"] and "down" in tx["delivery_error"]
    assert sum("Could not email" in t for t in activity_texts(tx_id)) == 1
    env["smtp"].fail_with = None
    watcher.check_all()
    assert db.get_tx(tx_id)["delivered_at"] and len(env["smtp"].sent) == 1


def test_share_link_delivery_has_no_attachment(env, monkeypatch):
    monkeypatch.setattr(config, "ATTACH_LIMIT_BYTES", 100)
    send(env, share_link="https://dropbox.com/s/abc")
    env["smtp"].sent.clear()
    env["fake"].pay("plink_1", 150000)
    watcher.check_all()
    (msg,) = env["smtp"].sent
    assert attachments(msg) == [] and "https://dropbox.com/s/abc" in msg.get_body(("plain",)).get_content()


def test_refund_detected(env):
    tx_id = send(env)
    env["fake"].pay("plink_1", 150000)
    watcher.check_all()
    db.set_flag(db.get_tx(tx_id), "refund_checked_at", "2000-01-01T00:00:00+00:00")
    env["fake"].refunds["pi_plink_1"] = "refunded"
    watcher.check_all()
    assert db.get_tx(tx_id)["status"] == "refunded"
    assert "Refunded in Stripe (the plans had already been emailed)" in activity_texts(tx_id)


def test_resend(env):
    tx_id = send(env)
    sender.resend(tx_id)
    assert env["smtp"].sent[-1]["Subject"].startswith("Plans ready for review")
    env["fake"].pay("plink_1", 150000)
    watcher.check_all()
    sender.resend(tx_id)
    assert env["smtp"].sent[-1]["Subject"].startswith("Your final plans")
    assert "Final plans re-sent to client@example.com" in activity_texts(tx_id)


# ------------------------------------------------------------------ Stripe parameters (real module, mocked HTTP)
def test_payment_link_is_single_use_with_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("PLANSENDER_HOME", str(tmp_path))
    import keyring
    from desktop import payments
    from desktop.tests.conftest import MemoryKeyring

    keyring.set_keyring(MemoryKeyring())
    config.set_secret("stripe_secret_key", "sk_test_x")
    calls = {}

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    monkeypatch.setattr("stripe.Price.create", lambda **kw: calls.setdefault("price", kw) and Obj(id="price_1"))
    monkeypatch.setattr("stripe.PaymentLink.create",
                        lambda **kw: calls.setdefault("link", kw) and Obj(id="plink_1", url="https://buy.stripe.com/t"))
    tx = {"id": 7, "title": "Lot 7", "client_email": "a+b@x.com", "currency": "usd", "price_cents": 1234, "sha256": "ab"}
    link_id, url = payments.create_payment_link(tx)
    assert link_id == "plink_1" and url == "https://buy.stripe.com/t?prefilled_email=a%2Bb%40x.com"
    assert calls["price"]["unit_amount"] == 1234
    link = calls["link"]
    assert link["restrictions"] == {"completed_sessions": {"limit": 1}}
    assert link["metadata"]["plansender_tx"] == "7"
    assert link["payment_method_types"] == ["card", "us_bank_account"]


# ------------------------------------------------------------------ window bridge (api.py)
def test_api_accounts_transactions_and_csv(env, tmp_path):
    from desktop.api import Api, write_csv

    a = Api()
    r = a.save_account({"preset": "gmail", "email": "Office@AcmeCivil.test", "name": "Acme", "password": "abcd efgh ijkl mnop"})
    assert r["ok"]
    assert config.smtp_password("office@acmecivil.test") == "abcdefghijklmnop"  # Gmail app-password spaces stripped
    assert not a.save_account({"preset": "custom", "email": "x@y.test", "password": "p"})["ok"]  # custom needs a host

    tx_id = send(env, from_email="office@acmecivil.test")
    env["fake"].pay("plink_1", 150000)
    assert a.check_now()["ok"]
    data = a.transactions()
    assert data["totals"] == {"sent": 1, "paid": 1, "collected": "$1,500.00", "waiting": "$0.00",
                              "by_method": ["Stripe $1,500.00"]}
    item = data["items"][0]
    assert item["label"] == "Paid via Stripe · plans sent" and item["activity"][0]["text"].startswith("Final plans emailed")

    out = tmp_path / "log.csv"
    write_csv(out)
    rows = out.read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].startswith("Date,Time,Project") and len(rows) == 1 + len(db.activity(tx_id))
    assert "Paid $1,500.00 by Stripe" in out.read_text(encoding="utf-8-sig")

    assert a.remove_account("office@acmecivil.test")["ok"]
    assert config.smtp_password("office@acmecivil.test") == ""


def test_api_send_reports_errors_instead_of_raising(env):
    from desktop.api import Api

    r = Api().send({"from_email": "pat@acmecivil.test", "to_email": "bad", "price": "10", "pdf_path": str(env["pdf"])})
    assert r == {"ok": False, "error": "Enter the client's email address."}
