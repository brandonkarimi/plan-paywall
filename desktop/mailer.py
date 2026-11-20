"""Send email through the engineer's own account (SMTP), plus the message templates."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from pathlib import Path
from urllib.parse import quote

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
    return f"Hi {tx['client_name'] or 'there'},"


def _manifest(tx):
    lines = [f"Sheets: {tx['pages']} page{'s' if tx['pages'] != 1 else ''}"]
    sheets = tx.get("sheets") or []
    if sheets and not all(s.startswith("Sheet ") for s in sheets):
        lines += [f"  • {s}" for s in sheets[:40]]
        if len(sheets) > 40:
            lines.append(f"  … and {len(sheets) - 40} more")
    lines.append(f"File fingerprint (SHA-256): {tx['sha256']}")
    return lines


def venmo_link(username, cents, note):
    user = username.strip().lstrip("@")
    return (f"https://venmo.com/{quote(user)}?txn=pay&amount={cents / 100:.2f}"
            f"&note={quote(note)}")


def invite(tx, settings):
    price = money(tx["price_cents"])
    ref = tx.get("ref_code") or f"PLN-{tx['id']}"
    methods = tx.get("methods") or ["stripe"]
    intro = (f"Your plans for {tx['title']} are ready. A watermarked preview of every sheet is attached for "
             f"review. The final PDF is emailed to you once payment of {price} is received.")
    paragraphs, buttons = [_greeting(tx), intro], []
    many = len(methods) > 1
    if "stripe" in methods and tx.get("link_url"):
        p = (("Option: " if many else "") + "Pay online by card or bank transfer (secure checkout by Stripe). "
             "The final PDF is emailed automatically within minutes.")
        paragraphs.append(p)
        buttons.append({"after": p, "label": f"Pay {price} online", "url": tx["link_url"]})
    if "zelle" in methods:
        z = settings.get("zelle") or {}
        who = z.get("recipient", "")
        name = f" ({z['name']})" if z.get("name") else ""
        paragraphs.append([("Option: " if many else "") + "Pay with Zelle (no fees):",
                           f"  Send {price} to {who}{name}",
                           f"  Memo: {ref}"])
    if "venmo" in methods:
        v = settings.get("venmo") or {}
        user = v.get("username", "").strip().lstrip("@")
        p = [("Option: " if many else "") + f"Pay with Venmo: send {price} to @{user} with the note {ref}."]
        paragraphs.append(p)
        buttons.append({"after": p, "label": f"Pay {price} with Venmo", "color": "#008cff",
                        "url": venmo_link(user, tx["price_cents"], f"{ref} {tx['title']}")})
    if any(m in methods for m in ("zelle", "venmo")):
        paragraphs.append(f"Please include {ref} with Zelle or Venmo payments so we can match them. Once your "
                          "payment is confirmed (usually the same day), the final PDF is emailed to this address.")
    paragraphs += [
        _manifest(tx),
        "The fingerprint above identifies the exact final file you'll receive; you can check it after downloading "
        "(Windows: certutil -hashfile file.pdf SHA256, Mac: shasum -a 256 file.pdf).",
    ]
    text, html = _wrap(paragraphs, settings, buttons)
    return f"Plans ready for review: {tx['title']}", text, html


def files(tx, settings):
    paid = money(tx["amount_paid_cents"] or tx["price_cents"])
    if tx.get("share_link"):
        body = f"Thank you, your payment of {paid} was received. Download the final plans here:"
        button = {"after": body, "label": "Download final plans", "url": tx["share_link"]}
    else:
        body = f"Thank you, your payment of {paid} was received. The final plans are attached."
        button = None
    paragraphs = [_greeting(tx), body, _manifest(tx),
                  "Please keep this email for your records."]
    text, html = _wrap(paragraphs, settings, button)
    return f"Your final plans: {tx['title']}", text, html


def pending(tx, settings):
    paragraphs = [_greeting(tx),
                  f"We've received your bank transfer for {tx['title']}. Bank payments usually take 3–5 business "
                  "days to clear; the final plans will be emailed to you automatically as soon as it does. "
                  "There's nothing else you need to do."]
    text, html = _wrap(paragraphs, settings)
    return f"Bank transfer received: {tx['title']}", text, html


def ach_failed(tx, settings):
    intro = (f"Your bank transfer for {tx['title']} didn't go through, so the plans haven't been released yet. "
             "You can pay again here (card or bank):")
    button = {"after": intro, "label": f"Pay {money(tx['price_cents'])}", "url": tx["link_url"]}
    text, html = _wrap([_greeting(tx), intro], settings, button)
    return f"Payment didn't go through: {tx['title']}", text, html


def test_message(settings):
    text, html = _wrap(["This is a test from Plan Sender. Your email account is set up correctly."], settings)
    return "Plan Sender test email", text, html
