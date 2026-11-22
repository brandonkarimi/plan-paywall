"""Automatic payments: read the engineer's inbox for the "you got paid" emails Wells Fargo (Zelle) and Venmo send,
check each one is genuine, match it to a plan waiting for payment and release the plans.

Matching (see match()):
1. The client put the plan's code (PLN-12) in the Zelle memo / Venmo note and paid at least the price → released.
2. No code, but exactly one waiting plan has that exact price (and the payer's name fits the client name, when one
   was entered) → released.
3. Anything else is kept for the engineer to confirm in Transactions. Plans are never released on a guess.

Only emails that the receiving mail server marked as DKIM-signed by wellsfargo.com / venmo.com are trusted, so a
fake "you got paid" email can't release plans.
"""
import email
import imaplib
import logging
import re
import select
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape

from desktop import config, db, mailer, sender

log = logging.getLogger(__name__)

# Payment method → the domains whose signed emails we trust for it.
SOURCES = {"Zelle": ("wellsfargo.com",), "Venmo": ("venmo.com",)}

IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
    "yahoo.com": "imap.mail.yahoo.com", "aol.com": "imap.aol.com",
    "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com",
    "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com", "live.com": "outlook.office365.com",
}

CODE_RE = re.compile(r"\bPLN[\s\-_#:]*0*(\d{1,7})\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")
LOOKBACK_LIMIT = timedelta(days=60)
CLOCK_SLACK = timedelta(minutes=5)  # the bank's clock and this PC's may differ a little
IDLE_RENEW = 25 * 60                # servers end a wait-for-new-mail after ~29 minutes; renew before that

_lock = threading.Lock()  # one check at a time


class InboxError(Exception):
    pass


def imap_host_for(address):
    domain = address.rsplit("@", 1)[-1].lower()
    return IMAP_HOSTS.get(domain, f"imap.{domain}")


def auto_settings(settings):
    """The automatic-payments settings, tidied: {"enabled", "zelle": {"email"}, "venmo": {"email"}}."""
    auto = settings.get("auto_payments") or {}
    zelle = (auto.get("zelle") or {}).get("email") or auto.get("email") or ""  # "email": the 1.3.0 test version
    venmo = (auto.get("venmo") or {}).get("email") or ""
    return {"enabled": bool(auto.get("enabled")), "zelle": {"email": zelle.strip().lower()},
            "venmo": {"email": venmo.strip().lower()}}


def inboxes(auto):
    """The inboxes to read: [{"email", "host", "methods"}]. Without its own email, Venmo uses the Zelle inbox;
    one address used for both is read once."""
    zelle = auto["zelle"]["email"]
    venmo = auto["venmo"]["email"] or zelle
    found = {}
    for method, address in (("Zelle", zelle), ("Venmo", venmo)):
        if address:
            box = found.setdefault(address, {"email": address, "host": imap_host_for(address), "methods": []})
            box["methods"].append(method)
    return list(found.values())


def inbox_password(acct):
    """The saved inbox password; if the inbox is also a sending account, its (app) password works too."""
    email_addr = (acct.get("email") or "").lower()
    if acct.get("password"):  # a new password being tried in Settings, before it's saved
        return acct["password"]
    return config.get_secret(f"imap:{email_addr}") or config.smtp_password(email_addr)


# ------------------------------------------------------------------ reading one email
def _domain_ok(domain, trusted):
    domain = domain.lower().strip(".")
    return any(domain == t or domain.endswith("." + t) for t in trusted)


def trusted_method(msg):
    """'Zelle' / 'Venmo' if this email really comes from Wells Fargo / Venmo, else None.

    Trust comes from the topmost Authentication-Results header, which the engineer's own mail server adds on
    arrival (anything further down could have been written by the sender). It must show DKIM passing for the
    bank's or Venmo's domain, and the From address must be that domain too."""
    results = msg.get_all("Authentication-Results") or []
    if not results:
        return None
    first = str(results[0])
    signed = {m.group(1) for m in re.finditer(r"dkim=pass\b[^;]*?header\.(?:d|i)=@?([\w.-]+)", first, re.IGNORECASE)}
    sender_domain = parseaddr(str(msg.get("From", "")))[1].rsplit("@", 1)[-1]
    for method, trusted in SOURCES.items():
        if _domain_ok(sender_domain, trusted) and any(_domain_ok(d, trusted) for d in signed):
            return method
    return None


