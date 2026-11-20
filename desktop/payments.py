"""Stripe Payment Links: create one per send, poll it for payment, switch it off once paid."""
from urllib.parse import quote

import stripe

from desktop import config


class PaymentsError(Exception):
    pass


def is_test_key(key: str) -> bool:
    return key.startswith(("sk_test_", "rk_test_"))


def _stripe():
    key = config.stripe_key()
    if not key:
        raise PaymentsError("Add your Stripe secret key in Settings first.")
    stripe.api_key = key
    stripe.max_network_retries = 2
    return stripe


def _get(obj, name, default=None):
    try:
        value = getattr(obj, name)
    except (AttributeError, KeyError):
        return default
    return default if value is None else value


def confirmation_message(tx) -> str:
    return (f"Payment received, thank you! The final plans for “{tx['title']}” will be emailed to "
            f"{tx['client_email']} shortly (usually within minutes, at most 1 business day). "
            "Questions? Just reply to the email you received.")[:500]


def create_payment_link(tx, allow_ach=True):
    """Create a single-use Stripe Payment Link for this transaction. Returns (link_id, url)."""
    s = _stripe()
    name = f"{tx['title']}: engineering plans"[:250]
    meta = {"plansender_tx": str(tx["id"]), "sha256": tx.get("sha256", "")}
    price = s.Price.create(
        currency=tx["currency"], unit_amount=tx["price_cents"],
        product_data={"name": name, "metadata": meta},
    )
    params = dict(
        line_items=[{"price": price.id, "quantity": 1}],
        restrictions={"completed_sessions": {"limit": 1}},  # can only be paid once
        after_completion={"type": "hosted_confirmation",
                          "hosted_confirmation": {"custom_message": confirmation_message(tx)}},
        metadata=meta,
        payment_intent_data={"metadata": meta, "description": name},
    )
    methods = ["card", "us_bank_account"] if allow_ach and tx["currency"] == "usd" else ["card"]
    try:
        link = s.PaymentLink.create(payment_method_types=methods, **params)
    except stripe.InvalidRequestError:
        if methods == ["card"]:
            raise
        # Bank payments (ACH) not enabled on this Stripe account; fall back to cards only.
        link = s.PaymentLink.create(payment_method_types=["card"], **params)
    return link.id, f"{link.url}?prefilled_email={quote(tx['client_email'])}"


def poll(link_id):
    """Look up the link's checkout sessions.

    Returns {"state": "open"} if nobody has paid, otherwise state is "paid", "pending" (bank transfer
    clearing) or "failed" (bank transfer bounced), with session_id/amount/currency/payment_intent.
    """
    s = _stripe()
    sessions = s.checkout.Session.list(payment_link=link_id, limit=10)
    for sess in sessions.data:  # newest first
        if _get(sess, "status") != "complete":
            continue
        pi = _get(sess, "payment_intent", "")
        pi_id = pi if isinstance(pi, str) else _get(pi, "id", "")
        info = {
            "session_id": sess.id,
            "amount": _get(sess, "amount_total", 0),
            "currency": (_get(sess, "currency", "") or "").lower(),
            "payment_intent": pi_id,
        }
        if _get(sess, "payment_status") in ("paid", "no_payment_required"):
            return {"state": "paid", **info}
        if pi_id:
            intent = s.PaymentIntent.retrieve(pi_id)
            if _get(intent, "status") in ("requires_payment_method", "canceled"):
                return {"state": "failed", **info}
            if _get(intent, "status") == "succeeded":
                return {"state": "paid", **info}
        return {"state": "pending", **info}
    return {"state": "open"}


def refund_status(payment_intent_id) -> str:
    """'refunded', 'partial' or 'none'."""
    if not payment_intent_id:
        return "none"
    s = _stripe()
    intent = s.PaymentIntent.retrieve(payment_intent_id, expand=["latest_charge"])
    charge = _get(intent, "latest_charge")
    if not charge or isinstance(charge, str):
        return "none"
    if _get(charge, "refunded", False):
        return "refunded"
    return "partial" if _get(charge, "amount_refunded", 0) > 0 else "none"


def deactivate(link_id):
    if link_id:
        _stripe().PaymentLink.modify(link_id, active=False)


def check_key(key: str) -> dict:
    """Validate a key before saving it."""
    stripe.api_key = key
    stripe.PaymentLink.list(limit=1)
    return {"ok": True, "test_mode": is_test_key(key)}
