"""Bridge between the window (ui/index.html) and Python. Every public method is callable from JavaScript."""
import csv
import logging
import os
import re
import webbrowser
from datetime import datetime
from pathlib import Path

from desktop import background, config, db, images, mailer, payments, sender

log = logging.getLogger(__name__)


def _ok(**kw):
    return {"ok": True, **kw}


def _err(exc_or_msg, **kw):
    return {"ok": False, "error": str(exc_or_msg), **kw}


def public_settings(settings=None):
    settings = settings or config.load_settings()
    accounts = [{**a, "has_password": bool(config.smtp_password(a["email"]))} for a in settings["accounts"]]
    auto = payments.auto_settings(settings)
    for key in ("zelle", "venmo"):
        address = auto[key]["email"] or (auto["zelle"]["email"] if key == "venmo" else "")
        auto[key]["has_password"] = bool(address and payments.inbox_password({"email": address}))
    return {**settings, "accounts": accounts, "auto_payments": auto, "enabled_methods": config.enabled_methods(settings)}


# Older versions wrote more technical log lines; show them in today's plain wording.
_OLD_WORDING = [
    (re.compile(r"^Sent to (\S+) from \S+ \((?:[^)]*?)(, invoice attached)?\)\..*$"),
     lambda m: f"Preview{' and invoice' if m.group(2) else ''} sent to {m.group(1)}"),
    (re.compile(r"^Paid (\$[\d,.]+) via (\w+) \(confirmed by you\)$"), lambda m: f"Marked as paid: {m.group(1)} by {m.group(2)}"),
    (re.compile(r"^Paid via (\w+) \(confirmed by you\)$"), lambda m: f"Marked as paid by {m.group(1)}"),
    (re.compile(r"^Final plans emailed to (.+)$"), lambda m: f"Full plans emailed to {m.group(1)}"),
    (re.compile(r"^Final plans re-sent to (.+)$"), lambda m: f"Full plans emailed again to {m.group(1)}"),
    (re.compile(r"^Payment instructions re-sent to client$"), lambda m: "Preview emailed again"),
    (re.compile(r"^Could not email the final plans: (.+)$"), lambda m: f"Couldn't email the full plans: {m.group(1)}"),
]


def friendly(text):
    for pattern, fix in _OLD_WORDING:
        m = pattern.match(text)
        if m:
            return fix(m)
    return text


def _or_list(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1] if items else ""


def transaction_view(tx):
    method = tx.get("paid_method") or ""
    if tx["status"] == "paid":
        if tx["delivered_at"]:
            label, tone = (f"Paid by {method}" if method else "Paid"), "ok"
        else:
            label, tone = "Paid, plans not sent yet", "err"
    else:
        label, tone = "Waiting for payment", ""
    pay_by = [config.PAYMENT_METHODS[m] for m in tx.get("methods") or [] if m in config.PAYMENT_METHODS]
    pages = tx["pages"] or 0
    return {
        "id": tx["id"], "title": tx["title"], "client_name": tx["client_name"], "client_email": tx["client_email"],
        "from_email": tx["from_email"], "status": tx["status"],
        "price": (mailer.money(tx["price_cents"]) if tx["price_cents"]
                  else "On invoice" if tx.get("invoice_path") else "-"),
        "price_value": f"{tx['price_cents'] / 100:.2f}" if tx["price_cents"] else "",
        "has_invoice": bool(tx.get("invoice_path")),
        "label": label, "tone": tone, "sent_at": tx["sent_at"], "paid_at": tx["paid_at"], "pages": tx["pages"],
        "sha256": tx["sha256"], "share_link": tx["share_link"], "delivery_error": tx["delivery_error"],
        "ref_code": tx.get("ref_code") or "", "paid_method": method,
        "pay_by": pay_by, "pay_by_text": _or_list(pay_by),
        "plans_text": f"{pages} page{'s' if pages != 1 else ''}" + (" (sent by download link)" if tx["share_link"] else ""),
        "activity": [{**a, "text": friendly(a["text"])} for a in db.activity(tx["id"])],
    }


