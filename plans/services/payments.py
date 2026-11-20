"""Stripe Checkout, webhook handling and refunds.

Payment is only ever trusted from a signature-verified webhook, never from the browser redirect.
"""
import json
import logging

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from plans.models import ProcessedStripeEvent, Project
from plans.services import delivery, notify

log = logging.getLogger(__name__)


class WebhookError(Exception):
    pass


def _client():
    if not settings.STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def create_checkout_session(project: Project):
    s = _client()
    rev = project.current_revision
    methods = ["card"]
    if project.allow_ach and project.currency.lower() == "usd":
        methods.append("us_bank_account")
    name = f"{project.title}: engineering plans"
    if project.project_number:
        name = f"{project.project_number} {name}"
    if rev is None:
        raise RuntimeError("Project has no published revision")
    meta = {"project_id": str(project.pk), "revision": str(rev.number), "sha256": rev.sha256}
    session = s.checkout.Session.create(
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": project.currency.lower(),
                "unit_amount": project.price_cents,
                "product_data": {
                    "name": name[:250],
                    "description": f"{rev.page_count} sheet(s), Rev {rev.number}. Instant download after payment.",
                },
            },
        }],
        payment_method_types=methods,
        customer_email=project.client_email,
        client_reference_id=str(project.pk),
        metadata=meta,
        payment_intent_data={"metadata": meta, "description": name[:250]},
        success_url=project.portal_url() + "?paid=1",
        cancel_url=project.portal_url(),
    )
    Project.objects.filter(pk=project.pk).update(stripe_session_id=session.id)
    return session.url


def handle_webhook(payload: bytes, sig_header: str):
    """Verify and process a Stripe webhook. Returns a short status string."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise WebhookError("STRIPE_WEBHOOK_SECRET is not configured")
    try:
        stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise WebhookError(f"Invalid webhook: {exc}") from exc

    event = json.loads(payload)  # signature verified above; plain JSON is easiest to work with
    try:
        with transaction.atomic():
            ProcessedStripeEvent.objects.create(event_id=event["id"], event_type=event["type"])
    except IntegrityError:
        return "duplicate"

    try:
        return _process(event)
    except Exception:
        # Forget the event so Stripe's automatic retry gets processed instead of skipped as a duplicate.
        ProcessedStripeEvent.objects.filter(event_id=event["id"]).delete()
        raise


def _process(event):
    etype = event["type"]
    obj = event["data"]["object"]
    if etype == "charge.refunded":
        return _handle_refund(obj)
    if etype not in {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
    }:
        return "ignored"

    project = _project_for_session(obj)
    if project is None:
        log.warning("Webhook %s for unknown project: %s", etype, obj.get("metadata"))
        return "unknown project"

    if etype == "checkout.session.async_payment_failed":
        if project.status == Project.Status.PAYMENT_PENDING:
            Project.objects.filter(pk=project.pk).update(status=Project.Status.VIEWED)
        project.log("Bank transfer failed. Plans still locked")
        notify.to_engineer("engineer_alert", project, message="The client's bank transfer (ACH) failed. Files remain locked.")
        notify.to_client("payment_failed", project)
        return "async failed"

    if obj.get("payment_status") != "paid":
        # ACH: checkout completed but funds haven't cleared yet.
        Project.objects.filter(pk=project.pk).exclude(status=Project.Status.PAID).update(
            status=Project.Status.PAYMENT_PENDING, stripe_session_id=obj.get("id", "")
        )
        project.log("Client paid by bank transfer; waiting for it to clear (3–5 business days)")
        notify.to_client("payment_pending", project)
        return "pending"

    amount = obj.get("amount_total") or 0
    currency = (obj.get("currency") or "").lower()
    if amount < project.price_cents or currency != project.currency.lower():
        project.log(f"Payment amount mismatch ({amount / 100:,.2f} {currency.upper()}). Plans NOT unlocked")
        notify.to_engineer(
            "engineer_alert", project,
            message=f"Payment amount mismatch: received {amount / 100:.2f} {currency.upper()}, "
            f"expected {project.price} {project.currency.upper()}. Files NOT released; review in Stripe.",
        )
        return "amount mismatch"

    delivery.mark_paid(
        project, session_id=obj.get("id", ""), payment_intent=obj.get("payment_intent") or "", amount_cents=amount
    )
    return "paid"


def _project_for_session(obj):
    pid = (obj.get("metadata") or {}).get("project_id") or obj.get("client_reference_id")
    if not pid:
        return None
    try:
        return Project.objects.get(pk=int(pid))
    except (Project.DoesNotExist, ValueError):
        return None


def _handle_refund(charge):
    """A refund made anywhere (e.g. the Stripe dashboard): lock the project again and record it."""
    pi = charge.get("payment_intent")
    project = Project.objects.filter(stripe_payment_intent=pi).first() if pi else None
    if project is None or project.status == Project.Status.REFUNDED:
        return "refund noted"
    if charge.get("amount_refunded", 0) < charge.get("amount", 0):
        project.log("Partial refund issued; plans stay unlocked")
        notify.to_engineer("engineer_alert", project, message="A partial refund was issued; files remain unlocked.")
        return "partial refund"
    project.status = Project.Status.REFUNDED
    project.refunded_at = timezone.now()
    project.save(update_fields=["status", "refunded_at"])
    project.log("Refunded (from Stripe). Plans locked again")
    return "refunded"


def refund(project: Project, reason=""):
    if not project.stripe_payment_intent:
        raise RuntimeError("No payment intent recorded for this project")
    s = _client()
    s.Refund.create(
        payment_intent=project.stripe_payment_intent,
        metadata={"project_id": str(project.pk), "reason": reason[:450]},
    )
    project.status = Project.Status.REFUNDED
    project.refunded_at = timezone.now()
    project.save(update_fields=["status", "refunded_at"])
    project.log(f"Refunded: {reason}" if reason else "Refunded")
    notify.to_client("refunded", project, reason=reason)
    notify.to_engineer("engineer_alert", project, message=f"Client was refunded. Reason: {reason}")
