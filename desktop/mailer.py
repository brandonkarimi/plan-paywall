"""Send email through the engineer's own account (SMTP), plus the message templates."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from pathlib import Path

from desktop import config


class MailError(Exception):
    pass


def send(from_email, to, subject, text, html=None, attachments=(), settings=None, inline_images=()):
    """attachments: iterable of (path, filename). inline_images: (content_id, png_path) pairs shown inside the HTML."""
    acct = config.account_for(from_email, settings)
    if not acct:
        raise MailError(f"{from_email} isn't set up yet. Add it in Settings → Email accounts.")
    password = config.smtp_password(acct["email"])
    if not password:
        raise MailError(f"No password saved for {acct['email']}. Add it in Settings → Email accounts.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((acct.get("name") or "", acct["email"]))
    msg["To"] = to
    msg["Reply-To"] = acct["email"]
    msg["Message-ID"] = make_msgid(domain=acct["email"].split("@")[-1])
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
        html_part = msg.get_payload()[-1]
        for cid, path in inline_images:
            html_part.add_related(Path(path).read_bytes(), maintype="image", subtype="png", cid=f"<{cid}>",
                                  disposition="inline", filename=Path(path).name)
    for path, filename in attachments:
        msg.add_attachment(Path(path).read_bytes(), maintype="application", subtype="pdf", filename=filename)

    host, port, security = acct["host"], int(acct["port"]), acct.get("security", "starttls")
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=90)
        else:
            server = smtplib.SMTP(host, port, timeout=90)
        with server:
            if security != "ssl":
                server.starttls(context=context)
            server.login(acct["email"], password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(f"Email login failed for {acct['email']}. Check the password/app password in Settings.") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailError(f"The email server refused the address {to}.") from exc
    except smtplib.SMTPDataError as exc:
        raise MailError(f"The email server rejected the message ({exc.smtp_code}): it may be too large.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"Couldn't send email via {host}: {exc}") from exc


# ------------------------------------------------------------------ templates
def money(cents) -> str:
    return f"${cents / 100:,.2f}"


def _signature(settings):
    sig = settings.get("signature") or {}
    lines = []
    if sig.get("name"):
        lines.append(sig["name"] + (", P.E." if sig.get("license") else ""))
    if sig.get("firm"):
        lines.append(sig["firm"])
    if sig.get("license"):
        lines.append(f"PE License #{sig['license']}")
    if sig.get("phone"):
        lines.append(sig["phone"])
    return lines


def _wrap(paragraphs, settings, button=None):
    """Build (text, html) from paragraphs; a paragraph may be a list of lines."""
    text_parts, html_parts = [], []
    for p in paragraphs:
        if isinstance(p, dict):  # a ready-made HTML block (e.g. QR codes) with no plain-text version
            html_parts.append(p["html"])
            continue
        lines = p if isinstance(p, list) else [p]
        text_parts.append("\n".join(lines))
        html_parts.append("<p style='margin:0 0 14px'>" + "<br>".join(escape(x) for x in lines) + "</p>")
        buttons = button if isinstance(button, list) else [button] if button else []
        for b in buttons:
            if p is b["after"]:
                text_parts.append(f"{b['label']}: {b['url']}")
                html_parts.append(
                    f"<p style='margin:4px 0 20px'><a href='{escape(b['url'])}' style='background:{b.get('color', '#1d4ed8')};"
                    f"color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600;"
                    f"display:inline-block'>{escape(b['label'])}</a></p>")
    sig = _signature(settings)
    if sig:
        text_parts.append("\n".join(sig))
        html_parts.append("<p style='margin:18px 0 0;color:#555'>" + "<br>".join(escape(x) for x in sig) + "</p>")
    text = "\n\n".join(text_parts) + "\n"
    html = ("<div style='font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#16191d;"
            "max-width:600px'>" + "".join(html_parts) + "</div>")
    return text, html


def _greeting(tx):
    return f"Hello {tx['client_name']}," if tx.get("client_name") else "Hello,"


def payment_images(tx, settings=None):
    """[(method, content_id, png_path)] for the QR codes to show in this transaction's preview email."""
    out = []
    for method in tx.get("methods") or []:
        path = config.image_path(f"{method}_qr")
        if path:
            out.append((method, f"{method}-qr@plansender", path))
    return out