def pdf_details(path, label=""):
    """Check a chosen or dropped file is a readable PDF and describe it for the Send page."""
    from plancore.pdfinfo import inspect_pdf

    if not path:
        return _err("Couldn't read that file. Try choosing it instead.")
    path = Path(path)
    prefix = f"{label}: " if label else ""
    if path.suffix.lower() != ".pdf":
        return _err(f"{prefix}{path.name} isn't a PDF.")
    try:
        with path.open("rb") as f:
            info = inspect_pdf(f)
    except Exception as exc:
        return _err(f"{prefix}{exc}")
    return _ok(path=str(path), name=path.name, pages=info["page_count"],
               size_mb=round(info["size_bytes"] / 1e6, 1),
               too_big=info["size_bytes"] > config.ATTACH_LIMIT_BYTES)


def payment_view(p):
    when = datetime.fromisoformat(p["received_at"]).astimezone() if p.get("received_at") else None
    return {"id": p["id"], "method": p["method"], "amount": mailer.money(p["amount_cents"]), "payer": p["payer"],
            "memo": p["memo"], "note": p["note"], "when": f"{when:%b %d, %I:%M %p}" if when else ""}


def payment_summary(result):
    parts = []
    if result.get("released"):
        parts.append("Payment received: plans sent for " + ", ".join(f"“{t}”" for t in result["released"]) + ".")
    if result.get("review"):
        n = result["review"]
        parts.append(f"{n} payment{'s' if n != 1 else ''} found that need{'s' if n == 1 else ''} your OK in Transactions.")
    return " ".join(parts)


def same_price(price):
    """If another plan waiting for payment has exactly this price, that plan and the nearest free amount above it
    (e.g. $1,500.01). Payments are matched by amount, so every unpaid plan needs its own. Else None."""
    if not str(price or "").strip():
        return None
    try:
        cents = sender._price_cents(price)
    except sender.SendError:
        return None  # the send itself reports the bad price
    waiting = db.list_txs(["sent"])
    taken = {t["price_cents"] for t in waiting if t["price_cents"]}
    other = next((t for t in waiting if t["price_cents"] == cents), None)
    if not other:
        return None
    free = next(c for c in range(cents + 1, cents + 10000) if c not in taken)
    return {"other": other, "price": mailer.money(cents), "suggested": f"{free / 100:.2f}",
            "suggested_text": mailer.money(free)}


DRAFT_TEXT = ("from_email", "to_email", "client_name", "title", "price", "share_link", "subject", "message")