def _text(msg):
    """The email's readable text (plain part preferred, else the HTML with tags removed), whitespace tidied."""
    body = msg.get_body(("plain", "html"))
    content = body.get_content() if body is not None else ""
    if body is not None and body.get_content_type() == "text/html":
        content = re.sub(r"(?is)<(script|style).*?</\1>", " ", content)
        content = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", content)
        content = unescape(re.sub(r"<[^>]+>", " ", content))
    content = content.replace("\xa0", " ")
    return "\n".join(" ".join(line.split()) for line in content.splitlines() if line.strip())


def _cents(text):
    return int(round(float(text.replace(",", "")) * 100))


def parse_payment(msg, method):
    """Pull amount, payer and memo out of a trusted payment email. None if it isn't a 'money received' email."""
    subject = " ".join(str(msg.get("Subject", "")).split())
    text = _text(msg)
    everything = f"{subject}\n{text}"
    low = everything.lower()
    if method == "Venmo":
        m = re.search(r"^(.*?)\s+paid you\s+\$", subject, re.IGNORECASE)
        if not m:  # not a payment received (requests, "You paid ...", statements, ...)
            return None
        payer = m.group(1)
    else:
        if "zelle" not in low or not re.search(r"received|sent you|deposited", low):
            return None
        if re.search(r"you sent|request(ed)? (money|from)|payment request", low):
            return None
        payer = ""
        for pattern in (r"from\s+([A-Za-z][\w .,'&-]{1,60}?)(?:\s+(?:with|via|on|has|sent|for)\b|[.\n(]|$)",
                        r"([A-Za-z][\w .,'&-]{1,60}?)\s+(?:sent|has sent) you\b"):
            m = re.search(pattern, everything)
            if m:
                payer = m.group(1).strip(" .,")
                break
    amount = AMOUNT_RE.search(subject) or AMOUNT_RE.search(text)
    if not amount:
        return None
    memo = ""
    m = re.search(r"(?:memo|note|message)\s*[:\-]\s*(.+)", text, re.IGNORECASE)
    if m:
        memo = m.group(1).strip()[:200]
    try:
        received = parsedate_to_datetime(str(msg.get("Date"))).astimezone(timezone.utc)
    except (TypeError, ValueError):
        received = datetime.now(timezone.utc)
    codes = {int(c) for c in CODE_RE.findall(everything)}
    return {"method": method, "amount_cents": _cents(amount.group(1)), "payer": payer[:120], "memo": memo,
            "codes": codes, "received_at": received.isoformat(timespec="seconds"),
            "message_id": str(msg.get("Message-ID", "")).strip()}


# ------------------------------------------------------------------ matching
def _words(name):
    return {w for w in re.findall(r"[a-z]+", (name or "").lower()) if len(w) > 1}


def names_match(client_name, payer):
    return bool(_words(client_name) & _words(payer))


def _before(payment, sent_at):
    """True if the payment email is from before ``sent_at`` (so it can't be for that plan)."""
    sent = datetime.fromisoformat(sent_at) - CLOCK_SLACK
    return datetime.fromisoformat(payment["received_at"]) < sent


def _after(tx, payment):
    return not tx.get("sent_at") or not _before(payment, tx["sent_at"])


