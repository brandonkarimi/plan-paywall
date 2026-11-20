"""Sending an invoice PDF alongside the watermarked preview."""
import io

import pytest
from reportlab.pdfgen import canvas

from desktop import db, sender
from desktop.tests.test_desktop import attachments, body, log_texts, send
from plancore.invoice import find_amount_cents


def invoice_pdf(lines):
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 780
    for line in lines:
        c.drawString(72, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


@pytest.fixture
def invoice(env):
    path = env["tmp"] / "Invoice_1042.pdf"
    path.write_bytes(invoice_pdf(["INVOICE #1042", "Grading plan   $1,200.00", "Site visit   $300.00",
                                  "Subtotal   $1,500.00", "Total   $1,500.00", "Balance Due   $1,500.00"]))
    return path


@pytest.mark.parametrize("lines, cents", [
    (["Subtotal $1,500.00", "Total $1,500.00", "Balance Due $1,500.00"], 150000),
    (["Total $2,000.00", "Deposit paid -$500.00", "Amount Due: $1,500.00"], 150000),
    (["Engineering services 950.00", "TOTAL 950.00"], 95000),
    (["Invoice total: USD 12,345.67"], 1234567),
    (["Amount due", "$3,250.00"], 325000),
    (["Subtotal $400.00"], None),
    (["Thanks for your business"], None),
])
def test_find_amount(lines, cents):
    assert find_amount_cents(invoice_pdf(lines)) == cents


def test_invoice_without_price(env, invoice):
    tx_id = send(env, price="", invoice_path=str(invoice))
    tx = db.get_tx(tx_id)
    assert tx["price_cents"] == 0 and tx["invoice_path"].endswith("invoice.pdf")

    (msg,) = env["smtp"].sent
    names = [n for n, _ in attachments(msg)]
    assert names == ["Invoice - Lot 7 Grading.pdf", "PREVIEW - Lot 7 Grading.pdf"]
    assert attachments(msg)[0][1] == invoice.read_bytes()
    text = body(msg)
    assert "along with your invoice. To receive the full PDF, please pay the invoice." in text
    assert "Zelle: send the invoice amount to pat@acmecivil.test" in text
    assert f"https://venmo.com/AcmeCivil-Eng?txn=pay&note=PLN-{tx_id}" in text  # no amount pre-filled
    assert "amount on invoice" in log_texts(tx_id)[0] and "invoice attached" in log_texts(tx_id)[0]


def test_invoice_with_price(env, invoice):
    send(env, price="1500", invoice_path=str(invoice))
    text = body(env["smtp"].sent[0])
    assert "along with your invoice for $1,500.00." in text and "Zelle: send $1,500.00" in text


def test_price_still_required_without_invoice(env):
    with pytest.raises(sender.SendError, match="Enter the price, or attach an invoice"):
        send(env, price="")


def test_bad_invoice_rejected(env):
    bad = env["tmp"] / "invoice.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(sender.SendError, match="Invoice"):
        send(env, price="", invoice_path=str(bad))
    assert db.list_txs() == []


def test_mark_paid_with_amount_on_invoice(env, invoice):
    t1 = send(env, price="", invoice_path=str(invoice))
    assert sender.mark_paid(t1, "Zelle")  # no amount typed: fine, amount unknown
    assert db.get_tx(t1)["amount_paid_cents"] is None and "Paid via Zelle (confirmed by you)" in log_texts(t1)
    t2 = send(env, price="", invoice_path=str(invoice), to_email="b@example.com")
    assert sender.mark_paid(t2, "Venmo", "1500")
    assert db.get_tx(t2)["amount_paid_cents"] == 150000


def test_resend_includes_invoice_and_api_view(env, invoice):
    from desktop.api import Api

    tx_id = send(env, price="", invoice_path=str(invoice))
    sender.resend(tx_id)
    assert [n for n, _ in attachments(env["smtp"].sent[-1])][0].startswith("Invoice")
    item = Api().transactions()["items"][0]
    assert item["price"] == "On invoice" and item["price_value"] == "" and item["has_invoice"]
