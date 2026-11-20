"""Send a plan PDF behind a pay wall: preview + Stripe Payment Link + email. The final PDF stays local."""
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path

from desktop import config, db, mailer, payments
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
    if value < Decimal("0.50"):
        raise SendError("The price must be at least $0.50 (Stripe's minimum).")
    return int((value * 100).quantize(Decimal("1")))


def watermark_text(client_name, client_email):
    return f"PRELIMINARY – NOT FOR CONSTRUCTION  ·  {client_name or client_email}"


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


def check_ready(from_email, settings):
    if not config.stripe_key():
        raise SendError("Add your Stripe key in Settings first.")
    if not config.account_for(from_email, settings):
        raise SendError(f"{from_email or 'The From address'} isn't set up. Add it in Settings → Email accounts.")
    if not config.smtp_password(from_email):
        raise SendError(f"No password saved for {from_email}. Add it in Settings → Email accounts.")


def send_plans(*, from_email, to_email, price, pdf_path, client_name="", title="", share_link="", settings=None):
    """Returns the new transaction id. Raises SendError (NeedShareLink for very large PDFs)."""
    settings = settings or config.load_settings()
    from_email, to_email = from_email.strip(), to_email.strip()
    client_name, title, share_link = client_name.strip(), title.strip(), share_link.strip()
    if not EMAIL_RE.match(to_email):
        raise SendError("Enter the client's email address.")
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
    check_ready(from_email, settings)

    title = title or src.stem.replace("_", " ").strip()
    tx_id = db.create_tx(
        title=title, client_name=client_name, client_email=to_email, from_email=from_email,
        price_cents=price_cents, status="draft", original_name=src.name, share_link=share_link,
        sha256=info["sha256"], pages=info["page_count"], size_bytes=info["size_bytes"], sheets=info["sheet_index"],
    )
    folder = config.files_dir(tx_id)
    link_id = ""
    try:
        final = folder / "final.pdf"
        shutil.copy2(src, final)
        preview = make_preview(final, folder / "preview.pdf", watermark_text(client_name, to_email), settings)
        db.update_tx(tx_id, final_path=str(final), preview_path=str(preview))

        link_id, link_url = payments.create_payment_link(db.get_tx(tx_id), allow_ach=settings.get("allow_ach", True))
        db.update_tx(tx_id, link_id=link_id, link_url=link_url)

        tx = db.get_tx(tx_id)
        subject, text, html = mailer.invite(tx, settings)
        mailer.send(from_email, to_email, subject, text, html,
                    attachments=[(preview, f"PREVIEW - {title}.pdf")], settings=settings)
    except Exception as exc:
        if link_id:
            try:
                payments.deactivate(link_id)
            except Exception:
                pass
        db.delete_tx(tx_id)
        shutil.rmtree(folder, ignore_errors=True)
        if isinstance(exc, SendError):
            raise
        raise SendError(str(exc)) from exc

    db.update_tx(tx_id, status="sent", sent_at=db.now())
    db.log(tx_id, f"Sent to {to_email} from {from_email} ({mailer.money(price_cents)}, "
                  f"{info['page_count']} page{'s' if info['page_count'] != 1 else ''})")
    return tx_id


def resend(tx_id, settings=None):
    """Re-send the pay link (unpaid) or the final plans (paid)."""
    from desktop import watcher

    settings = settings or config.load_settings()
    tx = db.get_tx(tx_id)
    if tx is None:
        raise SendError("Transaction not found.")
    if tx["status"] in ("paid", "refunded"):
        if tx["status"] == "refunded":
            raise SendError("This one was refunded; the plans aren't re-sent.")
        if not watcher.deliver(tx, settings=settings, force=True):
            raise SendError(db.get_tx(tx_id)["delivery_error"] or "Couldn't send.")
        return
    subject, text, html = mailer.invite(tx, settings)
    mailer.send(tx["from_email"], tx["client_email"], subject, text, html,
                attachments=[(tx["preview_path"], f"PREVIEW - {tx['title']}.pdf")], settings=settings)
    db.log(tx_id, "Pay link re-sent to client")