def match(payment, waiting):
    """(tx, None) when this payment clearly pays one waiting plan, else (None, reason shown to the engineer)."""
    waiting = [t for t in waiting if _after(t, payment)]
    by_code = [t for t in waiting if t["id"] in payment["codes"]]
    if len(by_code) == 1:
        tx = by_code[0]
        if not tx["price_cents"]:
            return None, f"Has the code {tx['ref_code']}, but no price was entered for that plan."
        if payment["amount_cents"] < tx["price_cents"]:
            return None, (f"Has the code {tx['ref_code']}, but is less than the "
                          f"{mailer.money(tx['price_cents'])} price.")
        return tx, None
    if len(by_code) > 1:
        return None, "Mentions more than one plan code."
    if payment["codes"]:
        return None, "Has a plan code that isn't waiting for payment."
    same = [t for t in waiting if t["price_cents"] and t["price_cents"] == payment["amount_cents"]]
    if len(same) == 1:
        tx = same[0]
        if not tx["client_name"] or names_match(tx["client_name"], payment["payer"]):
            return tx, None
        return None, f"Matches the price of “{tx['title']}”, but the payer's name differs from {tx['client_name']}."
    if len(same) > 1:
        return None, "More than one waiting plan has this price, and there's no plan code."
    return None, "No waiting plan has this price, and there's no plan code."


# ------------------------------------------------------------------ inbox
def open_inbox(acct, connect=None):
    """A signed-in connection to the inbox, opened read-only. Raises InboxError."""
    password = inbox_password(acct)
    if not password:
        raise InboxError(f"No password saved for {acct['email']}.")
    host = acct.get("host") or imap_host_for(acct["email"])
    try:
        conn = (connect or imaplib.IMAP4_SSL)(host, int(acct.get("port") or 993),
                                             ssl_context=ssl.create_default_context(), timeout=60)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise InboxError(f"Couldn't reach {host}: {exc}") from exc
    try:
        conn.login(acct["email"], password)
        conn.select("INBOX", readonly=True)
    except (OSError, imaplib.IMAP4.error) as exc:
        close(conn)
        raise InboxError(f"Couldn't sign in to {acct['email']}. Check the app password.") from exc
    return conn


def close(conn):
    try:
        conn.logout()
    except Exception:
        pass


def _uidvalidity(conn):
    try:
        data = conn.response("UIDVALIDITY")[1]
        value = data[0] if data else b""
        return value.decode() if isinstance(value, bytes) else str(value or "")
    except Exception:
        return ""


def _fetch(acct, since, connect=None):
    """New emails from Wells Fargo / Venmo (only those this inbox is used for).

    Only mail the app hasn't looked at before is downloaded: the inbox's position (IMAP UID) is remembered once the
    emails are handled. Returns (messages, position to remember)."""
    conn = open_inbox(acct, connect)
    try:
        validity = _uidvalidity(conn)
        saved = db.inbox_state(acct["email"])
        last = saved["last_uid"] if saved and saved["uidvalidity"] == validity else 0
        found, top = [], last
        for method, domains in SOURCES.items():
            if method not in acct.get("methods", SOURCES):
                continue
            for domain in domains:
                criteria = ["SINCE", since.strftime("%d-%b-%Y"), "FROM", domain]
                if last:
                    criteria = ["UID", f"{last + 1}:*"] + criteria
                status, data = conn.uid("SEARCH", None, *criteria)
                if status != "OK" or not data or not data[0]:
                    continue
                for uid in data[0].split():
                    if int(uid) <= last:  # "n:*" always includes the newest message, even if already seen
                        continue
                    top = max(top, int(uid))
                    status, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: don't mark it read
                    raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                    if status == "OK" and raw:
                        found.append(email.message_from_bytes(raw, policy=policy.default))
        return found, {"email": acct["email"], "uidvalidity": validity, "last_uid": top}
    finally:
        close(conn)


def test_login(acct, connect=None):
    """Raises InboxError if the inbox can't be opened with the saved details."""
    close(open_inbox(acct, connect))


def _readable(sock, timeout):
    if getattr(sock, "pending", None) and sock.pending():  # already decrypted, waiting to be read
        return True
    return bool(select.select([sock], [], [], timeout)[0])


