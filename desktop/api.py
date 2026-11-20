"""Bridge between the window (ui/index.html) and Python. Every public method is callable from JavaScript."""
import csv
import logging
import os
import webbrowser
from datetime import datetime
from pathlib import Path

from desktop import config, db, mailer, sender

log = logging.getLogger(__name__)


def _ok(**kw):
    return {"ok": True, **kw}


def _err(exc_or_msg, **kw):
    return {"ok": False, "error": str(exc_or_msg), **kw}


def public_settings(settings=None):
    settings = settings or config.load_settings()
    accounts = [{**a, "has_password": bool(config.smtp_password(a["email"]))} for a in settings["accounts"]]
    return {**settings, "accounts": accounts, "enabled_methods": config.enabled_methods(settings)}


def transaction_view(tx):
    via = f" via {tx['paid_method']}" if tx.get("paid_method") else ""
    if tx["status"] == "paid":
        label, tone = (f"Paid{via} · plans sent", "ok") if tx["delivered_at"] else (f"Paid{via} · plans NOT sent", "err")
    else:
        label, tone = "Not paid yet", ""
    return {
        "id": tx["id"], "title": tx["title"], "client_name": tx["client_name"], "client_email": tx["client_email"],
        "from_email": tx["from_email"], "price": mailer.money(tx["price_cents"]), "status": tx["status"],
        "label": label, "tone": tone, "sent_at": tx["sent_at"], "paid_at": tx["paid_at"], "pages": tx["pages"],
        "sha256": tx["sha256"], "share_link": tx["share_link"], "delivery_error": tx["delivery_error"],
        "ref_code": tx.get("ref_code") or "", "paid_method": tx.get("paid_method") or "",
        "pay_by": [config.PAYMENT_METHODS[m] for m in tx.get("methods") or [] if m in config.PAYMENT_METHODS],
        "activity": db.activity(tx["id"]),
    }


class Api:
    def __init__(self):
        self._window = None

    def _attach(self, window):
        self._window = window

    # ------------------------------------------------------------ state
    def state(self):
        settings = public_settings()
        return {"settings": settings, "presets": config.SMTP_PRESETS, "payment_methods": config.PAYMENT_METHODS,
                "manual_methods": config.MANUAL_METHODS, "data_dir": str(config.data_dir()),
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
            "totals": {
                "sent": len(txs), "paid": len(paid),
                "collected": mailer.money(sum(t["amount_paid_cents"] or t["price_cents"] for t in paid)),
                "waiting": mailer.money(sum(t["price_cents"] for t in txs if t["status"] == "sent")),
                "by_method": [f"{m} {mailer.money(c)}" for m, c in sorted(by_method.items(), key=lambda x: -x[1])],
            },
        }

    # ------------------------------------------------------------ sending & payments
    def choose_pdf(self):
        import webview

        result = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=("PDF files (*.pdf)",))
        if not result:
            return _ok(path=None)
        path = Path(result[0] if isinstance(result, (list, tuple)) else result)
        try:
            from plancore.pdfinfo import inspect_pdf

            with path.open("rb") as f:
                info = inspect_pdf(f)
        except Exception as exc:
            return _err(exc)
        return _ok(path=str(path), name=path.name, pages=info["page_count"],
                   size_mb=round(info["size_bytes"] / 1e6, 1),
                   too_big=info["size_bytes"] > config.ATTACH_LIMIT_BYTES)

    def send(self, form):
        try:
            tx_id = sender.send_plans(
                from_email=form.get("from_email", ""), to_email=form.get("to_email", ""),
                price=form.get("price", ""), pdf_path=form.get("pdf_path", ""),
                client_name=form.get("client_name", ""), title=form.get("title", ""),
                share_link=form.get("share_link", ""), methods=form.get("methods"),
            )
        except sender.NeedShareLink as exc:
            return _err(exc, need_share_link=True)
        except sender.SendError as exc:
            return _err(exc)
        except Exception as exc:  # never leave the window hanging
            log.exception("Send failed")
            return _err(f"Unexpected error: {exc}")
        tx = db.get_tx(tx_id)
        return _ok(tx_id=tx_id, message=f"Sent “{tx['title']}” to {tx['client_email']} (ref {tx['ref_code']}). "
                                        f"When their payment arrives, click Mark as paid below.")

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
        path = tx and tx.get("final_path" if which == "final" else "preview_path")
        if not path or not Path(path).is_file():
            return _err("That file isn't available.")
        os.startfile(path)  # noqa: S606 - opens in the user's PDF viewer
        return _ok()

    def open_url(self, url):
        if url.startswith("https://"):
            webbrowser.open(url)
        return _ok()

    def open_data_folder(self):
        os.startfile(config.data_dir())  # noqa: S606
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

    # ------------------------------------------------------------ settings
    def save_settings(self, payload):
        settings = config.load_settings()
        if "signature" in payload:
            settings["signature"] = {k: str(v).strip() for k, v in payload["signature"].items()}
        for field in ("zelle", "venmo"):
            if field in payload:
                clean = {k: (v.strip() if isinstance(v, str) else bool(v)) for k, v in payload[field].items()}
                if field == "venmo":
                    clean["username"] = clean.get("username", "").lstrip("@")
                if clean.get("enabled") and not (clean.get("recipient") or clean.get("username")):
                    return _err(f"Enter your {field.title()} {'email or phone' if field == 'zelle' else 'username'}, "
                                f"or switch {field.title()} off.")
                settings[field] = {**settings[field], **clean}
        config.save_settings(settings)
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
        w.writerow(["Date", "Time", "Ref", "Project", "Client email", "Client name", "Sent from", "Price",
                    "Current status", "Update"])
        for tx in db.list_txs():
            view = transaction_view(tx)
            for entry in reversed(view["activity"]):
                ts = datetime.fromisoformat(entry["ts"]).astimezone()
                w.writerow([f"{ts:%Y-%m-%d}", f"{ts:%H:%M}", view["ref_code"], tx["title"], tx["client_email"],
                            tx["client_name"], tx["from_email"], view["price"], view["label"], entry["text"]])
