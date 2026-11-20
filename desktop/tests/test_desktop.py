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


def test_delete_one_transaction_keeps_the_others(env):
    from desktop.api import Api
    a = Api()
    keep, gone = send(env, title="Keep me"), send(env, title="Delete me")
    gone_files = config.data_dir() / "files" / str(gone)
    assert gone_files.is_dir() and db.activity(gone)
    r = a.delete_tx(gone)
    assert r["ok"] and "Delete me" in r["message"]
    assert db.get_tx(gone) is None and not db.activity(gone) and not gone_files.exists()
    assert db.get_tx(keep) and (config.data_dir() / "files" / str(keep)).is_dir()
    assert [t["title"] for t in a.transactions()["items"]] == ["Keep me"]
    assert not a.delete_tx(gone)["ok"]
    assert send(env) > gone  # ids are never reused


def test_every_email_carries_the_signature_with_linked_logo(env):
    from desktop import mailer
    from desktop.api import Api
    logo_url = "https://lh3.googleusercontent.com/abc123=s200"
    assert Api().save_settings({"signature": {
        "name": "Pat Lee, MSCE, PE", "firm": "", "phone": "555-123-4567", "email": "pat@acmecivil.test",
        "license": "C12345", "street": "1 Main St #2", "city": "Springfield CA 90000", "logo_url": logo_url}})["ok"]

    tx_id = send(env)
    sender.mark_paid(tx_id, "Zelle")
    for msg in env["smtp"].sent:  # preview email and final-plans email
        text = body(msg)
        assert "Pat Lee, MSCE, PE\nCell: 555-123-4567\npat@acmecivil.test\nPE Lic#: C12345\n1 Main St #2\nSpringfield CA 90000" in text
        assert "P.E." not in text and "googleusercontent" not in text
        html = msg.get_body(("html",)).get_content()
        assert f"src='{logo_url}'" in html and "tel:5551234567" in html and "mailto:pat@acmecivil.test" in html
        # The logo is linked, never carried: no picture inside the email for Gmail to list as an attachment.
        assert not [p for p in msg.walk() if p.get_content_maintype() == "image"]
        assert "cid:" not in html

    # Without a logo link there's no picture at all.
    assert Api().save_settings({"signature": {"logo_url": ""}})["ok"]
    env["smtp"].sent.clear()
    send(env)
    assert "<img" not in env["smtp"].sent[0].get_body(("html",)).get_content()


def test_logo_link_must_be_a_web_address(env):
    from desktop.api import Api
    from desktop import mailer
    for bad in ("C:\\logo.png", "javascript:alert(1)", "logo.png", "https://x.test/a b.png"):
        r = Api().save_settings({"signature": {"logo_url": bad}})
        assert not r["ok"] and "https://" in r["error"]
        assert "<img" not in mailer.signature_html({"signature": {"name": "Pat", "logo_url": bad}})
    assert Api().save_settings({"signature": {"logo_url": "https://mkd.test/logo.png"}})["ok"]
    assert config.load_settings()["signature"]["name"] == "Pat Lee"  # other signature fields are kept


def test_signature_preview_and_empty_signature(env):
    from desktop.api import Api
    from desktop import mailer
    html = Api().signature_preview({"name": "Pat <Lee>", "street": "1 Main St"})
    assert "Pat &lt;Lee&gt;" in html and "google.com/maps" in html
    assert mailer.signature_html({"signature": {}}) == ""


def test_page_cache_is_cleared_but_other_webview_data_kept(tmp_path):
    from desktop.app import clear_page_cache
    default = tmp_path / "EBWebView" / "Default"
    for name in ("Cache", "Code Cache", "Local Storage"):
        (default / name).mkdir(parents=True)
        (default / name / "f").write_text("x")
    clear_page_cache(tmp_path)
    assert not (default / "Cache").exists() and not (default / "Code Cache").exists()
    assert (default / "Local Storage" / "f").exists()
    clear_page_cache(tmp_path / "missing")   # no error when nothing is there yet


def test_theme_defaults_to_light_and_is_remembered(env):
    from desktop.api import Api
    a = Api()
    assert a.state()["settings"]["theme"] == "light"
    assert a.set_theme("dark")["ok"]
    assert config.load_settings()["theme"] == "dark"
    assert a.save_settings({"signature": {"name": "Pat"}})["ok"]   # other saves keep the choice
    assert config.load_settings()["theme"] == "dark"
    assert not a.set_theme("purple")["ok"]
    assert config.load_settings()["theme"] == "dark"


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


def _png(path, color=(30, 60, 110), size=(60, 40), bg="white"):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, bg)
    ImageDraw.Draw(img).rectangle([10, 10, 40, 30], fill=color)
    img.save(path)
    return path