def _qr_block(tx, settings):
    images = payment_images(tx, settings)
    if not images:
        return None
    cells = []
    for method, cid, _ in images:
        label = config.PAYMENT_METHODS[method]
        img = (f"<img src='cid:{cid}' alt='{label} QR code' width='180' style='display:block;width:180px;height:auto;"
               f"border:1px solid #e5e7eb;border-radius:8px'>")
        caption = "Scan with your banking app" if method == "zelle" else "Scan with the Venmo app"
        user = (settings.get("venmo") or {}).get("username", "").strip().lstrip("@")
        if method == "venmo" and user:
            img = f"<a href='https://venmo.com/u/{escape(user)}'>{img}</a>"
            caption += " or tap the code"
        cells.append(f"<td style='padding:0 18px 0 0;vertical-align:top;text-align:center'>{img}"
                     f"<div style='font-size:13px;color:#555;margin-top:6px'><b>{label}</b><br>{caption}</div></td>")
    return {"html": "<table role='presentation' cellpadding='0' cellspacing='0' style='margin:2px 0 18px'><tr>"
                    + "".join(cells) + "</tr></table>"}


def invite(tx, settings):
    """The preview email: greeting, one short message, how to pay. The preview (and invoice) are attached."""
    cents = tx["price_cents"]
    price = money(cents) if cents else ""
    pay = price or "the invoice amount"
    methods = tx.get("methods") or []
    if tx.get("invoice_path"):
        intro = (f"Here is a preview of your plans for {tx['title']}, along with your invoice"
                 + (f" for {price}" if price else "") + ". To receive the full PDF, please pay the invoice.")
    else:
        intro = f"Here is a preview of your plans for {tx['title']}. To receive the full PDF, please pay {price}."
    paragraphs = [_greeting(tx), intro]
    options = []
    if "zelle" in methods:
        z = settings.get("zelle") or {}
        name = f" ({z['name']})" if z.get("name") else ""
        if z.get("recipient"):
            options.append(f"Zelle: send {pay} to {z['recipient']}{name}")
        else:
            options.append(f"Zelle: send {pay} using the QR code below")
    if "venmo" in methods:
        user = (settings.get("venmo") or {}).get("username", "").strip().lstrip("@")
        options.append(f"Venmo: send {pay} to @{user}" if user else f"Venmo: send {pay} using the QR code below")
    if len(options) > 1:
        # Make it obvious the client only needs to use one of them.
        paragraphs.append(["You can pay with Zelle or Venmo, whichever you prefer:"] + [f"  • {o}" for o in options])
    else:
        paragraphs += options
    qr = _qr_block(tx, settings)
    if qr:
        paragraphs.append(qr)
    paragraphs.append("Once payment is received, the full PDF will be emailed to you.")
    text, html = _wrap(paragraphs, settings)
    return f"Preview: {tx['title']}", text, html


def files(tx, settings):
    """The final-plans email, sent once the engineer marks the payment received."""
    if tx.get("share_link"):
        body = f"Thank you for your payment. Download the full plans for {tx['title']} here:"
        button = {"after": body, "label": "Download plans", "url": tx["share_link"]}
    else:
        body = f"Thank you for your payment. The full plans for {tx['title']} are attached."
        button = None
    text, html = _wrap([_greeting(tx), body], settings, button)
    return f"Plans: {tx['title']}", text, html


def test_message(settings):
    text, html = _wrap(["This is a test from Plan Sender. Your email account is set up correctly."], settings)
    return "Plan Sender test email", text, html
