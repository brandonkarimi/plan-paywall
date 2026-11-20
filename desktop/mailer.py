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


SIG_LINK = "color:#1a56db;text-decoration:underline"


def _signature(settings):
    """Plain-text signature lines."""
    sig = {k: str(v).strip() for k, v in (settings.get("signature") or {}).items()}
    lines = [sig[k] for k in ("name", "firm") if sig.get(k)]
    if sig.get("phone"):
        lines.append(f"Cell: {sig['phone']}")
    if sig.get("email"):
        lines.append(sig["email"])
    if sig.get("license"):
        lines.append(f"PE Lic#: {sig['license']}")
    lines += [sig[k] for k in ("street", "city") if sig.get(k)]
    return lines


def logo_url_ok(url) -> bool:
    return url.lower().startswith(("https://", "http://")) and " " not in url and len(url) <= 2000


def signature_html(settings):
    """The signature as it appears in emails: logo on the left, a dividing line, then the details.

    The logo is linked from where it's stored online, the way Gmail's own signatures do it, rather than
    carried inside the email (Gmail would list a carried picture as an attachment)."""
    from urllib.parse import quote

    sig = {k: str(v).strip() for k, v in (settings.get("signature") or {}).items()}
    logo_src = sig.get("logo_url") if logo_url_ok(sig.get("logo_url", "")) else None
    rows = []
    if sig.get("name"):
        rows.append(f"<div style='font-family:Cambria,Georgia,serif;font-weight:700;font-size:16px;color:#0f1f3d'>"
                    f"{escape(sig['name'])}</div>")
    if sig.get("firm"):
        rows.append(f"<div>{escape(sig['firm'])}</div>")
    if sig.get("phone"):
        digits = "".join(c for c in sig["phone"] if c.isdigit() or c == "+")
        rows.append(f"<div>Cell: <a href='tel:{escape(digits)}' style='{SIG_LINK}'>{escape(sig['phone'])}</a></div>")
    if sig.get("email"):
        rows.append(f"<div><a href='mailto:{escape(sig['email'])}' style='{SIG_LINK}'>{escape(sig['email'])}</a></div>")
    if sig.get("license"):
        rows.append(f"<div>PE Lic#: <span style='color:#1a56db'>{escape(sig['license'])}</span></div>")
    address = [sig[k] for k in ("street", "city") if sig.get(k)]
    if address:
        maps = "https://www.google.com/maps/search/?api=1&query=" + quote(", ".join(address))
        rows += [f"<div><a href='{escape(maps)}' style='{SIG_LINK}'>{escape(line)}</a></div>" for line in address]
    if not rows and not logo_src:
        return ""
    details = (f"<td style='padding:2px 0 2px 14px;border-left:2px solid #16191d;font-size:14px;line-height:1.7;"
               f"color:#16191d;vertical-align:middle'>{''.join(rows)}</td>") if rows else ""
    logo = (f"<td style='padding:0 16px 0 0;vertical-align:middle'><img src='{escape(logo_src)}' alt='"
            f"{escape(sig.get('firm') or 'Logo')}' height=90 style='height:90px;width:auto;display:block;border:0'></td>"
            ) if logo_src else ""
    return ("<table role='presentation' cellpadding=0 cellspacing=0 border=0 style='margin:22px 0 0;"
            f"font-family:Segoe UI,Arial,sans-serif'><tr>{logo}{details}</tr></table>")


def _para_html(lines):
    return "<p style='margin:0 0 14px'>" + "<br>".join(escape(x) for x in lines) + "</p>"


def _wrap(paragraphs, settings, button=None):
    """Build (text, html) from paragraphs; a paragraph may be a list of lines."""
    text_parts, html_parts = [], []
    for p in paragraphs:
        if isinstance(p, dict):  # separate plain-text and HTML versions (e.g. QR codes only exist in HTML)
            if p.get("text"):
                text_parts.append("\n".join(p["text"]))
            if p.get("html"):
                html_parts.append(p["html"] if isinstance(p["html"], str) else _para_html(p["html"]))
            continue
        lines = p if isinstance(p, list) else [p]
        text_parts.append("\n".join(lines))
        html_parts.append(_para_html(lines))
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
    html_parts.append(signature_html(settings))
    text = "\n\n".join(text_parts) + "\n"
    html = ("<div style='font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#16191d;"
            "max-width:600px'>" + "".join(html_parts) + "</div>")
    return text, html


def _greeting(tx):
    return f"Hello {tx['client_name']}," if tx.get("client_name") else "Hello,"


QR_SIZE = 200  # px, roughly; two cards side by side still fit the 600px message width
QR_CARD = {
    "zelle": ("Zelle", "#6d1ed4", "Scan in your banking app to pay"),
    "venmo": ("Venmo", "#008cff", "Scan with the Venmo app to pay"),
}


def _qr_matrix(text):
    import segno

    return [[bool(v) for v in row] for row in segno.make(text, error="m").matrix_iter(scale=1, border=0)]


