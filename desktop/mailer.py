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


def send(from_email, to, subject, text, html=None, attachments=(), settings=None):
    """attachments: iterable of (path, filename)."""
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


QR_SIZE = 240  # px, roughly; two cards side by side still fit the 600px message width
QR_CARD = {
    "zelle": ("Zelle", "#6d1ed4", "Scan in your banking app to pay"),
    "venmo": ("Venmo", "#008cff", "Scan with the Venmo app to pay"),
}


def qr_table(text, size=QR_SIZE):
    """Draw a QR code as plain HTML table cells (no image), so mail apps show it as part of the message:
    it can't be opened, downloaded or swiped through like a picture, yet scans exactly like the original."""
    import segno

    matrix = [list(row) for row in segno.make(text, error="m").matrix_iter(scale=1, border=3)]
    cell = max(3, round(size / len(matrix)))
    width = cell * len(matrix)
    rows = []
    for row in matrix:
        tds, x = [], 0
        while x < len(row):
            run = 1
            while x + run < len(row) and bool(row[x + run]) == bool(row[x]):
                run += 1
            tds.append(f"<td width={run * cell}" + (" bgcolor=#000>" if row[x] else ">") + "</td>")  # kept terse:
            # a QR code is ~1,000 cells, and Gmail cuts off emails over ~100 KB
            x += run
        # One table per row: rows have different run lengths, so they can't share a column grid.
        rows.append(f"<table cellpadding=0 cellspacing=0 border=0 width={width}><tr height={cell}>{''.join(tds)}</tr></table>")
    return (f"<div style='width:{width}px;margin:0 auto;background:#ffffff;font-size:0;line-height:0'>"
            + "".join(rows) + "</div>")


def _qr_block(tx, settings):
    from desktop import images

    cells = []
    for method in tx.get("methods") or []:
        text = images.qr_text(f"{method}_qr")
        if not text:
            continue
        label, color, caption = QR_CARD[method]
        if method == "zelle":
            z = settings.get("zelle") or {}
            who = " · ".join(x for x in (z.get("name"), z.get("recipient")) if x)
        else:
            user = (settings.get("venmo") or {}).get("username", "").strip().lstrip("@")
            who = f"@{user}" if user else ""
        cells.append(
            "<td style='padding:0 14px 12px 0;vertical-align:top'>"
            "<div style='border:1px solid #e5e7eb;border-radius:12px;padding:14px 16px 12px;text-align:center'>"
            f"<div style='font-size:20px;font-weight:700;color:{color};margin-bottom:10px'>{label}</div>"
            + qr_table(text)
            + f"<div style='font-size:13px;color:#444;margin-top:10px'>{caption}</div>"
            + (f"<div style='font-size:13px;font-weight:600;color:#16191d'>{escape(who)}</div>" if who else "")
            + "</div></td>")
    if not cells:
        return None
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
