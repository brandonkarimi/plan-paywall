"""Send plans behind a pay wall: the client gets a watermarked preview and payment instructions; the final PDF
stays on this PC until the engineer marks the transaction paid."""
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path

from desktop import config, db, mailer
from plancore.pdfinfo import inspect_pdf
from plancore.preview import build_preview

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PREVIEW_EMAIL_LIMIT = 12 * 1024 * 1024  # keep the preview attachment comfortably under mail limits


class SendError(Exception):
    pass


class NeedShareLink(SendError):
    pass


def _price_cents(price) -> int:
    try:
        value = Decimal(str(price).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        raise SendError("Enter a price like 1500 or 1500.00.")
    if value <= 0:
        raise SendError("Enter a price greater than $0.")
    return int((value * 100).quantize(Decimal("1")))


WATERMARK_TEXT = "PREVIEW"


def make_preview(final_path: Path, out_path: Path, text: str, settings) -> Path:
    """Build the preview, stepping the resolution down if it would be too big to email."""
    data = final_path.read_bytes()
    for dpi in sorted({settings.get("preview_dpi", 100), 75, 60, 45}, reverse=True):
        preview = build_preview(data, text, dpi=dpi, jpeg_quality=65, seal_box=settings.get("seal_box") or None)
        if len(preview) <= PREVIEW_EMAIL_LIMIT:
            break
    out_path.write_bytes(preview)
    if len(preview) > PREVIEW_EMAIL_LIMIT:
        raise SendError("This plan set is too large to email a preview of. Try splitting it into smaller sets.")
    return out_path


INVOICE_LIMIT_BYTES = 6 * 1024 * 1024


def send_plans(*, from_email, to_email, price, pdf_path, client_name="", title="", share_link="", methods=None,
               invoice_path="", settings=None):
    """Returns the new transaction id. Raises SendError (NeedShareLink for very large PDFs).

    methods: "zelle" and/or "venmo" (default: every method enabled in Settings).
    invoice_path: optional invoice PDF, attached to the email as-is. With an invoice the price is optional
    (0 = "see invoice").
    """
    settings = settings or config.load_settings()
    from_email, to_email = from_email.strip(), to_email.strip()
    client_name, title, share_link = client_name.strip(), title.strip(), share_link.strip()
    methods = [m for m in (methods if methods is not None else config.enabled_methods(settings))
               if m in config.PAYMENT_METHODS]
    if not methods:
        raise SendError("Choose how the client can pay (set up Zelle or Venmo in Settings).")
    if "zelle" in methods and not settings["zelle"].get("recipient"):
        raise SendError("Add your Zelle email or phone in Settings.")
    if "venmo" in methods and not settings["venmo"].get("username"):
        raise SendError("Add your Venmo username in Settings.")
    if not EMAIL_RE.match(to_email):
        raise SendError("Enter the client's email address.")
    invoice = Path(invoice_path) if invoice_path else None
    if invoice is not None:
        if not invoice.is_file():
            raise SendError("The invoice file can't be found. Choose it again.")
        with invoice.open("rb") as f:
            try:
                inv_info = inspect_pdf(f)
            except ValueError as exc:
                raise SendError(f"Invoice: {exc}")
        if inv_info["size_bytes"] > INVOICE_LIMIT_BYTES:
            raise SendError("The invoice PDF is too large to email (over 6 MB).")
    if invoice is not None and not str(price or "").strip():
        price_cents = 0  # amount is on the invoice
    elif not str(price or "").strip():
        raise SendError("Enter the price, or attach an invoice to fill it in.")
    else:
        price_cents = _price_cents(price)
    src = Path(pdf_path or "")
    if not src.is_file():
        raise SendError("Choose the plan PDF to send.")
    with src.open("rb") as f:
        try:
            info = inspect_pdf(f)
        except ValueError as exc:
            raise SendError(str(exc))
    if share_link and not share_link.lower().startswith("https://"):
        raise SendError("The download link must start with https://")
    if info["size_bytes"] > config.ATTACH_LIMIT_BYTES and not share_link:
        raise NeedShareLink(
            f"This PDF is {info['size_bytes'] / 1e6:.0f} MB, too big to email as an attachment. Upload it to "
            "OneDrive, Dropbox or Google Drive, create a share link, and paste it below. The link is only "
            "emailed to the client after they pay. Don't share it anywhere else.")
    if not config.account_for(from_email, settings):
        raise SendError(f"{from_email or 'The From address'} isn't set up. Add it in Settings → Email accounts.")
    if not config.smtp_password(from_email):
        raise SendError(f"No password saved for {from_email}. Add it in Settings → Email accounts.")

    title = title or src.stem.replace("_", " ").strip()
    tx_id = db.create_tx(
        title=title, client_name=client_name, client_email=to_email, from_email=from_email,
        price_cents=price_cents, status="draft", original_name=src.name, share_link=share_link,
        sha256=info["sha256"], pages=info["page_count"], size_bytes=info["size_bytes"], sheets=info["sheet_index"],
        methods=methods,
    )
    db.update_tx(tx_id, ref_code=f"PLN-{tx_id}")
    folder = config.files_dir(tx_id)
    try:
        final = folder / "final.pdf"
        shutil.copy2(src, final)
        preview = make_preview(final, folder / "preview.pdf", WATERMARK_TEXT, settings)
        db.update_tx(tx_id, final_path=str(final), preview_path=str(preview))
        if invoice is not None:
            stored_invoice = folder / "invoice.pdf"
            shutil.copy2(invoice, stored_invoice)
            db.update_tx(tx_id, invoice_path=str(stored_invoice))
        tx = db.get_tx(tx_id)
        subject, text, html = mailer.invite(tx, settings)
        mailer.send(from_email, to_email, subject, text, html, attachments=invite_attachments(tx), settings=settings)
    except Exception as exc:
        db.delete_tx(tx_id)
        shutil.rmtree(folder, ignore_errors=True)
        if isinstance(exc, SendError):
            raise
        raise SendError(str(exc)) from exc

    db.update_tx(tx_id, status="sent", sent_at=db.now())
    db.log(tx_id, f"Preview{' and invoice' if invoice is not None else ''} sent to {to_email}")
    return tx_id


def invite_attachments(tx):
    """Invoice first (if any), then the watermarked preview."""
    files = []
    if tx.get("invoice_path"):
        files.append((tx["invoice_path"], f"Invoice - {tx['title']}.pdf"))
    files.append((tx["preview_path"], f"PREVIEW - {tx['title']}.pdf"))
    return files


def mark_paid(tx_id, method, amount=None, allow_short=False, settings=None):
    """The engineer confirms the payment arrived; the final plans are emailed straight away.

    Returns True if the plans were emailed (False = marked paid but the email failed; use Resend).
    """
    settings = settings or config.load_settings()
    tx = db.get_tx(int(tx_id))
    if tx is None:
        raise SendError("Transaction not found.")
    if tx["status"] == "paid":
        raise SendError("This one is already marked paid.")
    if method not in config.MANUAL_METHODS:
        raise SendError("Choose how the client paid.")
    if amount in (None, ""):
        cents = tx["price_cents"] or None  # price "on invoice" and no amount typed: amount unknown
    else:
        cents = _price_cents(amount)
    if cents is not None and cents < tx["price_cents"] and not allow_short:
        raise SendError(f"That's less than the {mailer.money(tx['price_cents'])} price. Tick “release anyway” to "
                        "send the plans for a partial payment.")
    db.update_tx(tx["id"], status="paid", paid_at=db.now(), amount_paid_cents=cents, paid_method=method)
    db.log(tx["id"], f"Marked as paid: {mailer.money(cents)} by {method}" if cents else f"Marked as paid by {method}")
    return deliver(db.get_tx(tx["id"]), settings)


def deliver(tx, settings=None):
    """Email the final plans. Returns True on success; a failure is logged and shown in the app."""
    settings = settings or config.load_settings()
    try:
        attachments = []
        if not tx["share_link"]:
            if not Path(tx["final_path"]).is_file():
                raise mailer.MailError("The final PDF is missing from the app's files folder.")
            attachments = [(tx["final_path"], f"{tx['title']}.pdf")]
        subject, text, html = mailer.files(tx, settings)
        mailer.send(tx["from_email"], tx["client_email"], subject, text, html, attachments, settings=settings)
    except Exception as exc:
        db.update_tx(tx["id"], delivery_error=str(exc))
        db.log(tx["id"], f"Couldn't email the full plans: {exc}")
        return False
    first = not tx["delivered_at"]
    db.update_tx(tx["id"], delivered_at=tx["delivered_at"] or db.now(), delivery_error="")
    db.log(tx["id"], ("Full plans emailed to " if first else "Full plans emailed again to ") + tx["client_email"])
    return True


def resend(tx_id, settings=None):
    """Re-send the payment instructions (unpaid) or the final plans (paid)."""
    settings = settings or config.load_settings()
    tx = db.get_tx(tx_id)
    if tx is None:
        raise SendError("Transaction not found.")
    if tx["status"] == "paid":
        if not deliver(tx, settings):
            raise SendError(db.get_tx(tx_id)["delivery_error"] or "Couldn't send.")
        return
    subject, text, html = mailer.invite(tx, settings)
    mailer.send(tx["from_email"], tx["client_email"], subject, text, html,
                attachments=invite_attachments(tx), settings=settings)
    db.log(tx_id, f"Preview emailed again to {tx['client_email']}")