def _qr(path, text):
    import segno

    segno.make(text).save(str(path), scale=8)
    return path


ZELLE_LINK = "https://enroll.zellepay.com/qr-codes?data=test"
VENMO_LINK = "https://venmo.com/code?user_id=123"


def _scan_html_qr(html):
    """Rebuild the QR grid drawn in the email and scan it, as a phone would."""
    import re

    import zxingcpp
    from PIL import Image

    found = []
    for block in re.findall(r"<div style='width:\d+px;margin:0 auto.*?</div>", html, re.S):
        rows = re.findall(r"<tr height=(\d+)>(.*?)</tr>", block)
        cell = int(rows[0][0])
        width = sum(int(w) for w, _ in re.findall(r"<td width=(\d+)( bgcolor=#000)?>", rows[0][1]))
        img = Image.new("L", (width, cell * len(rows)), 255)
        for y, (_, tds) in enumerate(rows):
            x = 0
            for w, dark in re.findall(r"<td width=(\d+)( bgcolor=#000)?>", tds):
                if dark:
                    img.paste(0, (x, y * cell, x + int(w), (y + 1) * cell))
                x += int(w)
        found += [r.text for r in zxingcpp.read_barcodes(img.resize((width * 2, img.height * 2)))]
    return found


def test_qr_codes_are_drawn_into_the_email_not_attached(env):
    from desktop import images

    images.store("zelle_qr", _qr(env["tmp"] / "z.png", ZELLE_LINK))
    images.store("venmo_qr", _qr(env["tmp"] / "v.png", VENMO_LINK))
    sender.send_plans(from_email="pat@acmecivil.test", to_email="client@example.test", price="100",
                      pdf_path=env["pdf"], settings=config.load_settings())
    msg = env["smtp"].sent[-1]
    html = msg.get_body(("html",)).get_content()
    assert _scan_html_qr(html) == [ZELLE_LINK, VENMO_LINK]
    assert "<a " not in html and "<img" not in html  # nothing to click, open or swipe through
    assert not [p for p in msg.walk() if p.get_content_maintype() == "image"]
    assert {a.get_content_type() for a in msg.iter_attachments()} == {"application/pdf"}
    assert len(html.encode()) < 90_000  # Gmail cuts off messages over ~100 KB


def test_only_the_chosen_methods_qr_code_is_shown(env):
    from desktop import images

    images.store("zelle_qr", _qr(env["tmp"] / "z.png", ZELLE_LINK))
    images.store("venmo_qr", _qr(env["tmp"] / "v.png", VENMO_LINK))
    sender.send_plans(from_email="pat@acmecivil.test", to_email="client@example.test", price="100",
                      pdf_path=env["pdf"], methods=["venmo"], settings=config.load_settings())
    assert _scan_html_qr(env["smtp"].sent[-1].get_body(("html",)).get_content()) == [VENMO_LINK]


def test_no_qr_codes_without_pictures(env):
    sender.send_plans(from_email="pat@acmecivil.test", to_email="client@example.test", price="100",
                      pdf_path=env["pdf"], settings=config.load_settings())
    assert _scan_html_qr(env["smtp"].sent[-1].get_body(("html",)).get_content()) == []


def test_picture_without_a_qr_code_is_rejected(env):
    from desktop import images

    with pytest.raises(images.ImageError, match="QR code"):
        images.store("zelle_qr", _png(env["tmp"] / "plain.png"))
    assert config.image_path("zelle_qr") is None


def test_removing_a_qr_code_forgets_it(env):
    from desktop import images

    images.store("venmo_qr", _qr(env["tmp"] / "v.png", VENMO_LINK))
    assert images.qr_text("venmo_qr") == VENMO_LINK
    images.remove("venmo_qr")
    assert images.qr_text("venmo_qr") == "" and not list(config.images_dir().glob("venmo_qr.*"))


def test_watermark_logo_background_is_removed_and_used_on_previews(env):
    from PIL import Image

    from desktop import images
    from plancore.preview import build_preview

    stored = images.store("watermark", _png(env["tmp"] / "logo.png", size=(80, 60)))
    logo = Image.open(stored)
    assert logo.mode == "RGBA" and logo.size == (31, 21)  # white border trimmed off
    assert logo.getpixel((15, 10))[3] == 255
    from desktop.tests.conftest import make_pdf

    plain = build_preview(make_pdf(pages=1), "PREVIEW", dpi=30)
    branded = build_preview(make_pdf(pages=1), "PREVIEW", dpi=30, logo=stored)
    assert branded != plain and branded[:5] == b"%PDF-"