def wait_for_mail(conn, stop, renew=IDLE_RENEW, poll=30):
    """Wait (IMAP IDLE) until the mail server says new mail arrived → True. Returns False after ``renew`` seconds
    or once ``stop`` is set. Uses no CPU while waiting: the server pushes the news down the open connection."""
    tag = conn._new_tag()
    conn.send(tag + b" IDLE\r\n")
    if not conn.readline().startswith(b"+"):
        raise InboxError("The mail server won't report new mail.")
    deadline = time.monotonic() + renew
    new_mail = False
    while not new_mail and not stop.is_set() and time.monotonic() < deadline:
        if not _readable(conn.socket(), min(poll, max(0.0, deadline - time.monotonic()))):
            continue
        line = conn.readline()
        if not line:
            raise InboxError("The mail server closed the connection.")
        new_mail = b"EXISTS" in line.upper()
    conn.send(b"DONE\r\n")
    while True:  # finish the IDLE: read up to the server's reply to it
        line = conn.readline()
        if not line:
            raise InboxError("The mail server closed the connection.")
        if line.startswith(tag):
            return new_mail


def check_inbox(settings=None, connect=None):
    """Look for new payments and release plans that are clearly paid.
    Returns {"released": [tx titles], "review": count of new payments to confirm}, plus "errors" (inboxes that
    couldn't be read) when there were any."""
    settings = settings or config.load_settings()
    auto = auto_settings(settings)
    boxes = inboxes(auto)
    result = {"released": [], "review": 0}
    if not auto["enabled"] or not boxes:
        return result
    with _lock:
        waiting = db.list_txs(["sent"])
        if not waiting:
            return result
        oldest = min(datetime.fromisoformat(t["sent_at"]) for t in waiting if t.get("sent_at"))
        since = max(oldest, datetime.now(timezone.utc) - LOOKBACK_LIMIT) - timedelta(days=1)
        messages, errors, positions = [], [], []
        for box in boxes:
            try:
                found, position = _fetch(box, since, connect)
                messages += found
                positions.append(position)
            except InboxError as exc:
                errors.append(str(exc))
        if errors:
            result["errors"] = errors
        first_sent = min(t["sent_at"] for t in waiting if t.get("sent_at"))
        for msg in messages:
            method = trusted_method(msg)
            message_id = str(msg.get("Message-ID", "")).strip()
            if not method or not message_id or db.payment_seen(message_id):
                continue
            payment = parse_payment(msg, method)
            if payment is None:
                db.add_payment(message_id=message_id, status="ignored")
                continue
            if _before(payment, first_sent):  # an older payment, from before any waiting plan was sent
                db.add_payment(message_id=message_id, status="ignored", note="Before the plans were sent")
                continue
            tx, reason = match(payment, db.list_txs(["sent"]))
            fields = {k: payment[k] for k in ("message_id", "method", "amount_cents", "payer", "memo", "received_at")}
            if tx is None:
                db.add_payment(**fields, status="review", note=reason)
                result["review"] += 1
                continue
            pay_id = db.add_payment(**fields, status="released", tx_id=tx["id"])
            try:
                delivered = release(tx, payment, settings)
            except sender.SendError as exc:  # e.g. marked paid by hand a moment ago
                db.update_payment(pay_id, status="review", tx_id=None, note=str(exc))
                result["review"] += 1
                continue
            result["released"].append(tx["title"] if delivered else f"{tx['title']} (email failed, use Resend)")
        for position in positions:  # everything up to here is handled: next time, only newer mail is downloaded
            db.set_inbox_state(**position)
    return result


def release(tx, payment, settings=None):
    """Mark the plan paid from a payment email and email the plans. Returns True if the plans were emailed."""
    who = f" from {payment['payer']}" if payment.get("payer") else ""
    db.log(tx["id"], f"{payment['method']} payment of {mailer.money(payment['amount_cents'])}{who} found in your email")
    return sender.mark_paid(tx["id"], payment["method"], f"{payment['amount_cents'] / 100:.2f}", allow_short=True,
                            settings=settings)