def qr_table(matrix, size=QR_SIZE, modules=None):
    """Draw a QR matrix as plain HTML table cells (no image), so mail apps show it as part of the message:
    it can't be opened, downloaded or swiped through like a picture, yet scans exactly like the original.

    ``modules`` pads the quiet zone so codes of different densities come out the same size.
    """
    modules = max(modules or 0, len(matrix) + 6)
    pad = (modules - len(matrix)) // 2
    blank = [False] * modules
    rows = [blank] * pad + [[False] * pad + r + [False] * (modules - pad - len(r)) for r in matrix]
    rows += [blank] * (modules - len(rows))
    cell = max(3, round(size / modules))
    width = cell * modules
    out = []
    for row in rows:
        tds, x = [], 0
        while x < len(row):
            run = 1
            while x + run < len(row) and row[x + run] == row[x]:
                run += 1
            # Kept terse: a QR code is ~1,000 cells, and Gmail cuts off emails over ~100 KB.
            tds.append(f"<td width={run * cell}" + (" bgcolor=#000>" if row[x] else ">") + "</td>")
            x += run
        # One table per row: rows have different run lengths, so they can't share a column grid.
        out.append(f"<table cellpadding=0 cellspacing=0 border=0 width={width}><tr height={cell}>{''.join(tds)}</tr></table>")
    return (f"<div style='width:{width}px;margin:0 auto;background:#ffffff;font-size:0;line-height:0'>"
            + "".join(out) + "</div>")


def _zelle_contact(qr_link):
    """The phone/email a Zelle QR code pays, e.g. 555-123-4567 ("" if it can't be read)."""
    import base64
    import json
    from urllib.parse import parse_qs, urlparse

    try:
        data = parse_qs(urlparse(qr_link).query)["data"][0]
        token = str(json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))["token"]).strip()
    except (KeyError, IndexError, ValueError, TypeError):
        return ""
    digits = token[1:] if len(token) == 11 and token.startswith("1") else token
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if digits.isdigit() and len(digits) == 10 else token


CARD_WIDTH = 236  # px; both cards share it, and two side by side fit the 600px message width


def _qr_block(tx, settings):
    from desktop import images

    cards = []
    for method in tx.get("methods") or []:
        text = images.qr_text(f"{method}_qr")
        if not text:
            continue
        if method == "zelle":
            who = (settings.get("zelle") or {}).get("recipient", "").strip() or _zelle_contact(text)
        else:
            user = (settings.get("venmo") or {}).get("username", "").strip().lstrip("@")
            who = f"@{user}" if user else ""
        cards.append((method, who, _qr_matrix(text)))
    if not cards:
        return None, set()
    modules = max(len(m) for _, _, m in cards) + 6  # same size for every code
    cells = []
    for i, (method, who, matrix) in enumerate(cards):
        label, color, caption = QR_CARD[method]
        if i:
            cells.append("<td width=14 style='width:14px'></td>")
        cells.append(
            f"<td width={CARD_WIDTH} valign=top style='width:{CARD_WIDTH}px;border:1px solid #e5e7eb;"
            "border-radius:12px;padding:14px 0 12px;text-align:center;vertical-align:top'>"
            f"<div style='font-size:20px;font-weight:700;color:{color}'>{label}</div>"
            f"<div style='font-size:15px;font-weight:600;color:#16191d;margin:2px 0 10px'>{escape(who) or '&nbsp;'}</div>"
            + qr_table(matrix, modules=modules)
            + f"<div style='font-size:13px;color:#444;margin-top:10px'>{caption}</div></td>")
    html = ("<table role='presentation' cellpadding=0 cellspacing=0 style='margin:2px 0 18px;"
            "border-collapse:separate'><tr>" + "".join(cells) + "</tr></table>")
    return {"html": html}, {method for method, _, _ in cards}


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
    options = {}
    if "zelle" in methods:
        z = settings.get("zelle") or {}
        name = f" ({z['name']})" if z.get("name") else ""
        from desktop import images

        to = z.get("recipient") or _zelle_contact(images.qr_text("zelle_qr"))
        options["zelle"] = f"Zelle: send {pay} to {to}{name}" if to else f"Zelle: send {pay} using the QR code below"
    if "venmo" in methods:
        user = (settings.get("venmo") or {}).get("username", "").strip().lstrip("@")
        options["venmo"] = f"Venmo: send {pay} to @{user}" if user else f"Venmo: send {pay} using the QR code below"
    qr, shown = _qr_block(tx, settings)

    def how_to_pay(lines):
        if len(options) > 1:  # make it obvious the client only needs to use one of them
            return ["You can pay with Zelle or Venmo, whichever you prefer:"] + [f"  • {o}" for o in lines]
        return lines or [f"You can pay with {config.PAYMENT_METHODS[m]}:" for m in options]

    # A QR card already shows who to pay, so its line is left out of the email; text-only mail apps still get it.
    paragraphs.append({"text": how_to_pay(list(options.values())),
                       "html": how_to_pay([o for m, o in options.items() if m not in shown])})
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
