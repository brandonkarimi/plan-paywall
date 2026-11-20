"""Bridge between the window (ui/index.html) and Python. Every public method is callable from JavaScript."""
import csv
import logging
import os
import webbrowser
from datetime import datetime
from pathlib import Path

from desktop import config, db, mailer, payments, sender, watcher

log = logging.getLogger(__name__)


def _ok(**kw):
    return {"ok": True, **kw}


def _err(exc_or_msg, **kw):
    return {"ok": False, "error": str(exc_or_msg), **kw}


def public_settings(settings=None):
    settings = settings or config.load_settings()
    accounts = [{**a, "has_password": bool(config.smtp_password(a["email"]))} for a in settings["accounts"]]
    key = config.stripe_key()
    return {**settings, "accounts": accounts, "stripe_connected": bool(key),
            "test_mode": payments.is_test_key(key) if key else False}


def transaction_view(tx):
    if tx["status"] == "paid" and not tx["delivered_at"]:
        label, tone = "Paid · plans NOT sent yet", "err"
    else:
        label, tone = {
            "sent": ("Not paid yet", ""),
            "pending": ("Bank transfer clearing", "warn"),
            "paid": ("Paid · plans sent", "ok"),
            "refunded": ("Refunded", "err"),
        }.get(tx["status"], (tx["status"], ""))
    return {
        "id": tx["id"], "title": tx["title"], "client_name": tx["client_name"], "client_email": tx["client_email"],
        "from_email": tx["from_email"], "price": mailer.money(tx["price_cents"]), "price_cents": tx["price_cents"],
        "status": tx["status"], "label": label, "tone": tone, "sent_at": tx["sent_at"], "paid_at": tx["paid_at"],
        "link_url": tx["link_url"], "pages": tx["pages"], "sha256": tx["sha256"], "share_link": tx["share_link"],
        "delivery_error": tx["delivery_error"], "delivered": bool(tx["delivered_at"]),
        "activity": db.activity(tx["id"]),
    }


class Api:
    def __init__(self, watcher_thread=None, notify=None):
        self._window = None
        self._watcher = watcher_thread
        self._notify = notify or (lambda title, message: None)

    def _attach(self, window):
        self._window = window

    # ------------------------------------------------------------ state
    def state(self):
        settings = public_settings()
        last = self._watcher.last_run.isoformat() if self._watcher and self._watcher.last_run else None
        return {"settings": settings, "presets": config.SMTP_PRESETS, "last_check": last,
                "data_dir": str(config.data_dir()),
                "setup_needed": not (settings["stripe_connected"] and settings["accounts"])}

    def transactions(self):
        txs = db.list_txs()
        paid = [t for t in txs if t["status"] == "paid"]
        waiting = [t for t in txs if t["status"] in ("sent", "pending")]
        return {
            "items": [transaction_view(t) for t in txs],
            "totals": {
                "sent": len(txs), "paid": len(paid),
                "collected": mailer.money(sum(t["amount_paid_cents"] or t["price_cents"] for t in paid)),
                "waiting": mailer.money(sum(t["price_cents"] for t in waiting)),
            },
        }

    # ------------------------------------------------------------ sending
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
                share_link=form.get("share_link", ""),
            )
        except sender.NeedShareLink as exc:
            return _err(exc, need_share_link=True)
        except sender.SendError as exc:
            return _err(exc)
        except Exception as exc:  # never leave the window hanging
            log.exception("Send failed")
            return _err(f"Unexpected error: {exc}")
        tx = db.get_tx(tx_id)
        return _ok(message=f"Sent “{tx['title']}” to {tx['client_email']}. "
                           f"The final PDF is emailed to them automatically once they pay {mailer.money(tx['price_cents'])}.")

    def resend(self, tx_id):
        try:
            sender.resend(int(tx_id))
        except Exception as exc:
            return _err(exc)
        return _ok(message="Sent again.")

    def check_now(self):
        try:
            summary = watcher.check_all(self._notify)
        except Exception as exc:
            return _err(exc)
        if self._watcher:
            self._watcher.last_run = datetime.now().astimezone()
        return _ok(summary=summary)

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
        key = (payload.get("stripe_key") or "").strip()
        if key:
            if not key.startswith(("sk_", "rk_")):
                return _err("That doesn't look like a Stripe secret key (it starts with sk_ or rk_).")
            try:
                payments.check_key(key)
            except Exception as exc:
                return _err(f"Stripe didn't accept that key: {exc}")
            config.set_secret("stripe_secret_key", key)
        for field in ("signature",):
            if field in payload:
                settings[field] = {k: str(v).strip() for k, v in payload[field].items()}
        for field in ("allow_ach", "start_with_windows"):
            if field in payload:
                settings[field] = bool(payload[field])
        if "poll_minutes" in payload:
            settings["poll_seconds"] = max(1, int(payload["poll_minutes"] or 2)) * 60
        config.save_settings(settings)
        if self._watcher:
            self._watcher.trigger()
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
        w.writerow(["Date", "Time", "Project", "Client email", "Client name", "Sent from", "Price",
                    "Current status", "Update"])
        for tx in db.list_txs():
            view = transaction_view(tx)
            for entry in reversed(view["activity"]):
                ts = datetime.fromisoformat(entry["ts"]).astimezone()
                w.writerow([f"{ts:%Y-%m-%d}", f"{ts:%H:%M}", tx["title"], tx["client_email"], tx["client_name"],
                            tx["from_email"], view["price"], view["label"], entry["text"]])
