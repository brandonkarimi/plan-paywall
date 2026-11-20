"""Background checker: asks Stripe about every unpaid send, emails the final PDF once paid, notices refunds."""
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from desktop import config, db, mailer, payments

log = logging.getLogger(__name__)
_check_lock = threading.Lock()
REFUND_CHECK_EVERY = timedelta(hours=1)
REFUND_CHECK_WINDOW = timedelta(days=120)


def _noop(title, message):
    pass


def check_all(notify=_noop, settings=None):
    """One full pass. Safe to call from several threads; overlapping calls wait their turn."""
    with _check_lock:
        settings = settings or config.load_settings()
        summary = {"checked": 0, "paid": 0, "delivered": 0, "errors": []}
        for tx in db.list_txs(["sent", "pending"]):
            if not tx["link_id"]:  # Zelle/Venmo-only: nothing to ask Stripe; the engineer marks these paid
                continue
            summary["checked"] += 1
            try:
                _check_payment(tx, notify, settings, summary)
            except Exception as exc:
                log.exception("Payment check failed for %s", tx["id"])
                summary["errors"].append(f"{tx['title']}: {exc}")
        for tx in db.list_txs(["paid"]):
            if not tx["delivered_at"]:
                if deliver(tx, notify, settings):
                    summary["delivered"] += 1
            try:
                _maybe_check_refund(tx, notify)
            except Exception as exc:
                log.warning("Refund check failed for %s: %s", tx["id"], exc)
        return summary


def _check_payment(tx, notify, settings, summary):
    if not config.stripe_key():
        raise payments.PaymentsError("No Stripe key set")
    result = payments.poll(tx["link_id"])
    state = result["state"]
    if state == "open":
        return

    if state == "pending":
        if tx["status"] != "pending":
            db.update_tx(tx["id"], status="pending", session_id=result["session_id"],
                         payment_intent=result["payment_intent"])
            db.log(tx["id"], "Client paid by bank transfer; waiting for it to clear (3–5 business days)")
            notify("Bank transfer started", f"{tx['title']}: clears in 3–5 business days.")
            _best_effort_email(tx, mailer.pending, settings)
        return

    if state == "failed":
        marker = f"failed:{result['session_id']}"
        if tx["flags"].get(marker):
            return
        db.set_flag(tx, marker)
        db.log(tx["id"], "Bank transfer failed. Plans still locked")
        # The old link is used up (single use), so issue a fresh one and email it.
        try:
            payments.deactivate(tx["link_id"])
        except Exception:
            pass
        link_id, link_url = payments.create_payment_link(tx, allow_ach=settings.get("allow_ach", True))
        db.update_tx(tx["id"], status="sent", link_id=link_id, link_url=link_url,
                     old_link_ids=[*tx["old_link_ids"], tx["link_id"]])
        tx = db.get_tx(tx["id"])
        db.log(tx["id"], "New pay link created and emailed to client")
        _best_effort_email(tx, mailer.ach_failed, settings)
        notify("Bank transfer failed", f"{tx['title']}: client was sent a new pay link.")
        return

    # paid
    if result["amount"] < tx["price_cents"] or result["currency"] != tx["currency"]:
        if not tx["flags"].get("mismatch"):
            db.set_flag(tx, "mismatch")
            db.log(tx["id"], f"Payment amount mismatch ({mailer.money(result['amount'])} "
                             f"{result['currency'].upper()}). Plans NOT sent; check Stripe")
            notify("Check this payment", f"{tx['title']}: amount didn't match. Plans not sent.")
        return
    db.update_tx(tx["id"], status="paid", paid_at=db.now(), amount_paid_cents=result["amount"],
                 payment_intent=result["payment_intent"], session_id=result["session_id"], paid_method="Stripe")
    db.log(tx["id"], f"Paid {mailer.money(result['amount'])} by Stripe")
    summary["paid"] += 1
    try:
        payments.deactivate(tx["link_id"])
    except Exception as exc:
        log.warning("Couldn't deactivate link %s: %s", tx["link_id"], exc)
    tx = db.get_tx(tx["id"])
    delivered = deliver(tx, notify, settings)
    if delivered:
        summary["delivered"] += 1
    notify(f"PAID: {tx['title']}", f"{mailer.money(result['amount'])} from {tx['client_email']}. "
           + ("Plans emailed to client." if delivered else "Plans NOT emailed yet; see the app."))


def deliver(tx, notify=_noop, settings=None, force=False):
    """Email the final plans. Returns True on success; failures are logged once and retried next pass."""
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
        message = str(exc)
        if message != tx["delivery_error"]:
            db.log(tx["id"], f"Could not email the final plans: {message}. Will retry automatically")
            notify("Plans not sent", f"{tx['title']}: {message}")
        db.update_tx(tx["id"], delivery_error=message)
        tx["delivery_error"] = message
        return False
    first = not tx["delivered_at"]
    db.update_tx(tx["id"], delivered_at=tx["delivered_at"] or db.now(), delivery_error="")
    db.log(tx["id"], ("Final plans emailed to " if first else "Final plans re-sent to ") + tx["client_email"])
    return True


def _maybe_check_refund(tx, notify):
    now = datetime.now(timezone.utc)
    paid_at = datetime.fromisoformat(tx["paid_at"]) if tx["paid_at"] else now
    if now - paid_at > REFUND_CHECK_WINDOW or not tx["payment_intent"]:
        return
    last = tx["flags"].get("refund_checked_at")
    if last and now - datetime.fromisoformat(last) < REFUND_CHECK_EVERY:
        return
    db.set_flag(tx, "refund_checked_at", now.isoformat(timespec="seconds"))
    status = payments.refund_status(tx["payment_intent"])
    if status == "refunded":
        db.update_tx(tx["id"], status="refunded", refunded_at=db.now())
        db.log(tx["id"], "Refunded in Stripe" + (" (the plans had already been emailed)" if tx["delivered_at"] else ""))
        notify("Refund", f"{tx['title']} was refunded.")
    elif status == "partial" and not tx["flags"].get("partial_refund"):
        db.set_flag(tx, "partial_refund")
        db.log(tx["id"], "Partial refund issued in Stripe")


def _best_effort_email(tx, template, settings):
    try:
        subject, text, html = template(tx, settings)
        mailer.send(tx["from_email"], tx["client_email"], subject, text, html, settings=settings)
    except Exception as exc:
        db.log(tx["id"], f"Couldn't email the client an update: {exc}")


class Watcher(threading.Thread):
    """Runs check_all every poll_seconds; trigger() runs one immediately."""

    def __init__(self, notify=_noop, on_change=None):
        super().__init__(daemon=True, name="stripe-watcher")
        self.notify = notify
        self.on_change = on_change or (lambda: None)
        self._wake = threading.Event()
        self._halt = threading.Event()
        self.last_run = None
        self.last_summary = None

    def trigger(self):
        self._wake.set()

    def stop(self):
        self._halt.set()
        self._wake.set()

    def run(self):
        while not self._halt.is_set():
            try:
                self.last_summary = check_all(self.notify)
                self.last_run = datetime.now(timezone.utc)
                self.on_change()
            except Exception:
                log.exception("Watcher pass failed")
            seconds = max(30, int(config.load_settings().get("poll_seconds", 120)))
            self._wake.wait(seconds)
            self._wake.clear()