class Api:
    def __init__(self):
        self._window = None

    def _attach(self, window):
        self._window = window

    # ------------------------------------------------------------ state
    # ------------------------------------------------------------ unsent Send page (survives closing the app)

    def save_draft(self, draft):
        draft = draft or {}
        clean = {k: str(draft.get(k) or "")[:20000] for k in DRAFT_TEXT}
        clean["methods"] = [m for m in (draft.get("methods") or []) if m in config.PAYMENT_METHODS]
        clean["plan_path"] = str(draft.get("plan_path") or "")
        clean["invoice_path"] = str(draft.get("invoice_path") or "")
        path = config.draft_path()
        if not any(clean[k] for k in DRAFT_TEXT if k != "from_email") and not clean["plan_path"] \
                and not clean["invoice_path"]:
            path.unlink(missing_ok=True)  # nothing typed: no draft
            return _ok()
        import json

        path.write_text(json.dumps(clean), encoding="utf-8")
        return _ok()

    def load_draft(self):
        """The unsent Send page, with the PDFs checked again (a moved or deleted file is left out)."""
        import json

        try:
            draft = json.loads(config.draft_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _ok(draft=None)
        for key, label in (("plan_path", ""), ("invoice_path", "Invoice")):
            path = draft.pop(key, "")
            details = pdf_details(path, label) if path and Path(path).is_file() else None
            draft[key.replace("_path", "")] = details if details and details["ok"] else None
        return _ok(draft=draft)

    def state(self):
        settings = public_settings()
        return {"settings": settings, "presets": config.SMTP_PRESETS, "payment_methods": config.PAYMENT_METHODS,
                "manual_methods": config.MANUAL_METHODS, "data_dir": str(config.data_dir()),
                "images": images.thumbnails(),
                "setup_needed": not (settings["enabled_methods"] and settings["accounts"])}

    def transactions(self):
        txs = db.list_txs()
        paid = [t for t in txs if t["status"] == "paid"]
        by_method = {}
        for t in paid:
            m = t.get("paid_method") or "Other"
            by_method[m] = by_method.get(m, 0) + (t["amount_paid_cents"] or t["price_cents"])
        return {
            "items": [transaction_view(t) for t in txs],
            "review": [payment_view(p) for p in db.list_payments("review")],
            "payment_errors": (background.read_status().get("errors") or []) if background.enabled() else [],
            "waiting": [{"id": t["id"], "label": f"{t['ref_code']} · {t['title']} · {t['client_email']}"
                         + (f" · {mailer.money(t['price_cents'])}" if t["price_cents"] else "")}
                        for t in txs if t["status"] == "sent"],
            "totals": {
                "sent": len(txs), "paid": len(paid),
                "collected": mailer.money(sum(t["amount_paid_cents"] or t["price_cents"] for t in paid)),
                "waiting": mailer.money(sum(t["price_cents"] for t in txs if t["status"] == "sent")),
                "by_method": [f"{m} {mailer.money(c)}" for m, c in sorted(by_method.items(), key=lambda x: -x[1]) if c],
            },
        }

    # ------------------------------------------------------------ sending & payments
    def choose_pdf(self):
        import webview

        result = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=("PDF files (*.pdf)",))
        if not result:
            return _ok(path=None)
        return pdf_details(result[0] if isinstance(result, (list, tuple)) else result)

    def choose_invoice(self):
        import webview

        result = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=("PDF files (*.pdf)",))
        if not result:
            return _ok(path=None)
        return pdf_details(result[0] if isinstance(result, (list, tuple)) else result, "Invoice")

    def send(self, form):
        clash = same_price(form.get("price", ""))
        if clash:
            return _err(f"“{clash['other']['title']}” ({clash['other']['client_email']}) is also waiting for "
                        f"{clash['price']}.", same_price=True, suggested=clash["suggested"],
                        suggested_text=clash["suggested_text"], price_text=clash["price"],
                        other=clash["other"]["title"])
        try:
            tx_id = sender.send_plans(
                from_email=form.get("from_email", ""), to_email=form.get("to_email", ""),
                price=form.get("price", ""), pdf_path=form.get("pdf_path", ""),
                client_name=form.get("client_name", ""), title=form.get("title", ""),
                share_link=form.get("share_link", ""), methods=form.get("methods"),
                invoice_path=form.get("invoice_path", ""),
                subject=form.get("subject", ""), message=form.get("message", ""),
            )
        except sender.NeedShareLink as exc:
            return _err(exc, need_share_link=True)
        except sender.SendError as exc:
            return _err(exc)
        except Exception as exc:  # never leave the window hanging
            log.exception("Send failed")
            return _err(f"Unexpected error: {exc}")
        tx = db.get_tx(tx_id)
        return _ok(tx_id=tx_id, message=f"Sent to {tx['client_email']}. Click Mark as paid once their payment arrives.")

    # ------------------------------------------------------------ automatic payments
    def _set_auto_payments(self, settings, data):
        """Validate and store the payment-alert inbox(es). Returns an error message, or "" when saved."""
        enabled = bool(data.get("enabled"))
        typed = {}
        for key, label in (("zelle", "Zelle"), ("venmo", "Venmo")):
            part = data.get(key) or {}
            address = (part.get("email") or "").strip().lower()
            if address and not sender.EMAIL_RE.match(address):
                return f"Check the {label} alerts email address."
            typed[key] = {"email": address, "password": (part.get("password") or "").replace(" ", "")}
        auto = {"enabled": enabled, "zelle": {"email": typed["zelle"]["email"]},
                "venmo": {"email": typed["venmo"]["email"]}}
        boxes = payments.inboxes(auto)
        if enabled and not boxes:
            return "Enter the email that gets your Zelle or Venmo payment alerts."
        # A password typed next to an email belongs to that inbox (Venmo's row, left blank, means the Zelle inbox).
        where = {"zelle": typed["zelle"]["email"], "venmo": typed["venmo"]["email"] or typed["zelle"]["email"]}
        new_passwords = {}
        for box in boxes:
            password = next((typed[k]["password"] for k in ("zelle", "venmo")
                             if typed[k]["password"] and where[k] == box["email"]), "")
            if enabled:
                if not payments.inbox_password({**box, "password": password}):
                    return f"Enter the app password for {box['email']}."
                try:
                    payments.test_login({**box, "password": password})
                except payments.InboxError as exc:
                    return str(exc)
            if password:
                new_passwords[box["email"]] = password
        for address, password in new_passwords.items():  # saved only once every inbox works
            config.set_secret(f"imap:{address}", password)
        settings["auto_payments"] = auto
        return ""

    def confirm_payment(self, payment_id, tx_id):
        """The engineer says which plan a found payment is for: mark it paid and email the plans."""
        payment = db.get_payment(int(payment_id))
        tx = db.get_tx(int(tx_id)) if tx_id else None
        if not payment or payment["status"] != "review":
            return _err("That payment was already handled.")
        if not tx or tx["status"] != "sent":
            return _err("Choose a plan that's waiting for payment.")
        try:
            delivered = payments.release(tx, payment)
        except sender.SendError as exc:
            return _err(exc)
        db.update_payment(payment["id"], status="confirmed", tx_id=tx["id"])
        if delivered:
            return _ok(message=f"Marked paid. The final plans were emailed to {tx['client_email']}.")
        return _err(f"Marked paid, but the plans couldn't be emailed: {db.get_tx(tx['id'])['delivery_error']}. "
                    "Use Resend.")

    def dismiss_payment(self, payment_id):
        db.update_payment(int(payment_id), status="dismissed")
        return _ok()

    def mark_paid(self, tx_id, method, amount="", allow_short=False):
        try:
            delivered = sender.mark_paid(int(tx_id), method, amount, bool(allow_short))
        except sender.SendError as exc:
            return _err(exc, short=("less than" in str(exc)))
        except Exception as exc:
            log.exception("Mark paid failed")
            return _err(f"Unexpected error: {exc}")
        tx = db.get_tx(int(tx_id))
        if delivered:
            return _ok(message=f"Marked paid via {method}. The final plans were emailed to {tx['client_email']}.")
        return _err(f"Marked paid, but the plans couldn't be emailed: {tx['delivery_error']}. Fix it and use Resend.")

    def resend(self, tx_id):
        try:
            sender.resend(int(tx_id))
        except Exception as exc:
            return _err(exc)
        return _ok(message="Sent again.")

    # ------------------------------------------------------------ files & links
    def open_file(self, tx_id, which):
        tx = db.get_tx(int(tx_id))
        path = tx and tx.get({"final": "final_path", "invoice": "invoice_path"}.get(which, "preview_path"))
        if not path or not Path(path).is_file():
            return _err("That file isn't available.")
        os.startfile(path)  # noqa: S606 - opens in the user's PDF viewer
        return _ok()

    def open_url(self, url):
        if url.startswith("https://"):
            webbrowser.open(url)
        return _ok()

    def export_csv(self):
        import webview

        default = f"plan-sender-log-{datetime.now():%Y-%m-%d}.csv"
        result = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=default,
                                                 file_types=("CSV files (*.csv)",))
        if not result:
            return _ok(path=None)
        path = Path(result[0] if isinstance(result, (list, tuple)) else result)
        write_csv(path)
        return _ok(path=str(path))

    def delete_tx(self, tx_id):
        """Delete one transaction, its history and its stored PDFs."""
        tx = db.get_tx(tx_id)
        if not tx:
            return _err("That transaction no longer exists.")
        db.delete_tx(tx_id)
        config.remove_tree(config.data_dir() / "files" / str(int(tx_id)))
        log.info("Transaction %s deleted", tx_id)
        return _ok(message=f"Deleted “{tx['title']}” and its stored files.")

    def reset_log(self, confirm):
        """Delete every transaction, its log and its stored PDFs. Requires typing DELETE."""
        if (confirm or "").strip().upper() != "DELETE":
            return _err("Type DELETE to confirm.")
        removed = db.delete_all()
        files = config.data_dir() / "files"
        config.remove_tree(files)
        files.mkdir(parents=True, exist_ok=True)
        log.info("Transaction log reset (%s removed)", removed)
        return _ok(message=f"Cleared {removed} transaction{'s' if removed != 1 else ''} and their stored files.")

    # ------------------------------------------------------------ settings
    def save_settings(self, payload):
        settings = config.load_settings()
        if "auto_payments" in payload:
            error = self._set_auto_payments(settings, payload["auto_payments"] or {})
            if error:
                return _err(error)
        if "signature" in payload:
            settings["signature"] = {k: str(v).strip() for k, v in payload["signature"].items()}
        for field in ("zelle", "venmo"):
            if field in payload:
                clean = {k: (v.strip() if isinstance(v, str) else bool(v)) for k, v in payload[field].items()}
                if field == "venmo":
                    clean["username"] = clean.get("username", "").lstrip("@")
                if clean.get("enabled") and not (clean.get("recipient") or clean.get("username")
                                                 or config.image_path(f"{field}_qr")):
                    return _err(f"Enter your {field.title()} {'email or phone' if field == 'zelle' else 'username'} "
                                f"or add your {field.title()} QR code, "
                                f"or switch {field.title()} off.")
                settings[field] = {**settings[field], **clean}
        config.save_settings(settings)
        if "auto_payments" in payload:
            background.sync(settings)  # start (and start with Windows) or stop the payment checker
        return _ok(state=self.state())

    def signature_preview(self, signature):
        """The email signature as clients will see it, for the live preview in Settings."""
        logo = images.thumbnails().get("signature_logo") or None
        sig = {k: str(v) for k, v in (signature or {}).items()}
        return mailer.signature_html({"signature": sig}, logo)

    def set_theme(self, theme):
        if theme not in ("light", "dark"):
            return _err("Unknown theme.")
        settings = config.load_settings()
        settings["theme"] = theme
        config.save_settings(settings)
        return _ok(theme=theme)

    def choose_image(self, slot):
        """Pick a picture (QR code or watermark logo) and copy it into the app."""
        import webview

        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=("Pictures (*.png;*.jpg;*.jpeg)", "All files (*.*)"))
        if not result:
            return _ok(state=None)
        try:
            images.store(slot, result[0] if isinstance(result, (list, tuple)) else result)
        except images.ImageError as exc:
            return _err(exc)
        return _ok(state=self.state())

    def remove_image(self, slot):
        images.remove(slot)
        return _ok(state=self.state())

    def save_account(self, acct):
        email = (acct.get("email") or "").strip().lower()
        if not sender.EMAIL_RE.match(email):
            return _err("Enter the email address.")
        preset = config.SMTP_PRESETS.get(acct.get("preset") or "custom", config.SMTP_PRESETS["custom"])
        host = (acct.get("host") or preset["host"]).strip()
        if not host:
            return _err("Enter the SMTP server for this account.")
        entry = {"email": email, "name": (acct.get("name") or "").strip(), "preset": acct.get("preset") or "custom",
                 "host": host, "port": int(acct.get("port") or preset["port"]),
                 "security": acct.get("security") or preset["security"]}
        settings = config.load_settings()
        settings["accounts"] = [a for a in settings["accounts"] if a["email"] != email] + [entry]
        if not settings["default_from"]:
            settings["default_from"] = email
        if acct.get("password"):
            config.set_secret(f"smtp:{email}", acct["password"].replace(" ", "") if entry["preset"] == "gmail"
                              else acct["password"])
        elif not config.smtp_password(email):
            return _err("Enter the password (or app password) for this account.")
        config.save_settings(settings)
        return _ok(state=self.state())

    def remove_account(self, email):
        settings = config.load_settings()
        settings["accounts"] = [a for a in settings["accounts"] if a["email"] != email]
        if settings["default_from"] == email:
            settings["default_from"] = settings["accounts"][0]["email"] if settings["accounts"] else ""
        config.set_secret(f"smtp:{email}", "")
        config.save_settings(settings)
        return _ok(state=self.state())

    def test_email(self, email):
        settings = config.load_settings()
        try:
            subject, text, html = mailer.test_message(settings)
            mailer.send(email, email, subject, text, html, settings=settings)
        except Exception as exc:
            return _err(exc)
        return _ok(message=f"Test email sent to {email}. Check that inbox.")


def write_csv(path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads accents correctly
        w = csv.writer(f)
        w.writerow(["Date", "Time", "Project", "Client name", "Client email", "Amount", "Status", "What happened"])
        for tx in db.list_txs():
            view = transaction_view(tx)
            for entry in reversed(view["activity"]):
                ts = datetime.fromisoformat(entry["ts"]).astimezone()
                w.writerow([f"{ts:%Y-%m-%d}", f"{ts:%I:%M %p}", tx["title"], tx["client_name"], tx["client_email"],
                            view["price"], view["label"], entry["text"]])