def test_bad_picture_is_rejected(env):
    from desktop import images

    bad = env["tmp"] / "notes.png"
    bad.write_text("not an image")
    with pytest.raises(images.ImageError):
        images.store("zelle_qr", bad)
    assert config.image_path("zelle_qr") is None


def test_qr_code_alone_is_enough_to_offer_a_method(env):
    from desktop import images

    settings = config.load_settings()
    settings["zelle"] = {"enabled": True, "recipient": "", "name": ""}
    settings["venmo"] = {"enabled": True, "username": ""}
    config.save_settings(settings)
    assert config.enabled_methods() == []
    with pytest.raises(sender.SendError, match="QR code"):
        sender.send_plans(from_email="pat@acmecivil.test", to_email="client@example.test", price="100",
                          pdf_path=env["pdf"], methods=["zelle"], settings=config.load_settings())

    images.store("zelle_qr", _qr(env["tmp"] / "z.png", ZELLE_LINK))
    images.store("venmo_qr", _qr(env["tmp"] / "v.png", VENMO_LINK))
    assert config.enabled_methods() == ["zelle", "venmo"]
    sender.send_plans(from_email="pat@acmecivil.test", to_email="client@example.test", price="100",
                      pdf_path=env["pdf"], settings=config.load_settings())
    msg = env["smtp"].sent[-1]
    text = msg.get_body(("plain",)).get_content()
    assert "Zelle: send $100.00 using the QR code below" in text
    assert "Venmo: send $100.00 using the QR code below" in text
    assert _scan_html_qr(msg.get_body(("html",)).get_content()) == [ZELLE_LINK, VENMO_LINK]


def test_settings_accept_a_method_with_only_a_qr_code(env):
    from desktop import images
    from desktop.api import Api

    r = Api().save_settings({"zelle": {"enabled": True, "recipient": "", "name": ""}})
    assert not r["ok"] and "QR code" in r["error"]
    images.store("zelle_qr", _qr(env["tmp"] / "z.png", ZELLE_LINK))
    assert Api().save_settings({"zelle": {"enabled": True, "recipient": "", "name": ""}})["ok"]


def _zelle_link(token):
    import base64
    import json

    data = base64.urlsafe_b64encode(json.dumps({"token": token, "name": "ACME"}).encode()).decode().rstrip("=")
    return f"https://enroll.zellepay.com/qr-codes?data={data}"


@pytest.mark.parametrize("token, shown", [("5551234567", "555-123-4567"), ("15551234567", "555-123-4567"),
                                          ("pay@acme.test", "pay@acme.test")])
def test_zelle_contact_is_read_from_the_qr_code(token, shown):
    from desktop.mailer import _zelle_contact

    assert _zelle_contact(_zelle_link(token)) == shown
    assert _zelle_contact("https://example.test/not-zelle") == ""


def test_cards_show_contact_and_are_the_same_size(env):
    import re

    from desktop import images

    settings = config.load_settings()
    settings["zelle"] = {"enabled": True, "recipient": "", "name": ""}
    config.save_settings(settings)
    images.store("zelle_qr", _qr(env["tmp"] / "z.png", _zelle_link("5551234567") + "&long=" + "x" * 80))
    images.store("venmo_qr", _qr(env["tmp"] / "v.png", VENMO_LINK))
    sender.send_plans(from_email="pat@acmecivil.test", to_email="client@example.test", price="100",
                      pdf_path=env["pdf"], settings=config.load_settings())
    msg = env["smtp"].sent[-1]
    html = msg.get_body(("html",)).get_content()
    assert "555-123-4567" in html and "@AcmeCivil-Eng" in html
    assert "Zelle: send $100.00 to 555-123-4567" in msg.get_body(("plain",)).get_content()
    assert len(set(re.findall(r"<div style='width:(\d+)px;margin:0 auto", html))) == 1  # QR codes match
    assert len(set(re.findall(r"<td width=(\d+) valign=top", html))) == 1  # so do the cards


def test_qr_cards_replace_the_pay_to_lines_in_the_email(env):
    from desktop import images

    images.store("zelle_qr", _qr(env["tmp"] / "z.png", ZELLE_LINK))
    sender.send_plans(from_email="pat@acmecivil.test", to_email="client@example.test", price="93.50",
                      pdf_path=env["pdf"], settings=config.load_settings())
    msg = env["smtp"].sent[-1]
    html, text = msg.get_body(("html",)).get_content(), msg.get_body(("plain",)).get_content()
    assert "whichever you prefer" in html
    assert "Zelle: send" not in html  # the Zelle card shows it instead
    assert "Venmo: send $93.50 to @AcmeCivil-Eng" in html  # no Venmo QR code, so the line stays
    assert "Zelle: send $93.50 to pat@acmecivil.test" in text and "Venmo: send" in text  # text-only mail apps
